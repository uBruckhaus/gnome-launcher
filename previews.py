"""Animated sketches on App Manager's settings page: what the layout and
the dock options do, drawn live in the theme's colours.

Each sketch is a Gtk.DrawingArea that redraws on the frame clock while it
is shown (GTK only ticks visible widgets). The geometry is in plain
functions so it can be tested without a display."""
import math
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

import dock_core

SCREEN_W = 1920        # The sketched screen, in logical pixels.
DOCK_ZOOM = 1.7        # The dock is drawn larger than to scale, or its icons would be dots.
CYCLE = 6.0            # Seconds per animation loop.
WIDTH = 300            # The sketches' width: inside a settings card, whatever the popup's width.


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


def reveal(t, autohide):
    """How far the dock is shown (0..1) at loop time t: with auto-hide it
    slides in when the pointer reaches the edge and out after it leaves."""
    if not autohide:
        return 1.0
    if t < 1.0:
        return 0.0
    if t < 1.3:
        return ease((t - 1.0) / 0.3)
    if t < 4.4:
        return 1.0
    if t < 4.7:
        return 1.0 - ease((t - 4.4) / 0.3)
    return 0.0


def pointer_along(t):
    """Where the pointer is along the dock (0..1) while it is shown."""
    if t < 1.3:
        return 0.5
    if t < 4.2:
        u = (t - 1.3) / 2.9
        return 0.5 + 0.45 * math.sin(u * 2 * math.pi)
    return 0.5


def magnification(center, pointer, item, magnify):
    """macOS-like wave: full growth under the pointer, fading over 1.5 icons."""
    if magnify <= 1.0:
        return 1.0
    distance = abs(center - pointer) / max(1.0, 1.5 * item)
    return 1.0 + (magnify - 1.0) * max(0.0, 1.0 - distance)


def edge_matrix(edge, x, y, w, h):
    """A cairo matrix from dock space (along the edge, away from it) to the screen."""
    if edge == "bottom":
        return cairo.Matrix(1, 0, 0, -1, x, y + h)
    if edge == "top":
        return cairo.Matrix(1, 0, 0, 1, x, y)
    if edge == "left":
        return cairo.Matrix(0, 1, 1, 0, x, y)
    return cairo.Matrix(0, 1, -1, 0, x + w, y)


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


