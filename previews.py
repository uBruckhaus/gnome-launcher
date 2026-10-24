"""Animated sketch on the launcher's settings page: what the layout options
do, drawn live in the theme's colours.

Each sketch is a Gtk.DrawingArea that redraws on the frame clock while it
is shown (GTK only ticks visible widgets). The geometry is in plain
functions so it can be tested without a display."""
import math
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


SCREEN_W = 1920        # The sketched screen, in logical pixels.
DOCK_ZOOM = 1.7        # The dock is drawn larger than to scale, or its icons would be dots.
CYCLE = 6.0            # Seconds per animation loop.
WIDTH = 300            # The sketches' width: inside a settings card, whatever the window's width.


def rgba(hex_code, alpha=1.0):
    hex_code = (hex_code or "#888888").lstrip("#")
    try:
        r, g, b = (int(hex_code[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        r = g = b = 0.5
    return r, g, b, alpha


def rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


class Sketch(Gtk.DrawingArea):
    def __init__(self, height, colors):
        super().__init__()
        self.colors = colors  # A callable: the theme palette.
        self.set_content_height(height)
        self.set_content_width(WIDTH)
        self.set_size_request(WIDTH, height)
        self.set_hexpand(False)
        self.set_halign(Gtk.Align.START)
        self.set_draw_func(self.draw)
        self.started = time.monotonic()
        self.add_tick_callback(lambda *_a: (self.queue_draw(), True)[1])

    def loop_time(self):
        return (time.monotonic() - self.started) % CYCLE

    def draw(self, _area, cr, width, height):
        raise NotImplementedError


class LayoutSketch(Sketch):
    """The window: the list when narrow (with or without view buttons),
    then resized wide, folders or cards."""

    def __init__(self, colors, state):
        super().__init__(120, colors)
        self.state = state  # A callable: (wide_layout, view_buttons_on, corner radius in px).

    def draw(self, _area, cr, width, height):
        c = self.colors()
        layout, view_buttons, radius = self.state()
        # The sketch is about half size: the card's corners, then the folders' and cards'.
        card_r, tile_r, dot_r = radius / 2, radius / 3, min(2, radius / 4)
        t = self.loop_time()
        grow = ease((t - 2.0) / 0.8) if t < 4.8 else 1.0 - ease((t - 5.2) / 0.6)
        small_w, wide_w = min(110.0, width * 0.32), width - 16
        card_w = small_w + (wide_w - small_w) * grow
        card_h = height - 12
        x, y = (width - card_w) / 2, 6
        rounded(cr, x, y, card_w, card_h, card_r)
        cr.set_source_rgba(*rgba(c["background"], 1))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c["accent"], 0.9))
        cr.set_line_width(1)
        cr.stroke()
        # Header: title, view buttons (small only), gear.
        cr.rectangle(x + 8, y + 8, 28, 4)
        cr.set_source_rgba(*rgba(c["foreground"], 0.8))
        cr.fill()
        buttons = 3 if view_buttons and grow < 0.5 else 0
        for i in range(buttons + 1):
            bx = x + card_w - 12 - i * 9
            rounded(cr, bx, y + 6, 7, 7, dot_r)
            cr.set_source_rgba(*rgba(c["accent"] if i else c["foreground"], 0.7 if i else 0.5))
            cr.fill()
        top = y + 20
        inner_w, inner_h = card_w - 16, card_h - 26
        colours = [c["accent"], c.get("red", "#e06c75"), c.get("yellow", "#e5c07b"),
                   c.get("green", "#98c379"), c.get("orange", "#d19a66")]
        cr.save()
        cr.rectangle(x + 4, top, card_w - 8, inner_h + 2)
        cr.clip()
        if grow < 0.5:
            # The list: icon and a line per row.
            alpha = 1 - grow * 2
            for row in range(7):
                ry = top + row * 12
                rounded(cr, x + 8, ry, 8, 8, 2)
                cr.set_source_rgba(*rgba(colours[row % len(colours)], alpha))
                cr.fill()
                cr.rectangle(x + 20, ry + 2, min(inner_w - 16, 40 + (row * 13) % 30), 3)
                cr.set_source_rgba(*rgba(c["foreground"], 0.45 * alpha))
                cr.fill()
        else:
            alpha = (grow - 0.5) * 2
            if layout == "folders":
                tile = 30
                columns = max(1, int(inner_w // (tile + 8)))
                for i in range(columns * 2):
                    tx = x + 8 + (i % columns) * (tile + 8)
                    ty = top + (i // columns) * (tile + 10)
                    rounded(cr, tx, ty, tile, tile, tile_r)
                    cr.set_source_rgba(*rgba(c["foreground"], 0.08 * alpha))
                    cr.fill()
                    for j in range(9 - (i * 2) % 5):
                        rounded(cr, tx + 3 + (j % 3) * 9, ty + 3 + (j // 3) * 9, 6, 6, 2)
                        cr.set_source_rgba(*rgba(colours[(i + j) % len(colours)], alpha))
                        cr.fill()
                    cr.rectangle(tx + 8, ty + tile + 3, tile - 16, 3)
                    cr.set_source_rgba(*rgba(c["foreground"], 0.5 * alpha))
                    cr.fill()
            else:
                tile_w, tile_h = 52, 30
                columns = max(1, int(inner_w // (tile_w + 6)))
                for i in range(columns * 2):
                    tx = x + 8 + (i % columns) * (tile_w + 6)
                    ty = top + (i // columns) * (tile_h + 6)
                    rounded(cr, tx, ty, tile_w, tile_h, tile_r)
                    cr.set_source_rgba(*rgba(c["foreground"], 0.08 * alpha))
                    cr.fill()
                    rounded(cr, tx + 5, ty + 5, 11, 11, 3)
                    cr.set_source_rgba(*rgba(colours[i % len(colours)], alpha))
                    cr.fill()
                    cr.rectangle(tx + 5, ty + 21, tile_w - 14, 3)
                    cr.set_source_rgba(*rgba(c["foreground"], 0.5 * alpha))
                    cr.fill()
            # The group bar along the bottom of the wide window.
            for i in range(6):
                rounded(cr, x + 8 + i * 22, y + card_h - 10, 18, 6, 3)
                cr.set_source_rgba(*rgba(c["accent"] if i == 0 else c["foreground"], (0.8 if i == 0 else 0.3) * alpha))
                cr.fill()
        cr.restore()
