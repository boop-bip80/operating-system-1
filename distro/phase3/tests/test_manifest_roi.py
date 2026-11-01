# SPDX-License-Identifier: GPL-2.0-only
"""Offline cross-file Phase 3 QMP/PPM verifier contract regressions.

These inspect actual smoke/manifest AST and run the production pixel-difference
function on synthetic frames. They do not invent screenshot evidence, replay
an actual QEMU disk, or bypass SHA-bound offline manifest verification.
"""

from __future__ import annotations

import ast
from pathlib import Path
import types
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SMOKE = SCRIPTS / "smoke-test.py"
MANIFEST = SCRIPTS / "manifest.py"
STAGES = (
    "ready", "launcher", "preferences", "foot", "saved", "second", "focused",
    "menu_minimize", "hidden", "restored", "menu_close", "closed", "desktop",
)
CASE_NAMES = ("round1", "round2", "negative", "shell-negative")
EXPECTED_ROIS = {
    ("ready", "foot", "SCREEN_BOX", .07, 18),
    ("ready", "foot", "SCREEN_BOX", .005, 25),  # independent real terminal glyph/border detail
    ("ready", "launcher", "LAUNCHER_BOX", .015, 25),
    ("launcher", "preferences", "DOCK_ICON_BOX", .025, 25),
    ("ready", "preferences", "LAUNCHER_BOX", .015, 25),
    ("saved", "second", "FOOT_CONTENT_BOX", .001, 25),
    ("second", "focused", "FOOT_CONTENT_BOX", .001, 25),
    ("focused", "menu_minimize", "MENU_POPUP_BOX", .003, 25),
    ("focused", "hidden", "FOOT_CONTENT_BOX", .001, 25),
    ("hidden", "restored", "FOOT_CONTENT_BOX", .001, 25),
    ("restored", "menu_close", "MENU_POPUP_BOX", .003, 25),
    ("restored", "closed", "FOOT_CONTENT_BOX", .001, 25),
    ("closed", "desktop", "SCREEN_BOX", .07, 18),
}
SMOKE_FRAME_ALIASES = {
    "first": "ready", "launcher": "launcher", "preferences": "preferences",
    "opened": "foot", "saved_frame": "saved",
}


def parsed():
    return (
        ast.parse(SMOKE.read_text(encoding="utf-8"), filename=str(SMOKE)),
        ast.parse(MANIFEST.read_text(encoding="utf-8"), filename=str(MANIFEST)),
    )


def function(module, name):
    found = [node for node in module.body if
             isinstance(node, ast.FunctionDef) and node.name == name]
    if len(found) != 1:
        raise AssertionError(f"missing {name}() production function")
    return found[0]


def frame_stage(expr):
    if isinstance(expr, ast.Name):
        value = SMOKE_FRAME_ALIASES.get(expr.id, expr.id)
    elif (isinstance(expr, ast.Subscript) and
          isinstance(expr.value, ast.Name) and expr.value.id == "frames" and
          isinstance(expr.slice, ast.Constant)):
        value = expr.slice.value
    else:
        raise AssertionError(f"unrecognized production framebuffer: {ast.dump(expr)}")
    if value not in STAGES:
        raise AssertionError(f"unrecognized QMP screenshot stage: {value}")
    return value


def roi_requirements(method, expected_op):
    checks = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        call = node.left
        if (not isinstance(call, ast.Call) or
                not (isinstance(call.func, ast.Name) and
                     call.func.id == "changed_fraction") and
                not (isinstance(call.func, ast.Attribute) and
                     call.func.attr == "changed_fraction")):
            continue
        if not isinstance(node.ops[0], expected_op):
            raise AssertionError("production screenshot threshold polarity changed")
        if len(call.args) != 2 or len(node.comparators) != 1:
            raise AssertionError("unbound pixel comparison arguments")
        if any(isinstance(arg, ast.Name) and arg.id == "first_ready_frame"
               for arg in call.args):
            # Separate cross-boot original-ready Dock icon oracle.
            continue
        first, second = (frame_stage(arg) for arg in call.args)
        box = "SCREEN_BOX"
        pixel_delta = 25
        for kw in call.keywords:
            if kw.arg == "box":
                if not isinstance(kw.value, (ast.Name, ast.Attribute)):
                    raise AssertionError("unbounded screenshot ROI")
                box = kw.value.id if isinstance(kw.value, ast.Name) else kw.value.attr
            elif kw.arg == "pixel_delta":
                pixel_delta = ast.literal_eval(kw.value)
            else:
                raise AssertionError("unexpected screenshot metric option")
        minimum = ast.literal_eval(node.comparators[0])
        if type(minimum) is not float or type(pixel_delta) is not int:
            raise AssertionError("nonliteral screenshot or pixel threshold")
        checks.add((first, second, box, minimum, pixel_delta))
    return checks


