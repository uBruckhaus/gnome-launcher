"""Everything that depends on the desktop, behind one small interface.

The rest of App Manager asks this module, never Omarchy or Hyprland directly
for these things: how the window is shown, the theme's colours and window
rounding, the windows that are open, and which of the Omarchy-only features
(shell plugins, the dock, Hyprland shortcuts, Omarchy's update) exist here.

Two implementations: desktop_omarchy (Omarchy, Hyprland: the bar popup) and
desktop_generic (GNOME, KDE, …: an ordinary app window in GNOME's look).
current() picks one at start: APP_MANAGER_DESKTOP=omarchy|generic decides
when set (for testing), else Omarchy when its files are there
(/usr/share/omarchy or $OMARCHY_PATH), else generic.

The interface (each implementation provides all of it):

    NAME                          "omarchy" or "generic"
    app_id() -> str               the GTK application id, window class and icon name
    popup() -> bool               a full-screen layer-shell popup (Omarchy's bar) or an app window
    theme_colors() -> dict        the theme's colours (palette.DEFAULT_COLORS keys)
    window_rounding() -> int|None the windows' corner radius, None when unknown
    rounding_source() -> str      who set it: "user", "theme", "default" or "desktop"
    clients() -> list | None      the open windows (hyprctl clients -j), None when unknown
    knows_windows() -> bool       clients() lists the windows (else a running app counts as open)
    close_window(client)          close one of them
    can_float() -> bool           windows tile, so "Open in floating window" is offered
    exec_floating(argv) -> bool   start argv with its window floating
    has_plugins() -> bool         shell plugins and tray icons (the Plugins page)
    has_dock() -> bool            App Manager's own dock
    has_shortcuts() -> bool       global shortcuts in the compositor's key binds
    watches_menus() -> bool       other menus opening close the popup (LayerWatch)
    system_update() -> list|None  the command that updates the system in a window
"""

import os
from pathlib import Path

OMARCHY_ROOT = Path("/usr/share/omarchy")
LOOKS = ("omarchy", "adwaita")


def detect(environ=None) -> str:
    """"omarchy" or "generic"."""
    environ = os.environ if environ is None else environ
    wanted = environ.get("APP_MANAGER_DESKTOP", "")
    if wanted in ("omarchy", "generic"):
        return wanted
    omarchy = Path(environ.get("OMARCHY_PATH") or OMARCHY_ROOT)
    return "omarchy" if omarchy.is_dir() else "generic"


_current = None


def current():
    """The implementation for this desktop (chosen once)."""
    global _current
    if _current is None:
        if detect() == "omarchy":
            import desktop_omarchy as chosen
        else:
            import desktop_generic as chosen
        _current = chosen
    return _current


_adwaita = None


def adwaita() -> bool:
    """GNOME's look (libadwaita, Adwaita colours): always outside Omarchy,
    on Omarchy when Settings › Look says so. Read once: changing the look
    restarts the app."""
    global _adwaita
    if _adwaita is None:
        if current().NAME != "omarchy":
            _adwaita = True
        else:
            from launcher_core import saved_look
            _adwaita = saved_look() == "adwaita"
    return _adwaita


def __getattr__(name):
    """desktop.<anything in the interface> → the current implementation's."""
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(current(), name)
