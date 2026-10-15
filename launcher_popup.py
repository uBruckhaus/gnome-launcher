#!/usr/bin/env python3
"""AppLauncher as a Wayland layer-shell popup, rather than an app window."""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# GTK4 layer shell must load before GTK's Wayland client library. The Python
# bindings load GTK first, so preload the layer-shell library on re-exec.
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

# Once Gtk4LayerShell is loaded into this process by ld.so, remove it from
# LD_PRELOAD so child processes and launched applications (like Steam / pressure-vessel)
# do not inherit it and fail to start.
_preloads = [p for p in os.environ.get("LD_PRELOAD", "").split(":") if p and p != layer_shell_library]
if _preloads:
    os.environ["LD_PRELOAD"] = ":".join(_preloads)
else:
    os.environ.pop("LD_PRELOAD", None)

import atexit
import concurrent.futures
import faulthandler
import colorsys
import copy
import shutil
from launcher_core import (
    TERMINALS, app_process_name, browser_web_app, background_process_names, child_process_names, client_matches_app,
    process_pids, terminal_command,
    DESKTOP_DIRS, HIDDEN_FILE, HIDDEN_PLUGINS_FILE, get_appimage_directories,
    admin_action_enabled, admin_command, running_first_enabled, set_running_first, saved_view, set_view,
    saved_wide_layout, set_wide_layout, view_buttons_shown, set_view_buttons_shown, saved_corners, set_corners, read_bounded_output,
    saved_size, set_saved_size, saved_position, set_saved_position,
    plugin_manifests, set_admin_action, forget_saved_admin_action, clipboard_text, launch_action, launch_app, launch_as_admin, load_hidden_apps, open_plugin, parse_desktop_files, parse_plugins,
    plugin_removal_note, save_hidden_apps, set_in_bar, BAR_SECTIONS, USER_PLUGINS_DIR,
    icon_bytes,
)
from palette import ThemePalette
import i18n
from i18n import _, N_, ngettext
import details
import groups
from layer_watch import LayerWatch
import pidfile
import shortcuts
import threading
import tray
import updates
import newapps
import icons
import corners
import dock_core
import previews
import tempfile
import cairo

# Height of the app list and the shortcut page, whatever they hold.
LIST_HEIGHT = 590
CARD_WIDTH = 420
MIN_LIST_HEIGHT = 300
# Resized wide enough for two cards side by side, the list turns into the
# overview: cards in columns, filtered by group in a bar at the bottom.
TILE_WIDTH = 196       # A card's narrowest width.
TILE_MAX_WIDTH = 240   # The widest it gets: limits the popup's width.
TILE_SPACING = 10
MAX_COLUMNS = 6
DRAG_PREFIX = "app-manager-entry:"  # A card dragged onto a group chip.
DRAG_GROUP_PREFIX = "app-manager-group:"  # A group dragged to a new place.
# Folder layout: each group a folder of FOLDER_COLUMNS x FOLDER_ROWS icons.
FOLDER_COLUMNS = 3
FOLDER_ROWS = 3
FOLDER_ICON = 36       # The smallest icon: below it the page scrolls.
FOLDER_ICON_MAX = 96   # The largest, when few folders fill a big popup.
FOLDER_CELL = FOLDER_ICON + 14  # The icon, its button's padding and border.
# Under the icons: the box's spacing and the name label (12 px bold).
FOLDER_NAME_HEIGHT = 6 + 18
# A folder's narrowest width: its icons, their spacing, the frame's padding
# and border, and the tile's padding. Two fit side by side at the default size.
FOLDER_TILE_WIDTH = FOLDER_COLUMNS * FOLDER_CELL + (FOLDER_COLUMNS - 1) * 6 + 2 * 6 + 2 + 2 * 4
CARD_PADDING = 34      # The card's padding, left and right.
DROP_MARKER = 4        # The drop marker bar's thickness.
SETTINGS_CARD_WIDTH = 350  # A settings card: fits the small popup, the same for every group.
HANG_LOG = Path.home() / ".local/state/applauncher/hang.log"
WATCHDOG_SECONDS = 20  # The main loop silent this long: hung, log and exit.
MAX_CARD_WIDTH = CARD_PADDING + MAX_COLUMNS * TILE_MAX_WIDTH + (MAX_COLUMNS - 1) * TILE_SPACING


