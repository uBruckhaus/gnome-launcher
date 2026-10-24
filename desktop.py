"""What GNOME Launcher asks of the GNOME desktop.

- theme: libadwaita's colours for the system's light or dark style and accent
  colour (Adw.StyleManager, else `gsettings` org.gnome.desktop.interface
  color-scheme / accent-color; light and blue when unknown); 12 px corners
- windows: unknown (Wayland does not list other apps' windows), so an app
  counts as running by its process
- the Dash: GNOME Shell's favourites (org.gnome.shell favorite-apps), which
  the Dash and Dash to Dock show; pinning there is "Pin to Dash"
- updates: GNOME Software's update page, when it is installed.
"""

import shutil

APP_ID = "io.github.ubruckhaus.Launcher"
RADIUS = 12          # libadwaita's window corners
SHELL_SCHEMA = "org.gnome.shell"

# libadwaita's palette (Adwaita named colours), light and dark.
ACCENTS = {"blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a", "yellow": "#c88800", "orange": "#ed5b00",
           "red": "#e62d42", "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396"}
LIGHT = {"background": "#fafafb", "foreground": "#323233", "muted": "#77767b", "selection": "#dedddf",
         "red": "#e01b24", "yellow": "#c88800", "orange": "#e66100", "green": "#26a269"}
DARK = {"background": "#222226", "foreground": "#ffffff", "muted": "#9a9996", "selection": "#3a3a3e",
        "red": "#ff7b63", "yellow": "#f8e45c", "orange": "#ffa348", "green": "#8ff0a4"}

# Update windows: (program, its arguments).
UPDATERS = (("gnome-software", ["--mode=updates"]),)


def _gsetting(key: str) -> str:
    from launcher_core import read_bounded_output
    gsettings = shutil.which("gsettings")
    if gsettings is None:
        return ""
    out = read_bounded_output([gsettings, "get", "org.gnome.desktop.interface", key], max_bytes=1024, timeout=2)
    return (out or "").strip().strip("'")


def _adw_style():
    """(dark, accent hex) from libadwaita's style manager, or None before
    there is a display (tests)."""
    try:
        import gi
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk
        if Gdk.Display.get_default() is None or not Adw.is_initialized():
            return None
        manager = Adw.StyleManager.get_default()
        rgba = manager.get_accent_color_rgba()
        accent = "#%02x%02x%02x" % tuple(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue))
        return manager.get_dark(), accent
    except (ImportError, ValueError, AttributeError):
        return None


def theme_colors() -> dict:
    """libadwaita's colours for the system style."""
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


def system_update():
    """The command that updates the system in a window: GNOME Software's
    update page, else a terminal running the package manager's full update
    (an AUR helper's when there is one), or None."""
    for program, args in UPDATERS:
        path = shutil.which(program)
        if path:
            return [path, *args]
    terminal = terminal_argv()
    if terminal is None or not shutil.which("pacman"):
        return None
    helper = next((h for h in ("paru", "yay") if shutil.which(h)), None)
    update = f"{helper} -Syu" if helper else "sudo pacman -Syu"
    return [*terminal, "sh", "-c", f"{update}; echo; read -r -p 'Press Enter to close. ' _"]


# Terminals and how each one runs a command (xdg-terminal-exec: the default one).
TERMINAL_EXEC = (("xdg-terminal-exec", []), ("ptyxis", ["--"]), ("kgx", ["--"]), ("gnome-terminal", ["--"]))


def terminal_argv():
    """argv prefix that opens the default terminal running a command, or None."""
    for program, args in TERMINAL_EXEC:
        path = shutil.which(program)
        if path:
            return [path, *args]
    return None


def shell_settings():
    """GNOME Shell's settings, or None without its schema (tests, other desktops)."""
    from gi.repository import Gio
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(SHELL_SCHEMA, True) is None:
        return None
    return Gio.Settings.new(SHELL_SCHEMA)


def favorites() -> list:
    """The desktop file names pinned to the Dash, in its order."""
    settings = shell_settings()
    return list(settings.get_strv("favorite-apps")) if settings is not None else []


def has_dash() -> bool:
    return shell_settings() is not None


def set_pinned(desktop_file: str, pinned: bool) -> bool:
    """Pin desktop_file ("org.gnome.Nautilus.desktop") to the Dash or take it
    off; False when GNOME Shell's settings are not there."""
    settings = shell_settings()
    if settings is None:
        return False
    current = list(settings.get_strv("favorite-apps"))
    if pinned and desktop_file not in current:
        current.append(desktop_file)
    elif not pinned:
        current = [name for name in current if name != desktop_file]
    else:
        return True
    settings.set_strv("favorite-apps", current)
    from gi.repository import Gio
    Gio.Settings.sync()
    return True
