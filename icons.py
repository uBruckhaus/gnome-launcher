"""Icons chosen by hand for entries without a good one (CLI agents, …).

icons.json maps a desktop id to a theme icon name, or to a file App Manager
saved in ICON_DIR. Online candidates come from two open icon collections on
cdn.jsdelivr.net, fetched with curl (https only, no redirects, one deadline,
a size cap) through the bounded reader; the
popup decodes them only through the same checks as every other icon file
(launcher_core.icon_bytes). No GTK here: the downloads run on a thread."""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from launcher_core import read_bounded_output

OVERRIDES_FILE = Path.home() / ".config/applauncher/icons.json"
ICON_DIR = Path.home() / ".local/share/applauncher/icons"
MAX_FILE_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024
MAX_OVERRIDES = 2000
TIMEOUT = 8
HOST = "cdn.jsdelivr.net"
_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")

# Names the collections use for a tool, where its own name finds nothing.
ALIASES = {
    "codex": ["codex", "openai"], "cursor-agent": ["cursor"], "cursor": ["cursor"],
    "copilot": ["github-copilot", "copilot", "github"], "github copilot cli": ["github-copilot"],
    "claude": ["claude", "anthropic"], "claude code": ["claude", "anthropic"],
    "gemini": ["google-gemini", "gemini"], "gemini cli": ["google-gemini"],
    "opencode": ["opencode"], "grok": ["grok", "x"], "crush": ["charm", "crush"],
    "lm studio": ["lm-studio", "lmstudio"], "pi": ["pi"], "oh my pi": ["pi"],
}


def sources(slug):
    """[(collection, url, kind)] to try for one slug."""
    plain = slug.replace("-", "")
    return [
        ("Dashboard Icons", f"https://{HOST}/gh/homarr-labs/dashboard-icons/svg/{slug}.svg", "svg"),
        ("Dashboard Icons", f"https://{HOST}/gh/homarr-labs/dashboard-icons/png/{slug}.png", "png"),
        ("Simple Icons", f"https://{HOST}/npm/simple-icons@latest/icons/{plain}.svg", "mono"),
    ]


def slugs_for(query, command=None):
    """Slugs to look up for a typed query (and the entry's command)."""
    words = []
    for text in (query, command):
        text = str(text or "").strip().casefold()
        if not text:
            continue
        words += ALIASES.get(text, [])
        words.append(re.sub(r"[^a-z0-9]+", "-", text).strip("-"))
        first = re.split(r"[^a-z0-9]+", text)[0]
        if first:
            words.append(first)
    return [s for s in dict.fromkeys(words) if _SLUG_RE.match(s)][:6]


def looks_like(data, kind):
    if kind == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower())


_URL_RE = re.compile(r"^https://cdn\.jsdelivr\.net/[A-Za-z0-9@._/-]{1,200}\.(?:svg|png)$")


def curl_command(url):
    """https only, no redirects, one deadline for the whole transfer, a size
    cap; the output is read through the bounded reader on top."""
    return ["curl", "--silent", "--fail", "--proto", "=https", "--max-redirs", "0",
            "--max-time", str(TIMEOUT), "--max-filesize", str(MAX_DOWNLOAD_BYTES), "--", url]


def fetch(url, kind, run=read_bounded_output):
    """The icon at url (https on HOST only), at most MAX_DOWNLOAD_BYTES, or None."""
    if not _URL_RE.match(url or "") or ".." in url or not shutil.which("curl"):
        return None
    try:
        data = run(curl_command(url), max_bytes=MAX_DOWNLOAD_BYTES, timeout=TIMEOUT + 2, binary=True)
    except OSError:
        return None
    if not data or len(data) > MAX_DOWNLOAD_BYTES or not looks_like(data, kind):
        return None
    return data


def tint(svg, color):
    """Simple Icons are black shapes: paint them in the theme's text colour."""
    if not re.match(r"^#[0-9a-fA-F]{6}$", color or ""):
        return svg
    return re.sub(rb"<svg\b", b'<svg fill="' + color.encode() + b'"', svg, count=1)


def find_online(query, command=None, color=None, run=read_bounded_output):
    """[(collection, slug, data, extension)] for the candidates that exist."""
    found = []
    for slug in slugs_for(query, command):
        for collection, url, kind in sources(slug):
            data = fetch(url, kind, run)
            if data is None:
                continue
            if kind == "mono":
                data = tint(data, color)
            found.append((collection, slug, data, ".png" if kind == "png" else ".svg"))
            if kind == "svg":
                break  # The SVG is enough; skip the same icon as PNG.
    return found


def load(path=None):
    path = Path(path or OVERRIDES_FILE)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
        data = json.loads(raw) if len(raw) <= MAX_FILE_BYTES else None
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in list(data.items())[:MAX_OVERRIDES]:
        if isinstance(key, str) and 0 < len(key) <= 300 and isinstance(value, str) and valid_value(value):
            result[key] = value
    return result


def valid_value(value):
    """A theme icon name, or a file of ours in ICON_DIR."""
    if _NAME_RE.match(value):
        return True
    path = Path(value)
    return path.is_absolute() and path.parent == ICON_DIR and _NAME_RE.match(path.name) is not None


def _write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save(overrides, path=None):
    _write(Path(path or OVERRIDES_FILE), json.dumps(overrides, indent=2).encode())


def store_file(desktop_id, data, extension):
    """Save a downloaded icon for desktop_id; its path."""
    name = hashlib.sha256(desktop_id.encode()).hexdigest()[:16] + (".png" if extension == ".png" else ".svg")
    path = ICON_DIR / name
    _write(path, data, 0o644)
    return str(path)


def apply(entries, overrides):
    """Use the chosen icons; the original stays in "icon_default"."""
    for app in entries:
        app.setdefault("icon_default", app.get("icon", ""))
        chosen = overrides.get(app.get("desktop_id"))
        app["icon"] = chosen if chosen else app["icon_default"]
    return entries
