"""Dropping on a group chip: only our own card drags, the drop data is never read."""
import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def launcher_methods(*names):
    tree = ast.parse((ROOT / "launcher_popup.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Launcher")
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    gdk = Mock()
    gdk.DragAction.COPY = 1
    namespace = {"Gdk": gdk, "GLib": Mock(), "Gtk": Mock()}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "launcher_popup.py", "exec"), namespace)
    return namespace["Launcher"]


class CardDropTests(unittest.TestCase):
    def setUp(self):
        self.app = launcher_methods("own_card_drag", "on_chip_drop", "on_tile_drag_begin", "on_tile_drag_end")()
        self.app.put_in_group = Mock()
        self.app.filter_bar = Mock()
        self.app.search_query = ""
        self.app.mode = "apps"
        self.app.overview_filter = {"apps": "all", "plugins": "all"}

    def test_foreign_drop_is_refused_without_reading_it(self):
        drop = Mock()
        self.assertFalse(self.app.on_chip_drop(None, drop, 0, 0, "media"))
        drop.finish.assert_called_once_with(0)
        drop.read_async.assert_not_called()
        drop.read_value_async.assert_not_called()
        self.app.put_in_group.assert_not_called()

    def test_own_card_moves_to_the_group(self):
        card = {"desktop_id": "vlc", "name": "VLC"}
        self.app.on_tile_drag_begin(Mock(), Mock(), card)
        drop = Mock()
        self.assertTrue(self.app.on_chip_drop(None, drop, 0, 0, "media"))
        self.app.put_in_group.assert_called_once_with(card, "media")
        drop.read_async.assert_not_called()
        self.app.on_tile_drag_end()
        self.assertFalse(self.app.own_card_drag(drop))

    def test_drop_while_typing_shows_the_group_afterwards(self):
        card = {"desktop_id": "vlc", "name": "VLC"}
        self.app.search_query = "vl"
        self.app.on_tile_drag_begin(Mock(), Mock(), card)
        self.app.on_chip_drop(None, Mock(), 0, 0, "media")
        self.assertEqual(self.app.overview_filter["apps"], "media")

    def test_drop_without_typing_keeps_the_filter(self):
        self.app.on_tile_drag_begin(Mock(), Mock(), {"desktop_id": "vlc", "name": "VLC"})
        self.app.on_chip_drop(None, Mock(), 0, 0, "media")
        self.assertEqual(self.app.overview_filter["apps"], "all")

    def test_tray_icons_have_no_group(self):
        self.app.on_tile_drag_begin(Mock(), Mock(), {"desktop_id": "x", "tray_id": "x"})
        drop = Mock()
        self.assertFalse(self.app.on_chip_drop(None, drop, 0, 0, "media"))
        self.app.put_in_group.assert_not_called()


if __name__ == "__main__":
    unittest.main()
