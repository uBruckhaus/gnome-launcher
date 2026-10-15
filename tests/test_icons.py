"""Hand-chosen icons: overrides file, slugs, bounded and checked downloads."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import icons  # noqa: E402


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class IconTests(unittest.TestCase):
    def test_slugs_use_aliases_and_names(self):
        self.assertEqual(icons.slugs_for("Codex", "codex")[:2], ["codex", "openai"])
        self.assertIn("cursor", icons.slugs_for("Cursor Agent", "cursor-agent"))
        self.assertEqual(icons.slugs_for("../../etc"), ["etc"])

    def test_fetch_only_https_on_the_cdn_with_curl_caps(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        calls = []

        def run(cmd, max_bytes, timeout, binary):
            calls.append((cmd, max_bytes))
            return svg
        good = f"https://{icons.HOST}/gh/homarr-labs/dashboard-icons/svg/codex.svg"
        with mock.patch.object(icons.shutil, "which", return_value="/usr/bin/curl"):
            self.assertEqual(icons.fetch(good, "svg", run), svg)
            cmd, cap = calls[0]
            for flag in ("--proto", "=https", "--max-redirs", "0", "--max-time", "--max-filesize", "--"):
                self.assertIn(flag, cmd)
            self.assertEqual(cmd[-1], good)
            self.assertEqual(cap, icons.MAX_DOWNLOAD_BYTES)
            for bad in ("http://cdn.jsdelivr.net/x.svg", "https://evil.example/x.svg",
                        f"https://{icons.HOST}/a/../b.svg", f"https://{icons.HOST}/x.svg?x=1", "-o/etc/x.svg"):
                self.assertIsNone(icons.fetch(bad, "svg", run))
            self.assertEqual(len(calls), 1)
            self.assertIsNone(icons.fetch(good, "svg", lambda *a, **k: b"<html>"))
            self.assertIsNone(icons.fetch(good.replace(".svg", ".png"), "png", run))  # not a PNG
            self.assertIsNone(icons.fetch(good, "svg", lambda *a, **k: None))  # curl failed or over the cap

    def test_simple_icons_are_tinted(self):
        self.assertEqual(icons.tint(b'<svg viewBox="0 0 24 24">', "#aabbcc"), b'<svg fill="#aabbcc" viewBox="0 0 24 24">')
        self.assertEqual(icons.tint(b"<svg>", '"/><script>'), b"<svg>")

    def test_overrides_round_trip_and_bad_values_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "icons.json"
            with mock.patch.object(icons, "ICON_DIR", Path(tmp) / "icons"):
                stored = icons.store_file("cli:codex", b"<svg/>", ".svg")
                icons.save({"cli:codex": stored, "a": "utilities-terminal"}, path)
                self.assertEqual(icons.load(path), {"cli:codex": stored, "a": "utilities-terminal"})
                path.write_text('{"x": "/etc/passwd", "y": "../z", "z": 5}')
                self.assertEqual(icons.load(path), {})

    def test_apply_keeps_the_default(self):
        entries = [{"desktop_id": "a", "icon": "old"}]
        icons.apply(entries, {"a": "new"})
        self.assertEqual(entries[0]["icon"], "new")
        icons.apply(entries, {})
        self.assertEqual(entries[0]["icon"], "old")


if __name__ == "__main__":
    unittest.main()
