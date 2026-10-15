"""App Manager's dock: settings, pinned apps and what the dock shows.
No GTK here, so it can be tested; dock.py draws it.

Settings live in App Manager's settings.json (keys "dock_*"); the dock
process watches the file, so changes in App Manager apply at once."""
import json
import subprocess
from pathlib import Path

from launcher_core import (_load_settings, _save_setting, _save_settings, app_window_classes, client_matches_app,
                           read_bounded_output)

HYPRCTL_MAX_BYTES = 4 * 1024 * 1024  # Window titles come from web pages: never read unbounded.

POSITIONS = ("bottom", "top", "left", "right")
ALIGNS = ("start", "center", "end")
OPPOSITE = {"bottom": "top", "top": "bottom", "left": "right", "right": "left"}
SHELL_FILE = Path.home() / ".config/omarchy/shell.json"
HEIGHTS = (32, 40, 48, 56, 64, 72, 80, 96)          # The dock band, in pixels.
ICON_SIZES = (24, 32, 40, 48, 56, 64, 72, 80, 96)   # Icons larger than the band stick out of it.
MAGNIFY = (100, 125, 150, 175, 200)                 # Percent on hover; 100: off.
SPACINGS = (0, 2, 4, 6, 8, 12, 16)                  # The gap between two dock icons, in pixels.
INDICATORS = ("dot", "line")                        # How running apps are marked.
INDICATOR_COLORS = ("theme", "foreground", "red", "yellow", "green", "orange")  # Palette keys; theme: accent.
CHOICES = {"dock_height": HEIGHTS, "dock_icon_size": ICON_SIZES, "dock_magnify": MAGNIFY, "dock_spacing": SPACINGS,
           "dock_position": POSITIONS, "dock_align": ALIGNS,
           "dock_indicator": INDICATORS, "dock_indicator_color": INDICATOR_COLORS}
ITEM_EXTRA = 14     # Around an icon: the button's padding and the room for the running dot.
BOUNCE = 14         # How far an icon jumps when its app wants attention.
DEFAULTS = {
    "dock_enabled": False,
    "dock_position": "bottom",
    "dock_align": "center",
    "dock_height": 48,
    "dock_icon_size": 32,
    "dock_spacing": 6,
    "dock_magnify": 100,
    "dock_bounce": False,
    "dock_indicator_shown": True,
    "dock_indicator": "line",
    "dock_indicator_color": "theme",
    "dock_autohide": False,
    "dock_full_width": False,
    "dock_rounded": False,
    "dock_transparent": False,
    "dock_pinned": [],
}
MAX_PINNED = 60


def settings(path=None):
    """The dock settings, each one checked; anything odd falls back to its default."""
    raw = _load_settings(path)
    result = dict(DEFAULTS)
    for key in ("dock_enabled", "dock_autohide", "dock_full_width", "dock_rounded", "dock_transparent",
                "dock_bounce", "dock_indicator_shown"):
        if isinstance(raw.get(key), bool):
            result[key] = raw[key]
    for key, allowed in CHOICES.items():
        value = raw.get(key)
        if value in allowed and not isinstance(value, bool):
            result[key] = value
    pinned = raw.get("dock_pinned")
    if isinstance(pinned, list):
        result["dock_pinned"] = list(dict.fromkeys(
            p for p in pinned if isinstance(p, str) and 0 < len(p) <= 300))[:MAX_PINNED]
    return result


# What "Reset to defaults" sets back, the dock itself off included; the
# pinned apps stay for when it is switched on again.
RESET_KEYS = ("dock_enabled", "dock_position", "dock_align", "dock_height", "dock_icon_size", "dock_spacing",
              "dock_magnify", "dock_bounce",
              "dock_autohide", "dock_full_width", "dock_rounded", "dock_transparent", "dock_indicator_shown",
              "dock_indicator",
              "dock_indicator_color")


def reset(path=None):
    _save_settings({key: DEFAULTS[key] for key in RESET_KEYS}, path)


def set_setting(key, value, path=None):
    if key not in DEFAULTS or key == "dock_pinned":
        raise KeyError(key)
    if key in CHOICES:
        if value not in CHOICES[key] or isinstance(value, bool):
            raise ValueError(value)
        _save_setting(key, value, path)
    else:
        _save_setting(key, bool(value), path)


def bar_position(path=None):
    """The edge Omarchy's bar is on (shell.json "bar.position"), "top" by default."""
    try:
        with open(Path(path or SHELL_FILE), "rb") as fh:
            data = json.loads(fh.read(1024 * 1024) or b"{}")
        position = data.get("bar", {}).get("position") if isinstance(data, dict) else None
    except (OSError, ValueError, AttributeError):
        position = None
    return position if position in POSITIONS else "top"


def edge(dock, bar):
    """The edge the dock really uses: never the bar's one (the opposite then)."""
    return OPPOSITE[dock["dock_position"]] if dock["dock_position"] == bar else dock["dock_position"]


def sizes(dock):
    """(band, item, room): the band's thickness, an item's (icon, padding,
    dot) and the window's whole thickness, with room above the band for
    icons that stick out, grow on hover or bounce."""
    band = dock["dock_height"]
    item = dock["dock_icon_size"] + ITEM_EXTRA
    grow = round(dock["dock_icon_size"] * (dock["dock_magnify"] - 100) / 100)
    room = max(band, item + grow + (BOUNCE if dock["dock_bounce"] else 0))
    return band, item, room


def hyprctl_json(*args, run=read_bounded_output):
    try:
        out = run(["hyprctl", *args, "-j"], max_bytes=HYPRCTL_MAX_BYTES, timeout=2)
        return json.loads(out) if out else None
    except (OSError, ValueError):
        return None


