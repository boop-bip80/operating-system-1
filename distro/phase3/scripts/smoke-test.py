#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Phase 3 QEMU acceptance: actual Sway/Wayland UI, HID, storage and failure.

This driver reuses Phase 2's independently regression-tested QMP/PPM transport.
It does not generate screenshots or inject a challenge through serial, disk,
firmware, kernel arguments, or a guest test fixture. A host nonce is created
only after QMP has captured the original desktop framebuffer, then typed into
the real foot terminal opened by clicking the real GTK Dock via emulated HID.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time


PHASE = Path(__file__).resolve().parents[1]
ROOT = PHASE.parents[1]
OUT = ROOT / ".build" / "phase3"
P2_DRIVER = ROOT / "distro" / "phase2" / "scripts" / "smoke-test.py"
P2_SPEC = importlib.util.spec_from_file_location("p3_phase2_transport", P2_DRIVER)
if P2_SPEC is None or P2_SPEC.loader is None:
    raise RuntimeError("Phase 2 QMP transport is missing")
p2 = importlib.util.module_from_spec(P2_SPEC)
sys.modules[P2_SPEC.name] = p2
P2_SPEC.loader.exec_module(p2)

SCREEN_STAGES = ("ready", "preferences", "foot", "saved", "desktop")
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
RELEASE_RE = re.compile(r"[A-Za-z0-9._+\-]+\Z")
FOCUS_RE = re.compile(r"P3:WINDOW_FOCUSED:([1-9][0-9]*)\Z")
SOURCE_RE = re.compile(r"[0-9a-f]{40}\Z")
SCREEN_BOX = (160, 75, 864, 680)
DOCK_X, DOCK_Y = 512, 730
MENU_X, MENU_Y = 150, 20
COMPACT_X, COMPACT_Y = 60, 444
BOOT_MARKERS = (
    "P3:USERSPACE:debian", "P3:ROOT_OK", "P3:DRM_OK",
    "P3:DESKTOP_USER:1000", "P3:SWAY_READY", "P3:LAYER_SHELL_READY",
    "P3:SHELL_READY",
)


class Phase3Failure(RuntimeError):
    """Required independent acceptance evidence is absent or contradictory."""


@dataclass
class BootResult:
    name: str
    output: str = ""
    returncode: int = -1
    duration_seconds: float = 0.0
    timed_out: bool = False
    output_limit: bool = False
    previous_nonce: str | None = None
    nonce: str | None = None
    window_id: str | None = None
    pointer_clicks: int = 0
    preference_toggled: bool = False
    keyboard_sent: bool = False
    exit_sent: bool = False
    powerdown_sent: bool = False
    screenshots: dict = field(default_factory=dict)


def lines(output: str) -> list[str]:
    return p2.serial_lines(output)


def exactly_once(items: list[str], value: str) -> int:
    found = [index for index, line in enumerate(items) if line == value]
    if len(found) != 1:
        raise Phase3Failure(f"expected exactly one complete {value!r} marker")
    return found[0]


def identity(items: list[str], release: str, source: str) -> None:
    src = exactly_once(items, f"P3:SOURCE:{source}")
    kernel = exactly_once(items, f"P3:KERNEL:{release}")
    root = exactly_once(items, "P3:ROOT_OK")
    if max(src, kernel) >= root:
        raise Phase3Failure("kernel/source identity was not established before root checks")
    if not any(re.match(r"(?:\[[\d .]+\]\s*)?Linux version " + re.escape(release) + r"\s", line)
               for line in items):
        raise Phase3Failure("guest did not boot the expected kernel")


def ready(output: str, release: str, source: str, previous: str | None) -> None:
    items = lines(output)
    identity(items, release, source)
    values = (*BOOT_MARKERS, f"P3:PERSISTED:{previous or 'EMPTY'}")
    positions = [exactly_once(items, value) for value in values]
    if positions != sorted(positions):
        # Persistence marker is generated during boot-check, not by the shell;
        # its placement relative to Sway may vary. All other boot markers must
        # remain in their ordered dependency chain.
        if [exactly_once(items, value) for value in BOOT_MARKERS] != sorted(
                exactly_once(items, value) for value in BOOT_MARKERS):
            raise Phase3Failure("shell prerequisites were reported out of order")
    if exactly_once(items, f"P3:PERSISTED:{previous or 'EMPTY'}") >= exactly_once(items, "P3:SHELL_READY"):
        raise Phase3Failure("persistence was not inspected before desktop readiness")
    state = "true" if previous else "false"
    exactly_once(items, f"P3:PREFERENCE_STATE:compact_dock={state}")
    disk = "compact_dock=true:uid=1000" if previous else "compact_dock=absent"
    exactly_once(items, f"P3:PREFERENCE_DISK:{disk}")
    if exactly_once(items, f"P3:PREFERENCE_DISK:{disk}") >= exactly_once(items, "P3:SHELL_READY"):
        raise Phase3Failure("persistent preference was not inspected before starting the GUI")
    if any(item.startswith(("P2:FAIL:", "P3:FAIL:")) for item in items):
        raise Phase3Failure("guest reported failure during positive startup")


