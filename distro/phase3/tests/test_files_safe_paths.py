# SPDX-License-Identifier: GPL-2.0-only
"""Hostile-filesystem regressions for Phase 3 Files read-only SafeHome.

These tests use real temporary Linux files, symlinks, hardlinks and FIFOs.
They do not claim to substitute for QEMU/GTK application interaction.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

# "python3 -m unittest discover -s distro/phase3/tests" does not add the
# sibling files/ directory to sys.path; import the exact shipped module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
from safe_paths import Entry, SafeHome, SafePathError


class SafeHomeConfinement(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="phase3-files-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "home"
        self.root.mkdir()
        self.outside = self.base / "private"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("OUTSIDE PRIVATE DATA\n", encoding="utf-8")
        self.safe = SafeHome(self.root)

    def write(self, name, content):
        destination = self.root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content if isinstance(content, bytes)
                                else content.encode("utf-8"))
        return destination

    def test_basic_nested_read_navigation_parent_and_home(self):
        self.write("Projects/demo/notes.txt", "Résumé\nline two\n")
        self.assertEqual(self.safe.child((), "Projects"), ("Projects",))
        self.assertEqual(self.safe.child(("Projects",), "demo"),
                         ("Projects", "demo"))
        self.assertEqual(self.safe.parent(("Projects", "demo")),
                         ("Projects",))
        self.assertEqual(self.safe.parent(()), ())
        self.assertEqual(self.safe.parent(("Projects",)), ())
        self.assertEqual(self.safe.read_text(("Projects", "demo", "notes.txt")),
                         "Résumé\nline two\n")
        items = self.safe.list_dir(("Projects", "demo"))
        self.assertEqual([(item.name, item.kind) for item in items],
                         [("notes.txt", "file")])

    def test_root_must_be_actual_absolute_non_symlink_directory(self):
        for candidate in (".", "/", self.base / "missing",
                          self.base / "home" / ".." / "private",
                          str(self.base) + "/bad\x00root", 42, None):
            with self.subTest(candidate=candidate):
                with self.assertRaises(SafePathError):
                    SafeHome(candidate)
        (self.base / "home-link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SafePathError):
            SafeHome(self.base / "home-link")
        ancestor = self.base / "link-parent"
        ancestor.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(SafePathError):
            SafeHome(ancestor / "home")

    def test_component_rejects_parent_absolute_encoded_uri_and_controls(self):
        names = (".", "..", "", "../private", "/etc", "nested/name",
                 "nested\\name", "%2e%2e", "file%2Fetc", "file://etc",
                 "C:\\Windows", "a\x00b", "a\nb", "a\x7fb",
                 "é" * 150, 23, False, None)
        self.write("folder/ok.txt", "ok")
        for name in names:
            with self.subTest(name=repr(name)):
                with self.assertRaises(SafePathError):
                    self.safe.child((), name)
                with self.assertRaises(SafePathError):
                    self.safe.read_text((name,))
                with self.assertRaises(SafePathError):
                    self.safe.list_dir((name,))
        for invalid in ("folder", ["folder"], ("folder", ".."),
                        ("folder", "/tmp"), (23,), (False,), None):
            with self.subTest(parts=repr(invalid)):
                with self.assertRaises(SafePathError):
                    self.safe.parent(invalid)
        with self.assertRaises(SafePathError):
            self.safe.list_dir(("a",) * 65)

    def test_symlink_files_and_directories_inside_or_outside_are_never_followed(self):
        (self.root / "real").mkdir()
        self.write("real/inside.txt", "inside")
        (self.root / "outside-dir").symlink_to(self.outside, target_is_directory=True)
        (self.root / "inside-dir").symlink_to(self.root / "real", target_is_directory=True)
        (self.root / "outside-file").symlink_to(self.outside / "secret.txt")
        (self.root / "inside-file").symlink_to(self.root / "real" / "inside.txt")
        kinds = {item.name: item.kind for item in self.safe.list_dir()}
        for name in ("outside-dir", "inside-dir", "outside-file", "inside-file"):
            self.assertEqual(kinds[name], "symlink")
            with self.subTest(name=name):
                with self.assertRaises(SafePathError):
                    self.safe.child((), name)
                with self.assertRaises(SafePathError):
                    self.safe.read_text((name,))
        with self.assertRaises(SafePathError):
            self.safe.read_text(("outside-dir", "secret.txt"))
        self.assertEqual(self.safe.read_text(("real", "inside.txt")), "inside")

    def test_symlink_substitution_after_listing_and_root_replacement(self):
        (self.root / "folder").mkdir()
        (self.root / "folder" / "local.txt").write_text("local", encoding="utf-8")
        self.assertIn("folder", [item.name for item in self.safe.list_dir()])
        (self.root / "folder").rename(self.root / "folder-old")
        (self.root / "folder").symlink_to(self.outside, target_is_directory=True)
        for operation in (
            lambda: self.safe.child((), "folder"),
            lambda: self.safe.list_dir(("folder",)),
            lambda: self.safe.read_text(("folder", "secret.txt")),
        ):
            with self.assertRaises(SafePathError):
                operation()
        (self.root / "folder").unlink()
        self.root.rename(self.base / "renamed-home")
        self.root.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(SafePathError):
            self.safe.list_dir()

    def test_openat_is_race_safe_even_when_symlink_swap_happens_during_click(self):
        (self.root / "victim").mkdir()
        original_open = os.open
        did_swap = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal did_swap
            if path == "victim" and not did_swap:
                did_swap = True
                (self.root / "victim").rename(self.root / "old-victim")
                (self.root / "victim").symlink_to(
                    self.outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with mock.patch("safe_paths.os.open", side_effect=swap_before_open):
            with self.assertRaises(SafePathError):
                self.safe.child((), "victim")
        self.assertTrue(did_swap)

    def test_preview_leaf_replaced_by_symlink_after_listing_is_refused(self):
        self.write("preview.txt", "benign")
        self.assertIn("preview.txt", [item.name for item in self.safe.list_dir()])
        (self.root / "preview.txt").unlink()
        (self.root / "preview.txt").symlink_to(self.outside / "secret.txt")
        with self.assertRaises(SafePathError):
            self.safe.read_text(("preview.txt",))

    def test_openat_refuses_leaf_swapped_during_preview(self):
        self.write("race.txt", "benign")
        original_open = os.open
        did_swap = False

        def swap_before_leaf(path, flags, *args, **kwargs):
            nonlocal did_swap
            if path == "race.txt" and not did_swap:
                did_swap = True
                (self.root / "race.txt").unlink()
                (self.root / "race.txt").symlink_to(self.outside / "secret.txt")
            return original_open(path, flags, *args, **kwargs)

        with mock.patch("safe_paths.os.open", side_effect=swap_before_leaf):
            with self.assertRaises(SafePathError):
                self.safe.read_text(("race.txt",))
        self.assertTrue(did_swap)

    def test_file_growth_between_fstat_and_read_is_bounded(self):
        destination = self.write("grow.txt", "small")
        original_read = os.read
        did_grow = False

        def grow_before_read(fd, length):
            nonlocal did_grow
            if not did_grow:
                did_grow = True
                with destination.open("ab") as writer:
                    writer.write(b"x" * 30)
            return original_read(fd, length)

        with mock.patch("safe_paths.os.read", side_effect=grow_before_read):
            with self.assertRaises(SafePathError):
                self.safe.read_text(("grow.txt",), max_bytes=16)
        self.assertTrue(did_grow)

    def test_hardlink_alias_cannot_preview_outside_file(self):
        os.link(self.outside / "secret.txt", self.root / "alias.txt")
        self.assertIn("alias.txt", [item.name for item in self.safe.list_dir()])
        with self.assertRaises(SafePathError):
            self.safe.read_text(("alias.txt",))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.root / "pipe")
        self.assertEqual(next(i.kind for i in self.safe.list_dir()
                              if i.name == "pipe"), "other")
        with self.assertRaises(SafePathError):
            self.safe.read_text(("pipe",))

    def test_listing_hidden_sorted_directory_first_and_frozen_entries(self):
        self.write(".private", "x")
        self.write("beta", "b")
        self.write("Alpha", "a")
        (self.root / "zDir").mkdir()
        (self.root / "aDir").mkdir()
        (self.root / "xLink").symlink_to(self.root / "Alpha")
        visible = self.safe.list_dir()
        self.assertEqual([(e.name, e.kind) for e in visible], [
            ("aDir", "directory"), ("zDir", "directory"), ("Alpha", "file"),
            ("beta", "file"), ("xLink", "symlink")])
        self.assertTrue(all(isinstance(e, Entry) and isinstance(e.mtime_ns, int)
                            for e in visible))
        self.assertEqual(next(e.size for e in visible if e.name == "beta"), 1)
        self.assertIn(".private", [e.name for e in self.safe.list_dir(show_hidden=True)])
        with self.assertRaises((AttributeError, TypeError)):
            visible[0].name = "changed"

    def test_bounded_list_and_strict_arguments(self):
        for index in range(3):
            self.write(f"entry-{index}", "v")
        for bound in (1, 2):
            with self.assertRaises(SafePathError):
                self.safe.list_dir(limit=bound)
        self.assertEqual(len(self.safe.list_dir(limit=3)), 3)
        for kwargs in ({"limit": 0}, {"limit": 257}, {"limit": True},
                       {"limit": 1.0}, {"show_hidden": 1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SafePathError):
                    self.safe.list_dir(**kwargs)

    def test_preview_utf8_size_binary_controls_and_file_growth(self):
        self.write("safe.txt", "汉字🙂\tHello\r\n")
        self.assertEqual(self.safe.read_text(("safe.txt",)), "汉字🙂\tHello\r\n")
        self.write("huge.txt", "a" * 65537)
        self.write("bad-utf8.txt", b"before\xffafter")
        self.write("binary.txt", b"before\x00after")
        self.write("control.txt", b"before\x1bafter")
        for path in ("huge.txt", "bad-utf8.txt", "binary.txt", "control.txt"):
            with self.subTest(path=path), self.assertRaises(SafePathError):
                self.safe.read_text((path,))
        self.assertEqual(self.safe.read_text(("safe.txt",), max_bytes=32),
                         "汉字🙂\tHello\r\n")
        for max_bytes in (0, 65537, True, 8.2):
            with self.subTest(max_bytes=max_bytes), self.assertRaises(SafePathError):
                self.safe.read_text(("safe.txt",), max_bytes=max_bytes)
        self.write("exact.txt", "x" * 16)
        self.assertEqual(len(self.safe.read_text(("exact.txt",), max_bytes=16)), 16)
        with self.assertRaises(SafePathError):
            self.safe.read_text(("exact.txt",), max_bytes=15)

    def test_literal_shell_metacharacters_are_not_executed(self):
        # The filename is passed only as a literal openat component.
        name = "report;$(touch PWNED).txt"
        self.write(name, "literal contents")
        self.assertEqual(self.safe.read_text((name,)), "literal contents")
        self.assertIn(name, [entry.name for entry in self.safe.list_dir()])
        self.assertFalse((self.root / "PWNED").exists())

    def test_undecodable_on_disk_names_cannot_be_shown_or_selected(self):
        raw_path = os.fsencode(self.root) + b"/unreadable-\xff"
        descriptor = os.open(raw_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.assertFalse(any("unreadable" in e.name for e in self.safe.list_dir()))
        unsafe = os.fsdecode(b"unreadable-\xff")
        with self.assertRaises(SafePathError):
            self.safe.child((), unsafe)

    def test_only_public_navigation_and_read_operations(self):
        public = {name for name in dir(SafeHome)
                  if not name.startswith("_") and callable(getattr(SafeHome, name))}
        self.assertEqual(public, {"parent", "child", "list_dir", "read_text"})


if __name__ == "__main__":
    unittest.main()