def grid_columns(card_width):
    """Card columns at this popup width; 1 means the list."""
    inner = card_width - CARD_PADDING
    return max(1, min(MAX_COLUMNS, (inner + TILE_SPACING) // (TILE_WIDTH + TILE_SPACING)))


def folder_columns(card_width):
    """Folders side by side at this popup width (the folder layout's start page)."""
    inner = card_width - CARD_PADDING
    return max(1, (inner + TILE_SPACING) // (FOLDER_TILE_WIDTH + TILE_SPACING))


def folder_tile_size(icon):
    """A folder's narrowest width and its height with icons of this size."""
    cell = icon + 14
    icons = FOLDER_COLUMNS * cell + (FOLDER_COLUMNS - 1) * 6
    rows = FOLDER_ROWS * cell + (FOLDER_ROWS - 1) * 6
    # Frame padding (6 / 10) and border, then the tile's padding (4 / 4 + 6).
    return icons + 2 * 6 + 2 + 2 * 4, rows + 2 * 10 + 2 + 4 + 6 + FOLDER_NAME_HEIGHT


def folder_fit(count, card_width, height):
    """Folders per row and icon size so that `count` folders fill the page
    (card width x page height): the column count that gives the largest
    icons wins. Too many to fit at FOLDER_ICON: the smallest size, as many
    per row as fit, and the page scrolls."""
    inner = card_width - CARD_PADDING
    if count < 1:
        return folder_columns(card_width), FOLDER_ICON
    base_w, base_h = folder_tile_size(0)
    best_columns, best_icon = 1, -1
    for columns in range(1, count + 1):
        rows = -(-count // columns)
        tile_w = (inner - (columns - 1) * TILE_SPACING) / columns
        tile_h = (height - (rows - 1) * TILE_SPACING) / rows
        icon = int(min((tile_w - base_w) / FOLDER_COLUMNS - 14, (tile_h - base_h) / FOLDER_ROWS - 14))
        if icon > best_icon:
            best_columns, best_icon = columns, icon
    if best_icon < FOLDER_ICON:
        return folder_columns(card_width), FOLDER_ICON
    return best_columns, min(best_icon, FOLDER_ICON_MAX)


def folder_icons_height(count, columns, icon, height):
    """The height of a folder's icon area that fills the page's height: the
    rows spread out when the width limits the icons. Never less than the
    three rows themselves (then the page scrolls)."""
    rows_height = FOLDER_ROWS * (icon + 14) + (FOLDER_ROWS - 1) * 6
    rows = -(-max(count, 1) // max(columns, 1))
    tile_h = (height - (rows - 1) * TILE_SPACING) / rows
    return max(rows_height, int(tile_h - (folder_tile_size(icon)[1] - rows_height)))

# Where each browser lists its installed web apps.
APPS_PAGES = {"Chrome": "chrome://apps", "Chromium": "chrome://apps", "Brave": "brave://apps",
              "Edge": "edge://apps", "Vivaldi": "vivaldi://apps"}


ICON_DECODE_SIZE = 128         # the largest folder icons; four times the 32 px rows, for HiDPI
_icon_cache = {}               # (path, mtime_ns, size) -> Gdk.Texture or None


def icon_texture(path):
    """A small texture for an icon file, or None to use the themed icon.

    `icon_bytes` reads the file once under a byte cap and refuses headers
    that declare an oversized canvas; the loader then scales to
    ICON_DECODE_SIZE as it decodes (SVG is rendered at that size directly).
    Results are cached, so rebuilding the list never decodes again."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key in _icon_cache:
        return _icon_cache[key]
    texture = None
    data = icon_bytes(path)
    if data is not None:
        def fit(loader, width, height):
            scale = min(1.0, ICON_DECODE_SIZE / max(width, height, 1))
            loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))

        loader = GdkPixbuf.PixbufLoader()
        loader.connect("size-prepared", fit)
        try:
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            texture = Gdk.Texture.new_for_pixbuf(pixbuf) if pixbuf is not None else None
        except GLib.Error:
            try:
                loader.close()
            except GLib.Error:
                pass
    if len(_icon_cache) > 512:
        _icon_cache.clear()
    _icon_cache[key] = texture
    return texture


def write_pid_file():
    try:
        pidfile.write()
    except OSError:
        pass


def cleanup_pid():
    try:
        pidfile.remove_if_own()
    except OSError:
        pass
    leave_capture()


def plain_text(text, limit=300):
    """Text with no markup or control characters, for notifications."""
    text = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in str(text))
    return re.sub(r"[<>&]", "", text)[:limit]


def hyprctl(*args):
    try:
        result = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


_capturing = False


def enter_capture():
    """Switch Hyprland to an empty submap: bound combos (even Omarchy's own)
    then reach the shortcut page instead of running."""
    global _capturing
    enter = f'hl.dsp.submap("{shortcuts.CAPTURE_SUBMAP}")'
    if hyprctl("dispatch", enter) != "ok":
        # Not in the generated file yet (no shortcut saved since updating).
        hyprctl("eval", shortcuts.CAPTURE_LUA)
        if hyprctl("dispatch", enter) != "ok":
            return False
    _capturing = True
    return True


def leave_capture():
    global _capturing
    if _capturing and hyprctl("submap") == shortcuts.CAPTURE_SUBMAP:
        hyprctl("dispatch", 'hl.dsp.submap("reset")')
    _capturing = False


atexit.register(cleanup_pid)


def hyprland_rounding():
    """The windows' rounding (theme or looknfeel.lua), or None if unknown."""
    out = read_bounded_output(["hyprctl", "getoption", "decoration:rounding", "-j"], max_bytes=4096, timeout=2)
    return corners.parse_rounding(out or "")


# A key pressed alone this soon after a combo makes a two-step shortcut.
TWO_STEP_WINDOW_US = 1_200_000

# Non-letter keys offered on the shortcut page, as GDK/Hyprland key names.
OTHER_KEYS = ("Return", "space", "BackSpace", "Delete", "Home", "End")


PLUGIN_DIR = Path(__file__).resolve().parent


def is_widget(app):
    """Entries of the widget list: shell plugins and tray icons."""
    return "plugin_id" in app or "tray_id" in app


def _mix(a, b, amount):
    """Blend hex colour a toward b by amount (0..1)."""
    a, b = a.lstrip("#"), b.lstrip("#")
    try:
        mixed = [round(int(a[i:i + 2], 16) * (1 - amount) + int(b[i:i + 2], 16) * amount) for i in (0, 2, 4)]
    except ValueError:
        return "#" + a
    return "#" + "".join(f"{v:02x}" for v in mixed)


def _hue(hex_code):
    h = hex_code.lstrip("#")
    try:
        return colorsys.rgb_to_hsv(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))[0]
    except ValueError:
        return 0.0


def key_owner_colors(colors):
    """Free, yours (yellow), Omarchy (orange) and error (red) colours. Taken
    keys are hints — they can be rebound. Some themes' red is nearly their
    orange; push it toward a clear red so errors stand out."""
    orange, red = colors["orange"], colors["red"]
    distance = abs(_hue(red) - _hue(orange))
    distance = min(distance, 1 - distance)
    if distance < 0.06:
        red = _mix(red, "#e5383b", 0.6)
    return colors["green"], colors["yellow"], orange, red


def code_stamp():
    """Modification times of the launcher's own code, to notice updates."""
    stamp = {}
    for path in PLUGIN_DIR.glob("*.py"):
        try:
            stamp[path.name] = path.stat().st_mtime_ns
        except OSError:
            pass
    return stamp


# Key buttons on the shortcut page, by who has the combo.
KEY_STYLES = {
    shortcuts.FREE: "sc-free-key", shortcuts.SAME: "sc-own-key", shortcuts.RETARGET: "sc-own-key",
    shortcuts.LAUNCHER: "sc-yours-key", shortcuts.CUSTOM: "sc-yours-key", shortcuts.OMARCHY: "sc-omarchy-key",
}


class Launcher(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.omarchy.simplelauncherforfools")
        self.window = None
        self.apps = []
        self.plugins = []
        # The list shown: "apps", or "plugins" (shell widgets, panels and
        # overlays). Each keeps its own hidden entries and show-hidden toggle.
        self.mode = "plugins" if os.environ.pop("SIMPLE_LAUNCHER_MODE", "") == "plugins" else "apps"
        self.hidden_by_mode = {"apps": load_hidden_apps(HIDDEN_FILE),
                               "plugins": load_hidden_apps(HIDDEN_PLUGINS_FILE)}
        self.show_hidden_by_mode = {"apps": False, "plugins": False}
        self.running_first = running_first_enabled()
        self.admin_auth_pending = False
        # Omarchy's menus (Super + Space, …) open underneath this popup: close for them.
        self.layer_watch = LayerWatch("simple-launcher-for-fools", self.on_menu_opened)
        try:
            forget_saved_admin_action()
        except OSError:
            pass
        # "list" or "groups" (collapsible groups); apps and plugins (Tab) each
        # have their own groups.
        self.view = saved_view()
        # The list | groups | origin buttons in the small popup's header are
        # a setting, off by default; then the popup shows the plain list.
        self.view_buttons_on = view_buttons_shown()
        # The overview (popup resized to two or more columns): the same
        # entries as cards, one group (or all) at a time, picked in the
        # filter bar at the bottom. The filter is per page, like the groups.
        self.overview = False
        self.overview_filter = {"apps": "all", "plugins": "all"}
        # "cards", or "folders": "All" shows each group as a folder of icons.
        self.wide_layout = saved_wide_layout()
        self.height_pages = []  # Sub-pages as high as the list.
        self.card_w, self.list_h = CARD_WIDTH, LIST_HEIGHT
        self.resize_start = None
        self.position = saved_position()  # Top-left after a move by the header; None: centred.
        self.move_start = None
        self.groups_all = groups.load()
        self.group_name_target = None  # (group id or None, app to put in it or None)
        self.details_token = 0
        self.clients = []
        self.window_children = {}  # pid -> child process names, per refresh.
        self.process_names = set()
        self.theme = ThemePalette()
        self.search_query = ""
        self.shortcut_state = shortcuts.load_state()
        self.list_bindings = None  # Snapshot for the list chips, refreshed on open.
        self.shortcut_app = None
        self.shortcut_bindings = None
        self.actions_popover = None
        self.code_stamp = code_stamp()
        self.app_monitors = []
        self.apps_changed_source = 0
        # Available updates (updates.check), refreshed in the background.
        self.updates = {"apps": {}, "plugins": {}}
        self.updates_at = None
        self.updates_running = False
        self.seen = newapps.load()  # When each entry was first seen (the new star).
        self.icon_overrides = icons.load()  # Icons chosen by hand (Change icon…).
        self.icon_app = None
        self.icon_search_token = 0
        self.icon_tmp = None

    @property
    def groups(self):
        """The groups of the page shown: apps or plugins."""
        return self.groups_all[self.mode]

    @property
    def list_kind(self):
        return self.mode

    def hidden_ids_for(self, app):
        return self.hidden_by_mode["plugins" if is_widget(app) else "apps"]

    @property
    def show_hidden(self):
        return self.show_hidden_by_mode[self.list_kind]

    def start_watchdog(self):
        """If the main loop ever hangs, this full-screen layer would block the
        whole desktop. A heartbeat re-arms faulthandler every 2 s; after 20 s
        without one it writes every thread's stack to hang.log and exits, so
        the desktop is free again and the next click starts a fresh popup."""
        try:
            HANG_LOG.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(HANG_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self.hang_log = os.fdopen(fd, "a", buffering=1)
        except OSError:
            self.hang_log = sys.stderr

        def beat():
            faulthandler.dump_traceback_later(WATCHDOG_SECONDS, exit=True, file=self.hang_log)
            return GLib.SOURCE_CONTINUE
        beat()
        GLib.timeout_add_seconds(2, beat)

    def do_shutdown(self):
        cleanup_pid()
        super().do_shutdown()

    def do_activate(self):
        if self.window is None:
            self.build_window()
            self.hold()  # Keep the singleton alive while its popup is hidden.
            signal.signal(signal.SIGUSR1, lambda *_: GLib.idle_add(self.toggle))
            # launcher-toggle apps / plugins: open (or close) that list.
            signal.signal(signal.SIGUSR2, lambda *_: GLib.idle_add(self.toggle, "apps"))
            signal.signal(signal.SIGRTMIN + 1, lambda *_: GLib.idle_add(self.toggle, "plugins"))
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda *_: sys.exit(0))
            GLib.timeout_add(100, lambda: True)  # Let Python dispatch SIGUSR1.
            GLib.timeout_add_seconds(1, self.poll)
            self.start_watchdog()
            self.watch_app_dirs()
            write_pid_file()
        self.open()

    def build_window(self):
        settings = Gtk.Settings.get_default()
        if settings is not None:
            # No middle-click paste anywhere in the popup (see guard_text_field).
            settings.set_property("gtk-enable-primary-paste", False)
        window = Gtk.ApplicationWindow(application=self)
        window.set_decorated(False)
        window.set_title("App Manager")
        window.set_name("launcher-window")
        Gtk4LayerShell.init_for_window(window)
        Gtk4LayerShell.set_namespace(window, "simple-launcher-for-fools")
        Gtk4LayerShell.set_layer(window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(window, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM,
                     Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
            Gtk4LayerShell.set_anchor(window, edge, True)
        Gtk4LayerShell.set_exclusive_zone(window, 0)

        overlay = Gtk.Overlay()
        overlay.add_css_class("launcher-overlay")
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_halign(Gtk.Align.FILL)
        overlay.set_valign(Gtk.Align.FILL)
        window.set_child(overlay)
        overlay.set_child(Gtk.Box())
        click = Gtk.GestureClick()
        click.connect("released", self.on_overlay_click)  # Close on release so the bar never sees a half-finished click.
        overlay.add_controller(click)
        # Resizing: the grip in the card's corner. The drag is tracked on the
        # full-screen overlay, which stays put while the card grows around
        # its centre (the grip moves with the card).
        sizer = Gtk.GestureDrag()
        sizer.connect("drag-begin", self.on_resize_begin)
        sizer.connect("drag-update", self.on_resize_update)
        sizer.connect("drag-end", self.on_resize_end)
        overlay.add_controller(sizer)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("launcher-card")
        card.set_size_request(CARD_WIDTH, -1)
        frame = Gtk.Overlay()
        frame.set_child(card)
        frame.set_halign(Gtk.Align.CENTER)
        frame.set_valign(Gtk.Align.CENTER)
        frame.set_margin_top(24)
        frame.set_margin_bottom(24)
        grip = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        grip.add_css_class("launcher-grip")
        grip.set_pixel_size(14)
        grip.set_halign(Gtk.Align.END)
        grip.set_valign(Gtk.Align.END)
        grip.set_cursor_from_name("se-resize")
        grip.set_tooltip_text(_("Drag to resize · double-click for the default size"))
        frame.add_overlay(grip)
        overlay.add_overlay(frame)
        self.card_frame, self.grip = frame, grip

        header = Gtk.Box(spacing=8)
        # Double-click the header: left goes back to the default size, right
        # maximizes. The card always stays centred.
        header_clicks = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        header_clicks.connect("pressed", self.on_header_click)
        back_button = Gtk.Button()
        back_button.set_tooltip_text(_("Back to apps (Esc)"))
        back_button.add_css_class("flat")
        back_button.add_css_class("launcher-back-btn")
        # "← Back" and the key that does the same: Esc on the shortcut page
        # (Backspace is a key you may want to bind there), ⌫ on the settings page.
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        esc = Gtk.Label(label="Esc")
        esc.add_css_class("launcher-shortcut-chip")
        esc.set_valign(Gtk.Align.CENTER)
        back_box.append(esc)
        back_button.set_child(back_box)
        back_button.set_visible(False)
        back_button.connect("clicked", lambda *_: self.close_subpage())
        self.back_chip = esc
        title_side = Gtk.Box(spacing=8)  # Back button and title, left of the hint.
        title_side.append(back_button)
        title = Gtk.Label(label=_("Apps"))
        title.add_css_class("launcher-title")
        # FILL + xalign 0, not START: START gives the label only its natural
        # width, which GTK measures too short for letter-spaced section titles.
        title.set_xalign(0)
        title.set_halign(Gtk.Align.FILL)
        title.set_ellipsize(3)
        title.set_valign(Gtk.Align.CENTER)
        title_side.append(title)
        # Small, centred between the title and the header buttons: what the
        # mouse does with the header. No natural width of its own (the full
        # text would widen the card): it takes the room left and ends in "…".
        mouse_hint = Gtk.Label(xalign=0.5)
        mouse_hint.add_css_class("launcher-header-hint")
        mouse_hint.set_ellipsize(3)
        mouse_hint.set_max_width_chars(1)
        mouse_hint.set_hexpand(True)
        mouse_hint.set_valign(Gtk.Align.CENTER)
        mouse_hint.set_margin_start(6)
        mouse_hint.set_margin_end(6)
        self.mouse_hint = mouse_hint
        # App list only: running apps in a group of their own on top. A dot,
        # like the run dot on the icons (the play icon is the Open action).
        running_button = Gtk.Button(icon_name="media-record-symbolic")
        running_button.add_css_class("flat")
        running_button.add_css_class("launcher-header-btn")
        running_button.add_css_class("launcher-running-btn")
        running_button.connect("clicked", self.toggle_running_first)
        header.append(running_button)
        self.running_button = running_button
        # List or groups, as a two-part switch; the shown one is marked.
        view_box = Gtk.Box()
        view_box.add_css_class("linked")
        view_box.add_css_class("launcher-view-switch")
        self.view_buttons = {}
        for view, icon in (("list", "view-list-symbolic"), ("groups", "folder-symbolic"),
                           ("origin", "package-x-generic-symbolic")):
            button = Gtk.Button(icon_name=icon)
            button.add_css_class("flat")
            button.add_css_class("launcher-header-btn")
            button.connect("clicked", lambda _b, v=view: self.toggle_view(v))
            view_box.append(button)
            self.view_buttons[view] = button
        header.append(view_box)
        self.view_button = view_box
        mode_button = Gtk.Button()
        mode_button.add_css_class("flat")
        mode_button.add_css_class("launcher-header-btn")
        mode_button.connect("clicked", self.toggle_mode)
        header.append(mode_button)
        hidden_button = Gtk.Button(icon_name="view-reveal-symbolic")
        hidden_button.set_tooltip_text(_("Show hidden apps"))
        hidden_button.add_css_class("flat")
        hidden_button.add_css_class("launcher-header-btn")
        hidden_button.connect("clicked", self.toggle_hidden)
        header.append(hidden_button)
        # The same Nerd Font gear (U+F0493) in a small bordered box as the other plugins.
        settings_button = Gtk.Button()
        gear = Gtk.Label(label="\U000F0493")
        gear.add_css_class("launcher-gear")
        settings_button.set_child(gear)
        settings_button.set_tooltip_text(_("Settings"))
        settings_button.add_css_class("flat")
        settings_button.add_css_class("launcher-header-btn")
        settings_button.add_css_class("launcher-gear-btn")
        settings_button.connect("clicked", lambda *_: self.open_settings_page())
        header.append(settings_button)
        self.settings_button = settings_button
        header_bar = Gtk.Box()
        header_bar.append(title_side)
        header_bar.append(mouse_hint)
        header_bar.append(header)
        header_bar.add_controller(header_clicks)
        # Drag the header to move the card (Super + drag belongs to Hyprland,
        # and a layer surface is not a window it moves). The drag itself is
        # tracked on the overlay, see on_resize_begin.
        header_bar.set_cursor_from_name("grab")
        title.set_tooltip_text(_("Drag to move"))
        card.append(header_bar)
        self.header_bar = header_bar

        # Asks before deleting hand-written shortcuts of removed apps.
        question = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        question.add_css_class("sc-status")
        question.add_css_class("sc-info")
        question.set_visible(False)
        question_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
        question.append(question_label)
        answers = Gtk.Box(spacing=6)
        answers.set_halign(Gtk.Align.END)
        keep = Gtk.Button(label=_("Keep"))
        keep.add_css_class("sc-small")
        keep.connect("clicked", lambda *_: self.answer_dead_binds(False))
        answers.append(keep)
        delete = Gtk.Button(label=_("Delete"))
        delete.add_css_class("sc-primary")
        delete.add_css_class("sc-danger-btn")
        delete.connect("clicked", lambda *_: self.answer_dead_binds(True))
        answers.append(delete)
        question.append(answers)
        card.append(question)
        self.dead_question, self.dead_question_label, self.dead_binds = question, question_label, []

        scroller = Gtk.ScrolledWindow()
        # A fixed height: the card never shrinks with a short list, a search
        # or the screen it opens on, and the shortcut page gets the same.
        scroller.set_size_request(420, -1)
        scroller.set_min_content_height(LIST_HEIGHT)
        scroller.set_max_content_height(LIST_HEIGHT)
        # EXTERNAL keeps wheel/touchpad/keyboard scrolling but hides the scrollbar.
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(140)
        stack.set_vhomogeneous(False)
        stack.set_interpolate_size(False)
        stack.add_named(scroller, "list")
        card.append(stack)
        rows = Gtk.ListBox()
        rows.set_selection_mode(Gtk.SelectionMode.SINGLE)
        rows.add_css_class("launcher-list")
        rows.connect("row-activated", self.activate_row)
        rows.set_header_func(self.list_header)
        scroller.set_child(rows)

        # The overview page: cards in as many columns as the card is wide.
        grid_scroller = Gtk.ScrolledWindow()
        grid_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        grid = Gtk.FlowBox()
        grid.add_css_class("launcher-grid")
        grid.set_selection_mode(Gtk.SelectionMode.SINGLE)
        grid.set_activate_on_single_click(True)
        grid.set_homogeneous(True)
        grid.set_valign(Gtk.Align.START)
        grid.set_row_spacing(TILE_SPACING)
        grid.set_column_spacing(TILE_SPACING)
        grid.connect("child-activated", lambda _g, tile: self.activate_tile(tile))
        grid.connect("selected-children-changed", lambda *_a: self.mark_selected_group())
        grid.set_vadjustment(grid_scroller.get_vadjustment())  # Keyboard moves scroll along.
        grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # The drop marker for reordering folders: a bar in the gap between two.
        grid_layer = Gtk.Overlay()
        grid_layer.set_child(grid)
        self.grid_marker = self.drop_marker()
        grid_layer.add_overlay(self.grid_marker)
        grid_box.append(grid_layer)
        # Folder layout: a filter field above the folders (typing anywhere
        # goes there too). Kept in step with search_query.
        filter_entry = Gtk.SearchEntry()
        filter_entry.add_css_class("launcher-filter-entry")
        filter_entry.set_placeholder_text(_("Type to filter"))
        filter_entry.set_max_width_chars(1)
        filter_entry.set_hexpand(True)
        filter_entry.set_visible(False)
        filter_entry.get_delegate().set_max_length(200)
        filter_entry.connect("search-changed", self.on_filter_entry_changed)
        filter_entry.connect("activate", lambda *_a: self.selected_item() is not None
                             and self.activate_tile(self.selected_item()))
        # Esc in the field: clear it, then leave the folder, then close.
        filter_entry.connect("stop-search", lambda *_a: self.on_escape())
        self.guard_text_field(filter_entry)
        self.filter_entry = filter_entry
        empty = Gtk.Label(label=_("Nothing to show here"))
        empty.add_css_class("sc-empty")
        empty.set_margin_top(40)
        empty.set_visible(False)
        grid_box.append(empty)
        grid_scroller.set_child(grid_box)
        stack.add_named(grid_scroller, "overview")
        card.insert_child_after(filter_entry, self.header_bar)
        stack.connect("notify::visible-child-name", lambda *_a: self.sync_filter_entry())

        # Under the overview: apps | plugins, then one button per group.
        filter_bar = Gtk.Box(spacing=8)
        filter_bar.add_css_class("launcher-filter-bar")
        mode_switch = Gtk.Box()
        mode_switch.add_css_class("linked")
        mode_switch.add_css_class("launcher-view-switch")
        self.mode_switch_buttons = {}
        for mode, label in (("apps", _("Apps")), ("plugins", _("Plugins"))):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.add_css_class("launcher-filter-mode")
            button.set_tooltip_text(_("Apps and plugins (Tab)"))
            button.connect("clicked", lambda _b, m=mode: m != self.mode and self.toggle_mode())
            mode_switch.append(button)
            self.mode_switch_buttons[mode] = button
        filter_bar.append(mode_switch)
        # A new group: click to name it, or drop a card here to start it.
        new_group = Gtk.Button()
        new_group.add_css_class("flat")
        new_group.add_css_class("launcher-filter-chip")
        new_group.add_css_class("launcher-filter-new")
        new_group.set_valign(Gtk.Align.CENTER)
        new_box = Gtk.Box(spacing=4)
        new_box.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        new_label = Gtk.Label(label=_("New group"))
        new_box.append(new_label)
        self.new_group_label = new_label  # Hidden while narrow: room for the groups.
        new_group.set_child(new_box)
        new_group.set_tooltip_text(_("New group (or drop a card here to start one with it)"))
        new_group.set_focus_on_click(False)
        new_group.connect("clicked", lambda *_a: self.open_group_name_page(None))
        self.add_chip_drop(new_group, None)
        filter_bar.append(new_group)
        self.new_group_button = new_group
        chips_scroller = Gtk.ScrolledWindow()
        # More groups than fit: the row scrolls (wheel, touchpad, the thin
        # scrollbar or the ‹ › buttons, which show only then).
        chips_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        chips_scroller.set_hexpand(True)
        chips = Gtk.Box(spacing=6)
        chips.set_margin_bottom(4)  # Room for the overlay scrollbar.
        chips_layer = Gtk.Overlay()  # The drop marker for reordering groups.
        chips_layer.set_child(chips)
        self.chips_marker = self.drop_marker()
        chips_layer.add_overlay(self.chips_marker)
        chips_scroller.set_child(chips_layer)
        # The mouse wheel scrolls the row of groups sideways.
        wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        wheel.connect("scroll", self.on_filter_wheel)
        chips_scroller.add_controller(wheel)
        self.filter_arrows = []
        for step, icon in ((-1, "go-previous-symbolic"), (1, "go-next-symbolic")):
            arrow = Gtk.Button(icon_name=icon)
            arrow.add_css_class("flat")
            arrow.add_css_class("launcher-header-btn")
            arrow.set_valign(Gtk.Align.CENTER)
            arrow.set_focus_on_click(False)
            arrow.connect("clicked", lambda _b, d=step: self.scroll_filter_bar(d))
            self.filter_arrows.append(arrow)
        filter_bar.append(self.filter_arrows[0])
        filter_bar.append(chips_scroller)
        filter_bar.append(self.filter_arrows[1])
        # Cards or folders, like a start menu's "View: Category".
        layout_switch = Gtk.Box()
        layout_switch.add_css_class("linked")
        layout_switch.add_css_class("launcher-view-switch")
        layout_switch.set_valign(Gtk.Align.CENTER)
        self.layout_buttons = {}
        for layout, icon, tip in (("cards", "view-grid-symbolic", _("Show every entry as a card")),
                                  ("folders", "folder-symbolic", _("Show the groups as folders"))):
            button = Gtk.Button(icon_name=icon)
            button.add_css_class("flat")
            button.add_css_class("launcher-header-btn")
            button.set_tooltip_text(tip)
            button.set_focus_on_click(False)
            button.connect("clicked", lambda _b, l=layout: self.set_wide_layout(l))
            layout_switch.append(button)
            self.layout_buttons[layout] = button
        filter_bar.append(layout_switch)
        adjustment = chips_scroller.get_hadjustment()
        for signal_name in ("changed", "value-changed"):
            # Not during the layout pass that changed it: showing the arrows resizes.
            adjustment.connect(signal_name, lambda *_a: GLib.idle_add(self.update_filter_arrows))
        filter_bar.set_visible(False)
        card.append(filter_bar)
        self.grid, self.grid_scroller, self.grid_empty = grid, grid_scroller, empty
        self.filter_bar, self.filter_chips, self.filter_scroller = filter_bar, chips, chips_scroller

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)
        # Capture phase: while the shortcut page is open every key press is a
        # candidate combo, even when a button inside the page has focus.
        capture_keys = Gtk.EventControllerKey()
        capture_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        capture_keys.connect("key-pressed", self.on_shortcut_key)
        window.add_controller(capture_keys)
        window.connect("notify::is-active", self.on_active_changed)

        self.window, self.overlay, self.card = window, overlay, card
        self.rows, self.scroller = rows, scroller
        self.title, self.hidden_button, self.mode_button = title, hidden_button, mode_button
        self.back_button, self.stack = back_button, stack
        self.build_shortcut_page()
        self.build_settings_page()
        self.build_group_name_page()
        self.build_details_page()
        self.build_icon_page()
        self.apply_theme()
        self.resize_to(*self.saved_card_size())
        self.move_to(self.position)

    # --- size and overview --------------------------------------------------------

    def main_page(self):
        return "overview" if self.overview else "list"

    def main_page_open(self):
        return self.stack.get_visible_child_name() in ("list", "overview")

    def item_view(self):
        """The widget holding the entries shown: the list or the card grid."""
        return self.grid if self.overview else self.rows

    def selected_item(self):
        if self.overview:
            selected = self.grid.get_selected_children()
            return selected[0] if selected else None
        return self.rows.get_selected_row()

    @staticmethod
    def default_card_size():
        return CARD_WIDTH, LIST_HEIGHT

    def saved_card_size(self):
        return saved_size("size") or self.default_card_size()

    @staticmethod
    def dock_space():
        """{"left"|"right"|"bottom": pixels} the card keeps free for an
        auto-hiding dock, which this popup keeps shown while open. A dock
        without auto-hide reserves its space itself (exclusive zone)."""
        try:
            dock = dock_core.settings()
        except Exception:
            return {}
        if not (dock["dock_enabled"] and dock["dock_autohide"]):
            return {}
        # The dock's whole thickness (band, icons sticking out, growth) and a gap.
        return {dock_core.edge(dock, dock_core.bar_position()): dock_core.sizes(dock)[2] + 12}

    def screen_size(self):
        """The room the card has: the overlay fills the monitor (less the
        bar, and less an auto-hiding dock's edge)."""
        width, height = self.full_screen_size()
        space = self.dock_space()
        return (width - space.get("left", 0) - space.get("right", 0),
                height - space.get("bottom", 0) - space.get("top", 0))

    def full_screen_size(self):
        width, height = self.overlay.get_width(), self.overlay.get_height()
        if width > 0 and height > 0:
            return width, height
        display = Gdk.Display.get_default()
        monitors = display.get_monitors() if display is not None else None
        monitor = monitors.get_item(0) if monitors is not None and monitors.get_n_items() else None
        if monitor is not None:
            geometry = monitor.get_geometry()
            return geometry.width, geometry.height - 40
        return 1920, 1040

    def page_height(self):
        return self.list_h

    def resize_to(self, width, height, save=False):
        """Card width and page height, within the screen. The overview shows
        as many card columns as fit; sub-pages keep the same height so
        switching pages never makes the card jump."""
        screen_w, screen_h = self.screen_size()
        self.card_w = max(CARD_WIDTH, min(int(width), screen_w - 48, MAX_CARD_WIDTH))
        columns = grid_columns(self.card_w)
        folders = self.wide_layout == "folders"
        # Header, filter bar (and field), padding and the 24 px margins around the card.
        chrome = (190 if folders else 150) if columns > 1 else 110
        self.list_h = max(MIN_LIST_HEIGHT, min(int(height), screen_h - chrome))
        max_w = max(CARD_WIDTH, min(screen_w - 48, MAX_CARD_WIDTH))
        max_chrome = (190 if folders else 150) if grid_columns(max_w) > 1 else 110
        self.max_card_size = (max_w, max(MIN_LIST_HEIGHT, screen_h - max_chrome))
        self.card.set_size_request(self.card_w, -1)
        for scroller in (self.scroller, self.grid_scroller):
            # Raise the maximum first: GTK refuses a minimum above the maximum.
            scroller.set_max_content_height(100000)
            scroller.set_min_content_height(self.list_h)
            scroller.set_max_content_height(self.list_h)
        for page in self.height_pages:
            if isinstance(page, Gtk.ScrolledWindow):
                page.set_max_content_height(100000)
                page.set_min_content_height(self.list_h)
                page.set_max_content_height(self.list_h)
            else:
                page.set_size_request(-1, self.list_h)
        if self.shortcut_app is not None:
            self.sc_page.set_size_request(-1, self.list_h)
        self.apply_grid_columns()
        self.refit_folders()
        if hasattr(self, "new_group_label"):
            self.new_group_label.set_visible(self.card_w >= 700)
        # The list at the default size; cards or folders once two fit side by side.
        overview = columns > 1
        if overview != self.overview:
            self.set_overview(overview)
        self.sync_previews()  # Animated previews in the wide popup only.
        self.update_mouse_hint()
        if save:
            default = (self.card_w, self.list_h) == tuple(self.default_card_size())
            try:
                set_saved_size("size", None if default else (self.card_w, self.list_h))
            except OSError as error:
                self.notify(_("Could not save the setting"), str(error))

    def on_header_click(self, gesture, count, _x, _y):
        if count != 2:
            return
        button = gesture.get_current_button()
        if button not in (Gdk.BUTTON_PRIMARY, Gdk.BUTTON_SECONDARY):
            return
        # At the smallest size either button maximizes, at the largest either
        # one goes back; in between left = default size, right = maximize.
        self.on_header_double_click(button)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def on_header_double_click(self, button):
        size = self.size_state()
        if size == "max" or (size == "between" and button == Gdk.BUTTON_PRIMARY):
            self.resize_to(*self.default_card_size(), save=True)
        else:
            self.resize_to(MAX_CARD_WIDTH, 100000, save=True)
        self.move_to(None, save=True)  # Both sizes come back centred.

    def size_state(self):
        """"default", "max" or "between"."""
        if (self.card_w, self.list_h) == tuple(self.default_card_size()):
            return "default"
        if (self.card_w, self.list_h) == tuple(getattr(self, "max_card_size", ())):
            return "max"
        return "between"

    def update_mouse_hint(self):
        if not hasattr(self, "mouse_hint"):
            return
        size = self.size_state()
        self.mouse_hint.set_label(_("Double-click: maximize") if size == "default" else
                                  _("Double-click: default size") if size == "max" else
                                  _("Double-click: default size · right double-click: maximize"))
        self.mouse_hint.set_tooltip_text(self.mouse_hint.get_label())

    def on_resize_begin(self, gesture, x, y):
        """A press on the full-screen overlay: the grip resizes, the header
        (not its buttons) moves the card. Anywhere else is not ours."""
        picked = self.overlay.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is not None and picked is not self.grip and self.on_header(picked):
            self.begin_move(gesture)
            return
        if picked is not self.grip:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        # A second press on the grip within the double-click time: default size.
        now = time.monotonic()
        if now - getattr(self, "last_grip_press", 0.0) < 0.4:
            self.last_grip_press = 0.0
            self.resize_start = None
            self.resized_at = now
            self.resize_to(*self.default_card_size(), save=True)
            return
        self.last_grip_press = now
        self.resize_start = (self.card_w, self.list_h)

    def on_resize_update(self, _gesture, dx, dy):
        if self.move_start is not None:
            (x, y) = self.move_start
            self.move_to((x + round(dx), y + round(dy)))
            return
        if self.resize_start is None:
            return
        width, height = self.resize_start
        # Centred, the card grows by twice the pointer's way; moved, it grows
        # right and down from its top-left corner.
        factor = 1 if self.position is not None else 2
        self.resize_to(width + factor * round(dx), height + factor * round(dy))

    def on_resize_end(self, _gesture, _dx, _dy):
        if self.move_start is not None:
            self.move_start = None
            self.resized_at = time.monotonic()
            self.header_bar.set_cursor_from_name("grab")
            self.move_to(self.position, save=True)
            return
        if self.resize_start is None:
            return
        self.resize_start = None
        self.resized_at = time.monotonic()
        self.resize_to(self.card_w, self.list_h, save=True)
        GLib.idle_add(self.keep_on_screen)

    def on_header(self, widget):
        """True for the header bar or a label in it, not for its buttons."""
        while widget is not None and widget is not self.header_bar:
            if isinstance(widget, Gtk.Button):
                return False
            widget = widget.get_parent()
        return widget is self.header_bar

    def begin_move(self, gesture):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        # A second left press within the double-click time: the header's
        # double-click (a click gesture would lose the press to this drag).
        now = time.monotonic()
        if now - getattr(self, "last_header_press", 0.0) < 0.4:
            self.last_header_press = 0.0
            self.move_start = None
            self.resized_at = now
            self.on_header_double_click(Gdk.BUTTON_PRIMARY)
            return
        self.last_header_press = now
        self.move_start = self.card_position()
        self.header_bar.set_cursor_from_name("grabbing")

    def keep_on_screen(self):
        """Once laid out at its new size: a moved card stays fully on screen."""
        if self.position is not None:
            before = self.position
            self.move_to(self.position)
            if self.position != before:
                self.move_to(self.position, save=True)
        return GLib.SOURCE_REMOVE

    def card_position(self):
        """The card's top-left on the overlay, also while centred."""
        if self.position is not None:
            return self.position
        ok, bounds = self.card_frame.compute_bounds(self.overlay)
        if not ok:
            return 0, 0
        return round(bounds.get_x()), round(bounds.get_y())

    def move_to(self, position, save=False):
        """Put the card's top-left at position (kept on screen), or centre it for None."""
        frame = self.card_frame
        space = self.dock_space()
        left = space.get("left", 0)
        if position is not None:
            screen_w, screen_h = self.screen_size()
            frame_w, frame_h = frame.get_width(), frame.get_height()
            top = space.get("top", 0)
            position = (max(left, min(int(position[0]), left + screen_w - frame_w)),
                        max(top, min(int(position[1]), top + screen_h - frame_h)))
        self.position = position
        frame.set_margin_end(space.get("right", 0) if position is None else 0)
        if position is None:
            frame.set_halign(Gtk.Align.CENTER)
            frame.set_valign(Gtk.Align.CENTER)
            frame.set_margin_start(left)  # Centred in the room the dock leaves.
            frame.set_margin_top(24 + space.get("top", 0))
            frame.set_margin_bottom(24 + space.get("bottom", 0))
        else:
            frame.set_halign(Gtk.Align.START)
            frame.set_valign(Gtk.Align.START)
            frame.set_margin_start(position[0])
            frame.set_margin_top(position[1])
            frame.set_margin_bottom(0)
        if save:
            try:
                set_saved_position(position)
            except OSError as error:
                self.notify(_("Could not save the setting"), str(error))

    def apply_grid_columns(self):
        """Folders or cards per row at the current width."""
        if self.showing_folders():
            columns = folder_fit(getattr(self, "folder_count", 0), self.card_w, self.list_h)[0]
        else:
            columns = grid_columns(self.card_w)
        if columns != getattr(self, "grid_columns", None):
            self.grid.set_min_children_per_line(columns)
            self.grid.set_max_children_per_line(columns)
            self.grid_columns = columns

    def refit_folders(self):
        """Resized: the folders grow or shrink to fill the page. Rebuilt once
        the size has settled, not on every step of a drag on the grip."""
        if not getattr(self, "folder_count", 0) or not self.showing_folders():
            return
        columns, icon = folder_fit(self.folder_count, self.card_w, self.list_h)
        if (icon == getattr(self, "folder_icon", FOLDER_ICON) and folder_icons_height(
                self.folder_count, columns, icon, self.list_h) == getattr(self, "folder_icons_h", None)):
            return
        if getattr(self, "refit_source", None):
            GLib.source_remove(self.refit_source)
        self.refit_source = GLib.timeout_add(120, self.on_refit_folders)

    def on_refit_folders(self):
        self.refit_source = None
        if self.window is not None and self.showing_folders() and self.main_page_open():
            self.refresh_list()
        return GLib.SOURCE_REMOVE

    def set_overview(self, enabled):
        """List (one column) or overview (cards): follows the popup's width.
        Sub-pages stay open; they go back to whichever it is then."""
        selected = self.row_key(self.selected_item()) if self.window is not None else None
        self.overview = enabled
        if self.window is None or not hasattr(self, "running_button"):
            return
        if self.main_page_open():
            self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            self.stack.set_visible_child_name(self.main_page())
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
            self.show_main_header()
            self.refresh_list(selected_id=selected if selected and not selected.startswith("group:") else None)

    def overview_running(self, app):
        if self.mode == "plugins":
            return "tray_id" in app
        return self.app_state(app) != "stopped"

    def overview_sections(self, entries):
        """[(filter id, label, entries)] for the filter bar: all, running,
        then the groups."""
        result = [("all", _("All"), entries)]
        running = [a for a in entries if self.overview_running(a)]
        if running:
            result.append((groups.RUNNING, groups.display_name(groups.RUNNING, page=self.mode), running))
        for gid, members in groups.sections(entries, self.groups):
            # Every group stays, empty or not, also while searching: each is
            # a place to drag a card to. Other only when something is in it
            # or a card could go back there.
            if members or gid != groups.OTHER or self.search_query.strip():
                result.append((gid, groups.display_name(gid, self.groups, self.mode), members))
        return result

    def refresh_overview(self, entries, selected_id):
        sections = self.overview_sections(entries)
        wanted = self.overview_filter[self.mode]
        if self.search_query.strip():
            # A search looks through everything unless a chip is clicked
            # during it; the chosen group comes back after it. The chips
            # mark where the selected card belongs.
            wanted = getattr(self, "search_filter", None) or "all"
        else:
            self.search_filter = None
        if wanted not in {gid for gid, _label, _members in sections}:
            wanted = "all"
        self.fill_filter_bar(sections, wanted)
        self.apply_grid_columns()
        if self.showing_folders(wanted):
            self.fill_folders(sections, selected_id)
            return
        members = next(m for gid, _label, m in sections if gid == wanted)
        if wanted == "all" and (self.mode == "plugins" or self.running_first):
            # Stable sort: running apps (tray icons) first, each part in the usual order.
            members = sorted(members, key=lambda a: not self.overview_running(a))
        while child := self.grid.get_first_child():
            self.grid.remove(child)
        selected = None
        for app in members:
            tile = self.make_tile(app)
            self.grid.append(tile)
            if selected is None and selected_id and app.get("desktop_id") == selected_id:
                selected = tile
        self.grid_empty.set_visible(not members)
        selected = selected or self.grid.get_child_at_index(0)
        if selected is not None:
            self.grid.select_child(selected)
            GLib.timeout_add(50, self.ensure_tile_visible, selected)

    def on_filter_entry_changed(self, entry):
        if getattr(self, "filter_syncing", False):
            return
        text = entry.get_text()
        if text != self.search_query:
            self.search_query = text
            self.refresh_list()

    def sync_filter_entry(self):
        entry = getattr(self, "filter_entry", None)
        if entry is None or not hasattr(self, "stack"):
            return
        entry.set_visible(self.overview and self.wide_layout == "folders" and self.main_page_open())
        if entry.get_text() != self.search_query:
            self.filter_syncing = True
            entry.set_text(self.search_query)
            entry.set_position(-1)
            self.filter_syncing = False

    def on_escape(self):
        if self.search_query:
            self.search_query = ""
            self.refresh_list()
        elif self.open_folder():
            self.leave_folder()
        else:
            self.hide()

    def filter_has_focus(self):
        entry = getattr(self, "filter_entry", None)
        return entry is not None and entry.get_visible() and entry.get_delegate().has_focus()

    def showing_folders(self, wanted=None):
        """The folder layout's start page: "All", not typing to filter."""
        if wanted is None:
            wanted = self.overview_filter[self.mode]
        return (self.overview and self.wide_layout == "folders" and wanted == "all"
                and not self.search_query.strip())

    def open_folder(self):
        """In a folder of the folder layout (Esc / Backspace go back)."""
        return (self.overview and self.wide_layout == "folders" and not self.search_query.strip()
                and self.overview_filter[self.mode] != "all")

    def fill_folders(self, sections, selected_id):
        while child := self.grid.get_first_child():
            self.grid.remove(child)
        selected = None
        folders = [(gid, label, members) for gid, label, members in sections
                   if gid != "all" and (members or gid not in (groups.RUNNING, groups.OTHER))]
        # As many as there are fill the page: added or deleted groups refit.
        self.folder_count = len(folders)
        columns, self.folder_icon = folder_fit(self.folder_count, self.card_w, self.list_h)
        self.folder_icons_h = folder_icons_height(self.folder_count, columns, self.folder_icon, self.list_h)
        self.apply_grid_columns()
        for gid, label, members in folders:
            tile = self.make_folder_tile(gid, label, members)
            self.grid.append(tile)
            if selected is None and selected_id == "group:" + gid:
                selected = tile
        self.grid_empty.set_visible(not folders)
        selected = selected or self.grid.get_child_at_index(0)
        if selected is not None:
            self.grid.select_child(selected)
            GLib.timeout_add(50, self.ensure_tile_visible, selected)

    def set_wide_layout(self, layout):
        if layout == self.wide_layout:
            return
        self.wide_layout = layout
        try:
            set_wide_layout(layout)
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))
        self.sync_layout_controls()
        # The folders start on "All", where the folders are.
        self.overview_filter[self.mode] = "all"
        before = self.overview
        self.resize_to(self.card_w, self.list_h)
        if self.overview and before:
            self.refresh_list()

    def corner_labels(self):
        rounding = getattr(self, "window_rounding", None)
        like = _("Like windows ({0} px)").format(rounding) if rounding is not None else _("Like windows")
        return [like, _("Rounded"), _("Square")]

    def corners_tip(self):
        """Where the windows' rounding comes from: the theme, looknfeel.lua or Omarchy."""
        source = corners.rounding_source()
        if source == "user":
            origin = _("Your windows' rounding is set in ~/.config/hypr/looknfeel.lua.")
        elif source == "theme":
            origin = _("Your windows' rounding is set by the current theme (its hyprland.lua).")
        else:
            origin = _("Your windows use Omarchy's default rounding (square); a theme or ~/.config/hypr/looknfeel.lua can change it.")
        return _("Corners of the popup, its folders and cards, and the dock. “Like windows” follows Hyprland's window rounding.") + " " + origin

    def set_corners(self, value):
        try:
            set_corners(value)  # The dock follows through settings.json.
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))
        self.apply_theme()

    def sync_corners_control(self):
        """The window rounding may have changed (a theme or looknfeel.lua):
        show its new value and source."""
        dropdown = getattr(self, "corners_dropdown", None)
        if dropdown is None:
            return
        self.corners_syncing = True
        dropdown.set_model(Gtk.StringList.new(self.corner_labels()))
        dropdown.set_selected(self.corner_choices.index(saved_corners()))
        self.corners_syncing = False
        tip = self.corners_tip()
        dropdown.set_tooltip_text(tip)
        self.corners_label.set_tooltip_text(tip)

    def sync_layout_controls(self):
        for layout, button in getattr(self, "layout_buttons", {}).items():
            if layout == self.wide_layout:
                button.add_css_class("launcher-toggle-active")
            else:
                button.remove_css_class("launcher-toggle-active")
        dropdown = getattr(self, "layout_dropdown", None)
        if dropdown is not None:
            self.layout_syncing = True
            dropdown.set_selected(0 if self.wide_layout == "folders" else 1)
            self.layout_syncing = False

    def leave_folder(self):
        gid = self.overview_filter[self.mode]
        self.overview_filter[self.mode] = "all"
        self.refresh_list(selected_id="group:" + gid)

    def activate_tile(self, tile):
        if hasattr(tile, "group"):
            self.set_overview_filter(tile.group)
        else:
            self.launch(tile.app, tile.actions_button)

    def make_folder_tile(self, gid, label, members):
        """A group in the folder layout: a box with its icons (each opens its
        app; nine show, more scroll) and the name under it. A click elsewhere
        on it opens the group; right click: rename, move, delete."""
        tile = Gtk.FlowBoxChild()
        tile.group = gid
        tile.add_css_class("launcher-folder")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        # Every folder is the same size: three rows of three icons. More
        # than nine scroll inside the folder.
        icons = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True, row_homogeneous=True)
        icons.set_halign(Gtk.Align.FILL)
        overflow = len(members) > FOLDER_COLUMNS * FOLDER_ROWS
        # Rows spread over the folder's height, unless they scroll.
        icons.set_valign(Gtk.Align.START if overflow else Gtk.Align.FILL)
        icon_marker = self.drop_marker()
        icon_size = getattr(self, "folder_icon", FOLDER_ICON)
        cell_size = icon_size + 14
        for index in range(max(FOLDER_COLUMNS * FOLDER_ROWS, len(members))):
            if index < len(members):
                app = members[index]
                button = Gtk.Button()
                button.add_css_class("flat")
                button.add_css_class("launcher-folder-app")
                note = self.update_note(app)
                hidden = app["desktop_id"] in self.hidden_ids_for(app)
                off_bar = self.off_bar(app)
                slot = Gtk.Overlay()
                icon = self.app_icon(app, icon_size)
                slot.set_child(icon)
                if note:
                    # A small mark on the icon, like the run dot on the cards.
                    mark = Gtk.Box()
                    mark.add_css_class("launcher-update-dot")
                    mark.set_halign(Gtk.Align.END)
                    mark.set_valign(Gtk.Align.START)
                    mark.set_can_target(False)
                    slot.add_overlay(mark)
                is_new = self.is_new(app)
                if is_new:
                    star = Gtk.Label(label="★")
                    star.add_css_class("launcher-new-star")
                    star.set_halign(Gtk.Align.START)
                    star.set_valign(Gtk.Align.START)
                    star.set_can_target(False)
                    slot.add_overlay(star)
                if hidden or off_bar:
                    # Shown only with "show hidden": faded, grey, dashed, with
                    # the crossed-out eye (or the minus of "not in bar").
                    icon.add_css_class("launcher-folder-hidden-icon")
                    button.add_css_class("launcher-folder-hidden")
                    mark = Gtk.Image.new_from_icon_name("view-conceal-symbolic" if hidden else "list-remove-symbolic")
                    mark.set_pixel_size(12)
                    mark.add_css_class("launcher-folder-hidden-mark")
                    mark.set_halign(Gtk.Align.START)
                    mark.set_valign(Gtk.Align.END)
                    mark.set_can_target(False)
                    slot.add_overlay(mark)
                button.set_child(slot)
                button.set_size_request(cell_size, cell_size)
                button.set_valign(Gtk.Align.CENTER)
                tip = app["name"]
                if hidden or off_bar:
                    tip += " (" + (_("Hidden") if hidden else _("Not in bar")) + ")"
                if is_new:
                    tip += " · ★ " + _("Newly installed")
                button.set_tooltip_text(f"{tip}\n{note}" if note else tip)
                # Right click: the entry's own actions, as on its card.
                menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
                menu_click.connect("pressed", lambda gesture, *_a, b=button, a=app: (
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED), self.show_actions(b, a)))
                button.add_controller(menu_click)
                button.set_focus_on_click(False)
                button.set_can_focus(False)
                button.connect("clicked", lambda b, a=app: self.launch(a, b))
                button.folder_app = app  # Dragged by the folder's drag source.
                if gid != groups.RUNNING:
                    button.drop_marker = (icon_marker, icons, 6)
                    self.add_icon_drop(button, gid, app)
                cell = button
            else:
                cell = Gtk.Box()  # Keeps three columns in small groups.
                cell.set_size_request(cell_size, cell_size)
            icons.attach(cell, index % FOLDER_COLUMNS, index // FOLDER_COLUMNS, 1, 1)
        scroller = Gtk.ScrolledWindow()
        scroller.add_css_class("launcher-folder-scroller")
        height = max(FOLDER_ROWS * cell_size + (FOLDER_ROWS - 1) * 6, getattr(self, "folder_icons_h", 0))
        scroller.set_min_content_height(height)
        scroller.set_max_content_height(height)
        # Only a folder with more than nine scrolls; the others let the
        # wheel scroll the page.
        scroller.set_policy(Gtk.PolicyType.NEVER,
                            Gtk.PolicyType.AUTOMATIC if overflow else Gtk.PolicyType.NEVER)
        icons_layer = Gtk.Overlay()  # The drop marker for reordering icons.
        icons_layer.set_child(icons)
        icons_layer.add_overlay(icon_marker)
        if not overflow:
            # A scroller that never scrolls takes its child's height: ask there.
            icons_layer.set_size_request(-1, height)
        scroller.set_child(icons_layer)
        frame = Gtk.Box()
        frame.add_css_class("launcher-folder-icons")
        frame.append(scroller)
        scroller.set_hexpand(True)
        box.append(frame)
        name = Gtk.Label(label=label, xalign=0.5)
        name.add_css_class("launcher-folder-name")
        name.set_ellipsize(3)
        name.set_max_width_chars(1)
        name.set_hexpand(True)
        if self.new_group_star(gid) is not None:
            # In the label itself, so a long name still ellipsizes, centred.
            yellow = self.theme.colors.get("yellow", "#e5c07b")
            name.set_markup(f'<span foreground="{GLib.markup_escape_text(yellow)}">★</span> '
                            + GLib.markup_escape_text(label))
            name.set_tooltip_text(_("New group: drag apps onto it to fill it."))
        box.append(name)
        tile.set_child(box)
        tile.actions_button = frame
        count = ngettext("{0} entry", "{0} entries", len(members)).format(len(members))
        tile.set_tooltip_text(f"{label} · {count}\n" + _("Click to open the group, or an icon to start it. Esc goes back."))
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a, t=tile: t not in self.grid.get_selected_children()
                       and self.select_tile(t))
        tile.add_controller(motion)
        if gid != groups.RUNNING:
            tile.drop_marker = (self.grid_marker, self.grid, TILE_SPACING)
            self.add_group_drop(tile, gid, "launcher-folder-drop")
        # One drag source for the folder: an icon drags its app (onto another
        # folder or group chip: moves it there), the rest of the folder the
        # group (onto another group: takes its place).
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag.connect("prepare", lambda _s, x, y, t=tile: self.prepare_folder_drag(t, x, y))
        drag.connect("drag-begin", lambda source, _d: self.begin_folder_drag(source))
        drag.connect("drag-end", lambda *_a: (self.on_tile_drag_end(), self.on_group_drag_end()))
        tile.add_controller(drag)
        if gid not in (groups.RUNNING, groups.OTHER):
            menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            menu_click.connect("pressed", lambda *_a, t=tile: self.show_group_actions(t.actions_button, gid))
            tile.add_controller(menu_click)
        return tile

    @staticmethod
    def app_icon(app, size):
        icon_name = app.get("icon") or "application-x-executable"
        image_extensions = {".png", ".svg", ".xpm", ".jpg", ".jpeg", ".webp", ".bmp"}
        themed_name = Path(icon_name).stem if Path(icon_name).suffix.lower() in image_extensions else Path(icon_name).name
        texture = icon_texture(icon_name) if Path(icon_name).is_file() else None
        icon = Gtk.Image.new_from_paintable(texture) if texture is not None else Gtk.Image.new_from_icon_name(themed_name)
        icon.set_pixel_size(size)
        return icon

    def ensure_tile_visible(self, tile):
        if tile.get_parent() == self.grid and tile in self.grid.get_selected_children():
            if not self.filter_has_focus():  # Keep typing in the filter field.
                tile.grab_focus()
            ok, bounds = tile.compute_bounds(self.grid)
            if ok:
                self.grid_scroller.get_vadjustment().clamp_page(
                    bounds.get_y(), bounds.get_y() + bounds.get_height())
        return GLib.SOURCE_REMOVE

    def fill_filter_bar(self, sections, wanted):
        for mode, button in self.mode_switch_buttons.items():
            if mode == self.mode:
                button.add_css_class("launcher-toggle-active")
            else:
                button.remove_css_class("launcher-toggle-active")
        while child := self.filter_chips.get_first_child():
            self.filter_chips.remove(child)
        self.filter_ids = []
        self.filter_buttons = {}
        for gid, label, members in sections:
            button = Gtk.Button()
            button.add_css_class("flat")
            button.add_css_class("launcher-filter-chip")
            box = Gtk.Box(spacing=5)
            if gid == groups.RUNNING:
                dot = Gtk.Box()
                dot.add_css_class("launcher-filter-dot")
                dot.set_valign(Gtk.Align.CENTER)
                box.append(dot)
            star = self.new_group_star(gid)
            if star is not None:
                box.append(star)
            box.append(Gtk.Label(label=label))
            count = Gtk.Label(label=str(len(members)))
            count.add_css_class("launcher-filter-count")
            box.append(count)
            button.set_child(box)
            button.set_tooltip_text(_("Show everything (Ctrl+G: next group)") if gid == "all" else
                                    _("Show only this group (Ctrl+G: next group)") if gid == groups.RUNNING else
                                    _("Show only this group (Ctrl+G: next group). Right click: rename, move or delete."))
            if gid == wanted:
                button.add_css_class("launcher-filter-on")
                self.filter_on_button = button
            button.set_focus_on_click(False)
            button.connect("clicked", lambda _b, g=gid: self.set_overview_filter(g))
            if gid not in ("all", groups.RUNNING):
                self.add_chip_drop(button, gid)
                menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
                menu_click.connect("pressed", lambda *_a, b=button, g=gid: self.show_group_actions(b, g))
                button.add_controller(menu_click)
            self.filter_chips.append(button)
            self.filter_ids.append(gid)
            self.filter_buttons[gid] = button
        self.new_group_button.set_sensitive(len(self.groups["groups"]) < groups.MAX_GROUPS)
        self.sync_layout_controls()
        GLib.idle_add(self.reveal_filter_chip)

    def mark_selected_group(self):
        """Outline the chip of the group the selected card is in."""
        tile = self.selected_item() if self.overview else None
        app = getattr(tile, "app", None)
        gid = groups.group_of(app, self.groups) if app is not None and "tray_id" not in app else None
        if hasattr(tile, "group"):
            gid = tile.group
        for chip_gid, button in getattr(self, "filter_buttons", {}).items():
            if chip_gid == gid and app is None:
                button.add_css_class("launcher-filter-current")
            elif chip_gid == gid:
                button.add_css_class("launcher-filter-current")
                button.set_tooltip_text(_("{0} is in this group. Drag it onto another group to move it.").format(app["name"]))
            elif button.has_css_class("launcher-filter-current"):
                button.remove_css_class("launcher-filter-current")
                button.set_tooltip_text(_("Show only this group (Ctrl+G: next group). Right click: rename, move or delete."))
        if gid in getattr(self, "filter_buttons", {}) and self.search_query.strip():
            self.filter_on_button = self.filter_buttons[gid]
            GLib.idle_add(self.reveal_filter_chip)

    def reveal_filter_chip(self):
        button = getattr(self, "filter_on_button", None)
        if button is None or button.get_parent() != self.filter_chips:
            return GLib.SOURCE_REMOVE
        ok, bounds = button.compute_bounds(self.filter_chips)
        if ok:
            self.filter_scroller.get_hadjustment().clamp_page(
                bounds.get_x() - 8, bounds.get_x() + bounds.get_width() + 8)
        return GLib.SOURCE_REMOVE

    def scroll_filter_bar(self, direction):
        adjustment = self.filter_scroller.get_hadjustment()
        adjustment.set_value(adjustment.get_value() + direction * adjustment.get_page_size() * 0.7)

    def update_filter_arrows(self):
        adjustment = self.filter_scroller.get_hadjustment()
        overflow = adjustment.get_upper() - adjustment.get_page_size() > 1
        value = adjustment.get_value()
        self.filter_arrows[0].set_visible(overflow)
        self.filter_arrows[1].set_visible(overflow)
        self.filter_arrows[0].set_sensitive(value > 0.5)
        self.filter_arrows[1].set_sensitive(value < adjustment.get_upper() - adjustment.get_page_size() - 0.5)
        return GLib.SOURCE_REMOVE

    def on_filter_wheel(self, _controller, dx, dy):
        adjustment = self.filter_scroller.get_hadjustment()
        adjustment.set_value(adjustment.get_value() + (dy or dx) * 40)
        return True

    def set_overview_filter(self, gid):
        if self.search_query.strip():
            self.search_filter = gid  # For this search only.
            self.refresh_list(selected_id=self.row_key(self.selected_item()))
            return
        if self.overview_filter[self.mode] == gid:
            return
        self.overview_filter[self.mode] = gid
        selected = self.row_key(self.selected_item())
        self.refresh_list(selected_id=selected)

    def next_overview_filter(self, step=1):
        ids = getattr(self, "filter_ids", None) or ["all"]
        current = ((getattr(self, "search_filter", None) or "all") if self.search_query.strip()
                   else self.overview_filter[self.mode])
        position = ids.index(current) if current in ids else 0
        self.set_overview_filter(ids[(position + step) % len(ids)])

    def make_tile(self, app):
        """One card of the overview: icon with the run dot, ☰ for the
        actions (also right click), name, description, shortcut and badges."""
        is_hidden = app["desktop_id"] in self.hidden_ids_for(app)
        off_bar = bool(app.get("bar_widget")) and not app.get("in_bar")
        tile = Gtk.FlowBoxChild()
        tile.app = app
        tile.add_css_class("launcher-tile")
        if is_hidden or off_bar:
            tile.add_css_class("launcher-row-hidden")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=6)
        icon = self.app_icon(app, 44)
        if is_hidden or off_bar:
            icon.add_css_class("launcher-hidden-icon")
        icon_slot = Gtk.Overlay()
        icon_slot.set_child(icon)
        icon_slot.set_halign(Gtk.Align.START)
        state = self.app_state(app)
        if state != "stopped":
            dot = Gtk.Box()
            dot.add_css_class("launcher-run-dot")
            dot.add_css_class(state)
            dot.set_halign(Gtk.Align.END)
            dot.set_valign(Gtk.Align.END)
            dot.set_can_target(False)
            icon_slot.add_overlay(dot)
            icon_slot.set_tooltip_text(_("Open windows") if state == "active" else _("Running in background"))
        top.append(icon_slot)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)
        actions = Gtk.Button(icon_name="open-menu-symbolic")
        actions.add_css_class("flat")
        actions.add_css_class("launcher-tile-menu")
        actions.set_valign(Gtk.Align.START)
        actions.set_tooltip_text(_("Tray menu") if "tray_id" in app else
                                 _("Plugin actions") if "plugin_id" in app else _("App actions"))
        actions.connect("clicked", lambda button, target=app: self.show_actions(button, target))
        top.append(actions)
        box.append(top)

        name = Gtk.Label(label=app["name"], xalign=0)
        name.add_css_class("launcher-tile-name")
        name.set_ellipsize(3)
        box.append(name)
        desc_text = app.get("description", "").strip()
        desc = Gtk.Label(label=desc_text, xalign=0, yalign=0, wrap=True, lines=2)
        desc.set_ellipsize(3)
        desc.set_max_width_chars(1)  # Wrap at the card's width, not the text's.
        desc.set_hexpand(True)
        desc.add_css_class("launcher-app-desc")
        desc.set_vexpand(True)
        box.append(desc)

        foot = Gtk.Box(spacing=4)
        foot.set_size_request(-1, 18)
        if is_hidden:
            foot.append(self.tile_badge("view-conceal-symbolic", _("Hidden")))
        if off_bar:
            foot.append(self.tile_badge("list-remove-symbolic", _("Not in bar")))
        if self.is_new(app):
            foot.append(self.new_star())
        note = self.update_note(app)
        if note:
            foot.append(self.update_badge(note))
        assigned = self.app_shortcuts(app, self.list_bindings)
        if assigned:
            chip = Gtk.Label(label=shortcuts.combo_label(assigned[0]).replace(" + ", "+"))
            chip.add_css_class("launcher-shortcut-chip")
            chip.set_valign(Gtk.Align.CENTER)
            chip.set_ellipsize(3)
            foot.append(chip)
            if len(assigned) > 1:
                more = Gtk.Label(label=f"+{len(assigned) - 1}")
                more.add_css_class("launcher-shortcut-more")
                more.set_valign(Gtk.Align.CENTER)
                foot.append(more)
            foot.set_tooltip_text((_("Global shortcuts:") + "\n" if len(assigned) > 1 else _("Global shortcut:") + " ") + "\n".join(
                shortcuts.combo_label(c) + (f" → {shortcuts.action_name(app, c['action'])}" if c["action"] else "")
                for c in assigned))
        box.append(foot)
        tile.set_child(box)
        tile.actions_button = actions
        prefix = "[" + _("Hidden") + "] " if is_hidden else ""
        tile.set_tooltip_text(f"{prefix}{app['name']} — {desc_text}" if desc_text else f"{prefix}{app['name']}")

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a, t=tile: t not in self.grid.get_selected_children()
                       and self.select_tile(t))
        tile.add_controller(motion)
        menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu_click.connect("pressed", lambda *_a, t=tile: self.show_actions(t.actions_button, t.app))
        tile.add_controller(menu_click)
        if "tray_id" not in app:
            # Drag the card onto a group in the filter bar to move it there.
            drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            drag.connect("prepare", lambda *_a, a=app: Gdk.ContentProvider.new_for_value(
                DRAG_PREFIX + a["desktop_id"]))
            drag.connect("drag-begin", lambda source, _d, i=icon, a=app: self.on_tile_drag_begin(source, i, a))
            drag.connect("drag-end", lambda *_a: self.on_tile_drag_end())
            tile.add_controller(drag)
        return tile

    def on_tile_drag_begin(self, source, icon, app):
        self.tile_dragging = True
        self.dragged_app = app
        source.set_icon(Gtk.WidgetPaintable.new(icon), 22, 22)
        self.filter_bar.add_css_class("launcher-filter-dragging")

    def on_tile_drag_end(self):
        self.tile_dragging = False
        self.dragged_app = None
        self.filter_bar.remove_css_class("launcher-filter-dragging")
        self.clear_all_drop_marks()
        self.refresh_held_back()

    def clear_all_drop_marks(self):
        for widget in getattr(self, "drop_marked", []):
            self.clear_drop_marks(widget, "launcher-filter-drop", "launcher-folder-drop")
        self.drop_marked = []

    def refresh_held_back(self):
        held = getattr(self, "refresh_after_drag", None)
        if held is not None and not getattr(self, "tile_dragging", False) \
                and getattr(self, "dragged_group", None) is None:
            self.refresh_after_drag = None
            self.refresh_list(selected_id=held[0])

    def add_chip_drop(self, button, gid):
        """Cards dropped on the chip move to group gid (None: a new group).
        Only drags that started on one of our own cards are accepted, and the
        offered data is never read: the card being dragged is known already.
        (A plain DropTarget would buffer whatever another app offers, unbounded.)"""
        self.add_group_drop(button, gid, "launcher-filter-drop")
        button.add_css_class("launcher-filter-group")
        if gid is not None:  # "New group" takes cards only, never a group: no bar.
            button.drop_marker = (self.chips_marker, self.filter_chips, 6)
        if gid in {g["id"] for g in self.groups["groups"]}:
            # Drag a group's chip onto another group to change the order.
            self.add_group_drag(button, gid)

    def add_group_drop(self, widget, gid, css_class):
        """Our own cards (or folder icons) dropped on widget move to group
        gid; a dragged group takes gid's place in the order."""
        target = Gtk.DropTargetAsync.new(None, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("accept", lambda _t, drop: self.accepts_drop(drop, gid))
        # Hyprland preselects MOVE: accept both, or the drop is refused.
        target.connect("drag-enter", lambda _t, _d, x, _y, w=widget: self.on_group_drag_over(w, gid, css_class, x))
        target.connect("drag-motion", lambda _t, _d, x, _y, w=widget: self.on_group_drag_over(w, gid, css_class, x))
        target.connect("drag-leave", lambda *_a, w=widget: self.clear_drop_marks(w, css_class))
        target.connect("drop", self.on_chip_drop, gid)
        widget.add_controller(target)

    def on_group_drag_over(self, widget, gid, css_class, x):
        """A card over a group: highlight it. A group over another: a bar on
        the side it will land on (left half: before it, right half: after)."""
        if not self.accepts_drop(None, gid):
            return 0
        if getattr(self, "dragged_group", None) is not None:
            self.mark_drop_side(widget, x, "launcher-drop")
        else:
            widget.add_css_class(css_class)
            if widget not in getattr(self, "drop_marked", []):
                self.drop_marked = getattr(self, "drop_marked", []) + [widget]
        return Gdk.DragAction.COPY

    @staticmethod
    def drop_marker():
        """An accent bar shown in the gap where a dragged item will land."""
        marker = Gtk.Box()
        marker.add_css_class("launcher-drop-marker")
        marker.set_can_target(False)
        marker.set_halign(Gtk.Align.START)
        marker.set_valign(Gtk.Align.START)
        marker.set_visible(False)
        return marker

    def mark_drop_side(self, widget, x, _prefix=None):
        """Show the bar before or after widget (left or right half of it);
        remember the side for the drop."""
        self.drop_after = x > widget.get_width() / 2
        if widget not in getattr(self, "drop_marked", []):
            self.drop_marked = getattr(self, "drop_marked", []) + [widget]
        marker, container, gap = widget.drop_marker
        ok, bounds = widget.compute_bounds(container)
        if not ok:
            return
        edge = bounds.get_x() + (bounds.get_width() if self.drop_after else 0)
        edge += gap / 2 if self.drop_after else -gap / 2
        marker.set_margin_start(max(0, round(edge - DROP_MARKER / 2)))
        marker.set_margin_top(max(0, round(bounds.get_y())))
        marker.set_size_request(DROP_MARKER, max(1, round(bounds.get_height())))
        marker.set_visible(True)

    @staticmethod
    def clear_drop_marks(widget, *classes):
        for name in classes:
            widget.remove_css_class(name)
        marker = getattr(widget, "drop_marker", None)
        if marker is not None:
            marker[0].set_visible(False)

    def accepts_drop(self, drop, gid):
        group = getattr(self, "dragged_group", None)
        if group is not None:
            # Groups only trade places with other groups (Other stays last).
            return gid not in (None, group) and gid in {g["id"] for g in self.groups["groups"]}
        return self.own_card_drag(drop)

    def add_group_drag(self, widget, gid):
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag.connect("prepare", lambda *_a: Gdk.ContentProvider.new_for_value(DRAG_GROUP_PREFIX + gid))
        drag.connect("drag-begin", lambda source, _d, w=widget: self.on_group_drag_begin(source, w, gid))
        drag.connect("drag-end", lambda *_a: self.on_group_drag_end())
        widget.add_controller(drag)

    def add_icon_drop(self, button, gid, app):
        """An icon dragged onto another icon lands right before or after it
        (a bar shows where), in this group."""
        target = Gtk.DropTargetAsync.new(None, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        accepts = lambda: (getattr(self, "dragged_group", None) is None and self.own_card_drag(None)
                           and self.dragged_app.get("desktop_id") != app["desktop_id"])
        target.connect("accept", lambda *_a: accepts())

        def over(_t, _d, x, _y):
            if not accepts():
                return 0
            self.mark_drop_side(button, x, "launcher-icon-drop")
            return Gdk.DragAction.COPY
        target.connect("drag-enter", over)
        target.connect("drag-motion", over)
        target.connect("drag-leave", lambda *_a: self.clear_drop_marks(button))

        def dropped(_t, drop, _x, _y):
            self.clear_drop_marks(button)
            dragged = getattr(self, "dragged_app", None)
            if not accepts() or dragged is None or "tray_id" in dragged:
                drop.finish(0)
                return False
            drop.finish(Gdk.DragAction.COPY)
            self.put_in_group(dragged, gid, app["desktop_id"], getattr(self, "drop_after", False))
            return True
        target.connect("drop", dropped)
        button.add_controller(target)

    def prepare_folder_drag(self, tile, x, y):
        widget = tile.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not tile and not hasattr(widget, "folder_app"):
            widget = widget.get_parent()
        app = getattr(widget, "folder_app", None)
        if app is not None:
            if "tray_id" in app:
                return None
            self.folder_drag = ("app", app, widget.get_child())
            return Gdk.ContentProvider.new_for_value(DRAG_PREFIX + app["desktop_id"])
        if tile.group in {g["id"] for g in self.groups["groups"]}:
            self.folder_drag = ("group", tile.group, tile)
            return Gdk.ContentProvider.new_for_value(DRAG_GROUP_PREFIX + tile.group)
        return None

    def begin_folder_drag(self, source):
        kind, target, widget = getattr(self, "folder_drag", (None, None, None))
        self.folder_drag = None
        if kind == "app":
            self.on_tile_drag_begin(source, widget, target)
        elif kind == "group":
            self.on_group_drag_begin(source, widget, target)

    def on_group_drag_begin(self, source, widget, gid):
        self.dragged_group = gid
        source.set_icon(Gtk.WidgetPaintable.new(widget), 20, 12)
        self.filter_bar.add_css_class("launcher-filter-dragging")

    def on_group_drag_end(self):
        self.dragged_group = None
        self.filter_bar.remove_css_class("launcher-filter-dragging")
        self.clear_all_drop_marks()
        self.refresh_held_back()

    def own_card_drag(self, _drop):
        """Only while one of our cards is being dragged (there is one pointer:
        no other app's drag can be under it at the same time)."""
        return getattr(self, "tile_dragging", False) and getattr(self, "dragged_app", None) is not None

    def on_chip_drop(self, _target, drop, _x, _y, gid):
        group = getattr(self, "dragged_group", None)
        if group is not None:
            if not self.accepts_drop(drop, gid):
                drop.finish(0)
                return False
            drop.finish(Gdk.DragAction.COPY)
            if groups.reorder_group(self.groups, group, gid, after=getattr(self, "drop_after", False)):
                self.save_groups()
                self.refresh_list(selected_id="group:" + group)
            return True
        app = getattr(self, "dragged_app", None)
        if not self.own_card_drag(drop) or app is None or "tray_id" in app:
            drop.finish(0)
            return False
        drop.finish(Gdk.DragAction.COPY)
        if gid is None:
            # After the drop has finished: the name page takes the keyboard.
            GLib.idle_add(lambda: self.open_group_name_page(None, app) and False)
        else:
            if self.search_query.strip():
                # Dropped while typing to filter: once the filter is cleared,
                # show the group it went to, with the card selected.
                self.overview_filter[self.mode] = gid
            self.put_in_group(app, gid)
        return True

    @staticmethod
    def tile_badge(icon_name, text):
        badge = Gtk.Box(spacing=3)
        badge.add_css_class("launcher-hidden-badge")
        badge.set_valign(Gtk.Align.CENTER)
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(11)
        badge.append(image)
        badge.append(Gtk.Label(label=text))
        return badge

    def select_tile(self, tile):
        if tile is not None:
            self.grid.select_child(tile)
            if not self.filter_has_focus():
                tile.grab_focus()

    def overview_key(self, key, state):
        """Arrows, Home/End and Page keys in the card grid. True when handled."""
        tiles = []
        child = self.grid.get_first_child()
        while child is not None:
            tiles.append(child)
            child = child.get_next_sibling()
        if not tiles:
            return False
        current = self.selected_item()
        position = tiles.index(current) if current in tiles else 0
        columns = max(1, getattr(self, "grid_columns", 1))
        page = columns * max(1, self.list_h // 170)
        moves = {Gdk.KEY_Left: -1, Gdk.KEY_KP_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_KP_Right: 1,
                 Gdk.KEY_Up: -columns, Gdk.KEY_KP_Up: -columns, Gdk.KEY_Down: columns, Gdk.KEY_KP_Down: columns,
                 Gdk.KEY_Page_Up: -page, Gdk.KEY_KP_Page_Up: -page,
                 Gdk.KEY_Page_Down: page, Gdk.KEY_KP_Page_Down: page}
        if key in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            target = 0
        elif key in (Gdk.KEY_End, Gdk.KEY_KP_End):
            target = len(tiles) - 1
        elif key in moves:
            target = position + moves[key]
            if abs(moves[key]) > 1:
                target = max(0, min(target, len(tiles) - 1))
            elif not 0 <= target < len(tiles):
                return True
        else:
            return False
        self.select_tile(tiles[target])
        self.ensure_tile_visible(tiles[target])
        return True

    def apply_theme(self):
        colors = self.theme.colors
        display = Gdk.Display.get_default()
        if getattr(self, "style_provider", None) is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self.style_provider)

        def lum(hex_code):
            try:
                h = hex_code.lstrip("#")
                r, g, b = [int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4)]
                return 0.2126 * r + 0.7152 * g + 0.0722 * b
            except Exception:
                return 0.5

        # Corners: Hyprland's window rounding like the Omarchy shell (the
        # default), or rounded / square whatever the windows do.
        self.window_rounding = hyprland_rounding()
        radius = corners.radius(saved_corners(), self.window_rounding)
        # For the settings previews (drawn every frame: no file reads there).
        self.corner_radius = radius
        self.dock_corner_radius = radius or corners.ROUNDED
        sel_hex = colors.get("selection", "#45475a")
        bg_hex = colors.get("background", "#1e1e2e")
        fg_hex = colors.get("foreground", "#cdd6f4")
        muted_hex = colors.get("muted", "#888894")
        # The settings labels' grey in the QML plugins: Qt.darker(foreground, 1.45),
        # i.e. the HSV value divided by 1.45.
        try:
            import colorsys
            r, g, b = (int(fg_hex.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
            h, sat, val = colorsys.rgb_to_hsv(r, g, b)
            dim_hex = "#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, sat, val / 1.45))
        except (ValueError, IndexError):
            dim_hex = muted_hex
        free_hex, taken_hex, omarchy_hex, error_hex = key_owner_colors(colors)

        sel_lum = lum(sel_hex)
        fg_lum = lum(fg_hex)

        if abs(sel_lum - fg_lum) < 0.25:
            if sel_lum > 0.45:
                sel_fg = bg_hex
                sel_desc_fg = "#444444"
            else:
                sel_fg = "#ffffff"
                sel_desc_fg = "#cccccc"
        else:
            sel_fg = fg_hex
            sel_desc_fg = muted_hex

        css = f"""
        window#launcher-window, .launcher-overlay {{ background: transparent; }}
        .launcher-card {{ background: {colors['background']}; color: {colors['foreground']};
            border: 1px solid {colors['accent']}; border-radius: {radius}px; padding: 17px; }}
        .launcher-title {{ font-size: 15px; font-weight: 600; }}
        .launcher-list {{ background: transparent; }}
        .launcher-list row {{ border-radius: {radius}px; padding: 6px 4px 6px 8px; min-height: 44px; }}
        /* Header eye button and row menu buttons share one column, 8px in from
           the card edge like the app icons on the left (4px margin/padding + 4px button padding). */
        .launcher-card button.launcher-header-btn {{ padding: 4px; min-width: 0; min-height: 0; margin-right: 4px; }}
        .launcher-list row:selected {{ background: {sel_hex}; }}
        .launcher-card label, .launcher-card button {{ color: {colors['foreground']}; }}
        .launcher-app-name {{ font-size: 13px; font-weight: 600; }}
        .launcher-app-desc {{ font-size: 11px; color: {muted_hex}; opacity: 0.85; }}
        .launcher-list row:selected .launcher-app-name {{ color: {sel_fg}; font-weight: 600; }}
        .launcher-list row:selected .launcher-app-desc {{ color: {sel_desc_fg}; opacity: 0.95; }}
        .launcher-list row:selected > box > button {{ color: {sel_fg}; }}
        .launcher-list row > box > button.flat {{
            background: transparent;
            border: none;
            box-shadow: none;
            border-radius: {radius}px;
            padding: 4px;
            min-width: 0;
            min-height: 0;
        }}
        .launcher-list row > box > button.flat:hover {{
            background: rgba(255, 255, 255, 0.1);
        }}
        .launcher-list row:selected > box > button.flat:hover {{
            background: rgba(0, 0, 0, 0.12);
        }}
        .launcher-muted {{ color: {colors['muted']}; }}
        .launcher-running {{ color: {colors['red']}; }}
        /* Run badge on the app icon: bright fill, ring in the row colour to
           cut it out from icon and list, hairline so it shows on dark icons. */
        .launcher-run-dot {{
            min-width: 8px; min-height: 8px;
            border-radius: 999px;
            border: 2px solid {colors['background']};
            box-shadow: 0 0 0 1px alpha({colors['foreground']}, 0.35);
            margin: 0 -3px -3px 0;
        }}
        .launcher-run-dot.active {{ background: {colors['green']}; }}
        .launcher-run-dot.background {{ background: {colors['yellow']}; }}
        .launcher-list row:selected .launcher-run-dot {{ border-color: {sel_hex}; }}

        /* Popover / submenu styling */
        popover.launcher-popover contents, popover contents {{
            background-color: #282526;
            color: #ffffff;
            border: 1px solid {colors['accent']};
            border-radius: {radius}px;
            padding: 5px;
            min-width: 170px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.7);
        }}
        popover.launcher-popover arrow, popover arrow {{
            background-color: #282526;
            border-color: {colors['accent']};
        }}
        .launcher-back-btn {{ padding: 2px 6px; }}
        .launcher-back-bordered {{ border: 1px solid alpha({colors['foreground']}, 0.35); border-radius: {radius}px; padding: 4px 10px; }}
        .launcher-card button.launcher-gear-btn {{ border: 1px solid alpha({colors['foreground']}, 0.35); border-radius: {radius}px;
            padding: 1px 6px; margin-right: 0; }}
        .launcher-gear {{ font-family: "JetBrainsMono Nerd Font", "Symbols Nerd Font", monospace; font-size: 13px; }}
        .launcher-section-title {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
        .launcher-settings-card {{ background: alpha({colors['foreground']}, 0.04);
            border: 1px solid alpha({colors['foreground']}, 0.12); border-radius: {radius}px; padding: 12px 14px; }}
        separator.launcher-settings-rule {{ background-color: alpha({colors['foreground']}, 0.12); background-image: none; min-height: 1px; margin: 0 0 2px 0; }}
        /* Option names: easy to read (they were 11 px in the dim grey). */
        .launcher-card label.launcher-setting-label {{ font-size: 13px; color: alpha({colors['foreground']}, 0.88); }}
        .launcher-settings-card label.launcher-settings-hint {{ font-size: 12px; color: alpha({colors['foreground']}, 0.65); }}
        dropdown.launcher-compact-dropdown > button {{ min-height: 0; padding: 2px 8px; font-size: 12px;
            border: 1px solid alpha({colors['foreground']}, 0.35); border-radius: {radius}px; background: transparent; }}
        dropdown.launcher-compact-dropdown popover label {{ font-size: 12px; }}
        /* Clearly visible on the dark card: a light track and knob when off,
           the accent when on; only a switch that cannot be used is faded. */
        switch.launcher-compact-switch {{ min-height: 16px; min-width: 36px;
            background: alpha({colors['foreground']}, 0.22); border: 1px solid alpha({colors['foreground']}, 0.45); }}
        switch.launcher-compact-switch:checked {{ background: {colors['accent']}; border-color: {colors['accent']}; }}
        switch.launcher-compact-switch slider {{ min-width: 14px; min-height: 14px; margin: 1px;
            background: {colors['foreground']}; border: none; box-shadow: none; }}
        switch.launcher-compact-switch:checked slider {{ background: {colors['background']}; }}
        switch.launcher-compact-switch:disabled {{ opacity: 0.4; }}
        .launcher-menu-item {{
            outline: none;
            background: transparent;
            border-radius: {radius}px;
            padding: 0;
            color: #ffffff;
        }}
        .launcher-menu-item label {{
            font-size: 13px;
            font-weight: 500;
            color: #ffffff;
        }}
        .launcher-menu-item image {{
            color: {colors['foreground']};
        }}
        separator.launcher-menu-separator {{
            background-color: {muted_hex};
            background-image: none;
            opacity: 0.6;
            min-height: 1px;
            margin: 3px 6px;
        }}
        .launcher-menu-item:focus {{
            background: {sel_hex};
        }}
        .launcher-menu-item:focus label {{
            color: {sel_fg};
            font-weight: 600;
        }}
        .launcher-menu-item:focus image {{
            color: {sel_fg};
        }}
        .launcher-menu-item.destructive label {{
            color: #ff9999;
        }}
        .launcher-menu-item.destructive image {{
            color: #ff9999;
        }}
        .launcher-menu-item.destructive:focus {{
            background: {colors['red']};
        }}
        .launcher-menu-item.destructive:focus label,
        .launcher-menu-item.destructive:focus image {{
            color: #ffffff;
        }}

        /* Hidden apps styling */
        .launcher-row-hidden {{
            opacity: 0.85;
        }}
        .launcher-hidden-icon {{
            opacity: 0.65;
        }}
        .launcher-hidden-badge {{
            background: rgba(188, 183, 176, 0.12);
            border: 1px solid rgba(188, 183, 176, 0.3);
            border-radius: {radius}px;
            padding: 1px 5px;
        }}
        .launcher-hidden-badge label {{
            font-size: 10px;
            font-weight: 500;
            color: {muted_hex};
        }}
        .launcher-hidden-badge image {{
            color: {muted_hex};
        }}
        .launcher-list row:selected .launcher-hidden-badge {{
            background: rgba(34, 31, 32, 0.15);
            border-color: rgba(34, 31, 32, 0.4);
        }}
        .launcher-list row:selected .launcher-hidden-badge label,
        .launcher-list row:selected .launcher-hidden-badge image {{
            color: {sel_fg};
        }}
        /* Group headings: a tinted band with the name in bold, the count in a chip. */
        .launcher-list row.launcher-group-row {{ padding: 0; min-height: 0; background: none; }}
        .launcher-list row.launcher-group-row-next {{ margin-top: 10px; }}
        .launcher-group-band {{
            background: alpha({colors['foreground']}, 0.07);
            border-left: 3px solid {colors['accent']};
            border-radius: {radius}px;
            padding: 5px 6px 5px 8px;
        }}
        .launcher-list row.launcher-group-row:selected .launcher-group-band {{ background: alpha({colors['accent']}, 0.22); }}
        .launcher-group-title {{ font-size: 14px; font-weight: 700; }}
        .launcher-group-count {{
            font-size: 11px; font-weight: 600; color: {dim_hex};
            border: 1px solid alpha({colors['foreground']}, 0.25); border-radius: 9px; padding: 0 6px;
        }}
        .launcher-group-chevron {{ color: {colors['accent']}; }}
        .launcher-view-switch {{
            border: 1px solid alpha({colors['foreground']}, 0.25); border-radius: {radius}px; margin-right: 4px;
        }}
        .launcher-view-switch button.launcher-header-btn {{ margin-right: 0; border-radius: 0; }}
        .launcher-view-switch button.launcher-toggle-active {{ background: alpha({colors['accent']}, 0.35); }}
        .launcher-card button.launcher-group-menu {{ padding: 2px; min-width: 0; min-height: 0; }}
        .launcher-group-error {{ color: {colors['red']}; font-size: 12px; }}
        .launcher-details-name {{ font-size: 15px; font-weight: 600; }}
        .launcher-details-value {{ font-size: 12px; }}
        .launcher-card button.launcher-copy-btn {{ padding: 1px; min-width: 0; min-height: 0; }}
        .launcher-toggle-active {{
            background: rgba(147, 147, 195, 0.25);
            border-radius: {radius}px;
        }}
        /* Resize grip in the card's corner. */
        .launcher-grip {{ color: {muted_hex}; margin: 4px; opacity: 0.6; }}
        .launcher-grip:hover {{ color: {colors['accent']}; opacity: 1; }}
        /* Overview cards. */
        .launcher-grid {{ background: transparent; padding: 2px; }}
        .launcher-grid flowboxchild.launcher-tile {{
            background: alpha({colors['foreground']}, 0.045);
            border: 1px solid alpha({colors['foreground']}, 0.10);
            border-radius: {radius}px; padding: 10px 10px 8px 12px; min-height: 128px;
        }}
        .launcher-grid flowboxchild.launcher-tile:hover {{ border-color: alpha({colors['accent']}, 0.55); }}
        .launcher-grid flowboxchild.launcher-tile:selected {{
            background: alpha({colors['accent']}, 0.16); border-color: {colors['accent']};
        }}
        .launcher-grid flowboxchild:focus-visible {{ outline: none; }}
        .launcher-tile-name {{ font-size: 13px; font-weight: 600; margin-top: 4px; }}
        .launcher-card button.launcher-tile-menu {{ padding: 2px; min-width: 0; min-height: 0; opacity: 0.55; }}
        .launcher-tile:selected button.launcher-tile-menu, .launcher-tile:hover button.launcher-tile-menu {{ opacity: 1; }}
        .launcher-tile .launcher-run-dot {{ min-width: 10px; min-height: 10px; }}
        /* Folder layout: a group as a box of icons, the name under it. */
        .launcher-grid flowboxchild.launcher-folder {{
            background: transparent; border: none; padding: 4px 4px 6px 4px; border-radius: {radius}px;
        }}
        .launcher-folder-icons {{
            background: alpha({colors['foreground']}, 0.06);
            border: 1px solid alpha({colors['foreground']}, 0.10);
            border-radius: {radius}px; padding: 10px 6px;
        }}
        .launcher-folder-scroller {{ background: transparent; }}
        .launcher-grid flowboxchild.launcher-folder.launcher-folder-drop .launcher-folder-icons {{
            background: alpha({colors['accent']}, 0.30); border-color: {colors['accent']}; border-style: dashed;
        }}
        .launcher-filter-entry {{ min-height: 30px; border-radius: 999px; padding: 0 10px; font-size: 13px;
            background: alpha({colors['foreground']}, 0.06); border: 1px solid alpha({colors['foreground']}, 0.14); }}
        .launcher-filter-entry:focus-within {{ border-color: {colors['accent']}; }}
        .launcher-grid flowboxchild.launcher-folder:hover .launcher-folder-icons {{ border-color: alpha({colors['accent']}, 0.55); }}
        .launcher-grid flowboxchild.launcher-folder:selected {{ background: transparent; }}
        .launcher-grid flowboxchild.launcher-folder:selected .launcher-folder-icons {{
            background: alpha({colors['accent']}, 0.16); border-color: {colors['accent']};
        }}
        .launcher-card button.launcher-folder-app {{ padding: 6px; min-width: 0; min-height: 0; border-radius: {radius}px; }}
        .launcher-card button.launcher-folder-app:hover {{ background: alpha({colors['foreground']}, 0.10); }}
        .launcher-card .launcher-update-badge {{ border-color: alpha({colors['accent']}, 0.6); }}
        .launcher-update-badge label, .launcher-update-badge image {{ color: {colors['accent']}; }}
        /* Where a dragged group or icon will land: a bar before or after. */
        .launcher-drop-marker {{ background: {colors['accent']}; border-radius: 999px;
            box-shadow: 0 0 6px alpha({colors['accent']}, 0.8); }}
        .launcher-new-star-inline {{ color: {colors['yellow']}; font-size: 12px; }}
        .launcher-new-star {{ color: {colors['yellow']}; font-size: 14px; margin: -4px 0 0 -3px;
            text-shadow: 0 0 2px {colors['background']}, 0 0 2px {colors['background']}; }}
        .launcher-folder-hidden-icon {{ opacity: 0.4; filter: grayscale(1); }}
        .launcher-card button.launcher-folder-hidden {{ border: 1px dashed alpha({colors['foreground']}, 0.45); }}
        .launcher-folder-hidden-mark {{
            color: {colors['foreground']}; background: alpha({colors['background']}, 0.9);
            border-radius: 999px; padding: 2px; margin: -2px;
        }}
        .launcher-update-dot {{
            min-width: 10px; min-height: 10px; border-radius: 999px;
            background: {colors['accent']}; border: 2px solid {colors['background']};
        }}
        .launcher-folder-name {{ font-size: 12px; font-weight: 600; }}
        /* Filter bar under the overview. */
        .launcher-filter-bar {{ padding-top: 8px; border-top: 1px solid alpha({colors['foreground']}, 0.12); }}
        .launcher-card button.launcher-filter-mode {{ padding: 3px 12px; min-height: 0; border-radius: 0; font-size: 12px; }}
        .launcher-card button.launcher-filter-mode.launcher-toggle-active {{ background: alpha({colors['accent']}, 0.35); font-weight: 600; }}
        .launcher-card button.launcher-filter-chip {{
            padding: 3px 10px; min-height: 0; font-size: 12px; border-radius: 999px;
            border: 1px solid alpha({colors['foreground']}, 0.18);
        }}
        .launcher-card button.launcher-filter-chip:hover {{ border-color: alpha({colors['accent']}, 0.7); }}
        .launcher-card button.launcher-filter-chip.launcher-filter-on {{
            background: {colors['accent']}; border-color: {colors['accent']};
        }}
        .launcher-card button.launcher-filter-chip.launcher-filter-on label {{ color: {colors['background']}; font-weight: 600; }}
        /* The selected card's group: an accent outline and an underline. */
        .launcher-card button.launcher-filter-chip.launcher-filter-current {{
            border: 2px solid {colors['accent']}; box-shadow: inset 0 -2px {colors['accent']};
        }}
        .launcher-header-hint {{ font-size: 10px; color: {dim_hex}; }}
        .launcher-filter-dragging button.launcher-filter-group {{ border-style: dashed; border-color: {colors['accent']}; }}
        .launcher-card button.launcher-filter-chip.launcher-filter-drop {{ background: alpha({colors['accent']}, 0.45); }}
        .launcher-filter-count {{ font-size: 10px; opacity: 0.7; }}
        .launcher-filter-dot {{ min-width: 7px; min-height: 7px; border-radius: 999px; background: {colors['green']}; }}
        .launcher-card button.launcher-filter-chip.launcher-filter-new {{ border-style: dashed; color: {dim_hex}; }}
        /* The running toggle's dot: green while any app runs, grey otherwise. */
        button.launcher-running-btn image {{ color: {dim_hex}; }}
        button.launcher-running-btn.any-running image {{ color: {colors['green']}; }}

        /* Shortcuts */
        .launcher-shortcut-chip {{
            font-size: 10px;
            color: {muted_hex};
            border: 1px solid alpha({muted_hex}, 0.45);
            border-radius: {radius}px;
            padding: 0 4px;
        }}
        .launcher-list row:selected > box > box > .launcher-shortcut-chip {{ color: {sel_desc_fg}; border-color: alpha({sel_desc_fg}, 0.5); }}
        /* Same chip look in the app menu, which otherwise takes the menu label font. */
        .launcher-menu-item label.launcher-shortcut-chip {{
            font-size: 10px; font-weight: normal; color: {colors['foreground']};
        }}
        .launcher-menu-item:focus label.launcher-shortcut-chip {{
            font-weight: normal; color: {sel_desc_fg}; border-color: alpha({sel_desc_fg}, 0.5);
        }}
        .launcher-shortcut-more {{ font-size: 9px; font-weight: 700; color: {colors['background']};
            background: alpha({muted_hex}, 0.8); border-radius: 999px; padding: 0 4px; }}
        .launcher-list row:selected .launcher-shortcut-more {{ background: {sel_desc_fg}; }}
        .sc-item-editing {{ background: alpha({colors['accent']}, 0.14); box-shadow: inset 2px 0 {colors['accent']}; }}
        .sc-section {{ font-size: 11px; font-weight: 600; color: {muted_hex}; margin-top: 4px; }}
        .sc-item {{ background: alpha({colors['foreground']}, 0.05); border-radius: {radius}px; padding: 6px 8px; }}
        .sc-keycap {{ font-size: 13px; font-weight: 600; }}
        .sc-note {{ font-size: 11px; color: {muted_hex}; }}
        .sc-empty {{ font-size: 12px; color: {muted_hex}; padding: 4px 2px; }}
        .sc-mod {{ border-radius: {radius}px; padding: 4px 10px; min-height: 0;
            background: alpha({colors['foreground']}, 0.06); border: 1px solid alpha({colors['foreground']}, 0.12); }}
        .sc-mod:checked {{ background: {colors['accent']}; color: {colors['background']}; border-color: {colors['accent']}; }}
        .sc-key {{ border-radius: {radius}px; padding: 8px 10px; min-height: 30px;
            background: alpha({colors['foreground']}, 0.04); border: 1px dashed {colors['accent']}; }}
        .sc-status {{ font-size: 12px; border-radius: {radius}px; padding: 6px 8px; }}
        .sc-free {{ color: {colors['green']}; background: alpha({colors['green']}, 0.12); }}
        .sc-warn {{ color: {taken_hex}; background: alpha({taken_hex}, 0.12); }}
        .sc-danger {{ color: {error_hex}; background: alpha({error_hex}, 0.14); }}
        .sc-info {{ color: {muted_hex}; background: alpha({colors['foreground']}, 0.05); }}
        .sc-suggest {{ font-size: 11px; border-radius: {radius}px; padding: 3px 8px; min-height: 0;
            color: {colors['green']}; border: 1px solid alpha({colors['green']}, 0.5); background: transparent; }}
        .sc-suggest:hover {{ background: alpha({colors['green']}, 0.15); }}
        .sc-primary {{ background: {colors['accent']}; color: {colors['background']}; border-radius: {radius}px; padding: 4px 14px; }}
        .sc-primary.sc-warn-btn {{ background: {taken_hex}; color: {colors['background']}; }}
        .sc-primary.sc-danger-btn {{ background: {error_hex}; color: #ffffff; }}
        .sc-primary label, .launcher-card .sc-primary {{ color: {colors['background']}; }}
        .launcher-card .sc-primary.sc-danger-btn {{ color: #ffffff; }}
        .launcher-card .sc-mod:checked {{ color: {colors['background']}; }}
        .sc-cap {{ font-size: 12px; font-weight: 600; padding: 1px 7px; border-radius: {radius}px;
            border: 1px solid alpha({colors['foreground']}, 0.35); border-bottom-width: 2px;
            background: alpha({colors['foreground']}, 0.06); }}
        .sc-cap-lg {{ font-size: 15px; padding: 3px 10px; }}
        .sc-cap-dim {{ opacity: 0.5; }}
        .sc-item-off .sc-note {{ font-style: italic; }}
        .sc-tag {{ font-size: 10px; color: {muted_hex}; }}
        .launcher-card .sc-tag {{ color: {muted_hex}; }}
        .sc-divider {{ background-color: alpha({colors['foreground']}, 0.15); min-height: 1px; margin: 4px 0; }}
        .launcher-card .sc-placeholder {{ font-style: italic; font-size: 14px; color: {muted_hex}; margin-left: 4px; }}
        .launcher-card .sc-default label {{ font-size: 11px; color: {muted_hex}; }}
        .launcher-card .sc-default check {{ border-radius: {radius}px; background: transparent;
            border: 1px solid {muted_hex}; color: {colors['background']}; }}
        .launcher-card .sc-default check:checked {{ background: {colors['accent']}; border-color: {colors['accent']}; }}
        .sc-letter {{ font-size: 11px; border-radius: {radius}px; padding: 3px 0; min-height: 0; min-width: 26px;
            background: transparent; border: 1px solid; }}
        .launcher-card button.sc-keychip.sc-free-key, .launcher-card button.sc-keychip.sc-free-key label {{ color: {free_hex}; border-color: alpha({free_hex}, 0.55); }}
        .launcher-card button.sc-keychip.sc-yours-key, .launcher-card button.sc-keychip.sc-yours-key label {{ color: {taken_hex}; border-color: alpha({taken_hex}, 0.6); }}
        .launcher-card button.sc-keychip.sc-omarchy-key, .launcher-card button.sc-keychip.sc-omarchy-key label {{ color: {omarchy_hex}; border-color: alpha({omarchy_hex}, 0.6); }}
        .launcher-card button.sc-keychip.sc-own-key, .launcher-card button.sc-keychip.sc-own-key label {{ color: {colors['foreground']}; border-color: {colors['accent']}; }}
        .sc-keychip.sc-free-key:hover {{ background: alpha({free_hex}, 0.15); }}
        .sc-keychip.sc-yours-key:hover {{ background: alpha({taken_hex}, 0.15); }}
        .sc-keychip.sc-omarchy-key:hover {{ background: alpha({omarchy_hex}, 0.15); }}
        .launcher-card button.sc-keychip.sc-key-picked {{ background: alpha({colors['foreground']}, 0.18); border-width: 2px; }}
        .sc-legend {{ font-size: 10px; }}
        .launcher-card .sc-legend-free {{ color: {free_hex}; }}
        .launcher-card .sc-legend-yours {{ color: {taken_hex}; }}
        .launcher-card .sc-legend-omarchy {{ color: {omarchy_hex}; }}
        .sc-hint-omarchy {{ color: {omarchy_hex}; background: alpha({omarchy_hex}, 0.12); }}
        .launcher-card .sc-hint-omarchy {{ color: {omarchy_hex}; }}
        .sc-primary.sc-omarchy-btn {{ background: {omarchy_hex}; color: {colors['background']}; }}
        .launcher-menu-item.uninstall label, .launcher-menu-item.uninstall image {{ color: {error_hex}; }}
        .launcher-menu-item.uninstall:focus {{ background: {error_hex}; }}
        .launcher-menu-item.uninstall:focus label, .launcher-menu-item.uninstall:focus image {{ color: #ffffff; }}
        .sc-other {{ font-size: 11px; border-radius: {radius}px; padding: 3px 8px; min-height: 0;
            background: alpha({colors['foreground']}, 0.06); border: 1px solid alpha({colors['foreground']}, 0.18); }}
        .launcher-card button.sc-other {{ background: transparent; border: 1px solid; }}
        .sc-suggest-on {{ background: alpha({colors['green']}, 0.25); }}
        .launcher-card button.sc-primary:disabled {{ background: alpha({colors['foreground']}, 0.08); }}
        .launcher-card button.sc-primary:disabled label {{ color: {muted_hex}; font-weight: 500; }}
        .launcher-card .sc-mod:checked label, .launcher-card button.sc-mod:checked {{ color: {colors['background']}; font-weight: 600; }}
        .launcher-card button.sc-primary label {{ color: {colors['background']}; font-weight: 600; }}
        .launcher-card button.sc-primary.sc-danger-btn label {{ color: #ffffff; }}
        .launcher-card button.sc-suggest label {{ color: {colors['green']}; }}
        .launcher-card .sc-free {{ color: {colors['green']}; }}
        .launcher-card .sc-warn {{ color: {taken_hex}; }}
        .launcher-card .sc-danger {{ color: {error_hex}; }}
        .launcher-card .sc-suggest {{ color: {colors['green']}; }}
        .sc-small {{ font-size: 11px; padding: 2px 8px; min-height: 0; border-radius: {radius}px; }}
        .launcher-card button, .launcher-card scrollbar, .launcher-card scrollbar slider,
        popover.launcher-popover contents, popover.launcher-popover button {{ border-radius: {radius}px; }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.style_provider = provider

    def open(self):
        # A drag that never reported its end must not hold refreshes back.
        self.tile_dragging, self.dragged_app, self.dragged_group = False, None, None
        self.refresh_after_drag = None
        self.shortcut_state = shortcuts.load_state()
        self.prune_uninstalled_shortcuts()
        self.update_dead_binds_question()
        self.list_bindings = None
        try:
            self.apps = parse_desktop_files()
            self.prepare_entries(self.apps, "apps")
        except Exception as e:
            print("Error parsing desktop files:", e, file=sys.stderr)
        try:
            self.theme.reload()
            self.apply_theme()
        except Exception as e:
            print("Error applying theme:", e, file=sys.stderr)
        if self.mode == "plugins":
            self.load_plugins()
        self.search_query = ""
        self.resize_to(*self.saved_card_size())  # Within the monitor it opens on.
        self.move_to(self.position)
        GLib.idle_add(self.keep_on_screen)
        self.update_hidden_button_state()
        try:
            self.refresh_running()
            self.refresh_list()
        except Exception as e:
            print("Error refreshing list:", e, file=sys.stderr)
        self.window.present()
        self.tell_dock(True)
        # The dock shows itself now: fit the hole to it once it has, then follow it.
        GLib.timeout_add(250, self.update_input_region)
        if self.dock_space() and not getattr(self, "dock_follow_source", 0):
            self.dock_follow_source = GLib.timeout_add(1000, self.follow_dock)
        self.layer_watch.start()
        if self.mode == "apps":
            self.prefetch_plugins()
        self.check_updates()
        # Restarted after a language change: back to where the user was.
        if os.environ.pop("SIMPLE_LAUNCHER_PAGE", "") == "settings":
            GLib.idle_add(self.open_settings_page)
            return
        def _focus():
            if self.overview:
                first = self.selected_item() or self.grid.get_child_at_index(0)
                (first or self.grid).grab_focus()
                return GLib.SOURCE_REMOVE
            first = self.rows.get_first_child()
            if first:
                first.grab_focus()
            else:
                self.rows.grab_focus()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(_focus)

    def on_menu_opened(self, _name):
        """Another menu wants the keyboard: get out of its way at once."""
        if self.window and self.window.get_visible() and not self.admin_auth_pending:
            self.hide()

    def hide(self):
        self.layer_watch.stop()
        self.tell_dock(False)
        if self.actions_menu_open():
            self.actions_popover.popdown()
        if self.aux_page_open():
            self.close_aux_page(refresh=False)
        if self.shortcut_app is not None:
            self.close_shortcut_page(focus_list=False)
        if self.window and self.window.get_visible():
            self.window.set_visible(False)

    def toggle(self, mode=None):
        """Show or hide the launcher; with a mode, show that list: a key for
        the plugins switches an open app list over instead of closing it."""
        if self.window and self.window.get_visible():
            if mode and mode != self.mode and self.shortcut_app is None:
                self.toggle_mode()
            else:
                self.hide()
        elif code_stamp() != self.code_stamp:
            self.restart(mode)
        else:
            if mode:
                self.mode = mode
            self.open()
        return GLib.SOURCE_REMOVE

    def restart(self, mode=None):
        """Replace this process with the updated code. The PID stays, so the
        toggle script still finds it; the new process opens on start."""
        os.environ["SIMPLE_LAUNCHER_MODE"] = mode or ""
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, str(PLUGIN_DIR / "launcher_popup.py")])

    def watch_app_dirs(self):
        """Refresh the open list when apps are installed or removed."""
        for directory in (*DESKTOP_DIRS, *map(Path, get_appimage_directories())):
            if not directory.is_dir():
                continue
            try:
                monitor = Gio.File.new_for_path(str(directory)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            except GLib.Error:
                continue
            monitor.connect("changed", self.on_app_dir_changed)
            self.app_monitors.append(monitor)
        # Plugins appear the same way: a new folder, or the shell enabling one
        # in shell.json (marketplace installs do both).
        for directory, names in ((USER_PLUGINS_DIR, None), (USER_PLUGINS_DIR.parent, {"shell.json"})):
            try:
                monitor = Gio.File.new_for_path(str(directory)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                continue
            monitor.connect("changed", lambda _m, file, other, _e, names=names: self.on_app_dir_changed()
                            if names is None or {f.get_basename() for f in (file, other) if f} & names else None)
            self.app_monitors.append(monitor)

    def on_app_dir_changed(self, *_args):
        # Package managers touch many files at once; act once things settle.
        if self.apps_changed_source:
            GLib.source_remove(self.apps_changed_source)
        self.apps_changed_source = GLib.timeout_add(700, self.reload_apps)

    def reload_apps(self):
        self.apps_changed_source = 0
        if not (self.window and self.window.get_visible()):
            # Shortcuts of an app removed while the launcher is closed go now,
            # not only on the next open; the list follows on open.
            self.shortcut_state = shortcuts.load_state()
            self.prune_uninstalled_shortcuts()
            return GLib.SOURCE_REMOVE
        if self.shortcut_app is not None:
            return GLib.SOURCE_REMOVE  # Picked up on the next open.
        if self.actions_menu_open():
            self.apps_changed_source = GLib.timeout_add(700, self.reload_apps)
            return GLib.SOURCE_REMOVE
        row = self.selected_item()
        selected = self.row_key(row) if row is not None else None
        try:
            self.apps = parse_desktop_files()
            self.prepare_entries(self.apps, "apps")
            if self.mode == "plugins":
                self.load_plugins()
            self.prune_uninstalled_shortcuts()
            self.update_dead_binds_question()
            self.refresh_list(selected_id=selected)
        except Exception as e:
            print("Error reloading apps:", e, file=sys.stderr)
        return GLib.SOURCE_REMOVE

    def prune_uninstalled_shortcuts(self):
        """Drop launcher shortcuts of uninstalled apps, so their keys do not
        stay dead and the bindings they replaced work again."""
        state = copy.deepcopy(self.shortcut_state)
        removed = shortcuts.prune_missing(state)
        names = []
        if removed:
            try:
                shortcuts.save_and_apply(state)
            except (RuntimeError, OSError) as error:
                print("Could not drop shortcuts of removed apps:", error, file=sys.stderr)
            else:
                self.shortcut_state = shortcuts.load_state()
                names += [f"{entry['name']} ({', '.join(shortcuts.combo_label(c) for c in entry['shortcuts']) or 'turned-off keys'})"
                          for entry in removed]
        if names:
            self.list_bindings = None
            self.notify(_("Shortcuts of removed apps deleted"), ", ".join(names))

    def update_dead_binds_question(self):
        """Hand-written shortcuts of removed apps live in the user's own files:
        they are only deleted after a yes here, line by line as listed."""
        try:
            pending = shortcuts.pending_dead_binds()
        except OSError:
            pending = []
        self.dead_binds = [bind for _path, _number, bind in pending]
        if not self.dead_binds:
            self.dead_question.set_visible(False)
            return
        listed = "\n".join(f"• {b['description']}: {shortcuts.combo_label(b['combo'])} ({b['file']})"
                           for b in self.dead_binds)
        self.dead_question_label.set_text(
            ngettext("The app of this shortcut was uninstalled. Delete it from your Hyprland files?",
                     "The apps of these shortcuts were uninstalled. Delete them from your Hyprland files?",
                     len(self.dead_binds)) + f"\n{listed}")
        self.dead_question.set_visible(True)

    def answer_dead_binds(self, delete):
        keys = [b["key"] for b in self.dead_binds]
        self.dead_question.set_visible(False)
        try:
            if not delete:
                shortcuts.keep_binds(keys)
                return
            removed = shortcuts.remove_dead_user_binds(keys)
        except (RuntimeError, OSError) as error:
            self.notify(_("Could not delete the shortcuts"), str(error))
            return
        if removed:
            self.list_bindings = None
            self.refresh_list()
            self.notify(_("Shortcuts of removed apps deleted"),
                        ", ".join(f"{b['description']} ({shortcuts.combo_label(b['combo'])})" for b in removed))

    def on_overlay_click(self, _gesture, _count, x, y):
        if self.resize_start is not None or time.monotonic() < getattr(self, "resized_at", 0) + 0.3:
            return  # The end of a resize, not a click beside the card.
        picked = self.overlay.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is None or not (picked == self.card_frame or picked.is_ancestor(self.card_frame)):
            self.hide()

    def on_active_changed(self, window, _property):
        if window.get_visible() and not window.is_active():
            GLib.timeout_add(100, self.hide_if_inactive)

    def hide_if_inactive(self):
        if self.actions_menu_open():
            return GLib.SOURCE_REMOVE
        if getattr(self, "tile_dragging", False):
            return GLib.SOURCE_REMOVE  # A card is being dragged onto a group.
        # The password dialog for "Open as administrator" has the focus.
        if getattr(self, "admin_auth_pending", False):
            return GLib.SOURCE_REMOVE
        # Just opened a website from the details page: the browser has the focus.
        if time.monotonic() < getattr(self, "keep_open_until", 0):
            return GLib.SOURCE_REMOVE
        if not (self.window and self.window.get_visible() and not self.window.is_active()):
            return GLib.SOURCE_REMOVE
        rect = getattr(self, "dock_screen_rect", None)
        if rect is None:
            self.hide()
            return GLib.SOURCE_REMOVE
        # The pointer may have gone over the dock (focus follows the mouse):
        # then stay open; back over the popup, it gets the keyboard again.
        # Asked on a thread, never blocking the popup.

        def work():
            over = dock_core.inside(dock_core.hyprctl_json("cursorpos"), rect)
            GLib.idle_add(decide, over)

        def decide(over):
            if not over and self.window.get_visible() and not self.window.is_active():
                self.hide()
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def select_row(self, row):
        if row:
            self.rows.select_row(row)
            row.grab_focus()

    def on_key(self, _controller, key, _code, state):
        # The settings page: Esc and Backspace go back to the list.
        if self.settings_open() and key in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            self.close_settings_page()
            return True
        page = self.stack.get_visible_child_name()
        # The details page: Esc and Backspace go back; the group name page
        # needs Backspace for typing, so only Esc.
        if page == "details":
            if key in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
                self.close_aux_page()
                return True
            return False
        if page == "icon":
            # Backspace edits the search field: only Esc goes back.
            if key == Gdk.KEY_Escape:
                self.close_aux_page()
                return True
            return False
        if page == "groupname":
            if key == Gdk.KEY_Escape:
                self.close_aux_page()
                return True
            # Enter saves from anywhere on the page (the field's own activate too).
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter) and self.group_name_target:
                self.save_group_name()
                return True
            return False
        if key == Gdk.KEY_Escape:
            if self.actions_menu_open():
                self.actions_popover.popdown()
                return True
            self.on_escape()
            return True

        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if self.overview and self.shortcut_app is None:
            if key in (Gdk.KEY_g, Gdk.KEY_G) and ctrl:
                self.next_overview_filter(-1 if state & Gdk.ModifierType.SHIFT_MASK else 1)
                return True
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
                tile = self.selected_item()
                if tile is not None:
                    self.activate_tile(tile)
                return True
            if key == Gdk.KEY_Menu:
                tile = self.selected_item()
                if hasattr(tile, "group"):
                    if tile.group not in (groups.RUNNING, groups.OTHER):
                        self.show_group_actions(tile.actions_button, tile.group)
                elif tile is not None:
                    self.show_actions(tile.actions_button, tile.app)
                return True
            if not ctrl and self.overview_key(key, state):
                return True

        if key in (Gdk.KEY_g, Gdk.KEY_G) and ctrl and self.shortcut_app is None:
            self.toggle_view()
            return True

        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            self.activate_selected()
            return True

        if key == Gdk.KEY_Down:
            row = self.rows.get_selected_row()
            target = row.get_next_sibling() if row else self.rows.get_first_child()
            if target:
                self.select_row(target)
            return True

        if key == Gdk.KEY_Up:
            row = self.rows.get_selected_row()
            target = row.get_prev_sibling() if row else self.rows.get_first_child()
            if target:
                self.select_row(target)
            return True

        if key in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            target = self.rows.get_first_child()
            if target:
                self.select_row(target)
            return True

        if key in (Gdk.KEY_End, Gdk.KEY_KP_End):
            target = self.rows.get_last_child()
            if target:
                self.select_row(target)
            return True

        if key in (Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down):
            row = self.rows.get_selected_row() or self.rows.get_first_child()
            for _ in range(6):
                if row and row.get_next_sibling():
                    row = row.get_next_sibling()
            if row:
                self.select_row(row)
            return True

        if key in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
            row = self.rows.get_selected_row() or self.rows.get_first_child()
            for _ in range(6):
                if row and row.get_prev_sibling():
                    row = row.get_prev_sibling()
            if row:
                self.select_row(row)
            return True

        if key in (Gdk.KEY_Right, Gdk.KEY_Menu):
            row = self.rows.get_selected_row()
            if row and hasattr(row, "group"):
                if row.group in self.groups["collapsed"] and key == Gdk.KEY_Right:
                    self.toggle_group(row.group)
                elif getattr(row, "actions_button", None) is not None:
                    self.show_group_actions(row.actions_button, row.group)
                return True
            if row and hasattr(row, "actions_button"):
                self.show_actions(row.actions_button, row.app)
                return True

        if key in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            row = self.rows.get_selected_row()
            if row and hasattr(row, "group") and row.group not in self.groups["collapsed"]:
                self.toggle_group(row.group)
                return True

        if key == Gdk.KEY_Tab and self.shortcut_app is None:
            self.toggle_mode()
            return True

        if key in (Gdk.KEY_k, Gdk.KEY_K) and state & Gdk.ModifierType.CONTROL_MASK:
            row = self.selected_item()
            if row is not None and hasattr(row, "app"):
                self.open_shortcut_page(row.app)
            return True

        if key == Gdk.KEY_Delete:
            row = self.selected_item()
            if row is not None and hasattr(row, "app"):
                self.close_app(row.app)
            return True

        if key == Gdk.KEY_BackSpace:
            if self.search_query:
                self.search_query = self.search_query[:-1]
                self.refresh_list()
                return True
            if self.open_folder():
                self.leave_folder()
                return True
            return False

        has_modifier = bool(state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK))
        if not has_modifier:
            ch_code = Gdk.keyval_to_unicode(key)
            if ch_code:
                ch = chr(ch_code)
                if ch and ch.isprintable() and key not in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
                    self.search_query += ch
                    self.refresh_list()
                    return True

        return False

    def poll(self):
        if self.window and self.window.is_visible():
            # Replacing a row also destroys its anchored actions menu.
            # Rebuilding the cards mid-drag would drop the dragged card.
            menu_open = self.actions_menu_open() or getattr(self, "tile_dragging", False)
            # Tray icons come and go with their apps (Steam started, NordVPN quit).
            if self.mode == "plugins" and not menu_open and self.shortcut_app is None and self.tray_changed():
                self.reload_apps()
            elif not menu_open:
                self.refresh_running_async()
            if self.theme.reload():
                self.apply_theme()
        return GLib.SOURCE_CONTINUE

    def refresh_running_async(self):
        """The poll's window and process scan on a thread; the list is
        rebuilt on the main loop when something changed."""
        if getattr(self, "running_scan", False):
            return
        self.running_scan = True

        def work():
            clients = None
            try:
                result = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    clients = json.loads(result.stdout)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            names = background_process_names()
            GLib.idle_add(done, clients, names)

        def done(clients, names):
            self.running_scan = False
            previous = (self.clients, self.process_names)
            if clients is not None:
                self.clients = clients
            self.process_names = names
            self.window_children = {}
            menu_open = self.actions_menu_open() or getattr(self, "tile_dragging", False)
            if previous != (self.clients, self.process_names) and not menu_open and self.window.is_visible():
                self.refresh_list()
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    def refresh_running(self):
        previous = (self.clients, self.process_names)
        try:
            result = subprocess.run(["hyprctl", "clients", "-j"],
                                    capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                self.clients = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self.process_names = background_process_names()
        self.window_children = {}
        return previous != (self.clients, self.process_names)

    def window_matches(self, client, app):
        """Whether this window belongs to app. A terminal window running a
        command another app launches (foot -e claude) belongs to that app,
        not to the terminal itself."""
        children = self.window_children.get(client.get("pid"))
        if children is None:
            children = self.window_children[client.get("pid")] = child_process_names(client.get("pid"))
        command = terminal_command(app)
        if command:
            if command in children:
                return True
        elif children & self.terminal_commands():
            return False
        return client_matches_app(client, app)

    def terminal_commands(self):
        cached = getattr(self, "_terminal_commands", None)
        if cached is None or cached[0] is not self.apps:
            cached = self._terminal_commands = (self.apps, {c for a in self.apps if (c := terminal_command(a))})
        return cached[1]

    def app_state(self, app):
        """Run state, remembered until the next window/process scan."""
        key = (id(self.clients), id(self.process_names), id(self.apps))
        if getattr(self, "_state_key", None) != key:
            self._state_key, self._states = key, {}
        ident = (app.get("desktop_id"), "tray_id" in app, "plugin_id" in app)
        if ident not in self._states:
            self._states[ident] = self._app_state(app)
        return self._states[ident]

    def _app_state(self, app):
        if "tray_id" in app:
            return "background"  # Listed only while its app runs.
        if "plugin_id" in app:
            return "stopped"  # The shell does not report which panels are open.
        if any(self.window_matches(client, app) for client in self.clients):
            return "active"
        name = app_process_name(app)
        if name in self.process_names:
            if name in TERMINALS and not self.terminal_runs_on_its_own(name):
                return "stopped"
            return "background"
        return "stopped"

    def terminal_runs_on_its_own(self, name):
        """Whether a terminal process exists that is not just the window of
        another app (foot -e claude belongs to Claude CLI, not to Foot)."""
        claimed = {client.get("pid") for client in self.clients
                   if self.window_children.get(client.get("pid"), set()) & self.terminal_commands()}
        return bool(process_pids(name) - claimed)

    def refresh_list(self, selected_id=None):
        if getattr(self, "tile_dragging", False) or getattr(self, "dragged_group", None) is not None:
            # Rebuilding now would destroy the dragged widget and the drop
            # targets, and GTK cancels the drag: refresh once it has ended.
            self.refresh_after_drag = (selected_id,) if selected_id is not None else \
                getattr(self, "refresh_after_drag", None) or (None,)
            return
        self.update_title()
        self.sync_filter_entry()
        if any(self.app_state(app) != "stopped" for app in self.apps):
            self.running_button.add_css_class("any-running")
        else:
            self.running_button.remove_css_class("any-running")
        selected = self.selected_item()
        if selected_id is None:
            selected_id = self.row_key(selected) if selected else None
        selected_row = None
        query = self.search_query.casefold().strip()
        if self.list_bindings is None:
            self.list_bindings = shortcuts.Bindings(self.shortcut_state)

        def shown(app):
            is_hidden = app["desktop_id"] in self.hidden_ids_for(app)
            # Bar widgets taken out of the bar are hidden too: the eye shows them
            # to put them back. The launcher itself stays, for its own keys.
            if (is_hidden or self.off_bar(app)) and not self.show_hidden:
                return False
            return query in app["name"].casefold()

        if self.overview:
            entries = self.plugins if self.mode == "plugins" else self.apps
            self.refresh_overview([a for a in entries if shown(a)], selected_id)
            return
        while child := self.rows.get_first_child():
            self.rows.remove(child)

        def add(row):
            nonlocal selected_row
            self.rows.append(row)
            # A running app shows twice in the group views; keep the first.
            if selected_row is None and selected_id and selected_id == self.row_key(row):
                selected_row = row

        if self.view in ("groups", "origin"):
            entries = [a for a in (self.plugins if self.mode == "plugins" else self.apps) if shown(a)]
            # Tray icons head the plugin page, as in the list; running apps the
            # app page when the dot is on.
            if self.mode == "plugins":
                running = lambda a: "tray_id" in a
            else:
                running = (lambda a: self.app_state(a) != "stopped") if self.running_first else None
            first = True
            if self.view == "origin":
                index, defaults = details.omarchy_index(), details.default_desktops()
                sections = groups.origin_sections(
                    entries, lambda a: details.origin_section(a, index, defaults), running)
            else:
                sections = groups.sections(entries, self.groups, running)
            for gid, members in sections:
                # Empty groups show while browsing (to fill them), not while filtering.
                if not members and (query or gid in (groups.RUNNING, groups.OTHER, *groups.ORIGINS)):
                    continue
                collapsed = gid in self.groups["collapsed"] and not query
                add(self.make_group_row(gid, len(members), collapsed, first))
                first = False
                if not collapsed:
                    for app in members:
                        add(self.make_app_row(app))
        else:
            entries = self.plugins if self.mode == "plugins" else self.apps
            if self.grouping_running():
                # Stable sort: both groups keep the usual order.
                entries = sorted(entries, key=lambda a: self.app_state(a) == "stopped")
            for app in entries:
                if shown(app):
                    add(self.make_app_row(app))
        selected_row = selected_row or self.first_app_row() or self.rows.get_first_child()
        if selected_row:
            self.rows.select_row(selected_row)
            # Rebuilt rows need a layout before focus can scroll them into view.
            GLib.timeout_add(50, self.ensure_selected_visible, selected_row)

    @staticmethod
    def row_key(row):
        if row is None:
            return None
        if hasattr(row, "group"):
            return "group:" + row.group
        return row.app.get("desktop_id") if hasattr(row, "app") else None

    def first_app_row(self):
        row = self.rows.get_first_child()
        while row is not None and not hasattr(row, "app"):
            row = row.get_next_sibling()
        return row

    def make_app_row(self, app):
        is_hidden = app["desktop_id"] in self.hidden_ids_for(app)
        row = Gtk.ListBoxRow()
        row.app = app
        row.running = self.app_state(app) != "stopped"
        if is_hidden:
            row.add_css_class("launcher-row-hidden")
        line = Gtk.Box(spacing=8)
        icon_name = app.get("icon") or "application-x-executable"
        image_extensions = {".png", ".svg", ".xpm", ".jpg", ".jpeg", ".webp", ".bmp"}
        themed_name = Path(icon_name).stem if Path(icon_name).suffix.lower() in image_extensions else Path(icon_name).name
        texture = icon_texture(icon_name) if Path(icon_name).is_file() else None
        icon = Gtk.Image.new_from_paintable(texture) if texture is not None else Gtk.Image.new_from_icon_name(themed_name)
        icon.set_pixel_size(32)
        icon.set_valign(Gtk.Align.CENTER)
        if is_hidden:
            icon.add_css_class("launcher-hidden-icon")
        # The run dot sits on the icon's corner, like a dock, so the
        # shortcut chips on the right always line up.
        icon_slot = Gtk.Overlay()
        icon_slot.set_valign(Gtk.Align.CENTER)
        icon_slot.set_child(icon)
        state = self.app_state(app)
        if state != "stopped":
            dot = Gtk.Box()
            dot.add_css_class("launcher-run-dot")
            dot.add_css_class(state)
            dot.set_halign(Gtk.Align.END)
            dot.set_valign(Gtk.Align.END)
            dot.set_can_target(False)
            icon_slot.add_overlay(dot)
            icon_slot.set_tooltip_text(_("Open windows") if state == "active" else _("Running in background"))
        line.append(icon_slot)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.CENTER)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_row.set_valign(Gtk.Align.CENTER)

        name = Gtk.Label(label=app["name"], xalign=0)
        name.add_css_class("launcher-app-name")
        name.set_ellipsize(3)
        title_row.append(name)

        badges = []
        if is_hidden:
            badges.append(("view-conceal-symbolic", _("Hidden")))
        if app.get("bar_widget") and not app.get("in_bar"):
            badges.append(("list-remove-symbolic", _("Not in bar")))
            row.add_css_class("launcher-row-hidden")
            icon.add_css_class("launcher-hidden-icon")
        for badge_icon, badge_text in badges:
            badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
            badge.add_css_class("launcher-hidden-badge")
            badge.set_valign(Gtk.Align.CENTER)
            eye_icon = Gtk.Image.new_from_icon_name(badge_icon)
            eye_icon.set_pixel_size(11)
            badge.append(eye_icon)
            badge_lbl = Gtk.Label(label=badge_text)
            badge.append(badge_lbl)
            title_row.append(badge)
        if self.is_new(app):
            title_row.append(self.new_star())
        note = self.update_note(app)
        if note:
            title_row.append(self.update_badge(note))

        text_box.append(title_row)

        desc_text = app.get("description", "").strip()
        if desc_text:
            desc = Gtk.Label(label=desc_text, xalign=0)
            desc.add_css_class("launcher-app-desc")
            desc.set_ellipsize(3)
            text_box.append(desc)
            prefix = "[" + _("Hidden") + "] " if is_hidden else ""
            row.set_tooltip_text(f"{prefix}{app['name']} — {desc_text}")
        else:
            prefix = "[" + _("Hidden") + "] " if is_hidden else ""
            row.set_tooltip_text(f"{prefix}{app['name']}")

        line.append(text_box)
        assigned = self.app_shortcuts(app, self.list_bindings)
        if assigned:
            chips = Gtk.Box(spacing=3)
            chips.set_valign(Gtk.Align.CENTER)
            chip = Gtk.Label(label=shortcuts.combo_label(assigned[0]).replace(" + ", "+"))
            chip.add_css_class("launcher-shortcut-chip")
            chips.append(chip)
            if len(assigned) > 1:
                more = Gtk.Label(label=f"+{len(assigned) - 1}")
                more.add_css_class("launcher-shortcut-more")
                chips.append(more)
            chips.set_tooltip_text((_("Global shortcuts:") + "\n" if len(assigned) > 1 else _("Global shortcut:") + " ") + "\n".join(
                shortcuts.combo_label(c) + (f" → {shortcuts.action_name(app, c['action'])}" if c["action"] else "")
                for c in assigned))
            line.append(chips)
        actions = Gtk.Button(icon_name="open-menu-symbolic")
        actions.add_css_class("flat")
        actions.set_tooltip_text(_("Tray menu") if "tray_id" in app else
                                 _("Plugin actions") if "plugin_id" in app else _("App actions"))
        actions.connect("clicked", lambda button, target=app: self.show_actions(button, target))
        line.append(actions)
        row.set_child(line)
        row.actions_button = actions
        motion = Gtk.EventControllerMotion()
        def on_enter(_ctrl, _x, _y, target_row=row):
            if self.rows.get_selected_row() != target_row:
                self.select_row(target_row)
        motion.connect("enter", on_enter)
        row.add_controller(motion)
        if "tray_id" not in app:
            # Drag a row onto the dock to pin it there.
            drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            drag.connect("prepare", lambda *_a, a=app: Gdk.ContentProvider.new_for_value(
                DRAG_PREFIX + a["desktop_id"]))
            drag.connect("drag-begin", lambda source, _d, i=icon, a=app: self.on_tile_drag_begin(source, i, a))
            drag.connect("drag-end", lambda *_a: self.on_tile_drag_end())
            row.add_controller(drag)
        return row

    def grouping_running(self):
        return self.view == "list" and self.mode == "apps" and self.running_first

    def make_group_row(self, gid, count, collapsed, first):
        """A group's heading in the group view: click or Enter folds it, ☰
        (or Right) has rename, move, delete and new group."""
        row = Gtk.ListBoxRow()
        row.group = gid
        row.add_css_class("launcher-group-row")
        if not first:
            row.add_css_class("launcher-group-row-next")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("launcher-group-band")
        line = Gtk.Box(spacing=8)
        chevron = Gtk.Image.new_from_icon_name("pan-end-symbolic" if collapsed else "pan-down-symbolic")
        chevron.set_pixel_size(14)
        chevron.add_css_class("launcher-group-chevron")
        line.append(chevron)
        title = Gtk.Label(label=groups.display_name(gid, self.groups, self.mode), xalign=0)
        title.add_css_class("launcher-group-title")
        star = self.new_group_star(gid)
        if star is not None:
            line.append(star)
        line.append(title)
        number = Gtk.Label(label=str(count))
        number.add_css_class("launcher-group-count")
        number.set_valign(Gtk.Align.CENTER)
        line.append(number)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        line.append(spacer)
        row.actions_button = None
        # Running and the origin sections are fixed: nothing to rename or move.
        if gid != groups.RUNNING and gid not in groups.ORIGINS:
            menu = Gtk.Button(icon_name="open-menu-symbolic")
            menu.add_css_class("flat")
            menu.add_css_class("launcher-group-menu")
            menu.set_tooltip_text(_("Group actions"))
            menu.connect("clicked", lambda button, g=gid: self.show_group_actions(button, g))
            line.append(menu)
            row.actions_button = menu
        outer.append(line)
        row.set_child(outer)
        row.set_tooltip_text(_("Show this group (Enter, Right)") if collapsed else _("Fold this group (Enter, Left)"))
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a, target_row=row: self.rows.get_selected_row() != target_row
                       and self.select_row(target_row))
        row.add_controller(motion)
        return row

    def toggle_group(self, gid):
        groups.toggle_collapsed(self.groups, gid)
        self.save_groups()
        self.refresh_list(selected_id="group:" + gid)
        if gid not in self.groups["collapsed"]:
            # After the selection has scrolled to the heading (50 ms).
            GLib.timeout_add(80, self.reveal_group, gid)

    def reveal_group(self, gid):
        """Scroll so an unfolded group shows: as many of its entries as fit,
        the heading never above the top. A heading at the bottom of the
        list otherwise unfolds out of sight."""
        header = self.rows.get_first_child()
        while header is not None and getattr(header, "group", None) != gid:
            header = header.get_next_sibling()
        if header is None:
            return GLib.SOURCE_REMOVE
        last = header
        while (following := last.get_next_sibling()) is not None and not hasattr(following, "group"):
            last = following
        ok_top, top = header.compute_bounds(self.rows)
        ok_bottom, bottom = last.compute_bounds(self.rows)
        if not (ok_top and ok_bottom):
            return GLib.SOURCE_REMOVE
        adjustment = self.scroller.get_vadjustment()
        start, end = top.get_y(), bottom.get_y() + bottom.get_height()
        if end > adjustment.get_value() + adjustment.get_page_size():
            adjustment.set_value(max(0, min(start, end - adjustment.get_page_size())))
        return GLib.SOURCE_REMOVE

    def save_groups(self):
        try:
            groups.save(self.groups_all)
        except OSError as error:
            self.notify(_("Could not save the groups"), str(error))

    def list_header(self, row, before):
        """Section titles in the widget list: tray icons, then plugins. In the
        app list with the running toggle on: a "Running" group, closed by a rule.
        The group view draws its own heading rows."""
        if self.view != "list":
            row.set_header(None)
            return
        if self.grouping_running() and hasattr(row, "app"):
            if before is None and row.running:
                title = Gtk.Label(label=_("Running"), xalign=0)
                title.add_css_class("sc-section")
                title.set_margin_start(8)
                title.set_margin_bottom(2)
                row.set_header(title)
            elif before is not None and getattr(before, "running", False) and not row.running:
                rule = Gtk.Separator()
                rule.add_css_class("launcher-settings-rule")
                rule.set_margin_top(6)
                rule.set_margin_bottom(6)
                row.set_header(rule)
            else:
                row.set_header(None)
            return
        if self.mode != "plugins" or not hasattr(row, "app"):
            row.set_header(None)
            return
        in_tray = "tray_id" in row.app
        if before is not None and hasattr(before, "app") and ("tray_id" in before.app) == in_tray:
            row.set_header(None)
            return
        title = Gtk.Label(label=_("Running in the bar") if in_tray else _("Widgets & plugins"), xalign=0)
        title.add_css_class("sc-section")
        title.set_margin_start(8)
        title.set_margin_bottom(2)
        if before is not None:
            title.set_margin_top(8)
        row.set_header(title)

    @staticmethod
    def off_bar(app):
        """A bar widget taken out of the bar: listed only with hidden entries.
        The launcher itself always stays, for its own keys."""
        return bool(app.get("bar_widget")) and not app.get("in_bar") and not app.get("self_launcher")

    def ensure_selected_visible(self, row):
        if row.get_parent() == self.rows and self.rows.get_selected_row() == row:
            row.grab_focus()
            try:
                ok, bounds = row.compute_bounds(self.rows)
                if ok:
                    self.scroller.get_vadjustment().clamp_page(
                        bounds.get_y(), bounds.get_y() + bounds.get_height())
                else:
                    alloc = row.get_allocation()
                    self.scroller.get_vadjustment().clamp_page(
                        alloc.y, alloc.y + alloc.height)
            except Exception:
                alloc = row.get_allocation()
                self.scroller.get_vadjustment().clamp_page(
                    alloc.y, alloc.y + alloc.height)
        return GLib.SOURCE_REMOVE

    def activate_selected(self, *_args):
        row = self.rows.get_selected_row()
        if row:
            self.activate_row(self.rows, row)

    def activate_row(self, _list, row):
        if hasattr(row, "group"):
            self.toggle_group(row.group)
            return
        self.launch(row.app, row.actions_button)

    def launch(self, app, button=None):
        """Open an app, a shell plugin or a tray icon, and close the launcher."""
        if app.get("desktop_id") and newapps.opened(self.seen, app["desktop_id"]):
            self.save_seen()  # Opened once: no longer marked new.
        if "tray_id" in app:
            # Activate like a click on the icon; Steam's icon has no such action,
            # so open its app instead. A menu-only icon (NordVPN) shows its menu.
            if not app["tray_is_menu"] and tray.activate(app):
                self.hide()
            elif app.get("app"):
                launch_app(app["app"])
                self.hide()
            elif button is not None:
                self.show_actions(button, app)
            else:
                self.notify(_("{0} has no window to open").format(app['name']), _("Use its tray menu."))
            return
        if app.get("self_launcher"):
            self.open_shortcut_page(app)  # Already open: Enter sets its keys.
            return
        if "plugin_id" in app and not app.get("openable", True):
            if button is not None:
                self.show_actions(button, app)
            else:
                self.notify(_("{0} cannot be opened from here").format(app['name']),
                            _("Put it in the bar first.") if app.get("bar_widget") and not app.get("in_bar") else "")
            return
        if "plugin_id" not in app:
            launch_app(app)
            self.hide()
            return
        # Out of the way first: the plugin's panel takes the keyboard.
        self.hide()
        if not open_plugin(app):
            self.notify(_("Could not open {0}").format(app['name']), _("The shell has no panel to open for it."))

    def open_as_admin(self, app):
        """Open app as root: pkexec asks for the password, or sudo in a terminal."""
        process = launch_as_admin(app)
        self.hide()
        if process is None:
            self.notify(_("Could not open {0} as administrator").format(app['name']))
            return

        started = time.monotonic()

        def done(_pid, status):
            # pkexec exits 127 when not authorized or no polkit agent runs, 126
            # when the dialog is dismissed. An app that quits with an error soon
            # after the password most likely refuses to run as root.
            process.returncode = os.waitstatus_to_exitcode(status)  # Reaped here.
            if process.returncode == 127:
                self.notify(_("{0} was not opened as administrator").format(app['name']),
                            _("Not authorized, or no polkit agent is running."))
            elif process.returncode not in (0, 126) and time.monotonic() - started < 60:
                self.notify(_("{0} did not open as administrator").format(app['name']),
                            _("It quit right away (exit code {0}). Some apps refuse to run as root.").format(process.returncode))
        if process.args[0] == "pkexec":
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, done)

    def actions_menu_open(self):
        return self.actions_popover is not None and self.actions_popover.get_visible()

    def on_actions_closed(self, popover):
        if self.actions_popover is popover:
            self.actions_popover = None
        # Release the popup after GTK finishes dispatching the closing click.
        def cleanup():
            if popover.get_parent() is not None:
                popover.unparent()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(cleanup)

    def show_actions(self, button, app):
        if self.actions_menu_open():
            self.actions_popover.popdown()
        popover = Gtk.Popover()
        popover.add_css_class("launcher-popover")
        popover.set_parent(button)
        self.actions_popover = popover
        popover.connect("closed", self.on_actions_closed)
        # Right opens the menu, Left closes it again: on a menu item only, so
        # Left still moves between the buttons of the uninstall question.
        # Backspace closes it from anywhere in the menu.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda _c, key, _code, _state: self.close_actions_on_left(popover, key))
        popover.add_controller(keys)
        self.fill_actions(popover, app)
        popover.popup()

    def close_actions_on_left(self, popover, key):
        focus = self.window.get_focus()
        if key == Gdk.KEY_BackSpace and not isinstance(focus, Gtk.Editable):
            popover.popdown()
            return True
        if key not in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            return False
        if focus is None or not focus.has_css_class("launcher-menu-item"):
            return False
        popover.popdown()
        return True

    def fill_actions(self, popover, app, trail=()):
        """The actions menu; for a tray icon, trail is the open submenu path."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        assigned = self.app_shortcuts(app, self.list_bindings) if self.list_bindings else []

        def chip_for(action_id):
            combo = next((c for c in assigned if c["action"] == action_id), None)
            return shortcuts.combo_label(combo).replace(" + ", "+") if combo else None

        def add_action(label, callback, icon_name=None, is_destructive=False, shortcut=None, keep_open=False,
                       css=None, target=box, sensitive=True, end_icon=None):
            item = Gtk.Button()
            item.add_css_class("flat")
            item.add_css_class("launcher-menu-item")
            self.focus_follows_pointer(item)
            if is_destructive:
                item.add_css_class("destructive")
            if css:
                item.add_css_class(css)
            item_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            item_box.set_margin_start(8)
            item_box.set_margin_end(8)
            item_box.set_margin_top(6)
            item_box.set_margin_bottom(6)
            if icon_name:
                icon = Gtk.Image.new_from_icon_name(icon_name)
                icon.set_pixel_size(14)
                item_box.append(icon)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_hexpand(True)
            item_box.append(lbl)
            if shortcut:
                chip = Gtk.Label(label=shortcut)
                chip.add_css_class("launcher-shortcut-chip")
                chip.set_valign(Gtk.Align.CENTER)
                item_box.append(chip)
            if end_icon:
                item_box.append(Gtk.Image.new_from_icon_name(end_icon))
            item.set_child(item_box)
            item.set_sensitive(sensitive)
            item.connect("clicked", lambda *_: callback() if keep_open else (popover.popdown(), callback()))
            target.append(item)
            return item

        def add_separator(target=box):
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            separator.add_css_class("launcher-menu-separator")
            target.append(separator)

        if "tray_id" in app:
            first = self.fill_tray_items(popover, app, trail, box, add_action, add_separator, chip_for)
            popover.set_child(box)
            if trail:
                self.focus_first_entry(box, first)
                return  # A submenu shows only its entries.
        elif app.get("self_launcher"):
            for action, icon in (("apps", "view-app-grid-symbolic"), ("plugins", "application-x-addon-symbolic")):
                name = next(a["name"] for a in app["actions"] if a["id"] == action)
                add_action(name, lambda m=action: self.mode != m and self.toggle_mode(), icon,
                           shortcut=chip_for(action))
            add_separator()
        else:
            if app.get("openable", True):
                add_action(_("Open"), lambda: self.launch(app), "media-playback-start-symbolic",
                           shortcut=chip_for(""))
                if admin_action_enabled() and admin_command(app):
                    add_action(_("Open as administrator"), lambda: self.open_as_admin(app),
                               "dialog-password-symbolic")
            for action in app.get("actions", []):
                add_action(action["name"], lambda action_id=action["id"]:
                           (launch_action(app, action_id), self.hide()), "system-run-symbolic",
                           shortcut=chip_for(action["id"]))

        if app.get("plugin_id") and self.updates["plugins"].get(app["plugin_id"]):
            # Straight away, in the background: fast-forward only, validated, rolled back on failure.
            add_action(_("Update"), lambda: self.update_plugin(app), "software-update-available-symbolic")
        elif app.get("desktop_path") and self.updates["apps"].get(app["desktop_path"]):
            # A package is never updated on its own on Arch (partial upgrades break
            # things): Omarchy's system update, as from the bar's update icon.
            add_action(_("Update system…"), self.update_system, "software-update-available-symbolic")
        # A shortcut needs something to open: not a widget that is only a bar part.
        if app.get("self_launcher") or app.get("openable", True):
            if not app.get("self_launcher"):
                add_separator()
            add_action(_("Shortcut…"), lambda: self.open_shortcut_page(app),
                       "preferences-desktop-keyboard-shortcuts-symbolic")
            add_separator()
        if "tray_id" not in app:
            page = self.groups_all["plugins" if is_widget(app) else "apps"]
            add_action(_("Group: {0}").format(groups.display_name(groups.group_of(app, page), page)),
                       lambda: self.fill_group_choice(popover, app), "folder-symbolic",
                       keep_open=True, end_icon="go-next-symbolic")
            add_action(_("Details…"), lambda: self.open_details_page(app), "dialog-information-symbolic")
            add_action(_("Change icon…"), lambda: self.open_icon_page(app), "image-x-generic-symbolic")
            dock = dock_core.settings()
            if dock["dock_enabled"] and "plugin_id" not in app:
                if app["desktop_id"] in dock["dock_pinned"]:
                    add_action(_("Unpin from dock"), lambda: dock_core.unpin(app["desktop_id"]), "view-pin-symbolic")
                else:
                    add_action(_("Pin to dock"), lambda: dock_core.pin(app["desktop_id"]), "view-pin-symbolic")
            add_separator()
        hidden = app["desktop_id"] in self.hidden_by_mode["plugins" if is_widget(app) else "apps"]
        # Out of the bar, a widget is hidden by that already: Hide would do nothing.
        # Unhide stays for one hidden by hand, or it stays hidden once back in the bar.
        if hidden or not self.off_bar(app):
            add_action(_("Unhide from list") if hidden else _("Hide from list"),
                       lambda: self.set_hidden(app, not hidden),
                       "view-reveal-symbolic" if hidden else "view-conceal-symbolic")
        if app.get("bar_widget") and set_in_bar(app["plugin_id"], not app["in_bar"]):
            if app["in_bar"]:
                add_action(_("Remove from bar"), lambda: self.set_bar_placement(app, False), "list-remove-symbolic")
            else:
                # Where it goes is the user's choice: left, center or right.
                add_action(_("Add to bar"), lambda: self.fill_bar_sections(popover, app), "list-add-symbolic",
                           keep_open=True, end_icon="go-next-symbolic")
        state = self.app_state(app) if not is_widget(app) else "stopped"
        if state == "active":
            add_action(_("Close"), lambda: self.close_app(app), "window-close-symbolic", is_destructive=True)
        quit_app = self.quit_action(app, state) if state != "stopped" else None
        if quit_app:
            add_action(_("Quit"), quit_app, "application-exit-symbolic", is_destructive=True)
        # A tray icon uninstalls the app it belongs to (Steam's tray: Steam).
        owner = app.get("app", app) if "tray_id" in app else app
        if app.get("removable") or (owner.get("desktop_path") and shutil.which("omarchy-remove-launcher-entry")):
            add_separator()
            add_action(_("Uninstall…"), lambda: self.confirm_uninstall(owner), "user-trash-symbolic",
                       keep_open=True, css="uninstall")
        popover.set_child(box)
        self.focus_first_entry(box, first if "tray_id" in app else None)

    @staticmethod
    def focus_follows_pointer(item):
        """Moving the pointer onto a menu item focuses it. Menus highlight only
        the focused item, so what looks selected is what Enter runs; a pointer
        that merely rests on an item when a menu opens does not move the focus."""
        shown = time.monotonic()

        def follow(*_):
            # The pointer "enters" whatever item appears under it as the menu
            # opens; only a later enter or a move is the user pointing.
            if time.monotonic() - shown > 0.3 and item.get_sensitive() and not item.has_focus():
                item.grab_focus()

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", follow)
        motion.connect("motion", follow)
        item.add_controller(motion)

    @staticmethod
    def focus_first_entry(box, first=None):
        """Focus the menu's first usable entry once it is on screen, so every
        menu and submenu opens with the keyboard on its top item."""
        def find(widget):
            child = widget.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Button):
                    if child.get_sensitive() and child.get_visible():
                        return child
                elif (found := find(child)) is not None:
                    return found
                child = child.get_next_sibling()
            return None

        def focus():
            target = first if first is not None and first.get_sensitive() else find(box)
            if target is not None:
                target.grab_focus()
            return GLib.SOURCE_REMOVE
        focus()
        GLib.idle_add(focus)  # Again after popup(): a new popover moves the focus itself.

    def fill_tray_items(self, popover, app, trail, box, add_action, add_separator, chip_for):
        """A tray icon's own menu, one level at a time; long ones scroll.
        Returns the first entry, to focus it after a level change."""
        first = None
        if trail:
            first = add_action(_("Back"), lambda: self.fill_actions(popover, app, trail[:-1]),
                               "go-previous-symbolic", keep_open=True)
            title = Gtk.Label(label=" › ".join(node["label"] for node in trail), xalign=0)
            title.add_css_class("sc-section")
            title.set_margin_start(8)
            box.append(title)
            nodes = trail[-1]["children"]
        else:
            if not app["tray_is_menu"] or app.get("app"):
                first = add_action(_("Open"), lambda: self.launch(app), "media-playback-start-symbolic",
                                   shortcut=chip_for(""))
                add_separator()
            nodes = app["tray_menu"]
        entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(560)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(entries)
        box.append(scroller)
        # The app's own Quit stays in sight below the list, however long it is.
        quit_node = None if trail else tray.find_quit(nodes)
        for node in nodes:
            if node is quit_node:
                continue
            if node.get("separator"):
                add_separator(entries)
            elif node["children"]:
                item = add_action(node["label"], lambda n=node: self.fill_actions(popover, app, (*trail, n)),
                                  keep_open=True, target=entries, sensitive=node["enabled"],
                                  end_icon="go-next-symbolic")
                first = first or item
            else:
                icon = "object-select-symbolic" if node["toggle"] and node["checked"] else None
                item = add_action(node["label"], lambda n=node: self.click_tray_entry(app, n), icon,
                                  shortcut=chip_for(node["action"]), target=entries,
                                  sensitive=node["enabled"])
                first = first or (item if node["enabled"] else None)
        if not nodes:
            empty = Gtk.Label(label=_("No menu"), xalign=0)
            empty.add_css_class("sc-empty")
            entries.append(empty)
        # Separators left at the end (before the moved Quit) would double up.
        while (last := entries.get_last_child()) is not None and isinstance(last, Gtk.Separator):
            entries.remove(last)
        if quit_node:
            add_separator()
            add_action(quit_node["label"], lambda: self.click_tray_entry(app, quit_node),
                       "application-exit-symbolic", is_destructive=True, shortcut=chip_for(quit_node["action"]))
        return first

    def click_tray_entry(self, app, node):
        self.hide()
        if not tray.click(app, node["id"]):
            self.notify(_("{0}: “{1}” did not respond").format(app['name'], node['label']))

    def confirm_uninstall(self, app):
        """Omarchy's uninstall: the same question, then omarchy-remove-launcher-entry,
        which decides how (web app, TUI, own entry, pacman or Flatpak)."""
        popover = self.actions_popover
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(8)
        browser = None if "plugin_id" in app else browser_web_app(app)
        question = (_("How to uninstall {0}") if browser else _("Do you want to uninstall {0}?")).format(app['name'])
        message = Gtk.Label(label=question, xalign=0, wrap=True, max_width_chars=32)
        box.append(message)
        loses_work = False
        if "plugin_id" in app:
            note_text, loses_work = plugin_removal_note(app["plugin_id"])
            note = Gtk.Label(label=note_text, xalign=0, wrap=True, max_width_chars=32)
            note.add_css_class("sc-status")
            note.add_css_class("sc-danger" if loses_work else "sc-info")
            box.append(note)
        elif browser:
            # Only the browser can uninstall its web apps: deleting the menu
            # entry here would not, so there is no Uninstall button.
            note = Gtk.Label(label=_("{0} is a web app installed in {1}, so only {1} can uninstall it: open the app "
                                     "and choose ⋮ → Uninstall, or right-click it on {2} and choose Remove.").format(
                                   app['name'], browser, APPS_PAGES.get(browser, _("its apps page"))),
                             xalign=0, wrap=True, max_width_chars=32)
            note.add_css_class("sc-status")
            note.add_css_class("sc-info")
            box.append(note)
        buttons = Gtk.Box(spacing=6)
        buttons.set_halign(Gtk.Align.END)
        if browser:
            close = Gtk.Button(label=_("Close"))
            self.focus_follows_pointer(close)
            close.add_css_class("sc-small")
            close.connect("clicked", lambda *_: popover.popdown())
            buttons.append(close)
            box.append(buttons)
            popover.set_child(box)
            close.grab_focus()
            return
        cancel = Gtk.Button(label=_("Cancel"))
        self.focus_follows_pointer(cancel)
        cancel.add_css_class("sc-small")
        cancel.connect("clicked", lambda *_: popover.popdown())
        buttons.append(cancel)
        confirm = Gtk.Button(label=_("Uninstall"))
        self.focus_follows_pointer(confirm)
        confirm.add_css_class("sc-primary")
        confirm.add_css_class("sc-danger-btn")
        confirm.connect("clicked", lambda *_: (popover.popdown(), self.uninstall_plugin(app)
                                               if "plugin_id" in app else self.uninstall_app(app)))
        buttons.append(confirm)
        box.append(buttons)
        popover.set_child(box)
        # Enter confirms, Escape cancels: the terminal still asks for the password
        # and pacman for a yes. Deleting a plugin checkout with unsaved work asks
        # nothing more, so there Enter must not do it by accident.
        (cancel if loses_work else confirm).grab_focus()

    def uninstall_app(self, app):
        # Package and Flatpak removal open a terminal that needs the focus.
        self.hide()
        try:
            process = Gio.Subprocess.new(["omarchy-remove-launcher-entry", app["desktop_id"], app["name"]],
                                         Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as error:
            self.notify(f"Could not uninstall {app['name']}", error.message)
            return

        # A failure would otherwise pass silently: say why.
        def done(proc, result):
            try:
                _ok, output, _err = proc.communicate_utf8_finish(result)
            except GLib.Error as error:
                output = error.message
            if not proc.get_successful():
                lines = (output or "").strip().splitlines()
                self.notify(f"Could not uninstall {app['name']}", lines[-1] if lines else "")

        process.communicate_utf8_async(None, None, done)

    def menu_item(self, box, label, icon, callback, note=None, destructive=False, end_icon=None, sensitive=True):
        """One entry of a menu filled by hand (bar sections, groups)."""
        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("launcher-menu-item")
        if destructive:
            button.add_css_class("destructive")
        self.focus_follows_pointer(button)
        line = Gtk.Box(spacing=8)
        for side in ("start", "end"):
            getattr(line, f"set_margin_{side}")(8)
        line.set_margin_top(6)
        line.set_margin_bottom(6)
        image = Gtk.Image.new_from_icon_name(icon) if icon else Gtk.Image()
        image.set_pixel_size(14)
        line.append(image)
        text = Gtk.Label(label=label, xalign=0)
        text.set_hexpand(True)
        line.append(text)
        if note:
            tag = Gtk.Label(label=note)
            tag.add_css_class("launcher-shortcut-chip")
            line.append(tag)
        if end_icon:
            line.append(Gtk.Image.new_from_icon_name(end_icon))
        button.set_child(line)
        button.set_sensitive(sensitive)
        button.text_label = text
        button.connect("clicked", lambda *_: callback())
        box.append(button)
        return button

    @staticmethod
    def menu_box():
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(4)
        return box

    @staticmethod
    def menu_title(box, text):
        title = Gtk.Label(label=text, xalign=0)
        title.add_css_class("sc-section")
        title.set_margin_start(8)
        title.set_ellipsize(3)
        box.append(title)

    @staticmethod
    def menu_separator(box):
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.add_css_class("launcher-menu-separator")
        box.append(separator)

    def fill_group_choice(self, popover, app):
        """Second level of Group: the group this entry belongs in."""
        box = self.menu_box()
        self.menu_item(box, _("Back"), "go-previous-symbolic", lambda: self.fill_actions(popover, app))
        self.menu_title(box, _("Group for {0}").format(app["name"]))
        current = groups.group_of(app, self.groups)
        focus = None
        for gid in [g["id"] for g in self.groups["groups"]] + [groups.OTHER]:
            item = self.menu_item(box, groups.display_name(gid, self.groups),
                                  "object-select-symbolic" if gid == current else None,
                                  lambda g=gid: (popover.popdown(), self.put_in_group(app, g)))
            if gid == current:
                focus = item
        self.menu_separator(box)
        self.menu_item(box, _("New group…"), "list-add-symbolic",
                       lambda: (popover.popdown(), self.open_group_name_page(None, app)))
        popover.set_child(box)
        self.focus_first_entry(box, focus)

    def group_member_ids(self, gid):
        """The ids of group gid's entries in the order they show."""
        entries = self.plugins if self.mode == "plugins" else self.apps
        members = dict(groups.sections(entries, self.groups)).get(gid, [])
        return [a["desktop_id"] for a in members]

    def put_in_group(self, app, gid, target_id=None, after=False):
        """Into group gid, at the end or next to target_id (dropped on it)."""
        try:
            groups.place(self.groups, app["desktop_id"], gid, target_id, self.group_member_ids(gid), after)
        except ValueError:
            return
        self.save_groups()
        self.refresh_list(selected_id=app["desktop_id"])

    def show_group_actions(self, button, gid):
        if self.actions_menu_open():
            self.actions_popover.popdown()
        popover = Gtk.Popover()
        popover.add_css_class("launcher-popover")
        popover.set_parent(button)
        self.actions_popover = popover
        popover.connect("closed", self.on_actions_closed)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda _c, key, _code, _state: self.close_actions_on_left(popover, key))
        popover.add_controller(keys)
        box = self.menu_box()
        ids = [g["id"] for g in self.groups["groups"]]
        if gid in ids:
            index = ids.index(gid)
            name = groups.display_name(gid, self.groups)
            self.menu_item(box, _("Rename…"), "document-edit-symbolic",
                           lambda: (popover.popdown(), self.open_group_name_page(gid)))
            self.menu_item(box, _("Move up"), "go-up-symbolic", lambda: (popover.popdown(), self.move_group(gid, -1)),
                           sensitive=index > 0)
            self.menu_item(box, _("Move down"), "go-down-symbolic", lambda: (popover.popdown(), self.move_group(gid, 1)),
                           sensitive=index < len(ids) - 1)
            # Asks in place: the first click turns the entry into the question.
            delete = None

            def ask_or_delete():
                if getattr(delete, "asked", False):
                    popover.popdown()
                    groups.delete_group(self.groups, gid)
                    self.save_groups()
                    self.refresh_list()
                else:
                    delete.asked = True
                    delete.text_label.set_label(_("Really delete “{0}”? Its entries move back.").format(name))
            delete = self.menu_item(box, _("Delete group"), "user-trash-symbolic", ask_or_delete, destructive=True)
            self.menu_separator(box)
        self.menu_item(box, _("New group…"), "list-add-symbolic",
                       lambda: (popover.popdown(), self.open_group_name_page(None)))
        popover.set_child(box)
        self.focus_first_entry(box)
        popover.popup()

    def move_group(self, gid, delta):
        if groups.move_group(self.groups, gid, delta):
            self.save_groups()
            self.refresh_list(selected_id="group:" + gid)

    def fill_bar_sections(self, popover, app):
        """Second level of Add to bar: the section to put the widget in."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(4)

        def item(label, icon, callback, note=None):
            button = Gtk.Button()
            button.add_css_class("flat")
            button.add_css_class("launcher-menu-item")
            self.focus_follows_pointer(button)
            line = Gtk.Box(spacing=8)
            for side in ("start", "end"):
                getattr(line, f"set_margin_{side}")(8)
            line.set_margin_top(6)
            line.set_margin_bottom(6)
            image = Gtk.Image.new_from_icon_name(icon)
            image.set_pixel_size(14)
            line.append(image)
            text = Gtk.Label(label=label, xalign=0)
            text.set_hexpand(True)
            line.append(text)
            if note:
                tag = Gtk.Label(label=note)
                tag.add_css_class("launcher-shortcut-chip")
                line.append(tag)
            button.set_child(line)
            button.connect("clicked", lambda *_: callback())
            box.append(button)
            return button

        item(_("Back"), "go-previous-symbolic", lambda: self.fill_actions(popover, app))
        title = Gtk.Label(label=_("Add {0} to the bar").format(app['name']), xalign=0)
        title.add_css_class("sc-section")
        title.set_margin_start(8)
        box.append(title)
        default = app.get("bar_section") if app.get("bar_section") in BAR_SECTIONS else None
        buttons = {section: item({"left": _("Left"), "center": _("Center"), "right": _("Right")}.get(section, section.capitalize()),
                                 f"format-justify-{section}-symbolic",
                                 lambda s=section: (popover.popdown(), self.set_bar_placement(app, True, s)),
                                 "default" if section == default else None)
                   for section in BAR_SECTIONS}
        popover.set_child(box)
        buttons[default or "left"].grab_focus()

    def set_bar_placement(self, plugin, placed, section=None):
        """Put a bar widget into a bar section or take it out (omarchy plugin
        enable/disable). Runs in the background; the list follows."""
        argv = set_in_bar(plugin["plugin_id"], placed, section)
        if argv is None:
            return
        try:
            process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as error:
            self.notify(_("Could not change the bar for {0}").format(plugin['name']), error.message)
            return

        # Taken out of the bar, the row leaves the list: keep the place there.
        selected_id = plugin["desktop_id"]
        if not placed and not self.show_hidden:
            row = self.item_view().get_first_child()
            while row is not None:
                if getattr(row, "app", None) is plugin:
                    neighbour = row.get_next_sibling() or row.get_prev_sibling()
                    if neighbour is not None and hasattr(neighbour, "app"):
                        selected_id = neighbour.app["desktop_id"]
                    break
                row = row.get_next_sibling()

        def done(proc, result):
            try:
                _ok, output, _err = proc.communicate_utf8_finish(result)
            except GLib.Error as error:
                output = error.message
            if not proc.get_successful():
                lines = (output or "").strip().splitlines()
                self.notify(f"Could not change the bar for {plugin['name']}", lines[-1] if lines else "")
            if self.mode == "plugins" and self.window.get_visible() and self.shortcut_app is None:
                self.load_plugins()
                self.refresh_list(selected_id=selected_id)

        process.communicate_utf8_async(None, None, done)

    def uninstall_plugin(self, plugin):
        """omarchy plugin remove: disables it, unloads it from the shell and
        removes its folder. Runs in the background; the list follows."""
        plugin_id = plugin["plugin_id"]
        try:
            process = Gio.Subprocess.new(["omarchy-plugin-remove", plugin_id, "--yes"],
                                         Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as error:
            self.notify(f"Could not uninstall {plugin['name']}", error.message)
            return

        def done(proc, result):
            try:
                _ok, output, _err = proc.communicate_utf8_finish(result)
            except GLib.Error as error:
                output = error.message
            lines = (output or "").strip().splitlines()
            if proc.get_successful():
                self.hidden_by_mode["plugins"].discard(plugin["desktop_id"])
                save_hidden_apps(self.hidden_by_mode["plugins"], HIDDEN_PLUGINS_FILE)
                self.prune_uninstalled_shortcuts()
                self.notify(f"Uninstalled {plugin['name']}", lines[0] if lines else "")
            else:
                self.notify(f"Could not uninstall {plugin['name']}", lines[-1] if lines else "")
            if self.mode == "plugins" and self.window.get_visible() and self.shortcut_app is None:
                self.load_plugins()
                self.refresh_list()

        process.communicate_utf8_async(None, None, done)

    def notify(self, title, body=""):
        # Names come from apps, plugins and tray menus; notification daemons
        # render markup, so none of it may reach them as markup.
        try:
            subprocess.Popen(["notify-send", "-a", "App Manager", plain_text(title), plain_text(body)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    # --- shortcut page --------------------------------------------------------

    def build_shortcut_page(self):
        page = Gtk.ScrolledWindow()
        page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # Same 8px inset as the app rows, so both pages share their edge gaps.
        box.set_margin_start(8)
        box.set_margin_end(8)
        page.set_child(box)

        def section(text):
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("sc-section")
            box.append(label)
            return label

        # 1. What this app has now.
        current_title = section(_("Shortcuts"))
        current = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(current)

        divider = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        divider.add_css_class("sc-divider")
        box.append(divider)

        # 2. Adding a new one: modifiers, key, verdict, free keys, button.
        add_header = Gtk.Box(spacing=6)
        add_title = Gtk.Label(label=_("Add a shortcut"), xalign=0)
        add_title.add_css_class("sc-section")
        add_title.set_hexpand(True)
        add_header.append(add_title)
        add_cancel = Gtk.Button(label=_("Cancel"))
        add_cancel.add_css_class("sc-small")
        add_cancel.add_css_class("flat")
        add_cancel.set_tooltip_text(_("Keep the current key"))
        add_cancel.set_visible(False)
        add_cancel.connect("clicked", lambda *_: self.cancel_change_shortcut())
        add_header.append(add_cancel)
        box.append(add_header)

        target_row = Gtk.Box(spacing=8)
        target_label = Gtk.Label(label=_("Opens"), xalign=0)
        target_label.add_css_class("sc-note")
        target_row.append(target_label)
        target = Gtk.DropDown.new_from_strings([""])
        target.set_hexpand(True)
        target.set_tooltip_text(_("Open the app, or one of its own actions such as a private window"))
        target.connect("notify::selected", lambda *_: self.on_shortcut_target_changed())
        target_row.append(target)
        box.append(target_row)
        mods_row = Gtk.Box(spacing=6)
        toggles = {}
        for mod in shortcuts.MOD_ORDER:
            toggle = Gtk.ToggleButton(label=shortcuts.MOD_LABELS[mod])
            toggle.add_css_class("sc-mod")
            toggle.set_hexpand(True)
            toggle.connect("toggled", self.on_shortcut_mod_toggled)
            mods_row.append(toggle)
            toggles[mod] = toggle
        box.append(mods_row)

        default_check = Gtk.CheckButton(label=_("Default for apps without a shortcut"))
        default_check.add_css_class("sc-default")
        default_check.connect("toggled", self.on_shortcut_default_toggled)
        box.append(default_check)

        two_step = Gtk.CheckButton(label=_("Two steps — then press another key (e.g. Super + Alt + Space, N)"))
        two_step.add_css_class("sc-default")
        two_step.set_tooltip_text(_("Press the first combo, let go, then a single key. Several shortcuts "
                                    "can share the first combo, each with its own second key."))
        two_step.connect("toggled", self.on_two_step_toggled)
        box.append(two_step)

        key_box = Gtk.Box(spacing=6)
        key_box.add_css_class("sc-key")
        key_box.set_halign(Gtk.Align.FILL)
        key_box.set_tooltip_text(_("Press a key now. Hold modifiers while pressing, or toggle them above."))
        key_box.set_hexpand(True)
        # Assign sits beside the key it applies, to save a row.
        key_row = Gtk.Box(spacing=8)
        key_row.append(key_box)
        apply = Gtk.Button(label=_("Assign"))
        apply.add_css_class("sc-primary")
        apply.set_valign(Gtk.Align.CENTER)
        apply.connect("clicked", lambda *_: self.apply_shortcut())
        key_row.append(apply)
        box.append(key_row)

        status = Gtk.Label(xalign=0, wrap=True)
        status.add_css_class("sc-status")
        box.append(status)

        suggest_title = section(_("Free keys"))
        legend = Gtk.Box(spacing=10)
        for text, css in (("● " + _("free"), "sc-legend-free"), ("● " + _("yours"), "sc-legend-yours"),
                          ("● " + _("Omarchy default"), "sc-legend-omarchy")):
            item = Gtk.Label(label=text)
            item.add_css_class("sc-legend")
            item.add_css_class(css)
            legend.append(item)
        box.append(legend)
        suggest = Gtk.FlowBox()
        suggest.set_selection_mode(Gtk.SelectionMode.NONE)
        suggest.set_max_children_per_line(13)
        suggest.set_min_children_per_line(9)
        suggest.set_column_spacing(4)
        suggest.set_row_spacing(4)
        suggest.set_homogeneous(False)
        box.append(suggest)

        # Keys that are not letters, used with the selected modifiers. Clicking
        # also helps when Hyprland grabs a combo before the launcher sees it.
        section(_("Other keys"))
        others = Gtk.FlowBox()
        others.set_selection_mode(Gtk.SelectionMode.NONE)
        others.set_max_children_per_line(6)
        others.set_min_children_per_line(3)
        others.set_column_spacing(4)
        others.set_row_spacing(4)
        other_buttons = {}
        for key in OTHER_KEYS:
            button = Gtk.Button(label=shortcuts.combo_label({"mods": [], "key": key}))
            button.add_css_class("sc-other")
            button.connect("clicked", lambda *_, k=key: self.pick_then(k) if self.picking_then()
                           else self.pick_shortcut_key(self.selected_shortcut_mods(), k))
            others.append(button)
            other_buttons[key] = button
        box.append(others)

        self.stack.add_named(page, "shortcut")
        self.sc_page, self.sc_current, self.sc_current_title = page, current, current_title
        self.sc_toggles, self.sc_key_box, self.sc_status = toggles, key_box, status
        self.sc_suggest, self.sc_suggest_title, self.sc_apply_button = suggest, suggest_title, apply
        self.sc_default_check = default_check
        self.sc_two_step, self.sc_then = two_step, None
        self.sc_pressed_at = 0
        self.sc_add_title, self.sc_add_cancel = add_title, add_cancel
        self.sc_target, self.sc_target_row, self.sc_target_ids = target, target_row, [""]
        self.sc_other_buttons = other_buttons
        self.sc_legend = legend
        self.sc_editing = None
        self.sc_key, self.sc_keycode, self.sc_syncing = None, None, False
        self.sc_suggestions = []

    def keycaps(self, combo, large=False, dim=False):
        """A row of keycap labels: [Super] [Alt] [B]."""
        caps = Gtk.Box(spacing=4)
        caps.set_valign(Gtk.Align.CENTER)
        parts = shortcuts.combo_label(shortcuts.leader(combo)).split(" + ") if combo else []
        if combo and combo.get("then"):
            parts += [None, shortcuts.key_label(combo["then"])]
        for part in parts:
            if part is None:
                then = Gtk.Label(label=_("then"))
                then.add_css_class("sc-note")
                caps.append(then)
                continue
            cap = Gtk.Label(label=part)
            cap.add_css_class("sc-cap")
            if large:
                cap.add_css_class("sc-cap-lg")
            if dim:
                cap.add_css_class("sc-cap-dim")
            caps.append(cap)
        return caps

    def build_settings_page(self):
        """Settings, laid out like the other plugins' settings pages: a rule
        under the header, then label | control rows. Switching the language
        restarts the popup (same process id) so every label is rebuilt."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_start(4)
        page.set_margin_end(4)
        page.set_size_request(-1, LIST_HEIGHT)
        self.height_pages.append(page)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("launcher-settings-rule")
        page.append(rule)
        grid = Gtk.Grid(column_spacing=10, row_spacing=10)

        def label(text, tooltip=None):
            widget = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
            widget.add_css_class("launcher-setting-label")
            widget.set_size_request(165, -1)  # Long names (translations) wrap instead of widening the card.
            if tooltip:
                widget.set_tooltip_text(tooltip)
            return widget

        self.language_codes = ["auto"] + [code for code, _name in i18n.LANGUAGES]
        names = [_("System ({0})").format(i18n.language_name(i18n.system_language()))] + [name for _code, name in i18n.LANGUAGES]
        dropdown = Gtk.DropDown.new_from_strings(names)
        dropdown.add_css_class("launcher-compact-dropdown")
        dropdown.set_halign(Gtk.Align.START)
        dropdown.set_valign(Gtk.Align.CENTER)
        dropdown.set_tooltip_text(_("The launcher restarts in the new language."))
        setting = i18n.language_setting()
        dropdown.set_selected(self.language_codes.index(setting) if setting in self.language_codes else 0)
        dropdown.connect("notify::selected", self.on_language_selected)
        grid.attach(label(_("Language"), _("The launcher restarts in the new language.")), 0, 0, 1, 1)
        grid.attach(dropdown, 1, 0, 1, 1)

        admin_tip = _("Adds “Open as administrator” to an app's menu. Switching it on asks for your password; it is off again after you log in next time.")
        admin_switch = Gtk.Switch(active=admin_action_enabled(), halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        admin_switch.add_css_class("launcher-compact-switch")
        admin_switch.set_tooltip_text(admin_tip)
        admin_switch.connect("notify::active", self.on_admin_action_toggled)
        grid.attach(label(_("Open as administrator"), admin_tip), 0, 1, 1, 1)
        grid.attach(admin_switch, 1, 1, 1, 1)
        layout_tip = _("How the popup shows the entries once it is resized to two columns or more: each group as a folder of icons (the default), or every entry as a card.")
        layout_dropdown = Gtk.DropDown.new_from_strings([_("Folders"), _("Cards")])
        layout_dropdown.add_css_class("launcher-compact-dropdown")
        layout_dropdown.set_halign(Gtk.Align.START)
        layout_dropdown.set_valign(Gtk.Align.CENTER)
        layout_dropdown.set_tooltip_text(layout_tip)
        layout_dropdown.set_selected(0 if self.wide_layout == "folders" else 1)
        layout_dropdown.connect("notify::selected", lambda d, _p: getattr(self, "layout_syncing", False)
                                or self.set_wide_layout("folders" if d.get_selected() == 0 else "cards"))
        grid.attach(label(_("Layout when wide"), layout_tip), 0, 3, 1, 1)  # After the small one.
        grid.attach(layout_dropdown, 1, 3, 1, 1)
        self.layout_dropdown = layout_dropdown
        small_tip = _("How the small popup shows the entries: as one list (the default), in groups, or by origin.")
        self.small_views = ("list", "groups", "origin")
        small_dropdown = Gtk.DropDown.new_from_strings([_("List"), _("Groups"), _("By origin")])
        small_dropdown.add_css_class("launcher-compact-dropdown")
        small_dropdown.set_halign(Gtk.Align.START)
        small_dropdown.set_valign(Gtk.Align.CENTER)
        small_dropdown.set_tooltip_text(small_tip)
        small_dropdown.set_selected(self.small_views.index(self.view))
        small_dropdown.connect("notify::selected", lambda d, _p: getattr(self, "view_syncing", False)
                               or self.toggle_view(self.small_views[d.get_selected()]))
        grid.attach(label(_("Layout when small"), small_tip), 0, 2, 1, 1)
        grid.attach(small_dropdown, 1, 2, 1, 1)
        self.small_dropdown = small_dropdown
        views_tip = _("Buttons in the small popup's header to switch between groups, list and origin (Ctrl+G does the same).")
        views_switch = Gtk.Switch(active=self.view_buttons_on, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        views_switch.add_css_class("launcher-compact-switch")
        views_switch.set_tooltip_text(views_tip)
        views_switch.connect("notify::active", lambda sw, _p: self.set_view_buttons(sw.get_active()))
        grid.attach(label(_("View buttons when small"), views_tip), 0, 4, 1, 1)
        grid.attach(views_switch, 1, 4, 1, 1)
        self.views_switch = views_switch
        self.corner_choices = corners.CHOICES
        corners_dropdown = Gtk.DropDown.new_from_strings(self.corner_labels())
        corners_dropdown.add_css_class("launcher-compact-dropdown")
        corners_dropdown.set_halign(Gtk.Align.START)
        corners_dropdown.set_valign(Gtk.Align.CENTER)
        corners_dropdown.set_tooltip_text(self.corners_tip())
        corners_dropdown.set_selected(self.corner_choices.index(saved_corners()))
        corners_dropdown.connect("notify::selected", lambda d, _p: getattr(self, "corners_syncing", False)
                                 or self.set_corners(self.corner_choices[d.get_selected()]))
        self.corners_label = label(_("Corners"), self.corners_tip())
        grid.attach(self.corners_label, 0, 5, 1, 1)
        grid.attach(corners_dropdown, 1, 5, 1, 1)
        self.corners_dropdown = corners_dropdown
        # What Layout and View buttons do, animated.
        layout_sketch = previews.LayoutSketch(lambda: self.theme.colors,
                                              lambda: (self.wide_layout, self.view_buttons_on,
                                                       getattr(self, "corner_radius", 0)))
        layout_sketch.set_tooltip_text(_("Preview: the small list, then resized wide."))
        dock_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        self.build_dock_settings(dock_grid, label, 0)
        # Each group of options in a card, its animated preview to the right
        # of the card (wide popup) or a hint under it (small popup).
        sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sections.append(self.settings_section(grid, layout_sketch, self.reset_general_settings))
        sections.append(self.dock_heading)
        sections.append(self.settings_section(dock_grid, self.dock_sketch, self.reset_dock_settings))
        # The options grew taller than the popup: they scroll.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        sections.set_margin_end(6)
        scroller.set_child(sections)
        page.append(scroller)
        self.admin_switch = admin_switch
        self.language_dropdown = dropdown
        self.stack.add_named(page, "settings")

    def build_dock_settings(self, grid, label, row):
        """The DOCK group: on/off, edge, auto-hide, length, corners."""
        heading = Gtk.Label(label=_("Dock").upper(), xalign=0)
        heading.add_css_class("launcher-section-title")
        heading.set_margin_top(8)
        self.dock_heading = heading  # Placed above the dock card by the caller.
        dock = dock_core.settings()
        self.dock_cache, self.bar_cache = dock, dock_core.bar_position()
        self.dock_controls = []
        self.dock_switches = {}

        def switch(key, text, tip, line):
            widget = Gtk.Switch(active=dock[key], halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
            widget.add_css_class("launcher-compact-switch")
            widget.set_tooltip_text(tip)
            widget.connect("notify::active", lambda sw, _p: self.set_dock_setting(key, sw.get_active()))
            grid.attach(label(text, tip), 0, line, 1, 1)
            grid.attach(widget, 1, line, 1, 1)
            self.dock_switches[key] = widget
            return widget

        show_tip = _("A dock with your pinned apps and the running ones along a screen edge. Pin apps with “Pin to dock” in their menu.")
        switch("dock_enabled", _("Show dock"), show_tip, row + 1)
        position_tip = _("The screen edge the dock sits on. The bar's edge is left out: the dock never covers the bar.")
        positions = Gtk.DropDown.new_from_strings([""])
        positions.add_css_class("launcher-compact-dropdown")
        positions.set_halign(Gtk.Align.START)
        positions.set_valign(Gtk.Align.CENTER)
        positions.set_tooltip_text(position_tip)
        positions.connect("notify::selected", lambda d, _p: getattr(self, "dock_syncing", False) or self.set_dock_setting(
            "dock_position", self.dock_position_choices[d.get_selected()]))
        grid.attach(label(_("Position"), position_tip), 0, row + 2, 1, 1)
        grid.attach(positions, 1, row + 2, 1, 1)
        align_tip = _("Where the icons sit in a full-length dock. A compact dock is always centred.")
        aligns = Gtk.DropDown.new_from_strings([""])
        aligns.add_css_class("launcher-compact-dropdown")
        aligns.set_halign(Gtk.Align.START)
        aligns.set_valign(Gtk.Align.CENTER)
        aligns.set_tooltip_text(align_tip)
        aligns.connect("notify::selected", lambda d, _p: getattr(self, "dock_syncing", False) or self.set_dock_setting(
            "dock_align", dock_core.ALIGNS[d.get_selected()]))
        grid.attach(label(_("Icons"), align_tip), 0, row + 7, 1, 1)
        grid.attach(aligns, 1, row + 7, 1, 1)
        self.dock_aligns = aligns

        def choice(key, text, tip, values, names, line):
            widget = Gtk.DropDown.new_from_strings(names)
            widget.add_css_class("launcher-compact-dropdown")
            widget.set_halign(Gtk.Align.START)
            widget.set_valign(Gtk.Align.CENTER)
            widget.set_tooltip_text(tip)
            widget.set_selected(values.index(dock[key]))
            widget.connect("notify::selected", lambda d, _p: getattr(self, "dock_syncing", False)
                           or self.set_dock_setting(key, values[d.get_selected()]))
            grid.attach(label(text, tip), 0, line, 1, 1)
            grid.attach(widget, 1, line, 1, 1)
            self.dock_choices[key] = (widget, values)

        self.dock_choices = {}
        px = lambda values: [f"{v} px" for v in values]
        choice("dock_height", _("Dock height"), _("How high the dock's background is."),
               dock_core.HEIGHTS, px(dock_core.HEIGHTS), row + 8)
        choice("dock_icon_size", _("Icon size"),
               _("Icons larger than the dock stick out of it, as on macOS."),
               dock_core.ICON_SIZES, px(dock_core.ICON_SIZES), row + 9)
        choice("dock_spacing", _("Icon spacing"), _("The gap between two icons in the dock."),
               dock_core.SPACINGS, px(dock_core.SPACINGS), row + 10)
        choice("dock_magnify", _("Grow on hover"),
               _("The icon under the pointer grows, its neighbours halfway, as on macOS."),
               dock_core.MAGNIFY, [_("Off")] + [f"{v} %" for v in dock_core.MAGNIFY[1:]], row + 11)
        switch("dock_bounce", _("Bounce on alerts"),
               _("An app that wants attention bounces in the dock until you switch to it."), row + 12)
        switch("dock_indicator_shown", _("Show indicator"),
               _("Mark running apps in the dock (style and colour below)."), row + 13)
        choice("dock_indicator", _("Indicator"),
               _("How running apps are marked: a dot or a line next to the icon. The focused app's mark is larger."),
               dock_core.INDICATORS, [_("Dot"), _("Line")], row + 14)  # Line: the default.
        choice("dock_indicator_color", _("Indicator colour"),
               _("The colour of the running marks; Theme uses the theme's accent colour."),
               dock_core.INDICATOR_COLORS, [_("Theme"), _("Light"), _("Red"), _("Yellow"), _("Green"), _("Orange")],
               row + 15)
        self.dock_positions = positions
        self.dock_controls += [
            positions,
            switch("dock_autohide", _("Auto-hide"),
                   _("Hidden until the pointer touches the screen edge. Off: the dock keeps its space and windows stay clear of it."),
                   row + 3),
            switch("dock_full_width", _("Full length"),
                   _("Along the whole screen edge; off: only as long as its icons."), row + 4),
            switch("dock_rounded", _("Rounded corners"), _("Round corners, like the windows."), row + 5),
            switch("dock_transparent", _("Transparent"),
                   _("No background, only the icons. Double-click the dock's background to switch, as on the bar."),
                   row + 6),
        ]
        # What the dock options do, animated, right beneath its options.
        sketch = previews.DockSketch(lambda: self.theme.colors, lambda: self.dock_cache, lambda: self.bar_cache,
                                     lambda: (getattr(self, "window_rounding", None) or 0,
                                              getattr(self, "dock_corner_radius", corners.ROUNDED)))
        sketch.set_tooltip_text(_("Preview: the dock as set, with the bar and two windows."))
        self.dock_sketch = sketch
        self.sync_dock_settings()


    def settings_section(self, grid, sketch, reset=None):
        """[card with the options | animated preview], and the hint about the
        preview under the card while the popup is small."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("launcher-settings-card")
        card.set_size_request(SETTINGS_CARD_WIDTH, -1)  # Both cards alike.
        card.set_halign(Gtk.Align.START)
        card.set_valign(Gtk.Align.FILL)
        card.append(grid)
        if reset is not None:
            button = Gtk.Button(label=_("Reset to defaults"))
            button.add_css_class("sc-small")
            button.set_halign(Gtk.Align.END)
            button.set_margin_top(12)
            button.connect("clicked", lambda *_a: reset())
            card.append(button)
        hint = Gtk.Label(label=_("Make the popup wider to see an animated preview of these options."),
                         xalign=0, wrap=True, max_width_chars=40)
        hint.add_css_class("launcher-settings-hint")
        hint.set_margin_top(10)
        card.append(hint)
        # The preview in a card of its own, as wide and as high as the options card.
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.add_css_class("launcher-settings-card")
        frame.set_size_request(SETTINGS_CARD_WIDTH, -1)
        frame.set_valign(Gtk.Align.FILL)
        sketch.set_halign(Gtk.Align.CENTER)
        sketch.set_valign(Gtk.Align.CENTER)
        sketch.set_vexpand(True)
        frame.append(sketch)
        frame.set_vexpand(False)  # The sketch fills its card, not the whole page.
        row = Gtk.Box(spacing=16)
        row.set_vexpand(False)
        row.set_halign(Gtk.Align.START)
        row.append(card)
        row.append(frame)
        self.preview_slots = getattr(self, "preview_slots", []) + [(frame, hint)]
        self.sync_previews()
        return row

    def reset_general_settings(self):
        """Language: the system's; Open as administrator: off; layouts:
        folders when wide, the list when small; corners like the windows. A
        new language restarts the popup (on this page), so it comes last."""
        if self.admin_switch.get_active():
            self.admin_switch.set_active(False)  # Its handler switches the action off properly.
        self.set_wide_layout("folders")
        self.toggle_view("list")
        self.corners_dropdown.set_selected(0)  # "Like windows": saves and redraws.
        if i18n.language_setting() != "auto":
            self.language_dropdown.set_selected(0)  # "System (…)": saves and restarts.

    def reset_dock_settings(self):
        """Back to the dock defaults (off); the pinned apps stay."""
        try:
            dock_core.reset()
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))
            return
        self.dock_cache = dock_core.settings()
        self.sync_dock_settings()
        self.resize_to(self.card_w, self.list_h)
        self.move_to(self.position)
        self.update_input_region()
        self.tell_dock(True)

    def sync_previews(self):
        wide = getattr(self, "overview", False)
        for sketch, hint in getattr(self, "preview_slots", []):
            sketch.set_visible(wide)
            hint.set_visible(not wide)

    def sync_dock_settings(self):
        """The dock changes some settings itself (a double-click on it, Pin):
        show the saved values whenever the settings page opens."""
        dock = dock_core.settings()
        self.dock_cache, self.bar_cache = dock, dock_core.bar_position()
        for key, widget in getattr(self, "dock_switches", {}).items():
            if widget.get_active() != dock[key]:
                widget.set_active(dock[key])
        self.dock_syncing = True
        for key, (widget, values) in getattr(self, "dock_choices", {}).items():
            if widget.get_selected() != values.index(dock[key]):
                widget.set_selected(values.index(dock[key]))
        self.dock_syncing = False
        positions = getattr(self, "dock_positions", None)
        if positions is None:
            return
        # The edges the dock can use: all but the bar's one.
        bar = dock_core.bar_position()
        edge = dock_core.edge(dock, bar)
        names = {"bottom": _("Bottom"), "top": _("Top"), "left": _("Left"), "right": _("Right")}
        self.dock_syncing = True
        choices = [p for p in dock_core.POSITIONS if p != bar]
        # New lists only when their entries change (the bar moved, the edge
        # turned from horizontal to vertical): a new model resets the selection.
        if choices != getattr(self, "dock_position_choices", None):
            self.dock_position_choices = choices
            positions.set_model(Gtk.StringList.new([names[p] for p in choices]))
        if positions.get_selected() != choices.index(edge):
            positions.set_selected(choices.index(edge))
        ends = (_("Top"), _("Bottom")) if edge in ("left", "right") else (_("Left"), _("Right"))
        align_names = [ends[0], _("Center"), ends[1]]
        if align_names != getattr(self, "dock_align_names", None):
            self.dock_align_names = align_names
            self.dock_aligns.set_model(Gtk.StringList.new(align_names))
        if self.dock_aligns.get_selected() != dock_core.ALIGNS.index(dock["dock_align"]):
            self.dock_aligns.set_selected(dock_core.ALIGNS.index(dock["dock_align"]))
        self.dock_syncing = False

    def set_dock_setting(self, key, value):
        # Unchanged (a dropdown re-reporting its own refill): nothing to do.
        # This also breaks any notify → save → refill → notify cycle.
        if getattr(self, "dock_cache", {}).get(key) == value:
            return
        try:
            dock_core.set_setting(key, value)
        except (OSError, KeyError, ValueError) as error:
            self.notify(_("Could not save the setting"), str(error))
            return
        self.dock_cache = dock_core.settings()  # The preview follows at once.
        if key == "dock_enabled" and value:
            self.start_dock()
        if key == "dock_position":
            # The alignment names follow the edge (left/right or top/bottom).
            # Later, never inside the dropdown's own change handler.
            GLib.idle_add(lambda: self.sync_dock_settings() and False)
        if key in ("dock_enabled", "dock_autohide", "dock_position", "dock_height", "dock_icon_size",
                   "dock_magnify", "dock_bounce"):  # (Indicator settings only change how the dock looks.)
            # The card makes room for the dock (or takes it back) at once.
            self.resize_to(self.card_w, self.list_h)
            self.move_to(self.position)
            self.update_input_region()
            self.tell_dock(True)
            # Off: the dock sees the setting and quits by itself.

    def update_input_region(self):
        """Leave the dock's own rectangle out of this full-screen popup's
        input, so clicks and drags reach the dock under it. Where the dock is
        comes from hyprctl, asked on a thread: a slow answer must never stall
        this popup (its transparent layer would block the whole desktop)."""
        if self.window is None or getattr(self, "dock_lookup_running", False):
            return GLib.SOURCE_REMOVE
        if not self.dock_space():
            self.dock_screen_rect = None
            self.apply_input_region(None)
            return GLib.SOURCE_REMOVE
        self.dock_lookup_running = True

        def work():
            found = dock_core.dock_rect(dock_core.hyprctl_json("layers"), dock_core.hyprctl_json("monitors"))
            GLib.idle_add(done, found)

        def done(found):
            self.dock_lookup_running = False
            self.dock_screen_rect = found[0] if found else None
            self.apply_input_region(found)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def apply_input_region(self, found):
        surface = self.window.get_surface() if self.window is not None else None
        if surface is None:
            return
        width, height = self.full_screen_size()
        region = cairo.Region(cairo.RectangleInt(0, 0, width, height))
        if found:
            # Only the dock itself: beside it, the popup still takes the pointer
            # (a click there closes it, moving there does not).
            (x, y, w, h), (ox, oy) = found
            region.subtract(cairo.RectangleInt(x - ox, y - oy, w, h))
        surface.set_input_region(region)

    def follow_dock(self):
        """While open: the dock may grow (shown, items added); keep its hole fitting."""
        if self.window is None or not self.window.get_visible():
            self.dock_follow_source = 0
            return GLib.SOURCE_REMOVE
        self.update_input_region()
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def tell_dock(popup_open):
        """Keep an auto-hiding dock shown while this popup is open: the
        popup's full-screen overlay keeps the pointer from the dock's edge."""
        try:
            if not dock_core.settings()["dock_enabled"]:
                return
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            group = Gio.DBusActionGroup.get(bus, "org.omarchy.appmanager.dock", "/org/omarchy/appmanager/dock")
            group.activate_action("popup-open", GLib.Variant.new_boolean(popup_open))
        except GLib.Error:
            pass  # No dock running.

    @staticmethod
    def start_dock():
        """Start the dock (a running one just re-reads its settings)."""
        try:
            subprocess.Popen([sys.executable, str(PLUGIN_DIR / "dock.py")], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            print("Could not start the dock:", error, file=sys.stderr)

    def on_language_selected(self, dropdown, _param):
        code = self.language_codes[dropdown.get_selected()]
        if code == i18n.language_setting():
            return
        try:
            i18n.save_language(code)
        except OSError as error:
            self.notify(_("Could not save the language"), str(error))
            return
        os.environ["SIMPLE_LAUNCHER_PAGE"] = "settings"
        self.restart(self.mode)

    def set_admin_switch(self, active):
        """Move the switch without treating it as the user's choice."""
        self.admin_syncing = True
        self.admin_switch.set_active(active)
        self.admin_syncing = False

    def on_admin_action_toggled(self, switch, _param):
        if getattr(self, "admin_syncing", False):
            return
        if not switch.get_active():
            try:
                set_admin_action(False)
            except OSError as error:
                self.notify(_("Could not save the setting"), str(error))
            return
        # Switching on needs the password first: the switch stays off until
        # polkit says yes (pkexec runs only /usr/bin/true, which does nothing).
        self.set_admin_switch(False)
        if self.admin_auth_pending:
            return
        if not shutil.which("pkexec") or not os.access("/usr/bin/true", os.X_OK):
            self.notify(_("Open as administrator was not switched on"), _("Not authorized, or no polkit agent is running."))
            return
        try:
            process = Gio.Subprocess.new(["pkexec", "/usr/bin/true"],
                                         Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as error:
            self.notify(_("Open as administrator was not switched on"), error.message)
            return
        self.admin_auth_pending = True
        switch.set_sensitive(False)

        def done(proc, result):
            self.admin_auth_pending = False
            switch.set_sensitive(True)
            try:
                proc.wait_finish(result)
                ok = proc.get_successful()
            except GLib.Error:
                ok = False
            if not ok:
                self.notify(_("Open as administrator was not switched on"),
                            _("The password was not confirmed."))
                return
            try:
                set_admin_action(True)
            except OSError as error:
                self.notify(_("Could not save the setting"), str(error))
                return
            self.set_admin_switch(True)
        process.wait_async(None, done)

    def settings_open(self):
        return self.stack.get_visible_child_name() == "settings"

    def open_settings_page(self):
        self.sync_dock_settings()
        if self.shortcut_app is not None:
            self.close_shortcut_page(focus_list=False)
        self.back_chip.set_label("· ⌫")
        self.back_chip.remove_css_class("launcher-shortcut-chip")
        self.back_button.add_css_class("launcher-back-bordered")
        self.title.add_css_class("launcher-section-title")
        self.back_button.set_tooltip_text(_("Back (Backspace, Esc)"))
        self.hide_main_header()
        self.set_admin_switch(admin_action_enabled())  # Off again in a new session.
        self.sync_corners_control()  # The windows' rounding may have changed.
        self.stack.set_visible_child_name("settings")
        self.update_title()
        self.language_dropdown.grab_focus()

    def close_settings_page(self):
        self.back_chip.add_css_class("launcher-shortcut-chip")
        self.back_button.remove_css_class("launcher-back-bordered")
        self.title.remove_css_class("launcher-section-title")
        self.show_main_header()
        self.stack.set_visible_child_name(self.main_page())
        self.update_title()
        self.refresh_list()

    def close_subpage(self):
        if self.settings_open():
            self.close_settings_page()
        elif self.aux_page_open():
            self.close_aux_page()
        else:
            self.close_shortcut_page()

    def hide_main_header(self):
        self.back_button.set_visible(True)
        for button in (self.hidden_button, self.mode_button, self.running_button, self.view_button,
                       self.settings_button, self.filter_bar, self.mouse_hint):
            button.set_visible(False)
        # No hint on sub-pages: the title takes the whole header.
        self.title.set_hexpand(True)

    def show_main_header(self):
        """The list's header buttons. The overview filters by group in its
        bottom bar instead of the list | groups | origin switch."""
        self.back_button.set_visible(False)
        for button in (self.hidden_button, self.mode_button, self.settings_button):
            button.set_visible(True)
        self.view_button.set_visible(not self.overview and self.view_buttons_on)
        self.filter_bar.set_visible(self.overview)
        self.mouse_hint.set_visible(True)
        self.title.set_hexpand(False)  # The hint takes the free room.
        self.update_mouse_hint()
        self.running_button.set_visible(self.mode == "apps")

    def sync_small_dropdown(self):
        dropdown = getattr(self, "small_dropdown", None)
        if dropdown is not None and dropdown.get_selected() != self.small_views.index(self.view):
            self.view_syncing = True
            dropdown.set_selected(self.small_views.index(self.view))
            self.view_syncing = False
        return GLib.SOURCE_REMOVE

    def set_view_buttons(self, shown):
        if shown == self.view_buttons_on:
            return
        self.view_buttons_on = shown
        try:
            set_view_buttons_shown(shown)
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))

    def toggle_view(self, view=None):
        """Switch to view ("list", "groups" or "origin"), or to the next one."""
        GLib.idle_add(self.sync_small_dropdown)  # Settings shows it too (after this handler).
        if self.actions_menu_open():
            self.actions_popover.popdown()
        order = ("list", "groups", "origin")
        view = view if view in order else order[(order.index(self.view) + 1) % len(order)]
        if view == self.view:
            return
        self.view = view
        try:
            set_view(self.view)
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))
        row = self.rows.get_selected_row()
        self.update_hidden_button_state()
        self.refresh_list(selected_id=self.row_key(row) if row is not None and hasattr(row, "app") else "")

    # --- group name and details pages ------------------------------------------

    def aux_page_open(self):
        return self.stack.get_visible_child_name() in ("groupname", "details", "icon")

    def open_aux_page(self, name, title, backspace_goes_back):
        if self.actions_menu_open():
            self.actions_popover.popdown()
        self.aux_title = title
        if backspace_goes_back:
            self.back_chip.set_label("· ⌫")
            self.back_chip.remove_css_class("launcher-shortcut-chip")
            self.back_button.set_tooltip_text(_("Back (Backspace, Esc)"))
        else:
            self.back_chip.set_label("Esc")
            self.back_chip.add_css_class("launcher-shortcut-chip")
            self.back_button.set_tooltip_text(_("Back (Esc)"))
        self.back_button.add_css_class("launcher-back-bordered")
        self.title.add_css_class("launcher-section-title")
        self.hide_main_header()
        self.stack.set_visible_child_name(name)
        self.update_title()
        # Keys go to the page, not into a selectable value (which would select it all).
        self.back_button.grab_focus()

    def close_aux_page(self, refresh=True):
        self.details_token += 1  # Late facts of the closed page are dropped.
        self.back_chip.set_label("Esc")
        self.back_chip.add_css_class("launcher-shortcut-chip")
        self.back_button.remove_css_class("launcher-back-bordered")
        self.title.remove_css_class("launcher-section-title")
        self.show_main_header()
        self.stack.set_visible_child_name(self.main_page())
        self.update_title()
        if refresh:
            self.refresh_list()

    def build_group_name_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_start(4)
        page.set_margin_end(4)
        page.set_size_request(-1, LIST_HEIGHT)
        self.height_pages.append(page)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("launcher-settings-rule")
        page.append(rule)
        prompt = Gtk.Label(label=_("Name of the group"), xalign=0)
        prompt.add_css_class("launcher-setting-label")
        page.append(prompt)
        entry = Gtk.Entry()
        entry.set_max_length(groups.MAX_NAME)
        self.guard_text_field(entry)
        entry.set_placeholder_text(_("e.g. Office, Media, Work"))
        entry.connect("activate", lambda *_: self.save_group_name())
        page.append(entry)
        error = Gtk.Label(xalign=0, wrap=True)
        error.add_css_class("launcher-group-error")
        error.set_visible(False)
        page.append(error)
        buttons = Gtk.Box(spacing=6)
        buttons.set_halign(Gtk.Align.END)
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("sc-primary")
        save.connect("clicked", lambda *_: self.save_group_name())
        buttons.append(save)
        page.append(buttons)
        self.group_entry, self.group_error, self.group_prompt = entry, error, prompt
        self.stack.add_named(page, "groupname")

    def guard_text_field(self, entry):
        """Route every way text can enter the field from another client
        through bounded reads. GTK's own paste, middle-click paste and text
        drops buffer the whole offer in memory with no limit or deadline."""
        text = entry.get_delegate()
        # Ctrl+V, Shift+Insert and the context menu's Paste all emit this.
        text.connect("paste-clipboard", self.on_field_paste, entry)
        # Middle-click pastes the primary selection: claim button 2 first.
        middle = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
        middle.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        middle.connect("pressed", lambda gesture, *_: gesture.set_state(Gtk.EventSequenceState.CLAIMED))
        entry.add_controller(middle)
        # Text dropped on the field: remove the field's own (unbounded) drop target.
        controllers = text.observe_controllers()
        for i in reversed(range(controllers.get_n_items())):
            controller = controllers.get_item(i)
            if isinstance(controller, (Gtk.DropTarget, Gtk.DropTargetAsync)):
                text.remove_controller(controller)

    def on_field_paste(self, text, entry):
        text.stop_emission_by_name("paste-clipboard")

        def worker():
            pasted = clipboard_text(groups.MAX_NAME)
            GLib.idle_add(self.insert_pasted, entry, pasted)
        threading.Thread(target=worker, daemon=True).start()

    def insert_pasted(self, entry, pasted):
        if pasted is None:
            self.notify(_("Clipboard text is too long to paste"))
        elif pasted:
            entry.delete_selection()
            position = entry.insert_text(pasted, entry.get_position())
            entry.set_position(position)
        return GLib.SOURCE_REMOVE

    def open_group_name_page(self, gid, app=None):
        """Name a new group (and put app in it), or rename group gid."""
        self.group_name_target = (gid, app)
        self.group_entry.set_text(groups.display_name(gid, self.groups) if gid else "")
        self.group_error.set_visible(False)
        self.group_prompt.set_label(_("New name for “{0}”").format(groups.display_name(gid, self.groups)) if gid
                                    else _("Name of the new group") if app is None
                                    else _("Name of the new group for {0}").format(app["name"]))
        self.open_aux_page("groupname", _("Rename group") if gid else _("New group"), backspace_goes_back=False)
        self.group_entry.grab_focus()
        # Again once the page has slid in: a closing menu hands the focus back
        # to its button, and the page cannot take it while it slides.
        GLib.timeout_add(self.stack.get_transition_duration() + 40, self.focus_group_entry)

    def focus_group_entry(self):
        if self.window is not None and self.stack.get_visible_child_name() == "groupname" \
                and not self.group_entry.get_delegate().has_focus():
            self.group_entry.grab_focus()
            self.group_entry.set_position(-1)
        return GLib.SOURCE_REMOVE

    def save_group_name(self):
        gid, app = self.group_name_target or (None, None)
        try:
            if gid:
                groups.rename_group(self.groups, gid, self.group_entry.get_text())
            else:
                gid = groups.add_group(self.groups, self.group_entry.get_text())
                if app is not None:
                    groups.assign(self.groups, app["desktop_id"], gid)
        except ValueError as error:
            self.group_error.set_label(str(error))
            self.group_error.set_visible(True)
            return
        self.save_groups()
        self.group_name_target = None
        # Show where it went: the overview filtered to the group, or the
        # group view, on the group or the moved entry.
        if getattr(self, "overview", False):
            # A new empty group: stay on All (its folder or chip has a star
            # until something is put in it). One made for an entry shows it.
            self.overview_filter[self.mode] = gid if app is not None else "all"
        elif self.view != "groups":
            self.view = "groups"
            try:
                set_view(self.view)
            except OSError:
                pass
            self.update_hidden_button_state()
        self.groups["collapsed"] = [g for g in self.groups["collapsed"] if g != gid]
        self.close_aux_page(refresh=False)
        self.refresh_list(selected_id=app["desktop_id"] if app is not None else "group:" + gid)

    def build_details_page(self):
        page = Gtk.ScrolledWindow()
        page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        page.set_min_content_height(LIST_HEIGHT)
        page.set_max_content_height(LIST_HEIGHT)
        self.height_pages.append(page)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(4)
        box.set_margin_end(4)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("launcher-settings-rule")
        box.append(rule)
        top = Gtk.Box(spacing=12)
        icon_slot = Gtk.Box()
        top.append(icon_slot)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        names.set_valign(Gtk.Align.CENTER)
        names.set_hexpand(True)
        name = Gtk.Label(xalign=0, wrap=True)
        name.add_css_class("launcher-details-name")
        names.append(name)
        desc = Gtk.Label(xalign=0, wrap=True)
        desc.add_css_class("launcher-app-desc")
        names.append(desc)
        top.append(names)
        box.append(top)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)
        page.set_child(box)
        self.details_icon_slot, self.details_name, self.details_desc, self.details_grid = icon_slot, name, desc, grid
        self.stack.add_named(page, "details")

    def build_icon_page(self):
        """Change icon…: icons found online (two open icon collections) and
        in the installed icon themes; a click applies one, in every view."""
        page = Gtk.ScrolledWindow()
        page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        page.set_min_content_height(LIST_HEIGHT)
        page.set_max_content_height(LIST_HEIGHT)
        self.height_pages.append(page)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(4)
        box.set_margin_end(4)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("launcher-settings-rule")
        box.append(rule)
        top = Gtk.Box(spacing=12)
        self.icon_current = Gtk.Box()
        top.append(self.icon_current)
        self.icon_name_label = Gtk.Label(xalign=0, hexpand=True)
        self.icon_name_label.add_css_class("launcher-details-name")
        self.icon_name_label.set_ellipsize(3)
        self.icon_name_label.set_max_width_chars(1)
        top.append(self.icon_name_label)
        reset = Gtk.Button(label=_("Reset to default"))
        reset.add_css_class("sc-small")
        reset.set_valign(Gtk.Align.CENTER)
        reset.connect("clicked", lambda *_a: self.choose_icon(None))
        top.append(reset)
        self.icon_reset = reset
        box.append(top)
        search = Gtk.Box(spacing=6)
        entry = Gtk.SearchEntry()
        entry.add_css_class("launcher-filter-entry")
        entry.set_placeholder_text(_("Search icons"))
        entry.set_hexpand(True)
        entry.get_delegate().set_max_length(80)
        entry.connect("activate", lambda e: self.search_icons(e.get_text()))
        self.guard_text_field(entry)
        search.append(entry)
        go = Gtk.Button(label=_("Search"))
        go.add_css_class("sc-small")
        go.connect("clicked", lambda *_a: self.search_icons(entry.get_text()))
        search.append(go)
        box.append(search)
        self.icon_entry = entry
        self.icon_status = Gtk.Label(xalign=0)
        self.icon_status.add_css_class("launcher-app-desc")
        box.append(self.icon_status)
        self.icon_flows = {}
        for key, heading in (("online", _("Original icons (online)")), ("theme", _("Installed icons"))):
            title = Gtk.Label(label=heading.upper(), xalign=0)
            title.add_css_class("launcher-section-title")
            box.append(title)
            flow = Gtk.FlowBox()
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_homogeneous(True)
            flow.set_min_children_per_line(4)
            flow.set_max_children_per_line(10)
            flow.set_row_spacing(6)
            flow.set_column_spacing(6)
            box.append(flow)
            self.icon_flows[key] = flow
        page.set_child(box)
        self.stack.add_named(page, "icon")

    def open_icon_page(self, app):
        self.icon_app = app
        self.icon_name_label.set_label(app["name"])
        self.show_current_icon()
        self.icon_entry.set_text(app["name"])
        self.open_aux_page("icon", _("Icon for {0}").format(app["name"]), backspace_goes_back=False)
        self.search_icons(app["name"])
        self.icon_entry.grab_focus()

    def show_current_icon(self):
        while child := self.icon_current.get_first_child():
            self.icon_current.remove(child)
        self.icon_current.append(self.app_icon(self.icon_app, 48))
        self.icon_reset.set_sensitive(self.icon_app.get("desktop_id") in self.icon_overrides)

    def icon_tile(self, image, caption, choice):
        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("launcher-folder-app")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        image.set_pixel_size(40)
        inner.append(image)
        label = Gtk.Label(label=caption)
        label.add_css_class("launcher-app-desc")
        label.set_ellipsize(3)
        label.set_max_width_chars(1)
        label.set_hexpand(True)
        inner.append(label)
        button.set_child(inner)
        button.set_size_request(76, -1)
        button.set_tooltip_text(caption)
        button.connect("clicked", lambda *_a: self.choose_icon(choice))
        return button

    def search_icons(self, query):
        """Installed theme icons at once; online candidates when they arrive."""
        app = self.icon_app
        if app is None:
            return
        self.icon_search_token += 1
        token = self.icon_search_token
        for flow in self.icon_flows.values():
            flow.remove_all()
        command = app.get("cli_command") or app.get("desktop_id", "").split(":")[-1]
        words = [w for w in icons.slugs_for(query, command) if len(w) >= 3]
        theme = Gtk.IconTheme.get_for_display(self.window.get_display())
        names = sorted((n for n in theme.get_icon_names()
                        if not n.endswith("-symbolic") and any(w in n.casefold() for w in words)),
                       key=lambda n: (len(n), n))[:30]
        for name in names:
            self.icon_flows["theme"].append(self.icon_tile(Gtk.Image.new_from_icon_name(name), name, name))
        self.icon_status.set_label(_("Searching…"))
        color = self.theme.colors.get("foreground")

        state = {"shown": 0, "left": None}

        def work():
            try:
                todo = icons.candidates(query, command)
            except Exception as error:
                print("Icon search failed:", error, file=sys.stderr)
                todo = []
            GLib.idle_add(begin, len(todo))
            # A few downloads at a time; each icon shows as soon as it is here.
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(icons.download, c, color) for c in todo]
                for index, future in enumerate(futures):
                    try:
                        result = future.result()
                    except Exception:
                        result = None
                    if token != self.icon_search_token:
                        for rest in futures:
                            rest.cancel()
                        return
                    GLib.idle_add(show, index, result)

        def begin(count):
            if token != self.icon_search_token:
                return GLib.SOURCE_REMOVE
            state["left"] = count
            if self.icon_tmp is None:
                self.icon_tmp = Path(tempfile.mkdtemp(prefix="app-manager-icons-"))
                atexit.register(shutil.rmtree, self.icon_tmp, True)
            for old in self.icon_tmp.iterdir():  # Candidates of earlier searches.
                old.unlink(missing_ok=True)
            if not count:
                finish()
            return GLib.SOURCE_REMOVE

        def show(index, result):
            if token != self.icon_search_token or self.stack.get_visible_child_name() != "icon":
                return GLib.SOURCE_REMOVE
            state["left"] -= 1
            if result is not None:
                collection, slug, data, extension = result
                path = self.icon_tmp / f"{token}-{index}{extension}"
                try:
                    path.write_bytes(data)
                    texture = icon_texture(str(path))  # The same checks as every icon file.
                except OSError:
                    texture = None
                if texture is not None:
                    tile = self.icon_tile(Gtk.Image.new_from_paintable(texture), f"{slug} · {collection}",
                                          (data, extension))
                    tile.rank = index
                    # Best matches first, whatever order they arrive in.
                    flow = self.icon_flows["online"]
                    position = 0
                    while (child := flow.get_child_at_index(position)) is not None \
                            and getattr(child.get_child(), "rank", -1) < index:
                        position += 1
                    flow.insert(tile, position)
                    state["shown"] += 1
            if state["left"] <= 0:
                finish()
            return GLib.SOURCE_REMOVE

        def finish():
            self.icon_status.set_label(_("Nothing found. Try another name, e.g. the company.")
                                       if not state["shown"] and not names else _("Click an icon to use it."))
        threading.Thread(target=work, daemon=True).start()

    def choose_icon(self, choice):
        """Use choice (a theme icon name, or downloaded (data, extension));
        None: back to the entry's own icon."""
        app = self.icon_app
        if app is None:
            return
        key = app["desktop_id"]
        try:
            if choice is None:
                self.icon_overrides.pop(key, None)
            else:
                value = icons.store_file(key, *choice) if isinstance(choice, tuple) else choice
                self.icon_overrides[key] = value
            icons.save(self.icon_overrides)
        except OSError as error:
            self.notify(_("Could not save the icon"), str(error))
            return
        for entries in (self.apps, self.plugins):
            icons.apply(entries, self.icon_overrides)
        app["icon"] = self.icon_overrides.get(key) or app.get("icon_default", app.get("icon", ""))
        _icon_cache.clear()  # A stored file may have new content under the same name.
        self.close_aux_page(refresh=False)
        self.refresh_list(selected_id=key)

    def open_details_page(self, app):
        self.details_token += 1
        token = self.details_token
        while (child := self.details_icon_slot.get_first_child()) is not None:
            self.details_icon_slot.remove(child)
        icon_name = app.get("icon") or "application-x-executable"
        texture = icon_texture(icon_name) if Path(icon_name).is_file() else None
        icon = Gtk.Image.new_from_paintable(texture) if texture is not None else \
            Gtk.Image.new_from_icon_name(Path(icon_name).name)
        icon.set_pixel_size(48)
        self.details_icon_slot.append(icon)
        self.details_name.set_label(app["name"])
        self.details_desc.set_label(app.get("description", "").strip())
        self.details_desc.set_visible(bool(app.get("description", "").strip()))
        self.fill_details([(_("Reading…"), "")])
        self.open_aux_page("details", _("Details"), backspace_goes_back=True)

        def work():
            try:
                facts = details.plugin_facts(app, plugin_manifests()) if "plugin_id" in app else details.app_facts(app)
            except Exception as error:  # A broken entry must not take the page down.
                facts = {"error": str(error)}
            GLib.idle_add(lambda: token == self.details_token and self.fill_details(self.details_rows(app, facts)))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def format_date(timestamp):
        try:
            return GLib.DateTime.new_from_unix_local(int(timestamp)).format("%x %H:%M")
        except (TypeError, ValueError, OverflowError):
            return ""

    def details_rows(self, app, facts):
        kinds = {"app": _("App"), "webapp": _("Web app"), "terminal": _("Terminal app"),
                 "flatpak": _("Flatpak or Snap app"), "appimage": _("AppImage"),
                 "plugin": _("Shell plugin"), "widget": _("Bar widget")}
        origins = {"bundled": _("Comes with Omarchy"), "installed": _("Installed with Omarchy"),
                   "optional": _("Optional Omarchy package"), "builtin": _("Built into Omarchy")}
        roles = {"browser": _("Web browser"), "terminal": _("Terminal"), "email": _("Email"),
                 "files": _("Files"), "editor": _("Text editor"), "images": _("Images"),
                 "video": _("Videos"), "music": _("Music"), "pdf": _("PDF documents")}
        page = self.groups_all["plugins" if is_widget(app) else "apps"]
        rows = [(_("Type"), kinds.get(facts.get("kind"), "")),
                (_("Omarchy"), origins.get(facts.get("omarchy"), "")),
                (_("Default app for"), ", ".join(roles[r] for r in facts.get("roles", ()) if r in roles)),
                (_("Group"), groups.display_name(groups.group_of(app, page), page)),
                (_("ID"), facts.get("id", ""))]
        if facts.get("kind") == "widget":
            rows.append((_("In the bar"), _("Yes") if facts.get("in_bar") else _("No")))
        for key, label in (("version", _("Version")), ("author", _("Author")), ("license", _("License"))):
            rows.append((label, facts.get(key, "")))
        rows += [(_("Desktop file"), facts.get("desktop_file", ""), True),
                 (_("Command"), facts.get("command", "")),
                 (_("Program"), facts.get("program") or "", True),
                 (_("Folder"), facts.get("folder", ""), True)]
        package = facts.get("package")
        if package:
            rows += [(_("Package"), f"{package['name']} {package.get('version', '')}".strip()),
                     (_("Installed size"), GLib.format_size(package["size"]) if "size" in package else ""),
                     (_("Installed or updated"), self.format_date(package.get("installed"))),
                     (_("Website"), package.get("url", ""), True)]
        if facts.get("website"):
            rows.append((_("Website"), facts["website"], True))
        if "size" in facts:
            size = GLib.format_size(facts["size"])
            rows.append((_("Size on disk"), size if facts.get("size_complete", True) else _("more than {0}").format(size)))
        if "file_size" in facts:
            rows += [(_("File size"), GLib.format_size(facts["file_size"])),
                     (_("Last changed"), self.format_date(facts.get("modified")))]
        source = facts.get("source")
        if source:
            rows += [(_("Source"), source.get("url") or "", True),
                     (_("Commit"), source.get("commit") or ""),
                     (_("Installed"), self.format_date(source.get("cloned")))]
        if facts.get("error"):
            rows.append((_("Error"), facts["error"]))
        return [row for row in rows if row[1]]

    def fill_details(self, rows):
        grid = self.details_grid
        while (child := grid.get_first_child()) is not None:
            grid.remove(child)
        for index, (label, value, *copyable) in enumerate(rows):
            caption = Gtk.Label(label=label, xalign=0, yalign=0)
            caption.add_css_class("launcher-setting-label")
            caption.set_size_request(120, -1)
            grid.attach(caption, 0, index, 1, 1)
            text = Gtk.Label(label=details.short(value, 600), xalign=0, wrap=True, selectable=True)
            text.set_focusable(False)  # Still selectable with the mouse, to copy a path.
            text.set_wrap_mode(2)  # Pango WORD_CHAR: long paths break anywhere.
            text.set_max_width_chars(34)
            text.set_hexpand(True)
            text.add_css_class("launcher-details-value")
            grid.attach(text, 1, index, 1, 1)
            if copyable and copyable[0]:
                copy = Gtk.Button(icon_name="edit-copy-symbolic")
                copy.add_css_class("flat")
                copy.add_css_class("launcher-copy-btn")
                copy.set_valign(Gtk.Align.START)
                copy.set_tooltip_text(_("Copy"))
                copy.connect("clicked", lambda button, v=value: self.copy_text(button, v))
                grid.attach(copy, 2, index, 1, 1)
                address = self.web_link(value)
                if address:
                    visit = Gtk.Button(icon_name="web-browser-symbolic")
                    visit.add_css_class("flat")
                    visit.add_css_class("launcher-copy-btn")
                    visit.set_valign(Gtk.Align.START)
                    visit.set_tooltip_text(_("Open website"))
                    visit.connect("clicked", lambda _b, a=address: self.open_website(a))
                    grid.attach(visit, 3, index, 1, 1)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def web_link(value):
        """An http(s) address to open, or None. A git remote opens its web
        page (https://github.com/a/b.git -> https://github.com/a/b)."""
        text = str(value or "").strip()
        if not re.fullmatch(r"https?://[^\s<>\"']{1,2000}", text):
            return None
        return text[:-4] if text.endswith(".git") else text

    def open_website(self, address):
        """Open the address in the default browser. The popup stays open:
        the browser taking the focus must not close it."""
        self.keep_open_until = time.monotonic() + 5
        try:
            Gio.AppInfo.launch_default_for_uri(address, None)
        except GLib.Error as error:
            self.notify(_("Could not open the website"), error.message)

    def copy_text(self, button, text):
        """Put text on the clipboard; the button shows a check for a moment."""
        try:
            self.window.get_clipboard().set(text)
        except Exception as error:  # No clipboard: say so instead of failing silently.
            self.notify(_("Could not copy"), str(error))
            return
        button.set_icon_name("object-select-symbolic")
        button.set_tooltip_text(_("Copied"))
        GLib.timeout_add(1500, lambda: (button.set_icon_name("edit-copy-symbolic"),
                                        button.set_tooltip_text(_("Copy")), GLib.SOURCE_REMOVE)[-1])

    def open_shortcut_page(self, app):
        if shortcuts.app_launch_argv(app) is None:
            return
        if not app.get("self_launcher") and not app.get("openable", True):
            return
        self.sc_page.set_size_request(-1, self.page_height())
        self.shortcut_app = app
        self.sc_app_id = shortcuts.app_key(app)
        self.shortcut_state = shortcuts.load_state()
        self.shortcut_bindings = shortcuts.Bindings(self.shortcut_state)
        self.sc_editing = None
        self.sc_target_ids = [""] + [a["id"] for a in app.get("actions", [])]
        self.sc_syncing = True
        self.sc_target.set_model(Gtk.StringList.new([app["name"]] + [a["name"] for a in app.get("actions", [])]))
        self.sc_target.set_selected(0)
        self.sc_syncing = False
        self.sc_target_row.set_visible(len(self.sc_target_ids) > 1)
        self.sync_change_mode()
        self.render_shortcut_current()
        # A new shortcut always starts from the default modifiers (Super + Shift
        # unless changed). The key stays open ("?") until the user presses one
        # or picks a free key, so Assign starts disabled.
        self.set_two_step(False)
        self.set_shortcut_combo(shortcuts.load_default_mods(), None, None)
        self.back_chip.set_label("Esc")
        self.back_button.set_tooltip_text(_("Back to apps (Esc)"))
        self.hide_main_header()
        self.stack.set_visible_child_name("shortcut")
        self.update_title()
        self.set_shortcut_inhibit(True)
        self.sc_apply_button.grab_focus()

    def close_shortcut_page(self, focus_list=True):
        app = self.shortcut_app
        self.set_shortcut_inhibit(False)
        self.shortcut_app = None
        self.sc_editing = None
        self.show_main_header()
        self.stack.set_visible_child_name(self.main_page())
        self.update_title()
        if focus_list:
            self.refresh_list(selected_id=app["desktop_id"] if app else None)

    def set_shortcut_inhibit(self, enabled):
        # Bound combos must reach the capture field instead of running. The
        # empty submap does that in Hyprland; the inhibitor is for the rest,
        # though not every compositor honors it for layer surfaces.
        if enabled:
            enter_capture()
        else:
            leave_capture()
        try:
            surface = self.window.get_surface()
            if enabled:
                surface.inhibit_system_shortcuts(None)
            else:
                surface.restore_system_shortcuts()
        except Exception:
            pass

    def app_shortcuts(self, app, bindings):
        """Combos that open app right now: launcher ones, then external ones.
        Remembered per shortcut state and bindings snapshot (one per refresh)."""
        key = (id(self.shortcut_state), id(bindings))
        if getattr(self, "_shortcut_key", None) != key:
            self._shortcut_key, self._shortcut_memo = key, {}
        ident = shortcuts.app_key(app)
        if ident not in self._shortcut_memo:
            self._shortcut_memo[ident] = self._app_shortcuts(app, bindings)
        return [dict(c) for c in self._shortcut_memo[ident]]

    def _app_shortcuts(self, app, bindings):
        entry = self.shortcut_state["apps"].get(shortcuts.app_key(app), {"shortcuts": [], "disabled": []})
        disabled_ids = {shortcuts.combo_id(c) for c in entry["disabled"]}
        owned_ids = shortcuts.owned_ids(self.shortcut_state)
        external = [{**b["combo"], "action": b["action"]} for b in bindings.external_shortcuts(app)
                    if shortcuts.combo_id(b["combo"]) not in disabled_ids | owned_ids]
        found = [{**c, "action": c.get("action", "")} for c in entry["shortcuts"]] + external
        # Opening the app itself first; that one is the chip in the list.
        return sorted(found, key=lambda c: bool(c["action"]))

    def shortcut_target(self, app, combo):
        """What a shortcut opens, for labels: the app or an action of it."""
        action = combo.get("action", "")
        return shortcuts.action_name(app, action) if action else app["name"]

    def active_app_shortcuts(self):
        return self.app_shortcuts(self.shortcut_app, self.shortcut_bindings)

    def render_shortcut_current(self):
        """Active shortcuts (any source) with Remove, then turned-off ones."""
        app, bindings = self.shortcut_app, self.shortcut_bindings
        entry = self.shortcut_state["apps"].get(self.sc_app_id, {"shortcuts": [], "disabled": []})
        while child := self.sc_current.get_first_child():
            self.sc_current.remove(child)

        editing_id = shortcuts.combo_id(self.sc_editing["combo"]) if self.sc_editing else None

        def item(combo, source, note, button, tooltip, callback, dim=False, change=None):
            row = Gtk.Box(spacing=8)
            row.add_css_class("sc-item")
            if dim:
                row.add_css_class("sc-item-off")
            if change and shortcuts.combo_id(combo) == editing_id:
                row.add_css_class("sc-item-editing")
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            text.set_hexpand(True)
            text.append(self.keycaps(combo, dim=dim))
            if note:
                note_label = Gtk.Label(label=note, xalign=0)
                note_label.add_css_class("sc-note")
                note_label.set_ellipsize(3)
                text.append(note_label)
            row.append(text)
            tag = Gtk.Label(label=source)
            tag.add_css_class("sc-tag")
            tag.set_valign(Gtk.Align.CENTER)
            row.append(tag)
            if change:
                edit = Gtk.Button(icon_name="document-edit-symbolic")
                edit.add_css_class("sc-small")
                edit.add_css_class("flat")
                edit.set_tooltip_text(_("Change this shortcut to another key"))
                edit.set_valign(Gtk.Align.CENTER)
                edit.connect("clicked", lambda *_: change())
                row.append(edit)
                click = Gtk.GestureClick()
                click.connect("released", lambda *_: change())
                text.add_controller(click)
                text.set_tooltip_text(_("Click to change this shortcut"))
            action = Gtk.Button(label=button) if len(button) > 1 else Gtk.Button(icon_name="window-close-symbolic")
            action.add_css_class("sc-small")
            action.set_tooltip_text(tooltip)
            action.set_valign(Gtk.Align.CENTER)
            action.connect("clicked", lambda *_: callback())
            row.append(action)
            self.sc_current.append(row)

        active = 0
        externals = bindings.external_shortcuts(app)
        for combo in entry["shortcuts"]:
            note = _("Opens {0}").format(self.shortcut_target(app, combo))
            same = next((b for b in externals if shortcuts.combo_id(b["combo"]) == shortcuts.combo_id(combo)
                         and b["action"] == combo.get("action", "")), None)
            if same:
                note += " · " + _("duplicate of your {0} binding — remove it here to tidy up").format(same['file'])
            elif combo.get("replaces"):
                note += " · " + _("overrides “{0}”").format(combo['replaces'])
            item(combo, "Launcher", note, "×", _("Remove this shortcut")
                 + (" — " + _("“{0}” works again").format(combo['replaces']) if combo.get("replaces") else ""),
                 lambda c=combo: self.remove_shortcut(c),
                 change=lambda c=combo: self.start_change_shortcut(c, None))
            active += 1
        disabled_ids = {shortcuts.combo_id(c) for c in entry["disabled"]}
        owned_ids = shortcuts.owned_ids(self.shortcut_state)
        for bind in externals:
            cid = shortcuts.combo_id(bind["combo"])
            if cid in disabled_ids or cid in owned_ids:
                continue
            origin = _("Omarchy default") if bind["file"] == "Omarchy" else _("Your {0}").format(bind['file'])
            note = _("Opens {0}").format(self.shortcut_target(app, bind)) + f" · {origin}"
            if bind["description"] and bind["description"] != self.shortcut_target(app, bind):
                note += f" “{bind['description']}”"
            item(bind["combo"], bind["file"], note, "×",
                 _("Turn off — {0} stays unchanged and you can turn it back on").format(bind['file']),
                 lambda b=bind: self.disable_external_shortcut(b),
                 change=lambda b=bind: self.start_change_shortcut({**b["combo"], "action": b["action"]}, b))
            active += 1
        if not active:
            empty = Gtk.Label(label=_("None yet — add one below."), xalign=0)
            empty.add_css_class("sc-empty")
            self.sc_current.append(empty)
        for combo in entry["disabled"]:
            was = " · " + _("was “{0}”").format(combo['description']) if combo.get("description") else ""
            item(combo, combo.get("source") or "Config", _("Turned off") + was, _("Turn on"),
                 _("Use this shortcut from your config again"), lambda c=combo: self.restore_external_shortcut(c),
                 dim=True)
        count = f" ({active})" if active > 1 else ""
        self.sc_current_title.set_label(_("Shortcuts for {0}").format(app['name']) + count)

    def current_shortcut_action(self):
        index = self.sc_target.get_selected()
        return self.sc_target_ids[index] if 0 <= index < len(self.sc_target_ids) else ""

    def set_shortcut_action(self, action):
        self.sc_syncing = True
        self.sc_target.set_selected(self.sc_target_ids.index(action) if action in self.sc_target_ids else 0)
        self.sc_syncing = False

    def on_shortcut_target_changed(self):
        if not self.sc_syncing and self.shortcut_app is not None:
            self.update_shortcut_status()

    def classify_shortcut(self, combo):
        """classify() for the page: the app, the picked action, and the
        shortcut being changed (its own key is free to reuse)."""
        action = self.current_shortcut_action()
        if self.sc_editing and shortcuts.combo_id(combo) == shortcuts.combo_id(self.sc_editing["combo"]):
            if self.sc_editing["combo"].get("action", "") == action:
                return shortcuts.SAME, ""
            return shortcuts.RETARGET if self.sc_editing["bind"] is None else shortcuts.FREE, ""
        if self.sc_two_step.get_active() and not combo.get("then"):
            return self.shortcut_bindings.classify_first_step(combo, self.sc_keycode)
        return self.shortcut_bindings.classify(combo, self.sc_app_id, self.sc_keycode,
                                               app=self.shortcut_app, action=action)

    def start_change_shortcut(self, combo, bind):
        """Pick a new key for an existing shortcut with the Add controls.
        bind is the external binding, or None for a launcher shortcut."""
        self.sc_editing = {"combo": combo, "bind": bind}
        self.sync_change_mode()
        self.render_shortcut_current()
        self.set_shortcut_action(combo.get("action", ""))
        self.set_two_step(bool(combo.get("then")))
        self.set_shortcut_combo(combo["mods"], None, None)
        self.sc_apply_button.grab_focus()

    def cancel_change_shortcut(self):
        if self.sc_editing is None:
            return
        self.sc_editing = None
        self.sync_change_mode()
        self.render_shortcut_current()
        self.set_shortcut_combo(shortcuts.load_default_mods(), None, None)

    def sync_change_mode(self):
        if self.sc_editing:
            label = shortcuts.combo_label(self.sc_editing["combo"])
            self.sc_add_title.set_label(_("Change {0} to").format(label))
        else:
            self.sc_add_title.set_label(_("Add a shortcut"))
        self.sc_add_cancel.set_visible(self.sc_editing is not None)

    def render_shortcut_suggestions(self):
        """Free keys for the selected modifiers; clicking one picks it."""
        app = self.shortcut_app
        mods = [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]
        while child := self.sc_suggest.get_first_child():
            self.sc_suggest.remove(child)
        self.sc_legend.set_visible(bool(mods))
        self.sc_legend.set_tooltip_text(_("Taken keys can be rebound; the original works again when you remove the shortcut."))
        if self.picking_then():
            first = shortcuts.combo_label({"mods": mods, "key": self.sc_key})
            self.sc_suggestions = [shortcuts.make_combo(mods, self.sc_key, k) for k in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            self.sc_suggest_title.set_label(_("After {0}, press").format(first))
            for combo in self.sc_suggestions:
                chip = Gtk.Button(label=combo["then"])
                chip.add_css_class("sc-letter")
                self.style_key_chip(chip, combo)
                chip.connect("clicked", lambda *_, c=combo: self.pick_then(c["then"]))
                self.sc_suggest.append(chip)
            return self.mark_other_keys(mods)
        if mods:
            # Every letter, coloured by who has it; taken ones can be rebound.
            self.sc_suggestions = [shortcuts.make_combo(mods, k) for k in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            self.sc_suggest_title.set_label(_("Keys with {0}").format(" + ".join(shortcuts.MOD_LABELS[m] for m in mods)))
            for combo in self.sc_suggestions:
                chip = Gtk.Button(label=combo["key"])
                chip.add_css_class("sc-letter")
                self.style_key_chip(chip, combo)
                chip.connect("clicked", lambda *_, c=combo: self.pick_shortcut_key(c["mods"], c["key"]))
                self.sc_suggest.append(chip)
            return self.mark_other_keys(mods)
        self.sc_suggestions = self.shortcut_bindings.suggestions(app, self.sc_app_id, None)
        self.sc_suggest_title.set_label(_("Free suggestions"))
        for combo in self.sc_suggestions:
            chip = Gtk.Button(label=shortcuts.combo_label(combo).replace(" + ", "+"))
            chip.add_css_class("sc-suggest")
            chip.set_tooltip_text(_("Use {0}").format(shortcuts.combo_label(combo)))
            chip.connect("clicked", lambda *_, c=combo: self.pick_shortcut_key(c["mods"], c["key"]))
            self.sc_suggest.append(chip)
        self.mark_other_keys(mods)
        if not self.sc_suggestions:
            none = Gtk.Label(label=_("Choose modifiers above to see every key."), xalign=0)
            none.add_css_class("sc-note")
            self.sc_suggest.append(none)

    def mark_other_keys(self, mods):
        for key, button in self.sc_other_buttons.items():
            if self.picking_then():
                self.style_key_chip(button, shortcuts.make_combo(mods, self.sc_key, key))
            elif mods:
                self.style_key_chip(button, shortcuts.make_combo(mods, key))
            else:
                for css in KEY_STYLES.values():
                    button.remove_css_class(css)
                button.remove_css_class("sc-key-picked")
                button.set_tooltip_text(_("Choose modifiers first"))

    def style_key_chip(self, chip, combo):
        """Colour a key button by who has the combo, and say so on hover."""
        level, owner = self.classify_shortcut(combo)
        chip.add_css_class("sc-keychip")
        for css in KEY_STYLES.values():
            chip.remove_css_class(css)
        chip.add_css_class(KEY_STYLES.get(level, "sc-free-key"))
        if combo.get("then"):
            picked = bool(self.sc_then) and combo["then"].casefold() == self.sc_then.casefold()
        else:
            picked = bool(self.sc_key) and combo["key"].casefold() == self.sc_key.casefold()
        (chip.add_css_class if picked else chip.remove_css_class)("sc-key-picked")
        label = shortcuts.combo_label(combo)
        if level == shortcuts.FREE:
            tip = _("{0} — free").format(label)
        elif level in (shortcuts.SAME, shortcuts.RETARGET):
            tip = _("{0} — already opens {1}").format(label, self.shortcut_app['name'])
        elif level == shortcuts.OMARCHY:
            tip = _("{0} — Omarchy default “{1}”; click to rebind").format(label, owner)
        elif level == shortcuts.LAUNCHER:
            tip = _("{0} — opens {1}; click to move it here").format(label, owner)
        else:
            tip = _("{0} — your binding “{1}”; click to rebind").format(label, owner)
        chip.set_tooltip_text(tip)

    def pick_shortcut_key(self, mods, key):
        """Pick a key by clicking it; clicking the picked key again clears
        it, the way a second click turns a modifier off."""
        combo, current = shortcuts.make_combo(mods, key), self.current_shortcut_combo()
        if combo and current and shortcuts.combo_id(combo) == shortcuts.combo_id(current):
            self.set_shortcut_combo(mods, None, None)
        else:
            self.set_shortcut_combo(mods, key, None)

    def picking_then(self):
        """Two steps with the first one chosen: keys now pick the second."""
        return self.sc_two_step.get_active() and bool(self.sc_key)

    def pick_then(self, key):
        """Pick the second key; clicking the picked one again clears it."""
        same = self.sc_then is not None and self.sc_then.casefold() == str(key).casefold()
        self.sc_then = None if same else key
        self.update_shortcut_status()

    def set_two_step(self, enabled):
        self.sc_syncing = True
        self.sc_two_step.set_active(enabled)
        self.sc_syncing = False
        self.sc_then = None

    def on_two_step_toggled(self, _check):
        if self.sc_syncing:
            return
        self.sc_then = None
        self.update_shortcut_status()

    def set_shortcut_combo(self, mods, key, keycode):
        self.sc_syncing = True
        for mod, toggle in self.sc_toggles.items():
            toggle.set_active(mod in mods)
        self.sc_syncing = False
        self.sc_key, self.sc_keycode = key, keycode
        self.sc_then = None  # A new first step needs its second key again.
        self.update_shortcut_status()

    def on_shortcut_mod_toggled(self, _toggle):
        if not self.sc_syncing:
            self.update_shortcut_status()

    def selected_shortcut_mods(self):
        return [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]

    def sync_shortcut_default_check(self):
        """Checked when the selected modifiers are the saved default."""
        mods = self.selected_shortcut_mods()
        default = shortcuts.load_default_mods()
        self.sc_syncing = True
        self.sc_default_check.set_active(bool(mods) and mods == default)
        self.sc_syncing = False
        self.sc_default_check.set_sensitive(bool(mods))
        label = " + ".join(shortcuts.MOD_LABELS[m] for m in default)
        self.sc_default_check.set_tooltip_text(
            _("Current default: {0}. Check to make the selected modifiers the default; "
              "uncheck to go back to Super + Shift.").format(label))

    def on_shortcut_default_toggled(self, check):
        if self.sc_syncing:
            return
        mods = self.selected_shortcut_mods()
        try:
            shortcuts.save_default_mods(mods if check.get_active() and mods else shortcuts.DEFAULT_MODS)
        except OSError as error:
            self.set_shortcut_status(_("Could not save the default: {0}").format(error), "sc-danger")
        self.sync_shortcut_default_check()

    def current_shortcut_combo(self):
        if not self.sc_key:
            return None
        mods = [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]
        if self.sc_two_step.get_active():
            return shortcuts.make_combo(mods, self.sc_key, self.sc_then) if self.sc_then else None
        return shortcuts.make_combo(mods, self.sc_key)

    def set_shortcut_status(self, text, level, button_label=None, button_style=None, enabled=False):
        for css in ("sc-free", "sc-warn", "sc-hint-omarchy", "sc-danger", "sc-info"):
            self.sc_status.remove_css_class(css)
        self.sc_status.add_css_class(level)
        self.sc_status.set_label(text)
        for css in ("sc-warn-btn", "sc-omarchy-btn", "sc-danger-btn"):
            self.sc_apply_button.remove_css_class(css)
        if button_style:
            self.sc_apply_button.add_css_class(button_style)
        if self.sc_editing and button_label in (None, "Assign"):
            button_label = N_("Change")
        self.sc_apply_button.set_label(_(button_label or "Assign"))
        self.sc_apply_button.set_sensitive(enabled)

    def show_shortcut_keys(self, combo):
        while child := self.sc_key_box.get_first_child():
            self.sc_key_box.remove(child)
        if combo:
            self.sc_key_box.append(self.keycaps(combo, large=True))
            return
        # No key yet: the chosen modifiers as keycaps, then a grey placeholder.
        mods = [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]
        if self.picking_then():
            # First step chosen: show it, then wait for the second key.
            self.sc_key_box.append(self.keycaps({"mods": mods, "key": self.sc_key}, large=True))
            then = Gtk.Label(label=_("then"))
            then.add_css_class("sc-note")
            then.set_valign(Gtk.Align.CENTER)
            self.sc_key_box.append(then)
            placeholder = Gtk.Label(label=_("a second key"), xalign=0)
            placeholder.add_css_class("sc-placeholder")
            placeholder.set_valign(Gtk.Align.CENTER)
            self.sc_key_box.append(placeholder)
            return
        if mods:
            caps = self.keycaps({"mods": mods, "key": ""}, large=True)
            caps.remove(caps.get_last_child())  # drop the empty key cap
            self.sc_key_box.append(caps)
        placeholder = Gtk.Label(label=_("select a key"), xalign=0)
        placeholder.add_css_class("sc-placeholder")
        placeholder.set_valign(Gtk.Align.CENTER)
        self.sc_key_box.append(placeholder)

    def update_shortcut_status(self):
        if self.shortcut_app is None:
            return
        combo = self.current_shortcut_combo()
        self.sync_shortcut_default_check()
        self.show_shortcut_keys(combo)
        self.render_shortcut_suggestions()
        name = self.shortcut_app["name"]
        if combo is None:
            if self.picking_then():
                self.set_shortcut_status(_("Now press the second key on its own, or pick one below."), "sc-info")
            elif self.sc_two_step.get_active():
                self.set_shortcut_status(_("Press the first combo (with modifiers), or pick a key below."), "sc-info")
            else:
                self.set_shortcut_status(_("Press a key, or pick a free key below."), "sc-info")
            return
        if not combo["mods"]:
            self.set_shortcut_status(_("Choose at least one modifier — Super is recommended."), "sc-info")
            return
        level, owner = self.classify_shortcut(combo)
        target = self.shortcut_target(self.shortcut_app, {"action": self.current_shortcut_action()})
        if self.sc_editing and shortcuts.combo_id(combo) == shortcuts.combo_id(self.sc_editing["combo"]):
            if level == shortcuts.SAME:
                self.set_shortcut_status(_("That is the current key — press a different one, or pick "
                                           "something else to open."), "sc-info")
            else:
                self.set_shortcut_status(_("✓ Same key, now opens {0}.").format(target), "sc-free", N_("Change"), None, True)
            return
        if level == shortcuts.RETARGET:
            was = self.shortcut_target(self.shortcut_app, {"action": owner})
            self.set_shortcut_status(_("Opens {0} now — assigning makes it open {1}.").format(was, target),
                                     "sc-info", N_("Change"), None, True)
            return
        if level == shortcuts.FREE:
            tip = "" if "SUPER" in combo["mods"] else " " + _("Without Super, apps can no longer receive it.")
            self.set_shortcut_status(_("✓ Free.") + tip, "sc-free", "Assign", None, True)
        elif level == shortcuts.SAME:
            source = f" (“{owner}”)" if owner and owner != name else ""
            self.set_shortcut_status(_("Already opens {0} — nothing to add.").format(target + source), "sc-info")
        elif level == shortcuts.LAUNCHER:
            self.set_shortcut_status(_("Opens {0} now. Assigning moves it to {1}.").format(owner, target),
                                     "sc-warn", N_("Move here"), "sc-warn-btn", True)
        elif level == shortcuts.CUSTOM:
            self.set_shortcut_status(_("Your binding “{0}”. Assigning turns it off while this shortcut "
                                       "exists; removing the shortcut brings it back.").format(owner),
                                     "sc-warn", N_("Rebind"), "sc-warn-btn", True)
        else:
            self.set_shortcut_status(_("Omarchy default “{0}”. Assigning turns it off while this shortcut "
                                       "exists; removing the shortcut brings it back.").format(owner),
                                     "sc-hint-omarchy", N_("Rebind"), "sc-omarchy-btn", True)

    def on_shortcut_key(self, controller, keyval, keycode, state):
        if self.shortcut_app is None:
            return False
        if keyval == Gdk.KEY_Escape:
            if self.sc_editing:
                self.cancel_change_shortcut()
            else:
                self.close_shortcut_page()
            return True
        any_mod = (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.SHIFT_MASK
                   | Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)
        # Plain Enter assigns; with modifiers held it is the key being captured.
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter) and not state & any_mod:
            if self.sc_apply_button.get_sensitive():
                self.apply_shortcut()
            return True
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            return False
        modifier_keys = {Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
                         Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
                         Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
                         Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock}
        if keyval in modifier_keys:
            return True

        base = Gdk.keyval_to_lower(keyval)
        try:
            ok, keys, keyvals = self.window.get_display().map_keycode(keycode)
            group = controller.get_group()
            if ok:
                for entry, value in zip(keys, keyvals):
                    if entry.level == 0 and entry.group == group:
                        base = value
                        break
        except Exception:
            pass
        name = Gdk.keyval_name(base) or ""
        if shortcuts.make_combo([], name) is None:
            self.sc_key = None
            self.show_shortcut_keys(None)
            self.set_shortcut_status(_("That key cannot be used for a shortcut."), "sc-info")
            return True

        held = {
            "SUPER": Gdk.ModifierType.SUPER_MASK, "SHIFT": Gdk.ModifierType.SHIFT_MASK,
            "CTRL": Gdk.ModifierType.CONTROL_MASK, "ALT": Gdk.ModifierType.ALT_MASK,
        }
        mods = [mod for mod, mask in held.items() if state & mask]
        now = GLib.get_monotonic_time()
        if (not mods and self.sc_key and not self.sc_two_step.get_active()
                and now - self.sc_pressed_at < TWO_STEP_WINDOW_US):
            self.set_two_step(True)  # Pressed like Super + Alt + Space, N.
        if mods:
            self.sc_pressed_at = now
        if not mods and self.picking_then():
            self.sc_then = shortcuts.make_combo([], name)["key"]
            self.update_shortcut_status()
            return True
        if not mods:
            mods = [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]
        self.set_shortcut_combo(mods, name, keycode)
        return True

    def commit_shortcuts(self, new_state, message):
        new_state["apps"] = {k: v for k, v in new_state["apps"].items() if v["shortcuts"] or v["disabled"]}
        try:
            shortcuts.save_and_apply(new_state)
        except (RuntimeError, OSError) as error:
            first = str(error).strip().splitlines()[0] if str(error).strip() else _("unknown error")
            self.set_shortcut_status(_("Not applied — Hyprland reported: {0}").format(first), "sc-danger")
            return False
        finally:
            # Saving reloads Hyprland, which leaves the capture submap.
            if self.shortcut_app is not None:
                enter_capture()
        self.shortcut_state = shortcuts.load_state()
        self.shortcut_bindings = shortcuts.Bindings(self.shortcut_state)
        self.list_bindings = self.shortcut_bindings
        self.sc_key, self.sc_keycode, self.sc_then = None, None, None
        self.sc_editing = None
        self.sync_change_mode()
        self.render_shortcut_current()
        self.show_shortcut_keys(None)
        self.render_shortcut_suggestions()
        self.set_shortcut_status(message, "sc-free")
        return True

    def shortcut_entry(self, state):
        app = self.shortcut_app
        entry = state["apps"].setdefault(self.sc_app_id, {"shortcuts": [], "disabled": []})
        entry["name"] = app["name"]
        entry["launch"] = shortcuts.app_launch_argv(app)
        return entry

    def apply_shortcut(self):
        combo = self.current_shortcut_combo()
        if combo is None or not combo["mods"] or self.shortcut_app is None:
            return
        level, owner = self.classify_shortcut(combo)
        if level == shortcuts.SAME:
            return
        action = self.current_shortcut_action()
        new_state = copy.deepcopy(self.shortcut_state)
        cid = shortcuts.combo_id(combo)
        if level in (shortcuts.LAUNCHER, shortcuts.RETARGET):
            for entry in new_state["apps"].values():
                entry["shortcuts"] = [c for c in entry["shortcuts"] if not shortcuts.collides(c, combo)]
        entry = self.shortcut_entry(new_state)
        entry["disabled"] = [c for c in entry["disabled"] if shortcuts.combo_id(c) != cid]
        replaces = owner if level in (shortcuts.CUSTOM, shortcuts.OMARCHY) else ""
        target = self.shortcut_target(self.shortcut_app, {"action": action})
        message = _("✓ Saved — {0} opens {1}.").format(shortcuts.combo_label(combo), target)
        editing = self.sc_editing
        if editing:
            old = editing["combo"]
            old_id = shortcuts.combo_id(old)
            if editing["bind"] is None:
                # Dropping it also ends any override it held, so that binding works again.
                entry["shortcuts"] = [c for c in entry["shortcuts"] if shortcuts.combo_id(c) != old_id]
            else:
                bind = editing["bind"]
                entry["disabled"].append({**old, "description": bind["description"], "source": bind["file"]})
            message = (_("✓ {0} now opens {1}.").format(shortcuts.combo_label(combo), target) if old_id == cid else
                       _("✓ Changed {0} to {1}.").format(shortcuts.combo_label(old), shortcuts.combo_label(combo)))
        entry["shortcuts"].append({**combo, "replaces": replaces, "action": action})
        self.commit_shortcuts(new_state, message)

    def remove_shortcut(self, combo):
        new_state = copy.deepcopy(self.shortcut_state)
        cid = shortcuts.combo_id(combo)
        entry = self.shortcut_entry(new_state)
        entry["shortcuts"] = [c for c in entry["shortcuts"] if shortcuts.combo_id(c) != cid]
        restored = " " + _("“{0}” works again").format(combo['replaces']) + "." if combo.get("replaces") else ""
        self.commit_shortcuts(new_state, _("✓ Removed {0}.").format(shortcuts.combo_label(combo)) + restored)

    def disable_external_shortcut(self, bind):
        new_state = copy.deepcopy(self.shortcut_state)
        entry = self.shortcut_entry(new_state)
        entry["disabled"].append({**bind["combo"], "description": bind["description"], "source": bind["file"]})
        self.commit_shortcuts(new_state, _("✓ Turned off {0}. Turn it on again any time.").format(
            shortcuts.combo_label(bind['combo'])))

    def restore_external_shortcut(self, combo):
        new_state = copy.deepcopy(self.shortcut_state)
        cid = shortcuts.combo_id(combo)
        entry = self.shortcut_entry(new_state)
        entry["disabled"] = [c for c in entry["disabled"] if shortcuts.combo_id(c) != cid]
        self.commit_shortcuts(new_state, _("✓ {0} is on again.").format(shortcuts.combo_label(combo)))

    def set_hidden(self, app, hidden):
        mode = "plugins" if is_widget(app) else "apps"
        hidden_ids = self.hidden_by_mode[mode]
        selected_id = app["desktop_id"]
        if hidden:
            row = self.item_view().get_first_child()
            while row:
                if hasattr(row, "app") and row.app["desktop_id"] == selected_id:
                    target = row.get_next_sibling() or row.get_prev_sibling()
                    if target:
                        selected_id = self.row_key(target)
                    break
                row = row.get_next_sibling()
        if hidden:
            hidden_ids.add(app["desktop_id"])
        else:
            hidden_ids.discard(app["desktop_id"])
        save_hidden_apps(hidden_ids, HIDDEN_PLUGINS_FILE if mode == "plugins" else HIDDEN_FILE)
        self.refresh_list(selected_id=selected_id)

    def update_title(self):
        if not hasattr(self, "title"):
            return
        if hasattr(self, "stack") and self.settings_open():
            self.title.set_label(_("Settings").upper())
            return
        if getattr(self, "shortcut_app", None) is not None:
            self.title.set_label(self.shortcut_app["name"])
            return
        if hasattr(self, "stack") and self.aux_page_open():
            self.title.set_label(self.aux_title.upper())
            return
        label = _("Widgets & Plugins") if self.mode == "plugins" else _("Apps")
        if self.show_hidden:
            label += " (" + _("Showing hidden") + ")"
        if self.search_query:
            label += f" — {self.search_query}"
        self.title.set_label(label)

    def update_hidden_button_state(self):
        if not hasattr(self, "hidden_button"):
            return
        what = self.list_kind
        if self.show_hidden:
            self.hidden_button.set_icon_name("view-conceal-symbolic")
            self.hidden_button.set_tooltip_text(_("Hide hidden plugins") if what == "plugins" else _("Hide hidden apps"))
            self.hidden_button.add_css_class("launcher-toggle-active")
        else:
            self.hidden_button.set_icon_name("view-reveal-symbolic")
            self.hidden_button.set_tooltip_text(_("Show hidden plugins") if what == "plugins" else _("Show hidden apps"))
            self.hidden_button.remove_css_class("launcher-toggle-active")
        self.view_buttons["list"].set_tooltip_text(_("List view (Ctrl+G)"))
        self.view_buttons["groups"].set_tooltip_text(_("Group view (Ctrl+G)"))
        self.view_buttons["origin"].set_tooltip_text(_("By origin: your default apps, Omarchy, other (Ctrl+G)"))
        for view, button in self.view_buttons.items():
            if view == self.view:
                button.add_css_class("launcher-toggle-active")
            else:
                button.remove_css_class("launcher-toggle-active")
        if self.main_page_open():
            self.show_main_header()
        if self.running_first:
            self.running_button.set_tooltip_text(_("Sort apps by name"))
            self.running_button.add_css_class("launcher-toggle-active")
        else:
            self.running_button.set_tooltip_text(_("Show running apps first"))
            self.running_button.remove_css_class("launcher-toggle-active")
        # The button shows where it leads, like the eye.
        if self.mode == "plugins":
            self.mode_button.set_icon_name("view-app-grid-symbolic")
            self.mode_button.set_tooltip_text(_("Show apps (Tab)"))
        else:
            self.mode_button.set_icon_name("application-x-addon-symbolic")
            self.mode_button.set_tooltip_text(_("Show widgets & plugins (Tab)"))
        self.update_title()

    def toggle_running_first(self, *_args):
        self.running_first = not self.running_first
        try:
            set_running_first(self.running_first)
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))
        self.update_hidden_button_state()
        self.refresh_list()

    def toggle_hidden(self, *_args):
        self.show_hidden_by_mode[self.mode] = not self.show_hidden
        self.update_hidden_button_state()
        self.refresh_list()

    def tray_changed(self):
        try:
            current = tray.registered()
        except Exception:
            return False
        changed = current != getattr(self, "tray_seen", current)
        self.tray_seen = current
        return changed

    def tray_app(self, item):
        """The installed app a tray icon belongs to, or None."""
        key = item["tray_id"].casefold()
        app = next((a for a in self.apps if a["desktop_id"].casefold() == key
                    or a["name"].casefold() == item["name"].casefold()), None)
        if app is None and item.get("tray_programs"):
            # Matched by the process behind the icon (LM Studio runs as lm-studio).
            app = next((a for a in self.apps if app_process_name(a) in item["tray_programs"]
                        or Path(a.get("argv", [""])[0]).stem.casefold() in item["tray_programs"]), None)
        return app

    def quit_action(self, app, state):
        """How to quit a running app, or None: its own tray Quit entry when it
        has one (a clean exit), else end its process when it only runs in the
        background (no window left to close)."""
        try:
            item = next((i for i in tray.list_items() if self.tray_app(i) is app), None)
        except Exception:
            item = None
        node = tray.find_quit(item["tray_menu"]) if item else None
        if node:
            return lambda: (tray.click(item, node["id"]), GLib.timeout_add(800, self.refresh_after_close))
        name = app_process_name(app)
        if state != "background" or not name or name in TERMINALS:
            return None
        pids = process_pids(name)
        if not pids:
            return None

        def end():
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            GLib.timeout_add(800, self.refresh_after_close)
        return end

    def load_plugins(self):
        self.tray_seen, self.plugins = self.collect_plugins()
        self.prepare_entries(self.plugins, "plugins")
        self.plugins_at = time.monotonic()

    def prefetch_plugins(self):
        """Read the plugin page in the background (about 0.1 s of D-Bus and
        omarchy-shell), so Tab shows it at once. A newer read wins."""
        self.plugins_generation = getattr(self, "plugins_generation", 0) + 1
        generation = self.plugins_generation

        def work():
            try:
                result = self.collect_plugins()
            except Exception as error:
                print("Error reading plugins:", error, file=sys.stderr)
                return
            GLib.idle_add(apply, result)

        def apply(result):
            if generation != self.plugins_generation:
                return GLib.SOURCE_REMOVE
            self.tray_seen, self.plugins = result
            self.prepare_entries(self.plugins, "plugins")
            self.plugins_at = time.monotonic()
            # Already on the plugin page: show the fresh list, unless a menu
            # or a sub-page is open (rebuilding rows would close it).
            if (self.mode == "plugins" and self.window.get_visible() and not self.actions_menu_open()
                    and self.main_page_open()):
                row = self.selected_item()
                self.refresh_list(selected_id=self.row_key(row) if row is not None else None)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    def check_updates(self):
        """Look for app and plugin updates in the background, at most every
        updates.CHECK_INTERVAL seconds; the marks appear when it is done."""
        now = time.monotonic()
        if self.updates_running or (self.updates_at is not None
                                    and now - self.updates_at < updates.CHECK_INTERVAL):
            return
        self.updates_running = True
        desktop_paths = [a.get("desktop_path", "") for a in self.apps]
        try:
            plugin_ids = [p.name for p in USER_PLUGINS_DIR.iterdir() if p.is_dir()]
        except OSError:
            plugin_ids = []

        def work():
            try:
                result = updates.check(desktop_paths, plugin_ids)
            except Exception as error:
                print("Error checking for updates:", error, file=sys.stderr)
                result = None
            GLib.idle_add(apply, result)

        def apply(result):
            self.updates_running = False
            self.updates_at = time.monotonic()
            if result is not None and result != self.updates:
                self.updates = result
                if (self.window.get_visible() and not self.actions_menu_open() and self.main_page_open()):
                    row = self.selected_item()
                    self.refresh_list(selected_id=self.row_key(row) if row is not None else None)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    def prepare_entries(self, entries, kind):
        """Freshly read entries: note new ones, use the icons chosen by hand."""
        self.note_new(entries, kind)
        icons.apply(entries, self.icon_overrides)

    def note_new(self, entries, kind):
        ids = [a["desktop_id"] for a in entries if a.get("desktop_id") and "tray_id" not in a]
        installed = {a["desktop_id"]: a["installed_at"] for a in entries if a.get("installed_at")}
        if newapps.note(self.seen, ids, kind, installed=installed):
            self.save_seen()

    def save_seen(self):
        try:
            newapps.save(self.seen)
        except OSError as error:
            print("Could not save seen.json:", error, file=sys.stderr)

    def is_new(self, app):
        return bool(app.get("desktop_id")) and "tray_id" not in app and newapps.is_new(self.seen, app["desktop_id"])

    def new_group_star(self, gid):
        """A group just made: a star until its first entry is put in it."""
        if not groups.is_new(self.groups, gid):
            return None
        star = Gtk.Label(label="★")
        star.add_css_class("launcher-new-star-inline")
        star.set_valign(Gtk.Align.CENTER)
        star.set_tooltip_text(_("New group: drag apps onto it to fill it."))
        return star

    @staticmethod
    def new_star():
        """Newly installed: just a small star, the same in rows, cards and folders."""
        star = Gtk.Label(label="★")
        star.add_css_class("launcher-new-star-inline")
        star.set_valign(Gtk.Align.CENTER)
        star.set_tooltip_text(_("Newly installed"))
        return star

    def update_plugin(self, app):
        """Update the plugin in the background (`omarchy plugin update --yes`)
        and say how it went."""
        plugin_id = app.get("plugin_id", "")
        updating = getattr(self, "plugins_updating", set())
        if not plugin_id or plugin_id in updating:
            return
        self.plugins_updating = updating | {plugin_id}
        self.notify(_("Updating {0}…").format(app["name"]))

        def work():
            ok, message = updates.run_plugin_update(plugin_id)
            GLib.idle_add(done, ok, message)

        def done(ok, message):
            self.plugins_updating = self.plugins_updating - {plugin_id}
            if ok:
                self.updates["plugins"].pop(plugin_id, None)
                self.notify(_("{0} updated").format(app["name"]))
                self.prefetch_plugins()  # New version, maybe a new name or description.
            else:
                self.notify(_("{0} could not be updated").format(app["name"]), message)
            if self.window.get_visible() and not self.actions_menu_open() and self.main_page_open():
                row = self.selected_item()
                self.refresh_list(selected_id=self.row_key(row) if row is not None else None)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    def update_system(self):
        """Omarchy's own update window (password, snapshot, all packages)."""
        launcher = shutil.which("omarchy-launch-floating-terminal-with-presentation")
        if launcher is None or shutil.which("omarchy-update") is None:
            self.notify(_("Could not start the update"), "omarchy-update")
            return
        try:
            subprocess.Popen([launcher, "omarchy-update"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            self.notify(_("Could not start the update"), str(error))
            return
        self.updates_at = None  # Check again the next time the popup opens.
        self.hide()

    def update_note(self, app):
        """"Update available: …" for an entry with an update, else None."""
        behind = self.updates["plugins"].get(app.get("plugin_id")) if app.get("plugin_id") else None
        if behind:
            return ngettext("Update available: {0} new commit", "Update available: {0} new commits",
                            behind).format(behind)
        package = self.updates["apps"].get(app.get("desktop_path")) if app.get("desktop_path") else None
        if package:
            return _("Update available: {0} {1} → {2}").format(*package)
        return None

    def update_badge(self, note):
        badge = self.tile_badge("software-update-available-symbolic", _("Update"))
        badge.add_css_class("launcher-update-badge")
        badge.set_tooltip_text(note)
        return badge

    def collect_plugins(self):
        """(tray registration snapshot, plugin page entries). No GTK here:
        it also runs on the prefetch thread."""
        plugins, items, seen = [], [], getattr(self, "tray_seen", None)
        try:
            seen = tray.registered()
        except Exception:
            pass
        try:
            plugins = parse_plugins()
        except Exception as e:
            print("Error listing shell plugins:", e, file=sys.stderr)
        try:
            items = tray.list_items()
        except Exception as e:
            print("Error listing tray icons:", e, file=sys.stderr)
        for item in items:
            # The app's own icon reads better than a monochrome tray glyph.
            app = self.tray_app(item)
            if app:
                item["name"] = app["name"]
                item["app"] = app
                item["icon"] = app.get("icon") or item["icon"]
        # Apps running in the bar tray first: they are what is open right now.
        return seen, items + plugins

    def toggle_mode(self, *_args):
        if self.actions_menu_open():
            self.actions_popover.popdown()
        self.mode = "apps" if self.mode == "plugins" else "plugins"
        if self.mode == "plugins":
            # The background read from opening is fresh enough to show now;
            # it is read again behind it. Without one, read it here.
            if self.plugins and time.monotonic() - getattr(self, "plugins_at", 0) < 120:
                self.prefetch_plugins()
            else:
                self.load_plugins()
        # The search stays: it filters the other list the same way.
        self.update_hidden_button_state()
        self.refresh_list(selected_id="")

    def close_app(self, app):
        for client in self.clients:
            if self.window_matches(client, app) and client.get("address", "").startswith("0x"):
                command = "hl.dsp.window.close({window = " + json.dumps("address:" + client["address"]) + "})"
                subprocess.Popen(["hyprctl", "dispatch", command],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        GLib.timeout_add(300, self.refresh_after_close)

    def refresh_after_close(self):
        self.refresh_running()
        self.refresh_list()
        return GLib.SOURCE_REMOVE


if __name__ == "__main__":
    raise SystemExit(Launcher().run(sys.argv))
