"""Groups for the group view: Office, Media, … on the app page; System,
Network, … on the plugin page. Each page has its own groups.

Every entry is in exactly one group. An entry the user never moved lands in
the group its categories suggest (desktop Categories for apps, the
manifest's bar category for plugins), or in Other when there is none or that
group was deleted; a move by hand is remembered by desktop id. Built-in
groups keep an empty name until renamed, so they follow the interface
language. Tray icons are not grouped: they head the plugin page.
"""
import json
import os
import re
from pathlib import Path

from i18n import _, N_

GROUPS_FILE = Path.home() / ".config/applauncher/groups.json"
OTHER = "other"        # Always there, always last; not renamed or deleted.
RUNNING = "_running"   # The "Running" section on top of the group view.
PAGES = ("apps", "plugins")
# The fixed sections of the "by origin" view.
ORIGINS = ("o-defaults", "o-omarchy", "o-other")
MAX_GROUPS = 50
MAX_MEMBERS = 5000
MAX_NAME = 40
MAX_FILE_BYTES = 256 * 1024

BUILTIN_NAMES = {
    "office": N_("Office"),
    "internet": N_("Internet"),
    "media": N_("Media"),
    "graphics": N_("Graphics"),
    "development": N_("Development"),
    "games": N_("Games"),
    "system": N_("System"),
    "utilities": N_("Utilities"),
    "network": N_("Network"),
    "home": N_("Home"),
    "tools": N_("Tools"),
}
BUILTIN_ORDER = {
    "apps": ("office", "internet", "media", "graphics", "development", "games", "system", "utilities"),
    "plugins": ("system", "network", "media", "home", "tools"),
}
# A plugin's barWidget.category (free text in manifests) to a plugin group.
PLUGIN_CATEGORY_GROUPS = (
    ("system", {"system", "compositor", "layout", "windows", "files", "info", "time", "hardware", "desktop"}),
    ("network", {"network", "communication", "internet"}),
    ("media", {"media", "audio", "video", "music"}),
    ("home", {"home", "smart home"}),
    ("tools", {"tools", "utilities", "widgets", "accessibility", "ai", "productivity", "developer tools"}),
)

# First match wins: a text editor tagged Utility;Development is development.
CATEGORY_GROUPS = (
    ("games", {"Game"}),
    ("development", {"Development", "IDE"}),
    ("office", {"Office"}),
    ("media", {"AudioVideo", "Audio", "Video", "Player", "Music"}),
    ("graphics", {"Graphics", "Photography"}),
    ("internet", {"Network", "WebBrowser", "Email", "Chat", "InstantMessaging"}),
    ("system", {"System", "Settings", "Monitor", "PackageManager", "TerminalEmulator",
                "FileManager", "HardwareSettings"}),
    ("utilities", {"Utility", "Accessibility", "Education", "Science"}),
)

_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


def default_page(page):
    return {"groups": [{"id": gid, "name": ""} for gid in BUILTIN_ORDER[page]],
            "members": {}, "collapsed": [], "order": []}


def default_data():
    return {page: default_page(page) for page in PAGES}


def clean_name(text):
    """A group name as typed: no control or direction characters, single
    spaces, at most MAX_NAME characters; "" when nothing is left."""
    text = _CONTROL_RE.sub(" ", str(text or ""))
    return " ".join(text.split())[:MAX_NAME].strip()


def load(path=None):
    """{"apps": page, "plugins": page}: the saved groups, or the suggested
    ones. Anything malformed is dropped entry by entry."""
    path = Path(path or GROUPS_FILE)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return default_data()
        data = json.loads(raw)
    except (OSError, ValueError):
        return default_data()
    if not isinstance(data, dict):
        return default_data()
    return {page: _clean_page(data.get(page), page) for page in PAGES}