def capture_roi_requirements(method):
    checks = set()
    for node in ast.walk(method):
        if (not isinstance(node, ast.Call) or
                not isinstance(node.func, ast.Name) or
                node.func.id != "capture" or len(node.args) < 3 or
                not isinstance(node.args[2], ast.Constant)):
            continue
        stage = node.args[2].value
        opts = {kw.arg: kw.value for kw in node.keywords}
        if "changed_from" not in opts or "min_changed" not in opts:
            continue
        before = frame_stage(opts["changed_from"])
        if stage not in STAGES:
            raise AssertionError("capture uses noncanonical QMP screenshot stage")
        box = opts.get("box")
        roi = "SCREEN_BOX" if box is None else (
            box.id if isinstance(box, ast.Name) else
            box.attr if isinstance(box, ast.Attribute) else None
        )
        if roi is None:
            raise AssertionError("nonliteral dynamic screenshot ROI")
        minimum = ast.literal_eval(opts["min_changed"])
        delta = ast.literal_eval(opts["pixel_delta"]) if "pixel_delta" in opts else 25
        if type(minimum) is not float or type(delta) is not int:
            raise AssertionError("unbounded QMP screenshot color difference")
        checks.add((before, stage, roi, minimum, delta))
        if "min_high_contrast_changed" in opts:
            high_contrast = ast.literal_eval(opts["min_high_contrast_changed"])
            if type(high_contrast) is not float or high_contrast <= 0:
                raise AssertionError("missing positive high-contrast detail floor")
            checks.add((before, stage, roi, high_contrast, 25))
    return checks


def names_assigned(method, name):
    values = [
        node.value for node in ast.walk(method) if
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets)
    ]
    if len(values) != 1:
        raise AssertionError(f"missing {name} scenario contract")
    return tuple(ast.literal_eval(values[0]))


def dotted_name(expr):
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return dotted_name(expr.value) + "." + expr.attr
    return ""


