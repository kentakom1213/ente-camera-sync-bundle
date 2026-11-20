#!/usr/bin/env python3
"""Add-only Ente Photos v0.4 downloader, with an isolated SQLite index.

Initialize explicitly before syncing; all sync operations are dry-run unless
--apply is given. The legacy exporter and database are never modified.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime, timezone
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import quote
from zoneinfo import ZoneInfo
import zipfile


CHUNK_SIZE = 4 * 1024 * 1024
VERSION = "1"
MAX_ZIP_BYTES = 16 * 1024**3
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".gif", ".dng", ".tif", ".tiff", ".bmp", ".mp4", ".mov", ".m4v", ".3gp", ".webm", ".mkv", ".avi", ".mts", ".m2ts"}


class SyncError(Exception):
    pass


def digest(path: Path) -> tuple[str, str, os.stat_result]:
    before = path.stat()
    sha, b2 = hashlib.sha256(), hashlib.blake2b(digest_size=64)
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            sha.update(chunk)
            b2.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
            after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev):
        raise SyncError(f"File changed while hashing: {path}")
    return sha.hexdigest(), base64.b64encode(b2.digest()).decode("ascii"), after


def valid_hash(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 64
    except (ValueError, binascii.Error):
        return False


def parts_for(entry: dict) -> list[tuple[str, str]] | None:
    kind, value = entry.get("type"), entry.get("hash")
    if kind not in {"image", "video", "livephoto"} or not isinstance(value, str):
        return None
    if kind == "livephoto":
        hashes = value.split(":")
        return list(zip(("image", "video"), hashes)) if len(hashes) == 2 and all(map(valid_hash, hashes)) else None
    return [("main", value)] if valid_hash(value) else None


def safe_name(value: object, ident: str, kind: str) -> str:
    if not isinstance(value, str):
        value = ""
    value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    value = "".join(c if c.isprintable() and c not in '/\\\x00' else "_" for c in value)
    if value in {"", ".", ".."} or value.startswith("."):
        value = f"ente_{ident}"
    value = value[:180]
    if not Path(value).suffix:
        value += ".mp4" if kind == "video" else ".jpg"
    return value


def destination_dir(root: Path, entry: dict, tz: ZoneInfo) -> Path:
    created = entry.get("createdAt")
    if not isinstance(created, str):
        return root / "unknown-date"
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("naive timestamp")
        local = dt.astimezone(tz)
        if not (1900 <= local.year <= 9999):
            raise ValueError("invalid year")
    except ValueError:
        return root / "unknown-date"
    return root / f"{local.year:04d}" / f"{local.month:02d}"


def safe_dirs(root: Path, destination: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise SyncError(f"Camera root must be an existing real directory: {root}")
    try:
        segments = destination.relative_to(root).parts
    except ValueError as exc:
        raise SyncError("Destination escapes Camera root") from exc
    current = root
    for segment in segments:
        current = current / segment
        if current.is_symlink():
            raise SyncError(f"Refusing symlinked directory: {current}")
        current.mkdir(mode=0o755, exist_ok=True)
        if not current.is_dir():
            raise SyncError(f"Not a directory: {current}")


def iter_camera(root: Path):
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".ente-camera-") and
                         not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            if name.endswith(".part") or ".part." in name:
                continue
            path = Path(folder) / name
            if not path.is_symlink() and path.is_file():
                yield path


def read_legacy(path: Path) -> dict[str, tuple[int, int, str]]:
    if not path.is_file():
        raise SyncError(f"Legacy database does not exist: {path}")
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise SyncError("Legacy database integrity check failed")
        rows = db.execute("SELECT path,size,mtime_ns,sha256 FROM file_hashes WHERE role='camera'")
        return {str(name): (int(size), int(mtime), str(sha)) for name, size, mtime, sha in rows}


def create_schema(db: sqlite3.Connection, root: Path, account: str) -> None:
    db.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE local_files(
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            blake2b TEXT NOT NULL,
            verified_at TEXT NOT NULL
        );
        CREATE INDEX local_hash_idx ON local_files(blake2b);
        CREATE TABLE ente_files(
            account TEXT NOT NULL,
            file_id TEXT NOT NULL,
            name TEXT,
            type TEXT,
            hash TEXT,
            created_at TEXT,
            updated_at TEXT,
            album_ids TEXT,
            present INTEGER NOT NULL DEFAULT 1,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(account,file_id)
        );
        CREATE TABLE ente_file_parts(
            account TEXT NOT NULL,
            file_id TEXT NOT NULL,
            part TEXT NOT NULL,
            hash TEXT,
            local_path TEXT,
            status TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY(account,file_id,part)
        );
        CREATE TABLE sync_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            listed INTEGER,
            downloaded INTEGER,
            errors INTEGER
        );
    """)
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("schema_version", VERSION), ("camera_root", str(root)), ("account", account)])
    db.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_local(db: sqlite3.Connection, path: Path, sha: str, b2: str, stat: os.stat_result) -> None:
    db.execute("""INSERT INTO local_files VALUES (?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
        sha256=excluded.sha256, blake2b=excluded.blake2b, verified_at=excluded.verified_at""",
        (str(path), stat.st_size, stat.st_mtime_ns, sha, b2, utc_now()))


