import os
import hashlib
import tempfile
import json
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
import gi
gi.require_version('GioUnix', '2.0')
from gi.repository import GioUnix

HIDDEN_FILE = Path.home() / ".config/applauncher/hidden.json"
HIDDEN_PLUGINS_FILE = Path.home() / ".config/applauncher/hidden-plugins.json"


def load_hidden_apps(path=HIDDEN_FILE):
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return set(data)
        except Exception:
            pass
    return set()


def save_hidden_apps(hidden_set, path=HIDDEN_FILE):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(hidden_set), indent=2))
    except Exception as e:
        print("Could not save hidden apps:", e, file=sys.stderr)


def get_desktop_directories():
    directories = []
    data_home = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")
    directories.append(Path(data_home) / "applications")
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    for directory in data_dirs:
        if directory.strip():
            candidate = Path(directory.strip()) / "applications"
            if candidate not in directories:
                directories.append(candidate)
    return tuple(directories)


DESKTOP_DIRS = get_desktop_directories()
CARD_WIDTH = 300
POPUP_MAX_HEIGHT = 620


def is_pwa(values):
    """Detect Chromium-based PWA desktop files (YouTube, Gemini, …).

    Chrome/Chromium/Edge/Brave PWAs set NoDisplay=true and identify
    themselves via a crx_* StartupWMClass or a bundled PNG icon file.
    """
    if values.get("StartupWMClass", "").startswith("crx_"):
        return True
    icon = values.get("Icon", "")
    return icon.startswith("/") and icon.endswith(".png")


# Browsers whose installed web apps start with --app-id; their menu entry is
# only a copy that the browser writes again while the app stays installed.
WEB_APP_BROWSERS = {
    "google-chrome": "Chrome", "google-chrome-stable": "Chrome", "chrome": "Chrome",
    "chromium": "Chromium", "brave": "Brave", "brave-browser": "Brave",
    "microsoft-edge": "Edge", "microsoft-edge-stable": "Edge", "vivaldi": "Vivaldi",
}


def browser_web_app(app):
    """The browser name when app is a web app installed in a Chromium-based
    browser (Chrome's "Install page as app"), else None."""
    try:
        argv = shlex.split(app.get("exec", ""))
    except ValueError:
        return None
    if not argv or not any(a.startswith("--app-id=") for a in argv):
        return None
    return WEB_APP_BROWSERS.get(Path(argv[0]).name, "the browser")


