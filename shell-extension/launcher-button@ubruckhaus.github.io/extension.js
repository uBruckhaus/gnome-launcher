// A top bar button for GNOME Launcher: opens it, or closes it while it is open.
// Launcher switches this extension on and off in its Settings (Top bar icon).

import Clutter from 'gi://Clutter';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const APP_ID = 'io.github.ubruckhaus.Launcher.desktop';

export default class LauncherButtonExtension extends Extension {
    enable() {
        this._button = new PanelMenu.Button(0.5, 'Launcher', true);
        this._button.add_child(new St.Icon({icon_name: 'view-app-grid-symbolic', style_class: 'system-status-icon'}));
        // No menu: a click (or tap) toggles the window.
        this._button.connect('event', (_actor, event) => {
            const type = event.type();
            if (type !== Clutter.EventType.BUTTON_RELEASE && type !== Clutter.EventType.TOUCH_END)
                return Clutter.EVENT_PROPAGATE;
            this.toggle();
            return Clutter.EVENT_STOP;
        });
        Main.panel.addToStatusArea(this.uuid, this._button);
    }

    disable() {
        this._button?.destroy();
        this._button = null;
    }

    // Closing goes through the window's close button, so Launcher remembers its size.
    toggle() {
        const app = Shell.AppSystem.get_default().lookup_app(APP_ID);
        if (!app) {
            Main.notify('Launcher is not installed', 'Run install.sh in its folder.');
            return;
        }
        const windows = app.get_windows();
        if (windows.length)
            windows.forEach(window => window.delete(global.display.get_current_time_roundtrip()));
        else
            app.activate();
    }
}
