"""Launch & Bind - global app shortcuts managed through Hyprland.

Shortcuts are stored in ~/.config/applauncher/shortcuts.json and rendered to
~/.config/hypr/simple-launcher-shortcuts.lua, which hyprland.lua requires last
so launcher shortcuts win over Omarchy defaults and hand-written bindings.
Replacing or disabling another binding is an `hl.unbind` in the generated
file, so removing the launcher shortcut restores the original binding on the
next reload. The only edit to hand-written files is deleting a single-line
`launch = "..."` bind whose app was uninstalled, and only after the user
said yes to exactly that line (with a backup next to it).
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

STATE_FILE = Path.home() / ".config/applauncher/shortcuts.json"
# Hand-written binds of removed apps the user chose to keep: not asked again.
KEPT_FILE = Path.home() / ".config/applauncher/kept-binds.json"
HYPR_DIR = Path.home() / ".config/hypr"
LUA_MODULE = "simple-launcher-shortcuts"
LUA_FILE = HYPR_DIR / f"{LUA_MODULE}.lua"
HYPR_MAIN = HYPR_DIR / "hyprland.lua"
REQUIRE_LINE = f'require("hypr.{LUA_MODULE}")'

# Hyprland modmask bits.
MOD_BITS = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}
MOD_ORDER = ("SUPER", "SHIFT", "CTRL", "ALT")
MOD_ALIASES = {
    "SUPER": "SUPER", "WIN": "SUPER", "LOGO": "SUPER", "MOD4": "SUPER", "META": "SUPER",
    "SHIFT": "SHIFT",
    "CTRL": "CTRL", "CONTROL": "CTRL",
    "ALT": "ALT", "MOD1": "ALT",
}
MOD_LABELS = {"SUPER": "Super", "SHIFT": "Shift", "CTRL": "Ctrl", "ALT": "Alt"}

# Conflict classes, from harmless to dangerous.
FREE, SAME, LAUNCHER, CUSTOM, OMARCHY = "free", "same", "launcher", "custom", "omarchy"
# The key already opens this app, but a different action of it.
RETARGET = "retarget"

_KEY_RE = re.compile(r"^(code:\d{1,3}|[A-Za-z0-9_]{1,32})$")
_BIND_RE = re.compile(r'\b(?:o\.bind(?:_toggle)?|hl\.bind)\(\s*"([^"]+)"\s*(?:,\s*"([^"]*)")?(.*)')
_UNBIND_RE = re.compile(r'\bhl\.unbind\(\s*"([^"]+)"')
_WEBAPP_RE = re.compile(r'\bwebapp\s*=\s*"([^"]+)"')
_OMARCHY_RE = re.compile(r'\bomarchy\s*=\s*"([^"]+)"')
_TUI_RE = re.compile(r'\btui\s*=\s*"([^"]+)"')
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_URL_RE = re.compile(r'https?://[^\s"\']+')
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


# --- combos ------------------------------------------------------------------

def _clean_key(key):
    key = str(key or "").strip()
    if not _KEY_RE.match(key):
        return None
    return key.upper() if len(key) == 1 and key.isalpha() else key


def make_combo(mods, key, then=None):
    """Return a canonical combo dict, or None when a key name is unusable.

    With `then`, it is a two-step shortcut: press mods + key, release, then
    press `then` on its own (e.g. Super + Alt + Space, N)."""
    key = _clean_key(key)
    if key is None:
        return None
    mods = {m for m in mods if m in MOD_BITS}
    combo = {"mods": [m for m in MOD_ORDER if m in mods], "key": key}
    if then:
        then = _clean_key(then)
        if then is None:
            return None
        combo["then"] = then
    return combo


def leader(combo):
    """The first step of a combo (the combo itself when it has one step)."""
    return {"mods": list(combo["mods"]), "key": combo["key"]}


def collides(a, b):
    """Whether two launcher shortcuts cannot both exist: the same keys, or one
    step that is the first step of a two-step shortcut."""
    if combo_id(a) == combo_id(b):
        return True
    return bool(a.get("then")) != bool(b.get("then")) and combo_id(leader(a)) == combo_id(leader(b))


def owned_ids(state):
    """Every key the launcher binds: its shortcuts and two-step first steps."""
    ids = set()
    for entry in state["apps"].values():
        for combo in entry["shortcuts"]:
            ids.add(combo_id(combo))
            ids.add(combo_id(leader(combo)))
    return ids


def parse_combo(text):
    """Parse a Hyprland combo like "SUPER + SHIFT + B" into a combo dict."""
    parts = [p.strip() for p in re.split(r"[+,]", text or "") if p.strip()]
    if not parts:
        return None
    mods = set()
    for part in parts[:-1]:
        mod = MOD_ALIASES.get(part.upper())
        if mod is None:
            return None
        mods.add(mod)
    return make_combo(mods, parts[-1])


def combo_string(combo):
    """Hyprland syntax: "SUPER + ALT + F"."""
    return " + ".join([*combo["mods"], combo["key"]])


def key_label(key):
    pretty = {"RETURN": "Enter", "SPACE": "Space", "ESCAPE": "Esc", "BACKSPACE": "Backspace",
              "TAB": "Tab", "COMMA": ",", "PERIOD": ".", "SLASH": "/", "MINUS": "-",
              "EQUAL": "=", "SEMICOLON": ";", "APOSTROPHE": "'", "GRAVE": "`"}
    return pretty.get(key.upper(), key if len(key) > 1 else key.upper())


def combo_label(combo):
    """Human syntax: "Super + Alt + F", two-step "Super + Alt + Space, N"."""
    label = " + ".join([*(MOD_LABELS[m] for m in combo["mods"]), key_label(combo["key"])])
    return f"{label}, {key_label(combo['then'])}" if combo.get("then") else label


def modmask(combo):
    return sum(MOD_BITS[m] for m in combo["mods"])


def key_ids(combo, keycode=None):
    """Every identifier Hyprland could match this key by."""
    key = combo["key"]
    ids = {key.casefold()}
    if keycode:
        ids.add(f"code:{int(keycode)}")
    return ids


def combo_id(combo):
    return (modmask(combo), combo["key"].casefold(), (combo.get("then") or "").casefold())


# --- reading existing bindings ---------------------------------------------

def read_live_binds():
    """Global (non-submap) keyboard binds currently active in Hyprland."""
    try:
        result = subprocess.run(["hyprctl", "binds"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return parse_live_binds(result.stdout)


def parse_live_binds(text):
    binds = []
    for block in re.split(r"\n\s*\n", text):
        fields = {}
        for line in block.splitlines():
            name, sep, value = line.strip().partition(":")
            if sep and name and name not in fields:
                fields[name] = value.strip()
        if "modmask" not in fields or fields.get("submap", ""):
            continue
        key = fields.get("key", "")
        # Keycode binds report the whole combo string as their key.
        if "code:" in key:
            key = key.rsplit("+", 1)[-1].strip()
        if not key or key.startswith("mouse"):
            continue
        try:
            mask = int(fields["modmask"])
        except ValueError:
            continue
        binds.append({"mask": mask, "key": key.casefold(),
                      "description": fields.get("description", "")})
    return binds


def user_config_files():
    """User Lua files that may bind keys, excluding the generated one."""
    try:
        return sorted(p for p in HYPR_DIR.glob("*.lua") if p.name != LUA_FILE.name)
    except OSError:
        return []


def omarchy_binding_files():
    """Omarchy's stock binding files, read-only."""
    root = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
    try:
        return sorted((root / "default/hypr/bindings").glob("*.lua"))
    except OSError:
        return []


