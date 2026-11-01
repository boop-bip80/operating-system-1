# SPDX-License-Identifier: GPL-2.0-only
"""Host-only contract regressions for Phase 3; fixtures are NOT VM evidence."""
import importlib.util
from pathlib import Path
import json
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
WID = "371"


def output(previous=None):
    state = "true" if previous else "false"
    disk = "compact_dock=true:uid=1000" if previous else "compact_dock=absent"
    markers = [
        f"[    0.000000] Linux version {RELEASE} (tester)",
        f"P3:SOURCE:{SOURCE}", f"P3:KERNEL:{RELEASE}",
        *smoke.BOOT_MARKERS[:3],
        f"P3:PERSISTED:{previous or 'EMPTY'}", f"P3:PREFERENCE_DISK:{disk}",
        *smoke.BOOT_MARKERS[3:],
        f"P3:PREFERENCE_STATE:compact_dock={state}",
    ]
    if previous is None:
        markers.append("P3:PREFERENCE_SAVED:compact_dock=true")
    markers += [
        "P3:APP_LAUNCHED:foot", f"P3:WINDOW_FOCUSED:{WID}",
        "P3:PREFERENCE_VERIFIED:compact_dock=true:uid=1000",
        f"P3:KEYBOARD_OK:{NONCE}", f"P3:WINDOW_FOCUSED:{WID}",
        f"P3:WINDOW_CLOSED:{WID}", "[   25.000000] reboot: Power down", "",
    ]
    return "\n".join(markers)


def positive(previous=None):
    return smoke.BootResult(
        name="round2" if previous else "round1", output=output(previous),
        returncode=0, previous_nonce=previous, nonce=NONCE, window_id=WID,
        pointer_clicks=4 if previous else 5, preference_toggled=not bool(previous),
        keyboard_sent=True, exit_sent=True, powerdown_sent=True,
        screenshots={stage: {"sha256": str(i+1) * 64, "png_sha256": str(i+4) * 64,
                             "width": 1024, "height": 768}
                     for i, stage in enumerate(smoke.SCREEN_STAGES)},
    )


def negative():
    return smoke.BootResult(name="negative", output="\n".join([
        f"[    0.000000] Linux version {RELEASE} (tester)",
        f"P2:SOURCE:{SOURCE}", f"P2:KERNEL:{RELEASE}",
        "P2:USERSPACE:debian", "P2:ROOT_OK", "P2:FAIL:NO_DRM",
        "[   7.000000] reboot: Power down", "",
    ]), returncode=0)


class PositiveAcceptance(unittest.TestCase):
    def test_round1_real_contract(self):
        smoke.validate_positive(positive(), RELEASE, SOURCE)

    def test_round2_reloaded_pref_and_persisted_nonce(self):
        smoke.validate_positive(positive(PREVIOUS), RELEASE, SOURCE)

    def test_missing_or_duplicate_shell_marker_rejected(self):
        for marker in smoke.BOOT_MARKERS:
            with self.subTest(marker=marker):
                result = positive()
                result.output = result.output.replace(marker + "\n", "")
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_positive(result, RELEASE, SOURCE)
                result = positive()
                result.output += marker + "\n"
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_positive(result, RELEASE, SOURCE)

    def test_wrong_source_uid_and_kernel_rejected(self):
        for marker in (f"P3:SOURCE:{SOURCE}", f"P3:KERNEL:{RELEASE}",
                       "P3:DESKTOP_USER:1000"):
            with self.subTest(marker=marker):
                result = positive()
                result.output = result.output.replace(marker, "malformed-" + marker)
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_positive(result, RELEASE, SOURCE)

    def test_preference_marker_without_disk_owner_evidence_rejected(self):
        for marker in ("P3:PREFERENCE_DISK:compact_dock=absent",
                       "P3:PREFERENCE_SAVED:compact_dock=true",
                       "P3:PREFERENCE_VERIFIED:compact_dock=true:uid=1000"):
            with self.subTest(marker=marker):
                result = positive()
                result.output = result.output.replace(marker + "\n", "")
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_positive(result, RELEASE, SOURCE)

    def test_second_boot_stale_preference_rejected(self):
        result = positive(PREVIOUS)
        result.output = result.output.replace("P3:PREFERENCE_STATE:compact_dock=true",
                                              "P3:PREFERENCE_STATE:compact_dock=false")
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_positive(result, RELEASE, SOURCE)

    def test_no_emulated_click_or_keyboard_rejected(self):
        for key, value in (("pointer_clicks", 0), ("keyboard_sent", False),
                           ("preference_toggled", False), ("powerdown_sent", False)):
            with self.subTest(field=key):
                result = positive()
                setattr(result, key, value)
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_positive(result, RELEASE, SOURCE)

    def test_wrong_challenge_rejected(self):
        result = positive()
        result.output = result.output.replace(f"P3:KEYBOARD_OK:{NONCE}",
                                              f"P3:KEYBOARD_OK:{PREVIOUS}")
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_positive(result, RELEASE, SOURCE)

    def test_unobserved_or_wrong_sway_window_rejected(self):
        for field, value in (("window_id", "42"), ("window_id", None)):
            result = positive()
            setattr(result, field, value)
            with self.assertRaises(smoke.Phase3Failure):
                smoke.validate_positive(result, RELEASE, SOURCE)
        result = positive()
        result.output = result.output.replace(f"P3:WINDOW_CLOSED:{WID}", "P3:WINDOW_CLOSED:55")
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_positive(result, RELEASE, SOURCE)

    def test_bad_frame_and_guest_exit_rejected(self):
        for stage in smoke.SCREEN_STAGES:
            with self.subTest(stage=stage):
                result = positive()
                del result.screenshots[stage]
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_positive(result, RELEASE, SOURCE)
        for field, value in (("returncode", 1), ("timed_out", True), ("output_limit", True)):
            with self.subTest(field=field):
                result = positive()
                setattr(result, field, value)
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.validate_positive(result, RELEASE, SOURCE)

    def test_kernel_failure_and_false_shutdown_rejected(self):
        for suffix in ("Kernel panic - not syncing", "Oops: 0000", "BUG: bad page",
                       "P2:FAIL:NO_DRM", "P3:FAIL:SHELL"):
            result = positive()
            result.output += suffix + "\n"
            with self.assertRaises(smoke.Phase3Failure):
                smoke.validate_positive(result, RELEASE, SOURCE)


