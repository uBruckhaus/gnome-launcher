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
                            patch.object(launcher_core, "scan_standalone_appimages", return_value=[]), \
                            patch.object(launcher_core.cli_tools, "cli_entries", return_value=[]):
                        apps = launcher_core.parse_desktop_files()
                    self.assertEqual(len(apps), 1)
                    with patch.object(launcher_core.shutil, "which", return_value=None), \
                            patch.object(launcher_core.subprocess, "Popen") as spawn:
                        self.assertFalse(launcher_core.launch_app(apps[0]))
                    spawn.assert_not_called()
                    self.assertFalse(marker.exists())

    def test_failed_gio_never_retries_raw_exec_or_basename(self):
        app = {"desktop_path": "/tmp/example.desktop", "exec": "true; touch INJECTED"}
        for outcome in [None, Mock(launch_uris=Mock(return_value=False)),
                        Mock(launch_uris=Mock(side_effect=RuntimeError("rejected")))]:
            with self.subTest(outcome=outcome), \
                    patch.object(launcher_core.subprocess, "Popen") as spawn, \
                    patch.object(launcher_core.GioUnix.DesktopAppInfo, "new_from_filename", return_value=outcome):
                self.assertFalse(launcher_core.launch_app(app))
                spawn.assert_not_called()

    def test_native_success_passes_the_launch_context(self):
        app = {"desktop_path": "/tmp/example.desktop", "exec": "unused"}
        info = Mock(launch_uris=Mock(return_value=True))
        context = object()
        with patch.object(launcher_core.GioUnix.DesktopAppInfo, "new_from_filename", return_value=info), \
                patch.object(launcher_core.subprocess, "Popen") as spawn:
            self.assertTrue(launcher_core.launch_app(app, context))
            info.launch_uris.assert_called_once_with([], context)
            spawn.assert_not_called()

    def test_actions_run_through_gio_with_the_context(self):
        app = {"desktop_path": "/tmp/example.desktop"}
        info = Mock(list_actions=Mock(return_value=["new-window"]))
        context = object()
        with patch.object(launcher_core.GioUnix.DesktopAppInfo, "new_from_filename", return_value=info):
            self.assertTrue(launcher_core.launch_action(app, "new-window", context))
            info.launch_action.assert_called_once_with("new-window", context)
            self.assertFalse(launcher_core.launch_action(app, "missing", context))

    def test_exec_without_desktop_path_is_not_executed(self):
        with patch.object(launcher_core.subprocess, "Popen") as spawn:
            self.assertFalse(launcher_core.launch_app({"exec": "touch INJECTED"}))
        spawn.assert_not_called()

class BrowserWebAppTest(unittest.TestCase):
    def test_chrome_web_apps_are_recognised(self):
        chrome = {"exec": "/opt/google/chrome/google-chrome --profile-directory=Default --app-id=mbfdd"}
        self.assertEqual(launcher_core.browser_web_app(chrome), "Chrome")
        self.assertEqual(launcher_core.browser_web_app({"exec": "brave --app-id=x"}), "Brave")
        self.assertEqual(launcher_core.browser_web_app({"exec": "odd-browser --app-id=x"}), "the browser")
        for other in ("/opt/google/chrome/google-chrome", "omarchy-launch-webapp https://x.y", "", '"'):
            self.assertIsNone(launcher_core.browser_web_app({"exec": other}))


if __name__ == "__main__":
    unittest.main()
