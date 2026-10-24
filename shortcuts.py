"""GNOME Launcher - global app shortcuts as GNOME custom keybindings.

Shortcuts are stored in ~/.config/gnome-launcher/shortcuts.json and written
as custom keybindings of GNOME's media-keys daemon (the ones GNOME Settings
lists under Keyboard › Custom Shortcuts), at paths of their own
(…/custom-keybindings/gnome-launcher-N/), so the user's other custom
shortcuts are never touched.

Taking a key from another binding (a GNOME default such as Super + 1, or
one of the user's own custom shortcuts) removes that accelerator from it and
remembers where it came from ("applied" in the state file); removing the
launcher shortcut puts it back. Turning a binding off for an app works the
same way. Nothing else in the user's settings is changed.
"""

import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

STATE_FILE = Path.home() / ".config/gnome-launcher/shortcuts.json"
SETTINGS_FILE = Path.home() / ".config/gnome-launcher/settings.json"
LAUNCH_HELPER = Path(__file__).resolve().parent / "launch_entry.py"

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = MEDIA_KEYS + ".custom-keybinding"
CUSTOM_LIST_KEY = "custom-keybindings"
CUSTOM_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
OWN_PREFIX = "gnome-launcher-"
# Schemas whose string-list keys are GNOME's own key bindings.
BINDING_SCHEMAS = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
                   "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings", MEDIA_KEYS)
FAVORITE_KEY = re.compile(r"^(?:switch-to|open-new-window)-application-(\d)$")

# Modifier bits, as GDK has them.
MOD_BITS = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}
MOD_ORDER = ("SUPER", "SHIFT", "CTRL", "ALT")
MOD_LABELS = {"SUPER": "Super", "SHIFT": "Shift", "CTRL": "Ctrl", "ALT": "Alt"}
ACCEL_MODS = {"SUPER": "<Super>", "SHIFT": "<Shift>", "CTRL": "<Control>", "ALT": "<Alt>"}
ACCEL_ALIASES = {"super": "SUPER", "shift": "SHIFT", "control": "CTRL", "ctrl": "CTRL", "primary": "CTRL",
                 "alt": "ALT", "mod1": "ALT"}

# Conflict classes, from harmless to dangerous.
FREE, SAME, LAUNCHER, CUSTOM, DESKTOP = "free", "same", "launcher", "custom", "desktop"
# The key already opens this app, but a different action of it.
RETARGET = "retarget"

