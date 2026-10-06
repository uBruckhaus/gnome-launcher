#!/usr/bin/env python3
"""AppLauncher as a Wayland layer-shell popup, rather than an app window."""

import json
import os
import signal
import subprocess
import sys
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
    TERMINALS, app_process_name, background_process_names, child_process_names, client_matches_app,
    process_pids, terminal_command,
    DESKTOP_DIRS, get_appimage_directories,
    launch_action, launch_app, load_hidden_apps, parse_desktop_files,
    save_hidden_apps,
)
from palette import ThemePalette
import pidfile
import shortcuts


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


atexit.register(cleanup_pid)


def hyprland_rounding():
    try:
        result = subprocess.run(["hyprctl", "getoption", "decoration:rounding", "-j"],
                                capture_output=True, text=True, timeout=2)
        return max(0, min(int(json.loads(result.stdout).get("int", 0)), 24))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return 0


# Non-letter keys offered on the shortcut page, as GDK/Hyprland key names.
OTHER_KEYS = ("Return", "space", "BackSpace", "Delete", "Home", "End")


PLUGIN_DIR = Path(__file__).resolve().parent


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
        self.hidden_ids = load_hidden_apps()
        self.show_hidden = False
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

    def do_shutdown(self):
        cleanup_pid()
        super().do_shutdown()

    def do_activate(self):
        if self.window is None:
            self.build_window()
            self.hold()  # Keep the singleton alive while its popup is hidden.
            signal.signal(signal.SIGUSR1, lambda *_: GLib.idle_add(self.toggle))
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
        back_button = Gtk.Button(icon_name="go-previous-symbolic")
        back_button.set_tooltip_text("Back to apps (Esc)")
        back_button.add_css_class("flat")
        back_button.set_visible(False)
        back_button.connect("clicked", lambda *_: self.close_shortcut_page())
        header.append(back_button)
        title = Gtk.Label(label="Apps")
        title.add_css_class("launcher-title")
        title.set_hexpand(True)
        title.set_halign(Gtk.Align.START)
        header.append(title)
        hidden_button = Gtk.Button(icon_name="view-reveal-symbolic")
        hidden_button.set_tooltip_text("Show hidden apps")
        hidden_button.add_css_class("flat")
        hidden_button.add_css_class("launcher-header-btn")
        hidden_button.connect("clicked", self.toggle_hidden)
        header.append(hidden_button)
        card.append(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_size_request(420, 50)
        scroller.set_max_content_height(590)
        scroller.set_propagate_natural_height(True)
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
        self.title, self.hidden_button = title, hidden_button
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
        .launcher-menu-item {{
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
        .launcher-menu-item:hover {{
            background: {sel_hex};
        }}
        .launcher-menu-item:hover label {{
            color: {sel_fg};
            font-weight: 600;
        }}
        .launcher-menu-item:hover image {{
            color: {sel_fg};
        }}
        .launcher-menu-item.destructive label {{
            color: #ff9999;
        }}
        .launcher-menu-item.destructive image {{
            color: #ff9999;
        }}
        .launcher-menu-item.destructive:hover {{
            background: {colors['red']};
        }}
        .launcher-menu-item.destructive:hover label,
        .launcher-menu-item.destructive:hover image {{
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
        .launcher-menu-item:hover label.launcher-shortcut-chip {{
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
        .launcher-menu-item.uninstall {{ background: alpha({error_hex}, 0.16); }}
        .launcher-menu-item.uninstall label, .launcher-menu-item.uninstall image {{ color: {error_hex}; font-weight: 700; }}
        .launcher-menu-item.uninstall:hover {{ background: {error_hex}; }}
        .launcher-menu-item.uninstall:hover label, .launcher-menu-item.uninstall:hover image {{ color: #ffffff; }}
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

    def toggle(self):
        if self.window and self.window.get_visible():
            self.hide()
        elif code_stamp() != self.code_stamp:
            self.restart()
        else:
            self.open()
        return GLib.SOURCE_REMOVE

    def restart(self):
        """Replace this process with the updated code. The PID stays, so the
        toggle script still finds it; the new process opens on start."""
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

    def on_app_dir_changed(self, *_args):
        # Package managers touch many files at once; act once things settle.
        if self.apps_changed_source:
            GLib.source_remove(self.apps_changed_source)
        self.apps_changed_source = GLib.timeout_add(700, self.reload_apps)

    def reload_apps(self):
        self.apps_changed_source = 0
        if not (self.window and self.window.get_visible()) or self.shortcut_app is not None:
            return GLib.SOURCE_REMOVE  # Picked up on the next open.
        if self.actions_menu_open():
            self.apps_changed_source = GLib.timeout_add(700, self.reload_apps)
            return GLib.SOURCE_REMOVE
        row = self.rows.get_selected_row()
        selected = row.app["desktop_id"] if row is not None and hasattr(row, "app") else None
        try:
            self.apps = parse_desktop_files()
            self.prune_uninstalled_shortcuts()
            self.refresh_list(selected_id=selected)
        except Exception as e:
            print("Error reloading apps:", e, file=sys.stderr)
        return GLib.SOURCE_REMOVE

    def prune_uninstalled_shortcuts(self):
        """Drop launcher shortcuts of uninstalled apps, so their keys do not
        stay dead and the bindings they replaced work again."""
        state = copy.deepcopy(self.shortcut_state)
        removed = shortcuts.prune_missing(state)
        if not removed:
            return
        try:
            shortcuts.save_and_apply(state)
        except (RuntimeError, OSError) as error:
            print("Could not drop shortcuts of removed apps:", error, file=sys.stderr)
            return
        self.shortcut_state = shortcuts.load_state()
        self.list_bindings = None
        keys = ", ".join(f"{entry['name']} ({', '.join(shortcuts.combo_label(c) for c in entry['shortcuts']) or 'turned-off keys'})"
                         for entry in removed)
        try:
            subprocess.Popen(["notify-send", "-a", "Simple Launcher", "Shortcuts of removed apps dropped", keys],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

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
            if not self.actions_menu_open() and self.refresh_running():
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
        for app in self.apps:
            is_hidden = app["desktop_id"] in self.hidden_ids
            if is_hidden and not self.show_hidden:
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

            if is_hidden:
                badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
                badge.add_css_class("launcher-hidden-badge")
                badge.set_valign(Gtk.Align.CENTER)
                eye_icon = Gtk.Image.new_from_icon_name("view-conceal-symbolic")
                eye_icon.set_pixel_size(11)
                badge.append(eye_icon)
                badge_lbl = Gtk.Label(label="Hidden")
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
            actions.set_tooltip_text("App actions")
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
        launch_app(row.app)
        self.hide()

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
                       css=None):
            item = Gtk.Button()
            item.add_css_class("flat")
            item.add_css_class("launcher-menu-item")
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
            item.set_child(item_box)
            item.connect("clicked", lambda *_: callback() if keep_open else (popover.popdown(), callback()))
            box.append(item)

        add_action("Open", lambda: (launch_app(app), self.hide()), "media-playback-start-symbolic",
                   shortcut=chip_for(""))
        for action in app.get("actions", []):
            add_action(action["name"], lambda action_id=action["id"]:
                       (launch_action(app, action_id), self.hide()), "system-run-symbolic",
                       shortcut=chip_for(action["id"]))
        def add_separator():
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            separator.add_css_class("launcher-menu-separator")
            box.append(separator)

        add_separator()
        add_action("Shortcut…", lambda: self.open_shortcut_page(app),
                   "preferences-desktop-keyboard-shortcuts-symbolic")
        add_separator()
        hidden = app["desktop_id"] in self.hidden_ids
        add_action("Unhide from list" if hidden else "Hide from list",
                   lambda: self.set_hidden(app, not hidden),
                   "view-reveal-symbolic" if hidden else "view-conceal-symbolic")
        if self.app_state(app) == "active":
            add_action("Close", lambda: self.close_app(app), "window-close-symbolic", is_destructive=True)
        if app.get("desktop_path") and shutil.which("omarchy-remove-launcher-entry"):
            add_separator()
            add_action("Uninstall…", lambda: self.confirm_uninstall(app), "user-trash-symbolic",
                       keep_open=True, css="uninstall")
        popover.set_child(box)
        popover.popup()

    def confirm_uninstall(self, app):
        """Omarchy's uninstall: the same question, then omarchy-remove-launcher-entry,
        which decides how (web app, TUI, own entry, pacman or Flatpak)."""
        popover = self.actions_popover
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(8)
        message = Gtk.Label(label=f"Do you want to uninstall {app['name']}?", xalign=0, wrap=True,
                            max_width_chars=32)
        box.append(message)
        buttons = Gtk.Box(spacing=6)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("sc-small")
        cancel.connect("clicked", lambda *_: popover.popdown())
        buttons.append(cancel)
        confirm = Gtk.Button(label="Uninstall")
        confirm.add_css_class("sc-primary")
        confirm.add_css_class("sc-danger-btn")
        confirm.connect("clicked", lambda *_: (popover.popdown(), self.uninstall_app(app)))
        buttons.append(confirm)
        box.append(buttons)
        popover.set_child(box)
        cancel.grab_focus()  # Enter must not uninstall by accident.

    def uninstall_app(self, app):
        # Package and Flatpak removal open a terminal that needs the focus.
        self.hide()
        try:
            subprocess.Popen(["omarchy-remove-launcher-entry", app["desktop_id"], app["name"]],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as error:
            self.notify(f"Could not uninstall {app['name']}", str(error))

    def notify(self, title, body=""):
        try:
            subprocess.Popen(["notify-send", "-a", "Simple Launcher", title, body],
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
            button.connect("clicked", lambda *_, k=key: self.set_shortcut_combo(self.selected_shortcut_mods(), k, None))
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

    def open_shortcut_page(self, app):
        if shortcuts.app_launch_argv(app) is None:
            return
        list_height = self.scroller.get_height()
        self.sc_page.set_size_request(-1, max(list_height, 380))
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
        self.set_shortcut_combo(shortcuts.load_default_mods(), None, None)
        self.back_button.set_visible(True)
        self.hidden_button.set_visible(False)
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
        self.stack.set_visible_child_name("list")
        self.update_title()
        if focus_list:
            self.refresh_list(selected_id=app["desktop_id"] if app else None)

    def set_shortcut_inhibit(self, enabled):
        # Ask Hyprland to deliver bound combos to the capture field instead of
        # running them. Not every compositor honors this for layer surfaces.
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
        owned_ids = {shortcuts.combo_id(c) for e in self.shortcut_state["apps"].values() for c in e["shortcuts"]}
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
        owned_ids = {shortcuts.combo_id(c) for e in self.shortcut_state["apps"].values() for c in e["shortcuts"]}
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
        if mods:
            # Every letter, coloured by who has it; taken ones can be rebound.
            self.sc_suggestions = [shortcuts.make_combo(mods, k) for k in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            self.sc_suggest_title.set_label("Keys with " + " + ".join(shortcuts.MOD_LABELS[m] for m in mods))
            for combo in self.sc_suggestions:
                chip = Gtk.Button(label=combo["key"])
                chip.add_css_class("sc-letter")
                self.style_key_chip(chip, combo)
                chip.connect("clicked", lambda *_, c=combo: self.set_shortcut_combo(c["mods"], c["key"], None))
                self.sc_suggest.append(chip)
            return self.mark_other_keys(mods)
        self.sc_suggestions = self.shortcut_bindings.suggestions(app, self.sc_app_id, None)
        self.sc_suggest_title.set_label("Free suggestions")
        for combo in self.sc_suggestions:
            chip = Gtk.Button(label=shortcuts.combo_label(combo).replace(" + ", "+"))
            chip.add_css_class("sc-suggest")
            chip.set_tooltip_text(f"Use {shortcuts.combo_label(combo)}")
            chip.connect("clicked", lambda *_, c=combo: self.set_shortcut_combo(c["mods"], c["key"], None))
            self.sc_suggest.append(chip)
        self.mark_other_keys(mods)
        if not self.sc_suggestions:
            none = Gtk.Label(label="Choose modifiers above to see every key.", xalign=0)
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
                button.set_tooltip_text("Choose modifiers first")

    def style_key_chip(self, chip, combo):
        """Colour a key button by who has the combo, and say so on hover."""
        level, owner = self.classify_shortcut(combo)
        chip.add_css_class("sc-keychip")
        for css in KEY_STYLES.values():
            chip.remove_css_class(css)
        chip.add_css_class(KEY_STYLES.get(level, "sc-free-key"))
        picked = self.sc_key and combo["key"].casefold() == self.sc_key.casefold()
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
        self.shortcut_state = shortcuts.load_state()
        self.shortcut_bindings = shortcuts.Bindings(self.shortcut_state)
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
                entry["shortcuts"] = [c for c in entry["shortcuts"] if shortcuts.combo_id(c) != cid]
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
            self.hidden_ids.add(app["desktop_id"])
        else:
            self.hidden_ids.discard(app["desktop_id"])
        save_hidden_apps(self.hidden_ids)
        self.refresh_list(selected_id=selected_id)

    def update_title(self):
        if not hasattr(self, "title"):
            return
        if getattr(self, "shortcut_app", None) is not None:
            self.title.set_label(self.shortcut_app["name"])
            return
        label = "Apps"
        if self.show_hidden:
            label += " (Showing Hidden)"
        if self.search_query:
            label += f" — {self.search_query}"
        self.title.set_label(label)

    def update_hidden_button_state(self):
        if not hasattr(self, "hidden_button"):
            return
        if self.show_hidden:
            self.hidden_button.set_icon_name("view-conceal-symbolic")
            self.hidden_button.set_tooltip_text("Hide hidden apps")
            self.hidden_button.add_css_class("launcher-toggle-active")
        else:
            self.hidden_button.set_icon_name("view-reveal-symbolic")
            self.hidden_button.set_tooltip_text("Show hidden apps")
            self.hidden_button.remove_css_class("launcher-toggle-active")
        self.update_title()

    def toggle_hidden(self, *_args):
        self.show_hidden = not self.show_hidden
        self.update_hidden_button_state()
        self.refresh_list()

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
