# SPDX-License-Identifier: GPL-2.0-only
"""GI-independent regression tests for the Phase 3 desktop-entry boundary.

These tests execute AST-extracted production _desktop_apps and
_launch_desktop_app and _fill_launcher methods with fake Gio/GLib/UI collaborators.
They test shell filtering, ordering and safe launch delegation, NOT Gio's
actual .desktop file parser or a running graphical environment.
"""

from __future__ import annotations

import ast
from contextlib import redirect_stderr
import io
import re
from pathlib import Path
import sys
import types
import unittest


SHELL = Path(__file__).resolve().parents[1] / "shell" / "shell.py"


class FakeGLibError(Exception):
    pass


class FakeApp:
    def __init__(
        self, name="Example", *, show=True, show_error=None,
        name_error=None, name_after_first=None, launch_value=True,
        launch_error=None, exec_line="", desktop_id="org.example.App.desktop",
        icon="app-symbolic", id_error=None, icon_error=None,
    ):
        self.name = name
        self.show = show
        self.show_error = show_error
        self.name_error = name_error
        self.name_after_first = name_after_first
        self.launch_value = launch_value
        self.launch_error = launch_error
        self.exec_line = exec_line
        self.desktop_id = desktop_id
        self.icon = icon
        self.id_error = id_error
        self.icon_error = icon_error
        self.name_calls = 0
        self.launch_calls = []

    def should_show(self):
        if self.show_error:
            raise self.show_error
        return self.show

    def get_name(self):
        self.name_calls += 1
        if self.name_error:
            raise self.name_error
        if self.name_after_first is not None and self.name_calls > 1:
            if isinstance(self.name_after_first, BaseException):
                raise self.name_after_first
            return self.name_after_first
        return self.name

    def get_id(self):
        if self.id_error:
            raise self.id_error
        return self.desktop_id

    def get_icon(self):
        if self.icon_error:
            raise self.icon_error
        return self.icon

    def launch(self, files, context):
        self.launch_calls.append((files, context))
        if self.launch_error:
            raise self.launch_error
        return self.launch_value


def production_methods(apps):
    """Compile ONLY actual launcher methods; no GTK/GI import is needed."""
    source = SHELL.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SHELL))
    classes = [node for node in tree.body if
               isinstance(node, ast.ClassDef) and node.name == "Phase3Shell"]
    if len(classes) != 1:
        raise AssertionError("production Phase3Shell class not found")
    wanted = {"_desktop_apps", "_launch_desktop_app", "_fill_launcher"}
    methods = [node for node in classes[0].body if
               isinstance(node, ast.FunctionDef) and node.name in wanted]
    if {method.name for method in methods} != wanted:
        raise AssertionError("production desktop-entry methods missing")
    shell_class = ast.ClassDef(
        name="ExtractedPhase3Shell", bases=[], keywords=[],
        body=methods, decorator_list=[],
    )
    safe_ids = [node for node in tree.body if
                isinstance(node, ast.FunctionDef) and node.name == "safe_desktop_id"]
    if len(safe_ids) != 1:
        raise AssertionError("production XDG desktop-id gate missing")
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and
                         target.id == "APP_INDEX_ID_CHAR_BUDGET"
                         for target in node.targets)]
    if len(constants) != 1:
        raise AssertionError("production app-index character budget missing")
    module = ast.fix_missing_locations(
        ast.Module(body=constants + safe_ids + [shell_class], type_ignores=[]),
    )

    class AppInfo:
        @staticmethod
        def get_all():
            return list(apps)

    def forbidden_shell(*_args, **_kwargs):
        raise AssertionError("desktop-entry Exec must never reach shell execution")

    forbidden_subprocess = types.SimpleNamespace(
        Popen=forbidden_shell, run=forbidden_shell,
        call=forbidden_shell, check_call=forbidden_shell,
    )
    namespace = {
        "Gio": types.SimpleNamespace(AppInfo=AppInfo),
        "GLib": types.SimpleNamespace(Error=FakeGLibError),
        "sys": sys, "subprocess": forbidden_subprocess, "re": re, "Any": object,
    }
    exec(compile(module, str(SHELL), "exec"), namespace)
    return namespace["ExtractedPhase3Shell"]


class FakeButton:
    def __init__(self, name):
        self.name = name
        self.callbacks = []

    def connect(self, event, callback):
        self.callbacks.append((event, callback))

    def activate(self):
        for event, callback in self.callbacks:
            if event == "clicked":
                callback(self)


class FakeList:
    def __init__(self):
        self.children = []
        self.removed = []
        self.mapped = False

    def get_children(self):
        return list(self.children)

    def remove(self, child):
        self.children.remove(child)
        self.removed.append(child)

    def pack_start(self, button, *_args):
        self.children.append(button)

    def show_all(self):
        self.mapped = True


class FakeSearch:
    def __init__(self, query):
        self.query = query

    def get_text(self):
        return self.query