_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_ACCEL_RE = re.compile(r"^((?:<[A-Za-z0-9]{1,16}>){0,6})([A-Za-z0-9_]{1,32})$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_WHERE_RE = re.compile(r"^(?:org\.gnome\.[a-z.-]{1,80}:[a-z0-9-]{1,80}|custom:" + re.escape(CUSTOM_BASE)
                       + r"[A-Za-z0-9_-]{1,64}/)$")


# --- combos ------------------------------------------------------------------

def _clean_key(key):
    key = str(key or "").strip()
    if not _KEY_RE.match(key):
        return None
    return key.upper() if len(key) == 1 and key.isalpha() else key


def make_combo(mods, key):
    """A canonical combo dict, or None when the key name is unusable."""
    key = _clean_key(key)
    if key is None:
        return None
    mods = {m for m in mods if m in MOD_BITS}
    return {"mods": [m for m in MOD_ORDER if m in mods], "key": key}


def collides(a, b):
    """Whether two launcher shortcuts cannot both exist: the same keys."""
    return combo_id(a) == combo_id(b)


def owned_ids(state):
    """Every key the launcher binds."""
    return {combo_id(combo) for entry in state["apps"].values() for combo in entry["shortcuts"]}


def accelerator(combo):
    """GNOME's syntax: "<Super><Shift>b"."""
    key = combo["key"].lower() if len(combo["key"]) == 1 else combo["key"]
    return "".join(ACCEL_MODS[m] for m in combo["mods"]) + key


def parse_accelerator(text):
    """A combo from GNOME's "<Super><Shift>b", or None (empty, unknown
    modifiers such as <Hyper>, or not a key)."""
    match = _ACCEL_RE.match(str(text or "").strip())
    if not match:
        return None
    mods = set()
    for name in re.findall(r"<([^>]+)>", match.group(1)):
        mod = ACCEL_ALIASES.get(name.casefold())
        if mod is None:
            return None
        mods.add(mod)
    return make_combo(mods, match.group(2))


def key_label(key):
    pretty = {"RETURN": "Enter", "SPACE": "Space", "ESCAPE": "Esc", "BACKSPACE": "Backspace",
              "TAB": "Tab", "COMMA": ",", "PERIOD": ".", "SLASH": "/", "MINUS": "-",
              "EQUAL": "=", "SEMICOLON": ";", "APOSTROPHE": "'", "GRAVE": "`", "PAGE_UP": "Page Up",
              "PAGE_DOWN": "Page Down", "ABOVE_TAB": "Above Tab"}
    return pretty.get(key.upper(), key if len(key) > 1 else key.upper())


def combo_label(combo):
    """Human syntax: "Super + Alt + F"."""
    return " + ".join([*(MOD_LABELS[m] for m in combo["mods"]), key_label(combo["key"])])


def modmask(combo):
    return sum(MOD_BITS[m] for m in combo["mods"])


def combo_id(combo):
    return modmask(combo), combo["key"].casefold()


def private_action(app):
    """The app's private/incognito window action id, or None."""
    for action in app.get("actions", []):
        if action["id"] in ("new-private-window", "private-window") or \
                re.search(r"private|incognito", action.get("name", ""), re.I):
            return action["id"]
    return None


def action_name(app, action_id):
    for action in app.get("actions", []):
        if action["id"] == action_id:
            return action["name"]
    return action_id


# --- GNOME's settings ----------------------------------------------------------

class GSettings:
    """GNOME's key bindings through Gio.Settings. Tests pass a fake with the
    same methods instead."""

    def __init__(self):
        from gi.repository import Gio
        self.Gio = Gio
        self.source = Gio.SettingsSchemaSource.get_default()

    def _schema(self, schema_id):
        return self.source.lookup(schema_id, True) if self.source is not None else None

    def available(self):
        return self._schema(CUSTOM_SCHEMA) is not None

    def binding_keys(self):
        """[(schema id, key, summary, [accelerators])] for GNOME's own bindings."""
        found = []
        for schema_id in BINDING_SCHEMAS:
            schema = self._schema(schema_id)
            if schema is None:
                continue
            settings = self.Gio.Settings.new(schema_id)
            for key in schema.list_keys():
                info = schema.get_key(key)
                if info.get_value_type().dup_string() != "as":
                    continue
                accels = [a for a in settings.get_strv(key) if a]
                found.append((schema_id, key, info.get_summary() or key, accels))
        return found

    def get_strv(self, schema_id, key):
        return list(self.Gio.Settings.new(schema_id).get_strv(key))

    def set_strv(self, schema_id, key, values):
        self.Gio.Settings.new(schema_id).set_strv(key, list(values))

    def custom_paths(self):
        return list(self.Gio.Settings.new(MEDIA_KEYS).get_strv(CUSTOM_LIST_KEY))

    def set_custom_paths(self, paths):
        self.Gio.Settings.new(MEDIA_KEYS).set_strv(CUSTOM_LIST_KEY, list(paths))

    def read_custom(self, path):
        settings = self.Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)
        return {key: settings.get_string(key) for key in ("name", "command", "binding")}

    def write_custom(self, path, values):
        settings = self.Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)
        for key, value in values.items():
            settings.set_string(key, value)

    def reset_custom(self, path):
        settings = self.Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)
        for key in ("name", "command", "binding"):
            settings.reset(key)

    def sync(self):
        self.Gio.Settings.sync()


