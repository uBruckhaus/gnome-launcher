"""Open as administrator: which apps get it and the exact argv, never a shell."""
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

import launcher_core

ENV = {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/user/1000", "DISPLAY": ":0",
       "XDG_SESSION_TYPE": "wayland"}
DISPLAY = ["WAYLAND_DISPLAY=/run/user/1000/wayland-1", "XDG_SESSION_TYPE=wayland", "DISPLAY=:0"]


def which(name):
    return name if name.startswith("/") else f"/usr/bin/{name}"


def entry(exec_line, **extra):
    return {"name": "App", "desktop_path": "/usr/share/applications/app.desktop", "exec": exec_line, **extra}


class AdminCommandTests(unittest.TestCase):
    def setUp(self):
        for target in (patch.dict(os.environ, ENV, clear=True),
                       patch.object(launcher_core.os, "geteuid", return_value=1000),
                       patch.object(launcher_core.shutil, "which", side_effect=which),
                       patch.object(launcher_core, "_uses_electron", return_value=False),
                       patch.object(launcher_core, "_desktop_look", return_value={}),
                       patch.object(launcher_core, "_display_scale", return_value=None),
                       patch.object(launcher_core, "can_admit_root_on_display", return_value=True)):
            target.start()
            self.addCleanup(target.stop)

    def test_graphical_app_goes_through_pkexec_with_display_only(self):
        self.assertEqual(launcher_core.admin_command(entry("gnome-disks --block-device=/dev/sda")),
                         ["pkexec", "env", *DISPLAY, "/usr/bin/gnome-disks", "--block-device=/dev/sda"])

    def test_user_runtime_dir_is_not_handed_to_root(self):
        argv = launcher_core.admin_command(entry("gnome-disks"))
        self.assertFalse(any(a.startswith("XDG_RUNTIME_DIR=") for a in argv))

    def test_electron_app_uses_xwayland_after_admitting_root(self):
        with patch.object(launcher_core, "_uses_electron", return_value=True), \
                patch.object(launcher_core, "allow_root_on_display") as allow, \
                patch.object(launcher_core.subprocess, "Popen") as spawn:
            argv = launcher_core.admin_command(entry("/opt/Kudu/kudu"))
            self.assertEqual(argv[-3:], ["/opt/Kudu/kudu", "--no-sandbox", "--ozone-platform=x11"])
            launcher_core.launch_as_admin(entry("/opt/Kudu/kudu"))
        allow.assert_called_once_with()
        spawn.assert_called_once()

    def test_root_app_gets_the_users_scale_and_dark_theme(self):
        look = {"GDK_SCALE": "2", "QT_QPA_PLATFORMTHEME": "gtk3", "HOME": "/home/u"}
        with patch.dict(os.environ, look), \
                patch.object(launcher_core, "_desktop_look",
                             return_value={"GTK_THEME": "Adwaita-dark", "ADW_DEBUG_COLOR_SCHEME": "prefer-dark"}):
            argv = launcher_core.admin_command(entry("gnome-disks"))
        self.assertEqual(argv[2:9], [*DISPLAY, "GDK_SCALE=2", "QT_QPA_PLATFORMTHEME=gtk3", "GTK_THEME=Adwaita-dark",
                                     "ADW_DEBUG_COLOR_SCHEME=prefer-dark"])
        self.assertFalse(any(a.startswith("HOME=") for a in argv))

    def test_electron_app_on_xwayland_gets_the_session_scale(self):
        with patch.object(launcher_core, "_uses_electron", return_value=True), \
                patch.object(launcher_core, "_display_scale", return_value="2"):
            self.assertEqual(launcher_core.admin_command(entry("/opt/Kudu/kudu"))[-2:],
                             ["--ozone-platform=x11", "--force-device-scale-factor=2"])

    def test_files_opens_root_files_through_gvfs_admin_as_the_user(self):
        with patch.object(launcher_core.Path, "exists", return_value=True):
            self.assertEqual(launcher_core.admin_command(entry("nautilus --new-window")),
                             ["/usr/bin/nautilus", "--new-window", "admin:///"])
        with patch.object(launcher_core.Path, "exists", return_value=False):
            self.assertIsNone(launcher_core.admin_command(entry("nautilus --new-window")))

    def test_non_electron_app_leaves_the_display_alone(self):
        with patch.object(launcher_core, "allow_root_on_display") as allow, \
                patch.object(launcher_core.subprocess, "Popen"):
            launcher_core.launch_as_admin(entry("gnome-disks"))
        allow.assert_not_called()

    def test_electron_app_gets_no_sandbox_and_wayland_hint_without_libx11(self):
        with patch.object(launcher_core, "_uses_electron", return_value=True), \
                patch.object(launcher_core, "can_admit_root_on_display", return_value=False):
            self.assertEqual(launcher_core.admin_command(entry("/opt/Kudu/kudu")),
                             ["pkexec", "env", *DISPLAY, "/opt/Kudu/kudu", "--no-sandbox",
                              "--ozone-platform-hint=auto"])
            self.assertEqual(launcher_core.admin_command(entry("/opt/Kudu/kudu --ozone-platform=x11"))[-2:],
                             ["--ozone-platform=x11", "--no-sandbox"])

    def test_shell_text_stays_one_argument(self):
        argv = launcher_core.admin_command(entry("gedit '$(touch INJECTED); x'"))
        self.assertEqual(argv[-1], "$(touch INJECTED); x")
        self.assertNotIn("sh", [Path(a).name for a in argv])

    def test_terminal_entry_runs_sudo_in_a_terminal(self):
        self.assertEqual(launcher_core.admin_command(entry("btop", terminal=True)),
                         ["xdg-terminal-exec", "sudo", "--", "/usr/bin/btop"])

    def test_terminal_launcher_puts_sudo_before_its_command(self):
        self.assertEqual(launcher_core.admin_command(entry("foot --app-id=claude-cli -e claude")),
                         ["foot", "--app-id=claude-cli", "-e", "sudo", "--", "/usr/bin/claude"])

    def test_bare_terminal_opens_a_root_shell(self):
        self.assertEqual(launcher_core.admin_command(entry("foot")), ["foot", "-e", "sudo", "-i"])

    def test_appimage_uses_its_path(self):
        app = {"name": "Tool", "argv": ["/home/u/Applications/Tool.AppImage"]}
        self.assertEqual(launcher_core.admin_command(app),
                         ["pkexec", "env", *DISPLAY, "/home/u/Applications/Tool.AppImage"])

    def test_apps_that_cannot_run_as_root_get_nothing(self):
        refused = [
            entry("omarchy-launch-webapp https://example.com"),
            entry("chromium --app=https://example.com"),
            entry("google-chrome --app-id=abc"),
            entry("xdg-open http://localhost:631/"),
            entry("flatpak run org.example.App"),
            entry("/snap/bin/app"),
            entry("app", sandboxed=True),
            entry("app", pwa=True),
            {"name": "Widget", "plugin_id": "x.y", "exec": "omarchy-shell shell toggle x.y"},
            {"name": "Tray", "tray_id": "1", "exec": ""},
            {"name": "App Manager", "self_launcher": True, "exec": "launcher-toggle"},
        ]
        for app in refused:
            with self.subTest(exec=app.get("exec")):
                self.assertIsNone(launcher_core.admin_command(app))

    def test_nothing_when_already_root_or_without_a_display(self):
        with patch.object(launcher_core.os, "geteuid", return_value=0):
            self.assertIsNone(launcher_core.admin_command(entry("gnome-disks")))
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(launcher_core.admin_command(entry("gnome-disks")))

    def test_launch_spawns_the_argv_directly(self):
        with patch.object(launcher_core.subprocess, "Popen") as spawn:
            launcher_core.launch_as_admin(entry("gnome-disks"))
        self.assertEqual(spawn.call_args.args[0], ["pkexec", "env", *DISPLAY, "/usr/bin/gnome-disks"])
        self.assertNotIn("shell", spawn.call_args.kwargs)


