#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Prove disk boot, native Wayland rendering, real input and durable guest storage.

The random challenge is generated only after a verified QEMU screendump. It is
never placed in fw_cfg, a kernel command line, serial stdin or the guest disk by
the host. The guest must receive it through the emulated keyboard, and persist
it only after the emulated pointer clicks the visible button.
"""

from __future__ import annotations

import argparse
import binascii
from collections import Counter
from dataclasses import dataclass, field
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from typing import Callable
import zlib


MAX_LOG_BYTES = 32 * 1024 * 1024
DIAGNOSTIC_TAIL_LINES = 80
DIAGNOSTIC_TAIL_BYTES = 16 * 1024
MAX_QMP_LINE = 1024 * 1024
MAX_SCREEN_BYTES = 4096 * 4096 * 3 + 4096
SOURCE_RE = re.compile(r"[0-9a-f]{40}(?:-dirty)?\Z")
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
KERNEL_FAILURE_RE = re.compile(
    # Serial contains both printk and desktop logs. Match kernel message
    # families at a line boundary, or after printk's numeric uptime prefix even
    # when a userspace write left a partial line. A desktop HH:MM:SS timestamp
    # and libinput's "client bug:" warnings are not kernel BUG reports.
    # No trailing newline is required: the live monitor scans partial writes.
    r"(?:(?:^|[\r\n])[ \t]*(?:<\d+>[ \t]*)?|"
    r"(?:<\d+>[ \t]*)?\[[ \t]*\d+(?:\.\d+)?\][ \t]*)"
    r"(?:Kernel panic\b|Oops:|BUG:|kernel BUG at\b|"
    r"(?:NMI )?watchdog:[ \t]+BUG:|No working init found\b|"
    r"EXT4-fs[^\r\n]*(?:\berror\b|corrupt|abort))", re.IGNORECASE,
)
POWER_DOWN_RE = re.compile(r"^(?:\[[\d .]+\]\s*)?reboot:\s*Power down\.?\s*$")
BOOT_MARKERS = (
    "P2:USERSPACE:debian", "P2:ROOT_OK", "P2:PACKAGE_OK", "P2:NETWORK_OK",
    "P2:DRM_OK", "P2:INPUT_OK", "P2:DESKTOP_USER:1000", "P2:WESTON_READY",
    "P2:TERMINAL_READY", "P2:WAYLAND_OK",
)
SCREEN_COLORS = {
    "ready": (28, 82, 120),
    "keyboard": (28, 120, 80),
    "pointer": (36, 134, 87),
}
SCREEN_STAGES = ("ready", "keyboard", "pointer", "desktop")


class SmokeFailure(RuntimeError):
    """The acceptance evidence is missing, contradictory, or unsafe to collect."""


@dataclass
class BootResult:
    name: str
    output: str = ""
    returncode: int = -1
    duration_seconds: float = 0.0
    timed_out: bool = False
    output_limit: bool = False
    nonce: str | None = None
    previous_nonce: str | None = None
    keyboard_sent: bool = False
    pointer_sent: bool = False
    escape_sent: bool = False
    powerdown_sent: bool = False
    screenshots: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    pixels: bytes


def serial_lines(output: str) -> list[str]:
    """Only complete, exact lines count; echoed commands and partial writes do not."""
    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")[:-1]


def require_once(lines: list[str], marker: str) -> int:
    positions = [index for index, line in enumerate(lines) if line == marker]
    if len(positions) != 1:
        raise SmokeFailure(f"expected one complete {marker!r} line, got {len(positions)}")
    return positions[0]


def require_prefix(lines: list[str], prefix: str, value: str) -> int:
    actual = [line for line in lines if line.startswith(prefix)]
    if actual != [prefix + value]:
        raise SmokeFailure(f"unexpected {prefix} evidence: {actual!r}")
    return lines.index(prefix + value)


def validate_identity(lines: list[str], release: str, source: str) -> None:
    source_index = require_prefix(lines, "P2:SOURCE:", source)
    kernel_index = require_prefix(lines, "P2:KERNEL:", release)
    root_index = require_once(lines, "P2:ROOT_OK")
    if max(source_index, kernel_index) > root_index:
        raise SmokeFailure("guest identity was not established before root validation")
    if not any(re.match(r"(?:\[[\d .]+\]\s*)?Linux version " + re.escape(release) + r"\s", line)
               for line in lines):
        raise SmokeFailure("the expected kernel did not identify itself during boot")


def validate_ready(output: str, release: str, source: str, previous_nonce: str | None) -> None:
    lines = serial_lines(output)
    validate_identity(lines, release, source)
    require_prefix(lines, "P2:USERSPACE:", "debian")
    require_prefix(lines, "P2:DESKTOP_USER:", "1000")
    require_prefix(lines, "P2:PERSISTED:", previous_nonce or "EMPTY")
    markers = (*BOOT_MARKERS, f"P2:PERSISTED:{previous_nonce or 'EMPTY'}", "P2:PROBE_READY")
    positions = [require_once(lines, marker) for marker in markers]
    if positions != sorted(positions):
        raise SmokeFailure("guest startup/persistence markers are out of order")


def validate_clean_exit(result: BootResult, expected_failure: str | None = None) -> int:
    if result.timed_out:
        raise SmokeFailure("guest exceeded its boot deadline")
    if result.output_limit:
        raise SmokeFailure("guest serial output exceeded the safety limit")
    if result.returncode != 0:
        raise SmokeFailure(f"QEMU exited with status {result.returncode}")
    if KERNEL_FAILURE_RE.search(result.output):
        raise SmokeFailure("a kernel failure appeared in the serial log")
    lines = serial_lines(result.output)
    failures = [line for line in lines if line.startswith("P2:FAIL:")]
    expected = [expected_failure] if expected_failure else []
    if failures != expected:
        raise SmokeFailure(f"unexpected guest failure evidence: {failures!r}")
    positions = [index for index, line in enumerate(lines) if POWER_DOWN_RE.fullmatch(line)]
    if len(positions) != 1:
        raise SmokeFailure("expected exactly one kernel power-down line")
    return positions[0]


def validate_test_boot(result: BootResult, release: str, source: str) -> None:
    power_down = validate_clean_exit(result)
    if result.nonce is None or NONCE_RE.fullmatch(result.nonce) is None:
        raise SmokeFailure("missing host-generated input challenge")
    if result.nonce == result.previous_nonce:
        raise SmokeFailure("the second boot reused its previous input challenge")
    if not all((result.keyboard_sent, result.pointer_sent, result.escape_sent, result.powerdown_sent)):
        raise SmokeFailure("host did not complete the real QMP input/shutdown sequence")
    validate_ready(result.output, release, source, result.previous_nonce)
    lines = serial_lines(result.output)
    require_prefix(lines, "P2:KEYBOARD_OK:", result.nonce)
    require_prefix(lines, "P2:POINTER_OK:", result.nonce)
    positions = [require_once(lines, marker) for marker in (
        "P2:PROBE_READY", f"P2:KEYBOARD_OK:{result.nonce}",
        f"P2:POINTER_OK:{result.nonce}", "P2:PROBE_CLOSED",
    )]
    if positions != sorted(positions) or power_down <= positions[-1]:
        raise SmokeFailure("input, close and shutdown evidence is out of order")
    if set(result.screenshots) != set(SCREEN_STAGES):
        raise SmokeFailure("one or more graphical evidence stages are missing")
    hashes = []
    for stage in SCREEN_STAGES:
        evidence = result.screenshots[stage]
        if (evidence.get("width"), evidence.get("height")) != (1024, 768):
            raise SmokeFailure(f"invalid {stage} screenshot dimensions")
        for key in ("sha256", "png_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", evidence.get(key, "")) is None:
                raise SmokeFailure(f"invalid {stage} screenshot digest")
        hashes.append(evidence["sha256"])
    if len(set(hashes)) != len(hashes):
        raise SmokeFailure("graphical stages did not produce distinct framebuffer evidence")


def validate_negative_boot(result: BootResult, release: str, source: str) -> None:
    power_down = validate_clean_exit(result, "P2:FAIL:NO_DRM")
    lines = serial_lines(result.output)
    validate_identity(lines, release, source)
    markers = (*BOOT_MARKERS[:4], "P2:FAIL:NO_DRM")
    positions = [require_once(lines, marker) for marker in markers]
    if positions != sorted(positions) or power_down <= positions[-1]:
        raise SmokeFailure("missing-GPU failure was not detected before clean shutdown")
    forbidden = ("P2:DRM_OK", "P2:INPUT_OK", "P2:DESKTOP_USER:", "P2:WESTON_READY",
                 "P2:TERMINAL_READY", "P2:WAYLAND_OK", "P2:PERSISTED:", "P2:PROBE_",
                 "P2:KEYBOARD_OK:", "P2:POINTER_OK:")
    if any(line.startswith(forbidden) for line in lines):
        raise SmokeFailure("the missing-GPU control unexpectedly reached the desktop")
    if result.keyboard_sent or result.pointer_sent or result.nonce or result.screenshots:
        raise SmokeFailure("negative control unexpectedly received graphical input")


def parse_ppm(data: bytes) -> Frame:
    """Parse the binary P6 format without eating whitespace-valued first pixels."""
    if len(data) > MAX_SCREEN_BYTES:
        raise SmokeFailure("screendump exceeds the image safety limit")
    position = 0

    def token() -> bytes:
        nonlocal position
        while position < len(data):
            if data[position] in b" \t\r\n":
                position += 1
            elif data[position] == ord("#"):
                newline = data.find(b"\n", position)
                if newline < 0:
                    raise SmokeFailure("unterminated PPM comment")
                position = newline + 1
            else:
                break
        start = position
        while position < len(data) and data[position] not in b" \t\r\n#":
            position += 1
        if start == position or position - start > 32:
            raise SmokeFailure("invalid PPM header token")
        return data[start:position]

    if token() != b"P6":
        raise SmokeFailure("QEMU screendump is not binary RGB PPM")
    try:
        width, height, maximum = int(token()), int(token()), int(token())
    except ValueError as error:
        raise SmokeFailure("PPM dimensions are not integers") from error
    if not (1 <= width <= 4096 and 1 <= height <= 4096) or maximum != 255:
        raise SmokeFailure("PPM dimensions or channel depth are unsupported")
    if position >= len(data) or data[position] not in b" \t\r\n":
        raise SmokeFailure("PPM header lacks its raster separator")
    if data[position:position + 2] == b"\r\n":
        position += 2
    else:
        position += 1
    pixels = data[position:]
    if len(pixels) != width * height * 3:
        raise SmokeFailure("PPM raster is truncated or contains trailing bytes")
    return Frame(width, height, pixels)


def color_fraction(frame: Frame, color: tuple[int, int, int]) -> float:
    # Ignore the anti-aliased edge and leave room for human-readable status text.
    target = bytes(color)
    matches = 0
    total = 0
    for y in range(116, 304):
        row = frame.pixels[(y * frame.width + 96) * 3:(y * frame.width + 928) * 3]
        for x in range(0, len(row), 3):
            matches += row[x:x + 3] == target
            total += 1
    return matches / total if total else 0.0


def validate_frame(frame: Frame, stage: str) -> None:
    if (frame.width, frame.height) != (1024, 768):
        raise SmokeFailure(f"expected 1024x768 scanout, got {frame.width}x{frame.height}")
    # Sample complete RGB pixels, not byte offsets that shift between channels.
    samples = [frame.pixels[index:index + 3] for index in range(0, len(frame.pixels), 3 * 31)]
    colors = Counter(samples)
    if len(colors) < 12 or max(colors.values()) / len(samples) > 0.95:
        raise SmokeFailure("screendump is blank or lacks desktop/probe detail")
    brightness = [sum(pixel) / 3 for pixel in colors]
    if max(brightness) - min(brightness) < 100:
        raise SmokeFailure("screendump lacks visible foreground/background contrast")
    if stage in SCREEN_COLORS:
        if color_fraction(frame, SCREEN_COLORS[stage]) < 0.70:
            raise SmokeFailure(f"screendump does not show the {stage} input state")
    elif stage == "desktop":
        if any(color_fraction(frame, color) > 0.10 for color in SCREEN_COLORS.values()):
            raise SmokeFailure("the fullscreen probe is still covering the desktop")
    else:
        raise ValueError(f"unknown screenshot stage: {stage}")


def png_bytes(frame: Frame) -> bytes:
    """Provide lossless review previews without a third-party imaging dependency."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", binascii.crc32(kind + payload) & 0xffffffff))

    row_bytes = frame.width * 3
    rows = b"".join(b"\0" + frame.pixels[offset:offset + row_bytes]
                    for offset in range(0, len(frame.pixels), row_bytes))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", frame.width, frame.height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def safe_file(path: Path, *, must_exist: bool = True) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise SmokeFailure(f"required artifact is missing: {path}")
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SmokeFailure(f"refusing non-regular, symlinked or hard-linked output: {path}")