def poweroff(result: BootResult, expected_failure: str | None = None) -> int:
    if result.timed_out or result.output_limit or result.returncode != 0:
        raise Phase3Failure("guest timed out, flooded serial output or QEMU failed")
    if p2.KERNEL_FAILURE_RE.search(result.output):
        raise Phase3Failure("kernel panic/oops/error in guest serial log")
    items = lines(result.output)
    failures = [x for x in items if x.startswith(("P2:FAIL:", "P3:FAIL:"))]
    if failures != ([expected_failure] if expected_failure else []):
        raise Phase3Failure(f"unexpected guest failures: {failures!r}")
    ends = [index for index, item in enumerate(items) if p2.POWER_DOWN_RE.fullmatch(item)]
    if len(ends) != 1:
        raise Phase3Failure("missing unique kernel power-down proof")
    return ends[0]


def focus_ids(items: list[str]) -> list[str]:
    return [match.group(1) for item in items if (match := FOCUS_RE.fullmatch(item))]


def validate_positive(result: BootResult, release: str, source: str) -> None:
    stop = poweroff(result)
    ready(result.output, release, source, result.previous_nonce)
    if result.nonce is None or not NONCE_RE.fullmatch(result.nonce):
        raise Phase3Failure("host challenge is absent")
    if result.nonce == result.previous_nonce:
        raise Phase3Failure("the second boot reused the first challenge")
    expected_clicks = 5 if result.previous_nonce is None else 4
    if not all((result.pointer_clicks == expected_clicks,
                result.preference_toggled == (result.previous_nonce is None), result.keyboard_sent,
                result.exit_sent, result.powerdown_sent)):
        raise Phase3Failure("QMP did not perform preferences, Dock, keyboard, exit and shutdown")
    items = lines(result.output)
    if result.previous_nonce is None:
        exactly_once(items, "P3:PREFERENCE_SAVED:compact_dock=true")
    elif any(item.startswith("P3:PREFERENCE_SAVED:") for item in items):
        raise Phase3Failure("second boot unexpectedly rewrote the persisted preference")
    exactly_once(items, "P3:PREFERENCE_VERIFIED:compact_dock=true:uid=1000")
    launched = exactly_once(items, "P3:APP_LAUNCHED:foot")
    keyboard = exactly_once(items, f"P3:KEYBOARD_OK:{result.nonce}")
    focus = focus_ids(items)
    if not focus or result.window_id != focus[0] or len(focus) < 2 or focus[1] != focus[0]:
        raise Phase3Failure("Dock did not focus its observed Sway window twice")
    closed = exactly_once(items, f"P3:WINDOW_CLOSED:{result.window_id}")
    if not (exactly_once(items, "P3:SHELL_READY") < launched < keyboard < closed < stop):
        raise Phase3Failure("window/keyboard/shutdown lifecycle was out of order")
    if len(result.screenshots) != len(SCREEN_STAGES) or set(result.screenshots) != set(SCREEN_STAGES):
        raise Phase3Failure("real graphical stages are missing")
    hashes = set()
    for stage in SCREEN_STAGES:
        proof = result.screenshots[stage]
        if (proof.get("width"), proof.get("height")) != (1024, 768):
            raise Phase3Failure("unexpected QEMU scanout dimensions")
        for key in ("sha256", "png_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(proof.get(key, ""))):
                raise Phase3Failure("invalid screenshot hash")
        hashes.add(proof["sha256"])
    if len(hashes) != len(SCREEN_STAGES):
        raise Phase3Failure("identical stage captures are not proof of UI interactions")


def validate_negative(result: BootResult, release: str, source: str) -> None:
    # Phase3 intentionally retains the Phase2 boot-check service. Its real
    # DRM failure, not a synthetic Phase3 marker, is the expected oracle.
    stop = poweroff(result, "P2:FAIL:NO_DRM")
    items = lines(result.output)
    markers = (f"P2:SOURCE:{source}", f"P2:KERNEL:{release}",
               "P2:USERSPACE:debian", "P2:ROOT_OK", "P2:FAIL:NO_DRM")
    ordered = [exactly_once(items, name) for name in markers]
    if ordered != sorted(ordered) or stop <= ordered[-1]:
        raise Phase3Failure("negative failure was not followed by clean shutdown")
    if not any(re.match(r"(?:\[[\d .]+\]\s*)?Linux version " + re.escape(release) + r"\s", line)
               for line in items):
        raise Phase3Failure("negative boot did not run the expected kernel")
    forbidden = ("P3:DRM_OK", "P3:SWAY_READY", "P3:LAYER_SHELL_READY", "P3:SHELL_READY",
                 "P3:APP_LAUNCHED", "P3:WINDOW_FOCUSED", "P3:KEYBOARD_OK", "P3:WINDOW_CLOSED")
    if any(x.startswith(forbidden) for x in items):
        raise Phase3Failure("negative case reached a desktop or used a fallback renderer")
    if result.nonce or result.keyboard_sent or result.pointer_clicks or result.screenshots:
        raise Phase3Failure("negative case unexpectedly performed graphical acceptance")


def changed_fraction(a: p2.Frame, b: p2.Frame, box=SCREEN_BOX) -> float:
    if (a.width, a.height) != (b.width, b.height):
        raise Phase3Failure("compared frames have different dimensions")
    x1, y1, x2, y2 = box
    different = 0
    count = 0
    for y in range(y1, y2, 2):
        for x in range(x1, x2, 2):
            start = (y * a.width + x) * 3
            before = a.pixels[start:start+3]
            after = b.pixels[start:start+3]
            different += max(abs(before[i]-after[i]) for i in range(3)) > 25
            count += 1
    return different / count


def verify_frame(frame: p2.Frame, stage: str) -> None:
    if (frame.width, frame.height) != (1024, 768):
        raise Phase3Failure("expected native 1024x768 QEMU GPU framebuffer")
    # A uniform or headless placeholder does not qualify as an actual shell.
    sampled = {frame.pixels[index:index+3]
               for index in range(0, len(frame.pixels)-2, 3*67)}
    if len(sampled) < 24:
        raise Phase3Failure("frame has insufficient detail for a real graphical desktop")
    top = {frame.pixels[(y*1024+x)*3:(y*1024+x)*3+3]
           for y in range(3, 38, 3) for x in range(12, 130, 3)}
    dock = {frame.pixels[(y*1024+x)*3:(y*1024+x)*3+3]
            for y in range(690, 762, 3) for x in range(480, 545, 3)}
    if len(top) < 4 or len(dock) < 4:
        raise Phase3Failure(f"{stage} framebuffer does not show detailed topbar and Dock regions")


def click(qmp: p2.QMP, x=DOCK_X, y=DOCK_Y) -> None:
    qmp.execute("input-send-event", {"events": p2.pointer_events(x, y)})
    time.sleep(.1)
    for down in (True, False):
        qmp.execute("input-send-event", {"events": [{"type": "btn", "data": {"down": down, "button": "left"}}]})
        time.sleep(.06)


def text_keys(value: str) -> list[str]:
    if not re.fullmatch(r"[a-z0-9 ]+", value):
        raise ValueError("host text must use only known safe QMP qcodes")
    return ["spc" if char == " " else char for char in value] + ["ret"]


def enter_text(qmp: p2.QMP, value: str) -> None:
    for key in text_keys(value):
        for down in (True, False):
            qmp.execute("input-send-event", {"events": [{"type": "key", "data": {
                "down": down, "key": {"type": "qcode", "data": key},
            }}]})
            time.sleep(.025)


class Phase3Monitor(p2.SerialMonitor):
    def check(self, *, require_running=True):
        super().check(require_running=require_running)
        failures = [line for line in lines(self.output()) if line.startswith(("P2:FAIL:", "P3:FAIL:"))]
        if any(value != self.expected_failure for value in failures):
            raise Phase3Failure(f"unexpected guest failure: {failures!r}")


def capture(qmp: p2.QMP, name: str, stage: str, deadline: float) -> tuple[dict, p2.Frame]:
    path = OUT / "screenshots" / f"{name}-{stage}.ppm"
    png = path.with_suffix(".png")
    p2.safe_file(path, must_exist=False)
    p2.safe_file(png, must_exist=False)
    last_error = "no QMP framebuffer"
    capture_deadline = min(deadline, time.monotonic() + 15)
    while time.monotonic() < capture_deadline:
        qmp.execute("screendump", {"filename": str(path)})
        p2.safe_file(path)
        try:
            frame = p2.parse_ppm(path.read_bytes())
            verify_frame(frame, stage)
        except (p2.SmokeFailure, Phase3Failure) as error:
            last_error = str(error)
            time.sleep(.2)
            continue
        image = p2.png_bytes(frame)
        png.write_bytes(image)
        return ({"path": str(path.relative_to(OUT)), "sha256": p2.digest(path),
                 "png_path": str(png.relative_to(OUT)), "png_sha256": hashlib.sha256(image).hexdigest(),
                 "width": frame.width, "height": frame.height}, frame)
    raise Phase3Failure(f"QEMU {stage} screendump not valid: {last_error}")


def transcript(records: str, nonce: str | None, screenshots: dict, *,
               preference_toggled: bool = False) -> None:
    """Require successful QMP acknowledgements in the exact real-HID sequence."""
    try:
        events = [json.loads(line) for line in records.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise Phase3Failure("invalid QMP JSONL transcript") from error
    if not isinstance(events, list) or len(events) > 10000:
        raise Phase3Failure("unbounded or invalid QMP transcript")
    if nonce is None:
        if events or screenshots:
            raise Phase3Failure("negative test used graphical QMP events")
        return
    requests = []
    pending = None
    greeted = False
    next_id = 1
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("message"), dict):
            raise Phase3Failure("malformed QMP event")
        msg = event["message"]
        if event.get("direction") == "send":
            if not greeted or pending is not None or msg.get("id") != next_id:
                raise Phase3Failure("out of order/unacknowledged QMP request")
            pending = msg
            next_id += 1
        elif event.get("direction") == "receive":
            if "QMP" in msg and not greeted and pending is None:
                greeted = True
            elif "event" in msg and greeted:
                if any(field in msg for field in ("id", "return", "error")):
                    raise Phase3Failure("malformed asynchronous QMP event")
                # Events can legitimately arrive before, during or after
                # command acknowledgements; never mistake them for replies.
                continue
            elif pending is not None and msg.get("id") == pending["id"] and "return" in msg and "error" not in msg:
                requests.append(pending)
                pending = None
            else:
                raise Phase3Failure("unmatched or failed QMP response")
        else:
            raise Phase3Failure("invalid QMP direction")
    if not greeted or pending is not None:
        raise Phase3Failure("QMP transcript incomplete")
    position = 0
    def expect(name: str, args: dict | None = None) -> None:
        nonlocal position
        if position >= len(requests) or requests[position].get("execute") != name or requests[position].get("arguments", {}) != (args or {}):
            raise Phase3Failure(f"QMP transcript missing expected {name} at action {position}")
        position += 1
    def screenshot(stage: str) -> None:
        nonlocal position
        p = OUT / screenshots[stage]["path"]
        seen = 0
        while position < len(requests) and requests[position].get("execute") == "screendump":
            if requests[position].get("arguments") != {"filename": str(p)}:
                raise Phase3Failure("QMP captured an unexpected screenshot path")
            seen += 1
            position += 1
        if not seen:
            raise Phase3Failure(f"missing real QMP {stage} screendump")
    def clicked(x: int, y: int) -> None:
        expect("input-send-event", {"events": p2.pointer_events(x, y)})
        for down in (True, False):
            expect("input-send-event", {"events": [{"type": "btn", "data": {"down": down, "button": "left"}}]})
    def typed(value: str) -> None:
        for key in text_keys(value):
            for down in (True, False):
                expect("input-send-event", {"events": [{"type": "key", "data": {
                    "down": down, "key": {"type": "qcode", "data": key}}}]})
    expect("qmp_capabilities")
    screenshot("ready")
    clicked(MENU_X, MENU_Y)
    if preference_toggled:
        clicked(COMPACT_X, COMPACT_Y)
    screenshot("preferences")
    clicked(MENU_X, MENU_Y)
    clicked(DOCK_X, DOCK_Y)
    screenshot("foot")
    typed("p3save " + nonce)
    screenshot("saved")
    clicked(DOCK_X, DOCK_Y)
    typed("exit")
    screenshot("desktop")
    expect("system_powerdown")
    if position != len(requests):
        raise Phase3Failure("QMP transcript includes unexpected extra commands")


def make_overlay(name: str) -> None:
    if name not in ("test", "negative"):
        raise ValueError("only dedicated test overlays may be recreated")
    path = OUT / f"{name}.qcow2"
    p2.safe_file(path, must_exist=False)
    if path.exists():
        path.unlink()
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                    "-b", str(OUT / "phase3.img"), str(path)], check=True, timeout=30)
    p2.safe_file(path)