def _strip_lua_comment(line):
    """The line up to a `--` comment, keeping `--` inside quoted strings."""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif line.startswith("--", i):
            return line[:i]
        i += 1
    return line


def parse_user_binds(files=None):
    """Literal binds still in effect from the user's files, plus combos the
    user unbound without binding again. Later lines win, like Hyprland."""
    binds, unbinds = [], set()
    for path in user_config_files() if files is None else files:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            code = _strip_lua_comment(line)
            match = _BIND_RE.search(code)
            if match:
                combo = parse_combo(match.group(1))
                if combo:
                    unbinds.discard(combo_id(combo))
                    desc = match.group(2) or ""
                    if not desc:
                        found = re.search(r'description\s*=\s*"([^"]*)"', code)
                        desc = found.group(1) if found else ""
                    binds.append({"combo": combo, "description": desc,
                                  "command": match.group(3), "file": path.name})
                continue
            match = _UNBIND_RE.search(code)
            if match and (combo := parse_combo(match.group(1))):
                cid = combo_id(combo)
                binds = [b for b in binds if combo_id(b["combo"]) != cid]
                unbinds.add(cid)
    return binds, unbinds


# Omarchy launch targets that open whichever app the user picked as default,
# with the command Omarchy asks to find it.
DEFAULT_APP_COMMANDS = {
    "terminal": ["xdg-terminal-exec", "--print-id"],
    "browser": ["xdg-settings", "get", "default-web-browser"],
}
EDITOR_STATE = Path.home() / ".local/state/omarchy/defaults/editor"
DEFAULT_APP_ROLES = (*DEFAULT_APP_COMMANDS, "editor")


