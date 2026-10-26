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
from pathlib import Path
import gi
from i18n import _
import cli_tools
import corners

gi.require_version('GioUnix', '2.0')
from gi.repository import GioUnix, GLib

CONFIG_DIR = Path.home() / ".config/gnome-launcher"
HIDDEN_FILE = CONFIG_DIR / "hidden.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"


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
    _save_settings({key: value}, path)


def _save_settings(values, path=None):
    """Set several keys in settings.json in one write (atomic, private)."""
    path = Path(path or SETTINGS_FILE)
    data = _load_settings(path)
    data.update(values)
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
    """The narrow window's layout: "list" (the default), "groups" or "origin"
    (settings, header buttons, Ctrl+G)."""
    view = _load_settings(path).get("view")
    return view if view in ("groups", "origin") else "list"


def set_view(view, path=None):
    _save_setting("view", view if view in ("groups", "origin") else "list", path)


def saved_wide_layout(path=None):
    """The wide window (overview): "folders" (each group as a folder of
    icons, like a start menu; the default) or "cards" (every entry as a card)."""
    return "cards" if _load_settings(path).get("wide_layout") == "cards" else "folders"


def set_wide_layout(layout, path=None):
    _save_setting("wide_layout", "folders" if layout == "folders" else "cards", path)


def saved_corners(path=None):
    """"windows" (the window's own rounding; the default), "rounded" or "square"."""
    return corners.choice(_load_settings(path).get("corners"))


def set_corners(value, path=None):
    _save_setting("corners", corners.choice(value), path)


APP_ICON_SIZES = (32, 40, 44, 48, 56, 64)   # Settings › App icon size, as the dock's Icon size
DEFAULT_APP_ICON_SIZE = 44


def saved_app_icon_size(path=None):
    """The app icons' size on the cards in px (the list's are 12 px smaller)."""
    value = _load_settings(path).get("app_icon_size")
    return value if value in APP_ICON_SIZES and not isinstance(value, bool) else DEFAULT_APP_ICON_SIZE


def set_app_icon_size(value, path=None):
    _save_setting("app_icon_size", value if value in APP_ICON_SIZES else DEFAULT_APP_ICON_SIZE, path)


def view_buttons_shown(path=None):
    """The list | groups | origin buttons in the narrow window's header (a
    setting, off by default: the plain list)."""
    return _load_settings(path).get("view_buttons") is True


def set_view_buttons_shown(shown, path=None):
    _save_setting("view_buttons", bool(shown), path)


def filter_field_shown(path=None):
    """The filter field above the folders (a setting, on by default; its ×
    turns it off). Without it, typing still filters, shown in the title."""
    return _load_settings(path).get("filter_field") is not False


def set_filter_field_shown(shown, path=None):
    _save_setting("filter_field", bool(shown), path)


def saved_size(key, path=None):
    """(width, height) the window was left at ("window"); None for the default."""
    value = _load_settings(path).get(key)
    if (isinstance(value, list) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 20000 for v in value)):
        return value[0], value[1]
    return None


def set_saved_size(key, size, path=None):
    _save_setting(key, [int(size[0]), int(size[1])] if size else None, path)


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


# env's options: those without a value, and those that take the next word. -C/--chdir
# is not among them: its program would be found relative to another folder, so env
# itself is checked, as for -S.
_ENV_FLAGS = {"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"}
_ENV_WITH_VALUE = {"-u", "--unset"}
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


def exec_program(command):
    """The program a desktop entry's Exec line starts (after an env prefix
    with its options and VAR=value words), or None, also for env forms it
    does not know (-S, other options)."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    # An index, not list slices: thousands of VAR=value words stay linear.
    i = 0
    if parts and Path(parts[0]).name == "env":
        i = 1
        while i < len(parts):
            word = parts[i]
            if word in _ENV_FLAGS or word.startswith("--unset=") or _ENV_ASSIGNMENT.fullmatch(word):
                i += 1
            elif word in _ENV_WITH_VALUE:
                i += 2
            elif word == "--":
                i += 1
                while i < len(parts) and _ENV_ASSIGNMENT.fullmatch(parts[i]):
                    i += 1
                break
            elif word.startswith("-"):
                return None
            else:
                break
    return parts[i] if i < len(parts) else None


def env_changes_path(command):
    """An env prefix sets or clears PATH (PATH=…, -i, -u PATH): a bare program
    name is then looked up in a PATH this process does not have."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts or Path(parts[0]).name != "env":
        return False
    words = iter(parts[1:])
    for word in words:
        if word.startswith("PATH=") or word in ("-i", "--ignore-environment", "--unset=PATH"):
            return True
        if word in _ENV_WITH_VALUE:
            if next(words, "") == "PATH":
                return True
        elif not (word in _ENV_FLAGS or word == "--" or word.startswith("--unset=")
                  or _ENV_ASSIGNMENT.fullmatch(word)):
            return False            # the program (or a form exec_program does not know)
    return False


