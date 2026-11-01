#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Phase 3 QEMU acceptance: actual Sway/Wayland UI, HID, storage and fail-closed controls.

This driver reuses Phase 2's independently regression-tested QMP/PPM transport.
It does not generate screenshots or inject a challenge through serial, disk,
firmware, kernel arguments, or a guest test fixture. A host nonce is created
only after QMP has captured the original desktop framebuffer, then typed into
the real foot terminal opened from the original GTK launcher via emulated HID.
A second real foot client proves Dock focus switching and honest scratchpad
hide/restore/kill behavior using compositor IPC and QMP scanout observations.
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

SCREEN_STAGES = (
    "ready", "launcher", "preferences", "foot", "saved", "second", "focused",
    "menu_minimize", "hidden", "restored", "menu_close", "closed", "desktop",
)
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
RELEASE_RE = re.compile(r"[A-Za-z0-9._+\-]+\Z")
FOCUS_RE = re.compile(r"P3:WINDOW_FOCUSED:([1-9][0-9]*)\Z")
DISCOVERED_RE = re.compile(r"P3:WINDOW_DISCOVERED:([1-9][0-9]*):foot\Z")
ACTIVATED_RE = re.compile(r"P3:WINDOW_ACTIVATED:([1-9][0-9]*)\Z")
GTK_TARGET_RE = {name: re.compile(r"P3:" + name + r"_TARGET:([0-9]+):([0-9]+):([0-9]+):([0-9]+)\Z")
                 for name in ("COMPACT", "TERMINAL")}
