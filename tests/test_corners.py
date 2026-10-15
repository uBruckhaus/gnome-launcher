"""The Corners setting: Hyprland's rounding, the choices and where it is set."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import corners  # noqa: E402


class CornersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_hyprctl(self):
        self.assertEqual(corners.parse_rounding('{"option": "decoration:rounding", "int": 8, "set": true}'), 8)
        self.assertEqual(corners.parse_rounding('{"int": 400}'), corners.MAX_RADIUS)
        self.assertEqual(corners.parse_rounding('{"int": -3}'), 0)
        for bad in ("", "not json", '{"value": 8}', '{"int": "8"}', '{"int": true}', "[1]"):
            self.assertIsNone(corners.parse_rounding(bad), bad)

    def test_radius_per_choice(self):
        self.assertEqual(corners.radius("windows", 8), 8)
        self.assertEqual(corners.radius("windows", None), 0)
        self.assertEqual(corners.radius("rounded", 0), corners.ROUNDED)
        self.assertEqual(corners.radius("square", 8), 0)
        self.assertEqual(corners.radius("bogus", 6), 6)  # Unknown values read as the default.

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return path

    def test_source_user_wins_over_theme(self):
        theme = self.write("theme.lua", "hl.config({ decoration = { rounding = 0 } })\n")
        user = self.write("user.lua", "hl.config({\n  decoration = {\n    rounding = 8,\n  },\n})\n")
        self.assertEqual(corners.rounding_source(theme, user), "user")

    def test_commented_example_does_not_count(self):
        theme = self.write("theme.lua", "local x = 1\n")
        user = self.write("user.lua", "-- hl.config({\n--   decoration = {\n--     rounding = 8,\n")
        self.assertEqual(corners.rounding_source(theme, user), "default")
        theme = self.write("theme2.lua", "decoration = { rounding = 4, -- round\n gradient_rounding = 2 }\n")
        self.assertEqual(corners.rounding_source(theme, user), "theme")
        self.assertEqual(corners.lua_rounding(theme), 4)

    def test_other_rounding_keys_do_not_count(self):
        theme = self.write("t.lua", "group = { groupbar = { gradient_rounding = 3, rounding_power = 2 } }\n")
        self.assertIsNone(corners.lua_rounding(theme))

    def test_missing_symlinked_or_huge_files(self):
        self.assertIsNone(corners.lua_rounding(self.dir / "missing.lua"))
        real = self.write("real.lua", "rounding = 5\n")
        link = self.dir / "link.lua"
        link.symlink_to(real)
        self.assertIsNone(corners.lua_rounding(link))
        huge = self.write("huge.lua", "rounding = 5\n" + "-" * (corners.MAX_LUA_BYTES + 10))
        self.assertIsNone(corners.lua_rounding(huge))


if __name__ == "__main__":
    unittest.main()
