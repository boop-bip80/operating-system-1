# SPDX-License-Identifier: GPL-2.0
"""Host-only regressions: prove the smoke parser cannot greenwash failed boots."""

import importlib.util
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke-test.py"
SPEC = importlib.util.spec_from_file_location("phase1_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)

RELEASE = "7.3.0-rc7-phase1"
SOURCE = "a" * 40
NONCE = "0123456789abcdef0123456789abcdef"


def output_for(round_number=1):
    return "\n".join(
        [f"Linux version {RELEASE} (builder)", f"P1:KERNEL:{RELEASE}", f"P1:SOURCE:{SOURCE}"]
        + list(smoke.ROUND_MARKERS[round_number])
        + ["[    1.002003] reboot: Power down", ""]
    )


class TestPositiveValidation(unittest.TestCase):
    def check(self, output=None, round_number=1, **kwargs):
        result = smoke.BootResult(
            name="test", output=output_for(round_number) if output is None else output,
            returncode=kwargs.pop("returncode", 0), **kwargs
        )
        smoke.validate_test_boot(result, round_number, RELEASE, SOURCE)

    def test_round1_passes(self):
        self.check()

    def test_round2_passes(self):
        self.check(round_number=2)

    def test_crlf_passes(self):
        self.check(output_for().replace("\n", "\r\n"))

    def test_missing_marker_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for().replace("P1:NET_OK\n", ""))

    def test_duplicate_marker_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for().replace("P1:NET_OK\n", "P1:NET_OK\nP1:NET_OK\n"))

    def test_reordered_markers_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for().replace(
                "P1:FS_OK\nP1:DISK_WRITE_OK", "P1:DISK_WRITE_OK\nP1:FS_OK"
            ))

    def test_echoed_marker_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for().replace("P1:NET_OK\n", "# echo P1:NET_OK\n"))

    def test_wrong_kernel_rejected(self):
        with self.assertRaisesRegex(smoke.SmokeFailure, "AC-02"):
            self.check(output_for().replace(f"P1:KERNEL:{RELEASE}", "P1:KERNEL:host-kernel"))

    def test_wrong_source_rejected(self):
        with self.assertRaisesRegex(smoke.SmokeFailure, "AC-01"):
            self.check(output_for().replace(f"P1:SOURCE:{SOURCE}", "P1:SOURCE:wrong"))

    def test_multiple_kernel_identities_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check("P1:KERNEL:unrelated\n" + output_for())

    def test_panic_exit_zero_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for() + "Kernel panic - not syncing: test\n")

    def test_oops_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for() + "Oops: 0000 [#1]\n")

    def test_guest_failure_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for() + "P1:FAIL:AC-08:write failed\n")

    def test_timeout_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(timed_out=True)

    def test_host_failure_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(returncode=1)

    def test_no_shutdown_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for().replace("[    1.002003] reboot: Power down", ""))

    def test_wrong_round_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_for() + "P1:PASS\n")

    def test_shutdown_before_success_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check("reboot: Power down\n" + output_for().replace(
                "[    1.002003] reboot: Power down", ""
            ))

    def test_log_flood_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(output_limit=True)


class TestInteractiveValidation(unittest.TestCase):
    def result(self):
        return smoke.BootResult(
            name="interactive", returncode=0, interaction_sent=True,
            output="\n".join([
                f"P1:KERNEL:{RELEASE}", f"P1:SOURCE:{SOURCE}", "P1:INIT_OK",
                "P1:SHELL_READY", f"P1:INTERACTIVE_KERNEL:{RELEASE}",
                f"P1:INTERACTIVE_OK:{NONCE}", "reboot: Power down", "",
            ]),
        )

    def test_interactive_passes(self):
        smoke.validate_interactive_boot(self.result(), RELEASE, SOURCE, NONCE)

    def test_no_input_rejected(self):
        result = self.result()
        result.interaction_sent = False
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_interactive_boot(result, RELEASE, SOURCE, NONCE)

    def test_echoed_input_not_evidence(self):
        result = self.result()
        result.output = result.output.replace(
            f"P1:INTERACTIVE_OK:{NONCE}", f"# printf '%s' 'P1:INTERACTIVE_OK:{NONCE}'"
        )
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_interactive_boot(result, RELEASE, SOURCE, NONCE)

    def test_automated_mode_not_interactive(self):
        result = self.result()
        result.output += "P1:PASS\n"
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_interactive_boot(result, RELEASE, SOURCE, NONCE)

    def test_commands_cannot_echo_full_nonce_marker(self):
        command = smoke.interactive_commands(NONCE).decode()
        self.assertNotIn(f"P1:INTERACTIVE_OK:{NONCE}", command)
        self.assertNotIn(f"P1:INTERACTIVE_KERNEL:{RELEASE}", command)

    def test_unsafe_nonce_rejected(self):
        with self.assertRaises(ValueError):
            smoke.interactive_commands("$(rm -rf /)")


class TestNegativeValidation(unittest.TestCase):
    def result(self):
        return smoke.BootResult(
            name="negative", returncode=0,
            output=(f"Linux version {RELEASE} (builder)\n"
                    "Kernel panic - not syncing: No working init found.\n"),
        )

    def test_expected_panic_passes_negative_control(self):
        smoke.validate_negative_boot(self.result(), RELEASE)

    def test_vfs_panic_is_expected_when_no_init_or_root(self):
        result = self.result()
        result.output = result.output.replace(
            "No working init found.", "VFS: Unable to mount root fs on unknown-block(0,0)"
        )
        smoke.validate_negative_boot(result, RELEASE)

    def test_host_launch_failure_rejected(self):
        result = smoke.BootResult("negative", "qemu: file not found\n", 1)
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE)

    def test_arbitrary_panic_rejected(self):
        result = self.result()
        result.output = result.output.replace("No working init found.", "memory corruption")
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE)

    def test_no_kernel_evidence_rejected(self):
        result = self.result()
        result.output = result.output.split("\n", 1)[1]
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE)

    def test_wrong_kernel_evidence_rejected(self):
        result = self.result()
        result.output = result.output.replace(RELEASE, "host-kernel")
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE)

    def test_reached_userspace_rejected(self):
        result = self.result()
        result.output += "P1:INIT_OK\n"
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE)

    def test_timed_out_negative_rejected(self):
        result = self.result()
        result.timed_out = True
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE)


class TestManifestBinding(unittest.TestCase):
    def test_digest_hashes_exact_manifest_bytes(self):
        raw = b'{"source_revision":"same-commit","kernel_sha256":"new-image"}\n'
        with patch.object(Path, "read_bytes", return_value=raw):
            self.assertEqual(smoke.manifest_digest(Path("manifest.json")), hashlib.sha256(raw).hexdigest())

    def test_unchanged_manifest_is_accepted(self):
        raw = b'{"kernel_sha256":"same-image"}\n'
        with patch.object(Path, "read_bytes", return_value=raw):
            smoke.validate_manifest_unchanged(Path("manifest.json"), hashlib.sha256(raw).hexdigest())

    def test_rebuilt_image_same_source_is_rejected(self):
        old = b'{"source_revision":"same-commit","kernel_sha256":"old-image"}\n'
        new = b'{"source_revision":"same-commit","kernel_sha256":"new-image"}\n'
        with patch.object(Path, "read_bytes", return_value=new):
            with self.assertRaisesRegex(smoke.SmokeFailure, "manifest changed"):
                smoke.validate_manifest_unchanged(Path("manifest.json"), hashlib.sha256(old).hexdigest())


if __name__ == "__main__":
    unittest.main()
