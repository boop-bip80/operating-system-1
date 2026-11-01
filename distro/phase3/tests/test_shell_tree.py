# SPDX-License-Identifier: GPL-2.0-only
"""GI-free Sway IPC/model regressions for the original Phase 3 shell.

Representative Sway get_tree fixtures exercise production pure functions;
these synthetic trees are never claimed as QEMU acceptance evidence.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
import unittest

SHELL = Path(__file__).resolve().parents[1] / "shell" / "shell.py"
PURE = {
    "Toplevel", "parse_toplevels", "is_terminal", "dock_model",
    "active_toplevel_id", "ipc_signature", "ipc_snapshot", "window_events",
}
CONSTANTS = {"MAX_IPC_WINDOWS", "MAX_IPC_SNAPSHOT_BYTES"}


def load_pure_shell(source: str) -> dict:
    """Compile actual pure functions from shell.py, without importing GTK."""
    tree = ast.parse(source, filename="phase3/shell.py")
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in PURE:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in CONSTANTS
            for target in node.targets
        ):
            selected.append(node)
    found = {
        node.name for node in selected
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    if found != PURE:
        raise AssertionError(f"production pure IPC functions missing: {sorted(PURE - found)}")
    namespace = {"__name__": __name__, "dataclass": dataclass, "Any": Any, "json": json}
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, filename="phase3/shell.py", mode="exec"), namespace)
    if not all(name in namespace for name in CONSTANTS):
        raise AssertionError("production IPC snapshot limits missing")
    return namespace


def leaf(con_id: int, pid: int, *, focused=False, visible=True,
         scratchpad="none", app_id="foot", title=None) -> dict:
    return {
        "type": "con", "id": con_id, "app_id": app_id,
        "name": title or f"Window {con_id}", "pid": pid,
        "focused": focused, "visible": visible, "scratchpad_state": scratchpad,
        "nodes": [], "floating_nodes": [],
    }


def sway_tree(*clients: dict) -> dict:
    """Sway-like root/output/workspace/floating-wrapper IPC hierarchy."""
    return {"type": "root", "id": 1, "nodes": [{
        "type": "output", "id": 2, "nodes": [{
            "type": "workspace", "id": 3, "nodes": [],
            "floating_nodes": [
                {"type": "floating_con", "id": 40 + i,
                 "nodes": [item], "floating_nodes": []}
                for i, item in enumerate(clients)
            ],
        }], "floating_nodes": [],
    }], "floating_nodes": []}


class SwayTreeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.logic = load_pure_shell(SHELL.read_text(encoding="utf-8"))

    def call(self, name: str, *args):
        return self.logic[name](*args)

    def test_two_real_foot_nodes_focus_b_then_pinned_a(self):
        # Traversal order differs from creation order: newest B appears first.
        before = self.call("parse_toplevels", sway_tree(
            leaf(372, 20372, focused=True), leaf(371, 20371),
        ))
        self.assertEqual(set(before), {371, 372})
        self.assertEqual(self.call("active_toplevel_id", before), 372)
        terminal, others = self.call("dock_model", before)
        self.assertEqual(terminal.con_id, 371)
        self.assertEqual(others, ())
        self.assertEqual((before[371].pid, before[372].pid), (20371, 20372))
        after = self.call("parse_toplevels", sway_tree(
            leaf(372, 20372), leaf(371, 20371, focused=True),
        ))
        self.assertEqual(self.call("active_toplevel_id", after), 371)
        self.assertEqual(self.call("dock_model", after)[0].con_id, 371)
        self.assertEqual(set(after), {371, 372})  # B stays live.
        self.assertIn("WINDOW_ACTIVATED:371", self.call("window_events", before, after))
        snapshot = json.loads(self.call("ipc_snapshot", after, 2))
        self.assertEqual(snapshot["source"], "sway.get_tree")
        self.assertEqual([w["id"] for w in snapshot["windows"]], [371, 372])
        self.assertEqual([w["focused"] for w in snapshot["windows"]], [True, False])

    def test_scratchpad_hide_then_restore_from_sway_state(self):
        original = self.call("parse_toplevels", sway_tree(
            leaf(371, 20371, focused=True),
        ))
        hidden = self.call("parse_toplevels", sway_tree(
            leaf(371, 20371, visible=False, scratchpad="fresh"),
        ))
        self.assertTrue(hidden[371].minimized)
        self.assertFalse(hidden[371].visible)
        self.assertIn("WINDOW_MINIMIZED:371", self.call("window_events", original, hidden))
        self.assertTrue(json.loads(self.call("ipc_snapshot", hidden, 2))
                        ["windows"][0]["minimized"])
        restored = self.call("parse_toplevels", sway_tree(
            leaf(371, 20371, focused=True, scratchpad="changed", visible=True),
        ))
        self.assertFalse(restored[371].minimized)
        transitions = self.call("window_events", hidden, restored)
        self.assertIn("WINDOW_RESTORED:371", transitions)
        self.assertIn("WINDOW_ACTIVATED:371", transitions)
        self.assertEqual(self.call("dock_model", restored)[0].con_id, 371)

    def test_close_drops_only_disappeared_real_id(self):
        opened = self.call("parse_toplevels", sway_tree(
            leaf(371, 20371), leaf(372, 20372, focused=True),
        ))
        after = self.call("parse_toplevels", sway_tree(
            leaf(372, 20372, focused=True),
        ))
        events = self.call("window_events", opened, after)
        self.assertIn("WINDOW_CLOSED:371", events)
        self.assertNotIn("WINDOW_CLOSED:372", events)
        self.assertEqual(self.call("dock_model", after)[0].con_id, 372)
        self.assertEqual(set(after), {372})

    def test_pin_is_stable_and_other_clients_sorted_capped(self):
        self.assertEqual(self.call("dock_model", {}), (None, ()))
        clients = {
            900 - i: self.logic["Toplevel"](
                900 - i, f"org.example.app{i}", "Other", False, False,
            ) for i in range(12)
        }
        clients[372] = self.logic["Toplevel"](372, "foot", "B", True, False)
        clients[371] = self.logic["Toplevel"](371, "foot", "A", False, False)
        terminal, others = self.call("dock_model", clients)
        self.assertEqual(terminal.con_id, 371)
        self.assertEqual(len(others), 9)
        self.assertEqual([w.con_id for w in others], sorted(w.con_id for w in others))
        self.assertNotIn(372, [w.con_id for w in others])

    def test_tree_root_focus_and_id_validation(self):
        with self.assertRaises(ValueError):
            self.call("parse_toplevels", {"type": "output", "nodes": []})
        both = self.call("parse_toplevels", sway_tree(
            leaf(371, 20371, focused=True), leaf(372, 20372, focused=True),
        ))
        with self.assertRaises(ValueError):
            self.call("active_toplevel_id", both)
        with self.assertRaises(ValueError):
            self.call("window_events", {}, both)
        invalid = sway_tree(
            leaf(0, 20000), leaf(True, 20001), leaf(371, -1, app_id=""),
        )
        self.assertEqual(self.call("parse_toplevels", invalid), {})

    def test_legacy_class_and_pid_bounds(self):
        client = leaf(420, 0, app_id="")
        client["window_properties"] = {"class": "org.example.Legacy"}
        clients = self.call("parse_toplevels", sway_tree(client))
        self.assertEqual(clients[420].app_id, "org.example.Legacy")
        self.assertIsNone(clients[420].pid)
        client["pid"] = True
        self.assertIsNone(self.call("parse_toplevels", sway_tree(client))[420].pid)

    def test_ipc_signature_ignores_title_but_tracks_focus(self):
        before = self.call("parse_toplevels", sway_tree(leaf(371, 20371)))
        title_only = self.call("parse_toplevels", sway_tree(
            leaf(371, 20371, title="Different title"),
        ))
        self.assertEqual(self.call("ipc_signature", before),
                         self.call("ipc_signature", title_only))
        self.assertEqual(self.call("window_events", before, title_only), [])
        focused = self.call("parse_toplevels", sway_tree(
            leaf(371, 20371, focused=True),
        ))
        self.assertNotEqual(self.call("ipc_signature", before),
                            self.call("ipc_signature", focused))
        self.assertEqual(self.call("window_events", before, focused),
                         ["WINDOW_ACTIVATED:371"])

    def test_snapshot_bounded_and_source_identity(self):
        observed = self.call("parse_toplevels", sway_tree(
            leaf(372, 20372, focused=True), leaf(371, 20371),
        ))
        for seq in (1, 100):
            with self.subTest(seq=seq):
                text = self.call("ipc_snapshot", observed, seq)
                self.assertLessEqual(len(text.encode()), self.logic["MAX_IPC_SNAPSHOT_BYTES"])
                self.assertEqual(json.loads(text)["seq"], seq)
                self.assertEqual(json.loads(text)["source"], "sway.get_tree")
        for bad in (0, -1, True, "1"):
            with self.subTest(sequence=bad):
                with self.assertRaises(ValueError):
                    self.call("ipc_snapshot", observed, bad)
        too_many = {
            i: self.logic["Toplevel"](i, "foot", str(i), False, False)
            for i in range(1, self.logic["MAX_IPC_WINDOWS"] + 2)
        }
        with self.assertRaises(ValueError):
            self.call("ipc_snapshot", too_many, 1)


if __name__ == "__main__":
    unittest.main()