def init_db(args: argparse.Namespace, root: Path) -> None:
    dest = args.db.expanduser().resolve()
    if dest.exists() or dest.is_symlink():
        raise SyncError(f"Refusing to overwrite existing database: {dest}")
    legacy = read_legacy(args.legacy_db.expanduser())
    if not legacy:
        raise SyncError("Legacy DB contains no Camera entries")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dest.parent.is_symlink():
        raise SyncError("State directory must not be symlinked")
    fd, name = tempfile.mkstemp(prefix=".ente-v04-init-", suffix=".sqlite3", dir=dest.parent)
    os.close(fd)
    staging = Path(name)
    try:
        with closing(sqlite3.connect(staging)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            create_schema(db, root, args.account)
            seen = set()
            for index, path in enumerate(iter_camera(root), start=1):
                row = legacy.get(str(path))
                if row is None:
                    raise SyncError(f"Unindexed local file; rerun the audit: {path}")
                sha, b2, stat = digest(path)
                if (stat.st_size, stat.st_mtime_ns, sha) != row:
                    raise SyncError(f"Legacy Camera record differs from file: {path}")
                seen.add(str(path))
                store_local(db, path, sha, b2, stat)
                if index % 1000 == 0:
                    print(f"Indexed {index} Camera files ...", file=sys.stderr)
            missing = set(legacy).difference(seen)
            if missing:
                raise SyncError(f"Legacy Camera index has {len(missing)} missing entries; first: {next(iter(missing))}")
            db.commit()
        # Atomically create the state path; never overwrite a legacy or v0.4 DB.
        os.link(staging, dest)
        print(f"Initialized {dest}: {len(seen)} verified Camera files, account={args.account}")
    finally:
        staging.unlink(missing_ok=True)


def open_db(path: Path, root: Path, account: str, readonly: bool) -> sqlite3.Connection:
    path = path.expanduser().resolve(strict=True)
    if readonly:
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        db.execute("PRAGMA query_only=ON")
    else:
        db = sqlite3.connect(path)
        db.execute("PRAGMA synchronous=FULL")
    try:
        settings = dict(db.execute("SELECT key,value FROM meta"))
        if settings.get("schema_version") != VERSION:
            raise SyncError("Unsupported SQLite schema version")
        if settings.get("camera_root") != str(root):
            raise SyncError(f"Camera path differs from initialized DB: {settings.get('camera_root')}")
        if settings.get("account") != account:
            raise SyncError(f"Account differs from initialized DB: {settings.get('account')}")
        return db
    except Exception:
        db.close()
        raise


def read_items(args: argparse.Namespace) -> list[dict]:
    if args.metadata_json:
        payload = args.metadata_json.read_text(encoding="utf-8")
    else:
        cmd = [args.ente_bin, "photos", "--account", args.account,
               "file", "list", "--all", "--json", "--no-input"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode:
            raise SyncError(f"Ente list failed ({result.returncode}): {result.stderr.strip()[:600]}")
        payload = result.stdout
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Invalid Ente JSON: {exc}") from exc
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise SyncError("Ente file list must be an array of objects")
    seen = set()
    for entry in entries:
        ident = entry.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9]+", ident) or ident in seen:
            raise SyncError(f"Invalid or duplicate Ente file ID: {ident}")
        seen.add(ident)
    return entries


def indexed_paths(db: sqlite3.Connection, root: Path, apply: bool) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for path_s, size, mtime, previous_sha, previous_b2 in db.execute(
            "SELECT path,size,mtime_ns,sha256,blake2b FROM local_files"):
        path = Path(path_s)
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file() or \
                not path.resolve().is_relative_to(root):
            if apply:
                db.execute("DELETE FROM local_files WHERE path=?", (path_s,))
            continue
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) == (size, mtime):
            b2 = previous_b2
        else:
            sha, b2, stat = digest(path)
            if apply:
                store_local(db, path, sha, b2, stat)
            print(f"Rehashed changed file: {path}", file=sys.stderr)
        result.setdefault(b2, []).append(path)
    return result


