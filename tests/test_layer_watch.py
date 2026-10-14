"""Closing for Omarchy's menus: which Hyprland events count."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import layer_watch  # noqa: E402

OWN = "simple-launcher-for-fools"


class LayerWatchTests(unittest.TestCase):
    def test_menus_count_others_do_not(self):
        data = (b"activewindow>>foot,~\nopenlayer>>omarchy-notifications\nopenlayer>>omarchy-osd\n"
                b"openlayer>>omarchy-polkit\nopenlayer>>simple-launcher-for-fools\nopenlayer>>omarchy-menu\n"
                b"closelayer>>omarchy-menu\nopenlayer>>theme-manager\n")
        found, rest = layer_watch.opened_menus(data, b"", OWN)
        self.assertEqual(found, ["omarchy-menu", "theme-manager"])
        self.assertEqual(rest, b"")

    def test_lines_split_across_reads(self):
        found, rest = layer_watch.opened_menus(b"workspace>>2\nopenlayer>>omarchy-me", b"", OWN)
        self.assertEqual((found, rest), ([], b"openlayer>>omarchy-me"))
        found, rest = layer_watch.opened_menus(b"nu\n", rest, OWN)
        self.assertEqual(found, ["omarchy-menu"])

    def test_endless_line_is_bounded(self):
        rest = b""
        for _ in range(100):
            found, rest = layer_watch.opened_menus(b"x" * 4096, rest, OWN)
            self.assertLessEqual(len(rest), layer_watch.MAX_PENDING)
        found, rest = layer_watch.opened_menus(b"\nopenlayer>>walker\n", rest, OWN)
        self.assertEqual(found, ["walker"])


if __name__ == "__main__":
    unittest.main()