def safe_directory(path: Path) -> None:
    if path.resolve() != path or path.is_symlink() or (path.exists() and not path.is_dir()):
        raise SmokeFailure(f"refusing unsafe output directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.resolve() != path:
        raise SmokeFailure(f"output directory traverses a symlink: {path}")


def digest(path: Path) -> str:
    safe_file(path)
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def validate_unchanged(paths: dict[Path, str]) -> None:
    for path, expected in paths.items():
        if digest(path) != expected:
            raise SmokeFailure(f"build artifact changed during the boot suite: {path.name}")


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


class SerialMonitor:
    """Drain serial continuously while QMP commands wait, so QEMU cannot deadlock."""

    def __init__(self, process: subprocess.Popen[bytes], log_path: Path, deadline: float,
                 expected_failure: str | None = None):
        self.process = process
        self.deadline = deadline
        self.expected_failure = expected_failure
        self.data = bytearray()
        self.lock = threading.Lock()
        self.output_limit = False
        self.timed_out = False
        self.read_error: str | None = None
        safe_file(log_path, must_exist=False)
        self.log = log_path.open("wb")
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            assert self.process.stdout is not None
            while True:
                block = os.read(self.process.stdout.fileno(), 65536)
                if not block:
                    break
                with self.lock:
                    room = MAX_LOG_BYTES - len(self.data)
                    self.data.extend(block[:room])
                    self.log.write(block[:room])
                    self.log.flush()
                    if len(block) > room:
                        self.output_limit = True
                        break
        except (OSError, ValueError) as error:
            self.read_error = str(error)

    def output(self) -> str:
        with self.lock:
            return bytes(self.data).decode("utf-8", errors="replace")

    def check(self, *, require_running: bool = True) -> None:
        if self.output_limit:
            raise SmokeFailure("guest serial output exceeded the safety limit")
        if self.read_error:
            raise SmokeFailure(f"serial collection failed: {self.read_error}")
        if time.monotonic() >= self.deadline:
            self.timed_out = True
            raise SmokeFailure("guest exceeded its boot deadline")
        output = self.output()
        if KERNEL_FAILURE_RE.search(output):
            raise SmokeFailure("kernel failure during boot (see serial log)")
        failures = [line for line in serial_lines(output) if line.startswith("P2:FAIL:")]
        if any(line != self.expected_failure for line in failures):
            raise SmokeFailure(f"guest reported failure: {failures!r}")
        if require_running and self.process.poll() is not None:
            raise SmokeFailure(f"QEMU exited early with status {self.process.returncode}")

    def wait_marker(self, marker: str) -> None:
        while True:
            self.check(require_running=False)
            if marker in serial_lines(self.output()):
                return
            if self.process.poll() is not None:
                self.thread.join(timeout=1)
                if marker in serial_lines(self.output()):
                    return
                raise SmokeFailure(f"QEMU exited before {marker}")
            time.sleep(0.05)

    def wait_exit(self) -> None:
        while self.process.poll() is None:
            self.check(require_running=False)
            time.sleep(0.05)
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise SmokeFailure("serial pipe did not close after QEMU exited")
        self.check(require_running=False)

    def close(self) -> None:
        self.thread.join(timeout=2)
        if not self.thread.is_alive():
            self.log.close()
            if self.process.stdout is not None:
                self.process.stdout.close()


class QMP:
    def __init__(self, path: Path, log_path: Path, deadline: float, health: Callable[[], None]):
        self.deadline = deadline
        self.health = health
        self.buffer = bytearray()
        self.sequence = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(0.5)
        safe_file(log_path, must_exist=False)
        self.log = log_path.open("w", encoding="utf-8")
        try:
            while True:
                self._check()
                try:
                    self.sock.connect(str(path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.05)
            greeting = self._receive()
            if "QMP" not in greeting or not isinstance(greeting["QMP"], dict):
                raise SmokeFailure("invalid QMP greeting")
            self.execute("qmp_capabilities")
        except BaseException:
            self.close()
            raise

    def _check(self) -> None:
        self.health()
        if time.monotonic() >= self.deadline:
            raise SmokeFailure("QMP exceeded the guest deadline")

    def _record(self, direction: str, value: dict) -> None:
        self.log.write(json.dumps({"direction": direction, "message": value}, sort_keys=True) + "\n")
        self.log.flush()

    def _receive(self) -> dict:
        while True:
            self._check()
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                if len(line) > MAX_QMP_LINE:
                    raise SmokeFailure("QMP response exceeded the safety limit")
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeDecodeError) as error:
                    raise SmokeFailure("invalid QMP JSON response") from error
                if not isinstance(value, dict):
                    raise SmokeFailure("QMP response is not an object")
                self._record("receive", value)
                return value
            if len(self.buffer) > MAX_QMP_LINE:
                raise SmokeFailure("QMP response exceeded the safety limit")
            try:
                block = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not block:
                raise SmokeFailure("QMP connection closed before its response")
            self.buffer.extend(block)

    def execute(self, command: str, arguments: dict | None = None):
        self._check()
        self.sequence += 1
        request = {"execute": command, "id": self.sequence}
        if arguments is not None:
            request["arguments"] = arguments
        self._record("send", request)
        self.sock.sendall(json.dumps(request).encode("utf-8") + b"\r\n")
        while True:
            response = self._receive()
            if "event" in response:
                continue
            if response.get("id") != self.sequence:
                raise SmokeFailure("QMP response has a mismatched request id")
            if "error" in response:
                raise SmokeFailure(f"QMP {command} failed: {response['error']!r}")
            if "return" not in response:
                raise SmokeFailure("QMP response has neither return nor error")
            return response["return"]

    def close(self) -> None:
        self.sock.close()
        self.log.close()


def key_events(key: str) -> list[dict]:
    if not (len(key) == 1 and key in "0123456789abcdef") and key not in ("ret", "esc"):
        raise ValueError("only challenge, Enter and Escape keys may be injected")
    return [{"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": key}}}
            for down in (True, False)]


