# SPDX-License-Identifier: GPL-2.0-only
"""GI-free source-bound regressions for Sway Dock Close focus handoff.

These compile the production Phase3Shell close/proc/poll methods and pure Sway
helpers. Synthetic trees and PID replies are fault injections, never substitutes
for the real guest /proc or QMP get_tree acceptance evidence.
"""
from __future__ import annotations

import ast
from contextlib import redirect_stderr
from dataclasses import dataclass
import io
from pathlib import Path
import subprocess
import sys
import time
import types
import unittest


SHELL = Path(__file__).resolve().parents[1] / "shell" / "shell.py"
PURE = {
    "Toplevel", "ProcessWatch", "valid_con_id", "close_handoff_candidate",
    "active_toplevel_id", "is_terminal", "ipc_signature", "window_events",
}
METHODS = {"close_window", "_check_process_exits", "_poll_windows"}


def actual_handoff_fixture(*, kill_ack=True, initial_b=None, proc=("S", 777)):
    root = ast.parse(SHELL.read_text(encoding="utf-8"), filename=str(SHELL))
    cls = next(node for node in root.body
               if isinstance(node, ast.ClassDef) and node.name == "Phase3Shell")
    selected = [node for node in root.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in PURE]
    if {node.name for node in selected} != PURE:
        raise AssertionError("production Sway handoff helpers are missing")
    methods = [node for node in cls.body
               if isinstance(node, ast.FunctionDef) and node.name in METHODS]
    if {node.name for node in methods} != METHODS:
        raise AssertionError("real Dock Close/proc/poll source methods missing")
    constructor = next(node for node in cls.body
                       if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    state_assignments = [
        node for node in constructor.body if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance((node.targets[0] if isinstance(node, ast.Assign)
                        else node.target), ast.Attribute)
        and (node.targets[0] if isinstance(node, ast.Assign)
             else node.target).attr == "_pending_close_handoffs"
    ]
    if len(state_assignments) != 1:
        raise AssertionError("constructor must initialize close handoff ledger")
    method_class = ast.ClassDef(
        name="ActualDockHandoff", bases=[], keywords=[],
        body=methods, decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(
        body=selected + [method_class], type_ignores=[]))
    clock = types.SimpleNamespace(now=100.0)
    state = types.SimpleNamespace(exit="running", proc=proc, tree={})
    events = []
    commands = []
    snapshots = []
    timers = []
    accepted = types.SimpleNamespace(kill=kill_ack, focus=True)
    namespace = {
        "__name__": __name__, "dataclass": dataclass,
        "time": types.SimpleNamespace(monotonic=lambda: clock.now),
        "subprocess": subprocess, "sys": sys,
        "GLib": types.SimpleNamespace(
            timeout_add=lambda ms, callback: timers.append((ms, callback))),
        "read_proc_state": lambda _pid: state.proc,
        "process_exit_status": lambda _pid, _start: state.exit,
        "emit": lambda marker: events.append(marker),
        "sway_ipc": lambda *args: state.tree,
        "parse_toplevels": lambda raw: raw,
        "LAUNCH_TIMEOUT": 5, "MAX_PROCESS_WATCHES": 16,
        "PROCESS_EXIT_TIMEOUT": 8,
    }
    exec(compile(module, str(SHELL), "exec"), namespace)
    shell = namespace["ActualDockHandoff"]()
    init_module = ast.fix_missing_locations(ast.Module(
        body=state_assignments, type_ignores=[]))
    exec(compile(init_module, str(SHELL), "exec"), {"self": shell})
    if shell._pending_close_handoffs != {}:
        raise AssertionError("new shell does not start without pending handoffs")
    Top = namespace["Toplevel"]
    a = Top(con_id=5, app_id="foot", title="A", focused=True,
            minimized=False, pid=388)
    b = initial_b if initial_b is not None else Top(
        con_id=6, app_id="foot", title="B", focused=False,
        minimized=False, pid=406,
    )
    shell._ready = True
    shell._startup_checks = 0
    shell._last_ipc_warning = 0.0
    shell.toplevels = {5: a, 6: b} if b is not False else {5: a}
    state.tree = dict(shell.toplevels)
    shell._pending_focus = set()
    shell._pending_process_exits = {}
    shell._launch_requests = []
    shell._record_ipc_snapshot = lambda observed: snapshots.append(dict(observed))
    shell._refresh_dock = lambda: None
    shell._poll_once = lambda: False
    shell.active_title = types.SimpleNamespace(set_text=lambda value: None)

    def sway_command(con_id, action):
        commands.append((con_id, action))
        return accepted.kill if action == "kill" else accepted.focus
    shell._sway_command = sway_command
    return shell, namespace, state, clock, events, commands, snapshots, timers, accepted


class RealSwayCloseHandoffContract(unittest.TestCase):
    def test_pure_candidate_matches_only_exact_live_unfocused_successor(self):
        shell, ns, state, _, _, _, _, _, _ = actual_handoff_fixture()
        B = shell.toplevels[6]
        self.assertEqual(ns["close_handoff_candidate"](5, 6, 406, {6: B}), 6)
        self.assertIsNone(ns["close_handoff_candidate"](5, 6, 406, {5: shell.toplevels[5], 6: B}))
        self.assertIsNone(ns["close_handoff_candidate"](5, 6, 406, {}))
        self.assertIsNone(ns["close_handoff_candidate"](5, 6, 999, {6: B}))
        for bad in (0, -1, True, "6", None):
            with self.subTest(bad_con_id=bad):
                self.assertIsNone(ns["close_handoff_candidate"](5, bad, 406, {6: B}))
        for bad in (0, -1, True, "406", None):
            with self.subTest(bad_pid=bad):
                self.assertIsNone(ns["close_handoff_candidate"](5, 6, bad, {6: B}))

    def test_pure_candidate_rejects_hidden_reused_and_other_focused_clients(self):
        shell, ns, state, _, _, _, _, _, _ = actual_handoff_fixture()
        T = ns["Toplevel"]
        for changed in (
            T(6, "foot", "B", False, True, pid=406),
            T(6, "foot", "B", False, False, visible=False, pid=406),
            T(6, "foot", "B", False, False, pid=999),
            T(7, "foot", "B", False, False, pid=406),
        ):
            with self.subTest(changed=changed):
                self.assertIsNone(ns["close_handoff_candidate"](5, 6, 406, {6: changed}))
        C = T(7, "org.app", "C", True, False, pid=499)
        self.assertIsNone(ns["close_handoff_candidate"](
            5, 6, 406, {6: shell.toplevels[6], 7: C}))

    def test_no_focus_from_kill_ack_or_tree_removal_until_original_pid_exits(self):
        shell, ns, state, clock, events, commands, snapshots, timers, accepted = actual_handoff_fixture()
        shell.close_window(5)
        self.assertEqual(commands, [(5, "kill")])
        self.assertEqual(set(shell._pending_close_handoffs), {5})
        self.assertEqual(set(shell._pending_process_exits), {5})
        self.assertEqual(shell._pending_close_handoffs[5][:2], (6, 406))
        shell._poll_windows()  # A still exists in actual Sway tree.
        self.assertEqual(commands, [(5, "kill")])
        state.tree = {6: shell.toplevels[6]}
        state.exit = "running"
        shell._poll_windows()  # Sway removal alone is NOT /proc death.
        self.assertIn("WINDOW_CLOSED:5", events)
        self.assertNotIn("PROCESS_EXITED:5:388", events)
        self.assertEqual(commands, [(5, "kill")])
        self.assertNotIn("WINDOW_ACTIVATED:6", events)
        self.assertEqual(set(shell._pending_close_handoffs), {5})
        self.assertEqual(set(shell._pending_process_exits), {5})

    def test_actual_proc_exit_then_numeric_focus_request_then_next_tree_activation(self):
        shell, ns, state, clock, events, commands, snapshots, timers, accepted = actual_handoff_fixture()
        shell.close_window(5)
        B = shell.toplevels[6]
        state.tree = {6: B}
        state.exit = "exited"
        shell._poll_windows()
        self.assertEqual(commands, [(5, "kill"), (6, "focus")])
        self.assertEqual(events[:2], ["WINDOW_CLOSED:5", "PROCESS_EXITED:5:388"])
        self.assertNotIn("WINDOW_ACTIVATED:6", events)
        self.assertEqual(shell._pending_close_handoffs, {})
        self.assertEqual(shell._pending_process_exits, {})
        self.assertIn(6, shell._pending_focus)
        self.assertTrue(snapshots and 5 not in snapshots[-1])
        self.assertTrue(any(delay == 175 for delay, _ in timers))
        # IPC ACK is not activation proof. A second fresh real get_tree must
        # observe B focused before any activation/focus marker can be emitted.
        state.tree = {6: ns["Toplevel"](6, "foot", "B", True, False, pid=406)}
        shell._poll_windows()
        self.assertEqual(commands, [(5, "kill"), (6, "focus")])
        self.assertIn("WINDOW_ACTIVATED:6", events)
        self.assertIn("WINDOW_FOCUSED:6", events)
        self.assertLess(events.index("PROCESS_EXITED:5:388"),
                        events.index("WINDOW_ACTIVATED:6"))
        self.assertEqual(snapshots[-1][6].focused, True)
        shell._poll_windows()
        self.assertEqual(events.count("WINDOW_ACTIVATED:6"), 1)

    def test_failed_kill_ack_or_missing_original_proc_watch_never_handoffs(self):
        for kill_ack, proc in ((False, ("S", 777)), (True, None)):
            with self.subTest(kill_ack=kill_ack, proc=proc):
                shell, _, state, _, events, commands, _, _, _ = actual_handoff_fixture(
                    kill_ack=kill_ack, proc=proc)
                shell.close_window(5)
                self.assertEqual(shell._pending_close_handoffs, {})
                self.assertEqual(shell._pending_process_exits, {})
                state.tree = {6: shell.toplevels[6]}
                state.exit = "exited"
                shell._poll_windows()
                self.assertEqual(commands, [(5, "kill")])
                self.assertNotIn("PROCESS_EXITED:5:388", events)
                self.assertNotIn("WINDOW_ACTIVATED:6", events)

    def test_reused_or_running_pid_never_yields_focus_or_exit_marker(self):
        for status in ("reused", "running"):
            with self.subTest(status=status):
                shell, _, state, _, events, commands, _, _, _ = actual_handoff_fixture()
                shell.close_window(5)
                state.tree = {6: shell.toplevels[6]}
                state.exit = status
                with redirect_stderr(io.StringIO()):
                    shell._poll_windows()
                self.assertEqual(commands, [(5, "kill")])
                self.assertNotIn("PROCESS_EXITED:5:388", events)
                self.assertNotIn("WINDOW_ACTIVATED:6", events)

    def test_stale_successor_pid_or_other_focus_prevents_handoff_even_after_exit(self):
        for changed in (
            lambda T,B: T(6, "foot", "B", False, False, pid=999),
            lambda T,B: T(6, "foot", "B", False, True, pid=406),
            lambda T,B: T(6, "foot", "B", False, False, visible=False, pid=406),
        ):
            with self.subTest(changed=changed):
                shell, ns, state, _, events, commands, _, _, _ = actual_handoff_fixture()
                shell.close_window(5)
                state.exit = "exited"
                state.tree = {6: changed(ns["Toplevel"], shell.toplevels[6])}
                shell._poll_windows()
                self.assertEqual(commands, [(5, "kill")])
                self.assertIn("PROCESS_EXITED:5:388", events)
                self.assertNotIn("WINDOW_ACTIVATED:6", events)
        shell, ns, state, _, events, commands, _, _, _ = actual_handoff_fixture()
        shell.close_window(5)
        C = ns["Toplevel"](7, "org.app", "C", True, False, pid=499)
        state.tree = {6: shell.toplevels[6], 7: C}
        state.exit = "exited"
        shell._poll_windows()
        self.assertEqual(commands, [(5, "kill")])
        self.assertIn("PROCESS_EXITED:5:388", events)
        self.assertNotIn("WINDOW_ACTIVATED:6", events)

    def test_handoff_timeout_fails_closed_without_focus_or_fabricated_activation(self):
        shell, _, state, clock, events, commands, _, _, _ = actual_handoff_fixture()
        shell.close_window(5)
        state.tree = {6: shell.toplevels[6]}
        state.exit = "exited"
        clock.now = 109.1
        with redirect_stderr(io.StringIO()):
            shell._poll_windows()
        self.assertEqual(commands, [(5, "kill")])
        self.assertNotIn("WINDOW_ACTIVATED:6", events)
        self.assertEqual(shell._pending_close_handoffs, {})

    def test_sway_focus_ack_false_does_not_fabricate_activation(self):
        shell, _, state, _, events, commands, _, _, accepted = actual_handoff_fixture()
        accepted.focus = False
        shell.close_window(5)
        state.tree = {6: shell.toplevels[6]}
        state.exit = "exited"
        shell._poll_windows()
        self.assertEqual(commands, [(5, "kill"), (6, "focus")])
        self.assertNotIn("WINDOW_ACTIVATED:6", events)
        self.assertNotIn("WINDOW_FOCUSED:6", events)
        self.assertEqual(shell._pending_close_handoffs, {})


if __name__ == "__main__":
    unittest.main()