def executable_exists(command):
    """Check the program named by a desktop entry without running it. An
    env form exec_program does not know, or one that changes PATH for a bare
    program name: env itself is checked."""
    program = exec_program(command)
    if program and "/" not in program and env_changes_path(command):
        program = None
    if not program:
        try:
            parts = shlex.split(command)
        except ValueError:
            return False
        if not parts or Path(parts[0]).name != "env" or len(parts) < 2:
            return False
        program = parts[0]
    if "\0" in program:
        return False  # os.access and shutil.which raise on a NUL; such an entry cannot run
    return os.access(program, os.X_OK) if "/" in program else shutil.which(program) is not None


# A desktop entry is a few KiB; anything far larger is not one.
MAX_DESKTOP_FILE_BYTES = 256 * 1024


def read_desktop_file(path, cap=MAX_DESKTOP_FILE_BYTES):
    """The text of a desktop entry, or None when it is not a regular file or
    larger than cap. O_NONBLOCK: a FIFO (or a symlink to one) in an
    applications folder can neither block the open nor the read; the size is
    checked on the opened descriptor and the read itself stops after cap + 1
    bytes, so an endless source cannot exhaust memory."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0))
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > cap:
        return None
    return data.decode("utf-8", errors="ignore")


# (desktop id, Exec line) of every desktop entry the last scan read, before
# any filtering; quit_target checks them for a shared executable.
ENTRY_EXECS = []


def parse_desktop_files():
    global ENTRY_EXECS
    apps, seen_desktop_ids, seen_names = [], set(), set()
    entry_execs = []
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
            text = read_desktop_file(path)
            if text is None:
                continue
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("["):
                    section = line.strip("[]")
                elif section == "Desktop Entry" and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
                elif (section or "").startswith("Desktop Action ") and line.startswith("Exec="):
                    action_execs[section.removeprefix("Desktop Action ")] = line[5:]
            # Every entry counts for Quit's "executable shared by another entry"
            # check, also those hidden (NoDisplay), deduplicated or failing TryExec.
            for line in [values.get("Exec")] + list(action_execs.values()):
                if line:
                    entry_execs.append((path.stem, line))
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
                # From the bytes read above: the file is not opened a second time.
                keyfile = GLib.KeyFile()
                keyfile.load_from_data(text, len(text.encode("utf-8")), GLib.KeyFileFlags.KEEP_TRANSLATIONS)
                info = GioUnix.DesktopAppInfo.new_from_keyfile(keyfile)
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

    # An AppImage a .desktop file already starts (an installer wrote one) is that app.
    started = set()
    for app in apps:
        program = exec_program(app.get("exec", ""))
        if program and program.casefold().endswith(".appimage"):
            started.add(program)
    apps.extend(scan_standalone_appimages(seen_names, started))
    # CLI agents installed with mise (Codex, Copilot, …) that no .desktop file opens.
    known = set()
    for app in apps:
        try:
            known.update(Path(arg).name for arg in shlex.split(app.get("exec", "")))
        except ValueError:
            pass
    apps.extend(e for e in cli_tools.cli_entries(known) if e["name"].strip().casefold() not in seen_names)
    ENTRY_EXECS = entry_execs
    return sorted(apps, key=lambda app: app["name"].lower())


def get_appimage_directories():
    candidates = [
        Path.home() / "AppImages",
        Path.home() / "Applications",
        Path.home() / ".local/bin",
        Path.home() / "bin",
    ]
    return [d for d in candidates if d.is_dir()]


def read_bounded_output(command, max_bytes=64 * 1024, timeout=1, binary=False, ok_codes=(0,), env=None):
    """Read optional archive metadata with a byte limit and a total deadline."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env) as process:
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
            if remaining <= 0 or process.wait(timeout=remaining) not in ok_codes:
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
    # O_NONBLOCK: a FIFO swapped in for the file (a tray app names its own icon path) can
    # neither block the open nor the read; only a regular file is read at all.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
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


