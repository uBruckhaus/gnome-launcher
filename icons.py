"""Icons chosen by hand for entries without a good one (CLI agents, …).

icons.json maps a desktop id to a theme icon name, or to a file App Manager
saved in ICON_DIR. Online candidates come from three open icon collections on
cdn.jsdelivr.net (Dashboard Icons, selfh.st icons, Simple Icons). Their name
lists (with aliases) are downloaded at most once a week into INDEX_DIR and
searched by name, alias and word; the icons themselves are fetched with curl
(https only, no redirects, one deadline, a size cap) through the bounded
reader, and the popup decodes them only through the same checks as every
other icon file (launcher_core.icon_bytes). No GTK here: the downloads run
on threads."""
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from launcher_core import read_bounded_output

OVERRIDES_FILE = Path.home() / ".config/applauncher/icons.json"
ICON_DIR = Path.home() / ".local/share/applauncher/icons"
MAX_FILE_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024
MAX_OVERRIDES = 2000
TIMEOUT = 8
HOST = "cdn.jsdelivr.net"
INDEX_DIR = Path.home() / ".cache/applauncher/icon-index"
MAX_INDEX_BYTES = 4 * 1024 * 1024
INDEX_TIMEOUT = 20
INDEX_MAX_AGE = 7 * 24 * 3600
MAX_RESULTS = 24
# The collections' name lists: (file in INDEX_DIR, url).
INDEXES = {
    "dashboard": f"https://{HOST}/gh/homarr-labs/dashboard-icons/metadata.json",
    "selfhst": f"https://{HOST}/gh/selfhst/icons/index.json",
    "simple": f"https://{HOST}/npm/simple-icons@latest/data/simple-icons.json",
}
COLLECTION_NAMES = {"dashboard": "Dashboard Icons", "selfhst": "selfh.st", "simple": "Simple Icons"}
_INDEX_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
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
    "vs code": ["visual-studio-code", "vscode"], "code": ["visual-studio-code", "vscode"],
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


_URL_RE = re.compile(r"^https://cdn\.jsdelivr\.net/[A-Za-z0-9@._/-]{1,200}\.(?:svg|png|json)$")


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


def fold(text):
    """Lower case, every run of other characters one space."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).split())


def index_entries(key, data):
    """[(collection, slug, url, kind, names)] from one collection's name list;
    anything malformed is skipped."""
    out = []
    if key == "dashboard" and isinstance(data, dict):
        for slug, meta in list(data.items())[:20000]:
            if not isinstance(slug, str) or not _INDEX_SLUG_RE.match(slug) or not isinstance(meta, dict):
                continue
            kind = "png" if meta.get("base") == "png" else "svg"
            aliases = meta.get("aliases") if isinstance(meta.get("aliases"), list) else []
            url = f"https://{HOST}/gh/homarr-labs/dashboard-icons/{kind}/{slug}.{kind}"
            out.append(("dashboard", slug, url, kind, [slug] + [a for a in aliases[:20] if isinstance(a, str)]))
    elif key == "selfhst" and isinstance(data, list):
        for item in data[:20000]:
            if not isinstance(item, dict):
                continue
            slug, name = item.get("Reference"), item.get("Name")
            if not isinstance(slug, str) or not _INDEX_SLUG_RE.match(slug):
                continue
            kind = "svg" if item.get("SVG") == "Yes" else "png" if item.get("PNG") == "Yes" else None
            if kind is None:
                continue
            tags = item.get("Tags") if isinstance(item.get("Tags"), str) else ""
            names = [slug] + ([name] if isinstance(name, str) else []) + [t for t in tags.split(",")[:20] if t.strip()]
            out.append(("selfhst", slug, f"https://{HOST}/gh/selfhst/icons/{kind}/{slug}.{kind}", kind, names))
    elif key == "simple" and isinstance(data, list):
        for item in data[:20000]:
            if not isinstance(item, dict):
                continue
            slug, title = item.get("slug"), item.get("title")
            if not isinstance(slug, str) or not _INDEX_SLUG_RE.match(slug) or "." in slug:
                continue
            aka = (item.get("aliases") or {}).get("aka") if isinstance(item.get("aliases"), dict) else None
            names = [slug] + ([title] if isinstance(title, str) else []) + \
                [a for a in (aka if isinstance(aka, list) else [])[:20] if isinstance(a, str)]
            out.append(("simple", slug, f"https://{HOST}/npm/simple-icons@latest/icons/{slug}.svg", "mono", names))
    return out


def _read_cache(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_INDEX_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= MAX_INDEX_BYTES else None


def load_index(key, run=read_bounded_output, now=None, directory=None):
    """One collection's entries: the cached name list while it is younger
    than a week, else downloaded again (the old copy when that fails)."""
    directory = Path(directory or INDEX_DIR)
    path = directory / f"{key}.json"
    now = time.time() if now is None else now
    raw = _read_cache(path)
    try:
        fresh = raw is not None and now - os.lstat(path).st_mtime < INDEX_MAX_AGE
    except OSError:
        fresh = False
    if not fresh and shutil.which("curl"):
        url = INDEXES[key]
        command = curl_command(url)
        command[command.index("--max-time") + 1] = str(INDEX_TIMEOUT)
        command[command.index("--max-filesize") + 1] = str(MAX_INDEX_BYTES)
        try:
            data = run(command, max_bytes=MAX_INDEX_BYTES, timeout=INDEX_TIMEOUT + 2, binary=True)
        except OSError:
            data = None
        if data and len(data) <= MAX_INDEX_BYTES:
            try:
                json.loads(data)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write(path, data)
                raw = data
            except (ValueError, OSError):
                pass
    try:
        return index_entries(key, json.loads(raw)) if raw else []
    except ValueError:
        return []


def score(query, names):
    """How well a typed query matches an icon's names; 0: not at all."""
    q = fold(query)
    if not q:
        return 0
    compact = q.replace(" ", "")
    words = q.split()
    best = 0
    for name in names:
        n = fold(name)
        if not n:
            continue
        n_compact = n.replace(" ", "")
        n_words = n.split()
        if n == q or n_compact == compact:
            best = max(best, 100)
        elif n.startswith(q) or n_compact.startswith(compact):
            best = max(best, 80 - min(20, len(n_compact) - len(compact)))
        elif all(any(w2.startswith(w) for w2 in n_words) for w in words):
            best = max(best, 60)
        elif len(compact) >= 3 and compact in n_compact:
            best = max(best, 40)
        elif len(words) > 1 and any(w == w2 for w in words if len(w) >= 3 for w2 in n_words):
            best = max(best, 20)
    return best


