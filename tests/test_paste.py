"""Pasting into the group name field reads the clipboard bounded."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher_core  # noqa: E402


class PasteTests(unittest.TestCase):
    def run_with(self, replies):
        calls = []

        def fake(command, max_bytes=0, timeout=0, binary=False):
            calls.append((command, max_bytes, timeout))
            return replies.get(command[-1])
        with mock.patch.object(launcher_core, "read_bounded_output", fake), \
             mock.patch.object(launcher_core.shutil, "which", return_value="/usr/bin/wl-paste"):
            return launcher_core.clipboard_text(40), calls

    def test_text_is_cleaned_and_capped(self):
        text, calls = self.run_with({"--list-types": "image/png\ntext/plain\n",
                                     "text/plain": "Work\r\n\x1b[31mstuff‮ " + "x" * 100})
        self.assertNotIn("\x1b", text)
        self.assertLessEqual(len(text), 40)
        self.assertTrue(text.startswith("Work [31mstuff"))
        self.assertTrue(all(c[1] <= launcher_core.MAX_PASTE_TEXT_BYTES or c[0][-1] == "--list-types" for c in calls))
        self.assertTrue(all(c[2] <= 2.0 for c in calls))

    def test_empty_clipboard_and_oversized_offer(self):
        self.assertEqual(self.run_with({"--list-types": None})[0], "")       # wl-paste fails when empty
        self.assertEqual(self.run_with({"--list-types": "image/png\n"})[0], "")
        self.assertIsNone(self.run_with({"--list-types": "text/plain\n", "text/plain": None})[0])


if __name__ == "__main__":
    unittest.main()
