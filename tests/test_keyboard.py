import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

spec = importlib.util.spec_from_file_location('keyboard_x11', Path(__file__).resolve().parents[1] / 'payload/keyboard_x11.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class KeyboardTests(unittest.TestCase):
    def keyboard(self, send):
        return module.Keyboard(Mock(), send, {'ctrl': 1, 'c': 2, 'v': 3, 'shift': 4})

    def test_chords_release_every_key_and_keep_clipboard_timing(self):
        events = []
        keyboard = self.keyboard(lambda pressed, code: events.append((pressed, code)))
        with patch.object(module.time, 'sleep') as sleep:
            keyboard.hotkey('ctrl', 'c')
        self.assertEqual(events, [(True, 1), (True, 2), (False, 2), (False, 1)])
        keyboard.connection.sync.assert_called_once()
        sleep.assert_called_once_with(.1)

    def test_press_failure_still_releases_modifiers(self):
        events = []
        def send(pressed, code):
            events.append((pressed, code))
            if pressed and code == 2:
                raise RuntimeError('send error')
        with self.assertRaisesRegex(RuntimeError, 'send error'):
            self.keyboard(send).hotkey('ctrl', 'c')
        self.assertEqual(events[-2:], [(False, 2), (False, 1)])

    def test_unrelated_shortcuts_are_not_supported(self):
        send = Mock()
        keyboard = self.keyboard(send)
        with self.assertRaises(ValueError):
            keyboard.hotkey('alt', 'tab')
        with self.assertRaises(ValueError):
            keyboard.keyUp('v')
        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
