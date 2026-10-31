# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


PHASE = Path(__file__).resolve().parents[1]
GUEST = PHASE / "rootfs/usr/lib/phase2"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.modules.setdefault("probe_state", load_module("probe_state", GUEST / "probe_state.py"))
probe = load_module("input_probe", GUEST / "input-probe.py")
terminal = load_module("terminal_state", GUEST / "terminal_state.py")


class WaylandDisplayTests(unittest.TestCase):
    def test_native_wayland_runtime_gtype_is_accepted(self):
        display = SimpleNamespace(__gtype__=SimpleNamespace(name="GdkWaylandDisplay"))
        probe.require_wayland_display(display)

    def test_missing_display_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "not a native Wayland display"):
            probe.require_wayland_display(None)

    def test_x11_and_other_runtime_backends_are_rejected(self):
        for name in ("GdkX11Display", "GdkBroadwayDisplay", "GdkDisplay", ""):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                probe.require_wayland_display(SimpleNamespace(__gtype__=SimpleNamespace(name=name)))

    def test_generic_object_cannot_substitute_for_runtime_gtype(self):
        with self.assertRaises(RuntimeError):
            probe.require_wayland_display(object())

    def test_startup_does_not_require_or_import_optional_gdkwayland_namespace(self):
        gi = ModuleType("gi")
        gi.require_foreign = Mock()
        gi.require_version = Mock()
        repository = ModuleType("gi.repository")
        display = SimpleNamespace(__gtype__=SimpleNamespace(name="GdkWaylandDisplay"))
        repository.Gdk = SimpleNamespace(Display=SimpleNamespace(get_default=lambda: display))
        repository.GLib = SimpleNamespace()
        repository.Gtk = SimpleNamespace()
        # No GdkWayland attribute/typelib exists, just like the pinned package.
        gi.repository = repository
        with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}), \
                patch.object(probe.os, "getuid", return_value=1000), \
                patch.dict(probe.os.environ, {"GDK_BACKEND": "wayland"}), \
                patch.object(probe, "require_wayland_display", side_effect=RuntimeError("display check reached")) as check:
            with self.assertRaisesRegex(RuntimeError, "display check reached"):
                probe.main()
        check.assert_called_once_with(display)
        self.assertEqual([call.args for call in gi.require_version.call_args_list],
                         [("Gtk", "3.0"), ("Gdk", "3.0")])
        gi.require_foreign.assert_called_once_with("cairo")

    def test_wayland_backend_environment_is_still_mandatory(self):
        with patch.object(probe.os, "getuid", return_value=1000), \
                patch.dict(probe.os.environ, {"GDK_BACKEND": "x11"}):
            with self.assertRaisesRegex(RuntimeError, "non-root Wayland session"):
                probe.main()


class CallbackGuardTests(unittest.TestCase):
    def setUp(self):
        self.stops = []
        self.guard = probe.CallbackGuard(lambda: self.stops.append("stop"))

    def test_success_retains_callback_result_and_arguments(self):
        @self.guard.guard
        def callback(first, *, second):
            return first + second

        self.assertEqual(callback(2, second=3), 5)
        self.assertEqual(callback.__name__, "callback")
        self.guard.raise_if_failed()
        self.assertEqual(self.stops, [])

    def test_exception_stops_loop_and_is_raised_after_dispatch(self):
        error = OSError("cannot persist input")

        @self.guard.guard
        def callback():
            raise error

        self.assertFalse(callback())
        self.assertEqual(self.stops, ["stop"])
        with self.assertRaisesRegex(RuntimeError, "cannot persist input") as raised:
            self.guard.raise_if_failed()
        self.assertIs(raised.exception.__cause__, error)

    def test_no_success_callbacks_run_after_failure(self):
        @self.guard.guard
        def failing():
            raise ValueError("bad frame")

        success = []

        @self.guard.guard
        def ready():
            success.append("ready")

        failing()
        self.assertFalse(ready())
        self.assertFalse(failing())
        self.assertEqual(success, [])
        self.assertEqual(self.stops, ["stop"])

    def test_system_exit_cannot_be_swallowed_by_gi(self):
        @self.guard.guard
        def callback():
            raise SystemExit(7)

        self.assertFalse(callback())
        with self.assertRaises(RuntimeError):
            self.guard.raise_if_failed()


class TerminalReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.receipt = self.directory / "ready"
        self.proc = self.directory / "proc"
        self.process = self.proc / "456"
        (self.process / "fd").mkdir(parents=True)
        self.receipt.write_text("c1\t456\t123\t/dev/pts/2\n", encoding="ascii")
        self.receipt.chmod(0o600)
        self.uid = os.getuid()
        self.status()
        (self.process / "cmdline").write_bytes(b"/bin/bash\0-i\0")
        (self.process / "exe").symlink_to(os.path.realpath("/bin/bash"))
        for number in (0, 1):
            (self.process / "fd" / str(number)).symlink_to("/dev/pts/2")

    def status(self, *, uid=None, parent=123):
        if uid is None:
            uid = self.uid
        (self.process / "status").write_text(
            f"Name:\tbash\nPPid:\t{parent}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        )

    def ready(self, *, session="c1", parent=123):
        return terminal.terminal_ready(
            self.receipt, session, parent, proc_root=self.proc, uid=self.uid
        )

    def test_interactive_bash_with_pty_and_matching_parent_is_ready(self):
        self.assertEqual(self.ready(), 456)

    def test_receipt_before_shell_exec_is_not_ready(self):
        (self.process / "cmdline").write_bytes(
            b"/bin/bash\0/usr/lib/phase2/terminal-shell.sh\0"
        )
        self.assertIsNone(self.ready())

    def test_missing_receipt_is_pending(self):
        self.receipt.unlink()
        self.assertIsNone(self.ready())

    def test_exited_shell_is_not_ready(self):
        (self.process / "status").unlink()
        self.assertIsNone(self.ready())

    def test_wrong_session_or_receipt_parent_is_rejected(self):
        for values in ({"session": "c2"}, {"parent": 999}):
            with self.assertRaisesRegex(ValueError, "session/process"):
                self.ready(**values)

    def test_actual_process_uid_and_parent_are_checked(self):
        self.status(uid=self.uid + 1)
        with self.assertRaisesRegex(ValueError, "identity or parent"):
            self.ready()
        self.status(parent=999)
        with self.assertRaisesRegex(ValueError, "identity or parent"):
            self.ready()

    def test_redirected_output_is_rejected(self):
        output = self.process / "fd/1"
        output.unlink()
        output.symlink_to("/dev/null")
        with self.assertRaisesRegex(ValueError, "reported PTY"):
            self.ready()

    def test_forged_command_line_without_bash_executable_is_rejected(self):
        executable = self.process / "exe"
        executable.unlink()
        executable.symlink_to("/bin/false")
        with self.assertRaisesRegex(ValueError, "not Bash"):
            self.ready()

    def test_malformed_receipt_is_rejected(self):
        for value in ("", "c1\t456\t123\t/dev/null\n", "c1\t456\t123\t/dev/pts/2",
                      "c1\t456\t123\t/dev/pts/2\nextra\n", "x" * 300):
            self.receipt.write_text(value)
            with self.assertRaises(ValueError):
                self.ready()

    def test_non_private_receipt_is_rejected(self):
        self.receipt.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.ready()

    def test_symlink_receipt_is_rejected(self):
        original = self.directory / "original"
        self.receipt.rename(original)
        self.receipt.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.ready()

    def test_hardlinked_receipt_is_rejected(self):
        os.link(self.receipt, self.directory / "alias")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.ready()


class SessionContractTests(unittest.TestCase):
    def test_terminal_helper_checks_pty_before_publishing_then_execs_bash(self):
        helper = (GUEST / "terminal-shell.sh").read_text()
        self.assertIn("[[ -t 0 && -t 1 ]]", helper)
        self.assertIn(": < /dev/tty", helper)
        self.assertIn("/proc/$PPID/exe", helper)
        self.assertLess(helper.index("[[ -t 0 && -t 1 ]]"), helper.index("mv -T"))
        self.assertLess(helper.index("mv -T"), helper.index("exec /bin/bash -i"))

    def test_wrapper_requires_fresh_receipt_before_marker(self):
        script = (GUEST / "start-desktop.sh").read_text()
        self.assertIn('mktemp -d -- "$XDG_RUNTIME_DIR/phase2-terminal.XXXXXXXX"', script)
        self.assertIn("weston-terminal --shell=/usr/lib/phase2/terminal-shell.sh", script)
        self.assertLess(script.index("terminal_state.py"), script.index("P2:TERMINAL_READY"))
        self.assertIn('kill -0 "$terminal_shell_pid"', script)


if __name__ == "__main__":
    unittest.main()
