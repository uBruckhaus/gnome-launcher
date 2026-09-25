import os
import json
import re
import shlex
import shutil
import signal
import subprocess
import sys
import urllib.parse
from pathlib import Path
import gi
gi.require_version('GioUnix', '2.0')
from gi.repository import GioUnix

HIDDEN_FILE = Path.home() / ".config/applauncher/hidden.json"


def load_hidden_apps():
    if HIDDEN_FILE.exists():
        try:
            data = json.loads(HIDDEN_FILE.read_text())
            if isinstance(data, list):
                return set(data)
        except Exception:
            pass
    return set()


def save_hidden_apps(hidden_set):
    try:
        HIDDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        HIDDEN_FILE.write_text(json.dumps(sorted(hidden_set), indent=2))
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

            values, section = {}, None
            try:
                for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw.strip()
                    if line.startswith("["):
                        section = line.strip("[]")
                    elif section == "Desktop Entry" and "=" in line:
                        key, value = line.split("=", 1)
                        values[key] = value
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
                    actions = [{"id": action, "name": info.get_action_name(action)}
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

            if has_7z:
                try:
                    res = subprocess.run(
                        ["7z", "e", "-so", str(path), "*.desktop"],
                        capture_output=True, text=True, timeout=1
                    )
                    if res.returncode == 0 and "[Desktop Entry]" in res.stdout:
                        for line in res.stdout.splitlines():
                            if line.startswith("Name=") and not app_name:
                                app_name = line.split("=", 1)[1].strip()
                            elif line.startswith("Comment=") and not app_desc:
                                app_desc = line.split("=", 1)[1].strip()
                            elif line.startswith("StartupWMClass="):
                                startup_class = line.split("=", 1)[1].strip()
                            elif line.startswith("Icon="):
                                raw_icon = line.split("=", 1)[1].strip()
                                icon_cache_dir.mkdir(parents=True, exist_ok=True)
                                target_icon = icon_cache_dir / f"{path.stem}_{Path(raw_icon).name}.png"
                                if target_icon.exists():
                                    icon_path = str(target_icon)
                                else:
                                    subprocess.run(
                                        ["7z", "e", "-y", f"-o{icon_cache_dir}", str(path),
                                         f"*{raw_icon}*.png", f"*{raw_icon}*.svg"],
                                        capture_output=True, timeout=2
                                    )
                                    found = list(icon_cache_dir.glob(f"*{raw_icon}*"))
                                    if found:
                                        icon_path = str(found[0])
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
                "exec": f'"{path}"',
                "icon": icon_path or "application-x-executable",
                "desktop_path": "",
                "actions": [],
                "description": app_desc or "",
                "desktop_id": path.stem,
                "startup_class": startup_class or path.stem,
            })
    return apps


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
    return name


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
    desktop_path = app.get("desktop_path", "")
    desktop_file = Path(desktop_path).name if desktop_path else ""

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

    if desktop_file and shutil.which("uwsm-app"):
        try:
            subprocess.Popen(
                ["uwsm-app", "--", desktop_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_clean_env(),
            )
            return True
        except OSError:
            pass

    # 3. Tertiary: GioUnix.DesktopAppInfo
    if desktop_path:
        try:
            info = GioUnix.DesktopAppInfo.new_from_filename(desktop_path)
            if info:
                info.launch_uris([], None)
                return True
        except Exception:
            pass

    # 4. Fallback: raw shell execution
    command = app.get("exec", "")
    if command:
        try:
            subprocess.Popen(
                ["sh", "-c", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_clean_env(),
            )
            return True
        except OSError:
            pass

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
