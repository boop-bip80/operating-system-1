# SPDX-License-Identifier: GPL-2.0-only
"""GI-free Linux /proc process-exit oracle regressions for Phase 3 Dock Close.

Tests compile the production parser, identity-checker and process-watch method
via AST with synthetic /proc records and Sway trees; they cannot replace a
guest kernel /proc observation or live QEMU close/exit evidence.
"""

from __future__ import annotations

import ast
from contextlib import redirect_stderr
from dataclasses import dataclass
import io
from pathlib import Path
import sys
import types
import unittest


SHELL = Path(__file__).resolve().parents[1] / "shell" / "shell.py"
FUNCTIONS = {"parse_proc_stat", "read_proc_state", "process_exit_status"}


def actual_process_oracle():
    source = SHELL.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SHELL))
    production = next(node for node in tree.body if
                      isinstance(node, ast.ClassDef) and node.name == "Phase3Shell")
    extracted = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
    ) or (
        isinstance(node, ast.ClassDef) and node.name == "ProcessWatch"
    )]
    method = next(node for node in production.body if
                  isinstance(node, ast.FunctionDef) and node.name == "_check_process_exits")
    if {node.name for node in extracted} != FUNCTIONS | {"ProcessWatch"}:
        raise AssertionError("native PID proof functions missing")
    shell_type = ast.ClassDef(
        name="ProcessExitShell", bases=[], keywords=[],
        body=[method], decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(
        body=extracted + [shell_type], type_ignores=[],
    ))
    emitted = []
    timestamp = types.SimpleNamespace(value=100.)
    namespace = {
        "__name__": __name__, "dataclass": dataclass, "Path": Path,
        "Toplevel": object, "sys": sys,
        "time": types.SimpleNamespace(monotonic=lambda: timestamp.value),
        "emit": lambda marker: emitted.append(marker),
    }
    exec(compile(module, str(SHELL), "exec"), namespace)
    return namespace, emitted, timestamp


def proc_stat(pid=20371, state="R", start=912345, comm="foot (worker) )name"):
    # /proc/<pid>/stat: field 3 state, field 22 starttime, rest is filler.
    fields = [state] + ["0"] * 18 + [str(start)] + ["0"] * 8
    return f"{pid} ({comm}) " + " ".join(fields)


class ProcStatParsing(unittest.TestCase):
    def test_field_22_survives_nested_parentheses_and_spaces(self):
        ns, _, _ = actual_process_oracle()
        self.assertEqual(
            ns["parse_proc_stat"](proc_stat()),
            (20371, "R", 912345),
        )
        self.assertEqual(
            ns["parse_proc_stat"](proc_stat(pid=999, state="Z", start=1,
                                             comm="strange ) (comm) : ")),
            (999, "Z", 1),
        )

    def test_rejects_missing_fields_oversize_invalid_start_and_state(self):
        ns, _, _ = actual_process_oracle()
        parse = ns["parse_proc_stat"]
        for invalid in (
            "not a /proc stat", "20371 (foot) R 1 2 3",
            "20371 (foot) " + "R " * 4097,
            proc_stat(state="RUN"), proc_stat(state="1"),
            proc_stat(start=0), proc_stat(start=-9), proc_stat(start="NaN"),
            proc_stat(pid="bad"),
        ):
            with self.subTest(record=invalid[:60]):
                with self.assertRaises(ValueError):
                    parse(invalid)

    def test_read_proc_requires_strict_pid_and_matched_stat_identity(self):
        ns, _, _ = actual_process_oracle()
        read = ns["read_proc_state"]
        for invalid in (0, -1, True, "20371", 2**31):
            with self.subTest(pid=invalid):
                with self.assertRaises(ValueError):
                    read(invalid)

        class FakePath:
            stat = proc_stat(pid=20371, state="S", start=123)
            missing = False
            symlink = False

            def __init__(self, path="/proc"):
                self.value = path

            def __truediv__(self, component):
                return FakePath(self.value + "/" + str(component))

            @property
            def parent(self):
                return FakePath(self.value.rsplit("/", 1)[0])

            def is_symlink(self):
                return self.symlink

            def open(self, *_args, **_kwargs):
                if self.missing:
                    raise FileNotFoundError("process disappeared")
                return io.StringIO(self.stat)

        ns["Path"] = FakePath
        self.assertEqual(read(20371), ("S", 123))
        FakePath.missing = True
        self.assertIsNone(read(20371))
        FakePath.missing = False
        FakePath.stat = proc_stat(pid=999, start=123)
        with self.assertRaises(ValueError):
            read(20371)
        FakePath.stat = proc_stat(pid=20371, start=123)
        FakePath.symlink = True
        with self.assertRaises(ValueError):
            read(20371)

    def test_pid_reuse_and_non_zombie_never_count_as_exit(self):
        ns, _, _ = actual_process_oracle()
        status = ns["process_exit_status"]
        for stat_value, expected in (
            (None, "exited"), (("Z", 321), "exited"),
            (("R", 321), "running"), (("S", 321), "running"),
            (("Z", 999), "reused"), (("R", 999), "reused"),
        ):
            with self.subTest(stat=stat_value):
                ns["read_proc_state"] = lambda _pid, value=stat_value: value
                self.assertEqual(status(20371, 321), expected)

    def test_invalid_original_starttime_fails_closed(self):
        ns, _, _ = actual_process_oracle()
        for invalid in (0, -1, "321", None, True):
            with self.subTest(original=invalid):
                with self.assertRaises(ValueError):
                    ns["process_exit_status"](20371, invalid)