# File name words that are not part of an app's name: version, system, CPU.
_APPIMAGE_NOISE = {"linux", "gnu", "appimage", "x86", "x64", "x86-64", "amd64", "i386", "i686",
                   "aarch64", "arm64", "armhf", "armv7l", "universal", "portable"}


# A version word: "1.0", "v2", "0.14.0", "1.0rc1", "1.0+build5", or plain digits.
_VERSION_WORD = re.compile(r"v?\d+(?:\.\d+)*(?:[.+~]?[a-z][\w.+~]*)?|v?\d+(?:\.\d+)+[\w.+~]*", re.IGNORECASE | re.ASCII)
_RELEASE = re.compile(r"v?(\d+(?:\.\d+)*)(.*)", re.IGNORECASE | re.ASCII)
_PRERELEASE = re.compile(r"[.~-]?(alpha|beta|preview|pre|rc|dev|nightly|a|b)\.?(\d*)", re.IGNORECASE | re.ASCII)
# CPU names hold digits too (x86_64): never part of the name or the version.
_CPU = re.compile(r"(?<![^-_ .])(?:x86[-_]64|aarch64|arm64|amd64|armv7l|i[36]86)(?=$|[-_ .])", re.IGNORECASE)
# Prerelease stages, earliest first; a final release is above them all.
_STAGES = {"dev": 0, "nightly": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "pre": 3, "preview": 3, "rc": 4}


def _is_version(word):
    """A version word; one that only starts with a digit (3D, 2048game) is a name."""
    if not _VERSION_WORD.fullmatch(word):
        return False
    release = _RELEASE.fullmatch(word)
    tail = release.group(2)
    # "3D": digits then letters with no dot and no prerelease tag is a name.
    return not tail or "." in release.group(1) or bool(_PRERELEASE.fullmatch(tail)) or tail[0] in ".+~" \
        or word[0] in "vV"


def _appimage_words(stem):
    """(name words, version words) of an AppImage file name: the name ends
    at the first version word after the first word."""
    words = [w for w in re.split(r"[-_ ]+", _CPU.sub("", stem)) if w]
    cut = next((i for i, w in enumerate(words) if i and _is_version(w)), len(words))
    name = [w for i, w in enumerate(words[:cut]) if not (i and w.casefold() in _APPIMAGE_NOISE)]
    return name, words[cut:]


def appimage_base_name(stem):
    """The app's name in an AppImage file name: "photocraft-0.14.0-linux-x86_64"
    → "photocraft", "Asterism-0.7.0-x86_64" → "Asterism", "Foo-3D-1.0" → "Foo 3D"."""
    return " ".join(_appimage_words(stem)[0]) or stem


def _name_key(name):
    return re.sub(r"[-_ ]", "", name.strip().casefold())


def _version_key(stem):
    """Comparable version of an AppImage file name, larger is newer: the
    release numbers, then final above prerelease (1.0 > 1.0rc2 > 1.0rc1);
    build metadata (+build5) does not count."""
    release, pre = [], None
    words = []
    for word in _appimage_words(stem)[1]:
        # Build metadata runs from the first "+" to the end, hyphens included (1.0+build-999).
        word, plus, _ = word.partition("+")
        words.append(word)
        if plus:
            break
    for word in words:
        tag = _PRERELEASE.fullmatch(word)
        if tag:
            pre = pre or (_STAGES[tag.group(1).casefold()], int(tag.group(2) or 0))
            continue
        match = _RELEASE.fullmatch(word)
        if not match:
            continue
        release.extend(int(n) for n in match.group(1).split("."))
        tail = _PRERELEASE.fullmatch(match.group(2)) if match.group(2) else None
        if tail:
            pre = pre or (_STAGES[tail.group(1).casefold()], int(tail.group(2) or 0))
    # Trailing zeros do not matter: 1.0 is 1.0.0.
    while release and release[-1] == 0:
        release.pop()
    return (tuple(release), 0 if pre else 1, pre or ())


def _sorted_newest_first(paths):
    """One app's versions together, the newest first (0.14.0 before 0.9.0, v2.0 before 1.0)."""
    paths = sorted(paths, key=lambda p: p.name)
    paths.sort(key=lambda p: _version_key(p.stem), reverse=True)
    paths.sort(key=lambda p: _name_key(appimage_base_name(p.stem)))
    return paths


