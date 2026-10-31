#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""A real native Wayland client: challenges arrive only in input callbacks."""
from functools import wraps
import os
from pathlib import Path
import sys

from probe_state import BUTTON, COLORS, ProbeState, read_nonce, save_nonce


def emit(value):
    print("P2:" + value, flush=True)


def require_wayland_display(display):
    # Debian's GTK 3 GIR package does not ship a GdkWayland namespace. Check
    # the actual instance's registered GType instead of importing that typelib.
    # GTK 3.24.49 gdk/wayland/gdkdisplay-wayland.c registers GdkWaylandDisplay.
    if getattr(getattr(display, "__gtype__", None), "name", None) != "GdkWaylandDisplay":
        raise RuntimeError("not a native Wayland display")


class CallbackGuard:
    """Turn GI's normally swallowed callback exceptions into a failed probe."""

    def __init__(self, stop_loop):
        self.stop_loop = stop_loop
        self.error = None

    def guard(self, callback):
        @wraps(callback)
        def guarded(*args, **kwargs):
            if self.error is not None:
                return False
            try:
                return callback(*args, **kwargs)
            except BaseException as error:
                # A GI callback cannot reliably throw through Gtk.main(). Keep
                # the first failure, stop dispatching success callbacks, and
                # re-raise it explicitly once the main loop has returned.
                self.error = error
                self.stop_loop()
                return False

        return guarded

    def raise_if_failed(self):
        if self.error is not None:
            raise RuntimeError("GTK callback failed: " + str(self.error)) from self.error


def main():
    if os.getuid() != 1000 or os.environ.get("GDK_BACKEND") != "wayland":
        raise RuntimeError("probe requires the non-root Wayland session")
    import gi
    gi.require_foreign("cairo")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    require_wayland_display(Gdk.Display.get_default())

    def stop_loop():
        # Some signals can run during show_all(), before Gtk.main() starts.
        if Gtk.main_level() > 0:
            Gtk.main_quit()

    callbacks = CallbackGuard(stop_loop)
    directory = Path.home() / ".local/share/phase2"
    previous = read_nonce(directory)
    state = ProbeState()
    window = Gtk.Window(title="Phase 2 — desktop input verification")
    window.set_decorated(False)
    window.set_default_size(1024, 768)
    window.fullscreen()
    canvas = Gtk.DrawingArea()
    canvas.set_can_focus(True)
    canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
    window.add(canvas)
    announced = False
    closed = False

    @callbacks.guard
    def ready():
        emit("WAYLAND_OK")
        emit("PERSISTED:" + previous)
        emit("PROBE_READY")
        return GLib.SOURCE_REMOVE

    @callbacks.guard
    def draw(widget, cr):
        nonlocal announced
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        if (width, height) != (1024, 768):
            # GTK may paint an initial allocation before the fullscreen
            # configure arrives. Do not publish readiness for that frame.
            if announced:
                raise RuntimeError("desktop resolution changed after readiness")
            return False
        cr.set_source_rgb(15 / 255, 25 / 255, 39 / 255)
        cr.paint()
        cr.set_source_rgb(*(channel / 255 for channel in COLORS[state.stage]))
        cr.rectangle(80, 100, 864, 220)
        cr.fill()
        cr.set_source_rgb(0.93, 0.96, 1)
        cr.select_font_face("DejaVu Sans")
        cr.set_font_size(30)
        cr.move_to(112, 156)
        cr.show_text("Phase 2 / Wayland desktop")
        cr.set_font_size(20)
        cr.move_to(112, 206)
        labels = {"ready": "Waiting for real keyboard input", "keyboard": "Keyboard verified — click to save", "pointer": "Pointer verified — saved to disk"}
        cr.show_text(labels[state.stage])
        cr.set_font_size(16)
        cr.move_to(112, 260)
        cr.show_text("Own kernel · Debian userspace · non-root session")
        cr.set_source_rgb(0.85, 0.9, 0.97)
        cr.set_font_size(19)
        cr.move_to(112, 380)
        cr.show_text(state.text or "Input challenge will appear here")
        cr.set_source_rgb(0.82, 0.88, 0.96)
        cr.rectangle(*BUTTON)
        cr.fill()
        cr.set_source_rgb(0.06, 0.1, 0.16)
        cr.set_font_size(22)
        cr.move_to(352, 499)
        cr.show_text("Save pointer confirmation")
        cr.set_source_rgb(0.65, 0.72, 0.82)
        cr.set_font_size(17)
        cr.move_to(112, 630)
        cr.show_text("After verification, Escape returns to the desktop terminal.")
        if not announced:
            announced = True
            GLib.idle_add(ready)
        return False

    @callbacks.guard
    def key(_widget, event):
        nonlocal closed
        name = Gdk.keyval_name(event.keyval)
        if name == "Escape" and state.stage == "pointer":
            closed = True
            window.destroy()
            return True
        if state.key(name, event.string or ""):
            emit("KEYBOARD_OK:" + state.text)
        canvas.queue_draw()
        return True

    @callbacks.guard
    def click(_widget, event):
        if state.can_click(event.button, event.x, event.y):
            save_nonce(directory, state.text)
            state.stage = "pointer"
            emit("POINTER_OK:" + state.text)
            canvas.queue_draw()
        return True

    @callbacks.guard
    def destroy(_window):
        stop_loop()

    canvas.connect("draw", draw)
    canvas.connect("button-press-event", click)
    window.connect("key-press-event", key)
    window.connect("destroy", destroy)
    window.show_all()
    canvas.grab_focus()
    window.present()
    callbacks.raise_if_failed()
    Gtk.main()
    callbacks.raise_if_failed()
    if not closed:
        raise RuntimeError("probe closed before completing input proof")
    emit("PROBE_CLOSED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("phase2 input probe: " + str(exc), file=sys.stderr, flush=True)
        emit("FAIL:INPUT_PROBE")
        sys.exit(1)
