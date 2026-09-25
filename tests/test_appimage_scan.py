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


if __name__ == "__main__":
    unittest.main()