IPC_PREFIX = "P3:IPC_TREE:"
LAYOUT_PREFIX = "P3:IPC_LAYOUT:"
SOURCE_RE = re.compile(r"[0-9a-f]{40}\Z")
SCREEN_BOX = (160, 75, 864, 680)
# Sway foot floats centered at 760x530; text/active border begin near x132,y119.
# A contains the host nonce, while B is a distinct, initially blank terminal.
FOOT_CONTENT_BOX = (132, 116, 680, 284)
MENU_POPUP_BOX = (380, 495, 690, 755)
# Centered pinned Terminal icon only, above the Dock caption and below topbar.
# GTK DND icon is 32px in round1; BUTTON icon is 16px after persisted compact.
DOCK_ICON_BOX = (492, 704, 532, 740)
DOCK_X, DOCK_Y = 512, 730
MENU_X, MENU_Y = 150, 20
# Launcher is a TOP/LEFT-anchored real GtkLayerShell surface (14px/43px).
# Neither the Terminal row nor Compact checkbox has a static hitbox:
# both clickable centers and dimensions come from mapped GTK allocations.
LAUNCHER_BOX = (20, 52, 375, 420)
BOOT_MARKERS = (
    "P3:USERSPACE:debian", "P3:ROOT_OK", "P3:DRM_OK",
    "P3:DESKTOP_USER:1000", "P3:RENDERER_ENV:pixman",
    "P3:BACKENDS_ENV:libinput,drm", "P3:DRM_FD:card0",
    "P3:DRM_MODESET_REQUESTED:Virtual-1", "P3:PIXMAN_RENDERER_CREATED",
    "P3:SWAY_READY", "P3:LAYER_SHELL_READY",
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
    second_window_id: str | None = None
    second_opened: bool = False
    minimized: bool = False
    restored: bool = False
    close_via_dock: bool = False
    menu_targets: dict[str, list[int]] = field(default_factory=dict)
    compact_target: list[int] | None = None
    terminal_target: list[int] | None = None
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

def gtk_target_from_output(output: str, kind: str) -> tuple[int, int, int, int]:
    """Validate actual mapped GtkWidget center and allocation in QMP pixels."""
    if kind not in GTK_TARGET_RE:
        raise ValueError("unknown GTK launcher widget")
    prefix = "P3:" + kind + "_TARGET:"
    tokens = lines(output)
    raw = [item for item in tokens if item.startswith(prefix)]
    matches = [(index, match) for index, item in enumerate(tokens)
               if (match := GTK_TARGET_RE[kind].fullmatch(item))]
    if len(raw) != 1 or len(matches) != 1:
        raise Phase3Failure("missing/duplicated mapped GTK " + kind + " widget allocation")
    x, y, width, height = (int(matches[0][1].group(n)) for n in range(1, 5))
    if not (25 <= x <= 380 and 32 <= width <= 370 and 16 <= height <= 90
            and 14 <= x - width // 2 and x + (width + 1) // 2 <= 394
            and 43 <= y - height // 2 and y + (height + 1) // 2 <= 650):
        raise Phase3Failure("GTK " + kind + " target is outside mapped launcher geometry")
    if (kind == "TERMINAL" and not 85 <= y <= 360) or (
            kind == "COMPACT" and not 240 <= y <= 610):
        raise Phase3Failure("GTK " + kind + " target is in the wrong launcher row")
    return x, y, width, height


def ipc_layout_from_output(output: str) -> tuple[int, dict]:
    """Fail-closed proof of Sway's real output/workarea and GTK exclusive zones.

    This JSON is sourced from Sway get_outputs/get_workspaces and the two
    mapped GtkLayerShell surfaces, not from a static generated configuration.
    QMP independently captures the native 1024x768 GPU output afterward.
    """
    items = lines(output)
    evidence = [(index, line[len(LAYOUT_PREFIX):]) for index, line in enumerate(items)
                if line.startswith(LAYOUT_PREFIX)]
    if len(evidence) != 1 or not 0 < len(evidence[0][1]) <= 4096:
        raise Phase3Failure("missing, duplicate or oversized live Sway IPC layout proof")
    position, payload = evidence[0]
    try:
        layout = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise Phase3Failure("live Sway IPC layout is malformed JSON") from exc
    keys = {"source", "output", "output_rect", "scale", "mode", "workspace",
            "workspace_rect", "top_exclusive", "dock_exclusive"}
    if not isinstance(layout, dict) or set(layout) != keys:
        raise Phase3Failure("Sway IPC layout missing required source-bound fields")
    if json.dumps(layout, sort_keys=True, separators=(",", ":")) != payload:
        raise Phase3Failure("Sway IPC layout JSON is not canonical")
    if (layout["source"] != "sway.get_outputs+get_workspaces" or
            layout["output"] != "Virtual-1" or layout["workspace"] != "1" or
            type(layout["scale"]) not in (int, float) or layout["scale"] != 1.0):
        raise Phase3Failure("Sway IPC output/workspace identity or scale changed")
    def rect(value: object) -> dict:
        if (not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"} or
                any(type(v) is not int for v in value.values())):
            raise Phase3Failure("Sway IPC rectangle uses invalid coordinates")
        return value
    output_rect = rect(layout["output_rect"])
    workarea = rect(layout["workspace_rect"])
    mode = layout["mode"]
    if (not isinstance(mode, dict) or set(mode) != {"width", "height"} or
            any(type(v) is not int for v in mode.values()) or
            output_rect != {"x": 0, "y": 0, "width": 1024, "height": 768} or
            mode != {"width": 1024, "height": 768}):
        raise Phase3Failure("Sway IPC output geometry does not match native QEMU scanout")
    top = layout["top_exclusive"]
    bottom = layout["dock_exclusive"]
    if type(top) is not int or type(bottom) is not int or top != 38 or bottom != 68:
        raise Phase3Failure("original topbar/Dock exclusive zones were not source-bound")
    # Sway's configured inner gap is 4px and may inset the real workspace.
    # Distinguish that small symmetric compositor gap from missing 38/68px
    # layer-shell reservations; no fabricated full-output rectangle can pass.
    left = workarea["x"] - output_rect["x"]
    right = output_rect["x"] + output_rect["width"] - (
        workarea["x"] + workarea["width"]
    )
    top_extra = workarea["y"] - (output_rect["y"] + top)
    bottom_extra = output_rect["y"] + output_rect["height"] - bottom - (
        workarea["y"] + workarea["height"]
    )
    if (not 0 <= left == right <= 4 or not 0 <= top_extra <= 4 or
            not 0 <= bottom_extra <= 12 or
            workarea["width"] <= 0 or workarea["height"] <= 0):
        raise Phase3Failure("original topbar/Dock did not reserve real Sway workarea")
    return position, layout


def app_index_from_output(output: str) -> tuple[int, dict]:
    """Audit real Gio desktop IDs separately from GTK-packed launcher rows."""
    items = lines(output)
    prefix = "P3:APP_INDEX:"
    observed = [(i, line[len(prefix):]) for i, line in enumerate(items)
                if line.startswith(prefix)]
    if len(observed) != 1 or not 0 < len(observed[0][1]) <= 4096:
        raise Phase3Failure("missing, duplicated or oversized real Gio app index")
    position, raw = observed[0]
    try:
        index = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Phase3Failure("Gio app index marker is malformed JSON") from exc
    if (not isinstance(index, dict) or
            set(index) != {"source", "eligible_count", "eligible_ids",
                           "displayed_count", "displayed_ids", "sha256"} or
            json.dumps(index, sort_keys=True, separators=(",", ":"), ensure_ascii=True) != raw):
        raise Phase3Failure("Gio app index is not the canonical six-field schema")
    if index["source"] != "Gio.AppInfo.get_all":
        raise Phase3Failure("launcher index does not originate in Gio.AppInfo")
    eligible = index["eligible_ids"]
    displayed = index["displayed_ids"]
    eligible_count = index["eligible_count"]
    displayed_count = index["displayed_count"]
    def valid_ids(ids: object) -> bool:
        return (
            isinstance(ids, list)
            and all(isinstance(item, str) and len(item) <= 128 and
                    re.fullmatch(r"[A-Za-z0-9_.+-]+\.desktop", item) is not None
                    for item in ids)
            and ids == sorted(set(ids))
        )
    if (type(eligible_count) is not int or not 1 <= eligible_count <= 60 or
            type(displayed_count) is not int or not 0 <= displayed_count <= 60 or
            not valid_ids(eligible) or not valid_ids(displayed) or
            len(eligible) != eligible_count or len(displayed) != displayed_count or
            not set(displayed).issubset(set(eligible)) or
            "foot.desktop" in displayed or
            (displayed_count == 0 and "foot.desktop" not in eligible)):
        raise Phase3Failure("Gio eligible/packed GTK IDs are missing, unsafe or contradictory")
    canonical = json.dumps(
        {"displayed_ids": displayed, "eligible_ids": eligible},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if index["sha256"] != expected:
        raise Phase3Failure("Gio app digest does not bind both eligible and packed rows")
    return position, index


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
    layout_position, _ = ipc_layout_from_output(output)
    first_tree = next((index for index, value in enumerate(items)
                       if value.startswith(IPC_PREFIX)), None)
    if (first_tree is None or not
            (exactly_once(items, "P3:SWAY_READY") < first_tree < layout_position <
             exactly_once(items, "P3:LAYER_SHELL_READY") <
             exactly_once(items, "P3:SHELL_READY"))):
        raise Phase3Failure("real Sway tree/layout and GTK reservations were not proved before shell readiness")
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


def clean_shutdown(result: BootResult) -> int:
    if result.timed_out or result.output_limit or result.returncode != 0:
        raise Phase3Failure("guest timed out, flooded serial output or QEMU failed")
    if p2.KERNEL_FAILURE_RE.search(result.output):
        raise Phase3Failure("kernel panic/oops/error in guest serial log")
    ends = [index for index, item in enumerate(lines(result.output))
            if p2.POWER_DOWN_RE.fullmatch(item)]
    if len(ends) != 1:
        raise Phase3Failure("missing unique kernel power-down proof")
    return ends[0]


def poweroff(result: BootResult, expected_failure: str | None = None) -> int:
    stop = clean_shutdown(result)
    failures = [x for x in lines(result.output) if x.startswith(("P2:FAIL:", "P3:FAIL:"))]
    if failures != ([expected_failure] if expected_failure else []):
        raise Phase3Failure(f"unexpected guest failures: {failures!r}")
    return stop


def focus_ids(items: list[str]) -> list[str]:
    return [match.group(1) for item in items if (match := FOCUS_RE.fullmatch(item))]


def ipc_states(items: list[str]) -> list[tuple[int, dict]]:
    """Parse independently obtained real Sway get_tree snapshots from the guest.

    A shell action marker alone is insufficient; every lifecycle transition
    below is checked against the compositor's normalized JSON state, and the
    real QMP frames provide separate host-side visual evidence.
    """
    observed = []
    previous_seq = 0
    for position, marker in enumerate(items):
        if not marker.startswith(IPC_PREFIX):
            continue
        raw = marker[len(IPC_PREFIX):]
        if len(raw) > 4096:
            raise Phase3Failure("oversized Sway IPC state snapshot")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Phase3Failure("invalid Sway IPC state JSON") from exc
        if not isinstance(value, dict) or value.get("source") != "sway.get_tree":
            raise Phase3Failure("IPC snapshot is not from a real Sway tree query")
        sequence = value.get("seq")
        windows = value.get("windows")
        if type(sequence) is not int or sequence != previous_seq + 1:
            raise Phase3Failure("Sway IPC sequence is missing or reordered")
        if not isinstance(windows, list) or len(windows) > 32:
            raise Phase3Failure("Sway IPC snapshot has invalid window count")
        previous_seq = sequence
        ids = set()
        focused = 0
        for node in windows:
            if not isinstance(node, dict):
                raise Phase3Failure("Sway IPC snapshot contains a malformed node")
            con_id = node.get("id")
            pid = node.get("pid")
            app_id = node.get("app_id")
            scratch = node.get("scratchpad_state")
            visible = node.get("visible")
            is_focused = node.get("focused")
            minimized = node.get("minimized")
            if (type(con_id) is not int or con_id <= 0 or con_id in ids or
                    not isinstance(app_id, str) or len(app_id) > 128 or
                    (pid is not None and (type(pid) is not int or pid <= 0)) or
                    not isinstance(scratch, str) or len(scratch) > 32 or
                    type(visible) is not bool or type(is_focused) is not bool or
                    type(minimized) is not bool or
                    minimized != (scratch != "none" and not visible)):
                raise Phase3Failure("inconsistent Sway IPC leaf state")
            ids.add(con_id)
            focused += is_focused
        if focused > 1:
            raise Phase3Failure("multiple compositor leaf windows claim focus")
        observed.append((position, value))
    if not observed or observed[0][1]["seq"] != 1:
        raise Phase3Failure("missing first real Sway IPC tree snapshot")
    return observed


def verified_window_state(
    snapshots: list[tuple[int, dict]], before: int, con_id: int,
) -> tuple[dict | None, dict]:
    """Latest Sway snapshot at/before an observed transition marker."""
    available = [value for position, value in snapshots if position < before]
    if not available:
        raise Phase3Failure("no Sway IPC tree evidence before lifecycle event")
    tree = available[-1]
    return next((item for item in tree["windows"] if item["id"] == con_id), None), tree


def validate_positive(result: BootResult, release: str, source: str) -> None:
    stop = poweroff(result)
    ready(result.output, release, source, result.previous_nonce)
    if result.nonce is None or not NONCE_RE.fullmatch(result.nonce):
        raise Phase3Failure("host challenge is absent")
    if result.nonce == result.previous_nonce:
        raise Phase3Failure("the second boot reused the first challenge")
    expected_clicks = 9 if result.previous_nonce is None else 8
    if not all((
        result.pointer_clicks == expected_clicks,
        result.preference_toggled == (result.previous_nonce is None),
        result.keyboard_sent, result.exit_sent, result.powerdown_sent,
        result.second_opened, result.minimized, result.restored,
        result.close_via_dock, set(result.menu_targets) == {"Minimize", "Close"},
    )):
        raise Phase3Failure("QMP did not exercise launcher, two windows, Dock lifecycle and shutdown")
    a, b = result.window_id, result.second_window_id
    if (not isinstance(a, str) or not a.isdigit() or int(a) <= 0 or
            not isinstance(b, str) or not b.isdigit() or int(b) <= 0 or a == b):
        raise Phase3Failure("distinct live Sway foot IDs were not observed")
    items = lines(result.output)
    opened = exactly_once(items, "P3:LAUNCHER_OPEN")
    compact = gtk_target_from_output(result.output, "COMPACT")
    terminal = gtk_target_from_output(result.output, "TERMINAL")
    compact_mark = exactly_once(items, "P3:COMPACT_TARGET:" + ":".join(map(str, compact)))
    terminal_mark = exactly_once(items, "P3:TERMINAL_TARGET:" + ":".join(map(str, terminal)))
    launched = exactly_once(items, "P3:APP_LAUNCHED:foot")
    index_mark, _ = app_index_from_output(result.output)
    if not (exactly_once(items, "P3:SHELL_READY") < opened < compact_mark < terminal_mark <
            index_mark < launched):
        raise Phase3Failure("mapped GTK/Gio index and hitboxes were not observed before Terminal")
    if (result.compact_target != list(compact) or
            result.terminal_target != list(terminal) or
            terminal[1] >= compact[1]):
        raise Phase3Failure("QMP GTK hitbox coordinates disagree with real launcher geometry")
    if result.previous_nonce is None:
        saved = exactly_once(items, "P3:PREFERENCE_SAVED:compact_dock=true")
        if not terminal_mark < saved < launched:
            raise Phase3Failure("real GTK Compact checkbox was not saved before launching Terminal")
    elif any(item.startswith("P3:PREFERENCE_SAVED:") for item in items):
        raise Phase3Failure("second boot unexpectedly rewrote the persisted preference")
    exactly_once(items, "P3:PREFERENCE_VERIFIED:compact_dock=true:uid=1000")
    keyboard = exactly_once(items, f"P3:KEYBOARD_OK:{result.nonce}")
    # The first GTK Launcher Terminal must really obtain Sway focus.
    # The live harness waits for this marker, and the offline manifest must
    # independently replay it rather than trusting the harness result flags.
    first_focus = next((i for i, marker in enumerate(items)
                        if marker == f"P3:WINDOW_FOCUSED:{a}" and
                        launched < i < keyboard), None)
    if first_focus is None:
        raise Phase3Failure("initial launcher Terminal focus not observed")
    discovered = [(i, match.group(1)) for i, marker in enumerate(items)
                  if (match := DISCOVERED_RE.fullmatch(marker))]
    if len(discovered) != 2 or {key for _, key in discovered} != {a, b}:
        raise Phase3Failure("Sway did not independently discover exactly two live foot clients")
    created_a = exactly_once(items, f"P3:WINDOW_DISCOVERED:{a}:foot")
    created_b = exactly_once(items, f"P3:WINDOW_DISCOVERED:{b}:foot")
    minimized = exactly_once(items, f"P3:WINDOW_MINIMIZED:{a}")
    restored = exactly_once(items, f"P3:WINDOW_RESTORED:{a}")
    closed_a = exactly_once(items, f"P3:WINDOW_CLOSED:{a}")
    closed_b = exactly_once(items, f"P3:WINDOW_CLOSED:{b}")
    # A Dock 'kill' is not complete merely because its Wayland surface
    # disappeared. The guest must independently prove that the same PID
    # (from its pre-close Sway get_tree) is absent or zombie in /proc.
    exit_markers = [(i, text) for i, text in enumerate(items)
                    if text.startswith("P3:PROCESS_EXITED:")]
    exited = [(i, match) for i, text in exit_markers
              if (match := re.fullmatch(
                  rf"P3:PROCESS_EXITED:{re.escape(a)}:([1-9][0-9]*)", text))]
    if len(exit_markers) != 1 or len(exited) != 1:
        raise Phase3Failure("Dock Close lacks unique real A process-exit proof")
    process_exited, exited_match = exited[0]
    activated = [(i, match.group(1)) for i, marker in enumerate(items)
                 if (match := ACTIVATED_RE.fullmatch(marker))]
    b_focus = next((i for i, cid in activated if cid == b and i >= created_b), None)
    dock_focus = next((i for i, cid in enumerate(items)
                       if cid == f"P3:WINDOW_FOCUSED:{a}" and
                       b_focus is not None and i > b_focus), None)
    restored_focus = next((i for i, cid in activated
                           if cid == a and i > restored), None)
    b_after_hide = next((i for i, cid in activated
                         if cid == b and i > minimized), None)
    b_after_close = next((i for i, cid in activated
                          if cid == b and i > closed_a), None)
    if (b_focus is None or dock_focus is None or restored_focus is None or
            b_after_hide is None or b_after_close is None):
        raise Phase3Failure("two-window Sway focus switching and restore were not proven")
    if not (exactly_once(items, "P3:SHELL_READY") < created_a <= launched <
            first_focus < keyboard < created_b <= b_focus < dock_focus < minimized <
            b_after_hide < restored < restored_focus < closed_a <
            process_exited < closed_b < stop and closed_a < b_after_close < closed_b):
        raise Phase3Failure("real terminal/Dock/scratchpad lifecycle was out of order")
    snapshots = ipc_states(items)
    preclose, _ = verified_window_state(snapshots, closed_a, int(a))
    observed_pids = {
        node["pid"] for position, tree in snapshots if position < closed_a
        for node in tree["windows"] if node["id"] == int(a)
    }
    if preclose is not None or observed_pids != {int(exited_match.group(1))}:
        raise Phase3Failure("process-exit PID differs from the real Sway foot process")
    for position, con_id, present, state in (
        (created_a, a, True, None), (first_focus, a, True, "focused"),
        (b_focus, b, True, "focused"),
        (dock_focus, a, True, "focused"),
        (minimized, a, True, "minimized"),
        (b_after_hide, b, True, "focused"),
        (restored, a, True, "restored"),
        (restored_focus, a, True, "focused"),
        (closed_a, a, False, None),
        (b_after_close, b, True, "focused"), (closed_b, b, False, None),
    ):
        node, tree = verified_window_state(snapshots, position, int(con_id))
        if present != (node is not None):
            raise Phase3Failure("Sway IPC tree contradicts window lifecycle marker")
        if node is not None and (
            "foot" not in node["app_id"].casefold() or
            type(node["pid"]) is not int or node["pid"] <= 0 or
            (state == "focused" and not node["focused"]) or
            (state == "minimized" and not node["minimized"]) or
            (state == "restored" and node["minimized"])
        ):
            raise Phase3Failure("Sway IPC tree does not show the asserted real foot state")
        if position in (b_focus, dock_focus, minimized, b_after_hide,
                        restored, closed_a, b_after_close):
            ids = {item["id"] for item in tree["windows"]}
            if int(b) not in ids or (position < closed_a and int(a) not in ids):
                raise Phase3Failure("both real terminal clients were not live during Dock switch")
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
    if len(hashes) < 8:
        raise Phase3Failure("QMP snapshots lack distinct genuine application states")
    # Tie the true DRM mode reported by Sway IPC to the separate, host-read
    # virtio-GPU QMP scanout before accepting the original shell as visible.
    mode = ipc_layout_from_output(result.output)[1]["mode"]
    if ((result.screenshots["ready"]["width"], result.screenshots["ready"]["height"]) !=
            (mode["width"], mode["height"])):
        raise Phase3Failure("Sway reserved workarea has no corresponding GPU framebuffer")

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
    forbidden = ("P3:DRM_OK", "P3:RENDERER_ENV:", "P3:PIXMAN_RENDERER_CREATED",
                 "P3:BACKENDS_ENV:", "P3:DRM_FD:", "P3:DRM_MODESET_REQUESTED:",
                 "P3:SWAY_READY", "P3:LAYER_SHELL_READY", "P3:SHELL_READY",
                 "P3:IPC_LAYOUT:", "P3:APP_INDEX:", "P3:LAUNCHER_OPEN",
                 "P3:COMPACT_TARGET:", "P3:TERMINAL_TARGET:",
                 "P3:APP_LAUNCHED", "P3:WINDOW_", "P3:IPC_TREE:", "P3:PROCESS_EXITED:", "P3:KEYBOARD_OK")
    if any(x.startswith(forbidden) for x in items):
        raise Phase3Failure("negative case reached a desktop or used a fallback renderer")
    if (result.nonce or result.keyboard_sent or result.pointer_clicks or
            result.screenshots or result.second_window_id or result.second_opened or
            result.minimized or result.restored or result.close_via_dock or
            result.menu_targets or result.compact_target is not None or
            result.terminal_target is not None):
        raise Phase3Failure("negative case unexpectedly performed graphical acceptance")


def validate_shell_negative(result: BootResult, release: str, source: str) -> None:
    """Genuine Sway/DRM startup followed by a test-gated real GTK child crash."""
    stop = clean_shutdown(result)
    items = lines(result.output)
    identity(items, release, source)
    ordered = (
        f"P2:SOURCE:{source}", f"P2:KERNEL:{release}",
        "P2:USERSPACE:debian", "P2:ROOT_OK",
        f"P3:SOURCE:{source}", f"P3:KERNEL:{release}",
        "P3:USERSPACE:debian", "P3:ROOT_OK", "P3:DRM_OK",
        "P3:PERSISTED:EMPTY", "P3:PREFERENCE_DISK:compact_dock=absent",
        "P3:DESKTOP_USER:1000", "P3:RENDERER_ENV:pixman",
        "P3:BACKENDS_ENV:libinput,drm", "P3:DRM_FD:card0",
        "P3:DRM_MODESET_REQUESTED:Virtual-1", "P3:PIXMAN_RENDERER_CREATED",
        "P3:SWAY_READY",
        "P3:INJECTED:SHELL_FAIL", "P3:FAIL:SHELL_EXIT",
    )
    positions = [exactly_once(items, marker) for marker in ordered]
    # phase3-log.service forwards the session file asynchronously, while the
    # inherited Phase 2 OnFailure service writes straight to ttyS0. Their
    # serial order is NOT a reliable inter-service happens-before relation.
    p2_failure = exactly_once(items, "P2:FAIL:DESKTOP_SERVICE")
    if positions != sorted(positions) or stop <= max(positions[-1], p2_failure):
        raise Phase3Failure("native Sway/GTK fault was not followed by clean shutdown")
    # Depending on whether Gtk aborts or raises a Python exception, the real
    # shell may also emit its own startup-failure marker before the supervisor.
    child_failure = [i for i, item in enumerate(items) if item == "P3:FAIL:SHELL_STARTUP"]
    if len(child_failure) > 1 or any(
            not (positions[-2] < i < positions[-1]) for i in child_failure):
        raise Phase3Failure("GTK child startup failure was duplicated or out of order")
    failures = [item for item in items if item.startswith(("P2:FAIL:", "P3:FAIL:"))]
    expected = (["P3:FAIL:SHELL_STARTUP"] if child_failure else []) + [
        "P3:FAIL:SHELL_EXIT", "P2:FAIL:DESKTOP_SERVICE"]
    if sorted(failures) != sorted(expected):
        raise Phase3Failure(f"shell-negative had unexpected guest failures: {failures!r}")
    forbidden = ("P3:LAYER_SHELL_READY", "P3:SHELL_READY",
                 "P3:PREFERENCE_STATE:", "P3:PREFERENCE_SAVED:",
                 "P3:IPC_LAYOUT:", "P3:APP_INDEX:", "P3:LAUNCHER_OPEN",
                 "P3:COMPACT_TARGET:", "P3:TERMINAL_TARGET:",
                 "P3:APP_LAUNCHED:", "P3:WINDOW_", "P3:IPC_TREE:", "P3:PROCESS_EXITED:", "P3:KEYBOARD_OK:")
    if any(item.startswith(forbidden) for item in items):
        raise Phase3Failure("broken GTK child reported shell/UI success")
    if any((result.previous_nonce is not None, result.nonce is not None,
            result.window_id is not None, result.second_window_id is not None,
            result.second_opened, result.minimized, result.restored,
            result.close_via_dock, result.pointer_clicks != 0,
            bool(result.menu_targets), result.compact_target is not None,
            result.terminal_target is not None, result.preference_toggled,
            result.keyboard_sent, result.exit_sent,
            result.powerdown_sent, bool(result.screenshots))):
        raise Phase3Failure("shell-negative attempted a positive GUI/HID/QMP flow")


def changed_fraction(a: p2.Frame, b: p2.Frame, box=SCREEN_BOX, *, pixel_delta: int = 25) -> float:
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
            different += max(abs(before[i]-after[i]) for i in range(3)) > pixel_delta
            count += 1
    return different / count


def prove_compact_dock_visible(regular: p2.Frame, compact: p2.Frame) -> tuple[float, int, int]:
    """Cross-boot GPU proof of a smaller real pinned Terminal icon.

    Compare both initial ready frames, before any host action. The ROI excludes
    the clock and launcher; the Dock outer window has a fixed 88px default size,
    so only icon-content shrinkage (GTK DND 32 -> BUTTON 16) is asserted.
    """
    if (regular.width, regular.height, compact.width, compact.height) != (1024, 768, 1024, 768):
        raise Phase3Failure("compact Dock frames have incompatible GPU dimensions")
    delta = changed_fraction(regular, compact, box=DOCK_ICON_BOX)
    def bright_pixels(frame: p2.Frame) -> int:
        x1, y1, x2, y2 = DOCK_ICON_BOX
        bright = 0
        for y in range(y1, y2):
            for x in range(x1, x2):
                index = (y * frame.width + x) * 3
                r, g, b = frame.pixels[index:index + 3]
                bright += r >= 150 and g >= 160 and b >= 170
        return bright
    regular_bright = bright_pixels(regular)
    compact_bright = bright_pixels(compact)
    if delta < .025 or regular_bright < compact_bright + 20:
        raise Phase3Failure(
            "saved Compact Dock did not visibly shrink the real GPU icon "
            f"(ROI change={delta:.4f}, bright pixels={regular_bright}/{compact_bright})"
        )
    return delta, regular_bright, compact_bright


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


def click(qmp: p2.QMP, x=DOCK_X, y=DOCK_Y, *, button: str = "left") -> None:
    """Real QMP tablet pointer+button interaction; no guest-side UI bypass."""
    if button not in ("left", "right"):
        raise ValueError("unsupported emulated pointer button")
    qmp.execute("input-send-event", {"events": p2.pointer_events(x, y)})
    time.sleep(.1)
    for down in (True, False):
        qmp.execute("input-send-event", {"events": [
            {"type": "btn", "data": {"down": down, "button": button}},
        ]})
        time.sleep(.06)


def key_event(qmp: p2.QMP, key: str, down: bool) -> None:
    qmp.execute("input-send-event", {"events": [{"type": "key", "data": {
        "down": down, "key": {"type": "qcode", "data": key},
    }}]})
    time.sleep(.035)


def press_key(qmp: p2.QMP, key: str) -> None:
    key_event(qmp, key, True)
    key_event(qmp, key, False)


def press_chord(qmp: p2.QMP, *keys: str) -> None:
    """Send a real Sway Mod4+Return binding through emulated keyboard input."""
    if tuple(keys) != ("meta_l", "ret"):
        raise ValueError("unsupported acceptance keyboard chord")
    for key in keys:
        key_event(qmp, key, True)
    for key in reversed(keys):
        key_event(qmp, key, False)


def popup_menu_target(before: p2.Frame, popup: p2.Frame,
                      operation: str) -> tuple[int, int]:
    """Derive the real GTK menu row from actual QEMU framebuffer pixels.

    The Dock keeps KeyboardMode.NONE, so focus cannot be stolen from foot.
    CSS paints the GTK menu body #1a2a40 and 1px border #7598b4.
    We select exactly one plausible changed, color-matched rectangle in the
    bottom-center GPU scanout. Unknown dimensions/theme fail closed; a tested
    image of the first real VM popup is needed before release.
    """
    if operation not in ("Minimize", "Close"):
        raise ValueError("unsupported Dock context operation")
    if (before.width, before.height) != (1024, 768) or (
            popup.width, popup.height) != (1024, 768):
        raise Phase3Failure("popup calibration requires native QEMU GPU frames")
    # GTK colors are CSS literals, not invented pixel coordinates. Bounds
    # include cursor at (512,730) and permit Wayland popup placement/flipping.
    body, border = (26, 42, 64), (117, 152, 180)
    x1, y1, x2, y2 = (330, 440, 820, 755)
    pixels: set[tuple[int, int]] = set()
    border_pixels: set[tuple[int, int]] = set()
    for y in range(y1, y2):
        for x in range(x1, x2):
            offset = (y * 1024 + x) * 3
            old = before.pixels[offset:offset + 3]
            new = popup.pixels[offset:offset + 3]
            # Popup #1a2a40 differs from wallpaper #152437 by only 5/6/9;
            # a 13-level cutoff incorrectly discards genuine CSS menu interiors.
            if max(abs(old[k] - new[k]) for k in range(3)) < 5:
                continue
            on_body = max(abs(new[k] - body[k]) for k in range(3)) <= 15
            on_border = max(abs(new[k] - border[k]) for k in range(3)) <= 24
            if on_body or on_border:
                pixels.add((x, y))
                if on_border:
                    border_pixels.add((x, y))
    # Flood-fill the CSS-colored changed region; Dock hover is too short to
    # qualify as a three-row popup, and disconnected fragments are rejected.
    remaining = set(pixels)
    candidates: list[tuple[int, int, int, int]] = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        min_x = max_x = seed[0]
        min_y = max_y = seed[1]
        count = 0
        border_count = 0
        while stack:
            x, y = stack.pop()
            count += 1
            border_count += (x, y) in border_pixels
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for neighbor in ((x-1, y), (x+1, y), (x, y-1),
                             (x, y+1), (x-1, y-1), (x+1, y-1),
                             (x-1, y+1), (x+1, y+1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        width, height = max_x - min_x + 1, max_y - min_y + 1
        if (96 <= width <= 310 and 66 <= height <= 194 and
                400 <= min_x <= 680 and 510 <= min_y < max_y <= 740 and
                min_x < DOCK_X + 90 and max_x > DOCK_X - 90 and
                # Require a substantial solid menu body, not only a thin
                # border or the Dock hover repaint behind the popup.
                count >= width * height // 3 and border_count >= 35):
            candidates.append((min_x, min_y, max_x, max_y))
    if len(candidates) != 1:
        raise Phase3Failure("real GTK menu rectangle ambiguous or not visible")
    x1, y1, x2, y2 = candidates[0]
    row = 1 if operation == "Minimize" else 2
    x = (x1 + x2) // 2
    y = round(y1 + (row + .5) * (y2 - y1 + 1) / 3)
    if not (x1 + 12 <= x <= x2 - 12 and y1 + 10 <= y <= y2 - 8):
        raise Phase3Failure("calibrated QMP menu click falls outside GTK row")
    return x, y


def click_dock_context_menu(qmp: p2.QMP, before: p2.Frame,
                            popup: p2.Frame, operation: str) -> tuple[int, int]:
    """Physically click the selected menu item with QMP tablet HID."""
    target = popup_menu_target(before, popup, operation)
    click(qmp, *target)
    return target


def text_keys(value: str) -> list[str]:
    if not re.fullmatch(r"[a-z0-9 ]+", value):
        raise ValueError("host text must use only known safe QMP qcodes")
    return ["spc" if char == " " else char for char in value] + ["ret"]


def enter_text(qmp: p2.QMP, value: str) -> None:
    for key in text_keys(value):
        press_key(qmp, key)

class Phase3Monitor(p2.SerialMonitor):
    def check(self, *, require_running=True):
        super().check(require_running=require_running)
        failures = [line for line in lines(self.output()) if line.startswith(("P2:FAIL:", "P3:FAIL:"))]
        allowed = {self.expected_failure} if self.expected_failure else set()
        if self.expected_failure == "P2:FAIL:DESKTOP_SERVICE":
            allowed.update(("P3:FAIL:SHELL_STARTUP", "P3:FAIL:SHELL_EXIT"))
        if any(value not in allowed for value in failures):
            raise Phase3Failure(f"unexpected guest failure: {failures!r}")


def capture(qmp: p2.QMP, name: str, stage: str, deadline: float, *,
            changed_from: p2.Frame | None = None, box=SCREEN_BOX,
            min_changed: float = 0.0, pixel_delta: int = 25,
            min_high_contrast_changed: float = 0.0,
            popup_operation: str | None = None) -> tuple[dict, p2.Frame]:
    """Capture real QMP scanout; retry until UI transition is actually visible.

    Gtk Wayland popup mapping and Sway focus/modeset rendering are asynchronous
    to serial IPC markers. For Dock popups, a changed hover pixel is not
    enough: the *same actual framebuffer* must contain one uniquely calibrated
    three-row GTK menu before the screenshot is accepted. Every screendump
    retry remains in the QMP JSONL transcript with a successful QMP reply;
    only the final accepted framebuffer is hashed into the evidence manifest.
    """
    if popup_operation is not None and changed_from is None:
        raise ValueError("GTK popup calibration needs its preceding QEMU frame")
    if min_high_contrast_changed and changed_from is None:
        raise ValueError("high-contrast GPU proof needs its preceding QEMU frame")
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
            if changed_from is not None and (
                    changed_fraction(changed_from, frame, box=box,
                                     pixel_delta=pixel_delta) < min_changed):
                raise Phase3Failure("QMP GPU pixels have not changed for " + stage)
            if min_high_contrast_changed and changed_fraction(
                    changed_from, frame, box=box) < min_high_contrast_changed:
                raise Phase3Failure("QMP GPU lacks high-contrast detail for " + stage)
            if popup_operation is not None:
                # GTK may draw Dock hover long before its xdg_popup appears.
                # Retry real screendumps until the actual CSS menu is mapped.
                popup_menu_target(changed_from, frame, popup_operation)
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
               preference_toggled: bool = False,
               menu_targets: dict | None = None,
               compact_target: list[int] | None = None,
               terminal_target: list[int] | None = None) -> None:
    """Require acknowledgments for every QMP framebuffer, HID and power action."""
    try:
        events = [json.loads(line) for line in records.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise Phase3Failure("invalid QMP JSONL transcript") from error
    if not isinstance(events, list) or len(events) > 10000:
        raise Phase3Failure("unbounded or invalid QMP transcript")
    if nonce is None:
        if (events or screenshots or menu_targets or compact_target is not None or
                terminal_target is not None):
            raise Phase3Failure("negative test used graphical QMP events")
        return
    if (not isinstance(menu_targets, dict) or
            set(menu_targets) != {"Minimize", "Close"} or
            any(not isinstance(target, list) or len(target) != 2 or
                type(target[0]) is not int or not 0 <= target[0] < 1024 or
                type(target[1]) is not int or not 0 <= target[1] < 768
                for target in menu_targets.values())):
        raise Phase3Failure("unbound/invalid GPU-calibrated Dock click targets")
    for kind, target in (("COMPACT", compact_target), ("TERMINAL", terminal_target)):
        if (not isinstance(target, list) or len(target) != 4 or
                any(type(value) is not int for value in target)):
            raise Phase3Failure("unbound/invalid real GTK " + kind + " allocation")
        x, y, width, height = target
        if (not (25 <= x <= 380 and 32 <= width <= 370 and 16 <= height <= 90
                 and 14 <= x - width // 2 and x + (width + 1) // 2 <= 394
                 and 43 <= y - height // 2 and y + (height + 1) // 2 <= 650)
                or (kind == "TERMINAL" and not 85 <= y <= 360)
                or (kind == "COMPACT" and not 240 <= y <= 610)):
            raise Phase3Failure("real GTK " + kind + " hitbox is off-screen/implausible")
    if terminal_target[1] >= compact_target[1]:
        raise Phase3Failure("GTK Terminal row must be above Compact checkbox")
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
        if (position >= len(requests) or requests[position].get("execute") != name or
                requests[position].get("arguments", {}) != (args or {})):
            raise Phase3Failure(f"QMP transcript missing expected {name} at action {position}")
        position += 1
    def screenshot(stage: str) -> None:
        nonlocal position
        p = OUT / screenshots[stage]["path"]
        seen = 0
        # Distinguish same-stage async retries from consecutive *different*
        # screenshot stages (launcher then preferences on second boot has no
        # intervening pointer input). The next path belongs to the next stage.
        while (position < len(requests) and
               requests[position].get("execute") == "screendump" and
               requests[position].get("arguments") == {"filename": str(p)}):
            seen += 1
            position += 1
        if not seen:
            raise Phase3Failure(f"missing real QMP {stage} screendump")
    def clicked(x: int, y: int, button="left") -> None:
        expect("input-send-event", {"events": p2.pointer_events(x, y)})
        for down in (True, False):
            expect("input-send-event", {"events": [
                {"type": "btn", "data": {"down": down, "button": button}},
            ]})
    def pressed(key: str) -> None:
        for down in (True, False):
            expect("input-send-event", {"events": [{"type": "key", "data": {
                "down": down, "key": {"type": "qcode", "data": key},
            }}]})
    def chord(*keys: str) -> None:
        for key in keys:
            expect("input-send-event", {"events": [{"type": "key", "data": {
                "down": True, "key": {"type": "qcode", "data": key},
            }}]})
        for key in reversed(keys):
            expect("input-send-event", {"events": [{"type": "key", "data": {
                "down": False, "key": {"type": "qcode", "data": key},
            }}]})
    def typed(value: str) -> None:
        for key in text_keys(value):
            pressed(key)
    def menu(operation: str) -> None:
        target = menu_targets[operation]
        clicked(*target)
    expect("qmp_capabilities")
    screenshot("ready")
    clicked(MENU_X, MENU_Y)
    screenshot("launcher")
    if preference_toggled:
        clicked(*compact_target[:2])
    screenshot("preferences")
    clicked(*terminal_target[:2])
    screenshot("foot")
    typed("p3save " + nonce)
    screenshot("saved")
    chord("meta_l", "ret")
    screenshot("second")
    clicked(DOCK_X, DOCK_Y)
    screenshot("focused")
    clicked(DOCK_X, DOCK_Y, "right")
    screenshot("menu_minimize")
    menu("Minimize")
    screenshot("hidden")
    clicked(DOCK_X, DOCK_Y)
    screenshot("restored")
    clicked(DOCK_X, DOCK_Y, "right")
    screenshot("menu_close")
    menu("Close")
    screenshot("closed")
    typed("exit")
    screenshot("desktop")
    expect("system_powerdown")
    if position != len(requests):
        raise Phase3Failure("QMP transcript includes unexpected extra commands")

def make_overlay(name: str) -> None:
    if name not in ("test", "negative", "shell-negative"):
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
    if name not in ("round1", "round2", "negative", "shell-negative"):
        raise ValueError("unknown Phase3 QEMU acceptance scenario")
    negative = name == "negative"
    shell_negative = name == "shell-negative"
    mode = "negative" if negative else ("shell-negative" if shell_negative else "test")
    disk = OUT / ("negative.qcow2" if negative else
                  ("shell-negative.qcow2" if shell_negative else "test.qcow2"))
    command = ["bash", str(PHASE / "scripts" / "boot-qemu.sh"), "--mode",
               mode, "--name", name, "--disk", str(disk)]
    serial = OUT / "logs" / f"boot-{name}.log"
    qmp_path = OUT / "logs" / f"qmp-{name}.jsonl"
    started = time.monotonic()
    deadline = started + timeout
    result = BootResult(name=name, previous_nonce=previous)
    record = {"name": name, "mode": mode, "status": "fail", "command": command,
              "serial_log": str(serial.relative_to(OUT)), "qmp_log": str(qmp_path.relative_to(OUT))}
    process = monitor = client = None
    try:
        p2.safe_file(serial, must_exist=False)
        p2.safe_file(qmp_path, must_exist=False)
        qmp_path.write_text("", encoding="utf-8")
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        expected_failure = ("P2:FAIL:NO_DRM" if negative else
                            ("P2:FAIL:DESKTOP_SERVICE" if shell_negative else None))
        monitor = Phase3Monitor(process, serial, deadline, expected_failure)
        if negative:
            monitor.wait_marker("P2:FAIL:NO_DRM")
        elif shell_negative:
            monitor.wait_marker("P3:SWAY_READY")
            monitor.wait_marker("P3:INJECTED:SHELL_FAIL")
            monitor.wait_marker("P3:FAIL:SHELL_EXIT")
            monitor.wait_marker("P2:FAIL:DESKTOP_SERVICE")
        else:
            client = p2.QMP(OUT / f"qmp-{name}.sock", qmp_path, deadline, monitor.check)
            monitor.wait_marker("P3:SHELL_READY")
            # A mapped GTK layer surface and a genuine compositor IPC snapshot
            # must exist before any host-supplied challenge or pointer action.
            monitor.wait_marker(f"P3:PREFERENCE_STATE:compact_dock={'true' if previous else 'false'}")
            ready(monitor.output(), release, source, previous)
            result.screenshots["ready"], first = capture(client, name, "ready", deadline)
            print(f"phase3: {name}: native ready GPU framebuffer captured", flush=True)
            # The challenge is created after the real QMP framebuffer proves
            # a detailed 1024x768 GPU output, never injected via serial/fw_cfg.
            result.nonce = secrets.token_hex(16)
            while result.nonce == previous:
                result.nonce = secrets.token_hex(16)

            def wait_observed(predicate, description: str, *,
                              max_wait: float | None = None) -> None:
                # Post-Close focus is a short asynchronous Sway IPC handoff.
                # An absent observation must fail promptly with source evidence,
                # not consume the entire 300s QEMU boot budget.
                if max_wait is not None and not (0 < max_wait <= 30):
                    raise ValueError("invalid Sway observation timeout")
                observed_deadline = (deadline if max_wait is None else
                                     min(deadline, time.monotonic() + max_wait))
                while not predicate(lines(monitor.output())):
                    monitor.check()
                    if time.monotonic() >= observed_deadline:
                        if max_wait is not None:
                            # Diagnostic is derived only from observed serial
                            # Sway get_tree; never fabricate activation markers.
                            last_ipc = next((
                                item[len("P3:IPC_TREE:"):][:240]
                                for item in reversed(lines(monitor.output()))
                                if item.startswith("P3:IPC_TREE:")
                            ), "none")
                            raise Phase3Failure(
                                "timed out waiting for " + description +
                                "; last source IPC_TREE=" + last_ipc)
                        raise Phase3Failure("timed out waiting for " + description)
                    time.sleep(.05)

            click(client, MENU_X, MENU_Y)
            result.pointer_clicks += 1
            monitor.wait_marker("P3:LAUNCHER_OPEN")
            # A GTK show/allocation marker alone does not prove compositor
            # mapping. Accept the real 13th QMP frame only after the top-left
            # launcher *actually changed* scanout pixels, before any settings click.
            result.screenshots["launcher"], launcher = capture(
                client, name, "launcher", deadline, changed_from=first,
                box=LAUNCHER_BOX, min_changed=.015)
            print(f"phase3: {name}: real GTK launcher scanout visible", flush=True)
            wait_observed(lambda items: any(GTK_TARGET_RE["COMPACT"].fullmatch(m) for m in items),
                          "real mapped GTK Compact checkbox geometry")
            wait_observed(lambda items: any(GTK_TARGET_RE["TERMINAL"].fullmatch(m) for m in items),
                          "real mapped GTK Terminal row geometry")
            result.compact_target = list(gtk_target_from_output(monitor.output(), "COMPACT"))
            result.terminal_target = list(gtk_target_from_output(monitor.output(), "TERMINAL"))
            wait_observed(lambda items: any(item.startswith("P3:APP_INDEX:") for item in items),
                          "real mapped Gio desktop application index")
            _, app_index = app_index_from_output(monitor.output())
            print(f"phase3: {name}: {app_index['displayed_count']} mapped Gio rows from {app_index['eligible_count']} eligible desktop IDs", flush=True)
            if result.terminal_target[1] >= result.compact_target[1]:
                raise Phase3Failure("GTK Terminal row is not above Compact checkbox")
            print(f"phase3: {name}: two mapped GTK widget hitboxes verified", flush=True)
            if previous is None:
                click(client, *result.compact_target[:2])
                result.pointer_clicks += 1
                result.preference_toggled = True
                monitor.wait_marker("P3:PREFERENCE_SAVED:compact_dock=true")
                print(f"phase3: {name}: Compact preference saved by genuine Gtk.CheckButton", flush=True)
                result.screenshots["preferences"], preferences = capture(
                    client, name, "preferences", deadline, changed_from=launcher,
                    box=DOCK_ICON_BOX, min_changed=.025)
                if changed_fraction(launcher, preferences, box=DOCK_ICON_BOX) < .025:
                    raise Phase3Failure("GTK Compact checkbox did not change Dock GPU icon pixels")
            else:
                result.screenshots["preferences"], preferences = capture(
                    client, name, "preferences", deadline, changed_from=first,
                    box=LAUNCHER_BOX, min_changed=.015)
                if changed_fraction(first, preferences, box=LAUNCHER_BOX) < .015:
                    raise Phase3Failure("second-boot GTK launcher was not visible")

            # Pointer must activate the actual GTK Terminal row in the mapped
            # launcher, not guess coordinates or bypass it with host exec/swaymsg.
            click(client, *result.terminal_target[:2])
            result.pointer_clicks += 1
            monitor.wait_marker("P3:APP_LAUNCHED:foot")
            print(f"phase3: {name}: GTK Terminal row launched real foot", flush=True)
            wait_observed(lambda items: len([m for m in items if DISCOVERED_RE.fullmatch(m)]) >= 1,
                          "first Sway terminal")
            result.window_id = next(DISCOVERED_RE.fullmatch(x).group(1)
                                    for x in lines(monitor.output()) if DISCOVERED_RE.fullmatch(x))
            monitor.wait_marker(f"P3:WINDOW_FOCUSED:{result.window_id}")
            # Foot's dark #242424 canvas differs from wallpaper #152437 by
            # only 19 RGB units. Require broad rendered area plus genuine
            # high-contrast terminal detail; retry QMP until BOTH appear.
            result.screenshots["foot"], opened = capture(
                client, name, "foot", deadline, changed_from=first,
                min_changed=.07, pixel_delta=18, min_high_contrast_changed=.005)
            if changed_fraction(first, opened, pixel_delta=18) < .07:
                raise Phase3Failure("launcher Terminal did not change real framebuffer")
            if changed_fraction(first, opened) < .005:
                raise Phase3Failure("launcher Terminal GPU frame lacks high-contrast detail")

            enter_text(client, "p3save " + result.nonce)
            result.keyboard_sent = True
            monitor.wait_marker(f"P3:KEYBOARD_OK:{result.nonce}")
            result.screenshots["saved"], saved_frame = capture(client, name, "saved", deadline)

            # A second genuine terminal is launched by Sway's documented
            # Mod4+Return keyboard binding, not by a guest test bypass.
            press_chord(client, "meta_l", "ret")
            wait_observed(lambda items: len([m for m in items if DISCOVERED_RE.fullmatch(m)]) >= 2,
                          "second live foot container")
            seen_ids = [match.group(1) for item in lines(monitor.output())
                        if (match := DISCOVERED_RE.fullmatch(item))]
            result.second_window_id = next(
                (cid for cid in seen_ids if cid != result.window_id), None)
            if not result.second_window_id:
                raise Phase3Failure("second real foot process has no distinct Sway container")
            monitor.wait_marker(f"P3:WINDOW_ACTIVATED:{result.second_window_id}")
            result.second_opened = True
            result.screenshots["second"], second = capture(
                client, name, "second", deadline, changed_from=saved_frame,
                box=FOOT_CONTENT_BOX, min_changed=.001)
            if changed_fraction(saved_frame, second, box=FOOT_CONTENT_BOX) < .001:
                raise Phase3Failure("second live terminal did not change the GPU framebuffer")

            # Oldest foot is the pinned Dock target. While B is active, click
            # that Dock icon to focus A; both IDs must stay live in Sway IPC.
            click(client)
            result.pointer_clicks += 1
            wait_observed(
                lambda items: any(
                    i > next((j for j, m in enumerate(items)
                              if m == f"P3:WINDOW_ACTIVATED:{result.second_window_id}"), len(items))
                    and m == f"P3:WINDOW_FOCUSED:{result.window_id}"
                    for i, m in enumerate(items)),
                "Dock focus switch from B to A",
            )
            result.screenshots["focused"], focused = capture(
                client, name, "focused", deadline, changed_from=second,
                box=FOOT_CONTENT_BOX, min_changed=.001)
            if changed_fraction(second, focused, box=FOOT_CONTENT_BOX) < .001:
                raise Phase3Failure("Dock focus switch did not alter actual GPU pixels")

            # Real GTK Dock right-click menu (not a guessed Sway command).
            # Calibrate the row from actual QEMU pixels, then click by tablet.
            click(client, button="right")
            result.pointer_clicks += 1
            result.screenshots["menu_minimize"], menu_minimize = capture(
                client, name, "menu_minimize", deadline, changed_from=focused,
                box=MENU_POPUP_BOX, min_changed=.003, popup_operation="Minimize")
            if changed_fraction(focused, menu_minimize, box=MENU_POPUP_BOX) < .003:
                raise Phase3Failure("Dock Minimize context menu was not visible")
            target = click_dock_context_menu(client, focused, menu_minimize, "Minimize")
            result.menu_targets["Minimize"] = list(target)
            result.pointer_clicks += 1
            monitor.wait_marker(f"P3:WINDOW_MINIMIZED:{result.window_id}")
            wait_observed(
                lambda items: any(
                    i > next((j for j, m in enumerate(items)
                              if m == f"P3:WINDOW_MINIMIZED:{result.window_id}"), len(items))
                    and m == f"P3:WINDOW_ACTIVATED:{result.second_window_id}"
                    for i, m in enumerate(items)),
                "surviving terminal B focus after scratchpad hide",
            )
            result.minimized = True
            result.screenshots["hidden"], hidden = capture(
                client, name, "hidden", deadline, changed_from=focused,
                box=FOOT_CONTENT_BOX, min_changed=.001)
            if changed_fraction(focused, hidden, box=FOOT_CONTENT_BOX) < .001:
                raise Phase3Failure("scratchpad hide did not change the framebuffer")

            click(client)
            result.pointer_clicks += 1
            monitor.wait_marker(f"P3:WINDOW_RESTORED:{result.window_id}")
            wait_observed(
                lambda items: any(
                    i > next((j for j, m in enumerate(items)
                              if m == f"P3:WINDOW_RESTORED:{result.window_id}"), len(items))
                    and m == f"P3:WINDOW_ACTIVATED:{result.window_id}"
                    for i, m in enumerate(items)),
                "scratchpad restoration and focus",
            )
            result.restored = True
            result.screenshots["restored"], restored = capture(
                client, name, "restored", deadline, changed_from=hidden,
                box=FOOT_CONTENT_BOX, min_changed=.001)
            if changed_fraction(hidden, restored, box=FOOT_CONTENT_BOX) < .001:
                raise Phase3Failure("scratchpad restore did not change the framebuffer")

            click(client, button="right")
            result.pointer_clicks += 1
            result.screenshots["menu_close"], menu_close = capture(
                client, name, "menu_close", deadline, changed_from=restored,
                box=MENU_POPUP_BOX, min_changed=.003, popup_operation="Close")
            if changed_fraction(restored, menu_close, box=MENU_POPUP_BOX) < .003:
                raise Phase3Failure("Dock Close context menu was not visible")
            target = click_dock_context_menu(client, restored, menu_close, "Close")
            result.menu_targets["Close"] = list(target)
            result.pointer_clicks += 1
            monitor.wait_marker(f"P3:WINDOW_CLOSED:{result.window_id}")
            wait_observed(
                lambda items: any(re.fullmatch(
                    rf"P3:PROCESS_EXITED:{re.escape(result.window_id)}:[1-9][0-9]*", marker)
                    for marker in items),
                "real Dock-closed foot PID exited /proc",
            )
            wait_observed(
                lambda items: any(
                    i > next((j for j, m in enumerate(items)
                              if m == f"P3:WINDOW_CLOSED:{result.window_id}"), len(items))
                    and m == f"P3:WINDOW_ACTIVATED:{result.second_window_id}"
                    for i, m in enumerate(items)),
                "surviving terminal B focus after Sway Dock kill",
                max_wait=15,
            )
            result.close_via_dock = True
            result.screenshots["closed"], closed = capture(
                client, name, "closed", deadline, changed_from=restored,
                box=FOOT_CONTENT_BOX, min_changed=.001)
            if changed_fraction(restored, closed, box=FOOT_CONTENT_BOX) < .001:
                raise Phase3Failure("Sway Dock kill did not remove its actual GPU window")

            # After Sway closes A, surviving foot B is focused; close it via
            # real terminal keyboard so the final desktop is visibly empty.
            enter_text(client, "exit")
            result.exit_sent = True
            monitor.wait_marker(f"P3:WINDOW_CLOSED:{result.second_window_id}")
            # A dark foot window disappearing exposes wallpaper with a
            # 19-unit max-channel contrast; keep the broad 7% area proof.
            result.screenshots["desktop"], desktop = capture(
                client, name, "desktop", deadline, changed_from=closed,
                min_changed=.07, pixel_delta=18)
            if changed_fraction(closed, desktop, pixel_delta=18) < .07:
                raise Phase3Failure("second foot did not visibly exit")
            client.execute("system_powerdown")
            result.powerdown_sent = True
        monitor.wait_exit()
        result.output = monitor.output()
        result.returncode = process.returncode
        if negative:
            validate_negative(result, release, source)
        elif shell_negative:
            validate_shell_negative(result, release, source)
        else:
            validate_positive(result, release, source)
        transcript(qmp_path.read_text(encoding="utf-8"), result.nonce, result.screenshots,
                   preference_toggled=result.preference_toggled,
                   menu_targets=result.menu_targets,
                   compact_target=result.compact_target,
                   terminal_target=result.terminal_target)
        if name == "round2":
            # The first boot saved Compact Dock on disk; the second must show
            # an actually smaller GTK icon in the independent GPU scanout.
            regular_path = OUT / "screenshots" / "round1-ready.ppm"
            compact_path = OUT / "screenshots" / "round2-ready.ppm"
            p2.safe_file(regular_path)
            p2.safe_file(compact_path)
            regular_frame = p2.parse_ppm(regular_path.read_bytes())
            compact_frame = p2.parse_ppm(compact_path.read_bytes())
            delta, before, after = prove_compact_dock_visible(regular_frame, compact_frame)
            print(f"phase3: real Compact Dock GPU proof: change={delta:.4f}, icon pixels {before}->{after}", flush=True)
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
                       "window_id": result.window_id, "second_window_id": result.second_window_id,
                       "second_opened": result.second_opened, "minimized": result.minimized,
                       "restored": result.restored, "close_via_dock": result.close_via_dock,
                       "menu_targets": result.menu_targets,
                       "compact_target": result.compact_target,
                       "terminal_target": result.terminal_target,
                       "pointer_clicks": result.pointer_clicks,
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
        make_overlay("shell-negative")
        previous = None
        for name in ("round1", "round2", "negative", "shell-negative"):
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
        print("phase3: native launcher/dual-window Dock/scratchpad/kill/HID/persistence/negative evidence PASS")
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