def boot(name: str, timeout: float, release: str, source: str,
         previous: str | None = None) -> tuple[BootResult, dict]:
    negative = name == "negative"
    disk = OUT / ("negative.qcow2" if negative else "test.qcow2")
    command = ["bash", str(PHASE / "scripts" / "boot-qemu.sh"), "--mode",
               "negative" if negative else "test", "--name", name, "--disk", str(disk)]
    serial = OUT / "logs" / f"boot-{name}.log"
    qmp_path = OUT / "logs" / f"qmp-{name}.jsonl"
    started = time.monotonic()
    deadline = started + timeout
    result = BootResult(name=name, previous_nonce=previous)
    record = {"name": name, "status": "fail", "command": command,
              "serial_log": str(serial.relative_to(OUT)), "qmp_log": str(qmp_path.relative_to(OUT))}
    process = monitor = client = None
    try:
        p2.safe_file(serial, must_exist=False)
        p2.safe_file(qmp_path, must_exist=False)
        qmp_path.write_text("", encoding="utf-8")
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        monitor = Phase3Monitor(process, serial, deadline, "P2:FAIL:NO_DRM" if negative else None)
        if negative:
            monitor.wait_marker("P2:FAIL:NO_DRM")
        else:
            client = p2.QMP(OUT / f"qmp-{name}.sock", qmp_path, deadline, monitor.check)
            monitor.wait_marker("P3:SHELL_READY")
            # The shell publishes preference state after mapping through the
            # compositor; serial log forwarding can lag the readiness marker.
            monitor.wait_marker(f"P3:PREFERENCE_STATE:compact_dock={'true' if previous else 'false'}")
            ready(monitor.output(), release, source, previous)
            result.screenshots["ready"], first = capture(client, name, "ready", deadline)
            # The random challenge is not provided to the guest until after
            # a real, non-blank native framebuffer has been observed.
            result.nonce = secrets.token_hex(16)
            while result.nonce == previous:
                result.nonce = secrets.token_hex(16)
            click(client, MENU_X, MENU_Y)
            result.pointer_clicks += 1
            if previous is None:
                click(client, COMPACT_X, COMPACT_Y)
                result.pointer_clicks += 1
                result.preference_toggled = True
                monitor.wait_marker("P3:PREFERENCE_SAVED:compact_dock=true")
            result.screenshots["preferences"], preferences = capture(client, name, "preferences", deadline)
            if changed_fraction(first, preferences) < .025:
                raise Phase3Failure("Applications/preferences view did not open visibly")
            click(client, MENU_X, MENU_Y)
            result.pointer_clicks += 1
            click(client)
            result.pointer_clicks += 1
            monitor.wait_marker("P3:APP_LAUNCHED:foot")
            while not focus_ids(lines(monitor.output())):
                monitor.check()
                time.sleep(.05)
            result.window_id = focus_ids(lines(monitor.output()))[0]
            result.screenshots["foot"], opened = capture(client, name, "foot", deadline)
            if changed_fraction(first, opened) < .07:
                raise Phase3Failure("native terminal did not materially change the framebuffer")
            enter_text(client, "p3save " + result.nonce)
            result.keyboard_sent = True
            monitor.wait_marker(f"P3:KEYBOARD_OK:{result.nonce}")
            result.screenshots["saved"], _ = capture(client, name, "saved", deadline)
            click(client)
            result.pointer_clicks += 1
            while focus_ids(lines(monitor.output())).count(result.window_id) < 2:
                monitor.check()
                time.sleep(.05)
            enter_text(client, "exit")
            result.exit_sent = True
            monitor.wait_marker(f"P3:WINDOW_CLOSED:{result.window_id}")
            result.screenshots["desktop"], final = capture(client, name, "desktop", deadline)
            if changed_fraction(opened, final) < .07:
                raise Phase3Failure("foot window did not visually disappear after exit")
            client.execute("system_powerdown")
            result.powerdown_sent = True
        monitor.wait_exit()
        result.output = monitor.output()
        result.returncode = process.returncode
        if negative:
            validate_negative(result, release, source)
        else:
            validate_positive(result, release, source)
        transcript(qmp_path.read_text(encoding="utf-8"), result.nonce, result.screenshots,
                   preference_toggled=result.preference_toggled)
        record["status"] = "pass"
    except (Phase3Failure, p2.SmokeFailure, OSError, subprocess.SubprocessError, ValueError) as error:
        record["error"] = str(error)
    finally:
        if client is not None:
            client.close()
        if process is not None:
            p2.terminate_process(process)
            result.returncode = process.returncode
        if monitor is not None:
            monitor.close()
            result.output = monitor.output()
            result.timed_out = monitor.timed_out
            result.output_limit = monitor.output_limit
        result.duration_seconds = round(time.monotonic() - started, 3)
        record.update({"returncode": result.returncode, "duration_seconds": result.duration_seconds,
                       "timed_out": result.timed_out, "output_limit": result.output_limit,
                       "nonce": result.nonce, "previous_nonce": result.previous_nonce,
                       "window_id": result.window_id, "pointer_clicks": result.pointer_clicks,
                       "preference_toggled": result.preference_toggled,
                       "keyboard_sent": result.keyboard_sent, "exit_sent": result.exit_sent,
                       "powerdown_sent": result.powerdown_sent, "screenshots": result.screenshots})
        for key, path in (("serial_log_sha256", serial), ("qmp_log_sha256", qmp_path)):
            try:
                record[key] = p2.digest(path)
            except (p2.SmokeFailure, OSError) as error:
                record["status"] = "fail"
                record.setdefault("error", str(error))
    return result, record


