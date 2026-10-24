"""Facts for the details page: where an app lives, which package
it came from, how big it is and when it was installed; where it came from
(the "by origin" view) and how it is uninstalled.

Everything here only reads. Paths come from desktop files, so
they are resolved and checked before use; pacman gets absolute paths only;
every read is bounded.
"""
import os
import re
import shutil
import shlex
from pathlib import Path

from launcher_core import TERMINALS, read_bounded_output

PACMAN_DB = Path("/var/lib/pacman/local")
MAX_DESC_BYTES = 256 * 1024
MAX_TEXT = 300
_PKG_RE = re.compile(r"^[A-Za-z0-9@._+-]{1,128}$")


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def short(text, limit=MAX_TEXT):
    """One line of plain text: no control or direction characters, capped."""
    text = " ".join(_CONTROL_RE.sub(" ", str(text or "")).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


SHELLS = {"sh", "bash", "zsh", "fish", "dash"}
WEBAPP_LAUNCHERS = {"xdg-open"}


def command_argv(app):
    """The desktop entry's command as argv, without leading env VAR=x."""
    argv = app.get("argv")
    if argv:
        return list(argv)
    try:
        argv = shlex.split(app.get("exec") or "")
    except ValueError:
        return []
    if argv and Path(argv[0]).name == "env":
        argv = argv[1:]
        while argv and ("=" in argv[0] or argv[0].startswith("-")):
            argv = argv[1:]
    return argv


def web_address(app):
    """The address a web app opens (xdg-open URL), or None."""
    argv = command_argv(app)
    if len(argv) > 1 and Path(argv[0]).name in WEBAPP_LAUNCHERS and re.match(r"^https?://", argv[1]):
        return argv[1]
    return None


def terminal_program(argv):
    """For foot -e claude, xdg-terminal-exec -e btop: the program the terminal
    runs (not a shell running a command line), or None."""
    if not argv or Path(argv[0]).name.casefold() not in (*TERMINALS, "xdg-terminal-exec"):
        return None
    for flag in ("-e", "--command", "--"):
        if flag in argv:
            rest = argv[argv.index(flag) + 1:]
            if rest and Path(rest[0]).name not in SHELLS:
                return rest[0]
            return None
    return None


def program_path(app):
    """The program a desktop entry runs, as an absolute real path, or None.
    A terminal app is the program inside the terminal."""
    argv = command_argv(app)
    if not argv:
        return None
    program = terminal_program(argv) or argv[0]
    found = program if "/" in program else shutil.which(program)
    if not found:
        return None
    try:
        path = Path(found).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return str(path) if path.is_absolute() and path.is_file() else None


def parse_desc(text):
    """%NAME%\\nvalue\\n… blocks of a pacman local database 'desc' file."""
    fields, key = {}, None
    for line in text.splitlines():
        if line.startswith("%") and line.endswith("%") and len(line) > 2:
            key = line.strip("%")
            fields[key] = []
        elif key and line:
            fields[key].append(line)
        elif not line:
            key = None
    return {k: "\n".join(v) for k, v in fields.items()}


def package_owner(path):
    """The pacman package that owns an absolute path, or None."""
    if not path or not os.path.isabs(path) or not shutil.which("pacman"):
        return None
    output = read_bounded_output(["pacman", "-Qqo", path], max_bytes=4096, timeout=3)
    name = (output or "").strip().splitlines()
    name = name[0] if name else ""
    return name if _PKG_RE.match(name) else None


def package_info(name, db=PACMAN_DB):
    """Version, size, install date and URL from pacman's local database."""
    if not name or not _PKG_RE.match(name):
        return None
    try:
        candidates = [p for p in db.iterdir() if p.name.startswith(name + "-")]
    except OSError:
        return None
    for directory in candidates:
        try:
            with open(directory / "desc", "rb") as fh:
                fields = parse_desc(fh.read(MAX_DESC_BYTES).decode("utf-8", "replace"))
        except OSError:
            continue
        if fields.get("NAME") != name:
            continue
        info = {"name": name, "version": fields.get("VERSION", ""), "url": fields.get("URL", "")}
        for key, field in (("size", "SIZE"), ("installed", "INSTALLDATE"), ("built", "BUILDDATE")):
            try:
                info[key] = int(fields.get(field, ""))
            except ValueError:
                pass
        return info
    return None


def app_facts(app):
    """Facts of a desktop entry or AppImage as a dict (see the page for labels)."""
    address = web_address(app)
    argv = command_argv(app)
    facts = {"kind": "appimage" if app.get("argv") and not app.get("desktop_path") else
             "flatpak" if app.get("sandboxed") else "webapp" if app.get("pwa") or address else
             "terminal" if app.get("terminal") or terminal_program(argv) else "app",
             "id": app.get("desktop_id", ""), "desktop_file": app.get("desktop_path", ""),
             "command": short(app.get("exec", ""))}
    facts["roles"] = default_roles(app)
    if address:
        # The launcher script is the browser's; the app is the web address.
        facts["website"] = short(address)
        return facts
    program = program_path(app)
    facts["program"] = program
    owner = package_owner(program) or package_owner(app.get("desktop_path"))
    info = package_info(owner) if owner else None
    if info:
        facts["package"] = info
    elif program:
        try:
            stat = os.stat(program)
            facts["file_size"], facts["modified"] = stat.st_size, int(stat.st_mtime)
        except OSError:
            pass
    return facts


# --- Default apps ---------------------------------------------------------------

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
MAX_LIST_BYTES = 256 * 1024

# Roles a default app can have, by the MIME types or URL schemes that pick it.
ROLE_MIMES = (
    ("browser", ("x-scheme-handler/http", "x-scheme-handler/https", "text/html")),
    ("email", ("x-scheme-handler/mailto",)),
    ("files", ("inode/directory",)),
    ("editor", ("text/plain",)),
    ("images", ("image/png", "image/jpeg")),
    ("video", ("video/mp4", "video/x-matroska")),
    ("music", ("audio/mpeg", "audio/flac")),
    ("pdf", ("application/pdf",)),
)


def _read_lines(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_LIST_BYTES).decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _mime_defaults(config_home=CONFIG_HOME):
    """mime -> first default desktop id, from the user's mimeapps.list files
    (the first file that names a type wins, as in the XDG spec)."""
    found = {}
    for path in (config_home / "gnome-mimeapps.list", config_home / "mimeapps.list",
                 Path.home() / ".local/share/applications/mimeapps.list"):
        section = None
        for line in _read_lines(path):
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Default Applications]" and "=" in line:
                mime, ids = line.split("=", 1)
                first = next((i for i in ids.split(";") if i.strip()), "")
                found.setdefault(mime.strip(), first.strip())
    return found


