#!/usr/bin/env python3
"""Simple Launcher - tray icons (StatusNotifierItem) and their menus.

Used by the launcher's widget list, and as a command for shortcuts:

    tray.py <item id>            activate the item (e.g. show Steam)
    tray.py <item id> <action>   click a menu entry, e.g. "library" or
                                 "all-connections.germany" (see action_id)

Tray items get a new D-Bus address every time their app starts, so a
shortcut names the item by its Id and the entry by its labels.
"""

import re
import sys

from gi.repository import Gio, GLib

WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
MENU_INTERFACE = "com.canonical.dbusmenu"
TIMEOUT_MS = 1500
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _call(bus, name, path, interface, method, args=None, reply=None):
    return bus.call_sync(name, path, interface, method, args, GLib.VariantType(reply) if reply else None,
                         Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)


def _properties(bus, name, path):
    reply = _call(bus, name, path, "org.freedesktop.DBus.Properties", "GetAll",
                  GLib.Variant("(s)", (ITEM_INTERFACE,)), "(a{sv})")
    return reply.unpack()[0]


def clean_label(label):
    """Drop GTK mnemonic underscores: "_Quit" -> "Quit", "a__b" -> "a_b"."""
    return re.sub(r"_(.)", r"\1", str(label or "")).strip()


def slug(label):
    return re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or "item"


def action_id(trail):
    """A stable id for a menu entry from its labels, e.g. "settings.notifications"."""
    return ".".join(slug(label) for label in trail)[:64]


def _menu_nodes(children, trail=()):
    nodes = []
    for child in children:
        node_id, props, grandchildren = child
        if props.get("visible", True) is False:
            continue
        if props.get("type") == "separator":
            nodes.append({"separator": True})
            continue
        label = clean_label(props.get("label", ""))
        if not label:
            continue
        path = (*trail, label)
        nodes.append({
            "id": int(node_id),
            "label": label,
            "action": action_id(path),
            "enabled": props.get("enabled", True) is not False,
            "toggle": props.get("toggle-type", "") if props.get("toggle-type") in ("checkmark", "radio") else "",
            "checked": props.get("toggle-state", 0) == 1,
            "children": _menu_nodes(grandchildren, path),
        })
    return nodes


def read_menu(bus, name, menu_path):
    """The item's menu as nested nodes; [] when it has none."""
    try:
        # Some apps fill their menu only when it is about to be shown.
        _call(bus, name, menu_path, MENU_INTERFACE, "AboutToShow", GLib.Variant("(i)", (0,)), "(b)")
    except GLib.Error:
        pass
    try:
        reply = _call(bus, name, menu_path, MENU_INTERFACE, "GetLayout",
                      GLib.Variant("(iias)", (0, -1, [])), "(u(ia{sv}av))")
    except GLib.Error:
        return []
    return _menu_nodes(reply.unpack()[1][2])


def flat_actions(nodes, trail=()):
    """Clickable entries for the shortcut page: {"id", "name"}."""
    actions = []
    for node in nodes:
        if node.get("separator") or not node["enabled"]:
            continue
        path = (*trail, node["label"])
        if node["children"]:
            actions.extend(flat_actions(node["children"], path))
        else:
            actions.append({"id": node["action"], "name": " › ".join(path), "exec": ""})
    return actions


def _registered(bus):
    try:
        reply = _call(bus, *WATCHER[:2], "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (WATCHER[2], "RegisteredStatusNotifierItems")), "(v)")
    except GLib.Error:
        return []
    return list(reply.unpack()[0])


def registered():
    """The registered tray entries right now, to notice icons come and go."""
    return tuple(sorted(str(entry) for entry in _registered(_bus())))


def _split(entry):
    """"<service>/<path>" (path optional) -> (service, path)."""
    index = entry.find("/")
    if index < 0:
        return entry, "/StatusNotifierItem"
    return entry[:index], entry[index:]


GENERIC_ID_RE = re.compile(r"^(chrome_status_icon|electron|tray|statusicon)[_-]?\d*$", re.IGNORECASE)


def item_key(raw_id, title, taken):
    """The name a tray icon is listed and found by. Electron apps (LM Studio,
    Antigravity, …) all say "chrome_status_icon_1", so those go by title."""
    if not (raw_id or title):
        return None
    if raw_id and ITEM_ID_RE.match(raw_id) and not GENERIC_ID_RE.match(raw_id) and raw_id not in taken:
        return raw_id
    key = slug(title or raw_id)[:64]
    return key if ITEM_ID_RE.match(key) and key not in taken else None


def _owner_programs(bus, name):
    """Program names of the process behind a tray icon: its executable, the
    name it was started as and its AppImage, to find the app it belongs to
    (LM Studio's icon only says "chrome_status_icon_1")."""
    import os
    try:
        pid = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "GetConnectionUnixProcessID", GLib.Variant("(s)", (name,)),
                            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, TIMEOUT_MS, None).unpack()[0]
    except GLib.Error:
        return set()
    names = set()
    try:
        # Only our own processes say which app an icon belongs to.
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return set()
        names.add(os.path.basename(os.readlink(f"/proc/{pid}/exe")))
        argv0 = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0].decode(errors="replace")
        names.add(os.path.basename(argv0))
        for line in open(f"/proc/{pid}/environ", "rb").read().split(b"\0"):
            key, _, value = line.decode(errors="replace").partition("=")
            if key in ("ARGV0", "APPIMAGE") and value:
                names.add(os.path.basename(value).removesuffix(".AppImage").removesuffix(".appimage"))
    except OSError:
        pass
    return {n.casefold() for n in names if n}


