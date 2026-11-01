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
import json
import os
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
    if isinstance(answer, list) and any(
        isinstance(item, dict) and not item.get("success", False)
        for item in answer
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
                scratch = node.get("scratchpad_state", "none") != "none"
                found[con_id] = Toplevel(
                    con_id=con_id,
                    app_id=str(app_id),
                    title=str(node.get("name") or app_id),
                    focused=bool(node.get("focused", False)),
                    minimized=scratch and not bool(node.get("visible", False)),
                )
        for child in children:
            visit(child)

    visit(tree)
    return found


def valid_con_id(con_id: int) -> int:
    # Never interpolate untrusted window names or arbitrary strings into IPC.
    if type(con_id) is not int or not 0 < con_id <= 2**63 - 1:
        raise ValueError("invalid compositor container ID")
    return con_id


def is_terminal(item: Toplevel) -> bool:
    return "foot" in item.app_id.casefold()


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
        self._launch_requests: list[tuple[float, str]] = []
        self._ready = False
        self._startup_checks = 0
        self._last_ipc_warning = 0.0
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
        GtkLayerShell.set_exclusive_zone(self.dock, 0)
        styled(self.dock, "phase3-dock")
        self.dock_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.dock_box.set_border_width(6)
        self.dock.add(self.dock_box)

    def _make_launcher(self) -> None:
        self.launcher = layer_window(
            "Phase3 Application Launcher", GtkLayerShell.Layer.OVERLAY,
            (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.LEFT), 380, 432,
        )
        GtkLayerShell.set_margin(self.launcher, GtkLayerShell.Edge.TOP, 43)
        GtkLayerShell.set_margin(self.launcher, GtkLayerShell.Edge.LEFT, 14)
        GtkLayerShell.set_exclusive_zone(self.launcher, 0)
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
        compact.set_active(self.preferences["compact_dock"])
        compact.connect("toggled", self._compact_toggled)
        body.pack_start(compact, False, False, 0)
        self._fill_launcher()

    @staticmethod
    def _desktop_apps() -> list[Gio.AppInfo]:
        apps: list[Gio.AppInfo] = []
        for app in Gio.AppInfo.get_all():
            try:
                if app.should_show() and app.get_name():
                    apps.append(app)
            except (GLib.Error, AttributeError):
                continue
        return sorted(apps, key=lambda a: a.get_name().casefold())

    def _fill_launcher(self) -> None:
        for child in self.app_list.get_children():
            self.app_list.remove(child)
        query = self.search.get_text().strip().casefold()
        if not query or "terminal".find(query) >= 0 or "foot".find(query) >= 0:
            button = self._launcher_row("Terminal", "utilities-terminal-symbolic")
            button.connect("clicked", lambda *_: self.launch_terminal())
            self.app_list.pack_start(button, False, False, 0)
        count = 0
        for app in self.apps:
            if count >= 60:
                break
            name = app.get_name()
            desktop_id = app.get_id() or ""
            if desktop_id.casefold().startswith("foot."):
                continue
            if query and query not in name.casefold():
                continue
            button = self._launcher_row(name, "application-x-executable-symbolic", app.get_icon())
            button.connect("clicked", lambda _btn, item=app: self._launch_desktop_app(item))
            self.app_list.pack_start(button, False, False, 0)
            count += 1
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

    def toggle_launcher(self) -> None:
        if self.launcher.get_visible():
            self.hide_launcher()
            return
        self.search.set_text("")
        self._fill_launcher()
        self.launcher.show_all()
        self.launcher.present()
        self.search.grab_focus()
        emit("LAUNCHER_OPEN")

    def hide_launcher(self) -> None:
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
        existing = next((w for w in self.toplevels.values() if is_terminal(w)), None)
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
            GLib.timeout_add(175, self._poll_windows)

    def minimize_window(self, con_id: int) -> None:
        self._sway_command(con_id, "move scratchpad")
        GLib.timeout_add(175, self._poll_windows)

    def close_window(self, con_id: int) -> None:
        self._sway_command(con_id, "kill")
        GLib.timeout_add(175, self._poll_windows)

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
        terminal = next((w for w in self.toplevels.values() if is_terminal(w)), None)
        other = sorted(
            (w for w in self.toplevels.values() if not is_terminal(w)),
            key=lambda w: w.con_id,
        )
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
        for item in other[:9]:
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
                self.toplevels = observed
                self._ready = True
                emit("LAYER_SHELL_READY")
                emit("SHELL_READY")
                emit("PREFERENCE_STATE:compact_dock=" + str(self.preferences["compact_dock"]).lower())
                self._refresh_dock()
                return False
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                pass
        if self._startup_checks >= 120:
            emit("FAIL:SHELL_STARTUP")
            Gtk.main_quit()
            self._ready = False
            return False
        return True

    def _poll_windows(self) -> bool:
        if not self._ready:
            return False if self._startup_checks >= 120 else True
        try:
            current = parse_toplevels(sway_ipc("-t", "get_tree"))
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            now = time.monotonic()
            if now - self._last_ipc_warning >= 10:
                print("phase3: IPC refresh failed: " + str(exc), file=sys.stderr, flush=True)
                self._last_ipc_warning = now
            return True
        old = self.toplevels
        for con_id, item in current.items():
            if con_id not in old and is_terminal(item):
                if self._launch_requests:
                    self._launch_requests.pop(0)
                    emit("APP_LAUNCHED:foot")
                    # A launched client counts as focused only after Sway
                    # itself reports focus, not merely after spawning it.
                    self._pending_focus.add(con_id)
            if con_id in self._pending_focus and item.focused:
                emit("WINDOW_FOCUSED:" + str(con_id))
                self._pending_focus.remove(con_id)
        for con_id in old.keys() - current.keys():
            emit("WINDOW_CLOSED:" + str(con_id))
            self._pending_focus.discard(con_id)
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
