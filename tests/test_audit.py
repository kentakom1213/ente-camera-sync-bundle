"""Tests for the read-only migration audit using synthetic data only."""

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

MODULE_PATH = Path(__file__).resolve().parents[1] / "ente-camera-audit.py"
spec = importlib.util.spec_from_file_location("ente_camera_audit", MODULE_PATH)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def ente_hash(data):
    return base64.b64encode(hashlib.blake2b(data, digest_size=64).digest()).decode()


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.camera = self.root / "Camera"
        self.camera.mkdir()
        self.db_path = self.root / "state.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("CREATE TABLE file_hashes (path TEXT PRIMARY KEY, role TEXT, "
                        "size INTEGER, mtime_ns INTEGER, sha256 TEXT, updated_at TEXT)")
        self.db.commit()

    def add_camera(self, name, content, indexed=True):
        path = self.camera / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if indexed:
            stat = path.stat()
            self.db.execute("INSERT INTO file_hashes VALUES (?, 'camera', ?, ?, ?, '')",
                            (str(path), len(content), stat.st_mtime_ns,
                             hashlib.sha256(content).hexdigest()))
            self.db.commit()
        return path

    def run_audit(self, records):
        metadata = self.root / "ente.json"
        metadata.write_text(json.dumps(records))
        self.db.close()
        original_db = self.db_path.read_bytes()
        original_files = {p: p.read_bytes() for p in self.camera.rglob('*') if p.is_file()}
        with redirect_stdout(io.StringIO()):
            rc = audit_module.main([
                "--legacy-db", str(self.db_path), "--camera-root", str(self.camera),
                "--metadata-json", str(metadata), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.db_path.read_bytes(), original_db)
        self.assertEqual({p: p.read_bytes() for p in self.camera.rglob('*') if p.is_file()}, original_files)
        return audit_module.audit(audit_module.parser().parse_args([
            "--legacy-db", str(self.db_path), "--camera-root", str(self.camera),
            "--metadata-json", str(metadata)]))

    def test_matched_duplicates_missing_and_unindexed(self):
        self.add_camera("2026/01/a.jpg", b"photo")
        self.add_camera("2026/02/b.jpg", b"independent", indexed=False)
        report = self.run_audit([
            {"id": "1", "type": "image", "name": "a.jpg", "hash": ente_hash(b"photo")},
            {"id": "2", "type": "image", "name": "duplicate.jpg", "hash": ente_hash(b"photo")},
            {"id": "3", "type": "image", "name": "missing.jpg", "hash": ente_hash(b"not stored")},
            {"id": "4", "type": "image", "name": "unknown.jpg", "hash": None},
            {"id": "5", "type": "video", "name": "bad.mp4", "hash": "not base64"},
            {"id": "6", "type": "video", "name": "b.mp4", "hash": ente_hash(b"independent")},
        ])
        s = report["summary"]
        self.assertEqual((s["matched"], s["missing"], s["no_hash"],
                          s["unsupported_hash"], s["duplicate_ente_content"],
                          s["local_unindexed"]), (3, 1, 1, 1, 1, 1))

    def test_live_photo_and_legacy_mismatch(self):
        path = self.add_camera("old/image.heic", b"image")
        self.add_camera("old/video.mov", b"video")
        self.add_camera("old/changed.jpg", b"before")
        (self.camera / "old/changed.jpg").write_bytes(b"after")
        missing = self.add_camera("old/gone.jpg", b"gone")
        missing.unlink()
        h = ente_hash(b"image") + ":" + ente_hash(b"video")
        report = self.run_audit([
            {"id": "10", "type": "livephoto", "name": "live.heic", "hash": h},
            {"id": "11", "type": "livephoto", "name": "partial.heic",
             "hash": ente_hash(b"image") + ":" + ente_hash(b"absent")},
        ])
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual(report["summary"]["partial_livephoto"], 1)
        self.assertEqual(report["summary"]["legacy_missing"], 1)
        self.assertEqual(report["summary"]["local_content_changed"], 1)
        self.assertEqual(report["files"][0]["local_paths"],
                         [str(path), str(self.camera / "old/video.mov")])

    def test_readonly_db_and_invalid_records(self):
        self.add_camera("x.jpg", b"x")
        with self.assertRaises(ValueError):
            self._duplicate_ids()

    def _duplicate_ids(self):
        records = [{"id": "1", "type": "image", "hash": None}] * 2
        metadata = self.root / "duplicates.json"
        metadata.write_text(json.dumps(records))
        args = audit_module.parser().parse_args([
            "--legacy-db", str(self.db_path), "--camera-root", str(self.camera),
            "--metadata-json", str(metadata)])
        audit_module.audit(args)


if __name__ == "__main__":
    unittest.main()
