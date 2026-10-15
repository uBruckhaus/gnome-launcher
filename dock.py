#!/usr/bin/env python3
"""App Manager's dock: pinned and running apps along a screen edge, like
the docks of macOS and Windows. Optional (App Manager → Settings → Dock).

One process (Gtk.Application with a unique id: starting it again only makes
the running dock re-read its settings). App Manager's bar widget starts it
when the shell loads; it quits by itself when the dock is switched off."""

import os
import sys

# GTK4 layer shell must load before GTK's Wayland client library (see launcher_popup.py).
layer_shell_library = "libgtk4-layer-shell.so.0"
if layer_shell_library not in os.environ.get("LD_PRELOAD", "").split(":"):
    os.environ["LD_PRELOAD"] = ":".join(filter(None, (
        layer_shell_library, os.environ.get("LD_PRELOAD", ""))))
    os.execv(sys.executable, [sys.executable, *sys.argv])

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Gtk4LayerShell

# Apps started from the dock must not inherit the preload.
_preloads = [p for p in os.environ.get("LD_PRELOAD", "").split(":") if p and p != layer_shell_library]
if _preloads:
    os.environ["LD_PRELOAD"] = ":".join(_preloads)
else:
    os.environ.pop("LD_PRELOAD", None)

import faulthandler
import json
import signal
import subprocess
import threading
from pathlib import Path

import dock_core
import icons
from i18n import _
import corners
from launcher_core import icon_bytes, launch_action, launch_app, parse_desktop_files, read_bounded_output, saved_corners
from palette import ThemePalette

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config/applauncher"
POLL_MS = 4000          # A fallback: Hyprland's events update the dock at once.
HIDE_DELAY_MS = 700
EDGE_STRIP = 2          # The hidden dock's hover strip, in pixels.
DRAG_PREFIX = "app-manager-dock:"
EVENT_CHUNK = 4096      # Hyprland's event socket, read this much at a time.
EVENT_LINE_MAX = 4096   # Longer event lines (huge window titles) are skipped.
MAX_URGENT = 256        # Windows asking for attention, at most.
MARKER = 4              # The drop marker's thickness.
_texture_cache = {}


def dock_radius():
    """The dock's corners when "Rounded corners" is on: App Manager's Corners
    setting (like the windows, rounded or square), never square."""
    out = read_bounded_output(["hyprctl", "getoption", "decoration:rounding", "-j"], max_bytes=4096, timeout=2)
    return corners.radius(saved_corners(), corners.parse_rounding(out or "")) or corners.ROUNDED


