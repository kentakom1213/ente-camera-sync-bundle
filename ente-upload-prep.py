#!/usr/bin/env python3
"""Review local-only media and prepare safe, manual Ente Desktop upload batches.

No command uploads to Ente, deletes photos, or writes the Camera sync database.
"""

import argparse
import base64
import binascii
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import fcntl
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from urllib.parse import quote

IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".gif",
          ".dng", ".tif", ".tiff", ".bmp", ".cr2", ".cr3", ".nef", ".arw", ".raf"}
VIDEOS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".webm"}
LIVE_IMAGES = {".jpg", ".jpeg", ".heic", ".heif"}
HIDDEN_EXCLUDES = {".cache", ".thumbnails", ".trash", ".trash-1000"}
CHUNK = 1024 * 1024
FICLONE = 0x40049409  # Linux ioctl: clone data without sharing writable inodes.


class PrepError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def valid_hash(value):
    if not isinstance(value, str):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 64
    except (ValueError, binascii.Error):
        return False


def remote_parts(entry):
    value = entry.get("hash")
    kind = entry.get("type")
    if kind not in {"image", "video", "livephoto"}:
        return []
    if kind == "livephoto":
        hashes = value.split(":") if isinstance(value, str) else []
        if len(hashes) != 2 or not all(valid_hash(h) for h in hashes):
            raise PrepError(f"Missing/invalid Live Photo hash for Ente ID {entry.get('id')}")
        return hashes
    if not valid_hash(value):
        raise PrepError(f"Missing/invalid hash for Ente ID {entry.get('id')}")
    return [value]


def fetch_remote(args):
    import subprocess
    command = [args.ente_bin, "photos", "--account", args.account,
               "file", "list", "--all", "--json", "--no-input"]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise PrepError(f"Ente listing failed ({result.returncode}): {result.stderr.strip()[:400]}")
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PrepError("Ente returned invalid JSON") from exc
    if not isinstance(entries, list):
        raise PrepError("Ente listing is not an array")
    validate_remote(entries)
    return entries


def validate_remote(entries):
    if not isinstance(entries, list):
        raise PrepError("Ente listing is not an array")
    ids = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or \
                not re.fullmatch(r"[0-9]+", entry["id"]) or entry["id"] in ids:
            raise PrepError("Ente listing has invalid or duplicate file IDs")
        ids.add(entry["id"])
        remote_parts(entry)


