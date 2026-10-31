# SPDX-License-Identifier: GPL-2.0-only
"""Mock-only provenance regressions: never accept altered/stale/partial results."""

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manifest.py"
SPEC = importlib.util.spec_from_file_location("phase1_manifest", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
manifest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manifest
SPEC.loader.exec_module(manifest)

SOURCE = "a" * 40
RELEASE = "7.3.0-rc7-phase1"
FINGERPRINT = "e" * 64
MANIFEST_DIGEST = "f" * 64


class FakePath:
    """The verifier only reads these methods; no temporary files are needed."""

    def __init__(self, name, store, files, symlinks):
        self.name = name
        self.store = store
        self.files = files
        self.symlinks = symlinks

    def __truediv__(self, other):
        return FakePath(self.name + "/" + str(other), self.store, self.files, self.symlinks)

    def read_text(self):
        if self.name not in self.store:
            raise FileNotFoundError(self.name)
        return self.store[self.name]

    def read_bytes(self):
        return self.read_text().encode()

    def is_file(self):
        return self.name in self.files

    def is_symlink(self):
        return self.name in self.symlinks


class TestManifestVerification(unittest.TestCase):
    def setUp(self):
        self.state = (SOURCE, False, SOURCE)
        self.fingerprint = FINGERPRINT
        self.files = {"out/" + relative for relative in manifest.FILES.values()}
        self.symlinks = set()
        self.hashes = {name: "b" * 64 for name in self.files}
        self.hashes["out/manifest.json"] = MANIFEST_DIGEST
        self.payload = {
            "schema_version": 1,
            "source_sha": SOURCE,
            "source_revision": SOURCE,
            "source_fingerprint": FINGERPRINT,
            "kernel_release": RELEASE,
            "outputs": {
                name: {"path": relative, "sha256": "b" * 64, "bytes": 1}
                for name, relative in manifest.FILES.items()
            },
        }
        self.results = {
            "status": "passed",
            "source_revision": SOURCE,
            "kernel_release": RELEASE,
            "manifest_sha256": MANIFEST_DIGEST,
            "cases": [
                {"name": name, "status": "passed"}
                for name in ("round1", "round2", "interactive", "negative")
            ],
        }

    def verify(self, ci=False):
        store = {
            "out/manifest.json": json.dumps(self.payload),
            "out/test-results.json": json.dumps(self.results),
            "out/source-fingerprint": FINGERPRINT + "\n",
        }
        output = FakePath("out", store, self.files, self.symlinks)
        with (
            patch.object(manifest, "OUT", output),
            patch.object(manifest, "source_state", return_value=self.state),
            patch.object(manifest, "source_fingerprint", return_value=self.fingerprint, create=True),
            patch.object(manifest, "digest", side_effect=lambda path: self.hashes[path.name]),
            patch.dict(manifest.os.environ, {"CI": "true" if ci else "false"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            manifest.verify()

    def test_complete_current_evidence_passes(self):
        self.verify()

    def test_wrong_commit_rejected(self):
        self.state = ("c" * 40, False, "c" * 40)
        with self.assertRaisesRegex(ValueError, "checkout changed"):
            self.verify()

    def test_became_dirty_after_build_rejected(self):
        self.state = (SOURCE, True, SOURCE + "-dirty")
        with self.assertRaisesRegex(ValueError, "checkout changed"):
            self.verify()

    def test_dirty_ci_rejected_even_when_revision_matches(self):
        self.state = (SOURCE, True, SOURCE + "-dirty")
        self.payload["source_revision"] = self.state[2]
        self.results["source_revision"] = self.state[2]
        with self.assertRaisesRegex(ValueError, "dirty CI"):
            self.verify(ci=True)

    def test_unknown_schema_rejected(self):
        self.payload["schema_version"] = 99
        with self.assertRaisesRegex(ValueError, "schema"):
            self.verify()

    def test_path_substitution_rejected(self):
        self.payload["outputs"]["kernel"]["path"] = "../../unrelated-kernel"
        with self.assertRaisesRegex(ValueError, "artifact path"):
            self.verify()

    def test_missing_kernel_rejected(self):
        self.files.remove("out/" + manifest.FILES["kernel"])
        with self.assertRaisesRegex(ValueError, "missing/unsafe"):
            self.verify()

    def test_symlink_kernel_rejected(self):
        self.symlinks.add("out/" + manifest.FILES["kernel"])
        with self.assertRaisesRegex(ValueError, "missing/unsafe"):
            self.verify()

    def test_changed_kernel_bytes_rejected(self):
        self.hashes["out/" + manifest.FILES["kernel"]] = "d" * 64
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.verify()

    def test_failed_overall_result_rejected(self):
        self.results["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "not passed"):
            self.verify()

    def test_results_from_other_source_rejected(self):
        self.results["source_revision"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "another source"):
            self.verify()

    def test_results_from_other_kernel_rejected(self):
        self.results["kernel_release"] = "unrelated-kernel"
        with self.assertRaisesRegex(ValueError, "another kernel"):
            self.verify()

    def test_missing_case_rejected(self):
        self.results["cases"].pop()
        with self.assertRaisesRegex(ValueError, "cases missing"):
            self.verify()

    def test_duplicate_case_cannot_replace_negative_control(self):
        self.results["cases"][-1] = copy.deepcopy(self.results["cases"][0])
        with self.assertRaisesRegex(ValueError, "cases missing"):
            self.verify()

    def test_failed_case_under_passing_summary_rejected(self):
        self.results["cases"][0]["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "case failed"):
            self.verify()

    def test_already_dirty_source_content_changed_rejected(self):
        self.state = (SOURCE, True, SOURCE + "-dirty")
        self.payload["source_revision"] = self.state[2]
        self.results["source_revision"] = self.state[2]
        self.fingerprint = "0" * 64
        with self.assertRaises(ValueError):
            self.verify()

    def test_passing_results_bound_to_other_manifest_rejected(self):
        self.results["manifest_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.verify()

    def test_results_without_manifest_binding_rejected(self):
        del self.results["manifest_sha256"]
        with self.assertRaises((ValueError, KeyError)):
            self.verify()


if __name__ == "__main__":
    unittest.main()
