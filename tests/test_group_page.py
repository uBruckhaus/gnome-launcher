"""The group name page and group menu actions, without starting GTK."""
import ast
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import i18n  # noqa: E402
import groups  # noqa: E402


class GroupPageTests(unittest.TestCase):
    def setUp(self):
        i18n._lang = "en"
        source = ROOT / "launcher_popup.py"
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Launcher")
        names = {"save_group_name", "save_groups", "put_in_group", "toggle_group", "move_group"}
        cls.bases = []
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        namespace = {"groups": groups, "_": i18n._, "set_view": Mock()}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
        self.set_view = namespace["set_view"]
        self.app = namespace["Launcher"]()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "groups.json"
        patcher = patch.object(groups, "GROUPS_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app.groups_all = groups.default_data()
        self.app.groups = self.app.groups_all["apps"]
        self.app.view = "list"
        self.app.group_entry = Mock()
        self.app.group_error = Mock()
        for name in ("refresh_list", "close_aux_page", "load_plugins", "update_hidden_button_state", "notify"):
            setattr(self.app, name, Mock())

    def tearDown(self):
        i18n._lang = None
        self.tmp.cleanup()

    def test_new_group_for_an_app_is_saved_and_shown(self):
        self.app.group_name_target = (None, {"desktop_id": "vlc", "name": "VLC"})
        self.app.group_entry.get_text.return_value = "Work"
        self.app.save_group_name()
        saved = groups.load(self.path)["apps"]
        gid = saved["groups"][-1]["id"]
        self.assertEqual(saved["groups"][-1]["name"], "Work")
        self.assertEqual(saved["members"], {"vlc": gid})
        self.assertEqual(self.app.view, "groups")
        self.app.close_aux_page.assert_called_once_with(refresh=False)
        self.app.refresh_list.assert_called_once_with(selected_id="vlc")

    def test_duplicate_name_shows_the_error_and_stays(self):
        self.app.group_name_target = (None, None)
        self.app.group_entry.get_text.return_value = "office"
        self.app.save_group_name()
        self.app.group_error.set_label.assert_called_once_with("A group with this name exists already.")
        self.app.close_aux_page.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_rename(self):
        self.app.group_name_target = ("media", None)
        self.app.group_entry.get_text.return_value = "Fun"
        self.app.save_group_name()
        self.assertEqual(groups.display_name("media", groups.load(self.path)["apps"]), "Fun")


if __name__ == "__main__":
    unittest.main()
