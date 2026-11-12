import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class FakeSettings:
    def __init__(self):
        self.raw = {}
        self.fail_once = None
        for schema, key, value in (
            (installer.MEDIA, 'custom-keybindings', []),
            (installer.INPUT, 'sources', [('xkb', 'us'), ('xkb', 'ru')]),
            (installer.INPUT, 'mru-sources', [('xkb', 'us'), ('xkb', 'ru')]),
            (installer.SHELL, 'enabled-extensions', ['unrelated@example']),
            (installer.SHELL, 'disabled-extensions', []),
            (installer.SHELL, 'disable-user-extensions', False),
        ):
            self.raw[schema, key] = installer.variant(value)

    def get_raw(self, schema, key):
        return self.raw.get((schema, key), "''")

    def get(self, schema, key):
        return installer.parse_variant(self.get_raw(schema, key))

    def set_raw(self, schema, key, raw):
        if self.fail_once == (schema, key):
            self.fail_once = None
            raise RuntimeError('simulated write failure')
        self.raw[schema, key] = raw

    def set(self, schema, key, value):
        self.set_raw(schema, key, installer.variant(value))

    def add_binding(self, suffix, name, command, chord):
        path = installer.CUSTOM_ROOT + suffix + '/'
        active = self.get(installer.MEDIA, 'custom-keybindings')
        self.set(installer.MEDIA, 'custom-keybindings', active + [path])
        for key, value in (('name', name), ('command', command), ('binding', chord)):
            self.set(installer.CUSTOM_SCHEMA + path, key, value)
        return path


def apply(settings, plan):
    for schema, key, value in plan:
        settings.set(schema, key, value)


