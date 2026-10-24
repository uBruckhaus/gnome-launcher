#!/usr/bin/env python3
"""GNOME Launcher: all your apps in one window: launch, group, bind, inspect."""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

import desktop  # noqa: E402

import atexit  # noqa: E402
import concurrent.futures  # noqa: E402
import colorsys  # noqa: E402
import copy  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from launcher_core import (  # noqa: E402
    TERMINALS, app_process_name, background_process_names, process_pids, terminal_command,
    DESKTOP_DIRS, HIDDEN_FILE, get_appimage_directories,
    admin_action_enabled, admin_command, running_first_enabled, set_running_first, saved_view, set_view,
    saved_wide_layout, set_wide_layout, view_buttons_shown, set_view_buttons_shown, filter_field_shown,
    set_filter_field_shown, saved_corners, set_corners, saved_size, set_saved_size,
    set_admin_action, forget_saved_admin_action, clipboard_text, launch_action, launch_app, launch_as_admin,
    load_hidden_apps, parse_desktop_files, save_hidden_apps, icon_bytes, icon_file_for,
)
from palette import ThemePalette  # noqa: E402
from i18n import _, N_, ngettext  # noqa: E402
import details  # noqa: E402
import groups  # noqa: E402
import shortcuts  # noqa: E402
import updates  # noqa: E402
import newapps  # noqa: E402
import icons  # noqa: E402
import corners  # noqa: E402
import previews  # noqa: E402
import topbar  # noqa: E402

# The pages' height at the default window size; the window sizes them after.
LIST_HEIGHT = 590
CARD_WIDTH = 420
MIN_LIST_HEIGHT = 300
# Resized wide enough for two cards side by side, the list turns into the
# overview: cards in columns, filtered by group in a bar at the bottom.
TILE_WIDTH = 196       # A card's narrowest width.
TILE_SPACING = 10
MAX_COLUMNS = 6
DRAG_PREFIX = "gnome-launcher-entry:"  # A card dragged onto a group chip.
DRAG_GROUP_PREFIX = "gnome-launcher-group:"  # A group dragged to a new place.
# Folder layout: each group a folder of FOLDER_COLUMNS x FOLDER_ROWS icons.
FOLDER_COLUMNS = 3
FOLDER_ROWS = 3
FOLDER_ICON = 36       # The smallest icon: below it the page scrolls.
FOLDER_ICON_MAX = 96   # The largest, when few folders fill a big window.
FOLDER_CELL = FOLDER_ICON + 14  # The icon, its button's padding and border.
# Under the icons: the box's spacing and the name label (12 px bold).
FOLDER_NAME_HEIGHT = 6 + 18
# A folder's narrowest width: its icons, their spacing, the frame's padding
# and border, and the tile's padding. Two fit side by side at the default size.
FOLDER_TILE_WIDTH = FOLDER_COLUMNS * FOLDER_CELL + (FOLDER_COLUMNS - 1) * 6 + 2 * 6 + 2 + 2 * 4
CARD_PADDING = 34      # The card's padding, left and right.
DROP_MARKER = 4        # The drop marker bar's thickness.
SETTINGS_CARD_WIDTH = 350  # A settings card: fits the narrow window, the same for every group.


def grid_columns(card_width):
    """Card columns at this width; 1 means the list."""
    inner = card_width - CARD_PADDING
    return max(1, min(MAX_COLUMNS, (inner + TILE_SPACING) // (TILE_WIDTH + TILE_SPACING)))


def folder_columns(card_width):
    """Folders side by side at this width (the folder layout's start page)."""
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


def app_image(icon_name):
    """A Gtk.Image for a desktop file's Icon: its file, the theme's icon, or
    the hicolor/pixmaps file the theme misses (a stale icon-theme.cache)."""
    image_extensions = {".png", ".svg", ".xpm", ".jpg", ".jpeg", ".webp", ".bmp"}
    path = Path(icon_name)
    themed_name = path.stem if path.suffix.lower() in image_extensions else path.name
    texture = icon_texture(icon_name) if path.is_file() else None
    if texture is None:
        display = Gdk.Display.get_default()
        if display is not None and not Gtk.IconTheme.get_for_display(display).has_icon(themed_name):
            found = icon_file_for(themed_name)
            texture = icon_texture(found) if found is not None else None
            if texture is None:
                themed_name = "application-x-executable"  # Not the theme's red "missing" sign.
    return Gtk.Image.new_from_paintable(texture) if texture is not None else Gtk.Image.new_from_icon_name(themed_name)


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
            texture = pixbuf_texture(pixbuf) if pixbuf is not None else None
        except GLib.Error:
            try:
                loader.close()
            except GLib.Error:
                pass
    if len(_icon_cache) > 512:
        _icon_cache.clear()
    _icon_cache[key] = texture
    return texture


def pixbuf_texture(pixbuf):
    """A texture with the pixbuf's pixels (straight alpha, as GdkPixbuf has them)."""
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), fmt,
                                 pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())


def plain_text(text, limit=300):
    """Text with no markup or control characters, for notifications."""
    text = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in str(text))
    return re.sub(r"[<>&]", "", text)[:limit]


# Non-letter keys offered on the shortcut page, as GDK key names.
OTHER_KEYS = ("Return", "space", "BackSpace", "Delete", "Home", "End")


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
    """Free, yours (yellow), GNOME (orange) and error (red) colours. Taken
    keys are hints — they can be rebound. Some themes' red is nearly their
    orange; push it toward a clear red so errors stand out."""
    orange, red = colors["orange"], colors["red"]
    distance = abs(_hue(red) - _hue(orange))
    distance = min(distance, 1 - distance)
    if distance < 0.06:
        red = _mix(red, "#e5383b", 0.6)
    return colors["green"], colors["yellow"], orange, red


# Key buttons on the shortcut page, by who has the combo.
KEY_STYLES = {
    shortcuts.FREE: "sc-free-key", shortcuts.SAME: "sc-own-key", shortcuts.RETARGET: "sc-own-key",
    shortcuts.LAUNCHER: "sc-yours-key", shortcuts.CUSTOM: "sc-yours-key", shortcuts.DESKTOP: "sc-desktop-key",
}


class AppWindow(Adw.ApplicationWindow):
    """The app window: tells when it gets another size, so the list turns
    into folders or cards."""

    def __init__(self, on_size, **kwargs):
        super().__init__(**kwargs)
        self.on_size = on_size
        self.last_size = (0, 0)

    def do_size_allocate(self, width, height, baseline):
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        if (width, height) != self.last_size:
            self.last_size = (width, height)
            GLib.idle_add(self.on_size)