def store_snapshot(db: sqlite3.Connection, entries: list[dict], account: str) -> None:
    now = utc_now()
    db.execute("UPDATE ente_files SET present=0 WHERE account=?", (account,))
    for entry in entries:
        db.execute("""INSERT INTO ente_files
            (account,file_id,name,type,hash,created_at,updated_at,album_ids,present,last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,1,?) ON CONFLICT(account,file_id) DO UPDATE SET
            name=excluded.name, type=excluded.type, hash=excluded.hash,
            created_at=excluded.created_at, updated_at=excluded.updated_at,
            album_ids=excluded.album_ids, present=1, last_seen_at=excluded.last_seen_at""",
            (account, entry["id"], entry.get("name"), entry.get("type"),
             entry.get("hash"), entry.get("createdAt"), entry.get("updatedAt"),
             json.dumps(entry.get("albumIds", [])), now))


def store_parts(db: sqlite3.Connection, account: str, ident: str,
                parts: list[tuple[str, str]], known: dict[str, list[Path]]) -> None:
    for part, hash_value in parts:
        path = next(iter(known.get(hash_value, [])), None)
        db.execute("""INSERT INTO ente_file_parts VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(account,file_id,part) DO UPDATE SET hash=excluded.hash,
            local_path=excluded.local_path, status=excluded.status,
            verified_at=excluded.verified_at""",
            (account, ident, part, hash_value, str(path) if path else None,
             "matched" if path else "missing", utc_now() if path else None))


