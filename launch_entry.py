#!/usr/bin/env python3
"""Run one action of a desktop entry (a private window, …): what a GNOME
Launcher shortcut for an app's action runs, since `gio launch` opens only
the app itself. Usage: launch_entry.py /path/app.desktop action-id"""
import sys

import gi

gi.require_version("GioUnix", "2.0")
from gi.repository import GioUnix  # noqa: E402


def main(argv):
    if len(argv) != 3 or not argv[1].endswith(".desktop"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    info = GioUnix.DesktopAppInfo.new_from_filename(argv[1])
    if info is None or argv[2] not in info.list_actions():
        print(f"No action {argv[2]!r} in {argv[1]}", file=sys.stderr)
        return 1
    info.launch_action(argv[2], None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
