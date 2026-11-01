# SPDX-License-Identifier: GPL-2.0-only
"""Fail-closed Phase 3 artifact/provenance unit tests (not VM qualification)."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "manifest.py"
SPEC = importlib.util.spec_from_file_location("phase3_manifest_unit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
manifest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manifest
SPEC.loader.exec_module(manifest)


class ArtifactSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="phase3-manifest-test-")
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name).resolve()
        self.patch = patch.object(manifest, "OUT", self.out)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_regular_file_record(self):
        (self.out / "proof.log").write_bytes(b"actual bytes\n")
        record = manifest.record("proof.log")
        self.assertEqual(record["sha256"], hashlib.sha256(b"actual bytes\n").hexdigest())
        self.assertEqual(record["bytes"], 13)
        manifest.check_record(record, "proof.log")

    def test_mutated_bytes_fail_closed(self):
        file = self.out / "proof.log"
        file.write_text("original")
        record = manifest.record("proof.log")
        file.write_text("modified")
        with self.assertRaises(ValueError):
            manifest.check_record(record, "proof.log")

    def test_path_escape_refused(self):
        for value in ("../other", "/etc/passwd", "sub/../../other"):
            with self.subTest(path=value):
                with self.assertRaises(ValueError):
                    manifest.safe_file(value, missing=True)

    def test_symlink_and_hardlink_refused(self):
        orig = self.out / "proof.log"
        orig.write_text("proof")
        (self.out / "link.log").symlink_to(orig)
        with self.assertRaises(ValueError):
            manifest.safe_file("link.log")
        (self.out / "hardlink.log").hardlink_to(orig)
        with self.assertRaises(ValueError):
            manifest.safe_file("hardlink.log")

    def test_symlinked_parent_refused(self):
        outside = self.out / "real"
        outside.mkdir()
        (self.out / "alias").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            manifest.safe_file("alias/forged.log", missing=True)

    def test_missing_report_fails_verification(self):
        with patch.object(manifest, "check_build", return_value={
            "source_revision": "a" * 40, "kernel_release": "7.3.0-phase3"
        }):
            with self.assertRaises(FileNotFoundError):
                manifest.verify()

    def test_incomplete_report_cannot_be_qualified(self):
        (self.out / "manifest.json").write_text('{"phase":3}')
        (self.out / "test-results.json").write_text(json.dumps({
            "schema_version": 1, "status": "pass", "cases": [],
            "manifest_sha256": manifest.digest(self.out / "manifest.json"),
            "source_revision": "a" * 40, "kernel_release": "7.3.0-phase3",
        }))
        with patch.object(manifest, "check_build", return_value={
            "source_revision": "a" * 40, "kernel_release": "7.3.0-phase3"
        }):
            with self.assertRaises(ValueError):
                manifest.verify()

    def test_not_run_report_cannot_be_qualified(self):
        (self.out / "test-results.json").write_text('{"schema_version":1,"status":"not_run"}')
        with patch.object(manifest, "check_build", return_value={
            "source_revision": "a" * 40, "kernel_release": "7.3.0-phase3"
        }):
            with self.assertRaises(ValueError):
                manifest.verify()


if __name__ == "__main__":
    unittest.main()
