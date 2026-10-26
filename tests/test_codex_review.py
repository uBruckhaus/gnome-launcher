"""2.4.1: findings from the Codex review — an env prefix that changes PATH,
Quit after a PID was reused, and an icon path swapped for a FIFO; then
Quit only by the canonical executable and bounded desktop entry reads."""
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


class EndProcessesTests(unittest.TestCase):
    def test_only_a_process_that_still_is_the_app_is_ended(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            time.sleep(0.1)
            path = _sleep_path()
            targets = launcher_core.executable_processes(path)
            self.assertIn(proc.pid, targets)
            mine = {proc.pid: targets[proc.pid]}
            # A reused PID now belongs to another program: nothing is sent.
            self.assertEqual(launcher_core.end_processes("/usr/bin/not-sleep", mine), 0)
            # Same PID and program, but another start time (a later process): nothing either.
            self.assertEqual(launcher_core.end_processes(path, {proc.pid: mine[proc.pid] + 1}), 0)
            self.assertIsNone(proc.poll())
            self.assertEqual(launcher_core.end_processes(path, mine), 1)
            self.assertEqual(proc.wait(timeout=5), -15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_gone_pids_are_skipped(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.assertEqual(launcher_core.end_processes(os.path.realpath(shutil.which("true")), {proc.pid: 0}), 0)


class QuitIdentityTests(unittest.TestCase):
    def test_interpreters_and_wrappers_have_no_quit_target(self):
        python = os.path.realpath(sys.executable)          # e.g. /usr/bin/python3.14
        for command in (f"{python} /opt/app/app.py", "/usr/bin/python3.14 app.py", "python3 -m app",
                        "env FOO=1 python3.14 app.py", "bash -c app", "/usr/bin/env app",
                        "flatpak run org.example.App", "node22 app.js", "foot", "xdg-terminal-exec -e python3.14 app.py", "/usr/bin/mise x -- codex"):
            with self.subTest(command=command):
                self.assertIsNone(launcher_core.quit_target({"exec": command, "desktop_id": "x"}))

    def test_web_apps_and_cli_tools_have_none(self):
        sleep = _sleep_path()
        for app in ({"exec": sleep, "pwa": True},
                    {"exec": f"{sleep} --app-id=abc"}, {"exec": sleep, "cli_command": "x"}):
            with self.subTest(app=app):
                self.assertIsNone(launcher_core.quit_target(app, [], []))
        # Terminal=true: Gio opens it in a terminal, its own executable stays the target.
        self.assertEqual(launcher_core.quit_target({"exec": sleep, "terminal": True}, [], []), sleep)

    def test_the_canonical_executable_is_the_target(self):
        self.assertEqual(launcher_core.quit_target({"exec": "sleep 30", "desktop_id": "s"}, [], []), _sleep_path())

    def test_a_terminal_app_is_its_command(self):
        app = {"exec": "xdg-terminal-exec -e sleep 30", "desktop_id": "t"}
        self.assertEqual(launcher_core.quit_target(app, [app], []), _sleep_path())
        self.assertIsNone(launcher_core.quit_target({"exec": "xdg-terminal-exec", "desktop_id": "t"}, [], []))

    def test_two_entries_of_one_executable_are_ambiguous(self):
        writer = {"exec": "sleep --writer", "desktop_id": "writer"}
        calc = {"exec": "sleep --calc", "desktop_id": "calc"}
        self.assertIsNone(launcher_core.quit_target(writer, [writer, calc], []))
        self.assertEqual(launcher_core.quit_target(writer, [writer], []), _sleep_path())

    def test_same_name_in_another_folder_is_not_the_app(self):
        # An entry for /tmp/x/sleep (a copy) must not reach the system's sleep processes.
        with tempfile.TemporaryDirectory() as folder:
            copy = os.path.join(folder, "sleep")
            shutil.copy2(_sleep_path(), copy)
            proc = subprocess.Popen(["sleep", "30"])
            try:
                time.sleep(0.1)
                target = launcher_core.quit_target({"exec": copy, "desktop_id": "copy"}, [], [])
                self.assertEqual(target, os.path.realpath(copy))
                found = launcher_core.executable_processes(target)
                self.assertNotIn(proc.pid, found)
                self.assertEqual(launcher_core.end_processes(target, {proc.pid: 0}), 0)
                self.assertIsNone(proc.poll())
            finally:
                proc.kill()
                proc.wait()

    def test_a_hidden_entry_of_the_same_executable_makes_it_ambiguous(self):
        app = {"exec": "sleep 30", "desktop_id": "visible"}
        self.assertIsNone(launcher_core.quit_target(app, [app], [("hidden-helper", "sleep --helper")]))
        self.assertEqual(launcher_core.quit_target(app, [app], [("visible", "sleep --own-action")]), _sleep_path())

    def test_unresolved_wrapper_forms_count_as_sharing(self):
        app = {"exec": "/usr/bin/sleep --first", "desktop_id": "first"}
        for line in ("env -C /tmp /usr/bin/sleep --second", "env -S '/usr/bin/sleep --second'",
                     "sh -c 'sleep 5'", "launcher --exec=/opt/x/sleep"):
            with self.subTest(line=line):
                self.assertIsNone(launcher_core.quit_target(app, [app], [("other", line)]))

    def test_symlink_aliases_of_one_executable_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as folder:
            real = os.path.join(folder, "editor")
            shutil.copy2(_sleep_path(), real)
            for alias in ("editor-a", "editor-b"):
                os.symlink(real, os.path.join(folder, alias))
            a = {"exec": os.path.join(folder, "editor-a"), "desktop_id": "a"}
            b = {"exec": os.path.join(folder, "editor-b"), "desktop_id": "b"}
            self.assertIsNone(launcher_core.quit_target(a, [a, b], []))
            self.assertIsNone(launcher_core.quit_target(a, [a], [("b", f"env -C /tmp {b['exec']} --x")]))
            self.assertEqual(launcher_core.quit_target(a, [a], []), os.path.realpath(real))

    def test_relative_paths_have_no_quit_target(self):
        with tempfile.TemporaryDirectory() as folder:
            shutil.copy2(_sleep_path(), os.path.join(folder, "foo"))
            cwd = os.getcwd()
            try:
                os.chdir(folder)
                self.assertIsNone(launcher_core.quit_target({"exec": "./foo", "desktop_id": "r"}, [], []))
                with mock.patch.dict(os.environ, {"PATH": "."}):
                    self.assertIsNone(launcher_core.quit_target({"exec": "foo", "desktop_id": "r"}, [], []))
            finally:
                os.chdir(cwd)

    def test_scan_records_hidden_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            (d / "app.desktop").write_text(f"[Desktop Entry]\nName=App\nExec={_sleep_path()} 1\n")
            (d / "helper.desktop").write_text(f"[Desktop Entry]\nName=Helper\nNoDisplay=true\nExec={_sleep_path()} 2\n")
            with mock.patch.object(launcher_core, "DESKTOP_DIRS", [d]), \
                    mock.patch.object(launcher_core, "scan_standalone_appimages", return_value=[]), \
                    mock.patch.object(launcher_core.cli_tools, "cli_entries", return_value=[]):
                apps = launcher_core.parse_desktop_files()
            self.assertEqual([a["name"] for a in apps], ["App"])
            self.assertIsNone(launcher_core.quit_target(apps[0], apps))

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


if __name__ == "__main__":
    unittest.main()
