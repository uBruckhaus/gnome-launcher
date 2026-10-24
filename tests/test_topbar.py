"""The top bar icon setting: GNOME Shell's extension lists and the link."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["GSETTINGS_BACKEND"] = "memory"  # Never the real dconf.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop  # noqa: E402
import topbar  # noqa: E402


@unittest.skipIf(desktop.shell_settings() is None, "GNOME Shell's schema is not installed")
class TopbarTests(unittest.TestCase):
    def setUp(self):
        self.settings = desktop.shell_settings()
        self.settings.set_strv("enabled-extensions", ["other@example.org"])
        self.settings.set_strv("disabled-extensions", [topbar.UUID])

    def test_off_by_default(self):
        self.settings.reset("enabled-extensions")
        self.assertFalse(topbar.enabled(self.settings))

    def test_switch_on_and_off(self):
        self.assertTrue(topbar.set_enabled(True, self.settings))
        self.assertTrue(topbar.enabled(self.settings))
        self.assertEqual(list(self.settings.get_strv("enabled-extensions")), ["other@example.org", topbar.UUID])
        self.assertEqual(list(self.settings.get_strv("disabled-extensions")), [])
        topbar.set_enabled(True, self.settings)  # Twice: still listed once.
        self.assertEqual(list(self.settings.get_strv("enabled-extensions")).count(topbar.UUID), 1)
        topbar.set_enabled(False, self.settings)
        self.assertFalse(topbar.enabled(self.settings))
        self.assertEqual(list(self.settings.get_strv("enabled-extensions")), ["other@example.org"])

    def test_install_links_the_extension(self):
        with tempfile.TemporaryDirectory() as data:
            old = os.environ.get("XDG_DATA_HOME")
            os.environ["XDG_DATA_HOME"] = data
            try:
                topbar.install()
                topbar.install()  # Again: nothing to do.
                target = topbar.target_dir()
                self.assertTrue(target.is_symlink())
                self.assertEqual(target.resolve(), topbar.SOURCE)
                self.assertTrue((target / "metadata.json").is_file())
            finally:
                if old is None:
                    del os.environ["XDG_DATA_HOME"]
                else:
                    os.environ["XDG_DATA_HOME"] = old


if __name__ == "__main__":
    unittest.main()
