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
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell

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
    launch_action, launch_app, load_hidden_apps, open_plugin, parse_desktop_files, parse_plugins,
    plugin_removal_note, save_hidden_apps, set_in_bar, BAR_SECTIONS, USER_PLUGINS_DIR,
)
from palette import ThemePalette
import pidfile
import shortcuts
import tray

# Height of the app list and the shortcut page, whatever they hold.
LIST_HEIGHT = 590

# Where each browser lists its installed web apps.
APPS_PAGES = {"Chrome": "chrome://apps", "Chromium": "chrome://apps", "Brave": "brave://apps",
              "Edge": "edge://apps", "Vivaldi": "vivaldi://apps"}


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
    def hidden_ids(self):
        return self.hidden_by_mode[self.mode]

    @property
    def show_hidden(self):
        return self.show_hidden_by_mode[self.mode]

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
        window = Gtk.ApplicationWindow(application=self)
        window.set_decorated(False)
        window.set_title("Simple Launcher")
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
        back_button.set_tooltip_text("Back to apps (Esc)")
        back_button.add_css_class("flat")
        back_button.add_css_class("launcher-back-btn")
        # The key that does the same, as a hint next to the arrow.
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        esc = Gtk.Label(label="Esc")
        esc.add_css_class("launcher-shortcut-chip")
        esc.set_valign(Gtk.Align.CENTER)
        back_box.append(esc)
        back_button.set_child(back_box)
        back_button.set_visible(False)
        back_button.connect("clicked", lambda *_: self.close_shortcut_page())
        header.append(back_button)
        title = Gtk.Label(label="Apps")
        title.add_css_class("launcher-title")
        title.set_hexpand(True)
        title.set_halign(Gtk.Align.START)
        header.append(title)
        mode_button = Gtk.Button()
        mode_button.add_css_class("flat")
        mode_button.add_css_class("launcher-header-btn")
        mode_button.connect("clicked", self.toggle_mode)
        header.append(mode_button)
        hidden_button = Gtk.Button(icon_name="view-reveal-symbolic")
        hidden_button.set_tooltip_text("Show hidden apps")
        hidden_button.add_css_class("flat")
        hidden_button.add_css_class("launcher-header-btn")
        hidden_button.connect("clicked", self.toggle_hidden)
        header.append(hidden_button)
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
        keep = Gtk.Button(label="Keep")
        keep.add_css_class("sc-small")
        keep.connect("clicked", lambda *_: self.answer_dead_binds(False))
        answers.append(keep)
        delete = Gtk.Button(label="Delete")
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
        .launcher-toggle-active {{
            background: rgba(147, 147, 195, 0.25);
            border-radius: {radius}px;
        }}

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
        def _focus():
            first = self.rows.get_first_child()
            if first:
                first.grab_focus()
            else:
                self.rows.grab_focus()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(_focus)

    def hide(self):
        if self.actions_menu_open():
            self.actions_popover.popdown()
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
        selected = row.app["desktop_id"] if row is not None and hasattr(row, "app") else None
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
            self.notify("Shortcuts of removed apps deleted", ", ".join(names))

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
            f"The app of {'this shortcut' if len(self.dead_binds) == 1 else 'these shortcuts'} was uninstalled. "
            f"Delete {'it' if len(self.dead_binds) == 1 else 'them'} from your Hyprland files?\n{listed}")
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
            self.notify("Could not delete the shortcuts", str(error))
            return
        if removed:
            self.list_bindings = None
            self.refresh_list()
            self.notify("Shortcuts of removed apps deleted",
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
        if self.window and self.window.get_visible() and not self.window.is_active():
            self.hide()
        return GLib.SOURCE_REMOVE

    def select_row(self, row):
        if row:
            self.rows.select_row(row)
            row.grab_focus()

    def on_key(self, _controller, key, _code, state):
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
            if row and hasattr(row, "actions_button"):
                self.show_actions(row.actions_button, row.app)
                return True

        if key == Gdk.KEY_Tab and self.shortcut_app is None:
            self.toggle_mode()
            return True

        if key in (Gdk.KEY_k, Gdk.KEY_K) and state & Gdk.ModifierType.CONTROL_MASK:
            row = self.rows.get_selected_row()
            if row is not None:
                self.open_shortcut_page(row.app)
            return True

        if key == Gdk.KEY_Delete:
            row = self.rows.get_selected_row()
            if row is not None:
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
        return {c for a in self.apps if (c := terminal_command(a))}

    def app_state(self, app):
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
        selected = self.rows.get_selected_row()
        if selected_id is None:
            selected_id = selected.app.get("desktop_id") if selected else None
        selected_row = None
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        query = self.search_query.casefold().strip()
        if self.list_bindings is None:
            self.list_bindings = shortcuts.Bindings(self.shortcut_state)
        for app in self.plugins if self.mode == "plugins" else self.apps:
            is_hidden = app["desktop_id"] in self.hidden_ids
            # Bar widgets taken out of the bar are hidden too: the eye shows them
            # to put them back. The launcher itself stays, for its own keys.
            if (is_hidden or self.off_bar(app)) and not self.show_hidden:
                continue
            if query not in app["name"].casefold():
                continue
            row = Gtk.ListBoxRow()
            row.app = app
            if is_hidden:
                row.add_css_class("launcher-row-hidden")
            line = Gtk.Box(spacing=8)
            icon_name = app.get("icon") or "application-x-executable"
            image_extensions = {".png", ".svg", ".xpm", ".jpg", ".jpeg", ".webp", ".bmp"}
            themed_name = Path(icon_name).stem if Path(icon_name).suffix.lower() in image_extensions else Path(icon_name).name
            icon = Gtk.Image.new_from_file(icon_name) if Path(icon_name).is_file() else Gtk.Image.new_from_icon_name(themed_name)
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
                icon_slot.set_tooltip_text("Open windows" if state == "active" else "Running in background")
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
                badges.append(("view-conceal-symbolic", "Hidden"))
            if app.get("bar_widget") and not app.get("in_bar"):
                badges.append(("list-remove-symbolic", "Not in bar"))
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
                prefix = "[Hidden] " if is_hidden else ""
                row.set_tooltip_text(f"{prefix}{app['name']} — {desc_text}")
            else:
                prefix = "[Hidden] " if is_hidden else ""
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
                chips.set_tooltip_text("Global shortcut" + ("s:\n" if len(assigned) > 1 else ": ") + "\n".join(
                    shortcuts.combo_label(c) + (f" → {shortcuts.action_name(app, c['action'])}" if c["action"] else "")
                    for c in assigned))
                line.append(chips)
            actions = Gtk.Button(icon_name="open-menu-symbolic")
            actions.add_css_class("flat")
            actions.set_tooltip_text("Tray menu" if "tray_id" in app else
                                     "Plugin actions" if "plugin_id" in app else "App actions")
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
            self.rows.append(row)
            if selected_id and selected_id == app["desktop_id"]:
                selected_row = row
        selected_row = selected_row or self.rows.get_first_child()
        if selected_row:
            self.rows.select_row(selected_row)
            # Rebuilt rows need a layout before focus can scroll them into view.
            GLib.timeout_add(50, self.ensure_selected_visible, selected_row)

    def list_header(self, row, before):
        """Section titles in the widget list: tray icons, then plugins."""
        if self.mode != "plugins" or not hasattr(row, "app"):
            row.set_header(None)
            return
        in_tray = "tray_id" in row.app
        if before is not None and hasattr(before, "app") and ("tray_id" in before.app) == in_tray:
            row.set_header(None)
            return
        title = Gtk.Label(label="Running in the bar" if in_tray else "Widgets & plugins", xalign=0)
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
                self.notify(f"{app['name']} has no window to open", "Use its tray menu.")
            return
        if app.get("self_launcher"):
            self.open_shortcut_page(app)  # Already open: Enter sets its keys.
            return
        if "plugin_id" in app and not app.get("openable", True):
            if button is not None:
                self.show_actions(button, app)
            else:
                self.notify(f"{app['name']} cannot be opened from here",
                            "Put it in the bar first." if app.get("bar_widget") and not app.get("in_bar") else "")
            return
        if "plugin_id" not in app:
            launch_app(app)
            self.hide()
            return
        # Out of the way first: the plugin's panel takes the keyboard.
        self.hide()
        if not open_plugin(app):
            self.notify(f"Could not open {app['name']}", "The shell has no panel to open for it.")

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
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda _c, key, _code, _state: self.close_actions_on_left(popover, key))
        popover.add_controller(keys)
        self.fill_actions(popover, app)
        popover.popup()

    def close_actions_on_left(self, popover, key):
        if key not in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            return False
        focus = self.window.get_focus()
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
                add_action("Open", lambda: self.launch(app), "media-playback-start-symbolic",
                           shortcut=chip_for(""))
            for action in app.get("actions", []):
                add_action(action["name"], lambda action_id=action["id"]:
                           (launch_action(app, action_id), self.hide()), "system-run-symbolic",
                           shortcut=chip_for(action["id"]))

        # A shortcut needs something to open: not a widget that is only a bar part.
        if app.get("self_launcher") or app.get("openable", True):
            if not app.get("self_launcher"):
                add_separator()
            add_action("Shortcut…", lambda: self.open_shortcut_page(app),
                       "preferences-desktop-keyboard-shortcuts-symbolic")
            add_separator()
        hidden = app["desktop_id"] in self.hidden_by_mode["plugins" if is_widget(app) else "apps"]
        # Out of the bar, a widget is hidden by that already: Hide would do nothing.
        # Unhide stays for one hidden by hand, or it stays hidden once back in the bar.
        if hidden or not self.off_bar(app):
            add_action("Unhide from list" if hidden else "Hide from list",
                       lambda: self.set_hidden(app, not hidden),
                       "view-reveal-symbolic" if hidden else "view-conceal-symbolic")
        if app.get("bar_widget") and set_in_bar(app["plugin_id"], not app["in_bar"]):
            if app["in_bar"]:
                add_action("Remove from bar", lambda: self.set_bar_placement(app, False), "list-remove-symbolic")
            else:
                # Where it goes is the user's choice: left, center or right.
                add_action("Add to bar", lambda: self.fill_bar_sections(popover, app), "list-add-symbolic",
                           keep_open=True, end_icon="go-next-symbolic")
        state = self.app_state(app) if not is_widget(app) else "stopped"
        if state == "active":
            add_action("Close", lambda: self.close_app(app), "window-close-symbolic", is_destructive=True)
        quit_app = self.quit_action(app, state) if state != "stopped" else None
        if quit_app:
            add_action("Quit", quit_app, "application-exit-symbolic", is_destructive=True)
        # A tray icon uninstalls the app it belongs to (Steam's tray: Steam).
        owner = app.get("app", app) if "tray_id" in app else app
        if app.get("removable") or (owner.get("desktop_path") and shutil.which("omarchy-remove-launcher-entry")):
            add_separator()
            add_action("Uninstall…", lambda: self.confirm_uninstall(owner), "user-trash-symbolic",
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
            first = add_action("Back", lambda: self.fill_actions(popover, app, trail[:-1]),
                               "go-previous-symbolic", keep_open=True)
            title = Gtk.Label(label=" › ".join(node["label"] for node in trail), xalign=0)
            title.add_css_class("sc-section")
            title.set_margin_start(8)
            box.append(title)
            nodes = trail[-1]["children"]
        else:
            if not app["tray_is_menu"] or app.get("app"):
                first = add_action("Open", lambda: self.launch(app), "media-playback-start-symbolic",
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
            empty = Gtk.Label(label="No menu", xalign=0)
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
            self.notify(f"{app['name']}: “{node['label']}” did not respond")

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
        question = f"How to uninstall {app['name']}" if browser else f"Do you want to uninstall {app['name']}?"
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
            note = Gtk.Label(label=f"{app['name']} is a web app installed in {browser}, so only {browser} "
                                   f"can uninstall it: open the app and choose ⋮ → Uninstall, or right-click "
                                   f"it on {APPS_PAGES.get(browser, 'its apps page')} and choose Remove.",
                             xalign=0, wrap=True, max_width_chars=32)
            note.add_css_class("sc-status")
            note.add_css_class("sc-info")
            box.append(note)
        buttons = Gtk.Box(spacing=6)
        buttons.set_halign(Gtk.Align.END)
        if browser:
            close = Gtk.Button(label="Close")
            self.focus_follows_pointer(close)
            close.add_css_class("sc-small")
            close.connect("clicked", lambda *_: popover.popdown())
            buttons.append(close)
            box.append(buttons)
            popover.set_child(box)
            close.grab_focus()
            return
        cancel = Gtk.Button(label="Cancel")
        self.focus_follows_pointer(cancel)
        cancel.add_css_class("sc-small")
        cancel.connect("clicked", lambda *_: popover.popdown())
        buttons.append(cancel)
        confirm = Gtk.Button(label="Uninstall")
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

        item("Back", "go-previous-symbolic", lambda: self.fill_actions(popover, app))
        title = Gtk.Label(label=f"Add {app['name']} to the bar", xalign=0)
        title.add_css_class("sc-section")
        title.set_margin_start(8)
        box.append(title)
        default = app.get("bar_section") if app.get("bar_section") in BAR_SECTIONS else None
        buttons = {section: item(section.capitalize(), f"format-justify-{section}-symbolic",
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
            self.notify(f"Could not change the bar for {plugin['name']}", error.message)
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
            subprocess.Popen(["notify-send", "-a", "Simple Launcher", plain_text(title), plain_text(body)],
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
        current_title = section("Shortcuts")
        current = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(current)

        divider = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        divider.add_css_class("sc-divider")
        box.append(divider)

        # 2. Adding a new one: modifiers, key, verdict, free keys, button.
        add_header = Gtk.Box(spacing=6)
        add_title = Gtk.Label(label="Add a shortcut", xalign=0)
        add_title.add_css_class("sc-section")
        add_title.set_hexpand(True)
        add_header.append(add_title)
        add_cancel = Gtk.Button(label="Cancel")
        add_cancel.add_css_class("sc-small")
        add_cancel.add_css_class("flat")
        add_cancel.set_tooltip_text("Keep the current key")
        add_cancel.set_visible(False)
        add_cancel.connect("clicked", lambda *_: self.cancel_change_shortcut())
        add_header.append(add_cancel)
        box.append(add_header)

        target_row = Gtk.Box(spacing=8)
        target_label = Gtk.Label(label="Opens", xalign=0)
        target_label.add_css_class("sc-note")
        target_row.append(target_label)
        target = Gtk.DropDown.new_from_strings([""])
        target.set_hexpand(True)
        target.set_tooltip_text("Open the app, or one of its own actions such as a private window")
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

        default_check = Gtk.CheckButton(label="Default for apps without a shortcut")
        default_check.add_css_class("sc-default")
        default_check.connect("toggled", self.on_shortcut_default_toggled)
        box.append(default_check)

        two_step = Gtk.CheckButton(label="Two steps — then press another key (e.g. Super + Alt + Space, N)")
        two_step.add_css_class("sc-default")
        two_step.set_tooltip_text("Press the first combo, let go, then a single key. Several shortcuts "
                                  "can share the first combo, each with its own second key.")
        two_step.connect("toggled", self.on_two_step_toggled)
        box.append(two_step)

        key_box = Gtk.Box(spacing=6)
        key_box.add_css_class("sc-key")
        key_box.set_halign(Gtk.Align.FILL)
        key_box.set_tooltip_text("Press a key now. Hold modifiers while pressing, or toggle them above.")
        key_box.set_hexpand(True)
        # Assign sits beside the key it applies, to save a row.
        key_row = Gtk.Box(spacing=8)
        key_row.append(key_box)
        apply = Gtk.Button(label="Assign")
        apply.add_css_class("sc-primary")
        apply.set_valign(Gtk.Align.CENTER)
        apply.connect("clicked", lambda *_: self.apply_shortcut())
        key_row.append(apply)
        box.append(key_row)

        status = Gtk.Label(xalign=0, wrap=True)
        status.add_css_class("sc-status")
        box.append(status)

        suggest_title = section("Free keys")
        legend = Gtk.Box(spacing=10)
        for text, css in (("● free", "sc-legend-free"), ("● yours", "sc-legend-yours"),
                          ("● Omarchy default", "sc-legend-omarchy")):
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
        section("Other keys")
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
                then = Gtk.Label(label="then")
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
        self.back_button.set_visible(True)
        self.hidden_button.set_visible(False)
        self.mode_button.set_visible(False)
        self.stack.set_visible_child_name("shortcut")
        self.update_title()
        self.set_shortcut_inhibit(True)
        self.sc_apply_button.grab_focus()

    def close_shortcut_page(self, focus_list=True):
        app = self.shortcut_app
        self.set_shortcut_inhibit(False)
        self.shortcut_app = None
        self.sc_editing = None
        self.back_button.set_visible(False)
        self.hidden_button.set_visible(True)
        self.mode_button.set_visible(True)
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
        """Combos that open app right now: launcher ones, then external ones."""
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
                edit.set_tooltip_text("Change this shortcut to another key")
                edit.set_valign(Gtk.Align.CENTER)
                edit.connect("clicked", lambda *_: change())
                row.append(edit)
                click = Gtk.GestureClick()
                click.connect("released", lambda *_: change())
                text.add_controller(click)
                text.set_tooltip_text("Click to change this shortcut")
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
            note = f"Opens {self.shortcut_target(app, combo)}"
            same = next((b for b in externals if shortcuts.combo_id(b["combo"]) == shortcuts.combo_id(combo)
                         and b["action"] == combo.get("action", "")), None)
            if same:
                note += f" · duplicate of your {same['file']} binding — remove it here to tidy up"
            elif combo.get("replaces"):
                note += f" · overrides “{combo['replaces']}”"
            item(combo, "Launcher", note, "×", "Remove this shortcut"
                 + (f" — “{combo['replaces']}” works again" if combo.get("replaces") else ""),
                 lambda c=combo: self.remove_shortcut(c),
                 change=lambda c=combo: self.start_change_shortcut(c, None))
            active += 1
        disabled_ids = {shortcuts.combo_id(c) for c in entry["disabled"]}
        owned_ids = shortcuts.owned_ids(self.shortcut_state)
        for bind in externals:
            cid = shortcuts.combo_id(bind["combo"])
            if cid in disabled_ids or cid in owned_ids:
                continue
            origin = "Omarchy default" if bind["file"] == "Omarchy" else f"Your {bind['file']}"
            note = f"Opens {self.shortcut_target(app, bind)} · {origin}"
            if bind["description"] and bind["description"] != self.shortcut_target(app, bind):
                note += f" “{bind['description']}”"
            item(bind["combo"], bind["file"], note, "×",
                 f"Turn off — {bind['file']} stays unchanged and you can turn it back on",
                 lambda b=bind: self.disable_external_shortcut(b),
                 change=lambda b=bind: self.start_change_shortcut({**b["combo"], "action": b["action"]}, b))
            active += 1
        if not active:
            empty = Gtk.Label(label="None yet — add one below.", xalign=0)
            empty.add_css_class("sc-empty")
            self.sc_current.append(empty)
        for combo in entry["disabled"]:
            was = f" · was “{combo['description']}”" if combo.get("description") else ""
            item(combo, combo.get("source") or "Config", f"Turned off{was}", "Turn on",
                 "Use this shortcut from your config again", lambda c=combo: self.restore_external_shortcut(c),
                 dim=True)
        count = f" ({active})" if active > 1 else ""
        self.sc_current_title.set_label(f"Shortcuts for {app['name']}{count}")

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
            self.sc_add_title.set_label(f"Change {label} to")
        else:
            self.sc_add_title.set_label("Add a shortcut")
        self.sc_add_cancel.set_visible(self.sc_editing is not None)

    def render_shortcut_suggestions(self):
        """Free keys for the selected modifiers; clicking one picks it."""
        app = self.shortcut_app
        mods = [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]
        while child := self.sc_suggest.get_first_child():
            self.sc_suggest.remove(child)
        self.sc_legend.set_visible(bool(mods))
        self.sc_legend.set_tooltip_text("Taken keys can be rebound; the original works again when you remove the shortcut.")
        if self.picking_then():
            first = shortcuts.combo_label({"mods": mods, "key": self.sc_key})
            self.sc_suggestions = [shortcuts.make_combo(mods, self.sc_key, k) for k in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            self.sc_suggest_title.set_label(f"After {first}, press")
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
            self.sc_suggest_title.set_label("Keys with " + " + ".join(shortcuts.MOD_LABELS[m] for m in mods))
            for combo in self.sc_suggestions:
                chip = Gtk.Button(label=combo["key"])
                chip.add_css_class("sc-letter")
                self.style_key_chip(chip, combo)
                chip.connect("clicked", lambda *_, c=combo: self.pick_shortcut_key(c["mods"], c["key"]))
                self.sc_suggest.append(chip)
            return self.mark_other_keys(mods)
        self.sc_suggestions = self.shortcut_bindings.suggestions(app, self.sc_app_id, None)
        self.sc_suggest_title.set_label("Free suggestions")
        for combo in self.sc_suggestions:
            chip = Gtk.Button(label=shortcuts.combo_label(combo).replace(" + ", "+"))
            chip.add_css_class("sc-suggest")
            chip.set_tooltip_text(f"Use {shortcuts.combo_label(combo)}")
            chip.connect("clicked", lambda *_, c=combo: self.pick_shortcut_key(c["mods"], c["key"]))
            self.sc_suggest.append(chip)
        self.mark_other_keys(mods)
        if not self.sc_suggestions:
            none = Gtk.Label(label="Choose modifiers above to see every key.", xalign=0)
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
                button.set_tooltip_text("Choose modifiers first")

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
            tip = f"{label} — free"
        elif level in (shortcuts.SAME, shortcuts.RETARGET):
            tip = f"{label} — already opens {self.shortcut_app['name']}"
        elif level == shortcuts.OMARCHY:
            tip = f"{label} — Omarchy default “{owner}”; click to rebind"
        elif level == shortcuts.LAUNCHER:
            tip = f"{label} — opens {owner}; click to move it here"
        else:
            tip = f"{label} — your binding “{owner}”; click to rebind"
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
            f"Current default: {label}. Check to make the selected modifiers the default; "
            "uncheck to go back to Super + Shift.")

    def on_shortcut_default_toggled(self, check):
        if self.sc_syncing:
            return
        mods = self.selected_shortcut_mods()
        try:
            shortcuts.save_default_mods(mods if check.get_active() and mods else shortcuts.DEFAULT_MODS)
        except OSError as error:
            self.set_shortcut_status(f"Could not save the default: {error}", "sc-danger")
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
            button_label = "Change"
        self.sc_apply_button.set_label(button_label or "Assign")
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
            then = Gtk.Label(label="then")
            then.add_css_class("sc-note")
            then.set_valign(Gtk.Align.CENTER)
            self.sc_key_box.append(then)
            placeholder = Gtk.Label(label="a second key", xalign=0)
            placeholder.add_css_class("sc-placeholder")
            placeholder.set_valign(Gtk.Align.CENTER)
            self.sc_key_box.append(placeholder)
            return
        if mods:
            caps = self.keycaps({"mods": mods, "key": ""}, large=True)
            caps.remove(caps.get_last_child())  # drop the empty key cap
            self.sc_key_box.append(caps)
        placeholder = Gtk.Label(label="select a key", xalign=0)
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
                self.set_shortcut_status("Now press the second key on its own, or pick one below.", "sc-info")
            elif self.sc_two_step.get_active():
                self.set_shortcut_status("Press the first combo (with modifiers), or pick a key below.", "sc-info")
            else:
                self.set_shortcut_status("Press a key, or pick a free key below.", "sc-info")
            return
        if not combo["mods"]:
            self.set_shortcut_status("Choose at least one modifier — Super is recommended.", "sc-info")
            return
        level, owner = self.classify_shortcut(combo)
        target = self.shortcut_target(self.shortcut_app, {"action": self.current_shortcut_action()})
        if self.sc_editing and shortcuts.combo_id(combo) == shortcuts.combo_id(self.sc_editing["combo"]):
            if level == shortcuts.SAME:
                self.set_shortcut_status("That is the current key — press a different one, or pick "
                                         "something else to open.", "sc-info")
            else:
                self.set_shortcut_status(f"✓ Same key, now opens {target}.", "sc-free", "Change", None, True)
            return
        if level == shortcuts.RETARGET:
            was = self.shortcut_target(self.shortcut_app, {"action": owner})
            self.set_shortcut_status(f"Opens {was} now — assigning makes it open {target}.",
                                     "sc-info", "Change", None, True)
            return
        if level == shortcuts.FREE:
            tip = "" if "SUPER" in combo["mods"] else " Without Super, apps can no longer receive it."
            self.set_shortcut_status(f"✓ Free.{tip}", "sc-free", "Assign", None, True)
        elif level == shortcuts.SAME:
            source = f" (“{owner}”)" if owner and owner != name else ""
            self.set_shortcut_status(f"Already opens {target}{source} — nothing to add.", "sc-info")
        elif level == shortcuts.LAUNCHER:
            self.set_shortcut_status(f"Opens {owner} now. Assigning moves it to {target}.",
                                     "sc-warn", "Move here", "sc-warn-btn", True)
        elif level == shortcuts.CUSTOM:
            self.set_shortcut_status(f"Your binding “{owner}”. Assigning turns it off while this shortcut "
                                     "exists; removing the shortcut brings it back.",
                                     "sc-warn", "Rebind", "sc-warn-btn", True)
        else:
            self.set_shortcut_status(f"Omarchy default “{owner}”. Assigning turns it off while this shortcut "
                                     "exists; removing the shortcut brings it back.",
                                     "sc-hint-omarchy", "Rebind", "sc-omarchy-btn", True)

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
            self.set_shortcut_status("That key cannot be used for a shortcut.", "sc-info")
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
            first = str(error).strip().splitlines()[0] if str(error).strip() else "unknown error"
            self.set_shortcut_status(f"Not applied — Hyprland reported: {first}", "sc-danger")
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
        message = f"✓ Saved — {shortcuts.combo_label(combo)} opens {target}."
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
            message = (f"✓ {shortcuts.combo_label(combo)} now opens {target}." if old_id == cid else
                       f"✓ Changed {shortcuts.combo_label(old)} to {shortcuts.combo_label(combo)}.")
        entry["shortcuts"].append({**combo, "replaces": replaces, "action": action})
        self.commit_shortcuts(new_state, message)

    def remove_shortcut(self, combo):
        new_state = copy.deepcopy(self.shortcut_state)
        cid = shortcuts.combo_id(combo)
        entry = self.shortcut_entry(new_state)
        entry["shortcuts"] = [c for c in entry["shortcuts"] if shortcuts.combo_id(c) != cid]
        restored = f" “{combo['replaces']}” works again." if combo.get("replaces") else ""
        self.commit_shortcuts(new_state, f"✓ Removed {shortcuts.combo_label(combo)}.{restored}")

    def disable_external_shortcut(self, bind):
        new_state = copy.deepcopy(self.shortcut_state)
        entry = self.shortcut_entry(new_state)
        entry["disabled"].append({**bind["combo"], "description": bind["description"], "source": bind["file"]})
        self.commit_shortcuts(new_state, f"✓ Turned off {shortcuts.combo_label(bind['combo'])}. "
                                         "Turn it on again any time.")

    def restore_external_shortcut(self, combo):
        new_state = copy.deepcopy(self.shortcut_state)
        cid = shortcuts.combo_id(combo)
        entry = self.shortcut_entry(new_state)
        entry["disabled"] = [c for c in entry["disabled"] if shortcuts.combo_id(c) != cid]
        self.commit_shortcuts(new_state, f"✓ {shortcuts.combo_label(combo)} is on again.")

    def set_hidden(self, app, hidden):
        mode = "plugins" if is_widget(app) else "apps"
        hidden_ids = self.hidden_by_mode[mode]
        selected_id = app["desktop_id"]
        if hidden:
            row = self.rows.get_first_child()
            while row:
                if row.app["desktop_id"] == selected_id:
                    target = row.get_next_sibling() or row.get_prev_sibling()
                    if target:
                        selected_id = target.app["desktop_id"]
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
        if getattr(self, "shortcut_app", None) is not None:
            self.title.set_label(self.shortcut_app["name"])
            return
        label = "Widgets & Plugins" if self.mode == "plugins" else "Apps"
        if self.show_hidden:
            label += " (Showing Hidden)"
        if self.search_query:
            label += f" — {self.search_query}"
        self.title.set_label(label)

    def update_hidden_button_state(self):
        if not hasattr(self, "hidden_button"):
            return
        what = "plugins" if self.mode == "plugins" else "apps"
        if self.show_hidden:
            self.hidden_button.set_icon_name("view-conceal-symbolic")
            self.hidden_button.set_tooltip_text(f"Hide hidden {what}")
            self.hidden_button.add_css_class("launcher-toggle-active")
        else:
            self.hidden_button.set_icon_name("view-reveal-symbolic")
            self.hidden_button.set_tooltip_text(f"Show hidden {what}")
            self.hidden_button.remove_css_class("launcher-toggle-active")
        # The button shows where it leads, like the eye.
        if self.mode == "plugins":
            self.mode_button.set_icon_name("view-app-grid-symbolic")
            self.mode_button.set_tooltip_text("Show apps (Tab)")
        else:
            self.mode_button.set_icon_name("application-x-addon-symbolic")
            self.mode_button.set_tooltip_text("Show widgets & plugins (Tab)")
        self.update_title()

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
        plugins, items = [], []
        try:
            self.tray_seen = tray.registered()
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
        self.plugins = items + plugins

    def toggle_mode(self, *_args):
        if self.actions_menu_open():
            self.actions_popover.popdown()
        self.mode = "apps" if self.mode == "plugins" else "plugins"
        if self.mode == "plugins":
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
