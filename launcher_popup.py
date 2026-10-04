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
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell

# Once Gtk4LayerShell is loaded into this process by ld.so, remove it from
# LD_PRELOAD so child processes and launched applications (like Steam / pressure-vessel)
# do not inherit it and fail to start.
_preloads = [p for p in os.environ.get("LD_PRELOAD", "").split(":") if p and p != layer_shell_library]
if _preloads:
    os.environ["LD_PRELOAD"] = ":".join(_preloads)
else:
    os.environ.pop("LD_PRELOAD", None)

import atexit
import copy
from launcher_core import (
    app_process_name, background_process_names, client_matches_app,
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


class Launcher(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.omarchy.simplelauncherforfools")
        self.window = None
        self.apps = []
        self.hidden_ids = load_hidden_apps()
        self.show_hidden = False
        self.clients = []
        self.process_names = set()
        self.theme = ThemePalette()
        self.search_query = ""
        self.shortcut_state = shortcuts.load_state()
        self.shortcut_app = None
        self.shortcut_bindings = None

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
        hidden_button.connect("clicked", self.toggle_hidden)
        header.append(hidden_button)
        card.append(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_size_request(420, 50)
        scroller.set_max_content_height(590)
        scroller.set_propagate_natural_height(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
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
        .launcher-list row {{ border-radius: {radius}px; padding: 6px 8px; min-height: 44px; }}
        .launcher-list row:selected {{ background: {sel_hex}; }}
        .launcher-card label, .launcher-card button {{ color: {colors['foreground']}; }}
        .launcher-app-name {{ font-size: 13px; font-weight: 600; }}
        .launcher-app-desc {{ font-size: 11px; color: {muted_hex}; opacity: 0.85; }}
        .launcher-list row:selected .launcher-app-name {{ color: {sel_fg}; font-weight: 600; }}
        .launcher-list row:selected .launcher-app-desc {{ color: {sel_desc_fg}; opacity: 0.95; }}
        .launcher-list row:selected > box > button {{ color: {sel_fg}; }}
        .launcher-list row button.flat {{
            background: transparent;
            border: none;
            box-shadow: none;
            border-radius: {radius}px;
            padding: 4px;
        }}
        .launcher-list row button.flat:hover {{
            background: rgba(255, 255, 255, 0.1);
        }}
        .launcher-list row:selected button.flat:hover {{
            background: rgba(0, 0, 0, 0.12);
        }}
        .launcher-muted {{ color: {colors['muted']}; }}
        .launcher-running {{ color: {colors['red']}; }}

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
        .launcher-list row:selected .launcher-shortcut-chip {{ color: {sel_desc_fg}; border-color: alpha({sel_desc_fg}, 0.5); }}
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
        .sc-warn {{ color: {colors['yellow']}; background: alpha({colors['yellow']}, 0.12); }}
        .sc-danger {{ color: {colors['red']}; background: alpha({colors['red']}, 0.14); }}
        .sc-info {{ color: {muted_hex}; background: alpha({colors['foreground']}, 0.05); }}
        .sc-suggest {{ font-size: 11px; border-radius: {radius}px; padding: 3px 8px; min-height: 0;
            color: {colors['green']}; border: 1px solid alpha({colors['green']}, 0.5); background: transparent; }}
        .sc-suggest:hover {{ background: alpha({colors['green']}, 0.15); }}
        .sc-primary {{ background: {colors['accent']}; color: {colors['background']}; border-radius: {radius}px; padding: 4px 14px; }}
        .sc-primary.sc-warn-btn {{ background: {colors['yellow']}; color: {colors['background']}; }}
        .sc-primary.sc-danger-btn {{ background: {colors['red']}; color: #ffffff; }}
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
        .sc-suggest-on {{ background: alpha({colors['green']}, 0.25); }}
        .launcher-card button.sc-primary:disabled {{ background: alpha({colors['foreground']}, 0.08); }}
        .launcher-card button.sc-primary:disabled label {{ color: {muted_hex}; font-weight: 500; }}
        .launcher-card .sc-mod:checked label, .launcher-card button.sc-mod:checked {{ color: {colors['background']}; font-weight: 600; }}
        .launcher-card button.sc-primary label {{ color: {colors['background']}; font-weight: 600; }}
        .launcher-card button.sc-primary.sc-danger-btn label {{ color: #ffffff; }}
        .launcher-card button.sc-suggest label {{ color: {colors['green']}; }}
        .launcher-card .sc-free {{ color: {colors['green']}; }}
        .launcher-card .sc-warn {{ color: {colors['yellow']}; }}
        .launcher-card .sc-danger {{ color: {colors['red']}; }}
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
        if self.shortcut_app is not None:
            self.close_shortcut_page(focus_list=False)
        if self.window and self.window.get_visible():
            self.window.set_visible(False)

    def toggle(self):
        if self.window and self.window.get_visible():
            self.hide()
        else:
            self.open()
        return GLib.SOURCE_REMOVE

    def on_overlay_click(self, _gesture, _count, x, y):
        picked = self.overlay.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is None or not picked.is_ancestor(self.card):
            self.hide()

    def on_active_changed(self, window, _property):
        if window.get_visible() and not window.is_active():
            GLib.timeout_add(100, self.hide_if_inactive)

    def hide_if_inactive(self):
        if self.window and self.window.get_visible() and not self.window.is_active():
            self.hide()
        return GLib.SOURCE_REMOVE

    def select_row(self, row):
        if row:
            self.rows.select_row(row)
            row.grab_focus()

    def on_key(self, _controller, key, _code, state):
        if key == Gdk.KEY_Escape:
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
            if self.refresh_running():
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
        return previous != (self.clients, self.process_names)

    def app_state(self, app):
        if any(client_matches_app(client, app) for client in self.clients):
            return "active"
        if app_process_name(app) in self.process_names:
            return "background"
        return "stopped"

    def refresh_list(self, selected_id=None):
        self.update_title()
        selected = self.rows.get_selected_row()
        if selected_id is None:
            selected_id = selected.app.get("desktop_id") if selected else None
        selected_row = None
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        query = self.search_query.casefold().strip()
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
            line.append(icon)

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
            assigned = self.shortcut_state["apps"].get(shortcuts.app_key(app), {}).get("shortcuts", [])
            if assigned:
                chip = Gtk.Label(label=shortcuts.combo_label(assigned[0]).replace(" + ", "+"))
                chip.add_css_class("launcher-shortcut-chip")
                chip.set_valign(Gtk.Align.CENTER)
                chip.set_tooltip_text("Global shortcut" + ("s: " if len(assigned) > 1 else ": ")
                                      + ", ".join(shortcuts.combo_label(c) for c in assigned))
                line.append(chip)
            state = self.app_state(app)
            if state != "stopped":
                dot = Gtk.Label(label="●")
                dot.add_css_class("launcher-running" if state == "active" else "launcher-muted")
                dot.set_tooltip_text("Open windows" if state == "active" else "Running in background")
                line.append(dot)
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

    def show_actions(self, button, app):
        popover = Gtk.Popover()
        popover.add_css_class("launcher-popover")
        popover.set_parent(button)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        def add_action(label, callback, icon_name=None, is_destructive=False):
            item = Gtk.Button()
            item.add_css_class("flat")
            item.add_css_class("launcher-menu-item")
            if is_destructive:
                item.add_css_class("destructive")
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
            item.set_child(item_box)
            item.connect("clicked", lambda *_: (popover.popdown(), callback()))
            box.append(item)

        add_action("Open", lambda: (launch_app(app), self.hide()), "media-playback-start-symbolic")
        for action in app.get("actions", []):
            add_action(action["name"], lambda action_id=action["id"]:
                       (launch_action(app, action_id), self.hide()), "system-run-symbolic")
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
        popover.set_child(box)
        popover.popup()

    # --- shortcut page --------------------------------------------------------

    def build_shortcut_page(self):
        page = Gtk.ScrolledWindow()
        page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(2)
        box.set_margin_end(2)
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
        section("Add a shortcut")
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
        box.append(key_box)

        status = Gtk.Label(xalign=0, wrap=True)
        status.add_css_class("sc-status")
        box.append(status)

        suggest_title = section("Free keys")
        suggest = Gtk.FlowBox()
        suggest.set_selection_mode(Gtk.SelectionMode.NONE)
        suggest.set_max_children_per_line(8)
        suggest.set_min_children_per_line(4)
        suggest.set_column_spacing(4)
        suggest.set_row_spacing(4)
        suggest.set_homogeneous(False)
        box.append(suggest)

        apply = Gtk.Button(label="Assign")
        apply.add_css_class("sc-primary")
        apply.set_halign(Gtk.Align.END)
        apply.set_margin_top(4)
        apply.connect("clicked", lambda *_: self.apply_shortcut())
        box.append(apply)

        self.stack.add_named(page, "shortcut")
        self.sc_page, self.sc_current, self.sc_current_title = page, current, current_title
        self.sc_toggles, self.sc_key_box, self.sc_status = toggles, key_box, status
        self.sc_suggest, self.sc_suggest_title, self.sc_apply_button = suggest, suggest_title, apply
        self.sc_default_check = default_check
        self.sc_key, self.sc_keycode, self.sc_confirm, self.sc_syncing = None, None, False, False
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
        self.render_shortcut_current()
        # Start from the modifiers the app already uses (e.g. Super + Shift for
        # Brave), else the user's default (Super + Shift unless changed). The key stays open ("?") until the
        # user presses one or picks a free key, so Assign starts disabled.
        existing = self.active_app_shortcuts()
        mods = existing[0]["mods"] if existing else shortcuts.load_default_mods()
        self.set_shortcut_combo(mods, None, None)
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
        self.sc_confirm = False
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

    def active_app_shortcuts(self):
        """Combos that open this app right now: launcher ones, then hand-written."""
        entry = self.shortcut_state["apps"].get(self.sc_app_id, {"shortcuts": [], "disabled": []})
        disabled_ids = {shortcuts.combo_id(c) for c in entry["disabled"]}
        owned_ids = {shortcuts.combo_id(c) for e in self.shortcut_state["apps"].values() for c in e["shortcuts"]}
        external = [b["combo"] for b in self.shortcut_bindings.external_shortcuts(self.shortcut_app)
                    if shortcuts.combo_id(b["combo"]) not in disabled_ids | owned_ids]
        return list(entry["shortcuts"]) + external

    def render_shortcut_current(self):
        """Active shortcuts (any source) with Remove, then turned-off ones."""
        app, bindings = self.shortcut_app, self.shortcut_bindings
        entry = self.shortcut_state["apps"].get(self.sc_app_id, {"shortcuts": [], "disabled": []})
        while child := self.sc_current.get_first_child():
            self.sc_current.remove(child)

        def item(combo, source, note, button, tooltip, callback, dim=False):
            row = Gtk.Box(spacing=8)
            row.add_css_class("sc-item")
            if dim:
                row.add_css_class("sc-item-off")
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
            action = Gtk.Button(label=button) if len(button) > 1 else Gtk.Button(icon_name="window-close-symbolic")
            action.add_css_class("sc-small")
            action.set_tooltip_text(tooltip)
            action.set_valign(Gtk.Align.CENTER)
            action.connect("clicked", lambda *_: callback())
            row.append(action)
            self.sc_current.append(row)

        active = 0
        for combo in entry["shortcuts"]:
            note = f"Overrides “{combo['replaces']}”" if combo.get("replaces") else ""
            item(combo, "Launcher", note, "×", "Remove this shortcut"
                 + (f" — “{combo['replaces']}” works again" if combo.get("replaces") else ""),
                 lambda c=combo: self.remove_shortcut(c))
            active += 1
        disabled_ids = {shortcuts.combo_id(c) for c in entry["disabled"]}
        owned_ids = {shortcuts.combo_id(c) for e in self.shortcut_state["apps"].values() for c in e["shortcuts"]}
        for bind in bindings.external_shortcuts(app):
            cid = shortcuts.combo_id(bind["combo"])
            if cid in disabled_ids or cid in owned_ids:
                continue
            item(bind["combo"], bind["file"], "", "×",
                 f"Turn off — {bind['file']} stays unchanged and you can turn it back on",
                 lambda b=bind: self.disable_external_shortcut(b))
            active += 1
        if not active:
            empty = Gtk.Label(label="None yet — add one below.", xalign=0)
            empty.add_css_class("sc-empty")
            self.sc_current.append(empty)
        for combo in entry["disabled"]:
            item(combo, "bindings.lua", "Turned off", "Turn on",
                 "Use this shortcut from your config again", lambda c=combo: self.restore_external_shortcut(c),
                 dim=True)
        self.sc_current_title.set_label(f"Shortcuts for {app['name']}")

    def render_shortcut_suggestions(self):
        """Free keys for the selected modifiers; clicking one picks it."""
        app = self.shortcut_app
        mods = [mod for mod, toggle in self.sc_toggles.items() if toggle.get_active()]
        while child := self.sc_suggest.get_first_child():
            self.sc_suggest.remove(child)
        self.sc_suggestions = self.shortcut_bindings.suggestions(app, self.sc_app_id, mods or None)
        if mods:
            self.sc_suggest_title.set_label("Free with " + " + ".join(shortcuts.MOD_LABELS[m] for m in mods))
        else:
            self.sc_suggest_title.set_label("Free suggestions")
        for combo in self.sc_suggestions:
            text = shortcuts.combo_label({"mods": [], "key": combo["key"]}) if mods else \
                shortcuts.combo_label(combo).replace(" + ", "+")
            chip = Gtk.Button(label=text)
            chip.add_css_class("sc-suggest")
            if combo["key"] == self.sc_key and combo["mods"] == (mods or combo["mods"]):
                chip.add_css_class("sc-suggest-on")
            chip.set_tooltip_text(f"Use {shortcuts.combo_label(combo)}")
            chip.connect("clicked", lambda *_, c=combo: self.set_shortcut_combo(c["mods"], c["key"], None))
            self.sc_suggest.append(chip)
        if not self.sc_suggestions:
            none = Gtk.Label(label="No free letter with these modifiers — try another set.", xalign=0)
            none.add_css_class("sc-note")
            self.sc_suggest.append(none)

    def set_shortcut_combo(self, mods, key, keycode):
        self.sc_syncing = True
        for mod, toggle in self.sc_toggles.items():
            toggle.set_active(mod in mods)
        self.sc_syncing = False
        self.sc_key, self.sc_keycode, self.sc_confirm = key, keycode, False
        self.update_shortcut_status()

    def on_shortcut_mod_toggled(self, _toggle):
        if not self.sc_syncing:
            self.sc_confirm = False
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
        for css in ("sc-free", "sc-warn", "sc-danger", "sc-info"):
            self.sc_status.remove_css_class(css)
        self.sc_status.add_css_class(level)
        self.sc_status.set_label(text)
        for css in ("sc-warn-btn", "sc-danger-btn"):
            self.sc_apply_button.remove_css_class(css)
        if button_style:
            self.sc_apply_button.add_css_class(button_style)
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
        level, owner = self.shortcut_bindings.classify(combo, self.sc_app_id, self.sc_keycode)
        if level == shortcuts.FREE:
            tip = "" if "SUPER" in combo["mods"] else " Without Super, apps can no longer receive it."
            self.set_shortcut_status(f"✓ Free.{tip}", "sc-free", "Assign", None, True)
        elif level == shortcuts.SAME:
            self.set_shortcut_status(f"Already opens {name}.", "sc-info")
        elif level == shortcuts.LAUNCHER:
            self.set_shortcut_status(f"⚠ Already opens {owner}. Assigning moves it to {name}.",
                                     "sc-warn", "Move here", "sc-warn-btn", True)
        elif level == shortcuts.CUSTOM:
            self.set_shortcut_status(f"⚠ Used by your binding “{owner}”. It works again when you "
                                     "remove this shortcut.", "sc-warn", "Replace", "sc-warn-btn", True)
        elif self.sc_confirm:
            self.set_shortcut_status(f"⛔ Really turn off the Omarchy default “{owner}” while this "
                                     "shortcut exists?", "sc-danger", "Confirm override", "sc-danger-btn", True)
        else:
            self.set_shortcut_status(f"⛔ Omarchy default: “{owner}”. Better pick a free key.",
                                     "sc-danger", "Override…", "sc-danger-btn", True)

    def on_shortcut_key(self, controller, keyval, keycode, state):
        if self.shortcut_app is None:
            return False
        if keyval == Gdk.KEY_Escape:
            if self.sc_confirm:
                self.sc_confirm = False
                self.update_shortcut_status()
            else:
                self.close_shortcut_page()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
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
        self.render_shortcut_current()
        self.sc_key, self.sc_keycode, self.sc_confirm = None, None, False
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
        level, owner = self.shortcut_bindings.classify(combo, self.sc_app_id, self.sc_keycode)
        if level == shortcuts.SAME:
            return
        if level == shortcuts.OMARCHY and not self.sc_confirm:
            self.sc_confirm = True
            self.update_shortcut_status()
            return
        new_state = copy.deepcopy(self.shortcut_state)
        cid = shortcuts.combo_id(combo)
        if level == shortcuts.LAUNCHER:
            for entry in new_state["apps"].values():
                entry["shortcuts"] = [c for c in entry["shortcuts"] if shortcuts.combo_id(c) != cid]
        entry = self.shortcut_entry(new_state)
        entry["disabled"] = [c for c in entry["disabled"] if shortcuts.combo_id(c) != cid]
        replaces = owner if level in (shortcuts.CUSTOM, shortcuts.OMARCHY) else ""
        entry["shortcuts"].append({**combo, "replaces": replaces})
        self.commit_shortcuts(new_state, f"✓ Saved — {shortcuts.combo_label(combo)} opens "
                                         f"{self.shortcut_app['name']}.")

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
        entry["disabled"].append({**bind["combo"], "description": bind["description"]})
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
            if client_matches_app(client, app) and client.get("address", "").startswith("0x"):
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