def default_app_id(role):
    """Desktop id (without .desktop) or program name of the app
    `omarchy = "<role>"` opens; "" when unknown."""
    if role == "editor":
        # omarchy-launch-editor: the saved choice, else nvim.
        try:
            words = EDITOR_STATE.read_text().split()
        except OSError:
            words = []
        return Path(words[0]).name if words else "nvim"
    env = {k: v for k, v in os.environ.items() if k != "BROWSER"}
    try:
        result = subprocess.run(DEFAULT_APP_COMMANDS[role], capture_output=True,
                                text=True, timeout=2, env=env)
    except (KeyError, OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    lines = result.stdout.strip().splitlines()
    # An entry may carry an action suffix: "foot.desktop:server".
    return lines[0].split(":", 1)[0].removesuffix(".desktop") if lines else ""


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


# --- state -------------------------------------------------------------------

SETTINGS_FILE = Path.home() / ".config/applauncher/settings.json"
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


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"apps": {}}
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, dict):
        return {"apps": {}}
    clean = {}
    for app_id, entry in apps.items():
        if not isinstance(entry, dict):
            continue
        shortcuts = []
        for item in entry.get("shortcuts", []):
            combo = make_combo(item.get("mods", []), item.get("key", ""),
                               item.get("then")) if isinstance(item, dict) else None
            if combo:
                combo["replaces"] = str(item.get("replaces", ""))[:120]
                action = str(item.get("action", ""))
                combo["action"] = action if _ACTION_RE.match(action) else ""
                shortcuts.append(combo)
        disabled = []
        for item in entry.get("disabled", []):
            combo = make_combo(item.get("mods", []), item.get("key", "")) if isinstance(item, dict) else None
            if combo:
                combo["description"] = str(item.get("description", ""))[:120]
                combo["source"] = str(item.get("source", ""))[:60]
                disabled.append(combo)
        launch = entry.get("launch")
        if (shortcuts or disabled) and isinstance(launch, list) and launch and all(isinstance(a, str) for a in launch):
            clean[str(app_id)] = {"name": str(entry.get("name", app_id))[:120], "launch": launch,
                                  "shortcuts": shortcuts, "disabled": disabled}
    return {"apps": clean}


def plugin_installed(plugin_id):
    """Whether a shell plugin with this id is installed (yours or built in)."""
    omarchy = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
    for root in (Path.home() / ".config/omarchy/plugins", omarchy / "shell/plugins"):
        if (root / plugin_id).exists():
            return True
        for manifest in [*root.glob("*/manifest.json"), *root.glob("*/*/manifest.json")]:
            try:
                if json.loads(manifest.read_text()).get("id") == plugin_id:
                    return True
            except (OSError, ValueError, AttributeError):
                continue
    return False


def launch_target_missing(launch):
    """True when a shortcut's app is gone: its .desktop file, an AppImage
    deleted from a folder that is still there (not an unmounted drive), or a
    removed shell plugin."""
    plugin_id = plugin_toggle_id(launch)
    if plugin_id:
        return not plugin_installed(plugin_id)
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


_LAUNCH_RE = re.compile(r'\blaunch\s*=\s*"([^"]+)"')


def _desktop_entry_exists(desktop_id):
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    name = desktop_id if desktop_id.endswith(".desktop") else desktop_id + ".desktop"
    return any((Path(d) / "applications" / name).exists() for d in (data_home, *data_dirs) if d.strip())


