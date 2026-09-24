# Simple Launcher for Fools

Fast, theme-aware application launcher popup and native bar widget for [Omarchy](https://omarchy.org).

![Simple Launcher for Fools](preview.png)

## Features

- **Omarchy Bar Widget**: Sits natively in the Quickshell top bar as an app-grid button. Click to toggle the launcher on and off.
- **Wayland Layer-Shell Overlay**: Built on GTK4 Layer Shell for zero-latency, buttery-smooth appearance above all windows.
- **Theme Harmony**: Automatically loads and syncs with Omarchy's active theme palette (`~/.local/state/omarchy/current/theme/colors.toml`). Accent colors, selections, and background match your desktop styling dynamically.
- **Instant Search**: Start typing anywhere to immediately filter applications. Supports backspace and Escape to clear.
- **Running App Tracking**: Inspects Hyprland client states and running processes in real-time, displaying status indicator dots next to active apps.
- **PWA & AppImage Support**: Automatically scans `.desktop` files, Chromium/Chrome Progressive Web Apps (PWAs), and standalone AppImage executables.
- **Hidden Apps Management**: Conceal distracting helper tools or background daemons from the launcher view with one click, or toggle visibility with the top-right eye icon.
- **Lightweight & Self-Contained**: Powered by standard system Python and GIO/GTK4. No heavy background virtual environments needed.

## Installation

Install using the Omarchy plugin manager:

```bash
omarchy plugin add https://github.com/ubruckhaus/omarchy-simple-launcher-for-fools.git --enable
```

Or install locally:

1. Clone or copy into `~/.config/omarchy/plugins/ubruckhaus.simple-launcher-for-fools/`
2. Enable via the Omarchy CLI:
   ```bash
   omarchy plugin enable ubruckhaus.simple-launcher-for-fools
   ```

## Keybindings (Optional)

To bind Simple Launcher for Fools to a global shortcut (such as `SUPER + SPACE`), add the following to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER, SPACE", "Simple Launcher", "~/.config/omarchy/plugins/ubruckhaus.simple-launcher-for-fools/launcher-toggle")
```

Or for `SUPER + CTRL + ALT + SPACE`:

```lua
o.bind("SUPER + CTRL + ALT + SPACE", "Simple Launcher", "~/.config/omarchy/plugins/ubruckhaus.simple-launcher-for-fools/launcher-toggle")
```

## Keyboard Navigation

| Key | Action |
| --- | --- |
| `Any letter / number` | Type to filter apps instantly |
| `Up` / `Down` | Move selection through app list |
| `Page Up` / `Page Down` | Jump selection by 6 items |
| `Home` / `End` | Jump to start / end of list |
| `Return` / `Enter` | Launch selected application |
| `Backspace` | Erase search character |
| `Escape` | Clear search query, or close launcher if search is empty |
| `Right Arrow` | Open context actions menu (for apps with desktop actions) |
| `Delete` | Close active instance of the selected app |
| `Click outside` | Dismiss launcher popup |

## Removal

To remove the plugin:

```bash
omarchy plugin remove ubruckhaus.simple-launcher-for-fools
```

Hidden apps configuration is saved in `~/.config/applauncher/hidden.json` and will persist across updates.

## License

MIT License. See [LICENSE](LICENSE) for details.