class ProcessWatchLifecycle(unittest.TestCase):
    def probe(self, status="exited", *, deadline=120.):
        ns, events, timestamp = actual_process_oracle()
        watch = ns["ProcessWatch"](
            con_id=371, pid=20371, start_time=321, deadline=deadline,
        )
        shell = ns["ProcessExitShell"]()
        shell._pending_process_exits = {371: watch}
        ns["process_exit_status"] = lambda _pid, _start: status
        return shell, ns, events, timestamp

    def test_sway_client_must_disappear_before_exit_can_be_claimed(self):
        shell, ns, events, _ = self.probe("exited")
        shell._check_process_exits({371: object()})
        self.assertEqual(events, [])
        self.assertIn(371, shell._pending_process_exits)
        ns["process_exit_status"] = lambda _pid, _start: "running"
        shell._check_process_exits({})
        self.assertEqual(events, [])
        self.assertIn(371, shell._pending_process_exits)

    def test_genuine_exit_after_tree_removal_emits_one_observed_pid(self):
        shell, _, events, _ = self.probe("exited")
        shell._check_process_exits({})
        self.assertEqual(events, ["PROCESS_EXITED:371:20371"])
        self.assertEqual(shell._pending_process_exits, {})
        shell._check_process_exits({})
        self.assertEqual(len(events), 1)

    def test_pid_reuse_and_probe_errors_suppress_success_markers(self):
        for behavior in ("reused", OSError("unavailable"), ValueError("bad stat")):
            with self.subTest(behavior=str(behavior)):
                shell, ns, events, _ = self.probe("running")
                def result(_pid, _start):
                    if isinstance(behavior, Exception):
                        raise behavior
                    return behavior
                ns["process_exit_status"] = result
                with redirect_stderr(io.StringIO()):
                    shell._check_process_exits({})
                self.assertEqual(events, [])
                self.assertEqual(shell._pending_process_exits, {})

    def test_watch_deadline_does_not_forge_exit(self):
        shell, _, events, timestamp = self.probe("exited", deadline=99.)
        timestamp.value = 100.
        with redirect_stderr(io.StringIO()):
            shell._check_process_exits({})
        self.assertEqual(events, [])
        self.assertEqual(shell._pending_process_exits, {})


if __name__ == "__main__":
    unittest.main()