_backend = None


def backend():
    global _backend
    if _backend is None:
        _backend = GSettings()
    return _backend


def available():
    """Whether GNOME's custom keybindings are there to write to."""
    try:
        return backend().available()
    except Exception:
        return False


# --- state -------------------------------------------------------------------

DEFAULT_MODS = ["SUPER", "SHIFT"]


def load_default_mods():
    """Modifiers preselected for apps without a shortcut."""
    try:
        mods = json.loads(SETTINGS_FILE.read_text()).get("shortcut_default_mods")
    except (OSError, ValueError, AttributeError):
        return list(DEFAULT_MODS)
    if isinstance(mods, list):
        clean = [m for m in MOD_ORDER if m in mods]
        if clean:
            return clean
    return list(DEFAULT_MODS)


def save_default_mods(mods):
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        if not isinstance(settings, dict):
            settings = {}
    except (OSError, ValueError):
        settings = {}
    settings["shortcut_default_mods"] = [m for m in MOD_ORDER if m in mods]
    _atomic_write(SETTINGS_FILE, json.dumps(settings, indent=2) + "\n")


def _clean_where(value):
    value = str(value or "")
    return value if _WHERE_RE.match(value) else ""


def load_state(path=None):
    """{"apps": {key: entry}, "applied": [{"where", "accel"}]}: the
    shortcuts, and the accelerators taken from other bindings for them."""
    path = Path(path or STATE_FILE)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    apps = data.get("apps") if isinstance(data, dict) else None
    clean = {}
    for app_id, entry in (apps.items() if isinstance(apps, dict) else ()):
        if not isinstance(entry, dict):
            continue
        shortcuts = []
        for item in entry.get("shortcuts", []):
            combo = make_combo(item.get("mods", []), item.get("key", "")) if isinstance(item, dict) else None
            if combo:
                combo["replaces"] = str(item.get("replaces", ""))[:120]
                combo["taken_from"] = _clean_where(item.get("taken_from"))
                action = str(item.get("action", ""))
                combo["action"] = action if _ACTION_RE.match(action) else ""
                if combo["action"] and item.get("label"):
                    combo["label"] = str(item["label"])[:80]
                shortcuts.append(combo)
        disabled = []
        for item in entry.get("disabled", []):
            combo = make_combo(item.get("mods", []), item.get("key", "")) if isinstance(item, dict) else None
            where = _clean_where(item.get("where")) if isinstance(item, dict) else ""
            if combo and where:
                combo["description"] = str(item.get("description", ""))[:120]
                combo["source"] = str(item.get("source", ""))[:60]
                combo["where"] = where
                disabled.append(combo)
        launch = entry.get("launch")
        if (shortcuts or disabled) and isinstance(launch, list) and launch and all(isinstance(a, str) for a in launch):
            clean[str(app_id)] = {"name": str(entry.get("name", app_id))[:120], "launch": launch,
                                  "shortcuts": shortcuts, "disabled": disabled}
    applied = []
    raw = data.get("applied") if isinstance(data, dict) else None
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, dict) and _clean_where(item.get("where")) and parse_accelerator(item.get("accel")):
            applied.append({"where": item["where"], "accel": item["accel"]})
    return {"apps": clean, "applied": applied}


def launch_target_missing(launch):
    """True when a shortcut's app is gone: its .desktop file, or an AppImage
    deleted from a folder that is still there (not an unmounted drive)."""
    if len(launch) != 1:
        return False
    path = Path(launch[0])
    if not path.is_absolute() or path.exists():
        return False
    return path.suffix == ".desktop" or path.parent.is_dir()


