# SPDX-License-Identifier: GPL-2.0-only
"""GI-free direct Sway IPC and real workspace-exclusive-zone oracle regressions.

AST-extracts production shell sway_ipc/layout_proof and offline manifest
check_native_layout, with synthetic Sway JSON. No VM or compositor is faked
into acceptance: the real QEMU test must observe these native IPC replies.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
from typing import Any
import unittest


PHASE = Path(__file__).resolve().parents[1]
SHELL = PHASE / "shell" / "shell.py"
MANIFEST = PHASE / "scripts" / "manifest.py"

SMOKE = PHASE / "scripts" / "smoke-test.py"
SPEC = importlib.util.spec_from_file_location("phase3_ipc_layout_smoke", SMOKE)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def extracted_functions(path, names, namespace):
    root = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = [node for node in root.body if isinstance(node, ast.FunctionDef)
             and node.name in names]
    if {node.name for node in nodes} != set(names):
        raise AssertionError("required production Sway IPC function missing")
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


class FakeSway:
    def __init__(self):
        self.stdout = "{}"
        self.returncode = 0
        self.stderr = ""
        self.calls = []

    def set_reply(self, value, *, code=0, stderr=""):
        self.stdout = json.dumps(value)
        self.returncode = code
        self.stderr = stderr

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return types.SimpleNamespace(
            stdout=self.stdout, returncode=self.returncode, stderr=self.stderr,
        )


def live_shell_pure():
    sway = FakeSway()
    namespace = {
        "Any": Any, "json": json,
        "os": types.SimpleNamespace(
            environ={"SWAYSOCK": "/run/user/1000/sway-ipc.1000.1.sock",
                     "WAYLAND_DISPLAY": "wayland-1"}),
        "subprocess": types.SimpleNamespace(run=sway.run),
        "SWAYMSG": "/usr/bin/swaymsg",
    }
    extracted_functions(SHELL, ("_ipc_rect", "layout_proof", "sway_ipc"), namespace)
    return namespace, sway


def native_arrays():
    return (
        [{"name": "Virtual-1", "active": True, "transform": "normal",
          "rect": {"x": 0, "y": 0, "width": 1024, "height": 768},
          "scale": 1.0, "current_mode": {"width": 1024, "height": 768}}],
        [{"name": "1", "visible": True, "output": "Virtual-1",
          "rect": {"x": 0, "y": 38, "width": 1024, "height": 662}}],
    )


def canonical_marker(proof):
    return "P3:IPC_LAYOUT:" + json.dumps(
        proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def serial_items(marker):
    return [
        "P3:SWAY_READY",
        'P3:IPC_TREE:{"seq":1,"source":"sway.get_tree","windows":[]}',
        marker,
        "P3:LAYER_SHELL_READY", "P3:SHELL_READY",
    ]


def offline_layout():
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    namespace = {"json": json, "require": require}
    extracted_functions(MANIFEST, ("check_native_layout",), namespace)
    return namespace["check_native_layout"]


class RealSwayWorkspaceContract(unittest.TestCase):
    def test_valid_output_workspace_and_exclusive_zones_project_canonical_json(self):
        ns, _ = live_shell_pure()
        outputs, workspaces = native_arrays()
        proof = ns["layout_proof"](outputs, workspaces, 38, 68)
        self.assertEqual(proof, {
            "source": "sway.get_outputs+get_workspaces",
            "output": "Virtual-1", "workspace": "1",
            "output_rect": {"x": 0, "y": 0, "width": 1024, "height": 768},
            "workspace_rect": {"x": 0, "y": 38, "width": 1024, "height": 662},
            "mode": {"width": 1024, "height": 768},
            "scale": 1.0, "top_exclusive": 38, "dock_exclusive": 68,
        })
        offline_layout()(serial_items(canonical_marker(proof)))

    def test_actual_workspace_rect_not_full_monitor_is_required(self):
        ns, _ = live_shell_pure()
        for top, height in ((0, 768), (20, 680), (38, 700), (50, 700)):
            with self.subTest(top=top, height=height):
                outputs, workspaces = native_arrays()
                workspaces[0]["rect"]["y"] = top
                workspaces[0]["rect"]["height"] = height
                with self.assertRaises(ValueError):
                    ns["layout_proof"](outputs, workspaces, 38, 68)

    def test_native_gpu_mode_scale_and_complete_rectangle_are_strict(self):
        ns, _ = live_shell_pure()
        for change in (
            lambda a,b: a[0].update(name="HDMI-A-1"),
            lambda a,b: a[0].update(scale=True),
            lambda a,b: a[0].update(transform="90"),
            lambda a,b: a[0].pop("transform"),
            lambda a,b: a[0]["current_mode"].update(width=800),
            lambda a,b: a[0]["rect"].update(width=800),
            lambda a,b: b[0]["rect"].update(x=1),
            lambda a,b: b[0]["rect"].update(width=1023),
            lambda a,b: b[0]["rect"].update(y=True),
            lambda a,b: b[0]["rect"].pop("height"),
            lambda a,b: b[0].update(output="Fake-1"),
            lambda a,b: b[0].update(name=""),
        ):
            with self.subTest(change=str(change)):
                outputs, workspaces = native_arrays()
                change(outputs, workspaces)
                with self.assertRaises(ValueError):
                    ns["layout_proof"](outputs, workspaces, 38, 68)

    def test_multiple_active_outputs_or_visible_workspaces_fail(self):
        ns, _ = live_shell_pure()
        outputs, workspaces = native_arrays()
        outputs.append(copy.deepcopy(outputs[0]))
        with self.assertRaises(ValueError):
            ns["layout_proof"](outputs, workspaces, 38, 68)
        outputs, workspaces = native_arrays()
        workspaces.append(copy.deepcopy(workspaces[0]))
        with self.assertRaises(ValueError):
            ns["layout_proof"](outputs, workspaces, 38, 68)

    def test_requested_gtk_exclusive_zones_are_strict_not_bools(self):
        ns, _ = live_shell_pure()
        outputs, workspaces = native_arrays()
        for top, dock in ((0,68),(38,0),(True,68),(38,True),(39,68),(38,69)):
            with self.subTest(zones=(top,dock)):
                with self.assertRaises(ValueError):
                    ns["layout_proof"](outputs, workspaces, top, dock)


class SwayIPCReplyBoundary(unittest.TestCase):
    def test_genuine_get_outputs_workspaces_list_data_lacks_success_and_is_accepted(self):
        ns, sway = live_shell_pure()
        outputs, workspaces = native_arrays()
        for query, payload in (("get_outputs",outputs),("get_workspaces",workspaces)):
            with self.subTest(query=query):
                sway.set_reply(payload)
                self.assertEqual(ns["sway_ipc"]("-t",query),payload)
                self.assertEqual(sway.calls[-1][0],
                                 ["/usr/bin/swaymsg","-r","-t",query])

    def test_control_requires_nonempty_explicit_boolean_success_ack(self):
        ns, sway = live_shell_pure()
        for payload in ([],[{}],[{"success":False}],[{"success":1}],
                        [{"success":True},{"success":False}],{}):
            with self.subTest(payload=payload):
                sway.set_reply(payload)
                with self.assertRaises(RuntimeError):
                    ns["sway_ipc"]("[con_id=371] focus")
        sway.set_reply([{"success":True}])
        self.assertEqual(ns["sway_ipc"]("[con_id=371] focus"),
                         [{"success":True}])

    def test_rejected_query_bad_json_nonzero_and_missing_session_fail(self):
        ns, sway = live_shell_pure()
        sway.set_reply([{"success":False}])
        with self.assertRaises(RuntimeError):
            ns["sway_ipc"]("-t","get_outputs")
        sway.set_reply([{"active":True}],code=1,stderr="Sway disconnected")
        with self.assertRaises(RuntimeError):
            ns["sway_ipc"]("-t","get_outputs")
        sway.returncode = 0
        sway.stdout = "not JSON"
        with self.assertRaises(RuntimeError):
            ns["sway_ipc"]("-t","get_outputs")
        sway.set_reply([])
        ns["os"].environ.pop("SWAYSOCK")
        with self.assertRaises(RuntimeError):
            ns["sway_ipc"]("-t","get_outputs")


class OfflineLayoutReplay(unittest.TestCase):
    def test_malformed_duplicate_forged_source_or_order_rejected(self):
        ns, _ = live_shell_pure()
        outputs, workspaces = native_arrays()
        proof = ns["layout_proof"](outputs, workspaces, 38, 68)
        marker = canonical_marker(proof)
        verify = offline_layout()
        mutations = (
            lambda items: items[:2] + items[3:],
            lambda items: items[:3] + [marker] + items[3:],
            lambda items: items[:2] + ["P3:IPC_LAYOUT:not-json"] + items[3:],
            lambda items: items[:2] + ["P3:IPC_LAYOUT:" + json.dumps(proof)] + items[3:],
            lambda items: [marker] + [item for item in items if item != marker],
            lambda items: [item for item in items if item != marker] + [marker],
        )
        for mutate in mutations:
            with self.subTest(mutate=str(mutate)):
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    verify(mutate(serial_items(marker)))

    def test_schema_and_geometry_forgery_rejected_offline(self):
        ns, _ = live_shell_pure()
        outputs, workspaces = native_arrays()
        real = ns["layout_proof"](outputs, workspaces, 38, 68)
        for key, value in (
            ("source","mock.get_outputs"),("output","VGA-1"),
            ("workspace","42"),("scale",True),("top_exclusive",37),
            ("dock_exclusive",0),
            ("output_rect",{"x":0,"y":0,"width":800,"height":768}),
            ("workspace_rect",{"x":0,"y":0,"width":1024,"height":768}),
            ("workspace_rect",{"x":0,"y":38,"width":1024,"height":720}),
        ):
            with self.subTest(key=key,value=value):
                altered = copy.deepcopy(real)
                altered[key] = value
                with self.assertRaises(ValueError):
                    offline_layout()(serial_items(canonical_marker(altered)))


class HostNativeLayoutOracle(unittest.TestCase):
    @staticmethod
    def fixture():
        ns, _ = live_shell_pure()
        outputs, workspaces = native_arrays()
        return canonical_marker(ns["layout_proof"](outputs, workspaces, 38, 68))

    def test_host_decodes_exact_same_sway_layout_geometry(self):
        marker = self.fixture()
        position, value = smoke.ipc_layout_from_output(
            "\n".join(serial_items(marker)) + "\n",
        )
        self.assertEqual(position, 2)
        self.assertEqual(value["workspace_rect"]["y"], 38)
        self.assertEqual(value["workspace_rect"]["height"], 662)

    def test_host_rejects_forged_and_duplicated_layout_json(self):
        marker = self.fixture()
        for data in (
            serial_items(marker)[:2] + serial_items(marker)[3:],
            serial_items(marker)[:3] + [marker] + serial_items(marker)[3:],
            serial_items(marker)[:2] + ["P3:IPC_LAYOUT:{not-json}"] +
                serial_items(marker)[3:],
            serial_items(marker)[:2] + [
                "P3:IPC_LAYOUT:" + json.dumps(json.loads(marker.split(":",2)[2]))
            ] + serial_items(marker)[3:],
        ):
            with self.subTest(serial=data):
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.ipc_layout_from_output("\n".join(data) + "\n")

    def test_positive_ready_orders_real_tree_layout_and_layer_mapping(self):
        source, release = "a" * 40, "7.3.0-rc7-phase2"
        marker = self.fixture()
        prefix = [
            f"[    0.000000] Linux version {release} (unit)",
            f"P3:SOURCE:{source}", f"P3:KERNEL:{release}",
            "P3:USERSPACE:debian", "P3:ROOT_OK", "P3:DRM_OK",
            "P3:PERSISTED:EMPTY", "P3:PREFERENCE_DISK:compact_dock=absent",
            "P3:DESKTOP_USER:1000",
            "P3:RENDERER_ENV:pixman", "P3:BACKENDS_ENV:libinput,drm",
            "P3:DRM_FD:card0", "P3:DRM_MODESET_REQUESTED:Virtual-1",
            "P3:PIXMAN_RENDERER_CREATED",
        ]
        items = prefix + serial_items(marker) + [
            "P3:PREFERENCE_STATE:compact_dock=false",
        ]
        smoke.ready("\n".join(items) + "\n", release, source, None)
        for swap in ("before_tree", "after_layer", "missing"):
            with self.subTest(where=swap):
                altered = list(items)
                original = altered.index(marker)
                altered.pop(original)
                if swap == "before_tree":
                    altered.insert(altered.index("P3:SWAY_READY") + 1, marker)
                elif swap == "after_layer":
                    altered.insert(altered.index("P3:LAYER_SHELL_READY") + 1, marker)
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.ready("\n".join(altered) + "\n", release, source, None)


if __name__ == "__main__":
    unittest.main()