def _clean_page(data, page):
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return default_page(page)
    groups, seen = [], {OTHER, RUNNING}
    for group in data["groups"][:MAX_GROUPS]:
        if not isinstance(group, dict):
            continue
        gid, name = group.get("id"), group.get("name", "")
        if not isinstance(gid, str) or not _ID_RE.match(gid) or gid in seen:
            continue
        seen.add(gid)
        name = clean_name(name) if isinstance(name, str) else ""
        if not name and gid not in BUILTIN_NAMES:
            continue
        groups.append({"id": gid, "name": name})
    members = {}
    raw_members = data.get("members")
    if isinstance(raw_members, dict):
        for key, gid in list(raw_members.items())[:MAX_MEMBERS]:
            if isinstance(key, str) and 0 < len(key) <= 300 and isinstance(gid, str) and gid in seen:
                members[key] = gid
    raw_collapsed = data.get("collapsed")
    foldable = seen | set(ORIGINS)
    collapsed = [g for g in raw_collapsed if isinstance(g, str) and g in foldable][:MAX_GROUPS + 5] \
        if isinstance(raw_collapsed, list) else []
    # Entries placed by hand inside their group (drag and drop), in order.
    raw_order = data.get("order")
    order = list(dict.fromkeys(k for k in raw_order if isinstance(k, str) and 0 < len(k) <= 300))[:MAX_MEMBERS] \
        if isinstance(raw_order, list) else []
    return {"groups": groups, "members": members, "collapsed": collapsed, "order": order}