def init_db(conn, account):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scan_files(
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            ctime_ns INTEGER NOT NULL, dev INTEGER NOT NULL, inode INTEGER NOT NULL,
            sha256 TEXT NOT NULL, blake2b TEXT NOT NULL, checked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ente_history(
            file_id TEXT NOT NULL, hash TEXT NOT NULL, name TEXT, kind TEXT, owner_id TEXT,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            present INTEGER NOT NULL DEFAULT 0, origin TEXT NOT NULL,
            PRIMARY KEY(file_id, hash)
        );
        CREATE INDEX IF NOT EXISTS history_hash_idx ON ente_history(hash);
    """)
    schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if schema and schema[0] != "1":
        raise PrepError("Unsupported upload preparation database schema")
    if not schema:
        conn.execute("INSERT INTO meta VALUES ('schema_version','1')")
    saved = conn.execute("SELECT value FROM meta WHERE key='account'").fetchone()
    if saved and saved[0] != account:
        raise PrepError(f"Upload index is for Ente account {saved[0]!r}, not {account!r}")
    if not saved:
        conn.execute("INSERT INTO meta VALUES ('account', ?)", (account,))
    conn.commit()

def readonly_connection(path):
    uri = f"file:{quote(str(path.resolve(strict=True)), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def import_download_history(db, download_db, account):
    if not download_db.is_file() or download_db.is_symlink():
        return
    with closing(readonly_connection(download_db)) as source:
        settings = dict(source.execute("SELECT key,value FROM meta"))
        if settings.get("account") != account:
            raise PrepError("Camera download database belongs to another account")
        records = source.execute("SELECT file_id, hash, name, type, last_seen_at "
                                 "FROM ente_files WHERE account=?", (account,))
        for file_id, digest, name, kind, seen in records:
            if not digest:
                continue
            parts = remote_parts({"id": file_id, "hash": digest, "type": kind})
            if not parts:
                continue
            db.execute("""INSERT OR IGNORE INTO ente_history
                (file_id,hash,name,kind,owner_id,first_seen_at,last_seen_at,present,origin)
                VALUES(?,?,?,?,?,?,?,0,'camera_db')""",
                       (file_id, digest, name, kind, None, seen or now(), seen or now()))
    db.commit()


def refresh_history(db, args):
    entries = fetch_remote(args)
    validate_remote(entries)
    old_count = db.execute("SELECT value FROM meta WHERE key='last_remote_count'").fetchone()
    old_count = int(old_count[0]) if old_count else 0
    if old_count and not entries and not args.allow_empty_remote:
        raise PrepError("Ente listing is empty after a nonempty snapshot; rerun with --allow-empty-remote only if expected")
    # A sudden collapse is more likely a partial listing/auth problem than a deliberate deletion.
    if old_count >= 20 and len(entries) < old_count // 2 and not args.allow_large_drop:
        raise PrepError("Ente listing unexpectedly shrank by >50%; review and pass --allow-large-drop if expected")
    timestamp = now()
    with db:
        db.execute("UPDATE ente_history SET present=0")
        for item in entries:
            hashes = item["hash"]  # retain the full composite Live Photo hash
            db.execute("""INSERT INTO ente_history
                (file_id,hash,name,kind,owner_id,first_seen_at,last_seen_at,present,origin)
                VALUES(?,?,?,?,?,?,?,1,'cli')
                ON CONFLICT(file_id,hash) DO UPDATE SET
                    name=excluded.name,kind=excluded.kind,owner_id=excluded.owner_id,
                    last_seen_at=excluded.last_seen_at,present=1""",
                (item["id"], hashes, item.get("name"), item.get("type"),
                 item.get("ownerId"), timestamp, timestamp))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_remote_count',?)", (str(len(entries)),))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_remote_at',?)", (timestamp,))
    return entries, timestamp



class HashProgress:
    """Terminal-only progress for scanning media, without external dependencies."""

    def __init__(self, total):
        self.total = total
        self.done = 0
        self.cached = 0
        self.bytes_read = 0
        self.current_size = 0
        self.current_read = 0
        self.tty = sys.stderr.isatty()
        self.last_update = 0.0
        self.last_width = 0

    def begin(self, size):
        self.current_size = size
        self.current_read = 0

    def read(self, size):
        self.bytes_read += size
        self.current_read += size
        self.render()

    def finish(self, cached):
        self.done += 1
        self.cached += int(cached)
        self.current_size = 0
        self.current_read = 0
        self.render()

    def render(self):
        if not self.tty:
            return
        current_time = time.monotonic()
        if self.done < self.total and current_time - self.last_update < 0.1:
            return
        self.last_update = current_time
        fraction = 0 if not self.total else (
            self.done + min(1.0, self.current_read / self.current_size)
            if self.current_size else self.done) / self.total
        width = 20
        filled = int(width * fraction)
        line = (f"Hash scan [{'#' * filled}{'-' * (width - filled)}] "
                f"{self.done}/{self.total} files, "
                f"{self.bytes_read / (1024 ** 3):.2f} GiB read, "
                f"{self.cached} cached")
        # Erase any remaining text from a previously longer progress line.
        sys.stderr.write("\r" + line + " " * max(0, self.last_width - len(line)))
        sys.stderr.flush()
        self.last_width = len(line)

    def close(self):
        if self.tty:
            self.render()
            sys.stderr.write("\n")
            sys.stderr.flush()
        else:
            print(f"Hash scan: {self.done}/{self.total} files, "
                  f"{self.bytes_read / (1024 ** 3):.2f} GiB read, "
                  f"{self.cached} cached", file=sys.stderr)


def hash_file(path, on_bytes=None):
    first = path.lstat()
    sha, b2 = hashlib.sha256(), hashlib.blake2b(digest_size=64)
    with path.open("rb") as file:
        while buf := file.read(CHUNK):
            sha.update(buf)
            b2.update(buf)
            if on_bytes is not None:
                on_bytes(len(buf))
    second = path.lstat()
    attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if path.is_symlink() or any(getattr(first, a) != getattr(second, a) for a in attrs):
        raise PrepError(f"File changed while hashing: {path}")
    return sha.hexdigest(), base64.b64encode(b2.digest()).decode(), second


def scan_paths(root, excludes):
    for folder, directories, files in os.walk(root, followlinks=False):
        base = Path(folder)
        def include(p):
            rel = p.relative_to(root).as_posix()
            return not any(fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel + "/", pattern)
                           for pattern in excludes)
        directories[:] = sorted(d for d in directories if not d.startswith(".") and
                                d.lower() not in HIDDEN_EXCLUDES and
                                not (base / d).is_symlink() and include(base / d))
        for name in sorted(files):
            path = base / name
            if name.startswith(".") or path.is_symlink() or not path.is_file() or not include(path):
                continue
            ext = path.suffix.lower()
            if ext in IMAGES or ext in VIDEOS:
                yield path, "image" if ext in IMAGES else "video"


def indexed_camera(db_path, account):
    if not db_path.is_file() or db_path.is_symlink():
        return {}
    with closing(readonly_connection(db_path)) as source:
        settings = dict(source.execute("SELECT key,value FROM meta"))
        if settings.get("account") != account:
            raise PrepError("Camera download database belongs to another account")
        return {p: (s, m, sha, h) for p, s, m, sha, h in source.execute(
            "SELECT path,size,mtime_ns,sha256,blake2b FROM local_files")}


def scan(db, args, root, current, timestamp):
    current_by_hash = defaultdict(list)
    for item in current:
        for part in remote_parts(item):
            current_by_hash[part].append(item)
    history_ids = defaultdict(set)
    for file_id, hash_text in db.execute("SELECT file_id,hash FROM ente_history"):
        for value in hash_text.split(":"):
            if valid_hash(value):
                history_ids[value].add(file_id)
    camera = indexed_camera(args.camera_db, args.account)
    rows = []
    files = list(scan_paths(root, args.exclude))
    progress = HashProgress(len(files))
    try:
        for path, kind in files:
            stat = path.lstat()
            cache = db.execute("SELECT size,mtime_ns,ctime_ns,dev,inode,sha256,blake2b "
                               "FROM scan_files WHERE path=?", (str(path),)).fetchone()
            unchanged = cache and (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                                   stat.st_dev, stat.st_ino) == tuple(cache[:5])
            cached = True
            if unchanged and not args.rehash:
                sha, digest = cache[5:7]
            elif not args.rehash and str(path) in camera and \
                    (stat.st_size, stat.st_mtime_ns) == camera[str(path)][:2]:
                sha, digest = camera[str(path)][2:4]
            else:
                cached = False
                progress.begin(stat.st_size)
                sha, digest, stat = hash_file(path, on_bytes=progress.read)
            db.execute("""INSERT INTO scan_files VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                ctime_ns=excluded.ctime_ns,dev=excluded.dev,inode=excluded.inode,
                sha256=excluded.sha256,blake2b=excluded.blake2b,checked_at=excluded.checked_at""",
                (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev,
                 stat.st_ino, sha, digest, timestamp))
            matches = current_by_hash.get(digest, [])
            exact_name = any(item.get("name") == path.name for item in matches)
            if matches:
                status = "uploaded"
            elif digest in history_ids:
                status = "previously_seen"
            else:
                status = "local_only"
            rows.append({"path": str(path), "relative_path": path.relative_to(root).as_posix(),
                         "size": stat.st_size, "sha256": sha, "hash": digest, "kind": kind,
                         "status": status, "reason": "", "pair_id": None,
                         "name_mismatch": bool(matches and not exact_name),
                         "duplicate_of": None,
                         "matching_ente_ids": sorted({m["id"] for m in matches}),
                         "historical_ente_ids": sorted(history_ids.get(digest, set()))})
            progress.finish(cached)

    finally:
        progress.close()
    db.commit()
    tag_livephoto_pairs(rows, current)
    tag_duplicates(rows)
    return rows


def tag_livephoto_pairs(rows, current):
    by_stem = defaultdict(list)
    for row in rows:
        ext = Path(row["path"]).suffix.lower()
        if ext in LIVE_IMAGES or ext == ".mov":
            by_stem[(str(Path(row["path"]).parent), Path(row["path"]).stem.lower())].append(row)
    remote_pairs = {frozenset(remote_parts(item)) for item in current
                    if item.get("type") == "livephoto"}
    for group in by_stem.values():
        imgs = [r for r in group if Path(r["path"]).suffix.lower() in LIVE_IMAGES]
        movs = [r for r in group if Path(r["path"]).suffix.lower() == ".mov"]
        if not imgs or not movs:
            continue
        if len(imgs) != 1 or len(movs) != 1:
            for item in group:
                if item["status"] == "local_only":
                    item["status"], item["reason"] = "needs_review", "ambiguous_livephoto_pair"
            continue
        pair = [imgs[0], movs[0]]
        pair_id = pair[0]["relative_path"].rsplit(".", 1)[0]
        for item in pair:
            item["pair_id"] = pair_id
        statuses = {r["status"] for r in pair}
        both_remote_as_live = frozenset(r["hash"] for r in pair) in remote_pairs
        if not both_remote_as_live and "local_only" in statuses and len(statuses) > 1:
            for item in pair:
                if item["status"] == "local_only":
                    item["status"], item["reason"] = "needs_review", "partial_livephoto_remote_match"


def tag_duplicates(rows):
    """Identify redundant copies without creating a separate file status."""
    known = {}
    pairs = defaultdict(list)
    for row in rows:
        if row["pair_id"]:
            pairs[row["pair_id"]].append(row)
    seen_pairs = {}
    for pair_id, group in sorted(pairs.items()):
        if len(group) != 2 or any(row["status"] != "local_only" for row in group):
            continue
        signature = tuple(sorted(row["hash"] for row in group))
        previous = seen_pairs.get(signature)
        if previous:
            for row in group:
                row["duplicate_of"] = previous
        else:
            seen_pairs[signature] = pair_id
            for row in group:
                known.setdefault(row["hash"], row["relative_path"])
    for row in sorted(rows, key=lambda item: item["relative_path"]):
        if row["status"] != "local_only" or row["pair_id"]:
            continue
        previous = known.get(row["hash"])
        if previous:
            row["duplicate_of"] = previous
        else:
            known[row["hash"]] = row["relative_path"]

def display(rows, root, fmt, output):
    counts = Counter(row["status"] for row in rows)
    print(f"Scanned {len(rows)} media files in {root}:", file=sys.stderr)
    for key, value in sorted(counts.items()):
        print(f"  {key}: {value}", file=sys.stderr)
    candidates = [r for r in rows if r["status"] == "local_only" and not r["duplicate_of"]]
    print(f"  upload_candidates: {len(candidates)}", file=sys.stderr)
    print(f"  local_only_bytes: {sum(r['size'] for r in candidates)}", file=sys.stderr)
    by_dir = defaultdict(lambda: {"files": 0, "bytes": 0})
    for row in candidates:
        parts = Path(row["relative_path"]).parts[:-1]
        group = "/".join(parts[:2]) if parts else "."
        by_dir[group]["files"] += 1
        by_dir[group]["bytes"] += row["size"]
    for group, stats in sorted(by_dir.items()):
        print(f"    {group}: {stats['files']} files, {stats['bytes']} bytes", file=sys.stderr)
    keys = ("relative_path", "status", "size", "hash", "sha256", "reason", "pair_id",
            "name_mismatch", "duplicate_of", "matching_ente_ids", "historical_ente_ids")
    if fmt == "json":
        content = json.dumps({"root": str(root), "summary": dict(counts),
                              "by_directory": dict(by_dir), "files": rows},
                             ensure_ascii=False, indent=2) + "\n"
    elif fmt == "csv":
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(row[k]) if k in {"matching_ente_ids", "historical_ente_ids"}
                             else row[k] for k in keys})
        content = stream.getvalue()
    else:
        content = "".join(
            f"{r['status']}\t{r['relative_path']}"
            + (f"\tduplicate_of={r['duplicate_of']}" if r["duplicate_of"] else "") + "\n"
            for r in rows if r["status"] != "uploaded")
    if output:
        if output.exists() or output.is_symlink():
            raise PrepError(f"Refusing to overwrite report: {output}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(output, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    else:
        print(content, end="")

def normalized_name(name):
    """Compare output names across case-insensitive and Unicode filesystems."""
    return unicodedata.normalize("NFC", name).casefold()


def plan(rows, flatten):
    """Select every distinct candidate and resolve its destination before writing."""
    selected = sorted(
        (row for row in rows if row["status"] == "local_only" and not row["duplicate_of"]),
        key=lambda row: row["relative_path"])
    if not flatten:
        return [dict(row, staged=(Path("media") / row["relative_path"]).as_posix())
                for row in selected]

    by_group = defaultdict(list)
    for row in selected:
        by_group[row["pair_id"] or row["relative_path"]].append(row)
    groups = []
    for _, items in sorted(by_group.items()):
        # A Live Photo's image and movie must share a basename after flattening.
        representative = next(
            (row for row in items if Path(row["relative_path"]).suffix.lower() in LIVE_IMAGES),
            items[0])
        stem = Path(representative["relative_path"]).stem
        token = (items[0]["sha256"] if len(items) == 1 else
                 hashlib.sha256(":".join(sorted(row["sha256"] for row in items))
                                .encode("ascii")).hexdigest())
        groups.append({"items": items, "stem": stem, "token": token, "suffix_size": 0})

    # Rename every colliding group. Retry if a generated name happens to
    # collide with an original name or another generated name.
    while True:
        owners = defaultdict(set)
        staged_paths = {}
        for index, group in enumerate(groups):
            suffix = ("__" + group["token"][:group["suffix_size"]]
                      if group["suffix_size"] else "")
            for row in group["items"]:
                extension = Path(row["relative_path"]).suffix
                name = group["stem"] + suffix + extension
                if len(name.encode("utf-8")) > 255:
                    raise PrepError(f"Flattened filename too long: {name}")
                owners[normalized_name(name)].add(index)
                staged_paths[row["relative_path"]] = (Path("media") / name).as_posix()
        collisions = set().union(*(ids for ids in owners.values() if len(ids) > 1))
        if not collisions:
            break
        for index in collisions:
            group = groups[index]
            group["suffix_size"] = 8 if not group["suffix_size"] else group["suffix_size"] + 4
            if group["suffix_size"] > len(group["token"]):
                raise PrepError("Cannot resolve flattened filename collision")
    return [dict(row, staged=staged_paths[row["relative_path"]]) for row in selected]


class StageProgress:
    """Report logical staged bytes; reflinks share data without copying it."""

    def __init__(self, total_files, total_bytes):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.current_bytes = 0
        self.reflinks = 0
        self.copies = 0
        self.phase = ""
        self.tty = sys.stderr.isatty()
        self.last_update = 0.0
        self.last_width = 0

    def start(self):
        self.current_bytes = 0
        self.phase = "linking"
        self.render(force=True)

    def copied(self, size):
        self.current_bytes += size
        self.phase = "copying"
        self.render()

    def verifying(self):
        self.phase = "verifying"
        self.render(force=True)

    def finish(self, size, method):
        self.done_files += 1
        self.done_bytes += size
        self.current_bytes = 0
        if method == "reflink":
            self.reflinks += 1
        else:
            self.copies += 1
        self.phase = ""
        self.render(force=True)

    def summary(self):
        return (f"{self.done_files}/{self.total_files} files, "
                f"{human_size(self.done_bytes + self.current_bytes)} / "
                f"{human_size(self.total_bytes)}, "
                f"{self.reflinks} reflink, {self.copies} copy")

    def render(self, force=False):
        if not self.tty:
            return
        current_time = time.monotonic()
        if not force and current_time - self.last_update < 0.1:
            return
        self.last_update = current_time
        if self.total_bytes:
            fraction = min(1.0, (self.done_bytes + self.current_bytes) / self.total_bytes)
        else:
            fraction = self.done_files / self.total_files if self.total_files else 1.0
        if self.done_files < self.total_files:
            fraction = min(fraction, 0.99)
        filled = int(20 * fraction)
        line = (f"Staging [{'#' * filled}{'-' * (20 - filled)}] "
                f"{self.summary()}")
        if self.phase:
            line += f" ({self.phase})"
        sys.stderr.write("\r" + line + " " * max(0, self.last_width - len(line)))
        sys.stderr.flush()
        self.last_width = len(line)

    def close(self):
        if self.tty:
            self.render(force=True)
            sys.stderr.write("\n")
            sys.stderr.flush()
        else:
            print(f"Staging: {self.summary()}", file=sys.stderr)

def safe_replicate(source, dest, expected, on_copy_bytes=None, on_verifying=None):
    before = source.lstat()
    if source.is_symlink():
        raise PrepError(f"Refusing source symlink: {source}")
    with source.open("rb") as inp, dest.open("xb") as out:
        try:
            fcntl.ioctl(out.fileno(), FICLONE, inp.fileno())
            method = "reflink"
        except OSError:
            out.seek(0)
            out.truncate(0)
            inp.seek(0)
            while buf := inp.read(CHUNK):
                out.write(buf)
                if on_copy_bytes is not None:
                    on_copy_bytes(len(buf))
            method = "copy"
        out.flush()
        os.fsync(out.fileno())
    if on_verifying is not None:
        on_verifying()
    after = source.lstat()
    attrs = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev")
    if any(getattr(before, attr) != getattr(after, attr) for attr in attrs):
        raise PrepError(f"Source changed during staging: {source}")
    sha, digest, _ = hash_file(dest)
    if sha != expected["sha256"] or digest != expected["hash"] or after.st_size != expected["size"]:
        raise PrepError(f"Staged file content mismatch: {source}")
    os.utime(dest, ns=(after.st_atime_ns, after.st_mtime_ns))
    return method



def human_size(size):
    """Format a non-negative byte count using binary units."""
    if size < 1024:
        return f"{size:,} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024
        if value < 1024 or unit == "PiB":
            return f"{value:.2f} {unit}"

def prepare(args, root, rows, timestamp):
    output = args.output.expanduser().absolute()
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise PrepError("Staging must be outside the scanned source tree")
    if output.exists() or output.is_symlink():
        raise PrepError(f"Staging directory already exists: {output}")

    planned = plan(rows, args.flatten)
    total = sum(row["size"] for row in planned)
    print(f"Files: {len(planned):,}")
    print(f"Total size: {human_size(total)}")
    print(f"Destination: {output}")
    if args.dry_run:
        print("Dry run: staging not created.")
        return
    if not planned:
        raise PrepError("No eligible files to stage")

    output.mkdir(mode=0o700)
    manifest = {"format_version": 1, "account": args.account, "source_root": str(root),
                "created_at": now(), "remote_snapshot_at": timestamp,
                "flatten": args.flatten, "files": []}
    progress = StageProgress(len(planned), total)
    try:
        for row in planned:
            target = output / row["staged"]
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            progress.start()
            method = safe_replicate(Path(row["path"]), target, row,
                                    on_copy_bytes=progress.copied,
                                    on_verifying=progress.verifying)
            manifest["files"].append(
                {key: row[key] for key in ("path", "relative_path", "size",
                                         "hash", "sha256", "pair_id", "staged")}
                | {"method": method})
            progress.finish(row["size"], method)
    finally:
        progress.close()
    with (output / "manifest.json").open("x", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    print(f"Prepared {len(planned)} files. Open {output / 'media'} in Ente Desktop.")

def verify(args, current):
    manifest_path = args.manifest.expanduser().resolve(strict=True)
    with manifest_path.open(encoding="utf-8") as file:
        manifest = json.load(file)
    if manifest.get("format_version") != 1 or manifest.get("account") != args.account:
        raise PrepError("Manifest version or Ente account differs")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PrepError("Invalid upload manifest")
    by_hash = defaultdict(list)
    live_pairs = set()
    for item in current:
        parts = remote_parts(item)
        for part in parts:
            by_hash[part].append(item)
        if item["type"] == "livephoto":
            live_pairs.add(frozenset(parts))
    groups = defaultdict(list)
    for row in files:
        if not isinstance(row, dict) or not valid_hash(row.get("hash")):
            raise PrepError("Invalid file in upload manifest")
        if row.get("pair_id"):
            groups[row["pair_id"]].append(row)
    results = []
    for row in files:
        matches = by_hash[row["hash"]]
        pair = groups.get(row.get("pair_id")) if row.get("pair_id") else None
        if pair and len(pair) == 2 and frozenset(r["hash"] for r in pair) in live_pairs:
            status = "verified"
        elif pair and len(pair) == 2:
            status = "needs_review" if matches else "not_found"
        elif any(item.get("name") == Path(row.get("staged", row["path"])).name
                 for item in matches):
            status = "verified"
        elif matches:
            status = "content_present_other_name"
        else:
            status = "not_found"
        results.append((row, status))
    counts = Counter(status for _, status in results)
    print(f"Verification: {len(files)} files")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")
    for row, status in results:
        if status != "verified":
            print(f"  {status}: {row['relative_path']}")
    print("Verification establishes current Ente content/name matches; it does not prove which upload added a file.")
    return 0 if counts.get("verified", 0) == len(files) else 1

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--account", required=True, help="Name from `ente account list`")
    p.add_argument("--ente-bin", default="ente")
    p.add_argument("--db", type=Path, default=Path.home() / ".local/state/ente-camera-sync/upload-preparation.sqlite3")
    p.add_argument("--camera-db", type=Path, default=Path.home() / ".local/state/ente-camera-sync/state-v04.sqlite3")
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("local-only", "prepare-upload"):
        c = commands.add_parser(name)
        c.add_argument("directory", type=Path)
        c.add_argument("--allow-empty-remote", action="store_true")
        c.add_argument("--allow-large-drop", action="store_true")
        c.add_argument("--exclude", action="append", default=[])
        c.add_argument("--rehash", action="store_true", help="Recompute all hashes")
        if name == "local-only":
            c.add_argument("--format", choices=("text", "csv", "json"), default="text")
            c.add_argument("--output", type=Path)
        else:
            c.add_argument("--output", required=True, type=Path)
            c.add_argument("--dry-run", action="store_true",
                           help="Preview without creating staging or changing persistent SQLite")
            c.add_argument("--flatten", action="store_true",
                           help="Place all chosen files directly in media/")
    verify_cmd = commands.add_parser("verify-upload")
    verify_cmd.add_argument("manifest", type=Path)
    verify_cmd.add_argument("--allow-empty-remote", action="store_true")
    verify_cmd.add_argument("--allow-large-drop", action="store_true")
    return p


def execute(db, args, root):
    """Work against the supplied SQLite connection (persistent or in-memory)."""
    init_db(db, args.account)
    import_download_history(db, args.camera_db.expanduser(), args.account)
    current, timestamp = refresh_history(db, args)
    if args.command == "verify-upload":
        return verify(args, current)
    rows = scan(db, args, root, current, timestamp)
    if args.command == "local-only":
        display(rows, root, args.format, args.output)
        return 0
    prepare(args, root, rows, timestamp)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)  # SQLite, reports, lock and manifests may contain private file paths.
    try:
        root = None  # verify-upload uses the manifest, not a scanned source directory.
        if args.command in ("local-only", "prepare-upload"):
            supplied_root = args.directory.expanduser()
            if supplied_root.is_symlink():
                raise PrepError("Refusing symlinked source root")
            root = supplied_root.resolve(strict=True)
            if not root.is_dir():
                raise PrepError("Source must be a directory")
        path = args.db.expanduser()
        if path.is_symlink():
            raise PrepError("Refusing symlinked upload database")
        # A preview must not create or update the preparation DB or lock file.
        # Clone a consistent snapshot into RAM; queries and refreshes can write
        # to the in-memory copy without affecting persistent state.
        if args.command == "prepare-upload" and args.dry_run:
            with closing(sqlite3.connect(":memory:")) as scratch:
                if path.exists():
                    if not path.is_file():
                        raise PrepError("Upload database path is not a file")
                    with closing(readonly_connection(path)) as source:
                        source.backup(scratch)
                return execute(scratch, args, root)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = path.parent / ".ente-upload-prep.lock"
        if lock_path.is_symlink():
            raise PrepError("Refusing symlinked lock")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PrepError("Another upload-preparation job is running") from exc
            with closing(sqlite3.connect(path)) as db:
                return execute(db, args, root)
    except (PrepError, OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
