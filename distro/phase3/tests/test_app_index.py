# SPDX-License-Identifier: GPL-2.0-only
"""GI-free source-bound installed desktop-entry inventory regressions.

Compiles production Gio AppInfo ID sanitization/index and offline verifier
against synthetic desktop IDs, not a real Gio database or graphical VM.
Actual acceptance additionally requires QMP-mapped launcher and signed rootfs.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any
import unittest


PHASE = Path(__file__).resolve().parents[1]
SHELL = PHASE / "shell" / "shell.py"
MANIFEST = PHASE / "scripts" / "manifest.py"
SMOKE = PHASE / "scripts" / "smoke-test.py"
SPEC = importlib.util.spec_from_file_location("phase3_app_index_smoke", SMOKE)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def extract(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defs = [node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name in names]
    if {node.name for node in defs} != set(names):
        raise AssertionError("missing required production Gio index function")
    module = ast.fix_missing_locations(ast.Module(body=defs, type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def source_index():
    return extract(
        SHELL, {"safe_desktop_id", "app_index_payload"},
        {"Any": Any, "re": re, "json": json, "hashlib": hashlib},
    )


def offline_index():
    def require(ok, reason):
        if not ok:
            raise ValueError(reason)
    env = {"json": json, "re": re, "hashlib": hashlib, "require": require}
    return extract(MANIFEST, {"check_native_app_index"}, env)["check_native_app_index"]


ELIGIBLE = [
    "foot-server.desktop", "foot.desktop", "footclient.desktop",
    "org.example.Files.desktop",
]
DISPLAYED = [
    "foot-server.desktop", "footclient.desktop", "org.example.Files.desktop",
]


def index_marker(eligible=None, displayed=None):
    funcs = source_index()
    value = funcs["app_index_payload"](
        list(ELIGIBLE if eligible is None else eligible),
        list(DISPLAYED if displayed is None else displayed),
    )
    return "P3:APP_INDEX:" + json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def ordered_serial(marker):
    return [
        "P3:LAUNCHER_OPEN",
        "P3:COMPACT_TARGET:60:444:92:26",
        "P3:TERMINAL_TARGET:140:153:248:40",
        marker, "P3:APP_LAUNCHED:foot",
    ]


class AppIndexSourceContract(unittest.TestCase):
    def test_safe_real_ascii_desktop_ids_only(self):
        safe = source_index()["safe_desktop_id"]
        for value in ("foot.desktop", "footclient.desktop",
                      "org.example.App.desktop", "a-b_2+.desktop"):
            self.assertTrue(safe(value), value)
        for value in (
            "", None, False, 5, "../escape.desktop", "/usr/share/x.desktop",
            "shell;run.desktop", "a b.desktop", "unicode-é.desktop",
            "x.desktop\nINJECTED", "a" * 129 + ".desktop",
            "wrong.txt", "foot.desktop/../other.desktop",
        ):
            with self.subTest(id=value):
                self.assertFalse(safe(value))

    def test_real_eligible_and_displayed_lists_bind_canonical_dual_hash(self):
        make = source_index()["app_index_payload"]
        value = make(list(reversed(ELIGIBLE)), list(reversed(DISPLAYED)))
        self.assertEqual(value["source"], "Gio.AppInfo.get_all")
        self.assertEqual(value["eligible_ids"], sorted(ELIGIBLE))
        self.assertEqual(value["displayed_ids"], sorted(DISPLAYED))
        self.assertEqual((value["eligible_count"], value["displayed_count"]),
                         (4, 3))
        payload = json.dumps({
            "displayed_ids": sorted(DISPLAYED), "eligible_ids": sorted(ELIGIBLE),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.assertEqual(value["sha256"],
                         hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def test_source_rejects_duplicate_malformed_unbound_and_missing_entries(self):
        make = source_index()["app_index_payload"]
        cases = (
            ([], []),
            (["foot.desktop", "foot.desktop"], []),
            (["../evil.desktop"], []),
            (["files.desktop"], []),
            (["foot.desktop"], ["absent.desktop"]),
            (["foot.desktop", "files.desktop"], ["files.desktop", "files.desktop"]),
            (["foot.desktop", "A.desktop"], ["é.desktop"]),
            (["foot.desktop"] * 61, []),
        )
        for eligible, displayed in cases:
            with self.subTest(eligible=eligible, displayed=displayed):
                with self.assertRaises(ValueError):
                    make(eligible, displayed)

    def test_empty_displayed_allowed_only_for_actual_foot_appinfo_entry(self):
        make = source_index()["app_index_payload"]
        value = make(["foot.desktop"], [])
        self.assertEqual(value["displayed_count"], 0)
        self.assertEqual(value["eligible_ids"], ["foot.desktop"])


class AppIndexHostReplay(unittest.TestCase):
    def test_live_serial_parser_reconstructs_real_desktop_id_index(self):
        marker = index_marker()
        position, index = smoke.app_index_from_output(
            "\n".join(ordered_serial(marker)) + "\n",
        )
        self.assertEqual(position, 3)
        self.assertEqual(index["eligible_count"], 4)
        self.assertEqual(index["displayed_count"], 3)

    def test_missing_duplicate_noncanonical_or_invalid_json_fails(self):
        marker = index_marker()
        samples = (
            [],
            [marker, marker],
            ["P3:APP_INDEX:{not-json}"],
            ["P3:APP_INDEX:" + json.dumps(
                json.loads(marker.split(":", 2)[2]), sort_keys=False)],
        )
        for replacement in samples:
            with self.subTest(replacement=replacement):
                serial = ordered_serial(marker)
                pos = serial.index(marker)
                serial[pos:pos + 1] = replacement
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.app_index_from_output("\n".join(serial) + "\n")

    def test_forged_source_eligible_counts_hashes_and_subset_fail(self):
        marker = index_marker()
        base = json.loads(marker.split(":", 2)[2])
        for key, value in (
            ("source", "host.synthetic"),
            ("eligible_count", 5),
            ("displayed_count", 2),
            ("sha256", "0" * 64),
            ("eligible_ids", ["foot.desktop", "../escape.desktop"]),
            ("displayed_ids", ["../fake.desktop"]),
            ("displayed_ids", ["not-installed.desktop"]),
            ("displayed_ids", []),
            ("displayed_ids", sorted(DISPLAYED + ["foot.desktop"])),
        ):
            with self.subTest(key=key, value=value):
                modified = copy.deepcopy(base)
                modified[key] = value
                raw = json.dumps(modified, sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=True)
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.app_index_from_output(
                        "\n".join(ordered_serial("P3:APP_INDEX:" + raw)) + "\n",
                    )

    def test_forged_sorted_duplicate_or_nonsafe_ids_rejected_even_rehashed(self):
        make = source_index()["app_index_payload"]
        base = make(ELIGIBLE, DISPLAYED)
        for eligible, displayed in (
            (list(reversed(sorted(ELIGIBLE))), sorted(DISPLAYED)),
            (sorted(ELIGIBLE) + ["foot.desktop"], sorted(DISPLAYED)),
            (sorted(ELIGIBLE), list(reversed(sorted(DISPLAYED)))),
            (sorted(ELIGIBLE), sorted(DISPLAYED) + ["foot.desktop"]),
        ):
            with self.subTest(eligible=eligible, displayed=displayed):
                altered = copy.deepcopy(base)
                altered["eligible_ids"] = eligible
                altered["displayed_ids"] = displayed
                altered["eligible_count"] = len(eligible)
                altered["displayed_count"] = len(displayed)
                canonical = json.dumps(
                    {"eligible_ids": eligible, "displayed_ids": displayed},
                    sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                )
                altered["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
                raw = json.dumps(altered, sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=True)
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.app_index_from_output("P3:APP_INDEX:" + raw + "\n")


class AppIndexOfflineReplay(unittest.TestCase):
    def test_manifest_replays_real_gio_inventory_after_gtk_map(self):
        marker = index_marker()
        offline_index()(ordered_serial(marker))
        for altered in (
            ordered_serial(marker)[:3] + ordered_serial(marker)[4:],
            [marker] + [x for x in ordered_serial(marker) if x != marker],
            [x for x in ordered_serial(marker) if x != marker] + [marker],
        ):
            with self.assertRaises(ValueError):
                offline_index()(altered)

    def test_offline_rejects_rehashed_displayed_foot_and_forged_eligible(self):
        marker = index_marker()
        base = json.loads(marker.split(":", 2)[2])
        for eligible, displayed in (
            (ELIGIBLE, DISPLAYED + ["foot.desktop"]),
            (["org.example.Files.desktop"], []),
            (["foot.desktop", "../forged.desktop"], []),
        ):
            modified = copy.deepcopy(base)
            modified["eligible_ids"] = sorted(eligible)
            modified["displayed_ids"] = sorted(displayed)
            modified["eligible_count"] = len(eligible)
            modified["displayed_count"] = len(displayed)
            modified["sha256"] = hashlib.sha256(json.dumps(
                {"eligible_ids": sorted(eligible),
                 "displayed_ids": sorted(displayed)},
                sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            ).encode()).hexdigest()
            raw = json.dumps(modified, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True)
            with self.subTest(eligible=eligible, displayed=displayed):
                with self.assertRaises(ValueError):
                    offline_index()(ordered_serial("P3:APP_INDEX:" + raw))


if __name__ == "__main__":
    unittest.main()
