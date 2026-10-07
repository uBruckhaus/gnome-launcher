"""Tray icons: menu parsing, action ids and their shortcuts."""
import unittest
from unittest import mock

import shortcuts
import tray

LAYOUT = [
    (21, {"label": "Not secured", "enabled": False}, []),
    (22, {"label": "_Secure my connection"}, []),
    (23, {"label": "All connections", "children-display": "submenu"}, [
        (24, {"label": "Countries:", "enabled": False}, []),
        (25, {"label": "Germany"}, []),
    ]),
    (26, {"type": "separator"}, []),
    (27, {"label": "Tray icon", "toggle-type": "checkmark", "toggle-state": 1}, []),
    (28, {"label": "Hidden", "visible": False}, []),
]
STEAM = {"name": "Steam", "tray_id": "steam", "desktop_id": "tray:steam", "actions": [], "exec": ""}


class TrayMenuTests(unittest.TestCase):
    def test_menu_nodes_keep_structure_and_drop_mnemonics(self):
        nodes = tray._menu_nodes(LAYOUT)
        self.assertEqual([n.get("label") for n in nodes],
                         ["Not secured", "Secure my connection", "All connections", None, "Tray icon"])
        self.assertEqual(nodes[2]["children"][1]["action"], "all-connections.germany")
        self.assertTrue(nodes[4]["checked"])
        self.assertEqual(nodes[4]["toggle"], "checkmark")

    def test_flat_actions_are_clickable_leaves(self):
        actions = tray.flat_actions(tray._menu_nodes(LAYOUT))
        self.assertEqual([(a["id"], a["name"]) for a in actions], [
            ("secure-my-connection", "Secure my connection"),
            ("all-connections.germany", "All connections › Germany"),
            ("tray-icon", "Tray icon")])
        for action in actions:
            self.assertRegex(action["id"], shortcuts._ACTION_RE)

    def test_run_clicks_the_entry_named_by_the_shortcut(self):
        item = {"tray_id": "NordVPN", "tray_menu": tray._menu_nodes(LAYOUT)}
        with mock.patch.object(tray, "list_items", return_value=[item]), \
                mock.patch.object(tray, "click", return_value=True) as click, \
                mock.patch.object(tray, "activate", return_value=True) as activate:
            self.assertTrue(tray.run("NordVPN", "all-connections.germany"))
            click.assert_called_once_with(item, 25)
            self.assertFalse(tray.run("NordVPN", "not-secured"))  # Disabled entries stay disabled.
            self.assertFalse(tray.run("Steam", "library"))
            self.assertTrue(tray.run("NordVPN"))
            activate.assert_called_once_with(item)

    def test_cli_rejects_bad_ids(self):
        self.assertEqual(tray.main(["tray.py", "a b"]), 2)
        self.assertEqual(tray.main(["tray.py"]), 2)


class TrayKeyTests(unittest.TestCase):
    def test_electron_icons_go_by_title(self):
        taken = set()
        first = tray.item_key("chrome_status_icon_1", "LM Studio", taken)
        taken.add(first)
        second = tray.item_key("chrome_status_icon_1", "Antigravity", taken)
        self.assertEqual((first, second), ("lm-studio", "antigravity"))
        self.assertEqual(tray.item_key("steam", "Steam", set()), "steam")
        # The same Id twice: the second one is told apart by its title.
        self.assertEqual(tray.item_key("app", "Other App", {"app"}), "other-app")
        self.assertIsNone(tray.item_key("", "", set()))

    def test_title_falls_back_to_tooltip(self):
        self.assertEqual(tray._title({"Title": "Steam"}), "Steam")
        self.assertEqual(tray._title({"Title": "", "ToolTip": ("", [], "LM Studio", "")}), "LM Studio")
        self.assertEqual(tray._title({"Title": ""}), "")


class TrayShortcutTests(unittest.TestCase):
    def test_tray_shortcut_runs_tray_script_directly(self):
        launch = shortcuts.app_launch_argv(STEAM)
        self.assertEqual(launch, [shortcuts.TRAY_SCRIPT, "steam"])
        self.assertEqual(shortcuts.tray_launch_id(launch), "steam")
        state = {"apps": {"tray:steam": {"name": "Steam", "launch": launch, "disabled": [], "shortcuts": [
            {"mods": ["SUPER", "ALT"], "key": "L", "replaces": "", "action": "library"},
            {"mods": ["SUPER", "ALT"], "key": "S", "replaces": "", "action": ""}]}}}
        lua = shortcuts.render_lua(state)
        self.assertIn(f'"{shortcuts.TRAY_SCRIPT} steam library"', lua)
        self.assertIn(f'"{shortcuts.TRAY_SCRIPT} steam"', lua)
        self.assertNotIn("uwsm-app", lua)
        self.assertNotIn("launch =", lua)

    def test_tray_icons_have_no_external_shortcuts(self):
        bindings = shortcuts.Bindings({"apps": {}}, live=[], user=([], set()), defaults=[])
        self.assertEqual(bindings.external_shortcuts(STEAM), [])


if __name__ == "__main__":
    unittest.main()
