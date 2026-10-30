"""COSMIC shortcuts: reading its files, writing only the launcher's entries
and giving taken keys back."""
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import shortcuts  # noqa: E402

DEFAULTS = textwrap.dedent('''\
    {
        (modifiers: [Super], key: "q"): Close,
        (modifiers: [Super], key: "t"): System(Terminal),
        (modifiers: [Super, Shift], key: "Left"): Move(Left),
        (modifiers: [Super], key: "s"): ToggleStacking,
        (modifiers: [Super]): System(Launcher),
    }
''')
CUSTOM = textwrap.dedent('''\
    {
        // My keys
        (modifiers: [Super], key: "w"): Close,
        (modifiers: [Super], key: "s"): Disable,
        (modifiers: [Super], key: "Return"): System(Terminal),
        (
            modifiers: [
                Ctrl,
                Alt,
            ],
            key: "n",
            description: Some("Notes"),
        ): Spawn("xed \\"a b\\""),
    }
''')


def app_entry(name, **combo):
    return {"name": name, "launch": [f"/apps/{name}.desktop"], "disabled": [],
            "shortcuts": [{**combo, "action": "", "replaces": "", "taken_from": combo.get("taken_from", "")}]}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.custom = self.dir / "custom"
        self.custom.write_text(CUSTOM)
        (self.dir / "defaults").write_text(DEFAULTS)
        self.state = self.dir / "shortcuts.json"

    def settings(self):
        return shortcuts.CosmicSettings(self.dir, self.dir / "defaults", self.dir / "taken.json")

    def apply(self, apps):
        settings = self.settings()
        shortcuts.save_and_apply({"apps": apps, "applied": []}, settings, self.state)
        return settings

    def entries(self):
        return shortcuts.parse_cosmic_shortcuts(self.custom.read_text())[1]


