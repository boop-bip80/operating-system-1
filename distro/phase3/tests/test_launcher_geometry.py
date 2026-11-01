# SPDX-License-Identifier: GPL-2.0-only
"""Host GI-free tests of production GTK allocation-to-QMP launcher targets.

Production Phase3Shell methods are AST-extracted and run with fake mapped GTK
widgets/screens/GLib timer; these are NOT live Wayland geometry measurements.
"""

from __future__ import annotations

import ast
from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import types
import unittest


SHELL = Path(__file__).resolve().parents[1] / "shell" / "shell.py"
METHODS = {
    "_launcher_widget_target", "_report_launcher_targets",
    "toggle_launcher", "hide_launcher",
}
CONSTANTS = {
    "LAUNCHER_LEFT", "LAUNCHER_TOP", "LAUNCHER_TARGET_MAX_CHECKS",
    "LAUNCHER_TARGET_INTERVAL_MS",
}


class FakeWidget:
    def __init__(self, x, y, width, height, *, mapped=True, point=None):
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.mapped = mapped
        self.point = point

    def get_mapped(self):
        return self.mapped

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height

    def translate_coordinates(self, _launcher, _x, _y):
        return (self.x, self.y) if self.point is None else self.point


class FakeLauncher:
    def __init__(self, *, width=380, height=435, visible=True, mapped=True):
        self.width, self.height = width, height
        self.visible, self.mapped = visible, mapped
        self.presented = False

    def get_visible(self):
        return self.visible

    def get_mapped(self):
        return self.mapped

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height

    def show_all(self):
        self.visible = True
        self.mapped = True

    def present(self):
        self.presented = True

    def hide(self):
        self.visible = False
        self.mapped = False


class FakeSearch:
    def __init__(self):
        self.queried = None
        self.focused = False

    def set_text(self, value):
        self.queried = value

    def grab_focus(self):
        self.focused = True


def actual_launcher_geometry():
    tree = ast.parse(SHELL.read_text(encoding="utf-8"), filename=str(SHELL))
    production = next(node for node in tree.body if
                      isinstance(node, ast.ClassDef) and node.name == "Phase3Shell")
    members = [node for node in production.body if
               isinstance(node, ast.FunctionDef) and node.name in METHODS]
    if {node.name for node in members} != METHODS:
        raise AssertionError("missing production GTK launcher geometry methods")
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and len(node.targets) == 1 and
                 isinstance(node.targets[0], ast.Name) and
                 node.targets[0].id in CONSTANTS]
    if {node.targets[0].id for node in constants} != CONSTANTS:
        raise AssertionError("missing launcher position/retry limits")
    fake_class = ast.ClassDef(
        name="ExtractedShell", bases=[], keywords=[], body=members, decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=constants + [fake_class],
                                                  type_ignores=[]))
    events, timers = [], []
    def timer(milliseconds, callback, generation):
        timers.append((milliseconds, callback, generation))
        return 1
    namespace = {
        "Gtk": types.SimpleNamespace(Widget=object),
        "Gdk": types.SimpleNamespace(
            Screen=types.SimpleNamespace(
                get_default=lambda: types.SimpleNamespace(get_width=lambda: 1024,
                                                           get_height=lambda: 768))),
        "GLib": types.SimpleNamespace(timeout_add=timer),
        "sys": sys, "emit": lambda marker: events.append(marker),
    }
    exec(compile(module, str(SHELL), "exec"), namespace)
    return namespace["ExtractedShell"], events, timers, namespace


def fixture():
    cls, events, timers, namespace = actual_launcher_geometry()
    shell = cls()
    shell.launcher = FakeLauncher()
    shell.search = FakeSearch()
    shell.compact_toggle = FakeWidget(46, 401, 92, 26)
    shell.terminal_row = FakeWidget(126, 110, 248, 40)
    shell._launcher_target_generation = 0
    shell._launcher_target_checks = 0
    shell._fill_launcher = lambda: None
    return shell, events, timers, namespace