def send_key(qmp: QMP, key: str) -> None:
    for event in key_events(key):
        qmp.execute("input-send-event", {"events": [event]})
        time.sleep(0.025)


def send_challenge(qmp: QMP, nonce: str) -> None:
    if NONCE_RE.fullmatch(nonce) is None:
        raise ValueError("challenge must be 32 lowercase hexadecimal characters")
    for character in nonce:
        send_key(qmp, character)
    send_key(qmp, "ret")


def pointer_events(x: int = 512, y: int = 490) -> list[dict]:
    if not (0 <= x < 1024 and 0 <= y < 768):
        raise ValueError("pointer coordinates exceed the verified scanout")
    return [
        {"type": "abs", "data": {"axis": "x", "value": round(x * 32767 / 1023)}},
        {"type": "abs", "data": {"axis": "y", "value": round(y * 32767 / 767)}},
    ]


def click_button(qmp: QMP) -> None:
    qmp.execute("input-send-event", {"events": pointer_events()})
    time.sleep(0.1)
    for down in (True, False):
        qmp.execute("input-send-event", {"events": [
            {"type": "btn", "data": {"down": down, "button": "left"}},
        ]})
        time.sleep(0.05)


def validate_qmp_log(records: str | list[dict], nonce: str | None, screenshots: dict) -> None:
    """Replay acknowledged host actions; ignore unrelated asynchronous QMP events.

    This is also called by provenance verification after hashes are checked, so
    manually changing action booleans in the result cannot replace input proof.
    Screenshot retries are allowed, but never before/after the wrong input phase.
    """
    if isinstance(records, str):
        if len(records) > MAX_LOG_BYTES:
            raise SmokeFailure("QMP transcript exceeds the safety limit")
        try:
            records = [json.loads(line) for line in records.splitlines() if line.strip()]
        except ValueError as error:
            raise SmokeFailure("invalid QMP transcript JSON") from error
    if not isinstance(records, list) or len(records) > 8192:
        raise SmokeFailure("invalid QMP transcript record set")
    if nonce is None:
        if records or screenshots:
            raise SmokeFailure("negative control unexpectedly used graphical QMP commands")
        return
    if NONCE_RE.fullmatch(nonce) is None or set(screenshots) != set(SCREEN_STAGES):
        raise SmokeFailure("QMP transcript lacks challenge or screenshot context")
    commands = []
    pending = None
    greeting = False
    last_id = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("message"), dict):
            raise SmokeFailure("invalid QMP transcript record")
        message = record["message"]
        direction = record.get("direction")
        if direction == "send":
            if not greeting or pending is not None:
                raise SmokeFailure("QMP transcript sent a command before greeting/acknowledgement")
            if (type(message.get("id")) is not int or message["id"] != last_id + 1 or
                    set(message) - {"id", "execute", "arguments"} or
                    not isinstance(message.get("execute"), str) or
                    not isinstance(message.get("arguments", {}), dict)):
                raise SmokeFailure("invalid QMP transcript request")
            last_id = message["id"]
            pending = message
        elif direction == "receive":
            if "event" in message:
                if "id" in message or "return" in message or "error" in message:
                    raise SmokeFailure("malformed asynchronous QMP event")
                continue
            if "QMP" in message:
                if greeting or pending is not None or commands or not isinstance(message["QMP"], dict):
                    raise SmokeFailure("invalid or repeated QMP transcript greeting")
                greeting = True
                continue
            if (pending is None or message.get("id") != pending["id"] or
                    "return" not in message or "error" in message):
                raise SmokeFailure("QMP transcript lacks a successful matching response")
            commands.append(pending)
            pending = None
        else:
            raise SmokeFailure("invalid QMP transcript direction")
    if pending is not None or not greeting:
        raise SmokeFailure("QMP transcript ended without acknowledgement")
    position = 0
    screenshot_root = None

    def command(name: str, arguments: dict | None = None) -> None:
        nonlocal position
        if position >= len(commands):
            raise SmokeFailure(f"QMP transcript is missing {name}")
        request = commands[position]
        if request["execute"] != name or request.get("arguments", {}) != (arguments or {}):
            raise SmokeFailure(f"QMP transcript has wrong command/input while expecting {name}")
        position += 1

    def screen(stage: str) -> None:
        nonlocal position, screenshot_root
        relative = Path(screenshots[stage].get("path", ""))
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "screenshots" or ".." in relative.parts:
            raise SmokeFailure("unsafe QMP screenshot evidence path")
        captured = False
        while position < len(commands) and commands[position]["execute"] == "screendump":
            arguments = commands[position].get("arguments", {})
            filename = arguments.get("filename")
            if not isinstance(filename, str) or set(arguments) != {"filename"}:
                raise SmokeFailure("invalid QMP screendump request")
            path = Path(filename)
            if not path.is_absolute() or path.parts[-2:] != relative.parts:
                raise SmokeFailure(f"QMP transcript captured the wrong {stage} screenshot")
            if screenshot_root is None:
                screenshot_root = path.parent.parent
            elif path.parent.parent != screenshot_root:
                raise SmokeFailure("QMP screenshots changed output directories")
            position += 1
            captured = True
        if not captured:
            raise SmokeFailure(f"QMP transcript is missing the {stage} screendump")

    command("qmp_capabilities")
    screen("ready")
    for key in list(nonce) + ["ret"]:
        for event in key_events(key):
            command("input-send-event", {"events": [event]})
    screen("keyboard")
    command("input-send-event", {"events": pointer_events()})
    for down in (True, False):
        command("input-send-event", {"events": [
            {"type": "btn", "data": {"down": down, "button": "left"}},
        ]})
    screen("pointer")
    for event in key_events("esc"):
        command("input-send-event", {"events": [event]})
    screen("desktop")
    command("system_powerdown")
    if position != len(commands):
        raise SmokeFailure("QMP transcript contains unexpected extra commands")


