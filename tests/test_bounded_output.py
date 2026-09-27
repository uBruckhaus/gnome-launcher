import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher_core


class BoundedOutputTests(unittest.TestCase):
    def extract(self, script, **kwargs):
        processes = []
        real_popen = subprocess.Popen

        def spawn(*args, **options):
            process = real_popen(*args, **options)
            processes.append(process)
            return process

        with patch.object(launcher_core.subprocess, "Popen", side_effect=spawn):
            result = launcher_core.read_bounded_output(
                [sys.executable, "-c", script], **kwargs)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        return result

    def test_valid_metadata_and_exact_byte_limit(self):
        self.assertEqual(self.extract("print('[Desktop Entry]\\nName=Example')"),
                         "[Desktop Entry]\nName=Example\n")
        self.assertEqual(self.extract("import os; os.write(1, b'x' * 1024)",
                                      max_bytes=1024), "x" * 1024)

    def test_one_byte_over_limit_is_rejected(self):
        self.assertIsNone(self.extract("import os; os.write(1, b'x' * 1025)",
                                       max_bytes=1024))

    def test_unending_stdout_is_bounded_and_child_reaped(self):
        self.assertIsNone(self.extract(
            "import os\nwhile True: os.write(1, b'x' * 8192)", max_bytes=1024))

    def test_stderr_is_discarded(self):
        self.assertEqual(self.extract(
            "import os\nfor _ in range(256): os.write(2, b'x' * 8192)\n"
            "os.write(1, b'ok')"), "ok")

    def test_timeout_covers_silence_and_closed_stdout(self):
        for script in ["import time; time.sleep(30)",
                       "import os, time; os.close(1); time.sleep(30)"]:
            with self.subTest(script=script):
                start = time.monotonic()
                self.assertIsNone(self.extract(script, timeout=0.2))
                self.assertLess(time.monotonic() - start, 3)

    def test_nonzero_exit_discards_partial_metadata(self):
        self.assertIsNone(self.extract("print('[Desktop Entry]'); exit(1)"))

    def test_failed_metadata_keeps_appimage_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "Example.AppImage"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            with patch.object(launcher_core, "get_appimage_directories", return_value=[Path(tmp)]), \
                    patch.object(launcher_core.shutil, "which", return_value="/usr/bin/7z"), \
                    patch.object(launcher_core, "read_bounded_output", return_value=None), \
                    patch.object(launcher_core.subprocess, "run") as icon_extract:
                apps = launcher_core.scan_standalone_appimages(set())
            self.assertEqual(len(apps), 1)
            self.assertEqual(apps[0]["name"], "Example")
            self.assertEqual(apps[0]["argv"], [str(executable)])
            self.assertEqual(apps[0]["icon"], "application-x-executable")
            icon_extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
