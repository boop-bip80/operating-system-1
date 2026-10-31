# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
from pathlib import Path
import tempfile
import unittest

PHASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("probe_state", PHASE / "rootfs/usr/lib/phase2/probe_state.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class InputStateTests(unittest.TestCase):
    def type_nonce(self, state, nonce="0123456789abcdef" * 2):
        for letter in nonce:
            self.assertFalse(state.key(letter, letter))
        return nonce

    def test_challenge_only_commits_after_enter(self):
        state = probe.ProbeState()
        self.type_nonce(state)
        self.assertEqual(state.stage, "ready")
        self.assertTrue(state.key("Return", "\r"))
        self.assertEqual(state.stage, "keyboard")
        self.assertFalse(state.key("Return", "\r"))

    def test_short_challenge_cannot_pass(self):
        state = probe.ProbeState()
        self.type_nonce(state, "abc")
        self.assertFalse(state.key("Return", "\r"))
        self.assertEqual(state.stage, "ready")

    def test_ignores_invalid_keys_and_caps_length(self):
        state = probe.ProbeState()
        for value in ("Z", "A", "g", " ", "xx", "\n", ""):
            state.key(value, value)
        self.assertEqual(state.text, "")
        self.type_nonce(state)
        state.key("a", "a")
        self.assertEqual(len(state.text), 32)

    def test_backspace_and_retyping(self):
        state = probe.ProbeState()
        self.type_nonce(state, "012")
        state.key("BackSpace", "")
        self.assertEqual(state.text, "01")

    def test_click_requires_keyboard_left_button_and_inside_rectangle(self):
        state = probe.ProbeState()
        self.assertFalse(state.can_click(1, 512, 490))
        self.type_nonce(state)
        state.key("Return", "\r")
        self.assertTrue(state.can_click(1, 512, 490))
        for button, x, y in ((2, 512, 490), (1, 311, 490), (1, 712, 490), (1, 512, 439), (1, 512, 540)):
            self.assertFalse(state.can_click(button, x, y))
        state.stage = "pointer"
        self.assertFalse(state.can_click(1, 512, 490))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_empty_then_atomic_roundtrip(self):
        self.assertEqual(probe.read_nonce(self.directory), "EMPTY")
        first, second = "a" * 32, "b" * 32
        probe.save_nonce(self.directory, first)
        self.assertEqual(probe.read_nonce(self.directory), first)
        probe.save_nonce(self.directory, second)
        self.assertEqual(probe.read_nonce(self.directory), second)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["input-nonce"])
        self.assertEqual((self.directory / "input-nonce").stat().st_mode & 0o777, 0o600)

    def test_rejects_invalid_nonce_before_writing(self):
        for value in ("", "a" * 31, "A" * 32, "a" * 33, "a" * 32 + "\n", "../../target"):
            with self.assertRaises(ValueError):
                probe.save_nonce(self.directory, value)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_rejects_corrupt_persisted_data(self):
        path = self.directory / "input-nonce"
        for value in ("a" * 32, "a" * 32 + "x", "A" * 32 + "\n", "x" * 200):
            path.write_text(value)
            with self.assertRaises(ValueError):
                probe.read_nonce(self.directory)

    def test_does_not_follow_persistence_symlink(self):
        target = self.directory / "target"
        target.write_text("a" * 32 + "\n")
        (self.directory / "input-nonce").symlink_to(target)
        with self.assertRaises(ValueError):
            probe.read_nonce(self.directory)
        # Atomic replace changes the link itself, never its external target.
        probe.save_nonce(self.directory, "b" * 32)
        self.assertEqual(target.read_text(), "a" * 32 + "\n")
        self.assertEqual(probe.read_nonce(self.directory), "b" * 32)

    def test_rejects_symlink_directory(self):
        link = self.directory / "alias"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            probe.save_nonce(link, "a" * 32)


class GuestConfigurationTests(unittest.TestCase):
    def test_session_is_nonroot_pam_managed_and_not_world_writable(self):
        unit = (PHASE / "rootfs/etc/systemd/system/phase2-desktop.service").read_text()
        self.assertIn("User=desktop\n", unit)
        self.assertIn("PAMName=login\n", unit)
        self.assertIn("TTYPath=/dev/tty1\n", unit)
        self.assertNotIn("NotifyAccess=all", unit)
        script = (PHASE / "rootfs/usr/lib/phase2/start-desktop.sh").read_text()
        self.assertIn("--backend=drm --renderer=pixman", script)
        self.assertNotIn("mkdir /run/user", script)
        self.assertNotIn("chmod 777", script)

    def test_host_challenge_not_guest_configured(self):
        script = (PHASE / "rootfs/usr/lib/phase2/input-probe.py").read_text()
        self.assertNotIn("sys.argv[", script)
        self.assertNotIn("token_hex", script)
        self.assertNotIn("/proc/cmdline", script)
        self.assertIn('window.connect("key-press-event", key)', script)
        self.assertIn('canvas.connect("button-press-event", click)', script)


if __name__ == "__main__":
    unittest.main()
