"""GNOME shortcuts: accelerators, who owns a key, writing custom keybindings
and giving taken keys back."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import shortcuts  # noqa: E402

BASE = shortcuts.CUSTOM_BASE


class FakeSettings:
    """gsettings in a dict: GNOME's bindings and the custom keybindings."""

    def __init__(self, strv=None, custom=None):
        self.strv = {k: list(v) for k, v in (strv or {}).items()}
        self.paths = list(custom or {})
        self.custom = {p: dict(v) for p, v in (custom or {}).items()}

    def available(self):
        return True

    def binding_keys(self):
        return [(*where.split(":", 1), where.split(":", 1)[1].replace("-", " "), [a for a in accels if a])
                for where, accels in self.strv.items()]

    def get_strv(self, schema_id, key):
        return list(self.strv.get(f"{schema_id}:{key}", []))

    def set_strv(self, schema_id, key, values):
        self.strv[f"{schema_id}:{key}"] = list(values)

    def custom_paths(self):
        return list(self.paths)

    def set_custom_paths(self, paths):
        self.paths = list(paths)

    def read_custom(self, path):
        return {"name": "", "command": "", "binding": "", **self.custom.get(path, {})}

    def write_custom(self, path, values):
        self.custom.setdefault(path, {}).update(values)

    def reset_custom(self, path):
        self.custom.pop(path, None)

    def sync(self):
        pass


SHELL = "org.gnome.shell.keybindings"
WM = "org.gnome.desktop.wm.keybindings"


def state(**apps):
    return {"apps": apps, "applied": []}


def entry(name, launch, *combos, disabled=()):
    return {"name": name, "launch": launch, "shortcuts": list(combos), "disabled": list(disabled)}


class AcceleratorTests(unittest.TestCase):
    def test_round_trip(self):
        combo = shortcuts.make_combo(["SHIFT", "SUPER"], "b")
        self.assertEqual(combo, {"mods": ["SUPER", "SHIFT"], "key": "B"})
        self.assertEqual(shortcuts.accelerator(combo), "<Super><Shift>b")
        self.assertEqual(shortcuts.parse_accelerator("<Super><Shift>b"), combo)
        self.assertEqual(shortcuts.combo_label(combo), "Super + Shift + B")

    def test_gnome_spellings(self):
        self.assertEqual(shortcuts.parse_accelerator("<Primary><Alt>F2"), {"mods": ["CTRL", "ALT"], "key": "F2"})
        self.assertEqual(shortcuts.parse_accelerator("<Ctrl><Shift><Alt>R")["mods"], ["SHIFT", "CTRL", "ALT"])
        self.assertEqual(shortcuts.parse_accelerator("XF86AudioPlay"), {"mods": [], "key": "XF86AudioPlay"})
        self.assertEqual(shortcuts.parse_accelerator("<Super>Page_Down")["key"], "Page_Down")
        for bad in ("", "<Hyper>a", "<Super>", "<Super>a b", "<Super>;rm"):
            self.assertIsNone(shortcuts.parse_accelerator(bad), bad)

    def test_same_key_however_written(self):
        a, b = shortcuts.parse_accelerator("<Control>t"), shortcuts.parse_accelerator("<Primary>T")
        self.assertEqual(shortcuts.combo_id(a), shortcuts.combo_id(b))