def prune_missing(state):
    """Drop entries whose app was uninstalled. Returns the removed entries."""
    removed = {k: v for k, v in state["apps"].items() if launch_target_missing(v["launch"])}
    for key in removed:
        del state["apps"][key]
    return list(removed.values())


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def app_launch_argv(app):
    """What a shortcut opens: [desktop file] or the entry's own argv."""
    if "argv" in app:
        return list(app["argv"])
    if app.get("desktop_path"):
        return [app["desktop_path"]]
    return None


def shortcut_command(launch, action=""):
    """The command line GNOME runs for a shortcut (split by GLib, no shell):
    `gio launch` for a desktop file, the launch helper for one of its
    actions, the argv itself otherwise."""
    if len(launch) == 1 and launch[0].endswith(".desktop"):
        if action:
            return shlex.join([sys.executable, str(LAUNCH_HELPER), launch[0], action])
        return shlex.join(["gio", "launch", launch[0]])
    return shlex.join(launch)


def app_key(app):
    return app.get("desktop_path") or app.get("desktop_id", "")


# --- conflicts and suggestions ----------------------------------------------

def read_desktop_binds(settings=None):
    """GNOME's own bindings: [{combo, description, where, accel}]."""
    binds = []
    for schema_id, key, summary, accels in (settings or backend()).binding_keys():
        for accel in accels:
            combo = parse_accelerator(accel)
            if combo:
                binds.append({"combo": combo, "description": summary, "where": f"{schema_id}:{key}",
                              "accel": accel, "key_name": key})
    return binds


def read_custom_binds(settings=None):
    """The user's own custom shortcuts (not the launcher's):
    [{combo, description, command, where, accel}]."""
    settings = settings or backend()
    binds = []
    for path in settings.custom_paths():
        if not path.startswith(CUSTOM_BASE) or path[len(CUSTOM_BASE):].startswith(OWN_PREFIX):
            continue
        values = settings.read_custom(path)
        combo = parse_accelerator(values.get("binding"))
        if combo:
            binds.append({"combo": combo, "description": values.get("name") or values.get("command", ""),
                          "command": values.get("command", ""), "where": "custom:" + path,
                          "accel": values["binding"]})
    return binds


