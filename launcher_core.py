import os
import ctypes
import hashlib
import tempfile
import json
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
import gi
import i18n
from i18n import _, ngettext

# The interface language is stored next to the launcher's other files.
i18n.SETTINGS_FILE = Path.home() / ".config/applauncher/settings.json"
gi.require_version('GioUnix', '2.0')
from gi.repository import GioUnix

HIDDEN_FILE = Path.home() / ".config/applauncher/hidden.json"
SETTINGS_FILE = i18n.SETTINGS_FILE


def _load_settings(path=None):
    path = Path(path or SETTINGS_FILE)
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(64 * 1024) or b"{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_setting(key, value, path=None):
    """Set one key in settings.json (atomic, private), keeping the others."""
    path = Path(path or SETTINGS_FILE)
    data = _load_settings(path)
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


ADMIN_FLAG_NAME = "app-manager-admin-action"


def _session_dir():
    """The login session's private runtime folder ($XDG_RUNTIME_DIR), or None
    unless it is a real folder of ours that nobody else can enter. It is
    emptied when the user logs out."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not os.path.isabs(runtime):
        return None
    try:
        info = os.lstat(runtime)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    return Path(runtime)


def _admin_flag(path=None):
    if path is not None:
        return Path(path)
    folder = _session_dir()
    return folder / ADMIN_FLAG_NAME if folder else None


SESSIONS_DIR = Path("/run/systemd/sessions")
BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")


def session_token(sessions=SESSIONS_DIR, boot_id_file=BOOT_ID_FILE, owner=0):
    """Who switched "Open as administrator" on: this boot, this login session
    and the session's start time, from systemd-logind's record of it
    (root-owned, removed at logout). None when that is not known. The runtime
    folder alone is not enough: it outlives a logout while user services keep
    the user's systemd instance running."""
    sid = os.environ.get("XDG_SESSION_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", sid):
        return None
    record = Path(sessions) / sid
    try:
        info = os.lstat(record)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
            return None
        with open(record, "rb") as fh:
            fields = dict(line.split("=", 1) for line in fh.read(64 * 1024).decode("utf-8", "replace").splitlines()
                          if "=" in line)
        with open(boot_id_file, "rb") as fh:
            boot = fh.read(64).decode("ascii", "replace").strip()
    except OSError:
        return None
    if fields.get("UID") != str(os.getuid()) or fields.get("STATE") not in ("active", "online"):
        return None
    started = fields.get("REALTIME", "")
    if not re.fullmatch(r"[0-9]{1,20}", started) or not re.fullmatch(r"[0-9a-f-]{36}", boot):
        return None
    return f"{boot}:{sid}:{started}"


