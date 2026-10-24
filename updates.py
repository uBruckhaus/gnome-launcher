"""Which apps have updates: no GTK here, it runs on a thread.

Packages: the package that owns the app's .desktop file has a newer version
in the repositories (checkupdates) or the AUR (yay / paru -Qua).
Flatpaks: `flatpak remote-ls --updates` lists the app's id."""
import os
import re
import shutil
import subprocess

from launcher_core import read_bounded_output

CHECK_INTERVAL = 30 * 60   # Seconds between checks (each one fetches).
MAX_DESKTOP_FILES = 2000
# "name 1.2-1 -> 1.3-1" from checkupdates and yay -Qua.
_UPDATE_LINE = re.compile(r"^([A-Za-z0-9@._+-]{1,128}) (\S{1,64}) -> (\S{1,64})$")
# "org.gnome.Maps\t49.1" from flatpak's application,version columns.
_FLATPAK_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]{2,254})\t(\S{0,64})$")
# "/usr/share/applications/x.desktop is owned by name 1.2-1" from pacman -Qo.
_OWNER_LINE = re.compile(r"^(/.{1,1024}) is owned by ([A-Za-z0-9@._+-]{1,128}) \S{1,64}$")


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


def parse_flatpak_lines(text):
    """{app id: version} from flatpak's `--columns=application,version`."""
    result = {}
    for line in (text or "").splitlines():
        match = _FLATPAK_LINE.match(line.rstrip("\n"))
        if match:
            result[match.group(1)] = match.group(2)
    return result


def flatpak_updates():
    """{app id: (installed version, available version)} for the Flatpak
    apps with an update (asks the remotes)."""
    if not shutil.which("flatpak"):
        return {}
    columns = ["--app", "--columns=application,version"]
    available = parse_flatpak_lines(read_bounded_output(
        ["flatpak", "remote-ls", "--updates", *columns], max_bytes=1024 * 1024, timeout=90))
    if not available:
        return {}
    installed = parse_flatpak_lines(read_bounded_output(
        ["flatpak", "list", *columns], max_bytes=1024 * 1024, timeout=20))
    return {app: (installed.get(app, ""), version) for app, version in available.items()}


def run_flatpak_update(app_id, run=None):
    """`flatpak update -y <app id>`: one Flatpak app on its own (unlike a
    package, which is never updated alone on Arch). (ok, message)."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,254}", app_id or "") or ".." in app_id:
        return False, "invalid app id"
    run = run or run_bounded
    code, output = run(["flatpak", "update", "-y", "--noninteractive", app_id], 64 * 1024, 600,
                       {**os.environ, "LC_ALL": "C"})
    if code == 0:
        return True, ""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    return False, (lines[-1] if lines else ("timed out or too much output" if code is None else f"exit {code}"))[:300]


def check(desktop_paths, fetch=True):
    """Everything at once, for the window's background thread: {"apps":
    {.desktop path: (package, installed, available)}, "flatpaks": {app id:
    (installed, available)}}."""
    owners = desktop_packages(desktop_paths)
    packages = package_updates() if owners else {}
    return {
        "apps": {path: (pkg, *packages[pkg]) for path, pkg in owners.items() if pkg in packages},
        "flatpaks": flatpak_updates() if fetch else {},
    }
