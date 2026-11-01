#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Phase 3 original desktop shell: Wayland layer surfaces and Sway IPC.

This is a desktop component, not a QEMU screenshot fixture. All controls act on
the compositor's real toplevel tree, and applications use Gio's desktop-entry
launcher (or an explicit executable argument vector), never a shell command.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402


FOOT = "/usr/bin/foot"
SWAYMSG = "/usr/bin/swaymsg"
STYLE = "/usr/share/phase3/style.css"
POLL_SECONDS = 1
LAUNCH_TIMEOUT = 30.0
# Launcher is a Wayland layer surface anchored at these pixel margins.
LAUNCHER_LEFT = 14
LAUNCHER_TOP = 43
LAUNCHER_TARGET_INTERVAL_MS = 75
LAUNCHER_TARGET_MAX_CHECKS = 120
# Keep BOTH eligible/displayed Gio ID arrays plus canonical JSON below 4096B.
APP_INDEX_ID_CHAR_BUDGET = 1600
MAX_IPC_WINDOWS = 32
MAX_IPC_SNAPSHOT_BYTES = 4096
PROCESS_EXIT_TIMEOUT = 8.0
MAX_PROCESS_WATCHES = 16
P3_CONFIG = Path.home() / ".config" / "phase3" / "preferences.json"


def emit(marker: str) -> None:
    """Write only independently observed success markers to the session log."""
    print("P3:" + marker, flush=True)


def sway_ipc(*args: str) -> Any:
    if not os.environ.get("SWAYSOCK") or not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("not running inside a Sway Wayland session")
    result = subprocess.run(
        [SWAYMSG, "-r", *args],
        check=False, capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError("swaymsg failed: " + result.stderr.strip()[:240])
    try:
        answer = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Sway returned non-JSON IPC") from exc
    # Sway's read-only GET_* replies are JSON data (often arrays of dicts
    # with NO "success" field), while control commands require an explicit
    # success=true acknowledgment for every selector-matched container.
    is_query = len(args) == 2 and args[0] == "-t" and args[1] in (
        "get_tree", "get_outputs", "get_workspaces",
    )
    if is_query:
        if isinstance(answer, list) and any(
            isinstance(item, dict) and "success" in item and item["success"] is not True
            for item in answer
        ):
            raise RuntimeError("Sway rejected query: " + repr(answer)[:240])
    elif (
        not isinstance(answer, list) or not answer
        or any(not isinstance(item, dict) or item.get("success") is not True
               for item in answer)
    ):
        raise RuntimeError("Sway rejected command: " + repr(answer)[:240])
    return answer


@dataclass(frozen=True)
class Toplevel:
    con_id: int
    app_id: str
    title: str
    focused: bool
    minimized: bool
    scratchpad_state: str = "none"
    visible: bool = True
    pid: int | None = None


@dataclass(frozen=True)
class ProcessWatch:
    con_id: int
    pid: int
    start_time: int
    deadline: float


def parse_toplevels(tree: Any) -> dict[int, Toplevel]:
    """Extract real leaf clients, including hidden scratchpad clients."""
    if not isinstance(tree, dict) or tree.get("type") != "root":
        raise ValueError("Sway IPC did not return a compositor tree")
    found: dict[int, Toplevel] = {}

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        children = [*node.get("nodes", []), *node.get("floating_nodes", [])]
        if node.get("type") in ("con", "floating_con") and not children:
            app_id = node.get("app_id") or ""
            legacy = node.get("window_properties") or {}
            if not app_id and isinstance(legacy, dict):
                app_id = legacy.get("class") or ""
            con_id = node.get("id")
            if app_id and type(con_id) is int and con_id > 0:
                scratch = node.get("scratchpad_state") or "none"
                scratch = scratch if isinstance(scratch, str) else "unknown"
                visible = bool(node.get("visible", False))
                raw_pid = node.get("pid")
                found[con_id] = Toplevel(
                    con_id=con_id,
                    app_id=str(app_id),
                    title=str(node.get("name") or app_id),
                    focused=bool(node.get("focused", False)),
                    minimized=scratch != "none" and not visible,
                    scratchpad_state=scratch,
                    visible=visible,
                    pid=raw_pid if type(raw_pid) is int and raw_pid > 0 else None,
                )
        for child in children:
            visit(child)

    visit(tree)
    return found


def _ipc_rect(value: Any) -> dict[str, int]:
    """Reject incomplete or synthetic geometry before recording Sway proof."""
    if not isinstance(value, dict):
        raise ValueError("Sway IPC geometry is not a rectangle")
    rect = {key: value.get(key) for key in ("x", "y", "width", "height")}
    if any(type(number) is not int for number in rect.values()):
        raise ValueError("Sway IPC geometry must contain integer coordinates")
    if rect["width"] <= 0 or rect["height"] <= 0:
        raise ValueError("Sway IPC geometry has an empty size")
    return rect


def layout_proof(
    outputs: Any, workspaces: Any, top_exclusive: int, dock_exclusive: int,
) -> dict[str, Any]:
    """Confirm actual compositor workspace geometry reserves both mapped bars.

    A GtkLayerShell getter alone proves only the client request. Sway's
    get_workspaces rect is derived from its output usable_area after layer
    exclusive zones, so this combines two independent real runtime APIs.
    """
    if type(top_exclusive) is not int or top_exclusive != 38:
        raise ValueError("topbar did not request a 38-pixel exclusive zone")
    if type(dock_exclusive) is not int or dock_exclusive != 68:
        raise ValueError("Dock did not request a 68-pixel exclusive zone")
    if not isinstance(outputs, list) or not isinstance(workspaces, list):
        raise ValueError("Sway output/workspace IPC replies must be arrays")
    active = [item for item in outputs if isinstance(item, dict)
              and item.get("active") is True]
    if len(active) != 1 or active[0].get("name") != "Virtual-1":
        raise ValueError("not exactly one active native virtio output")
    output = active[0]
    output_rect = _ipc_rect(output.get("rect"))
    if output_rect != {"x": 0, "y": 0, "width": 1024, "height": 768}:
        raise ValueError("Sway output geometry differs from QEMU's 1024x768")
    scale = output.get("scale")
    if type(scale) not in (int, float) or scale != 1:
        raise ValueError("Sway output scale must be 1 for QMP pixel coordinates")
    if output.get("transform") != "normal":
        raise ValueError("Sway output transform must be unrotated for QMP coordinates")
    current_mode = output.get("current_mode")
    if not isinstance(current_mode, dict):
        raise ValueError("Sway output has no current DRM mode")
    mode = {key: current_mode.get(key) for key in ("width", "height")}
    if any(type(value) is not int for value in mode.values()):
        raise ValueError("Sway output mode has malformed dimensions")
    if mode != {"width": 1024, "height": 768}:
        raise ValueError("Sway DRM output mode differs from tested framebuffer")
    visible = [item for item in workspaces if isinstance(item, dict)
               and item.get("visible") is True]
    if len(visible) != 1 or visible[0].get("output") != "Virtual-1":
        raise ValueError("not exactly one visible workspace on virtio GPU")
    workspace = visible[0]
    workspace_name = workspace.get("name")
    if not isinstance(workspace_name, str) or not 0 < len(workspace_name) <= 64:
        raise ValueError("Sway returned a malformed workspace name")
    work_rect = _ipc_rect(workspace.get("rect"))
    # Sway config's "gaps inner 4" legitimately insets the workspace from
    # the full DRM output. The real 1024x768 guest reports x=4/width=1016.
    # Accept only symmetric <=4px horizontal gaps, and tightly bounded
    # vertical extras around the GTK-requested top38/bottom68 reservations.
    # Anything missing either exclusive zone, shifted or clipped fails closed.
    left_inset = work_rect["x"] - output_rect["x"]
    right_inset = (output_rect["x"] + output_rect["width"]
                   - work_rect["x"] - work_rect["width"])
    top_extra = work_rect["y"] - output_rect["y"] - top_exclusive
    bottom_extra = (output_rect["y"] + output_rect["height"]
                    - dock_exclusive - work_rect["y"] - work_rect["height"])
    if not (
        0 <= left_inset == right_inset <= 4
        and 0 <= top_extra <= 4
        and 0 <= bottom_extra <= 12
    ):
        # These rects have already passed the integer-only _ipc_rect gate.
        # Include only bounded numeric geometry, never raw Sway window titles,
        # to diagnose an actually applied layer-shell usable-area mismatch.
        raise ValueError(
            f"Sway workspace does not reserve required bar/Dock areas: work={work_rect} "
            f"output={output_rect} top={top_exclusive} bottom={dock_exclusive}"
        )
    return {
        "source": "sway.get_outputs+get_workspaces",
        "output": output["name"],
        "output_rect": output_rect,
        "scale": float(scale),
        "mode": mode,
        "workspace": workspace_name,
        "workspace_rect": work_rect,
        "top_exclusive": top_exclusive,
        "dock_exclusive": dock_exclusive,
    }


def safe_desktop_id(value: Any) -> bool:
    """Allow loggable XDG desktop IDs, never names, Exec, paths or control bytes."""
    return (
        isinstance(value, str) and 0 < len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9_.+-]+\.desktop", value) is not None
    )


