"""Which apps and plugins have updates: no GTK here, it runs on a thread.

Plugins: a git checkout in ~/.config/omarchy/plugins that is behind its
remote's default branch after a fetch (what `omarchy plugin update` would pull).
Apps: the package that owns the app's .desktop file has a newer version in
the repositories (checkupdates) or the AUR (yay -Qua)."""
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from launcher_core import USER_PLUGINS_DIR, read_bounded_output

CHECK_INTERVAL = 30 * 60   # Seconds between checks (each one fetches).
MAX_PLUGINS = 64
MAX_DESKTOP_FILES = 2000
# "name 1.2-1 -> 1.3-1" from checkupdates and yay -Qua.
_UPDATE_LINE = re.compile(r"^([A-Za-z0-9@._+-]{1,128}) (\S{1,64}) -> (\S{1,64})$")
# "/usr/share/applications/x.desktop is owned by name 1.2-1" from pacman -Qo.
_OWNER_LINE = re.compile(r"^(/.{1,1024}) is owned by ([A-Za-z0-9@._+-]{1,128}) \S{1,64}$")


def _quiet_git_env():
    env = dict(os.environ)
    # Never ask for a password or a host key: a check runs unattended.
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/true", SSH_ASKPASS="/bin/true",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10", LC_ALL="C")
    return env


def plugin_behind(directory, fetch=True):
    """Commits the checkout is behind its upstream, or None (no git, no
    upstream, offline)."""
    directory = Path(directory)
    if not (directory / ".git").exists():
        return None
    env = _quiet_git_env()
    # The same comparison as `omarchy plugin update`: the remote's default
    # branch (origin HEAD), fetched into FETCH_HEAD.
    if fetch:
        read_bounded_output(["git", "-C", str(directory), "fetch", "--quiet", "--no-tags", "origin", "HEAD"],
                            max_bytes=64 * 1024, timeout=30, env=env)
    count = read_bounded_output(["git", "-C", str(directory), "rev-list", "--count", "HEAD..FETCH_HEAD"],
                                max_bytes=64, timeout=5, env=env)
    try:
        return int(count.strip()) if count is not None else None
    except ValueError:
        return None


def plugin_updates(plugin_ids, fetch=True):
    """{plugin id: commits behind} for the installed plugins with updates."""
    ids = [p for p in dict.fromkeys(plugin_ids) if isinstance(p, str) and p and "/" not in p
           and ".." not in p][:MAX_PLUGINS]
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = pool.map(lambda p: plugin_behind(USER_PLUGINS_DIR / p, fetch), ids)
        return {p: n for p, n in zip(ids, counts) if n}


def run_plugin_update(plugin_id, run=None):
    """`omarchy plugin update <id> --yes`: fast-forward only (local changes
    stop it), validated, rolled back on failure, then the shell rescans.
    (ok, message): the command's last error line when it failed."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", plugin_id or "") or ".." in plugin_id:
        return False, "invalid plugin id"
    run = run or run_bounded
    code, output = run(["omarchy", "plugin", "update", plugin_id, "--yes"], 64 * 1024, 180, _quiet_git_env())
    if code == 0:
        return True, ""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    message = lines[-1] if lines else ("timed out or too much output" if code is None else f"exit {code}")
    return False, message.removeprefix("omarchy-plugin-update: ")[:300]


def run_bounded(command, max_bytes, timeout, env=None):
    """(exit code, stdout+stderr) with a byte cap and one deadline; the code
    is None when the command ran too long or wrote too much (then killed)."""
    import selectors
    import time
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env) as process:
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        return None, output.decode("utf-8", "replace")
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > max_bytes:
                        return None, output[:max_bytes].decode("utf-8", "replace")
            return process.wait(timeout=max(0.1, deadline - time.monotonic())), output.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return None, output.decode("utf-8", "replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def parse_update_lines(text):
    """{package: (installed, available)} from checkupdates / yay -Qua output."""
    result = {}
    for line in (text or "").splitlines():
        match = _UPDATE_LINE.match(line.strip())
        if match:
            result[match.group(1)] = (match.group(2), match.group(3))
    return result


def package_updates():
    """{package: (installed, available)} from the repositories and the AUR."""
    result = {}
    if shutil.which("checkupdates"):
        # Exit 2: no updates. checkupdates syncs a private copy of the database.
        result.update(parse_update_lines(read_bounded_output(
            ["checkupdates"], max_bytes=1024 * 1024, timeout=90, ok_codes=(0, 2))))
    for helper in ("yay", "paru"):
        if shutil.which(helper):
            # Exit 1: no AUR updates.
            result.update(parse_update_lines(read_bounded_output(
                [helper, "-Qua"], max_bytes=1024 * 1024, timeout=90, ok_codes=(0, 1))))
            break
    return result


def parse_owner_lines(text):
    result = {}
    for line in (text or "").splitlines():
        match = _OWNER_LINE.match(line.strip())
        if match:
            result[match.group(1)] = match.group(2)
    return result


def desktop_packages(paths):
    """{.desktop path: owning package} for the system-wide desktop files."""
    paths = [p for p in dict.fromkeys(paths) if isinstance(p, str) and p.startswith("/usr/")
             and p.endswith(".desktop")][:MAX_DESKTOP_FILES]
    if not paths or not shutil.which("pacman"):
        return {}
    # Exit 1 when some file has no owner; the others are still listed.
    return parse_owner_lines(read_bounded_output(
        ["pacman", "-Qo", "--", *paths], max_bytes=1024 * 1024, timeout=20, ok_codes=(0, 1),
        env={**os.environ, "LC_ALL": "C"}))


def check(desktop_paths, plugin_ids, fetch=True):
    """Everything at once, for the popup's background thread."""
    owners = desktop_packages(desktop_paths)
    packages = package_updates() if owners else {}
    return {
        "apps": {path: (pkg, *packages[pkg]) for path, pkg in owners.items() if pkg in packages},
        "plugins": plugin_updates(plugin_ids, fetch),
    }
