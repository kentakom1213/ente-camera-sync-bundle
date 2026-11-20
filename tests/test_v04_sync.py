"""Offline integration tests for the v0.4 add-only sync, using a fake Ente CLI."""

import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout, closing
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "ente-camera-sync-v04.py"
spec = importlib.util.spec_from_file_location("ente_camera_sync_v04", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def b2(data):
    return base64.b64encode(hashlib.blake2b(data, digest_size=64).digest()).decode()


class V04SyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.camera = self.root / "Camera"
        self.camera.mkdir()
        self.legacy = self.root / "old.sqlite3"
        self.db = self.root / "state-v04.sqlite3"
        self.metadata = self.root / "list.json"
        self.mapping = self.root / "download.json"
        self.cli = self.root / "ente-fake"
        self.cli.write_text("""#!/usr/bin/env python3
import base64, json, os, pathlib, sys
argv = sys.argv
if 'download' not in argv:
    sys.exit(2)
ident = argv[argv.index('download') + 1]
output = pathlib.Path(argv[argv.index('--output') + 1])
items = json.loads(os.environ['FAKE_ENTE_DATA'])
output.write_bytes(base64.b64decode(items[ident]))
""")
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        with closing(sqlite3.connect(self.legacy)) as db:
            db.execute("CREATE TABLE file_hashes(path TEXT PRIMARY KEY, role TEXT, size INTEGER, "
                       "mtime_ns INTEGER, sha256 TEXT, updated_at TEXT)")
        self.local("2020/01/old.jpg", b"old photo")
        self.init()

    def local(self, rel, data):
        path = self.camera / rel
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(data)
        s = path.stat()
        with closing(sqlite3.connect(self.legacy)) as db:
            db.execute("INSERT INTO file_hashes VALUES (?,?,?,?,?,?)",
                       (str(path), "camera", s.st_size, s.st_mtime_ns,
                        hashlib.sha256(data).hexdigest(), ""))
            db.commit()
        return path

    def common(self):
        return ["--camera-root", str(self.camera), "--db", str(self.db),
                "--account", "testing", "--ente-bin", str(self.cli)]

    def init(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main([*self.common(), "init", "--legacy-db", str(self.legacy)]), 0)

    def run_sync(self, entries, apply=False, limit=None, files=None):
        self.metadata.write_text(json.dumps(entries))
        items = {k: base64.b64encode(v).decode() for k, v in (files or {}).items()}
        argv = [*self.common(), "sync", "--metadata-json", str(self.metadata)]
        if apply:
            argv.append("--apply")
        if limit is not None:
            argv.extend(["--max-downloads", str(limit)])
        stdout = io.StringIO()
        with patch.dict(os.environ, {"FAKE_ENTE_DATA": json.dumps(items)}), redirect_stdout(stdout):
            code = mod.main(argv)
        return code, stdout.getvalue()

    def test_init_does_not_touch_legacy_and_refuses_replacement(self):
        before = self.legacy.read_bytes()
        self.assertEqual(mod.main([*self.common(), "init", "--legacy-db", str(self.legacy)]), 2)
        self.assertEqual(before, self.legacy.read_bytes())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM local_files").fetchone()[0], 1)

    def test_dry_run_and_single_download_idempotent(self):
        rows = [
            {"id": "1", "type": "image", "name": "old.jpg", "hash": b2(b"old photo")},
            {"id": "2", "type": "image", "name": "new.jpg", "hash": b2(b"new photo"),
             "createdAt": "2026-11-20T23:30:00Z"},
            {"id": "3", "type": "image", "name": "third.jpg", "hash": b2(b"third")},
        ]
        db_before = self.db.read_bytes()
        code, output = self.run_sync(rows)
        self.assertEqual(code, 0)
        self.assertIn("missing: 2", output)
        self.assertEqual(self.db.read_bytes(), db_before)
        self.assertEqual(len(list(self.camera.rglob("*.jpg"))), 1)
        code, output = self.run_sync(rows, apply=True, limit=1, files={"2": b"new photo"})
        self.assertEqual(code, 0, output)
        self.assertIn("downloaded: 1", output)
        new_path = self.camera / "2026/11/new.jpg"  # 23:30 UTC => 08:30 JST on 21st
        self.assertEqual(new_path.read_bytes(), b"new photo")
        code, output = self.run_sync(rows)
        self.assertEqual(code, 0)
        self.assertIn("missing: 1", output)
        code, output = self.run_sync(rows, apply=True, files={"3": b"third"})
        self.assertEqual(code, 0, output)
        code, output = self.run_sync(rows, apply=True, files={})
        self.assertEqual(code, 0, output)
        self.assertIn("downloaded: 0", output)
        self.assertEqual(len(list(self.camera.rglob("*.jpg"))), 3)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ente_files WHERE present=1").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ente_file_parts WHERE status='matched'").fetchone()[0], 3)

    def test_hash_mismatch_never_publishes(self):
        rows = [{"id": "4", "type": "image", "name": "bad.jpg", "hash": b2(b"expected")}]
        code, output = self.run_sync(rows, apply=True, files={"4": b"wrong"})
        self.assertEqual(code, 1)
        self.assertFalse((self.camera / "unknown-date/bad.jpg").exists())
        self.assertEqual(len(list(self.camera.rglob("*.jpg"))), 1)

    def test_collision_does_not_overwrite(self):
        target = self.camera / "unknown-date/collision.jpg"
        target.parent.mkdir()
        target.write_bytes(b"someone else's file")
        rows = [{"id": "5", "type": "image", "name": "collision.jpg", "hash": b2(b"new") }]
        code, output = self.run_sync(rows, apply=True, files={"5": b"new"})
        self.assertEqual(code, 0, output)
        self.assertEqual(target.read_bytes(), b"someone else's file")
        self.assertEqual((target.parent / "collision__ente_5.jpg").read_bytes(), b"new")

    def test_enumeration_change_never_deletes_local(self):
        rows = [{"id": "9", "type": "image", "name": "old.jpg", "hash": b2(b"old photo")}]
        self.assertEqual(self.run_sync(rows, apply=True)[0], 0)
        self.assertEqual(self.run_sync([], apply=True)[0], 2)  # refuse suspicious empty listing
        self.assertTrue((self.camera / "2020/01/old.jpg").exists())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT present FROM ente_files WHERE file_id='9'").fetchone(), (1,))

    def test_live_photo_zip_split_and_resume(self):
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, 'w') as zf:
            zf.writestr("subdir/photo.heic", b"image bytes")
            zf.writestr("photo.mov", b"video bytes")
        rows = [{"id": "7", "type": "livephoto", "name": "PXL_1.heic",
                 "hash": b2(b"image bytes") + ":" + b2(b"video bytes"),
                 "createdAt": "2026-01-01T00:00:00Z"}]
        code, output = self.run_sync(rows, apply=True, files={"7": bio.getvalue()})
        self.assertEqual(code, 0, output)
        self.assertEqual((self.camera / "2026/01/PXL_1.heic").read_bytes(), b"image bytes")
        self.assertEqual((self.camera / "2026/01/PXL_1.mov").read_bytes(), b"video bytes")
        self.assertEqual(self.run_sync(rows, apply=True)[0], 0)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM ente_file_parts WHERE file_id='7' AND status='matched'").fetchone()[0], 2)

    def test_reject_switching_accounts(self):
        argv = ["--camera-root", str(self.camera), "--db", str(self.db),
                "--account", "other-account", "sync", "--metadata-json", str(self.metadata)]
        self.assertEqual(mod.main(argv), 2)


if __name__ == "__main__":
    unittest.main()