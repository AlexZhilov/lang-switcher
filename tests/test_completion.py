import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPLETION = ROOT / 'payload/lang-switcher.bash'


class CompletionTests(unittest.TestCase):
    def complete(self, words, mode='main', root='', cwd=None):
        script = r'''
source "$1"
mode=$2
backup_root=$3
COMP_CWORD=$4
shift 4
COMP_WORDS=("$@")
COMPREPLY=()
if [[ $mode == backups ]]; then
    _lang_switcher_complete_backups "${COMP_WORDS[COMP_CWORD]}" "$backup_root"
elif [[ $mode == dispatch ]]; then
    _lang_switcher_complete_backups() { COMPREPLY=('known-backup'); }
    _lang_switcher
else
    _lang_switcher
fi
for candidate in "${COMPREPLY[@]}"; do printf '%s\0' "$candidate"; done
'''
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', script,
                                 'completion-test', str(COMPLETION), mode, str(root),
                                 str(len(words) - 1), *words], cwd=cwd,
                                capture_output=True, check=True)
        self.assertFalse(result.stderr, result.stderr.decode())
        return [part.decode() for part in result.stdout.split(b'\0') if part]

    def test_commands_and_prefixes(self):
        commands = self.complete(['lang-switcher', ''])
        self.assertEqual(set(commands), {'status', 'update', 'rollback', 'uninstall', 'restore',
                                         'completion', 'layout', 'case', 'help', '--help', '-h'})
        self.assertEqual(self.complete(['lang-switcher', 'sta']), ['status'])
        self.assertEqual(self.complete(['lang-switcher', 'up']), ['update'])

    def test_only_supported_options_are_suggested(self):
        self.assertEqual(self.complete(['lang-switcher', 'status', '--v']), ['--verbose'])
        self.assertEqual(self.complete(['lang-switcher', 'layout', '--v']), [])
        self.assertEqual(self.complete(['lang-switcher', 'case', '--h']), ['--help'])
        self.assertEqual(self.complete(['lang-switcher', 'unknown', '']), [])

    def test_used_options_are_not_repeated(self):
        options = self.complete(['lang-switcher', 'status', '--verbose', '--'])
        self.assertEqual(options, ['--help'])
        options = self.complete(['lang-switcher', 'update', '--repository=example/repo', '--'])
        self.assertNotIn('--repository', options)
        self.assertIn('--verbose', options)

    def test_repository_values_and_equals_forms(self):
        self.assertEqual(self.complete(['lang-switcher', 'update', '--repository', 'Al']), ['AlexZhilov/lang-switcher'])
        self.assertEqual(self.complete(['lang-switcher', 'update', '--repository=Al']), ['--repository=AlexZhilov/lang-switcher'])
        self.assertEqual(self.complete(['lang-switcher', 'update', '--repository', '=', 'Al']), ['AlexZhilov/lang-switcher'])

    def test_completion_shell_choice(self):
        self.assertEqual(self.complete(['lang-switcher', 'completion', 'b']), ['bash'])
        self.assertEqual(self.complete(['lang-switcher', 'completion', 'bash', '']), [])

    def test_restore_position_is_a_path_then_options(self):
        self.assertEqual(self.complete(['lang-switcher', 'restore', ''], mode='dispatch'), ['known-backup'])
        self.assertEqual(self.complete(['lang-switcher', 'restore', '/backup', '--v']), ['--verbose'])

    def test_snapshot_paths_keep_spaces_and_require_a_snapshot(self):
        with tempfile.TemporaryDirectory(prefix='completion paths ') as directory:
            root = Path(directory)
            first = root / '20260101T000000Z-first copy'
            second = root / '20260102T000000Z-second'
            incomplete = root / '20260103-not-a-snapshot'
            for folder in (first, second, incomplete):
                folder.mkdir()
            for folder in (first, second):
                (folder / 'state.json').write_text('{}')
            result = self.complete(['lang-switcher', 'restore', '2026'], mode='backups', root=root)
            self.assertEqual(result, [str(first), str(second)])

    def test_manual_directory_paths_are_supported(self):
        with tempfile.TemporaryDirectory(prefix='completion dirs ') as directory:
            root = Path(directory)
            target = root / 'manual backup'
            target.mkdir()
            result = self.complete(['lang-switcher', 'restore', str(root / 'man')], mode='backups', root=root / 'unused')
            self.assertEqual(result, [str(target)])

    def test_names_are_returned_as_data_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / '2026$(touch PWNED)'
            target.mkdir()
            (target / 'state.json').write_text('{}')
            self.assertEqual(self.complete(['lang-switcher', 'restore', '2026'], mode='backups', root=root, cwd=root), [str(target)])
            self.assertFalse((root / 'PWNED').exists())

    def test_registration_can_be_loaded_without_the_cli(self):
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                 'source "$1"; complete -p lang-switcher', 'test', str(COMPLETION)],
                                capture_output=True, text=True, check=True)
        self.assertIn('-F _lang_switcher lang-switcher', result.stdout)


if __name__ == '__main__':
    unittest.main()