def _title(props):
    """Title, else the tooltip's title (Electron leaves Title empty)."""
    title = clean_label(props.get("Title") or "")
    if title:
        return title
    tooltip = props.get("ToolTip")
    try:
        return clean_label(tooltip[2]) if tooltip else ""
    except (IndexError, TypeError):
        return ""


def list_items(with_menus=True):
    """Tray items registered right now, as launcher list entries."""
    bus = _bus()
    items, seen = [], set()
    for entry in _registered(bus):
        name, path = _split(str(entry))
        try:
            props = _properties(bus, name, path)
        except GLib.Error:
            continue
        title = _title(props)
        programs = _owner_programs(bus, name)
        if not title and programs:
            title = sorted(programs)[0]
        item_id = item_key(str(props.get("Id") or ""), title, seen)
        if item_id is None:
            continue
        seen.add(item_id)
        menu_path = str(props.get("Menu") or "")
        menu = read_menu(bus, name, menu_path) if with_menus and menu_path.startswith("/") else []
        is_menu = bool(props.get("ItemIsMenu", False))
        items.append({
            "name": title or item_id,
            "tray_programs": programs,
            "tray_id": item_id,
            "tray_service": name,
            "tray_path": path,
            "tray_menu_path": menu_path,
            "tray_menu": menu,
            "tray_is_menu": is_menu,
            "exec": "",
            "icon": icon_file(props) or str(props.get("IconName") or "") or "application-x-executable",
            "desktop_path": "",
            "actions": flat_actions(menu),
            "description": "Tray icon — opens its menu" if is_menu else "Tray icon",
            "desktop_id": f"tray:{item_id}",
            "startup_class": "",
        })
    return sorted(items, key=lambda item: item["name"].lower())


def icon_file(props):
    """An icon in the item's own theme folder (Steam ships its tray icon so)."""
    from pathlib import Path
    theme_path, icon = str(props.get("IconThemePath") or ""), str(props.get("IconName") or "")
    if not theme_path or not icon or "/" in icon:
        return ""
    root = Path(theme_path)
    for candidate in (root / f"{icon}.png", root / f"{icon}.svg"):
        if candidate.is_file():
            return str(candidate)
    return ""


def activate(item):
    """Primary action, like a left click on the icon. False when the item
    has none (menu-only items such as NordVPN)."""
    try:
        _call(_bus(), item["tray_service"], item["tray_path"], ITEM_INTERFACE, "Activate",
              GLib.Variant("(ii)", (0, 0)))
        return True
    except GLib.Error:
        return False


def open_app(item_id):
    """Launch the app a tray icon belongs to (steam -> steam.desktop), for
    icons without a primary action. Running apps come to the front."""
    try:
        import gi
        gi.require_version("GioUnix", "2.0")
        from gi.repository import GioUnix
        info = GioUnix.DesktopAppInfo.new(f"{item_id}.desktop")
    except (ValueError, TypeError, ImportError):
        info = None
    if info is None:
        return False
    try:
        import subprocess
        subprocess.Popen(["uwsm-app", "--", info.get_filename()], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return bool(info.launch([], None))


def click(item, node_id):
    try:
        _call(_bus(), item["tray_service"], item["tray_menu_path"], MENU_INTERFACE, "Event",
              GLib.Variant("(isvu)", (int(node_id), "clicked", GLib.Variant("i", 0), 0)))
        return True
    except GLib.Error:
        return False


def find_node(nodes, action):
    for node in nodes:
        if node.get("separator"):
            continue
        if node["action"] == action and not node["children"]:
            return node
        found = find_node(node["children"], action)
        if found:
            return found
    return None


QUIT_RE = re.compile(r"^(quit|exit|close|beenden)\b", re.IGNORECASE)


def find_quit(nodes):
    """The app's own quit entry ("Quit LM Studio", "Exit Steam"), if any."""
    for node in nodes:
        if node.get("separator") or not node["enabled"]:
            continue
        if not node["children"] and QUIT_RE.match(node["label"]):
            return node
    return None


def run(item_id, action=""):
    """Shortcut entry point: activate an item or click one of its entries."""
    item = next((i for i in list_items(with_menus=bool(action)) if i["tray_id"] == item_id), None)
    if item is None:
        return False
    if not action:
        return activate(item) or open_app(item_id)
    node = find_node(item["tray_menu"], action)
    return bool(node and node["enabled"] and click(item, node["id"]))


def main(argv):
    if len(argv) not in (2, 3) or not ITEM_ID_RE.match(argv[1]):
        print("usage: tray.py <item id> [action]", file=sys.stderr)
        return 2
    return 0 if run(argv[1], argv[2] if len(argv) == 3 else "") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
