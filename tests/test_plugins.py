"""Shell plugins in the launcher: discovery, opening and their shortcuts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher_core
import shortcuts

CLIPBOARD = {"name": "Clipboard", "plugin_id": "omarchy.clipboard", "desktop_id": "plugin:omarchy.clipboard",
             "exec": "omarchy-shell shell toggle omarchy.clipboard", "actions": [], "desktop_path": ""}


class PluginShortcutTests(unittest.TestCase):
    def test_plugin_shortcut_toggles_through_the_shell(self):
        self.assertEqual(shortcuts.app_launch_argv(CLIPBOARD),
                         ["omarchy-shell", "shell", "toggle", "omarchy.clipboard"])
        self.assertEqual(shortcuts.app_key(CLIPBOARD), "plugin:omarchy.clipboard")
        state = {"apps": {"plugin:omarchy.clipboard": {
            "name": "Clipboard", "launch": shortcuts.app_launch_argv(CLIPBOARD), "disabled": [],
            "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "V", "replaces": "", "action": ""}]}}}
        lua = shortcuts.render_lua(state)
        self.assertIn('o.bind("SUPER + ALT + V", "Clipboard", "omarchy-shell shell toggle omarchy.clipboard")', lua)
        self.assertNotIn("uwsm-app", lua)
        self.assertNotIn("launch =", lua)

    def test_plugin_toggle_requires_a_clean_id(self):
        self.assertIsNone(shortcuts.plugin_toggle_id(["omarchy-shell", "shell", "toggle", "a;rm -rf"]))
        self.assertIsNone(shortcuts.plugin_toggle_id(["/x/app.desktop"]))

    def test_plugin_state_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(shortcuts, "STATE_FILE", Path(tmp) / "s.json"):
            launch = shortcuts.app_launch_argv(CLIPBOARD)
            shortcuts.STATE_FILE.write_text(json.dumps({"apps": {"plugin:omarchy.clipboard": {
                "name": "Clipboard", "launch": launch, "disabled": [],
                "shortcuts": [{"mods": ["SUPER"], "key": "V"}]}}}))
            state = shortcuts.load_state()
        self.assertEqual(state["apps"]["plugin:omarchy.clipboard"]["launch"], launch)
        self.assertEqual(shortcuts.prune_missing(state), [])

    def test_omarchy_default_plugin_binding_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clipboard.lua"
            path.write_text('o.bind("SUPER + CTRL + V", "Clipboard manager", "omarchy-shell shell toggle omarchy.clipboard")\n'
                            'o.bind("SUPER + CTRL + X", "Other", "omarchy-shell shell toggle omarchy.clipboard-extra")\n')
            defaults = shortcuts.parse_user_binds([path])[0]
        live = [{"mask": 68, "key": "v", "description": "Clipboard manager"},
                {"mask": 68, "key": "x", "description": "Other"}]
        bindings = shortcuts.Bindings({"apps": {}}, live=live, user=([], set()), defaults=defaults)
        found = bindings.external_shortcuts(CLIPBOARD)
        self.assertEqual([shortcuts.combo_label(b["combo"]) for b in found], ["Super + Ctrl + V"])
        self.assertEqual(found[0]["action"], "")


class LauncherShortcutTests(unittest.TestCase):
    def test_launcher_shortcut_runs_its_toggle_script(self):
        entry = launcher_core.self_entry()
        launch = shortcuts.app_launch_argv(entry)
        self.assertEqual(launch, [shortcuts.SELF_TOGGLE])
        self.assertTrue(Path(launch[0]).is_file())
        self.assertFalse(shortcuts.launch_target_missing(launch))
        state = {"apps": {entry["desktop_id"]: {"name": "Simple Launcher", "launch": launch, "disabled": [],
                 "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "space", "replaces": "", "action": ""}]}}}
        lua = shortcuts.render_lua(state)
        self.assertIn(f'o.bind("SUPER + ALT + space", "Simple Launcher", "{shortcuts.SELF_TOGGLE}")', lua)
        self.assertNotIn("uwsm-app", lua)

    def test_launcher_list_shortcuts_pass_the_list(self):
        entry = launcher_core.self_entry()
        self.assertEqual([a["id"] for a in entry["actions"]], ["apps", "plugins"])
        launch = shortcuts.app_launch_argv(entry)
        self.assertEqual(shortcuts.shortcut_launch(launch, "plugins"), [shortcuts.SELF_TOGGLE, "plugins"])
        state = {"apps": {entry["desktop_id"]: {"name": "Simple Launcher", "launch": launch, "disabled": [],
                 "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "W", "replaces": "", "action": "plugins"}]}}}
        self.assertIn(f'"{shortcuts.SELF_TOGGLE} plugins")', shortcuts.render_lua(state))

    def test_hand_written_list_binding_maps_to_its_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bindings.lua"
            path.write_text('o.bind("SUPER + ALT + W", "Widgets", "/x/launcher-toggle plugins")\n'
                            'o.bind("SUPER + ALT + A", "Apps", "/x/launcher-toggle \'apps\'")\n')
            user = shortcuts.parse_user_binds([path])
        bindings = shortcuts.Bindings({"apps": {}}, live=[], user=user, defaults=[])
        found = bindings.external_shortcuts(launcher_core.self_entry())
        self.assertEqual([b["action"] for b in found], ["plugins", "apps"])

    def test_hand_written_launcher_binding_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bindings.lua"
            path.write_text('o.bind("SUPER + CTRL + ALT + SPACE", "Toggle Simple Launcher", '
                            '"/home/x/.config/omarchy/plugins/ubruckhaus.simple-launcher-for-fools/launcher-toggle")\n')
            user = shortcuts.parse_user_binds([path])
        bindings = shortcuts.Bindings({"apps": {}}, live=[], user=user, defaults=[])
        found = bindings.external_shortcuts(launcher_core.self_entry())
        self.assertEqual([shortcuts.combo_label(b["combo"]) for b in found], ["Super + Ctrl + Alt + Space"])
        self.assertEqual(found[0]["file"], "bindings.lua")


class PluginDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def plugin(self, name, kinds, qml=None):
        directory = self.root / name
        directory.mkdir()
        manifest = {"id": name, "name": name.title(), "kinds": kinds, "description": f"{name} plugin",
                    "entryPoints": {"barWidget": "BarWidget.qml"} if qml is not None else {}}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        if qml is not None:
            (directory / "BarWidget.qml").write_text(qml)
        return directory, manifest

    def test_only_plugins_the_shell_can_open(self):
        self.assertTrue(launcher_core.plugin_can_open(*self.plugin("overlay", ["overlay"])))
        self.assertTrue(launcher_core.plugin_can_open(*self.plugin("panel", ["bar-widget"], "Panel {\n}")))
        self.assertTrue(launcher_core.plugin_can_open(*self.plugin(
            "own", ["bar-widget"], "Ui.BarWidget {\n  property bool opened\n  function open() {}\n}")))
        self.assertFalse(launcher_core.plugin_can_open(*self.plugin(
            "button", ["bar-widget"], "BarWidget {\n  onClicked: run()\n}")))
        self.assertFalse(launcher_core.plugin_can_open(*self.plugin("service", ["service"])))

    def test_parse_plugins_lists_enabled_openable_plugins(self):
        self.plugin("zeta", ["overlay"])
        self.plugin("alpha", ["panel"])
        self.plugin("off", ["panel"])
        self.plugin("button", ["bar-widget"], "BarWidget {}")
        listed = [{"id": i, "name": i.title(), "enabled": i != "off"}
                  for i in ("zeta", "alpha", "off", "button", launcher_core.SELF_PLUGIN_ID)]
        with mock.patch.object(launcher_core, "plugin_roots", return_value=(self.root,)), \
                mock.patch.object(launcher_core.shutil, "which", return_value="/usr/bin/omarchy-shell"), \
                mock.patch.object(launcher_core, "read_bounded_output", return_value=json.dumps(listed)):
            plugins = launcher_core.parse_plugins()
        self.assertEqual([p["plugin_id"] for p in plugins], ["alpha", launcher_core.SELF_PLUGIN_ID, "zeta"])
        self.assertTrue(plugins[1]["self_launcher"])
        self.assertEqual(plugins[0]["desktop_id"], "plugin:alpha")
        self.assertEqual(plugins[0]["description"], "alpha plugin")

    def test_parse_plugins_without_shell_lists_only_the_launcher(self):
        with mock.patch.object(launcher_core.shutil, "which", return_value=None):
            self.assertEqual([p["name"] for p in launcher_core.parse_plugins()], ["Simple Launcher"])

    def test_open_plugin_reports_shell_answer(self):
        read = mock.Mock(return_value="unknown\n")
        with mock.patch.object(launcher_core, "read_bounded_output", read):
            self.assertFalse(launcher_core.open_plugin(CLIPBOARD))
            read.return_value = "ok\n"
            self.assertTrue(launcher_core.open_plugin(CLIPBOARD))
            read.return_value = None  # failed, timed out or too much output
            self.assertFalse(launcher_core.open_plugin(CLIPBOARD))
        self.assertEqual(read.call_args[0][0], ["omarchy-shell", "shell", "summon", "omarchy.clipboard", "{}"])


class NotificationTextTests(unittest.TestCase):
    def test_notifications_carry_no_markup(self):
        import ast
        source = Path(__file__).resolve().parents[1] / "launcher_popup.py"
        func = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == "plain_text")
        import re
        namespace = {"re": re}
        exec(compile(ast.Module(body=[func], type_ignores=[]), str(source), "exec"), namespace)
        plain = namespace["plain_text"]
        text = plain('Quit <img src="http://x/y.png"> &amp;\r\nnext' + "x" * 500)
        self.assertNotRegex(text, r"[<>&\x00-\x1f]")
        self.assertLessEqual(len(text), 300)


class PluginRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patch = mock.patch.object(launcher_core, "USER_PLUGINS_DIR", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def git(self, directory, *args):
        import subprocess
        subprocess.run(["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    def test_only_installed_plugins_are_removable(self):
        (self.root / "mine").mkdir()
        (self.root / "linked").symlink_to(self.root / "mine")
        with mock.patch.object(launcher_core.shutil, "which", return_value="/usr/bin/omarchy-plugin-remove"):
            self.assertTrue(launcher_core.plugin_removable("mine"))
            self.assertTrue(launcher_core.plugin_removable("linked"))
            self.assertFalse(launcher_core.plugin_removable("omarchy.clipboard"))
            self.assertFalse(launcher_core.plugin_removable("../etc"))

    def test_removal_note_warns_about_local_git_work(self):
        upstream, plugin = self.root / "upstream.git", self.root / "plugin"
        self.git(self.root, "init", "--bare", "-q", str(upstream))
        self.git(self.root, "clone", "-q", str(upstream), str(plugin))
        (plugin / "a").write_text("a")
        self.git(plugin, "add", "a")
        self.git(plugin, "commit", "-qm", "a")
        self.git(plugin, "push", "-q", "origin", "HEAD")
        note, loses = launcher_core.plugin_removal_note("plugin")
        self.assertFalse(loses, note)
        (plugin / "b").write_text("b")
        self.git(plugin, "add", "b")
        self.git(plugin, "commit", "-qm", "b")
        (plugin / "c").write_text("c")
        note, loses = launcher_core.plugin_removal_note("plugin")
        self.assertTrue(loses)
        self.assertIn("1 changed file", note)
        self.assertIn("1 unpushed commit", note)

    def test_plain_folders_are_backed_up(self):
        (self.root / "plain").mkdir()
        note, loses = launcher_core.plugin_removal_note("plain")
        self.assertFalse(loses)
        self.assertIn("backup", note)

    def test_shortcuts_of_removed_plugins_are_pruned(self):
        gone = ["omarchy-shell", "shell", "toggle", "removed.plugin"]
        with mock.patch.object(shortcuts, "plugin_installed", return_value=False):
            self.assertTrue(shortcuts.launch_target_missing(gone))
        with mock.patch.object(shortcuts, "plugin_installed", return_value=True):
            self.assertFalse(shortcuts.launch_target_missing(gone))
