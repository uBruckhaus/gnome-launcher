"""Open in floating window: the command, its Lua quoting, and the filter field setting."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import desktop_generic  # noqa: E402
import desktop_omarchy  # noqa: E402
import launcher_core  # noqa: E402


class LuaStringTests(unittest.TestCase):
    def test_every_byte_is_escaped(self):
        text = 'x"); os.execute("rm") --\n]]ö'
        literal = desktop_omarchy.lua_string(text)
        self.assertRegex(literal, r'^"(\\\d{3})+"$')
        decoded = bytes(int(n) for n in literal[2:-1].split("\\"))
        self.assertEqual(decoded.decode(), text)

    def test_exec_floating_quotes_the_words(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok\n")
            self.assertTrue(desktop_omarchy.exec_floating(["uwsm-app", "--", "/a b/c'd.desktop"]))
        code = run.call_args.args[0][2]
        self.assertTrue(code.startswith("hl.exec_cmd(") and code.endswith(", {float = true})"))
        literal = code[len("hl.exec_cmd("):-len(", {float = true})")]
        command = bytes(int(n) for n in literal[2:-1].split("\\")).decode()
        self.assertEqual(command, "uwsm-app -- '/a b/c'\"'\"'d.desktop'")

    def test_generic_desktop_does_not_float(self):
        self.assertFalse(desktop_generic.can_float())
        self.assertFalse(desktop_generic.exec_floating(["true"]))


class FloatingArgvTests(unittest.TestCase):
    def test_appimage_runs_directly(self):
        self.assertEqual(launcher_core.floating_argv({"argv": ["/x/a.AppImage"]}), ["/x/a.AppImage"])
        self.assertIsNone(launcher_core.floating_argv({"argv": ["/x/a.AppImage"]}, "new-window"))

    def test_desktop_file_through_uwsm(self):
        with mock.patch("shutil.which", return_value="/usr/bin/uwsm-app"):
            argv = launcher_core.floating_argv({"desktop_path": "/usr/share/applications/foot.desktop"}, "server")
        self.assertEqual(argv, ["uwsm-app", "--", "/usr/share/applications/foot.desktop:server"])

    def test_nothing_to_start(self):
        self.assertIsNone(launcher_core.floating_argv({"name": "x"}))


class FilterFieldSettingTests(unittest.TestCase):
    def test_on_by_default_and_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertTrue(launcher_core.filter_field_shown(path))
            launcher_core.set_filter_field_shown(False, path)
            self.assertFalse(launcher_core.filter_field_shown(path))
            launcher_core.set_filter_field_shown(True, path)
            self.assertTrue(launcher_core.filter_field_shown(path))


if __name__ == "__main__":
    unittest.main()
