"""The top bar icon: a small GNOME Shell extension (shell-extension/) whose
button opens Launcher, or closes it while it is open. Settings › Top bar icon
switches it on and off (off by default); install.sh links it into the folder
GNOME Shell loads extensions from, so it runs from this folder too.

GNOME Shell finds a newly linked extension only when it starts: switched on
before that, the icon appears after the next login."""

import os
from pathlib import Path

import desktop

UUID = "launcher-button@ubruckhaus.github.io"
SOURCE = Path(__file__).resolve().parent / "shell-extension" / UUID


def target_dir() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(data) / "gnome-shell" / "extensions" / UUID


def install():
    """Link the extension into GNOME Shell's folder (OSError when that fails).
    A folder already there (an older copy) is left alone."""
    target = target_dir()
    if target.is_symlink() and target.resolve() == SOURCE:
        return
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(SOURCE, target_is_directory=True)


def enabled(settings=None) -> bool:
    settings = settings or desktop.shell_settings()
    return settings is not None and UUID in settings.get_strv("enabled-extensions")


def set_enabled(on: bool, settings=None) -> bool:
    """Switch the extension on or off the way the Extensions app does: GNOME
    Shell follows its enabled-extensions list at once. False without GNOME
    Shell's settings."""
    settings = settings or desktop.shell_settings()
    if settings is None:
        return False
    enabled_list = [uuid for uuid in settings.get_strv("enabled-extensions") if uuid != UUID]
    disabled_list = [uuid for uuid in settings.get_strv("disabled-extensions") if uuid != UUID]
    if on:
        enabled_list.append(UUID)
    else:
        disabled_list.append(UUID)
    settings.set_strv("enabled-extensions", enabled_list)
    settings.set_strv("disabled-extensions", disabled_list)
    return True


def user_extensions_off(settings=None) -> bool:
    """GNOME's switch for all user extensions (Extensions app) is off."""
    settings = settings or desktop.shell_settings()
    return settings is not None and settings.get_boolean("disable-user-extensions")


def known_to_shell():
    """True when the running GNOME Shell has found the extension, False when
    it has not (linked after it started), None when it cannot be asked."""
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.gnome.Shell.Extensions", "/org/gnome/Shell/Extensions",
                              "org.gnome.Shell.Extensions", "GetExtensionInfo", GLib.Variant("(s)", (UUID,)),
                              GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        return None
    return bool(reply.unpack()[0])