def admin_action_enabled(path=None, token=None):
    """'Open as administrator' in the app menu: opt-in, switched on only after
    a password check, and only for the login session that did it. The marker
    in the session's runtime folder names that session; any other session
    (a new login, or after a reboot) reads it as off."""
    flag = _admin_flag(path)
    token = token if token is not None else session_token()
    if flag is None or not token:
        return False
    try:
        fd = os.open(flag, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        return os.read(fd, 256).decode("ascii", "replace").strip() == token
    finally:
        os.close(fd)


def set_admin_action(enabled, path=None, token=None):
    flag = _admin_flag(path)
    if not enabled:
        if flag is not None:
            try:
                os.unlink(flag)
            except FileNotFoundError:
                pass
        return
    token = token if token is not None else session_token()
    if flag is None or not token:
        raise OSError("no private session folder or login session to tie the setting to")
    tmp = flag.with_name(f".{flag.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, token.encode("ascii"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, flag)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def forget_saved_admin_action(path=None):
    """Up to 1.7.x the switch was saved for good in settings.json; drop it."""
    path = Path(path or SETTINGS_FILE)
    data = _load_settings(path)
    if "admin_action" in data:
        del data["admin_action"]
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


def running_first_enabled(path=None):
    """Header toggle on the app list: running apps in their own group on top."""
    return _load_settings(path).get("running_first") is True


def set_running_first(enabled, path=None):
    _save_setting("running_first", bool(enabled), path)


def saved_view(path=None):
    """The list as a plain list, or in groups (header toggle, Ctrl+G)."""
    view = _load_settings(path).get("view")
    return view if view in ("groups", "origin") else "list"


def set_view(view, path=None):
    _save_setting("view", view if view in ("groups", "origin") else "list", path)
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
    return WEB_APP_BROWSERS.get(Path(argv[0]).name, _("the browser"))


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
                         "terminal": values.get("Terminal", "").strip().lower() in ("true", "1"),
                         "sandboxed": bool(values.get("X-Flatpak") or values.get("X-SnapInstanceName")),
                         "pwa": is_pwa(values),
                         "categories": [c for c in values.get("Categories", "").split(";") if c][:20],
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

# Icon files come from .desktop files, AppImages and tray peers (via their
# IconThemePath), so none of them is decoded on trust. The declared size is
# read from the header first: a tiny PNG can declare a canvas that the image
# loaders would allocate in full before scaling it down to 32 px.
MAX_ICON_FILE_BYTES = 4 * 1024 * 1024
MAX_SVG_ICON_BYTES = 1024 * 1024
MAX_ICON_SIDE = 4096
MAX_ICON_PIXELS = 4096 * 4096        # width × height × frames


def _image_header_size(handle):
    """``(width, height, frames)`` from a PNG/JPEG/GIF/WebP/BMP header,
    reading only headers and block lengths, never pixel data; else None."""
    head = handle.read(32)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        if head[12:16] != b"IHDR":
            return None
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"), 1
    if head[:3] == b"\xff\xd8\xff":
        return _jpeg_size(handle)
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return _gif_size(handle, head)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _webp_size(handle)
    if head[:2] == b"BM" and len(head) >= 26:
        if int.from_bytes(head[14:18], "little") == 12:   # OS/2 core header
            return int.from_bytes(head[18:20], "little"), int.from_bytes(head[20:22], "little"), 1
        width = int.from_bytes(head[18:22], "little", signed=True)
        height = int.from_bytes(head[22:26], "little", signed=True)
        return abs(width), abs(height), 1
    return None


def _jpeg_size(handle):
    handle.seek(2)
    for _ in range(10000):   # segments are walked by length, never scanned
        if handle.read(1) != b"\xff":
            return None
        marker = handle.read(1)
        while marker == b"\xff":
            marker = handle.read(1)
        if not marker:
            return None
        code = marker[0]
        if code == 0x01 or 0xD0 <= code <= 0xD7:
            continue
        if code in (0xD9, 0xDA):   # no frame header before the scan
            return None
        length = int.from_bytes(handle.read(2), "big")
        if length < 2:
            return None
        if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
            data = handle.read(5)
            if len(data) < 5:
                return None
            return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big"), 1
        handle.seek(length - 2, 1)
    return None


def _gif_size(handle, head):
    width = int.from_bytes(head[6:8], "little")
    height = int.from_bytes(head[8:10], "little")
    pos = 13
    if head[10] & 0x80:
        pos += 3 << ((head[10] & 0x07) + 1)
    handle.seek(pos)
    frames = 0

    def skip_sub_blocks():
        while True:
            size = handle.read(1)
            if not size:
                return False
            if size[0] == 0:
                return True
            handle.seek(size[0], 1)

    while True:
        block = handle.read(1)
        if block == b"\x2c":
            desc = handle.read(9)
            if len(desc) < 9:
                break
            frames += 1
            width = max(width, int.from_bytes(desc[0:2], "little") + int.from_bytes(desc[4:6], "little"))
            height = max(height, int.from_bytes(desc[2:4], "little") + int.from_bytes(desc[6:8], "little"))
            if desc[8] & 0x80:
                handle.seek(3 << ((desc[8] & 0x07) + 1), 1)
            handle.read(1)
            if not skip_sub_blocks():
                break
            if width * height * frames > MAX_ICON_PIXELS:
                break
        elif block == b"\x21":
            handle.read(1)
            if not skip_sub_blocks():
                break
        else:
            break
    return (width, height, frames) if frames else None


def _webp_size(handle):
    handle.seek(12)
    size = None
    frames = 0
    for _ in range(100000):
        header = handle.read(8)
        if len(header) < 8:
            break
        kind, length = header[:4], int.from_bytes(header[4:8], "little")
        data = handle.read(min(length, 10))
        if kind == b"VP8X" and len(data) >= 10:
            size = (1 + int.from_bytes(data[4:7], "little"), 1 + int.from_bytes(data[7:10], "little"))
        elif kind == b"VP8 " and len(data) >= 10 and size is None:
            size = (int.from_bytes(data[6:8], "little") & 0x3FFF,
                    int.from_bytes(data[8:10], "little") & 0x3FFF)
        elif kind == b"VP8L" and len(data) >= 5 and size is None:
            bits = int.from_bytes(data[1:5], "little")
            size = (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
        elif kind == b"ANMF":
            frames += 1
            if size and size[0] * size[1] * frames > MAX_ICON_PIXELS:
                break
        handle.seek(length + (length & 1) - len(data), 1)
    return (size[0], size[1], max(frames, 1)) if size else None


def icon_bytes(path):
    """The bytes of an icon file that is safe to decode, else None.

    The file is read once with a byte cap, and the size check runs on those
    same bytes, so nothing can swap the file between check and decode. SVG
    is vector data the caller renders straight at icon size; raster formats
    must declare at most MAX_ICON_SIDE per side and MAX_ICON_PIXELS in total
    (all frames). Other formats (XPM, ICO, ...) are not loaded from files."""
    path = Path(path)
    is_svg = path.suffix.lower() in (".svg", ".svgz")
    cap = MAX_SVG_ICON_BYTES if is_svg else MAX_ICON_FILE_BYTES
    try:
        with open(path, "rb") as handle:
            data = handle.read(cap + 1)
    except OSError:
        return None
    if not data or len(data) > cap:
        return None
    if is_svg:
        return data
    import io
    try:
        size = _image_header_size(io.BytesIO(data))
    except (ValueError, IndexError):
        return None
    if size is None:
        return None
    width, height, frames = size
    if not width or not height or width > MAX_ICON_SIDE or height > MAX_ICON_SIDE \
            or width * height * frames > MAX_ICON_PIXELS:
        return None
    return data



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
        "name": "App Manager",
        "plugin_id": SELF_PLUGIN_ID,
        "self_launcher": True,
        "exec": str(Path(__file__).resolve().parent / "launcher-toggle"),
        "icon": "view-app-grid-symbolic",
        "desktop_path": "",
        # Each list can have its own shortcut: `launcher-toggle apps|plugins`.
        "actions": [{"id": "apps", "name": _("Show apps"), "exec": ""},
                    {"id": "plugins", "name": _("Show widgets & plugins"), "exec": ""}],
        "description": _("This launcher — set the keys that open it"),
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
            "plugin_dir": str(directory),
            # Suggests the plugin's group (System, Network, …) in the group view.
            "category": str((manifest.get("barWidget") or {}).get("category") or manifest.get("category") or "")[:40]
                        if isinstance(manifest.get("barWidget") or {}, dict) else "",
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
        return _("Only the link is removed; the folder it points to stays."), False
    if not (target / ".git").is_dir():
        return _("The folder is kept as a backup in ~/.config/omarchy/plugins."), False
    lost = []
    changed = _git_count(target, "status", "--porcelain")
    if changed is None:
        lost.append(_("its git state could not be checked"))
    elif changed.strip():
        count = len(changed.strip().splitlines())
        lost.append(ngettext("{0} changed file", "{0} changed files", count).format(count))
    ahead = _git_count(target, "rev-list", "--count", "@{u}..HEAD")
    if ahead is None:
        lost.append(_("commits that exist nowhere else (no upstream)"))
    elif ahead.strip() not in ("", "0"):
        lost.append(ngettext("{0} unpushed commit", "{0} unpushed commits", int(ahead.strip())).format(ahead.strip()))
    if lost:
        return _("The folder is deleted, including {0}.").format(_(" and ").join(lost)), True
    return _("The folder is deleted; it can be installed again from the marketplace."), False


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
    name ("claude"); None for anything else. Remembered in the entry: the
    run-state checks ask for it thousands of times per list."""
    command = app.get("exec", "")
    cached = app.get("_terminal_command")
    if cached is not None and cached[0] == command:
        return cached[1]
    result = _terminal_command(command)
    app["_terminal_command"] = (command, result)
    return result


def _terminal_command(command):
    try:
        args = shlex.split(command)
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


# What a root process needs to open a window on this session. WAYLAND_DISPLAY
# becomes an absolute socket path, so the user's XDG_RUNTIME_DIR is not handed
# to root (root-owned files there would break the user's own session).
# XDG_SESSION_TYPE makes toolkits pick Wayland: root has no access to the
# session's Xwayland, so an app that falls back to X11 cannot open a window.
ADMIN_DISPLAY_VARS = ("XDG_SESSION_TYPE", "DISPLAY", "XAUTHORITY")
# How the user's apps look: scale, cursor and toolkit themes. Root has none of
# the user's settings, so without these its windows come up tiny and light.
ADMIN_LOOK_VARS = ("GDK_SCALE", "GDK_DPI_SCALE", "QT_SCALE_FACTOR", "QT_QPA_PLATFORMTHEME",
                   "QT_STYLE_OVERRIDE", "GTK_THEME", "XCURSOR_THEME", "XCURSOR_SIZE")
# Launchers that only make sense for the user: web apps, Omarchy's own wrappers
# and app sandboxes, which do not run as root.
ADMIN_REFUSED_PROGRAMS = {"flatpak", "snap", "uwsm", "uwsm-app", "xdg-open", "gtk-launch"}


def _admin_command(app):
    """The app's own argv, or None when it has nothing that can run as root."""
    if "tray_id" in app or "plugin_id" in app or app.get("self_launcher"):
        return None
    if "argv" in app:
        return list(app["argv"])  # Standalone AppImage: an executable path.
    # browser_web_app() is not used here: it also matches foot's --app-id=.
    if not app.get("desktop_path") or app.get("sandboxed") or app.get("pwa"):
        return None
    try:
        argv = shlex.split(app.get("exec", ""))
    except ValueError:
        return None
    if not argv:
        return None
    program = Path(argv[0]).name
    if program in ADMIN_REFUSED_PROGRAMS or program.startswith("omarchy-") or argv[0].startswith("/snap/"):
        return None
    if program in WEB_APP_BROWSERS and any(a.startswith(("--app=", "--app-id=")) for a in argv):
        return None  # A browser opened as a web app window.
    return argv


# Terminals that run a command given after -e; with nothing after it they
# open a root shell instead.
ADMIN_TERMINALS = {"foot", "footclient", "alacritty", "ghostty", "xterm", "xdg-terminal-exec"}


def _uses_electron(program):
    """Electron and Chromium refuse to start as root without --no-sandbox.
    Spot them by the files Chromium ships next to its binary, or by a small
    launcher script that starts electron."""
    if Path(program).name in WEB_APP_BROWSERS:
        return True
    try:
        path = Path(program).resolve()
        folder = path.parent
        if (folder / "chrome-sandbox").exists() or (folder / "resources" / "app.asar").exists():
            return True
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"electron" in head.lower()


# Apps that refuse to run as root but open root's files through GVfs's admin
# backend themselves: they stay the user's app and polkit asks on first access.
ADMIN_URI_APPS = {"nautilus": "admin:///"}
GVFS_ADMIN_BACKENDS = (Path("/usr/lib/gvfsd-admin"), Path("/usr/lib/gvfs/gvfsd-admin"),
                       Path("/usr/libexec/gvfsd-admin"))


def _gsetting(key):
    """A string from the user's GNOME interface settings, or ""."""
    try:
        output = read_bounded_output(["gsettings", "get", "org.gnome.desktop.interface", key],
                                     max_bytes=4096, timeout=2)
    except OSError:
        return ""
    value = (output or "").strip().strip("'")
    return value if re.fullmatch(r"[\w.+-]{1,64}", value) else ""


def _desktop_look():
    """Theme variables for a root app: the user's GTK theme, and dark mode for
    GTK, libadwaita and Electron when the user prefers it."""
    env = {}
    dark = _gsetting("color-scheme") == "prefer-dark"
    if not os.environ.get("GTK_THEME"):
        theme = _gsetting("gtk-theme")
        if theme:
            env["GTK_THEME"] = f"{theme}:dark" if dark and "dark" not in theme.lower() else theme
    if dark:
        env["ADW_DEBUG_COLOR_SCHEME"] = "prefer-dark"
    return env


def _display_scale():
    """The session's scale for apps that do not scale themselves (X11)."""
    value = os.environ.get("GDK_SCALE", "")
    if not value:
        try:
            output = read_bounded_output(["hyprctl", "monitors", "-j"], max_bytes=256 * 1024, timeout=2)
            value = str(next((m.get("scale", 1) for m in json.loads(output or "[]")
                              if m.get("focused")), 1))
        except (OSError, ValueError, AttributeError):
            return None
    try:
        scale = float(value)
    except ValueError:
        return None
    return f"{scale:g}" if 0.5 <= scale <= 4 and scale != 1 else None


def admin_command(app):
    """The argv that opens app as root, or None when it cannot run as root.

    Graphical apps go through pkexec, which asks for the password with the
    session's polkit agent. Terminal apps run sudo inside a terminal instead,
    where sudo asks itself. Nothing is passed through a shell."""
    if os.geteuid() == 0:
        return None
    argv = _admin_command(app)
    if argv is None:
        return None

    # A terminal launcher (foot -e claude): sudo goes in front of the command,
    # named by its full path because sudo uses root's PATH. A bare terminal
    # opens a root login shell.
    if Path(argv[0]).name.casefold() in TERMINALS | ADMIN_TERMINALS:
        if Path(argv[0]).name.casefold() not in ADMIN_TERMINALS or not shutil.which("sudo"):
            return None
        if "-e" not in argv:
            return argv + ["-e", "sudo", "-i"]
        at = argv.index("-e") + 1
        if at >= len(argv):
            return None
        argv[at] = shutil.which(argv[at]) or argv[at]
        return argv[:at] + ["sudo", "--"] + argv[at:]
    if app.get("terminal"):
        if not (shutil.which("xdg-terminal-exec") and shutil.which("sudo")):
            return None
        return ["xdg-terminal-exec", "sudo", "--", shutil.which(argv[0]) or argv[0]] + argv[1:]

    program = shutil.which(argv[0])
    if program and Path(program).name in ADMIN_URI_APPS:
        if not any(backend.exists() for backend in GVFS_ADMIN_BACKENDS):
            return None
        return [program, "--new-window", ADMIN_URI_APPS[Path(program).name]]
    if not program or not shutil.which("pkexec"):
        return None
    argv[0] = program  # pkexec runs with root's PATH.
    env = []
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if wayland and not wayland.startswith("/") and runtime:
        wayland = str(Path(runtime) / wayland)
    if wayland.startswith("/"):
        env.append(f"WAYLAND_DISPLAY={wayland}")
    if _uses_electron(program):
        if "--no-sandbox" not in argv:
            argv.append("--no-sandbox")
        # As root some Electron apps switch to in-process GPU (Kudu), which shows
        # no window on Wayland; on Xwayland it does. Root may use the display
        # once launch_as_admin lets it in. Without libX11, Wayland is the
        # only way, and only as a hint: a forced --ozone-platform=wayland keeps
        # Kudu from showing a window.
        if not any(a.startswith("--ozone-platform") for a in argv):
            if can_admit_root_on_display():
                argv.append("--ozone-platform=x11")
                # Hyprland shows X11 windows unscaled (force_zero_scaling).
                scale = _display_scale()
                if scale and not any(a.startswith("--force-device-scale-factor") for a in argv):
                    argv.append(f"--force-device-scale-factor={scale}")
            elif wayland.startswith("/"):
                argv.append("--ozone-platform-hint=auto")
    for name in ADMIN_DISPLAY_VARS:
        if os.environ.get(name):
            env.append(f"{name}={os.environ[name]}")
    if not env:
        return None  # No display to open a window on.
    look = {name: os.environ[name] for name in ADMIN_LOOK_VARS if os.environ.get(name)}
    look.update(_desktop_look())
    env += [f"{name}={value}" for name, value in look.items()]
    return ["pkexec", "env"] + env + argv


class _XHostAddress(ctypes.Structure):
    _fields_ = [("family", ctypes.c_int), ("length", ctypes.c_int), ("address", ctypes.c_void_p)]


class _XServerInterpretedAddress(ctypes.Structure):
    _fields_ = [("typelength", ctypes.c_int), ("valuelength", ctypes.c_int),
                ("type", ctypes.c_char_p), ("value", ctypes.c_char_p)]


_X_FAMILY_SERVER_INTERPRETED = 5
_X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_x_errors = []
# Xlib's default handler ends the process on any error; this one only counts.
_ignore_x_error = _X_ERROR_HANDLER(lambda _display, _event: _x_errors.append(1) or 0)


def _libx11():
    """libX11 (every Omarchy install has it), or None."""
    try:
        lib = ctypes.CDLL("libX11.so.6")
    except OSError:
        return None
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XAddHost.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XHostAddress)]
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XSetErrorHandler.argtypes = [_X_ERROR_HANDLER]
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    return lib


def can_admit_root_on_display():
    return bool(os.environ.get("DISPLAY")) and _libx11() is not None


def allow_root_on_display():
    """Let root, and only root, open windows on this session's X display:
    Xwayland admits only the session user. The same as
    `xhost +si:localuser:root`, without needing xhost; it lasts until logout."""
    lib, name = _libx11(), os.environ.get("DISPLAY", "")
    if lib is None or not name:
        return False
    lib.XSetErrorHandler(_ignore_x_error)
    display = lib.XOpenDisplay(name.encode())
    if not display:
        return False
    try:
        root = _XServerInterpretedAddress(len(b"localuser"), len(b"root"), b"localuser", b"root")
        host = _XHostAddress(_X_FAMILY_SERVER_INTERPRETED, ctypes.sizeof(root),
                             ctypes.cast(ctypes.pointer(root), ctypes.c_void_p))
        errors = len(_x_errors)
        lib.XAddHost(display, ctypes.byref(host))
        lib.XSync(display, 0)
        return len(_x_errors) == errors
    finally:
        lib.XCloseDisplay(display)


def launch_as_admin(app):
    """Start app as root. Returns the process, or None when it cannot start."""
    argv = admin_command(app)
    if argv is None:
        return None
    if "--ozone-platform=x11" in argv:
        allow_root_on_display()
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, env=_clean_env())
    except OSError:
        return None


# --- bounded paste --------------------------------------------------------------

MAX_PASTE_TEXT_BYTES = 4096
_TEXT_MIMES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")


def clipboard_text(max_chars=200, max_bytes=MAX_PASTE_TEXT_BYTES, timeout=2.0):
    """Plain text from the Wayland clipboard for a text field, read with
    wl-paste under a byte cap and a deadline (GTK's own paste buffers the
    whole offer with neither). Control characters and line breaks become
    single spaces. "" if there is no text (an empty clipboard makes
    wl-paste fail); None if the text was too long, too slow or failed."""
    if not shutil.which("wl-paste"):
        return ""
    types = read_bounded_output(["wl-paste", "--list-types"], max_bytes=65536, timeout=timeout)
    offered = {line.strip() for line in (types or "").splitlines()}
    for mime in _TEXT_MIMES:
        if mime not in offered:
            continue
        data = read_bounded_output(["wl-paste", "--no-newline", "--type", mime],
                                   max_bytes=max_bytes, timeout=timeout)
        if data is None:
            return None
        text = "".join(" " if ord(ch) < 32 or 127 <= ord(ch) < 160 else ch for ch in data)
        return " ".join(text.split())[:max_chars]
    return ""