class Launcher(Adw.Application):
    def __init__(self):
        super().__init__(application_id=desktop.APP_ID)
        self.window = None
        self.apps = []
        self.hidden = load_hidden_apps(HIDDEN_FILE)
        self.show_hidden = False
        self.running_first = running_first_enabled()
        self.admin_auth_pending = False
        try:
            forget_saved_admin_action()
        except OSError:
            pass
        # "list", "groups" (collapsible groups) or "origin".
        self.view = saved_view()
        # The list | groups | origin buttons in the narrow window's header are
        # a setting, off by default; then it shows the plain list.
        self.view_buttons_on = view_buttons_shown()
        self.filter_field_on = filter_field_shown()
        # The overview (window two or more columns wide): the same entries
        # as cards, one group (or all) at a time, picked in the filter bar
        # at the bottom.
        self.overview = False
        self.overview_filter = "all"
        # "cards", or "folders": "All" shows each group as a folder of icons.
        self.wide_layout = saved_wide_layout()
        self.height_pages = []  # Sub-pages as high as the list.
        self.card_w, self.list_h = CARD_WIDTH, LIST_HEIGHT
        self.groups_all = groups.load()
        self.group_name_target = None  # (group id or None, app to put in it or None)
        self.details_token = 0
        self.process_names = set()
        self.theme = ThemePalette()
        self.search_query = ""
        self.mark_filter = None  # A legend entry: only entries with that mark.
        self.shortcut_state = shortcuts.load_state()
        self.list_bindings = None  # Snapshot for the list chips, refreshed on open.
        self.shortcut_app = None
        self.shortcut_bindings = None
        self.actions_popover = None
        self.app_monitors = []
        self.apps_changed_source = 0
        # Available updates (updates.check), refreshed in the background.
        self.updates = {"apps": {}, "flatpaks": {}}
        self.updates_at = None
        self.updates_running = False
        self.seen = newapps.load()  # When each entry was first seen (the new star).
        self.icon_overrides = icons.load()  # Icons chosen by hand (Change icon…).
        self.icon_app = None
        self.icon_search_token = 0
        self.icon_tmp = None
        self.favorites = desktop.favorites()  # Pinned to the Dash.

    @property
    def groups(self):
        return self.groups_all["apps"]

    def do_activate(self):
        if self.window is None:
            self.build_window()
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda *_: sys.exit(0))
            GLib.timeout_add(100, lambda: True)  # Let Python see the signals.
            GLib.timeout_add_seconds(1, self.poll)
            self.watch_app_dirs()
            self.watch_favorites()
        self.open()

    def build_window(self):
        settings = Gtk.Settings.get_default()
        if settings is not None:
            # No middle-click paste anywhere in the window (see guard_text_field).
            settings.set_property("gtk-enable-primary-paste", False)
        window = AppWindow(self.on_window_size, application=self)
        width, height = saved_size("window") or self.default_window_size()
        window.set_default_size(width, height)
        if saved_size("window_maximized"):
            window.maximize()
        window.connect("close-request", self.on_close_request)
        window.set_title("Launcher")
        window.set_name("launcher-window")
        window.set_icon_name(desktop.APP_ID)

        overlay = Gtk.Overlay()
        overlay.add_css_class("launcher-overlay")
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_halign(Gtk.Align.FILL)
        overlay.set_valign(Gtk.Align.FILL)

        # The card is the window's content; the window manager sizes and moves it.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("launcher-card")
        card.add_css_class("launcher-windowed")
        card.set_size_request(CARD_WIDTH, -1)
        card.set_vexpand(True)
        overlay.set_child(card)

        header = Gtk.Box(spacing=8)
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
        title_side = Gtk.Box(spacing=8)  # Back button and title.
        title_side.set_hexpand(True)
        title_side.append(back_button)
        title = Gtk.Label(label=_("Apps"))
        title.add_css_class("launcher-title")
        # FILL + xalign 0, not START: START gives the label only its natural
        # width, which GTK measures too short for letter-spaced section titles.
        title.set_xalign(0)
        title.set_halign(Gtk.Align.FILL)
        title.set_hexpand(True)
        title.set_ellipsize(3)
        title.set_valign(Gtk.Align.CENTER)
        title_side.append(title)
        # What the dots, stars and badges on the icons mean, in a popover
        # drawn with the same marks.
        legend_button = Gtk.MenuButton(icon_name="help-about-symbolic")
        legend_button.add_css_class("flat")
        legend_button.add_css_class("launcher-header-btn")
        legend_button.set_tooltip_text(_("What the marks mean"))
        legend_button.set_create_popup_func(lambda button: button.set_popover(self.legend_popover()))
        header.append(legend_button)
        self.legend_button = legend_button
        # Running apps in a group of their own on top. A dot, like the run
        # dot on the icons (the play icon is the Open action).
        running_button = Gtk.Button(icon_name="media-record-symbolic")
        running_button.add_css_class("flat")
        running_button.add_css_class("launcher-header-btn")
        running_button.add_css_class("launcher-running-btn")
        running_button.connect("clicked", self.toggle_running_first)
        header.append(running_button)
        self.running_button = running_button
        # List, groups or origin, as a three-part switch; the shown one is marked.
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
        hidden_button = Gtk.Button(icon_name="view-reveal-symbolic")
        hidden_button.set_tooltip_text(_("Show hidden apps"))
        hidden_button.add_css_class("flat")
        hidden_button.add_css_class("launcher-header-btn")
        hidden_button.connect("clicked", self.toggle_hidden)
        header.append(hidden_button)
        settings_button = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_button.set_tooltip_text(_("Settings"))
        settings_button.add_css_class("flat")
        settings_button.add_css_class("launcher-header-btn")
        settings_button.connect("clicked", lambda *_: self.open_settings_page())
        header.append(settings_button)
        self.settings_button = settings_button
        # The header row is the header bar's content, beside the window buttons.
        header_bar = Gtk.Box()
        header_bar.append(title_side)
        header_bar.append(header)
        header_bar.set_hexpand(True)
        header_bar.add_css_class("launcher-headerbar-content")
        bar = Adw.HeaderBar()
        bar.add_css_class("launcher-headerbar")
        # The window buttons go into the row itself: with buttons beside it,
        # the title widget is centred between them and the window edge,
        # which moves the title right of the filter field below.
        bar.set_show_start_title_buttons(False)
        bar.set_show_end_title_buttons(False)
        header_bar.append(Gtk.WindowControls(side=Gtk.PackType.END))
        # The header bar pads 6 px; the card below pads 17 px (CSS
        # .launcher-windowed): the title starts where the filter field does.
        header_bar.set_margin_start(11)
        header_bar.set_margin_end(11)
        bar.set_title_widget(header_bar)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(bar)
        toolbar.set_content(overlay)
        window.set_content(toolbar)
        self.header_bar = header_bar

        scroller = Gtk.ScrolledWindow()
        # A fixed height        scroller = Gtk.ScrolledWindow()
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
        # Each page its own width: the wide settings page (options and their
        # preview side by side) must not keep the window from getting narrow.
        stack.set_hhomogeneous(False)
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
        filter_entry.get_delegate().set_max_length(200)
        filter_entry.connect("search-changed", self.on_filter_entry_changed)
        filter_entry.connect("activate", lambda *_a: self.selected_item() is not None
                             and self.activate_tile(self.selected_item()))
        # Esc in the field: clear it, then leave the folder, then close.
        filter_entry.connect("stop-search", lambda *_a: self.on_escape())
        self.guard_text_field(filter_entry)
        self.filter_entry = filter_entry
        # × hides the field for good (Settings › Filter field brings it back).
        hide_filter = Gtk.Button(icon_name="window-close-symbolic")
        hide_filter.add_css_class("flat")
        hide_filter.add_css_class("launcher-header-button")
        hide_filter.set_valign(Gtk.Align.CENTER)
        hide_filter.set_focus_on_click(False)
        hide_filter.set_tooltip_text(_("Hide the filter field. Typing still filters; Settings can show it again."))
        hide_filter.connect("clicked", lambda *_a: self.set_filter_field(False))
        filter_row = Gtk.Box(spacing=6)
        filter_row.append(filter_entry)
        filter_row.append(hide_filter)
        filter_row.set_visible(False)
        self.filter_row = filter_row
        empty = Gtk.Label(label=_("Nothing to show here"))
        empty.add_css_class("sc-empty")
        empty.set_margin_top(40)
        empty.set_visible(False)
        grid_box.append(empty)
        grid_scroller.set_child(grid_box)
        stack.add_named(grid_scroller, "overview")
        card.insert_child_after(filter_row, None)
        stack.connect("notify::visible-child-name", lambda *_a: self.sync_filter_entry())

        # Under the overview: one button per group.
        filter_bar = Gtk.Box(spacing=8)
        filter_bar.add_css_class("launcher-filter-bar")
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

        self.window, self.overlay, self.card = window, overlay, card
        self.rows, self.scroller = rows, scroller
        self.title, self.hidden_button = title, hidden_button
        self.back_button, self.stack = back_button, stack
        self.build_shortcut_page()
        self.build_settings_page()
        self.build_group_name_page()
        self.build_details_page()
        self.build_icon_page()
        self.apply_theme()
        self.resize_to(*(saved_size("window") or self.default_window_size()))

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
    def default_window_size():
        """The app window at first start: wide enough for folders."""
        return 860, 720

    def on_window_size(self):
        """The window got another size: the card fills it, so the list
        turns into folders or cards."""
        if self.window is None:
            return GLib.SOURCE_REMOVE
        # The card's width, not the window's: that includes the shadow GNOME
        # draws around client-side decorated windows.
        width = self.card.get_width() if hasattr(self, "card") else 0
        width = width if width > 0 else self.window.get_width()
        height = self.stack.get_height() if hasattr(self, "stack") else 0
        if width > 0:
            self.resize_to(width, height if height > 0 else LIST_HEIGHT)
        return GLib.SOURCE_REMOVE

    def on_close_request(self, window):
        """The app window closes: remember its size (not while maximized)."""
        try:
            maximized = window.is_maximized()
            set_saved_size("window_maximized", (1, 1) if maximized else None)
            if not maximized:
                width, height = window.get_default_size()
                if width > 0 and height > 0:
                    set_saved_size("window", (width, height))
        except OSError:
            pass
        return False

    def page_height(self):
        return self.list_h

    def resize_to(self, width, height):
        """The window's width and the page's height decide the layout: the
        list, or cards or folders once two fit side by side. The pages only
        keep a small minimum, so the window can shrink again."""
        self.card_w = max(CARD_WIDTH, int(width))
        self.list_h = max(MIN_LIST_HEIGHT, int(height))
        self.card.set_size_request(CARD_WIDTH, -1)
        for scroller in (self.scroller, self.grid_scroller):
            scroller.set_max_content_height(-1)
            scroller.set_min_content_height(MIN_LIST_HEIGHT)
            scroller.set_vexpand(True)
        for page in self.height_pages:
            if isinstance(page, Gtk.ScrolledWindow):
                page.set_max_content_height(-1)
                page.set_min_content_height(MIN_LIST_HEIGHT)
            else:
                page.set_size_request(-1, MIN_LIST_HEIGHT)
            page.set_vexpand(True)
        if self.shortcut_app is not None:
            self.sc_page.set_size_request(-1, MIN_LIST_HEIGHT)
        self.apply_grid_columns()
        self.refit_folders()
        if hasattr(self, "new_group_label"):
            self.new_group_label.set_visible(self.card_w >= 700)
        overview = grid_columns(self.card_w) > 1
        if overview != self.overview:
            self.set_overview(overview)
        self.sync_previews()

    def apply_grid_columns(self):
        """Folders or cards per row at the current width."""
        if self.showing_folders():
            columns = folder_fit(getattr(self, "folder_count", 0), self.card_w, self.list_h)[0]
        else:
            columns = grid_columns(self.card_w)
        if columns != getattr(self, "grid_columns", None):
            # Only a cap: a minimum would keep the window from getting
            # narrower again (back to fewer columns and the list).
            self.grid.set_min_children_per_line(1)
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
        """List (one column) or overview (cards): follows the window's width.
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
        return self.app_state(app) != "stopped"

    def overview_sections(self, entries):
        """[(filter id, label, entries)] for the filter bar: all, running,
        then the groups."""
        result = [("all", _("All"), entries)]
        running = [a for a in entries if self.overview_running(a)]
        if running:
            result.append((groups.RUNNING, groups.display_name(groups.RUNNING), running))
        for gid, members in groups.sections(entries, self.groups):
            # Every group stays, empty or not, also while searching: each is
            # a place to drag a card to. Other only when something is in it
            # or a card could go back there.
            if members or gid != groups.OTHER or self.filtering():
                result.append((gid, groups.display_name(gid, self.groups), members))
        return result

    def refresh_overview(self, entries, selected_id):
        sections = self.overview_sections(entries)
        wanted = self.overview_filter
        if self.filtering():
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
        if wanted == "all" and self.running_first:
            # Stable sort: running apps first, each part in the usual order.
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
        self.filter_row.set_visible(self.filter_field_on and self.overview
                                    and self.wide_layout == "folders" and self.main_page_open())
        if entry.get_text() != self.search_query:
            self.filter_syncing = True
            entry.set_text(self.search_query)
            entry.set_position(-1)
            self.filter_syncing = False

    def filtering(self):
        """Typing to filter, or a legend entry chosen: the results as cards."""
        return bool(self.search_query.strip() or self.mark_filter)

    def on_escape(self):
        if self.search_query or self.mark_filter:
            self.search_query = ""
            self.mark_filter = None
            self.refresh_list()
        elif self.open_folder():
            self.leave_folder()
        else:
            self.hide()

    def filter_has_focus(self):
        entry = getattr(self, "filter_entry", None)
        return entry is not None and self.filter_row.get_visible() and entry.get_delegate().has_focus()

    def showing_folders(self, wanted=None):
        """The folder layout's start page: "All", not typing to filter."""
        if wanted is None:
            wanted = self.overview_filter
        return (self.overview and self.wide_layout == "folders" and wanted == "all"
                and not self.filtering())

    def open_folder(self):
        """In a folder of the folder layout (Esc / Backspace go back)."""
        return (self.overview and self.wide_layout == "folders" and not self.filtering()
                and self.overview_filter != "all")

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
        self.overview_filter = "all"
        before = self.overview
        self.resize_to(self.card_w, self.list_h)
        if self.overview and before:
            self.refresh_list()

    def corner_labels(self):
        rounding = getattr(self, "window_rounding", None)
        like = _("Like windows ({0} px)").format(rounding) if rounding is not None else _("Like windows")
        return [like, _("Rounded"), _("Square")]

    @staticmethod
    def corners_tip():
        return _("Corners of the folders, cards and menus. “Like windows” follows GNOME's rounded window corners.")

    def set_corners(self, value):
        try:
            set_corners(value)
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))
        self.apply_theme()

    def sync_corners_control(self):
        """Show the setting's current value."""
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
        gid = self.overview_filter
        self.overview_filter = "all"
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
                hidden = app["desktop_id"] in self.hidden
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
                if hidden:
                    # Shown only with "show hidden": faded, grey, dashed, with
                    # the crossed-out eye.
                    icon.add_css_class("launcher-folder-hidden-icon")
                    button.add_css_class("launcher-folder-hidden")
                    mark = Gtk.Image.new_from_icon_name("view-conceal-symbolic")
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
                if hidden:
                    tip += " (" + _("Hidden") + ")"
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
        icon = app_image(app.get("icon") or "application-x-executable")
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
        gid = groups.group_of(app, self.groups) if app is not None else None
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
        if gid in getattr(self, "filter_buttons", {}) and self.filtering():
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
        if self.filtering():
            self.search_filter = gid  # For this search only.
            self.refresh_list(selected_id=self.row_key(self.selected_item()))
            return
        if self.overview_filter == gid:
            return
        self.overview_filter = gid
        selected = self.row_key(self.selected_item())
        self.refresh_list(selected_id=selected)

    def next_overview_filter(self, step=1):
        ids = getattr(self, "filter_ids", None) or ["all"]
        current = ((getattr(self, "search_filter", None) or "all") if self.filtering()
                   else self.overview_filter)
        position = ids.index(current) if current in ids else 0
        self.set_overview_filter(ids[(position + step) % len(ids)])

    def make_tile(self, app):
        """One card of the overview: icon with the run dot, ☰ for the
        actions (also right click), name, description, shortcut and badges."""
        is_hidden = app["desktop_id"] in self.hidden
        tile = Gtk.FlowBoxChild()
        tile.app = app
        tile.add_css_class("launcher-tile")
        if is_hidden:
            tile.add_css_class("launcher-row-hidden")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=6)
        icon = self.app_icon(app, 44)
        if is_hidden:
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
        actions.set_tooltip_text(_("App actions"))
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
        if self.is_pinned(app):
            foot.append(self.pin_mark())
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
        # Some compositors preselect MOVE: accept both, or the drop is refused.
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
            if not accepts() or dragged is None:
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
        if not self.own_card_drag(drop) or app is None:
            drop.finish(0)
            return False
        drop.finish(Gdk.DragAction.COPY)
        if gid is None:
            # After the drop has finished: the name page takes the keyboard.
            GLib.idle_add(lambda: self.open_group_name_page(None, app) and False)
        else:
            if self.filtering():
                # Dropped while typing to filter: once the filter is cleared,
                # show the group it went to, with the card selected.
                self.overview_filter = gid
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

        # Corners: like GNOME's windows (the default), or rounded / square.
        self.window_rounding = desktop.window_rounding()
        radius = corners.radius(saved_corners(), self.window_rounding)
        # For the settings previews (drawn every frame: no file reads there).
        self.corner_radius = radius
        sel_hex = colors.get("selection", "#45475a")
        bg_hex = colors.get("background", "#1e1e2e")
        fg_hex = colors.get("foreground", "#cdd6f4")
        muted_hex = colors.get("muted", "#888894")
        # A dimmer grey for small labels: the foreground's HSV value divided by 1.45.
        try:
            r, g, b = (int(fg_hex.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
            h, sat, val = colorsys.rgb_to_hsv(r, g, b)
            dim_hex = "#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, sat, val / 1.45))
        except (ValueError, IndexError):
            dim_hex = muted_hex
        free_hex, taken_hex, desktop_hex, error_hex = key_owner_colors(colors)
        # Menus: a little lifted from the window, in light and dark alike.
        menu_bg = _mix(bg_hex, fg_hex, 0.05)

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
            background-color: {menu_bg};
            color: {fg_hex};
            border: 1px solid alpha({colors['accent']}, 0.6);
            border-radius: {radius}px;
            padding: 5px;
            min-width: 170px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
        }}
        popover.launcher-popover arrow, popover arrow {{
            background-color: {menu_bg};
            border-color: alpha({colors['accent']}, 0.6);
        }}
        .launcher-back-btn {{ padding: 2px 6px; }}
        .launcher-back-bordered {{ border: 1px solid alpha({colors['foreground']}, 0.35); border-radius: {radius}px; padding: 4px 10px; }}
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
            color: {fg_hex};
        }}
        .launcher-menu-item label {{
            font-size: 13px;
            font-weight: 500;
            color: {fg_hex};
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
            color: {colors['red']};
        }}
        .launcher-menu-item.destructive image {{
            color: {colors['red']};
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
        .launcher-pin-mark {{ color: {colors['accent']}; }}
        .launcher-legend-text {{ font-size: 13px; }}
        button.launcher-legend-row {{ padding: 5px 8px; }}
        /* In a button libadwaita's label colour wins over a plain class. */
        button label.launcher-new-star, button label.launcher-new-star-inline {{ color: {colors['yellow']}; }}
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
        .launcher-card button.sc-keychip.sc-desktop-key, .launcher-card button.sc-keychip.sc-desktop-key label {{ color: {desktop_hex}; border-color: alpha({desktop_hex}, 0.6); }}
        .launcher-card button.sc-keychip.sc-own-key, .launcher-card button.sc-keychip.sc-own-key label {{ color: {colors['foreground']}; border-color: {colors['accent']}; }}
        .sc-keychip.sc-free-key:hover {{ background: alpha({free_hex}, 0.15); }}
        .sc-keychip.sc-yours-key:hover {{ background: alpha({taken_hex}, 0.15); }}
        .sc-keychip.sc-desktop-key:hover {{ background: alpha({desktop_hex}, 0.15); }}
        .launcher-card button.sc-keychip.sc-key-picked {{ background: alpha({colors['foreground']}, 0.18); border-width: 2px; }}
        .sc-legend {{ font-size: 10px; }}
        .launcher-card .sc-legend-free {{ color: {free_hex}; }}
        .launcher-card .sc-legend-yours {{ color: {taken_hex}; }}
        .launcher-card .sc-legend-desktop {{ color: {desktop_hex}; }}
        .sc-hint-desktop {{ color: {desktop_hex}; background: alpha({desktop_hex}, 0.12); }}
        .launcher-card .sc-hint-desktop {{ color: {desktop_hex}; }}
        .sc-primary.sc-desktop-btn {{ background: {desktop_hex}; color: {colors['background']}; }}
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
        # The card is the window's content; GNOME draws the frame and corners.
        accent = colors["accent"]
        css += f"""
        window#launcher-window {{ background: {bg_hex}; }}
        .launcher-card.launcher-windowed {{ border: none; border-radius: 0; background: transparent;
            padding: 4px 17px 17px; }}
        headerbar.launcher-headerbar {{ background: {bg_hex}; color: {fg_hex}; box-shadow: none; }}
        .launcher-headerbar-content button.launcher-header-btn {{ padding: 4px; min-width: 0; min-height: 0; }}
        .launcher-headerbar-content button.launcher-toggle-active {{ background: alpha({accent}, 0.35); }}
        .launcher-headerbar-content .launcher-view-switch button.launcher-header-btn {{ border-radius: 0; }}
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
        self.list_bindings = None
        try:
            self.apps = parse_desktop_files()
            self.prepare_entries(self.apps)
        except Exception as e:
            print("Error parsing desktop files:", e, file=sys.stderr)
        try:
            self.theme.reload()
            self.apply_theme()
        except Exception as e:
            print("Error applying theme:", e, file=sys.stderr)
        self.search_query = ""
        self.mark_filter = None
        self.update_hidden_button_state()
        try:
            self.refresh_running()
            self.refresh_list()
        except Exception as e:
            print("Error refreshing list:", e, file=sys.stderr)
        self.window.present()
        self.check_updates()

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

    def hide(self):
        """Done: an app was opened, or Esc. The window closes (and the app
        quits); the next start opens it fresh."""
        if self.actions_menu_open():
            self.actions_popover.popdown()
        if self.shortcut_app is not None:
            self.close_shortcut_page(focus_list=False)
        if self.window and self.window.get_visible():
            self.window.close()

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

    def watch_favorites(self):
        """Pinned to the Dash elsewhere (the Dash's own menu, Dash to Dock):
        the pins in the list follow."""
        settings = desktop.shell_settings()
        if settings is None:
            return
        self.shell_settings = settings  # Kept: a dropped Gio.Settings sends no signals.
        settings.connect("changed::favorite-apps", lambda *_a: self.on_favorites_changed())

    def on_favorites_changed(self):
        favorites = desktop.favorites()
        if favorites == self.favorites:
            return
        self.favorites = favorites
        self.list_bindings = None  # Super + 1…9 follow the Dash's order.
        if self.window is not None and self.main_page_open() and not self.actions_menu_open():
            self.refresh_list()

    def on_app_dir_changed(self, *_args):
        # Package managers touch many files at once; act once things settle.
        if self.apps_changed_source:
            GLib.source_remove(self.apps_changed_source)
        self.apps_changed_source = GLib.timeout_add(700, self.reload_apps)

    def reload_apps(self):
        self.apps_changed_source = 0
        if not (self.window and self.window.get_visible()):
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
            self.prepare_entries(self.apps)
            self.prune_uninstalled_shortcuts()
            self.refresh_list(selected_id=selected)
        except Exception as e:
            print("Error reloading apps:", e, file=sys.stderr)
        return GLib.SOURCE_REMOVE

    def prune_uninstalled_shortcuts(self):
        """Drop launcher shortcuts of uninstalled apps, so their keys do not
        stay dead and the bindings they replaced work again."""
        if not shortcuts.available():
            return
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

        if key in (Gdk.KEY_k, Gdk.KEY_K) and state & Gdk.ModifierType.CONTROL_MASK:
            row = self.selected_item()
            if row is not None and hasattr(row, "app"):
                self.open_shortcut_page(row.app)
            return True

        if key == Gdk.KEY_Delete:
            row = self.selected_item()
            quit_app = self.quit_action(row.app) if row is not None and hasattr(row, "app") else None
            if quit_app is not None:
                quit_app()
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
            if not (self.actions_menu_open() or getattr(self, "tile_dragging", False)):
                self.refresh_running_async()
            if self.theme.reload():
                self.apply_theme()
        return GLib.SOURCE_CONTINUE

    def refresh_running_async(self):
        """The poll's process scan on a thread; the list is rebuilt on the
        main loop when something changed."""
        if getattr(self, "running_scan", False):
            return
        self.running_scan = True

        def work():
            GLib.idle_add(done, background_process_names())

        def done(names):
            self.running_scan = False
            previous = self.process_names
            self.process_names = names
            menu_open = self.actions_menu_open() or getattr(self, "tile_dragging", False)
            if previous != names and not menu_open and self.window.is_visible():
                self.refresh_list()
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    def refresh_running(self):
        previous = self.process_names
        self.process_names = background_process_names()
        return previous != self.process_names

    def app_state(self, app):
        """"running" or "stopped", remembered until the next process scan.
        GNOME does not list other apps' windows, so a running app counts as
        open."""
        key = (id(self.process_names), id(self.apps))
        if getattr(self, "_state_key", None) != key:
            self._state_key, self._states = key, {}
        ident = app.get("desktop_id")
        if ident not in self._states:
            self._states[ident] = self._app_state(app)
        return self._states[ident]

    def _app_state(self, app):
        # A terminal app (xdg-terminal-exec -e claude) runs as its command.
        name = terminal_command(app) or app_process_name(app)
        if name in self.process_names:
            if name in TERMINALS and not self.terminal_runs_on_its_own(name):
                return "stopped"
            return "active"
        return "stopped"

    def terminal_runs_on_its_own(self, name):
        """Whether the terminal itself was opened, not only as the window of
        a command-line app (a terminal server process stays around)."""
        return bool(process_pids(name)) and not any(
            terminal_command(a) in self.process_names for a in self.apps if terminal_command(a))

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
        if self.list_bindings is None and shortcuts.available():
            self.list_bindings = shortcuts.Bindings(self.shortcut_state, favorites=self.favorites)

        def shown(app):
            # The legend's Hidden shows hidden entries without the eye.
            if app["desktop_id"] in self.hidden and not self.show_hidden and self.mark_filter != "hidden":
                return False
            if self.mark_filter and not self.has_mark(app, self.mark_filter):
                return False
            return query in app["name"].casefold()

        if self.overview:
            self.refresh_overview([a for a in self.apps if shown(a)], selected_id)
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
            entries = [a for a in self.apps if shown(a)]
            # Running apps on top when the dot is on.
            running = (lambda a: self.app_state(a) != "stopped") if self.running_first else None
            first = True
            if self.view == "origin":
                defaults = details.default_desktops()
                sections = groups.origin_sections(
                    entries, lambda a: details.origin_section(a, defaults), running)
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
            entries = self.apps
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
        is_hidden = app["desktop_id"] in self.hidden
        row = Gtk.ListBoxRow()
        row.app = app
        row.running = self.app_state(app) != "stopped"
        if is_hidden:
            row.add_css_class("launcher-row-hidden")
        line = Gtk.Box(spacing=8)
        icon = app_image(app.get("icon") or "application-x-executable")
        icon.set_pixel_size(32)
        icon.set_valign(Gtk.Align.CENTER)
        if is_hidden:
            icon.add_css_class("launcher-hidden-icon")
        # The run dot sits on the icon's corner, like the Dash's, so the
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
        if self.is_pinned(app):
            title_row.append(self.pin_mark())
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
        actions.set_tooltip_text(_("App actions"))
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
        return row

    def grouping_running(self):
        return self.view == "list" and self.running_first

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
        title = Gtk.Label(label=groups.display_name(gid, self.groups), xalign=0)
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
        """In the list with the running toggle on: a "Running" group, closed
        by a rule. The group view draws its own heading rows."""
        if self.view != "list" or not self.grouping_running() or not hasattr(row, "app"):
            row.set_header(None)
            return
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
        """Open an app and close the launcher."""
        if app.get("desktop_id") and newapps.opened(self.seen, app["desktop_id"]):
            self.save_seen()  # Opened once: no longer marked new.
        if not launch_app(app, self.launch_context()):
            self.notify(_("Could not open {0}").format(app["name"]))
            return
        self.hide()

    def launch_action(self, app, action_id):
        if not launch_action(app, action_id, self.launch_context()):
            self.notify(_("Could not open {0}").format(app["name"]))
            return
        self.hide()

    def launch_context(self):
        """The display's launch context: GNOME then gives the new window the
        focus (an activation token) instead of an "is ready" notice."""
        display = self.window.get_display() if self.window is not None else Gdk.Display.get_default()
        return display.get_app_launch_context() if display is not None else None

    def open_as_admin(self, app):
        """Open app as root: pkexec asks for the password, or sudo in a terminal."""
        process = launch_as_admin(app)
        if process is None:
            self.notify(_("Could not open {0} as administrator").format(app['name']))
            return
        # The window closes; the process stays a minute to say when the app
        # was refused or quit at once.
        self.hold()
        held = [True]

        def release():
            if held[0]:
                held[0] = False
                self.release()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add_seconds(60, release)
        self.hide()
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
            release()
        if process.args[0] == "pkexec":
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, done)
        else:
            release()

    def actions_menu_open(self):
        return self.actions_popover is not None and self.actions_popover.get_visible()

    def on_actions_closed(self, popover):
        if self.actions_popover is popover:
            self.actions_popover = None
        # Release the menu after GTK finishes dispatching the closing click.
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

    def fill_actions(self, popover, app):
        """The actions menu."""
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

        add_action(_("Open"), lambda: self.launch(app), "media-playback-start-symbolic", shortcut=chip_for(""))
        if admin_action_enabled() and admin_command(app):
            add_action(_("Open as administrator"), lambda: self.open_as_admin(app), "dialog-password-symbolic")
        for action in app.get("actions", []):
            add_action(action["name"], lambda action_id=action["id"]: self.launch_action(app, action_id),
                       "system-run-symbolic", shortcut=chip_for(action["id"]))
        flatpak = self.flatpak_update(app)
        if flatpak:
            # A Flatpak updates on its own, in the background.
            add_action(_("Update"), lambda: self.update_flatpak(app), "software-update-available-symbolic")
        elif app.get("desktop_path") and self.updates["apps"].get(app["desktop_path"]):
            # A package is never updated on its own on Arch (partial upgrades
            # break things): the whole system is, in a terminal.
            add_action(_("Update system…"), self.update_system, "software-update-available-symbolic")
        add_separator()
        if shortcuts.available():
            add_action(_("Shortcut…"), lambda: self.open_shortcut_page(app),
                       "preferences-desktop-keyboard-shortcuts-symbolic")
        add_action(_("Group: {0}").format(groups.display_name(groups.group_of(app, self.groups), self.groups)),
                   lambda: self.fill_group_choice(popover, app), "folder-symbolic",
                   keep_open=True, end_icon="go-next-symbolic")
        add_action(_("Details…"), lambda: self.open_details_page(app), "dialog-information-symbolic")
        add_action(_("Change icon…"), lambda: self.open_icon_page(app), "image-x-generic-symbolic")
        if self.dash_id(app):
            if self.is_pinned(app):
                add_action(_("Unpin from Dash"), lambda: self.set_pinned(app, False), "view-pin-symbolic")
            else:
                add_action(_("Pin to Dash"), lambda: self.set_pinned(app, True), "view-pin-symbolic")
        add_separator()
        hidden = app["desktop_id"] in self.hidden
        add_action(_("Unhide from list") if hidden else _("Hide from list"),
                   lambda: self.set_hidden(app, not hidden),
                   "view-reveal-symbolic" if hidden else "view-conceal-symbolic")
        quit_app = self.quit_action(app) if self.app_state(app) != "stopped" else None
        if quit_app:
            add_action(_("Quit"), quit_app, "application-exit-symbolic", is_destructive=True)
        if details.uninstall_plan(app, look_up_package=False) is not None:
            add_separator()
            add_action(_("Uninstall…"), lambda: self.confirm_uninstall(app), "user-trash-symbolic",
                       keep_open=True, css="uninstall")
        popover.set_child(box)
        self.focus_first_entry(box)

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

    def confirm_uninstall(self, app):
        """Ask in the menu, saying how: Flatpak, the package (pacman, in a
        terminal), or your own entry to the trash. A web app only its
        browser can remove: then the menu says how, without a button."""
        popover = self.actions_popover
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(8)
        plan = details.uninstall_plan(app)
        if plan is None:
            self.notify(_("Could not uninstall {0}").format(app["name"]), _("No package owns its desktop file."))
            popover.popdown()
            return
        kind, what = plan
        question = (_("How to uninstall {0}") if kind == "webapp" else _("Do you want to uninstall {0}?")).format(app['name'])
        box.append(Gtk.Label(label=question, xalign=0, wrap=True, max_width_chars=32))
        if kind == "webapp":
            text = _("{0} is a web app installed in {1}, so only {1} can uninstall it: open the app "
                     "and choose ⋮ → Uninstall, or right-click it on {2} and choose Remove.").format(
                         app['name'], what, APPS_PAGES.get(what, _("its apps page")))
        elif kind == "flatpak":
            text = _("Removes the Flatpak {0}. Its data in ~/.var/app stays.").format(what)
        elif kind == "package":
            text = _("Removes the package {0} with pacman, in a terminal that asks for your password.").format(what)
        else:
            text = _("Moves {0} to the trash.").format(what)
        note = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=32)
        note.add_css_class("sc-status")
        note.add_css_class("sc-info")
        box.append(note)
        buttons = Gtk.Box(spacing=6)
        buttons.set_halign(Gtk.Align.END)
        if kind == "webapp":
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
        confirm.connect("clicked", lambda *_: (popover.popdown(), self.uninstall_app(app, kind, what)))
        buttons.append(confirm)
        box.append(buttons)
        popover.set_child(box)
        # Enter confirms, Escape cancels; pacman still asks in its terminal.
        confirm.grab_focus()

    def uninstall_app(self, app, kind, what):
        if kind == "trash":
            try:
                Gio.File.new_for_path(what).trash(None)
            except GLib.Error as error:
                self.notify(_("Could not uninstall {0}").format(app["name"]), error.message)
                return
            self.notify(_("Uninstalled {0}").format(app["name"]), _("{0} is in the trash.").format(what))
            self.on_app_dir_changed()
            return
        if kind == "package":
            terminal = desktop.terminal_argv()
            if terminal is None:
                self.notify(_("Could not uninstall {0}").format(app["name"]), _("No terminal found."))
                return
            argv = [*terminal, "sh", "-c", 'sudo pacman -Rns -- "$1"; echo; read -r -p "Press Enter to close. " _',
                    "uninstall", what]
        else:
            argv = ["flatpak", "uninstall", "-y", "--noninteractive", what]
        try:
            process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as error:
            self.notify(_("Could not uninstall {0}").format(app["name"]), error.message)
            return
        if kind == "package":
            return  # The terminal shows how it went; the list follows the removed files.

        # A failure would otherwise pass silently: say why.
        def done(proc, result):
            try:
                _ok, output, _err = proc.communicate_utf8_finish(result)
            except GLib.Error as error:
                output = error.message
            lines = (output or "").strip().splitlines()
            if proc.get_successful():
                self.notify(_("Uninstalled {0}").format(app["name"]))
            else:
                self.notify(_("Could not uninstall {0}").format(app["name"]), lines[-1] if lines else "")

        self.notify(_("Uninstalling {0}…").format(app["name"]))
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
        members = dict(groups.sections(self.apps, self.groups)).get(gid, [])
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

    def notify(self, title, body=""):
        # Names come from desktop entries; notification daemons
        # render markup, so none of it may reach them as markup.
        try:
            subprocess.Popen(["notify-send", "-a", "Launcher", "-i", desktop.APP_ID, plain_text(title), plain_text(body)],
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
                          ("● " + _("GNOME default"), "sc-legend-desktop")):
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
        # also helps when GNOME grabs a combo before the launcher sees it.
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
            button.connect("clicked", lambda *_, k=key: self.pick_shortcut_key(self.selected_shortcut_mods(), k))
            others.append(button)
            other_buttons[key] = button
        box.append(others)

        self.stack.add_named(page, "shortcut")
        self.sc_page, self.sc_current, self.sc_current_title = page, current, current_title
        self.sc_toggles, self.sc_key_box, self.sc_status = toggles, key_box, status
        self.sc_suggest, self.sc_suggest_title, self.sc_apply_button = suggest, suggest_title, apply
        self.sc_default_check = default_check
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
        parts = shortcuts.combo_label(combo).split(" + ") if combo else []
        for part in parts:
            cap = Gtk.Label(label=part)
            cap.add_css_class("sc-cap")
            if large:
                cap.add_css_class("sc-cap-lg")
            if dim:
                cap.add_css_class("sc-cap-dim")
            caps.append(cap)
        return caps

    def build_settings_page(self):
        """Settings: a rule under the header, then label | control rows in a
        card, its animated preview beside it when the window is wide."""
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
            widget.set_size_request(165, -1)  # Long names wrap instead of widening the card.
            if tooltip:
                widget.set_tooltip_text(tooltip)
            return widget

        admin_tip = _("Adds “Open as administrator” to an app's menu. Switching it on asks for your password; it is off again after you log in next time.")
        admin_switch = Gtk.Switch(active=admin_action_enabled(), halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        admin_switch.add_css_class("launcher-compact-switch")
        admin_switch.set_tooltip_text(admin_tip)
        admin_switch.connect("notify::active", self.on_admin_action_toggled)
        grid.attach(label(_("Open as administrator"), admin_tip), 0, 1, 1, 1)
        grid.attach(admin_switch, 1, 1, 1, 1)
        layout_tip = _("How the window shows the entries once it is two columns wide or more: each group as a folder of icons (the default), or every entry as a card.")
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
        small_tip = _("How the narrow window shows the entries: as one list (the default), in groups, or by origin.")
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
        views_tip = _("Buttons in the narrow window's header to switch between groups, list and origin (Ctrl+G does the same).")
        views_switch = Gtk.Switch(active=self.view_buttons_on, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        views_switch.add_css_class("launcher-compact-switch")
        views_switch.set_tooltip_text(views_tip)
        views_switch.connect("notify::active", lambda sw, _p: self.set_view_buttons(sw.get_active()))
        grid.attach(label(_("View buttons when small"), views_tip), 0, 4, 1, 1)
        grid.attach(views_switch, 1, 4, 1, 1)
        self.views_switch = views_switch
        filter_tip = _("The field above the folders when wide. Without it, typing still filters and the title shows what you typed.")
        filter_switch = Gtk.Switch(active=self.filter_field_on, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        filter_switch.add_css_class("launcher-compact-switch")
        filter_switch.set_tooltip_text(filter_tip)
        filter_switch.connect("notify::active", lambda sw, _p: self.set_filter_field(sw.get_active()))
        grid.attach(label(_("Filter field"), filter_tip), 0, 7, 1, 1)
        grid.attach(filter_switch, 1, 7, 1, 1)
        self.filter_switch = filter_switch
        topbar_tip = _("An icon in GNOME's top bar that opens Launcher, or closes it while it is open.")
        topbar_switch = Gtk.Switch(active=topbar.enabled(), halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        topbar_switch.add_css_class("launcher-compact-switch")
        topbar_switch.set_tooltip_text(topbar_tip)
        topbar_switch.connect("notify::active", self.on_topbar_toggled)
        grid.attach(label(_("Top bar icon"), topbar_tip), 0, 8, 1, 1)
        grid.attach(topbar_switch, 1, 8, 1, 1)
        self.topbar_switch = topbar_switch
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
        layout_sketch.set_tooltip_text(_("Preview: the narrow list, then the wide window."))
        # The options in a card, its animated preview to the right of the
        # card (wide window) or a hint under it (narrow window).
        sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sections.append(self.settings_section(grid, layout_sketch, self.reset_general_settings))
        # The options grew taller than the window: they scroll.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        sections.set_margin_end(6)
        scroller.set_child(sections)
        page.append(scroller)
        self.admin_switch = admin_switch
        self.stack.add_named(page, "settings")

    def settings_section(self, grid, sketch, reset=None):
        """[card with the options | animated preview], and the hint about the
        preview under the card while the window is narrow."""
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
        hint = Gtk.Label(label=_("Make the window wider to see an animated preview of these options."),
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
        """Open as administrator and the top bar icon: off; layouts: folders
        when wide, the list when narrow; corners like the windows."""
        if self.admin_switch.get_active():
            self.admin_switch.set_active(False)  # Its handler switches the action off properly.
        self.set_wide_layout("folders")
        self.toggle_view("list")
        self.set_filter_field(True)
        self.topbar_switch.set_active(False)
        self.corners_dropdown.set_selected(0)  # "Like windows": saves and redraws.

    def sync_previews(self):
        wide = getattr(self, "overview", False)
        for sketch, hint in getattr(self, "preview_slots", []):
            sketch.set_visible(wide)
            hint.set_visible(not wide)

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

    def set_topbar_switch(self, active):
        """Move the switch without treating it as the user's choice."""
        self.topbar_syncing = True
        self.topbar_switch.set_active(active)
        self.topbar_syncing = False

    def on_topbar_toggled(self, switch, _param):
        """The top bar icon is a GNOME Shell extension shipped with Launcher:
        linked into place, then switched on or off like in the Extensions app."""
        if getattr(self, "topbar_syncing", False):
            return
        on = switch.get_active()
        try:
            if on:
                topbar.install()
            done = topbar.set_enabled(on)
        except (OSError, GLib.Error) as error:
            self.notify(_("The top bar icon was not switched on") if on else _("The top bar icon was not switched off"),
                        str(getattr(error, "message", error)))
            self.set_topbar_switch(topbar.enabled())
            return
        if not done:
            self.notify(_("No top bar icon"), _("GNOME Shell's settings were not found."))
            self.set_topbar_switch(False)
            return
        if not on:
            return
        if topbar.user_extensions_off():
            self.notify(_("The top bar icon is hidden"),
                        _("Extensions are switched off in the Extensions app; switch them on to see it."))
        elif topbar.known_to_shell() is False:
            self.notify(_("The top bar icon appears after you log out and back in"),
                        _("GNOME Shell finds a new extension only when it starts."))

    def settings_open(self):
        return self.stack.get_visible_child_name() == "settings"

    def open_settings_page(self):
        if self.shortcut_app is not None:
            self.close_shortcut_page(focus_list=False)
        self.back_chip.set_label("· ⌫")
        self.back_chip.remove_css_class("launcher-shortcut-chip")
        self.back_button.add_css_class("launcher-back-bordered")
        self.title.add_css_class("launcher-section-title")
        self.back_button.set_tooltip_text(_("Back (Backspace, Esc)"))
        self.hide_main_header()
        self.set_admin_switch(admin_action_enabled())  # Off again in a new session.
        self.set_topbar_switch(topbar.enabled())  # Maybe switched in the Extensions app.
        self.sync_corners_control()
        self.stack.set_visible_child_name("settings")
        self.update_title()
        self.admin_switch.grab_focus()

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
        for button in (self.hidden_button, self.running_button, self.view_button,
                       self.settings_button, self.filter_bar):
            button.set_visible(False)

    def show_main_header(self):
        """The list's header buttons. The overview filters by group in its
        bottom bar instead of the list | groups | origin switch."""
        self.back_button.set_visible(False)
        for button in (self.hidden_button, self.settings_button, self.running_button):
            button.set_visible(True)
        self.view_button.set_visible(not self.overview and self.view_buttons_on)
        self.filter_bar.set_visible(self.overview)

    def sync_small_dropdown(self):
        dropdown = getattr(self, "small_dropdown", None)
        if dropdown is not None and dropdown.get_selected() != self.small_views.index(self.view):
            self.view_syncing = True
            dropdown.set_selected(self.small_views.index(self.view))
            self.view_syncing = False
        return GLib.SOURCE_REMOVE

    def set_filter_field(self, shown):
        if shown == self.filter_field_on:
            return
        had_focus = self.filter_has_focus()
        self.filter_field_on = shown
        try:
            set_filter_field_shown(shown)
        except OSError as error:
            self.notify(_("Could not save the setting"), str(error))
        if getattr(self, "filter_switch", None) is not None and self.filter_switch.get_active() != shown:
            self.filter_switch.set_active(shown)
        self.sync_filter_entry()
        if had_focus:
            self.grid.grab_focus()  # Typing goes on in the folders.

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
            self.overview_filter = gid if app is not None else "all"
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
        icons.apply(self.apps, self.icon_overrides)
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
                facts = details.app_facts(app)
                facts["origin"] = details.origin_section(app, details.default_desktops())
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
                 "flatpak": _("Flatpak or Snap app"), "appimage": _("AppImage")}
        origins = {"defaults": _("One of your default apps"), "system": _("Installed system-wide"),
                   "flatpak": _("Flatpak"), "user": _("Your own, in your home folder")}
        roles = {"browser": _("Web browser"), "terminal": _("Terminal"), "email": _("Email"),
                 "files": _("Files"), "editor": _("Text editor"), "images": _("Images"),
                 "video": _("Videos"), "music": _("Music"), "pdf": _("PDF documents")}
        rows = [(_("Type"), kinds.get(facts.get("kind"), "")),
                (_("Origin"), origins.get(facts.get("origin"), "")),
                (_("Default app for"), ", ".join(roles[r] for r in facts.get("roles", ()) if r in roles)),
                (_("Group"), groups.display_name(groups.group_of(app, self.groups), self.groups)),
                (_("Pinned to the Dash"), _("Yes") if self.is_pinned(app) else ""),
                (_("ID"), facts.get("id", ""))]
        rows += [(_("Desktop file"), facts.get("desktop_file", ""), True),
                 (_("Command"), facts.get("command", "")),
                 (_("Program"), facts.get("program") or "", True)]
        package = facts.get("package")
        if package:
            rows += [(_("Package"), f"{package['name']} {package.get('version', '')}".strip()),
                     (_("Installed size"), GLib.format_size(package["size"]) if "size" in package else ""),
                     (_("Installed or updated"), self.format_date(package.get("installed"))),
                     (_("Website"), package.get("url", ""), True)]
        if facts.get("website"):
            rows.append((_("Website"), facts["website"], True))
        if "file_size" in facts:
            rows += [(_("File size"), GLib.format_size(facts["file_size"])),
                     (_("Last changed"), self.format_date(facts.get("modified")))]
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
        """Open the address in the default browser. The window stays open:
        the browser taking the focus must not close it."""
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
        self.sc_page.set_size_request(-1, self.page_height())
        self.shortcut_app = app
        self.sc_app_id = shortcuts.app_key(app)
        self.shortcut_state = shortcuts.load_state()
        self.shortcut_bindings = shortcuts.Bindings(self.shortcut_state, favorites=self.favorites)
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
        # Bound combos must reach the capture field instead of running: GNOME
        # asks once whether the launcher may take over the shortcuts.
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
        Remembered per shortcut state and bindings snapshot (one per refresh).
        No bindings (GNOME's keybinding settings missing): no combos."""
        if bindings is None:
            return []
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
                note += " · " + _("duplicate of another shortcut — remove it here to tidy up")
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
            origin = _("GNOME default") if bind["file"] == "GNOME" else _("Your custom shortcut")
            note = _("Opens {0}").format(self.shortcut_target(app, bind)) + f" · {origin}"
            if bind["description"] and bind["description"] != self.shortcut_target(app, bind):
                note += f" “{bind['description']}”"
            item(bind["combo"], bind["file"], note, "×",
                 _("Turn off — you can turn it back on here"),
                 lambda b=bind: self.disable_external_shortcut(b),
                 change=lambda b=bind: self.start_change_shortcut({**b["combo"], "action": b["action"]}, b))
            active += 1
        if not active:
            empty = Gtk.Label(label=_("None yet — add one below."), xalign=0)
            empty.add_css_class("sc-empty")
            self.sc_current.append(empty)
        for combo in entry["disabled"]:
            was = " · " + _("was “{0}”").format(combo['description']) if combo.get("description") else ""
            item(combo, combo.get("source") or "GNOME", _("Turned off") + was, _("Turn on"),
                 _("Use this shortcut again"), lambda c=combo: self.restore_external_shortcut(c),
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
        return self.shortcut_bindings.classify(combo, self.sc_app_id, self.sc_keycode,
                                               app=self.shortcut_app, action=action)

    def start_change_shortcut(self, combo, bind):
        """Pick a new key for an existing shortcut with the Add controls.
        bind is the external binding, or None for a launcher shortcut."""
        self.sc_editing = {"combo": combo, "bind": bind}
        self.sync_change_mode()
        self.render_shortcut_current()
        self.set_shortcut_action(combo.get("action", ""))
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
            if mods:
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
        picked = bool(self.sc_key) and combo["key"].casefold() == self.sc_key.casefold()
        (chip.add_css_class if picked else chip.remove_css_class)("sc-key-picked")
        label = shortcuts.combo_label(combo)
        if level == shortcuts.FREE:
            tip = _("{0} — free").format(label)
        elif level in (shortcuts.SAME, shortcuts.RETARGET):
            tip = _("{0} — already opens {1}").format(label, self.shortcut_app['name'])
        elif level == shortcuts.DESKTOP:
            tip = _("{0} — GNOME's “{1}”; click to rebind").format(label, owner)
        elif level == shortcuts.LAUNCHER:
            tip = _("{0} — opens {1}; click to move it here").format(label, owner)
        else:
            tip = _("{0} — your custom shortcut “{1}”; click to rebind").format(label, owner)
        chip.set_tooltip_text(tip)

    def pick_shortcut_key(self, mods, key):
        """Pick a key by clicking it; clicking the picked key again clears
        it, the way a second click turns a modifier off."""
        combo, current = shortcuts.make_combo(mods, key), self.current_shortcut_combo()
        if combo and current and shortcuts.combo_id(combo) == shortcuts.combo_id(current):
            self.set_shortcut_combo(mods, None, None)
        else:
            self.set_shortcut_combo(mods, key, None)

    def set_shortcut_combo(self, mods, key, keycode):
        self.sc_syncing = True
        for mod, toggle in self.sc_toggles.items():
            toggle.set_active(mod in mods)
        self.sc_syncing = False
        self.sc_key, self.sc_keycode = key, keycode
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
        return shortcuts.make_combo(mods, self.sc_key)

    def set_shortcut_status(self, text, level, button_label=None, button_style=None, enabled=False):
        for css in ("sc-free", "sc-warn", "sc-hint-desktop", "sc-danger", "sc-info"):
            self.sc_status.remove_css_class(css)
        self.sc_status.add_css_class(level)
        self.sc_status.set_label(text)
        for css in ("sc-warn-btn", "sc-desktop-btn", "sc-danger-btn"):
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
            self.set_shortcut_status(_("Your custom shortcut “{0}”. Assigning turns it off while this shortcut "
                                       "exists; removing the shortcut brings it back.").format(owner),
                                     "sc-warn", N_("Rebind"), "sc-warn-btn", True)
        else:
            self.set_shortcut_status(_("GNOME's “{0}”. Assigning turns it off while this shortcut "
                                       "exists; removing the shortcut brings it back.").format(owner),
                                     "sc-hint-desktop", N_("Rebind"), "sc-desktop-btn", True)

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
        if not mods:
            mods = [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]
        self.set_shortcut_combo(mods, name, keycode)
        return True

    def commit_shortcuts(self, new_state, message):
        new_state["apps"] = {k: v for k, v in new_state["apps"].items() if v["shortcuts"] or v["disabled"]}
        try:
            shortcuts.save_and_apply(new_state)
        except (RuntimeError, OSError, GLib.Error) as error:
            first = str(error).strip().splitlines()[0] if str(error).strip() else _("unknown error")
            self.set_shortcut_status(_("Not applied: {0}").format(first), "sc-danger")
            return False
        self.shortcut_state = shortcuts.load_state()
        self.shortcut_bindings = shortcuts.Bindings(self.shortcut_state, favorites=self.favorites)
        self.list_bindings = self.shortcut_bindings
        self.sc_key, self.sc_keycode = None, None
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
        taken = level in (shortcuts.CUSTOM, shortcuts.DESKTOP)
        replaces = owner if taken else ""
        taken_from = self.shortcut_bindings.taken_from(combo) if taken else ""
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
                entry["disabled"].append({**shortcuts.make_combo(old["mods"], old["key"]),
                                          "description": bind["description"], "source": bind["file"],
                                          "where": bind["where"]})
            message = (_("✓ {0} now opens {1}.").format(shortcuts.combo_label(combo), target) if old_id == cid else
                       _("✓ Changed {0} to {1}.").format(shortcuts.combo_label(old), shortcuts.combo_label(combo)))
        added = {**combo, "replaces": replaces, "taken_from": taken_from, "action": action}
        if action:
            added["label"] = shortcuts.action_name(self.shortcut_app, action)  # The name in GNOME Settings.
        entry["shortcuts"].append(added)
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
        entry["disabled"].append({**bind["combo"], "description": bind["description"], "source": bind["file"],
                                  "where": bind["where"]})
        self.commit_shortcuts(new_state, _("✓ Turned off {0}. Turn it on again any time.").format(
            shortcuts.combo_label(bind['combo'])))

    def restore_external_shortcut(self, combo):
        new_state = copy.deepcopy(self.shortcut_state)
        cid = shortcuts.combo_id(combo)
        entry = self.shortcut_entry(new_state)
        entry["disabled"] = [c for c in entry["disabled"] if shortcuts.combo_id(c) != cid]
        self.commit_shortcuts(new_state, _("✓ {0} is on again.").format(shortcuts.combo_label(combo)))

    def set_hidden(self, app, hidden):
        hidden_ids = self.hidden
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
        save_hidden_apps(hidden_ids, HIDDEN_FILE)
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
        label = _("Apps")
        if self.show_hidden:
            label += " (" + _("Showing hidden") + ")"
        if self.mark_filter:
            label += f" — {self.mark_label(self.mark_filter)}"
        if self.search_query:
            label += f" — {self.search_query}"
        self.title.set_label(label)

    def update_hidden_button_state(self):
        if not hasattr(self, "hidden_button"):
            return
        if self.show_hidden:
            self.hidden_button.set_icon_name("view-conceal-symbolic")
            self.hidden_button.set_tooltip_text(_("Hide hidden apps"))
            self.hidden_button.add_css_class("launcher-toggle-active")
        else:
            self.hidden_button.set_icon_name("view-reveal-symbolic")
            self.hidden_button.set_tooltip_text(_("Show hidden apps"))
            self.hidden_button.remove_css_class("launcher-toggle-active")
        self.view_buttons["list"].set_tooltip_text(_("List view (Ctrl+G)"))
        self.view_buttons["groups"].set_tooltip_text(_("Group view (Ctrl+G)"))
        self.view_buttons["origin"].set_tooltip_text(_("By origin: your default apps, system, Flatpak, your own (Ctrl+G)"))
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
        self.show_hidden = not self.show_hidden
        self.update_hidden_button_state()
        self.refresh_list()

    def quit_action(self, app):
        """How to quit a running app, or None: end its processes (SIGTERM,
        which apps take as a request to quit and save). Not for terminals:
        their process is shared."""
        name = terminal_command(app) or app_process_name(app)
        if not name or name in TERMINALS:
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

    def check_updates(self):
        """Look for package and Flatpak updates in the background, at most every
        updates.CHECK_INTERVAL seconds; the marks appear when it is done."""
        now = time.monotonic()
        if self.updates_running or (self.updates_at is not None
                                    and now - self.updates_at < updates.CHECK_INTERVAL):
            return
        self.updates_running = True
        desktop_paths = [a.get("desktop_path", "") for a in self.apps]

        def work():
            try:
                result = updates.check(desktop_paths)
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

    def prepare_entries(self, entries):
        """Freshly read entries: note new ones, use the icons chosen by hand."""
        self.note_new(entries)
        icons.apply(entries, self.icon_overrides)

    def note_new(self, entries):
        ids = [a["desktop_id"] for a in entries if a.get("desktop_id")]
        installed = {a["desktop_id"]: a["installed_at"] for a in entries if a.get("installed_at")}
        if newapps.note(self.seen, ids, "apps", installed=installed):
            self.save_seen()

    def save_seen(self):
        try:
            newapps.save(self.seen)
        except OSError as error:
            print("Could not save seen.json:", error, file=sys.stderr)

    def is_new(self, app):
        return bool(app.get("desktop_id")) and newapps.is_new(self.seen, app["desktop_id"])

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

    def flatpak_update(self, app):
        """(installed, available) for a Flatpak app with an update, else None."""
        plan = details.uninstall_plan(app, look_up_package=False)
        if plan is None or plan[0] != "flatpak":
            return None
        return self.updates["flatpaks"].get(plan[1])

    def update_flatpak(self, app):
        """`flatpak update` for this one app, in the background; says how it went."""
        app_id = details.uninstall_plan(app, look_up_package=False)[1]
        updating = getattr(self, "flatpaks_updating", set())
        if app_id in updating:
            return
        self.flatpaks_updating = updating | {app_id}
        self.notify(_("Updating {0}…").format(app["name"]))
        self.hold()  # Keep running to report, also when the window closes.

        def work():
            ok, message = updates.run_flatpak_update(app_id)
            GLib.idle_add(done, ok, message)

        def done(ok, message):
            self.release()
            self.flatpaks_updating = self.flatpaks_updating - {app_id}
            if ok:
                self.updates["flatpaks"].pop(app_id, None)
                self.notify(_("{0} updated").format(app["name"]))
            else:
                self.notify(_("{0} could not be updated").format(app["name"]), message)
            if self.window is not None and self.window.get_visible() and not self.actions_menu_open() \
                    and self.main_page_open():
                row = self.selected_item()
                self.refresh_list(selected_id=self.row_key(row) if row is not None else None)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    def update_system(self):
        """GNOME Software's update page, or the package manager's full
        update in a terminal."""
        command = desktop.system_update()
        if command is None:
            self.notify(_("Could not start the update"), _("No GNOME Software and no terminal found."))
            return
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            self.notify(_("Could not start the update"), str(error))
            return
        self.updates_at = None  # Check again the next time the window opens.
        self.hide()

    def update_note(self, app):
        """"Update available: …" for an entry with an update, else None."""
        flatpak = self.flatpak_update(app)
        if flatpak:
            return _("Update available: {0} → {1}").format(*flatpak) if flatpak[0] else _("Update available")
        package = self.updates["apps"].get(app.get("desktop_path")) if app.get("desktop_path") else None
        if package:
            return _("Update available: {0} {1} → {2}").format(*package)
        return None

    def legend_popover(self):
        """The legend: every mark on an app's icon or card, as it looks
        there, with what it means."""
        def dot(*classes):
            mark = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            for name in classes:
                mark.add_css_class(name)
            return mark

        def star():
            label = Gtk.Label(label="★")
            label.add_css_class("launcher-new-star-inline")
            return label

        def symbol(icon_name):
            image = Gtk.Image.new_from_icon_name(icon_name)
            image.set_pixel_size(12)
            image.add_css_class("launcher-folder-hidden-mark")
            image.set_halign(Gtk.Align.CENTER)
            return image

        def chip(text, css):
            label = Gtk.Label(label=text, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            label.add_css_class(css)
            return label

        rows = [
            ("active", dot("launcher-run-dot", "active"), _("Running")),
            ("update", dot("launcher-update-dot"), _("Update available: Update in its menu")),
            ("new", star(), _("New: installed recently (until you open it), or a new group (until it has an entry)")),
            ("pinned", self.pin_mark(), _("Pinned to the Dash")),
            ("hidden", symbol("view-conceal-symbolic"), _("Hidden: shown only with the eye button")),
        ]
        if shortcuts.available():
            rows.append(("shortcut", chip("Super + B", "launcher-shortcut-chip"), _("Its global shortcut")))
            rows.append(("more_shortcuts", chip("+1", "launcher-shortcut-more"), _("More shortcuts: Shortcut… in its menu")))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        title = Gtk.Label(label=_("What the marks mean"), xalign=0)
        title.add_css_class("sc-section")
        title.set_margin_start(8)
        title.set_margin_bottom(4)
        box.append(title)
        popover = Gtk.Popover()
        popover.add_css_class("launcher-popover")
        # Every row is a button: only the entries with that mark (again: all).
        # One column for the marks: same width in every row (the widest mark,
        # the shortcut chip), each mark centred in it; only the text expands.
        sizes = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for mark_id, mark, text in rows:
            mark.set_halign(Gtk.Align.CENTER)
            mark.set_valign(Gtk.Align.CENTER)
            slot = Gtk.CenterBox(valign=Gtk.Align.CENTER, hexpand=False)
            slot.set_center_widget(mark)
            sizes.add_widget(slot)
            label = Gtk.Label(label=text, xalign=0, halign=Gtk.Align.FILL, hexpand=True,
                              wrap=True, max_width_chars=44)
            label.add_css_class("launcher-legend-text")
            line = Gtk.Box(spacing=12)
            line.append(slot)
            line.append(label)
            button = Gtk.Button(child=line)
            button.add_css_class("flat")
            button.add_css_class("launcher-legend-row")
            if mark_id == self.mark_filter:
                button.add_css_class("launcher-toggle-active")
            button.set_tooltip_text(_("Show only these (click again or Esc: all)"))
            button.connect("clicked", lambda _b, m=mark_id: (popover.popdown(), self.set_mark_filter(m)))
            box.append(button)
        popover.set_child(box)
        return popover

    def set_mark_filter(self, mark_id):
        """Show only the entries with this legend mark; the same again: all."""
        self.mark_filter = None if mark_id == self.mark_filter else mark_id
        self.refresh_list()

    def mark_label(self, mark_id):
        return {"active": _("Running"), "update": _("Update"), "new": _("Newly installed"),
                "pinned": _("Pinned to the Dash"), "hidden": _("Hidden"), "shortcut": _("Shortcuts"),
                "more_shortcuts": _("Shortcuts") + " +1"}[mark_id]

    def has_mark(self, app, mark_id):
        """Whether app shows this legend mark (the list and cards' marks)."""
        if mark_id == "active":
            return self.app_state(app) == mark_id
        if mark_id == "update":
            return bool(self.update_note(app))
        if mark_id == "new":
            return self.is_new(app)
        if mark_id == "pinned":
            return self.is_pinned(app)
        if mark_id == "hidden":
            return app["desktop_id"] in self.hidden
        count = len(self.app_shortcuts(app, self.list_bindings))
        return count >= (2 if mark_id == "more_shortcuts" else 1)

    def update_badge(self, note):
        badge = self.tile_badge("software-update-available-symbolic", _("Update"))
        badge.add_css_class("launcher-update-badge")
        badge.set_tooltip_text(note)
        return badge

    # --- the Dash -----------------------------------------------------------------

    @staticmethod
    def dash_id(app):
        """The desktop file name GNOME Shell knows the app by, or None for
        entries the Dash cannot hold (AppImages, command-line tools, files
        outside the applications folders)."""
        path = Path(app.get("desktop_path") or "")
        if path.suffix != ".desktop" or path.parent.name != "applications" or not desktop.has_dash():
            return None
        return path.name

    def is_pinned(self, app):
        return Path(app.get("desktop_path") or "").name in self.favorites

    def set_pinned(self, app, pinned):
        dash_id = self.dash_id(app)
        if dash_id is None or not desktop.set_pinned(dash_id, pinned):
            self.notify(_("Could not change the Dash"))
            return
        self.on_favorites_changed()

    @staticmethod
    def pin_mark():
        """Pinned to the Dash: a small pin, the same in rows and cards."""
        pin = Gtk.Image.new_from_icon_name("view-pin-symbolic")
        pin.set_pixel_size(12)
        pin.add_css_class("launcher-pin-mark")
        pin.set_valign(Gtk.Align.CENTER)
        pin.set_tooltip_text(_("Pinned to the Dash"))
        return pin

    def refresh_after_close(self):
        self.refresh_running()
        self.refresh_list()
        return GLib.SOURCE_REMOVE


if __name__ == "__main__":
    raise SystemExit(Launcher().run(sys.argv))
