"""Command-line agents and TUIs, as entries that open in a terminal.

Found three ways: wrappers in ~/.local/bin that run a mise tool
(`exec mise x "<tool>" -- "<bin>"`, as Omarchy writes them), listed only
when mise has really installed the tool (~/.local/share/mise/installs/<dir>)
and dated by when that folder was created, so only a fresh install counts as
new; agents mise installed without a wrapper, by their shim; and known
agents installed any other way (npm, their own installer), on the PATH."""
import re
import shutil
import subprocess
from pathlib import Path

from i18n import _

BIN_DIR = Path.home() / ".local/bin"
MISE_DIR = Path.home() / ".local/share/mise"
MAX_WRAPPER_BYTES = 4096
MAX_WRAPPERS = 400
_EXEC_RE = re.compile(r'^exec mise x "([^"\n]{1,512})" -- "([A-Za-z0-9._-]{1,64})" "\$@"$', re.MULTILINE)

# Display names; anything else shows its command name.
NAMES = {
    "claude": "Claude Code", "codex": "Codex", "copilot": "GitHub Copilot CLI",
    "cursor-agent": "Cursor Agent", "opencode": "OpenCode", "crush": "Crush", "grok": "Grok",
    "omp": "Oh My Pi", "muse": "Muse", "gemini": "Gemini CLI", "pi": "pi", "amp": "Amp",
    "goose": "Goose", "aider": "Aider", "qwen": "Qwen Code", "openclaw": "OpenClaw",
    "ghui": "ghui", "hunk": "hunk",
}
# Agents found without a wrapper: by their mise shim, or on the PATH.
KNOWN_AGENTS = ("codex", "claude", "copilot", "cursor-agent", "opencode", "gemini", "crush",
                "amp", "goose", "aider", "qwen", "openclaw", "pi")
# Wrapped too, but plain commands, not something to open in a window.
NOT_APPS = {"gh", "node", "npm", "npx", "bun", "deno", "python", "python3", "uv", "go", "cargo", "mise"}


def install_dir_candidates(spec):
    """mise's folder names for a tool spec: "opencode", "npm:@a/b" -> "npm-a-b"."""
    base = spec.split("[", 1)[0]
    plain = re.sub(r"[:/@]+", "-", base).strip("-")
    names = [plain, base.rsplit("/", 1)[-1].split(":", 1)[-1].lstrip("@")]
    # Only plain folder names inside mise's installs: never "..", ".", or a path.
    return [n for n in names if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", n) and ".." not in n]


def wrapper_tools(bin_dir=BIN_DIR):
    """{command: tool spec} from mise wrappers."""
    tools = {}
    try:
        paths = sorted(p for p in bin_dir.iterdir() if p.is_file())[:MAX_WRAPPERS]
    except OSError:
        return tools
    for path in paths:
        try:
            if path.stat().st_size > MAX_WRAPPER_BYTES:
                continue
            match = _EXEC_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if match and match.group(2) == path.name:
            tools[path.name] = match.group(1)
    return tools


def birth_time(path):
    """When path was created (seconds), or its change time when the file
    system does not tell."""
    try:
        out = subprocess.run(["stat", "-c", "%W", "--", str(path)], capture_output=True, text=True,
                             timeout=2, check=True).stdout.strip()
        born = int(out)
        if born > 0:
            return born
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        return int(path.stat().st_ctime)
    except OSError:
        return None


def icon_for(command):
    """A theme icon named like the command (one the user added), else a terminal."""
    for root in (Path.home() / ".local/share/icons/hicolor", Path("/usr/share/icons/hicolor")):
        if any(root.glob(f"*/apps/{command}.*")):
            return command
    if any(Path("/usr/share/pixmaps").glob(f"{command}.*")):
        return command
    return "utilities-terminal"


def cli_entries(known_commands, bin_dir=BIN_DIR, mise_dir=MISE_DIR, which=shutil.which):
    """Entries for installed CLI tools that no .desktop file opens already.
    known_commands: command names other entries launch (claude for foot -e …/claude)."""
    installs = mise_dir / "installs"
    tools = dict(wrapper_tools(bin_dir))
    for name in KNOWN_AGENTS:
        if name not in tools and (mise_dir / "shims" / name).exists():
            tools[name] = name
    # Known agents installed another way: the program on the PATH.
    elsewhere = {}
    for name in KNOWN_AGENTS:
        found = which(name) if name not in tools else None
        if found:
            elsewhere[name] = Path(found)
    entries = []
    for command, spec in sorted({**tools, **{name: None for name in elsewhere}}.items()):
        if command in known_commands or command in NOT_APPS:
            continue
        if spec is None:
            folder = target = elsewhere[command]
        else:
            folder = next((installs / c for c in install_dir_candidates(spec) if c and (installs / c).is_dir()), None)
            if folder is None:
                continue  # A wrapper whose tool is not installed yet.
            target = bin_dir / command if (bin_dir / command).exists() else mise_dir / "shims" / command
        app_id = f"io.github.ubruckhaus.Launcher.{command}"
        argv = ["xdg-terminal-exec", f"--app-id={app_id}", "-e", str(target)]
        entries.append({
            "name": NAMES.get(command, command),
            "exec": " ".join(argv),
            "argv": argv,
            "icon": icon_for(command),
            "desktop_path": "",
            "actions": [],
            "description": _("Command-line tool, opens in a terminal"),
            "categories": ["Development"],
            "desktop_id": f"cli:{command}",
            "startup_class": app_id,
            "cli_command": command,
            "installed_at": birth_time(folder),
        })
    return entries