_ORDER = {"dashboard": 0, "selfhst": 1, "simple": 2}


def search(query, command=None, entries=(), limit=MAX_RESULTS):
    """The best matching icons: [(collection key, slug, url, kind)]. The
    typed query counts most, the entry's command (and the aliases) less."""
    terms = [(query, 1.0)] + [(a, 0.9) for a in ALIASES.get(fold(query), [])]
    if command and fold(command) != fold(query):
        terms += [(command, 0.7)] + [(a, 0.6) for a in ALIASES.get(fold(command), [])]
    ranked = []
    for collection, slug, url, kind, names in entries:
        value = max(score(term, names) * weight for term, weight in terms)
        if value > 0:
            ranked.append((-value, _ORDER.get(collection, 9), len(slug), slug, collection, url, kind))
    ranked.sort()
    out, seen = [], set()
    for _v, _o, _l, slug, collection, url, kind in ranked:
        if (collection, slug) in seen:
            continue
        seen.add((collection, slug))
        out.append((collection, slug, url, kind))
        if len(out) >= limit:
            break
    return out


def candidates(query, command=None, run=read_bounded_output, directory=None):
    """What to download for a query: index matches, else the old guesses
    (when the name lists cannot be had)."""
    entries = []
    for key in INDEXES:
        entries += load_index(key, run, directory=directory)
    if entries:
        return search(query, command, entries)
    guesses = []
    for slug in slugs_for(query, command):
        for name, url, kind in sources(slug):
            key = next(k for k, v in COLLECTION_NAMES.items() if v == name)
            guesses.append((key, slug, url, kind))
    return guesses


def download(candidate, color=None, run=read_bounded_output):
    """(collection name, slug, data, extension) for one candidate, or None."""
    collection, slug, url, kind = candidate
    data = fetch(url, kind, run)
    if data is None and collection in ("dashboard", "selfhst") and kind in ("svg", "png"):
        # The CDN lacks some files of these repositories: try the other format.
        other = "png" if kind == "svg" else "svg"
        url = url[: -len(f"{kind}/{slug}.{kind}")] + f"{other}/{slug}.{other}"
        kind = other
        data = fetch(url, kind, run)
    if data is None:
        return None
    if kind == "mono":
        data = tint(data, color)
    return COLLECTION_NAMES.get(collection, collection), slug, data, ".png" if kind == "png" else ".svg"


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
