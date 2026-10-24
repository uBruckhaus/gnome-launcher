"""The settings page sketch: the animation helpers (no display needed)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import previews  # noqa: E402


class PreviewTests(unittest.TestCase):
    def test_easing_is_clamped(self):
        self.assertEqual(previews.ease(-1), 0.0)
        self.assertEqual(previews.ease(2), 1.0)
        self.assertTrue(0 < previews.ease(0.5) < 1)

    def test_colours(self):
        self.assertEqual(previews.rgba("#ff0000", 0.5), (1.0, 0.0, 0.0, 0.5))
        self.assertEqual(previews.rgba("bad")[:3], (0.5, 0.5, 0.5))


if __name__ == "__main__":
    unittest.main()
