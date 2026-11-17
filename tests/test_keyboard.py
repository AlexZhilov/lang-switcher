import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

spec = importlib.util.spec_from_file_location('keyboard_x11', Path(__file__).resolve().parents[1] / 'payload/keyboard_x11.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class KeyboardTests(unittest.TestCase):
    def keyboard(self, send):
        return module.Keyboard(Mock(), send, {
            'ctrl': 1, 'c': 2, 'v': 3, 'shift': 4, 'ctrlright': 5,
            'shiftright': 6, 'alt': 7, 'altright': 8,
            'super': 9, 'superright': 10,
        })

    def test_chords_release_every_key_without_fixed_delay(self):
        events = []
        keyboard = self.keyboard(lambda pressed, code: events.append((pressed, code)))
        with patch.object(module.time, 'sleep') as sleep:
            keyboard.hotkey('ctrl', 'c')
        self.assertEqual(events, [(True, 1), (True, 2), (False, 2), (False, 1)])
        keyboard.connection.sync.assert_called_once()
        sleep.assert_not_called()

    def test_press_failure_still_releases_modifiers(self):
        events = []
        def send(pressed, code):
            events.append((pressed, code))
            if pressed and code == 2:
                raise RuntimeError('send error')
        with self.assertRaisesRegex(RuntimeError, 'send error'):
            self.keyboard(send).hotkey('ctrl', 'c')
        self.assertEqual(events[-2:], [(False, 2), (False, 1)])

    def test_release_failure_still_releases_other_keys_and_syncs(self):
        events = []
        def send(pressed, code):
            events.append((pressed, code))
            if not pressed and code == 2:
                raise RuntimeError('release failed')
        keyboard = self.keyboard(send)
        with self.assertRaisesRegex(RuntimeError, 'release failed'):
            keyboard.hotkey('ctrl', 'c')
        self.assertEqual(events[-2:], [(False, 2), (False, 1)])
        keyboard.connection.sync.assert_called_once()

    def test_cleanup_error_does_not_hide_press_failure(self):
        events = []
        def send(pressed, code):
            events.append((pressed, code))
            if pressed and code == 2:
                raise ValueError('press failed')
            if not pressed and code == 2:
                raise RuntimeError('release failed')
        keyboard = self.keyboard(send)
        with self.assertRaisesRegex(ValueError, 'press failed'):
            keyboard.hotkey('ctrl', 'c')
        self.assertEqual(events[-1], (False, 1))
        keyboard.connection.sync.assert_called_once()

    def test_unattempted_key_is_not_released(self):
        events = []
        def send(pressed, code):
            events.append((pressed, code))
            if pressed:
                raise RuntimeError('first press failed')
        with self.assertRaisesRegex(RuntimeError, 'first press failed'):
            self.keyboard(send).hotkey('ctrl', 'v')
        self.assertEqual(events, [(True, 1), (False, 1)])

    def test_sync_failure_is_reported(self):
        keyboard = self.keyboard(Mock())
        keyboard.connection.sync.side_effect = RuntimeError('sync failed')
        with self.assertRaisesRegex(RuntimeError, 'sync failed'):
            keyboard.hotkey('ctrl', 'v')

    def test_wait_returns_immediately_when_modifiers_are_up(self):
        keyboard = self.keyboard(Mock())
        keyboard.connection.query_keymap.return_value = [0] * 32
        with patch.object(module.time, 'sleep') as sleep:
            keyboard.wait_for_modifiers_release()
        sleep.assert_not_called()

    def test_wait_checks_both_sides_and_stops_on_release(self):
        for modifier in ('ctrl', 'ctrlright', 'shift', 'shiftright',
                         'alt', 'altright', 'super', 'superright'):
            keyboard = self.keyboard(Mock())
            code = keyboard.keycodes[modifier]
            held = [0] * 32
            held[code // 8] = 1 << (code % 8)
            keyboard.connection.query_keymap.side_effect = [held, [0] * 32]
            with patch.object(module.time, 'sleep') as sleep:
                keyboard.wait_for_modifiers_release()
            sleep.assert_called_once_with(.005)
            keyboard.send_event.assert_not_called()

    def test_held_modifier_times_out_without_synthetic_release(self):
        keyboard = self.keyboard(Mock())
        keyboard.connection.query_keymap.return_value = [255] * 32
        with patch.object(module.time, 'monotonic', side_effect=[0, .8]):
            with self.assertRaisesRegex(RuntimeError, 'Отпустите'):
                keyboard.wait_for_modifiers_release()
        keyboard.send_event.assert_not_called()

    def test_unrelated_shortcuts_are_not_supported(self):
        send = Mock()
        keyboard = self.keyboard(send)
        with self.assertRaises(ValueError):
            keyboard.hotkey('alt', 'tab')
        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
