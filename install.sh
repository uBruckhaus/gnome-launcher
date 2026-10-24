#!/bin/bash
# Installs GNOME Launcher for the current user: a desktop entry that starts it
# from this folder, and its icon. `./install.sh --uninstall` removes both.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
ID=io.github.ubruckhaus.Launcher
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
ENTRY=$DATA/applications/$ID.desktop
ICON=$DATA/icons/hicolor/scalable/apps/$ID.svg
# The top bar icon (Settings › Top bar icon): linked now, so GNOME Shell finds
# it at the next login; it stays off until switched on there.
EXTENSION_UUID=launcher-button@ubruckhaus.github.io
EXTENSION=$DATA/gnome-shell/extensions/$EXTENSION_UUID

# Only a cache that is already there: a new one would go stale and hide the
# icons other apps add later.
refresh_cache() {
    if [ -f "$DATA/icons/hicolor/icon-theme.cache" ]; then
        gtk4-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$ENTRY" "$ICON"
    [ -L "$EXTENSION" ] && rm -f "$EXTENSION"
    refresh_cache
    echo "Removed GNOME Launcher's desktop entry, icon and top bar extension. Settings stay in ~/.config/gnome-launcher."
    exit 0
fi

python3 -c "import gi" 2>/dev/null || { echo "python-gobject is missing" >&2; exit 1; }
python3 -c 'import gi; gi.require_version("Adw", "1"); gi.require_version("Gtk", "4.0")' 2>/dev/null \
    || { echo "GTK 4 and libadwaita (with their GObject introspection data) are missing" >&2; exit 1; }

mkdir -p "$(dirname "$ENTRY")" "$(dirname "$ICON")"
install -m 644 "$ROOT/data/$ID.svg" "$ICON"
mkdir -p "$(dirname "$EXTENSION")"
if [ -L "$EXTENSION" ] || [ ! -e "$EXTENSION" ]; then
    ln -sfn "$ROOT/shell-extension/$EXTENSION_UUID" "$EXTENSION"
fi
# Quoted for the desktop entry spec: the path may contain spaces.
EXEC_PATH=$(printf '%s' "$ROOT/launcher.py" | sed 's/[\\"`$]/\\&/g')
cat > "$ENTRY" <<ENTRY
[Desktop Entry]
Type=Application
Name=Launcher
GenericName=App Launcher
Comment=Launch, group, bind and inspect your apps
Exec=python3 "$EXEC_PATH"
Icon=$ID
Terminal=false
Categories=Utility;GTK;
Keywords=apps;launcher;shortcuts;groups;folders;
StartupNotify=true
StartupWMClass=$ID
ENTRY
refresh_cache
echo "Installed: $ENTRY"
echo "Start it from the app grid (search \"Launcher\"), or give it a key: open it, then its own menu › Shortcut…"
