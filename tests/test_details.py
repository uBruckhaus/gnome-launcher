"""Details page facts: pacman database parsing, origins, default apps and uninstall plans."""
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
        youtube = {"exec": "xdg-open https://youtube.com/", "desktop_id": "YouTube"}
        self.assertEqual(details.web_address(youtube), "https://youtube.com/")
        facts = details.app_facts(youtube)
        self.assertEqual((facts["kind"], facts["website"]), ("webapp", "https://youtube.com/"))
        self.assertNotIn("package", facts)
        self.assertIsNone(details.web_address({"exec": "xdg-open file:///etc/passwd"}))
        self.assertEqual(details.terminal_program(["foot", "--app-id=x", "-e", "/usr/bin/btop"]), "/usr/bin/btop")
        self.assertIsNone(details.terminal_program(["xdg-terminal-exec", "-e", "bash", "-c", "dua i /"]))
        self.assertIsNone(details.terminal_program(["chromium", "-e", "x"]))

class DefaultAppTests(unittest.TestCase):
    def test_default_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            (config / "mimeapps.list").write_text("[Added Associations]\ntext/plain=evil.desktop\n"
                                                  "[Default Applications]\nx-scheme-handler/http=google-chrome.desktop;chromium.desktop\n"
                                                  "text/plain=micro.desktop\n")
            roles = lambda name: details.default_roles({"desktop_path": "/x/" + name}, config)
            self.assertEqual(roles("google-chrome.desktop"), ["browser"])
            self.assertEqual(roles("chromium.desktop"), [])   # second choice only
            self.assertEqual(roles("micro.desktop"), ["editor"])
            self.assertEqual(roles("evil.desktop"), [])       # an association, not a default
            self.assertEqual(roles("org.gnome.Ptyxis.desktop"), [])
            (config / "xdg-terminals.list").write_text("# mine\norg.gnome.Ptyxis.desktop\n")
            self.assertEqual(roles("org.gnome.Ptyxis.desktop"), ["terminal"])
            # GNOME's own list wins over the general one.
            (config / "gnome-xdg-terminals.list").write_text("kitty.desktop\n")
            self.assertEqual(roles("org.gnome.Ptyxis.desktop"), [])

    def test_origin_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            (data / "applications").mkdir(parents=True)
            defaults = {"google-chrome.desktop"}
            section = lambda app: details.origin_section(app, defaults, data)
            self.assertEqual(section({"desktop_path": "/usr/share/applications/google-chrome.desktop"}), "defaults")
            self.assertEqual(section({"desktop_path": "/usr/share/applications/org.gnome.Maps.desktop"}), "system")
            flatpak = "/var/lib/flatpak/exports/share/applications/com.mattjakeman.ExtensionManager.desktop"
            self.assertEqual(section({"desktop_path": flatpak, "sandboxed": True}), "flatpak")
            self.assertEqual(section({"desktop_path": str(data / "applications/mine.desktop")}), "user")
            self.assertEqual(section({"desktop_path": "", "argv": ["/home/u/AppImages/x.AppImage"]}), "user")
            self.assertEqual(section({"desktop_path": "/usr/share/applications/yt.desktop",
                                      "exec": "xdg-open https://youtube.com"}), "user")


class UninstallPlanTests(unittest.TestCase):
    def test_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            (data / "applications").mkdir(parents=True)
            own = data / "applications" / "mine.desktop"
            own.write_text("")
            appimage = Path(tmp) / "x.AppImage"
            appimage.write_text("")
            plan = lambda app, **kw: details.uninstall_plan(app, data, **kw)
            self.assertEqual(plan({"desktop_path": str(own)}), ("trash", str(own)))
            self.assertEqual(plan({"desktop_path": "", "argv": [str(appimage)]}), ("trash", str(appimage)))
            self.assertIsNone(plan({"desktop_path": "", "argv": ["xdg-terminal-exec", "-e", "claude"],
                                    "cli_command": "claude"}))
            flatpak = "/var/lib/flatpak/exports/share/applications/org.gnome.Maps.desktop"
            self.assertEqual(plan({"desktop_path": flatpak, "sandboxed": True}), ("flatpak", "org.gnome.Maps"))
            self.assertIsNone(plan({"desktop_path": "/var/lib/flatpak/exports/share/applications/-rf.desktop",
                                    "sandboxed": True}))
            web = {"desktop_path": str(own), "exec": "/opt/google/chrome/google-chrome --profile-directory=Default "
                                                   "--app-id=abcdefghijklmnopabcdefghijklmnop"}
            self.assertEqual(plan(web)[0], "webapp")
            system = {"desktop_path": "/usr/share/applications/org.gnome.Maps.desktop"}
            self.assertEqual(plan(system, look_up_package=False), ("package", None))
            with mock.patch.object(details, "package_owner", return_value="gnome-maps"):
                self.assertEqual(plan(system), ("package", "gnome-maps"))
            with mock.patch.object(details, "package_owner", return_value=None):
                self.assertIsNone(plan(system))


if __name__ == "__main__":
    unittest.main()