class DockSketch(Sketch):
    """A small screen with the bar, two windows and the dock as set."""

    def __init__(self, colors, dock, bar):
        super().__init__(150, colors)
        self.dock = dock  # Callables returning the dock settings and the bar's edge.
        self.bar = bar
        self.set_tooltip_text(None)

    def draw(self, _area, cr, width, height):
        c = self.colors()
        s, bar = self.dock(), self.bar()
        edge = dock_core.edge(s, bar)
        t = self.loop_time()
        # The screen, 16:9, centred.
        sh = min(height - 8, (width - 8) * 9 / 16)
        sw = sh * 16 / 9
        sx, sy = (width - sw) / 2, (height - sh) / 2
        scale = sw / SCREEN_W
        k = scale * DOCK_ZOOM
        rounded(cr, sx, sy, sw, sh, 6)
        cr.set_source_rgba(*rgba(c["foreground"], 0.05))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c["foreground"], 0.25))
        cr.set_line_width(1)
        cr.stroke()
        # The bar on its edge.
        bar_t = max(5.0, 30 * scale * 1.4)
        work = [sx, sy, sw, sh]
        bar_rect = {"top": (sx, sy, sw, bar_t), "bottom": (sx, sy + sh - bar_t, sw, bar_t),
                    "left": (sx, sy, bar_t, sh), "right": (sx + sw - bar_t, sy, bar_t, sh)}[bar]
        cr.rectangle(*bar_rect)
        cr.set_source_rgba(*rgba(c["foreground"], 0.16))
        cr.fill()
        if bar == "top":
            work[1] += bar_t
            work[3] -= bar_t
        elif bar == "bottom":
            work[3] -= bar_t
        elif bar == "left":
            work[0] += bar_t
            work[2] -= bar_t
        else:
            work[2] -= bar_t
        band, item, _room = dock_core.sizes(s)
        band, item = band * k, item * k
        gap = 0 if s["dock_full_width"] else 6 * k
        shown = reveal(t, s["dock_autohide"])
        # Without auto-hide the windows keep clear of the band.
        windows = list(work)
        if not s["dock_autohide"]:
            keep = band + gap
            if edge == "top":
                windows[1] += keep
                windows[3] -= keep
            elif edge == "bottom":
                windows[3] -= keep
            elif edge == "left":
                windows[0] += keep
                windows[2] -= keep
            else:
                windows[2] -= keep
        self.draw_windows(cr, c, *windows)
        cr.save()
        rounded(cr, sx, sy, sw, sh, 6)  # A hidden dock slides off the sketched screen.
        cr.clip()
        self.draw_dock(cr, c, s, edge, work, band, item, gap, shown, t, k)
        cr.restore()

    @staticmethod
    def draw_windows(cr, c, x, y, w, h):
        g = 3
        half = (w - 3 * g) / 2
        for i in range(2):
            wx = x + g + i * (half + g)
            rounded(cr, wx, y + g, half, h - 2 * g, 3)
            cr.set_source_rgba(*rgba(c["foreground"], 0.09))
            cr.fill_preserve()
            cr.set_source_rgba(*rgba(c["foreground"], 0.22))
            cr.set_line_width(1)
            cr.stroke()
            cr.rectangle(wx + 4, y + g + 4, half * 0.5, 2)
            cr.set_source_rgba(*rgba(c["foreground"], 0.25))
            cr.fill()

    def draw_dock(self, cr, c, s, edge, work, band, item, gap, shown, t, k):
        x, y, w, h = work
        length_room = w if edge in ("top", "bottom") else h
        icons = [c["accent"], c.get("red", "#e06c75"), c.get("yellow", "#e5c07b"),
                 c.get("green", "#98c379"), c.get("orange", "#d19a66")]
        count = 1 + len(icons)  # The App Manager button, then the apps.
        icon = s["dock_icon_size"] * k
        step = item + s.get("dock_spacing", 6) * k
        separators = 2
        length = count * step + separators * 6 * k + 12 * k
        full = s["dock_full_width"]
        band_len = length_room if full else min(length, length_room - 8)
        cr.save()
        cr.transform(edge_matrix(edge, x, y, w, h))
        # Slide in from the edge.
        cr.translate(0, -(1 - shown) * (band + gap + 2))
        start = 0 if full else (length_room - band_len) / 2
        radius = 0 if not s["dock_rounded"] else min(band / 2, 4 + 8 * k)
        if not s["dock_transparent"]:
            rounded(cr, start, gap, band_len, band, radius)
            cr.set_source_rgba(*rgba(c["background"], 0.92))
            cr.fill_preserve()
            cr.set_source_rgba(*rgba(c["foreground"], 0.22))
            cr.set_line_width(1)
            cr.stroke()
        # Icons: from the alignment (full length) or centred.
        if full:
            first = {"start": 10 * k, "end": length_room - length + 10 * k}.get(
                s["dock_align"], (length_room - length) / 2 + 10 * k)
        else:
            first = start + 6 * k
        offset = gap + max(0.0, (band - item) / 2) + 6 * k
        pointer = first + pointer_along(t) * (length - 12 * k)
        magnify = s["dock_magnify"] / 100
        pos = first
        for index in range(count):
            if index in (1, count - 1):
                cr.rectangle(pos + 2 * k, gap + band * 0.2, max(1.0, k), band * 0.6)
                cr.set_source_rgba(*rgba(c["foreground"], 0.25))
                cr.fill()
                pos += 6 * k
            center = pos + step / 2
            grow = magnification(center, pointer, step, magnify) if shown > 0.99 and 1.3 < t < 4.2 else 1.0
            size = icon * grow
            lift = 0.0
            if s["dock_bounce"] and index == 3:
                lift = abs(math.sin(t * math.pi / 0.7)) * dock_core.BOUNCE * k
            ix, iy = center - size / 2, offset + lift
            rounded(cr, ix, iy, size, size, size * 0.22)
            if index == 0:
                cr.set_source_rgba(*rgba(c["foreground"], 0.18))
                cr.fill()
                for gx in range(3):
                    for gy in range(3):
                        cr.arc(ix + size * (0.28 + 0.22 * gx), iy + size * (0.28 + 0.22 * gy),
                               max(0.6, size * 0.05), 0, 2 * math.pi)
                        cr.set_source_rgba(*rgba(c["foreground"], 0.85))
                        cr.fill()
            else:
                cr.set_source_rgba(*rgba(icons[(index - 1) % len(icons)]))
                cr.fill()
            if index in (3, count - 1) and s.get("dock_indicator_shown", True):  # Running: a dot or a line.
                mark_y = gap + max(2.0, (offset - gap) / 2)
                active = index == 3
                if s.get("dock_indicator") == "line":
                    half = icon * (0.3 if active else 0.15)
                    rounded(cr, center - half, mark_y - max(0.8, k), 2 * half, max(1.6, 2 * k), max(0.8, k))
                else:
                    cr.arc(center, mark_y, max(1.0, (2.2 if active else 1.6) * k), 0, 2 * math.pi)
                cr.set_source_rgba(*rgba(dock_core.indicator_color(s, c), 1.0 if active else 0.6))
                cr.fill()
            pos += step
        # The pointer: towards the edge with auto-hide, then along the dock.
        if s["dock_autohide"] and t < 1.3:
            across = (1 - min(1.0, t / 1.0)) * 40 * k + 2
        elif s["dock_autohide"] and t > 4.2:
            across = min(1.0, (t - 4.2) / 0.8) * 40 * k + band * 0.6
        else:
            across = gap + band * 0.6
        self.draw_pointer(cr, c, pointer, across, k)
        cr.restore()

    @staticmethod
    def draw_pointer(cr, c, x, y, k):
        size = max(5.0, 9 * k)
        cr.move_to(x, y)
        cr.line_to(x + size * 0.35, y + size)
        cr.line_to(x + size * 0.75, y + size * 0.6)
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.7)
        cr.set_line_width(0.8)
        cr.stroke()


