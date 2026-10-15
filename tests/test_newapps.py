"""Newly installed entries: the first look is the baseline, later ones are new for a while."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import newapps  # noqa: E402

DAY = 86400


class NewAppsTests(unittest.TestCase):
    def test_first_look_is_old_later_installs_are_new(self):
        data = newapps.empty()
        self.assertTrue(newapps.note(data, ["a", "b"], "apps", now=1000))
        self.assertFalse(newapps.is_new(data, "a", now=1000))
        self.assertTrue(newapps.note(data, ["a", "b", "c"], "apps", now=2000))
        self.assertTrue(newapps.is_new(data, "c", now=2000))
        self.assertFalse(newapps.note(data, ["a", "b", "c"], "apps", now=3000))

    def test_plugins_have_their_own_first_look(self):
        data = newapps.empty()
        newapps.note(data, ["a"], "apps", now=1000)
        newapps.note(data, ["plugin:x"], "plugins", now=5000)
        self.assertFalse(newapps.is_new(data, "plugin:x", now=5000))

    def test_new_ends_after_a_week_or_when_opened(self):
        data = newapps.empty()
        newapps.note(data, [], "apps", now=1000)
        newapps.note(data, ["c", "d"], "apps", now=2000)
        self.assertFalse(newapps.is_new(data, "c", now=2000 + newapps.NEW_DAYS * DAY))
        self.assertTrue(newapps.opened(data, "d"))
        self.assertFalse(newapps.is_new(data, "d", now=2001))
        self.assertFalse(newapps.opened(data, "d"))

    def test_late_entries_are_dated_by_their_install(self):
        data = newapps.empty()
        newapps.note(data, ["a"], "apps", now=100 * DAY)
        newapps.note(data, ["old", "fresh"], "apps", now=100 * DAY,
                     installed={"old": 80 * DAY, "fresh": 99 * DAY})
        self.assertFalse(newapps.is_new(data, "old", now=100 * DAY))
        self.assertTrue(newapps.is_new(data, "fresh", now=100 * DAY))
        self.assertFalse(newapps.is_new(data, "fresh", now=99 * DAY + newapps.NEW_DAYS * DAY))

    def test_saved_privately_and_bad_entries_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            data = newapps.empty()
            newapps.note(data, ["a"], "apps", now=1)
            newapps.save(data, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(newapps.load(path), data)
            path.write_text('{"baseline": ["apps", "x"], "seen": {"a": 5, "b": "x", "c": -1, "d": true}}')
            self.assertEqual(newapps.load(path), {"baseline": ["apps"], "seen": {"a": 5}})
            path.write_text("[]")
            self.assertEqual(newapps.load(path), newapps.empty())


if __name__ == "__main__":
    unittest.main()
