"""Any other desktop (GNOME, KDE, …): desktop.py's interface for App Manager
as an ordinary app.

- window: an app window with a header bar (the desktop draws its buttons);
  closing it quits
- theme: libadwaita's colours for the system's light or dark style and accent
  colour (Adw.StyleManager, else `gsettings` org.gnome.desktop.interface
  color-scheme / accent-color; light and blue when unknown); 12 px corners
- windows: unknown (Wayland desktops do not list other apps' windows), so an
  app counts as running by its process
- no shell plugins, dock, compositor shortcuts or menus watch; the system is
  updated in GNOME Software or KDE Discover when one is installed.
"""

import shutil

NAME = "generic"
APP_ID = "io.github.ubruckhaus.AppManager"
RADIUS = 12          # libadwaita's window corners

# libadwaita's palette (Adwaita named colours), light and dark.
ACCENTS = {"blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a", "yellow": "#c88800", "orange": "#ed5b00",
           "red": "#e62d42", "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396"}
LIGHT = {"background": "#fafafb", "foreground": "#323233", "muted": "#77767b", "selection": "#dedddf",
         "red": "#e01b24", "yellow": "#c88800", "orange": "#e66100", "green": "#26a269"}
DARK = {"background": "#222226", "foreground": "#ffffff", "muted": "#9a9996", "selection": "#3a3a3e",
        "red": "#ff7b63", "yellow": "#f8e45c", "orange": "#ffa348", "green": "#8ff0a4"}

# Update windows: (program, its arguments).
UPDATERS = (("gnome-software", ["--mode=updates"]), ("plasma-discover", ["--mode", "update"]))


def knows_windows() -> bool:
    return False  # Wayland desktops do not list other apps' windows.


def can_float() -> bool:
    return False  # Windows float anyway.


def exec_floating(argv) -> bool:
    return False


def app_id() -> str:
    return APP_ID


def popup() -> bool:
    return False


def _gsetting(key: str) -> str:
    from launcher_core import read_bounded_output
    gsettings = shutil.which("gsettings")
    if gsettings is None:
        return ""
    out = read_bounded_output([gsettings, "get", "org.gnome.desktop.interface", key], max_bytes=1024, timeout=2)
    return (out or "").strip().strip("'")


def _adw_style():
    """(dark, accent hex) from libadwaita's style manager (the system's style, also
    through the desktop portal on KDE), or None before there is a display (tests)."""
    try:
        import gi
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk
        if Gdk.Display.get_default() is None or not Adw.is_initialized():
            return None  # Not this process's style manager (the dock): gsettings then.
        manager = Adw.StyleManager.get_default()
        rgba = manager.get_accent_color_rgba()
        accent = "#%02x%02x%02x" % tuple(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue))
        return manager.get_dark(), accent
    except (ImportError, ValueError, AttributeError):
        return None


def theme_colors(path=None) -> dict:
    """libadwaita's colours for the system style; path: a TOML file instead (tests)."""
    if path is not None:
        import desktop_omarchy
        return desktop_omarchy.theme_colors(path)
    style = _adw_style()
    if style is not None:
        dark, accent = style
    else:
        dark = _gsetting("color-scheme") == "prefer-dark"
        accent = ACCENTS.get(_gsetting("accent-color"), ACCENTS["blue"])
    colors = dict(DARK if dark else LIGHT)
    colors["accent"] = accent
    return colors


def window_rounding():
    return RADIUS


def rounding_source() -> str:
    return "desktop"


def clients():
    return None


def close_window(_client):
    pass


def has_plugins() -> bool:
    return False


def has_dock() -> bool:
    return False


def has_shortcuts() -> bool:
    return False


def watches_menus() -> bool:
    return False


def system_update():
    for program, args in UPDATERS:
        path = shutil.which(program)
        if path:
            return [path, *args]
    return None

