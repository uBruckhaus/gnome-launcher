"""English only: the gettext-style helpers stay so the texts remain marked,
but nothing is translated."""


def _(text):
    return text


def N_(text):
    """Marks a module-level constant; used as is."""
    return text


def ngettext(one, other, n):
    return one if n == 1 else other
