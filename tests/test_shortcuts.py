import tempfile
import unittest
from pathlib import Path

import shortcuts

LIVE = """bindd
\tmodmask: 64
\tsubmap:
\tkey: F
\tkeycode: 0
\tdescription: Full screen

bindd
\tmodmask: 64
\tsubmap:
\tkey: SUPER + code:10
\tkeycode: 0
\tdescription: Switch to workspace 1

bindd
\tmodmask: 65
\tsubmap:
\tkey: B
\tkeycode: 0
\tdescription: Brave

bindd
\tmodmask: 64
\tsubmap: resize
\tkey: R
\tkeycode: 0
\tdescription: Inside a submap
"""

USER = '''hl.unbind("SUPER + SHIFT + B")
o.bind("SUPER + SHIFT + B", "Brave", { launch = "brave" })
o.bind("SUPER + SHIFT + Z", "Gone", { launch = "zed" })
hl.unbind("SUPER + SHIFT + Z")
-- o.bind("SUPER + SHIFT + Q", "Commented", "true")
'''


def combo(text):
    return shortcuts.parse_combo(text)


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "bindings.lua"
        path.write_text(USER)
        self.user = shortcuts.parse_user_binds([path])
        self.live = shortcuts.parse_live_binds(LIVE)

    def tearDown(self):
        self.tmp.cleanup()

    def bindings(self, state=None):
        return shortcuts.Bindings(state or {"apps": {}}, live=self.live, user=self.user)

    def test_parse_combo_normalizes_modifiers_and_rejects_junk(self):
        self.assertEqual(combo("alt + super + f"), {"mods": ["SUPER", "ALT"], "key": "F"})
        self.assertEqual(combo("SUPER, code:10")["key"], "code:10")
        self.assertIsNone(combo("SUPER + HYPER + F"))
        self.assertIsNone(combo('SUPER + F") os.execute("x'))

    def test_live_binds_skip_submaps_and_read_keycodes(self):
        keys = {(b["mask"], b["key"]) for b in self.live}
        self.assertEqual(keys, {(64, "f"), (64, "code:10"), (65, "b")})

    def test_user_binds_respect_line_order(self):
        binds, unbinds = self.user
        self.assertEqual([shortcuts.combo_label(b["combo"]) for b in binds], ["Super + Shift + B"])
        self.assertIn(shortcuts.combo_id(combo("SUPER + SHIFT + Z")), unbinds)

    def test_classification_levels(self):
        b = self.bindings()
        self.assertEqual(b.classify(combo("SUPER + ALT + K"), "app")[0], shortcuts.FREE)
        self.assertEqual(b.classify(combo("SUPER + SHIFT + B"), "app"), (shortcuts.CUSTOM, "Brave"))
        self.assertEqual(b.classify(combo("SUPER + F"), "app"), (shortcuts.OMARCHY, "Full screen"))
        # A keysym bind is found through the physical keycode as well.
        self.assertEqual(b.classify(combo("SUPER + 1"), "app", keycode=10)[0], shortcuts.OMARCHY)

    def test_launcher_owned_combos(self):
        state = {"apps": {"a": {"name": "Alpha", "launch": ["/a.desktop"], "disabled": [],
                                "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "K", "replaces": ""}]}}}
        b = self.bindings(state)
        self.assertEqual(b.classify(combo("SUPER + ALT + K"), "a")[0], shortcuts.SAME)
        self.assertEqual(b.classify(combo("SUPER + ALT + K"), "b"), (shortcuts.LAUNCHER, "Alpha"))

    def test_suggestions_are_free(self):
        b = self.bindings()
        found = b.suggestions({"name": "Firefox"}, "app")
        self.assertTrue(found)
        for item in found:
            self.assertEqual(b.classify(item, "app")[0], shortcuts.FREE)

    def test_suggestions_follow_selected_modifiers(self):
        b = self.bindings()
        found = b.suggestions({"name": "Brave"}, "app", ["SUPER", "CTRL"])
        self.assertTrue(found)
        self.assertTrue(all(c["mods"] == ["SUPER", "CTRL"] for c in found))
        self.assertIn("B", [c["key"] for c in found])
        self.assertEqual([c["key"] for c in found], sorted(c["key"] for c in found))
        self.assertNotIn("F", [c["key"] for c in b.suggestions({"name": "Firefox"}, "app", ["SUPER"])])

    def test_external_shortcuts_match_launch_command(self):
        b = self.bindings()
        found = b.external_shortcuts({"name": "Brave", "exec": "brave", "desktop_id": "brave-browser"})
        self.assertEqual([x["description"] for x in found], ["Brave"])

    def test_render_lua_quotes_names_and_paths(self):
        state = {"apps": {"x": {"name": 'Evil "name"\nhl.exec_cmd("rm")',
                                "launch": ["/tmp/a b/$(x).desktop"], "disabled": [],
                                "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "E", "replaces": ""}]}}}
        lua = shortcuts.render_lua(state)
        self.assertIn('hl.unbind("SUPER + ALT + E")', lua)
        self.assertIn('"Evil \\"name\\"\\nhl.exec_cmd(\\"rm\\")"', lua)
        self.assertIn("'/tmp/a b/$(x).desktop'", lua)
        self.assertNotIn('\nhl.exec_cmd("rm")', lua)

    def test_default_mods_round_trip(self):
        original = shortcuts.SETTINGS_FILE
        shortcuts.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"
        try:
            self.assertEqual(shortcuts.load_default_mods(), ["SUPER", "SHIFT"])
            shortcuts.save_default_mods(["CTRL", "SUPER"])
            self.assertEqual(shortcuts.load_default_mods(), ["SUPER", "CTRL"])
            shortcuts.SETTINGS_FILE.write_text('{"shortcut_default_mods": ["HYPER"]}')
            self.assertEqual(shortcuts.load_default_mods(), ["SUPER", "SHIFT"])
        finally:
            shortcuts.SETTINGS_FILE = original

    def test_load_state_drops_invalid_entries(self):
        path = Path(self.tmp.name) / "state.json"
        path.write_text('{"apps": {"a": {"name": "A", "launch": ["/a"], "shortcuts": '
                        '[{"mods": ["SUPER"], "key": "bad key"}, {"mods": ["SUPER"], "key": "k"}]},'
                        ' "b": {"name": "B", "launch": "not-a-list", "shortcuts": [{"mods": [], "key": "X"}]}}}')
        original = shortcuts.STATE_FILE
        shortcuts.STATE_FILE = path
        try:
            state = shortcuts.load_state()
        finally:
            shortcuts.STATE_FILE = original
        self.assertEqual(list(state["apps"]), ["a"])
        self.assertEqual(state["apps"]["a"]["shortcuts"][0]["key"], "K")


if __name__ == "__main__":
    unittest.main()