class InstallerTests(unittest.TestCase):
    def test_fresh_install_and_idempotent_bindings(self):
        settings = FakeSettings()
        home = Path('/home/other user')
        apply(settings, installer.settings_plan(settings, home))
        first = dict(settings.raw)
        self.assertEqual(len(settings.get(installer.MEDIA, 'custom-keybindings')), 2)
        self.assertEqual(settings.get(installer.SHELL, 'enabled-extensions'),
                         ['unrelated@example', installer.EXTENSION_UUID])
        apply(settings, installer.settings_plan(settings, home))
        self.assertEqual(settings.raw, first)
        for path in settings.get(installer.MEDIA, 'custom-keybindings'):
            command = settings.get(installer.CUSTOM_SCHEMA + path, 'command')
            self.assertEqual(len(__import__('shlex').split(command)), 1)
            self.assertIn(str(home), __import__('shlex').split(command)[0])

    def test_upgrade_reuses_old_paths_and_removes_only_known_duplicates(self):
        settings = FakeSettings()
        other = settings.add_binding('custom0', 'Screenshot', 'flameshot gui', '<Control><Alt>p')
        layout = settings.add_binding('custom1', 'Switch Layout', '/home/previous/.local/bin/switch_layout.sh', '<Primary><Shift>space')
        duplicate = settings.add_binding('custom2', 'Old duplicate', '/home/previous/.local/bin/switch_layout.sh', '<Control><Shift>space')
        case = settings.add_binding('custom3', 'Switch Case', '/home/previous/.local/bin/switch_case.sh', '<Control><Shift>q')
        old_other = {key: settings.get_raw(installer.CUSTOM_SCHEMA + other, key) for key in ('name', 'command', 'binding')}
        apply(settings, installer.settings_plan(settings, Path('/home/new')))
        self.assertEqual(settings.get(installer.MEDIA, 'custom-keybindings'), [other, layout, case])
        self.assertEqual({key: settings.get_raw(installer.CUSTOM_SCHEMA + other, key) for key in old_other}, old_other)
        self.assertNotIn(duplicate, settings.get(installer.MEDIA, 'custom-keybindings'))

    def test_conflicts_and_disabled_extensions_are_rejected_before_writes(self):
        settings = FakeSettings()
        settings.add_binding('custom0', 'Another tool', 'other-tool', '<Primary><Shift>q')
        original = dict(settings.raw)
        with self.assertRaisesRegex(installer.InstallError, 'уже занята'):
            installer.settings_plan(settings, Path('/home/new'))
        self.assertEqual(settings.raw, original)
        settings = FakeSettings()
        settings.set(installer.SHELL, 'disable-user-extensions', True)
        with self.assertRaisesRegex(installer.InstallError, 'отключены'):
            installer.settings_plan(settings, Path('/home/new'))

    def test_missing_sources_are_added_without_removing_others(self):
        settings = FakeSettings()
        settings.set(installer.INPUT, 'sources', [('xkb', 'de')])
        apply(settings, installer.settings_plan(settings, Path('/home/new')))
        self.assertEqual(settings.get(installer.INPUT, 'sources'), [('xkb', 'de'), ('xkb', 'us'), ('xkb', 'ru')])

    def test_restore_recovers_files_modes_symlinks_and_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings = FakeSettings()
            before = dict(settings.raw)
            target = home / '.local/bin/zorin-switcher/zorin_switcher.py'
            target.parent.mkdir(parents=True)
            target.write_text('old')
            target.chmod(0o750)
            alias = home / '.local/share/zorin-switcher/current'
            alias.parent.mkdir(parents=True)
            alias.symlink_to('previous runtime')
            payload = home / 'payload'
            payload.write_text('new')
            tx = installer.Transaction(home, settings, home / 'backup')
            tx.copy(payload, target, 0o755)
            tx.copy(payload, home / '.local/bin/switch_layout.sh', 0o644)
            tx.link_runtime(home / 'new runtime', alias)
            tx.set(installer.SHELL, 'enabled-extensions', ['changed'])
            installer.restore(tx.directory, home, settings)
            self.assertEqual(target.read_text(), 'old')
            self.assertEqual(target.stat().st_mode & 0o777, 0o750)
            self.assertEqual(os.readlink(alias), 'previous runtime')
            self.assertFalse((home / '.local/bin/switch_layout.sh').exists())
            self.assertEqual(settings.raw, before)

    def test_restore_refuses_another_user(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings = FakeSettings()
            tx = installer.Transaction(home, settings, home / 'backup')
            with self.assertRaises(installer.InstallError):
                installer.restore(tx.directory, Path('/home/another'), settings)

    def test_install_failure_rolls_back_active_script(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            old = home / '.local/bin/zorin-switcher/zorin_switcher.py'
            old.parent.mkdir(parents=True)
            old.write_text('old version')
            settings = FakeSettings()
            original = dict(settings.raw)
            settings.fail_once = (installer.MEDIA, 'custom-keybindings')
            runtime = home / '.local/share/zorin-switcher/runtimes/test-runtime'
            runtime.mkdir(parents=True)
            with patch.object(installer, 'preflight', return_value='GNOME Shell 43.9'), \
                 patch.object(installer, 'missing_packages', return_value=[]), \
                 patch.object(installer, 'make_runtime', return_value=runtime), \
                 patch.object(installer, 'execute'), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'simulated'):
                    installer.install(settings, home, True)
            self.assertEqual(old.read_text(), 'old version')
            self.assertFalse((home / '.local/share/zorin-switcher/current').exists())
            for key, value in original.items():
                self.assertEqual(settings.raw[key], value)
            for key in set(settings.raw) - set(original):
                self.assertEqual(settings.raw[key], "''")

    def test_install_upgrade_and_uninstall_restore_original(self):
        with tempfile.TemporaryDirectory(prefix='lang switcher ') as temp:
            home = Path(temp)
            old = home / '.local/bin/zorin-switcher/zorin_switcher.py'
            old.parent.mkdir(parents=True)
            old.write_text('original pre-install script')
            settings = FakeSettings()
            original = dict(settings.raw)
            def runtime(data, offline):
                path = data / 'runtimes' / ('python-20260101T000000Z-' + __import__('uuid').uuid4().hex[:8])
                path.mkdir(parents=True)
                (path / '.lang-switcher-runtime').write_text('lang-switcher')
                return path
            with patch.object(installer, 'preflight', return_value='GNOME Shell 43.9'), \
                 patch.object(installer, 'missing_packages', return_value=[]), \
                 patch.object(installer, 'make_runtime', side_effect=runtime), \
                 patch.object(installer, 'execute'), \
                 patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                 contextlib.redirect_stdout(io.StringIO()):
                installer.install(settings, home, True, 'example/lang-switcher')
                first = installer.read_config(home)
                first_bindings = settings.get(installer.MEDIA, 'custom-keybindings')
                installer.install(settings, home, True)
                second = installer.read_config(home)
                self.assertEqual(second['base_backup'], first['base_backup'])
                self.assertEqual(second['repository'], 'example/lang-switcher')
                self.assertNotEqual(second['last_backup'], first['last_backup'])
                self.assertEqual(settings.get(installer.MEDIA, 'custom-keybindings'), first_bindings)
                installer.restore(Path(second['last_backup']), home, settings)
                self.assertEqual(installer.read_config(home), first)
                installer.uninstall(home, settings)
            self.assertEqual(old.read_text(), 'original pre-install script')
            self.assertFalse((home / '.local/bin/lang-switcher').exists())
            self.assertFalse((home / '.local/share/bash-completion/completions/lang-switcher').exists())
            self.assertFalse((home / '.bashrc').exists())
            self.assertFalse((home / '.local/share/zorin-switcher/current').exists())
            self.assertEqual(list((home / '.local/share/zorin-switcher/runtimes').iterdir()), [])
            for key, value in original.items():
                self.assertEqual(settings.raw[key], value)

    def test_uninstall_preserves_later_unrelated_settings_and_repurposed_shortcuts(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings = FakeSettings()
            def runtime(data, offline):
                path = data / 'runtimes' / 'python-20260101T000000Z-00000001'
                path.mkdir(parents=True)
                (path / '.lang-switcher-runtime').write_text('lang-switcher')
                return path
            with patch.object(installer, 'preflight', return_value='GNOME Shell 43.9'), \
                 patch.object(installer, 'missing_packages', return_value=[]), \
                 patch.object(installer, 'make_runtime', side_effect=runtime), \
                 patch.object(installer, 'execute'), \
                 patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                 contextlib.redirect_stdout(io.StringIO()):
                installer.install(settings, home, True)
                paths = settings.get(installer.MEDIA, 'custom-keybindings')
                repurposed = paths[0]
                schema = installer.CUSTOM_SCHEMA + repurposed
                settings.set(schema, 'command', 'new-unrelated-app')
                settings.set(schema, 'name', 'My new tool')
                later = settings.add_binding('later', 'Later shortcut', 'unrelated-later', '<Control><Alt>z')
                settings.set(installer.SHELL, 'enabled-extensions',
                             settings.get(installer.SHELL, 'enabled-extensions') + ['later@example'])
                settings.set(installer.INPUT, 'sources', settings.get(installer.INPUT, 'sources') + [('xkb', 'de')])
                installer.uninstall(home, settings)
            self.assertEqual(settings.get(installer.MEDIA, 'custom-keybindings'), [repurposed, later])
            self.assertEqual(settings.get(schema, 'command'), 'new-unrelated-app')
            self.assertEqual(settings.get(schema, 'name'), 'My new tool')
            self.assertEqual(settings.get(installer.SHELL, 'enabled-extensions'), ['unrelated@example', 'later@example'])
            self.assertIn(('xkb', 'de'), settings.get(installer.INPUT, 'sources'))
            removed = installer.CUSTOM_SCHEMA + paths[1]
            for key in ('command', 'name', 'binding'):
                self.assertEqual(settings.get(removed, key), '')

    def test_uninstall_restores_completion_introduced_after_an_older_install(self):
        for original_completion in (None, 'user completion before feature upgrade'):
            with self.subTest(original_completion=original_completion), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                settings = FakeSettings()
                def runtime(data, offline):
                    path = data / 'runtimes' / ('python-20260101T000000Z-' + __import__('uuid').uuid4().hex[:8])
                    path.mkdir(parents=True)
                    (path / '.lang-switcher-runtime').write_text('lang-switcher')
                    return path
                with patch.object(installer, 'preflight', return_value='GNOME Shell 43.9'), \
                     patch.object(installer, 'missing_packages', return_value=[]), \
                     patch.object(installer, 'make_runtime', side_effect=runtime), \
                     patch.object(installer, 'execute'), \
                     patch.object(installer, 'module_active', return_value=True), \
                     contextlib.redirect_stdout(io.StringIO()):
                    installer.install(settings, home, True)
                    completion = home / '.local/share/bash-completion/completions/lang-switcher'
                    first = installer.read_config(home)
                    base_state = Path(first['base_backup']) / 'state.json'
                    state = json.loads(base_state.read_text())
                    del state['files'][str(completion)]  # Pre-feature installation snapshot.
                    base_state.write_text(json.dumps(state))
                    completion.unlink()
                    if original_completion is not None:
                        completion.write_text(original_completion)
                    installer.install(settings, home, True)
                    introduced = installer.read_config(home)
                    origin = introduced['extra_file_origins'][str(completion)]
                    self.assertEqual(completion.read_bytes(), (ROOT / 'payload/lang-switcher.bash').read_bytes())
                    installer.restore(Path(introduced['last_backup']), home, settings)
                    if original_completion is None:
                        self.assertFalse(completion.exists())
                    else:
                        self.assertEqual(completion.read_text(), original_completion)
                    installer.install(settings, home, True)
                    origin = installer.read_config(home)['extra_file_origins'][str(completion)]
                    installer.install(settings, home, True)
                    self.assertEqual(installer.read_config(home)['extra_file_origins'][str(completion)], origin)
                    installer.uninstall(home, settings)
                if original_completion is None:
                    self.assertFalse(completion.exists())
                else:
                    self.assertEqual(completion.read_text(), original_completion)
                self.assertFalse((home / '.bashrc').exists())

    def test_variant_and_repository_validation(self):
        self.assertEqual(installer.parse_variant('@as []'), [])
        self.assertFalse(installer.parse_variant('false'))
        self.assertEqual(installer.repository_name('owner/lang-switcher'), 'owner/lang-switcher')
        for invalid in ('https://github.com/owner/repo', '../a/b', 'owner/repo;echo'):
            with self.assertRaises(installer.InstallError):
                installer.repository_name(invalid)

    def test_install_lock_prevents_parallel_mutations(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with installer.installation_lock(home):
                with self.assertRaisesRegex(installer.InstallError, 'уже выполняется'):
                    with installer.installation_lock(home):
                        self.fail('Second installer obtained lock')


if __name__ == '__main__':
    unittest.main()