def launch_command_missing(command):
    """True when a hand-written `launch = "..."` opens a program or desktop
    entry that is no longer installed. Unclear commands count as present."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if argv[:1] in (["uwsm-app"], ["uwsm"]):
        argv = argv[2:] if argv[1:2] == ["app"] else argv[1:]
        if argv[:1] == ["--"]:
            argv = argv[1:]
    if argv[:1] == ["env"]:
        argv = argv[1:]
        while argv and "=" in argv[0]:
            argv = argv[1:]
    if not argv:
        return False
    program = argv[0]
    if program == "gtk-launch":
        return len(argv) > 1 and not _desktop_entry_exists(argv[1])
    if "/" in program:
        path = Path(program)
        if path.suffix == ".desktop":
            return not path.exists()
        return path.parent.is_dir() and not path.exists()
    return shutil.which(program) is None


def bind_key(path, line):
    """Identifies one hand-written line: its file and its exact text."""
    return f"{path}\0{line}"


def load_kept():
    try:
        data = json.loads(KEPT_FILE.read_text())
    except (OSError, ValueError):
        return set()
    return {k for k in data if isinstance(k, str)} if isinstance(data, list) else set()


def keep_binds(keys):
    """Remember that the user keeps these lines; they are not offered again."""
    _atomic_write(KEPT_FILE, json.dumps(sorted(load_kept() | set(keys)), indent=2) + "\n")


def pending_dead_binds(files=None):
    """Dead hand-written binds the user has not chosen to keep yet."""
    kept = load_kept()
    return [d for d in dead_user_binds(files) if d[2]["key"] not in kept]


def dead_user_binds(files=None):
    """Hand-written single-line binds whose app was uninstalled, as
    (path, line number, bind) tuples."""
    dead = []
    for path in user_config_files() if files is None else files:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines):
            code = _strip_lua_comment(line)
            match = _BIND_RE.search(code)
            launch = _LAUNCH_RE.search(code) if match else None
            combo = parse_combo(match.group(1)) if match else None
            if launch and combo and launch_command_missing(launch.group(1)):
                dead.append((path, number, {"combo": combo, "description": match.group(2) or launch.group(1),
                                            "file": path.name, "key": bind_key(path, line)}))
    return dead


def remove_dead_user_binds(keys, files=None):
    """Delete the hand-written shortcuts of uninstalled apps the user agreed
    to (by bind_key) from their files, keeping a backup of each changed file,
    and reload Hyprland. Lines that changed since, or whose app is back, stay.
    Returns the removed binds; restores the files on a new config error."""
    keys = set(keys)
    dead = [d for d in dead_user_binds(files) if d[2]["key"] in keys]
    if not dead:
        return []
    errors_before = _config_errors()
    originals = {}
    for path in {p for p, _n, _b in dead}:
        text = path.read_text()
        originals[path] = text
        drop = {n for p, n, _b in dead if p == path}
        kept = [line for n, line in enumerate(text.splitlines(keepends=True)) if n not in drop]
        shutil.copy2(path, path.with_name(path.name + ".before-simple-launcher-cleanup"))
        _atomic_write(path, "".join(kept))
    errors = _config_errors()
    if errors and errors != errors_before:
        for path, text in originals.items():
            _atomic_write(path, text)
        _config_errors()
        raise RuntimeError(errors)
    return [b for _p, _n, b in dead]


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


# A shell plugin's shortcut toggles it, like Omarchy's own Clipboard key.
PLUGIN_TOGGLE = ["omarchy-shell", "shell", "toggle"]


def plugin_toggle_id(launch):
    """The plugin id when launch toggles a shell plugin, else None."""
    if len(launch) == 4 and launch[:3] == PLUGIN_TOGGLE and _PLUGIN_ID_RE.match(launch[3]):
        return launch[3]
    return None


# A tray icon's shortcut runs tray.py with the item's Id (and a menu entry).
TRAY_SCRIPT = str(Path(__file__).resolve().parent / "tray.py")


def tray_launch_id(launch):
    """The tray item Id when launch runs tray.py, else None."""
    if len(launch) == 2 and Path(launch[0]).name == "tray.py" and _PLUGIN_ID_RE.match(launch[1]):
        return launch[1]
    return None


# The launcher's own shortcut runs its toggle script.
SELF_TOGGLE = str(Path(__file__).resolve().parent / "launcher-toggle")


def is_self_toggle(launch):
    return len(launch) == 1 and Path(launch[0]).name == "launcher-toggle"


def runs_directly(launch):
    """Shell plugins, tray icons and the launcher itself are commands, not
    apps for uwsm-app."""
    return bool(plugin_toggle_id(launch) or tray_launch_id(launch) or is_self_toggle(launch))


def app_launch_argv(app):
    """The argv a shortcut runs (through uwsm-app), matching a list click."""
    if app.get("self_launcher"):
        return [SELF_TOGGLE]
    if app.get("plugin_id"):
        return [*PLUGIN_TOGGLE, app["plugin_id"]]
    if app.get("tray_id"):
        return [TRAY_SCRIPT, app["tray_id"]]
    if "argv" in app:
        return list(app["argv"])
    if app.get("desktop_path"):
        return [app["desktop_path"]]
    return None


def shortcut_launch(launch, action=""):
    """argv for a shortcut: the app, or one of its desktop actions."""
    if action and len(launch) == 1 and launch[0].endswith(".desktop"):
        return [f"{launch[0]}:{action}"]
    if action and (tray_launch_id(launch) or is_self_toggle(launch)):
        return [*launch, action]
    return list(launch)


def app_key(app):
    return app.get("desktop_path") or app.get("desktop_id", "")


# --- conflicts and suggestions ----------------------------------------------

class Bindings:
    """Snapshot of who owns which key combination right now."""

    def __init__(self, state, live=None, user=None, defaults=None, main_mods=None, default_apps=None):
        self.state = state
        self._default_apps = dict(default_apps or {})
        self.live = read_live_binds() if live is None else live
        self.user_binds, self.user_unbinds = parse_user_binds() if user is None else user
        self.main_mods = main_mods
        self.default_binds = (parse_user_binds(omarchy_binding_files())[0]
                              if defaults is None else defaults)

    def default_app(self, role):
        """The user's default app for an Omarchy role, cached."""
        if role not in self._default_apps:
            self._default_apps[role] = default_app_id(role)
        return self._default_apps[role]

    @staticmethod
    def _matches(sc, combo, ids):
        return (modmask(sc) == modmask(combo) and sc["key"].casefold() in ids
                and (sc.get("then") or "").casefold() == (combo.get("then") or "").casefold())

    def _launcher_owner(self, combo, ids):
        for app_id, entry in self.state["apps"].items():
            for sc in entry["shortcuts"]:
                if self._matches(sc, combo, ids):
                    return app_id, entry
        return None, None

    def _sequence_after(self, combo, ids):
        """A launcher two-step shortcut whose first step is combo."""
        for entry in self.state["apps"].values():
            for sc in entry["shortcuts"]:
                if sc.get("then") and self._matches(leader(sc), combo, ids):
                    return entry, sc
        return None, None

    @classmethod
    def _launcher_action(cls, entry, combo, ids):
        for sc in entry["shortcuts"]:
            if cls._matches(sc, combo, ids):
                return sc.get("action", "")
        return ""

    def _user_bind(self, combo, ids):
        mask = modmask(combo)
        found = None
        for bind in self.user_binds:
            if modmask(bind["combo"]) == mask and bind["combo"]["key"].casefold() in ids:
                found = bind  # later definitions win, like Hyprland.
        return found

    def _disabled_by_launcher(self, combo, ids):
        mask = modmask(combo)
        return any(modmask(d) == mask and d["key"].casefold() in ids
                   for entry in self.state["apps"].values() for d in entry["disabled"])

    def classify(self, combo, app_id, keycode=None, app=None, action=""):
        """Return (level, description) for assigning combo to app_id, opening
        the app itself or, with `action`, one of its desktop actions."""
        ids = key_ids(combo, keycode)
        owner_id, owner = self._launcher_owner(combo, ids)
        if owner_id == app_id:
            current = self._launcher_action(owner, combo, ids)
            return (SAME, owner["name"]) if current == action else (RETARGET, current)
        if owner_id:
            return LAUNCHER, owner["name"]
        if combo.get("then"):
            return self.classify_first_step(leader(combo), keycode)
        entry, sequence = self._sequence_after(combo, ids)
        if sequence:
            # The first step of a two-step shortcut cannot open anything itself.
            return LAUNCHER, f"{entry['name']} ({combo_label(sequence)})"
        if app is not None:
            # Another binding already does exactly this: assigning would only duplicate it.
            for bind in self.external_shortcuts(app):
                if modmask(bind["combo"]) == modmask(combo) and bind["combo"]["key"].casefold() in ids:
                    if bind["action"] == action:
                        return SAME, bind["description"]
                    break
        return self._external_level(combo, ids)

    def classify_first_step(self, first, keycode=None):
        """The first step of a two-step shortcut: free when other two-step
        shortcuts already start with it (they share it), otherwise taken by
        whatever has this combo."""
        ids = key_ids(first, keycode)
        owner_id, owner = self._launcher_owner(first, ids)
        if owner_id:
            return LAUNCHER, owner["name"]
        if self._sequence_after(first, ids)[1]:
            return FREE, ""
        return self._external_level(first, ids)

    def _external_level(self, combo, ids):
        mask = modmask(combo)
        live = [b for b in self.live if b["mask"] == mask and b["key"] in ids]
        user = self._user_bind(combo, ids)
        if user and (live or not self.live):
            return CUSTOM, user["description"] or (live[-1]["description"] if live else "") or "custom binding"
        if live and not self._disabled_by_launcher(combo, ids):
            return OMARCHY, live[-1]["description"] or "Omarchy binding"
        return FREE, ""

    def used_letters(self):
        """Keys taken with the main modifiers (the default set, e.g. Super +
        Shift): live binds, user files and launcher shortcuts."""
        main = make_combo(load_default_mods() if self.main_mods is None else self.main_mods, "A")
        mask = modmask(main) if main else MOD_BITS["SUPER"] | MOD_BITS["SHIFT"]
        used = {b["key"] for b in self.live if b["mask"] == mask}
        used |= {b["combo"]["key"].casefold() for b in self.user_binds if modmask(b["combo"]) == mask}
        used |= {sc["key"].casefold() for entry in self.state["apps"].values()
                 for sc in entry["shortcuts"] if modmask(sc) == mask}
        return used

    def suggestions(self, app, app_id, mods=None, limit=8):
        """Free combos for this app, letters of its name first.

        With `mods`, only that exact modifier set is suggested; without, the
        first free letter of each common Super-based set is offered.
        """
        name = re.sub(r"[^A-Za-z0-9]", "", app.get("name", "")).upper()
        name_letters = [c for c in dict.fromkeys(name) if c.isalpha()]
        keys = name_letters + [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in name_letters]
        # A letter taken with the main modifiers is not offered with any
        # others either, so suggestions never look like they reuse e.g. YouTube's Y.
        keys = [k for k in keys if k.casefold() not in self.used_letters()]
        if mods:
            found = []
            for key in keys:
                combo = make_combo(mods, key)
                if combo and self.classify(combo, app_id)[0] == FREE:
                    found.append(combo)
                    if len(found) >= limit:
                        break
            # Picked name-letters first, shown alphabetically.
            return sorted(found, key=lambda c: c["key"])
        mod_sets = (("SUPER", "ALT"), ("SUPER", "CTRL"), ("SUPER", "CTRL", "ALT"),
                    ("SUPER", "SHIFT", "ALT"), ("SUPER", "SHIFT", "CTRL"))
        found = []
        for mod_set in mod_sets:
            free = self.suggestions(app, app_id, mod_set, 1)
            found.extend(free)
            if len(found) >= limit:
                break
        return found

    def external_shortcuts(self, app):
        """Hand-written user binds and live Omarchy defaults that appear to
        launch this app."""
        needles = set()
        desktop_id = app.get("desktop_id", "")
        if desktop_id:
            needles.add(desktop_id.casefold())
        try:
            program = Path(shlex.split(app.get("exec", ""))[0]).name.casefold()
        except (ValueError, IndexError):
            program = ""
        urls = {_normalize_url(u) for u in _URL_RE.findall(app.get("exec", ""))}
        is_web_app = "--app-id" in app.get("exec", "") or program == "omarchy-launch-webapp"
        name = app.get("name", "").casefold()
        # org.gnome.Nautilus -> nautilus, the name omarchy-launch-* uses.
        short_id = desktop_id.casefold().removesuffix(".desktop").rsplit(".", 1)[-1]

        app_ids = {desktop_id.casefold().removesuffix(".desktop"), program, short_id} - {""}

        def action_args(action):
            try:
                argv = shlex.split(action.get("exec", ""))
            except ValueError:
                return []
            return [a.casefold() for a in argv[1:] if not a.startswith("%")]

        def action_from(words):
            """The action whose extra arguments (e.g. --incognito) the command
            carries; "" for the app itself."""
            best, best_args = "", 0
            for action in app.get("actions", []):
                extra = action_args(action)
                if extra and all(a in words for a in extra) and len(extra) > best_args:
                    best, best_args = action["id"], len(extra)
            return best

        if app.get("tray_id"):
            return []  # Only launcher shortcuts know tray icons.
        plugin_id = app.get("plugin_id", "")
        plugin_re = re.compile(r"omarchy[- ]shell\s+shell\s+(?:toggle|summon)\s+['\"]?"
                               + re.escape(plugin_id.casefold()) + r"(?![\w.-])")

        def launches_app(bind):
            """None when bind does not open this app, else the action id it
            opens ("" for the app itself)."""
            command = bind["command"].casefold()
            if app.get("self_launcher"):
                found = re.search(r"launcher-toggle(?:['\"]?\s+['\"]?(apps|plugins)\b)?", command)
                return (found.group(1) or "") if found else None
            if plugin_id:
                return "" if plugin_re.search(command) else None
            webapp = _WEBAPP_RE.search(bind["command"])
            if webapp:
                hit = (_normalize_url(webapp.group(1)) in urls
                       or (is_web_app and name and name == bind["description"].casefold()))
                return "" if hit else None
            omarchy = _OMARCHY_RE.search(bind["command"])
            if omarchy:
                base, *args = omarchy.group(1).casefold().split()
                if base in DEFAULT_APP_ROLES:
                    default = self.default_app(base).casefold()
                    # Terminal and browser resolve to a desktop id, so web apps and
                    # terminal apps running on them do not match; the editor is a program.
                    own = {program} if base == "editor" else {desktop_id.casefold().removesuffix(".desktop")}
                    if not default or default not in own:
                        return None
                    if not args:
                        return ""
                    return private_action(app) if args == ["--private"] else None
                return "" if not args and base in app_ids else None
            tui = _TUI_RE.search(bind["command"])
            if tui:
                try:
                    target = Path(shlex.split(tui.group(1))[0]).name.casefold()
                except (ValueError, IndexError):
                    return None
                return "" if target in app_ids else None
            words = set(re.findall(r"[\w.\-]+", command))
            if not ("launch" in command or "exec" in command or "gtk-launch" in command):
                return None
            hit = any(n in words or n + ".desktop" in words for n in needles) or (
                program and program not in {"env", "sh", "bash", "flatpak", "gtk-launch", "python3",
                                            "omarchy-launch-webapp"}
                and program in words)
            return action_from(words) if hit else None

        result = []
        for bind in self.user_binds:
            action = launches_app(bind)
            if action is not None:
                result.append({**bind, "action": action})
        user_ids = {combo_id(b["combo"]) for b in self.user_binds} | self.user_unbinds
        for bind in self.default_binds:
            cid = combo_id(bind["combo"])
            # Only defaults Hyprland actually has: not overridden by the user,
            # not gated off (e.g. preinstalled bindings disabled).
            if cid in user_ids or not any(
                    (b["mask"], b["key"]) == cid[:2] and b["description"] == bind["description"]
                    for b in self.live):
                continue
            action = launches_app(bind)
            if action is not None:
                result.append({**bind, "file": "Omarchy", "action": action})
        return result


