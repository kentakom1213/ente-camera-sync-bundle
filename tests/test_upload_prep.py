"""Offline tests for local-only detection and Ente Desktop staging."""
import base64
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "ente-upload-prep.py"
spec = importlib.util.spec_from_file_location("upload_prep", MODULE)
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def digest(data):
    return base64.b64encode(hashlib.blake2b(data, digest_size=64).digest()).decode("ascii")


def media(ident, name, data, typ="image"):
    return {"id": str(ident), "name": name, "type": typ, "hash": digest(data)}


class UploadPrepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pictures = self.base / "Pictures"
        self.pictures.mkdir()
        self.db = self.base / "upload.sqlite3"
        self.camera_db = self.base / "camera.sqlite3"
        self.remote = []

    def file(self, name, data):
        file = self.pictures / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        return file

    def invoke(self, *args, remote=None):
        if remote is not None:
            self.remote = remote
        cmd = ["--account", "test", "--db", str(self.db), "--camera-db", str(self.camera_db), *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(prep, "fetch_remote", side_effect=lambda args: self.remote), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            rc = prep.main(cmd)
        return rc, stdout.getvalue(), stderr.getvalue()

    def report(self, *, remote=None, extra=()):
        report_path = self.base / f"report-{len(list(self.base.glob('report-*.json')))}.json"
        rc, stdout, stderr = self.invoke("local-only", str(self.pictures),
                                          "--format", "json", "--output", str(report_path),
                                          *extra, remote=remote)
        return rc, (json.loads(report_path.read_text()) if report_path.exists() else None), stderr

    def test_local_only_and_renamed_and_duplicates(self):
        self.file("Camera/present.jpg", b"same")
        self.file("Old/duplicate.jpg", b"same")
        self.file("Old/only.jpg", b"new")
        self.file("Old/copy.jpg", b"new")
        self.file("Old/readme.txt", b"unsupported")
        self.file(".thumbnails/small.jpg", b"thumbnail")
        rc, doc, err = self.report(remote=[media(1, "remote.jpg", b"same")])
        self.assertEqual(rc, 0, err)
        statuses = {r["relative_path"]: r["status"] for r in doc["files"]}
        self.assertEqual(statuses["Camera/present.jpg"], "uploaded_renamed")
        self.assertEqual(statuses["Old/duplicate.jpg"], "uploaded_renamed")
        self.assertEqual(statuses["Old/only.jpg"], "local_duplicate")  # sorted copy before only
        self.assertEqual(statuses["Old/copy.jpg"], "local_only")
        self.assertEqual(len(statuses), 4)
        self.assertEqual(len(list(self.db.parent.glob("*.jpg"))), 0)

    def test_history_persists_after_deleted_remote_item(self):
        self.file("lost.jpg", b"old remote")
        self.assertEqual(self.report(remote=[media(1, "lost.jpg", b"old remote")])[0], 0)
        rc, report, err = self.report(remote=[], extra=("--allow-empty-remote",))
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["files"][0]["status"], "previously_seen")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT present FROM ente_history").fetchone()[0], 0)

    def test_seeds_history_from_existing_camera_sync_db(self):
        self.file("deleted.jpg", b"old deleted")
        with sqlite3.connect(self.camera_db) as conn:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('account','test')")
            conn.execute("CREATE TABLE ente_files(file_id TEXT,hash TEXT,name TEXT,type TEXT,last_seen_at TEXT,account TEXT)")
            conn.execute("INSERT INTO ente_files VALUES(?,?,?,?,?,?)",
                         ("16", digest(b"old deleted"), "deleted.jpg", "image", "2026-11-01", "test"))
            conn.execute("CREATE TABLE local_files(path TEXT,size INT,mtime_ns INT,sha256 TEXT,blake2b TEXT)")
        rc, report, err = self.report(remote=[], extra=("--allow-empty-remote",))
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["files"][0]["status"], "previously_seen")

    def test_report_exclude_and_dont_overwrite(self):
        self.file("Screenshots/s.jpg", b"s")
        self.file("Camera/a.jpg", b"a")
        rc, report, err = self.report(remote=[], extra=("--exclude", "Screenshots/**"))
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["relative_path"] for r in report["files"]], ["Camera/a.jpg"])
        dest = self.base / "existing.csv"
        dest.write_text("do not touch")
        rc, _, _ = self.invoke("local-only", str(self.pictures), "--format", "csv", "--output", str(dest))
        self.assertEqual(rc, 2)
        self.assertEqual(dest.read_text(), "do not touch")

    def test_batch_preserves_paths_and_recheck(self):
        src = self.file("Old/2021/leaf.jpg", b"private photo")
        stage = self.base / "stage"
        rc, text, err = self.invoke("prepare-upload", str(self.pictures), "--output", str(stage), remote=[])
        self.assertEqual(rc, 0, err)
        self.assertFalse(stage.exists())
        rc, text, err = self.invoke("prepare-upload", str(self.pictures), "--output", str(stage), "--apply")
        self.assertEqual(rc, 0, err)
        staged = stage / "media/Old/2021/leaf.jpg"
        self.assertEqual(staged.read_bytes(), b"private photo")
        self.assertEqual(src.read_bytes(), b"private photo")
        self.assertNotEqual(src.stat().st_ino, staged.stat().st_ino)  # no hard links
        manifest = json.loads((stage / "manifest.json").read_text())
        self.assertEqual(len(manifest["files"]), 1)
        rc, _, _ = self.invoke("prepare-upload", str(self.pictures), "--output", str(self.base / "batch2"), "--apply")
        self.assertEqual(rc, 2)  # all pending already staged
        rc, _, _ = self.invoke("verify-upload", str(stage / "manifest.json"), remote=[])
        self.assertEqual(rc, 1)
        rc, text, _ = self.invoke("verify-upload", str(stage / "manifest.json"),
                                  remote=[media(2, "leaf.jpg", b"private photo")])
        self.assertEqual(rc, 0)
        self.assertIn("verified: 1", text)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT verified FROM batch_files").fetchone()[0], "verified")

    def test_livephoto_pair_not_split_by_limit_and_verified_as_composite(self):
        self.file("Camera/IMG_3.HEIC", b"photo")
        self.file("Camera/IMG_3.MOV", b"movie")
        stage = self.base / "one"
        rc, output, err = self.invoke("prepare-upload", str(self.pictures), "--output", str(stage),
                                      "--max-files", "1", "--apply", remote=[])
        self.assertEqual(rc, 2, output + err)  # pair counts as two files
        self.assertFalse(stage.exists())
        rc, output, err = self.invoke("prepare-upload", str(self.pictures), "--output", str(stage),
                                      "--max-files", "2", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(list((stage / "media").rglob("*.*"))), 2)
        live = {"id": "4", "name": "IMG_3.HEIC", "type": "livephoto",
                "hash": digest(b"photo") + ":" + digest(b"movie")}
        rc, text, err = self.invoke("verify-upload", str(stage / "manifest.json"), remote=[live])
        self.assertEqual(rc, 0, text + err)
        self.assertIn("verified: 2", text)

    def test_partial_livephoto_is_review_not_staged(self):
        self.file("IMG_3.HEIC", b"photo")
        self.file("IMG_3.MOV", b"movie")
        rc, report, err = self.report(remote=[media(1, "IMG_3.HEIC", b"photo")])
        self.assertEqual(rc, 0, err)
        statuses = {r["relative_path"]: r["status"] for r in report["files"]}
        self.assertEqual(statuses["IMG_3.MOV"], "needs_review")

    def test_error_on_remote_incomplete(self):
        self.file("x.jpg", b"a")
        self.assertEqual(self.report(remote=[media(1, "x.jpg", b"a")])[0], 0)
        bad = [{"id": "1", "type": "image", "name": "x.jpg", "hash": None}]
        rc, _, err = self.invoke("local-only", str(self.pictures), remote=bad)
        self.assertEqual(rc, 2)
        self.assertIn("invalid hash", err)

    def test_account_and_symlink_guards(self):
        self.file("x.jpg", b"a")
        self.assertEqual(self.report(remote=[])[0], 0)
        rc, _, err = self.invoke("local-only", str(self.pictures), remote=[])
        self.assertEqual(rc, 0, err)
        rootlink = self.base / "rootlink"
        rootlink.symlink_to(self.pictures, target_is_directory=True)
        rc, _, _ = self.invoke("local-only", str(rootlink), remote=[])
        self.assertEqual(rc, 2)
        with patch.object(prep, "fetch_remote", return_value=[]):
            self.assertEqual(prep.main(["--account", "wrong", "--db", str(self.db),
                                        "--camera-db", str(self.camera_db),
                                        "local-only", str(self.pictures)]), 2)




    def test_reflink_fallback_and_file_permissions(self):
        from unittest.mock import patch
        import errno
        self.file("Legacy/photo.jpg", b"original")
        target = self.base / "staging"
        with patch.object(prep.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "not supported")):
            rc, output, err = self.invoke("prepare-upload", str(self.pictures),
                                          "--output", str(target), "--apply", remote=[])
        self.assertEqual(rc, 0, err)
        manifest = json.loads((target / "manifest.json").read_text())
        self.assertEqual(manifest["files"][0]["method"], "copy")
        self.assertEqual((target / "media/Legacy/photo.jpg").read_bytes(), b"original")
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual((target / "manifest.json").stat().st_mode & 0o777, 0o600)


    def test_camera_database_is_read_only(self):
        camera_file = self.file("Camera/known.jpg", b"known file")
        st = camera_file.stat()
        with sqlite3.connect(self.camera_db) as conn:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('account','test')")
            conn.execute("CREATE TABLE ente_files(file_id TEXT,hash TEXT,name TEXT,type TEXT,last_seen_at TEXT,account TEXT)")
            conn.execute("CREATE TABLE local_files(path TEXT,size INT,mtime_ns INT,sha256 TEXT,blake2b TEXT)")
            conn.execute("INSERT INTO local_files VALUES(?,?,?,?,?)",
                         (str(camera_file), st.st_size, st.st_mtime_ns,
                          hashlib.sha256(b"known file").hexdigest(), digest(b"known file")))
        before = self.camera_db.read_bytes()
        with patch.object(prep, "hash_file", side_effect=AssertionError("rehashed camera file")):
            rc, report, err = self.report(remote=[])
        self.assertEqual(rc, 0, err)
        self.assertEqual(before, self.camera_db.read_bytes())
        self.assertEqual(report["files"][0]["hash"], digest(b"known file"))

    def test_staging_in_source_refused(self):
        self.file("photo.jpg", b"photo")
        nested = self.pictures / "staging"
        rc, output, err = self.invoke("prepare-upload", str(self.pictures),
                                      "--output", str(nested), "--apply", remote=[])
        self.assertEqual(rc, 2)
        self.assertFalse(nested.exists())
        self.assertIn("outside", err)


    def test_symlink_files_ignored(self):
        external = self.base / "external.jpg"
        external.write_bytes(b"secret")
        (self.pictures / "sym.jpg").symlink_to(external)
        self.file("ordinary.jpg", b"photo")
        rc, report, err = self.report(remote=[])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(report["files"]), 1)


    def test_rehash_changed_content_detected(self):
        file = self.file("photo.jpg", b"before")
        rc, report, err = self.report(remote=[])
        self.assertEqual(rc, 0, err)
        first = report["files"][0]["hash"]
        file.write_bytes(b"after!")
        rc, report, err = self.report(remote=[])
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(report["files"][0]["hash"], first)
        rc, report, err = self.report(remote=[], extra=("--rehash",))
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["files"][0]["hash"], digest(b"after!"))


    def test_large_remote_drop_refused(self):
        self.file("photo.jpg", b"new")
        remote = [media(i+1, f"photo{i}.jpg", bytes([i+1])) for i in range(25)]
        self.assertEqual(self.report(remote=remote)[0], 0)
        rc, _, err = self.report(remote=remote[:5])
        self.assertEqual(rc, 2)
        self.assertIn("shrank", err)
        rc, report, err = self.report(remote=remote[:5], extra=("--allow-large-drop",))
        self.assertEqual(rc, 0, err)


    def test_ambiguous_livephoto_requires_review(self):
        self.file("IMG_5.HEIC", b"image1")
        self.file("IMG_5.jpg", b"image2")
        self.file("IMG_5.MOV", b"video")
        rc, report, err = self.report(remote=[])
        self.assertEqual(rc, 0, err)
        self.assertEqual({r["status"] for r in report["files"]}, {"needs_review"})


if __name__ == "__main__":
    unittest.main()
