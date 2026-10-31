# SPDX-License-Identifier: GPL-2.0-only
"""Kconfig assertions must check resolved values, not only requested fragments."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate-config.py"
SPEC = importlib.util.spec_from_file_location("phase1_validate_config", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config
SPEC.loader.exec_module(config)


def validate(requested, actual):
    files = {"requested": requested, "actual": actual}
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(Path, "read_text", lambda path: files[str(path)]), \
         patch.object(sys, "argv", ["validate-config.py", "requested", "actual"]), \
         redirect_stdout(stdout), redirect_stderr(stderr):
        result = config.main()
    return result, stdout.getvalue(), stderr.getvalue()


class TestConfigValidation(unittest.TestCase):
    def test_parse_enabled_disabled_and_string_values(self):
        text = 'CONFIG_FOO=y\n# CONFIG_BAR is not set\nCONFIG_LOCALVERSION="-phase1"\n'
        with patch.object(Path, "read_text", return_value=text):
            self.assertEqual(config.parse_config("config"), {
                "CONFIG_FOO": "y", "CONFIG_BAR": "n", "CONFIG_LOCALVERSION": '"-phase1"'
            })

    def test_exact_effective_values_pass(self):
        result, _, _ = validate("CONFIG_VIRTIO_BLK=y\n", "CONFIG_VIRTIO_BLK=y\n")
        self.assertEqual(result, 0)

    def test_module_is_not_builtin(self):
        result, _, error = validate("CONFIG_VIRTIO_BLK=y\n", "CONFIG_VIRTIO_BLK=m\n")
        self.assertEqual(result, 1)
        self.assertIn("CONFIG_VIRTIO_BLK: required y, resolved m", error)

    def test_unresolved_dependency_is_failure(self):
        result, _, error = validate("CONFIG_BINFMT_SCRIPT=y\n", "# CONFIG_BINFMT_SCRIPT is not set\n")
        self.assertEqual(result, 1)
        self.assertIn("AC-03", error)

    def test_absent_required_symbol_is_failure(self):
        result, _, _ = validate("CONFIG_SERIAL_8250_CONSOLE=y\n", "")
        self.assertEqual(result, 1)

    def test_disabled_symbol_may_be_absent(self):
        result, _, _ = validate("# CONFIG_MODULES is not set\n", "")
        self.assertEqual(result, 0)

    def test_disabled_symbol_must_not_be_enabled(self):
        result, _, _ = validate("# CONFIG_MODULES is not set\n", "CONFIG_MODULES=y\n")
        self.assertEqual(result, 1)

    def test_wrong_local_version_is_failure(self):
        result, _, _ = validate('CONFIG_LOCALVERSION="-phase1"\n', 'CONFIG_LOCALVERSION=""\n')
        self.assertEqual(result, 1)

    def test_unrequested_default_symbols_allowed(self):
        result, _, _ = validate("CONFIG_TMPFS=y\n", "CONFIG_TMPFS=y\nCONFIG_OTHER=y\n")
        self.assertEqual(result, 0)

    def test_multiple_failures_are_reported(self):
        result, _, error = validate("CONFIG_A=y\nCONFIG_B=y\n", "CONFIG_A=m\nCONFIG_B=n\n")
        self.assertEqual(result, 1)
        self.assertIn("CONFIG_A", error)
        self.assertIn("CONFIG_B", error)


if __name__ == "__main__":
    unittest.main()
