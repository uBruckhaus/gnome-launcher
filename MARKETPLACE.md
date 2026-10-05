# Marketplace Publication Listing: Simple Launcher

This document contains the ready-to-use copy, metadata, and feature breakdown for submitting **Simple Launcher** to the [Omarchy Plugin Marketplace](https://plugins.omarchy.org) and `omacom/omarchy-plugin-marketplace`.

---

## 📋 Marketplace Metadata Sheet

- **Plugin ID**: `ubruckhaus.simple-launcher-for-fools`
- **Display Name**: `Simple Launcher`
- **Version**: `1.3.1`
- **Author**: `Uwe Bruckhaus`
- **Category**: `Compositor` *(Alternative: `Utilities`)*
- **Tags**: `launcher`, `bar-widget`, `gtk4`, `wayland`, `apps`, `shortcuts`, `keybindings`, `rebind`
- **Repository**: `https://github.com/ubruckhaus/omarchy-simple-launcher-for-fools`
- **License**: `MIT`
- **Default Bar Section**: `left`

---

## 🏷️ Short Description (Tagline)

> Fast, theme-aware app launcher for Omarchy with an advanced shortcut editor: bind keys to apps or their actions, see who owns every key, and rebind any of them in one click.

---

## 📝 Full Marketplace Description

**Simple Launcher** brings a lightning-fast, distraction-free application launcher to your Omarchy desktop. Designed to fit naturally into the Omarchy aesthetic, it pairs a native Quickshell status-bar widget with a high-performance GTK4 Layer-Shell overlay.

Whether summoned with a click on the status bar or via a global Hyprland keybinding, the launcher appears instantly above all tiled and floating windows. Start typing immediately to filter your tools, jump straight to running applications, or manage distracting helper utilities with built-in app concealment.

**New in 1.3 — advanced shortcuts.** A shortcut can now open an app *or one of its actions*: give Brave's **New Incognito Window** its own key, and the app menu shows it next to the action. The shortcut editor shows every key with its owner — green when free, yellow when one of your bindings has it, orange for an Omarchy default — and taking over a key is a single **Rebind** click; remove the shortcut and the original binding works again. Omarchy's terminal, browser and editor defaults show on your actual default apps, keys like Enter and Space can be bound, duplicates are prevented, and apps with several shortcuts get a `+1` badge. The app list updates live when apps are installed or removed, shortcuts of uninstalled apps are cleaned up, **Uninstall…** works the Omarchy way, and the launcher reloads itself after plugin updates.

New in 1.2: Omarchy's default app shortcuts show up on their apps, every app's shortcut is shown as a badge in the list, terminal apps like `foot -e claude` are tracked as their own app, and the running badge sits on the app icon.

New in 1.1: give any app its own global Hyprland shortcut straight from the launcher. Pick modifiers, press a key or click a free one, and Simple Launcher tells you whether the combination is free, already one of your own bindings, or an Omarchy default — before anything changes.

---

## ✨ Feature Highlights

- 🚀 **Native Omarchy Bar Widget**: Adds a clean 3×3 app grid button directly to the Quickshell bar. Left-click toggles the launcher; right-click reloads application desktop caches.
- ⚡ **Zero-Latency Wayland Overlay**: Powered by `libgtk4-layer-shell`, rendering a centered floating card above all workspaces. Auto-dismisses smoothly when clicking anywhere outside.
- 🎨 **Dynamic Theme Harmony**: Automatically monitors `~/.local/state/omarchy/current/theme/colors.toml`. Accent borders, active selection glow, text contrast, and running indicator dots immediately update whenever your Omarchy theme changes.
- 🔍 **Instant Type-to-Filter Search**: No need to click a search field—simply type any letter to filter applications in real time. Full keyboard navigation with arrows, `Enter`, `Backspace`, and `Escape`.
- 🟢 **Live Window & Process Tracking**: Inspects Hyprland client states and running processes on the fly. Active applications display glowing indicator dots so you know what's already open.
- 📦 **Universal Application Discovery**: Indexes system `.desktop` files, user applications in `~/.local/share/applications`, standalone AppImage binaries, and Chromium/Chrome Progressive Web Apps (PWAs).
- 👁️ **Hidden Apps Management**: Conceal background helpers or clutter from the launcher view with one click. Toggle hidden entries on and off via the header reveal button.
- ⌨️ **Advanced App Shortcuts**: Open an app's menu → **Shortcut…** (or press `Ctrl+K`). The page lists every shortcut that opens the app — launcher-made ones, your own `bindings.lua` binds and Omarchy defaults — with their source; change, remove or turn off any of them.
  - **Shortcuts for actions (new in 1.3)**: pick what a key opens — the app, or one of its desktop actions such as **New Incognito Window** in Brave or Chrome. Omarchy's private-browser default shows on your browser's private-window action.
  - **Every key, coloured by owner (new in 1.3)**: all letters and keys like Enter, Space or Backspace, green when free, yellow for your bindings, orange for Omarchy defaults; hover names the owner.
  - **One-click rebinding (new in 1.3)**: taken keys are an option, not an error — **Rebind** turns the old binding off while your shortcut exists.
  - **Non-destructive**: Your config files are never edited. Shortcuts live in a generated `~/.config/hypr/simple-launcher-shortcuts.lua`; overriding or turning off a binding is an `hl.unbind` there, so removing the shortcut brings the original back. Every change is checked with `hyprctl configerrors` and rolled back if Hyprland rejects it.
- 🔄 **Always Current (new in 1.3)**: The app list refreshes live when apps are installed or removed; shortcuts of uninstalled apps are dropped so their keys work again; after a plugin update the launcher restarts itself on the next open.
- 🗑️ **Uninstall (new in 1.3)**: The last item of the app menu asks “Do you want to uninstall …?” and hands over to Omarchy's own `omarchy-remove-launcher-entry`.
- 🪶 **Lightweight & Self-Contained**: Runs on standard system Python, GIO, and GTK4. Zero heavy virtual environments, background node daemons, or bloated runtime dependencies.

---

## ⌨️ Controls & Keyboard Shortcuts

| Shortcut | Function |
| :--- | :--- |
| **Any letter / digit** | Instant fuzzy filtering as you type |
| **Up / Down** | Navigate selection through app results |
| **Page Up / Page Down** | Jump 6 entries up or down |
| **Home / End** | Jump directly to top or bottom of the list |
| **Enter / Return** | Launch selected application or switch to active window |
| **Backspace** | Delete previous search character |
| **Escape** | Clear active search query, or close launcher if search is empty |
| **Right Arrow** | Open context actions menu (for apps declaring desktop actions) |
| **Delete** | Close active running window of the selected application |
| **Ctrl + K** | Manage the global shortcut of the selected application |
| **Click outside** | Instantly dismiss launcher |

---

## 💻 Installation

```bash
omarchy plugin add https://github.com/ubruckhaus/omarchy-simple-launcher-for-fools.git --enable
```
