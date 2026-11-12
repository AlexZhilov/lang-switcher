"""Only the X11 keyboard operations needed for copying and pasting text."""
import time


class Keyboard:
    def __init__(self, connection, send_event, keycodes):
        self.connection = connection
        self.send_event = send_event
        self.keycodes = keycodes

    @classmethod
    def connect(cls):
        from Xlib import X, XK, display
        from Xlib.ext.xtest import fake_input

        connection = display.Display()
        names = {'ctrl': 'Control_L', 'ctrlleft': 'Control_L',
                 'ctrlright': 'Control_R', 'shift': 'Shift_L', 'c': 'c', 'v': 'v'}
        keycodes = {key: connection.keysym_to_keycode(XK.string_to_keysym(name))
                    for key, name in names.items()}
        if not all(keycodes.values()):
            connection.close()
            raise RuntimeError('Не найдены нужные клавиши X11')

        def send_event(pressed, keycode):
            fake_input(connection, X.KeyPress if pressed else X.KeyRelease, keycode)

        return cls(connection, send_event, keycodes)

    def hotkey(self, *keys):
        if keys not in (('ctrl', 'c'), ('ctrl', 'v')):
            raise ValueError('Поддерживаются только Ctrl+C и Ctrl+V')
        codes = [self.keycodes[key] for key in keys]
        try:
            for code in codes:
                self.send_event(True, code)
        finally:
            for code in reversed(codes):
                self.send_event(False, code)
            self.connection.sync()
        # Keep the proven clipboard timing of the original script.
        time.sleep(0.1)

    def keyUp(self, key, _pause=True):
        if key not in ('ctrl', 'ctrlleft', 'ctrlright', 'shift'):
            raise ValueError('Поддерживается только отпускание Ctrl/Shift')
        self.send_event(False, self.keycodes[key])
        self.connection.sync()
        if _pause:
            time.sleep(0.1)