class ParseTests(unittest.TestCase):
    def test_reads_entries_comments_and_pretty_form(self):
        _head, entries, _tail = shortcuts.parse_cosmic_shortcuts(CUSTOM)
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[3]["combo"], {"mods": ["CTRL", "ALT"], "key": "N"})
        self.assertEqual(entries[3]["description"], "Notes")

    def test_missing_trailing_comma(self):
        entries = shortcuts.parse_cosmic_shortcuts('{ (modifiers: [Super], key: "a"): Close }')[1]
        self.assertEqual(entries[0]["action"], "Close")

    def test_comma_and_brace_inside_a_string(self):
        text = '{ (modifiers: [Super], key: "a"): Spawn("x, } // y"), }'
        self.assertEqual(shortcuts.parse_cosmic_shortcuts(text)[1][0]["action"], 'Spawn("x, } // y")')

    def test_broken_files_are_refused(self):
        for text in ("", "[]", "{", '{ (modifiers: [Super], key: "a): Close }'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                shortcuts.parse_cosmic_shortcuts(text)

    def test_quoting_round_trip(self):
        for text in ('plain', 'a "b" c', 'back\\slash'):
            self.assertEqual(shortcuts._ron_unquote(shortcuts._ron_quote(text)[1:-1]), text)


class ReadTests(Base):
    def test_desktop_binds_merge_defaults_and_custom(self):
        binds = {shortcuts.combo_label(b["combo"]): b for b in shortcuts.read_desktop_binds(self.settings())}
        self.assertEqual(binds["Super + W"]["description"], "Close")      # custom
        self.assertEqual(binds["Super + T"]["description"], "Terminal")   # default
        self.assertEqual(binds["Super + Shift + Left"]["description"], "Move Left")
        self.assertNotIn("Super + S", binds)                              # Disabled: free

    def test_user_commands_are_custom_binds(self):
        binds = shortcuts.read_custom_binds(self.settings())
        self.assertEqual([(b["description"], b["command"]) for b in binds], [("Notes", 'xed "a b"')])

    def test_where_survives_loading_state(self):
        self.assertTrue(shortcuts._clean_where("cosmic.shortcuts:super-shift-left"))


class WriteTests(Base):
    def test_only_adds_the_launcher_entry(self):
        self.apply({"a": app_entry("Firefox", mods=["SUPER", "SHIFT"], key="B")})
        text = self.custom.read_text()
        self.assertTrue(text.startswith(CUSTOM.rstrip()[:-1].rstrip()))
        last = self.entries()[-1]
        self.assertEqual(last["description"], "Firefox (Launcher)")
        self.assertEqual(last["combo"], {"mods": ["SUPER", "SHIFT"], "key": "B"})
        self.assertIn('launch_entry.py', last["action"])
        self.assertIn('key: "b"', last["raw"])

    def test_removing_restores_the_file_exactly(self):
        self.apply({"a": app_entry("Firefox", mods=["SUPER", "SHIFT"], key="B")})
        self.apply({})
        self.assertEqual(self.custom.read_text().split(), CUSTOM.split())

    def test_rewriting_keeps_one_entry(self):
        for key in "BC":
            self.apply({"a": app_entry("Firefox", mods=["SUPER", "SHIFT"], key=key)})
        own = [e for e in self.entries() if e["description"].endswith("(Launcher)")]
        self.assertEqual([e["combo"]["key"] for e in own], ["C"])

    def test_taking_a_default_and_giving_it_back(self):
        where = "cosmic.shortcuts:super-t"
        self.apply({"a": app_entry("Firefox", mods=["SUPER"], key="T", taken_from=where)})
        keys = [e for e in self.entries() if e["combo"] == {"mods": ["SUPER"], "key": "T"}]
        self.assertEqual(len(keys), 1)  # Own entry only: no Disable beside it.
        self.assertIn("Spawn", keys[0]["action"])
        self.apply({})
        self.assertEqual(self.custom.read_text().split(), CUSTOM.split())

    def test_taking_a_user_override_brings_it_back(self):
        where = "cosmic.shortcuts:super-w"
        self.apply({"a": app_entry("Firefox", mods=["SUPER"], key="W", taken_from=where)})
        self.apply({})
        close = [e for e in self.entries() if e["combo"] == {"mods": ["SUPER"], "key": "W"}]
        self.assertEqual([e["action"] for e in close], ["Close"])

    def test_taking_a_user_command_brings_it_back(self):
        where = f"custom:{shortcuts.CUSTOM_BASE}cosmic-ctrl-alt-n/"
        self.apply({"a": app_entry("Firefox", mods=["CTRL", "ALT"], key="N", taken_from=where)})
        self.assertEqual(shortcuts.read_custom_binds(self.settings()), [])
        self.apply({})
        self.assertEqual([b["description"] for b in shortcuts.read_custom_binds(self.settings())], ["Notes"])

    def test_a_free_disabled_key_is_disabled_again_after_use(self):
        self.apply({"a": app_entry("Firefox", mods=["SUPER"], key="S")})
        self.assertNotIn("Disable", [e["action"] for e in self.entries()])
        self.apply({})
        self.assertEqual(sorted(e["raw"] for e in self.entries()),
                         sorted(e["raw"] for e in shortcuts.parse_cosmic_shortcuts(CUSTOM)[1]))

    def test_turning_a_binding_off_writes_disable(self):
        entry = {**app_entry("Firefox", mods=["SUPER", "SHIFT"], key="B"),
                 "disabled": [{"mods": ["SUPER"], "key": "Q", "where": "cosmic.shortcuts:super-q"}]}
        self.apply({"a": entry})
        off = [e for e in self.entries() if e["combo"] == {"mods": ["SUPER"], "key": "Q"}]
        self.assertEqual([e["action"] for e in off], ["Disable"])

    def test_unreadable_file_is_left_alone(self):
        self.custom.write_text("{ broken")
        with self.assertRaises(ValueError):
            self.apply({"a": app_entry("Firefox", mods=["SUPER", "SHIFT"], key="B")})
        self.assertEqual(self.custom.read_text(), "{ broken")

    def test_missing_file_is_created(self):
        self.custom.unlink()
        self.apply({"a": app_entry("Firefox", mods=["SUPER", "SHIFT"], key="B")})
        self.assertEqual(len(self.entries()), 1)

    def test_changes_made_in_cosmic_settings_are_seen(self):
        settings = self.settings()
        self.custom.write_text('{ (modifiers: [Super], key: "z"): Close, }')
        keys = [k for _s, k, _d, _a in settings.binding_keys()]
        self.assertIn("super-z", keys)
        self.assertNotIn("super-w", keys)

    def test_taken_state_is_saved_for_later_runs(self):
        self.apply({"a": app_entry("Firefox", mods=["SUPER"], key="W", taken_from="cosmic.shortcuts:super-w")})
        saved = json.loads((self.dir / "taken.json").read_text())
        self.assertEqual(saved["taken"]["super-w"], '(modifiers: [Super], key: "w"): Close')


class BackendChoiceTests(unittest.TestCase):
    def test_desktop_name(self):
        from unittest import mock
        for value, name in (("COSMIC", "COSMIC"), ("pop:COSMIC", "COSMIC"), ("GNOME", "GNOME"), ("", "GNOME")):
            with mock.patch.dict("os.environ", {"XDG_CURRENT_DESKTOP": value}):
                self.assertEqual(shortcuts.desktop_name(), name)


if __name__ == "__main__":
    unittest.main()