def capture(qmp: QMP, output_dir: Path, name: str, stage: str, deadline: float) -> dict:
    path = output_dir / "screenshots" / f"{name}-{stage}.ppm"
    preview = path.with_suffix(".png")
    last_error = "no screendump"
    # A serial event can be flushed before the compositor's next page flip.
    # Observe that page flip, but do not turn an indefinitely stale frame green.
    visual_deadline = min(deadline, time.monotonic() + 20)
    while time.monotonic() < visual_deadline:
        safe_file(path, must_exist=False)
        qmp.execute("screendump", {"filename": str(path)})
        safe_file(path)
        if path.stat().st_size > MAX_SCREEN_BYTES:
            raise SmokeFailure("QEMU screendump exceeds the image safety limit")
        raw = path.read_bytes()
        try:
            frame = parse_ppm(raw)
            validate_frame(frame, stage)
        except SmokeFailure as error:
            last_error = str(error)
            time.sleep(0.15)
            continue
        safe_file(preview, must_exist=False)
        png = png_bytes(frame)
        preview.write_bytes(png)
        return {"path": str(path.relative_to(output_dir)), "sha256": hashlib.sha256(raw).hexdigest(),
                "png_path": str(preview.relative_to(output_dir)), "png_sha256": hashlib.sha256(png).hexdigest(),
                "width": frame.width, "height": frame.height}
    raise SmokeFailure(f"{stage} screenshot did not become valid: {last_error}")