def save(data, path=None):
    """Write atomically and privately (0600, never through a symlink)."""
    path = Path(path or GROUPS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def display_name(group_or_id, data=None, page="apps"):
    if isinstance(group_or_id, str):
        if group_or_id == OTHER:
            return _("Other")
        if group_or_id == RUNNING:
            return _("Running in the bar") if page == "plugins" else _("Running")
        if group_or_id == "o-defaults":
            return _("Your default apps")
        if group_or_id == "o-omarchy":
            return _("Built into Omarchy") if page == "plugins" else _("Comes with Omarchy")
        if group_or_id == "o-other":
            return _("Other")
        group = next((g for g in (data or {}).get("groups", []) if g["id"] == group_or_id), None)
        if group is None:
            return _("Other")
    else:
        group = group_or_id
    return group["name"] or _(BUILTIN_NAMES.get(group["id"], group["id"]))


def suggested_group(app):
    """The built-in group an entry belongs in by itself."""
    if "plugin_id" in app:
        category = str(app.get("category") or "").strip().casefold()
        for gid, names in PLUGIN_CATEGORY_GROUPS:
            if category in names:
                return gid
        return OTHER
    categories = set(app.get("categories") or ())
    for gid, names in CATEGORY_GROUPS:
        if categories & names:
            return gid
    if app.get("pwa"):
        return "internet"
    return OTHER


def group_of(app, data):
    ids = {g["id"] for g in data["groups"]}
    chosen = data["members"].get(app["desktop_id"])
    if chosen in ids:
        return chosen
    suggested = suggested_group(app)
    return suggested if suggested in ids else OTHER


def assign(data, desktop_id, gid):
    if gid != OTHER and gid not in {g["id"] for g in data["groups"]}:
        raise ValueError(gid)
    if len(data["members"]) >= MAX_MEMBERS and desktop_id not in data["members"]:
        raise ValueError("too many members")
    data["members"][desktop_id] = gid


def name_taken(data, name, except_id=None):
    folded = name.casefold()
    return any(display_name(g).casefold() == folded for g in data["groups"] if g["id"] != except_id) \
        or folded in (_("Other").casefold(), _("Running").casefold(), _("Running in the bar").casefold())


def add_group(data, name):
    """A new group at the end (before Other); returns its id."""
    name = clean_name(name)
    if not name:
        raise ValueError(_("Enter a name."))
    if name_taken(data, name):
        raise ValueError(_("A group with this name exists already."))
    if len(data["groups"]) >= MAX_GROUPS:
        raise ValueError(_("That is the most groups there can be."))
    ids = {g["id"] for g in data["groups"]}
    number = 1
    while f"g{number}" in ids:
        number += 1
    gid = f"g{number}"
    data["groups"].append({"id": gid, "name": name})
    return gid


def rename_group(data, gid, name):
    name = clean_name(name)
    if not name:
        raise ValueError(_("Enter a name."))
    if name_taken(data, name, except_id=gid):
        raise ValueError(_("A group with this name exists already."))
    for group in data["groups"]:
        if group["id"] == gid:
            group["name"] = name
            return
    raise ValueError(gid)


def delete_group(data, gid):
    """Its entries go back to their suggested group, or Other."""
    data["groups"] = [g for g in data["groups"] if g["id"] != gid]
    data["members"] = {k: v for k, v in data["members"].items() if v != gid}
    data["collapsed"] = [g for g in data["collapsed"] if g != gid]


def move_group(data, gid, delta):
    groups = data["groups"]
    index = next((i for i, g in enumerate(groups) if g["id"] == gid), None)
    if index is None or not 0 <= index + delta < len(groups):
        return False
    groups[index], groups[index + delta] = groups[index + delta], groups[index]
    return True


def reorder_group(data, gid, target, after=False):
    """Move group gid right before (or after) group target: a group dropped
    on another. False when nothing changes."""
    groups = data["groups"]
    ids = [g["id"] for g in groups]
    if gid == target or gid not in ids or target not in ids:
        return False
    group = groups.pop(ids.index(gid))
    index = [g["id"] for g in groups].index(target) + (1 if after else 0)
    groups.insert(index, group)
    return [g["id"] for g in groups] != ids


def toggle_collapsed(data, gid):
    if gid in data["collapsed"]:
        data["collapsed"].remove(gid)
    else:
        data["collapsed"].append(gid)


def sections(entries, data, running=None):
    """[(group id, [entries])] in display order: Running (when given), the
    groups, then Other. A running entry shows in Running and in its own
    group; tray icons have no group and show in Running only. Empty groups
    are included; callers drop them while filtering."""
    order = [g["id"] for g in data["groups"]] + [OTHER]
    buckets = {gid: [] for gid in order}
    first = []
    for app in entries:
        if running is not None and running(app):
            first.append(app)
        if "tray_id" not in app:
            buckets[group_of(app, data)].append(app)
    result = [(RUNNING, first)] if running is not None else []
    return result + [(gid, in_hand_order(buckets[gid], data)) for gid in order]


def in_hand_order(members, data):
    """Entries placed by hand first, in their order; the others after them
    in the usual order."""
    position = {key: i for i, key in enumerate(data.get("order") or ())}
    last = len(position)
    return sorted(members, key=lambda app: position.get(app.get("desktop_id"), last))


def place(data, desktop_id, gid, target_id, member_ids, after=False):
    """Put an entry into group gid right before (or after) target_id; None:
    at the end. member_ids: the group's entries as shown, which then keep
    this order."""
    assign(data, desktop_id, gid)
    ids = [i for i in member_ids if i != desktop_id]
    index = ids.index(target_id) + (1 if after else 0) if target_id in ids else len(ids)
    ids.insert(index, desktop_id)
    moved = set(ids)
    data["order"] = ([k for k in data.get("order") or () if k not in moved] + ids)[-MAX_MEMBERS:]


def origin_sections(entries, classify, running=None):
    """Sections of the "by origin" view: Running (when given), your default
    apps, came with Omarchy, other. classify(app) -> "defaults" | "omarchy" | "other"."""
    buckets = {gid: [] for gid in ORIGINS}
    first = []
    for app in entries:
        if running is not None and running(app):
            first.append(app)
        if "tray_id" not in app:
            buckets["o-" + classify(app)].append(app)
    result = [(RUNNING, first)] if running is not None else []
    return result + [(gid, buckets[gid]) for gid in ORIGINS]
