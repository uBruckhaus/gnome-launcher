# Simple Launcher

Fast, theme-aware application launcher popup and bar widget for [Omarchy](https://omarchy.org) — with an advanced shortcut editor: bind keys to apps **or their actions**, see who owns every key, and rebind any of them in one click.

![Simple Launcher](preview.png)

## Advanced shortcuts

Open an app's menu (`☰`, `→`) → **Shortcut…**, or press `Ctrl+K`.

- **Shortcuts for app actions**: a key can open the app or one of its own desktop actions. Example: give Brave's **New Incognito Window** `Super + Shift + Alt + B`. The `☰` menu shows each action's shortcut next to it.
- **Every key, coloured by owner**: with modifiers selected, all letters and keys like Enter or Space are shown — green when free, yellow when one of your bindings has it, orange for an Omarchy default.
- **One-click rebinding**: taking over a key is an option, not an error. **Rebind** turns the old binding off while your shortcut exists; remove the shortcut and the original works again.
- **All shortcuts of an app in one place**: launcher-made ones, your own `bindings.lua` binds and Omarchy defaults (including your default terminal, browser and editor, e.g. `Super + Enter` → Foot). Change, remove or turn off any of them; a `+1` badge in the list shows apps with more than one.
- **No duplicates, no edits to your files**: a key that already opens the same thing is never added twice. Everything lives in a generated `~/.config/hypr/simple-launcher-shortcuts.lua`, validated with `hyprctl configerrors` and rolled back if Hyprland rejects it.

## Features

- **Omarchy Bar Widget**: Sits natively in the Quickshell top bar as an app-grid button. Click to toggle the launcher on and off.
- **Wayland Layer-Shell Overlay**: Built on GTK4 Layer Shell for zero-latency, buttery-smooth appearance above all windows.
- **Theme Harmony**: Automatically loads and syncs with Omarchy's active theme palette (`~/.local/state/omarchy/current/theme/colors.toml`). Accent colors, selections, and background match your desktop styling dynamically.
- **Instant Search**: Start typing anywhere to immediately filter applications. Supports backspace and Escape to clear.
- **Running App Tracking**: Inspects Hyprland client states and running processes in real-time. A badge on the app icon shows green for open windows and amber for apps running in the background, so shortcut badges stay aligned. Terminal apps such as `foot -e claude` count as their own app, not as the terminal.
- **PWA & AppImage Support**: Automatically scans `.desktop` files, Chromium/Chrome Progressive Web Apps (PWAs), and standalone AppImage executables.
- **Hidden Apps Management**: Conceal distracting helper tools or background daemons from the launcher view with one click, or toggle visibility with the top-right eye icon.
- **Uninstall**: The last item of an app's actions menu asks “Do you want to uninstall …?” like Omarchy's menu, then runs `omarchy-remove-launcher-entry`, which removes web apps and TUIs, deletes your own launcher entries, and uninstalls packages (pacman, in a terminal) or Flatpaks.
- **Always Current**: Apps are re-read on every open, and the open list refreshes when apps are installed or removed. Shortcuts of uninstalled apps are dropped (with a notification), so their keys work again. After a plugin update the launcher restarts itself with the new code on the next open.
- **App Shortcuts**: Bind a global Hyprland shortcut to any app from its actions menu (`Shortcut…` or `Ctrl+K`). Every key shows who has it; taken keys — your own bindings or Omarchy defaults — can simply be rebound, and come back when the shortcut is removed.
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

Open an app's actions menu and choose **Shortcut…** (or press `Ctrl+K`). The top of the page lists the app's shortcuts, with a tag showing where each comes from (`Launcher`, your config file, or `Omarchy` for active Omarchy defaults such as `Super + Shift + Y` for YouTube or `Super + Shift + F` for Files). The same shortcuts appear as a badge next to each app in the list. **×** removes a launcher shortcut, or turns off a shortcut from your config; turned-off ones stay listed with **Turn on**. Below, under **Add a shortcut**, toggle modifiers and press a key, or click a letter: with modifiers selected, every letter A–Z is shown, green when free, yellow when your own binding or another launcher shortcut has it, orange for an Omarchy default (the tooltip names it) — taken keys can be rebound too. The **Other keys** are coloured the same way. Without modifiers, a few free combinations are suggested. Apps with a shortcut start with its modifiers; others start with your default (Super + Shift), which the **Default for apps without a shortcut** checkbox changes (saved in `~/.config/applauncher/settings.json`). **Assign** stays disabled until you choose a key. Non-letter keys work too: press them with the modifiers held (e.g. `Super + Ctrl + Enter`; Enter alone assigns), or click one under **Other keys** (Enter, Space, Backspace, Delete, Home, End) to combine it with the selected modifiers.

A shortcut can open the app or one of its own desktop actions, such as **New Incognito Window** in Chrome or Brave: pick it under **Opens** (shown for apps that have actions). Omarchy defaults that open your default terminal, browser or editor (`Super + Enter`, `Super + Shift + Enter`, `Super + Shift + N`) belong to that app, and `Super + Shift + Alt + B` belongs to the default browser's private window. In the list, an app shows its main shortcut plus a **+N** badge for the rest; the actions menu shows each action's shortcut next to it. Click a shortcut (or its pencil) to **Change** its key or what it opens. A key that already opens the same thing is never added twice, and a launcher shortcut that only repeats a binding from your config is marked as a duplicate.

| Status | Meaning | Action |
| --- | --- | --- |
| Free (green) | Nothing uses the combo | **Assign** |
| Yours (yellow) | Your own binding uses it; it is off while the shortcut exists | **Rebind** |
| Another app (yellow) | Another launcher shortcut uses it | **Move here** |
| Omarchy (orange) | An Omarchy default uses it; it is off while the shortcut exists | **Rebind** |

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
