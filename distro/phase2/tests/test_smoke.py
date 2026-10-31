# SPDX-License-Identifier: GPL-2.0-only
"""Host regressions: recorded markers alone must never turn a broken GUI green."""

import binascii
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zlib


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke-test.py"
SPEC = importlib.util.spec_from_file_location("phase2_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)

SOURCE = "a" * 40
RELEASE = "7.3.0-rc7-phase2"
NONCE = "0123456789abcdef0123456789abcdef"
OLD_NONCE = "f" * 32


def good_output(previous=None):
    return "\n".join([
        f"[    0.000000] Linux version {RELEASE} (builder)",
        f"P2:SOURCE:{SOURCE}", f"P2:KERNEL:{RELEASE}",
        *smoke.BOOT_MARKERS, f"P2:PERSISTED:{previous or 'EMPTY'}", "P2:PROBE_READY",
        f"P2:KEYBOARD_OK:{NONCE}", f"P2:POINTER_OK:{NONCE}", "P2:PROBE_CLOSED",
        "[   18.123000] reboot: Power down", "",
    ])


def good_result(previous=None):
    return smoke.BootResult(
        name="round2" if previous else "round1", output=good_output(previous),
        returncode=0, nonce=NONCE, previous_nonce=previous,
        keyboard_sent=True, pointer_sent=True, escape_sent=True, powerdown_sent=True,
        screenshots={stage: {"sha256": str(index + 1) * 64,
                             "png_sha256": str(index + 5) * 64,
                             "width": 1024, "height": 768}
                     for index, stage in enumerate(smoke.SCREEN_STAGES)},
    )


def good_negative():
    return smoke.BootResult(name="negative", returncode=0, output="\n".join([
        f"Linux version {RELEASE} (builder)", f"P2:SOURCE:{SOURCE}",
        f"P2:KERNEL:{RELEASE}", *smoke.BOOT_MARKERS[:4], "P2:FAIL:NO_DRM",
        "reboot: Power down", "",
    ]))


def patterned_frame(stage="ready"):
    # Detailed deterministic synthetic RGB data exercises image checks, not QEMU.
    row = b"".join(bytes((x % 256, x % 256, x % 256)) for x in range(1024))
    pixels = bytearray(row * 768)
    if stage in smoke.SCREEN_COLORS:
        panel = bytes(smoke.SCREEN_COLORS[stage]) * 864
        for y in range(100, 320):
            offset = (y * 1024 + 80) * 3
            pixels[offset:offset + len(panel)] = panel
    return smoke.Frame(1024, 768, bytes(pixels))


class TestPositiveEvidence(unittest.TestCase):
    def check(self, result):
        smoke.validate_test_boot(result, RELEASE, SOURCE)

    def test_first_boot(self):
        self.check(good_result())

    def test_persisted_second_boot(self):
        self.check(good_result(OLD_NONCE))

    def test_crlf_serial(self):
        result = good_result()
        result.output = result.output.replace("\n", "\r\n")
        self.check(result)

    def test_each_missing_marker_rejected(self):
        for marker in smoke.BOOT_MARKERS:
            with self.subTest(marker=marker):
                result = good_result()
                result.output = result.output.replace(marker + "\n", "")
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_each_duplicate_marker_rejected(self):
        for marker in (*smoke.BOOT_MARKERS, "P2:PROBE_READY", "P2:PROBE_CLOSED"):
            with self.subTest(marker=marker):
                result = good_result()
                result.output += marker + "\n"
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_echoed_marker_not_evidence(self):
        result = good_result()
        result.output = result.output.replace("P2:DRM_OK\n", "# echo P2:DRM_OK\n")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_reordered_startup_rejected(self):
        result = good_result()
        result.output = result.output.replace("P2:DRM_OK\nP2:INPUT_OK", "P2:INPUT_OK\nP2:DRM_OK")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_reordered_input_rejected(self):
        result = good_result()
        result.output = result.output.replace(f"P2:KEYBOARD_OK:{NONCE}\nP2:POINTER_OK:{NONCE}",
                                              f"P2:POINTER_OK:{NONCE}\nP2:KEYBOARD_OK:{NONCE}")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_root_desktop_rejected(self):
        result = good_result()
        result.output = result.output.replace("P2:DESKTOP_USER:1000", "P2:DESKTOP_USER:0")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_multiple_user_identities_rejected(self):
        result = good_result()
        result.output += "P2:DESKTOP_USER:0\n"
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_wrong_kernel_or_source_rejected(self):
        for prefix in ("P2:KERNEL:", "P2:SOURCE:"):
            with self.subTest(prefix=prefix):
                result = good_result()
                result.output = result.output.replace(prefix, prefix + "wrong-")
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_wrong_kernel_boot_line_rejected(self):
        result = good_result()
        result.output = result.output.replace(f"Linux version {RELEASE}", "Linux version host-kernel")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_late_identity_rejected(self):
        result = good_result()
        result.output = result.output.replace(f"P2:SOURCE:{SOURCE}\n", "") + f"P2:SOURCE:{SOURCE}\n"
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_wrong_challenge_rejected(self):
        for prefix in ("P2:KEYBOARD_OK:", "P2:POINTER_OK:"):
            with self.subTest(prefix=prefix):
                result = good_result()
                result.output = result.output.replace(prefix + NONCE, prefix + OLD_NONCE)
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_stale_persisted_value_rejected(self):
        result = good_result(OLD_NONCE)
        result.output = result.output.replace("P2:PERSISTED:" + OLD_NONCE, "P2:PERSISTED:EMPTY")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_nonempty_first_boot_rejected(self):
        result = good_result()
        result.output = result.output.replace("P2:PERSISTED:EMPTY", "P2:PERSISTED:" + OLD_NONCE)
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_reused_nonce_rejected(self):
        result = good_result(NONCE)
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_no_nonce_rejected(self):
        result = good_result()
        result.nonce = None
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_missing_host_input_rejected(self):
        for field in ("keyboard_sent", "pointer_sent", "escape_sent", "powerdown_sent"):
            with self.subTest(field=field):
                result = good_result()
                setattr(result, field, False)
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_missing_screenshot_rejected(self):
        for stage in smoke.SCREEN_STAGES:
            with self.subTest(stage=stage):
                result = good_result()
                del result.screenshots[stage]
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_identical_screenshots_rejected(self):
        result = good_result()
        result.screenshots["keyboard"]["sha256"] = result.screenshots["ready"]["sha256"]
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_invalid_screenshot_metadata_rejected(self):
        for key, value in (("width", 800), ("height", 600), ("sha256", "bad"), ("png_sha256", "bad")):
            with self.subTest(key=key):
                result = good_result()
                result.screenshots["ready"][key] = value
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_panic_oops_bug_and_failure_rejected(self):
        for failure in ("Kernel panic - not syncing", "Oops: 0000", "BUG: bad page",
                        "EXT4-fs error (device vda2)", "P2:FAIL:INPUT"):
            with self.subTest(failure=failure):
                result = good_result()
                result.output += failure + "\n"
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_timeout_output_flood_and_host_failure_rejected(self):
        for field, value in (("timed_out", True), ("output_limit", True), ("returncode", 1)):
            with self.subTest(field=field):
                result = good_result()
                setattr(result, field, value)
                with self.assertRaises(smoke.SmokeFailure):
                    self.check(result)

    def test_missing_shutdown_rejected(self):
        result = good_result()
        result.output = result.output.replace("[   18.123000] reboot: Power down\n", "")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_premature_shutdown_rejected(self):
        result = good_result()
        result.output = "reboot: Power down\n" + result.output.replace("[   18.123000] reboot: Power down\n", "")
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_duplicate_shutdown_rejected(self):
        result = good_result()
        result.output += "reboot: Power down\n"
        with self.assertRaises(smoke.SmokeFailure):
            self.check(result)

    def test_partial_line_not_evidence(self):
        self.assertEqual(smoke.serial_lines("P2:ROOT_OK\nP2:DRM_OK"), ["P2:ROOT_OK"])


class TestNegativeEvidence(unittest.TestCase):
    def test_expected_no_gpu_failure_passes(self):
        smoke.validate_negative_boot(good_negative(), RELEASE, SOURCE)

    def test_generic_crash_does_not_pass(self):
        for text in ("Kernel panic - not syncing", "P2:FAIL:WESTON", "no display", ""):
            with self.subTest(text=text):
                result = good_negative()
                result.output = result.output.replace("P2:FAIL:NO_DRM", text)
                with self.assertRaises(smoke.SmokeFailure):
                    smoke.validate_negative_boot(result, RELEASE, SOURCE)

    def test_positive_graphical_markers_rejected(self):
        for marker in ("P2:DRM_OK", "P2:PROBE_READY", "P2:DESKTOP_USER:1000",
                       f"P2:KEYBOARD_OK:{NONCE}"):
            with self.subTest(marker=marker):
                result = good_negative()
                result.output += marker + "\n"
                with self.assertRaises(smoke.SmokeFailure):
                    smoke.validate_negative_boot(result, RELEASE, SOURCE)

    def test_late_failure_rejected(self):
        result = good_negative()
        result.output = result.output.replace("P2:FAIL:NO_DRM\n", "") + "P2:FAIL:NO_DRM\n"
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE, SOURCE)

    def test_no_gpu_timeout_rejected(self):
        result = good_negative()
        result.timed_out = True
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE, SOURCE)

    def test_host_launch_failure_rejected(self):
        result = good_negative()
        result.returncode = 1
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_negative_boot(result, RELEASE, SOURCE)

    def test_no_gpu_requires_real_kernel_and_root(self):
        for marker in ("P2:ROOT_OK", "P2:NETWORK_OK", f"P2:SOURCE:{SOURCE}"):
            with self.subTest(marker=marker):
                result = good_negative()
                result.output = result.output.replace(marker + "\n", "")
                with self.assertRaises(smoke.SmokeFailure):
                    smoke.validate_negative_boot(result, RELEASE, SOURCE)