def boot(output_dir: Path, launcher: Path, name: str, timeout: float, release: str,
         source: str, previous_nonce: str | None = None) -> tuple[BootResult, dict]:
    negative = name == "negative"
    mode = "negative" if negative else "test"
    disk = output_dir / ("negative.qcow2" if negative else "test.qcow2")
    command = ["bash", str(launcher), "--mode", mode, "--name", name, "--disk", str(disk)]
    serial_path = output_dir / "logs" / f"boot-{name}.log"
    qmp_path = output_dir / "logs" / f"qmp-{name}.jsonl"
    started = time.monotonic()
    deadline = started + timeout
    result = BootResult(name=name, previous_nonce=previous_nonce)
    record = {"name": name, "status": "fail", "command": command,
              "serial_log": str(serial_path.relative_to(output_dir)),
              "qmp_log": str(qmp_path.relative_to(output_dir))}
    monitor = None
    qmp = None
    process = None
    try:
        safe_file(serial_path, must_exist=False)
        safe_file(qmp_path, must_exist=False)
        qmp_path.write_text("", encoding="utf-8")
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        monitor = SerialMonitor(process, serial_path, deadline, "P2:FAIL:NO_DRM" if negative else None)
        if negative:
            monitor.wait_marker("P2:FAIL:NO_DRM")
        else:
            qmp = QMP(output_dir / f"qmp-{name}.sock", qmp_path, deadline, monitor.check)
            monitor.wait_marker("P2:PROBE_READY")
            validate_ready(monitor.output(), release, source, previous_nonce)
            result.screenshots["ready"] = capture(qmp, output_dir, name, "ready", deadline)
            result.nonce = secrets.token_hex(16)
            while result.nonce == previous_nonce:
                result.nonce = secrets.token_hex(16)
            send_challenge(qmp, result.nonce)
            result.keyboard_sent = True
            monitor.wait_marker(f"P2:KEYBOARD_OK:{result.nonce}")
            result.screenshots["keyboard"] = capture(qmp, output_dir, name, "keyboard", deadline)
            click_button(qmp)
            result.pointer_sent = True
            monitor.wait_marker(f"P2:POINTER_OK:{result.nonce}")
            result.screenshots["pointer"] = capture(qmp, output_dir, name, "pointer", deadline)
            send_key(qmp, "esc")
            result.escape_sent = True
            monitor.wait_marker("P2:PROBE_CLOSED")
            result.screenshots["desktop"] = capture(qmp, output_dir, name, "desktop", deadline)
            qmp.execute("system_powerdown")
            result.powerdown_sent = True
        monitor.wait_exit()
        result.output = monitor.output()
        result.returncode = process.returncode
        if negative:
            validate_negative_boot(result, release, source)
        else:
            validate_test_boot(result, release, source)
        validate_qmp_log(qmp_path.read_text(encoding="utf-8"), result.nonce, result.screenshots)
        record["status"] = "pass"
    except (SmokeFailure, OSError, subprocess.SubprocessError) as error:
        record["error"] = str(error)
    finally:
        if qmp is not None:
            qmp.close()
        if process is not None:
            terminate_process(process)
            result.returncode = process.returncode
        if monitor is not None:
            monitor.close()
            result.output = monitor.output()
            result.timed_out = monitor.timed_out
            result.output_limit = monitor.output_limit
        result.duration_seconds = round(time.monotonic() - started, 3)
        record.update({"returncode": result.returncode, "duration_seconds": result.duration_seconds,
                       "timed_out": result.timed_out, "output_limit": result.output_limit,
                       "nonce": result.nonce, "previous_nonce": previous_nonce,
                       "keyboard_sent": result.keyboard_sent, "pointer_sent": result.pointer_sent,
                       "escape_sent": result.escape_sent, "powerdown_sent": result.powerdown_sent,
                       "screenshots": result.screenshots})
        for field_name, path in (("serial_log_sha256", serial_path), ("qmp_log_sha256", qmp_path)):
            try:
                record[field_name] = digest(path)
            except (SmokeFailure, OSError) as error:
                record["status"] = "fail"
                record.setdefault("error", str(error))
    return result, record


