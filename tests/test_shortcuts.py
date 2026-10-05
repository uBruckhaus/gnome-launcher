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

    def bindings(self, state=None, defaults=(), default_apps=None):
        return shortcuts.Bindings(state or {"apps": {}}, live=self.live, user=self.user,
                                  defaults=list(defaults), main_mods=["SUPER", "SHIFT"],
                                  default_apps=default_apps or {"terminal": "", "browser": "", "editor": ""})

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
        # B is taken with Super + Shift, so it is not offered with other modifiers either.
        self.assertNotIn("B", [c["key"] for c in found])
        self.assertIn("R", [c["key"] for c in found])
        self.assertEqual([c["key"] for c in found], sorted(c["key"] for c in found))
        self.assertNotIn("F", [c["key"] for c in b.suggestions({"name": "Firefox"}, "app", ["SUPER"])])
        # Super + F is taken, but not with the main modifiers, so F is offered elsewhere.
        self.assertIn("F", [c["key"] for c in b.suggestions({"name": "Firefox"}, "app", ["SUPER", "ALT"])])

    def test_external_shortcuts_match_launch_command(self):
        b = self.bindings()
        found = b.external_shortcuts({"name": "Brave", "exec": "brave", "desktop_id": "brave-browser"})
        self.assertEqual([x["description"] for x in found], ["Brave"])

    def test_external_shortcuts_include_live_omarchy_webapp_defaults(self):
        path = Path(self.tmp.name) / "applications.lua"
        path.write_text('o.bind("SUPER + SHIFT + Y", "YouTube", { webapp = "https://youtube.com/" })\n'
                        'o.bind("SUPER + SHIFT + X", "X", { webapp = "https://x.com/" })\n'
                        'o.bind("SUPER + SHIFT + B", "Browser", { omarchy = "browser" })\n')
        defaults, _ = shortcuts.parse_user_binds([path])
        self.live += shortcuts.parse_live_binds(
            "bindd\n\tmodmask: 65\n\tsubmap:\n\tkey: Y\n\tkeycode: 0\n\tdescription: YouTube\n")
        b = self.bindings(defaults=defaults)
        for app in ({"name": "YouTube", "exec": "omarchy-launch-webapp https://www.youtube.com"},
                    {"name": "YouTube", "exec": "chrome --app-id=abc"}):
            found = b.external_shortcuts(app)
            self.assertEqual([(x["description"], x["file"]) for x in found], [("YouTube", "Omarchy")])
        # X is not live (e.g. preinstalled bindings off), so it is not shown.
        self.assertEqual(b.external_shortcuts({"name": "X", "exec": "omarchy-launch-webapp https://x.com/"}), [])

    def test_external_shortcuts_include_omarchy_launch_defaults(self):
        path = Path(self.tmp.name) / "applications.lua"
        path.write_text('o.bind("SUPER + SHIFT + F", "File manager", { omarchy = "nautilus" })\n'
                        'o.bind("SUPER + ALT + SHIFT + F", "File manager (cwd)", { omarchy = "nautilus-cwd" })\n'
                        'o.bind("SUPER + SHIFT + RETURN", "Browser", { omarchy = "browser" })\n')
        defaults, _ = shortcuts.parse_user_binds([path])
        self.live += shortcuts.parse_live_binds(
            "bindd\n\tmodmask: 65\n\tsubmap:\n\tkey: F\n\tkeycode: 0\n\tdescription: File manager\n\n"
            "bindd\n\tmodmask: 73\n\tsubmap:\n\tkey: F\n\tkeycode: 0\n\tdescription: File manager (cwd)\n\n"
            "bindd\n\tmodmask: 65\n\tsubmap:\n\tkey: RETURN\n\tkeycode: 0\n\tdescription: Browser\n")
        b = self.bindings(defaults=defaults)
        files = {"name": "Files", "exec": "nautilus --new-window", "desktop_id": "org.gnome.Nautilus"}
        self.assertEqual([x["description"] for x in b.external_shortcuts(files)], ["File manager"])
        brave = {"name": "Brave", "exec": "brave", "desktop_id": "brave-browser"}
        self.assertNotIn("Browser", [x["description"] for x in b.external_shortcuts(brave)])

    def test_external_shortcuts_map_omarchy_terminal_to_default_terminal(self):
        path = Path(self.tmp.name) / "applications.lua"
        path.write_text('o.bind("SUPER + RETURN", "Terminal", { omarchy = "terminal" })\n')
        defaults, _ = shortcuts.parse_user_binds([path])
        self.live += shortcuts.parse_live_binds(
            "bindd\n\tmodmask: 64\n\tsubmap:\n\tkey: RETURN\n\tkeycode: 0\n\tdescription: Terminal\n")
        foot = {"name": "Foot", "exec": "foot", "desktop_id": "foot"}
        kitty = {"name": "kitty", "exec": "kitty", "desktop_id": "kitty"}
        b = self.bindings(defaults=defaults, default_apps={"terminal": "foot", "browser": ""})
        self.assertEqual([shortcuts.combo_label(x["combo"]) for x in b.external_shortcuts(foot)],
                         ["Super + Enter"])
        self.assertEqual(b.external_shortcuts(kitty), [])
        self.assertEqual(self.bindings(defaults=defaults).external_shortcuts(foot), [])

    def test_external_shortcuts_map_omarchy_browser_to_default_browser(self):
        path = Path(self.tmp.name) / "applications.lua"
        path.write_text('o.bind("SUPER + SHIFT + RETURN", "Browser", { omarchy = "browser" })\n'
                        'o.bind("SUPER + SHIFT + ALT + B", "Browser (private)", { omarchy = "browser --private" })\n')
        defaults, _ = shortcuts.parse_user_binds([path])
        self.live += shortcuts.parse_live_binds(
            "bindd\n\tmodmask: 65\n\tsubmap:\n\tkey: RETURN\n\tkeycode: 0\n\tdescription: Browser\n\n"
            "bindd\n\tmodmask: 73\n\tsubmap:\n\tkey: B\n\tkeycode: 0\n\tdescription: Browser (private)\n")
        chrome = {"name": "Google Chrome", "exec": "/usr/bin/google-chrome-stable %U",
                  "desktop_id": "google-chrome"}
        b = self.bindings(defaults=defaults, default_apps={"terminal": "", "browser": "google-chrome"})
        # Only the plain browser bind: the private one opens an incognito window.
        self.assertEqual([shortcuts.combo_label(x["combo"]) for x in b.external_shortcuts(chrome)],
                         ["Super + Shift + Enter"])
        self.assertEqual(self.bindings(defaults=defaults).external_shortcuts(chrome), [])

    CHROME = {"name": "Google Chrome", "exec": "/usr/bin/google-chrome-stable", "desktop_id": "google-chrome",
              "desktop_path": "/usr/share/applications/google-chrome.desktop",
              "actions": [{"id": "new-window", "name": "New Window", "exec": "/usr/bin/google-chrome-stable"},
                          {"id": "new-private-window", "name": "New Incognito Window",
                           "exec": "/usr/bin/google-chrome-stable --incognito"}]}

    def omarchy_defaults(self, text, live):
        path = Path(self.tmp.name) / "applications.lua"
        path.write_text(text)
        defaults, _ = shortcuts.parse_user_binds([path])
        self.live += shortcuts.parse_live_binds(live)
        return defaults

    def test_comments_do_not_cut_double_dashes_inside_strings(self):
        self.assertEqual(shortcuts._strip_lua_comment('o.bind("A", "B", { omarchy = "browser --private" }) -- x'),
                         'o.bind("A", "B", { omarchy = "browser --private" }) ')
        self.assertEqual(shortcuts._strip_lua_comment('-- o.bind("A")'), "")

    def test_omarchy_private_browser_maps_to_private_window_action(self):
        defaults = self.omarchy_defaults(
            'o.bind("SUPER + SHIFT + ALT + B", "Browser (private)", { omarchy = "browser --private" })\n',
            "bindd\n\tmodmask: 73\n\tsubmap:\n\tkey: B\n\tkeycode: 0\n\tdescription: Browser (private)\n")
        b = self.bindings(defaults=defaults, default_apps={"browser": "google-chrome"})
        self.assertEqual([(shortcuts.combo_label(x["combo"]), x["action"]) for x in b.external_shortcuts(self.CHROME)],
                         [("Super + Shift + Alt + B", "new-private-window")])

    def test_default_browser_does_not_claim_its_web_apps(self):
        defaults = self.omarchy_defaults(
            'o.bind("SUPER + SHIFT + RETURN", "Browser", { omarchy = "browser" })\n',
            "bindd\n\tmodmask: 65\n\tsubmap:\n\tkey: RETURN\n\tkeycode: 0\n\tdescription: Browser\n")
        b = self.bindings(defaults=defaults, default_apps={"browser": "google-chrome"})
        pwa = {"name": "Netflix", "exec": "/opt/google/chrome/google-chrome --app-id=abc",
               "desktop_id": "chrome-abc-Default"}
        self.assertEqual(b.external_shortcuts(pwa), [])
        self.assertEqual(len(b.external_shortcuts(self.CHROME)), 1)

    def test_tui_and_editor_defaults_match_their_programs(self):
        defaults = self.omarchy_defaults(
            'o.bind("SUPER + CTRL + T", "Activity", { tui = "btop" })\n'
            'o.bind("SUPER + SHIFT + N", "Editor", { omarchy = "editor" })\n',
            "bindd\n\tmodmask: 68\n\tsubmap:\n\tkey: T\n\tkeycode: 0\n\tdescription: Activity\n\n"
            "bindd\n\tmodmask: 65\n\tsubmap:\n\tkey: N\n\tkeycode: 0\n\tdescription: Editor\n")
        b = self.bindings(defaults=defaults, default_apps={"editor": "nvim"})
        btop = {"name": "btop++", "exec": "btop", "desktop_id": "btop"}
        nvim = {"name": "Neovim", "exec": "nvim", "desktop_id": "nvim"}
        self.assertEqual([x["description"] for x in b.external_shortcuts(btop)], ["Activity"])
        self.assertEqual([x["description"] for x in b.external_shortcuts(nvim)], ["Editor"])

    def test_user_bind_with_action_arguments_maps_to_that_action(self):
        path = Path(self.tmp.name) / "mine.lua"
        path.write_text('o.bind("SUPER + ALT + I", "Incognito", { launch = "google-chrome-stable --incognito" })\n')
        b = self.bindings()
        b.user_binds, b.user_unbinds = shortcuts.parse_user_binds([path])
        self.assertEqual([x["action"] for x in b.external_shortcuts(self.CHROME)], ["new-private-window"])

    def test_classify_treats_existing_same_binding_as_duplicate(self):
        b = self.bindings()
        brave = {"name": "Brave", "exec": "brave", "desktop_id": "brave-browser"}
        self.assertEqual(b.classify(combo("SUPER + SHIFT + B"), "brave", app=brave)[0], shortcuts.SAME)
        self.assertEqual(b.classify(combo("SUPER + SHIFT + B"), "brave")[0], shortcuts.CUSTOM)

    def test_launcher_shortcut_with_other_action_is_a_retarget(self):
        state = {"apps": {"chrome": {"name": "Google Chrome", "launch": ["/x/google-chrome.desktop"], "disabled": [],
                                     "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "C", "action": ""}]}}}
        b = self.bindings(state)
        self.assertEqual(b.classify(combo("SUPER + ALT + C"), "chrome", action="new-private-window"),
                         (shortcuts.RETARGET, ""))
        self.assertEqual(b.classify(combo("SUPER + ALT + C"), "chrome")[0], shortcuts.SAME)

    def test_render_lua_launches_desktop_actions(self):
        state = {"apps": {"chrome": {"name": "Google Chrome", "launch": ["/x/google-chrome.desktop"], "disabled": [],
                                     "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "I", "replaces": "",
                                                    "action": "new-private-window"}]}}}
        lua = shortcuts.render_lua(state)
        self.assertIn('{ launch = "/x/google-chrome.desktop:new-private-window" }', lua)
        self.assertEqual(shortcuts.shortcut_launch(["/a/b"], "x"), ["/a/b"])

    def test_prune_missing_drops_uninstalled_apps_only(self):
        tmp = Path(self.tmp.name)
        (tmp / "kept.desktop").write_text("")
        (tmp / "kept.AppImage").write_text("")
        entry = lambda launch: {"name": launch, "launch": [launch], "disabled": [],
                                "shortcuts": [{"mods": ["SUPER"], "key": "K"}]}
        state = {"apps": {k: entry(str(tmp / k)) for k in
                          ("kept.desktop", "gone.desktop", "kept.AppImage", "gone.AppImage")}}
        state["apps"]["unmounted"] = entry("/media/nothing-here/x.AppImage")
        removed = shortcuts.prune_missing(state)
        self.assertEqual(sorted(Path(e["name"]).name for e in removed), ["gone.AppImage", "gone.desktop"])
        self.assertEqual(sorted(state["apps"]), ["kept.AppImage", "kept.desktop", "unmounted"])

    def test_render_lua_quotes_names_and_paths(self):
        state = {"apps": {"x": {"name": 'Evil "name"\nhl.exec_cmd("rm")',
                                "launch": ["/tmp/a b/$(x).desktop"], "disabled": [],
                                "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "E", "replaces": ""}]}}}
        lua = shortcuts.render_lua(state)
        self.assertIn('hl.unbind("SUPER + ALT + E")', lua)
        self.assertIn('"Evil \\"name\\"\\nhl.exec_cmd(\\"rm\\")"', lua)
        self.assertIn("'/tmp/a b/$(x).desktop'", lua)
        self.assertNotIn('\nhl.exec_cmd("rm")', lua)

    def test_render_lua_comments_cannot_be_broken(self):
        payload = 'x\rhl.exec_cmd("a")\nb\r\nc\x0bd\x7f'
        state = {"apps": {"x": {"name": payload, "launch": ["/a.desktop"],
                                "disabled": [{"mods": ["SUPER"], "key": "F", "description": payload}],
                                "shortcuts": [{"mods": ["SUPER", "ALT"], "key": "E", "replaces": payload}]}}}
        lua = shortcuts.render_lua(state)
        for line in lua.splitlines():
            if line.startswith("--"):
                self.assertNotRegex(line, r"[\x00-\x1f\x7f]")
        # Lua ends a comment at CR or LF; no raw CR may survive anywhere in the file.
        self.assertNotIn("\r", lua)
        self.assertIn('-- x hl.exec_cmd("a") b  c d : disabled (was: x hl.exec_cmd("a") b  c d )', lua)

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