class BindingsTests(unittest.TestCase):
    def bindings(self, st=None, favorites=(), **kw):
        settings = FakeSettings(**kw)
        return shortcuts.Bindings(st or state(), shortcuts.read_desktop_binds(settings),
                                  shortcuts.read_custom_binds(settings), favorites, ["SUPER", "SHIFT"])

    def test_levels(self):
        combo = shortcuts.make_combo(["SUPER"], "A")
        b = self.bindings(strv={f"{SHELL}:toggle-application-view": ["<Super>a"]},
                          custom={BASE + "custom0/": {"name": "Notes", "command": "gnome-text-editor",
                                                      "binding": "<Super><Alt>n"}})
        self.assertEqual(b.classify(combo, "x"), (shortcuts.DESKTOP, "toggle application view"))
        self.assertEqual(b.taken_from(combo), f"{SHELL}:toggle-application-view")
        self.assertEqual(b.classify(shortcuts.make_combo(["SUPER", "ALT"], "N"), "x")[0], shortcuts.CUSTOM)
        self.assertEqual(b.classify(shortcuts.make_combo(["SUPER", "ALT"], "Q"), "x"), (shortcuts.FREE, ""))

    def test_launcher_owner_and_retarget(self):
        combo = {**shortcuts.make_combo(["SUPER", "SHIFT"], "F"), "action": "", "taken_from": "", "replaces": ""}
        st = state(**{"/a/firefox.desktop": entry("Firefox", ["/a/firefox.desktop"], combo)})
        b = self.bindings(st)
        self.assertEqual(b.classify(combo, "/a/firefox.desktop"), (shortcuts.SAME, "Firefox"))
        self.assertEqual(b.classify(combo, "/a/firefox.desktop", action="private")[0], shortcuts.RETARGET)
        self.assertEqual(b.classify(combo, "/b/other.desktop"), (shortcuts.LAUNCHER, "Firefox"))

    def test_launchers_own_keys_are_not_counted_twice(self):
        b = self.bindings(custom={BASE + "gnome-launcher-0/": {"binding": "<Super>x", "command": "gio launch /a"}})
        self.assertEqual(b.custom, [])

    def test_dash_position_shortcut_opens_a_favorite(self):
        app = {"name": "Files", "desktop_path": "/usr/share/applications/org.gnome.Nautilus.desktop", "exec": "nautilus"}
        b = self.bindings(favorites=["firefox.desktop", "org.gnome.Nautilus.desktop"],
                          strv={f"{SHELL}:open-new-window-application-2": ["<Super><Control>2"],
                                f"{SHELL}:switch-to-application-2": ["<Super>2"],
                                f"{SHELL}:switch-to-application-1": ["<Super>1"]})
        found = b.external_shortcuts(app)
        self.assertEqual([shortcuts.combo_label(x["combo"]) for x in found], ["Super + 2", "Super + Ctrl + 2"])
        self.assertEqual(b.classify(shortcuts.make_combo(["SUPER"], "2"), "k", app=app)[0], shortcuts.SAME)

    def test_custom_shortcut_that_opens_the_app(self):
        app = {"name": "Editor", "desktop_path": "/usr/share/applications/org.gnome.TextEditor.desktop",
               "exec": "gnome-text-editor %U"}
        b = self.bindings(custom={BASE + "c0/": {"name": "Edit", "command": "gnome-text-editor --new-window",
                                                 "binding": "<Super>e"},
                                  BASE + "c1/": {"name": "Shell", "command": "sh -c gnome-text-editor",
                                                 "binding": "<Super>w"}})
        self.assertEqual([x["description"] for x in b.external_shortcuts(app)], ["Edit"])

    def test_suggestions_skip_taken_letters(self):
        b = self.bindings(strv={f"{WM}:x": ["<Super><Shift>f"]})
        found = b.suggestions({"name": "Firefox"}, "k", ["SUPER", "ALT"], 3)
        self.assertNotIn("F", [c["key"] for c in found])
        self.assertEqual(len(found), 3)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "shortcuts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, settings, st):
        shortcuts.save_and_apply(st, settings, self.state_file)
        return shortcuts.load_state(self.state_file)

    def test_writes_own_keybindings_and_keeps_the_users(self):
        users = {BASE + "custom0/": {"name": "Mine", "command": "foo", "binding": "<Super>m"}}
        settings = FakeSettings(custom=users)
        combo = {**shortcuts.make_combo(["SUPER", "SHIFT"], "F"), "action": "", "taken_from": "", "replaces": ""}
        private = {**shortcuts.make_combo(["SUPER", "SHIFT"], "P"), "action": "new-private-window",
                   "label": "New Private Window", "taken_from": "", "replaces": ""}
        self.apply(settings, state(**{"/a/firefox.desktop": entry("Firefox", ["/a/firefox.desktop"], combo, private)}))
        own = [p for p in settings.paths if "gnome-launcher-" in p]
        self.assertEqual(settings.paths[0], BASE + "custom0/")
        self.assertEqual(len(own), 2)
        written = {settings.custom[p]["binding"]: settings.custom[p] for p in own}
        self.assertTrue(written["<Super><Shift>f"]["command"].endswith("launch_entry.py /a/firefox.desktop"))
        self.assertIn("launch_entry.py /a/firefox.desktop new-private-window", written["<Super><Shift>p"]["command"])
        self.assertEqual(written["<Super><Shift>p"]["name"], "Firefox: New Private Window")
        # Removing every shortcut removes the launcher's keybindings, not the user's.
        self.apply(settings, state())
        self.assertEqual(settings.paths, [BASE + "custom0/"])
        self.assertEqual(set(settings.custom), {BASE + "custom0/"})

    def test_taken_keys_go_back_when_the_shortcut_goes(self):
        settings = FakeSettings(strv={f"{SHELL}:switch-to-application-1": ["<Super>1"],
                                      f"{WM}:switch-applications": ["<Super>Tab", "<Alt>Tab"]})
        one = {**shortcuts.make_combo(["SUPER"], "1"), "action": "", "replaces": "x",
               "taken_from": f"{SHELL}:switch-to-application-1"}
        tab = {**shortcuts.make_combo(["SUPER"], "Tab"), "action": "", "replaces": "x",
               "taken_from": f"{WM}:switch-applications"}
        saved = self.apply(settings, state(a=entry("A", ["/a.desktop"], one, tab)))
        self.assertEqual(settings.strv[f"{SHELL}:switch-to-application-1"], [])
        self.assertEqual(settings.strv[f"{WM}:switch-applications"], ["<Alt>Tab"])
        self.assertEqual(len(saved["applied"]), 2)
        # The snapshot no longer sees GNOME owning the taken keys.
        bindings = shortcuts.Bindings(saved, shortcuts.read_desktop_binds(settings), [], [], ["SUPER"])
        self.assertEqual(bindings.owner(one)[0], shortcuts.FREE)
        saved = self.apply(settings, state(a=entry("A", ["/a.desktop"], tab)))
        self.assertEqual(settings.strv[f"{SHELL}:switch-to-application-1"], ["<Super>1"])
        self.assertEqual(saved["applied"], [{"where": f"{WM}:switch-applications", "accel": "<Super>Tab"}])
        self.apply(settings, state())
        self.assertEqual(settings.strv[f"{WM}:switch-applications"], ["<Alt>Tab", "<Super>Tab"])

    def test_turning_off_a_custom_shortcut_and_back_on(self):
        path = BASE + "custom3/"
        settings = FakeSettings(custom={path: {"name": "Term", "command": "ptyxis", "binding": "<Super>Return"}})
        off = {**shortcuts.make_combo(["SUPER"], "Return"), "description": "Term", "source": "Custom",
               "where": "custom:" + path}
        self.apply(settings, state(t=entry("Terminal", ["/t.desktop"], disabled=[off])))
        self.assertEqual(settings.custom[path]["binding"], "")
        self.apply(settings, state())
        self.assertEqual(settings.custom[path]["binding"], "<Super>Return")

    def test_a_key_the_user_rebound_meanwhile_is_not_overwritten(self):
        settings = FakeSettings(strv={f"{SHELL}:toggle-quick-settings": ["<Super>s"]})
        combo = {**shortcuts.make_combo(["SUPER"], "S"), "action": "", "replaces": "x",
                 "taken_from": f"{SHELL}:toggle-quick-settings"}
        self.apply(settings, state(a=entry("A", ["/a.desktop"], combo)))
        settings.strv[f"{SHELL}:toggle-quick-settings"] = ["<Super>q"]  # Changed in GNOME Settings.
        self.apply(settings, state())
        self.assertEqual(settings.strv[f"{SHELL}:toggle-quick-settings"], ["<Super>q", "<Super>s"])

    def test_hostile_state_file_is_cleaned(self):
        self.state_file.write_text(json.dumps({
            "apps": {"x": {"name": "X", "launch": ["/x.desktop"],
                           "shortcuts": [{"mods": ["SUPER"], "key": "x;rm", "action": ""},
                                         {"mods": ["SUPER"], "key": "y", "action": "../evil",
                                          "taken_from": "/etc/passwd"}],
                           "disabled": [{"mods": ["SUPER"], "key": "z", "where": "custom:/elsewhere/"}]}},
            "applied": [{"where": "org.gnome.shell.keybindings:x", "accel": "<Super>"}]}))
        loaded = shortcuts.load_state(self.state_file)
        combo = loaded["apps"]["x"]["shortcuts"]
        self.assertEqual([(c["key"], c["action"], c["taken_from"]) for c in combo], [("Y", "", "")])
        self.assertEqual(loaded["apps"]["x"]["disabled"], [])
        self.assertEqual(loaded["applied"], [])