def app_index_payload(
    eligible_ids: list[str], displayed_ids: list[str],
) -> dict[str, Any]:
    """Bound real Gio inventory and the subset actually packed into GTK rows.

    The builtin Terminal row intentionally replaces foot.desktop in the visible
    app list, but foot.desktop may still appear in the source-derived eligible
    inventory. Never invent IDs to cover a missing installed desktop entry.
    """
    if not isinstance(eligible_ids, list) or not 1 <= len(eligible_ids) <= 60:
        raise ValueError("no bounded index of eligible Gio applications")
    if not isinstance(displayed_ids, list) or len(displayed_ids) > 60:
        raise ValueError("unbounded displayed Gio launcher rows")
    if any(not safe_desktop_id(value) for value in [*eligible_ids, *displayed_ids]):
        raise ValueError("malformed XDG desktop ID in Gio index")
    if len(set(eligible_ids)) != len(eligible_ids) or len(set(displayed_ids)) != len(displayed_ids):
        raise ValueError("duplicate XDG desktop ID in Gio index")
    if not set(displayed_ids).issubset(set(eligible_ids)):
        raise ValueError("displayed Gio row has no matching installed index entry")
    if not displayed_ids and "foot.desktop" not in eligible_ids:
        raise ValueError("empty Gio rows without an actual builtin foot.desktop")
    eligible = sorted(eligible_ids)
    displayed = sorted(displayed_ids)
    canonical_lists = json.dumps(
        {"eligible_ids": eligible, "displayed_ids": displayed},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return {
        "displayed_count": len(displayed),
        "displayed_ids": displayed,
        "eligible_count": len(eligible),
        "eligible_ids": eligible,
        "sha256": hashlib.sha256(canonical_lists.encode("utf-8")).hexdigest(),
        "source": "Gio.AppInfo.get_all",
    }


def valid_con_id(con_id: int) -> int:
    # Never interpolate untrusted window names or arbitrary strings into IPC.
    if type(con_id) is not int or not 0 < con_id <= 2**63 - 1:
        raise ValueError("invalid compositor container ID")
    return con_id


def is_terminal(item: Toplevel) -> bool:
    return "foot" in item.app_id.casefold()


def dock_model(windows: dict[int, Toplevel]) -> tuple[Toplevel | None, tuple[Toplevel, ...]]:
    """Pin the oldest real foot container, regardless of Sway tree traversal."""
    terminal = min(
        (item for item in windows.values() if is_terminal(item)),
        key=lambda item: item.con_id, default=None,
    )
    others = tuple(sorted(
        (item for item in windows.values() if not is_terminal(item)),
        key=lambda item: item.con_id,
    ))[:9]
    return terminal, others


def active_toplevel_id(windows: dict[int, Toplevel]) -> int | None:
    focused = [item.con_id for item in windows.values() if item.focused]
    if len(focused) > 1:
        raise ValueError("Sway reported multiple focused client leaves")
    return focused[0] if focused else None


def ipc_signature(windows: dict[int, Toplevel]) -> tuple:
    """Ignore noisy title changes; retain real window/focus/scratchpad state."""
    return tuple(sorted(
        (item.con_id, item.app_id, item.pid, item.focused, item.minimized,
         item.scratchpad_state, item.visible)
        for item in windows.values()
    ))


def ipc_snapshot(windows: dict[int, Toplevel], sequence: int) -> str:
    """Bounded, auditable projection of a fresh Sway get_tree IPC response."""
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("invalid IPC snapshot sequence")
    if len(windows) > MAX_IPC_WINDOWS:
        raise ValueError("Sway tree exceeds bounded IPC snapshot window limit")
    payload = {
        "seq": sequence,
        "source": "sway.get_tree",
        "windows": [
            {
                "id": item.con_id,
                "app_id": item.app_id[:96],
                "pid": item.pid,
                "focused": item.focused,
                "scratchpad_state": item.scratchpad_state[:24],
                "visible": item.visible,
                "minimized": item.minimized,
            }
            for item in sorted(windows.values(), key=lambda item: item.con_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > MAX_IPC_SNAPSHOT_BYTES:
        raise ValueError("Sway IPC snapshot exceeds serial evidence limit")
    return encoded


def window_events(previous: dict[int, Toplevel],
                  current: dict[int, Toplevel]) -> list[str]:
    """Only report transitions independently observed in successive get_tree replies."""
    events: list[str] = []
    for con_id, item in sorted(current.items()):
        old_item = previous.get(con_id)
        if old_item is None:
            if is_terminal(item):
                events.append("WINDOW_DISCOVERED:" + str(con_id) + ":foot")
        elif old_item.minimized != item.minimized:
            kind = "WINDOW_MINIMIZED:" if item.minimized else "WINDOW_RESTORED:"
            events.append(kind + str(con_id))
    for con_id in sorted(previous.keys() - current.keys()):
        events.append("WINDOW_CLOSED:" + str(con_id))
    active = active_toplevel_id(current)
    if active is not None and active != active_toplevel_id(previous):
        events.append("WINDOW_ACTIVATED:" + str(active))
    return events


def parse_proc_stat(content: str) -> tuple[int, str, int]:
    """Extract PID, state and boot-tick starttime from Linux /proc/<pid>/stat.

    The comm field is parenthesized and can itself contain spaces/parentheses.
    Index fields only after its final close parenthesis; starttime is field 22.
    """
    if len(content) > 4096:
        raise ValueError("oversized /proc stat")
    split_at = content.rfind(") ")
    head_at = content.find(" (")
    if head_at <= 0 or split_at <= head_at:
        raise ValueError("invalid /proc stat comm field")
    pid_string = content[:head_at]
    fields = content[split_at + 2:].split()
    if not pid_string.isdecimal() or len(fields) < 20:
        raise ValueError("incomplete /proc stat identity")
    state = fields[0]
    if len(state) != 1 or not state.isascii() or not state.isalpha():
        raise ValueError("invalid /proc process state")
    try:
        start_time = int(fields[19], 10)
    except ValueError as exc:
        raise ValueError("invalid /proc starttime") from exc
    if start_time <= 0:
        raise ValueError("invalid /proc starttime")
    return int(pid_string), state, start_time


def read_proc_state(pid: int) -> tuple[str, int] | None:
    """Read only a bounded kernel /proc record; None means PID directory gone."""
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        raise ValueError("invalid Sway-reported process PID")
    path = Path("/proc") / str(pid) / "stat"
    try:
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("unsafe /proc process path")
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            content = stream.read(4097)
    except (FileNotFoundError, ProcessLookupError):
        return None
    observed_pid, state, start_time = parse_proc_stat(content)
    if observed_pid != pid:
        raise ValueError("/proc PID identity changed")
    return state, start_time


def process_exit_status(pid: int, original_start_time: int) -> str:
    """Never certify a reused PID or a still-running process as terminated."""
    if type(original_start_time) is not int or original_start_time <= 0:
        raise ValueError("invalid observed process starttime")
    current = read_proc_state(pid)
    if current is None:
        return "exited"
    state, start_time = current
    if start_time != original_start_time:
        return "reused"
    return "exited" if state == "Z" else "running"


def read_preferences() -> dict[str, bool]:
    defaults = {"compact_dock": False}
    try:
        if P3_CONFIG.is_symlink() or P3_CONFIG.stat().st_size > 4096:
            return defaults
        data = json.loads(P3_CONFIG.read_text(encoding="utf-8"))
        if isinstance(data, dict) and type(data.get("compact_dock")) is bool:
            defaults["compact_dock"] = data["compact_dock"]
    except (OSError, ValueError, UnicodeError):
        pass
    return defaults


def save_preferences(values: dict[str, bool]) -> None:
    """Atomic, user-scoped preference update; never follow a file symlink."""
    directory = P3_CONFIG.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or P3_CONFIG.is_symlink():
        raise OSError("unsafe preferences path")
    temp_path = directory / (".preferences-" + str(os.getpid()) + ".tmp")
    if temp_path.exists() or temp_path.is_symlink():
        raise OSError("temporary preferences file exists")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"compact_dock": values["compact_dock"]}, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, P3_CONFIG)
        dir_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_descriptor)
        finally:
            os.close(dir_descriptor)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def styled(widget: Gtk.Widget, style_class: str) -> Gtk.Widget:
    widget.get_style_context().add_class(style_class)
    return widget


def layer_window(
    title: str, layer: GtkLayerShell.Layer,
    edges: tuple[GtkLayerShell.Edge, ...], width: int, height: int,
) -> Gtk.Window:
    window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    window.set_title(title)
    window.set_decorated(False)
    window.set_resizable(False)
    window.set_default_size(width, height)
    GtkLayerShell.init_for_window(window)
    GtkLayerShell.set_layer(window, layer)
    GtkLayerShell.set_namespace(window, "phase3-shell")
    for edge in edges:
        GtkLayerShell.set_anchor(window, edge, True)
    return window


class Phase3Shell:
    def __init__(self) -> None:
        if os.geteuid() == 0:
            raise RuntimeError("desktop shell must not run as root")
        if not os.environ.get("XDG_RUNTIME_DIR"):
            raise RuntimeError("missing user runtime directory")
        self.preferences = read_preferences()
        self.toplevels: dict[int, Toplevel] = {}
        self._dock_signature: tuple[Any, ...] | None = None
        self._pending_focus: set[int] = set()
        self._pending_process_exits: dict[int, ProcessWatch] = {}
        self._launch_requests: list[tuple[float, str]] = []
        self._ready = False
        self._startup_checks = 0
        # Bounded real startup diagnostics; never imply shell readiness.
        self._last_startup_warning: float | None = None
        self._startup_last_failure = "waiting_for_mapped_layer_surfaces"
        self._last_ipc_warning = 0.0
        self._ipc_sequence = 0
        self._launcher_target_generation = 0
        self._launcher_target_checks = 0
        self.apps = self._desktop_apps()
        self._load_style()
        self._make_bar()
        self._make_dock()
        self._make_launcher()
        self.bar.show_all()
        self.dock.show_all()
        self._refresh_clock()
        self._refresh_dock()
        GLib.timeout_add(250, self._check_ready)
        GLib.timeout_add_seconds(POLL_SECONDS, self._poll_windows)
        GLib.timeout_add_seconds(15, self._refresh_clock)

    def _load_style(self) -> None:
        css = Gtk.CssProvider()
        css.load_from_path(STYLE)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)

    def _make_bar(self) -> None:
        self.bar = layer_window(
            "Phase3 Top Bar", GtkLayerShell.Layer.TOP,
            (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT),
            1024, 38,
        )
        GtkLayerShell.set_exclusive_zone(self.bar, 38)
        styled(self.bar, "phase3-bar")
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        outer.set_border_width(5)
        self.bar.add(outer)
        brand = styled(Gtk.Label(label="◆  Phase3"), "phase3-brand")
        outer.pack_start(brand, False, False, 9)
        self.launcher_button = styled(Gtk.Button(label="Applications"), "phase3-bar-button")
        self.launcher_button.connect("clicked", lambda *_: self.toggle_launcher())
        outer.pack_start(self.launcher_button, False, False, 0)
        window_button = styled(Gtk.Button(label="Window"), "phase3-bar-button")
        window_button.connect("clicked", self._window_menu)
        outer.pack_start(window_button, False, False, 0)
        outer.pack_start(Gtk.Label(), True, True, 0)
        self.active_title = styled(Gtk.Label(label="Desktop"), "phase3-current-app")
        self.active_title.set_max_width_chars(30)
        self.active_title.set_ellipsize(Pango.EllipsizeMode.END)
        outer.pack_end(self.active_title, False, False, 10)
        self.clock = styled(Gtk.Label(), "phase3-clock")
        outer.pack_end(self.clock, False, False, 8)

    def _make_dock(self) -> None:
        self.dock = layer_window(
            "Phase3 Dock", GtkLayerShell.Layer.TOP,
            (GtkLayerShell.Edge.BOTTOM,), 88, 60,
        )
        GtkLayerShell.set_margin(self.dock, GtkLayerShell.Edge.BOTTOM, 8)
        GtkLayerShell.set_exclusive_zone(self.dock, 68)
        styled(self.dock, "phase3-dock")
        self.dock_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.dock_box.set_border_width(6)
        self.dock.add(self.dock_box)

    def _make_launcher(self) -> None:
        self.launcher = layer_window(
            "Phase3 Application Launcher", GtkLayerShell.Layer.OVERLAY,
            (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.LEFT), 380, 432,
        )
        GtkLayerShell.set_margin(self.launcher, GtkLayerShell.Edge.TOP, LAUNCHER_TOP)
        GtkLayerShell.set_margin(self.launcher, GtkLayerShell.Edge.LEFT, LAUNCHER_LEFT)
        # Zone 0 makes Sway move this overlay BELOW the topbar's 38px
        # exclusive area: a TOP margin of 43 then becomes pixel y=81, while
        # GTK widget coordinates are relative to the launcher itself. -1
        # keeps our non-reserving popup anchored to the full output (y=43),
        # so mapped Gtk.Widget centers match real QMP framebuffer pixels.
        GtkLayerShell.set_exclusive_zone(self.launcher, -1)
        if hasattr(GtkLayerShell, "set_keyboard_mode"):
            GtkLayerShell.set_keyboard_mode(
                self.launcher, GtkLayerShell.KeyboardMode.ON_DEMAND
            )
        elif hasattr(GtkLayerShell, "set_keyboard_interactivity"):
            GtkLayerShell.set_keyboard_interactivity(self.launcher, True)
        styled(self.launcher, "phase3-launcher")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_border_width(16)
        self.launcher.add(body)
        title_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_line.pack_start(styled(Gtk.Label(label="Applications"), "phase3-heading"), False, False, 0)
        title_line.pack_start(Gtk.Label(), True, True, 0)
        hide_button = styled(Gtk.Button(label="✕"), "phase3-close-button")
        hide_button.connect("clicked", lambda *_: self.hide_launcher())
        title_line.pack_end(hide_button, False, False, 0)
        body.pack_start(title_line, False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search installed applications")
        self.search.connect("search-changed", lambda *_: self._fill_launcher())
        self.search.connect("key-press-event", self._launcher_key)
        body.pack_start(self.search, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(280)
        self.app_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroll.add_with_viewport(self.app_list)
        body.pack_start(scroll, True, True, 0)
        compact = Gtk.CheckButton(label="Compact Dock")
        # Keep the clickable GTK allocation around the painted checkbox/label,
        # not stretched across 348px of otherwise empty launcher space.
        compact.set_halign(Gtk.Align.START)
        compact.set_active(self.preferences["compact_dock"])
        compact.connect("toggled", self._compact_toggled)
        self.compact_toggle = compact
        body.pack_start(compact, False, False, 0)
        self._fill_launcher()

    @staticmethod
    def _desktop_apps() -> list[Gio.AppInfo]:
        # Capture each display name once while its errors are contained. A
        # malformed desktop entry can return a non-string name, or fail on
        # a subsequent getter; neither may crash the desktop at startup.
        named: list[tuple[str, Gio.AppInfo]] = []
        for app in Gio.AppInfo.get_all():
            try:
                if not app.should_show():
                    continue
                name = app.get_name()
                if isinstance(name, str) and name:
                    named.append((name.casefold(), app))
            except (GLib.Error, AttributeError, TypeError, ValueError):
                continue
        named.sort(key=lambda item: item[0])
        return [app for _, app in named]

    def _fill_launcher(self) -> None:
        self.terminal_row = None
        # Only rows that are truly packed into the visible Gio section count as
        # indexed desktop applications; the hand-built Terminal row is separate.
        self._eligible_ids: list[str] = []
        self._displayed_ids: list[str] = []
        eligible: set[str] = set()
        displayed: set[str] = set()
        eligible_chars = 0
        for child in self.app_list.get_children():
            self.app_list.remove(child)
        query = self.search.get_text().strip().casefold()
        if not query or "terminal".find(query) >= 0 or "foot".find(query) >= 0:
            button = self._launcher_row("Terminal", "utilities-terminal-symbolic")
            self.terminal_row = button
            button.connect("clicked", lambda *_: self.launch_terminal())
            self.app_list.pack_start(button, False, False, 0)
        count = 0
        for app in self.apps:
            try:
                # The installed desktop-entry database is an input boundary:
                # getters can raise or return malformed types even after
                # the startup index validated the entry earlier.
                name = app.get_name()
                desktop_id = app.get_id() or ""
                icon = app.get_icon()
                if not isinstance(name, str) or not name or not safe_desktop_id(desktop_id):
                    continue
                if desktop_id not in eligible:
                    # The canonical marker includes this ID in eligible AND
                    # possibly displayed lists. Cap the combined serial size
                    # deterministically before packing any unbound Gio row.
                    if (len(eligible) >= 60 or
                            eligible_chars + len(desktop_id) > APP_INDEX_ID_CHAR_BUDGET):
                        continue
                    self._eligible_ids.append(desktop_id)
                    eligible.add(desktop_id)
                    eligible_chars += len(desktop_id)
                if desktop_id.casefold().startswith("foot."):
                    continue
                if desktop_id in displayed or count >= 60:
                    continue
                if query and query not in name.casefold():
                    continue
                button = self._launcher_row(name, "application-x-executable-symbolic", icon)
                button.connect("clicked", lambda _btn, item=app: self._launch_desktop_app(item))
                self.app_list.pack_start(button, False, False, 0)
                self._displayed_ids.append(desktop_id)
                displayed.add(desktop_id)
                count += 1
            except (GLib.Error, AttributeError, TypeError, ValueError):
                # Skip only this malformed entry; real Gio.AppInfo.launch is
                # still the sole route for installed desktop applications.
                continue
        self.app_list.show_all()

    @staticmethod
    def _launcher_row(name: str, fallback_icon: str, icon: Gio.Icon | None = None) -> Gtk.Button:
        button = styled(Gtk.Button(), "phase3-launcher-row")
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        if icon is not None:
            picture = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.DND)
        else:
            picture = Gtk.Image.new_from_icon_name(fallback_icon, Gtk.IconSize.DND)
        content.pack_start(picture, False, False, 3)
        label = Gtk.Label(label=name, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        content.pack_start(label, True, True, 0)
        button.add(content)
        return button

    def _launcher_key(self, _entry: Gtk.SearchEntry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.hide_launcher()
            return True
        if event.keyval == Gdk.KEY_Return:
            children = self.app_list.get_children()
            if children:
                children[0].clicked()
                return True
        return False

    def _launcher_widget_target(self, widget: Gtk.Widget | None) -> tuple[int, int, int, int] | None:
        """Return a mapped GTK widget's true clickable center in QEMU pixels.

        GTK3 PyGObject translate_coordinates returns (x, y) or None, relative
        to the launcher allocation. Its layer-shell TOP/LEFT margins supply
        the actual compositor-space origin, never a guessed widget row.
        """
        if widget is None or not self.launcher.get_mapped() or not widget.get_mapped():
            return None
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        if width < 12 or height < 12:
            return None
        point = widget.translate_coordinates(self.launcher, width // 2, height // 2)
        if point is None or len(point) != 2:
            return None
        rel_x, rel_y = point
        launcher_width = self.launcher.get_allocated_width()
        launcher_height = self.launcher.get_allocated_height()
        # A center can be mapped yet clipped. Prove the complete allocation
        # fits within the real launcher surface before advertising a hitbox.
        rel_left = rel_x - width // 2
        rel_top = rel_y - height // 2
        if not (
            0 <= rel_left and rel_left + width <= launcher_width
            and 0 <= rel_top and rel_top + height <= launcher_height
        ):
            return None
        center_x = LAUNCHER_LEFT + rel_x
        center_y = LAUNCHER_TOP + rel_y
        screen = Gdk.Screen.get_default()
        if screen is None or not (
            0 <= center_x < screen.get_width() and 0 <= center_y < screen.get_height()
        ):
            return None
        return center_x, center_y, width, height

    def _report_launcher_targets(self, generation: int) -> bool:
        """Log both targets exactly once after the real Wayland popup maps."""
        if generation != self._launcher_target_generation or not self.launcher.get_visible():
            return False
        self._launcher_target_checks += 1
        compact = self._launcher_widget_target(self.compact_toggle)
        terminal = self._launcher_widget_target(self.terminal_row)
        if compact is not None and terminal is not None:
            try:
                indexed = app_index_payload(self._eligible_ids, self._displayed_ids)
                index_json = json.dumps(
                    indexed, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                )
                if len(index_json.encode("utf-8")) > 4096:
                    raise ValueError("displayed desktop ID index exceeds 4096 bytes")
            except ValueError as exc:
                # Do not advertise a fabricated inventory or clickable controls
                # when the actual installed Gio rows cannot be audited.
                print("phase3: launcher index not ready: " + str(exc),
                      file=sys.stderr, flush=True)
                return False
            emit("COMPACT_TARGET:" + ":".join(str(value) for value in compact))
            emit("TERMINAL_TARGET:" + ":".join(str(value) for value in terminal))
            emit("APP_INDEX:" + index_json)
            return False
        if self._launcher_target_checks >= LAUNCHER_TARGET_MAX_CHECKS:
            print("phase3: launcher controls never mapped with valid allocations",
                  file=sys.stderr, flush=True)
            return False
        return True

    def toggle_launcher(self) -> None:
        if self.launcher.get_visible():
            self.hide_launcher()
            return
        self.search.set_text("")
        self._fill_launcher()
        self.launcher.show_all()
        self.launcher.present()
        self.search.grab_focus()
        self._launcher_target_generation += 1
        self._launcher_target_checks = 0
        emit("LAUNCHER_OPEN")
        GLib.timeout_add(LAUNCHER_TARGET_INTERVAL_MS, self._report_launcher_targets,
                         self._launcher_target_generation)

    def hide_launcher(self) -> None:
        # Any post-map callback queued for the old popup must never advertise
        # stale hitboxes after the user hides or reopens it.
        self._launcher_target_generation += 1
        self.launcher.hide()

    def _compact_toggled(self, control: Gtk.CheckButton) -> None:
        self.preferences["compact_dock"] = control.get_active()
        try:
            save_preferences(self.preferences)
            emit("PREFERENCE_SAVED:compact_dock=" + str(self.preferences["compact_dock"]).lower())
        except OSError as exc:
            print("phase3: could not save preference: " + str(exc), file=sys.stderr, flush=True)
        self._dock_signature = None
        self._refresh_dock()

    def launch_terminal(self) -> None:
        self.hide_launcher()
        existing, _ = dock_model(self.toplevels)
        if existing is not None:
            self.focus_window(existing.con_id)
            return
        try:
            subprocess.Popen(
                [FOOT], close_fds=True, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._launch_requests.append((time.monotonic(), "foot"))
        except OSError as exc:
            print("phase3: terminal launch failed: " + str(exc), file=sys.stderr, flush=True)

    def _launch_desktop_app(self, app: Gio.AppInfo) -> None:
        self.hide_launcher()
        try:
            # Gio invokes the desktop-entry Exec specification correctly;
            # do not pass user-controlled desktop Exec lines to a shell.
            if not app.launch([], None):
                raise RuntimeError("desktop entry did not launch")
        except (GLib.Error, RuntimeError) as exc:
            print("phase3: application launch failed: " + str(exc), file=sys.stderr, flush=True)

    def _sway_command(self, con_id: int, operation: str) -> bool:
        con_id = valid_con_id(con_id)
        allowed = {"focus", "kill", "move scratchpad", "scratchpad show"}
        if operation not in allowed:
            raise ValueError("unsupported window operation")
        try:
            sway_ipc("[con_id=" + str(con_id) + "] " + operation)
            return True
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            print("phase3: compositor command failed: " + str(exc), file=sys.stderr, flush=True)
            return False

    def focus_window(self, con_id: int) -> None:
        item = self.toplevels.get(valid_con_id(con_id))
        if item is None:
            return
        command = "scratchpad show" if item.minimized else "focus"
        if self._sway_command(item.con_id, command):
            self._pending_focus.add(item.con_id)
            GLib.timeout_add(175, self._poll_once)

    def minimize_window(self, con_id: int) -> None:
        self._sway_command(con_id, "move scratchpad")
        GLib.timeout_add(175, self._poll_once)

    def close_window(self, con_id: int) -> None:
        con_id = valid_con_id(con_id)
        item = self.toplevels.get(con_id)
        watch: ProcessWatch | None = None
        if item is not None and item.pid is not None:
            try:
                identity = read_proc_state(item.pid)
                if identity is not None and identity[0] != "Z":
                    watch = ProcessWatch(
                        con_id, item.pid, identity[1],
                        time.monotonic() + PROCESS_EXIT_TIMEOUT,
                    )
            except (OSError, ValueError) as exc:
                print("phase3: cannot bind Close to a live PID: " + str(exc),
                      file=sys.stderr, flush=True)
        if self._sway_command(con_id, "kill"):
            # A successful command is NOT itself proof of exit. Wait until
            # actual Sway tree removal AND same-process /proc termination.
            if watch is not None and len(self._pending_process_exits) < MAX_PROCESS_WATCHES:
                self._pending_process_exits[con_id] = watch
        GLib.timeout_add(175, self._poll_once)

    def _window_menu(self, widget: Gtk.Button) -> None:
        focused = next((item for item in self.toplevels.values() if item.focused), None)
        menu = Gtk.Menu()
        if focused is None:
            item = Gtk.MenuItem(label="No active application")
            item.set_sensitive(False)
            menu.append(item)
        else:
            for title, callback in (
                ("Bring to Front", self.focus_window),
                ("Minimize", self.minimize_window),
                ("Close Window", self.close_window),
            ):
                entry = Gtk.MenuItem(label=title)
                entry.connect("activate", lambda _entry, cb=callback, cid=focused.con_id: cb(cid))
                menu.append(entry)
        menu.show_all()
        menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _dock_right_click(self, _button: Gtk.Button, event: Gdk.EventButton, con_id: int) -> bool:
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        item = self.toplevels.get(con_id)
        if item is None:
            return False
        for title, callback in (
            ("Restore" if item.minimized else "Bring to Front", self.focus_window),
            ("Minimize", self.minimize_window),
            ("Close", self.close_window),
        ):
            row = Gtk.MenuItem(label=title)
            row.connect("activate", lambda _row, cb=callback, cid=con_id: cb(cid))
            menu.append(row)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _refresh_dock(self) -> None:
        terminal, other = dock_model(self.toplevels)
        signature = (
            self.preferences["compact_dock"],
            None if terminal is None else (terminal.con_id, terminal.focused, terminal.minimized),
            tuple((w.con_id, w.app_id, w.focused, w.minimized) for w in other),
        )
        if signature == self._dock_signature:
            return
        self._dock_signature = signature
        for child in self.dock_box.get_children():
            self.dock_box.remove(child)
        terminal_button = self._dock_button(
            "Terminal", "utilities-terminal-symbolic",
            bool(terminal and terminal.focused), bool(terminal),
        )
        terminal_button.connect("clicked", lambda *_: self.launch_terminal())
        if terminal is not None:
            terminal_button.connect(
                "button-press-event", self._dock_right_click, terminal.con_id,
            )
        self.dock_box.pack_start(terminal_button, False, False, 0)
        for item in other:
            label = item.app_id.replace(".", " ").split()[0][:14] or "App"
            button = self._dock_button(
                label, "application-x-executable-symbolic",
                item.focused, True,
            )
            button.connect("clicked", lambda _button, cid=item.con_id: self.focus_window(cid))
            button.connect("button-press-event", self._dock_right_click, item.con_id)
            self.dock_box.pack_start(button, False, False, 0)
        self.dock_box.show_all()

    def _dock_button(self, name: str, icon_name: str, active: bool, running: bool) -> Gtk.Button:
        button = styled(Gtk.Button(), "phase3-dock-button")
        if active:
            styled(button, "phase3-active")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        size = Gtk.IconSize.BUTTON if self.preferences["compact_dock"] else Gtk.IconSize.DND
        content.pack_start(
            Gtk.Image.new_from_icon_name(icon_name, size), True, True, 0,
        )
        label = Gtk.Label(label=("● " if running else "") + name)
        styled(label, "phase3-dock-caption")
        content.pack_start(label, False, False, 0)
        if running:
            styled(content, "phase3-running")
        button.add(content)
        # The sole pinned Terminal button is centered at 512,730 in 1024x768.
        button.set_size_request(60 if self.preferences["compact_dock"] else 74, 46)
        return button

    def _refresh_clock(self) -> bool:
        self.clock.set_text(datetime.now().strftime("%a %d %b  %H:%M"))
        return True

    def _record_ipc_snapshot(self, observed: dict[int, Toplevel]) -> None:
        # Called only with parse_toplevels() from this process's actual Sway
        # get_tree response; serial transcript is bound by the host manifest.
        try:
            payload = ipc_snapshot(observed, self._ipc_sequence + 1)
        except ValueError as exc:
            print("phase3: bounded IPC snapshot unavailable: " + str(exc),
                  file=sys.stderr, flush=True)
            return
        self._ipc_sequence += 1
        emit("IPC_TREE:" + payload)

    def _poll_once(self) -> bool:
        # GLib.timeout_add callbacks repeat when returning True. Keep the
        # normal 1s poll, but schedule each post-action fast probe only once.
        self._poll_windows()
        return False

    def _check_process_exits(self, observed: dict[int, Toplevel]) -> None:
        for con_id, watch in list(self._pending_process_exits.items()):
            if time.monotonic() > watch.deadline:
                print("phase3: Close process proof timed out for " + str(con_id),
                      file=sys.stderr, flush=True)
                del self._pending_process_exits[con_id]
                continue
            if con_id in observed:
                # A closed/disappeared Sway leaf must precede any PID proof.
                continue
            try:
                status = process_exit_status(watch.pid, watch.start_time)
            except (OSError, ValueError) as exc:
                print("phase3: Close /proc probe failed: " + str(exc),
                      file=sys.stderr, flush=True)
                del self._pending_process_exits[con_id]
                continue
            if status == "reused":
                # PID number is alive again but belongs to a different process.
                print("phase3: PID reuse prevents Close exit proof: " + str(watch.pid),
                      file=sys.stderr, flush=True)
                del self._pending_process_exits[con_id]
            elif status == "exited":
                emit("PROCESS_EXITED:" + str(con_id) + ":" + str(watch.pid))
                del self._pending_process_exits[con_id]

    def _check_ready(self) -> bool:
        if self._ready:
            return False
        self._startup_checks += 1
        if self.bar.get_mapped() and self.dock.get_mapped():
            try:
                if not GtkLayerShell.is_layer_window(self.bar):
                    raise RuntimeError("topbar is not a layer surface")
                if not GtkLayerShell.is_layer_window(self.dock):
                    raise RuntimeError("Dock is not a layer surface")
                observed = parse_toplevels(sway_ipc("-t", "get_tree"))
                proof = layout_proof(
                    sway_ipc("-t", "get_outputs"),
                    sway_ipc("-t", "get_workspaces"),
                    GtkLayerShell.get_exclusive_zone(self.bar),
                    GtkLayerShell.get_exclusive_zone(self.dock),
                )
                layout_json = json.dumps(
                    proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                )
                if len(layout_json.encode("utf-8")) > 512:
                    raise ValueError("Sway output geometry proof is unbounded")
                self.toplevels = observed
                self._ready = True
                self._record_ipc_snapshot(observed)
                emit("IPC_LAYOUT:" + layout_json)
                emit("LAYER_SHELL_READY")
                emit("SHELL_READY")
                emit("PREFERENCE_STATE:compact_dock=" + str(self.preferences["compact_dock"]).lower())
                self._refresh_dock()
                return False
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                # Real Sway layout/IPC rejection was previously swallowed for
                # 30 seconds, obscuring the cause of genuine GUI startup failure.
                # One bounded stderr line immediately, then at most every 10s.
                self._startup_last_failure = repr(
                    f"{type(exc).__name__}: {exc}"
                )[:180]
                now = time.monotonic()
                if (self._last_startup_warning is None or
                        now - self._last_startup_warning >= 10):
                    print("phase3: SHELL_READY_WAIT:" + self._startup_last_failure,
                          file=sys.stderr, flush=True)
                    self._last_startup_warning = now
        if self._startup_checks >= 120:
            print("phase3: SHELL_READY_TIMEOUT:"
                  f"checks={self._startup_checks}:"
                  f"bar_mapped={self.bar.get_mapped()}:"
                  f"dock_mapped={self.dock.get_mapped()}:"
                  f"last={self._startup_last_failure}",
                  file=sys.stderr, flush=True)
            emit("FAIL:SHELL_STARTUP")
            Gtk.main_quit()
            self._ready = False
            return False
        return True

    def _poll_windows(self) -> bool:
        if not self._ready:
            return False if self._startup_checks >= 120 else True
        old = self.toplevels
        try:
            current = parse_toplevels(sway_ipc("-t", "get_tree"))
            changes = window_events(old, current)
            changed = ipc_signature(old) != ipc_signature(current)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            now = time.monotonic()
            if now - self._last_ipc_warning >= 10:
                print("phase3: IPC refresh failed: " + str(exc), file=sys.stderr, flush=True)
                self._last_ipc_warning = now
            return True
        # Snapshot BEFORE evidence markers so the host can correlate each
        # observed transition to the exact Sway container/PID/focus state.
        pending_observed = any(cid in current and current[cid].focused
                               for cid in self._pending_focus)
        if changed or pending_observed:
            self._record_ipc_snapshot(current)
        for marker in changes:
            emit(marker)
        for con_id, item in sorted(current.items()):
            if con_id not in old and is_terminal(item) and self._launch_requests:
                self._launch_requests.pop(0)
                emit("APP_LAUNCHED:foot")
                # A launched client counts as focused only after Sway itself
                # reports focus, not merely after spawning it.
                self._pending_focus.add(con_id)
            if con_id in self._pending_focus and item.focused:
                emit("WINDOW_FOCUSED:" + str(con_id))
                self._pending_focus.remove(con_id)
        for con_id in old.keys() - current.keys():
            self._pending_focus.discard(con_id)
        self._check_process_exits(current)
        now = time.monotonic()
        self._launch_requests = [
            request for request in self._launch_requests
            if now - request[0] <= LAUNCH_TIMEOUT
        ]
        self.toplevels = current
        focused = next((item for item in current.values() if item.focused), None)
        self.active_title.set_text(focused.title if focused else "Desktop")
        self._refresh_dock()
        return True


def main() -> int:
    try:
        shell = Phase3Shell()
        Gtk.main()
        return 0 if shell._ready else 1
    except (RuntimeError, OSError, GLib.Error) as exc:
        emit("FAIL:SHELL_STARTUP")
        print("phase3: " + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