class NegativeAcceptance(unittest.TestCase):
    def test_real_phase2_drm_control_accepted(self):
        smoke.validate_negative(negative(), RELEASE, SOURCE)

    def test_does_not_accept_host_launch_failure(self):
        result = negative()
        result.output = result.output.replace("P2:ROOT_OK\n", "")
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_negative(result, RELEASE, SOURCE)

    def test_does_not_accept_unexpected_headless_fallback(self):
        result = negative()
        result.output += "P3:SHELL_READY\n"
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_negative(result, RELEASE, SOURCE)

    def test_negative_must_have_real_kernel_shutdown(self):
        result = negative()
        result.output = result.output.replace("[   7.000000] reboot: Power down\n", "")
        with self.assertRaises(smoke.Phase3Failure):
            smoke.validate_negative(result, RELEASE, SOURCE)


class QMPContract(unittest.TestCase):
    def transcript_fixture(self, toggled=True):
        """Synthetic host-only protocol fixture; never substitutes for QEMU evidence."""
        events = [{"direction": "receive", "message": {"QMP": {"version": {}}}}]
        next_id = 0
        def send(name, args=None):
            nonlocal next_id
            next_id += 1
            msg = {"execute": name, "id": next_id}
            if args is not None:
                msg["arguments"] = args
            events.append({"direction": "send", "message": msg})
            # An unsolicited QEMU event may occur between request and reply.
            events.append({"direction": "receive", "message": {"event": "TEST_EVENT"}})
            events.append({"direction": "receive", "message": {"return": {}, "id": next_id}})
            # And also after the command reply; both must be tolerated.
            events.append({"direction": "receive", "message": {"event": "TEST_EVENT"}})
        def screenshot(stage):
            send("screendump", {"filename": str(smoke.OUT / f"screenshots/round1-{stage}.ppm")})
        def click(x, y):
            send("input-send-event", {"events": smoke.p2.pointer_events(x, y)})
            for down in (True, False):
                send("input-send-event", {"events": [{"type": "btn", "data": {
                    "down": down, "button": "left"}}]})
        def type_command(command):
            for key in smoke.text_keys(command):
                for down in (True, False):
                    send("input-send-event", {"events": [{"type": "key", "data": {
                        "down": down, "key": {"type": "qcode", "data": key}}}]})
        send("qmp_capabilities")
        screenshot("ready")
        click(smoke.MENU_X, smoke.MENU_Y)
        if toggled:
            click(smoke.COMPACT_X, smoke.COMPACT_Y)
        screenshot("preferences")
        click(smoke.MENU_X, smoke.MENU_Y)
        click(smoke.DOCK_X, smoke.DOCK_Y)
        screenshot("foot")
        type_command("p3save " + NONCE)
        screenshot("saved")
        click(smoke.DOCK_X, smoke.DOCK_Y)
        type_command("exit")
        screenshot("desktop")
        send("system_powerdown")
        screenshot_metadata = {stage: {"path": f"screenshots/round1-{stage}.ppm"}
                               for stage in smoke.SCREEN_STAGES}
        return events, screenshot_metadata

    def test_full_real_input_sequence_accepts_acknowledged_qmp_fixture(self):
        for toggled in (True, False):
            with self.subTest(toggled=toggled):
                events, images = self.transcript_fixture(toggled)
                smoke.transcript("\n".join(json.dumps(x) for x in events), NONCE, images,
                                 preference_toggled=toggled)

    def test_missing_dock_click_in_qmp_transcript_rejected(self):
        events, images = self.transcript_fixture()
        events = [x for i, x in enumerate(events) if i not in (22, 23, 24, 25)]
        with self.assertRaises(smoke.Phase3Failure):
            smoke.transcript("\n".join(json.dumps(x) for x in events), NONCE, images,
                             preference_toggled=True)

    def test_restrict_keyboard_to_safe_ascii(self):
        self.assertEqual(smoke.text_keys("p3save 123"), list("p3save") + ["spc", "1", "2", "3", "ret"])
        for malicious in ("echo hi; reboot", "foo\nbar", "a/b", "ABC", "p3save $(id)"):
            with self.subTest(value=malicious):
                with self.assertRaises(ValueError):
                    smoke.text_keys(malicious)

    def test_qmp_non_replied_transaction_rejected(self):
        # Unit-only fixture; an actual acceptance report needs QMP bytes/frames.
        events = [
            {"direction": "receive", "message": {"QMP": {}}},
            {"direction": "send", "message": {"execute": "qmp_capabilities", "id": 1}},
        ]
        with self.assertRaises(smoke.Phase3Failure):
            smoke.transcript("\n".join(json.dumps(x) for x in events), NONCE, {})

    def test_negative_cannot_contain_qmp_events(self):
        with self.assertRaises(smoke.Phase3Failure):
            smoke.transcript('{"direction":"send","message":{}}', None, {})


if __name__ == "__main__":
    unittest.main()
