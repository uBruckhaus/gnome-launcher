# Launcher (GNOME Launcher)

All your apps in one GNOME window: **launch, group, bind, inspect.** Type to
filter, sort apps into your own groups or by origin, give any of them a global
key on a key map that shows who owns every key, pin them to the Dash, and look
up where each one lives and which package it came from.

A GTK 4 / libadwaita app for GNOME (tested on GNOME 51, Wayland). It is a
fork of the Omarchy plugin *App Manager*, rebuilt for GNOME; local and not
published.

## Install

Needs Python 3, PyGObject, GTK 4 and libadwaita (all part of a GNOME desktop).

```sh
./install.sh              # desktop entry and icon in ~/.local/share
./install.sh --uninstall  # removes both; settings stay in ~/.config/gnome-launcher
```

It runs from this folder: after `git pull` the next start uses the new code.
Start it from the app grid ("Launcher"). To open it with a key, open it once,
then **its own menu › Shortcut…**.

## What it does

- **Three views when narrow:** one list, groups (Office, Internet, Media, …,
  your own), or **by origin**: your default apps, installed system-wide,
  Flatpak, and your own (entries in your home, AppImages, web apps,
  command-line tools).
- **Folders or cards when wide:** widen the window and each group becomes a
  folder of icons (or every app a card), with a group bar at the bottom.
  Drag apps between groups, drag groups to reorder them.
- **Global shortcuts, the GNOME way.** A shortcut is a GNOME custom
  keybinding (you also see it in Settings › Keyboard › Custom Shortcuts). The
  key map colours every key: free, yours, or a GNOME default. Taking a key
  from GNOME (for example Super + A) turns that binding off while your
  shortcut exists; removing your shortcut gives the key back. A shortcut can
  open the app or one of its own actions (a private window, a new terminal).
  GNOME's Super + 1…9 for apps pinned to the Dash are shown as the app's
  shortcuts too.
- **Pin to Dash / Unpin from Dash** in every app's menu; pinned apps carry a
  small pin, and pins made elsewhere show up at once.
- **Updates:** apps whose package has a newer version (`checkupdates`, and
  `yay`/`paru` for the AUR) or whose Flatpak has one get an Update mark.
  A Flatpak updates on its own in the background; a package opens the full
  system update in a terminal (GNOME Software's update page when installed),
  because Arch never updates one package alone.
- **Uninstall…:** a Flatpak with `flatpak uninstall`, a package with pacman in
  a terminal, your own desktop entry or AppImage to the trash; for a browser
  web app it says how to remove it in the browser.
- **Quit** ends a running app (SIGTERM). GNOME does not tell other apps which
  windows are open, so "running" means its process runs.
- **Details…:** type, origin, default-app roles, desktop file, program,
  package, version, size, install date and website, with copy buttons.
- **Change icon…:** icons from three open icon collections (searched online
  only when you ask) or your installed icon themes.
- **Newly installed** apps get a ★ for a week or until you open them.
- **Open as administrator** (off until switched on in Settings; asks for the
  password, off again at the next login).

Keys: type to filter, arrows and Enter, `Ctrl+G` for the next view or group,
`Ctrl+K` for the shortcut page, `Delete` to quit the selected app, `Esc` to
go back or close.

## Where things are kept

| What | Where |
| --- | --- |
| Settings, groups, hidden apps, shortcuts, new-app marks | `~/.config/gnome-launcher/` |
| Your custom icons | `~/.local/share/gnome-launcher/icons/` |
| Icon search index, AppImage icons | `~/.cache/gnome-launcher/` |
| The shortcuts themselves | GNOME's custom keybindings (`…/custom-keybindings/gnome-launcher-N/`) |

## Network

Nothing is sent about you. The network is used only for update checks
(`checkupdates`, `yay`/`paru -Qua`, `flatpak remote-ls --updates`, at most
every 30 minutes while the window is open) and, when you search in
**Change icon…**, for the icon lists and icons on `cdn.jsdelivr.net`.

## Development

```sh
python3 -m unittest discover -s tests   # no display needed
tools/headless-shots.sh shots           # screenshots from a throwaway headless GNOME Shell
SCHEME=prefer-dark tools/headless-shots.sh shots
```

The headless run has its own HOME, D-Bus and dconf, so it never touches your
session or your keybindings. The shortcut tests run against GNOME's real
schemas in memory (`GSETTINGS_BACKEND=memory`) for the same reason.

English only; the `_()` wrappers stay but nothing is translated.
