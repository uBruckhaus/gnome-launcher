import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher_core


class AppImageScanTests(unittest.TestCase):
    def test_disabled_appimage_is_skipped_without_changing_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Disabled.AppImage"
            path.write_bytes(b"disabled")
            path.chmod(0o640)
            with patch.object(launcher_core, "get_appimage_directories", return_value=[Path(tmp)]), \
                    patch.object(launcher_core.shutil, "which", return_value="/usr/bin/7z"), \
                    patch.object(launcher_core.subprocess, "run") as extract:
                self.assertEqual(launcher_core.scan_standalone_appimages(set()), [])
                extract.assert_not_called()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(path.read_bytes(), b"disabled")

    def test_executable_appimage_remains_available_with_original_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Enabled.AppImage"
            path.write_bytes(b"enabled")
            path.chmod(0o750)
            with patch.object(launcher_core, "get_appimage_directories", return_value=[Path(tmp)]), \
                    patch.object(launcher_core.shutil, "which", return_value=None):
                apps = launcher_core.scan_standalone_appimages(set())
            self.assertEqual([app["desktop_id"] for app in apps], ["Enabled"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o750)

    def test_base_name_drops_version_system_and_cpu(self):
        self.assertEqual(launcher_core.appimage_base_name("photocraft-0.14.0-linux-x86_64"), "photocraft")
        self.assertEqual(launcher_core.appimage_base_name("Asterism-0.7.0-x86_64"), "Asterism")
        self.assertEqual(launcher_core.appimage_base_name("LM-Studio-0.3.5-2-x64"), "LM Studio")
        self.assertEqual(launcher_core.appimage_base_name("2048-game-1.0"), "2048 game")

    def scan(self, names, seen=(), started=()):
        with tempfile.TemporaryDirectory() as tmp:
            for name in names:
                path = Path(tmp) / name
                path.write_bytes(b"app")
                path.chmod(0o750)
            started = [str(Path(tmp) / name) for name in started]
            with patch.object(launcher_core, "get_appimage_directories", return_value=[Path(tmp)]), \
                    patch.object(launcher_core.shutil, "which", return_value=None):
                return launcher_core.scan_standalone_appimages(set(seen), started)

    def test_versions_of_an_app_with_a_desktop_file_are_not_listed_again(self):
        names = ["photocraft-0.12.0-linux-x86_64.AppImage", "photocraft-0.14.0-linux-x86_64.AppImage"]
        self.assertEqual(self.scan(names, started=[names[1]]), [])
        # The installer's own name for it ("PhotoCraft") hides it as well.
        self.assertEqual(self.scan(names, seen={"photocraft"}), [])

    def test_only_the_newest_version_is_listed(self):
        apps = self.scan(["tool-0.9.0-x86_64.AppImage", "tool-0.14.0-x86_64.AppImage"])
        self.assertEqual([(a["name"], a["desktop_id"]) for a in apps], [("tool", "tool-0.14.0-x86_64")])

    def test_newest_version_by_numbers_not_by_spelling(self):
        for names, newest in ((["tool-1.0.AppImage", "tool-v2.0.AppImage"], "tool-v2.0"),
                              (["tool-1.0-x86_64.AppImage", "tool-1.0.1-x86_64.AppImage"], "tool-1.0.1-x86_64"),
                              (["Tool_0.9.0.AppImage", "tool-0.10.0-linux-x86_64.AppImage"], "tool-0.10.0-linux-x86_64")):
            self.assertEqual([a["desktop_id"] for a in self.scan(names)], [newest], names)

    def test_newest_version_wins_across_folders(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for folder, name in ((a, "tool-0.9.0-x86_64.AppImage"), (b, "tool-0.14.0-x86_64.AppImage")):
                path = Path(folder) / name
                path.write_bytes(b"app")
                path.chmod(0o750)
            with patch.object(launcher_core, "get_appimage_directories", return_value=[Path(a), Path(b)]), \
                    patch.object(launcher_core.shutil, "which", return_value=None):
                apps = launcher_core.scan_standalone_appimages(set())
        self.assertEqual([a["desktop_id"] for a in apps], ["tool-0.14.0-x86_64"])

    def test_desktop_entry_exec_marks_its_appimage_as_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            image = directory / "My App-1.2.0-x86_64.AppImage"
            (directory / "myapp.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Installed Name\n"
                f"Exec=/usr/bin/env '{image}' --no-sandbox %U\n")
            with patch.object(launcher_core, "DESKTOP_DIRS", [directory]), \
                    patch.object(launcher_core, "scan_standalone_appimages", return_value=[]) as scan, \
                    patch.object(launcher_core.cli_tools, "cli_entries", return_value=[]):
                launcher_core.parse_desktop_files()
            self.assertEqual(set(scan.call_args.args[1]), {str(image)})


if __name__ == "__main__":
    unittest.main()