class ElectronDetectionTests(unittest.TestCase):
    def test_chromium_files_next_to_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "app"
            binary.write_bytes(b"\x7fELF")
            self.assertFalse(launcher_core._uses_electron(str(binary)))
            (Path(tmp) / "chrome-sandbox").write_bytes(b"")
            self.assertTrue(launcher_core._uses_electron(str(binary)))

    def test_launcher_script_starting_electron(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "code"
            script.write_text("#!/bin/sh\nexec electron37 /usr/lib/code/out/cli.js \"$@\"\n")
            self.assertTrue(launcher_core._uses_electron(str(script)))


if __name__ == "__main__":
    unittest.main()


class DesktopLookTests(unittest.TestCase):
    def gsettings(self, values):
        def read(argv, **_kwargs):
            return f"'{values.get(argv[-1], '')}'\n"
        return patch.object(launcher_core, "read_bounded_output", side_effect=read)

    def test_dark_preference_reaches_gtk_and_libadwaita(self):
        with patch.dict(os.environ, {}, clear=True), \
                self.gsettings({"color-scheme": "prefer-dark", "gtk-theme": "Adwaita"}):
            self.assertEqual(launcher_core._desktop_look(),
                             {"GTK_THEME": "Adwaita:dark", "ADW_DEBUG_COLOR_SCHEME": "prefer-dark"})

    def test_light_theme_stays_as_is_and_odd_names_are_dropped(self):
        with patch.dict(os.environ, {}, clear=True), self.gsettings({"gtk-theme": "Arc"}):
            self.assertEqual(launcher_core._desktop_look(), {"GTK_THEME": "Arc"})
        with patch.dict(os.environ, {}, clear=True), self.gsettings({"gtk-theme": "x; rm -rf /"}):
            self.assertEqual(launcher_core._desktop_look(), {})

    def test_display_scale(self):
        for value, expected in [("2", "2"), ("1.5", "1.5"), ("1", None), ("9", None), ("abc", None)]:
            with self.subTest(value=value), patch.dict(os.environ, {"GDK_SCALE": value}):
                self.assertEqual(launcher_core._display_scale(), expected)


class RootDisplayAccessTests(unittest.TestCase):
    def test_no_libx11_or_display_means_no_grant(self):
        with patch.object(launcher_core, "_libx11", return_value=None), patch.dict(os.environ, {"DISPLAY": ":0"}):
            self.assertFalse(launcher_core.can_admit_root_on_display())
            self.assertFalse(launcher_core.allow_root_on_display())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(launcher_core.can_admit_root_on_display())

    def test_grant_names_only_the_local_root_user(self):
        lib = unittest.mock.Mock()
        lib.XOpenDisplay.return_value = 1234
        seen = {}

        def add_host(_display, host):
            entry = host._obj
            address = launcher_core.ctypes.cast(
                entry.address, launcher_core.ctypes.POINTER(launcher_core._XServerInterpretedAddress)).contents
            seen.update(family=entry.family, type=address.type, value=address.value)
        lib.XAddHost.side_effect = add_host
        with patch.object(launcher_core, "_libx11", return_value=lib), patch.dict(os.environ, {"DISPLAY": ":0"}):
            self.assertTrue(launcher_core.allow_root_on_display())
        self.assertEqual(seen, {"family": 5, "type": b"localuser", "value": b"root"})
        lib.XOpenDisplay.assert_called_once_with(b":0")
        lib.XCloseDisplay.assert_called_once_with(1234)


class AdminSettingTests(unittest.TestCase):
    def test_off_by_default_and_switchable_keeping_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertFalse(launcher_core.admin_action_enabled(path))
            path.write_text('{"language": "de", "shortcut_default_mods": ["SUPER"]}')
            self.assertFalse(launcher_core.admin_action_enabled(path))
            launcher_core.set_admin_action(True, path)
            self.assertTrue(launcher_core.admin_action_enabled(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            launcher_core.set_admin_action(False, path)
            self.assertFalse(launcher_core.admin_action_enabled(path))
            data = __import__("json").loads(path.read_text())
            self.assertEqual(data["language"], "de")
            self.assertEqual(data["shortcut_default_mods"], ["SUPER"])

    def test_only_a_real_true_enables_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            for text in ('{"admin_action": "yes"}', '{"admin_action": 1}', "not json", "[]"):
                path.write_text(text)
                self.assertFalse(launcher_core.admin_action_enabled(path), text)
