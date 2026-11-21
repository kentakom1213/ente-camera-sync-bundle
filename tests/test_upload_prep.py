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
        rows = {r["relative_path"]: r for r in doc["files"]}
        self.assertEqual(rows["Camera/present.jpg"]["status"], "uploaded")
        self.assertTrue(rows["Camera/present.jpg"]["name_mismatch"])
        self.assertEqual(rows["Old/duplicate.jpg"]["status"], "uploaded")
        self.assertEqual(rows["Old/copy.jpg"]["status"], "local_only")
        self.assertEqual(rows["Old/only.jpg"]["status"], "local_only")
        self.assertEqual(rows["Old/only.jpg"]["duplicate_of"], "Old/copy.jpg")
        self.assertIsNone(rows["Old/copy.jpg"]["duplicate_of"])
        self.assertEqual(len(rows), 4)
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

    def test_explicit_dry_run_does_not_create_staging_or_cache(self):
        self.file("Old/2021/leaf.jpg", b"private photo")
        stage = self.base / "preview"
        self.assertFalse(self.db.exists())
        rc, output, error = self.invoke(
            "prepare-upload", str(self.pictures),
            "--output", str(stage), "--dry-run", remote=[])
        self.assertEqual(rc, 0, error)
        self.assertIn("Dry run", output)
        self.assertIn("Selected 1 files", output)
        self.assertFalse(stage.exists())
        self.assertFalse(self.db.exists())
        self.assertFalse((self.base / ".ente-upload-prep.lock").exists())


    def test_explicit_dry_run_uses_snapshot_without_database_mutation(self):
        self.file("Original/a.jpg", b"photo A")
        first = self.base / "first"
        rc, _, error = self.invoke(
            "prepare-upload", str(self.pictures),
            "--output", str(first), remote=[])
        self.assertEqual(rc, 0, error)
        original_db = self.db.read_bytes()
        self.file("New/b.jpg", b"photo B")
        preview = self.base / "second"
        rc, output, error = self.invoke(
            "prepare-upload", str(self.pictures),
            "--output", str(preview), "--dry-run", remote=[])
        self.assertEqual(rc, 0, error)
        self.assertIn("New/b.jpg", output)
        self.assertIn("Selected 2 files", output)  # No persistent batch suppression
        self.assertFalse(preview.exists())
        self.assertEqual(self.db.read_bytes(), original_db)

    def test_apply_option_is_removed(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            prep.parser().parse_args([
                "--account", "test", "prepare-upload", str(self.pictures),
                "--output", str(self.base / "batch"),
                "--apply",
            ])
        self.assertEqual(error.exception.code, 2)


    def test_batch_preserves_paths_and_recheck(self):
        src = self.file("Old/2021/leaf.jpg", b"private photo")
        stage = self.base / "stage"
        rc, text, err = self.invoke("prepare-upload", str(self.pictures), "--output", str(stage), "--dry-run", remote=[])
        self.assertEqual(rc, 0, err)
        self.assertFalse(stage.exists())
        rc, text, err = self.invoke("prepare-upload", str(self.pictures), "--output", str(stage))
        self.assertEqual(rc, 0, err)
        staged = stage / "media/Old/2021/leaf.jpg"
        self.assertEqual(staged.read_bytes(), b"private photo")
        self.assertEqual(src.read_bytes(), b"private photo")
        self.assertNotEqual(src.stat().st_ino, staged.stat().st_ino)  # no hard links
        manifest = json.loads((stage / "manifest.json").read_text())
        self.assertEqual(len(manifest["files"]), 1)
        self.assertFalse((stage / "manifest.csv").exists())
        with sqlite3.connect(self.db) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse({"batches", "batch_files"} & tables)
        # Preparing the same content in another destination is intentional.
        rc, _, _ = self.invoke("prepare-upload", str(self.pictures), "--output", str(self.base / "batch2"))
        self.assertEqual(rc, 0)
        rc, _, _ = self.invoke("verify-upload", str(stage / "manifest.json"), remote=[])
        self.assertEqual(rc, 1)
        rc, text, _ = self.invoke("verify-upload", str(stage / "manifest.json"),
                                  remote=[media(2, "leaf.jpg", b"private photo")])
        self.assertEqual(rc, 0)
        self.assertIn("verified: 1", text)


    def test_livephoto_pair_staged_and_verified_as_composite(self):
        self.file("Camera/IMG_3.HEIC", b"photo")
        self.file("Camera/IMG_3.MOV", b"movie")
        stage = self.base / "one"
        rc, output, err = self.invoke("prepare-upload", str(self.pictures),
                                      "--output", str(stage), remote=[])
        self.assertEqual(rc, 0, output + err)
        self.assertEqual(len(list((stage / "media").rglob("*.*"))), 2)
        live = {"id": "4", "name": "IMG_3.HEIC", "type": "livephoto",
                "hash": digest(b"photo") + ":" + digest(b"movie")}
        rc, text, err = self.invoke("verify-upload", str(stage / "manifest.json"), remote=[live])
        self.assertEqual(rc, 0, text + err)
        self.assertIn("verified: 2", text)


    def test_flatten_colliding_names_are_both_renamed_and_verifiable(self):
        self.file("Camera/photo.jpg", b"one")
        self.file("Old/photo.jpg", b"two")
        self.file("Screenshots/unique.png", b"three")
        stage = self.base / "flat"
        rc, preview, err = self.invoke("prepare-upload", str(self.pictures),
                                       "--flatten", "--output", str(stage), "--dry-run", remote=[])
        self.assertEqual(rc, 0, err)
        self.assertIn("Selected 3 files", preview)
        self.assertFalse(stage.exists())
        rc, _, err = self.invoke("prepare-upload", str(self.pictures),
                                 "--flatten", "--output", str(stage))
        self.assertEqual(rc, 0, err)
        doc = json.loads((stage / "manifest.json").read_text())
        names = {row["relative_path"]: Path(row["staged"]).name for row in doc["files"]}
        self.assertTrue(names["Camera/photo.jpg"].startswith("photo__"))
        self.assertTrue(names["Old/photo.jpg"].startswith("photo__"))
        self.assertNotEqual(names["Camera/photo.jpg"], names["Old/photo.jpg"])
        self.assertEqual(names["Screenshots/unique.png"], "unique.png")
        self.assertFalse((stage / "media/photo.jpg").exists())
        remote = [media(1, names["Camera/photo.jpg"], b"one"),
                  media(2, names["Old/photo.jpg"], b"two"),
                  media(3, "unique.png", b"three")]
        rc, result, err = self.invoke("verify-upload", str(stage / "manifest.json"), remote=remote)
        self.assertEqual(rc, 0, err)
        self.assertIn("verified: 3", result)

    def test_flatten_case_and_unicode_normalization_collisions(self):
        self.file("A/Photo.JPG", b"upper")
        self.file("B/photo.jpg", b"lower")
        self.file("C/café.jpg", b"nfc")
        self.file("D/cafe\u0301.jpg", b"nfd")
        stage = self.base / "flat"
        rc, _, err = self.invoke("prepare-upload", str(self.pictures),
                                 "--flatten", "--output", str(stage), remote=[])
        self.assertEqual(rc, 0, err)
        paths = [p.name for p in (stage / "media").iterdir()]
        self.assertEqual(len(paths), 4)
        self.assertEqual(len({prep.normalized_name(name) for name in paths}), 4)
        self.assertTrue(all("__" in name for name in paths))

    def test_flatten_generated_suffix_collision(self):
        from hashlib import sha256
        self.file("A/photo.jpg", b"first")
        self.file("B/photo.jpg", b"second")
        generated_name = f"photo__{sha256(b'first').hexdigest()[:8]}.jpg"
        self.file("C/" + generated_name, b"third")
        stage = self.base / "flat"
        rc, _, err = self.invoke("prepare-upload", str(self.pictures),
                                 "--flatten", "--output", str(stage), remote=[])
        self.assertEqual(rc, 0, err)
        doc = json.loads((stage / "manifest.json").read_text())
        names = [Path(row["staged"]).name for row in doc["files"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 3)
        self.assertNotIn(generated_name, names)

    def test_flatten_live_photo_pair_keeps_matching_suffix(self):
        self.file("A/IMG_1.HEIC", b"image A")
        self.file("A/IMG_1.MOV", b"video A")
        self.file("B/IMG_1.HEIC", b"image B")
        self.file("B/IMG_1.MOV", b"video B")
        stage = self.base / "flat"
        rc, _, err = self.invoke("prepare-upload", str(self.pictures),
                                 "--flatten", "--output", str(stage), remote=[])
        self.assertEqual(rc, 0, err)
        doc = json.loads((stage / "manifest.json").read_text())
        by_parent = {}
        for row in doc["files"]:
            by_parent.setdefault(Path(row["relative_path"]).parts[0], []).append(Path(row["staged"]).name)
        self.assertEqual(set(by_parent), {"A", "B"})
        for filenames in by_parent.values():
            self.assertEqual(len(filenames), 2)
            stems = {Path(name).stem for name in filenames}
            self.assertEqual(len(stems), 1)
            self.assertIn("__", next(iter(stems)))
        remote = [
            {"id": "11", "name": "IMG_1.HEIC", "type": "livephoto",
             "hash": digest(b"image A") + ":" + digest(b"video A")},
            {"id": "12", "name": "IMG_1.HEIC", "type": "livephoto",
             "hash": digest(b"image B") + ":" + digest(b"video B")},
        ]
        rc, text, err = self.invoke("verify-upload", str(stage / "manifest.json"), remote=remote)
        self.assertEqual(rc, 0, err)
        self.assertIn("verified: 4", text)

    def test_flatten_skips_local_duplicates(self):
        self.file("A/one.jpg", b"same")
        self.file("B/two.jpg", b"same")
        stage = self.base / "flat"
        rc, output, err = self.invoke("prepare-upload", str(self.pictures),
                                      "--flatten", "--output", str(stage), remote=[])
        self.assertEqual(rc, 0, err)
        self.assertIn("Selected 1 files", output)
        self.assertEqual(len(list((stage / "media").iterdir())), 1)

    def test_legacy_batch_tables_are_ignored_without_deleting_old_data(self):
        self.file("A/photo.jpg", b"photo")
        self.assertEqual(self.report(remote=[])[0], 0)
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE batches(batch_id TEXT)")
            db.execute("INSERT INTO batches VALUES ('old')")
            db.execute("CREATE TABLE batch_files(batch_id TEXT, hash TEXT)")
            db.execute("INSERT INTO batch_files VALUES ('old', ?)", (digest(b"photo"),))
        stage = self.base / "stage"
        rc, _, err = self.invoke("prepare-upload", str(self.pictures),
                                 "--output", str(stage), remote=[])
        self.assertEqual(rc, 0, err)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)
        self.assertTrue((stage / "media/A/photo.jpg").exists())

    def test_removed_limit_and_batch_options_are_rejected(self):
        for option, value in (("--max-files", "3"), ("--max-bytes", "5GiB"),
                              ("--include-staged", None)):
            args = ["--account", "test", "prepare-upload", str(self.pictures),
                    "--output", str(self.base / "stage"), option]
            if value is not None:
                args.append(value)
            with self.subTest(option=option), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    prep.parser().parse_args(args)
                self.assertEqual(error.exception.code, 2)


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
                                          "--output", str(target), remote=[])
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
                                      "--output", str(nested), remote=[])
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
