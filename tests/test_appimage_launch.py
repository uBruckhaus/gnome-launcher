import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher_core


class AppImageLaunchTests(unittest.TestCase):
    def scan(self, directory):
        with patch.object(launcher_core, "get_appimage_directories", return_value=[directory]), \
                patch.object(launcher_core.shutil, "which", return_value=None):
            return launcher_core.scan_standalone_appimages(set())[0]

    def test_hostile_filenames_launch_only_the_selected_file(self):
        filenames = [
            'ordinary app.AppImage',
            'quote"; touch INJECTED; #.AppImage',
            '$(touch INJECTED).AppImage',
            '`touch INJECTED`.AppImage',
            "single'quote\\percent%f.AppImage",
            'line\nbreak.AppImage',
        ]
        for filename in filenames:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                executable = directory / filename
                executable.write_text('#!/bin/sh\nprintf "%s" "$0" > "$LAUNCH_MARKER"\n')
                executable.chmod(0o700)
                app = self.scan(directory)
                self.assertEqual(shlex.split(app["exec"]), [str(executable)])
                marker = directory / "launched"
                processes = []
                real_popen = subprocess.Popen

                def spawn(argv, **kwargs):
                    self.assertEqual(argv, [str(executable)])
                    self.assertFalse(kwargs.get("shell", False))
                    process = real_popen(argv, cwd=directory, **kwargs)
                    processes.append(process)
                    return process

                with patch.dict(os.environ, LAUNCH_MARKER=str(marker)), \
                        patch.object(launcher_core.subprocess, "Popen", side_effect=spawn):
                    self.assertTrue(launcher_core.launch_app(app))
                for process in processes:
                    self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(marker.read_text(), str(executable))
                self.assertFalse((directory / "INJECTED").exists())

    def test_failed_direct_launch_never_falls_back_to_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / 'removed"; touch INJECTED; #.AppImage'
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            app = self.scan(Path(tmp))
            with patch.object(launcher_core.subprocess, "Popen", side_effect=PermissionError) as spawn:
                self.assertFalse(launcher_core.launch_app(app))
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(spawn.call_args.args[0], [str(executable)])


if __name__ == "__main__":
    unittest.main()
