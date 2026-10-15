"""Groups: suggestions from desktop categories, edits, and a hostile groups.json."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import i18n  # noqa: E402
import groups  # noqa: E402


def app(desktop_id, categories=(), **extra):
    return {"desktop_id": desktop_id, "name": desktop_id, "categories": list(categories), **extra}


class GroupTests(unittest.TestCase):
    def setUp(self):
        i18n._lang = "en"
        self.all = groups.default_data()
        self.data = self.all["apps"]

    def tearDown(self):
        i18n._lang = None

    def test_suggestions_follow_categories_first_match_wins(self):
        self.assertEqual(groups.suggested_group(app("code", ["Utility", "TextEditor", "Development"])), "development")
        self.assertEqual(groups.suggested_group(app("vlc", ["AudioVideo", "Player"])), "media")
        self.assertEqual(groups.suggested_group(app("chromium", ["Network", "WebBrowser"])), "internet")
        self.assertEqual(groups.suggested_group(app("youtube", pwa=True)), "internet")
        self.assertEqual(groups.suggested_group(app("plugin:x", plugin_id="x", category="Network")), "network")
        self.assertEqual(groups.suggested_group(app("plugin:y", plugin_id="y", category="Compositor")), "system")
        self.assertEqual(groups.suggested_group(app("plugin:z", plugin_id="z")), groups.OTHER)
        self.assertEqual(groups.suggested_group(app("odd")), groups.OTHER)

    def test_move_by_hand_wins_and_deleting_the_group_moves_it_back(self):
        vlc = app("vlc", ["AudioVideo"])
        groups.assign(self.data, "vlc", "office")
        self.assertEqual(groups.group_of(vlc, self.data), "office")
        groups.delete_group(self.data, "office")
        self.assertEqual(groups.group_of(vlc, self.data), "media")
        groups.delete_group(self.data, "media")
        self.assertEqual(groups.group_of(vlc, self.data), groups.OTHER)

    def test_add_rename_and_duplicate_names(self):
        gid = groups.add_group(self.data, "  Work \n stuff ")
        self.assertEqual(groups.display_name(gid, self.data), "Work stuff")
        with self.assertRaises(ValueError):
            groups.add_group(self.data, "work STUFF")
        with self.assertRaises(ValueError):
            groups.add_group(self.data, "Office")      # a built-in name
        with self.assertRaises(ValueError):
            groups.add_group(self.data, "\x00‮  ")  # nothing left
        groups.rename_group(self.data, "office", "Büro")
        self.assertEqual(groups.display_name("office", self.data), "Büro")
        self.assertEqual(len(groups.clean_name("x" * 500)), groups.MAX_NAME)

    def test_move_and_sections_order(self):
        self.assertFalse(groups.move_group(self.data, "office", -1))
        self.assertTrue(groups.move_group(self.data, "internet", -1))
        entries = [app("vlc", ["AudioVideo"]), app("odd"), app("writer", ["Office"])]
        sections = groups.sections(entries, self.data, running=lambda a: a["desktop_id"] == "writer")
        self.assertEqual(sections[0], (groups.RUNNING, [entries[2]]))
        # Running and in its own group too; tray icons only in Running.
        self.assertIn(entries[2], dict(sections)["office"])
        tray_item = {"desktop_id": "nordvpn", "name": "NordVPN", "tray_id": "nordvpn"}
        with_tray = groups.sections([tray_item], self.data, running=lambda a: "tray_id" in a)
        self.assertEqual(with_tray[0], (groups.RUNNING, [tray_item]))
        self.assertTrue(all(tray_item not in members for _g, members in with_tray[1:]))
        self.assertEqual([gid for gid, _m in sections[1:3]], ["internet", "office"])
        self.assertEqual(sections[-1], (groups.OTHER, [entries[1]]))

    def test_reorder_moves_a_group_to_the_target_place(self):
        ids = lambda: [g["id"] for g in self.data["groups"]]
        start = ids()
        self.assertTrue(groups.reorder_group(self.data, start[0], start[2]))           # before
        self.assertEqual(ids(), [start[1], start[0], start[2]] + start[3:])
        self.assertTrue(groups.reorder_group(self.data, start[0], start[2], after=True))
        self.assertEqual(ids(), [start[1], start[2], start[0]] + start[3:])
        self.assertFalse(groups.reorder_group(self.data, start[0], start[2], after=True))  # already there
        self.assertTrue(groups.reorder_group(self.data, start[0], start[1]))
        self.assertEqual(ids(), start)
        self.assertFalse(groups.reorder_group(self.data, start[0], start[0]))
        self.assertFalse(groups.reorder_group(self.data, start[0], groups.OTHER))

    def test_place_orders_entries_inside_their_group(self):
        apps = [{"desktop_id": i, "name": i, "categories": ["AudioVideo"]} for i in ("a", "b", "c")]
        media = lambda: [a["desktop_id"] for a in dict(groups.sections(apps, self.data))["media"]]
        self.assertEqual(media(), ["a", "b", "c"])
        groups.place(self.data, "c", "media", "a", media())
        self.assertEqual(media(), ["c", "a", "b"])
        groups.place(self.data, "c", "media", "b", media(), after=True)
        self.assertEqual(media(), ["a", "b", "c"])
        groups.place(self.data, "a", "office", None, [])
        self.assertEqual(media(), ["b", "c"])
        self.assertEqual(dict(groups.sections(apps, self.data))["office"][0]["desktop_id"], "a")

    def test_hand_order_survives_saving_and_bad_entries_are_dropped(self):
        groups.place(self.data, "x", "media", None, ["y"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.json"
            groups.save({"apps": self.data, "plugins": groups.default_page("plugins")}, path)
            self.assertEqual(groups.load(path)["apps"]["order"], ["y", "x"])
            path.write_text('{"apps": {"groups": [], "order": ["a", 5, "a", ""]}}')
            self.assertEqual(groups.load(path)["apps"]["order"], ["a"])

    def test_hostile_file_is_cleaned_entry_by_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.json"
            path.write_text(json.dumps({"plugins": "junk", "apps": {
                "groups": [{"id": "../x", "name": "a"}, {"id": "g1", "name": "<b>Bold</b>‮"},
                           {"id": "g1", "name": "dupe"}, {"id": "office", "name": 5}, "junk",
                           {"id": "g2", "name": ""}],
                "members": {"vlc": "g1", "bad": "nope", "x" * 400: "g1"},
                "collapsed": ["g1", "gone", 3]}}))
            loaded = groups.load(path)
            self.assertEqual(loaded["plugins"], groups.default_page("plugins"))
            data = loaded["apps"]
            self.assertEqual([g["id"] for g in data["groups"]], ["g1", "office"])
            self.assertEqual(data["groups"][0]["name"], "<b>Bold</b>")   # shown as plain text
            self.assertEqual(data["members"], {"vlc": "g1"})
            self.assertEqual(data["collapsed"], ["g1"])
            path.write_bytes(b"{" * (groups.MAX_FILE_BYTES + 10))
            self.assertEqual(groups.load(path), groups.default_data())
            path.write_text("not json")
            self.assertEqual(groups.load(path), groups.default_data())

    def test_save_is_private_and_replaces_a_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "victim"
            target.write_text("keep")
            path = Path(tmp) / "groups.json"
            path.symlink_to(target)
            groups.save(self.all, path)
            self.assertEqual(target.read_text(), "keep")
            self.assertFalse(path.is_symlink())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(groups.load(path), self.all)


if __name__ == "__main__":
    unittest.main()
