# SPDX-License-Identifier: GPL-2.0-only
"""GI-free regression for bounded, truthful GTK/Sway startup diagnostics.

The actual Phase3Shell._check_ready body and initialization assignments are
compiled from the production source. Sway, GTK and time replies here are
synthetic fault injections, never evidence of a successfully booted VM.
"""
from __future__ import annotations

import ast
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import types
import unittest


SHELL = Path(__file__).resolve().parents[1] / "shell" / "shell.py"
READY_DEFAULTS = {"_last_startup_warning", "_startup_last_failure"}


def real_readiness_fixture(*, mode: str = "layout_error", bar_mapped: bool = True,
                           dock_mapped: bool = True):
    tree = ast.parse(SHELL.read_text(encoding="utf-8"), filename=str(SHELL))
    cls = next(node for node in tree.body if
               isinstance(node, ast.ClassDef) and node.name == "Phase3Shell")
    method = next(node for node in cls.body if
                  isinstance(node, ast.FunctionDef) and node.name == "_check_ready")
    constructor = next(node for node in cls.body if
                       isinstance(node, ast.FunctionDef) and node.name == "__init__")
    defaults = [
        node for node in constructor.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and ((node.targets[0] if isinstance(node, ast.Assign) else node.target))
        and isinstance((node.targets[0] if isinstance(node, ast.Assign)
                        else node.target), ast.Attribute)
        and (node.targets[0] if isinstance(node, ast.Assign)
             else node.target).attr in READY_DEFAULTS
    ]
    names = {(node.targets[0] if isinstance(node, ast.Assign)
              else node.target).attr for node in defaults}
    if names != READY_DEFAULTS:
        raise AssertionError("production shell no longer initializes startup diagnostic state")

    # Execute real production initialization assignments without importing GI.
    init_module = ast.fix_missing_locations(ast.Module(
        body=defaults, type_ignores=[]))

    clock = types.SimpleNamespace(now=100.0)
    state = types.SimpleNamespace(mode=mode)
    events = []
    snapshots = []
    quits = []
    surface_bar = types.SimpleNamespace(mapped=bar_mapped, zone=38, layer=True)
    surface_dock = types.SimpleNamespace(mapped=dock_mapped, zone=68, layer=True)
    for item in (surface_bar, surface_dock):
        item.get_mapped = lambda item=item: item.mapped

    class FakeLayerShell:
        @staticmethod
        def is_layer_window(window):
            return window.layer

        @staticmethod
        def get_exclusive_zone(window):
            return window.zone

    def ipc(*args):
        if state.mode == "ipc_error":
            raise RuntimeError("real swaymsg get_outputs returned malformed data")
        if args == ("-t", "get_tree"):
            return {"type": "root", "nodes": []}
        if args == ("-t", "get_outputs"):
            return [{"name": "Virtual-1"}]
        if args == ("-t", "get_workspaces"):
            return [{"name": "1"}]
        raise AssertionError("unrecognized Sway request")

    def prove_layout(_outputs, _workspaces, top, dock):
        if state.mode in ("layout_error", "long_error"):
            raise ValueError(
                "Sway workspace does not exclude topbar and Dock"
                if state.mode == "layout_error" else "X" * 1000
            )
        if state.mode == "valid":
            if (top, dock) != (38, 68):
                raise AssertionError("unexpected GtkLayerShell exclusive zones")
            return {
                "source": "sway.get_outputs+get_workspaces",
                "output": "Virtual-1", "workspace": "1",
                "output_rect": {"x": 0, "y": 0, "width": 1024, "height": 768},
                "workspace_rect": {"x": 0, "y": 38, "width": 1024, "height": 662},
                "scale": 1.0, "mode": {"width": 1024, "height": 768},
                "top_exclusive": 38, "dock_exclusive": 68,
            }
        raise AssertionError("unknown test mode")

    stage_cls = ast.ClassDef(
        name="ActualReadiness", bases=[], keywords=[],
        body=[method], decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[stage_cls], type_ignores=[]))
    namespace = {
        "GtkLayerShell": FakeLayerShell,
        "Gtk": types.SimpleNamespace(main_quit=lambda: quits.append(True)),
        "parse_toplevels": lambda tree: {},
        "sway_ipc": ipc, "layout_proof": prove_layout,
        "emit": lambda marker: events.append(marker),
        "json": json, "subprocess": subprocess, "sys": sys,
        "time": types.SimpleNamespace(monotonic=lambda: clock.now),
    }
    exec(compile(module, str(SHELL), "exec"), namespace)
    shell = namespace["ActualReadiness"]()
    exec(compile(init_module, str(SHELL), "exec"), {"self": shell})
    if shell._last_startup_warning is not None:
        raise AssertionError("first startup exception must be immediately loggable")
    if shell._startup_last_failure != "waiting_for_mapped_layer_surfaces":
        raise AssertionError("initial unmapped-surface diagnostic is missing")
    shell._ready = False
    shell._startup_checks = 0
    shell.bar = surface_bar
    shell.dock = surface_dock
    shell.toplevels = {}
    shell.preferences = {"compact_dock": False}
    shell._record_ipc_snapshot = lambda observed: snapshots.append(observed)
    shell._refresh_dock = lambda: snapshots.append("dock_refreshed")
    return shell, state, clock, events, snapshots, quits


