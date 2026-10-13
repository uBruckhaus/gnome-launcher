"""Every interface text in the sources has a translation in every language."""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import i18n  # noqa: E402

SOURCES = ("launcher_popup.py", "launcher_core.py", "tray.py")


def msgids():
    ids, plural = set(), set()
    for name in SOURCES:
        for node in ast.walk(ast.parse((ROOT / name).read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if node.func.id == "_":
                        ids.add(first.value)
                    elif node.func.id == "ngettext":
                        plural.add(first.value)
    return ids, plural


class TranslationTests(unittest.TestCase):
    def test_every_text_is_translated_with_the_same_placeholders(self):
        ids, plural = msgids()
        holes = lambda text: sorted(re.findall(r"\{\d*\}", text))
        for code, _name in i18n.LANGUAGES[1:]:
            table = i18n.STRINGS[code]
            for msgid in ids:
                self.assertIn(msgid, table, f"{code}: {msgid!r}")
                self.assertEqual(holes(table[msgid]), holes(msgid), f"{code}: {msgid!r}")
            for msgid in plural:
                self.assertIsInstance(table.get(i18n.PLURAL_PREFIX + msgid), list, f"{code}: {msgid!r}")

    def test_polish_plurals(self):
        i18n._lang = "pl"
        try:
            self.assertEqual(i18n.ngettext("{0} changed file", "{0} changed files", 3).format(3), "3 zmienione pliki")
            self.assertEqual(i18n.ngettext("{0} changed file", "{0} changed files", 5).format(5), "5 zmienionych plików")
        finally:
            i18n._lang = None


if __name__ == "__main__":
    unittest.main()