def make_overlay(output_dir: Path, name: str) -> None:
    if name not in ("test", "negative"):
        raise ValueError("only disposable test overlays may be replaced")
    path = output_dir / f"{name}.qcow2"
    safe_file(path, must_exist=False)
    if path.exists():
        path.unlink()
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                    "-b", str(output_dir / "phase2.img"), str(path)], check=True, timeout=30)
    safe_file(path)


def write_report(path: Path, report: dict) -> None:
    safe_file(path, must_exist=False)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def serial_diagnostic_tail(output: str) -> str:
    """Return bounded, prefixed diagnostics without modifying acceptance evidence."""
    lines = output.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    prefix = "phase2: guest | "
    # Escape terminal controls and prefix every line so guest output cannot be
    # mistaken for a host verdict or a CI workflow command.
    lines = ["".join(character if character.isprintable() or character == "\t"
                     else f"\\x{ord(character):02x}" for character in line)
             for line in lines[-DIAGNOSTIC_TAIL_LINES:]]
    text = "\n".join(prefix + line for line in lines) if lines else prefix + "(no serial output captured)"
    encoded = text.encode("utf-8")
    budget = DIAGNOSTIC_TAIL_BYTES - 1  # Include print's terminating newline.
    if len(encoded) > budget:
        truncated = prefix + "[earlier bytes truncated] "
        text = truncated + encoded[-(budget - len(truncated.encode("utf-8"))):].decode("utf-8", errors="ignore")
    return text


