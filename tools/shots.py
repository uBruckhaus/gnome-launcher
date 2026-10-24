"""Drives GNOME Launcher through its pages and saves a PNG of the window
after each step. Run by tools/headless-shots.sh, never in a real session."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402
from launcher import GLib, Gtk  # noqa: E402

OUT = Path(sys.argv[1])


def save(widget, name):
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, widget.get_width(), widget.get_height())
    node = snapshot.to_node()
    if node is None:
        print("empty:", name)
        return
    texture = widget.get_native().get_renderer().render_texture(node, None)
    texture.save_to_png(str(OUT / f"{name}.png"))
    print("saved", name)


def snap(app, name):
    save(app.window, name)
    # A menu is a surface of its own: saved beside the window.
    popover = app.actions_popover
    if popover is not None and popover.get_visible():
        save(popover, f"{name}-menu")


def first_row(app):
    row = app.rows.get_first_child()
    while row is not None and not hasattr(row, "app"):
        row = row.get_next_sibling()
    return row


def steps(app):
    first_app = next(iter(app.apps), None)
    yield "list-narrow", lambda: app.window.set_default_size(400, 760)
    yield "folders-wide", lambda: app.window.set_default_size(1100, 760)
    yield "cards-wide", lambda: app.set_wide_layout("cards")
    yield "groups-narrow", lambda: (app.set_wide_layout("folders"), app.window.set_default_size(400, 760),
                                    app.toggle_view("groups"))
    yield "origin-narrow", lambda: app.toggle_view("origin")
    yield "list-again", lambda: app.toggle_view("list")
    yield "menu", lambda: app.show_actions(first_row(app).actions_button, first_row(app).app)
    yield "settings", lambda: (app.actions_popover.popdown(), app.open_settings_page())
    yield "details", lambda: (app.close_settings_page(), app.open_details_page(first_app))
    yield "shortcut", lambda: (app.close_aux_page(), app.open_shortcut_page(first_app))
    yield "shortcut-key", lambda: app.pick_shortcut_key(["SUPER"], "A")
    yield "legend", lambda: (app.close_shortcut_page(), app.legend_button.popup())


def main():
    app = launcher.Launcher()
    plan = None

    def advance():
        nonlocal plan
        if plan is None:
            plan = steps(app)
        try:
            name, action = next(plan)
        except StopIteration:
            app.quit()
            return GLib.SOURCE_REMOVE
        try:
            action()
        except Exception as error:  # Report and go on: one broken page must not hide the others.
            print(f"step {name} failed: {error!r}")
        GLib.timeout_add(900, lambda: (snap(app, name), GLib.SOURCE_REMOVE)[1])
        return GLib.SOURCE_CONTINUE

    app.connect("activate", lambda *_a: GLib.timeout_add(1500, lambda: (GLib.timeout_add(1200, advance),
                                                                          GLib.SOURCE_REMOVE)[1]))
    app.hide = lambda: None  # Stay open between the steps.
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
