# SPDX-License-Identifier: GPL-2.0-only
"""GI-free regressions for the native Phase 3 post-Dock-close focus wait.

Execute the real nested boot.wait_observed function extracted from production
smoke-test.py by AST. This is a bounded host diagnostic, not guest acceptance;
only the native Sway get_tree markers can qualify WINDOW_ACTIVATED.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


SMOKE = Path(__file__).resolve().parents[1] / "scripts" / "smoke-test.py"
FOCUS_DESCRIPTION = "surviving terminal B focus after Sway Dock kill"


def source_focus_wait(source: str):
    tree = ast.parse(source, filename=str(SMOKE))
    boot = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "boot")
    nested = next(node for node in ast.walk(boot)
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "wait_observed")
    bounded = [
        node for node in ast.walk(boot)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "wait_observed"
        and any(key.arg == "max_wait" for key in node.keywords)
    ]
    if len(bounded) != 1:
        raise AssertionError("expected exactly one bounded post-Close Sway wait")
    call = bounded[0]
    if not (len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)
            and call.args[1].value == FOCUS_DESCRIPTION):
        raise AssertionError("bounded Sway wait applies to the wrong event")
    limit = next(key.value for key in call.keywords if key.arg == "max_wait")
    if not isinstance(limit, ast.Constant) or limit.value != 15:
        raise AssertionError("real post-Close focus wait must be 15 seconds")
    source_func = ast.fix_missing_locations(
        ast.Module(body=[nested], type_ignores=[]))
    source_predicate = ast.fix_missing_locations(ast.Expression(call.args[0]))
    return source_func, source_predicate


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, interval):
        self.now += interval


class FakeMonitor:
    def __init__(self, markers, on_check=None):
        self.markers = list(markers)
        self.on_check = on_check
        self.checks = 0

    def output(self):
        return "\n".join(self.markers)

    def check(self):
        self.checks += 1
        if self.on_check is not None:
            self.on_check(self)


def actual_wait(source: str, monitor: FakeMonitor, clock: FakeClock,
                deadline=300.0):
    function, predicate = source_focus_wait(source)
    namespace = {
        "Phase3Failure": type("Phase3Failure", (RuntimeError,), {}),
        "lines": str.splitlines,
        "time": clock,
        "monitor": monitor,
        "deadline": deadline,
    }
    exec(compile(function, filename=str(SMOKE), mode="exec"), namespace)
    predicate_ns = {
        "result": SimpleNamespace(window_id="5", second_window_id="6"),
        "re": re,
    }
    real_predicate = eval(compile(predicate, filename=str(SMOKE), mode="eval"),
                          predicate_ns)
    return namespace["wait_observed"], real_predicate, namespace["Phase3Failure"]


class PostCloseFocusDeadline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SMOKE.read_text(encoding="utf-8")

    def test_post_close_predicate_requires_activation_after_real_close(self):
        _wait, predicate, _fail = actual_wait(self.source, FakeMonitor([]), FakeClock())
        self.assertFalse(predicate(["P3:WINDOW_ACTIVATED:6", "P3:WINDOW_CLOSED:5"]))
        self.assertFalse(predicate(["P3:WINDOW_CLOSED:5", "P3:IPC_TREE:{\"focused\":false}"]))
        self.assertFalse(predicate(["P3:WINDOW_CLOSED:5", "P3:WINDOW_ACTIVATED:7"]))
        self.assertTrue(predicate(["P3:WINDOW_CLOSED:5", "P3:WINDOW_ACTIVATED:6"]))

    def test_missing_focus_fails_in_15s_with_real_bounded_ipc_snapshot(self):
        real_ipc = ('P3:IPC_TREE:{"seq":7,"source":"sway.get_tree",'
                    '"windows":[{"id":6,"focused":false}]}')
        clock = FakeClock()
        mon = FakeMonitor(["P3:WINDOW_CLOSED:5", real_ipc,
                           "P3:PROCESS_EXITED:5:388"])
        wait, predicate, Failure = actual_wait(self.source, mon, clock)
        with self.assertRaisesRegex(Failure, "last source IPC_TREE=") as caught:
            wait(predicate, FOCUS_DESCRIPTION, max_wait=15)
        self.assertGreaterEqual(clock.now, 15)
        self.assertLess(clock.now, 16)
        self.assertIn('"focused":false', str(caught.exception))
        self.assertNotIn("WINDOW_ACTIVATED:6", mon.output())

    def test_real_focus_transition_passes_only_after_source_event(self):
        clock = FakeClock()
        def observed(mon):
            if clock.now >= 2.0 and "P3:WINDOW_ACTIVATED:6" not in mon.markers:
                mon.markers.extend([
                    'P3:IPC_TREE:{"seq":8,"source":"sway.get_tree",'
                    '"windows":[{"id":6,"focused":true}]}',
                    "P3:WINDOW_ACTIVATED:6",
                ])
        mon = FakeMonitor(["P3:WINDOW_CLOSED:5",
                           "P3:PROCESS_EXITED:5:388"], observed)
        wait, predicate, _Failure = actual_wait(self.source, mon, clock)
        wait(predicate, FOCUS_DESCRIPTION, max_wait=15)
        self.assertGreaterEqual(clock.now, 2.0)
        self.assertLess(clock.now, 15)
        self.assertIn("P3:WINDOW_ACTIVATED:6", mon.output())

    def test_timeout_diagnostic_is_capped_for_untrusted_serial(self):
        clock = FakeClock()
        mon = FakeMonitor(["P3:WINDOW_CLOSED:5", "P3:IPC_TREE:" + "x" * 15000])
        wait, predicate, Failure = actual_wait(self.source, mon, clock)
        with self.assertRaises(Failure) as caught:
            wait(predicate, FOCUS_DESCRIPTION, max_wait=1)
        self.assertLess(len(str(caught.exception)), 400)
        self.assertIn("last source IPC_TREE=" + "x" * 240, str(caught.exception))

    def test_default_observations_still_use_full_boot_deadline(self):
        clock = FakeClock()
        mon = FakeMonitor(["P3:WINDOW_CLOSED:5"])
        wait, predicate, Failure = actual_wait(self.source, mon, clock, deadline=3.0)
        with self.assertRaisesRegex(Failure, "timed out waiting") as caught:
            wait(predicate, FOCUS_DESCRIPTION)
        self.assertGreaterEqual(clock.now, 3.0)
        self.assertNotIn("last source IPC_TREE", str(caught.exception))

    def test_invalid_wait_budgets_cannot_disable_fail_closed_bound(self):
        clock = FakeClock()
        mon = FakeMonitor([])
        wait, predicate, _Failure = actual_wait(self.source, mon, clock)
        for budget in (0, -1, 31, 300):
            with self.subTest(max_wait=budget):
                with self.assertRaises(ValueError):
                    wait(predicate, FOCUS_DESCRIPTION, max_wait=budget)
        self.assertEqual(mon.checks, 0)


if __name__ == "__main__":
    unittest.main()