class TestKernelFailureClassification(unittest.TestCase):
    WARNINGS = (
        "[22:58:58.061] client bug: timer event2 debounce: scheduled expiry is in the past (-24ms), your system is too slow",
        "[22:58:58.075] client bug: timer event2 debounce short: scheduled expiry is in the past (-52ms), your system is too slow",
    )
    KERNEL_MESSAGES = (
        "BUG: unable to handle page fault for address: 0000000000000000",
        "kernel BUG at mm/slub.c:123!",
        "watchdog: BUG: soft lockup - CPU#0 stuck for 22s! [weston:123]",
        "NMI watchdog: BUG: hard LOCKUP on cpu 1",
        "Kernel panic - not syncing: Fatal exception",
        "Oops: general protection fault, probably for non-canonical address",
        "No working init found. Try passing init= option to kernel.",
        "EXT4-fs error (device vda2): ext4_find_entry: inode corrupted",
        "EXT4-fs (vda2): Remounting filesystem read-only after abort",
        "EXT4-fs (vda2): I/O error while writing superblock",
    )

    def monitor(self, output):
        # Exercise the real live classifier without starting a VM/thread.
        monitor = smoke.SerialMonitor.__new__(smoke.SerialMonitor)
        monitor.process = Mock()
        monitor.process.poll.return_value = None
        monitor.deadline = time.monotonic() + 30
        monitor.expected_failure = None
        monitor.data = bytearray(output.encode())
        monitor.lock = threading.Lock()
        monitor.output_limit = False
        monitor.timed_out = False
        monitor.read_error = None
        return monitor

    def test_actual_libinput_warnings_are_not_kernel_failures(self):
        output = "\n".join(self.WARNINGS) + "\n"
        self.assertIsNone(smoke.KERNEL_FAILURE_RE.search(output))
        self.monitor(output).check()
        result = good_result(OLD_NONCE)
        result.output = result.output.replace("P2:PROBE_CLOSED", output + "P2:PROBE_CLOSED")
        smoke.validate_test_boot(result, RELEASE, SOURCE)

    def test_userspace_client_bug_is_not_classified_by_case_or_substring(self):
        for line in ("client bug: a slow timer", "client BUG: a slow timer",
                     "[   30.100000] client bug: a slow timer",
                     "[22:58:58.061] client BUG: a slow timer"):
            with self.subTest(line=line):
                self.assertIsNone(smoke.KERNEL_FAILURE_RE.search(line))

    def test_true_kernel_message_families_fail_with_or_without_printk_prefix(self):
        for message in self.KERNEL_MESSAGES:
            for prefix in ("", "[   30.100000] ", "<0>[   30.100000] ",
                           "<0> [   30.100000] ", "<0>\t", "<0> "):
                with self.subTest(message=message, prefix=prefix):
                    output = "ordinary boot output\r\n" + prefix + message
                    with self.assertRaisesRegex(smoke.SmokeFailure, "kernel failure"):
                        self.monitor(output).check()
                    result = good_result()
                    result.output += prefix + message + "\n"
                    with self.assertRaisesRegex(smoke.SmokeFailure, "kernel failure"):
                        smoke.validate_test_boot(result, RELEASE, SOURCE)

    def test_partial_serial_bug_is_detected_when_signature_arrives_without_newline(self):
        monitor = self.monitor("ordinary boot output\n[   30.100000] BU")
        monitor.check()
        monitor.data.extend(b"G: bad page")
        with self.assertRaisesRegex(smoke.SmokeFailure, "kernel failure"):
            monitor.check()

    def test_fragmented_libinput_warning_never_becomes_a_kernel_failure(self):
        monitor = self.monitor("")
        for byte in (self.WARNINGS[0] + "\n").encode():
            monitor.data.append(byte)
            monitor.check()

    def test_real_bug_after_userspace_warning_is_not_hidden(self):
        output = self.WARNINGS[0] + "\r\n[   30.100000] kernel BUG at mm/slub.c:123!"
        with self.assertRaisesRegex(smoke.SmokeFailure, "kernel failure"):
            self.monitor(output).check()

    def test_printk_uptime_detects_bug_after_unfinished_userspace_line(self):
        for prefix in ("[   30.100000] ", "<0> [   30.100000] "):
            with self.subTest(prefix=prefix):
                monitor = self.monitor("unfinished desktop message " + prefix + "BU")
                monitor.check()
                monitor.data.extend(b"G: bad page")
                with self.assertRaisesRegex(smoke.SmokeFailure, "kernel failure"):
                    monitor.check()

    def test_unprefixed_kernel_words_still_require_a_line_boundary(self):
        for message in self.KERNEL_MESSAGES:
            with self.subTest(message=message):
                self.assertIsNone(smoke.KERNEL_FAILURE_RE.search("desktop diagnostic text " + message))

    def test_guest_failure_marker_remains_fatal_after_libinput_warning(self):
        output = self.WARNINGS[0] + "\nP2:FAIL:INPUT_PROBE\n"
        with self.assertRaisesRegex(smoke.SmokeFailure, "guest reported failure"):
            self.monitor(output).check()