def scan_standalone_appimages(seen_names, started=()):
    apps = []
    icon_cache_dir = Path.home() / ".cache/gnome-launcher/icons"
    has_7z = shutil.which("7z") is not None
    started = {str(Path(p)) for p in started}
    # A .desktop file starting one version of an app stands for its other versions too.
    taken = {_name_key(appimage_base_name(Path(p).stem)) for p in started}

    # All folders at once: a newer version in another folder still wins.
    found = [path for directory in get_appimage_directories()
             for path in directory.glob("*.[aA][pP][pP][iI][mM][aA][gG][eE]")]
    for path in _sorted_newest_first(found):
        # Discovery must respect user permissions, never make files executable.
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        identity = _name_key(appimage_base_name(path.stem))
        if str(path) in started or identity in taken:
            continue
        # The newest version stands for the older ones, whatever names they carry inside.
        taken.add(identity)

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
            app_name = appimage_base_name(path.stem)

        name_key = app_name.strip().casefold()
        # If the app name is already present from a desktop file, skip
        if name_key in seen_names:
            continue
        normalized = _name_key(name_key)
        if any(normalized == _name_key(s) for s in seen_names):
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
    if not args or Path(args[0]).name.casefold() not in TERMINALS | {"xdg-terminal-exec"}:
        return None
    for flag in ("-e", "--command", "--"):
        if flag in args:
            rest = args[args.index(flag) + 1:]
            return Path(rest[0]).name.casefold() if rest else None
    return None


TERMINALS = {"foot", "footclient", "alacritty", "kitty", "ghostty", "wezterm", "xterm",
             "ptyxis", "kgx", "gnome-terminal", "gnome-terminal-server", "konsole"}


def app_process_name(app):
    try:
        args = shlex.split(app["exec"])
    except ValueError:
        return None
    i = 0
    if args and Path(args[0]).name == "env":
        i = 1
        while i < len(args) and "=" in args[i] and not args[i].startswith("-"):
            i += 1
    if i >= len(args):
        return None
    name = Path(args[i]).name.casefold()
    # Shared interpreters/wrappers cannot identify an individual application.
    if name in {"sh", "bash", "env", "flatpak", "gtk-launch", "python", "python3", "java"}:
        return None
    # A terminal running a command (foot -e claude) is the terminal's process,
    # often a shared server; such apps are found by their command instead.
    if name == "xdg-terminal-exec" or (name in TERMINALS and ("-e" in args or "--command" in args)):
        return None
    return name


def process_pids(name):
    """PIDs of the user's processes whose executable is called name (only for
    the running state; Quit uses quit_target and executable_processes)."""
    pids = set()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid == os.getuid() and Path(os.readlink(process / "exe")).name.casefold() == name:
                pids.add(int(process.name))
        except (OSError, ValueError):
            continue
    return pids


# Programs that run other code (a script, a jar, a web app) or start other
# programs: their executable says nothing about which app a process is, so Quit
# never ends processes by them. Version suffixes count too (python3.14, node22).
# Version-manager shims (mise, asdf, …) are symlinks to the manager itself.
_SHARED_PROGRAM_RE = re.compile(
    r"(?:python|pypy|perl|ruby|php|lua|luajit|node|nodejs|deno|bun|java|electron|wine|wine64|mono"
    r"|dotnet|gjs|qmlscene|tclsh|wish|rscript|guile|racket|sbcl|erl|elixir|julia)[0-9._-]*"
    r"|(?:ba|da|z|k|c|tc|mk|lk|a)?sh|fish|nu|xonsh|elvish|busybox|toybox|env|flatpak|snap|gtk-launch"
    r"|gio|xdg-open|xdg-terminal-exec|uwsm|uwsm-app|app2unit|systemd-run|sudo|pkexec|doas|run0|nohup"
    r"|setsid|stdbuf|nice|ionice|taskset|chrt|timeout|appimage-run|bwrap|firejail|distrobox|toolbox"
    r"|nix|nix-shell|steam|exec|mise|asdf|volta|corepack|npx|npm|pnpm|yarn|uv|uvx|pipx", re.IGNORECASE)