def dock_rect(layers, monitors):
    """The dock's surface on screen: (x, y, w, h) in global logical pixels,
    plus the monitor's origin, from `hyprctl layers -j` / `monitors -j`; None
    when no dock is mapped."""
    if not isinstance(layers, dict):
        return None
    origins = {m.get("name"): (m.get("x", 0), m.get("y", 0)) for m in monitors or [] if isinstance(m, dict)}
    for name, monitor in layers.items():
        levels = monitor.get("levels", {}) if isinstance(monitor, dict) else {}
        for surfaces in levels.values():
            for layer in surfaces if isinstance(surfaces, list) else []:
                if isinstance(layer, dict) and layer.get("namespace") == "app-manager-dock":
                    try:
                        rect = tuple(int(layer[k]) for k in ("x", "y", "w", "h"))
                    except (KeyError, TypeError, ValueError):
                        return None
                    return rect, origins.get(name, (0, 0))
    return None


def inside(point, rect):
    x, y, w, h = rect
    return isinstance(point, dict) and x <= point.get("x", -1) < x + w and y <= point.get("y", -1) < y + h


def indicator_color(dock, colors):
    """The running indicator's colour from the theme palette."""
    key = dock.get("dock_indicator_color", "theme")
    return colors.get("accent" if key == "theme" else key) or colors.get("accent", "#888888")


def has_new_window_action(app):
    """The app's .desktop file has its own new-window action (browsers)."""
    return any(str(a.get("id", "")).casefold().replace("_", "-") in ("new-window", "newwindow")
               for a in app.get("actions") or [])


def split_events(buffer, chunk, skipping, limit=4096):
    """Hyprland's event stream, split into lines without ever holding a long
    one: (complete lines, the unfinished rest, skipping). A line longer than
    limit (a window title from a web page) is dropped up to its end."""
    *lines, rest = (buffer + chunk).split(b"\n")
    out = []
    for line in lines:
        if skipping:
            skipping = False  # The end of a line that was too long.
            continue
        if len(line) <= limit:
            out.append(line.decode("utf-8", "replace"))
    if len(rest) > limit:
        rest, skipping = b"", True
    return out, rest, skipping


def pinned(path=None):
    return settings(path)["dock_pinned"]


def set_pinned(ids, path=None):
    _save_setting("dock_pinned", list(dict.fromkeys(ids))[:MAX_PINNED], path)


def pin(desktop_id, path=None):
    ids = pinned(path)
    if desktop_id not in ids:
        set_pinned(ids + [desktop_id], path)


def unpin(desktop_id, path=None):
    set_pinned([i for i in pinned(path) if i != desktop_id], path)


def move_pinned(ids, desktop_id, target_id, after=False):
    """ids with desktop_id moved right before (or after) target_id."""
    rest = [i for i in ids if i != desktop_id]
    if target_id not in rest:
        return ids
    rest.insert(rest.index(target_id) + (1 if after else 0), desktop_id)
    return rest


# What App Manager offers when an app is dragged out of it.
DROP_PREFIX = "app-manager-entry:"
MAX_DROP_BYTES = 512


def dropped_id(data):
    """The desktop id in a drop from App Manager, else None (bytes, read
    with a cap of MAX_DROP_BYTES; anything else is ignored)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_DROP_BYTES:
        return None
    try:
        text = bytes(data).decode("utf-8").strip().rstrip("\x00")
    except UnicodeDecodeError:
        return None
    if not text.startswith(DROP_PREFIX):
        return None
    desktop_id = text[len(DROP_PREFIX):]
    return desktop_id if 0 < len(desktop_id) <= 300 and "\n" not in desktop_id else None


def insert_pinned(ids, desktop_id, target_id=None, after=False):
    """ids with desktop_id placed before (or after) target_id; at the end
    without one. Already pinned: moved there."""
    rest = [i for i in ids if i != desktop_id]
    index = rest.index(target_id) + (1 if after else 0) if target_id in rest else len(rest)
    rest.insert(index, desktop_id)
    return rest[:MAX_PINNED]


def clients(run=read_bounded_output):
    """Hyprland's windows (hyprctl clients -j), [] when it does not answer."""
    data = hyprctl_json("clients", run=run)
    return [c for c in data if isinstance(c, dict) and c.get("mapped", True)] if isinstance(data, list) else []


def windows_of(app, all_clients):
    """The app's windows, most recently focused first."""
    classes = app_window_classes(app)
    found = [c for c in all_clients if client_matches_app(c, app, classes)]
    return sorted(found, key=lambda c: c.get("focusHistoryID", 1 << 30))


def items(apps, pinned_ids, all_clients):
    """[(app, windows, is_pinned)]: the pinned apps in their order, then
    the other apps that have windows open, in window order."""
    by_id = {a["desktop_id"]: a for a in apps if a.get("desktop_id")}
    result = []
    for desktop_id in pinned_ids:
        app = by_id.get(desktop_id)
        if app is not None:
            result.append((app, windows_of(app, all_clients), True))
    pinned_set = set(pinned_ids)
    running = []
    for app in apps:
        if app.get("desktop_id") in pinned_set or "tray_id" in app:
            continue
        found = windows_of(app, all_clients)
        if found:
            running.append((min(c.get("focusHistoryID", 1 << 30) for c in found), app, found))
    # Most recently used first would reshuffle on every focus change: keep a
    # stable order by name instead.
    for _order, app, found in sorted(running, key=lambda item: item[1]["name"].casefold()):
        result.append((app, found, False))
    return result