class TestScreenEvidence(unittest.TestCase):
    def test_binary_ppm_with_comments(self):
        frame = smoke.parse_ppm(b"P6\n# QEMU\n2 1\n255\n\0\x01\x02\x03\x04\x05")
        self.assertEqual((frame.width, frame.height), (2, 1))
        self.assertEqual(frame.pixels, bytes(range(6)))

    def test_whitespace_first_pixel_preserved(self):
        frame = smoke.parse_ppm(b"P6\n1 1\n255\n\n\r\t")
        self.assertEqual(frame.pixels, b"\n\r\t")

    def test_crlf_header(self):
        frame = smoke.parse_ppm(b"P6\r\n1 1\r\n255\r\n\x01\x02\x03")
        self.assertEqual(frame.pixels, b"\x01\x02\x03")

    def test_bad_ppm_rejected(self):
        samples = (b"", b"P3\n1 1\n255\n\0\0\0", b"P6\n# truncated", b"P6\nx 1\n255\n",
                   b"P6\n0 1\n255\n", b"P6\n99999 1\n255\n", b"P6\n1 1\n65535\n",
                   b"P6\n1 1\n255", b"P6\n1 1\n255\n\0\0", b"P6\n1 1\n255\n\0\0\0extra")
        for data in samples:
            with self.subTest(data=data):
                with self.assertRaises(smoke.SmokeFailure):
                    smoke.parse_ppm(data)

    def test_wrong_dimensions_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_frame(smoke.Frame(1, 1, b"\0\0\0"), "ready")

    def test_each_visual_input_state(self):
        for stage in smoke.SCREEN_COLORS:
            with self.subTest(stage=stage):
                smoke.validate_frame(patterned_frame(stage), stage)

    def test_stale_visual_state_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_frame(patterned_frame("ready"), "keyboard")

    def test_blank_screen_rejected(self):
        for color in (b"\0\0\0", b"\xff\xff\xff", bytes(smoke.SCREEN_COLORS["ready"])):
            with self.subTest(color=color):
                with self.assertRaises(smoke.SmokeFailure):
                    smoke.validate_frame(smoke.Frame(1024, 768, color * 1024 * 768), "ready")

    def test_desktop_after_probe_closes(self):
        smoke.validate_frame(patterned_frame("desktop"), "desktop")

    def test_probe_cannot_substitute_for_desktop(self):
        for stage in smoke.SCREEN_COLORS:
            with self.subTest(stage=stage):
                with self.assertRaises(smoke.SmokeFailure):
                    smoke.validate_frame(patterned_frame(stage), "desktop")

    def test_png_losslessly_preserves_scanout(self):
        frame = smoke.Frame(2, 1, bytes(range(6)))
        png = smoke.png_bytes(frame)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        position = 8
        chunks = {}
        while position < len(png):
            size = struct.unpack(">I", png[position:position + 4])[0]
            kind = png[position + 4:position + 8]
            payload = png[position + 8:position + 8 + size]
            crc = struct.unpack(">I", png[position + 8 + size:position + 12 + size])[0]
            self.assertEqual(crc, binascii.crc32(kind + payload) & 0xffffffff)
            chunks[kind] = payload
            position += size + 12
        self.assertEqual(struct.unpack(">II", chunks[b"IHDR"][:8]), (2, 1))
        self.assertEqual(zlib.decompress(chunks[b"IDAT"]), b"\0" + frame.pixels)
        self.assertEqual(chunks[b"IEND"], b"")


