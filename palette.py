"""GNOME Launcher - Theme colors: libadwaita's (desktop.py)."""

import desktop

DEFAULT_COLORS = {
    "background": "#fafafb",
    "foreground": "#323233",
    "accent": "#3584e4",
    "selection": "#dedddf",
    "muted": "#77767b",
    "red": "#e01b24",
    "yellow": "#c88800",
    "orange": "#e66100",
    "green": "#26a269",
}


class ThemePalette:
    """Expose the system style's colors to the launcher."""

    def __init__(self):
        self.colors = DEFAULT_COLORS.copy()
        self.reload()

    def reload(self):
        try:
            colors = desktop.theme_colors()
        except (OSError, ValueError):
            colors = DEFAULT_COLORS.copy()
        colors = {key: colors.get(key, fallback) for key, fallback in DEFAULT_COLORS.items()}
        if colors == self.colors:
            return False
        self.colors = colors
        return True
