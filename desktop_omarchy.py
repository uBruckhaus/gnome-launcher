"""Omarchy (Hyprland): desktop.py's interface for the bar popup.

- window: a full-screen transparent layer-shell surface with the card in it
- theme: ~/.local/state/omarchy/current/theme/colors.toml; rounding from
  `hyprctl getoption decoration:rounding -j`
- windows: `hyprctl clients -j`, closed through a Hyprland dispatcher;
  Hyprland tiles, so an app can be opened floating (`hl.exec_cmd` with the
  float rule)
- Omarchy-only features: shell plugins, the dock, Hyprland shortcuts, the
  menus watch and `omarchy-update`.
"""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

NAME = "omarchy"
APP_ID = "org.omarchy.simplelauncherforfools"
COLORS_FILE = Path.home() / ".local/state/omarchy/current/theme/colors.toml"


def app_id() -> str:
    return APP_ID


def popup() -> bool:
    return True


def theme_colors(path=None) -> dict:
    from palette import DEFAULT_COLORS
    try:
        theme = tomllib.loads(Path(path or COLORS_FILE).read_text())
    except (OSError, ValueError):
        return DEFAULT_COLORS.copy()
    colors = {key: theme.get(key, fallback) for key, fallback in DEFAULT_COLORS.items()}
    if "muted" not in theme:
        colors["muted"] = theme.get("dark_foreground") or theme.get("color8") or DEFAULT_COLORS["muted"]
    return colors


def window_rounding():
    from launcher_core import read_bounded_output
    import corners
    out = read_bounded_output(["hyprctl", "getoption", "decoration:rounding", "-j"], max_bytes=4096, timeout=2)
    return corners.parse_rounding(out or "")


def rounding_source() -> str:
    import corners
    return corners.rounding_source()


def clients():
    try:
        result = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def knows_windows() -> bool:
    return True


def close_window(client):
    address = client.get("address", "")
    if not address.startswith("0x"):
        return
    command = "hl.dsp.window.close({window = " + json.dumps("address:" + address) + "})"
    subprocess.Popen(["hyprctl", "dispatch", command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def can_float() -> bool:
    return True


def lua_string(text: str) -> str:
    """text as a Lua string literal: every byte escaped (\\ddd), so nothing in
    it can end the string or reach the Lua code around it."""
    return '"' + "".join(f"\\{byte:03d}" for byte in text.encode()) + '"'


def exec_floating(argv) -> bool:
    """Start argv through Hyprland with the float rule: its first window
    opens floating, whatever the window rules say (Hyprland's exec runs it
    in a shell, so the words are quoted)."""
    import shlex
    code = f"hl.exec_cmd({lua_string(shlex.join(argv))}, {{float = true}})"
    try:
        result = subprocess.run(["hyprctl", "eval", code], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "ok"


def has_plugins() -> bool:
    return True


def has_dock() -> bool:
    return True


def has_shortcuts() -> bool:
    return True


def watches_menus() -> bool:
    return True


def system_update():
    """Omarchy's own update window (password, snapshot, all packages)."""
    launcher = shutil.which("omarchy-launch-floating-terminal-with-presentation")
    if launcher is None or shutil.which("omarchy-update") is None:
        return None
    return [launcher, "omarchy-update"]
