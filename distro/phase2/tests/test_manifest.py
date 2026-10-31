# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

PHASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("phase2_manifest", PHASE / "scripts/manifest.py")
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


class ArtifactSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        patch = mock.patch.object(manifest, "OUT", self.out)
        patch.start()
        self.addCleanup(patch.stop)

    def test_regular_file_roundtrip_and_mutation(self):
        path = self.out / "image"
        path.write_bytes(b"original")
        record = manifest.file_record("image")
        manifest.validate_file_record(record, "image")
        path.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "changed after build"):
            manifest.validate_file_record(record, "image")

    def test_rejects_path_escape(self):
        for path in ("../image", "/tmp/image", "x/../../image", ""):
            with self.assertRaises(ValueError):
                manifest.safe_file(path, missing=True)

    def test_rejects_symlink_and_hardlink(self):
        target = self.out / "target"
        target.write_text("test")
        (self.out / "link").symlink_to(target)
        with self.assertRaises(ValueError):
            manifest.safe_file("link")
        os.link(target, self.out / "hardlink")
        with self.assertRaises(ValueError):
            manifest.safe_file("hardlink")

    def test_rejects_symlink_parent_and_fifo(self):
        (self.out / "dir").mkdir()
        (self.out / "alias").symlink_to(self.out / "dir", target_is_directory=True)
        with self.assertRaises(ValueError):
            manifest.safe_file("alias/new", missing=True)
        os.mkfifo(self.out / "fifo")
        with self.assertRaises(ValueError):
            manifest.safe_file("fifo")

    def test_unknown_or_missing_results_never_pass(self):
        for result in ({}, {"schema_version": 1, "status": "not_run"}, {"schema_version": 1, "status": "fail"}):
            with self.assertRaisesRegex(ValueError, "has not passed"):
                manifest.validate_results({}, result)

    def test_other_build_report_rejected(self):
        (self.out / "manifest.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "another manifest"):
            manifest.validate_results({}, {"schema_version": 1, "status": "pass", "manifest_sha256": "0" * 64})


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "tracked").write_text("initial\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"], check=True)
        patch = mock.patch.object(manifest, "ROOT", self.repo)
        patch.start()
        self.addCleanup(patch.stop)

    def test_tracked_edits_change_identity_without_changing_sha(self):
        before = manifest.source_fingerprint()
        (self.repo / "tracked").write_text("changed\n")
        self.assertNotEqual(before, manifest.source_fingerprint())
        self.assertTrue(manifest.source_state()[1])

    def test_untracked_file_bytes_and_mode_are_bound(self):
        before = manifest.source_fingerprint()
        path = self.repo / "new"
        path.write_bytes(b"a\0b")
        added = manifest.source_fingerprint()
        self.assertNotEqual(before, added)
        path.chmod(0o755)
        self.assertNotEqual(added, manifest.source_fingerprint())

    def test_untracked_symlink_target_is_bound_without_following(self):
        link = self.repo / "link"
        link.symlink_to("missing-a")
        before = manifest.source_fingerprint()
        link.unlink()
        link.symlink_to("missing-b")
        self.assertNotEqual(before, manifest.source_fingerprint())


if __name__ == "__main__":
    unittest.main()
