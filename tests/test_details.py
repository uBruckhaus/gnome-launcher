"""Details page facts: pacman database parsing, plugin folders, bounded walks."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import details  # noqa: E402


DESC = """%NAME%
foot

%VERSION%
1.20.2-1

%URL%
https://codeberg.org/dnkl/foot

%BUILDDATE%
1700000000

%INSTALLDATE%
1760000000

%SIZE%
1234567

"""


class DetailsTests(unittest.TestCase):
    def test_package_info_reads_the_local_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)
            (db / "foot-1.20.2-1").mkdir()
            (db / "foot-1.20.2-1" / "desc").write_text(DESC)
            (db / "foot-terminfo-1.20.2-1").mkdir()
            (db / "foot-terminfo-1.20.2-1" / "desc").write_text(DESC.replace("foot\n\n%VERSION", "foot-terminfo\n\n%VERSION"))
            info = details.package_info("foot", db)
            self.assertEqual(info["version"], "1.20.2-1")
            self.assertEqual(info["size"], 1234567)
            self.assertEqual(info["installed"], 1760000000)
            self.assertIsNone(details.package_info("../etc", db))
            self.assertIsNone(details.package_info("$(touch x)", db))

    def test_package_owner_needs_an_absolute_path_and_a_clean_name(self):
        with mock.patch.object(details.shutil, "which", return_value="/usr/bin/pacman"), \
             mock.patch.object(details, "read_bounded_output") as run:
            self.assertIsNone(details.package_owner("-Rns"))
            self.assertIsNone(details.package_owner("relative/path"))
            run.assert_not_called()
            run.return_value = "foot\n"
            self.assertEqual(details.package_owner("/usr/bin/foot"), "foot")
            self.assertEqual(run.call_args[0][0], ["pacman", "-Qqo", "/usr/bin/foot"])
            run.return_value = "evil name; rm\n"
            self.assertIsNone(details.package_owner("/usr/bin/foot"))

    def test_program_path_skips_env_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            program = Path(tmp) / "tool"
            program.write_text("#!/bin/sh\n")
            program.chmod(0o755)
            self.assertEqual(details.program_path({"exec": f"env FOO=1 {program} --flag"}), str(program))
            self.assertIsNone(details.program_path({"exec": "definitely-not-a-program-xyz"}))
            self.assertIsNone(details.program_path({"exec": "'unclosed"}))

    def test_web_apps_and_terminal_apps(self):
        youtube = {"exec": "omarchy-launch-webapp https://youtube.com/", "desktop_id": "YouTube"}
        self.assertEqual(details.web_address(youtube), "https://youtube.com/")
        facts = details.app_facts(youtube)
        self.assertEqual((facts["kind"], facts["website"]), ("webapp", "https://youtube.com/"))
        self.assertNotIn("package", facts)
        self.assertIsNone(details.web_address({"exec": "xdg-open file:///etc/passwd"}))
        self.assertEqual(details.terminal_program(["foot", "--app-id=x", "-e", "/usr/bin/btop"]), "/usr/bin/btop")
        self.assertIsNone(details.terminal_program(["xdg-terminal-exec", "-e", "bash", "-c", "dua i /"]))
        self.assertIsNone(details.terminal_program(["chromium", "-e", "x"]))

    def test_folder_size_does_not_follow_links_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").write_bytes(b"x" * 100)
            (root / "big").mkdir()
            (root / "loop").symlink_to(root)
            size, complete = details.folder_size(root)
            self.assertTrue(complete)
            self.assertLess(size, 1000)
            for i in range(30):
                (root / "big" / str(i)).write_text("y")
            self.assertEqual(details.folder_size(root, limit=10)[1], False)

    def test_git_source_reads_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = Path(tmp) / ".git"
            (git / "refs/heads").mkdir(parents=True)
            (git / "config").write_text('[remote "origin"]\n\turl = https://github.com/a/b.git\n')
            (git / "HEAD").write_text("ref: refs/heads/main\n")
            (git / "refs/heads/main").write_text("0123456789abcdef0123456789abcdef01234567\n")
            (git / "description").write_text("x")
            source = details.git_source(tmp)
            self.assertEqual(source["url"], "https://github.com/a/b.git")
            self.assertEqual(source["commit"], "0123456")
            (git / "HEAD").write_text("ref: refs/../../etc/passwd\n")
            self.assertIsNone(details.git_source(tmp)["commit"])


class OmarchyDefaultTests(unittest.TestCase):
    def test_origin_and_default_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config = Path(tmp) / "omarchy", Path(tmp) / "config"
            (root / "install").mkdir(parents=True)
            (root / "applications").mkdir()
            (root / "default/xdg-terminal-exec").mkdir(parents=True)
            (root / "install/omarchy-base.packages").write_text("# list\nchromium\nfoot\n")
            (root / "install/omarchy-other.packages").write_text("kdenlive\n")
            (root / "applications/HEY.desktop").write_text("")
            (root / "default/xdg-terminal-exec/hyprland-xdg-terminals.list").write_text("# x\nfoot.desktop\n")
            config.mkdir()
            (config / "mimeapps.list").write_text("[Added Associations]\ntext/plain=evil.desktop\n"
                                                  "[Default Applications]\nx-scheme-handler/http=google-chrome.desktop;chromium.desktop\n"
                                                  "text/plain=micro.desktop\n")
            o = lambda app, pkg=None: details.omarchy_origin(app, pkg, root)
            self.assertEqual(o({"desktop_path": "/x/HEY.desktop"}), "bundled")
            self.assertEqual(o({"desktop_path": "/x/chromium.desktop"}, "chromium"), "installed")
            self.assertEqual(o({"desktop_path": "/x/kdenlive.desktop"}, "kdenlive"), "optional")
            self.assertIsNone(o({"desktop_path": "/x/google-chrome.desktop"}, "google-chrome"))
            self.assertEqual(o({"plugin_dir": str(root / "shell/plugins/x")}), "builtin")
            roles = lambda name: details.default_roles({"desktop_path": "/x/" + name}, config, root)
            self.assertEqual(roles("google-chrome.desktop"), ["browser"])
            self.assertEqual(roles("chromium.desktop"), [])   # second choice only
            self.assertEqual(roles("micro.desktop"), ["editor"])
            self.assertEqual(roles("evil.desktop"), [])       # an association, not a default
            self.assertEqual(roles("foot.desktop"), ["terminal"])
            (config / "xdg-terminals.list").write_text("kitty.desktop\n")
            self.assertEqual(roles("foot.desktop"), [])

    def test_origin_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "applications").mkdir()
            (root / "applications/HEY.desktop").write_text("")
            index = (frozenset({"chromium.desktop"}), frozenset({"/usr/bin/foot"}))
            defaults = {"google-chrome.desktop", "foot.desktop"}
            section = lambda app: details.origin_section(app, index, defaults, root)
            self.assertEqual(section({"desktop_path": "/a/google-chrome.desktop"}), "defaults")
            self.assertEqual(section({"desktop_path": "/a/foot.desktop"}), "defaults")   # default wins
            self.assertEqual(section({"desktop_path": "/a/chromium.desktop"}), "omarchy")
            self.assertEqual(section({"desktop_path": "/a/HEY.desktop", "exec": "omarchy-launch-webapp https://hey.com"}), "omarchy")
            self.assertEqual(section({"desktop_path": "/a/mine.desktop", "exec": "true"}), "other")
            self.assertEqual(section({"plugin_id": "x", "plugin_dir": str(root / "shell/plugins/x")}), "omarchy")
            self.assertEqual(section({"plugin_id": "y", "plugin_dir": "/home/u/.config/omarchy/plugins/y"}), "other")


if __name__ == "__main__":
    unittest.main()