def _program_file(app):
    """The canonical path of the executable an entry starts, or None."""
    command = app.get("exec", "") if isinstance(app.get("exec"), str) else ""
    program = exec_program(command)
    if not program or "\0" in program:
        return None
    if "/" not in program:
        if env_changes_path(command):
            return None
        program = shutil.which(program)
        if not program:
            return None
    # Only absolute paths: a relative one (./foo, or a relative PATH entry)
    # would be resolved against this process's folder, not the entry's Path=.
    if not os.path.isabs(program):
        return None
    names = {Path(program).name.casefold()}
    try:
        path = os.path.realpath(program)
        if not os.path.isabs(path) or not stat.S_ISREG(os.stat(path).st_mode) or not os.access(path, os.X_OK):
            return None
    except (OSError, ValueError):
        return None
    names.add(Path(path).name.casefold())
    if any(_SHARED_PROGRAM_RE.fullmatch(n) or n in TERMINALS for n in names):
        return None
    return path


def _resolve_word(word):
    """The canonical file a word of an Exec line names as a program, or None."""
    try:
        if "/" not in word:
            word = shutil.which(word) or ""
        return os.path.realpath(word) if os.path.isabs(word) else None
    except (OSError, ValueError):
        return None


def _may_start(line, names, path, cache):
    """Whether an Exec line may start the executable path (called one of
    names), by any word of it: also after env options, inside sh -c '…' or
    env -S '…', or as --opt=value; by name, or by another name that resolves
    to path (a symlink alias). Errs towards yes; it only takes Quit away."""
    if not isinstance(line, str):
        return False
    for piece in re.split(r"[\s=:;'\"`|&()<>]+", line):
        if not piece:
            continue
        if Path(piece).name.casefold() in names:
            return True
        if piece not in cache:
            cache[piece] = _resolve_word(piece)
        if cache[piece] == path:
            return True
    return False


def quit_target(app, apps=(), entry_execs=None):
    """The canonical executable that identifies app's own processes, for
    Quit, or None when the app cannot be told apart from other programs: it
    runs through an interpreter or wrapper (python3.14 app.py, env, flatpak),
    is a terminal itself, a web app of a browser, from a relative path, or
    another entry may start the same executable (libreoffice --writer /
    --calc): another listed app, or any desktop entry (or action of one)
    the last scan read, hidden ones included (entry_execs, default
    ENTRY_EXECS). That check goes by any word of the other Exec line that has
    the program's name or resolves to the same file (a symlink alias), so
    wrapper forms it cannot parse count as sharing."""
    if app.get("pwa") or app.get("cli_command"):
        return None
    probe = app
    if terminal_command(app):
        # A command-line app in a terminal (xdg-terminal-exec -e claude) runs
        # as its command: that command's executable is the target.
        try:
            args = shlex.split(app.get("exec", ""))
        except ValueError:
            return None
        flag = next((f for f in ("-e", "--command", "--") if f in args), None)
        rest = args[args.index(flag) + 1:] if flag else []
        if not rest:
            return None
        probe = {"exec": shlex.join(rest)}
    elif browser_web_app(app):
        return None
    path = _program_file(probe)
    if path is None:
        return None
    names = {Path(path).name.casefold(), Path(exec_program(probe.get("exec", "")) or path).name.casefold()}
    own_id, cache = app.get("desktop_id"), {}
    for other in apps:
        if other is app or (own_id and other.get("desktop_id") == own_id):
            continue
        if _may_start(other.get("exec"), names, path, cache):
            return None
    for desktop_id, line in ENTRY_EXECS if entry_execs is None else entry_execs:
        if own_id and desktop_id == own_id:
            continue
        if _may_start(line, names, path, cache):
            return None
    return path


def _process_start(pid):
    """A process's start time in clock ticks since boot (field 22 of
    /proc/PID/stat), which tells a process apart from a later one that got
    the same PID."""
    with open(f"/proc/{int(pid)}/stat", "rb") as handle:
        data = handle.read(4096)
    return int(data[data.rindex(b")") + 2:].split()[19])


def _is_target(pid, path):
    process = Path(f"/proc/{int(pid)}")
    return process.stat().st_uid == os.getuid() and os.readlink(process / "exe") == path


def executable_processes(path):
    """{PID: start time} of the user's processes whose executable is exactly
    path (/proc/PID/exe: the resolved file, not just its name; a binary
    replaced since by an update reads "… (deleted)" and does not count)."""
    found = {}
    if not path:
        return found
    for process in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(process.name)
            if _is_target(pid, path):
                found[pid] = _process_start(pid)
        except (OSError, ValueError, IndexError):
            continue
    return found


