"""Menu lifetime checks without starting GTK or a desktop launcher."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class ActionsMenuTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "launcher_popup.py"
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Launcher")
        names = {"actions_menu_open", "on_actions_closed", "poll", "hide_if_inactive", "reload_apps", "on_key"}
        cls.bases = []
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.glib = SimpleNamespace(SOURCE_REMOVE=False, SOURCE_CONTINUE=True,
                                    idle_add=Mock(), timeout_add=Mock(return_value=123))
        namespace = {"GLib": self.glib, "Gdk": SimpleNamespace(KEY_Escape=27)}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
        self.app = namespace["Launcher"]()
        self.menu = Mock()
        self.menu.get_visible.return_value = True
        self.app.actions_popover = self.menu
        self.app.window = Mock()
        self.app.window.is_visible.return_value = True
        self.app.window.get_visible.return_value = True
        self.app.window.is_active.return_value = False
        self.app.refresh_running = Mock(return_value=True)
        self.app.refresh_list = Mock()
        self.app.theme = Mock()
        self.app.theme.reload.return_value = False
        self.app.hide = Mock()
        self.app.shortcut_app = None

    def test_status_refresh_resumes_after_menu_closes(self):
        self.app.poll()
        self.app.refresh_running.assert_not_called()
        self.app.refresh_list.assert_not_called()
        self.menu.get_visible.return_value = False
        self.app.poll()
        self.app.refresh_running.assert_called_once()
        self.app.refresh_list.assert_called_once()

    def test_app_directory_changes_defer_while_menu_is_open(self):
        self.app.reload_apps()
        self.glib.timeout_add.assert_called_once_with(700, self.app.reload_apps)
        self.assertEqual(self.app.apps_changed_source, 123)
        self.app.refresh_list.assert_not_called()

    def test_temporary_focus_loss_keeps_launcher_open(self):
        self.app.hide_if_inactive()
        self.app.hide.assert_not_called()
        self.menu.get_visible.return_value = False
        self.app.hide_if_inactive()
        self.app.hide.assert_called_once()

    def test_escape_dismisses_menu_without_clearing_search_or_launcher(self):
        self.app.search_query = "browser"
        self.assertTrue(self.app.on_key(None, 27, 0, 0))
        self.menu.popdown.assert_called_once()
        self.assertEqual(self.app.search_query, "browser")
        self.app.hide.assert_not_called()
        self.app.refresh_list.assert_not_called()

    def test_old_menu_cleanup_does_not_clear_new_menu(self):
        replacement = Mock()
        self.app.actions_popover = replacement
        self.app.on_actions_closed(self.menu)
        self.assertIs(self.app.actions_popover, replacement)
        self.menu.unparent.assert_not_called()
        cleanup = self.glib.idle_add.call_args.args[0]
        cleanup()
        self.menu.unparent.assert_called_once()


if __name__ == "__main__":
    unittest.main()
