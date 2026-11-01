# SPDX-License-Identifier: GPL-2.0-only
"""Source-bound, GI-free acceptance/security tests for the Phase 3.2 Files slice.

These tests exercise production SafeHome on disposable Linux filesystem fixtures
and AST-extracted, GI-independent Files UI callbacks. They are NOT evidence of
a mapped Wayland window, QMP input, a two-boot guest, or P3.2 qualification.
"""
from __future__ import annotations

import ast
import configparser
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest


PHASE3 = Path(__file__).resolve().parents[1]
FILES = PHASE3 / "files" / "files.py"
SAFE = PHASE3 / "files" / "safe_paths.py"
DESKTOP = PHASE3 / "files" / "phase3-files.desktop"
INSTALLER = PHASE3 / "scripts" / "install-rootfs.sh"


def _load_safe_paths():
    """Import the sibling source explicitly; unittest discover omits files/."""
    spec = importlib.util.spec_from_file_location("_p32_acceptance_safe_paths", SAFE)
    if spec is None or spec.loader is None:
        raise AssertionError("safe_paths source cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _actual_function(tree: ast.Module, name: str, namespace: dict):
    node = next((item for item in tree.body
                 if isinstance(item, ast.FunctionDef) and item.name == name), None)
    if node is None:
        raise AssertionError("missing production Files function " + name)
    future = ast.parse("from __future__ import annotations").body
    module = ast.fix_missing_locations(ast.Module(
        body=future + [node], type_ignores=[]))
    environment = dict(namespace)
    exec(compile(module, str(FILES), "exec"), environment)
    return environment[name]


def _actual_method(tree: ast.Module, name: str, namespace: dict):
    cls = next(item for item in tree.body if isinstance(item, ast.ClassDef)
               and item.name == "FilesWindow")
    node = next((item for item in cls.body
                 if isinstance(item, ast.FunctionDef) and item.name == name), None)
    if node is None:
        raise AssertionError("missing production Files callback " + name)
    future = ast.parse("from __future__ import annotations").body
    module = ast.fix_missing_locations(ast.Module(
        body=future + [node], type_ignores=[]))
    environment = dict(namespace)
    exec(compile(module, str(FILES), "exec"), environment)
    return environment[name]


class FilesSourceAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = FILES.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(FILES))
        cls.safe = _load_safe_paths()
        cls.display = staticmethod(_actual_function(cls.tree, "display_name", {}))
        cls.visible = staticmethod(_actual_function(cls.tree, "visible_entries", {}))
        cls.methods = {
            name: _actual_method(cls.tree, name, {
                "SafePathError": cls.safe.SafePathError,
                "display_name": cls.display,
                "MAX_PREVIEW_BYTES": 65_536,
                "MAX_ENTRIES": 256,
            })
            for name in ("_activate_entry", "_preview_file", "_refresh")
        }

    def test_app_identity_and_real_gtk_window_contract(self):
        declared = {target.id: node.value
                    for node in self.tree.body if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        self.assertEqual(ast.literal_eval(declared["APPLICATION_ID"]), "org.phase3.Files")
        self.assertEqual(ast.literal_eval(declared["MAX_ENTRIES"]), 256)
        self.assertEqual(ast.literal_eval(declared["MAX_PREVIEW_BYTES"]), 65_536)
        classes = {node.name: node for node in self.tree.body
                   if isinstance(node, ast.ClassDef)}
        self.assertIn("Gtk.ApplicationWindow",
                      [ast.unparse(x) for x in classes["FilesWindow"].bases])
        self.assertIn("Gtk.Application",
                      [ast.unparse(x) for x in classes["FilesApp"].bases])
        calls = [ast.unparse(node) for node in ast.walk(classes["FilesWindow"])
                 if isinstance(node, ast.Call)]
        self.assertTrue(any("self.set_default_size(720, 520)" == call for call in calls))
        self.assertIn("self.preview.set_editable(False)", calls)

    def test_fixed_desktop_entry_does_not_spawn_shell(self):
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(DESKTOP.read_text(encoding="utf-8"))
        entry = config["Desktop Entry"]
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "Files")
        self.assertEqual(entry["Exec"], "/usr/bin/python3 /usr/lib/phase3/files.py")
        self.assertEqual(entry["TryExec"], "/usr/bin/python3")
        self.assertEqual(entry["Terminal"].lower(), "false")
        self.assertIn("FileManager", entry["Categories"].split(";"))
        self.assertEqual(FILES.stat().st_mode & 0o111, 0o111)
        self.assertEqual(SAFE.stat().st_mode & 0o111, 0)
        self.assertEqual(DESKTOP.stat().st_mode & 0o111, 0)

    def test_installer_binds_all_four_files_and_modes(self):
        source = INSTALLER.read_text(encoding="utf-8")
        required = (
            'install -m 755 "$phase_dir/files/files.py" "$rootfs/usr/lib/phase3/files.py"',
            'install -m 644 "$phase_dir/files/safe_paths.py" "$rootfs/usr/lib/phase3/safe_paths.py"',
            'install -m 644 "$phase_dir/files/style.css" "$rootfs/usr/share/phase3/files.css"',
            'install -m 644 "$phase_dir/files/phase3-files.desktop" "$rootfs/usr/share/applications/phase3-files.desktop"',
            "Exec=/usr/bin/python3 /usr/lib/phase3/files.py",
        )
        for binding in required:
            with self.subTest(binding=binding):
                self.assertIn(binding, source)

    def test_secure_home_ignores_attacker_home_and_requires_uid_1000(self):
        selected = []
        def fake_safehome(*, root):
            selected.append(root)
            return root
        fake_os = SimpleNamespace(getuid=lambda: 1000, geteuid=lambda: 1000,
                                  environ={"HOME": "/etc"})
        fake_pwd = SimpleNamespace(getpwuid=lambda uid:
                                   SimpleNamespace(pw_dir="/home/desktop") if uid == 1000
                                   else None)
        secure_home = _actual_function(self.tree, "secure_home", {
            "os": fake_os, "pwd": fake_pwd, "SafeHome": fake_safehome,
            "SafePathError": self.safe.SafePathError, "Path": Path,
        })
        self.assertEqual(secure_home(), Path("/home/desktop"))
        self.assertEqual(selected, [Path("/home/desktop")])
        self.assertNotIn("Path.home(", ast.unparse(self.tree))
        self.assertNotIn("os.environ", ast.unparse(self.tree))

    def test_secure_home_denies_root_setuid_other_and_missing_nss(self):
        for uid, euid in ((0, 0), (1000, 0), (0, 1000), (1001, 1001)):
            with self.subTest(uid=uid, euid=euid):
                secure_home = _actual_function(self.tree, "secure_home", {
                    "os": SimpleNamespace(getuid=lambda: uid,
                                          geteuid=lambda: euid),
                    "pwd": SimpleNamespace(getpwuid=lambda _: None),
                    "SafeHome": lambda **_: self.fail("unsafe user got a file root"),
                    "SafePathError": self.safe.SafePathError,
                    "Path": Path,
                })
                with self.assertRaises(self.safe.SafePathError):
                    secure_home()
        missing = _actual_function(self.tree, "secure_home", {
            "os": SimpleNamespace(getuid=lambda: 1000, geteuid=lambda: 1000),
            "pwd": SimpleNamespace(getpwuid=lambda _: (_ for _ in ()).throw(KeyError())),
            "SafeHome": lambda **_: self.fail("missing NSS acquired a root"),
            "SafePathError": self.safe.SafePathError, "Path": Path,
        })
        with self.assertRaises(self.safe.SafePathError):
            missing()

    def test_files_ui_contains_no_write_shell_exec_or_direct_file_open(self):
        forbidden_imports = {"subprocess", "shutil", "socket", "ctypes"}
        forbidden_calls = {"open", "eval", "exec", "compile", "__import__",
                           "os.system", "os.popen", "os.spawnv", "os.execv",
                           "Gio.File.new_for_path", "Gio.AppInfo.launch_default_for_uri"}
        mutating_suffixes = (".write_text", ".write_bytes", ".write",
                             ".touch", ".mkdir", ".rmdir", ".unlink", ".rename",
                             ".replace", ".chmod", ".chown", ".mount",
                             ".set_markup", ".load_uri")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                self.assertFalse(forbidden_imports.intersection(
                    alias.name.split(".")[0] for alias in node.names))
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], forbidden_imports)
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                self.assertNotIn(name, forbidden_calls, name)
                self.assertFalse(name.endswith(mutating_suffixes), name)
        self.assertIn("SafeHome", self.source)
        self.assertIn("self.home.read_text(", self.source)
        self.assertIn("self.home.child(", self.source)
        self.assertIn("self.home.list_dir(", self.source)

    def test_source_limits_filter_and_uses_only_plain_text_widgets(self):
        entry = self.safe.Entry
        rows = [entry("Proof-AbC.txt", "file", 3, 0),
                entry("<b>evil</b>", "file", 0, 0),
                entry("Directory", "directory", 0, 0)]
        self.assertEqual(self.visible(rows, "proof"), rows[:1])
        self.assertEqual(self.visible(rows, "../etc"), [])
        self.assertEqual(self.visible(rows, "no match"), [])
        self.assertEqual(self.visible(rows, ""), rows)
        self.assertEqual(self.visible(rows, "PROOF" + "x" * 1000), [])
        escaped = self.display("\x1b<font color='red'>", limit=9)
        self.assertLessEqual(len(escaped), 9)
        self.assertFalse(any(ord(c) < 32 for c in escaped))
        self.assertNotIn("set_markup(", self.source)
        self.assertIn("self.search.set_max_length(128)", self.source)

    def test_directory_click_uses_safehome_child_before_navigation(self):
        calls = []
        entry = self.safe.Entry("reports", "directory", 0, 0)
        home = SimpleNamespace(child=lambda parts, name:
                               calls.append(("child", parts, name)) or
                               ("Documents", "reports"))
        ui = SimpleNamespace(home=home, parts=("Documents",),
                             _navigate=lambda parts: calls.append(("navigate", parts)),
                             _preview_file=lambda _: self.fail("directory previewed"),
                             _show_preview=lambda *args: calls.append(("show", args)),
                             _set_status=lambda msg: calls.append(("status", msg)))
        self.methods["_activate_entry"](ui, entry)
        self.assertEqual(calls, [
            ("child", ("Documents",), "reports"),
            ("navigate", ("Documents", "reports")),
        ])

    def test_symlink_and_special_rows_never_navigate_or_preview(self):
        for kind in ("symlink", "other"):
            with self.subTest(kind=kind):
                events = []
                ui = SimpleNamespace(
                    home=SimpleNamespace(child=lambda *a: self.fail("unsafe child")),
                    parts=(),
                    _navigate=lambda *a: self.fail("unsafe navigation"),
                    _preview_file=lambda *a: self.fail("unsafe preview"),
                    _show_preview=lambda *a: events.append(("preview", a)),
                    _set_status=lambda msg: events.append(("status", msg)),
                )
                self.methods["_activate_entry"](
                    ui, self.safe.Entry("escape-link", kind, 0, 0))
                self.assertEqual(len(events), 2)
                self.assertIn("blocked", events[-1][1].lower())

    def test_regular_file_preview_is_bounded_and_never_executes(self):
        calls = []
        text = "nonce-f7bc"
        ui = SimpleNamespace(
            home=SimpleNamespace(read_text=lambda parts, *, max_bytes:
                                 calls.append(("read", parts, max_bytes)) or text),
            parts=("Documents",),
            _show_preview=lambda title, message:
                          calls.append(("preview", title, message)),
            _set_status=lambda message: calls.append(("status", message)),
        )
        self.methods["_preview_file"](
            ui, self.safe.Entry("proof.txt", "file", len(text), 0))
        self.assertEqual(calls[0], ("read", ("Documents", "proof.txt"), 65536))
        self.assertEqual(calls[1], ("preview", "proof.txt", text))
        self.assertNotIn("launch", " ".join(str(x) for x in calls))

    def test_denied_preview_never_displays_stale_or_private_bytes(self):
        private = "SECRET-DENIED-BY-SAFEHOME"
        observed = []
        def deny(*args, **kwargs):
            raise self.safe.SafePathError(private)
        ui = SimpleNamespace(
            home=SimpleNamespace(read_text=deny), parts=(),
            _show_preview=lambda title, message: observed.append(message),
            _set_status=lambda message: observed.append(message),
        )
        self.methods["_preview_file"](
            ui, self.safe.Entry("escape-link", "file", 2, 0))
        self.assertEqual(len(observed), 2)
        self.assertTrue(any("unavailable" in x.lower() for x in observed))
        self.assertTrue(all(private not in x for x in observed))

    def test_denied_refresh_clears_stale_listing_and_preview(self):
        private = "OLD-SECRET-CONTENT"
        observed = []
        ui = SimpleNamespace(
            home=SimpleNamespace(list_dir=lambda *args, **kwargs:
                                 (_ for _ in ()).throw(self.safe.SafePathError("denied"))),
            parts=("Documents",), show_hidden=False,
            entries=[self.safe.Entry("private.txt", "file", 20, 0)],
            _render_entries=lambda: observed.append(("render",)),
            _show_preview=lambda *args: observed.append(("preview", args)),
            _set_status=lambda msg: observed.append(("status", msg)),
        )
        self.methods["_refresh"](ui)
        self.assertEqual(ui.entries, [])
        self.assertEqual([event[0] for event in observed],
                         ["render", "preview", "status"])
        self.assertNotIn(private, str(observed))

    def test_real_safehome_roundtrip_confinement_and_no_file_mutation(self):
        with tempfile.TemporaryDirectory(prefix="p32-files-accept-") as temp:
            root = Path(temp) / "desktop"
            challenge = root / "Documents" / "case-6e4a"
            challenge.mkdir(parents=True)
            proof = challenge / "proof-6e4a.txt"
            proof.write_text("nonce=6e4a\n", encoding="utf-8")
            (challenge / ".hidden.txt").write_text("hidden", encoding="utf-8")
            (challenge / "escape-link").symlink_to("/etc/os-release")
            before = proof.read_bytes()
            home = self.safe.SafeHome(root)
            docs = home.child((), "Documents")
            case = home.child(docs, "case-6e4a")
            shown = home.list_dir(case)
            self.assertIn(("proof-6e4a.txt", "file"),
                          {(e.name, e.kind) for e in shown})
            self.assertIn(("escape-link", "symlink"),
                          {(e.name, e.kind) for e in shown})
            self.assertNotIn(".hidden.txt", {e.name for e in shown})
            self.assertIn(".hidden.txt", {e.name for e in home.list_dir(
                case, show_hidden=True)})
            self.assertEqual(home.read_text(case + ("proof-6e4a.txt",)),
                             "nonce=6e4a\n")
            self.assertEqual(home.parent(case), docs)
            with self.assertRaises(self.safe.SafePathError):
                home.child(case, "escape-link")
            with self.assertRaises(self.safe.SafePathError):
                home.read_text(case + ("escape-link",))
            with self.assertRaises(self.safe.SafePathError):
                home.list_dir(("..",))
            self.assertEqual(proof.read_bytes(), before)

    def test_oversized_binary_and_fifo_never_preview(self):
        with tempfile.TemporaryDirectory(prefix="p32-files-negative-") as temp:
            root = Path(temp) / "desktop"
            root.mkdir()
            (root / "oversized.txt").write_bytes(b"x" * 65537)
            (root / "binary.bin").write_bytes(b"\x00\xff")
            pipe = root / "pipe"
            os.mkfifo(pipe)
            home = self.safe.SafeHome(root)
            for filename in ("oversized.txt", "binary.bin", "pipe"):
                with self.subTest(filename=filename):
                    with self.assertRaises(self.safe.SafePathError):
                        home.read_text((filename,))
            self.assertEqual(home.list_dir()[0].kind, "file")


if __name__ == "__main__":
    unittest.main()
