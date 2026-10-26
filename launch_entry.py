#!/usr/bin/env python3
"""Start a desktop entry, or one of its actions (a private window, …): what
a GNOME Launcher shortcut runs. The file is read bounded and without
blocking, so a FIFO or a huge file cannot stall the shortcut.
Usage: launch_entry.py /path/app.desktop [action-id]"""
import sys

import launcher_core  # noqa: E402  (next to this script)


def main(argv):
    if len(argv) not in (2, 3) or not argv[1].endswith(".desktop"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    # Bounded, non-blocking read: a FIFO or a huge file cannot stall the shortcut.
    info = launcher_core.desktop_app_info(argv[1])
    if len(argv) == 2:
        if info is None:
            print(f"Cannot start {argv[1]}", file=sys.stderr)
            return 1
        info.launch([], None)
        return 0
    if info is None or argv[2] not in info.list_actions():
        print(f"No action {argv[2]!r} in {argv[1]}", file=sys.stderr)
        return 1
    info.launch_action(argv[2], None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
