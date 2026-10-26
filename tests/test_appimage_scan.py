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
        image = "/home/u/My App-1.2.0-x86_64.AppImage"
        self.assertEqual(self.started_by(f"/usr/bin/env '{image}' --no-sandbox %U"), {image})

    def test_env_by_path_checks_the_real_program(self):
        self.assertFalse(launcher_core.executable_exists("/usr/bin/env X=1 /nonexistent/Tool.AppImage"))
        self.assertTrue(launcher_core.executable_exists("/usr/bin/env X=1 sh -c true"))

    def test_prerelease_stage_counts_before_its_number(self):
        for names, newest in ((["Tool-1.0-beta9.AppImage", "Tool-1.0-rc1.AppImage"], "Tool-1.0-rc1"),
                              (["Tool-1.0-rc1.AppImage", "Tool-1.0-beta1.AppImage"], "Tool-1.0-rc1"),
                              (["Tool-1.0-alpha5.AppImage", "Tool-1.0-beta1.AppImage"], "Tool-1.0-beta1")):
            self.assertEqual([a["desktop_id"] for a in self.scan(names)], [newest], names)

    def test_env_options_before_the_program(self):
        self.assertEqual(launcher_core.exec_program("/usr/bin/env -u PYTHONPATH /home/u/Tool.AppImage"),
                         "/home/u/Tool.AppImage")
        self.assertEqual(launcher_core.exec_program("env --unset=A -- B=1 foo --x"), "foo")
        self.assertEqual(launcher_core.exec_program("env -i A=1 foo"), "foo")
        # Forms it does not know: no program, but the entry is not rejected for it.
        self.assertIsNone(launcher_core.exec_program("env -S 'foo bar'"))
        self.assertTrue(launcher_core.executable_exists("/usr/bin/env -S 'sh -c true'"))
        self.assertTrue(launcher_core.executable_exists("/usr/bin/env -u PYTHONPATH sh -c true"))

    def test_env_chdir_checks_env_itself(self):
        # The program would be found relative to DIR: env is checked instead, the entry stays.
        for command in ("/usr/bin/env -C /tmp ./prog", "env --chdir=/tmp ./prog", "env --chdir /tmp prog"):
            self.assertIsNone(launcher_core.exec_program(command), command)
            self.assertTrue(launcher_core.executable_exists(command), command)

    def test_nul_in_exec_path_is_not_runnable_and_does_not_stop_the_scan(self):
        self.assertFalse(launcher_core.executable_exists("/opt/tool\0x --run"))
        self.assertFalse(launcher_core.executable_exists("/usr/bin/env /opt/tool\0x"))
        self.assertFalse(launcher_core.executable_exists("tool\0x"))

    def test_hyphenated_build_metadata_is_ignored(self):
        self.assertEqual([a["desktop_id"] for a in self.scan(["Tool-1.0+build-999.AppImage", "Tool-1.0.1.AppImage"])],
                         ["Tool-1.0.1"])
        self.assertEqual(launcher_core._version_key("Tool-1.0+build-999"), launcher_core._version_key("Tool-1.0"))

    def test_cpu_names_only_as_their_own_word(self):
        self.assertEqual(launcher_core.appimage_base_name("Superarm64-1.0"), "Superarm64")
        self.assertEqual(launcher_core.appimage_base_name("Super-arm64"), "Super")
        self.assertEqual(launcher_core.appimage_base_name("Tool_x86_64"), "Tool")
        apps = self.scan(["Superarm64-1.0.AppImage", "Super-2.0.AppImage"])
        self.assertEqual(sorted(a["name"] for a in apps), ["Super", "Superarm64"])

    def test_digit_words_in_the_name_stay(self):
        self.assertEqual(launcher_core.appimage_base_name("Foo-3D-1.0"), "Foo 3D")
        self.assertEqual(launcher_core.appimage_base_name("Tool-1.0-rc1-x86_64"), "Tool")
        self.assertEqual(launcher_core.appimage_base_name("Tool-x86_64"), "Tool")

    def test_final_release_beats_its_prereleases(self):
        for names, newest in ((["Tool-1.0rc1.AppImage", "Tool-1.0.AppImage"], "Tool-1.0"),
                              (["Tool-1.0-beta2.AppImage", "Tool-1.0.AppImage"], "Tool-1.0"),
                              (["Tool-1.0-rc1.AppImage", "Tool-1.0-rc2.AppImage"], "Tool-1.0-rc2"),
                              (["Tool-1.0+build99.AppImage", "Tool-1.1.AppImage"], "Tool-1.1")):
            self.assertEqual([a["desktop_id"] for a in self.scan(names)], [newest], names)

    def test_other_versions_stay_hidden_when_embedded_names_differ(self):
        def fake_read(argv, **_kwargs):
            name = "Tool Community" if "0.9" in argv[3] else "Tool"
            return f"[Desktop Entry]\nName={name}\n"
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("tool-0.9.0.AppImage", "tool-1.0.0.AppImage"):
                path = Path(tmp) / name
                path.write_bytes(b"app")
                path.chmod(0o750)
            with patch.object(launcher_core, "get_appimage_directories", return_value=[Path(tmp)]), \
                    patch.object(launcher_core.shutil, "which", return_value="/usr/bin/7z"), \
                    patch.object(launcher_core, "read_bounded_output", side_effect=fake_read):
                apps = launcher_core.scan_standalone_appimages(set())
        self.assertEqual([a["desktop_id"] for a in apps], ["tool-1.0.0"])

    def started_by(self, exec_line):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "entry.desktop").write_text(
                f"[Desktop Entry]\nType=Application\nName=Entry\nExec={exec_line}\n")
            with patch.object(launcher_core, "DESKTOP_DIRS", [directory]), \
                    patch.object(launcher_core, "executable_exists", return_value=True), \
                    patch.object(launcher_core, "scan_standalone_appimages", return_value=[]) as scan, \
                    patch.object(launcher_core.cli_tools, "cli_entries", return_value=[]):
                launcher_core.parse_desktop_files()
            return set(scan.call_args.args[1])

    def test_only_the_started_program_counts(self):
        # Opened as a file by another program: not the AppImage's own entry.
        self.assertEqual(self.started_by("/usr/bin/editor /home/u/Tool-1.0.AppImage"), set())
        self.assertEqual(self.started_by("env X=/a/b.AppImage /usr/bin/editor"), set())
        self.assertEqual(self.started_by("env -i X=1 /home/u/Tool-1.0.AppImage %U"), {"/home/u/Tool-1.0.AppImage"})

    def test_extension_case_does_not_matter(self):
        self.assertEqual(self.started_by("/home/u/Tool-1.0.APPIMAGE"), {"/home/u/Tool-1.0.APPIMAGE"})


if __name__ == "__main__":
    unittest.main()
