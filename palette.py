"""App Manager - Theme colors: Omarchy's theme, or Adwaita's (desktop.py)."""

import desktop

DEFAULT_COLORS = {
    "background": "#1e1e2e",
    "foreground": "#cdd6f4",
    "accent": "#89b4fa",
    "selection": "#45475a",
    "muted": "#888894",
    "red": "#f04452",
    "yellow": "#f9e2af",
    "orange": "#fab387",
    "green": "#a6e3a1",
}


class ThemePalette:
    """Expose the current theme colors to the launcher: the Omarchy theme's,
    or libadwaita's (other desktops, or Settings › Look on Omarchy)."""

    def __init__(self):
        self.colors = DEFAULT_COLORS.copy()
        self.reload()

    def reload(self):
        try:
            if desktop.adwaita():
                import desktop_generic
                colors = desktop_generic.theme_colors()
            else:
                colors = desktop.theme_colors()
        except (OSError, ValueError):
            colors = DEFAULT_COLORS.copy()
        colors = {key: colors.get(key, fallback) for key, fallback in DEFAULT_COLORS.items()}
        if colors == self.colors:
            return False
        self.colors = colors
        return True
