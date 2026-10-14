"""Facts for the details page: where an app or plugin lives, which package
it came from, how big it is and when it was installed.

Everything here only reads. Paths come from desktop files and manifests, so
they are resolved and checked before use; pacman gets absolute paths only;
every read and folder walk is bounded.
"""
import os
import re
import shutil
import shlex
from pathlib import Path

from launcher_core import TERMINALS, read_bounded_output

PACMAN_DB = Path("/var/lib/pacman/local")
MAX_DESC_BYTES = 256 * 1024
MAX_WALK_FILES = 20000
MAX_TEXT = 300
_PKG_RE = re.compile(r"^[A-Za-z0-9@._+-]{1,128}$")


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def short(text, limit=MAX_TEXT):
    """One line of plain text: no control or direction characters, capped."""
    text = " ".join(_CONTROL_RE.sub(" ", str(text or "")).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


SHELLS = {"sh", "bash", "zsh", "fish", "dash"}
WEBAPP_LAUNCHERS = {"omarchy-launch-webapp", "xdg-open"}


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
    """The address a web app opens (omarchy-launch-webapp URL), or None."""
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


def folder_size(path, limit=MAX_WALK_FILES):
    """(bytes, complete) for the files below path, never following links."""
    total, count = 0, 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            count += 1
            if count > limit:
                return total, False
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total, True


def git_source(directory):
    """(remote URL, short commit, clone time) of a plugin's git checkout.
    Read from the files, without running git in a folder that may be
    someone else's. .git/description is written once, by the clone."""
    git = Path(directory) / ".git"
    if not git.is_dir() or git.is_symlink():
        return None
    url = commit = None
    try:
        config = (git / "config").read_bytes()[:64 * 1024].decode("utf-8", "replace")
        match = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', config)
        url = match.group(1) if match else None
        head = (git / "HEAD").read_bytes()[:256].decode("utf-8", "replace").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            if re.fullmatch(r"refs/[A-Za-z0-9._/-]{1,200}", ref) and ".." not in ref:
                ref_file = git / ref
                if ref_file.is_file():
                    head = ref_file.read_bytes()[:64].decode("ascii", "replace").strip()
                else:
                    packed = (git / "packed-refs").read_bytes()[:1024 * 1024].decode("utf-8", "replace") \
                        if (git / "packed-refs").is_file() else ""
                    found = re.search(r"^([0-9a-f]{40}) " + re.escape(ref) + "$", packed, re.MULTILINE)
                    head = found.group(1) if found else ""
        commit = head[:7] if re.fullmatch(r"[0-9a-f]{40}", head) else None
    except OSError:
        pass
    try:
        cloned = int((git / "description").stat().st_mtime)
    except OSError:
        cloned = None
    return {"url": url, "commit": commit, "cloned": cloned}


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
        # The launcher script belongs to Omarchy; the app is the web address.
        facts["website"] = short(address)
        facts["omarchy"] = omarchy_origin(app)
        return facts
    program = program_path(app)
    facts["program"] = program
    owner = package_owner(program) or package_owner(app.get("desktop_path"))
    info = package_info(owner) if owner else None
    facts["omarchy"] = omarchy_origin(app, owner)
    if info:
        facts["package"] = info
    elif program:
        try:
            stat = os.stat(program)
            facts["file_size"], facts["modified"] = stat.st_size, int(stat.st_mtime)
        except OSError:
            pass
    return facts


def plugin_facts(plugin, manifests):
    """Facts of a shell plugin: manifest, folder, size, source."""
    directory, manifest = manifests.get(plugin.get("plugin_id"), (None, None))
    facts = {"kind": "widget" if plugin.get("bar_widget") else "plugin",
             "id": plugin.get("plugin_id", ""), "in_bar": plugin.get("in_bar")}
    if manifest:
        for key in ("version", "author", "license"):
            if isinstance(manifest.get(key), str):
                facts[key] = short(manifest[key], 80)
    if directory:
        facts["omarchy"] = omarchy_origin({"plugin_dir": str(directory)})
        facts["folder"] = str(directory)
        facts["size"], facts["size_complete"] = folder_size(directory)
        source = git_source(directory)
        if source:
            facts["source"] = source
        owner = package_owner(str(Path(directory) / "manifest.json"))
        info = package_info(owner) if owner else None
        if info:
            facts["package"] = info
    return facts


# --- Omarchy defaults -----------------------------------------------------------

OMARCHY_ROOT = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
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


def omarchy_packages(root=OMARCHY_ROOT):
    """(packages Omarchy installs, packages it offers or installs later)."""
    def names(name):
        return {line.strip() for line in _read_lines(root / "install" / name)
                if line.strip() and not line.lstrip().startswith("#")}
    return names("omarchy-base.packages"), names("omarchy-other.packages")


def omarchy_origin(app, package=None, root=OMARCHY_ROOT):
    """How an app or plugin came with Omarchy: "bundled" (Omarchy's own
    launcher entry, e.g. its web apps), "installed" (a package Omarchy
    installs), "optional" (one it offers), "builtin" (a shell plugin that
    is part of Omarchy) or None."""
    folder = app.get("plugin_dir")
    if folder:
        try:
            if Path(folder).resolve().is_relative_to(root.resolve()):
                return "builtin"
        except (OSError, ValueError):
            pass
    desktop = Path(app.get("desktop_path") or "").name
    if desktop and (root / "applications" / desktop).is_file():
        return "bundled"
    if package:
        base, other = omarchy_packages(root)
        if package in base:
            return "installed"
        if package in other:
            return "optional"
    return None


def _mime_defaults(config_home=CONFIG_HOME):
    """mime -> first default desktop id, from the user's mimeapps.list files
    (the first file that names a type wins, as in the XDG spec)."""
    found = {}
    for path in (config_home / "mimeapps.list", Path.home() / ".local/share/applications/mimeapps.list"):
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


def default_terminal(config_home=CONFIG_HOME, root=OMARCHY_ROOT):
    """The first terminal of xdg-terminal-exec's lists (Omarchy's default last)."""
    for path in (config_home / "hyprland-xdg-terminals.list", config_home / "xdg-terminals.list",
                 root / "default/xdg-terminal-exec/hyprland-xdg-terminals.list"):
        for line in _read_lines(path):
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def default_roles(app, config_home=CONFIG_HOME, root=OMARCHY_ROOT):
    """Roles this app is the default app for: browser, email, terminal, …"""
    desktop = Path(app.get("desktop_path") or "").name
    if not desktop:
        return []
    defaults = _mime_defaults(config_home)
    roles = [role for role, mimes in ROLE_MIMES if any(defaults.get(m) == desktop for m in mimes)]
    if default_terminal(config_home, root) == desktop:
        roles.insert(1 if roles[:1] == ["browser"] else 0, "terminal")
    return roles


# --- Grouping by origin ---------------------------------------------------------

PACMAN_FILES_LIMIT = 16 * 1024 * 1024
_origin_cache = {"key": None, "index": None}


def omarchy_index(db=PACMAN_DB, root=OMARCHY_ROOT):
    """(desktop file names, program paths) shipped by the packages Omarchy
    installs or offers, read from pacman's local database. Cached until the
    database or Omarchy's package lists change (about 0.02 s to build)."""
    try:
        key = (db.stat().st_mtime_ns, (root / "install/omarchy-base.packages").stat().st_mtime_ns)
    except OSError:
        key = None
    if key is not None and _origin_cache["key"] == key:
        return _origin_cache["index"]
    base, other = omarchy_packages(root)
    names = base | other
    desktops, programs = set(), set()
    try:
        entries = list(db.iterdir())
    except OSError:
        entries = []
    for directory in entries:
        if directory.name.rsplit("-", 2)[0] not in names:
            continue
        try:
            with open(directory / "files", "rb") as fh:
                data = fh.read(PACMAN_FILES_LIMIT)
        except OSError:
            continue
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("usr/share/applications/") and line.endswith(".desktop"):
                desktops.add(line.rsplit("/", 1)[1])
            elif line.startswith("usr/bin/") and not line.endswith("/"):
                programs.add("/" + line)
    index = (frozenset(desktops), frozenset(programs))
    _origin_cache.update(key=key, index=index)
    return index


def default_desktops(config_home=CONFIG_HOME, root=OMARCHY_ROOT):
    """Desktop file names that are one of the user's default apps."""
    defaults = _mime_defaults(config_home)
    found = {defaults[m] for _role, mimes in ROLE_MIMES for m in mimes if defaults.get(m)}
    terminal = default_terminal(config_home, root)
    return found | {terminal} if terminal else found


def origin_section(app, index, defaults, root=OMARCHY_ROOT):
    """"defaults" (one of the user's default apps), "omarchy" (came with
    Omarchy) or "other". A default app that also came with Omarchy counts
    as a default: that is the more specific fact."""
    if "plugin_id" in app:
        return "omarchy" if omarchy_origin({"plugin_dir": app.get("plugin_dir")}, root=root) == "builtin" else "other"
    desktop = Path(app.get("desktop_path") or "").name
    if desktop and desktop in defaults:
        return "defaults"
    desktops, programs = index
    if desktop and (desktop in desktops or (root / "applications" / desktop).is_file()):
        return "omarchy"
    if not web_address(app):
        program = program_path(app)
        if program and program in programs:
            return "omarchy"
    return "other"