class LayoutSketch(Sketch):
    """The popup: the list when small (with or without view buttons), then
    resized wide, folders or cards."""

    def __init__(self, colors, state):
        super().__init__(120, colors)
        self.state = state  # A callable: (wide_layout, view_buttons_on).

    def draw(self, _area, cr, width, height):
        c = self.colors()
        layout, view_buttons = self.state()
        t = self.loop_time()
        grow = ease((t - 2.0) / 0.8) if t < 4.8 else 1.0 - ease((t - 5.2) / 0.6)
        small_w, wide_w = min(110.0, width * 0.32), width - 16
        card_w = small_w + (wide_w - small_w) * grow
        card_h = height - 12
        x, y = (width - card_w) / 2, 6
        rounded(cr, x, y, card_w, card_h, 6)
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
            rounded(cr, bx, y + 6, 7, 7, 2)
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
                    rounded(cr, tx, ty, tile, tile, 4)
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
                    rounded(cr, tx, ty, tile_w, tile_h, 4)
                    cr.set_source_rgba(*rgba(c["foreground"], 0.08 * alpha))
                    cr.fill()
                    rounded(cr, tx + 5, ty + 5, 11, 11, 3)
                    cr.set_source_rgba(*rgba(colours[i % len(colours)], alpha))
                    cr.fill()
                    cr.rectangle(tx + 5, ty + 21, tile_w - 14, 3)
                    cr.set_source_rgba(*rgba(c["foreground"], 0.5 * alpha))
                    cr.fill()
            # The group bar along the bottom of the wide popup.
            for i in range(6):
                rounded(cr, x + 8 + i * 22, y + card_h - 10, 18, 6, 3)
                cr.set_source_rgba(*rgba(c["accent"] if i == 0 else c["foreground"], (0.8 if i == 0 else 0.3) * alpha))
                cr.fill()
        cr.restore()
