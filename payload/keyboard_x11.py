"""Only the X11 keyboard operations needed for copying and pasting text."""
import sys
import time

MODIFIER_KEYS = (
    'ctrl', 'ctrlright', 'shift', 'shiftright',
    'alt', 'altright', 'super', 'superright',
)
MODIFIER_TIMEOUT = 0.8
POLL_INTERVAL = 0.005


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
        names = {'ctrl': 'Control_L',
                 'ctrlright': 'Control_R', 'shift': 'Shift_L',
                 'shiftright': 'Shift_R', 'alt': 'Alt_L', 'altright': 'Alt_R',
                 'super': 'Super_L', 'superright': 'Super_R', 'c': 'c', 'v': 'v'}
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
        attempted = []
        try:
            for code in codes:
                # A transport failure may happen after delivery to X11.
                attempted.append(code)
                self.send_event(True, code)
        finally:
            original_error = sys.exc_info()[1]
            cleanup_errors = []
            for code in reversed(attempted):
                try:
                    self.send_event(False, code)
                except Exception as error:
                    cleanup_errors.append(error)
            try:
                self.connection.sync()
            except Exception as error:
                cleanup_errors.append(error)
            if cleanup_errors and original_error is None:
                raise cleanup_errors[0]

    def wait_for_modifiers_release(self, timeout=MODIFIER_TIMEOUT):
        """Do not turn Ctrl+C/V into Ctrl+Shift+C/V while the shortcut is held."""
        deadline = time.monotonic() + timeout
        while True:
            state = self.connection.query_keymap()
            if not any(state[self.keycodes[key] // 8] &
                       (1 << (self.keycodes[key] % 8)) for key in MODIFIER_KEYS):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('Отпустите клавиши сочетания и повторите')
            time.sleep(POLL_INTERVAL)