class ReadinessDiagnosticContract(unittest.TestCase):
    def test_first_exception_is_visible_and_throttled_for_ten_seconds(self):
        shell, state, clock, events, _, quits = real_readiness_fixture()
        captured = io.StringIO()
        with redirect_stderr(captured):
            for when in (100.0, 103.0, 109.9):
                clock.now = when
                self.assertTrue(shell._check_ready())
            clock.now = 110.0
            self.assertTrue(shell._check_ready())
            clock.now = 119.9
            self.assertTrue(shell._check_ready())
        messages = captured.getvalue().splitlines()
        self.assertEqual(len(messages), 2)
        for line in messages:
            self.assertTrue(line.startswith("phase3: SHELL_READY_WAIT:"))
            self.assertIn("ValueError: Sway workspace does not exclude", line)
        self.assertEqual(events, [])
        self.assertEqual(quits, [])
        self.assertEqual(shell._startup_checks, 5)

    def test_timeout_after_failed_real_layout_is_single_fail_marker(self):
        shell, _, clock, events, _, quits = real_readiness_fixture()
        captured = io.StringIO()
        with redirect_stderr(captured):
            for check in range(120):
                clock.now = 100 + check * .25
                expected = check < 119
                self.assertIs(shell._check_ready(), expected)
        lines = captured.getvalue().splitlines()
        self.assertEqual(len([x for x in lines if "SHELL_READY_WAIT:" in x]), 3)
        timed_out = [x for x in lines if "SHELL_READY_TIMEOUT:" in x]
        self.assertEqual(len(timed_out), 1)
        self.assertIn("checks=120:bar_mapped=True:dock_mapped=True", timed_out[0])
        self.assertIn("Sway workspace does not exclude", timed_out[0])
        self.assertEqual(events, ["FAIL:SHELL_STARTUP"])
        self.assertEqual(quits, [True])
        self.assertFalse(shell._ready)

    def test_unmapped_panel_reaches_timeout_without_fabricated_layout(self):
        shell, _, clock, events, _, quits = real_readiness_fixture(
            bar_mapped=False, dock_mapped=True)
        captured = io.StringIO()
        with redirect_stderr(captured):
            for check in range(120):
                clock.now = 100 + check * .25
                shell._check_ready()
        output = captured.getvalue()
        self.assertNotIn("SHELL_READY_WAIT:", output)
        self.assertIn("bar_mapped=False:dock_mapped=True", output)
        self.assertIn("waiting_for_mapped_layer_surfaces", output)
        self.assertEqual(events, ["FAIL:SHELL_STARTUP"])
        self.assertEqual(quits, [True])

    def test_long_exception_diagnostic_is_bounded_without_success_markers(self):
        shell, _, clock, events, _, quits = real_readiness_fixture(mode="long_error")
        captured = io.StringIO()
        with redirect_stderr(captured):
            self.assertTrue(shell._check_ready())
        lines = captured.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        prefix = "phase3: SHELL_READY_WAIT:"
        self.assertTrue(lines[0].startswith(prefix))
        self.assertLessEqual(len(lines[0]) - len(prefix), 180)
        self.assertNotIn("SHELL_READY", " ".join(events))
        self.assertEqual(quits, [])

    def test_real_success_body_preserves_marker_order_without_diagnostic(self):
        shell, state, clock, events, snapshots, quits = real_readiness_fixture(
            mode="valid")
        captured = io.StringIO()
        with redirect_stderr(captured):
            self.assertFalse(shell._check_ready())
        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], {})
        self.assertEqual(snapshots[1], "dock_refreshed")
        self.assertEqual([line.split(":", 1)[0] for line in events], [
            "IPC_LAYOUT", "LAYER_SHELL_READY", "SHELL_READY", "PREFERENCE_STATE",
        ])
        layout = json.loads(events[0].split(":", 1)[1])
        self.assertEqual(layout["source"], "sway.get_outputs+get_workspaces")
        self.assertEqual(events[-1], "PREFERENCE_STATE:compact_dock=false")
        self.assertTrue(shell._ready)
        self.assertEqual(quits, [])

    def test_sway_ipc_errors_are_logged_not_claimed_as_readiness(self):
        shell, state, clock, events, _, quits = real_readiness_fixture(
            mode="ipc_error")
        captured = io.StringIO()
        with redirect_stderr(captured):
            self.assertTrue(shell._check_ready())
        self.assertIn("RuntimeError: real swaymsg get_outputs", captured.getvalue())
        self.assertEqual(events, [])
        self.assertEqual(quits, [])


if __name__ == "__main__":
    unittest.main()
