# SPDX-License-Identifier: GPL-2.0-only
"""Host GI-free tests of production GTK allocation-to-QMP launcher targets.

Production Phase3Shell methods are AST-extracted and run with fake mapped GTK
widgets/screens/GLib timer; these are NOT live Wayland geometry measurements.
"""

from __future__ import annotations

import ast
from contextlib import redirect_stderr
import io
import hashlib
import json
import re
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
    extras = [node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name in ("safe_desktop_id", "app_index_payload")]
    if len(extras) != 2:
        raise AssertionError("production installed Gio index functions missing")
    module = ast.fix_missing_locations(ast.Module(body=constants + extras + [fake_class],
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
        "Any": object, "json": json, "hashlib": hashlib, "re": re,
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
    shell._eligible_ids = ["foot.desktop", "org.example.App.desktop"]
    shell._displayed_ids = ["org.example.App.desktop"]
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
        shell, events, _, ns = fixture()
        self.assertFalse(shell._report_launcher_targets(0))
        payload = ns["app_index_payload"](shell._eligible_ids, shell._displayed_ids)
        self.assertEqual(events, [
            "COMPACT_TARGET:60:444:92:26", "TERMINAL_TARGET:140:153:248:40",
            "APP_INDEX:" + json.dumps(payload, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True),
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

    def test_fabricated_or_missing_desktop_id_inventory_never_emits_targets(self):
        for eligible, displayed in (
            ([], []),
            (["org.example.App.desktop"], []),
            (["foot.desktop", "foot.desktop"], []),
            (["foot.desktop"], ["uninstalled.desktop"]),
            (["../escape.desktop"], ["../escape.desktop"]),
        ):
            with self.subTest(eligible=eligible, displayed=displayed):
                shell, events, _, _ = fixture()
                shell._eligible_ids = eligible
                shell._displayed_ids = displayed
                with redirect_stderr(io.StringIO()):
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
        payload = namespace["app_index_payload"](shell._eligible_ids, shell._displayed_ids)
        self.assertEqual(events[1:], [
            "COMPACT_TARGET:60:444:92:26", "TERMINAL_TARGET:140:153:248:40",
            "APP_INDEX:" + json.dumps(payload, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True),
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



def production_layer_setup(source: str, method_name: str, window_name: str) -> dict:
    """Execute the actual LayerShell setup AST with fake GTK interfaces.

    This exercises values sent to GtkLayerShell, including Wayland's
    exclusive_zone = -1 overlay semantics. It is a host contract test, not
    evidence of actual wlroots scanout or compositor positioning.
    """
    tree = ast.parse(source, filename=str(SHELL))
    production = next(node for node in tree.body if
                      isinstance(node, ast.ClassDef) and node.name == "Phase3Shell")
    method = next(node for node in production.body if
                  isinstance(node, ast.FunctionDef) and node.name == method_name)
    selected = []
    created = 0
    for node in method.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            call = node.value
            if (isinstance(target, ast.Attribute) and
                    isinstance(target.value, ast.Name) and
                    target.value.id == "self" and target.attr == window_name and
                    isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and
                    call.func.id == "layer_window"):
                selected.append(node)
                created += 1
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            func = call.func
            if (isinstance(func, ast.Attribute) and
                    isinstance(func.value, ast.Name) and
                    func.value.id == "GtkLayerShell" and
                    func.attr in {"set_margin", "set_exclusive_zone"} and
                    call.args and isinstance(call.args[0], ast.Attribute) and
                    isinstance(call.args[0].value, ast.Name) and
                    call.args[0].value.id == "self" and
                    call.args[0].attr == window_name):
                selected.append(node)
    if created != 1:
        raise AssertionError(f"{method_name} does not construct exactly one {window_name}")
    constants = [
        node for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and
        isinstance(node.targets[0], ast.Name) and
        node.targets[0].id in {"LAUNCHER_TOP", "LAUNCHER_LEFT"}
    ]
    if {node.targets[0].id for node in constants} != {"LAUNCHER_TOP", "LAUNCHER_LEFT"}:
        raise AssertionError("launcher source margins are missing")
    call_log = {"windows": [], "margins": [], "zones": []}
    fake_self = types.SimpleNamespace()

    def fake_layer_window(*args):
        window = types.SimpleNamespace(kind=window_name)
        call_log["windows"].append((window, args))
        return window

    def fake_margin(window, edge, size):
        call_log["margins"].append((window, edge, size))

    def fake_zone(window, size):
        call_log["zones"].append((window, size))

    edges = types.SimpleNamespace(TOP="top", BOTTOM="bottom",
                                  LEFT="left", RIGHT="right")
    layers = types.SimpleNamespace(TOP="top", OVERLAY="overlay")
    gtk_layer = types.SimpleNamespace(Edge=edges, Layer=layers,
                                      set_margin=fake_margin,
                                      set_exclusive_zone=fake_zone)
    namespace = {"self": fake_self, "GtkLayerShell": gtk_layer,
                 "layer_window": fake_layer_window}
    module = ast.fix_missing_locations(ast.Module(
        body=constants + selected, type_ignores=[]))
    exec(compile(module, filename=str(SHELL), mode="exec"), namespace)
    window = getattr(fake_self, window_name)
    if len(call_log["windows"]) != 1 or call_log["windows"][0][0] is not window:
        raise AssertionError("construction must bind actual LayerShell window")
    for kind in ("margins", "zones"):
        if any(first is not window for first, *_ in call_log[kind]):
            raise AssertionError(f"LayerShell {kind} target wrong window")
    return {
        "create": call_log["windows"][0][1],
        "margins": [(edge, size) for _, edge, size in call_log["margins"]],
        "zones": [zone for _, zone in call_log["zones"]],
    }


class LauncherLayerGeometryContract(unittest.TestCase):
    def test_launcher_overlay_does_not_inherit_topbar_workarea_offset(self):
        actual = production_layer_setup(
            SHELL.read_text(encoding="utf-8"), "_make_launcher", "launcher")
        self.assertEqual(actual["create"], (
            "Phase3 Application Launcher", "overlay", ("top", "left"), 380, 432))
        self.assertEqual(actual["margins"], [("top", 43), ("left", 14)])
        # Layer-shell 0 allows the compositor to relocate a non-exclusive
        # overlay below the topbar's exclusive 38px. -1 preserves global y=43.
        self.assertEqual(actual["zones"], [-1])
        self.assertLess(actual["margins"][0][1], 38 + 38)

    def test_topbar_and_dock_real_exclusive_zones_remain_intact(self):
        source = SHELL.read_text(encoding="utf-8")
        bar = production_layer_setup(source, "_make_bar", "bar")
        dock = production_layer_setup(source, "_make_dock", "dock")
        self.assertEqual(bar["create"], (
            "Phase3 Top Bar", "top", ("top", "left", "right"), 1024, 38))
        self.assertEqual(bar["margins"], [])
        self.assertEqual(bar["zones"], [38])
        self.assertEqual(dock["create"], (
            "Phase3 Dock", "top", ("bottom",), 88, 60))
        self.assertEqual(dock["margins"], [("bottom", 8)])
        self.assertEqual(dock["zones"], [68])

    def test_old_popup_zone_zero_regression_is_detected(self):
        source = SHELL.read_text(encoding="utf-8")
        actual = production_layer_setup(source, "_make_launcher", "launcher")
        self.assertEqual(actual["zones"], [-1])
        old_source = source.replace(
            "GtkLayerShell.set_exclusive_zone(self.launcher, -1)",
            "GtkLayerShell.set_exclusive_zone(self.launcher, 0)", 1)
        self.assertNotEqual(source, old_source, "launcher zone source guard absent")
        buggy = production_layer_setup(old_source, "_make_launcher", "launcher")
        self.assertEqual(buggy["zones"], [0])
        self.assertNotEqual(buggy["zones"], actual["zones"])


if __name__ == "__main__":
    unittest.main()
