import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import launcher_core


class DesktopLaunchTests(unittest.TestCase):
    def test_native_rejection_of_scanned_entry_never_runs_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            marker = directory / "INJECTED"
            for command in [f'/usr/bin/true $(touch {marker})',
                            f'/usr/bin/true ; touch {marker}',
                            f'/usr/bin/true `touch {marker}`']:
                with self.subTest(command=command):
                    desktop = directory / "rejected.desktop"
                    # The manual scanner accepts this, but Gio rejects a Link
                    # entry as an application, leaving only the former fallback.
                    desktop.write_text('[Desktop Entry]\nType=Link\nName=Rejected\n'
                                       f'Exec={command}\n')
                    with self.assertRaises(TypeError):
                        launcher_core.GioUnix.DesktopAppInfo.new_from_filename(str(desktop))
                    with patch.object(launcher_core, "DESKTOP_DIRS", [directory]), \
                            patch.object(launcher_core, "scan_standalone_appimages", return_value=[]):
                        apps = launcher_core.parse_desktop_files()
                    self.assertEqual(len(apps), 1)
                    with patch.object(launcher_core.shutil, "which", return_value=None), \
                            patch.object(launcher_core.subprocess, "Popen") as spawn:
                        self.assertFalse(launcher_core.launch_app(apps[0]))
                    spawn.assert_not_called()
                    self.assertFalse(marker.exists())

    def test_failed_uwsm_and_gio_never_retry_raw_exec_or_basename(self):
        app = {"desktop_path": "/tmp/example.desktop", "exec": "true; touch INJECTED"}
        for outcome in [None, Mock(launch_uris=Mock(return_value=False)),
                        Mock(launch_uris=Mock(side_effect=RuntimeError("rejected")))]:
            with self.subTest(outcome=outcome), \
                    patch.object(launcher_core.shutil, "which", return_value="/usr/bin/uwsm-app"), \
                    patch.object(launcher_core.subprocess, "Popen", side_effect=OSError) as spawn, \
                    patch.object(launcher_core.GioUnix.DesktopAppInfo, "new_from_filename", return_value=outcome):
                self.assertFalse(launcher_core.launch_app(app))
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(spawn.call_args.args[0], ["uwsm-app", "--", app["desktop_path"]])

    def test_native_success_is_preserved(self):
        app = {"desktop_path": "/tmp/example.desktop", "exec": "unused"}
        info = Mock(launch_uris=Mock(return_value=True))
        with patch.object(launcher_core.shutil, "which", return_value=None), \
                patch.object(launcher_core.GioUnix.DesktopAppInfo, "new_from_filename", return_value=info), \
                patch.object(launcher_core.subprocess, "Popen") as spawn:
            self.assertTrue(launcher_core.launch_app(app))
            info.launch_uris.assert_called_once_with([], None)
            spawn.assert_not_called()
        with patch.object(launcher_core.shutil, "which", return_value="/usr/bin/uwsm-app"), \
                patch.object(launcher_core.subprocess, "Popen") as spawn:
            self.assertTrue(launcher_core.launch_app(app))
            self.assertEqual(spawn.call_args.args[0], ["uwsm-app", "--", app["desktop_path"]])

    def test_exec_without_desktop_path_is_not_executed(self):
        with patch.object(launcher_core.subprocess, "Popen") as spawn:
            self.assertFalse(launcher_core.launch_app({"exec": "touch INJECTED"}))
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