def _normalize_url(url):
    url = re.sub(r"^https?://", "", url.strip().casefold())
    return re.sub(r"^www\.", "", url).rstrip("/")


# --- writing -----------------------------------------------------------------

def lua_string(value):
    out = ['"']
    for ch in str(value):
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def lua_comment(text):
    """One-line Lua comment; control characters (CR, LF, ...) would end it early."""
    return "-- " + "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in str(text))


# While the shortcut page is open the launcher switches Hyprland to this empty
# submap, so every combo reaches the page instead of running its binding.
# Super + Escape leaves it, should the launcher ever fail to.
CAPTURE_SUBMAP = "simple-launcher-capture"
CAPTURE_ESCAPE = "SUPER + Escape"
CAPTURE_LUA = (f'hl.define_submap("{CAPTURE_SUBMAP}", function() '
               f'hl.bind("{CAPTURE_ESCAPE}", hl.dsp.submap("reset")) end)')


def submap_name(first):
    return "simple-launcher-" + re.sub(r"[^a-z0-9]+", "-", combo_string(first).casefold()).strip("-")


def render_lua(state):
    lines = ["-- Managed by Launch & Bind (ubruckhaus.simple-launcher-for-fools).",
             "-- Edit shortcuts from the launcher; manual changes here are overwritten.", ""]
    sequences = {}  # first step id -> (first step, [(entry, combo, command)])
    for app_id in sorted(state["apps"], key=lambda k: state["apps"][k]["name"].casefold()):
        entry = state["apps"][app_id]
        for combo in entry["disabled"]:
            note = f" (was: {combo['description']})" if combo.get("description") else ""
            lines.append(lua_comment(f"{entry['name']}: disabled{note}"))
            lines.append(f"hl.unbind({lua_string(combo_string(combo))})")
        for combo in entry["shortcuts"]:
            command = " ".join(shlex.quote(a) for a in shortcut_launch(entry["launch"], combo.get("action", "")))
            if combo.get("then"):
                # Plugins and tray icons run directly; uwsm-app is for apps.
                line = command if runs_directly(entry["launch"]) else f"uwsm-app -- {command}"
                first = leader(combo)
                sequences.setdefault(combo_id(first), (first, []))[1].append((entry, combo, line))
                continue
            # Plugins and tray icons run directly; uwsm-app is for apps.
            dispatcher = (lua_string(command) if runs_directly(entry["launch"])
                          else f"{{ launch = {lua_string(command)} }}")
            what = f"{entry['name']}: {combo['action']}" if combo.get("action") else entry["name"]
            note = f" (replaces: {combo['replaces']})" if combo.get("replaces") else ""
            lines.append(lua_comment(f"{what}{note}"))
            lines.append(f"hl.unbind({lua_string(combo_string(combo))})")
            lines.append(f"o.bind({lua_string(combo_string(combo))}, {lua_string(entry['name'])}, "
                         f"{dispatcher})")
        lines.append("")
    lines.append(lua_comment("Key capture for the launcher's shortcut page"))
    lines.append(CAPTURE_LUA)
    lines.append("")
    # Two-step shortcuts: the first step enters a submap where the second key
    # runs the shortcut; any other key leaves it again.
    for first, items in sorted(sequences.values(), key=lambda group: combo_label(group[0])):
        name = submap_name(first)
        replaced = sorted({c["replaces"] for _e, c, _l in items if c.get("replaces")})
        note = f" (replaces: {', '.join(replaced)})" if replaced else ""
        lines.append(lua_comment(f"Two-step shortcuts after {combo_label(first)}{note}"))
        lines.append(f"hl.unbind({lua_string(combo_string(first))})")
        lines.append(f"hl.define_submap({lua_string(name)}, function()")
        for entry, combo, line in sorted(items, key=lambda item: item[1]["then"]):
            what = f"{entry['name']}: {combo['action']}" if combo.get("action") else entry["name"]
            lines.append("  " + lua_comment(f"{key_label(combo['then'])}: {what}"))
            lines.append(f"  hl.bind({lua_string(combo['then'])}, function()")
            lines.append('    hl.dispatch(hl.dsp.submap("reset"))')
            lines.append(f"    hl.exec_cmd({lua_string(line)})")
            lines.append(f"  end, {{ description = {lua_string(what)} }})")
        lines.append('  hl.bind("catchall", hl.dsp.submap("reset"))')
        lines.append("end)")
        summary = ", ".join(f"{key_label(c['then'])} {e['name']}" for e, c, _l in items)
        lines.append(f"hl.bind({lua_string(combo_string(first))}, hl.dsp.submap({lua_string(name)}), "
                     f"{{ description = {lua_string(('Launch & Bind, then: ' + summary)[:160])} }})")
        lines.append("")
    return "\n".join(lines)


