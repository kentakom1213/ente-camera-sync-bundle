#!/usr/bin/env python3
"""
Ente CLIのエクスポートを実行し，エクスポートされた写真・動画を
~/Pictures/Camera/YYYY/MMへ追加専用で整理する．

既定ではドライランであり，Camera側へ書き込まない．
--applyを指定した場合だけディレクトリ作成とコピーを行う．
--run-exportを指定した場合だけ，整理前に`ente export`を実行する．
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_VERSION = "1.0.0"
ALBUM_META_FILE = "album_meta.json"
META_DIR_NAME = ".meta"

PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif",
    ".gif", ".dng", ".tif", ".tiff", ".bmp",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".3gp", ".webm", ".mkv", ".avi", ".mts", ".m2ts",
}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaEntry:
    source: Path
    creation_time: datetime | None
    file_id: str
    metadata_path: Path


@dataclass
class Summary:
    mode: str
    export_ran: bool = False
    export_root: str = ""
    camera_root: str = ""
    timezone: str = ""
    metadata_files: int = 0
    referenced_files: int = 0
    media_files: int = 0
    copied: int = 0
    would_copy: int = 0
    duplicate: int = 0
    collisions_renamed: int = 0
    unknown_date: int = 0
    missing_referenced_file: int = 0
    invalid_metadata: int = 0
    unsupported_type: int = 0
    orphan_media: int = 0
    errors: list[str] = field(default_factory=list)


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enteのエクスポートを~/Pictures/Camera/YYYY/MMへ追加専用で整理します．"
            "既定はドライランです．"
        ),
    )
    parser.add_argument(
        "--export-root",
        default="/data/ente-export",
        help="Ente CLIに設定したエクスポート先（既定: /data/ente-export）",
    )
    parser.add_argument(
        "--camera-root",
        default="~/Pictures/Camera",
        help="整理先（既定: ~/Pictures/Camera）",
    )
    parser.add_argument(
        "--state-dir",
        default="~/.local/state/ente-camera-sync",
        help="SQLite・ロック・実行記録の保存先",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Tokyo",
        help="YYYY/MMの判定に使うタイムゾーン（既定: Asia/Tokyo）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Camera側へ実際にコピーする（指定しなければドライラン）",
    )
    parser.add_argument(
        "--run-export",
        action="store_true",
        help="整理前に`ente export`を実行する",
    )
    parser.add_argument(
        "--ente-bin",
        default="ente",
        help="Ente CLIのコマンドまたは絶対パス（既定: ente）",
    )
    parser.add_argument(
        "--album",
        action="append",
        default=[],
        help="エクスポート対象アルバム名．複数回指定可",
    )
    parser.add_argument(
        "--exclude-album",
        action="append",
        default=[],
        help="エクスポートから除外するアルバム名．複数回指定可",
    )
    parser.add_argument(
        "--exclude-hidden",
        action="store_true",
        help="非表示アルバムをエクスポートしない",
    )
    parser.add_argument(
        "--exclude-shared",
        action="store_true",
        help="共有アルバムをエクスポートしない",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Camera側のSHA-256キャッシュを無視して再計算する",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION}",
    )
    return parser


def resolve_executable(command: str) -> str:
    if "/" in command:
        path = expand_path(command)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SyncError(f"実行可能なEnte CLIが見つかりません: {path}")
        return str(path)

    resolved = shutil.which(command)
    if resolved is None:
        raise SyncError(
            f"`{command}`がPATH上に見つかりません．"
            "`--ente-bin /path/to/ente`で指定してください．"
        )
    return resolved


def run_ente_export(args: argparse.Namespace) -> None:
    ente_bin = resolve_executable(args.ente_bin)
    command = [ente_bin, "export"]

    if args.album:
        command += ["--albums", ",".join(args.album)]
    if args.exclude_album:
        command += ["--exclude-albums", ",".join(args.exclude_album)]
    if args.exclude_hidden:
        command.append("--hidden=false")
    if args.exclude_shared:
        command.append("--shared=false")

    print("Enteエクスポートを実行します:", " ".join(command), flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SyncError(
            f"`ente export`が終了コード{completed.returncode}で失敗しました．"
        )


def connect_database(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS file_hashes (
            path TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_hashes_sha256 "
        "ON file_hashes(sha256)"
    )
    return connection


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def cached_hash(
    connection: sqlite3.Connection,
    path: Path,
    role: str,
    *,
    reindex: bool = False,
) -> str:
    stat_result = path.stat()
    path_text = str(path)

    if not reindex:
        row = connection.execute(
            "SELECT size, mtime_ns, sha256 FROM file_hashes WHERE path = ?",
            (path_text,),
        ).fetchone()
        if row is not None:
            size, mtime_ns, digest = row
            if size == stat_result.st_size and mtime_ns == stat_result.st_mtime_ns:
                return str(digest)

    digest = sha256_file(path)
    connection.execute(
        """
        INSERT INTO file_hashes(path, role, size, mtime_ns, sha256, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            role = excluded.role,
            size = excluded.size,
            mtime_ns = excluded.mtime_ns,
            sha256 = excluded.sha256,
            updated_at = excluded.updated_at
        """,
        (
            path_text,
            role,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            digest,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return digest


def is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def iter_camera_files(camera_root: Path) -> Iterator[Path]:
    if not camera_root.exists():
        return

    for root, dir_names, file_names in os.walk(camera_root):
        dir_names[:] = [
            name for name in dir_names
            if not name.startswith(".ente-camera-sync-")
        ]
        root_path = Path(root)
        for name in file_names:
            if name.endswith(".part") or ".part." in name:
                continue
            path = root_path / name
            if is_regular_file(path):
                yield path


def index_camera(
    connection: sqlite3.Connection,
    camera_root: Path,
    *,
    reindex: bool,
) -> dict[str, Path]:
    hash_to_path: dict[str, Path] = {}
    seen_paths: set[str] = set()

    print("Camera側の既存ファイルを索引化しています...", flush=True)
    for path in iter_camera_files(camera_root):
        try:
            digest = cached_hash(connection, path, "camera", reindex=reindex)
        except OSError as error:
            eprint(f"警告: ハッシュを計算できません: {path}: {error}")
            continue
        seen_paths.add(str(path))
        hash_to_path.setdefault(digest, path)

    camera_prefix = str(camera_root) + os.sep
    rows = connection.execute(
        "SELECT path FROM file_hashes WHERE role = 'camera' AND path LIKE ?",
        (camera_prefix + "%",),
    ).fetchall()
    stale_paths = [
        path_text for (path_text,) in rows
        if path_text not in seen_paths
    ]
    connection.executemany(
        "DELETE FROM file_hashes WHERE path = ?",
        ((path_text,) for path_text in stale_paths),
    )
    connection.commit()
    return hash_to_path


def parse_creation_time(value: object, target_tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    local = parsed.astimezone(target_tz)
    current_year = datetime.now(target_tz).year
    if local.year < 1900 or local.year > current_year + 2:
        return None
    return local


def safe_child(parent: Path, file_name: object) -> Path | None:
    if not isinstance(file_name, str) or not file_name:
        return None
    child_name = Path(file_name)
    if child_name.name != file_name or child_name.is_absolute():
        return None
    candidate = parent / file_name
    try:
        candidate.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def classify_media(path: Path) -> str | None:
    extension = path.suffix.lower()
    if extension in PHOTO_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"

    # 未知の拡張子だけ`file`へ問い合わせる．全ファイルで起動しないため高速．
    file_bin = shutil.which("file")
    if file_bin is None:
        return None

    completed = subprocess.run(
        [file_bin, "--brief", "--mime-type", "--", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if completed.returncode != 0:
        return None
    detected = completed.stdout.strip()
    if detected.startswith("image/") and detected != "image/svg+xml":
        return "image"
    if detected.startswith("video/"):
        return "video"
    return None


def iter_ente_entries(
    export_root: Path,
    target_tz: ZoneInfo,
    summary: Summary,
) -> tuple[list[MediaEntry], set[Path]]:
    entries: list[MediaEntry] = []
    referenced_paths: set[Path] = set()

    meta_dirs = sorted(
        path for path in export_root.rglob(META_DIR_NAME)
        if path.is_dir() and path.parent != path
    )

    for meta_dir in meta_dirs:
        album_dir = meta_dir.parent
        for metadata_path in sorted(meta_dir.glob("*.json")):
            if metadata_path.name == ALBUM_META_FILE:
                continue

            summary.metadata_files += 1
            try:
                with metadata_path.open("r", encoding="utf-8") as file:
                    metadata = json.load(file)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                summary.invalid_metadata += 1
                summary.errors.append(f"メタデータを読めません: {metadata_path}: {error}")
                continue

            info = metadata.get("info")
            if not isinstance(info, dict):
                summary.invalid_metadata += 1
                summary.errors.append(f"`info`がありません: {metadata_path}")
                continue

            file_names = info.get("fileNames")
            if not isinstance(file_names, list) or not file_names:
                summary.invalid_metadata += 1
                summary.errors.append(f"`info.fileNames`がありません: {metadata_path}")
                continue

            file_id = str(info.get("id", "unknown"))
            creation_time = parse_creation_time(
                metadata.get("creationTime"),
                target_tz,
            )

            for file_name in file_names:
                source = safe_child(album_dir, file_name)
                if source is None:
                    summary.invalid_metadata += 1
                    summary.errors.append(
                        f"安全でないファイル名を無視しました: "
                        f"{metadata_path}: {file_name!r}"
                    )
                    continue

                referenced_paths.add(source)
                summary.referenced_files += 1

                if not is_regular_file(source):
                    summary.missing_referenced_file += 1
                    summary.errors.append(
                        f"参照先ファイルがありません: {source} "
                        f"(metadata: {metadata_path})"
                    )
                    continue

                if source.stat().st_size == 0:
                    summary.unsupported_type += 1
                    summary.errors.append(f"0バイトファイルを無視しました: {source}")
                    continue

                if classify_media(source) is None:
                    summary.unsupported_type += 1
                    continue

                summary.media_files += 1
                entries.append(
                    MediaEntry(
                        source=source,
                        creation_time=creation_time,
                        file_id=file_id,
                        metadata_path=metadata_path,
                    )
                )

    return entries, referenced_paths


def count_orphan_media(
    export_root: Path,
    referenced_paths: set[Path],
) -> int:
    count = 0
    for root, dir_names, file_names in os.walk(export_root):
        dir_names[:] = [name for name in dir_names if name != META_DIR_NAME]
        root_path = Path(root)
        for name in file_names:
            path = root_path / name
            if path in referenced_paths or not is_regular_file(path):
                continue
            if path.stat().st_size > 0 and classify_media(path) is not None:
                count += 1
    return count


def collision_name(source: Path, file_id: str, sequence: int | None = None) -> str:
    safe_id = "".join(character for character in file_id if character.isalnum())
    if not safe_id:
        safe_id = "unknown"
    suffix = f"__ente_{safe_id}"
    if sequence is not None:
        suffix += f"_{sequence}"
    return f"{source.stem}{suffix}{source.suffix}"


def choose_destination(
    entry: MediaEntry,
    camera_root: Path,
    source_hash: str,
    connection: sqlite3.Connection,
) -> tuple[Path, bool]:
    if entry.creation_time is None:
        target_dir = camera_root / "unknown-date"
    else:
        target_dir = (
            camera_root
            / f"{entry.creation_time.year:04d}"
            / f"{entry.creation_time.month:02d}"
        )

    candidate = target_dir / entry.source.name
    if not candidate.exists():
        return candidate, False

    if is_regular_file(candidate):
        try:
            if cached_hash(connection, candidate, "camera") == source_hash:
                return candidate, False
        except OSError:
            pass

    sequence: int | None = None
    while True:
        renamed = target_dir / collision_name(entry.source, entry.file_id, sequence)
        if not renamed.exists():
            return renamed, True
        if is_regular_file(renamed):
            try:
                if cached_hash(connection, renamed, "camera") == source_hash:
                    return renamed, True
            except OSError:
                pass
        sequence = 2 if sequence is None else sequence + 1


def atomic_copy_verified(source: Path, destination: Path, expected_hash: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.part.",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)

    try:
        shutil.copy2(source, temporary_path)
        actual_hash = sha256_file(temporary_path)
        if actual_hash != expected_hash:
            raise SyncError(
                f"コピー後のSHA-256が一致しません: {source} -> {destination}"
            )

        with temporary_path.open("rb") as file:
            os.fsync(file.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError as error:
            raise SyncError(
                f"コピー中に整理先が作成されました．再実行してください: "
                f"{destination}"
            ) from error
        temporary_path.unlink()

        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def write_run_record(state_dir: Path, summary: Summary) -> Path:
    runs_dir = state_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    destination = runs_dir / f"{timestamp}.json"

    temporary = destination.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(asdict(summary), file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, destination)
    return destination


def print_summary(summary: Summary, record_path: Path | None) -> None:
    print()
    print("=" * 60)
    print("Ente Camera同期結果")
    print("=" * 60)
    print(f"モード:                     {summary.mode}")
    print(f"Ente export実行:            {'yes' if summary.export_ran else 'no'}")
    print(f"エクスポート元:             {summary.export_root}")
    print(f"整理先:                     {summary.camera_root}")
    print(f"タイムゾーン:               {summary.timezone}")
    print(f"メタデータJSON:             {summary.metadata_files}")
    print(f"JSONから参照されたファイル: {summary.referenced_files}")
    print(f"写真・動画:                 {summary.media_files}")
    print(f"コピー:                     {summary.copied}")
    print(f"コピー予定:                 {summary.would_copy}")
    print(f"既存と同一でスキップ:       {summary.duplicate}")
    print(f"名前衝突で改名:             {summary.collisions_renamed}")
    print(f"日時不明:                   {summary.unknown_date}")
    print(f"参照先なし:                 {summary.missing_referenced_file}")
    print(f"不正なメタデータ:           {summary.invalid_metadata}")
    print(f"対象外形式・0バイト:        {summary.unsupported_type}")
    print(f"メタデータから未参照の媒体: {summary.orphan_media}")
    print(f"エラー:                     {len(summary.errors)}")
    if record_path is not None:
        print(f"実行記録:                   {record_path}")


def main() -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()

    export_root = expand_path(args.export_root)
    camera_root = expand_path(args.camera_root)
    state_dir = expand_path(args.state_dir)

    try:
        target_tz = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        eprint(f"不明なタイムゾーンです: {args.timezone}")
        return 2

    if export_root == camera_root:
        eprint("エクスポート元と整理先は別ディレクトリにしてください．")
        return 2
    try:
        camera_root.relative_to(export_root)
    except ValueError:
        pass
    else:
        eprint("整理先をエクスポート元の内部には置けません．")
        return 2

    try:
        export_root.relative_to(camera_root)
    except ValueError:
        pass
    else:
        eprint("エクスポート元を整理先の内部には置けません．")
        return 2

    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "sync.lock"

    summary = Summary(
        mode="APPLY" if args.apply else "DRY-RUN",
        export_root=str(export_root),
        camera_root=str(camera_root),
        timezone=args.timezone,
    )
    record_path: Path | None = None

    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            eprint("別のEnte Camera同期処理が実行中です．")
            return 75

        try:
            if args.run_export:
                run_ente_export(args)
                summary.export_ran = True

            if not export_root.is_dir():
                raise SyncError(
                    f"エクスポート先が見つかりません: {export_root}\n"
                    "`ente account update --email ... --dir "
                    f"{export_root}`を確認してください．"
                )

            database_path = state_dir / "state.sqlite3"
            with connect_database(database_path) as connection:
                camera_hashes = index_camera(
                    connection,
                    camera_root,
                    reindex=args.reindex,
                )

                entries, referenced_paths = iter_ente_entries(
                    export_root,
                    target_tz,
                    summary,
                )
                summary.orphan_media = count_orphan_media(
                    export_root,
                    referenced_paths,
                )

                # 同じEnteファイルが複数アルバムへ出力された場合も，
                # 同一実行内で一度だけコピーする．
                planned_hashes: dict[str, Path] = dict(camera_hashes)

                for index, entry in enumerate(entries, start=1):
                    try:
                        source_hash = cached_hash(
                            connection,
                            entry.source,
                            "export",
                        )

                        existing = planned_hashes.get(source_hash)
                        if existing is not None:
                            summary.duplicate += 1
                            print(
                                f"[{index}/{len(entries)}] SKIP 同一内容: "
                                f"{entry.source} == {existing}"
                            )
                            continue

                        if entry.creation_time is None:
                            summary.unknown_date += 1

                        destination, renamed = choose_destination(
                            entry,
                            camera_root,
                            source_hash,
                            connection,
                        )

                        if renamed:
                            summary.collisions_renamed += 1

                        if destination.exists() and is_regular_file(destination):
                            destination_hash = cached_hash(
                                connection,
                                destination,
                                "camera",
                            )
                            if destination_hash == source_hash:
                                summary.duplicate += 1
                                planned_hashes[source_hash] = destination
                                print(
                                    f"[{index}/{len(entries)}] SKIP 同一内容: "
                                    f"{entry.source} == {destination}"
                                )
                                continue

                        if args.apply:
                            atomic_copy_verified(
                                entry.source,
                                destination,
                                source_hash,
                            )
                            cached_hash(
                                connection,
                                destination,
                                "camera",
                                reindex=True,
                            )
                            summary.copied += 1
                            print(
                                f"[{index}/{len(entries)}] COPY: "
                                f"{entry.source} -> {destination}"
                            )
                        else:
                            summary.would_copy += 1
                            print(
                                f"[{index}/{len(entries)}] WOULD COPY: "
                                f"{entry.source} -> {destination}"
                            )

                        planned_hashes[source_hash] = destination
                    except (OSError, sqlite3.Error, SyncError) as error:
                        message = f"{entry.source}: {error}"
                        summary.errors.append(message)
                        eprint(f"エラー: {message}")

                connection.commit()

        except (OSError, sqlite3.Error, SyncError) as error:
            summary.errors.append(str(error))
            eprint(f"致命的エラー: {error}")
        finally:
            try:
                record_path = write_run_record(state_dir, summary)
            except OSError as error:
                eprint(f"警告: 実行記録を書き込めません: {error}")

    print_summary(summary, record_path)

    if not args.apply:
        if args.run_export:
            print(
                "Camera側は変更していませんが，`ente export`により"
                "エクスポート領域は更新されています．"
            )
        else:
            print("Camera内の実ファイルは変更していません．")
        print(
            "SHA-256索引と実行記録はstate-dirへ保存しています．"
        )
        print("内容を確認後，`--apply`を付けて再実行してください．")

    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
