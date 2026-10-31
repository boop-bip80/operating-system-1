# SPDX-License-Identifier: GPL-2.0-only
"""Pure input state and durable user-data storage, shared by probe and tests."""
import os
from pathlib import Path
import re
import tempfile

NONCE = re.compile(r"[0-9a-f]{32}\Z")
COLORS = {"ready": (28, 82, 120), "keyboard": (28, 120, 80), "pointer": (36, 134, 87)}
BUTTON = (312, 440, 400, 100)


class ProbeState:
    def __init__(self):
        self.text = ""
        self.stage = "ready"

    def key(self, name, text):
        if self.stage != "ready":
            return False
        if name == "BackSpace":
            self.text = self.text[:-1]
        elif name in ("Return", "KP_Enter") and NONCE.fullmatch(self.text):
            self.stage = "keyboard"
            return True
        elif len(text) == 1 and text in "0123456789abcdef" and len(self.text) < 32:
            self.text += text
        return False

    def can_click(self, button, x, y):
        left, top, width, height = BUTTON
        return self.stage == "keyboard" and button == 1 and left <= x < left + width and top <= y < top + height


def read_nonce(directory):
    path = Path(directory) / "input-nonce"
    if path.is_symlink():
        raise ValueError("persistent nonce must not be a symlink")
    if not path.exists():
        return "EMPTY"
    if not path.is_file() or path.stat().st_size != 33:
        raise ValueError("invalid persistent nonce file")
    value = path.read_text(encoding="ascii")
    if not value.endswith("\n") or not NONCE.fullmatch(value[:-1]):
        raise ValueError("invalid persistent nonce")
    return value[:-1]


def save_nonce(directory, value):
    if not NONCE.fullmatch(value):
        raise ValueError("invalid input nonce")
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("unsafe persistence directory")
    fd, name = tempfile.mkstemp(prefix=".input-nonce-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / "input-nonce")
        directory_fd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)