def ensure_required():
    """Append the require line to hyprland.lua once, keeping a backup."""
    try:
        text = HYPR_MAIN.read_text()
    except OSError as error:
        raise RuntimeError(f"Cannot read {HYPR_MAIN}: {error}") from error
    if REQUIRE_LINE in text:
        return
    backup = HYPR_MAIN.with_name(f"hyprland.lua.before-simple-launcher-shortcuts")
    if not backup.exists():
        shutil.copy2(HYPR_MAIN, backup)
    suffix = "" if text.endswith("\n") else "\n"
    _atomic_write(HYPR_MAIN, text + suffix +
                  "\n-- Launch & Bind app shortcuts (keep last so they override defaults)\n"
                  + REQUIRE_LINE + "\n")


def _config_errors():
    try:
        subprocess.run(["hyprctl", "reload"], capture_output=True, timeout=5)
        result = subprocess.run(["hyprctl", "configerrors"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    out = result.stdout.strip()
    if not out or out.lower() in {"no errors", "none"}:
        return ""
    return out


def save_and_apply(state):
    """Persist state, regenerate the Lua module and reload Hyprland.

    On a new configuration error the previous files are restored and the
    error text is raised as RuntimeError.
    """
    old_state = STATE_FILE.read_text() if STATE_FILE.exists() else None
    old_lua = LUA_FILE.read_text() if LUA_FILE.exists() else None
    errors_before = _config_errors() if old_lua is not None else ""
    _atomic_write(LUA_FILE, render_lua(state))
    ensure_required()
    _atomic_write(STATE_FILE, json.dumps(state, indent=2) + "\n")
    errors = _config_errors()
    if errors and errors != errors_before:
        if old_lua is None:
            _atomic_write(LUA_FILE, "")
        else:
            _atomic_write(LUA_FILE, old_lua)
        if old_state is None:
            STATE_FILE.unlink(missing_ok=True)
        else:
            _atomic_write(STATE_FILE, old_state)
        _config_errors()
        raise RuntimeError(errors)
