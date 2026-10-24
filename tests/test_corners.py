"""The Corners setting: the choices and their radius."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import corners  # noqa: E402


class CornersTests(unittest.TestCase):
    def test_radius_per_choice(self):
        self.assertEqual(corners.radius("windows", 12), 12)
        self.assertEqual(corners.radius("windows", None), 0)
        self.assertEqual(corners.radius("rounded", 0), corners.ROUNDED)
        self.assertEqual(corners.radius("square", 12), 0)
        self.assertEqual(corners.radius("bogus", 6), 6)  # Unknown values read as the default.


if __name__ == "__main__":
    unittest.main()
