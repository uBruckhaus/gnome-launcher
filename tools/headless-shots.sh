#!/bin/bash
# Runs GNOME Launcher in a throwaway headless GNOME Shell (own HOME, D-Bus,
# dconf and data folders: nothing reaches your session or settings), walks
# through its pages and saves a screenshot of each.
# Usage: tools/headless-shots.sh [output folder]   (default: ./shots)
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [ -z "${GL_INNER:-}" ]; then
    OUT=$(realpath -m "${1:-shots}")
    mkdir -p "$OUT"
    T=$(mktemp -d)
    mkdir -p "$T/home" "$T/config" "$T/data" "$T/cache" "$T/state" "$T/runtime"
    chmod 700 "$T/runtime"
    export GL_INNER=1 T OUT HOME=$T/home XDG_CONFIG_HOME=$T/config XDG_DATA_HOME=$T/data \
        XDG_CACHE_HOME=$T/cache XDG_STATE_HOME=$T/state
    dbus-run-session -- "$0" "$@" 2> >(grep -v -e dbus-daemon -e fusermount >&2)
    status=$?
    rm -rf "$T"
    exit $status
fi
gsettings set org.gnome.shell welcome-dialog-last-shown-version "$(gnome-shell --version | awk '{print $3}')"
gsettings set org.gnome.desktop.interface color-scheme "${SCHEME:-default}"
[ -n "${BUTTONS:-}" ] && gsettings set org.gnome.desktop.wm.preferences button-layout "$BUTTONS"
gnome-shell --headless --wayland --no-x11 --virtual-monitor 1600x1000 \
    --wayland-display gl-test >"$T/shell.log" 2>&1 &
SHELL_PID=$!
sleep 6
WAYLAND_DISPLAY=gl-test GDK_BACKEND=wayland timeout 120 python3 "${SHOTS:-$ROOT/tools/shots.py}" "$OUT"
status=$?
kill $SHELL_PID 2>/dev/null; sleep 1
exit $status
