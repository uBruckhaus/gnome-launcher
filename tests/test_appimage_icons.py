import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher_core


class AppImageIconTests(unittest.TestCase):
    def extract(self, directory, script, name="example.png"):
        real_popen = subprocess.Popen
        processes = []

        def spawn(command, **kwargs):
            self.assertEqual(command[:4], ["7z", "e", "-so", "-spd"])
            self.assertEqual(command[-1], name)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            process = real_popen([sys.executable, "-c", script], **kwargs)
            processes.append(process)
            return process

        with patch.object(launcher_core.subprocess, "Popen", side_effect=spawn):
            result = launcher_core.cache_appimage_icon(
                directory / "Example.AppImage", name, directory / "icons")
        self.assertTrue(processes)
        self.assertTrue(all(p.returncode is not None for p in processes))
        return result

    def test_binary_icon_at_limit_and_repeated_scan_use_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            size = launcher_core.MAX_APPIMAGE_ICON_BYTES
            result = self.extract(directory, f"import os; os.write(1, b'\xff' * {size})".replace("ÿ", "\\xff"))
            self.assertEqual(Path(result).read_bytes(), b"\xff" * size)
            again = self.extract(directory, "import os; os.write(1, b'updated')")
            self.assertEqual(again, result)
            self.assertEqual(Path(again).read_bytes(), b"updated")
            self.assertEqual(list((directory / "icons").iterdir()), [Path(result)])

    def test_oversized_and_unending_output_never_written_to_disk(self):
        scripts = [
            f"import os; os.write(1, b'x' * {launcher_core.MAX_APPIMAGE_ICON_BYTES + 1})",
            "import os\nwhile True: os.write(1, b'x' * 8192)",
        ]
        for script in scripts:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                self.assertEqual(self.extract(directory, script), "")
                self.assertFalse((directory / "icons").exists())

    def test_failed_extractor_does_not_cache_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(self.extract(directory, "print('partial'); exit(1)"), "")
            self.assertFalse((directory / "icons").exists())

    def test_cache_symlink_is_replaced_without_writing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = Path(self.extract(directory, "print('icon')"))
            victim = directory / "unrelated"
            victim.write_text("preserve")
            result.unlink()
            result.symlink_to(victim)
            self.extract(directory, "print('replacement')")
            self.assertEqual(victim.read_text(), "preserve")
            self.assertFalse(result.is_symlink())

    def test_scan_falls_back_to_generic_icon_and_preserves_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            appimage = directory / "Example.AppImage"
            appimage.write_bytes(b"example")
            appimage.chmod(0o700)
            with patch.object(launcher_core, "get_appimage_directories", return_value=[directory]), \
                    patch.object(launcher_core.shutil, "which", return_value="7z"), \
                    patch.object(launcher_core, "read_bounded_output", return_value="[Desktop Entry]\nName=Example\nIcon=example.png\n"), \
                    patch.object(launcher_core, "cache_appimage_icon", return_value=""):
                apps = launcher_core.scan_standalone_appimages(set())
            self.assertEqual(apps[0]["icon"], "application-x-executable")
            self.assertEqual(apps[0]["argv"], [str(appimage)])


if __name__ == "__main__":
    unittest.main()