class ManifestReplayContract(unittest.TestCase):
    def test_all_thirteen_qmp_stages_and_four_scenarios_match(self):
        smoke, manifest = parsed()
        smoke_stages = [
            ast.literal_eval(node.value) for node in smoke.body if
            isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SCREEN_STAGES"
                for target in node.targets)
        ]
        self.assertEqual(smoke_stages, [STAGES])
        verify = function(manifest, "verify")
        self.assertEqual(names_assigned(verify, "required_stages"), STAGES)
        self.assertTrue(any(
            isinstance(node, ast.List) and
            tuple(ast.literal_eval(node)) == CASE_NAMES
            for node in ast.walk(verify)
            if all(isinstance(item, ast.Constant) for item in getattr(node, "elts", []))
        ))

    def test_production_and_offline_manifest_use_exact_same_roi_oracles(self):
        smoke, manifest = parsed()
        boot = function(smoke, "boot")
        online = roi_requirements(boot, ast.Lt) | capture_roi_requirements(boot)
        offline = roi_requirements(function(manifest, "verify"), ast.GtE)
        self.assertEqual(online, EXPECTED_ROIS)
        self.assertEqual(offline, EXPECTED_ROIS)
        self.assertEqual(online, offline)

    def test_manifest_replays_independent_guest_and_qmp_proofs(self):
        _, manifest = parsed()
        verify = function(manifest, "verify")
        calls = {
            dotted_name(node.func) for node in ast.walk(verify)
            if isinstance(node, ast.Call)
        }
        for needed in (
            "smoke.validate_positive", "smoke.validate_negative",
            "smoke.validate_shell_negative", "smoke.ipc_states",
            "smoke.verify_frame", "smoke.p2.parse_ppm",
            "smoke.p2.png_bytes", "smoke.transcript",
            "smoke.popup_menu_target", "smoke.gtk_target_from_output",
            "smoke.prove_compact_dock_visible",
            "check_native_layout", "check_native_app_index",
        ):
            with self.subTest(validator=needed):
                self.assertIn(needed, calls)

    def test_menu_click_coordinates_recomputed_from_two_hashed_ppms(self):
        _, manifest = parsed()
        verify = function(manifest, "verify")
        calibrations = []
        for node in ast.walk(verify):
            if not isinstance(node, ast.Call) or dotted_name(node.func) != "smoke.popup_menu_target":
                continue
            self.assertEqual(len(node.args), 3)
            calibrations.append((
                frame_stage(node.args[0]), frame_stage(node.args[1]),
                ast.literal_eval(node.args[2]),
            ))
        self.assertEqual(set(calibrations), {
            ("focused", "menu_minimize", "Minimize"),
            ("restored", "menu_close", "Close"),
        })
        self.assertEqual(len(calibrations), 2)
        comparisons = [
            node for node in ast.walk(verify) if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and dotted_name(node.left) == "result.menu_targets"
            and len(node.comparators) == 1
            and dotted_name(node.comparators[0]) == "calibrated_targets"
        ]
        self.assertEqual(len(comparisons), 1)
        replay = [
            node for node in ast.walk(verify) if
            isinstance(node, ast.Call) and dotted_name(node.func) == "smoke.transcript"
        ]
        self.assertEqual(len(replay), 1)
        for key in ("menu_targets", "compact_target", "terminal_target"):
            self.assertTrue(any(
                kw.arg == key and dotted_name(kw.value) == "result." + key
                for kw in replay[0].keywords
            ))

    def test_mapped_gtk_hitboxes_are_reparsed_before_qmp_replay(self):
        _, manifest = parsed()
        verify = function(manifest, "verify")
        calls = [
            node for node in ast.walk(verify) if isinstance(node, ast.Call)
            and dotted_name(node.func) == "smoke.gtk_target_from_output"
        ]
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0].args[1], ast.Name)
        self.assertEqual(calls[0].args[1].id, "kind")
        loops = [node for node in ast.walk(verify) if isinstance(node, ast.For)
                 and isinstance(node.target, ast.Tuple) and
                 [x.id for x in node.target.elts if isinstance(x, ast.Name)] ==
                 ["kind", "field"]]
        self.assertEqual(len(loops), 1)
        self.assertEqual(set(ast.literal_eval(loops[0].iter)), {
            ("COMPACT", "compact_target"), ("TERMINAL", "terminal_target"),
        })
        self.assertTrue(any(
            isinstance(node, ast.Compare) and
            dotted_name(node.left) == "result.menu_targets"
            for node in ast.walk(verify)
        ))

    def test_actual_pixel_metric_is_roi_scoped_and_requires_real_color_change(self):
        smoke, _ = parsed()
        fn = function(smoke, "changed_fraction")
        boxes = {
            node.targets[0].id: ast.literal_eval(node.value) for node in smoke.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("SCREEN_BOX", "FOOT_CONTENT_BOX",
                                       "MENU_POPUP_BOX", "DOCK_ICON_BOX", "LAUNCHER_BOX")
        }
        Frame = types.SimpleNamespace
        namespace = {
            "p2": types.SimpleNamespace(Frame=Frame),
            "Phase3Failure": RuntimeError, **boxes,
        }
        module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
        exec(compile(module, str(SMOKE), "exec"), namespace)
        changed = namespace["changed_fraction"]
        base = bytes((70, 70, 70)) * (1024 * 768)
        altered = bytearray(base)
        for x, y in ((200, 170), (202, 172), (204, 174)):
            offset = (y * 1024 + x) * 3
            altered[offset:offset + 3] = bytes((240, 40, 180))
        before = Frame(width=1024, height=768, pixels=base)
        after = Frame(width=1024, height=768, pixels=bytes(altered))
        self.assertGreater(changed(before, after, box=boxes["FOOT_CONTENT_BOX"]), 0)
        self.assertEqual(changed(before, after, box=boxes["MENU_POPUP_BOX"]), 0)


    def test_dark_real_foot_pixels_need_broad_change_and_independent_detail(self):
        smoke, _ = parsed()
        metric = function(smoke, "changed_fraction")
        namespace = {
            "p2": types.SimpleNamespace(Frame=types.SimpleNamespace),
            "Phase3Failure": RuntimeError,
            "SCREEN_BOX": (160, 75, 864, 680),
        }
        module = ast.fix_missing_locations(ast.Module(body=[metric], type_ignores=[]))
        exec(compile(module, str(SMOKE), "exec"), namespace)
        changed = namespace["changed_fraction"]
        Frame = types.SimpleNamespace
        width, height = 1024, 768
        original = bytes((21, 36, 55)) * (width * height)
        baseline = Frame(width=width, height=height, pixels=original)
        # A large dark foot canvas differs from the real dark wallpaper by
        # only max-channel 19. Broad GPU area should pass 18 but not 25.
        tinted = bytearray(original)
        fill = bytes((40, 55, 74)) * 500
        for y in range(145, 565):
            start = (y * width + 200) * 3
            tinted[start:start + len(fill)] = fill
        flat = Frame(width=width, height=height, pixels=bytes(tinted))
        self.assertGreaterEqual(changed(baseline, flat, pixel_delta=18), .07)
        self.assertEqual(changed(baseline, flat), 0.0)
        # A flat-tinted rectangle is not sufficient: actual foot text/borders
        # add independently visible >25 high-contrast detail.
        detailed = bytearray(tinted)
        for y in range(161, 520, 8):
            for x in range(224, 680, 8):
                start = (y * width + x) * 3
                detailed[start:start + 3] = bytes((100, 110, 140))
        terminal = Frame(width=width, height=height, pixels=bytes(detailed))
        self.assertGreaterEqual(changed(baseline, terminal, pixel_delta=18), .07)
        self.assertGreaterEqual(changed(baseline, terminal), .005)
        # No change and a clock-only scanline are never a foot window.
        self.assertEqual(changed(baseline, baseline, pixel_delta=18), 0)
        clock = bytearray(original)
        for y in range(12, 37):
            for x in range(920, 1010):
                start = (y * width + x) * 3
                clock[start:start + 3] = bytes((250, 250, 250))
        clock_only = Frame(width=width, height=height, pixels=bytes(clock))
        self.assertEqual(changed(baseline, clock_only, pixel_delta=18), 0)
        self.assertEqual(changed(baseline, clock_only), 0)

    def test_capture_gate_retries_both_dark_palette_and_high_contrast(self):
        smoke, manifest = parsed()
        gate = function(smoke, "capture")
        comparisons = [
            node for node in ast.walk(gate) if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Call)
            and dotted_name(node.left.func) == "changed_fraction"
        ]
        self.assertEqual(len(comparisons), 2)
        bound_names = set()
        for node in comparisons:
            self.assertEqual(len(node.ops), 1)
            self.assertIsInstance(node.ops[0], ast.Lt)
            self.assertEqual(len(node.comparators), 1)
            self.assertIsInstance(node.comparators[0], ast.Name)
            bound_names.add(node.comparators[0].id)
            self.assertEqual(len(node.left.args), 2)
            self.assertEqual([arg.id for arg in node.left.args
                              if isinstance(arg, ast.Name)],
                             ["changed_from", "frame"])
        self.assertEqual(bound_names, {"min_changed", "min_high_contrast_changed"})
        first_foot = [
            node for node in ast.walk(function(smoke, "boot"))
            if isinstance(node, ast.Call) and
            dotted_name(node.func) == "capture" and
            len(node.args) >= 3 and
            isinstance(node.args[2], ast.Constant) and node.args[2].value == "foot"
        ]
        self.assertEqual(len(first_foot), 1)
        options = {kw.arg: ast.literal_eval(kw.value) for kw in first_foot[0].keywords
                   if kw.arg in ("min_changed", "pixel_delta", "min_high_contrast_changed")}
        self.assertEqual(options, {
            "min_changed": .07, "pixel_delta": 18,
            "min_high_contrast_changed": .005,
        })
        online = roi_requirements(function(smoke, "boot"), ast.Lt) | capture_roi_requirements(
            function(smoke, "boot"))
        offline = roi_requirements(function(manifest, "verify"), ast.GtE)
        self.assertEqual(online, offline)
        self.assertIn(("ready", "foot", "SCREEN_BOX", .07, 18), offline)
        self.assertIn(("ready", "foot", "SCREEN_BOX", .005, 25), offline)
        self.assertIn(("closed", "desktop", "SCREEN_BOX", .07, 18), offline)

    def test_compact_icon_crossboot_proof_requires_smaller_bright_gpu_region(self):
        smoke, manifest = parsed()
        funcs = [function(smoke, "changed_fraction"),
                 function(smoke, "prove_compact_dock_visible")]
        box_values = {
            node.targets[0].id: ast.literal_eval(node.value) for node in smoke.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("SCREEN_BOX", "DOCK_ICON_BOX")
        }
        Frame = types.SimpleNamespace
        namespace = {
            "p2": types.SimpleNamespace(Frame=Frame), "Phase3Failure": RuntimeError,
            **box_values,
        }
        module = ast.fix_missing_locations(ast.Module(body=funcs, type_ignores=[]))
        exec(compile(module, str(SMOKE), "exec"), namespace)
        prove = namespace["prove_compact_dock_visible"]
        width, height = 1024, 768
        base = bytes((45, 60, 70)) * (width * height)
        def icon_frame(left, top, size):
            pixels = bytearray(base)
            for y in range(top, top + size):
                for x in range(left, left + size):
                    offset = (y * width + x) * 3
                    pixels[offset:offset + 3] = bytes((230, 230, 230))
            return Frame(width=width, height=height, pixels=bytes(pixels))
        normal = icon_frame(500, 708, 24)
        compact = icon_frame(506, 714, 12)
        fraction, regular_bright, compact_bright = prove(normal, compact)
        self.assertGreaterEqual(fraction, .025)
        self.assertGreaterEqual(regular_bright, compact_bright + 20)
        with self.assertRaises(RuntimeError):
            prove(compact, normal)
        with self.assertRaises(RuntimeError):
            prove(normal, normal)
        self.assertIn("smoke.prove_compact_dock_visible", {
            dotted_name(node.func) for node in ast.walk(function(manifest, "verify"))
            if isinstance(node, ast.Call)
        })


if __name__ == "__main__":
    unittest.main()
