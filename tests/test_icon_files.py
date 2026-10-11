"""Icon files from .desktop files, AppImages and tray peers are size-checked
from their header before any image loader sees them."""
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_core  # noqa: E402

# launcher_popup re-executes itself to preload gtk4-layer-shell unless the
# library is already listed; tests only need textures, not a layer surface.
_preload = os.environ.get("LD_PRELOAD")
os.environ["LD_PRELOAD"] = ":".join(filter(None, ("libgtk4-layer-shell.so.0", _preload)))
try:
    import launcher_popup  # noqa: E402
except Exception:   # no GTK in this environment
    launcher_popup = None
finally:
    if _preload is None:
        os.environ.pop("LD_PRELOAD", None)
    else:
        os.environ["LD_PRELOAD"] = _preload


def png(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height)) if width * height <= 1 << 20 else b""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def gif(width=1, height=1, frames=1):
    frame = b"\x2c" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00" + b"\x02\x02\x44\x01\x00"
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + frame * frames + b"\x3b"


def jpeg(width=1, height=1):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp(width=1, height=1, frames=0):
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x02\x00\x00\x00" \
        + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    body = b"WEBP" + vp8x + (b"ANMF" + struct.pack("<I", 16) + b"\x00" * 16) * frames
    return b"RIFF" + struct.pack("<I", len(body)) + body


class IconBytesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_normal_icons_are_accepted(self):
        for name, data in {"a.png": png(256, 256), "b.gif": gif(48, 48), "c.jpg": jpeg(512, 512),
                           "d.webp": webp(128, 128), "e.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>"}.items():
            with self.subTest(name):
                self.assertEqual(launcher_core.icon_bytes(self.write(name, data)), data)

    def test_small_files_declaring_huge_canvases_are_refused(self):
        bombs = {"wide.png": png(100000, 10), "huge.png": png(60000, 60000), "big.jpg": jpeg(65000, 65000),
                 "frames.gif": gif(1024, 1024, frames=40), "canvas.gif": gif(30000, 30000),
                 "frames.webp": webp(1024, 1024, frames=40), "huge.webp": webp(16000, 16000)}
        for name, data in bombs.items():
            with self.subTest(name):
                self.assertLess(len(data), 4096)
                self.assertIsNone(launcher_core.icon_bytes(self.write(name, data)))

    def test_unknown_malformed_and_oversized_files_are_refused(self):
        cases = {"icon.png": b"\x89PNG\r\n\x1a\n", "fake.png": b"not an image",
                 "icon.xpm": b"/* XPM */\nstatic char *x[] = {\"99999 99999 1 1\"};",
                 "empty.png": b"", "zero.png": png(0, 5),
                 "big.svg": b"<svg>" + b" " * launcher_core.MAX_SVG_ICON_BYTES + b"</svg>",
                 "big.png": png(64, 64) + b"\x00" * launcher_core.MAX_ICON_FILE_BYTES}
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNone(launcher_core.icon_bytes(self.write(name, data)))
        self.assertIsNone(launcher_core.icon_bytes(self.dir / "missing.png"))
        self.assertIsNone(launcher_core.icon_bytes(self.dir))   # a directory


@unittest.skipIf(launcher_popup is None, "needs GTK")
class IconTextureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        launcher_popup._icon_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_oversized_icon_never_reaches_a_loader(self):
        bomb = self.dir / "tray-icon.png"   # what a tray peer's IconThemePath could hold
        bomb.write_bytes(png(60000, 60000))
        with patch.object(launcher_popup.GdkPixbuf, "PixbufLoader") as loader:
            self.assertIsNone(launcher_popup.icon_texture(str(bomb)))
        loader.assert_not_called()

    def test_icons_are_decoded_at_icon_size_and_cached(self):
        big = self.dir / "app.png"
        big.write_bytes(png(512, 256))
        texture = launcher_popup.icon_texture(str(big))
        self.assertEqual((texture.get_width(), texture.get_height()), (64, 32))
        svg = self.dir / "app.svg"
        svg.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg' width='100000' height='100000'>"
                        b"<rect width='100000' height='100000' fill='red'/></svg>")
        texture = launcher_popup.icon_texture(str(svg))
        self.assertEqual((texture.get_width(), texture.get_height()), (64, 64))
        with patch.object(launcher_popup.GdkPixbuf, "PixbufLoader") as loader:
            launcher_popup.icon_texture(str(big))
        loader.assert_not_called()   # cached: rebuilding the list does not decode again

    def test_replaced_file_is_checked_again(self):
        icon = self.dir / "swap.png"
        icon.write_bytes(png(16, 16))
        self.assertIsNotNone(launcher_popup.icon_texture(str(icon)))
        icon.write_bytes(png(60000, 60000))
        os.utime(icon, ns=(1, 1))   # different mtime and size: a new cache key
        self.assertIsNone(launcher_popup.icon_texture(str(icon)))


if __name__ == "__main__":
    unittest.main()