def print_failure_diagnostics(result: BootResult, serial_log: str) -> None:
    print(f"phase2: diagnostic serial tail for {result.name} "
          f"(last {DIAGNOSTIC_TAIL_LINES} lines, at most {DIAGNOSTIC_TAIL_BYTES} bytes; "
          f"full evidence unchanged: {serial_log})", file=sys.stderr)
    print(serial_diagnostic_tail(result.output), file=sys.stderr)
    print("phase2: end diagnostic serial tail", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300, help="deadline per VM boot (seconds)")
    args = parser.parse_args()
    if not (10 <= args.timeout <= 1800):
        parser.error("--timeout must be between 10 and 1800 seconds")
    phase_dir = Path(__file__).resolve().parents[1]
    repo = phase_dir.parents[1]
    output_dir = repo / ".build" / "phase2"
    report = {"schema_version": 1, "status": "fail", "cases": [],
              "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    report_path = output_dir / "test-results.json"
    try:
        for directory in (repo / ".build", output_dir, output_dir / "logs", output_dir / "screenshots"):
            safe_directory(directory)
        for name in ("phase2.img", "ovmf-code.fd", "ovmf-vars.fd", "kernelrelease", "source-revision", "manifest.json"):
            safe_file(output_dir / name)
        release = (output_dir / "kernelrelease").read_text(encoding="ascii").strip()
        source = (output_dir / "source-revision").read_text(encoding="ascii").strip()
        if re.fullmatch(r"[A-Za-z0-9._+\-]+", release) is None or SOURCE_RE.fullmatch(source) is None:
            raise SmokeFailure("invalid source or kernel identity metadata")
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        if source.removesuffix("-dirty") != head:
            raise SmokeFailure("built image source identity does not match the current checkout")
        subprocess.run([sys.executable, str(phase_dir / "scripts" / "manifest.py"), "check-build"], check=True)
        bound = {output_dir / name: digest(output_dir / name) for name in (
            "manifest.json", "phase2.img", "ovmf-code.fd", "ovmf-vars.fd", "kernelrelease", "source-revision",
        )}
        report.update({"source_revision": source, "kernel_release": release,
                       "manifest_sha256": bound[output_dir / "manifest.json"]})
        make_overlay(output_dir, "test")
        make_overlay(output_dir, "negative")
        previous_nonce = None
        for name in ("round1", "round2", "negative"):
            print(f"phase2: booting {name} (deadline {args.timeout:g}s)", flush=True)
            result, record = boot(output_dir, phase_dir / "scripts" / "boot-qemu.sh", name,
                                  args.timeout, release, source, previous_nonce if name == "round2" else None)
            report["cases"].append(record)
            write_report(report_path, report)
            if record["status"] != "pass":
                print_failure_diagnostics(result, record["serial_log"])
                raise SmokeFailure(f"{name}: {record.get('error', 'unknown failure')}; see {record['serial_log']}")
            if name == "round1":
                previous_nonce = result.nonce
            print(f"phase2: {name} PASS ({result.duration_seconds:.1f}s)", flush=True)
        validate_unchanged(bound)
        report["status"] = "pass"
        write_report(report_path, report)
        print(f"phase2: graphical input/persistence/negative-control suite PASS: {report_path}")
        return 0
    except (SmokeFailure, OSError, UnicodeError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
        if output_dir.is_dir() and not output_dir.is_symlink() and output_dir.resolve() == output_dir:
            try:
                write_report(report_path, report)
            except (SmokeFailure, OSError):
                pass
        print(f"phase2: FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