def unique_destination(folder: Path, name: str, ident: str, hash_value: str) -> tuple[Path, bool]:
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(1000):
        candidate = folder / (name if n == 0 else
                              f"{stem}__ente_{ident}{'_' + str(n) if n > 1 else ''}{suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate, False
        if candidate.is_symlink() or not candidate.is_file():
            continue
        _, actual_hash, _ = digest(candidate)
        if actual_hash == hash_value:
            return candidate, True
    raise SyncError(f"Too many filename collisions for {name}")


def publish(staged: Path, folder: Path, name: str, ident: str, expected_hash: str,
            db: sqlite3.Connection, known: dict[str, list[Path]]) -> Path:
    sha, b2, _ = digest(staged)
    if b2 != expected_hash:
        raise SyncError(f"Downloaded file failed BLAKE2b verification for Ente ID {ident}")
    with staged.open("rb") as stream:
        os.fsync(stream.fileno())
    final, exists = unique_destination(folder, name, ident, expected_hash)
    if not exists:
        try:
            os.link(staged, final, follow_symlinks=False)  # atomic; fails on collision
        except FileExistsError as exc:
            raise SyncError(f"Target created concurrently; retry: {final}") from exc
        # Make the directory entry durable before committing the SQLite reference.
        dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    stat = final.stat()
    # The published bytes were checked immediately before linking; no overwrite possible.
    store_local(db, final, sha, b2, stat)
    db.commit()
    if final not in known.setdefault(b2, []):
        known[b2].append(final)
    return final


def download_one(args: argparse.Namespace, db: sqlite3.Connection, root: Path,
                 entry: dict, parts: list[tuple[str, str]],
                 known: dict[str, list[Path]], tz: ZoneInfo) -> list[Path]:
    ident = entry["id"]
    kind = entry["type"]
    folder = destination_dir(root, entry, tz)
    safe_dirs(root, folder)
    name = safe_name(entry.get("name"), ident, kind)
    recorded = []
    with tempfile.TemporaryDirectory(prefix=".ente-camera-v04-", dir=folder) as tmp:
        tempdir = Path(tmp)
        archive = tempdir / "download"
        cmd = [args.ente_bin, "photos", "--account", args.account, "file", "download",
               ident, "--output", str(archive), "--json", "--no-input"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode or not archive.is_file() or archive.is_symlink():
            raise SyncError(f"Ente download failed for {ident} ({result.returncode}): {result.stderr.strip()[:500]}")
        if kind != "livephoto":
            _, expected_hash = parts[0]
            recorded.append(publish(archive, folder, name, ident, expected_hash, db, known))
        else:
            # Live Photos are ZIP files, not the two component files.
            try:
                with zipfile.ZipFile(archive) as zf:
                    members = [m for m in zf.infolist() if not m.is_dir()]
                    if not 2 <= len(members) <= 8 or sum(m.file_size for m in members) > MAX_ZIP_BYTES:
                        raise SyncError(f"Unexpected Live Photo ZIP size/entry count for {ident}")
                    if any(m.flag_bits & 1 or m.file_size > MAX_ZIP_BYTES or
                           (m.external_attr >> 16) & 0o170000 not in (0, 0o100000)
                           for m in members):
                        raise SyncError(f"Unsafe Live Photo ZIP member for {ident}")
                    targets = dict(parts)
                    selected: dict[str, tuple[Path, str]] = {}
                    for n, member in enumerate(members):
                        data = tempdir / f"part-{n}"
                        # Never use untrusted archive names as filesystem paths.
                        with zf.open(member) as src, data.open("xb") as out:
                            while chunk := src.read(CHUNK_SIZE):
                                out.write(chunk)
                        _, actual_hash, _ = digest(data)
                        matches = [p for p, h in parts if h == actual_hash and p not in selected]
                        if matches:
                            selected[matches[0]] = (data, Path(member.filename).suffix.lower())
                    if set(selected) != set(targets):
                        raise SyncError(f"Live Photo ZIP components do not match Ente hashes: {ident}")
                    for part, expected_hash in parts:
                        if known.get(expected_hash):
                            recorded.append(known[expected_hash][0])
                            continue
                        staged, suffix = selected[part]
                        suffix = suffix if suffix in MEDIA_SUFFIXES else (".heic" if part == "image" else ".mov")
                        part_name = f"{Path(name).stem}{suffix}"
                        recorded.append(publish(staged, folder, part_name, ident, expected_hash, db, known))
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                raise SyncError(f"Invalid Live Photo ZIP for {ident}: {exc}") from exc
    store_parts(db, args.account, ident, parts, known)
    db.commit()
    return recorded


def sync(args: argparse.Namespace, root: Path) -> int:
    db = open_db(args.db, root, args.account, readonly=not args.apply)
    try:
        entries = read_items(args)  # Failure must never mark a snapshot complete.
        prior = db.execute("SELECT COUNT(*) FROM ente_files WHERE account=? AND present=1",
                           (args.account,)).fetchone()[0]
        if not entries and prior:
            raise SyncError("Refusing an empty Ente listing for a previously nonempty account")
        tz = ZoneInfo(args.timezone)
        known = indexed_paths(db, root, args.apply)
        if args.apply:
            with db:
                store_snapshot(db, entries, args.account)
                db.execute("INSERT INTO sync_runs(started_at,listed,downloaded,errors) VALUES (?,?,0,0)",
                           (utc_now(), len(entries)))
                run_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        summary = {"listed": len(entries), "matched": 0, "missing": 0, "downloaded": 0,
                   "no_hash": 0, "unsupported": 0, "errors": 0, "partial_livephoto": 0}
        for entry in entries:
            parts = parts_for(entry)
            if parts is None:
                key = "no_hash" if not entry.get("hash") else "unsupported"
                summary[key] += 1
                continue
            hits = [bool(known.get(h)) for _, h in parts]
            if all(hits):
                summary["matched"] += 1
                if args.apply:
                    store_parts(db, args.account, entry["id"], parts, known)
                continue
            summary["missing"] += 1
            if any(hits):
                summary["partial_livephoto"] += 1
            print(f"MISSING {entry['id']} {entry.get('name')} (type={entry.get('type')})")
            if not args.apply or (args.max_downloads and summary["downloaded"] >= args.max_downloads):
                if args.apply:
                    store_parts(db, args.account, entry["id"], parts, known)
                continue
            try:
                written = download_one(args, db, root, entry, parts, known, tz)
                summary["downloaded"] += 1
                print(f"  SAVED {', '.join(map(str, written))}")
            except (SyncError, OSError, sqlite3.Error) as exc:
                summary["errors"] += 1
                print(f"  ERROR {entry['id']}: {exc}", file=sys.stderr)
                store_parts(db, args.account, entry["id"], parts, known)
        if args.apply:
            db.execute("UPDATE sync_runs SET finished_at=?,downloaded=?,errors=? WHERE id=?",
                       (utc_now(), summary["downloaded"], summary["errors"], run_id))
            db.commit()
        print("Ente Camera sync v0.4:")
        for key, value in summary.items():
            print(f"  {key}: {value}")
        print("Apply completed." if args.apply else "Dry-run: Camera and SQLite not modified.")
        return 1 if summary["errors"] else 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--camera-root", type=Path, default=Path.home() / "Pictures/Camera")
    p.add_argument("--db", type=Path, default=Path.home() / ".local/state/ente-camera-sync/state-v04.sqlite3")
    p.add_argument("--account", required=True, help="Fixed Ente CLI account name used for all operations")
    p.add_argument("--ente-bin", default="ente")
    p.add_argument("--timezone", default="Asia/Tokyo")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Verify local files and create a NEW v0.4 index")
    init.add_argument("--legacy-db", type=Path, required=True)
    s = commands.add_parser("sync", help="Preview or apply only missing downloads")
    flag = s.add_mutually_exclusive_group()
    flag.add_argument("--apply", action="store_true", help="Download missing files and update the NEW index")
    flag.add_argument("--dry-run", action="store_true", help="Explicit preview (default)")
    s.add_argument("--max-downloads", type=int, default=0, help="Maximum Ente IDs to download (0=unlimited)")
    s.add_argument("--metadata-json", type=Path, help="Use previously saved list JSON for testing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "max_downloads", 0) < 0:
        print("--max-downloads cannot be negative", file=sys.stderr)
        return 2
    if not args.account.strip():
        print("Account name cannot be empty", file=sys.stderr)
        return 2
    try:
        root = args.camera_root.expanduser().resolve(strict=True)
        if not root.is_dir() or args.camera_root.expanduser().is_symlink():
            raise SyncError("Camera root must be an existing real directory")
        state_dir = args.db.expanduser().parent
        if args.command == "init":
            init_db(args, root)
            return 0
        if not args.db.expanduser().is_file():
            raise SyncError("v0.4 index does not exist; run init first")
        if not args.apply:
            return sync(args, root)
        state_dir.mkdir(mode=0o700, exist_ok=True, parents=True)
        lock_path = state_dir / ".ente-v04.lock"
        if lock_path.is_symlink():
            raise SyncError("Refusing symlinked lock file")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SyncError("A v0.4 sync is already running") from exc
            return sync(args, root)
    except (SyncError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())