class TestInputProtocol(unittest.TestCase):
    def test_keys_include_press_and_release(self):
        events = smoke.key_events("a")
        self.assertEqual([event["data"]["down"] for event in events], [True, False])
        self.assertEqual(events[0]["data"]["key"], {"type": "qcode", "data": "a"})

    def test_invalid_keys_rejected(self):
        for key in ("", "ab", "ENTER", "A", "$(anything)", "shift"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    smoke.key_events(key)

    def test_nonce_only_through_key_events(self):
        qmp = Mock()
        with patch.object(smoke.time, "sleep"):
            smoke.send_challenge(qmp, NONCE)
        self.assertEqual(qmp.execute.call_count, 66)
        keys = [call.args[1]["events"][0]["data"]["key"]["data"]
                for call in qmp.execute.call_args_list[::2]]
        self.assertEqual(keys, list(NONCE) + ["ret"])
        self.assertTrue(all(call.args[0] == "input-send-event" for call in qmp.execute.call_args_list))

    def test_invalid_nonce_rejected_without_input(self):
        for nonce in ("a", "F" * 32, "../bad", "a" * 33):
            qmp = Mock()
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                smoke.send_challenge(qmp, nonce)
            qmp.execute.assert_not_called()

    def test_pointer_targets_button_center(self):
        events = smoke.pointer_events()
        self.assertEqual([event["data"]["axis"] for event in events], ["x", "y"])
        self.assertAlmostEqual(events[0]["data"]["value"] * 1023 / 32767, 512, delta=0.1)
        self.assertAlmostEqual(events[1]["data"]["value"] * 767 / 32767, 490, delta=0.1)

    def test_pointer_bounds(self):
        for x, y in ((-1, 0), (1024, 0), (0, 768)):
            with self.assertRaises(ValueError):
                smoke.pointer_events(x, y)

    def test_pointer_movement_precedes_click(self):
        qmp = Mock()
        with patch.object(smoke.time, "sleep"):
            smoke.click_button(qmp)
        calls = qmp.execute.call_args_list
        self.assertEqual(calls[0].args[1]["events"], smoke.pointer_events())
        self.assertTrue(calls[1].args[1]["events"][0]["data"]["down"])
        self.assertFalse(calls[2].args[1]["events"][0]["data"]["down"])


class TestQMPProtocol(unittest.TestCase):
    def client(self, replies):
        qmp = smoke.QMP.__new__(smoke.QMP)
        qmp.deadline = time.monotonic() + 5
        qmp.health = lambda: None
        qmp.buffer = bytearray()
        qmp.sequence = 0
        qmp.sock = Mock()
        qmp.sock.recv.side_effect = [json.dumps(reply).encode() + b"\r\n" for reply in replies]
        qmp.log = io.StringIO()
        return qmp

    def test_command_handles_async_events(self):
        qmp = self.client([{"event": "STOP"}, {"return": {}, "id": 1}])
        self.assertEqual(qmp.execute("query-status"), {})
        request = json.loads(qmp.sock.sendall.call_args.args[0])
        self.assertEqual(request, {"execute": "query-status", "id": 1})

    def test_fragmented_response(self):
        qmp = self.client([])
        qmp.sock.recv.side_effect = [b'{"ret', b'urn": {}, "id": 1}\r', b'\n']
        self.assertEqual(qmp.execute("query-status"), {})

    def test_mismatched_id_rejected(self):
        qmp = self.client([{"return": {}, "id": 2}])
        with self.assertRaises(smoke.SmokeFailure):
            qmp.execute("query-status")

    def test_qmp_error_rejected(self):
        qmp = self.client([{"error": {"class": "GenericError", "desc": "no display"}, "id": 1}])
        with self.assertRaises(smoke.SmokeFailure):
            qmp.execute("screendump")

    def test_malformed_response_rejected(self):
        for data in (b"not-json\n", b"[]\n", b'{"id": 1}\n', b""):
            with self.subTest(data=data):
                qmp = self.client([])
                qmp.sock.recv.side_effect = [data]
                with self.assertRaises(smoke.SmokeFailure):
                    qmp.execute("query-status")

    def test_qmp_timeout_rejected(self):
        qmp = self.client([])
        qmp.deadline = time.monotonic() - 1
        with self.assertRaises(smoke.SmokeFailure):
            qmp.execute("query-status")

    def test_qmp_log_is_bidirectional(self):
        qmp = self.client([{"return": {}, "id": 1}])
        qmp.execute("query-status")
        records = [json.loads(line) for line in qmp.log.getvalue().splitlines()]
        self.assertEqual([record["direction"] for record in records], ["send", "receive"])

    def test_qmp_output_limit(self):
        qmp = self.client([])
        qmp.buffer = bytearray(b"x" * (smoke.MAX_QMP_LINE + 1))
        with self.assertRaises(smoke.SmokeFailure):
            qmp._receive()


class TestQMPReplay(unittest.TestCase):
    def setUp(self):
        self.screenshots = {stage: {"path": f"screenshots/round1-{stage}.ppm"}
                            for stage in smoke.SCREEN_STAGES}
        self.records = [{"direction": "receive", "message": {"QMP": {"version": {}}}}]
        self.sequence = 0

        def emit(command, arguments=None):
            self.sequence += 1
            message = {"execute": command, "id": self.sequence}
            if arguments is not None:
                message["arguments"] = arguments
            self.records.extend([
                {"direction": "send", "message": message},
                {"direction": "receive", "message": {"return": {}, "id": self.sequence}},
            ])

        def screen(stage):
            emit("screendump", {"filename": f"/build/.build/phase2/screenshots/round1-{stage}.ppm"})

        emit("qmp_capabilities")
        screen("ready")
        screen("ready")  # A frame waiting for its page flip is a permitted retry.
        for key in list(NONCE) + ["ret"]:
            for event in smoke.key_events(key):
                emit("input-send-event", {"events": [event]})
        screen("keyboard")
        emit("input-send-event", {"events": smoke.pointer_events()})
        for down in (True, False):
            emit("input-send-event", {"events": [
                {"type": "btn", "data": {"down": down, "button": "left"}},
            ]})
        screen("pointer")
        for event in smoke.key_events("esc"):
            emit("input-send-event", {"events": [event]})
        screen("desktop")
        emit("system_powerdown")

    def check(self, records=None, nonce=NONCE):
        smoke.validate_qmp_log(self.records if records is None else records, nonce, self.screenshots)

    def test_complete_replay_and_screenshot_retries(self):
        self.check()

    def test_jsonl_and_async_events(self):
        self.records.insert(2, {"direction": "receive", "message": {"event": "DEVICE_TRAY_MOVED"}})
        self.check("\n".join(json.dumps(record) for record in self.records))

    def test_wrong_typed_nonce_rejected(self):
        with self.assertRaises(smoke.SmokeFailure):
            self.check(nonce=OLD_NONCE)

    def test_missing_key_up_rejected(self):
        record = next(record for record in self.records if record["message"].get("execute") == "input-send-event")
        record["message"]["arguments"]["events"][0]["data"]["down"] = False
        with self.assertRaises(smoke.SmokeFailure):
            self.check()

    def test_wrong_pointer_coordinate_rejected(self):
        for record in self.records:
            events = record["message"].get("arguments", {}).get("events", [])
            if events and events[0]["type"] == "abs":
                events[0]["data"]["value"] = 0
                break
        with self.assertRaises(smoke.SmokeFailure):
            self.check()

    def test_missing_acknowledgement_rejected(self):
        self.records.pop()
        with self.assertRaises(smoke.SmokeFailure):
            self.check()

    def test_response_error_rejected(self):
        self.records[-1]["message"] = {"id": self.sequence, "error": {"desc": "failure"}}
        with self.assertRaises(smoke.SmokeFailure):
            self.check()

    def test_reordered_or_wrong_screenshot_rejected(self):
        self.records[3]["message"]["arguments"]["filename"] = "/build/.build/phase2/screenshots/round1-keyboard.ppm"
        with self.assertRaises(smoke.SmokeFailure):
            self.check()

    def test_serial_or_monitor_injection_rejected(self):
        record = next(record for record in self.records if record["message"].get("execute") == "input-send-event")
        record["message"]["execute"] = "human-monitor-command"
        with self.assertRaises(smoke.SmokeFailure):
            self.check()

    def test_malformed_or_empty_transcript_rejected(self):
        for records in ("not-json", "", [], [None], [{"direction": "unknown", "message": {}}]):
            with self.subTest(records=records), self.assertRaises(smoke.SmokeFailure):
                self.check(records)

    def test_negative_allows_only_empty_transcript(self):
        smoke.validate_qmp_log("", None, {})
        with self.assertRaises(smoke.SmokeFailure):
            smoke.validate_qmp_log(self.records, None, {})


class TestArtifactSafety(unittest.TestCase):
    def test_symlink_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").write_text("data")
            (root / "link").symlink_to(root / "real")
            with self.assertRaises(smoke.SmokeFailure):
                smoke.safe_file(root / "link", must_exist=False)

    def test_hard_link_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").write_text("data")
            os.link(root / "real", root / "link")
            with self.assertRaises(smoke.SmokeFailure):
                smoke.safe_file(root / "link")

    def test_symlink_directory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(smoke.SmokeFailure):
                smoke.safe_directory(root / "link")

    def test_missing_required_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(smoke.SmokeFailure):
                smoke.safe_file(Path(directory) / "missing")

    def test_changed_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(b"original")
            bound = {path: smoke.digest(path)}
            smoke.validate_unchanged(bound)
            path.write_bytes(b"different")
            with self.assertRaises(smoke.SmokeFailure):
                smoke.validate_unchanged(bound)

    def test_only_test_overlays_may_be_replaced(self):
        for name in ("run", "../../other", "phase2.img", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                smoke.make_overlay(Path("/unused"), name)

    def test_overlay_create_uses_raw_backing_and_qcow2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def create(command, **kwargs):
                Path(command[-1]).write_bytes(b"mock qcow2")
            with patch.object(smoke.subprocess, "run", side_effect=create) as run:
                smoke.make_overlay(root, "test")
            command = run.call_args.args[0]
            self.assertEqual(command, ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                                       "-b", str(root / "phase2.img"), str(root / "test.qcow2")])


class TestFailureDiagnostics(unittest.TestCase):
    def test_only_last_eighty_lines_including_partial_final_line(self):
        output = "\n".join(f"line {index:03d}" for index in range(100))
        tail = smoke.serial_diagnostic_tail(output)
        self.assertEqual(len(tail.splitlines()), 80)
        self.assertTrue(tail.startswith("phase2: guest | line 020\n"))
        self.assertTrue(tail.endswith("phase2: guest | line 099"))
        self.assertNotIn("line 019", tail)

    def test_byte_budget_and_valid_unicode_when_line_is_huge(self):
        output = "old evidence\n" + "\u20ac" * 20000 + "\nP2:FAIL:PACKAGE_MANAGER\n"
        tail = smoke.serial_diagnostic_tail(output)
        self.assertLessEqual(len((tail + "\n").encode("utf-8")), smoke.DIAGNOSTIC_TAIL_BYTES)
        self.assertIn("[earlier bytes truncated]", tail)
        self.assertTrue(tail.endswith("phase2: guest | P2:FAIL:PACKAGE_MANAGER"))
        self.assertNotIn("old evidence", tail)

    def test_terminal_controls_and_workflow_commands_are_diagnostic_only(self):
        tail = smoke.serial_diagnostic_tail("\x1b[31mAPT error\r\n::error::not a host verdict\n")
        self.assertNotIn("\x1b", tail)
        self.assertIn("\\x1b[31mAPT error", tail)
        self.assertIn("phase2: guest | ::error::not a host verdict", tail)
        self.assertTrue(all(line.startswith("phase2: guest | ") for line in tail.splitlines()))

    def test_empty_guest_output_is_explicit(self):
        self.assertIn("(no serial output captured)", smoke.serial_diagnostic_tail(""))

    def test_printing_diagnostics_does_not_change_serial_evidence_or_result(self):
        result = smoke.BootResult(name="round1", output="P2:FAIL:PACKAGE_MANAGER\r\n", returncode=1)
        before = vars(result).copy()
        with patch.object(smoke.sys, "stderr", new_callable=io.StringIO) as stderr:
            smoke.print_failure_diagnostics(result, "logs/boot-round1.log")
        diagnostic = stderr.getvalue()
        self.assertIn("diagnostic serial tail for round1", diagnostic)
        self.assertIn("full evidence unchanged: logs/boot-round1.log", diagnostic)
        self.assertIn("phase2: guest | P2:FAIL:PACKAGE_MANAGER", diagnostic)
        self.assertIn("end diagnostic serial tail", diagnostic)
        self.assertEqual(vars(result), before)


class TestLauncher(unittest.TestCase):
    """Exercise the real shell command construction with non-VM tool stand-ins."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="p2-launch-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "boot-qemu.sh"
        self.script.write_bytes(SCRIPT.with_name("boot-qemu.sh").read_bytes())
        self.output = self.root / "out"
        self.output.mkdir()
        for name in ("phase2.img", "ovmf-code.fd", "ovmf-vars.fd", "test.qcow2", "negative.qcow2"):
            (self.output / name).write_bytes(b"test artifact")
        (self.root / "common.sh").write_text(f'''OUTPUT_DIR={str(self.output)!r}
QEMU_MACHINE=q35
QEMU_CPU_COUNT=2
QEMU_MEMORY_MIB=2048
phase2_die() {{ printf '%s\\n' "$*" >&2; exit 1; }}
phase2_require() {{ :; }}
phase2_prepare_output() {{ :; }}
phase2_assert_regular() {{ [[ -f "$1" && ! -L "$1" && "$(stat -c %h "$1")" == 1 ]] || phase2_die 'unsafe file'; }}
''')
        self.bin = self.root / "bin"
        self.bin.mkdir()
        qemu = self.bin / "qemu-system-x86_64"
        qemu.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        qemu.chmod(0o755)
        qemu_img = self.bin / "qemu-img"
        qemu_img.write_text(f'''#!/usr/bin/env python3
import json, pathlib, sys
if sys.argv[1] == 'info':
    print(json.dumps({{'format': 'qcow2', 'full-backing-filename': {str(self.output / "phase2.img")!r}, 'backing-filename-format': 'raw'}}))
elif sys.argv[1] == 'create':
    pathlib.Path(sys.argv[-1]).write_bytes(b'mock overlay')
''')
        qemu_img.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}

    def invoke(self, *args, success=True):
        process = subprocess.run(["bash", str(self.script), *args], text=True, capture_output=True, env=self.env)
        if success:
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        self.assertNotEqual(process.returncode, 0, process.stdout)
        return process

    def test_firmware_only_boot_and_devices(self):
        args = self.invoke("--mode", "test", "--name", "round1")
        self.assertNotIn("-kernel", args)
        self.assertNotIn("-initrd", args)
        self.assertNotIn("-append", args)
        self.assertIn("q35,accel=tcg", args)
        self.assertIn("virtio-gpu-pci,xres=1024,yres=768", args)
        self.assertIn("virtio-keyboard-pci", args)
        self.assertIn("virtio-tablet-pci", args)
        self.assertIn("name=opt/phase2/test,string=1", args)
        self.assertIn("-no-reboot", args)
        self.assertIn("2048", args)
        self.assertIn("2", args)
        self.assertEqual(sum("if=pflash" in arg for arg in args), 2)

    def test_negative_omits_gpu_and_vga(self):
        args = self.invoke("--mode", "negative")
        self.assertFalse(any("virtio-gpu" in arg for arg in args))
        self.assertEqual(args[args.index("-vga") + 1], "none")
        self.assertIn("name=opt/phase2/test,string=1", args)

    def test_only_localhost_vnc_in_run_mode(self):
        args = self.invoke("--mode", "run", "--vnc", "1")
        self.assertEqual(args[args.index("-vnc") + 1], "127.0.0.1:1")
        self.assertNotIn("-fw_cfg", args)
        self.assertNotIn("-kernel", args)

    def test_default_run_preserves_separate_overlay(self):
        self.invoke()
        self.assertTrue((self.output / "run.qcow2").is_file())
        (self.output / "run.qcow2").write_bytes(b"user data")
        self.invoke()
        self.assertEqual((self.output / "run.qcow2").read_bytes(), b"user data")

    def test_rebuilt_base_cannot_silently_reuse_run_overlay(self):
        self.invoke()
        (self.output / "phase2.img").write_bytes(b"changed base")
        self.invoke(success=False)
        self.assertEqual((self.output / "run.qcow2").read_bytes(), b"mock overlay")

    def test_test_rounds_share_disk_but_not_firmware_variables(self):
        first = self.invoke("--mode", "test", "--name", "round1")
        (self.output / "vars-round1.fd").write_bytes(b"firmware changes")
        second = self.invoke("--mode", "test", "--name", "round2")
        first_disk = next(arg for arg in first if "id=root,format=qcow2" in arg)
        second_disk = next(arg for arg in second if "id=root,format=qcow2" in arg)
        self.assertEqual(first_disk, second_disk)
        self.assertEqual((self.output / "vars-round2.fd").read_bytes(), b"test artifact")

    def test_arbitrary_disk_or_mode_rejected(self):
        for args in (("--mode", "bogus"), ("--mode", "test", "--name", "run"),
                     ("--mode", "test", "--name", "round1", "--disk", "/tmp/unrelated"),
                     ("--mode", "negative", "--disk", str(self.output / "test.qcow2")),
                     ("--mode", "test", "--name", "round1", "--vnc", "1"),
                     ("--vnc", "0.0.0.0:1"), ("--nonce", NONCE)):
            with self.subTest(args=args):
                self.invoke(*args, success=False)

    def test_symlink_vars_rejected(self):
        (self.output / "vars-round1.fd").symlink_to(self.output / "ovmf-vars.fd")
        self.invoke("--mode", "test", "--name", "round1", success=False)

    def test_regular_qmp_path_not_deleted(self):
        path = self.output / "qmp-round1.sock"
        path.write_bytes(b"user file")
        self.invoke("--mode", "test", "--name", "round1", success=False)
        self.assertEqual(path.read_bytes(), b"user file")

    def test_old_socket_is_replaced(self):
        path = self.output / "qmp-round1.sock"
        # Only a stale filesystem inode is needed; no listening socket or
        # network permission is involved in this launcher cleanup regression.
        os.mknod(path, stat.S_IFSOCK | 0o600)
        self.invoke("--mode", "test", "--name", "round1")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
