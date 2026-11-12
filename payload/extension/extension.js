'use strict';

const { Gio } = imports.gi;
const Main = imports.ui.main;
const Keyboard = imports.ui.status.keyboard;

const IFACE = `<node>
  <interface name="org.gnome.Shell.Extensions.ZorinSwitcher">
    <method name="GetLayout"><arg type="s" direction="out"/></method>
    <method name="SetLayout">
      <arg type="s" direction="in"/>
      <arg type="s" direction="out"/>
    </method>
  </interface>
</node>`;

class DirectInput {
    enable() {
        this._export = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._export.export(Gio.DBus.session,
            '/org/gnome/Shell/Extensions/ZorinSwitcher');
    }

    disable() {
        if (this._export) {
            this._export.unexport();
            this._export = null;
        }
    }

    GetLayout() {
        const manager = Keyboard.getInputSourceManager();
        return manager.currentSource ? manager.currentSource.id : '';
    }

    SetLayout(language) {
        if (Main.sessionMode.isLocked || Main.sessionMode.isGreeter)
            throw new Error('Unlock the session first');
        if (language !== 'us' && language !== 'ru')
            throw new Error('Only existing US and RU sources are supported');

        const manager = Keyboard.getInputSourceManager();
        const current = manager.currentSource;
        if (!current || current.type !== 'xkb')
            throw new Error('Current input source must be XKB');
        const source = Object.values(manager.inputSources).find(
            item => item.type === 'xkb' && item.id === language);
        if (!source)
            throw new Error(`Input source ${language} is not configured`);

        // GNOME 43 uses the SAME xkb:us::eng IBus engine for every XKB
        // source. Switching US <-> RU only needs its keyboard manager.
        // Keep the manager cache, indicator, MRU and per-window state in
        // sync, without activateInputSource()'s keyboard grab and IBus wait.
        if (current !== source) {
            manager.keyboardManager.apply(source.xkbId);
            manager._currentInputSourceChanged(source);
            manager._updateMruSettings();
        }
        return manager.currentSource.id;
    }
}

function init() {
    return new DirectInput();
}
