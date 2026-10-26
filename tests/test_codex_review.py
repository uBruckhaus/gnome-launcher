"""2.4.1: findings from the Codex review — an env prefix that changes PATH,
Quit after a PID was reused, and an icon path swapped for a FIFO."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
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


class EndProcessesTests(unittest.TestCase):
    def test_only_a_process_that_still_is_the_app_is_ended(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            time.sleep(0.1)
            # A reused PID now belongs to another program: nothing is sent.
            self.assertEqual(launcher_core.end_processes("not-sleep", {proc.pid}), 0)
            self.assertIsNone(proc.poll())
            self.assertEqual(launcher_core.end_processes("sleep", {proc.pid}), 1)
            self.assertEqual(proc.wait(timeout=5), -15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_gone_pids_are_skipped(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.assertEqual(launcher_core.end_processes("true", {proc.pid}), 0)


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
