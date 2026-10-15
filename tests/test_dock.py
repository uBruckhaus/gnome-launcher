"""The dock's settings, pinning and what it shows (no GTK)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dock_core  # noqa: E402


def app(i, cls=None):
    return {"desktop_id": i, "name": i.title(), "exec": i, "startup_class": cls or i}


class DockTests(unittest.TestCase):
    def test_settings_defaults_and_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(dock_core.settings(path), dock_core.DEFAULTS)
            path.write_text('{"dock_enabled": true, "dock_position": "middle", "dock_autohide": "yes", '
                            '"dock_pinned": ["a", "a", 5, ""]}')
            s = dock_core.settings(path)
            self.assertTrue(s["dock_enabled"])
            self.assertEqual(s["dock_position"], "bottom")
            self.assertFalse(s["dock_autohide"])  # "yes" is not a bool: the default.
            self.assertEqual(s["dock_pinned"], ["a"])
            with self.assertRaises(ValueError):
                dock_core.set_setting("dock_position", "middle", path)
            dock_core.set_setting("dock_position", "left", path)
            self.assertEqual(dock_core.settings(path)["dock_position"], "left")
            self.assertFalse(dock_core.settings(path)["dock_transparent"])
            dock_core.set_setting("dock_transparent", True, path)
            self.assertTrue(dock_core.settings(path)["dock_transparent"])

    def test_pin_unpin_and_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            for i in ("a", "b", "c"):
                dock_core.pin(i, path)
            dock_core.pin("a", path)
            self.assertEqual(dock_core.pinned(path), ["a", "b", "c"])
            dock_core.unpin("b", path)
            self.assertEqual(dock_core.pinned(path), ["a", "c"])
        self.assertEqual(dock_core.move_pinned(["a", "b", "c"], "c", "a"), ["c", "a", "b"])
        self.assertEqual(dock_core.move_pinned(["a", "b", "c"], "a", "c", after=True), ["b", "c", "a"])
        self.assertEqual(dock_core.move_pinned(["a", "b"], "a", "x"), ["a", "b"])

    def test_sizes_and_size_settings(self):
        dock = dict(dock_core.DEFAULTS)
        self.assertEqual(dock_core.sizes(dock), (48, 46, 48))       # The icons fit the band; no bounce room.
        dock.update(dock_icon_size=64, dock_magnify=150, dock_bounce=True)
        self.assertEqual(dock_core.sizes(dock), (48, 78, 124))      # 78 + 32 to grow into + 14 to bounce.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"dock_height": 57, "dock_icon_size": true, "dock_magnify": 150}')
            s = dock_core.settings(path)
            self.assertEqual((s["dock_height"], s["dock_icon_size"], s["dock_magnify"]), (48, 32, 150))
            with self.assertRaises(ValueError):
                dock_core.set_setting("dock_height", 57, path)
            dock_core.set_setting("dock_icon_size", 72, path)
            self.assertEqual(dock_core.settings(path)["dock_icon_size"], 72)

    def test_indicator_style_and_colour(self):
        dock = dict(dock_core.DEFAULTS)
        colors = {"accent": "#00ffcc", "red": "#ff0000", "foreground": "#eeeeee"}
        self.assertEqual((dock["dock_indicator_shown"], dock["dock_indicator"], dock["dock_indicator_color"]),
                         (True, "line", "theme"))
        self.assertEqual(dock_core.indicator_color(dock, colors), "#00ffcc")
        dock["dock_indicator_color"] = "red"
        self.assertEqual(dock_core.indicator_color(dock, colors), "#ff0000")
        dock["dock_indicator_color"] = "green"  # Not in this palette: the accent.
        self.assertEqual(dock_core.indicator_color(dock, colors), "#00ffcc")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with self.assertRaises(ValueError):
                dock_core.set_setting("dock_indicator", "star", path)
            dock_core.set_setting("dock_indicator", "dot", path)
            self.assertEqual(dock_core.settings(path)["dock_indicator"], "dot")

    def test_event_lines_are_bounded(self):
        lines, rest, skip = dock_core.split_events(b"", b"urgent>>abc\nactivewindowv2>>de", False, 64)
        self.assertEqual((lines, rest, skip), (["urgent>>abc"], b"activewindowv2>>de", False))
        lines, rest, skip = dock_core.split_events(rest, b"f\n", skip, 64)
        self.assertEqual(lines, ["activewindowv2>>def"])
        # A huge title: never buffered beyond the limit, skipped up to its end.
        buffer, skip, seen = b"", False, []
        for chunk in [b"windowtitlev2>>1,"] + [b"x" * 4096] * 50 + [b"\nurgent>>ok\n"]:
            lines, buffer, skip = dock_core.split_events(buffer, chunk, skip, 64)
            self.assertLessEqual(len(buffer), 64 + 4096)
            seen += lines
        self.assertEqual(seen, ["urgent>>ok"])

    def test_browsers_bring_their_own_new_window(self):
        self.assertTrue(dock_core.has_new_window_action({"actions": [{"id": "new-window", "name": "New Window"},
                                                                     {"id": "new-private-window"}]}))
        self.assertTrue(dock_core.has_new_window_action({"actions": [{"id": "NewWindow"}]}))
        self.assertFalse(dock_core.has_new_window_action({"actions": [{"id": "new-private-window"}]}))
        self.assertFalse(dock_core.has_new_window_action({}))

    def test_reset_switches_the_dock_off_and_keeps_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"dock_enabled": true, "dock_pinned": ["a"], "dock_height": 80, "dock_spacing": 16, "dock_autohide": true, '
                            '"dock_position": "left", "dock_full_width": true, "language": "de"}')
            dock_core.reset(path)
            s = dock_core.settings(path)
            self.assertEqual(s["dock_spacing"], 6)
            self.assertEqual((s["dock_height"], s["dock_icon_size"], s["dock_magnify"], s["dock_autohide"],
                              s["dock_full_width"], s["dock_align"], s["dock_position"], s["dock_bounce"],
                              s["dock_rounded"]),
                             (48, 32, 100, False, False, "center", "bottom", False, False))
            self.assertFalse(s["dock_enabled"])  # Off by default; switched off by the reset too.
            self.assertEqual(s["dock_pinned"], ["a"])
            self.assertIn('"language": "de"', path.read_text())

    def test_edge_never_collides_with_the_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = Path(tmp) / "shell.json"
            self.assertEqual(dock_core.bar_position(shell), "top")  # No file: Omarchy's default.
            shell.write_text('{"bar": {"position": "bottom"}}')
            self.assertEqual(dock_core.bar_position(shell), "bottom")
            shell.write_text('{"bar": {"position": "middle"}}')
            self.assertEqual(dock_core.bar_position(shell), "top")
        dock = dict(dock_core.DEFAULTS)
        self.assertEqual(dock_core.edge(dock, "top"), "bottom")
        self.assertEqual(dock_core.edge(dock, "bottom"), "top")
        dock["dock_position"] = "left"
        self.assertEqual(dock_core.edge(dock, "left"), "right")
        self.assertEqual(dock_core.edge(dock, "top"), "left")

    def test_dock_rect_from_hyprland(self):
        layers = {"HDMI-A-1": {"levels": {"0": [{"namespace": "omarchy-background", "x": 0, "y": 0, "w": 1, "h": 1}],
                                          "2": [{"namespace": "app-manager-dock", "x": 786, "y": 1004, "w": 349, "h": 76}]}}}
        monitors = [{"name": "HDMI-A-1", "x": 0, "y": 0}]
        rect, origin = dock_core.dock_rect(layers, monitors)
        self.assertEqual((rect, origin), ((786, 1004, 349, 76), (0, 0)))
        self.assertTrue(dock_core.inside({"x": 800, "y": 1050}, rect))
        self.assertFalse(dock_core.inside({"x": 700, "y": 1050}, rect))
        self.assertIsNone(dock_core.dock_rect({"X": {"levels": {}}}, monitors))
        self.assertIsNone(dock_core.dock_rect(None, monitors))

    def test_drops_from_app_manager(self):
        self.assertEqual(dock_core.dropped_id(b"app-manager-entry:firefox"), "firefox")
        for bad in (b"firefox", b"app-manager-entry:", b"app-manager-entry:a\nb", b"\xff\xfe",
                    b"app-manager-entry:" + b"x" * 600, "app-manager-entry:str"):
            self.assertIsNone(dock_core.dropped_id(bad))
        self.assertEqual(dock_core.insert_pinned(["a", "b"], "c", "b"), ["a", "c", "b"])
        self.assertEqual(dock_core.insert_pinned(["a", "b"], "c", "a", after=True), ["a", "c", "b"])
        self.assertEqual(dock_core.insert_pinned(["a", "b"], "a"), ["b", "a"])
        self.assertEqual(dock_core.insert_pinned([], "a", "x"), ["a"])

    def test_items_pinned_first_then_running(self):
        apps = [app("foot"), app("firefox"), app("zed", "dev.zed.Zed")]
        clients = [{"class": "dev.zed.Zed", "address": "0x2", "focusHistoryID": 1},
                   {"class": "firefox", "address": "0x1", "focusHistoryID": 0}]
        items = dock_core.items(apps, ["foot", "firefox", "gone"], clients)
        self.assertEqual([(a["desktop_id"], [w["address"] for w in ws], p) for a, ws, p in items],
                         [("foot", [], True), ("firefox", ["0x1"], True), ("zed", ["0x2"], False)])


if __name__ == "__main__":
    unittest.main()
