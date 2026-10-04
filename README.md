# Simple Launcher

Fast, theme-aware application launcher popup and bar widget for [Omarchy](https://omarchy.org), with per-app global shortcuts and conflict-aware key suggestions.

![Simple Launcher](preview.png)

## Features

- **Omarchy Bar Widget**: Sits natively in the Quickshell top bar as an app-grid button. Click to toggle the launcher on and off.
- **Wayland Layer-Shell Overlay**: Built on GTK4 Layer Shell for zero-latency, buttery-smooth appearance above all windows.
- **Theme Harmony**: Automatically loads and syncs with Omarchy's active theme palette (`~/.local/state/omarchy/current/theme/colors.toml`). Accent colors, selections, and background match your desktop styling dynamically.
- **Instant Search**: Start typing anywhere to immediately filter applications. Supports backspace and Escape to clear.
- **Running App Tracking**: Inspects Hyprland client states and running processes in real-time, displaying status indicator dots next to active apps.
- **PWA & AppImage Support**: Automatically scans `.desktop` files, Chromium/Chrome Progressive Web Apps (PWAs), and standalone AppImage executables.
- **Hidden Apps Management**: Conceal distracting helper tools or background daemons from the launcher view with one click, or toggle visibility with the top-right eye icon.
- **App Shortcuts**: Bind a global Hyprland shortcut to any app from its actions menu (`Shortcut…` or `Ctrl+K`). Conflicts are graded: free combos are suggested, your own bindings show a warning before being replaced, and Omarchy defaults need an explicit second confirmation.
- **Lightweight & Self-Contained**: Powered by standard system Python and GIO/GTK4. No heavy background virtual environments needed.

Standalone AppImage metadata is limited to 64 KiB. Icons are extracted through a bounded pipe with a 1 MiB limit before being saved atomically in the cache. Oversized, timed-out, or failed icon extractions use the generic application icon. The archive extractor never writes icons directly to disk.

Desktop entries launch through `uwsm-app` or Gio using the original desktop file. If native launching fails, the launcher reports failure; it never executes the entry’s `Exec` text through a shell.

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

To bind Simple Launcher to a global shortcut (such as `SUPER + SPACE`), add the following to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER, SPACE", "Simple Launcher", "~/.config/omarchy/plugins/ubruckhaus.simple-launcher-for-fools/launcher-toggle")
```

Or for `SUPER + CTRL + ALT + SPACE`:

```lua
o.bind("SUPER + CTRL + ALT + SPACE", "Simple Launcher", "~/.config/omarchy/plugins/ubruckhaus.simple-launcher-for-fools/launcher-toggle")
```

## App Shortcuts

Open an app's actions menu and choose **Shortcut…** (or press `Ctrl+K`). The top of the page lists the app's shortcuts, with a tag showing where each comes from (`Launcher` or your config file). **×** removes a launcher shortcut, or turns off a shortcut from your config; turned-off ones stay listed with **Turn on**. Below, under **Add a shortcut**, toggle modifiers and press a key, or click one of the free keys listed for the selected modifiers. Free keys are listed alphabetically. Apps with a shortcut start with its modifiers; others start with your default (Super + Shift), which the **Default for apps without a shortcut** checkbox changes (saved in `~/.config/applauncher/settings.json`). **Assign** stays disabled until you choose a key.

| Status | Meaning | Action |
| --- | --- | --- |
| Free (green) | Nothing uses the combo | **Assign** |
| Yours (yellow) | Your own binding or another launcher shortcut uses it | **Replace** / **Move here** |
| Omarchy (red) | An Omarchy default uses it | **Override…**, then **Confirm override** |

Shortcuts are stored in `~/.config/applauncher/shortcuts.json` and written to `~/.config/hypr/simple-launcher-shortcuts.lua`, which is required once at the end of `~/.config/hypr/hyprland.lua` (a backup is kept as `hyprland.lua.before-simple-launcher-shortcuts`). Your other config files are never edited: replacing or unbinding a binding is an `hl.unbind` in the generated file, so removing the shortcut restores the original. Every change is validated with `hyprctl configerrors` and rolled back if Hyprland rejects it.

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
| `Ctrl+K` | Manage the global shortcut of the selected app |
| `Click outside` | Dismiss launcher popup |

## Removal

To remove the plugin:

```bash
omarchy plugin remove ubruckhaus.simple-launcher-for-fools
```

Hidden apps configuration is saved in `~/.config/applauncher/hidden.json` and will persist across updates.

## License

MIT License. See [LICENSE](LICENSE) for details.
