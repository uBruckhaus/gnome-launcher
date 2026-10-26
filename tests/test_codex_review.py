"""2.4.1: findings from the Codex review — an env prefix that changes PATH,
an icon path swapped for a FIFO; then bounded desktop entry reads (scan,
launch, actions, shortcut helper) and no process-ending Quit."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_core  # noqa: E402


class EnvPathTests(unittest.TestCase):
    def test_entries_that_change_path_are_not_hidden(self):
        # The program is only in the PATH the entry sets: env itself is checked, so it stays listed.
        for command in ("/usr/bin/env PATH=/nonexistent/custom only-there", "env -i only-there",
                        "env -u PATH only-there", "env --unset=PATH only-there", "env X=1 PATH=/x -- only-there"):
            with self.subTest(command=command):
                self.assertTrue(launcher_core.env_changes_path(command))
                self.assertTrue(launcher_core.executable_exists(command))

    def test_other_env_forms_still_check_the_program(self):
        self.assertFalse(launcher_core.env_changes_path("env FOO=1 prog"))
        self.assertFalse(launcher_core.executable_exists("env FOO=1 /nonexistent/prog"))
        self.assertFalse(launcher_core.executable_exists("env PATH=/x /nonexistent/prog"))   # a path: checked as is
        self.assertFalse(launcher_core.env_changes_path("prog PATH=/x"))


def _sleep_path():
    return os.path.realpath(shutil.which("sleep"))


class EnvSpeedTests(unittest.TestCase):
    def test_many_env_assignments_stay_fast(self):
        command = "env " + " ".join(f"A{i}=1" for i in range(20000)) + " prog"
        started = time.monotonic()
        self.assertEqual(launcher_core.exec_program(command), "prog")
        self.assertEqual(launcher_core.app_process_name({"exec": command}), "prog")
        self.assertLess(time.monotonic() - started, 2.0)


class DesktopFileReadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.dir = Path(self.folder.name)
        patcher = mock.patch.object(launcher_core, "DESKTOP_DIRS", [self.dir])
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("scan_standalone_appimages",):
            p = mock.patch.object(launcher_core, name, return_value=[])
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(launcher_core.cli_tools, "cli_entries", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def write_good(self):
        (self.dir / "good.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName=Good\nExec={_sleep_path()} 1\n"
            "Actions=new;\n\n[Desktop Action new]\nName=New\nExec=sleep 2\n")

    def test_fifo_entry_never_blocks(self):
        os.mkfifo(self.dir / "fifo.desktop")
        os.symlink(self.dir / "fifo.desktop", self.dir / "link-to-fifo.desktop")
        self.write_good()
        started = time.monotonic()
        apps = launcher_core.parse_desktop_files()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual([a["name"] for a in apps], ["Good"])
        self.assertEqual([a["id"] for a in apps[0]["actions"]], ["new"])
        self.assertIsNone(launcher_core.read_desktop_file(self.dir / "fifo.desktop"))

    def test_oversized_entry_is_skipped(self):
        big = self.dir / "big.desktop"
        big.write_text(f"[Desktop Entry]\nName=Big\nExec={_sleep_path()}\n" + "X-Pad=" + "a" * (300 * 1024) + "\n")
        self.write_good()
        self.assertIsNone(launcher_core.read_desktop_file(big))
        self.assertEqual([a["name"] for a in launcher_core.parse_desktop_files()], ["Good"])

    def test_endless_source_is_cut_off(self):
        # /dev/zero is no regular file: refused before any read.
        os.symlink("/dev/zero", self.dir / "zero.desktop")
        self.assertIsNone(launcher_core.read_desktop_file(self.dir / "zero.desktop"))
        self.write_good()
        self.assertEqual([a["name"] for a in launcher_core.parse_desktop_files()], ["Good"])


    def test_launch_and_actions_never_reopen_a_fifo(self):
        fifo = self.dir / "swapped.desktop"
        os.mkfifo(fifo)
        app = {"name": "X", "exec": "x", "desktop_path": str(fifo)}
        started = time.monotonic()
        with mock.patch.object(launcher_core.shutil, "which", return_value=None), \
                mock.patch.object(launcher_core.GioUnix.DesktopAppInfo, "new_from_filename",
                                  side_effect=AssertionError("unbounded read")):
            self.assertFalse(launcher_core.launch_app(app))
            self.assertFalse(launcher_core.launch_action(app, "new"))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_desktop_app_info_reads_bounded(self):
        self.write_good()
        info = launcher_core.desktop_app_info(self.dir / "good.desktop")
        self.assertEqual(info.list_actions(), ["new"])
        big = self.dir / "big.desktop"
        big.write_text("[Desktop Entry]\nName=Big\nExec=sleep\nX-Pad=" + "a" * (300 * 1024) + "\n")
        self.assertIsNone(launcher_core.desktop_app_info(big))


    def test_shortcut_helper_never_blocks_on_a_fifo(self):
        import launch_entry
        os.mkfifo(self.dir / "helper.desktop")
        started = time.monotonic()
        self.assertEqual(launch_entry.main(["launch_entry.py", str(self.dir / "helper.desktop"), "new"]), 1)
        self.assertLess(time.monotonic() - started, 2.0)


class IconFifoTests(unittest.TestCase):
    def test_fifo_icon_never_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = os.path.join(folder, "icon.png")
            os.mkfifo(fifo)
            started = time.monotonic()
            self.assertIsNone(launcher_core.icon_bytes(fifo))
            self.assertLess(time.monotonic() - started, 1.0)



class AppIconSizeTests(unittest.TestCase):
    def test_saved_size_is_one_of_the_choices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            self.assertEqual(launcher_core.saved_app_icon_size(path), launcher_core.DEFAULT_APP_ICON_SIZE)
            launcher_core.set_app_icon_size(56, path)
            self.assertEqual(launcher_core.saved_app_icon_size(path), 56)
            for bad in (13, "48", True, None, 10 ** 9):
                path.write_text('{"app_icon_size": %s}' % __import__("json").dumps(bad))
                self.assertEqual(launcher_core.saved_app_icon_size(path), launcher_core.DEFAULT_APP_ICON_SIZE)


class DesktopFileLocationTest(unittest.TestCase):
    """%k keeps the desktop file's location although the info is built from a bounded read."""

    def test_k_is_the_quoted_path(self):
        from gi.repository import GLib
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'my "app" $x 5%.desktop')
            Path(path).write_text("[Desktop Entry]\nType=Application\nName=T\nExec=true --file %k --literal %%k\n")
            line = launcher_core.desktop_app_info(path).get_commandline()
            ok, argv = GLib.shell_parse_argv(line.replace("%%", "%"))
            self.assertEqual(argv[2], path)
            self.assertEqual(argv[-1], "%k")


if __name__ == "__main__":
    unittest.main()