class DesktopEntryContract(unittest.TestCase):
    def test_visible_applications_sorted_by_casefold(self):
        apps = [FakeApp("Zulu"), FakeApp("bETA"), FakeApp("alpha")]
        cls = production_methods(apps)
        self.assertEqual(cls._desktop_apps(), [apps[2], apps[1], apps[0]])

    def test_hidden_and_empty_metadata_is_filtered(self):
        hidden = FakeApp("Hidden", show=False)
        empty = FakeApp("")
        missing = FakeApp(None)
        valid = FakeApp("Visible")
        cls = production_methods([hidden, empty, missing, valid])
        self.assertEqual(cls._desktop_apps(), [valid])

    def test_glib_and_attribute_failures_are_filtered(self):
        bad = [
            FakeApp("Shown", show_error=FakeGLibError("bad entry")),
            FakeApp("Shown", show_error=AttributeError("missing field")),
            FakeApp("Shown", name_error=FakeGLibError("bad name")),
            FakeApp("Shown", name_error=AttributeError("invalid name")),
            types.SimpleNamespace(get_name=lambda: "Missing should_show"),
        ]
        valid = FakeApp("Valid")
        cls = production_methods(bad + [valid])
        self.assertEqual(cls._desktop_apps(), [valid])

    def test_malformed_nonstring_names_are_not_sort_errors(self):
        bad = [FakeApp(123), FakeApp(b"binary"), FakeApp(object())]
        valid = FakeApp("Valid")
        cls = production_methods(bad + [valid])
        self.assertEqual(cls._desktop_apps(), [valid])

    def test_name_is_captured_once_before_sort(self):
        a = FakeApp("Bravo", name_after_first=FakeGLibError("getter failed"))
        b = FakeApp("alpha", name_after_first=None)
        cls = production_methods([a, b])
        self.assertEqual(cls._desktop_apps(), [b, a])
        self.assertEqual(a.name_calls, 1)
        self.assertEqual(b.name_calls, 1)

    @staticmethod
    def filled(apps, query="", *, broken_row=None):
        cls = production_methods(apps)
        instance = cls()
        instance.apps = list(apps)
        instance.app_list = FakeList()
        instance.search = FakeSearch(query)
        launched = []
        instance.launch_terminal = lambda: launched.append("Terminal")
        instance._launch_desktop_app = lambda app: launched.append(app)

        def row(name, icon, app_icon=None):
            if name == broken_row:
                raise ValueError("malformed icon row")
            return FakeButton(name)
        instance._launcher_row = row
        instance._fill_launcher()
        return instance.app_list, launched

    def test_fill_launcher_skips_bad_name_id_icon_and_row(self):
        valid = FakeApp("Editor", desktop_id="org.example.Editor.desktop")
        apps = [
            FakeApp(123), FakeApp("Bad ID", desktop_id=b"bad.desktop"),
            FakeApp("Bad name", name_error=FakeGLibError("invalid")),
            FakeApp("Bad ID getter", id_error=AttributeError("invalid")),
            FakeApp("Bad icon", icon_error=FakeGLibError("invalid")),
            FakeApp("Bad row"), FakeApp("Foot duplicate", desktop_id="foot.desktop"),
            valid,
        ]
        view, _ = self.filled(apps, "", broken_row="Bad row")
        self.assertEqual([row.name for row in view.children], ["Terminal", "Editor"])
        self.assertTrue(view.mapped)

    def test_fill_launcher_preserves_terminal_and_gio_launch_callback(self):
        app = FakeApp("Document editor")
        view, launched = self.filled([app])
        self.assertEqual([row.name for row in view.children], ["Terminal", "Document editor"])
        for button in view.children:
            button.activate()
        self.assertEqual(launched, ["Terminal", app])

    def test_fill_launcher_filters_bad_flapping_getter_without_crashing(self):
        glitch = FakeApp("Visible", name_after_first=FakeGLibError("changed"))
        valid = FakeApp("Healthy")
        self.assertEqual(production_methods([glitch])._desktop_apps(), [glitch])
        view, _ = self.filled([glitch, valid], "h")
        self.assertEqual([row.name for row in view.children], ["Healthy"])

    def test_built_in_terminal_target_exists_only_when_row_is_visible(self):
        apps = [FakeApp("Document viewer")]
        cls = production_methods(apps)
        instance = cls()
        instance.apps = apps
        instance.search = FakeSearch("document")
        instance.app_list = FakeList()
        instance._launcher_row = lambda name, *_: FakeButton(name)
        instance._fill_launcher()
        self.assertIsNone(instance.terminal_row)
        self.assertEqual([row.name for row in instance.app_list.children],
                         ["Document viewer"])
        instance.search.query = ""
        instance._fill_launcher()
        self.assertIs(instance.terminal_row, instance.app_list.children[0])
        self.assertEqual(instance.terminal_row.name, "Terminal")

    def test_gio_eligible_inventory_and_actually_packed_rows_differ(self):
        items = [
            FakeApp("Foot", desktop_id="foot.desktop"),
            FakeApp("Files", desktop_id="org.example.Files.desktop"),
            FakeApp("Files duplicate", desktop_id="org.example.Files.desktop"),
            FakeApp("Broken", desktop_id="../exec.desktop"),
            FakeApp("Bad row", desktop_id="org.example.Bad.desktop"),
            FakeApp("Editor", desktop_id="org.example.Editor.desktop"),
        ]
        cls = production_methods(items)
        instance = cls()
        instance.apps = items
        instance.search = FakeSearch("")
        instance.app_list = FakeList()
        def row(name, *_args):
            if name == "Bad row":
                raise ValueError("GTK icon lookup failed")
            return FakeButton(name)
        instance._launcher_row = row
        instance._fill_launcher()
        self.assertEqual(instance._eligible_ids, [
            "foot.desktop", "org.example.Files.desktop",
            "org.example.Bad.desktop", "org.example.Editor.desktop",
        ])
        self.assertEqual(instance._displayed_ids, [
            "org.example.Files.desktop", "org.example.Editor.desktop",
        ])
        self.assertEqual([button.name for button in instance.app_list.children],
                         ["Terminal", "Files", "Editor"])

    def test_real_gio_inventory_caps_eligible_count_and_serial_character_budget(self):
        cls = production_methods([])
        for ids in (
            [f"org.example.App{i:03d}.desktop" for i in range(120)],
            [f"org.example.{i:03d}." + "a" * 98 + ".desktop" for i in range(80)],
        ):
            with self.subTest(input_count=len(ids), id_length=len(ids[0])):
                instance = cls()
                instance.apps = [FakeApp(f"App {i}", desktop_id=name)
                                 for i, name in enumerate(ids)]
                instance.search = FakeSearch("")
                instance.app_list = FakeList()
                instance._launcher_row = lambda name, *_: FakeButton(name)
                instance._fill_launcher()
                self.assertLessEqual(len(instance._eligible_ids), 60)
                self.assertLessEqual(sum(map(len, instance._eligible_ids)), 1600)
                self.assertEqual(set(instance._eligible_ids),
                                 set(instance._displayed_ids))
                self.assertNotIn("foot.desktop", instance._eligible_ids)
                self.assertEqual(len(instance.app_list.children),
                                 len(instance._displayed_ids) + 1)

    def test_fill_launcher_caps_entries_and_discards_old_rows(self):
        apps = [FakeApp(f"App {i:02d}",
                        desktop_id=f"org.example.App{i:02d}.desktop") for i in range(80)]
        cls = production_methods(apps)
        instance = cls()
        instance.apps = apps
        instance.search = FakeSearch("")
        instance.app_list = FakeList()
        old = FakeButton("Stale")
        instance.app_list.children.append(old)
        instance._launcher_row = lambda name, *_: FakeButton(name)
        instance._fill_launcher()
        self.assertEqual(len(instance.app_list.children), 61)
        self.assertEqual(instance.app_list.removed, [old])
        self.assertNotIn(old, instance.app_list.children)

    @staticmethod
    def launch(app):
        cls = production_methods([app])
        instance = cls()
        hidden = []
        instance.hide_launcher = lambda: hidden.append("hide")
        errors = io.StringIO()
        with redirect_stderr(errors):
            instance._launch_desktop_app(app)
        return hidden, errors.getvalue()

    def test_malicious_quoted_exec_never_passes_through_shell(self):
        app = FakeApp(
            "Untrusted", exec_line='sh -c "touch /tmp/p3-not-executed"; $(id)',
        )
        hidden, errors = self.launch(app)
        self.assertEqual(hidden, ["hide"])
        self.assertEqual(errors, "")
        self.assertEqual(app.launch_calls, [([], None)])
        self.assertEqual(app.exec_line, 'sh -c "touch /tmp/p3-not-executed"; $(id)')

    def test_negative_appinfo_launch_result_is_handled(self):
        app = FakeApp(launch_value=False)
        hidden, errors = self.launch(app)
        self.assertEqual(hidden, ["hide"])
        self.assertEqual(app.launch_calls, [([], None)])
        self.assertIn("desktop entry did not launch", errors)

    def test_glib_launch_error_is_handled_without_subprocess_fallback(self):
        app = FakeApp(launch_error=FakeGLibError("desktop entry refused"))
        hidden, errors = self.launch(app)
        self.assertEqual(hidden, ["hide"])
        self.assertEqual(app.launch_calls, [([], None)])
        self.assertIn("desktop entry refused", errors)

    def test_runtime_launch_error_is_handled(self):
        app = FakeApp(launch_error=RuntimeError("launch unavailable"))
        hidden, errors = self.launch(app)
        self.assertEqual(hidden, ["hide"])
        self.assertEqual(app.launch_calls, [([], None)])
        self.assertIn("launch unavailable", errors)


if __name__ == "__main__":
    unittest.main()