def end_processes(path, targets):
    """SIGTERM each process of targets ({PID: start time}, from
    executable_processes) that still is this user's process of executable
    path with the same start time. A pidfd is opened first and the process
    checked after that, so a PID reused meanwhile by another program gets
    nothing (the pidfd stays bound to the process it was opened for).
    Returns how many were signalled."""
    ended = 0
    for pid, start in dict(targets).items():
        try:
            fd = os.pidfd_open(pid)
        except (OSError, AttributeError, TypeError, ValueError):
            continue
        try:
            if _is_target(pid, path) and _process_start(pid) == start:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                ended += 1
        except (OSError, ValueError, IndexError):
            pass
        finally:
            os.close(fd)
    return ended


def background_process_names():
    """Names of the user's processes: the executable's, and the first two
    words of the command line, so scripts and node tools (node codex) count
    by their own name too."""
    names = set()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid != os.getuid():
                continue
            names.add(Path(os.readlink(process / "exe")).name.casefold())
            argv = (process / "cmdline").read_bytes()[:4096].split(b"\0")[:2]
            names.update(Path(a.decode(errors="replace")).name.casefold() for a in argv if a)
        except OSError:
            continue
    return names


def launch_app(app, context=None):
    """Open app; context: a Gio.AppLaunchContext (the display's), so GNOME
    gives the new window the focus instead of an "is ready" notice."""
    # Standalone AppImages and command-line tools are argv lists, never shell
    # programs. A failed direct launch must not fall through to another method.
    if "argv" in app:
        try:
            subprocess.Popen(app["argv"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except OSError:
            return False
    desktop_path = app.get("desktop_path", "")
    if desktop_path:
        try:
            info = GioUnix.DesktopAppInfo.new_from_filename(desktop_path)
            if info:
                return bool(info.launch_uris([], context))
        except Exception:
            pass
    # Exec is display/matching metadata, never a shell command.
    return False


_icon_files = {}


def icon_data_dirs(environ=None):
    """The XDG data directories, the user's first (as the icon spec)."""
    environ = os.environ if environ is None else environ
    home = environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    system = environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(home)] + [Path(d) for d in system.split(":") if d]


def icon_file_for(name, data_dirs=None):
    """The file of a hicolor or pixmaps icon called name, looked up on disk:
    for names the icon theme does not know because an icon-theme.cache is
    older than the icons added since (GTK then trusts the stale cache).
    Scalable first, then the largest size; None when there is none."""
    if not name or "/" in name or name.startswith("."):
        return None
    dirs = icon_data_dirs() if data_dirs is None else data_dirs
    key = (name, tuple(dirs))
    if key in _icon_files:
        return _icon_files[key]
    found = None
    for base in dirs:
        hicolor = base / "icons/hicolor"
        candidates = [hicolor / "scalable/apps" / f"{name}.svg"]
        try:
            sizes = [d.name for d in hicolor.iterdir() if re.fullmatch(r"\d+x\d+(@\d+)?", d.name)]
        except OSError:
            sizes = []
        sizes.sort(key=lambda d: int(d.split("x")[0]), reverse=True)
        candidates += [hicolor / size / "apps" / f"{name}.png" for size in sizes]
        candidates += [base / "pixmaps" / f"{name}.{ext}" for ext in ("svg", "png", "xpm")]
        found = next((c for c in candidates if c.is_file()), None)
        if found is not None:
            break
    _icon_files[key] = found
    return found


def launch_action(app, action_id, context=None):
    """Run one of the desktop entry's own actions (a private window, …)."""
    desktop_path = app.get("desktop_path", "")
    if desktop_path:
        try:
            info = GioUnix.DesktopAppInfo.new_from_filename(desktop_path)
            if info and action_id in info.list_actions():
                info.launch_action(action_id, context)
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
# Launchers that only make sense for the user: web apps and app sandboxes, which do not run as root.
ADMIN_REFUSED_PROGRAMS = {"flatpak", "snap", "xdg-open", "gtk-launch"}


def _admin_command(app):
    """The app's own argv, or None when it has nothing that can run as root."""
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
    if program in ADMIN_REFUSED_PROGRAMS or argv[0].startswith("/snap/"):
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
                # X11 windows unscaled by the compositor: the app scales itself.
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
    """libX11 (Xwayland brings it), or None."""
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
                                stderr=subprocess.DEVNULL, start_new_session=True)
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