class LauncherGeometryContract(unittest.TestCase):
    def test_global_centers_and_real_allocation_dimensions(self):
        shell, _, _, _ = fixture()
        self.assertEqual(shell._launcher_widget_target(shell.compact_toggle),
                         (60, 444, 92, 26))
        self.assertEqual(shell._launcher_widget_target(shell.terminal_row),
                         (140, 153, 248, 40))

    def test_gtk_map_and_allocation_guards_fail_closed(self):
        shell, _, _, _ = fixture()
        candidates = [
            None,
            FakeWidget(46, 401, 10, 26),
            FakeWidget(46, 401, 92, 6),
            FakeWidget(46, 401, 92, 26, mapped=False),
            FakeWidget(46, 401, 92, 26, point=(500, 600)),
            FakeWidget(46, 401, 92, 26, point=(-1, 10)),
            FakeWidget(46, 401, 92, 26, point=(46,)),
        ]
        for widget in candidates:
            with self.subTest(widget=str(widget)):
                self.assertIsNone(shell._launcher_widget_target(widget))
        shell.launcher.mapped = False
        self.assertIsNone(shell._launcher_widget_target(shell.compact_toggle))

    def test_both_mapped_targets_report_in_causal_order(self):
        shell, events, _, _ = fixture()
        self.assertFalse(shell._report_launcher_targets(0))
        self.assertEqual(events, [
            "COMPACT_TARGET:60:444:92:26", "TERMINAL_TARGET:140:153:248:40",
        ])

    def test_missing_terminal_row_never_claims_gui_success(self):
        shell, events, _, namespace = fixture()
        shell.terminal_row = None
        self.assertTrue(shell._report_launcher_targets(0))
        self.assertEqual(events, [])
        shell._launcher_target_checks = namespace["LAUNCHER_TARGET_MAX_CHECKS"] - 1
        with redirect_stderr(io.StringIO()):
            self.assertFalse(shell._report_launcher_targets(0))
        self.assertEqual(events, [])

    def test_bad_compact_allocation_and_stale_generation_rejected(self):
        shell, events, _, _ = fixture()
        shell.compact_toggle.mapped = False
        self.assertTrue(shell._report_launcher_targets(0))
        shell.compact_toggle.mapped = True
        shell._launcher_target_generation = 1
        self.assertFalse(shell._report_launcher_targets(0))
        self.assertEqual(events, [])

    def test_launcher_open_emits_then_schedules_mapped_retry(self):
        shell, events, timers, namespace = fixture()
        shell.launcher.visible = False
        shell.launcher.mapped = False
        shell.toggle_launcher()
        self.assertEqual(events, ["LAUNCHER_OPEN"])
        self.assertTrue(shell.launcher.presented)
        self.assertTrue(shell.search.focused)
        self.assertEqual(shell.search.queried, "")
        self.assertEqual(len(timers), 1)
        duration, callback, generation = timers[0]
        self.assertEqual(duration, namespace["LAUNCHER_TARGET_INTERVAL_MS"])
        self.assertEqual(generation, shell._launcher_target_generation)
        self.assertFalse(callback(generation))
        self.assertEqual(events[1:], [
            "COMPACT_TARGET:60:444:92:26", "TERMINAL_TARGET:140:153:248:40",
        ])

    def test_hide_invalidates_queued_gtk_geometry(self):
        shell, events, timers, _ = fixture()
        shell.launcher.visible = False
        shell.toggle_launcher()
        _, callback, generation = timers[0]
        shell.hide_launcher()
        self.assertFalse(shell.launcher.get_visible())
        self.assertNotEqual(shell._launcher_target_generation, generation)
        self.assertFalse(callback(generation))
        self.assertEqual(events, ["LAUNCHER_OPEN"])

    def test_toggle_open_launcher_hides_instead_of_reemitting_targets(self):
        shell, events, timers, _ = fixture()
        shell.toggle_launcher()
        self.assertFalse(shell.launcher.get_visible())
        self.assertEqual(events, [])
        self.assertEqual(timers, [])


if __name__ == "__main__":
    unittest.main()
