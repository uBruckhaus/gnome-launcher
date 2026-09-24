# Marketplace Publication Listing: Simple Launcher for Fools

This document contains the ready-to-use copy, metadata, and feature breakdown for submitting **Simple Launcher for Fools** to the [Omarchy Plugin Marketplace](https://plugins.omarchy.org) and `omacom/omarchy-plugin-marketplace`.

---

## 📋 Marketplace Metadata Sheet

- **Plugin ID**: `ubruckhaus.simple-launcher-for-fools`
- **Display Name**: `Simple Launcher for Fools`
- **Version**: `1.0.0`
- **Author**: `Uwe Bruckhaus`
- **Category**: `Compositor` *(Alternative: `Utilities`)*
- **Tags**: `launcher`, `bar-widget`, `gtk4`, `wayland`, `apps`
- **Repository**: `https://github.com/ubruckhaus/omarchy-simple-launcher-for-fools`
- **License**: `MIT`
- **Default Bar Section**: `left`

---

## 🏷️ Short Description (Tagline)

> Fast, keyboard-driven application launcher popup and native Quickshell bar widget for Omarchy, powered by GTK4 Layer Shell with automatic theme harmony.

---

## 📝 Full Marketplace Description

**Simple Launcher for Fools** brings a lightning-fast, distraction-free application launcher to your Omarchy desktop. Designed to fit naturally into the Omarchy aesthetic, it pairs a native Quickshell status-bar widget with a high-performance GTK4 Layer-Shell overlay.

Whether summoned with a click on the status bar or via a global Hyprland keybinding, the launcher appears instantly above all tiled and floating windows. Start typing immediately to filter your tools, jump straight to running applications, or manage distracting helper utilities with built-in app concealment.

---

## ✨ Feature Highlights

- 🚀 **Native Omarchy Bar Widget**: Adds a clean 3×3 app grid button directly to the Quickshell bar. Left-click toggles the launcher; right-click reloads application desktop caches.
- ⚡ **Zero-Latency Wayland Overlay**: Powered by `libgtk4-layer-shell`, rendering a centered floating card above all workspaces. Auto-dismisses smoothly when clicking anywhere outside.
- 🎨 **Dynamic Theme Harmony**: Automatically monitors `~/.local/state/omarchy/current/theme/colors.toml`. Accent borders, active selection glow, text contrast, and running indicator dots immediately update whenever your Omarchy theme changes.
- 🔍 **Instant Type-to-Filter Search**: No need to click a search field—simply type any letter to filter applications in real time. Full keyboard navigation with arrows, `Enter`, `Backspace`, and `Escape`.
- 🟢 **Live Window & Process Tracking**: Inspects Hyprland client states and running processes on the fly. Active applications display glowing indicator dots so you know what's already open.
- 📦 **Universal Application Discovery**: Indexes system `.desktop` files, user applications in `~/.local/share/applications`, standalone AppImage binaries, and Chromium/Chrome Progressive Web Apps (PWAs).
- 👁️ **Hidden Apps Management**: Conceal background helpers or clutter from the launcher view with one click. Toggle hidden entries on and off via the header reveal button.
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
| **Click outside** | Instantly dismiss launcher |

---

## 💻 Installation

```bash
omarchy plugin add https://github.com/ubruckhaus/omarchy-simple-launcher-for-fools.git --enable
```
