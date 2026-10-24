"""The "Corners" setting: "windows" (the default) follows the window's own
rounding (libadwaita's 12 px); "rounded" and "square" ignore it."""

CHOICES = ("windows", "rounded", "square")
ROUNDED = 12


def choice(value):
    return value if value in CHOICES else "windows"


def radius(setting, window_rounding):
    """The corner radius in px for this setting and the windows' rounding."""
    setting = choice(setting)
    if setting == "rounded":
        return ROUNDED
    if setting == "square":
        return 0
    return window_rounding if window_rounding is not None else 0
