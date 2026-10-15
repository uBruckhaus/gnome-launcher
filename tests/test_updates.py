"""Update checks: parsing tool output, a checkout behind its upstream, the mapping to entries."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import updates  # noqa: E402


def git(directory, *args):
    subprocess.run(["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


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
                mock.patch.object(updates, "plugin_updates", return_value={"p": 3}):
            self.assertEqual(updates.check([], []), {"apps": {"/usr/a.desktop": ("a", "1", "2")}, "plugins": {"p": 3}})


class RunUpdateTests(unittest.TestCase):
    def test_success_and_failure_message(self):
        ok = mock.Mock(return_value=(0, "Updated x."))
        self.assertEqual(updates.run_plugin_update("me.plugin", ok), (True, ""))
        self.assertEqual(ok.call_args[0][0], ["omarchy", "plugin", "update", "me.plugin", "--yes"])
        bad = mock.Mock(return_value=(1, "omarchy-plugin-update: cannot fast-forward 'me.plugin'; "
                                         "you have local changes in /x\n"))
        self.assertEqual(updates.run_plugin_update("me.plugin", bad),
                         (False, "cannot fast-forward 'me.plugin'; you have local changes in /x"))
        hung = mock.Mock(return_value=(None, ""))
        self.assertEqual(updates.run_plugin_update("me.plugin", hung), (False, "timed out or too much output"))

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
            self.assertFalse(updates.run_plugin_update(bad, run)[0])
        run.assert_not_called()


class GitTests(unittest.TestCase):
    def test_checkout_behind_its_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, plugins = Path(tmp) / "origin", Path(tmp) / "plugins"
            origin.mkdir()
            git(origin, "init", "-q", "-b", "main")
            (origin / "a").write_text("1")
            git(origin, "add", "a")
            git(origin, "commit", "-q", "-m", "one")
            plugins.mkdir()
            subprocess.run(["git", "clone", "-q", str(origin), str(plugins / "me.plugin")], check=True,
                           capture_output=True)
            with mock.patch.object(updates, "USER_PLUGINS_DIR", plugins):
                self.assertEqual(updates.plugin_updates(["me.plugin", "../x", "missing"]), {})
                (origin / "a").write_text("2")
                git(origin, "commit", "-q", "-am", "two")
                self.assertEqual(updates.plugin_updates(["me.plugin"], fetch=False), {})
                self.assertEqual(updates.plugin_updates(["me.plugin"]), {"me.plugin": 1})


if __name__ == "__main__":
    unittest.main()
