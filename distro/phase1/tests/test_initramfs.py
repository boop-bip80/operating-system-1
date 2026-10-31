# SPDX-License-Identifier: GPL-2.0-only
"""Validate CPIO parsing and boot-critical archive contracts entirely in memory."""

from contextlib import redirect_stdout
import gzip
import importlib.util
import io
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/initramfs.py"
SPEC = importlib.util.spec_from_file_location("phase1_initramfs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
archive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = archive
SPEC.loader.exec_module(archive)

RELEASE = b"7.3.0-rc7-phase1\n"
SOURCE = b"a" * 40 + b"\n"


def cpio_entry(name, data=b"", mode=stat.S_IFREG | 0o755, uid=0, gid=0, major=0, minor=0):
    name_bytes = name.encode() + b"\0"
    values = [1, mode, uid, gid, 1, 0, len(data), 0, 0, major, minor, len(name_bytes), 0]
    blob = b"070701" + b"".join(f"{value:08x}".encode() for value in values) + name_bytes
    blob += b"\0" * (-len(blob) % 4)
    blob += data
    return blob + b"\0" * (-len(blob) % 4)


def make_archive(entries, trailer=True):
    raw = b"".join(cpio_entry(**entry) for entry in entries)
    if trailer:
        raw += cpio_entry("TRAILER!!!", mode=0)
    return gzip.compress(raw, mtime=0)


def boot_entries():
    return [
        {"name": "init", "data": b"#!/bin/sh\nprintf hello\n"},
        {"name": "bin/busybox", "data": b"\x7fELFmock-validated-elsewhere"},
        {"name": "bin/sh", "data": b"busybox", "mode": stat.S_IFLNK | 0o777},
        {"name": "etc/udhcpc.script", "data": b"#!/bin/sh\n"},
        {"name": "dev/console", "mode": stat.S_IFCHR | 0o600, "major": 5, "minor": 1},
        {"name": "dev/null", "mode": stat.S_IFCHR | 0o666, "major": 1, "minor": 3},
        {"name": "etc/phase1-release", "mode": stat.S_IFREG | 0o644, "data": RELEASE},
        {"name": "etc/phase1-source", "mode": stat.S_IFREG | 0o644, "data": SOURCE},
    ]


def parse(blob):
    with patch.object(Path, "read_bytes", return_value=blob):
        return archive.read_archive("archive.cpio.gz")


def verify(entries):
    files = {"archive.cpio.gz": make_archive(entries), "release": RELEASE, "revision": SOURCE}
    with patch.object(Path, "read_bytes", lambda path: files[str(path)]), redirect_stdout(io.StringIO()):
        archive.verify("archive.cpio.gz", "release", "revision")


class TestArchiveParser(unittest.TestCase):
    def test_newc_regular_file_and_node(self):
        entries = parse(make_archive(boot_entries()))
        self.assertEqual(entries["/init"]["data"], b"#!/bin/sh\nprintf hello\n")
        self.assertEqual((entries["/dev/console"]["major"], entries["/dev/console"]["minor"]), (5, 1))
        self.assertTrue(stat.S_ISCHR(entries["/dev/console"]["mode"]))

    def test_missing_trailer_rejected(self):
        with self.assertRaisesRegex(ValueError, "trailer"):
            parse(make_archive([{"name": "init"}], trailer=False))

    def test_path_traversal_rejected(self):
        with self.assertRaisesRegex(ValueError, "traversal"):
            parse(make_archive([{"name": "etc/../init"}]))

    def test_absolute_and_relative_duplicates_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse(make_archive([{"name": "init"}, {"name": "/init"}]))

    def test_normalized_dot_path_duplicates_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse(make_archive([{"name": "./init"}, {"name": "init"}]))

    def test_wrong_cpio_magic_rejected(self):
        raw = cpio_entry("init")
        with self.assertRaisesRegex(ValueError, "magic"):
            parse(gzip.compress(b"070702" + raw[6:]))

    def test_truncated_data_rejected(self):
        raw = cpio_entry("init", data=b"12345678")
        with self.assertRaisesRegex(ValueError, "truncated"):
            parse(gzip.compress(raw[:-4]))

    def test_unterminated_name_rejected(self):
        raw = bytearray(cpio_entry("init"))
        raw[114] = ord("x")  # name terminator follows the 110-byte header and four chars
        with self.assertRaisesRegex(ValueError, "name"):
            parse(gzip.compress(raw))

    def test_invalid_gzip_rejected(self):
        with self.assertRaises((OSError, EOFError)):
            parse(b"not gzip data")


class TestBootArchiveContract(unittest.TestCase):
    def test_valid_minimal_archive(self):
        verify(boot_entries())

    def test_missing_init_rejected(self):
        with self.assertRaises((ValueError, KeyError)):
            verify([entry for entry in boot_entries() if entry["name"] != "init"])

    def test_nonexecutable_init_rejected(self):
        entries = boot_entries()
        entries[0]["mode"] = stat.S_IFREG | 0o644
        with self.assertRaisesRegex(ValueError, "executable"):
            verify(entries)

    def test_init_symlink_rejected(self):
        entries = boot_entries()
        entries[0]["mode"] = stat.S_IFLNK | 0o777
        with self.assertRaisesRegex(ValueError, "regular"):
            verify(entries)

    def test_wrong_init_interpreter_rejected(self):
        entries = boot_entries()
        entries[0]["data"] = b"#!/bin/bash\n"
        with self.assertRaisesRegex(ValueError, "interpreter"):
            verify(entries)

    def test_non_elf_busybox_rejected(self):
        entries = boot_entries()
        entries[1]["data"] = b"#!/bin/sh\n"
        with self.assertRaisesRegex(ValueError, "ELF"):
            verify(entries)

    def test_shell_outside_archive_rejected(self):
        entries = boot_entries()
        entries[2]["data"] = b"../../host/bin/sh"
        with self.assertRaisesRegex(ValueError, "BusyBox"):
            verify(entries)

    def test_absolute_busybox_shell_target_allowed(self):
        entries = boot_entries()
        entries[2]["data"] = b"/bin/busybox"
        verify(entries)

    def test_wrong_console_node_rejected(self):
        entries = boot_entries()
        entries[4]["major"] = 1
        with self.assertRaisesRegex(ValueError, "device"):
            verify(entries)

    def test_console_regular_file_rejected(self):
        entries = boot_entries()
        entries[4]["mode"] = stat.S_IFREG | 0o600
        with self.assertRaisesRegex(ValueError, "device"):
            verify(entries)

    def test_wrong_release_rejected(self):
        entries = boot_entries()
        entries[6]["data"] = b"host-kernel\n"
        with self.assertRaisesRegex(ValueError, "identity"):
            verify(entries)

    def test_wrong_source_rejected(self):
        entries = boot_entries()
        entries[7]["data"] = b"unrelated-source\n"
        with self.assertRaisesRegex(ValueError, "identity"):
            verify(entries)

    def test_nonroot_ownership_rejected(self):
        entries = boot_entries()
        entries[1]["uid"] = 1000
        with self.assertRaisesRegex(ValueError, "ownership"):
            verify(entries)


if __name__ == "__main__":
    unittest.main()
