"""Icons the theme misses (stale icon-theme.cache) are found on disk."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher_core  # noqa: E402


class IconFileTests(unittest.TestCase):
    def setUp(self):
        launcher_core._icon_files.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.home, self.system = Path(self.tmp.name, "home"), Path(self.tmp.name, "usr")

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, base, relative):
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_largest_png_in_the_users_dir_first(self):
        self.put(self.home, "icons/hicolor/48x48/apps/a.b.png")
        big = self.put(self.home, "icons/hicolor/128x128/apps/a.b.png")
        self.put(self.system, "icons/hicolor/scalable/apps/a.b.svg")
        self.assertEqual(launcher_core.icon_file_for("a.b", [self.home, self.system]), big)

    def test_scalable_and_pixmaps(self):
        svg = self.put(self.system, "icons/hicolor/scalable/apps/c.svg")
        pixmap = self.put(self.system, "pixmaps/d.xpm")
        dirs = [self.home, self.system]
        self.assertEqual(launcher_core.icon_file_for("c", dirs), svg)
        self.assertEqual(launcher_core.icon_file_for("d", dirs), pixmap)
        self.assertIsNone(launcher_core.icon_file_for("e", dirs))

    def test_no_paths_in_names(self):
        self.put(self.home, "icons/hicolor/48x48/apps/x.png")
        for name in ("../x", "a/b", "", ".hidden"):
            self.assertIsNone(launcher_core.icon_file_for(name, [self.home]))


if __name__ == "__main__":
    unittest.main()
