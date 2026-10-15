"""The settings page sketches: the animation timing and the dock geometry (no display needed)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import previews  # noqa: E402


class PreviewTests(unittest.TestCase):
    def test_autohide_slides_in_and_out(self):
        self.assertEqual(previews.reveal(0.5, True), 0.0)
        self.assertEqual(previews.reveal(2.0, True), 1.0)
        self.assertEqual(previews.reveal(5.5, True), 0.0)
        self.assertTrue(0 < previews.reveal(1.15, True) < 1)
        self.assertEqual(previews.reveal(0.5, False), 1.0)

    def test_magnification_wave(self):
        self.assertEqual(previews.magnification(100, 100, 40, 1.0), 1.0)
        self.assertAlmostEqual(previews.magnification(100, 100, 40, 1.5), 1.5)
        self.assertTrue(1.0 < previews.magnification(130, 100, 40, 1.5) < 1.5)
        self.assertEqual(previews.magnification(300, 100, 40, 1.5), 1.0)

    def test_edges_map_away_from_the_screen_edge(self):
        x, y, w, h = 10, 20, 160, 90
        for edge, expect in (("bottom", (10, 110)), ("top", (10, 20)), ("left", (10, 20)), ("right", (170, 20))):
            self.assertEqual(previews.edge_matrix(edge, x, y, w, h).transform_point(0, 0), expect)
        # One step away from the edge goes inwards.
        self.assertEqual(previews.edge_matrix("bottom", x, y, w, h).transform_point(0, 5), (10, 105))
        self.assertEqual(previews.edge_matrix("right", x, y, w, h).transform_point(0, 5), (165, 20))

    def test_colours(self):
        self.assertEqual(previews.rgba("#ff0000", 0.5), (1.0, 0.0, 0.0, 0.5))
        self.assertEqual(previews.rgba("bad")[:3], (0.5, 0.5, 0.5))


if __name__ == "__main__":
    unittest.main()