class Bindings:
    """Snapshot of who owns which key combination right now."""

    def __init__(self, state, desktop=None, custom=None, favorites=None, main_mods=None):
        self.state = state
        self.desktop = read_desktop_binds() if desktop is None else desktop
        self.custom = read_custom_binds() if custom is None else custom
        if favorites is None:
            import desktop as gnome
            favorites = gnome.favorites()
        self.favorites = list(favorites)
        self.main_mods = main_mods
        # Accelerators the launcher took away still belong to their owner
        # for the user: shown as taken by the launcher shortcut that has them.
        taken = {(a["where"], a["accel"]) for a in state.get("applied", [])}
        self.desktop = [b for b in self.desktop if (b["where"], b["accel"]) not in taken]
        self.custom = [b for b in self.custom if (b["where"], b["accel"]) not in taken]

    def _launcher_owner(self, combo):
        cid = combo_id(combo)
        for app_id, entry in self.state["apps"].items():
            for sc in entry["shortcuts"]:
                if combo_id(sc) == cid:
                    return app_id, entry, sc
        return None, None, None

    def _disabled_by_launcher(self, combo):
        cid = combo_id(combo)
        return any(combo_id(d) == cid for entry in self.state["apps"].values() for d in entry["disabled"])

    def owner(self, combo):
        """(level, the other binding) for a key not bound by the launcher."""
        cid = combo_id(combo)
        if self._disabled_by_launcher(combo):
            return FREE, None
        for bind in self.custom:
            if combo_id(bind["combo"]) == cid:
                return CUSTOM, bind
        for bind in self.desktop:
            if combo_id(bind["combo"]) == cid:
                return DESKTOP, bind
        return FREE, None

    def classify(self, combo, app_id, keycode=None, app=None, action=""):
        """(level, description) for assigning combo to app_id, opening the
        app itself or, with `action`, one of its desktop actions."""
        owner_id, owner, shortcut = self._launcher_owner(combo)
        if owner_id == app_id:
            current = shortcut.get("action", "")
            return (SAME, owner["name"]) if current == action else (RETARGET, current)
        if owner_id:
            return LAUNCHER, owner["name"]
        if app is not None:
            # Another binding already does exactly this: assigning would only duplicate it.
            for bind in self.external_shortcuts(app):
                if combo_id(bind["combo"]) == combo_id(combo):
                    if bind["action"] == action:
                        return SAME, bind["description"]
                    break
        level, bind = self.owner(combo)
        return level, (bind["description"] if bind else "")

    def taken_from(self, combo):
        """Where an assignment takes the key from (its owner), or ""."""
        level, bind = self.owner(combo)
        return bind["where"] if bind and level in (CUSTOM, DESKTOP) else ""

    def used_letters(self):
        """Keys taken with the main modifiers (the default set, e.g. Super + Shift)."""
        main = make_combo(load_default_mods() if self.main_mods is None else self.main_mods, "A")
        mask = modmask(main)
        used = {b["combo"]["key"].casefold() for b in self.desktop + self.custom if modmask(b["combo"]) == mask}
        used |= {sc["key"].casefold() for entry in self.state["apps"].values()
                 for sc in entry["shortcuts"] if modmask(sc) == mask}
        return used

    def suggestions(self, app, app_id, mods=None, limit=8):
        """Free combos for this app, letters of its name first. With `mods`,
        only that exact modifier set; without, the first free letter of each
        common Super-based set."""
        name = re.sub(r"[^A-Za-z0-9]", "", app.get("name", "")).upper()
        name_letters = [c for c in dict.fromkeys(name) if c.isalpha()]
        keys = name_letters + [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in name_letters]
        used = self.used_letters()
        keys = [k for k in keys if k.casefold() not in used]
        if mods:
            found = []
            for key in keys:
                combo = make_combo(mods, key)
                if combo and self.classify(combo, app_id)[0] == FREE:
                    found.append(combo)
                    if len(found) >= limit:
                        break
            return sorted(found, key=lambda c: c["key"])
        found = []
        for mod_set in (("SUPER", "ALT"), ("SUPER", "CTRL"), ("SUPER", "CTRL", "ALT"),
                        ("SUPER", "SHIFT", "ALT"), ("SUPER", "SHIFT", "CTRL")):
            found.extend(self.suggestions(app, app_id, mod_set, 1))
            if len(found) >= limit:
                break
        return found

    def external_shortcuts(self, app):
        """Other bindings that open this app: the user's own custom
        shortcuts whose command launches it, and GNOME's Super + 1…9 (and
        Super + Ctrl + 1…9 for a new window) for an app pinned to the Dash."""
        desktop_path = app.get("desktop_path", "")
        desktop_file = Path(desktop_path).name
        desktop_id = desktop_file.removesuffix(".desktop").casefold()
        try:
            program = Path(shlex.split(app.get("exec", ""))[0]).name.casefold()
        except (ValueError, IndexError):
            program = ""
        result = []
        for bind in self.custom:
            try:
                argv = shlex.split(bind["command"])
            except ValueError:
                continue
            if not argv:
                continue
            words = {Path(a).name.casefold().removesuffix(".desktop") for a in argv}
            first = Path(argv[0]).name.casefold()
            if desktop_path and desktop_path in argv or (desktop_id and desktop_id in words) or (
                    program and first == program and program not in {"env", "sh", "bash", "flatpak", "python3", "gio"}):
                result.append({**bind, "file": "Custom", "action": ""})
        if desktop_file and desktop_file in self.favorites:
            position = self.favorites.index(desktop_file) + 1
            dash = [bind for bind in self.desktop
                    if (match := FAVORITE_KEY.match(bind.get("key_name", ""))) and int(match.group(1)) == position]
            # Switching to the app first (Super + 1), then its new window (Super + Ctrl + 1).
            dash.sort(key=lambda bind: not bind["key_name"].startswith("switch-to"))
            result += [{**bind, "file": "GNOME", "action": ""} for bind in dash]
        return result


