"""Pure transformations and failure/concurrency behaviour; no desktop required."""
import ast
from contextlib import contextmanager, redirect_stderr
import fcntl
import importlib.util
import io
import os
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
PATH = Path(os.environ.get('LANG_SWITCHER_MODULE', ROOT / 'payload' / 'switcher.py'))
spec = importlib.util.spec_from_file_location('switcher', PATH)
switcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(switcher)


class SwitcherTests(unittest.TestCase):
    def test_language_and_text_agree_after_indentation(self):
        for original, expected, language in (
            ('ghbdtn', 'привет', 'ru'), ('руддщ', 'hello', 'us'),
            ('  \n  ghbdtn', '  \n  привет', 'ru'),
            ('123    руддщ', '123    hello', 'us'),
            ('GHBDTN', 'ПРИВЕТ', 'ru'), ('', '', 'us'),
        ):
            with self.subTest(original=original):
                self.assertEqual(switcher.target_layout(original), language)
                self.assertEqual(switcher.switch_layout(original), expected)

    def test_translation_preserves_original_punctuation_and_mixed_text(self):
        path = ROOT / 'tests' / 'fixtures' / 'original_transform.py'
        tree = ast.parse(path.read_text())
        nodes = [node for node in tree.body if
                 (isinstance(node, ast.FunctionDef) and node.name == 'switch_layout') or
                 (isinstance(node, ast.Assign) and any(
                     isinstance(t, ast.Name) and t.id in ('layout_01', 'layout_02') for t in node.targets))]
        original = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), original)
        alphabet = switcher.EN_LAYOUT + switcher.RU_LAYOUT + ' 123\n😀'
        rng = random.Random(0)
        for text in [alphabet, alphabet[::-1]] + [
            ''.join(rng.choices(alphabet, k=100)) for _ in range(100)
        ]:
            english = sum(c in switcher.EN_LAYOUT for c in text[:3])
            russian = sum(c not in switcher.EN_LAYOUT and c in switcher.RU_LAYOUT for c in text[:3])
            target = 'ru' if english > russian else 'us'
            self.assertEqual(switcher.switch_layout(text, target), original['switch_layout'](text))

    def test_case_semantics(self):
        for text, expected in [('Привет', 'привет'), ('привет', 'ПРИВЕТ'),
                               ('  hELLo', '  HELLO'), ('123!?', '123!?'), ('', '')]:
            self.assertEqual(switcher.toggle_case(text), expected)

    @contextmanager
    def desktop(self, selected='ghbdtn', copy_delay=.02):
        state = SimpleNamespace(now=0.0, value='original', ready=None)
        clipboard, keyboard = Mock(), Mock()

        def copy(text):
            state.value = text

        def paste():
            if state.ready is not None and state.now >= state.ready:
                state.value = selected
                state.ready = None
            return state.value

        def hotkey(*keys):
            if keys == ('ctrl', 'c'):
                state.ready = state.now + copy_delay
            elif keys == ('ctrl', 'v'):
                state.inserted = state.value

        def sleep(delay):
            state.now += delay

        clipboard.copy.side_effect = copy
        clipboard.paste.side_effect = paste
        keyboard.hotkey.side_effect = hotkey
        with patch.object(switcher, 'desktop_tools', return_value=(keyboard, clipboard)), \
             patch.object(switcher.time, 'sleep', side_effect=sleep), \
             patch.object(switcher.time, 'monotonic', side_effect=lambda: state.now):
            yield state, keyboard, clipboard

    def test_selection_ready_early_and_clipboard_restored_once(self):
        for delay in (0, .02, .4):
            with self.subTest(delay=delay), self.desktop(copy_delay=delay) as (state, keyboard, clipboard), \
                 patch.object(switcher, 'set_input_source') as source:
                switcher.process_selection(False)
                self.assertEqual(state.inserted, 'привет')
                self.assertEqual(state.value, 'original')
                self.assertEqual(clipboard.copy.call_args_list,
                                 [call(''), call('привет'), call('original')])
                self.assertLessEqual(state.now, delay + .106)
                keyboard.wait_for_modifiers_release.assert_called_once()
                source.assert_called_once_with('ru')

    def test_case_does_not_change_language(self):
        with self.desktop('Привет') as (state, keyboard, clipboard), \
             patch.object(switcher, 'set_input_source') as source:
            switcher.process_selection(True)
        self.assertEqual(state.inserted, 'привет')
        self.assertEqual(state.value, 'original')
        source.assert_not_called()

    def test_unchanged_text_is_not_reinserted(self):
        for change_case in (True, False):
            with self.subTest(change_case=change_case), self.desktop('123') as (state, keyboard, clipboard), \
                 patch.object(switcher, 'set_input_source') as source:
                switcher.process_selection(change_case)
                keyboard.hotkey.assert_called_once_with('ctrl', 'c')
                self.assertEqual(state.value, 'original')
                self.assertFalse(hasattr(state, 'inserted'))
                if change_case:
                    source.assert_not_called()
                else:
                    source.assert_called_once_with('us')

    def test_copy_timeout_restores_clipboard_and_does_not_paste(self):
        with self.desktop('') as (state, keyboard, clipboard), \
             patch.object(switcher, 'set_input_source') as source:
            switcher.process_selection(False)
        self.assertEqual(state.value, 'original')
        self.assertGreaterEqual(state.now, switcher.COPY_TIMEOUT)
        keyboard.hotkey.assert_called_once_with('ctrl', 'c')
        source.assert_not_called()

    def test_modifier_timeout_does_not_touch_clipboard(self):
        with self.desktop() as (state, keyboard, clipboard):
            keyboard.wait_for_modifiers_release.side_effect = RuntimeError('held')
            with self.assertRaisesRegex(RuntimeError, 'held'):
                switcher.process_selection(False)
        clipboard.copy.assert_not_called()
        clipboard.paste.assert_not_called()
        keyboard.hotkey.assert_not_called()

    def test_extension_failure_restores_original_clipboard(self):
        with self.desktop() as (state, keyboard, clipboard), \
             patch.object(switcher, 'set_input_source', side_effect=RuntimeError('no module')):
            with self.assertRaisesRegex(RuntimeError, 'no module'):
                switcher.process_selection(False)
        self.assertEqual(state.value, 'original')
        keyboard.hotkey.assert_called_once_with('ctrl', 'c')

    def test_copy_and_paste_errors_restore_clipboard(self):
        for chord in (('ctrl', 'c'), ('ctrl', 'v')):
            with self.subTest(chord=chord), self.desktop() as (state, keyboard, clipboard), \
                 patch.object(switcher, 'set_input_source'):
                send = keyboard.hotkey.side_effect
                def fail(*keys):
                    if keys == chord:
                        raise RuntimeError('desktop error')
                    send(*keys)
                keyboard.hotkey.side_effect = fail
                with self.assertRaisesRegex(RuntimeError, 'desktop error'):
                    switcher.process_selection(False)
                self.assertEqual(state.value, 'original')

    def test_restore_failure_does_not_hide_original_error(self):
        clipboard = Mock()
        clipboard.copy.side_effect = RuntimeError('restore failed')
        with redirect_stderr(io.StringIO()) as errors:
            with self.assertRaisesRegex(ValueError, 'original error'):
                with switcher.saved_text_clipboard(clipboard):
                    raise ValueError('original error')
        self.assertIn('Не удалось восстановить', errors.getvalue())

    def test_restore_failure_is_reported_after_success(self):
        clipboard = Mock()
        clipboard.copy.side_effect = RuntimeError('restore failed')
        with self.assertRaisesRegex(RuntimeError, 'restore failed'):
            with switcher.saved_text_clipboard(clipboard):
                pass

    def test_initial_clipboard_read_failure_does_not_write(self):
        clipboard = Mock()
        clipboard.paste.side_effect = RuntimeError('cannot read')
        with self.assertRaisesRegex(RuntimeError, 'cannot read'):
            with switcher.saved_text_clipboard(clipboard):
                self.fail('Should not enter the operation')
        clipboard.copy.assert_not_called()

    def test_cancelled_copy_restores_clipboard(self):
        with self.desktop() as (state, keyboard, clipboard):
            keyboard.hotkey.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                switcher.process_selection(False)
        self.assertEqual(state.value, 'original')

    def test_cancelled_command_returns_130_without_traceback(self):
        with tempfile.TemporaryDirectory() as runtime, \
             patch.dict(os.environ, XDG_RUNTIME_DIR=runtime), \
             patch.object(switcher, 'process_selection', side_effect=KeyboardInterrupt), \
             redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(switcher.main([]), 130)
        self.assertIn('Операция отменена', errors.getvalue())
        self.assertNotIn('Traceback', errors.getvalue())

    def test_overlapping_invocation_is_skipped(self):
        with tempfile.TemporaryDirectory() as runtime, \
             patch.dict(os.environ, XDG_RUNTIME_DIR=runtime), \
             patch.object(switcher, 'process_selection') as process:
            path = Path(runtime) / f'zorin-switcher-{os.getuid()}.lock'
            with path.open('a') as existing:
                fcntl.flock(existing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(switcher.main([]), 0)
                process.assert_not_called()
            self.assertEqual(switcher.main([]), 0)
            process.assert_called_once_with(False)

    def test_errors_return_nonzero_status(self):
        with tempfile.TemporaryDirectory() as runtime, \
             patch.dict(os.environ, XDG_RUNTIME_DIR=runtime), \
             patch.object(switcher, 'process_selection', side_effect=RuntimeError('test failure')), \
             redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(switcher.main([]), 1)
            self.assertIn('test failure', errors.getvalue())

    def test_dbus_response_is_checked(self):
        for response in ('()', '[]', '(123,)', "('ru', 'us')"):
            with patch.object(switcher.subprocess, 'run', return_value=SimpleNamespace(stdout=response)):
                with self.assertRaises(RuntimeError):
                    switcher.direct_input_call('GetLayout')
        with patch.object(switcher.subprocess, 'run', return_value=SimpleNamespace(stdout="('ru',)")):
            self.assertEqual(switcher.direct_input_call('GetLayout'), 'ru')


if __name__ == '__main__':
    unittest.main()