def hyprctl_dispatch(command):
    try:
        subprocess.Popen(["hyprctl", "dispatch", command], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def window_arg(client):
    return "{window = " + json.dumps("address:" + str(client.get("address", ""))) + "}"


def icon_image(app, size):
    """The app's icon (a theme name or a checked file), at size."""
    name = app.get("icon") or "application-x-executable"
    path = Path(name)
    if path.is_absolute() and path.is_file():
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            key = None
        texture = _texture_cache.get(key)
        if key is not None and key not in _texture_cache:
            data = icon_bytes(path)  # Size-capped and header-checked, as in App Manager.
            if data is not None:
                loader = GdkPixbuf.PixbufLoader()
                loader.connect("size-prepared", lambda l, w, h: l.set_size(
                    max(1, round(w * min(1.0, 96 / max(w, h, 1)))), max(1, round(h * min(1.0, 96 / max(w, h, 1))))))
                try:
                    loader.write(data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf) if pixbuf else None
                except GLib.Error:
                    texture = None
            _texture_cache[key] = texture
        if texture is not None:
            image = Gtk.Image.new_from_paintable(texture)
            image.set_pixel_size(size)
            return image
        name = path.stem
    image = Gtk.Image.new_from_icon_name(name)
    image.set_pixel_size(size)
    return image


class Dock(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.omarchy.appmanager.dock")
        self.window = None
        self.settings = dock_core.settings()
        self.edge = dock_core.edge(self.settings, dock_core.bar_position())
        self.apps = []
        self.clients = []
        self.signature = None
        self.revealed = True
        self.hide_source = 0
        self.menu = None
        self.dragging = None
        self.external = None  # (pinned id or None, after) while an app from App Manager hovers.
        self.popup_open = False  # App Manager open: stay shown (its overlay hides the edge).
        self.urgent = set()       # Window addresses that asked for attention (bounce).
        self.events_source = 0
        self.theme = ThemePalette()
        self.radius = dock_radius()
        self.corners = saved_corners()
        self.css = Gtk.CssProvider()

    # --- life cycle ------------------------------------------------------------

    def do_activate(self):
        if self.window is not None:
            self.reload_settings()  # Started again: re-read the settings.
            return
        if not self.settings["dock_enabled"]:
            self.quit()
            return
        self.hold()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: GLib.idle_add(self.quit))
        GLib.timeout_add(200, lambda: True)  # Let Python see signals.
        # App Manager says when its popup opens and closes ("popup-open", bool):
        # its full-screen overlay keeps the pointer from reaching the edge strip.
        action = Gio.SimpleAction.new("popup-open", GLib.VariantType.new("b"))
        action.connect("activate", self.on_popup_open)
        self.add_action(action)
        self.load_apps()
        self.build_window()
        self.apply_theme()
        self.apply_layout()
        self.refresh(force=True)
        GLib.timeout_add(POLL_MS, self.poll)
        self.listen_hyprland()
        self.start_watchdog()
        GLib.timeout_add_seconds(60, self.periodic)
        self.watch_config()

    def start_watchdog(self):
        """A hung main loop: write every thread's stack to dock-hang.log and
        exit (the bar widget starts the dock again with the next shell load,
        App Manager's Settings switch at once)."""
        log = Path.home() / ".local/state/applauncher/dock-hang.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self.hang_log = os.fdopen(fd, "a", buffering=1)
        except OSError:
            self.hang_log = sys.stderr

        def beat():
            faulthandler.dump_traceback_later(20, exit=True, file=self.hang_log)
            return GLib.SOURCE_CONTINUE
        beat()
        GLib.timeout_add_seconds(2, beat)

    def watch_config(self):
        """settings.json (dock settings, pinned apps) and icons.json change
        when App Manager saves them: follow at once."""
        try:
            self.monitor = Gio.File.new_for_path(str(CONFIG_DIR)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.monitor.connect("changed", self.on_config_changed)
            # The bar moved (shell.json): never share its edge.
            self.shell_monitor = Gio.File.new_for_path(str(dock_core.SHELL_FILE.parent)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.shell_monitor.connect("changed", self.on_config_changed)
        except GLib.Error:
            self.monitor = None
        self.config_source = 0

    def on_config_changed(self, _monitor, file, other, _event):
        names = {f.get_basename() for f in (file, other) if f is not None}
        if names & {"settings.json", "icons.json", "shell.json"}:
            if self.config_source:
                GLib.source_remove(self.config_source)
            self.config_source = GLib.timeout_add(250, self.reload_settings)

    def reload_settings(self):
        self.config_source = 0
        before, before_edge = self.settings, self.edge
        self.settings = dock_core.settings()
        self.edge = dock_core.edge(self.settings, dock_core.bar_position())
        if not self.settings["dock_enabled"]:
            self.quit()
            return GLib.SOURCE_REMOVE
        icons.apply(self.apps, icons.load())
        layout_keys = ("dock_position", "dock_align", "dock_autohide", "dock_full_width", "dock_rounded",
                       "dock_transparent", "dock_height", "dock_icon_size", "dock_spacing", "dock_magnify",
                       "dock_bounce",
                       "dock_indicator_shown", "dock_indicator", "dock_indicator_color")
        corners_before, self.corners = self.corners, saved_corners()
        if corners_before != self.corners:
            self.radius = dock_radius()
        if (self.edge != before_edge or corners_before != self.corners
                or any(before[k] != self.settings[k] for k in layout_keys)):
            self.apply_layout()
            self.apply_theme()
        self.refresh(force=True)
        return GLib.SOURCE_REMOVE

    def periodic(self):
        """Newly installed apps and theme changes."""
        self.load_apps()
        if self.theme.reload():
            self.radius = dock_radius()  # A theme can bring its own window rounding.
            self.apply_theme()
        self.refresh(force=True)
        return True

    def load_apps(self):
        try:
            self.apps = icons.apply(parse_desktop_files(), icons.load())
        except Exception as error:
            print("dock: could not read apps:", error, file=sys.stderr)

    # --- window -----------------------------------------------------------------

    def build_window(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_decorated(False)
        window.set_title("App Manager Dock")
        Gtk4LayerShell.init_for_window(window)
        Gtk4LayerShell.set_namespace(window, "app-manager-dock")
        Gtk4LayerShell.set_layer(window, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_keyboard_mode(window, Gtk4LayerShell.KeyboardMode.NONE)
        window.add_css_class("dock-window")

        self.outer = Gtk.Box()
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_duration(160)
        self.revealer.set_reveal_child(True)
        # The dock: a band of the chosen height (the background) with the
        # icons on a layer above it, so larger icons can stick out of it.
        self.panel = Gtk.Overlay()
        self.frame = Gtk.Box()
        self.spacer = Gtk.Box()
        self.band = Gtk.Box()
        self.band.add_css_class("dock-band")
        self.frame.append(self.spacer)
        self.frame.append(self.band)
        self.panel.set_child(self.frame)
        self.items_box = Gtk.Box(spacing=self.settings["dock_spacing"])
        # The drop marker: an accent bar in the gap where a dragged app will land.
        items_layer = Gtk.Overlay()
        items_layer.set_child(self.items_box)
        self.marker = Gtk.Box()
        self.marker.add_css_class("dock-drop-marker")
        self.marker.set_can_target(False)
        self.marker.set_visible(False)
        items_layer.add_overlay(self.marker)
        self.items_layer = items_layer
        self.panel.add_overlay(items_layer)
        # Double-click the background (not an icon): transparent or not, as on the bar.
        clicks = Gtk.GestureClick()
        clicks.connect("pressed", self.on_panel_click)
        self.panel.add_controller(clicks)
        self.add_external_drop()
        self.revealer.set_child(self.panel)
        self.strip = Gtk.Box()
        self.strip.add_css_class("dock-strip")
        self.outer.append(self.revealer)
        self.outer.append(self.strip)
        window.set_child(self.outer)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: self.show_dock())
        motion.connect("leave", lambda *_a: self.schedule_hide())
        window.add_controller(motion)
        self.window = window
        window.present()

    def apply_layout(self):
        """Edge (never the bar's), sizes, length, auto-hide and look."""
        s = self.settings
        window, position = self.window, self.edge
        vertical = position in ("left", "right")
        band, item, room = dock_core.sizes(s)
        self.band_size, self.item_size, self.room = band, item, room
        edges = {"bottom": Gtk4LayerShell.Edge.BOTTOM, "left": Gtk4LayerShell.Edge.LEFT,
                 "right": Gtk4LayerShell.Edge.RIGHT, "top": Gtk4LayerShell.Edge.TOP}
        along = ("top", "bottom") if vertical else ("left", "right")
        # Compact: always centred on its edge. Full length: the alignment
        # places the icons inside it (see below).
        for name, edge in edges.items():
            Gtk4LayerShell.set_anchor(window, edge, name == position or (s["dock_full_width"] and name in along))
        gap = 0 if s["dock_full_width"] else 6
        # Floating (compact): a gap to the edge. With auto-hide the gap is inside
        # the sliding part, so the hover strip stays right on the screen edge.
        for name, edge in edges.items():
            Gtk4LayerShell.set_margin(window, edge, gap if name == position and not s["dock_autohide"] else 0)
        inner = gap if s["dock_autohide"] else 0
        for side, setter in (("bottom", self.panel.set_margin_bottom), ("top", self.panel.set_margin_top),
                             ("left", self.panel.set_margin_start), ("right", self.panel.set_margin_end)):
            setter(inner if side == position else 0)
        orientation = Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL
        across = Gtk.Orientation.HORIZONTAL if vertical else Gtk.Orientation.VERTICAL
        self.items_box.set_orientation(orientation)
        self.items_box.set_spacing(s["dock_spacing"])  # The gap between two icons (a setting).
        # Band on the screen edge, the room for icons that stick out on the inside.
        self.frame.set_orientation(across)
        self.frame.remove(self.spacer)
        self.frame.remove(self.band)
        for child in ((self.band, self.spacer) if position in ("left", "top") else (self.spacer, self.band)):
            self.frame.append(child)
        if vertical:
            self.band.set_size_request(band, -1)
            self.spacer.set_size_request(room - band, -1)
        else:
            self.band.set_size_request(-1, band)
            self.spacer.set_size_request(-1, room - band)
        # Icons: on the edge side of the band, centred in it when they fit.
        offset = max(0, (band - item) // 2)
        layer = self.items_layer
        for side, setter in (("bottom", layer.set_margin_bottom), ("top", layer.set_margin_top),
                             ("left", layer.set_margin_start), ("right", layer.set_margin_end)):
            setter(offset if side == position else 0)
        edge_align = Gtk.Align.START if position in ("left", "top") else Gtk.Align.END
        along_align = {"start": Gtk.Align.START, "end": Gtk.Align.END}.get(s["dock_align"], Gtk.Align.CENTER) \
            if s["dock_full_width"] else Gtk.Align.CENTER
        if vertical:
            layer.set_halign(edge_align)
            layer.set_valign(along_align)
        else:
            layer.set_valign(edge_align)
            layer.set_halign(along_align)
        # The hover strip sits on the screen edge, after (or before) the dock.
        self.outer.set_orientation(across)
        self.outer.remove(self.revealer)
        self.outer.remove(self.strip)
        for child in ((self.strip, self.revealer) if position in ("left", "top") else (self.revealer, self.strip)):
            self.outer.append(child)
        self.strip.set_size_request(EDGE_STRIP if vertical else -1, -1 if vertical else EDGE_STRIP)
        self.revealer.set_transition_type({
            "bottom": Gtk.RevealerTransitionType.SLIDE_UP,
            "top": Gtk.RevealerTransitionType.SLIDE_DOWN,
            "left": Gtk.RevealerTransitionType.SLIDE_RIGHT,
            "right": Gtk.RevealerTransitionType.SLIDE_LEFT}[position])
        fill = Gtk.Align.FILL if s["dock_full_width"] else Gtk.Align.CENTER
        for widget in (self.outer, self.panel):
            (widget.set_valign if vertical else widget.set_halign)(fill)
        for name in list(self.outer.get_css_classes()):
            if name.startswith("dock-") and name != "dock-outer":
                self.outer.remove_css_class(name)
        self.outer.add_css_class("dock-outer")
        for flag, name in ((s["dock_transparent"], "dock-transparent"), (s["dock_rounded"], "dock-rounded"),
                           (s["dock_full_width"], "dock-full"), (s["dock_magnify"] > 100, "dock-magnify"),
                           (s["dock_indicator"] == "line", "dock-line-marks")):
            if flag:
                self.outer.add_css_class(name)
        self.outer.add_css_class(f"dock-{position}")
        if s["dock_autohide"]:
            Gtk4LayerShell.set_exclusive_zone(window, 0)
            self.strip.set_visible(True)
            self.hide_dock()
        else:
            # Windows keep clear of the band; icons sticking out may overlap them.
            Gtk4LayerShell.set_exclusive_zone(window, band)
            self.strip.set_visible(False)
            self.revealer.set_reveal_child(True)
            self.revealed = True
        GLib.idle_add(self.fit_band)

    def fit_band(self):
        """A compact band is as long as its icons (plus room for one to grow)."""
        if self.settings["dock_full_width"]:
            self.band.set_hexpand(self.edge in ("top", "bottom"))
            self.band.set_vexpand(self.edge in ("left", "right"))
            return GLib.SOURCE_REMOVE
        vertical = self.edge in ("left", "right")
        natural = self.items_box.measure(Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, -1)[1]
        grow = round(self.settings["dock_icon_size"] * (self.settings["dock_magnify"] - 100) / 100)
        length = natural + 16
        self.band.set_hexpand(False)
        self.band.set_vexpand(False)
        if vertical:
            self.band.set_size_request(self.band_size, length)
            self.band.set_margin_top(grow // 2)
            self.band.set_margin_bottom(grow // 2)
        else:
            self.band.set_size_request(length, self.band_size)
            self.band.set_margin_start(grow // 2)
            self.band.set_margin_end(grow // 2)
        return GLib.SOURCE_REMOVE

    def apply_theme(self):
        c = self.theme.colors
        radius = self.radius
        mag = self.settings["dock_magnify"] / 100
        near = 1 + (mag - 1) / 2
        bounce = dock_core.BOUNCE
        mark = dock_core.indicator_color(self.settings, c)
        line_long = max(12, round(self.settings["dock_icon_size"] * 0.6))
        line_short = max(6, round(line_long / 2))
        css = f"""
        window.dock-window {{ background: transparent; }}
        .dock-strip {{ background: transparent; }}
        .dock-band {{
            background: alpha({c['background']}, 0.92);
            border: 1px solid alpha({c['foreground']}, 0.15);
            border-radius: 0;
        }}
        .dock-transparent .dock-band {{ background: transparent; border-color: transparent; }}
        .dock-transparent .dock-dot {{ box-shadow: 0 0 2px alpha(#000000, 0.8); }}
        .dock-rounded .dock-band {{ border-radius: {radius + 4}px; }}
        .dock-full.dock-bottom .dock-band {{ border-left-width: 0; border-right-width: 0; border-bottom-width: 0; }}
        .dock-full.dock-top .dock-band {{ border-left-width: 0; border-right-width: 0; border-top-width: 0; }}
        .dock-full.dock-left .dock-band {{ border-top-width: 0; border-bottom-width: 0; border-left-width: 0; }}
        .dock-full.dock-right .dock-band {{ border-top-width: 0; border-bottom-width: 0; border-right-width: 0; }}
        .dock-full.dock-rounded.dock-bottom .dock-band {{ border-radius: {radius + 4}px {radius + 4}px 0 0; }}
        .dock-full.dock-rounded.dock-top .dock-band {{ border-radius: 0 0 {radius + 4}px {radius + 4}px; }}
        .dock-full.dock-rounded.dock-left .dock-band {{ border-radius: 0 {radius + 4}px {radius + 4}px 0; }}
        .dock-full.dock-rounded.dock-right .dock-band {{ border-radius: {radius + 4}px 0 0 {radius + 4}px; }}
        /* Grow on hover (macOS): the icon under the pointer, its neighbours halfway. */
        .dock-icon {{ transition: transform 120ms ease-out; }}
        .dock-bottom .dock-icon {{ transform-origin: 50% 100%; }}
        .dock-top .dock-icon {{ transform-origin: 50% 0%; }}
        .dock-left .dock-icon {{ transform-origin: 0% 50%; }}
        .dock-right .dock-icon {{ transform-origin: 100% 50%; }}
        .dock-magnify button.dock-item:hover .dock-icon {{ transform: scale({mag}); }}
        .dock-magnify button.dock-item.dock-near .dock-icon {{ transform: scale({near}); }}
        .dock-magnify button.dock-item:hover {{ background: transparent; }}
        /* An app that wants attention bounces until it is focused. */
        @keyframes dock-bounce-up {{ 0% {{ transform: translateY(0); }} 50% {{ transform: translateY(-{bounce}px); }} 100% {{ transform: translateY(0); }} }}
        @keyframes dock-bounce-down {{ 0% {{ transform: translateY(0); }} 50% {{ transform: translateY({bounce}px); }} 100% {{ transform: translateY(0); }} }}
        @keyframes dock-bounce-right {{ 0% {{ transform: translateX(0); }} 50% {{ transform: translateX({bounce}px); }} 100% {{ transform: translateX(0); }} }}
        @keyframes dock-bounce-left {{ 0% {{ transform: translateX(0); }} 50% {{ transform: translateX(-{bounce}px); }} 100% {{ transform: translateX(0); }} }}
        .dock-bottom .dock-bounce {{ animation: dock-bounce-up 700ms ease-in-out infinite; }}
        .dock-top .dock-bounce {{ animation: dock-bounce-down 700ms ease-in-out infinite; }}
        .dock-left .dock-bounce {{ animation: dock-bounce-right 700ms ease-in-out infinite; }}
        .dock-right .dock-bounce {{ animation: dock-bounce-left 700ms ease-in-out infinite; }}
        button.dock-item {{ padding: 4px; min-width: 0; min-height: 0; border-radius: {max(4, radius)}px;
            background: transparent; border: none; box-shadow: none; }}
        button.dock-item:hover {{ background: alpha({c['foreground']}, 0.10); }}
        /* Running apps: a dot or a line (a setting), in the chosen colour;
           the focused app's mark larger and full colour. */
        .dock-dot {{ min-width: 5px; min-height: 5px; border-radius: 999px; background: alpha({mark}, 0.6); }}
        .dock-dot.dock-active {{ min-width: 14px; background: {mark}; }}
        .dock-left .dock-dot.dock-active, .dock-right .dock-dot.dock-active {{ min-width: 5px; min-height: 14px; }}
        .dock-line-marks .dock-dot {{ min-width: {line_short}px; min-height: 3px; border-radius: 2px; }}
        .dock-line-marks .dock-dot.dock-active {{ min-width: {line_long}px; }}
        .dock-line-marks.dock-left .dock-dot, .dock-line-marks.dock-right .dock-dot {{ min-width: 3px; min-height: {line_short}px; }}
        .dock-line-marks.dock-left .dock-dot.dock-active, .dock-line-marks.dock-right .dock-dot.dock-active {{ min-width: 3px; min-height: {line_long}px; }}
        .dock-placeholder {{ border: 1px dashed alpha({c['foreground']}, 0.45); border-radius: {max(4, radius)}px;
            color: alpha({c['foreground']}, 0.7); margin: 4px; }}
        .dock-placeholder.dock-placeholder-drop {{ border-color: {c['accent']}; background: alpha({c['accent']}, 0.25); }}
        separator.dock-separator {{ background: alpha({c['foreground']}, 0.18); min-width: 1px; min-height: 1px; margin: 4px; }}
        .dock-drop-marker {{ background: {c['accent']}; border-radius: 999px;
            box-shadow: 0 0 6px alpha({c['accent']}, 0.8); }}
        popover.dock-menu contents {{ background-color: #282526; color: #ffffff; border: 1px solid {c['accent']};
            border-radius: {radius}px; padding: 5px; min-width: 170px; }}
        popover.dock-menu arrow {{ background-color: #282526; border-color: {c['accent']}; }}
        button.dock-menu-item {{ background: transparent; border-radius: {radius}px; padding: 5px 8px; color: #ffffff; }}
        button.dock-menu-item:hover {{ background: alpha({c['accent']}, 0.35); }}
        button.dock-menu-item label {{ font-size: 13px; font-weight: 500; color: #ffffff; }}
        button.dock-menu-item.destructive label {{ color: #ff9999; }}
        .dock-menu-title {{ font-size: 11px; font-weight: 700; padding: 2px 8px 4px 8px; color: alpha(#ffffff, 0.7); }}
        """
        self.css.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def on_panel_click(self, gesture, count, x, y):
        if count != 2:
            return
        widget = self.panel.pick(x, y, Gtk.PickFlags.DEFAULT)
        if widget is self.spacer:
            return
        while widget is not None and widget is not self.panel:
            if widget.has_css_class("dock-item"):
                return
            widget = widget.get_parent()
        try:
            dock_core.set_setting("dock_transparent", not self.settings["dock_transparent"])
        except OSError as error:
            print("dock: could not save:", error, file=sys.stderr)

    # --- auto-hide ----------------------------------------------------------------

    def on_popup_open(self, _action, value):
        self.popup_open = bool(value and value.get_boolean())
        if self.popup_open:
            self.show_dock()
        else:
            self.schedule_hide()

    def show_dock(self):
        if self.hide_source:
            GLib.source_remove(self.hide_source)
            self.hide_source = 0
        if not self.revealed:
            self.revealed = True
            self.revealer.set_reveal_child(True)

    def schedule_hide(self):
        if not self.settings["dock_autohide"]:
            return
        if self.hide_source:
            GLib.source_remove(self.hide_source)
        self.hide_source = GLib.timeout_add(HIDE_DELAY_MS, self.hide_when_idle)

    def hide_when_idle(self):
        self.hide_source = 0
        if self.menu is not None or self.dragging is not None or self.popup_open:
            self.hide_source = GLib.timeout_add(HIDE_DELAY_MS, self.hide_when_idle)
            return GLib.SOURCE_REMOVE
        self.hide_dock()
        return GLib.SOURCE_REMOVE

    def hide_dock(self):
        # Never while App Manager is open: it keeps the dock shown (also right
        # after a settings change rebuilt the dock, so the change is visible).
        if self.settings["dock_autohide"] and not self.popup_open:
            self.revealed = False
            self.revealer.set_reveal_child(False)

    # --- contents -------------------------------------------------------------------

    def listen_hyprland(self):
        """Hyprland's event socket: windows opening, closing, focus and
        "urgent" (an app wants attention) update the dock at once."""
        signature, runtime = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"), os.environ.get("XDG_RUNTIME_DIR")
        if not signature or not runtime or "/" in signature:
            return
        try:
            connection = Gio.SocketClient().connect(
                Gio.UnixSocketAddress.new(str(Path(runtime) / "hypr" / signature / ".socket2.sock")), None)
        except GLib.Error:
            GLib.timeout_add_seconds(5, lambda: self.listen_hyprland() and False)
            return
        self.events_connection = connection
        self.events = connection.get_input_stream()
        self.event_buffer = b""
        self.event_skipping = False
        self.events.read_bytes_async(EVENT_CHUNK, GLib.PRIORITY_DEFAULT, None, self.on_event_bytes)

    def on_event_bytes(self, stream, result):
        """Events in fixed chunks, split into lines here: a line longer than
        EVENT_LINE_MAX (a window title from a web page) is skipped, never
        buffered whole."""
        try:
            chunk = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            chunk = b""
        if not chunk:  # Hyprland restarted: connect again.
            GLib.timeout_add_seconds(3, lambda: self.listen_hyprland() and False)
            return
        lines, self.event_buffer, self.event_skipping = dock_core.split_events(
            self.event_buffer, chunk, self.event_skipping, EVENT_LINE_MAX)
        for line in lines:
            self.on_event(line)
        stream.read_bytes_async(EVENT_CHUNK, GLib.PRIORITY_DEFAULT, None, self.on_event_bytes)

    def on_event(self, line):
        name, _sep, data = line[:512].partition(">>")
        address = "0x" + data.split(",", 1)[0]
        if name == "urgent" and len(self.urgent) < MAX_URGENT:
            self.urgent.add(address)
        elif name == "activewindowv2":
            self.urgent.discard(address)  # Seen: stop bouncing.
        if name in ("urgent", "activewindowv2", "openwindow", "closewindow", "movewindowv2", "windowtitlev2"):
            if name == "closewindow":
                self.urgent.discard(address)
            if not self.events_source:
                self.events_source = GLib.timeout_add(120, self.on_events_settled)

    def on_events_settled(self):
        self.events_source = 0
        self.poll()
        return GLib.SOURCE_REMOVE

    def poll(self):
        """Hyprland's windows, read on a thread: a slow hyprctl never stalls the dock."""
        if getattr(self, "polling", False):
            return True
        self.polling = True

        def work():
            found = dock_core.clients()
            GLib.idle_add(done, found)

        def done(found):
            self.polling = False
            self.clients = found
            self.refresh()
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()
        return True

    def refresh(self, force=False):
        items = dock_core.items(self.apps, self.settings["dock_pinned"], self.clients)
        active = next((c.get("address") for c in self.clients if c.get("focusHistoryID") == 0), None)
        signature = (tuple((a["desktop_id"], a.get("icon"), tuple(w.get("address") for w in ws), p)
                           for a, ws, p in items), active, frozenset(self.urgent))
        if ((not force and signature == self.signature) or self.dragging is not None or self.menu is not None
                or self.external is not None):
            return
        self.signature = signature
        while child := self.items_box.get_first_child():
            self.items_box.remove(child)
        self.items_box.append(self.make_manager_button())
        vertical = self.edge in ("left", "right")
        separator_orientation = Gtk.Orientation.HORIZONTAL if vertical else Gtk.Orientation.VERTICAL
        self.items_box.append(self.separator(separator_orientation))
        if not any(p for _a, _w, p in items):
            # Nothing pinned yet: a slot that says where to drop apps.
            self.items_box.append(self.make_placeholder())
            if items:
                self.items_box.append(self.separator(separator_orientation))
        shown_unpinned = False
        for app, windows, is_pinned in items:
            if not is_pinned and not shown_unpinned:
                shown_unpinned = True
                if any(p for _a, _w, p in items):
                    self.items_box.append(self.separator(separator_orientation))
            self.items_box.append(self.make_item(app, windows, is_pinned, active))
        GLib.idle_add(self.fit_band)

    @staticmethod
    def separator(orientation):
        separator = Gtk.Separator(orientation=orientation)
        separator.add_css_class("dock-separator")
        return separator

    def make_placeholder(self):
        slot = Gtk.Box()
        slot.add_css_class("dock-placeholder")
        image = Gtk.Image.new_from_icon_name("list-add-symbolic")
        image.set_pixel_size(18)
        image.set_hexpand(True)
        image.set_vexpand(True)
        slot.append(image)
        size = self.settings["dock_icon_size"]
        slot.set_size_request(size, size)
        slot.set_valign(Gtk.Align.CENTER)
        slot.set_halign(Gtk.Align.CENTER)
        slot.set_tooltip_text(_("Drag apps here from App Manager to pin them"))
        self.placeholder = slot
        return slot

    def make_manager_button(self):
        button = Gtk.Button()
        button.add_css_class("dock-item")
        image = Gtk.Image.new_from_icon_name("view-app-grid-symbolic")
        size = self.settings["dock_icon_size"]
        image.set_pixel_size(max(16, size - 12))
        image.set_size_request(size, size)
        image.add_css_class("dock-icon")
        button.set_child(image)
        button.set_tooltip_text("App Manager")
        button.connect("clicked", lambda *_a: self.open_manager())
        return button

    def open_manager(self):
        try:
            subprocess.Popen([str(PLUGIN_DIR / "launcher-toggle")], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    def make_item(self, app, windows, is_pinned, active):
        button = Gtk.Button()
        button.add_css_class("dock-item")
        slot = Gtk.Overlay()
        slot.add_css_class("dock-icon")  # Grows on hover, bounces on alerts.
        image = icon_image(app, self.settings["dock_icon_size"])
        position = self.edge
        if self.settings["dock_bounce"] and any(w.get("address") in self.urgent for w in windows):
            slot.add_css_class("dock-bounce")
        # Room for the running dot on the screen-edge side of the icon.
        {"bottom": image.set_margin_bottom, "top": image.set_margin_top, "left": image.set_margin_start,
         "right": image.set_margin_end}[position](6)
        slot.set_child(image)
        if windows and self.settings["dock_indicator_shown"]:
            dot = Gtk.Box()
            dot.add_css_class("dock-dot")
            if any(w.get("address") == active for w in windows):
                dot.add_css_class("dock-active")
            dot.set_halign({"left": Gtk.Align.START, "right": Gtk.Align.END}.get(position, Gtk.Align.CENTER))
            dot.set_valign({"bottom": Gtk.Align.END, "top": Gtk.Align.START}.get(position, Gtk.Align.CENTER))
            dot.set_can_target(False)
            slot.add_overlay(dot)
        button.set_child(slot)
        count = len(windows)
        button.set_tooltip_text(app["name"] if count < 2 else f"{app['name']} ({count})")
        button.connect("clicked", lambda *_a: self.activate_app(app, windows))
        self.add_neighbour_growth(button)
        middle = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
        middle.connect("released", lambda *_a: launch_app(app))  # Always a new window.
        button.add_controller(middle)
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", lambda g, *_a: (g.set_state(Gtk.EventSequenceState.CLAIMED),
                                                 self.show_menu(button, app, windows, is_pinned)))
        button.add_controller(right)
        if is_pinned:
            button.dock_pinned_id = app["desktop_id"]
            self.add_reorder(button, app)
        return button

    def add_neighbour_growth(self, button):
        """macOS-like: the icons next to the one under the pointer grow halfway."""
        motion = Gtk.EventControllerMotion()

        def mark(on):
            for sibling in (button.get_prev_sibling(), button.get_next_sibling()):
                if sibling is not None and sibling.has_css_class("dock-item"):
                    (sibling.add_css_class if on else sibling.remove_css_class)("dock-near")
        motion.connect("enter", lambda *_a: mark(True))
        motion.connect("leave", lambda *_a: mark(False))
        button.add_controller(motion)

    def activate_app(self, app, windows):
        """Not running: start it. Running: focus its last window; already
        focused: the next one (cycles through its windows)."""
        if not windows:
            launch_app(app)
            return
        active = next((i for i, w in enumerate(windows) if w.get("focusHistoryID") == 0), None)
        target = windows[(active + 1) % len(windows)] if active is not None else windows[0]
        hyprctl_dispatch("hl.dsp.focus(" + window_arg(target) + ")")

    # --- menu --------------------------------------------------------------------------

    def show_menu(self, button, app, windows, is_pinned):
        if self.menu is not None:
            self.menu.popdown()
        popover = Gtk.Popover()
        popover.add_css_class("dock-menu")
        popover.set_parent(button)
        popover.set_position({"bottom": Gtk.PositionType.TOP, "top": Gtk.PositionType.BOTTOM,
                              "left": Gtk.PositionType.RIGHT, "right": Gtk.PositionType.LEFT}[self.edge])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=app["name"], xalign=0)
        title.add_css_class("dock-menu-title")
        box.append(title)

        def item(label, callback, destructive=False):
            entry = Gtk.Button(label=label)
            entry.add_css_class("dock-menu-item")
            entry.get_child().set_xalign(0)
            if destructive:
                entry.add_css_class("destructive")
            entry.connect("clicked", lambda *_a: (popover.popdown(), callback()))
            box.append(entry)

        # Browsers bring their own "New Window" action: no second one then.
        if not (windows and dock_core.has_new_window_action(app)):
            item(_("New window") if windows else _("Open"), lambda: launch_app(app))
        for action in app.get("actions", [])[:6]:
            item(action["name"], lambda a=action["id"]: launch_action(app, a))
        if is_pinned:
            item(_("Unpin from dock"), lambda: dock_core.unpin(app["desktop_id"]))
        else:
            item(_("Pin to dock"), lambda: dock_core.pin(app["desktop_id"]))
        if windows:
            item(_("Close") if len(windows) == 1 else _("Close all windows"),
                 lambda: [hyprctl_dispatch("hl.dsp.window.close(" + window_arg(w) + ")") for w in windows],
                 destructive=True)
        popover.set_child(box)
        popover.connect("closed", self.on_menu_closed)
        self.menu = popover
        popover.popup()

    def on_menu_closed(self, popover):
        def cleanup():
            popover.unparent()
            if self.menu is popover:
                self.menu = None
                self.refresh(force=True)
                if self.settings["dock_autohide"]:
                    self.schedule_hide()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(cleanup)

    # --- apps dragged in from App Manager -------------------------------------------------

    def add_external_drop(self):
        """Apps dragged out of App Manager (list rows, cards, folder icons)
        are pinned where the bar shows. Only App Manager's own format is
        accepted, read with a small cap (dock_core.dropped_id)."""
        target = Gtk.DropTargetAsync.new(Gdk.ContentFormats.new(["text/plain;charset=utf-8", "text/plain"]),
                                         Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("accept", lambda _t, drop: self.dragging is None and (
            drop.get_formats().contain_mime_type("text/plain;charset=utf-8")
            or drop.get_formats().contain_mime_type("text/plain")))
        target.connect("drag-enter", self.on_external_over)
        target.connect("drag-motion", self.on_external_over)
        target.connect("drag-leave", lambda *_a: self.end_external())
        target.connect("drop", self.on_external_drop)
        self.panel.add_controller(target)

    def show_marker(self, button, after):
        """The bar in the gap before (or after) button."""
        ok, bounds = button.compute_bounds(self.items_box)
        if not ok:
            return
        vertical = self.edge in ("left", "right")
        marker = self.marker
        gap = self.settings["dock_spacing"]
        offset = gap / 2 if after else -gap / 2
        if vertical:
            edge = bounds.get_y() + (bounds.get_height() if after else 0)
            marker.set_halign(Gtk.Align.FILL)
            marker.set_valign(Gtk.Align.START)
            marker.set_size_request(-1, MARKER)
            marker.set_margin_start(2)
            marker.set_margin_end(2)
            marker.set_margin_top(max(0, round(edge + offset - MARKER / 2)))
        else:
            edge = bounds.get_x() + (bounds.get_width() if after else 0)
            marker.set_halign(Gtk.Align.START)
            marker.set_valign(Gtk.Align.FILL)
            marker.set_size_request(MARKER, -1)
            marker.set_margin_top(2)
            marker.set_margin_bottom(2)
            marker.set_margin_start(max(0, round(edge + offset - MARKER / 2)))
        marker.set_visible(True)

    def hide_marker(self):
        self.marker.set_visible(False)

    def pinned_buttons(self):
        result, child = [], self.items_box.get_first_child()
        while child is not None:
            if hasattr(child, "dock_pinned_id"):
                result.append(child)
            child = child.get_next_sibling()
        return result

    def on_external_over(self, _target, _drop, x, y):
        self.show_dock()
        vertical = self.edge in ("left", "right")
        widget = self.panel.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.panel and not hasattr(widget, "dock_pinned_id"):
            widget = widget.get_parent()
        buttons = self.pinned_buttons()
        placeholder = getattr(self, "placeholder", None)
        if hasattr(widget, "dock_pinned_id"):
            point = self.panel.translate_coordinates(widget, x, y)
            local = (point[1] / max(1, widget.get_height())) if vertical else (point[0] / max(1, widget.get_width()))
            after = local > 0.5
            self.external = (widget.dock_pinned_id, after)
            self.show_marker(widget, after)
        else:
            self.external = (None, True)  # Elsewhere on the dock: after the last pinned app.
            if buttons:
                self.show_marker(buttons[-1], True)
            elif placeholder is not None and placeholder.get_parent() is not None:
                self.hide_marker()
                placeholder.add_css_class("dock-placeholder-drop")
        return Gdk.DragAction.COPY

    def end_external(self):
        self.hide_marker()
        placeholder = getattr(self, "placeholder", None)
        if placeholder is not None:
            placeholder.remove_css_class("dock-placeholder-drop")
        self.external = None
        GLib.idle_add(lambda: self.refresh(force=True) and False)
        self.schedule_hide()

    def on_external_drop(self, _target, drop, _x, _y):
        where = self.external or (None, True)
        drop.read_async(["text/plain;charset=utf-8", "text/plain"], GLib.PRIORITY_DEFAULT, None,
                        self.on_drop_stream, (drop, where))
        return True

    def on_drop_stream(self, drop, result, data):
        drop, where = data
        try:
            stream, _mime = drop.read_finish(result)
        except GLib.Error:
            drop.finish(0)
            self.end_external()
            return
        stream.read_bytes_async(dock_core.MAX_DROP_BYTES + 1, GLib.PRIORITY_DEFAULT, None,
                                self.on_drop_bytes, (drop, where))

    def on_drop_bytes(self, stream, result, data):
        drop, (target_id, after) = data
        try:
            chunk = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            chunk = None
        stream.close_async(GLib.PRIORITY_DEFAULT, None, None, None)
        desktop_id = dock_core.dropped_id(chunk)
        known = {a["desktop_id"] for a in self.apps if "plugin_id" not in a and "tray_id" not in a}
        if desktop_id in known:
            drop.finish(Gdk.DragAction.COPY)
            try:
                dock_core.set_pinned(dock_core.insert_pinned(dock_core.pinned(), desktop_id, target_id, after))
            except OSError as error:
                print("dock: could not save:", error, file=sys.stderr)
        else:
            drop.finish(0)
        self.end_external()

    # --- reorder pinned apps by drag and drop ----------------------------------------

    def add_reorder(self, button, app):
        vertical = self.edge in ("left", "right")
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag.connect("prepare", lambda *_a: Gdk.ContentProvider.new_for_value(DRAG_PREFIX + app["desktop_id"]))
        drag.connect("drag-begin", lambda source, _d: (setattr(self, "dragging", app["desktop_id"]),
                                                      source.set_icon(Gtk.WidgetPaintable.new(button), 20, 20)))
        drag.connect("drag-end", lambda *_a: self.end_drag())
        button.add_controller(drag)
        target = Gtk.DropTargetAsync.new(None, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        # Only our own drag (one pointer); the offered data is never read.
        accepts = lambda: self.dragging is not None and self.dragging != app["desktop_id"]
        target.connect("accept", lambda *_a: accepts())

        def over(_t, _d, x, y):
            if not accepts():
                return 0
            after = (y > button.get_height() / 2) if vertical else (x > button.get_width() / 2)
            self.drop_after = after
            self.show_marker(button, after)
            return Gdk.DragAction.COPY
        target.connect("drag-enter", over)
        target.connect("drag-motion", over)
        target.connect("drag-leave", lambda *_a: self.hide_marker())

        def dropped(_t, drop, _x, _y):
            if not accepts():
                drop.finish(0)
                return False
            drop.finish(Gdk.DragAction.COPY)
            ids = dock_core.move_pinned(self.settings["dock_pinned"], self.dragging, app["desktop_id"],
                                        getattr(self, "drop_after", False))
            try:
                dock_core.set_pinned(ids)
            except OSError as error:
                print("dock: could not save:", error, file=sys.stderr)
            return True
        target.connect("drop", dropped)
        button.add_controller(target)

    def end_drag(self):
        self.hide_marker()
        self.dragging = None
        self.settings = dock_core.settings()
        self.refresh(force=True)  # Rebuilt only now: a rebuild during the drag cancels it.
        self.schedule_hide()


if __name__ == "__main__":
    sys.exit(Dock().run([sys.argv[0]]))
