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


DASH = {"visual-studio-code": {"base": "svg", "aliases": ["vscode", "code"]},
        "google-maps": {"base": "png", "aliases": []},
        "../../etc/passwd": {"base": "svg"}, "Bad Slug": {}, "spotify": "not a dict"}
SELF = [{"Name": "Home Assistant", "Reference": "home-assistant", "SVG": "Yes", "PNG": "Yes", "Tags": "smart home"},
        {"Name": "No files", "Reference": "nofiles", "SVG": "No", "PNG": "No"},
        {"Reference": "x/../y", "SVG": "Yes"}, "junk"]
SIMPLE = [{"title": "Spotify", "slug": "spotify", "aliases": {"aka": ["Spotify Music"]}},
          {"title": "Dotted", "slug": "a.b"}, {"title": "No slug"}]


class IconIndexTests(unittest.TestCase):
    def entries(self):
        return (icons.index_entries("dashboard", DASH) + icons.index_entries("selfhst", SELF)
                + icons.index_entries("simple", SIMPLE))

    def test_malformed_entries_are_skipped_and_urls_stay_on_the_cdn(self):
        slugs = {(c, s) for c, s, _u, _k, _n in self.entries()}
        self.assertEqual(slugs, {("dashboard", "visual-studio-code"), ("dashboard", "google-maps"),
                                 ("selfhst", "home-assistant"), ("simple", "spotify")})
        for _c, _s, url, kind, _n in self.entries():
            self.assertRegex(url, icons._URL_RE)
            self.assertNotIn("..", url)
        png = next(e for e in self.entries() if e[1] == "google-maps")
        self.assertEqual((png[2].rsplit(".", 1)[1], png[3]), ("png", "png"))
        self.assertEqual(icons.index_entries("dashboard", ["a list"]), [])
        self.assertEqual(icons.index_entries("simple", {"a": "dict"}), [])

    def test_search_by_name_alias_and_words(self):
        entries = self.entries()
        self.assertEqual(icons.search("VS Code", None, entries)[0][1], "visual-studio-code")
        self.assertEqual(icons.search("zzz qqq", None, entries), [])
        self.assertEqual(icons.search("vscode", None, entries)[0][1], "visual-studio-code")
        self.assertEqual(icons.search("Visual Studio", None, entries)[0][1], "visual-studio-code")
        self.assertEqual(icons.search("home assist", None, entries)[0][1], "home-assistant")
        self.assertEqual(icons.search("Spotify Music", None, entries)[0][1], "spotify")
        self.assertEqual(icons.search("whatever", "google-maps", entries)[0][1], "google-maps")
        self.assertEqual(icons.search("", None, entries), [])

    def test_score_order(self):
        self.assertGreater(icons.score("spotify", ["spotify"]), icons.score("spotify", ["spotify-connect"]))
        self.assertGreater(icons.score("spot", ["spotify"]), icons.score("tify", ["spotify"]))
        self.assertEqual(icons.score("zz", ["spotify"]), 0)

    def test_index_cached_for_a_week_then_downloaded_again(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            self.assertIn("--proto", command)
            self.assertEqual(kwargs["max_bytes"], icons.MAX_INDEX_BYTES)
            return b'[{"title": "Spotify", "slug": "spotify"}]'

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(icons.shutil, "which", return_value="/usr/bin/curl"):
            self.assertEqual(len(icons.load_index("simple", run, directory=tmp)), 1)
            self.assertEqual(len(icons.load_index("simple", run, directory=tmp)), 1)
            self.assertEqual(len(calls), 1)  # The second time from the cache.
            later = icons.time.time() + icons.INDEX_MAX_AGE + 10
            icons.load_index("simple", run, now=later, directory=tmp)
            self.assertEqual(len(calls), 2)
            # Download fails or is no JSON: the old copy is used.
            self.assertEqual(len(icons.load_index("simple", lambda *a, **k: b"<html>", now=later, directory=tmp)), 1)
            self.assertEqual(len(icons.load_index("simple", lambda *a, **k: None, now=later, directory=tmp)), 1)

    def test_symlinked_or_huge_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.json"
            real.write_text('[{"title": "Spotify", "slug": "spotify"}]')
            (Path(tmp) / "simple.json").symlink_to(real)
            with mock.patch.object(icons.shutil, "which", return_value=None):
                self.assertEqual(icons.load_index("simple", directory=tmp), [])
            (Path(tmp) / "simple.json").unlink()
            (Path(tmp) / "simple.json").write_bytes(b"[" + b" " * icons.MAX_INDEX_BYTES + b"]")
            with mock.patch.object(icons.shutil, "which", return_value=None):
                self.assertEqual(icons.load_index("simple", directory=tmp), [])

    def test_without_name_lists_the_old_guesses(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(icons.shutil, "which", return_value=None):
            found = icons.candidates("Codex", "codex", directory=tmp)
        self.assertEqual(found[0][:2], ("dashboard", "codex"))
        self.assertTrue(all(icons._URL_RE.match(url) for _c, _s, url, _k in found))


class IconDownloadTests(unittest.TestCase):
    def test_other_format_when_one_is_missing(self):
        asked = []

        def run(command, **_k):
            asked.append(command[-1])
            return b"\x89PNG\r\n\x1a\n...." if command[-1].endswith(".png") else None

        with mock.patch.object(icons.shutil, "which", return_value="/usr/bin/curl"):
            url = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/code.svg"
            got = icons.download(("dashboard", "code", url, "svg"), run=run)
            self.assertEqual(got[0::3], ("Dashboard Icons", ".png"))
            self.assertEqual(asked, [url, "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/code.png"])
            asked.clear()
            self.assertIsNone(icons.download(("simple", "x", "https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/x.svg", "mono"), run=run))
            self.assertEqual(len(asked), 1)  # Simple Icons has only SVG.
