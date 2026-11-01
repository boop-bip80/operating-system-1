# SPDX-License-Identifier: GPL-2.0-only
"""Host-only Phase 3 serial/oracle regressions, NOT VM acceptance evidence.

Synthetic logs exercise the production acceptance validator's real-Sway IPC
requirements for two foot clients, Dock focus/scratchpad/close, source identity,
GPU backend proofs, persistent preference, and clean negative controls.
"""
import importlib.util
import json
import hashlib
from pathlib import Path
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke-test.py"
SPEC = importlib.util.spec_from_file_location("phase3_smoke_unit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)

SOURCE = "a" * 40
RELEASE = "7.3.0-rc7-phase2"
NONCE = "0123456789abcdef0123456789abcdef"
PREVIOUS = "f" * 32
A, B = "371", "372"

LAYOUT = {
    "source": "sway.get_outputs+get_workspaces",
    "output": "Virtual-1", "workspace": "1",
    "output_rect": {"x": 0, "y": 0, "width": 1024, "height": 768},
    "workspace_rect": {"x": 0, "y": 38, "width": 1024, "height": 662},
    "mode": {"width": 1024, "height": 768}, "scale": 1.0,
    "top_exclusive": 38, "dock_exclusive": 68,
}


def layout_marker():
    return "P3:IPC_LAYOUT:" + json.dumps(LAYOUT, sort_keys=True,
                                         separators=(",", ":"), ensure_ascii=True)


ELIGIBLE = sorted(["foot.desktop", "foot-server.desktop", "footclient.desktop",
                   "org.example.Files.desktop"])
DISPLAYED = sorted(["foot-server.desktop", "footclient.desktop",
                    "org.example.Files.desktop"])


def index_marker():
    ids = {"eligible_ids": ELIGIBLE, "displayed_ids": DISPLAYED}
    digest = hashlib.sha256(json.dumps(
        ids, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    value = {
        "source": "Gio.AppInfo.get_all",
        "eligible_count": len(ELIGIBLE), "eligible_ids": ELIGIBLE,
        "displayed_count": len(DISPLAYED), "displayed_ids": DISPLAYED,
        "sha256": digest,
    }
    return "P3:APP_INDEX:" + json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def node(con_id, *, focused=False, scratch="none", visible=True, pid=None):
    return {
        "id": int(con_id), "app_id": "foot",
        "pid": pid if pid is not None else 20000 + int(con_id),
        "focused": focused, "scratchpad_state": scratch,
        "visible": visible,
        "minimized": scratch != "none" and not visible,
    }


def snapshot(sequence, windows):
    return "P3:IPC_TREE:" + json.dumps({
        "seq": sequence, "source": "sway.get_tree", "windows": windows,
    }, separators=(",", ":"))


def output(previous=None):
    state = "true" if previous else "false"
    disk = "compact_dock=true:uid=1000" if previous else "compact_dock=absent"
    lines = [
        f"[    0.000000] Linux version {RELEASE} (tester)",
        f"P3:SOURCE:{SOURCE}", f"P3:KERNEL:{RELEASE}",
        "P3:USERSPACE:debian", "P3:ROOT_OK", "P3:DRM_OK",
        f"P3:PERSISTED:{previous or 'EMPTY'}", f"P3:PREFERENCE_DISK:{disk}",
        "P3:DESKTOP_USER:1000",
        "P3:RENDERER_ENV:pixman", "P3:BACKENDS_ENV:libinput,drm",
        "P3:DRM_FD:card0", "P3:DRM_MODESET_REQUESTED:Virtual-1",
        "P3:PIXMAN_RENDERER_CREATED",
        "P3:SWAY_READY",
        snapshot(1, []), layout_marker(),
        "P3:LAYER_SHELL_READY", "P3:SHELL_READY",
        f"P3:PREFERENCE_STATE:compact_dock={state}",
        "P3:LAUNCHER_OPEN", "P3:COMPACT_TARGET:60:444:92:26",
        "P3:TERMINAL_TARGET:140:153:248:40", index_marker(),
    ]
    if previous is None:
        lines.append("P3:PREFERENCE_SAVED:compact_dock=true")
    lines += [
        snapshot(2, [node(A, focused=True)]),
        f"P3:WINDOW_DISCOVERED:{A}:foot", f"P3:WINDOW_ACTIVATED:{A}",
        "P3:APP_LAUNCHED:foot", f"P3:WINDOW_FOCUSED:{A}",
        "P3:PREFERENCE_VERIFIED:compact_dock=true:uid=1000",
        f"P3:KEYBOARD_OK:{NONCE}",
        snapshot(3, [node(A), node(B, focused=True)]),
        f"P3:WINDOW_DISCOVERED:{B}:foot", f"P3:WINDOW_ACTIVATED:{B}",
        snapshot(4, [node(A, focused=True), node(B)]),
        f"P3:WINDOW_ACTIVATED:{A}", f"P3:WINDOW_FOCUSED:{A}",
        snapshot(5, [node(A, scratch="fresh", visible=False), node(B, focused=True)]),
        f"P3:WINDOW_MINIMIZED:{A}", f"P3:WINDOW_ACTIVATED:{B}",
        snapshot(6, [node(A, scratch="changed", focused=True), node(B)]),
        f"P3:WINDOW_RESTORED:{A}", f"P3:WINDOW_ACTIVATED:{A}",
        snapshot(7, [node(B, focused=True)]),
        f"P3:WINDOW_CLOSED:{A}", f"P3:WINDOW_ACTIVATED:{B}",
        f"P3:PROCESS_EXITED:{A}:20371",
        snapshot(8, []),
        f"P3:WINDOW_CLOSED:{B}",
        "[   90.000000] reboot: Power down",
        "",
    ]
    return "\n".join(lines)


def positive(previous=None):
    return smoke.BootResult(
        name="round2" if previous else "round1", output=output(previous),
        returncode=0, previous_nonce=previous, nonce=NONCE,
        window_id=A, second_window_id=B,
        second_opened=True, minimized=True, restored=True, close_via_dock=True,
        pointer_clicks=8 if previous else 9,
        menu_targets={"Minimize": [512, 610], "Close": [512, 680]},
        compact_target=[60, 444, 92, 26],
        terminal_target=[140, 153, 248, 40],
        preference_toggled=not bool(previous),
        keyboard_sent=True, exit_sent=True, powerdown_sent=True,
        screenshots={
            stage: {
                "sha256": f"{i + 1:064x}", "png_sha256": f"{i + 101:064x}",
                "width": 1024, "height": 768,
            }
            for i, stage in enumerate(smoke.SCREEN_STAGES)
        },
    )


def negative():
    return smoke.BootResult(
        name="negative", output="\n".join([
            f"[    0.000000] Linux version {RELEASE} (tester)",
            f"P2:SOURCE:{SOURCE}", f"P2:KERNEL:{RELEASE}",
            "P2:USERSPACE:debian", "P2:ROOT_OK", "P2:FAIL:NO_DRM",
            "[   8.000000] reboot: Power down", "",
        ]), returncode=0,
    )


class TwoFootPositiveOracle(unittest.TestCase):
    def assertRejects(self, result):
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_positive(result, RELEASE, SOURCE)

    def test_both_real_foot_and_persisted_rounds_are_accepted(self):
        self.assertEqual(len(smoke.SCREEN_STAGES), 13)
        smoke.validate_positive(positive(), RELEASE, SOURCE)
        smoke.validate_positive(positive(PREVIOUS), RELEASE, SOURCE)

    def test_all_native_renderer_and_shell_markers_required_once(self):
        for marker in smoke.BOOT_MARKERS:
            with self.subTest(marker=marker):
                result = positive()
                result.output = result.output.replace(marker + "\n", "", 1)
                self.assertRejects(result)
                result = positive()
                result.output += marker + "\n"
                self.assertRejects(result)

    def test_source_uid_kernel_and_storage_identity_must_match(self):
        for marker in (
            f"P3:SOURCE:{SOURCE}", f"P3:KERNEL:{RELEASE}",
            "P3:DESKTOP_USER:1000", "P3:PREFERENCE_DISK:compact_dock=absent",
            "P3:PREFERENCE_SAVED:compact_dock=true",
            "P3:PREFERENCE_VERIFIED:compact_dock=true:uid=1000",
        ):
            with self.subTest(marker=marker):
                result = positive()
                result.output = result.output.replace(marker + "\n", "")
                self.assertRejects(result)

    def test_mapped_gtk_launcher_centers_are_unique_ordered_and_bounded(self):
        for marker in (
            "P3:LAUNCHER_OPEN",
            "P3:COMPACT_TARGET:60:444:92:26",
            "P3:TERMINAL_TARGET:140:153:248:40",
        ):
            with self.subTest(marker=marker):
                result = positive()
                result.output = result.output.replace(marker + "\n", "", 1)
                self.assertRejects(result)
                result = positive()
                result.output += marker + "\n"
                self.assertRejects(result)
        for attribute, value in (
            ("compact_target", [60, 444, 92, 0]),
            ("compact_target", [400, 444, 92, 26]),
            ("terminal_target", [140, 444, 248, 40]),
            ("terminal_target", [140, 153, 1, 40]),
        ):
            with self.subTest(attribute=attribute, value=value):
                result = positive()
                setattr(result, attribute, value)
                self.assertRejects(result)
        result = positive()
        result.output = result.output.replace(
            "P3:TERMINAL_TARGET:140:153:248:40",
            "P3:TERMINAL_TARGET:140:444:248:40",
        )
        self.assertRejects(result)

    def test_real_gio_eligible_and_packed_launcher_index_is_source_bound(self):
        marker = index_marker()
        for changed in (
            "",
            marker.replace('"source":"Gio.AppInfo.get_all"',
                           '"source":"host.synthetic"'),
            marker.replace('"eligible_count":4', '"eligible_count":5'),
            marker.replace('"displayed_count":3', '"displayed_count":2'),
            marker.replace('"sha256":"', '"sha256":"bad'),
        ):
            with self.subTest(changed=changed[:120]):
                result = positive()
                result.output = result.output.replace(marker + "\n",
                                                      changed + "\n", 1)
                self.assertRejects(result)
        result = positive()
        result.output += marker + "\n"
        self.assertRejects(result)
        result = positive()
        result.output = result.output.replace(
            "P3:TERMINAL_TARGET:140:153:248:40\n" + marker,
            marker + "\nP3:TERMINAL_TARGET:140:153:248:40",
        )
        self.assertRejects(result)

    def test_second_boot_preference_or_nonce_regression_rejected(self):
        result = positive(PREVIOUS)
        result.output = result.output.replace(
            "P3:PREFERENCE_STATE:compact_dock=true",
            "P3:PREFERENCE_STATE:compact_dock=false",
        )
        self.assertRejects(result)
        result = positive(PREVIOUS)
        result.output += "P3:PREFERENCE_SAVED:compact_dock=true\n"
        self.assertRejects(result)
        result = positive(PREVIOUS)
        result.nonce = PREVIOUS
        self.assertRejects(result)

    def test_distinct_sway_foot_ids_and_real_ipc_pids_required(self):
        for key, value in (
            ("window_id", B), ("window_id", None),
            ("second_window_id", A), ("second_window_id", None),
        ):
            result = positive()
            setattr(result, key, value)
            self.assertRejects(result)
        result = positive()
        result.output = result.output.replace(
            f"P3:WINDOW_DISCOVERED:{B}:foot\n", "",
        )
        self.assertRejects(result)
        result = positive()
        result.output = result.output.replace(
            '"pid":20372', '"pid":null',
        )
        self.assertRejects(result)

    def test_sway_focus_scratchpad_and_both_closures_derive_from_tree(self):
        tamper = (
            (f"P3:WINDOW_ACTIVATED:{B}\n", "", 1),
            (f"P3:WINDOW_FOCUSED:{A}\n", "", 1),
            (f"P3:WINDOW_MINIMIZED:{A}\n", "", 1),
            (f"P3:WINDOW_RESTORED:{A}\n", "", 1),
            (f"P3:WINDOW_CLOSED:{A}\n", "", 1),
            (f"P3:WINDOW_CLOSED:{B}\n", "", 1),
            (f"P3:PROCESS_EXITED:{A}:20371\n", "", 1),
            ('"minimized":true', '"minimized":false', 1),
            ('"focused":true', '"focused":"yes"', 1),
        )
        for search, replacement, count in tamper:
            with self.subTest(tamper=search):
                result = positive()
                result.output = result.output.replace(search, replacement, count)
                self.assertRejects(result)

    def test_native_sway_layer_shell_layout_is_required_and_unforged(self):
        marker = layout_marker()
        for changed in (
            "", marker.replace('"y":38', '"y":0'),
            marker.replace('"height":662', '"height":768'),
            marker.replace('"source":"sway.get_outputs+get_workspaces"',
                           '"source":"synthetic.geometry"'),
        ):
            with self.subTest(change=changed):
                result = positive()
                result.output = result.output.replace(marker + "\n",
                                                      changed + "\n", 1)
                self.assertRejects(result)
        result = positive()
        result.output += marker + "\n"
        self.assertRejects(result)
        result = positive()
        result.output = result.output.replace(
            snapshot(1, []) + "\n" + marker,
            marker + "\n" + snapshot(1, []),
        )
        self.assertRejects(result)

    def test_initial_focus_must_have_focused_sway_tree_before_keyboard(self):
        result = positive()
        result.output = result.output.replace(
            snapshot(2, [node(A, focused=True)]),
            snapshot(2, [node(A)]), 1,
        )
        self.assertRejects(result)

        result = positive()
        first_focus = f"P3:WINDOW_FOCUSED:{A}\n"
        result.output = result.output.replace(first_focus, "", 1)
        result.output = result.output.replace(
            f"P3:KEYBOARD_OK:{NONCE}\n",
            f"P3:KEYBOARD_OK:{NONCE}\n" + first_focus, 1,
        )
        self.assertRejects(result)

    def test_process_exit_must_match_preclose_sway_pid_and_one_window(self):
        for altered in (
            f"P3:PROCESS_EXITED:{A}:99999",
            f"P3:PROCESS_EXITED:{B}:20371",
            "",
        ):
            with self.subTest(altered=altered):
                result = positive()
                result.output = result.output.replace(
                    f"P3:PROCESS_EXITED:{A}:20371", altered, 1,
                )
                self.assertRejects(result)
        result = positive()
        result.output = result.output.replace(
            f"P3:PROCESS_EXITED:{A}:20371\n",
            f"P3:PROCESS_EXITED:{A}:20371\nP3:PROCESS_EXITED:{B}:20372\n", 1,
        )
        self.assertRejects(result)

    def test_reordered_missing_and_forged_ipc_snapshots_rejected(self):
        for search, replacement in (
            ('"seq":3', '"seq":2'),
            ('"source":"sway.get_tree"', '"source":"mock.tree"'),
            (snapshot(3, [node(A), node(B, focused=True)]) + "\n", ""),
        ):
            with self.subTest(rewrite=search):
                result = positive()
                result.output = result.output.replace(search, replacement, 1)
                self.assertRejects(result)

    def test_twelve_actual_screenshot_stages_and_qmp_flags_required(self):
        for stage in smoke.SCREEN_STAGES:
            with self.subTest(stage=stage):
                result = positive()
                del result.screenshots[stage]
                self.assertRejects(result)
        for key, value in (
            ("pointer_clicks", 0), ("keyboard_sent", False),
            ("preference_toggled", False), ("powerdown_sent", False),
            ("second_opened", False), ("minimized", False),
            ("restored", False), ("close_via_dock", False),
            ("exit_sent", False),
            ("menu_targets", {"Minimize": [512, 610]}),
            ("compact_target", None), ("terminal_target", None),
        ):
            with self.subTest(field=key):
                result = positive()
                setattr(result, key, value)
                self.assertRejects(result)

    def test_challenge_keyboard_failure_and_false_shutdown_rejected(self):
        result = positive()
        result.output = result.output.replace(
            f"P3:KEYBOARD_OK:{NONCE}", f"P3:KEYBOARD_OK:{PREVIOUS}",
        )
        self.assertRejects(result)
        for flag, value in (("returncode", 1), ("timed_out", True), ("output_limit", True)):
            with self.subTest(flag=flag):
                result = positive()
                setattr(result, flag, value)
                self.assertRejects(result)
        for line in ("Kernel panic - not syncing", "Oops: 0000",
                     "P2:FAIL:NO_DRM", "P3:FAIL:SHELL_READY"):
            result = positive()
            result.output += line + "\n"
            self.assertRejects(result)


class NoGPUNegativeOracle(unittest.TestCase):
    def test_hardware_control_still_requires_real_no_drm_shutdown(self):
        smoke.validate_negative(negative(), RELEASE, SOURCE)

    def test_nogpu_rejects_missing_root_or_actual_kernel_shutdown(self):
        result = negative()
        result.output = result.output.replace("P2:ROOT_OK\n", "")
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_negative(result, RELEASE, SOURCE)
        result = negative()
        result.output = result.output.replace("[   8.000000] reboot: Power down\n", "")
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_negative(result, RELEASE, SOURCE)

    def test_nogpu_does_not_accept_renderer_or_ipc_fallback_proofs(self):
        for marker in (
            "P3:DRM_OK", "P3:RENDERER_ENV:pixman", "P3:BACKENDS_ENV:libinput,drm",
            "P3:DRM_FD:card0", "P3:DRM_MODESET_REQUESTED:Virtual-1",
            "P3:SWAY_READY", "P3:IPC_TREE:{}",
            "P3:LAYER_SHELL_READY", "P3:SHELL_READY",
        ):
            with self.subTest(marker=marker):
                result = negative()
                result.output += marker + "\n"
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_negative(result, RELEASE, SOURCE)


if __name__ == "__main__":
    unittest.main()
