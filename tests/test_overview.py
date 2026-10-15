"""Popup width -> list or card columns, and the saved size, without starting GTK."""
import ast
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher_core  # noqa: E402

NAMES = {"CARD_WIDTH", "TILE_WIDTH", "TILE_MAX_WIDTH", "TILE_SPACING", "MAX_COLUMNS", "CARD_PADDING",
         "MAX_CARD_WIDTH", "grid_columns", "FOLDER_COLUMNS", "FOLDER_ROWS", "FOLDER_ICON",
         "FOLDER_ICON_MAX", "FOLDER_CELL", "FOLDER_NAME_HEIGHT", "FOLDER_TILE_WIDTH", "folder_columns",
         "folder_tile_size", "folder_fit"}


def load():
    tree = ast.parse((ROOT / "launcher_popup.py").read_text())
    body = [n for n in tree.body
            if (isinstance(n, ast.Assign) and n.targets[0].id in NAMES if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name) else False)
            or (isinstance(n, ast.FunctionDef) and n.name in NAMES)]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "launcher_popup.py", "exec"), namespace)
    return namespace


class FolderFitTests(unittest.TestCase):
    def test_tile_size_matches_the_narrowest_folder(self):
        ns = load()
        self.assertEqual(ns["folder_tile_size"](ns["FOLDER_ICON"])[0], ns["FOLDER_TILE_WIDTH"])

    def test_few_folders_grow_to_fill_the_page(self):
        ns = load()
        columns, icon = ns["folder_fit"](4, 1200, 800)
        self.assertGreater(icon, ns["FOLDER_ICON"])
        self.assertLessEqual(icon, ns["FOLDER_ICON_MAX"])
        rows = -(-4 // columns)
        width, height = ns["folder_tile_size"](icon)
        self.assertLessEqual(columns * width + (columns - 1) * ns["TILE_SPACING"], 1200 - ns["CARD_PADDING"])
        self.assertLessEqual(rows * height + (rows - 1) * ns["TILE_SPACING"], 800)

    def test_more_folders_shrink_and_a_wider_page_grows(self):
        ns = load()
        self.assertGreaterEqual(ns["folder_fit"](4, 1000, 700)[1], ns["folder_fit"](8, 1000, 700)[1])
        self.assertGreaterEqual(ns["folder_fit"](6, 1400, 900)[1], ns["folder_fit"](6, 1000, 700)[1])

    def test_a_wide_page_puts_few_folders_in_one_row(self):
        ns = load()
        self.assertEqual(ns["folder_fit"](3, 1600, 400)[0], 3)

    def test_too_many_keep_the_smallest_size_and_scroll(self):
        ns = load()
        self.assertEqual(ns["folder_fit"](40, 600, 400), (ns["folder_columns"](600), ns["FOLDER_ICON"]))
        self.assertEqual(ns["folder_fit"](0, 600, 400)[1], ns["FOLDER_ICON"])


class OverviewTests(unittest.TestCase):
    def test_default_width_is_the_list(self):
        ns = load()
        self.assertEqual(ns["grid_columns"](ns["CARD_WIDTH"]), 1)

    def test_two_cards_switch_to_the_overview(self):
        ns = load()
        two = ns["CARD_PADDING"] + 2 * ns["TILE_WIDTH"] + ns["TILE_SPACING"]
        self.assertEqual(ns["grid_columns"](two - 1), 1)
        self.assertEqual(ns["grid_columns"](two), 2)

    def test_columns_are_capped(self):
        ns = load()
        self.assertEqual(ns["grid_columns"](ns["MAX_CARD_WIDTH"]), ns["MAX_COLUMNS"])
        self.assertEqual(ns["grid_columns"](10000), ns["MAX_COLUMNS"])

    def test_saved_size_round_trip_and_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertIsNone(launcher_core.saved_size("size", path))
            launcher_core.set_saved_size("size", (900, 640), path)
            self.assertEqual(launcher_core.saved_size("size", path), (900, 640))
            launcher_core.set_saved_size("size", None, path)
            self.assertIsNone(launcher_core.saved_size("size", path))
            path.write_text('{"size": [true, 5]}')
            self.assertIsNone(launcher_core.saved_size("size", path))

    def test_wide_layout_defaults_to_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(launcher_core.saved_wide_layout(path), "folders")
            launcher_core.set_wide_layout("cards", path)
            self.assertEqual(launcher_core.saved_wide_layout(path), "cards")
            path.write_text('{"wide_layout": "tiles"}')
            self.assertEqual(launcher_core.saved_wide_layout(path), "folders")

    def test_small_layout_defaults_to_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(launcher_core.saved_view(path), "list")
            launcher_core.set_view("groups", path)
            self.assertEqual(launcher_core.saved_view(path), "groups")

    def test_view_buttons_are_off_unless_switched_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertFalse(launcher_core.view_buttons_shown(path))
            launcher_core.set_view_buttons_shown(True, path)
            self.assertTrue(launcher_core.view_buttons_shown(path))


if __name__ == "__main__":
    unittest.main()