def executable_exists(command):
    """Check the program named by a desktop entry without running it."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    if parts[0] == "env":
        parts = parts[1:]
        while parts and ("=" in parts[0] or parts[0] in ("-i", "--ignore-environment")):
            parts = parts[1:]
    if not parts:
        return False
    program = parts[0]
    return os.access(program, os.X_OK) if "/" in program else shutil.which(program) is not None


def parse_desktop_files():
    apps, seen_desktop_ids, seen_names = [], set(), set()
    for directory in DESKTOP_DIRS:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.desktop"):
            # XDG Specification: Higher priority directories shadow lower priority ones.
            desktop_id = path.name
            if desktop_id in seen_desktop_ids:
                continue
            seen_desktop_ids.add(desktop_id)

            values, section, action_execs = {}, None, {}
            try:
                for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw.strip()
                    if line.startswith("["):
                        section = line.strip("[]")
                    elif section == "Desktop Entry" and "=" in line:
                        key, value = line.split("=", 1)
                        values[key] = value
                    elif (section or "").startswith("Desktop Action ") and line.startswith("Exec="):
                        action_execs[section.removeprefix("Desktop Action ")] = line[5:]
            except OSError:
                continue
            name, command = values.get("Name"), values.get("Exec")
            if not name or not command:
                continue
            if values.get("TryExec") and not executable_exists(values["TryExec"]):
                continue
            if not executable_exists(command):
                continue
            no_display = values.get("NoDisplay", "").lower() in ("true", "1")
            # PWAs set NoDisplay=true but are real apps the user wants to find.
            if no_display and not is_pwa(values):
                continue

            # Deduplicate by normalized app name (e.g. YouTube webapp vs Chrome PWA)
            name_key = name.strip().casefold()
            if name_key in seen_names:
                continue
            seen_names.add(name_key)

            command = re.sub(r"%[a-zA-Z]", "", command).strip()
            actions = []
            try:
                info = GioUnix.DesktopAppInfo.new_from_filename(str(path))
                if info:
                    actions = [{"id": action, "name": info.get_action_name(action),
                                "exec": action_execs.get(action, "")}
                               for action in info.list_actions()]
            except Exception:
                actions = []

            desc = values.get("Comment", "").strip()
            apps.append({"name": name, "exec": command, "icon": values.get("Icon", ""),
                         "desktop_path": str(path), "actions": actions,
                         "description": desc,
                         "desktop_id": path.stem, "startup_class": values.get("StartupWMClass", "")})

    apps.extend(scan_standalone_appimages(seen_names))
    return sorted(apps, key=lambda app: app["name"].lower())


def get_appimage_directories():
    candidates = [
        Path.home() / "AppImages",
        Path.home() / "Applications",
        Path.home() / ".local/bin",
        Path.home() / "bin",
    ]
    return [d for d in candidates if d.is_dir()]


def read_bounded_output(command, max_bytes=64 * 1024, timeout=1, binary=False):
    """Read optional archive metadata with a byte limit and a total deadline."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        return None
                    chunk = os.read(process.stdout.fileno(),
                                    min(8192, max_bytes + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > max_bytes:
                        return None
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0:
                return None
            return bytes(output) if binary else output.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return None
        finally:
            # Reap extractors even when they flood output or close stdout and hang.
            if process.poll() is None:
                process.kill()
            process.wait()


MAX_APPIMAGE_ICON_BYTES = 1024 * 1024


def cache_appimage_icon(path, raw_icon, cache_dir):
    """Extract to a bounded pipe, never let the archive tool write files."""
    icon_name = Path(raw_icon).name
    if not icon_name or icon_name in {".", ".."}:
        return ""
    # Archive contents never determine a destination path. One cache slot per
    # AppImage also prevents repeated scans accumulating extracted files.
    key = hashlib.sha256(os.fsencode(str(path.resolve()))).hexdigest()
    target = cache_dir / (key + ".icon")
    members = [icon_name] if icon_name.endswith((".png", ".svg")) else [icon_name + ".png", icon_name + ".svg"]
    for member in members:
        data = read_bounded_output(
            ["7z", "e", "-so", "-spd", str(path), member],
            max_bytes=MAX_APPIMAGE_ICON_BYTES, timeout=2, binary=True)
        if not data:
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            # Atomic replacement avoids following an existing cache symlink.
            os.replace(temporary, target)
            return str(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return ""


def scan_standalone_appimages(seen_names):
    apps = []
    icon_cache_dir = Path.home() / ".cache/applauncher/icons"
    has_7z = shutil.which("7z") is not None

    for directory in get_appimage_directories():
        for path in directory.glob("*.[aA][pP][pP][iI][mM][aA][gG][eE]"):
            # Discovery must respect user permissions, never make files executable.
            if not path.is_file() or not os.access(path, os.X_OK):
                continue

            app_name = ""
            app_desc = ""
            icon_path = ""
            startup_class = ""
            raw_icon = ""

            if has_7z:
                try:
                    desktop_text = read_bounded_output(
                        ["7z", "e", "-so", str(path), "*.desktop"])
                    if desktop_text and "[Desktop Entry]" in desktop_text:
                        for line in desktop_text.splitlines():
                            if line.startswith("Name=") and not app_name:
                                app_name = line.split("=", 1)[1].strip()
                            elif line.startswith("Comment=") and not app_desc:
                                app_desc = line.split("=", 1)[1].strip()
                            elif line.startswith("StartupWMClass="):
                                startup_class = line.split("=", 1)[1].strip()
                            elif line.startswith("Icon=") and not raw_icon:
                                raw_icon = line.split("=", 1)[1].strip()
                        if raw_icon:
                            icon_path = cache_appimage_icon(path, raw_icon, icon_cache_dir)
                except Exception:
                    pass

            if not app_name:
                clean_name = path.stem
                clean_name = re.sub(r"[-_](?:v?\d+[\d\.\-+_]*)(?:[-_]x86_64|[-_]x64|[-_]amd64)?", "", clean_name, flags=re.IGNORECASE)
                clean_name = clean_name.replace("-", " ").replace("_", " ").strip()
                app_name = clean_name or path.stem

            name_key = app_name.strip().casefold()
            # If the app name is already present from a desktop file, skip
            if name_key in seen_names:
                continue
            normalized = name_key.replace("-", "").replace(" ", "")
            if any(normalized == s.replace("-", "").replace(" ", "") for s in seen_names):
                continue

            seen_names.add(name_key)
            apps.append({
                "name": app_name,
                "exec": shlex.join([str(path)]),
                "argv": [str(path)],
                "icon": icon_path or "application-x-executable",
                "desktop_path": "",
                "actions": [],
                "description": app_desc or "",
                "desktop_id": path.stem,
                "startup_class": startup_class or path.stem,
            })
    return apps


SELF_PLUGIN_ID = "ubruckhaus.simple-launcher-for-fools"
# Shell plugins that are not something to open from a list: this launcher,
# and helpers the shell summons itself with a payload.
INTERNAL_PLUGINS = {SELF_PLUGIN_ID, "omarchy.image-picker", "omarchy.osd"}
# Kinds the shell opens through its own panel loader.
LOADER_KINDS = ("panel", "overlay", "menu")
PLUGIN_ICON = "application-x-addon-symbolic"
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_PANEL_ROOT_RE = re.compile(r"^(?:[A-Za-z]+\.)?Panel\s*\{", re.MULTILINE)


def plugin_roots():
    omarchy = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
    return (Path.home() / ".config/omarchy/plugins", omarchy / "shell/plugins")


def plugin_manifests():
    """id -> (directory, manifest) for installed shell plugins."""
    found = {}
    for root in plugin_roots():
        if not root.is_dir():
            continue
        for path in sorted([*root.glob("*/manifest.json"), *root.glob("*/*/manifest.json")]):
            try:
                manifest = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            plugin_id = manifest.get("id") if isinstance(manifest, dict) else None
            if isinstance(plugin_id, str) and plugin_id not in found:
                found[plugin_id] = (path.parent, manifest)
    return found


def plugin_can_open(directory, manifest):
    """Whether `omarchy-shell shell summon` can open this plugin: a panel,
    overlay or menu, or a bar widget that is a panel itself. Bar widgets that
    only run a command when clicked have nothing the shell can open."""
    kinds = manifest.get("kinds") or []
    if any(kind in kinds for kind in LOADER_KINDS):
        return True
    if "bar-widget" not in kinds:
        return False
    entry = (manifest.get("entryPoints") or {}).get("barWidget")
    if not isinstance(entry, str):
        return False
    try:
        path = (directory / entry).resolve()
        if not path.is_relative_to(directory.resolve()):
            return False
        text = path.read_text(errors="ignore")
    except (OSError, ValueError):
        return False
    return bool(_PANEL_ROOT_RE.search(text)) or ("function open" in text and "opened" in text)


def self_entry(in_bar=None):
    """The launcher itself, listed so its own shortcut can be set."""
    return {
        "name": "Launch & Bind",
        "plugin_id": SELF_PLUGIN_ID,
        "self_launcher": True,
        "exec": str(Path(__file__).resolve().parent / "launcher-toggle"),
        "icon": "view-app-grid-symbolic",
        "desktop_path": "",
        # Each list can have its own shortcut: `launcher-toggle apps|plugins`.
        "actions": [{"id": "apps", "name": "Show apps", "exec": ""},
                    {"id": "plugins", "name": "Show widgets & plugins", "exec": ""}],
        "description": "This launcher — set the keys that open it",
        "desktop_id": f"plugin:{SELF_PLUGIN_ID}",
        "startup_class": "",
        "removable": False,
        # Its bar button can be taken out of the bar and put back like any widget.
        "bar_widget": in_bar is not None,
        "in_bar": bool(in_bar),
        "openable": False,
    }


def parse_plugins():
    """Shell plugins that have something to open, as list entries shaped
    like apps, and the launcher itself: enabled panels and overlays, and bar
    widgets with a panel, in the bar or not (out of it they can be put back).
    Bar parts with nothing to open (Workspaces, Indicators, …) are left out."""
    if not shutil.which("omarchy-shell"):
        return [self_entry()]
    output = read_bounded_output(["omarchy-shell", "shell", "listPlugins"], max_bytes=512 * 1024, timeout=2)
    try:
        listed = json.loads(output) if output else []
    except ValueError:
        return []
    manifests = plugin_manifests()
    listed = listed if isinstance(listed, list) else []
    own = next((i for i in listed if isinstance(i, dict) and i.get("id") == SELF_PLUGIN_ID), None)
    plugins = [self_entry(bool(own.get("enabled")) if own else None)]
    for item in listed:
        if not isinstance(item, dict):
            continue
        plugin_id = str(item.get("id", ""))
        if not _PLUGIN_ID_RE.match(plugin_id) or plugin_id in INTERNAL_PLUGINS:
            continue
        directory, manifest = manifests.get(plugin_id, (None, None))
        if manifest is None:
            continue  # Parts built into the bar: nothing to open.
        kinds = manifest.get("kinds") or []
        # For a bar widget the shell's "enabled" means: placed in the bar.
        bar_widget = "bar-widget" in kinds
        enabled = bool(item.get("enabled"))
        can_open = plugin_can_open(directory, manifest)
        # A widget's panel opens from its bar button, so it needs to be in the bar.
        openable = can_open and (enabled or any(kind in kinds for kind in LOADER_KINDS))
        if not can_open or not (enabled or bar_widget):
            continue
        description = manifest.get("description")
        plugins.append({
            "name": str(item.get("name") or manifest.get("name") or plugin_id),
            "plugin_id": plugin_id,
            "exec": f"omarchy-shell shell toggle {plugin_id}",
            "icon": PLUGIN_ICON,
            "desktop_path": "",
            "actions": [],
            "description": description.strip() if isinstance(description, str) else "",
            "desktop_id": f"plugin:{plugin_id}",
            "startup_class": "",
            "removable": plugin_removable(plugin_id),
            "bar_widget": bar_widget,
            "bar_section": (manifest.get("barWidget") or {}).get("defaultSection")
                           if isinstance(manifest.get("barWidget"), dict) else None,
            "in_bar": bar_widget and enabled,
            "openable": openable,
        })
    return sorted(plugins, key=lambda plugin: plugin["name"].lower())


BAR_SECTIONS = ("left", "center", "right")


def set_in_bar(plugin_id, placed, section=None):
    """argv that puts a bar widget into a bar section (or its default one)
    or takes it out, through Omarchy's own commands; None when they are
    missing or the id or section is not valid."""
    if not _PLUGIN_ID_RE.match(plugin_id) or (section is not None and section not in BAR_SECTIONS):
        return None
    command = "omarchy-plugin-enable" if placed else "omarchy-plugin-disable"
    if not shutil.which(command):
        return None
    return [command, plugin_id, section] if placed and section else [command, plugin_id]


USER_PLUGINS_DIR = Path.home() / ".config/omarchy/plugins"


def plugin_removable(plugin_id):
    """Installed plugins (marketplace, git or linked) can be removed;
    Omarchy's built-in ones cannot."""
    target = USER_PLUGINS_DIR / plugin_id
    return (_PLUGIN_ID_RE.match(plugin_id) is not None and ".." not in plugin_id
            and (target.is_dir() or target.is_symlink())
            and shutil.which("omarchy-plugin-remove") is not None)


def _git_count(directory, *args):
    """git output (bounded: a checkout may have any number of changes), or None."""
    try:
        return read_bounded_output(["git", "-C", str(directory), *args], max_bytes=256 * 1024, timeout=3)
    except OSError:
        return None


def plugin_removal_note(plugin_id):
    """What `omarchy plugin remove` does with this plugin's folder, and what
    would be lost: it deletes git checkouts outright."""
    target = USER_PLUGINS_DIR / plugin_id
    if target.is_symlink():
        return "Only the link is removed; the folder it points to stays.", False
    if not (target / ".git").is_dir():
        return "The folder is kept as a backup in ~/.config/omarchy/plugins.", False
    lost = []
    changed = _git_count(target, "status", "--porcelain")
    if changed is None:
        lost.append("its git state could not be checked")
    elif changed.strip():
        count = len(changed.strip().splitlines())
        lost.append(f"{count} changed file{'s' if count != 1 else ''}")
    ahead = _git_count(target, "rev-list", "--count", "@{u}..HEAD")
    if ahead is None:
        lost.append("commits that exist nowhere else (no upstream)")
    elif ahead.strip() not in ("", "0"):
        lost.append(f"{ahead.strip()} unpushed commit{'s' if ahead.strip() != '1' else ''}")
    if lost:
        return "The folder is deleted, including " + " and ".join(lost) + ".", True
    return "The folder is deleted; it can be installed again from the marketplace.", False


def open_plugin(plugin):
    """Open a shell plugin; False when the shell could not."""
    try:
        output = read_bounded_output(["omarchy-shell", "shell", "summon", plugin["plugin_id"], "{}"],
                                     max_bytes=4096, timeout=3)
    except OSError:
        return False
    return (output or "").strip() == "ok"


def app_window_classes(app):
    names = {app.get("startup_class", ""), app.get("desktop_id", "")}
    explicit_class = app.get("startup_class", "")
    try:
        args = shlex.split(app["exec"])
    except ValueError:
        args = []
    if args:
        executable = Path(args[0]).name
        has_class_argument = any(arg.startswith(("--class=", "--app-id=")) or arg in {"--class", "--app-id"} for arg in args)
        if not explicit_class and not has_class_argument and executable not in {"env", "sh", "bash", "flatpak", "gtk-launch"}:
            names.add(executable)
        for index, arg in enumerate(args):
            if arg.startswith(("--class=", "--app-id=")):
                names.add(arg.split("=", 1)[1])
            elif arg in {"--class", "--app-id"} and index + 1 < len(args):
                names.add(args[index + 1])
            elif arg.startswith("--app="):
                url = arg.split("=", 1)[1]
                host = urllib.parse.urlparse(url).netloc
                if host:
                    names.update({f"chrome-{host}__-default", f"chromium-{host}__-default", f"chrome-{host}", f"chromium-{host}"})
            elif arg == "--app" and index + 1 < len(args):
                host = urllib.parse.urlparse(args[index + 1]).netloc
                if host:
                    names.update({f"chrome-{host}__-default", f"chromium-{host}__-default", f"chrome-{host}", f"chromium-{host}"})

    # Expand crx / Chrome PWA app-id variants across Wayland and X11
    for cls in list(names):
        raw = cls.removeprefix("crx_")
        if raw != cls or (len(raw) == 32 and raw.isalpha()):
            names.update({f"crx_{raw}", f"chrome-{raw}-default", f"chromium-{raw}-default", f"chrome-{raw}", f"chromium-{raw}"})

    return {name.casefold() for name in names if name}


def terminal_command(app):
    """For a terminal launcher like `foot -e /path/claude`, the command's
    name ("claude"); None for anything else."""
    try:
        args = shlex.split(app.get("exec", ""))
    except ValueError:
        return None
    if not args or Path(args[0]).name.casefold() not in TERMINALS:
        return None
    for flag in ("-e", "--command", "--"):
        if flag in args:
            rest = args[args.index(flag) + 1:]
            return Path(rest[0]).name.casefold() if rest else None
    return None


def child_process_names(pid):
    """Names of a process's direct children: exe and argv[0..1] basenames, so
    scripts (bash pi-with-llama) and node tools (node codex) are found too."""
    names = set()
    try:
        children = Path(f"/proc/{int(pid)}/task/{int(pid)}/children").read_text().split()
    except (OSError, ValueError, TypeError):
        return names
    for child in children:
        try:
            names.add(Path(os.readlink(f"/proc/{child}/exe")).name.casefold())
            argv = Path(f"/proc/{child}/cmdline").read_bytes().split(b"\0")[:2]
            names.update(Path(a.decode(errors="replace")).name.casefold() for a in argv if a)
        except OSError:
            continue
    return names


def client_matches_app(client, app, classes=None):
    if classes is None:
        classes = app_window_classes(app)
    client_classes = {
        client.get("class", "").casefold(),
        client.get("initialClass", "").casefold(),
    } - {""}
    if classes & client_classes:
        return True

    # Check Chrome/Chromium/Brave PWA patterns: chrome-<domain>__-<profile>
    for cls in client_classes:
        match = re.match(r"^(?:chrome|chromium|brave)-(.+?)(?:__.*|-.*)?$", cls)
        if not match:
            continue
        target = match.group(1).casefold()

        # 1. Match against app_id / startup_class (crx_...)
        raw_id = app.get("startup_class", "").removeprefix("crx_").casefold()
        if raw_id and (target == raw_id or target.startswith(raw_id)):
            return True

        # 2. Match window title suffix: Chrome PWAs format titles as "<Page> - <App Name>"
        title = client.get("title", "")
        app_name = app.get("name", "").strip()
        if app_name and (title.endswith(f" - {app_name}") or title.endswith(f" — {app_name}") or title == app_name):
            return True

        # 3. Match domain keywords in window class against app name words
        target_tokens = set(re.findall(r"[a-z0-9]{3,}", target)) - {"com", "org", "net", "app", "default", "io"}
        name_tokens = set(re.findall(r"[a-z0-9]{3,}", app_name.casefold()))
        if target_tokens and name_tokens and (target_tokens.issubset(name_tokens) or name_tokens.issubset(target_tokens)):
            return True

    return False


def _icon_search_roots():
    roots = []
    data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    roots.append(data_home / "icons")
    for directory in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if directory.strip():
            roots.append(Path(directory.strip()) / "icons")
    seen = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


def _icon_fallback_dirs():
    """Directories to check for a bare icon filename: icon theme trees
    (all XDG contexts, NxN sizes + scalable/symbolic) and the legacy
    <XDG_DATA_DIR>/pixmaps dirs."""
    dirs = []
    contexts = ("apps", "legacy", "status", "devices", "actions",
                "places", "mimetypes", "categories", "stock")
    for root in _icon_search_roots():
        try:
            theme_dirs = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            continue
        for theme_dir in theme_dirs:
            try:
                # "x" matches NxM size dirs (32x32, …); also accept scalable/ (SVG)
                size_dirs = [p for p in theme_dir.iterdir()
                             if p.is_dir() and ("x" in p.name or p.name in ("scalable", "symbolic"))]
            except OSError:
                continue
            for size_dir in size_dirs:
                for ctx in contexts:
                    dirs.append(size_dir / ctx)
    for directory in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if directory.strip():
            dirs.append(Path(directory.strip()) / "pixmaps")
    return dirs


def _hicolor_fallback(icon_name):
    """Find <icon_name> in the icon trees without relying on the active
    icon theme (e.g. chrome-<id>-Default PWA icons, AdwaitaLegacy's
    legacy/ context, /usr/share/pixmaps)."""
    best = None
    for directory in _icon_fallback_dirs():
        for ext in (".png", ".svg"):
            candidate = directory / (icon_name + ext)
            if candidate.is_file():
                if best is None or candidate.stat().st_size > best[1]:
                    best = (candidate, candidate.stat().st_size)
    if best is not None:
        return QIcon(str(best[0]))
    return QIcon()


def resolve_icon(raw_icon):
    """Resolve an app icon to a non-null QIcon.

    Handles absolute paths (flatpak icons, …), theme icon names, and PWA
    desktop files whose Icon= path points inside the browser's sandbox and
    doesn't exist on the host (the icon is installed in the hicolor theme
    under its basename, e.g. chrome-<extid>-Default).
    """
    if not QIcon.themeName():
        # Bare Qt apps on this Hyprland/DMS setup inherit no icon theme, so
        # QIcon.fromTheme() always failed. hicolor is where app icons actually
        # live (gnome-text-editor, kitty, firefox, …).
        QIcon.setThemeName("hicolor")
    if not raw_icon:
        return QIcon()
    if os.path.exists(raw_icon):
        return QIcon(raw_icon)
    base = os.path.basename(raw_icon)
    # Strip a real image extension, but NOT the dots of reverse-DNS names
    # (org.gnome.TextEditor) — splitext() would truncate those at the last dot.
    ext = os.path.splitext(base)[1].lower()
    icon_name = os.path.splitext(base)[0] if ext in (".png", ".svg", ".xpm", ".jpg", ".jpeg", ".webp", ".bmp") else base
    themed = QIcon.fromTheme(icon_name)
    if not themed.isNull():
        return themed
    return _hicolor_fallback(icon_name) if icon_name else QIcon()


TERMINALS = {"foot", "footclient", "alacritty", "kitty", "ghostty", "wezterm", "xterm"}


def app_process_name(app):
    try:
        args = shlex.split(app["exec"])
    except ValueError:
        return None
    if args and Path(args[0]).name == "env":
        args = args[1:]
        while args and "=" in args[0] and not args[0].startswith("-"):
            args = args[1:]
    if not args:
        return None
    name = Path(args[0]).name.casefold()
    # Shared interpreters/wrappers cannot identify an individual application.
    if name in {"sh", "bash", "env", "flatpak", "gtk-launch", "python", "python3", "java"}:
        return None
    # A terminal running a command (foot -e claude) is the terminal's process,
    # often a shared server; such apps are found by their window instead.
    if name in TERMINALS and ("-e" in args or "--command" in args):
        return None
    return name


def process_pids(name):
    """PIDs of the user's processes whose executable is called name."""
    pids = set()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid == os.getuid() and Path(os.readlink(process / "exe")).name.casefold() == name:
                pids.add(int(process.name))
        except (OSError, ValueError):
            continue
    return pids


def background_process_names():
    names = set()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid != os.getuid():
                continue
            names.add(Path(os.readlink(process / "exe")).name.casefold())
        except OSError:
            continue
    return names


def _clean_env():
    env = os.environ.copy()
    if "LD_PRELOAD" in env:
        parts = [p for p in env["LD_PRELOAD"].split(":") if p and "libgtk4-layer-shell" not in p]
        if parts:
            env["LD_PRELOAD"] = ":".join(parts)
        else:
            del env["LD_PRELOAD"]
    return env


def launch_app(app):
    # Standalone AppImages are executable paths, never shell programs. A failed
    # direct launch must not fall through to another launch method.
    if "argv" in app:
        try:
            subprocess.Popen(
                app["argv"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_clean_env(),
            )
            return True
        except OSError:
            return False

    desktop_path = app.get("desktop_path", "")

    # 1. Primary: uwsm-app (places app in app-graphical.slice and natively resolves desktop files)
    if desktop_path and shutil.which("uwsm-app"):
        try:
            subprocess.Popen(
                ["uwsm-app", "--", desktop_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_clean_env(),
            )
            return True
        except OSError:
            pass

    # Native fallback uses the same file and desktop-entry argument semantics.
    if desktop_path:
        try:
            info = GioUnix.DesktopAppInfo.new_from_filename(desktop_path)
            if info:
                return bool(info.launch_uris([], None))
        except Exception:
            pass

    # Exec is display/matching metadata, never a shell command. A native
    # rejection must remain a failure instead of gaining shell semantics.

    return False


def launch_action(app, action_id):
    desktop_path = app.get("desktop_path", "")
    if desktop_path and shutil.which("uwsm-app"):
        try:
            subprocess.Popen(
                ["uwsm-app", "--", f"{desktop_path}:{action_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_clean_env(),
            )
            return True
        except OSError:
            pass

    if desktop_path:
        try:
            info = GioUnix.DesktopAppInfo.new_from_filename(desktop_path)
            if info and action_id in info.list_actions():
                info.launch_action(action_id, None)
                return True
        except Exception:
            pass
    return False