# --- writing -----------------------------------------------------------------

def _desired_removals(state):
    """{(where, accel)} the launcher keeps taken from other bindings."""
    wanted = set()
    for entry in state["apps"].values():
        for combo in entry["shortcuts"]:
            if combo.get("taken_from"):
                wanted.add((combo["taken_from"], accelerator(combo)))
        for combo in entry["disabled"]:
            wanted.add((combo["where"], accelerator(combo)))
    return wanted


def _same_accel(a, b):
    first, second = parse_accelerator(a), parse_accelerator(b)
    return first is not None and second is not None and combo_id(first) == combo_id(second)


def _take(settings, where, accel):
    """Remove accel from the binding at where. True when it was there."""
    if where.startswith("custom:"):
        path = where[len("custom:"):]
        if path not in settings.custom_paths():
            return False
        values = settings.read_custom(path)
        if not _same_accel(values.get("binding"), accel):
            return False
        settings.write_custom(path, {"binding": ""})
        return True
    schema_id, key = where.split(":", 1)
    current = settings.get_strv(schema_id, key)
    kept = [a for a in current if not _same_accel(a, accel)]
    if kept == current:
        return False
    settings.set_strv(schema_id, key, kept)
    return True


def _give_back(settings, where, accel):
    """Put accel back on the binding at where (unless it got another key since)."""
    if where.startswith("custom:"):
        path = where[len("custom:"):]
        if path in settings.custom_paths() and not settings.read_custom(path).get("binding"):
            settings.write_custom(path, {"binding": accel})
        return
    schema_id, key = where.split(":", 1)
    current = [a for a in settings.get_strv(schema_id, key) if a]
    if not any(_same_accel(a, accel) for a in current):
        settings.set_strv(schema_id, key, current + [accel])


def save_and_apply(state, settings=None, path=None):
    """Write the launcher's custom keybindings, take or give back the keys
    of other bindings, and save state. Raises RuntimeError when GNOME's
    keybinding settings are not there."""
    settings = settings or backend()
    if not settings.available():
        raise RuntimeError("GNOME's custom keybinding settings are not installed")
    path = Path(path or STATE_FILE)
    previous = {(a["where"], a["accel"]) for a in load_state(path).get("applied", [])}
    wanted = _desired_removals(state)
    applied = []
    for where, accel in sorted(previous - {(w, a) for w, a in wanted}):
        _give_back(settings, where, accel)
    for where, accel in sorted(wanted):
        if (where, accel) in previous or _take(settings, where, accel):
            applied.append({"where": where, "accel": accel})

    paths = [p for p in settings.custom_paths()
             if not (p.startswith(CUSTOM_BASE) and p[len(CUSTOM_BASE):].startswith(OWN_PREFIX))]
    old_own = [p for p in settings.custom_paths() if p not in paths]
    own = []
    apps = [state["apps"][k] for k in sorted(state["apps"], key=lambda k: state["apps"][k]["name"].casefold())]
    for entry in apps:
        for combo in entry["shortcuts"]:
            custom_path = f"{CUSTOM_BASE}{OWN_PREFIX}{len(own)}/"
            what = f"{entry['name']}: {combo.get('label') or combo['action']}" if combo.get("action") \
                else entry["name"]
            settings.write_custom(custom_path, {"name": what[:120],
                                                "command": shortcut_command(entry["launch"], combo.get("action", "")),
                                                "binding": accelerator(combo)})
            own.append(custom_path)
    for stale in old_own:
        if stale not in own:
            settings.reset_custom(stale)
    settings.set_custom_paths(paths + own)
    settings.sync()
    _atomic_write(path, json.dumps({**state, "applied": applied}, indent=2) + "\n")
