#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Phase 3 Files: a home-confined, read-only GTK3 file browser.

All filesystem access passes through SafeHome's descriptor-relative NOFOLLOW
operations.  This UI never launches files, follows links, or writes user data.
"""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from safe_paths import Entry, SafeHome, SafePathError


APPLICATION_ID = "org.phase3.Files"
MAX_ENTRIES = 256
MAX_PREVIEW_BYTES = 65_536
CSS_PATH = "/usr/share/phase3/files.css"


def display_name(name: str, limit: int = 120) -> str:
    """Render filenames as plain, bounded text without terminal control chars."""
    safe = "".join(char if char.isprintable() else "\N{REPLACEMENT CHARACTER}"
                   for char in name)
    return safe[:limit] or "(unnamed)"


def visible_entries(entries: list[Entry], query: str) -> list[Entry]:
    """Local-only filtering: never interpret the search as a filesystem path."""
    term = query[:128].strip().casefold()
    if not term:
        return list(entries)
    return [entry for entry in entries if term in entry.name.casefold()]


def format_size(size: int) -> str:
    if type(size) is not int or size < 0:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def secure_home() -> SafeHome:
    """Bind browsing to UID 1000's passwd home, never the mutable HOME env."""
    # This process is launched only by the desktop's unprivileged systemd
    # user session. Reject root, setuid confusion and alternate accounts.
    if os.getuid() != 1000 or os.geteuid() != 1000:
        raise SafePathError("Files requires the desktop UID 1000 session")
    try:
        account = pwd.getpwuid(1000)
    except KeyError as exc:
        raise SafePathError("Desktop account is missing") from exc
    return SafeHome(root=Path(account.pw_dir))


def load_css() -> None:
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_path(CSS_PATH)
    except (GLib.Error, OSError):
        # The installed stylesheet is an optional appearance layer.
        # Filesystem confinement never depends on CSS.
        return
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


