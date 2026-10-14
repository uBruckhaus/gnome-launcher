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
import colorsys
import copy
import shutil
from launcher_core import (
    TERMINALS, app_process_name, browser_web_app, background_process_names, child_process_names, client_matches_app,
    process_pids, terminal_command,
    DESKTOP_DIRS, HIDDEN_FILE, HIDDEN_PLUGINS_FILE, get_appimage_directories,
    admin_action_enabled, admin_command, running_first_enabled, set_running_first, saved_view, set_view,
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

# Height of the app list and the shortcut page, whatever they hold.
LIST_HEIGHT = 590

# Where each browser lists its installed web apps.
APPS_PAGES = {"Chrome": "chrome://apps", "Chromium": "chrome://apps", "Brave": "brave://apps",
              "Edge": "edge://apps", "Vivaldi": "vivaldi://apps"}


ICON_DECODE_SIZE = 64          # twice the 32 px rows, for HiDPI
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
    try:
        result = subprocess.run(["hyprctl", "getoption", "decoration:rounding", "-j"],
                                capture_output=True, text=True, timeout=2)
        return max(0, min(int(json.loads(result.stdout).get("int", 0)), 24))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return 0


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

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("launcher-card")
        card.set_size_request(420, -1)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        card.set_margin_top(24)
        card.set_margin_bottom(24)
        overlay.add_overlay(card)

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
        header.append(back_button)
        title = Gtk.Label(label=_("Apps"))
        title.add_css_class("launcher-title")
        title.set_hexpand(True)
        title.set_halign(Gtk.Align.START)
        header.append(title)
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
        card.append(header)

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
        self.apply_theme()

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

        # Follow Hyprland's window rounding like the Omarchy shell does.
        radius = hyprland_rounding()
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
        separator.launcher-settings-rule {{ background-color: alpha({colors['foreground']}, 0.12); background-image: none; min-height: 1px; margin: 0 0 2px 0; }}
        .launcher-card label.launcher-setting-label {{ font-size: 11px; color: {dim_hex}; }}
        dropdown.launcher-compact-dropdown > button {{ min-height: 0; padding: 2px 8px; font-size: 11px;
            border: 1px solid alpha({colors['foreground']}, 0.35); border-radius: {radius}px; background: transparent; }}
        dropdown.launcher-compact-dropdown popover label {{ font-size: 11px; }}
        switch.launcher-compact-switch {{ min-height: 16px; min-width: 36px; }}
        switch.launcher-compact-switch slider {{ min-width: 14px; min-height: 14px; margin: 1px; }}
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
        self.shortcut_state = shortcuts.load_state()
        self.prune_uninstalled_shortcuts()
        self.update_dead_binds_question()
        self.list_bindings = None
        try:
            self.apps = parse_desktop_files()
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
        self.update_hidden_button_state()
        try:
            self.refresh_running()
            self.refresh_list()
        except Exception as e:
            print("Error refreshing list:", e, file=sys.stderr)
        self.window.present()
        self.layer_watch.start()
        if self.mode == "apps":
            self.prefetch_plugins()
        # Restarted after a language change: back to where the user was.
        if os.environ.pop("SIMPLE_LAUNCHER_PAGE", "") == "settings":
            GLib.idle_add(self.open_settings_page)
            return
        def _focus():
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
        row = self.rows.get_selected_row()
        selected = self.row_key(row) if row is not None else None
        try:
            self.apps = parse_desktop_files()
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
        picked = self.overlay.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is None or not picked.is_ancestor(self.card):
            self.hide()

    def on_active_changed(self, window, _property):
        if window.get_visible() and not window.is_active():
            GLib.timeout_add(100, self.hide_if_inactive)

    def hide_if_inactive(self):
        if self.actions_menu_open():
            return GLib.SOURCE_REMOVE
        # The password dialog for "Open as administrator" has the focus.
        if getattr(self, "admin_auth_pending", False):
            return GLib.SOURCE_REMOVE
        # Just opened a website from the details page: the browser has the focus.
        if time.monotonic() < getattr(self, "keep_open_until", 0):
            return GLib.SOURCE_REMOVE
        if self.window and self.window.get_visible() and not self.window.is_active():
            self.hide()
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
            if self.search_query:
                self.search_query = ""
                self.refresh_list()
            else:
                self.hide()
            return True

        if key in (Gdk.KEY_g, Gdk.KEY_G) and state & Gdk.ModifierType.CONTROL_MASK and self.shortcut_app is None:
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
            row = self.rows.get_selected_row()
            if row is not None and hasattr(row, "app"):
                self.open_shortcut_page(row.app)
            return True

        if key == Gdk.KEY_Delete:
            row = self.rows.get_selected_row()
            if row is not None and hasattr(row, "app"):
                self.close_app(row.app)
            return True

        if key == Gdk.KEY_BackSpace:
            if self.search_query:
                self.search_query = self.search_query[:-1]
                self.refresh_list()
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
            menu_open = self.actions_menu_open()
            # Tray icons come and go with their apps (Steam started, NordVPN quit).
            if self.mode == "plugins" and not menu_open and self.shortcut_app is None and self.tray_changed():
                self.reload_apps()
            elif not menu_open and self.refresh_running():
                self.refresh_list()
            if self.theme.reload():
                self.apply_theme()
        return GLib.SOURCE_CONTINUE

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
        self.update_title()
        if any(self.app_state(app) != "stopped" for app in self.apps):
            self.running_button.add_css_class("any-running")
        else:
            self.running_button.remove_css_class("any-running")
        selected = self.rows.get_selected_row()
        if selected_id is None:
            selected_id = self.row_key(selected) if selected else None
        selected_row = None
        while child := self.rows.get_first_child():
            self.rows.remove(child)
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

    def put_in_group(self, app, gid):
        try:
            groups.assign(self.groups, app["desktop_id"], gid)
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
            row = self.rows.get_first_child()
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
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("launcher-settings-rule")
        page.append(rule)
        grid = Gtk.Grid(column_spacing=10, row_spacing=10)

        def label(text, tooltip=None):
            widget = Gtk.Label(label=text, xalign=0)
            widget.add_css_class("launcher-setting-label")
            widget.set_size_request(150, -1)
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
        page.append(grid)
        self.admin_switch = admin_switch
        self.language_dropdown = dropdown
        self.stack.add_named(page, "settings")

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
        if self.shortcut_app is not None:
            self.close_shortcut_page(focus_list=False)
        self.back_chip.set_label("· ⌫")
        self.back_chip.remove_css_class("launcher-shortcut-chip")
        self.back_button.add_css_class("launcher-back-bordered")
        self.title.add_css_class("launcher-section-title")
        self.back_button.set_tooltip_text(_("Back (Backspace, Esc)"))
        self.hide_main_header()
        self.set_admin_switch(admin_action_enabled())  # Off again in a new session.
        self.stack.set_visible_child_name("settings")
        self.update_title()
        self.language_dropdown.grab_focus()

    def close_settings_page(self):
        self.back_chip.add_css_class("launcher-shortcut-chip")
        self.back_button.remove_css_class("launcher-back-bordered")
        self.title.remove_css_class("launcher-section-title")
        self.show_main_header()
        self.stack.set_visible_child_name("list")
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
                       self.settings_button):
            button.set_visible(False)

    def show_main_header(self):
        """The list's header buttons."""
        self.back_button.set_visible(False)
        for button in (self.hidden_button, self.view_button, self.mode_button, self.settings_button):
            button.set_visible(True)
        self.running_button.set_visible(self.mode == "apps")

    def toggle_view(self, view=None):
        """Switch to view ("list", "groups" or "origin"), or to the next one."""
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
        return self.stack.get_visible_child_name() in ("groupname", "details")

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
        self.stack.set_visible_child_name("list")
        self.update_title()
        if refresh:
            self.refresh_list()

    def build_group_name_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_start(4)
        page.set_margin_end(4)
        page.set_size_request(-1, LIST_HEIGHT)
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
        # Show where it went: the group view, on the group or the moved entry.
        if self.view != "groups":
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
        self.sc_page.set_size_request(-1, LIST_HEIGHT)
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
        self.stack.set_visible_child_name("list")
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
            row = self.rows.get_first_child()
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
        if self.stack.get_visible_child_name() == "list":
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
            self.plugins_at = time.monotonic()
            # Already on the plugin page: show the fresh list, unless a menu
            # or a sub-page is open (rebuilding rows would close it).
            if (self.mode == "plugins" and self.window.get_visible() and not self.actions_menu_open()
                    and self.stack.get_visible_child_name() == "list"):
                row = self.rows.get_selected_row()
                self.refresh_list(selected_id=self.row_key(row) if row is not None else None)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

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
