# SPDX-License-Identifier: GPL-2.0-only
"""Host-only QMP and Sway-IPC acceptance protocol regressions.

All protocol records and Sway trees here are synthetic fixtures. They cannot
qualify a VM: the separate QEMU smoke still requires real HID, DRM frames,
source-bound guest serial logs, and actual Sway IPC observations.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke-test.py"
SPEC = importlib.util.spec_from_file_location("phase3_smoke_interactions_unit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)

NONCE = "0123456789abcdef0123456789abcdef"
MENU_TARGETS = {"Minimize": [519, 624], "Close": [519, 667]}
COMPACT_TARGET = [60, 444, 92, 26]
TERMINAL_TARGET = [140, 153, 248, 40]


def checked_transcript(records, nonce, screenshots, *, preference_toggled=False,
                       menu_targets=None, compact_target=None, terminal_target=None):
    if menu_targets is None and nonce is not None:
        menu_targets = MENU_TARGETS
    if nonce is not None:
        compact_target = COMPACT_TARGET if compact_target is None else compact_target
        terminal_target = TERMINAL_TARGET if terminal_target is None else terminal_target
    return smoke.transcript(records, nonce, screenshots,
                            preference_toggled=preference_toggled,
                            menu_targets=menu_targets,
                            compact_target=compact_target,
                            terminal_target=terminal_target)


class QMPFixture:
    def __init__(self):
        self.events = [{"direction": "receive", "message": {"QMP": {"version": {}}}}]
        self.next_id = 0

    def send(self, command, arguments=None):
        self.next_id += 1
        request = {"execute": command, "id": self.next_id}
        if arguments is not None:
            request["arguments"] = arguments
        self.events.append({"direction": "send", "message": request})
        # QMP asynchronous events can appear before AND after a response.
        self.events.append({"direction": "receive", "message": {"event": "TEST"}})
        self.events.append({"direction": "receive",
                            "message": {"return": {}, "id": self.next_id}})
        self.events.append({"direction": "receive", "message": {"event": "TEST"}})

    def click(self, x, y, button="left"):
        self.send("input-send-event", {"events": smoke.p2.pointer_events(x, y)})
        for down in (True, False):
            self.send("input-send-event", {"events": [
                {"type": "btn", "data": {"down": down, "button": button}},
            ]})

    def key(self, key, down):
        self.send("input-send-event", {"events": [
            {"type": "key", "data": {"down": down,
                                    "key": {"type": "qcode", "data": key}}},
        ]})

    def pressed(self, key):
        self.key(key, True)
        self.key(key, False)

    def text(self, value):
        for key in smoke.text_keys(value):
            self.pressed(key)

    def screenshot(self, stage):
        self.send("screendump", {
            "filename": str(smoke.OUT / f"screenshots/round1-{stage}.ppm"),
        })

    def build(self, toggled=True):
        self.send("qmp_capabilities")
        self.screenshot("ready")
        self.click(smoke.MENU_X, smoke.MENU_Y)
        self.screenshot("launcher")
        if toggled:
            self.click(*COMPACT_TARGET[:2])
        self.screenshot("preferences")
        self.click(*TERMINAL_TARGET[:2])
        self.screenshot("foot")
        self.text("p3save " + NONCE)
        self.screenshot("saved")
        # Sway Mod4+Return, two simultaneously held genuine keyboard keys.
        self.key("meta_l", True)
        self.key("ret", True)
        self.key("ret", False)
        self.key("meta_l", False)
        self.screenshot("second")
        self.click(smoke.DOCK_X, smoke.DOCK_Y)
        self.screenshot("focused")
        self.click(smoke.DOCK_X, smoke.DOCK_Y, "right")
        self.screenshot("menu_minimize")
        self.click(*MENU_TARGETS["Minimize"])
        self.screenshot("hidden")
        self.click(smoke.DOCK_X, smoke.DOCK_Y)
        self.screenshot("restored")
        self.click(smoke.DOCK_X, smoke.DOCK_Y, "right")
        self.screenshot("menu_close")
        self.click(*MENU_TARGETS["Close"])
        self.screenshot("closed")
        self.text("exit")
        self.screenshot("desktop")
        self.send("system_powerdown")
        images = {
            stage: {"path": f"screenshots/round1-{stage}.ppm"}
            for stage in smoke.SCREEN_STAGES
        }
        return self.events, images

    @staticmethod
    def encoded(events):
        return "\n".join(json.dumps(entry) for entry in events)


def node(con_id, *, pid=20371, focused=False, scratch="none", visible=True):
    return {
        "id": con_id, "app_id": "foot", "pid": pid, "focused": focused,
        "scratchpad_state": scratch, "visible": visible,
        "minimized": scratch != "none" and not visible,
    }


def snapshot(seq, windows, *, source="sway.get_tree"):
    return "P3:IPC_TREE:" + json.dumps({
        "seq": seq, "source": source, "windows": windows,
    }, separators=(",", ":"))


class QMPInteractionContract(unittest.TestCase):
    def expect_valid(self, fixture, toggled=True):
        events, screenshots = fixture.build(toggled)
        checked_transcript(fixture.encoded(events), NONCE, screenshots,
                         preference_toggled=toggled)
        return events, screenshots

    def test_complete_two_foot_dock_menu_real_hid_protocol(self):
        for toggled in (True, False):
            with self.subTest(preference_toggled=toggled):
                self.expect_valid(QMPFixture(), toggled)

    def test_round2_launcher_and_preferences_frames_are_adjacent_without_click(self):
        fixture = QMPFixture()
        events, images = fixture.build(toggled=False)
        sends = [record["message"] for record in events
                 if record["direction"] == "send"]
        launcher_path = str(smoke.OUT / "screenshots/round1-launcher.ppm")
        preference_path = str(smoke.OUT / "screenshots/round1-preferences.ppm")
        launch_index = next(i for i, req in enumerate(sends)
                            if req.get("execute") == "screendump" and
                            req.get("arguments", {}).get("filename") == launcher_path)
        self.assertEqual(
            sends[launch_index + 1],
            {"execute": "screendump", "id": sends[launch_index + 1]["id"],
             "arguments": {"filename": preference_path}},
        )
        checked_transcript(fixture.encoded(events), NONCE, images,
                           preference_toggled=False)

    def test_missing_or_wrong_second_foot_mod4_chord_rejected(self):
        fixture = QMPFixture()
        events, images = self.expect_valid(fixture)
        for replacement in ("alt_l", "ctrl"):
            with self.subTest(replacement=replacement):
                mutated = copy.deepcopy(events)
                for entry in mutated:
                    msg = entry.get("message", {})
                    for event in msg.get("arguments", {}).get("events", []):
                        if event.get("type") == "key" and event["data"]["key"]["data"] == "meta_l":
                            event["data"]["key"]["data"] = replacement
                with self.assertRaises(smoke.Phase3Failure):
                    checked_transcript(fixture.encoded(mutated), NONCE, images,
                                     preference_toggled=True)

    def test_popup_right_click_and_calibrated_pointer_menu_sequence_required(self):
        fixture = QMPFixture()
        events, images = self.expect_valid(fixture)
        for change in ("wrong_button", "wrong_menu_target"):
            with self.subTest(change=change):
                mutated = copy.deepcopy(events)
                if change == "wrong_button":
                    changed = False
                    for entry in mutated:
                        for e in entry["message"].get("arguments", {}).get("events", []):
                            if e.get("type") == "btn" and e["data"]["button"] == "right":
                                e["data"]["button"] = "left"
                                changed = True
                    self.assertTrue(changed)
                else:
                    changed = False
                    expected = smoke.p2.pointer_events(*MENU_TARGETS["Minimize"])
                    for entry in mutated:
                        msg = entry["message"]
                        if msg.get("arguments", {}).get("events") == expected:
                            msg["arguments"]["events"] = smoke.p2.pointer_events(
                                500, 650)
                            changed = True
                            break
                    self.assertTrue(changed)
                with self.assertRaises(smoke.Phase3Failure):
                    checked_transcript(fixture.encoded(mutated), NONCE, images,
                                     preference_toggled=True)

    def test_dynamic_gtk_widget_coordinates_are_required_not_hardcoded(self):
        fixture = QMPFixture()
        events, images = self.expect_valid(fixture)
        for kwargs in (
            {"compact_target": [60, 444, 0, 26]},
            {"compact_target": [90, 460, 92, 26]},
            {"terminal_target": [130, 153, 248, 40]},
            {"terminal_target": [140, 153, 248, 0]},
        ):
            with self.subTest(fields=kwargs):
                with self.assertRaises(smoke.Phase3Failure):
                    checked_transcript(fixture.encoded(events), NONCE, images,
                                       preference_toggled=True, **kwargs)
        for stage in ("launcher", "preferences"):
            with self.subTest(stage=stage):
                corrupted = copy.deepcopy(events)
                path = str(smoke.OUT / f"screenshots/round1-{stage}.ppm")
                corrupted = [
                    event for event in corrupted if not (
                        event["direction"] == "send" and
                        event["message"].get("execute") == "screendump" and
                        event["message"].get("arguments", {}).get("filename") == path)
                ]
                with self.assertRaises(smoke.Phase3Failure):
                    checked_transcript(fixture.encoded(corrupted), NONCE, images,
                                       preference_toggled=True)

    def test_first_launcher_terminal_click_not_dock_bypass(self):
        fixture = QMPFixture()
        events, images = self.expect_valid(fixture)
        mutated = copy.deepcopy(events)
        target = smoke.p2.pointer_events(*TERMINAL_TARGET[:2])
        found = False
        for entry in mutated:
            request = entry["message"]
            if request.get("execute") == "input-send-event" and (
                    request.get("arguments", {}).get("events") == target):
                request["arguments"]["events"] = smoke.p2.pointer_events(
                    smoke.DOCK_X, smoke.DOCK_Y)
                found = True
                break
        self.assertTrue(found)
        with self.assertRaises(smoke.Phase3Failure):
            checked_transcript(fixture.encoded(mutated), NONCE, images,
                             preference_toggled=True)

    def test_all_thirteen_stage_screendumps_bound_to_exact_qmp_paths(self):
        fixture = QMPFixture()
        events, images = self.expect_valid(fixture)
        self.assertEqual(len(images), 13)
        for stage in smoke.SCREEN_STAGES:
            with self.subTest(stage=stage):
                mutated = copy.deepcopy(events)
                path = str(smoke.OUT / f"screenshots/round1-{stage}.ppm")
                for entry in mutated:
                    msg = entry.get("message", {})
                    if (msg.get("execute") == "screendump" and
                            msg.get("arguments", {}).get("filename") == path):
                        msg["arguments"]["filename"] = path + ".wrong"
                        break
                else:
                    self.fail("screenshot command absent in test fixture")
                with self.assertRaises(smoke.Phase3Failure):
                    checked_transcript(fixture.encoded(mutated), NONCE, images,
                                     preference_toggled=True)

    def test_qmp_reply_ids_errors_extra_commands_cannot_be_forged(self):
        fixture = QMPFixture()
        events, images = self.expect_valid(fixture)
        wrong_reply = copy.deepcopy(events)
        next(e for e in wrong_reply if "return" in e["message"])["message"]["id"] = 999
        with self.assertRaises(smoke.Phase3Failure):
            checked_transcript(fixture.encoded(wrong_reply), NONCE, images,
                             preference_toggled=True)
        bad_error = copy.deepcopy(events)
        next(e for e in bad_error if "return" in e["message"])["message"].update(
            {"error": {"class": "GenericError", "desc": "injected"}})
        with self.assertRaises(smoke.Phase3Failure):
            checked_transcript(fixture.encoded(bad_error), NONCE, images,
                             preference_toggled=True)
        extra = QMPFixture()
        extra.build()
        extra.send("query-status")
        with self.assertRaises(smoke.Phase3Failure):
            checked_transcript(extra.encoded(extra.events), NONCE, images,
                             preference_toggled=True)

    def test_acknowledged_popup_screendump_retries_are_replayed(self):
        base = QMPFixture()
        events, images = base.build()
        retried = QMPFixture()
        popup_path = str(smoke.OUT / "screenshots/round1-menu_minimize.ppm")
        retries = 0
        for entry in events:
            msg = entry["message"]
            if entry["direction"] != "send":
                continue
            retried.send(msg["execute"], msg.get("arguments"))
            if (msg["execute"] == "screendump" and
                    msg.get("arguments", {}).get("filename") == popup_path):
                retried.send(msg["execute"], msg.get("arguments"))
                retries += 1
        self.assertEqual(retries, 1)
        checked_transcript(retried.encoded(retried.events), NONCE, images,
                         preference_toggled=True)

        bad = copy.deepcopy(retried.events)
        # Omit the *ack* for the extra screendump, keeping request IDs and
        # every later command unchanged: a merely sent retry cannot count.
        retry_ids = [
            entry["message"]["id"] for entry in bad
            if entry["direction"] == "send" and
            entry["message"].get("execute") == "screendump" and
            entry["message"].get("arguments", {}).get("filename") == popup_path
        ]
        self.assertEqual(len(retry_ids), 2)
        retry_id = retry_ids[1]
        bad = [
            entry for entry in bad
            if not (entry["direction"] == "receive" and
                    entry["message"].get("id") == retry_id and
                    "return" in entry["message"])
        ]
        with self.assertRaises(smoke.Phase3Failure):
            checked_transcript(retried.encoded(bad), NONCE, images,
                             preference_toggled=True)

    def test_negative_case_rejects_all_gui_qmp_commands(self):
        fixture = QMPFixture()
        events, images = fixture.build()
        with self.assertRaises(smoke.Phase3Failure):
            checked_transcript(fixture.encoded(events), None, {})
        checked_transcript("", None, {})



def synthetic_ppm_popup(rectangles=(), *, body=(26, 42, 64),
                        border=(117, 152, 180), draw_border=True):
    """Build two native 1024x768 P6 frames; only rectangles differ."""
    width, height = 1024, 768
    before_pixels = bytes((61, 74, 90)) * (width * height)
    popup_pixels = bytearray(before_pixels)
    for x0, y0, rect_width, rect_height in rectangles:
        for y in range(y0, y0 + rect_height):
            for x in range(x0, x0 + rect_width):
                edge = (x in (x0, x0 + rect_width - 1) or
                        y in (y0, y0 + rect_height - 1))
                color = border if edge and draw_border else body
                offset = (y * width + x) * 3
                popup_pixels[offset:offset + 3] = bytes(color)
    header = b"P6\n1024 768\n255\n"
    return (smoke.p2.parse_ppm(header + before_pixels),
            smoke.p2.parse_ppm(header + bytes(popup_pixels)))


class DockPopupPixelContract(unittest.TestCase):
    def test_real_css_rectangle_calibrates_two_distinct_pointer_rows(self):
        before, popup = synthetic_ppm_popup([(430, 560, 180, 128)])
        self.assertEqual(smoke.popup_menu_target(before, popup, "Minimize"),
                         (519, 624))
        self.assertEqual(smoke.popup_menu_target(before, popup, "Close"),
                         (519, 667))

    def test_no_popup_and_wrong_palette_fail_closed(self):
        for rects, options in (
            ([], {}),
            ([(430, 560, 180, 128)], {"body": (230, 0, 180),
                                       "border": (230, 0, 180)}),
            ([(430, 560, 180, 128)], {"draw_border": False}),
        ):
            with self.subTest(rectangles=rects, options=options):
                before, popup = synthetic_ppm_popup(rects, **options)
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.popup_menu_target(before, popup, "Minimize")

    def test_two_equally_plausible_popups_are_ambiguous(self):
        before, popup = synthetic_ppm_popup([
            (410, 558, 132, 115), (560, 558, 132, 115),
        ])
        with self.assertRaises(smoke.Phase3Failure):
            smoke.popup_menu_target(before, popup, "Close")

    def test_popup_outside_dock_region_is_not_accepted(self):
        before, popup = synthetic_ppm_popup([(700, 560, 110, 100)])
        with self.assertRaises(smoke.Phase3Failure):
            smoke.popup_menu_target(before, popup, "Minimize")

    def test_unknown_menu_action_and_non_native_frame_rejected(self):
        before, popup = synthetic_ppm_popup([(430, 560, 180, 128)])
        with self.assertRaises(ValueError):
            smoke.popup_menu_target(before, popup, "Execute")
        narrow = type(before)(800, 768, before.pixels)
        with self.assertRaises(smoke.Phase3Failure):
            smoke.popup_menu_target(narrow, popup, "Close")


class SwayIPCContract(unittest.TestCase):
    def test_real_get_tree_shapes_sequencing_and_window_presence(self):
        messages = [
            snapshot(1, []),
            snapshot(2, [node(371, focused=True)]),
            "P3:WINDOW_DISCOVERED:371:foot",
            snapshot(3, [node(371), node(372, pid=20372, focused=True)]),
            "P3:WINDOW_ACTIVATED:372",
            snapshot(4, [node(371, focused=True), node(372, pid=20372)]),
            "P3:WINDOW_ACTIVATED:371",
            snapshot(5, [node(371, scratch="fresh", visible=False),
                         node(372, pid=20372, focused=True)]),
            "P3:WINDOW_MINIMIZED:371",
            snapshot(6, [node(372, pid=20372, focused=True)]),
            "P3:WINDOW_CLOSED:371",
        ]
        states = smoke.ipc_states(messages)
        self.assertEqual(len(states), 6)
        live, tree = smoke.verified_window_state(states, 4, 372)
        self.assertEqual((live["id"], live["pid"], live["focused"]),
                         (372, 20372, True))
        hidden, _ = smoke.verified_window_state(states, 8, 371)
        self.assertTrue(hidden["minimized"])
        closed, _ = smoke.verified_window_state(states, 10, 371)
        self.assertIsNone(closed)
        self.assertEqual([w["id"] for w in tree["windows"]], [371, 372])

    def test_missing_reordered_or_forged_snapshot_metadata_rejected(self):
        samples = (
            [],
            ["P3:WINDOW_DISCOVERED:371:foot"],
            [snapshot(2, [])],
            [snapshot(1, []), snapshot(1, [])],
            [snapshot(1, []), snapshot(3, [])],
            [snapshot(1, [], source="mock.tree")],
            ["P3:IPC_TREE:{not json}"],
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.ipc_states(sample)

    def test_malformed_ids_pids_types_focus_and_scratchpad_rejected(self):
        invalid = (
            [node(371), node(371)],  # duplicate con_id
            [node(0)],
            [node(True)],
            [node(371, pid=0)],
            [node(371, pid="20371")],
            [node(371, pid=True)],
            [node(371, focused=True), node(372, focused=True)],
            [{**node(371), "focused": "yes"}],
            [{**node(371), "scratchpad_state": 42}],
            [{**node(371), "minimized": True}],
            [{**node(371, scratch="fresh", visible=False), "minimized": False}],
        )
        for windows in invalid:
            with self.subTest(windows=windows):
                with self.assertRaises(smoke.Phase3Failure):
                    smoke.ipc_states([snapshot(1, windows)])

    def test_missing_pre_transition_snapshot_is_not_state_evidence(self):
        states = smoke.ipc_states([snapshot(1, []), "P3:WINDOW_DISCOVERED:371:foot"])
        with self.assertRaises(smoke.Phase3Failure):
            smoke.verified_window_state(states, 0, 371)
        missing, _ = smoke.verified_window_state(states, 1, 371)
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main()