class CommandTests(unittest.TestCase):
    def test_commands_glib_can_split(self):
        self.assertTrue(shortcuts.shortcut_command(["/usr/share/applications/My App.desktop"]).endswith(
            "launch_entry.py '/usr/share/applications/My App.desktop'"))
        self.assertEqual(shortcuts.shortcut_command(["/home/u/AppImages/x.AppImage", "--flag"]),
                         "/home/u/AppImages/x.AppImage --flag")


@unittest.skipUnless(subprocess.run(["gsettings", "list-relocatable-schemas"], capture_output=True, text=True)
                     .stdout.count(shortcuts.CUSTOM_SCHEMA), "GNOME's media-keys schema is not installed")
class RealSchemaTests(unittest.TestCase):
    """The real Gio backend against GNOME's installed schemas, in memory
    (GSETTINGS_BACKEND=memory): nothing reaches the user's settings."""

    def test_round_trip_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(f"""
                import sys; sys.path.insert(0, {str(ROOT)!r})
                from pathlib import Path
                import shortcuts
                g = shortcuts.GSettings()
                assert g.available()
                combo = {{**shortcuts.make_combo(["SUPER"], "1"), "action": "", "replaces": "x",
                          "taken_from": "org.gnome.shell.keybindings:switch-to-application-1"}}
                st = {{"apps": {{"a": {{"name": "A", "launch": ["/a.desktop"], "shortcuts": [combo], "disabled": []}}}},
                      "applied": []}}
                shortcuts.save_and_apply(st, g, Path({tmp!r}) / "s.json")
                assert g.get_strv("org.gnome.shell.keybindings", "switch-to-application-1") == [], "taken"
                own = [p for p in g.custom_paths() if "gnome-launcher-" in p]
                assert g.read_custom(own[0])["binding"] == "<Super>1"
                assert any(b["accel"] == "<Super>a" for b in shortcuts.read_desktop_binds(g))
                shortcuts.save_and_apply({{"apps": {{}}, "applied": []}}, g, Path({tmp!r}) / "s.json")
                assert g.get_strv("org.gnome.shell.keybindings", "switch-to-application-1") == ["<Super>1"]
                assert not [p for p in g.custom_paths() if "gnome-launcher-" in p]
                print("ok")
            """)
            env = {**os.environ, "GSETTINGS_BACKEND": "memory"}
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.stdout.strip(), "ok", result.stderr)



class OutdatedCommandTests(unittest.TestCase):
    def test_old_gio_launch_shortcuts_are_found(self):
        old = FakeSettings(custom={BASE + "gnome-launcher-0/": {"binding": "<Super>x", "command": "gio launch /a.desktop",
                                                                 "name": "A"}})
        self.assertTrue(shortcuts.outdated_commands(old))
        user = FakeSettings(custom={BASE + "custom0/": {"binding": "<Super>y", "command": "gio launch /b.desktop",
                                                        "name": "mine"}})
        self.assertFalse(shortcuts.outdated_commands(user))      # the user's own shortcuts are left alone


class LaunchEntryTests(unittest.TestCase):
    def test_plain_start_reads_bounded_and_never_blocks_on_a_fifo(self):
        import os
        import tempfile
        import time
        import launch_entry
        with tempfile.TemporaryDirectory() as folder:
            fifo = os.path.join(folder, "x.desktop")
            os.mkfifo(fifo)
            started = time.monotonic()
            self.assertEqual(launch_entry.main(["launch_entry.py", fifo]), 1)
            self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
