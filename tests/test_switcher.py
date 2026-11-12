"""Pure transformations and failure/concurrency behaviour; no desktop required."""
import ast
from contextlib import redirect_stderr
import fcntl
import importlib.util
import io
import os
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

    def test_clipboard_restored_if_copy_or_paste_fails(self):
        for action in (switcher.get_selected_text, lambda: switcher.paste_text('converted')):
            clipboard = SimpleNamespace(value='previous clipboard')
            clipboard.paste = lambda: clipboard.value
            clipboard.copy = lambda text: setattr(clipboard, 'value', text)
            keyboard = Mock()
            keyboard.hotkey.side_effect = RuntimeError('desktop error')
            with patch.object(switcher, 'desktop_tools', return_value=(keyboard, clipboard)), \
                 patch.object(switcher.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'desktop error'):
                    action()
                self.assertEqual(clipboard.value, 'previous clipboard')

    def test_missing_extension_does_not_replace_text_and_releases_keys(self):
        with patch.object(switcher, 'desktop_tools'), \
             patch.object(switcher.time, 'sleep'), \
             patch.object(switcher, 'get_selected_text', return_value='ghbdtn'), \
             patch.object(switcher, 'set_input_source', side_effect=RuntimeError('module unavailable')), \
             patch.object(switcher, 'paste_text') as paste, \
             patch.object(switcher, 'release_modifiers') as release:
            with self.assertRaisesRegex(RuntimeError, 'module unavailable'):
                switcher.process_selection(False)
            paste.assert_not_called()
            release.assert_called_once()

    def test_case_does_not_change_language(self):
        with patch.object(switcher, 'desktop_tools'), \
             patch.object(switcher.time, 'sleep'), \
             patch.object(switcher, 'get_selected_text', return_value='Привет'), \
             patch.object(switcher, 'set_input_source') as source, \
             patch.object(switcher, 'paste_text') as paste, \
             patch.object(switcher, 'release_modifiers'):
            switcher.process_selection(True)
            source.assert_not_called()
            paste.assert_called_once_with('привет')

    def test_no_selection_is_noop(self):
        with patch.object(switcher, 'desktop_tools'), \
             patch.object(switcher.time, 'sleep'), \
             patch.object(switcher, 'get_selected_text', return_value=''), \
             patch.object(switcher, 'set_input_source') as source, \
             patch.object(switcher, 'paste_text') as paste, \
             patch.object(switcher, 'release_modifiers') as release:
            switcher.process_selection(False)
            source.assert_not_called()
            paste.assert_not_called()
            release.assert_called_once()

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
                    switcher.current_input_source()
        with patch.object(switcher.subprocess, 'run', return_value=SimpleNamespace(stdout="('ru',)")):
            self.assertEqual(switcher.current_input_source(), 'ru')


if __name__ == '__main__':
    unittest.main()