def default_terminal(config_home=CONFIG_HOME):
    """The first terminal of xdg-terminal-exec's lists (GNOME's, then the
    general one), or None."""
    for path in (config_home / "gnome-xdg-terminals.list", config_home / "xdg-terminals.list"):
        for line in _read_lines(path):
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def default_roles(app, config_home=CONFIG_HOME):
    """Roles this app is the default app for: browser, email, terminal, …"""
    desktop = Path(app.get("desktop_path") or "").name
    if not desktop:
        return []
    defaults = _mime_defaults(config_home)
    roles = [role for role, mimes in ROLE_MIMES if any(defaults.get(m) == desktop for m in mimes)]
    if default_terminal(config_home) == desktop:
        roles.insert(1 if roles[:1] == ["browser"] else 0, "terminal")
    return roles


# --- Grouping by origin ---------------------------------------------------------

USER_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def default_desktops(config_home=CONFIG_HOME):
    """Desktop file names that are one of the user's default apps."""
    defaults = _mime_defaults(config_home)
    found = {defaults[m] for _role, mimes in ROLE_MIMES for m in mimes if defaults.get(m)}
    terminal = default_terminal(config_home)
    return found | {terminal} if terminal else found


def origin_section(app, defaults, user_data=USER_DATA):
    """"defaults" (one of the user's default apps), "flatpak" (Flatpak or
    Snap), "user" (your own: entries in your home, AppImages, web apps,
    command-line tools) or "system" (installed system-wide, by the package
    manager). A default app counts as a default: the more specific fact."""
    desktop_path = app.get("desktop_path") or ""
    desktop = Path(desktop_path).name
    if desktop and desktop in defaults:
        return "defaults"
    if app.get("sandboxed") or "/flatpak/" in desktop_path or "/snapd/" in desktop_path:
        return "flatpak"
    if not desktop_path or app.get("pwa") or web_address(app):
        return "user"
    try:
        if Path(desktop_path).resolve().is_relative_to(Path(user_data).resolve()):
            return "user"
    except (OSError, ValueError):
        pass
    return "system"


# --- Uninstalling ---------------------------------------------------------------

_FLATPAK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")


def uninstall_plan(app, user_data=USER_DATA, look_up_package=True):
    """How app is uninstalled, or None when the launcher cannot:
    ("webapp", browser): only its browser can (a note, no button);
    ("flatpak", app id): `flatpak uninstall`;
    ("package", name): pacman, in a terminal (it asks for the password);
    ("trash", path): your own desktop entry or AppImage, to the trash.
    look_up_package=False skips asking pacman (fast, for a menu): a
    system-wide entry is then ("package", None)."""
    from launcher_core import browser_web_app
    if app.get("cli_command"):
        return None  # Installed with mise or npm: removed there.
    browser = browser_web_app(app)
    if browser:
        return "webapp", browser
    desktop_path = app.get("desktop_path") or ""
    if app.get("sandboxed") and "/flatpak/" in desktop_path:
        app_id = Path(desktop_path).name.removesuffix(".desktop")
        return ("flatpak", app_id) if _FLATPAK_ID_RE.match(app_id) else None
    if not desktop_path:
        argv = app.get("argv") or []
        if len(argv) == 1 and Path(argv[0]).is_absolute() and Path(argv[0]).is_file():
            return "trash", argv[0]  # A standalone AppImage.
        return None
    try:
        own = Path(desktop_path).resolve().is_relative_to(Path(user_data).resolve() / "applications")
    except (OSError, ValueError):
        own = False
    if own:
        return "trash", desktop_path
    if not look_up_package:
        return "package", None
    owner = package_owner(desktop_path)
    return ("package", owner) if owner else None