def save_report(report: dict) -> None:
    path = OUT / "test-results.json"
    p2.safe_file(path, must_exist=False)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if not 20 <= args.timeout <= 1800:
        parser.error("timeout must be 20..1800 seconds")
    report = {"schema_version": 1, "status": "fail", "cases": [],
              "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        for directory in (ROOT / ".build", OUT, OUT / "logs", OUT / "screenshots"):
            p2.safe_directory(directory)
        for file in ("phase3.img", "phase3.qcow2", "ovmf-code.fd", "ovmf-vars.fd",
                     "kernelrelease", "source-revision", "manifest.json"):
            p2.safe_file(OUT / file)
        release = (OUT / "kernelrelease").read_text().strip()
        source = (OUT / "source-revision").read_text().strip()
        if not RELEASE_RE.fullmatch(release) or not SOURCE_RE.fullmatch(source):
            raise Phase3Failure("source/kernel release format invalid or dirty")
        if subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip() != source:
            raise Phase3Failure("built image source and checked-out SHA differ")
        subprocess.run([sys.executable, str(PHASE / "scripts" / "manifest.py"), "check-build"], check=True)
        bound = {OUT / name: p2.digest(OUT / name) for name in (
            "manifest.json", "phase3.img", "phase3.qcow2", "ovmf-code.fd", "ovmf-vars.fd",
            "kernelrelease", "source-revision")}
        report.update({"source_revision": source, "kernel_release": release,
                       "manifest_sha256": bound[OUT / "manifest.json"]})
        make_overlay("test")
        make_overlay("negative")
        previous = None
        for name in ("round1", "round2", "negative"):
            print(f"phase3: boot {name}", flush=True)
            outcome, record = boot(name, args.timeout, release, source,
                                   previous if name == "round2" else None)
            report["cases"].append(record)
            save_report(report)
            if record["status"] != "pass":
                if outcome.output:
                    print(p2.serial_diagnostic_tail(outcome.output), file=sys.stderr)
                raise Phase3Failure(f"{name}: {record.get('error', 'unknown failure')}")
            if name == "round1":
                previous = outcome.nonce
            print(f"phase3: {name} PASS ({outcome.duration_seconds}s)", flush=True)
        p2.validate_unchanged(bound)
        report["status"] = "pass"
        save_report(report)
        print("phase3: native shell/HID/persistence/negative evidence PASS")
        return 0
    except (Phase3Failure, p2.SmokeFailure, OSError, UnicodeError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
        if OUT.is_dir() and OUT.resolve() == OUT and not OUT.is_symlink():
            try:
                save_report(report)
            except (p2.SmokeFailure, OSError):
                pass
        print(f"phase3: FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
