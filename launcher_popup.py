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
from launcher_core import (
    app_process_name, background_process_names, client_matches_app,
    launch_action, launch_app, load_hidden_apps, parse_desktop_files,
    save_hidden_apps,
)
from palette import ThemePalette


def get_pid_file():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.isdir(runtime_dir):
        return Path(runtime_dir) / "simple-launcher-for-fools.pid"
    return Path("/tmp") / f"simple-launcher-for-fools-{os.getuid()}.pid"


def get_process_starttime(pid=None):
    if pid is None:
        pid = os.getpid()
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            content = f.read()
        after_comm = content[content.rfind(")") + 2:]
        fields = after_comm.split()
        return fields[19]
    except Exception:
        return ""


def write_pid_file():
    try:
        pid = os.getpid()
        st = get_process_starttime(pid)
        exe = str(Path(sys.executable).resolve())
        script = str(Path(__file__).resolve())
        get_pid_file().write_text(f"{pid}:{st}:{exe}:{script}\n")
    except Exception:
        pass


def cleanup_pid():
    try:
        pid = os.getpid()
        current_st = get_process_starttime(pid)
        pid_file = get_pid_file()
        if pid_file.exists():
            content = pid_file.read_text().strip()
            parts = content.split(":")
            stored_pid = parts[0]
            if stored_pid == str(pid):
                if len(parts) >= 2 and parts[1]:
                    if current_st and parts[1] != current_st:
                        return
                pid_file.unlink(missing_ok=True)
        legacy = Path("/tmp/simple-launcher-for-fools.pid")
        if legacy.exists() and legacy.read_text().strip().split(":")[0] == str(pid):
            legacy.unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(cleanup_pid)


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
        window.set_title("Simple Launcher for Fools")
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
        click.connect("pressed", self.on_overlay_click)
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
        card.append(scroller)
        rows = Gtk.ListBox()
        rows.set_selection_mode(Gtk.SelectionMode.SINGLE)
        rows.add_css_class("launcher-list")
        rows.connect("row-activated", self.activate_row)
        scroller.set_child(rows)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)
        window.connect("notify::is-active", self.on_active_changed)

        self.window, self.overlay, self.card = window, overlay, card
        self.rows, self.scroller = rows, scroller
        self.title, self.hidden_button = title, hidden_button
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
            border: 1px solid {colors['accent']}; border-radius: 12px; padding: 17px; }}
        .launcher-title {{ font-size: 15px; font-weight: 600; }}
        .launcher-list {{ background: transparent; }}
        .launcher-list row {{ border-radius: 10px; padding: 6px 8px; min-height: 44px; }}
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
            border-radius: 6px;
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
            border-radius: 10px;
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
            border-radius: 6px;
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
            border-radius: 4px;
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
            border-radius: 6px;
        }}
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
        hidden = app["desktop_id"] in self.hidden_ids
        add_action("Unhide from list" if hidden else "Hide from list",
                   lambda: self.set_hidden(app, not hidden),
                   "view-reveal-symbolic" if hidden else "view-conceal-symbolic")
        if self.app_state(app) == "active":
            add_action("Close", lambda: self.close_app(app), "window-close-symbolic", is_destructive=True)
        popover.set_child(box)
        popover.popup()

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
