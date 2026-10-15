"""Newly installed apps and plugins, marked with a star in the popup.

seen.json remembers when each entry (desktop id, or plugin:<id>) was first
seen. The first look at a page records everything already there as old, so
only later installs count as new. An entry stays new for NEW_DAYS, or until
it is opened from the popup."""
import json
import os
import time
from pathlib import Path

SEEN_FILE = Path.home() / ".config/applauncher/seen.json"
NEW_DAYS = 7
MAX_ENTRIES = 5000
MAX_FILE_BYTES = 512 * 1024
KINDS = ("apps", "plugins")


def empty():
    return {"baseline": [], "seen": {}}


def load(path=None):
    """The saved state; anything malformed is dropped entry by entry."""
    path = Path(path or SEEN_FILE)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
        data = json.loads(raw) if len(raw) <= MAX_FILE_BYTES else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return empty()
    baseline = [k for k in data.get("baseline", []) if k in KINDS] if isinstance(data.get("baseline"), list) else []
    raw_seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
    seen = {key: int(value) for key, value in list(raw_seen.items())[:MAX_ENTRIES]
            if isinstance(key, str) and 0 < len(key) <= 300
            and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0}
    return {"baseline": list(dict.fromkeys(baseline)), "seen": seen}


def save(data, path=None):
    """Write atomically and privately (0600, never through a symlink)."""
    path = Path(path or SEEN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def note(data, ids, kind, now=None, installed=None):
    """Record entries seen on page kind; True when something changed.
    installed: {id: when it was installed}, where known (an entry that shows
    up late, like a CLI agent App Manager learned to list, is new only if it
    was installed recently)."""
    now = int(now if now is not None else time.time())
    installed = installed or {}
    seen = data["seen"]
    first_look = kind not in data["baseline"]
    changed = first_look
    for key in ids:
        if isinstance(key, str) and key and key not in seen:
            when = installed.get(key)
            # 0: old, never new. An install time a week back or more counts as old too.
            seen[key] = 0 if first_look else (int(when) if isinstance(when, (int, float)) and 0 < when < now else now)
            if now - seen[key] >= NEW_DAYS * 86400:
                seen[key] = 0
            changed = True
    if first_look:
        data["baseline"].append(kind)
    if len(seen) > MAX_ENTRIES:
        # Forget the oldest first.
        for key, _value in sorted(seen.items(), key=lambda item: item[1])[:len(seen) - MAX_ENTRIES]:
            del seen[key]
    return changed


def is_new(data, key, now=None):
    first = data["seen"].get(key) or 0
    return first > 0 and (now if now is not None else time.time()) - first < NEW_DAYS * 86400


def opened(data, key):
    """Opened from the popup: not new any more. True when it was."""
    if data["seen"].get(key):
        data["seen"][key] = 0
        return True
    return False
