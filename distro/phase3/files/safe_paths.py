# SPDX-License-Identifier: GPL-2.0-only
"""Read-only, descriptor-confined navigation for the Phase 3 Files application.

Every filesystem access starts at a freshly opened, non-symlink HOME root and
walks one literal component at a time with openat(2)-style dir_fd handles and
O_NOFOLLOW. Display names never become shell commands or interpreted URIs.

This module deliberately contains no create, rename, delete, write, execute,
or external-application launch operations.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterator


class SafePathError(ValueError):
    """A path or preview cannot be safely confined to the configured home."""


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    kind: str  # directory, file, symlink, other
    size: int
    mtime_ns: int


_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_DEPTH = 64
_MAX_RELATIVE_BYTES = 4096
_MAX_PREVIEW = 65536
_MAX_LIST = 256


def _component(value: str) -> str:
    if type(value) is not str or not value or value in (".", ".."):
        raise SafePathError("path must contain literal, non-dot filename components")
    # Never decode URL fragments, expand a shell metacharacter or interpret a
    # Windows/URI pathname in a Linux component. Quotes/$/; are literal names.
    if any(char in value for char in ("/", "\\", "\x00", ":", "%")):
        raise SafePathError("encoded/URI/separator path component is forbidden")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SafePathError("control characters are forbidden in a path component")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise SafePathError("undecodable filesystem names are not safe for GTK")
    try:
        length = len(os.fsencode(value))
    except (UnicodeError, ValueError) as exc:
        raise SafePathError("filename cannot be encoded safely") from exc
    if length == 0 or length > 255:
        raise SafePathError("filename exceeds the filesystem component limit")
    return value


def _parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    if type(parts) is not tuple or len(parts) > _MAX_DEPTH:
        raise SafePathError("navigation must be a bounded tuple of path components")
    verified = tuple(_component(value) for value in parts)
    if sum(len(os.fsencode(value)) + 1 for value in verified) > _MAX_RELATIVE_BYTES:
        raise SafePathError("navigation path is too long")
    return verified


class SafeHome:
    """Read-only home root. No path is ever opened by concatenating components."""

    def __init__(self, root: Path | str):
        if not isinstance(root, (str, Path)):
            raise SafePathError("home root must be a path")
        home = Path(root)
        if not home.is_absolute() or home == Path("/"):
            raise SafePathError("home root must be a dedicated absolute directory")
        # Path() may preserve lexical parent segments; reject rather than
        # resolving them through an attacker-controlled symlink.
        components = home.parts[1:]
        if not components or any(item in ("", ".", "..") or "\x00" in item
                                 for item in components):
            raise SafePathError("unsafe home root")
        self.root = home
        with self._directory(()) as descriptor:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise SafePathError("home root is not a directory")

    @contextmanager
    def _directory(self, parts: tuple[str, ...]) -> Iterator[int]:
        relative = _parts(parts)
        descriptor = -1
        try:
            # Open every ancestor afresh from /, rejecting symlinks at HOME,
            # any ancestor, and each user-selected descendant directory.
            descriptor = os.open("/", _DIR_FLAGS)
            for component in (*self.root.parts[1:], *relative):
                following = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            yield descriptor
        except (OSError, OverflowError) as exc:
            raise SafePathError("directory is unavailable or unsafe") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def parent(parts: tuple[str, ...]) -> tuple[str, ...]:
        checked = _parts(parts)
        return checked[:-1]

    def child(self, parts: tuple[str, ...], name: str) -> tuple[str, ...]:
        target = (*_parts(parts), _component(name))
        _parts(target)
        # Reopen through HOME and every literal component; an entry replaced
        # with a symlink between list_dir and click is refused by O_NOFOLLOW.
        with self._directory(target):
            return target

    def list_dir(self, parts: tuple[str, ...] = (), *,
                 show_hidden: bool = False, limit: int = _MAX_LIST) -> list[Entry]:
        if type(show_hidden) is not bool or type(limit) is not int or not 1 <= limit <= _MAX_LIST:
            raise SafePathError("invalid directory visibility or listing bound")
        result: list[Entry] = []
        with self._directory(parts) as descriptor:
            try:
                with os.scandir(descriptor) as iterator:
                    for item in iterator:
                        if not show_hidden and item.name.startswith("."):
                            continue
                        try:
                            name = _component(item.name)
                        except SafePathError:
                            # Hostile on-disk names never become navigation
                            # targets or strings interpreted by GTK/Gio.
                            continue
                        metadata = item.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode):
                            kind = "symlink"
                        elif stat.S_ISDIR(metadata.st_mode):
                            kind = "directory"
                        elif stat.S_ISREG(metadata.st_mode):
                            kind = "file"
                        else:
                            kind = "other"
                        result.append(Entry(
                            name=name, kind=kind,
                            size=metadata.st_size if kind == "file" else 0,
                            mtime_ns=metadata.st_mtime_ns,
                        ))
                        if len(result) > limit:
                            raise SafePathError("directory exceeds bounded entry count")
            except OSError as exc:
                raise SafePathError("directory listing failed safely") from exc
        return sorted(result, key=lambda item: (
            item.kind != "directory", item.name.casefold(), item.name,
        ))

    def read_text(self, parts: tuple[str, ...], *,
                  max_bytes: int = _MAX_PREVIEW) -> str:
        checked = _parts(parts)
        if not checked:
            raise SafePathError("preview requires a regular file")
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_PREVIEW:
            raise SafePathError("invalid bounded preview size")
        with self._directory(checked[:-1]) as directory:
            descriptor = -1
            try:
                descriptor = os.open(checked[-1], _FILE_FLAGS, dir_fd=directory)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise SafePathError("preview target is not a unique regular file")
                if metadata.st_size < 0 or metadata.st_size > max_bytes:
                    raise SafePathError("preview exceeds maximum file size")
                blocks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    data = os.read(descriptor, min(remaining, 8192))
                    if not data:
                        break
                    blocks.append(data)
                    remaining -= len(data)
                content = b"".join(blocks)
                if len(content) > max_bytes:
                    raise SafePathError("preview grew beyond maximum file size")
                try:
                    result = content.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise SafePathError("preview is not UTF-8 text") from exc
                if any(ord(char) < 32 and char not in "\t\n\r"
                       or ord(char) == 127 for char in result):
                    raise SafePathError("binary/control bytes are not previewable text")
                return result
            except OSError as exc:
                raise SafePathError("file preview could not be opened safely") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
