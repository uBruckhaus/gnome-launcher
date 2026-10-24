"""Update checks: parsing tool output, the mapping to entries, Flatpak updates."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import updates  # noqa: E402



class ParseTests(unittest.TestCase):
    def test_update_lines(self):
        text = "firefox 140.0-1 -> 141.0-1\nnot a line\nyay-bin 12.5-1 -> 12.6-1\n"
        self.assertEqual(updates.parse_update_lines(text),
                         {"firefox": ("140.0-1", "141.0-1"), "yay-bin": ("12.5-1", "12.6-1")})
        self.assertEqual(updates.parse_update_lines(None), {})

    def test_owner_lines(self):
        text = ("/usr/share/applications/firefox.desktop is owned by firefox 140.0-1\n"
                "error: No package owns /usr/share/applications/x.desktop\n")
        self.assertEqual(updates.parse_owner_lines(text),
                         {"/usr/share/applications/firefox.desktop": "firefox"})

    def test_only_system_desktop_files_are_asked_about(self):
        with mock.patch.object(updates, "read_bounded_output", return_value="") as run:
            updates.desktop_packages(["/home/u/.local/share/applications/a.desktop", "/usr/x.txt"])
        run.assert_not_called()

    def test_check_maps_desktop_files_to_package_updates(self):
        with mock.patch.object(updates, "desktop_packages", return_value={"/usr/a.desktop": "a", "/usr/b.desktop": "b"}), \
                mock.patch.object(updates, "package_updates", return_value={"a": ("1", "2")}), \
                mock.patch.object(updates, "flatpak_updates", return_value={"org.x.Y": ("1", "2")}):
            self.assertEqual(updates.check([]), {"apps": {"/usr/a.desktop": ("a", "1", "2")},
                                                 "flatpaks": {"org.x.Y": ("1", "2")}})

    def test_flatpak_lines(self):
        text = "org.gnome.Maps\t49.1\ncom.example.NoVersion\t\nnot a line\n"
        self.assertEqual(updates.parse_flatpak_lines(text),
                         {"org.gnome.Maps": "49.1", "com.example.NoVersion": ""})

    def test_flatpak_updates_pair_installed_and_available(self):
        outputs = {"remote-ls": "org.gnome.Maps\t49.2\n", "list": "org.gnome.Maps\t49.1\nother.App\t1\n"}
        with mock.patch.object(updates.shutil, "which", return_value="/usr/bin/flatpak"), \
                mock.patch.object(updates, "read_bounded_output", side_effect=lambda argv, **_k: outputs[argv[1]]):
            self.assertEqual(updates.flatpak_updates(), {"org.gnome.Maps": ("49.1", "49.2")})


class RunUpdateTests(unittest.TestCase):
    def test_success_and_failure_message(self):
        ok = mock.Mock(return_value=(0, "Updated x."))
        self.assertEqual(updates.run_flatpak_update("org.gnome.Maps", ok), (True, ""))
        self.assertEqual(ok.call_args[0][0], ["flatpak", "update", "-y", "--noninteractive", "org.gnome.Maps"])
        bad = mock.Mock(return_value=(1, "Updating…\nerror: no remote\n"))
        self.assertEqual(updates.run_flatpak_update("org.gnome.Maps", bad), (False, "error: no remote"))
        hung = mock.Mock(return_value=(None, ""))
        self.assertEqual(updates.run_flatpak_update("org.gnome.Maps", hung), (False, "timed out or too much output"))

    def test_bounded_runner_stops_floods_and_slow_commands(self):
        code, out = updates.run_bounded(["sh", "-c", "yes x"], 1000, 5)
        self.assertIsNone(code)
        self.assertLessEqual(len(out), 1000)
        code, _out = updates.run_bounded(["sleep", "5"], 1000, 0.5)
        self.assertIsNone(code)
        self.assertEqual(updates.run_bounded(["sh", "-c", "echo hi; exit 3"], 1000, 5), (3, "hi\n"))

    def test_bad_ids_never_run(self):
        run = mock.Mock()
        for bad in ("", "../x", "-rf", "a b", "a..b"):
            self.assertFalse(updates.run_flatpak_update(bad, run)[0])
        run.assert_not_called()



if __name__ == "__main__":
    unittest.main()
