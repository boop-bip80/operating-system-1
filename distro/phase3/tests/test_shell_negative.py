# SPDX-License-Identifier: GPL-2.0-only
"""Host-only regressions for genuine Sway-ready/test-gated GTK failure.

These fabricated serial lines exercise the production validation function,
not QEMU acceptance evidence. The integration smoke must boot a real guest
with separate image backing and prove source, DRM, Sway, OnFailure shutdown.
"""
import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke-test.py"
SPEC = importlib.util.spec_from_file_location("phase3_smoke_shell_negative_unit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)

SOURCE = "a" * 40
RELEASE = "7.3.0-rc7-phase2"


def markers(*, child_failure: bool = False) -> list[str]:
    result = [
        f"[    0.000000] Linux version {RELEASE} (test builder)",
        f"P2:SOURCE:{SOURCE}", f"P2:KERNEL:{RELEASE}",
        "P2:USERSPACE:debian", "P2:ROOT_OK",
        f"P3:SOURCE:{SOURCE}", f"P3:KERNEL:{RELEASE}",
        "P3:USERSPACE:debian", "P3:ROOT_OK", "P3:DRM_OK",
        "P3:PERSISTED:EMPTY", "P3:PREFERENCE_DISK:compact_dock=absent",
        "P3:DESKTOP_USER:1000",
        "P3:RENDERER_ENV:pixman", "P3:BACKENDS_ENV:libinput,drm",
        "P3:DRM_FD:card0", "P3:DRM_MODESET_REQUESTED:Virtual-1",
        "P3:PIXMAN_RENDERER_CREATED", "P3:SWAY_READY",
        "P3:INJECTED:SHELL_FAIL",
    ]
    if child_failure:
        result.append("P3:FAIL:SHELL_STARTUP")
    result += [
        "P3:FAIL:SHELL_EXIT", "P2:FAIL:DESKTOP_SERVICE",
        "[   90.000000] reboot: Power down",
    ]
    return result


def fixture(*, child_failure: bool = False) -> smoke.BootResult:
    return smoke.BootResult(
        name="shell-negative",
        output="\n".join(markers(child_failure=child_failure)) + "\n",
        returncode=0,
    )


class ShellNegativeAcceptance(unittest.TestCase):
    def verify(self, result: smoke.BootResult) -> None:
        smoke.validate_shell_negative(result, RELEASE, SOURCE)

    def test_real_sway_then_injected_gtk_child_failure_is_accepted(self):
        for child_failure in (False, True):
            with self.subTest(child_failure=child_failure):
                self.verify(fixture(child_failure=child_failure))

    def test_all_prerequisite_and_failure_markers_are_required_once(self):
        for marker in markers()[1:-1]:
            with self.subTest(marker=marker):
                result = fixture()
                result.output = result.output.replace(marker + "\n", "", 1)
                with self.assertRaises(smoke.Phase3Failure):
                    self.verify(result)
                result = fixture()
                result.output += marker + "\n"
                with self.assertRaises(smoke.Phase3Failure):
                    self.verify(result)

    def test_injection_before_sway_or_failure_out_of_order_is_rejected(self):
        for left, right in (
            ("P3:SWAY_READY", "P3:INJECTED:SHELL_FAIL"),
            ("P3:INJECTED:SHELL_FAIL", "P3:FAIL:SHELL_EXIT"),
        ):
            with self.subTest(swap=(left, right)):
                result = fixture()
                text = result.output
                result.output = text.replace(left, "__TEMP__").replace(right, left).replace("__TEMP__", right)
                with self.assertRaises(smoke.Phase3Failure):
                    self.verify(result)

    def test_async_onfailure_marker_may_precede_relayed_session_failure(self):
        # P2's OnFailure unit writes ttyS0 directly; the P3 child/supervisor
        # session log is forwarded asynchronously. Cross-stream arrival order
        # is not a causal ordering guarantee.
        result = fixture()
        result.output = result.output.replace(
            "P3:FAIL:SHELL_EXIT\nP2:FAIL:DESKTOP_SERVICE\n",
            "P2:FAIL:DESKTOP_SERVICE\nP3:FAIL:SHELL_EXIT\n",
        )
        self.verify(result)

    def test_gtk_child_failure_is_optional_once_and_only_between_parent_gates(self):
        self.verify(fixture(child_failure=True))
        for where in ("before", "after", "duplicate"):
            with self.subTest(where=where):
                result = fixture()
                if where == "before":
                    result.output = result.output.replace(
                        "P3:INJECTED:SHELL_FAIL\n",
                        "P3:FAIL:SHELL_STARTUP\nP3:INJECTED:SHELL_FAIL\n",
                    )
                elif where == "after":
                    result.output = result.output.replace(
                        "P2:FAIL:DESKTOP_SERVICE\n",
                        "P2:FAIL:DESKTOP_SERVICE\nP3:FAIL:SHELL_STARTUP\n",
                    )
                else:
                    result = fixture(child_failure=True)
                    result.output = result.output.replace(
                        "P3:FAIL:SHELL_STARTUP\n",
                        "P3:FAIL:SHELL_STARTUP\nP3:FAIL:SHELL_STARTUP\n",
                    )
                with self.assertRaises(smoke.Phase3Failure):
                    self.verify(result)

    def test_no_shell_readiness_ui_hid_or_other_failure_can_leak(self):
        forbidden = (
            "P3:LAYER_SHELL_READY", "P3:SHELL_READY",
            "P3:LAUNCHER_OPEN", "P3:COMPACT_TARGET:60:444:92:26",
            "P3:TERMINAL_TARGET:140:153:248:40",
            "P3:PREFERENCE_STATE:compact_dock=false",
            "P3:PREFERENCE_SAVED:compact_dock=true",
            "P3:APP_LAUNCHED:foot", "P3:WINDOW_FOCUSED:42",
            "P3:WINDOW_CLOSED:42", "P3:IPC_TREE:{\"seq\":1}",
            "P3:PROCESS_EXITED:42:20371",
            "P3:KEYBOARD_OK:" + "f" * 32,
            "P2:FAIL:NO_DRM", "P3:FAIL:SWAY_READY",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                result = fixture()
                result.output = result.output.replace(
                    "P2:FAIL:DESKTOP_SERVICE\n",
                    marker + "\nP2:FAIL:DESKTOP_SERVICE\n",
                )
                with self.assertRaises(smoke.Phase3Failure):
                    self.verify(result)

    def test_host_launch_failure_and_nonclean_shutdown_cannot_qualify(self):
        for attribute, value in (
            ("returncode", 1), ("timed_out", True), ("output_limit", True),
        ):
            with self.subTest(attribute=attribute):
                result = fixture()
                setattr(result, attribute, value)
                with self.assertRaises(smoke.Phase3Failure):
                    self.verify(result)
        for changed in (
            lambda text: text.replace("[   90.000000] reboot: Power down\n", ""),
            lambda text: text.replace("reboot: Power down", "Kernel panic - not syncing"),
            lambda text: text + "[   99.000000] reboot: Power down\n",
            lambda text: text.replace(f"Linux version {RELEASE}", "Linux version 6.1.0"),
        ):
            result = fixture()
            result.output = changed(result.output)
            with self.assertRaises(smoke.Phase3Failure):
                self.verify(result)

    def test_nogpu_negative_cannot_claim_native_compositor_proofs(self):
        baseline = "\n".join([
            f"[    0.000000] Linux version {RELEASE} (test builder)",
            f"P2:SOURCE:{SOURCE}", f"P2:KERNEL:{RELEASE}",
            "P2:USERSPACE:debian", "P2:ROOT_OK", "P2:FAIL:NO_DRM",
            "[    8.000000] reboot: Power down", "",
        ])
        smoke.validate_negative(
            smoke.BootResult(name="negative", output=baseline, returncode=0),
            RELEASE, SOURCE,
        )
        for marker in (
            "P3:RENDERER_ENV:pixman", "P3:BACKENDS_ENV:libinput,drm",
            "P3:DRM_FD:card0", "P3:DRM_MODESET_REQUESTED:Virtual-1",
            "P3:PIXMAN_RENDERER_CREATED",
            "P3:PROCESS_EXITED:42:20371",
        ):
            with self.subTest(marker=marker):
                result = smoke.BootResult(
                    name="negative", output=baseline + marker + "\n",
                    returncode=0,
                )
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_negative(result, RELEASE, SOURCE)

    def test_negative_never_sends_qmp_or_contains_positive_result_fields(self):
        changes = (
            ("previous_nonce", "e" * 32),
            ("nonce", "f" * 32),
            ("window_id", "42"),
            ("second_window_id", "372"),
            ("second_opened", True),
            ("minimized", True),
            ("restored", True),
            ("close_via_dock", True),
            ("pointer_clicks", 1),
            ("menu_targets", {"Minimize": [519, 624]}),
            ("compact_target", [60, 444, 92, 26]),
            ("terminal_target", [140, 153, 248, 40]),
            ("preference_toggled", True),
            ("keyboard_sent", True),
            ("exit_sent", True),
            ("powerdown_sent", True),
            ("screenshots", {"ready": {"path": "fake.ppm"}}),
        )
        for attribute, value in changes:
            with self.subTest(attribute=attribute):
                result = fixture()
                setattr(result, attribute, value)
                with self.assertRaises(smoke.Phase3Failure):
                    self.verify(result)


if __name__ == "__main__":
    unittest.main()
