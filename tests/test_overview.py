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
         "MAX_CARD_WIDTH", "grid_columns"}


def load():
    tree = ast.parse((ROOT / "launcher_popup.py").read_text())
    body = [n for n in tree.body
            if (isinstance(n, ast.Assign) and n.targets[0].id in NAMES if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name) else False)
            or (isinstance(n, ast.FunctionDef) and n.name in NAMES)]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "launcher_popup.py", "exec"), namespace)
    return namespace


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


if __name__ == "__main__":
    unittest.main()
