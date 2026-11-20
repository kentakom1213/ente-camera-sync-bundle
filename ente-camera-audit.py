#!/usr/bin/env python3
"""Read-only migration audit: reconcile legacy Camera files with Ente Photos.

This command never downloads media, modifies Camera, or updates the legacy DB.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import quote


CHUNK_SIZE = 4 * 1024 * 1024


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--camera-root", type=Path, default=Path.home() / "Pictures/Camera")
    p.add_argument("--legacy-db", type=Path, required=True,
                   help="Restored legacy state.sqlite3 (opened read-only)")
    p.add_argument("--ente-bin", default="ente")
    p.add_argument("--account", help="Ente CLI account selector (optional)")
    p.add_argument("--metadata-json", type=Path,
                   help="Use a saved 'ente photos file list --all --json' response instead of Ente CLI")
    p.add_argument("--json", action="store_true", help="Output structured audit report to stdout")
    return p


def read_ente_items(args: argparse.Namespace) -> list[dict]:
    if args.metadata_json is not None:
        payload = args.metadata_json.read_text(encoding="utf-8")
    else:
        command = [args.ente_bin, "photos"]
        if args.account:
            command.extend(["--account", args.account])
        command.extend(["file", "list", "--all", "--json", "--no-input"])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Ente file list failed (exit {result.returncode}): "
                               f"{result.stderr.strip()[:1000]}")
        payload = result.stdout
    records = json.loads(payload)
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("Ente list JSON must be an array of objects")
    return records


def load_legacy(db_path: Path) -> dict[str, tuple[int, int, str]]:
    if not db_path.is_file():
        raise FileNotFoundError(f"Legacy DB not found: {db_path}")
    # mode=ro ensures the original database is not updated by the audit.
    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        check = db.execute("PRAGMA quick_check").fetchone()
        if check != ("ok",):
            raise ValueError(f"Legacy database failed integrity check: {check}")
        rows = db.execute(
            "SELECT path, size, mtime_ns, sha256 FROM file_hashes WHERE role='camera'"
        ).fetchall()
    return {str(path): (int(size), int(mtime_ns), str(digest))
            for path, size, mtime_ns, digest in rows}


def camera_paths(root: Path):
    for folder, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".ente-camera-sync-")
                             and not (Path(folder) / d).is_symlink())
        for filename in sorted(filenames):
            if filename.endswith(".part") or ".part." in filename:
                continue
            path = Path(folder) / filename
            if not path.is_symlink() and path.is_file():
                yield path


def digest_file(path: Path) -> tuple[str, str]:
    sha = hashlib.sha256()
    blake = hashlib.blake2b(digest_size=64)
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            sha.update(chunk)
            blake.update(chunk)
    return sha.hexdigest(), base64.b64encode(blake.digest()).decode("ascii")


def scan_camera(root: Path, legacy: dict[str, tuple[int, int, str]]) -> tuple[dict[str, list[str]], dict]:
    known: dict[str, list[str]] = {}
    encountered = set()
    errors = []
    stats = {"local_scanned": 0, "legacy_camera_records": len(legacy),
             "legacy_missing": 0, "local_unindexed": 0,
             "local_content_changed": 0, "local_metadata_changed": 0,
             "local_unreadable": 0}
    for path in camera_paths(root):
        path_str = str(path)
        encountered.add(path_str)
        stats["local_scanned"] += 1
        if stats["local_scanned"] % 1000 == 0:
            print(f"Hashed {stats['local_scanned']} local files ...", file=sys.stderr, flush=True)
        row = legacy.get(path_str)
        if row is None:
            stats["local_unindexed"] += 1
        try:
            before = path.stat()
            sha256, blake2b = digest_file(path)
            after = path.stat()
            # Do not trust a hash taken while a file was changing.
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                raise OSError("file changed during hashing")
        except OSError as exc:
            stats["local_unreadable"] += 1
            errors.append(f"{path_str}: {exc}")
            continue
        if row is not None:
            if (row[0], row[1]) != (after.st_size, after.st_mtime_ns):
                stats["local_metadata_changed"] += 1
            if row[2].lower() != sha256:
                stats["local_content_changed"] += 1
        known.setdefault(blake2b, []).append(path_str)
    # Legacy records may include paths from a different Camera root. Reject those
    # separately to avoid reporting unrelated historical paths as local missing.
    prefix = str(root) + os.sep
    tracked_here = (path for path in legacy if path.startswith(prefix))
    stats["legacy_missing"] = sum(1 for path in tracked_here if path not in encountered)
    return known, {**stats, "scan_errors": errors}


def is_blake2b(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 64
    except (ValueError, binascii.Error):
        return False


def classify(record: dict, known: dict[str, list[str]]) -> tuple[str, list[str]]:
    kind = record.get("type")
    if kind not in ("image", "video", "livephoto"):
        return "unsupported_type", []
    h = record.get("hash")
    if not h:
        return "no_hash", []
    if not isinstance(h, str):
        return "unsupported_hash", []
    if kind == "livephoto":
        parts = h.split(":")
        if len(parts) != 2 or not all(is_blake2b(part) for part in parts):
            return "unsupported_hash", []
        image_matches, video_matches = (known.get(part, []) for part in parts)
        if image_matches and video_matches:
            return "matched", [image_matches[0], video_matches[0]]
        if image_matches or video_matches:
            return "partial_livephoto", [*(image_matches[:1]), *(video_matches[:1])]
        return "missing", []
    if not is_blake2b(h):
        return "unsupported_hash", []
    paths = known.get(h, [])
    return ("matched", paths[:1]) if paths else ("missing", [])


def audit(args: argparse.Namespace) -> dict:
    camera_root = args.camera_root.expanduser().resolve(strict=True)
    if not camera_root.is_dir() or args.camera_root.is_symlink():
        raise ValueError(f"Camera root must be a real directory: {camera_root}")
    entries = read_ente_items(args)
    legacy = load_legacy(args.legacy_db.expanduser())
    known, local = scan_camera(camera_root, legacy)

    summary = {"ente_total": 0, "matched": 0, "missing": 0,
               "no_hash": 0, "unsupported_hash": 0,
               "unsupported_type": 0, "partial_livephoto": 0,
               "duplicate_ente_content": 0, **{k: v for k, v in local.items()
                                                if k != "scan_errors"}}
    results = []
    seen_ids = set()
    seen_hashes = set()
    for entry in entries:
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            raise ValueError("Ente JSON includes a missing/non-string file ID")
        if ident in seen_ids:
            raise ValueError(f"Ente JSON repeats file ID: {ident}")
        seen_ids.add(ident)
        status, paths = classify(entry, known)
        summary[status] += 1
        summary["ente_total"] += 1
        kind, h = entry.get("type"), entry.get("hash")
        if status not in {"no_hash", "unsupported_hash", "unsupported_type"}:
            key = (kind, h)
            if key in seen_hashes:
                summary["duplicate_ente_content"] += 1
            seen_hashes.add(key)
        results.append({"id": ident, "name": entry.get("name"), "type": kind,
                        "status": status, "local_paths": paths})
    return {"summary": summary, "scan_errors": local["scan_errors"],
            "files": results,
            "notice": "Read-only audit; missing entries are candidates, not confirmed loss. "
                      "A local scan error makes matching incomplete. No media was downloaded."}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = audit(args)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Ente Camera migration audit (read-only)")
        for key, value in report["summary"].items():
            print(f"  {key}: {value}")
        pending = [row for row in report["files"] if row["status"] != "matched"]
        for entry in pending[:20]:
            print(f"  {entry['status']}: {entry['id']} {entry['name']}")
        if len(pending) > 20:
            print(f"  ... {len(pending) - 20} additional non-matched entries (use --json)")
        print(report["notice"])
    return 2 if report["scan_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