class FilesWindow(Gtk.ApplicationWindow):
    """One native Wayland toplevel; no special compositor/layer-shell access."""

    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title="Files")
        self.set_default_size(720, 520)
        self.set_size_request(560, 360)
        self.get_style_context().add_class("phase3-files-window")

        self.parts: tuple[str, ...] = ()
        self.back_history: list[tuple[str, ...]] = []
        self.forward_history: list[tuple[str, ...]] = []
        self.entries: list[Entry] = []
        self.row_entries: dict[Gtk.ListBoxRow, Entry] = {}
        self.home: SafeHome | None = None
        self.show_hidden = False

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Files")
        header.set_subtitle("Read-only · Home")
        self.set_titlebar(header)

        self.back_button = self._header_button("go-previous-symbolic", "Back", self._go_back)
        self.forward_button = self._header_button("go-next-symbolic", "Forward", self._go_forward)
        self.up_button = self._header_button("go-up-symbolic", "Up one folder", self._go_parent)
        self.home_button = self._header_button("go-home-symbolic", "Home", self._go_home)
        self.refresh_button = self._header_button("view-refresh-symbolic", "Refresh", self._refresh)
        header.pack_start(self.back_button)
        header.pack_start(self.forward_button)
        header.pack_start(self.up_button)
        header.pack_start(self.home_button)
        header.pack_end(self.refresh_button)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        toolbar.set_border_width(12)
        toolbar.get_style_context().add_class("phase3-files-toolbar")
        root.pack_start(toolbar, False, False, 0)

        self.breadcrumb = Gtk.Label(label="Home")
        self.breadcrumb.set_xalign(0)
        self.breadcrumb.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.breadcrumb.set_selectable(False)
        self.breadcrumb.get_style_context().add_class("phase3-files-breadcrumb")
        toolbar.pack_start(self.breadcrumb, False, False, 0)

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        toolbar.pack_start(filters, False, False, 0)

        self.search = Gtk.SearchEntry()
        self.search.set_max_length(128)
        self.search.set_placeholder_text("Filter this folder")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search_changed)
        self.search.connect("activate", self._on_search_activate)
        self.search.get_style_context().add_class("phase3-files-search")
        filters.pack_start(self.search, True, True, 0)

        self.hidden_button = Gtk.CheckButton(label="Show hidden")
        self.hidden_button.connect("toggled", self._on_hidden_toggled)
        filters.pack_end(self.hidden_button, False, False, 0)

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_wide_handle(True)
        split.set_position(365)
        root.pack_start(split, True, True, 0)

        list_scroller = Gtk.ScrolledWindow()
        list_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroller.get_style_context().add_class("phase3-files-list-scroller")
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(False)
        self.listbox.connect("row-activated", self._on_row_activated)
        list_scroller.add(self.listbox)
        split.add1(list_scroller)

        preview_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        preview_panel.set_border_width(13)
        preview_panel.get_style_context().add_class("phase3-files-preview-panel")
        split.add2(preview_panel)

        self.preview_title = Gtk.Label(label="Preview")
        self.preview_title.set_xalign(0)
        self.preview_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.preview_title.get_style_context().add_class("phase3-files-preview-title")
        preview_panel.pack_start(self.preview_title, False, False, 0)

        preview_scroller = Gtk.ScrolledWindow()
        preview_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        preview_panel.pack_start(preview_scroller, True, True, 0)

        self.preview = Gtk.TextView()
        self.preview.set_editable(False)
        self.preview.set_cursor_visible(False)
        self.preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.preview.set_left_margin(12)
        self.preview.set_right_margin(12)
        self.preview.set_top_margin(12)
        self.preview.set_bottom_margin(12)
        self.preview.get_style_context().add_class("phase3-files-preview")
        preview_scroller.add(self.preview)
        self._show_preview("Preview", "Select a text file to preview it.\n"
                           "Files never opens or executes its selections.")

        self.status = Gtk.Label(label="Read-only · Home")
        self.status.set_xalign(0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_margin_start(14)
        self.status.set_margin_end(14)
        self.status.set_margin_top(8)
        self.status.set_margin_bottom(9)
        self.status.get_style_context().add_class("phase3-files-status")
        root.pack_end(self.status, False, False, 0)

        self.connect("key-press-event", self._on_key_press)

        try:
            self.home = secure_home()
        except (SafePathError, OSError, ValueError):
            self._set_status("Home unavailable: secure read-only access denied")
        else:
            self._navigate(())

        self.show_all()

    @staticmethod
    def _header_button(icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        button.set_tooltip_text(tooltip)
        button.get_style_context().add_class("phase3-files-header-button")
        button.connect("clicked", lambda _button: callback())
        return button

    def _set_status(self, message: str) -> None:
        self.status.set_text(message)

    def _show_preview(self, title: str, message: str) -> None:
        self.preview_title.set_text(display_name(title, 100))
        self.preview.get_buffer().set_text(message)

    def _update_path(self) -> None:
        names = [display_name(component, 45) for component in self.parts[-12:]]
        self.breadcrumb.set_text("Home" + "".join("  /  " + part for part in names))
        self.back_button.set_sensitive(bool(self.back_history))
        self.forward_button.set_sensitive(bool(self.forward_history))
        self.up_button.set_sensitive(bool(self.parts))
        self.home_button.set_sensitive(bool(self.parts))

    def _navigate(self, parts: tuple[str, ...], *, record_history: bool = True) -> bool:
        """Commit navigation only after the safe directory actually opened."""
        if self.home is None:
            self._set_status("Home unavailable: access denied")
            return False
        try:
            entries = self.home.list_dir(parts, show_hidden=self.show_hidden,
                                         limit=MAX_ENTRIES)
        except (SafePathError, OSError, ValueError):
            self._set_status("Folder unavailable or outside Home")
            self._show_preview("Preview", "This folder cannot be opened safely.")
            return False

        if record_history and parts != self.parts:
            self.back_history.append(self.parts)
            self.back_history = self.back_history[-32:]
            self.forward_history.clear()
        self.parts = parts
        self.entries = entries
        self.search.set_text("")
        self._update_path()
        self._render_entries()
        self._show_preview("Preview", "Select a text file to preview it.\n"
                           "Links and other special entries cannot be opened.")
        self._set_status(f"{len(entries)} entries shown (limit {MAX_ENTRIES}) · read-only")
        return True

    def _refresh(self) -> None:
        if self.home is None:
            self._set_status("Home unavailable: access denied")
            return
        try:
            entries = self.home.list_dir(self.parts, show_hidden=self.show_hidden,
                                         limit=MAX_ENTRIES)
        except (SafePathError, OSError, ValueError):
            self.entries = []
            self._render_entries()
            self._show_preview("Preview", "This folder cannot be opened safely.")
            self._set_status("Folder unavailable or outside Home")
            return
        self.entries = entries
        self._render_entries()
        self._set_status(f"{len(entries)} entries shown (limit {MAX_ENTRIES}) · read-only")

    def _go_back(self) -> None:
        if not self.back_history:
            return
        previous = self.parts
        target = self.back_history[-1]
        if self._navigate(target, record_history=False):
            self.back_history.pop()
            self.forward_history.append(previous)
            self.forward_history = self.forward_history[-32:]
            self._update_path()

    def _go_forward(self) -> None:
        if not self.forward_history:
            return
        previous = self.parts
        target = self.forward_history[-1]
        if self._navigate(target, record_history=False):
            self.forward_history.pop()
            self.back_history.append(previous)
            self.back_history = self.back_history[-32:]
            self._update_path()

    def _go_parent(self) -> None:
        if self.home is None:
            return
        try:
            parent = self.home.parent(self.parts)
        except (SafePathError, OSError, ValueError):
            self._set_status("Cannot navigate outside Home")
            return
        self._navigate(parent)

    def _go_home(self) -> None:
        self._navigate(())

    def _on_hidden_toggled(self, button: Gtk.CheckButton) -> None:
        self.show_hidden = bool(button.get_active())
        self._refresh()

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._render_entries()

    def _on_search_activate(self, _entry: Gtk.SearchEntry) -> None:
        """Return opens only the first visible row after bounded local filtering."""
        first = self.listbox.get_row_at_index(0)
        if first is not None:
            entry = self.row_entries.get(first)
            if entry is not None:
                self._activate_entry(entry)

    def _render_entries(self) -> None:
        for row in self.listbox.get_children():
            self.listbox.remove(row)
        self.row_entries.clear()
        selected = visible_entries(self.entries, self.search.get_text())
        for entry in selected:
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_selectable(True)
            row.get_style_context().add_class("phase3-files-row")
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
            content.set_border_width(8)
            icon = {
                "directory": "folder-symbolic",
                "file": "text-x-generic-symbolic",
                "symlink": "emblem-symbolic-link",
            }.get(entry.kind, "dialog-warning-symbolic")
            img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DND)
            img.set_pixel_size(22)
            content.pack_start(img, False, False, 0)
            name = Gtk.Label(label=display_name(entry.name))
            name.set_xalign(0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_hexpand(True)
            content.pack_start(name, True, True, 0)
            size = "Folder" if entry.kind == "directory" else (
                "Link" if entry.kind == "symlink" else
                format_size(entry.size) if entry.kind == "file" else "Blocked"
            )
            meta = Gtk.Label(label=size)
            meta.set_xalign(1)
            meta.get_style_context().add_class("phase3-files-meta")
            content.pack_end(meta, False, False, 0)
            row.add(content)
            self.listbox.add(row)
            self.row_entries[row] = entry
        self.listbox.show_all()
        if not selected:
            self._set_status("No matching entries · read-only")
        else:
            self._set_status(f"{len(selected)} of {len(self.entries)} entries visible · read-only")

    def _on_row_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        entry = self.row_entries.get(row)
        if entry is not None:
            self._activate_entry(entry)

    def _activate_entry(self, entry: Entry) -> None:
        if self.home is None:
            self._set_status("Home unavailable: access denied")
            return
        if entry.kind == "directory":
            try:
                child_parts = self.home.child(self.parts, entry.name)
            except (SafePathError, OSError, ValueError):
                self._set_status("Folder cannot be opened safely")
                return
            self._navigate(child_parts)
        elif entry.kind == "file":
            self._preview_file(entry)
        else:
            self._show_preview(display_name(entry.name),
                               "This entry is blocked. Links and special files "
                               "cannot be followed or opened.")
            self._set_status("Blocked: not a regular file or directory")

    def _preview_file(self, entry: Entry) -> None:
        if self.home is None:
            self._set_status("Home unavailable: access denied")
            return
        try:
            text = self.home.read_text(self.parts + (entry.name,),
                                       max_bytes=MAX_PREVIEW_BYTES)
        except (SafePathError, OSError, ValueError, UnicodeError):
            self._show_preview(display_name(entry.name),
                               "Preview unavailable. Only bounded UTF-8 text "
                               "files inside Home can be read.")
            self._set_status("Preview denied: file too large, changed, or unsafe")
            return
        self._show_preview(display_name(entry.name), text)
        self._set_status(f"Text preview · at most {MAX_PREVIEW_BYTES} bytes · read-only")

    def _on_key_press(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        state = event.state
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if alt and event.keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self._go_back()
            return True
        if alt and event.keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self._go_forward()
            return True
        if alt and event.keyval == Gdk.KEY_Up:
            self._go_parent()
            return True
        if alt and event.keyval == Gdk.KEY_Home:
            self._go_home()
            return True
        if ctrl and event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search.grab_focus()
            return True
        if ctrl and event.keyval in (Gdk.KEY_h, Gdk.KEY_H):
            self.hidden_button.set_active(not self.hidden_button.get_active())
            return True
        if (event.keyval == Gdk.KEY_BackSpace and
                self.get_focus() is not self.search):
            self._go_parent()
            return True
        return False


class FilesApp(Gtk.Application):
    def __init__(self) -> None:
        # The Phase 3 Sway session does not require a D-Bus session bus.
        # NON_UNIQUE keeps this window local while preserving its Wayland app_id.
        super().__init__(application_id=APPLICATION_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            load_css()
            window = FilesWindow(self)
        window.present()


def main() -> int:
    return FilesApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
