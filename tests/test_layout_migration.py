import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_installer import FakeSettings, installer


class LayoutMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='layout migration ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.runtime_dir = self.home / 'session'
        self.runtime_dir.mkdir()
        environment = patch.dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime_dir))
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = FakeSettings()

    def runtime(self, data, offline):
        import uuid
        path = data / 'runtimes' / ('python-20260101T000000Z-' + uuid.uuid4().hex[:8])
        path.mkdir(parents=True)
        (path / '.lang-switcher-runtime').write_text('lang-switcher')
        return path

    @contextlib.contextmanager
    def deployment(self):
        with patch.object(installer, 'preflight', return_value='GNOME Shell 43.9'), \
             patch.object(installer, 'missing_packages', return_value=[]), \
             patch.object(installer, 'make_runtime', side_effect=self.runtime), \
             patch.object(installer, 'execute'), \
             patch.object(installer, 'module_active', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            yield

    def legacy_install(self):
        """Historical file paths and snapshot format, including pre-package files."""
        data, _ = installer.layout_pairs(self.home)[0]
        app, _ = installer.layout_pairs(self.home)[1]
        app.mkdir(parents=True)
        core = app / 'zorin_switcher.py'
        core.write_text('original manual switcher')
        core.chmod(0o750)
        wrapper = self.home / '.local/bin/switch_layout.sh'
        wrapper.write_text(f'#!/bin/sh\nexec python3 "{core}" --layout\n')
        original_wrapper = wrapper.read_text()
        tx = installer.Transaction(self.home, self.settings, data / 'backups/old-base')
        source = self.home / 'old-payload'
        source.write_text('old packaged switcher')
        tx.copy(source, core, 0o755)
        source.write_text('# lang-switcher managed command\n# old CLI\n')
        tx.copy(source, self.home / '.local/bin/lang-switcher', 0o755)
        tx.copy(source, data / 'manager.py', 0o644)
        tx.link_runtime(self.runtime(data, True), data / 'current')
        for schema, key, value in installer.settings_plan(self.settings, self.home):
            if key == 'command':
                mode = value.rsplit(' ', 1)[1]
                value = str(self.home / '.local/bin' / f'switch_{mode}.sh')
            tx.set(schema, key, value)
        tx.remember_file(data / 'install.json')
        config = {'version': '1.0.2', 'repository': 'example/lang-switcher',
                  'base_backup': str(tx.directory), 'last_backup': str(tx.directory),
                  'uninstall_settings': tx.state['settings'], 'extra_file_origins': {}}
        (data / 'install.json').write_text(json.dumps(config))
        return config, original_wrapper

    def test_clean_install_creates_only_canonical_layout_and_cli_shortcuts(self):
        with self.deployment():
            installer.install(self.settings, self.home, True)
        for old, new in installer.layout_pairs(self.home):
            self.assertFalse(old.exists())
            self.assertFalse(old.is_symlink())
            self.assertTrue(new.is_dir())
        self.assertTrue((self.home / '.local/lib/lang-switcher/switcher.py').is_file())
        for mode in ('layout', 'case'):
            self.assertFalse((self.home / '.local/bin' / f'switch_{mode}.sh').exists())
        for node in self.settings.get(installer.MEDIA, 'custom-keybindings'):
            command = self.settings.get(installer.CUSTOM_SCHEMA + node, 'command')
            self.assertIn('/.local/bin/lang-switcher', command)
            self.assertEqual(installer.command_mode(command), command.rsplit(' ', 1)[1])

    def test_upgrade_rollback_reupgrade_and_uninstall_preserve_historical_snapshot(self):
        initial, wrapper = self.legacy_install()
        old_data, data = installer.layout_pairs(self.home)[0]
        old_app, app = installer.layout_pairs(self.home)[1]
        unknown = old_app / 'user-notes.txt'
        unknown.write_text('preserve me')
        with self.deployment(), installer.installation_lock(self.home), installer.operation_log(self.home):
            installer.install(self.settings, self.home, True)
            config = installer.read_config(self.home)
            self.assertTrue(installer.UI.log_path.is_relative_to(data))
            self.assertTrue(installer.UI.log_path.is_file())
            for old, new in installer.layout_pairs(self.home):
                self.assertTrue(old.is_symlink())
                self.assertFalse(new.is_symlink())
                self.assertEqual(old.resolve(), new)
            self.assertEqual(Path(initial['base_backup']).resolve(), Path(config['base_backup']))
            self.assertTrue(Path(config['base_backup']).is_relative_to(data))
            self.assertEqual((old_app / 'zorin_switcher.py').read_text(), 'old packaged switcher')
            # The lock follows the renamed directory; a second installer cannot enter.
            with self.assertRaisesRegex(installer.InstallError, 'уже выполняется'):
                with installer.installation_lock(self.home):
                    self.fail('Migration lost the installer lock')
            installer.restore(Path(config['last_backup']), self.home, self.settings)
            self.assertEqual(installer.read_config(self.home), initial)
            self.assertFalse((app / 'switcher.py').exists())
            self.assertEqual((self.home / '.local/bin/lang-switcher').read_text(),
                             '# lang-switcher managed command\n# old CLI\n')
            installer.install(self.settings, self.home, True)
            installer.install(self.settings, self.home, True)
            installer.uninstall(self.home, self.settings)
        self.assertEqual((old_app / 'zorin_switcher.py').read_text(), 'original manual switcher')
        self.assertEqual((old_app / 'zorin_switcher.py').stat().st_mode & 0o777, 0o750)
        self.assertEqual((self.home / '.local/bin/switch_layout.sh').read_text(), wrapper)
        self.assertEqual(unknown.read_text(), 'preserve me')
        self.assertFalse((app / 'switcher.py').exists())
        self.assertFalse((self.home / '.local/bin/lang-switcher').exists())
        self.assertFalse((old_data / 'install.json').exists())

    def test_failed_upgrade_leaves_previous_version_usable_through_aliases(self):
        previous, wrapper = self.legacy_install()
        before = dict(self.settings.raw)
        self.settings.fail_once = (installer.CUSTOM_SCHEMA +
                                   self.settings.get(installer.MEDIA, 'custom-keybindings')[0], 'command')
        with self.deployment(), self.assertRaisesRegex(RuntimeError, 'simulated'):
            installer.install(self.settings, self.home, True)
        self.assertEqual(installer.read_config(self.home), previous)
        self.assertEqual(self.settings.raw, before)
        self.assertEqual((self.home / '.local/bin/switch_layout.sh').read_text(), wrapper)
        self.assertEqual((self.home / '.local/bin/zorin-switcher/zorin_switcher.py').read_text(),
                         'old packaged switcher')
        self.assertFalse((self.home / '.local/lib/lang-switcher/switcher.py').exists())

    def test_unrelated_wrapper_is_preserved_on_clean_install(self):
        wrapper = self.home / '.local/bin/switch_layout.sh'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#!/bin/sh\necho unrelated-user-tool\n')
        with self.deployment():
            installer.install(self.settings, self.home, True)
        self.assertEqual(wrapper.read_text(), '#!/bin/sh\necho unrelated-user-tool\n')

    def test_existing_venv_and_absolute_script_shebang_survive_move(self):
        # Use a separate path without spaces, as required by a direct shebang.
        with tempfile.TemporaryDirectory(prefix='lang-venv-') as temp:
            home = Path(temp)
            old, new = installer.layout_pairs(home)[0]
            venv = old / 'runtimes/legacy-python'
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
            script = venv / 'bin/entrypoint'
            script.write_text(f'#!{venv}/bin/python\nprint("venv alive")\n')
            script.chmod(0o755)
            with contextlib.redirect_stdout(io.StringIO()):
                installer.migrate_layout(home)
            result = subprocess.run([str(script)], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout, 'venv alive\n')
            self.assertTrue((new / 'runtimes/legacy-python/pyvenv.cfg').is_file())

    def test_conflicts_are_rejected_before_any_directory_is_moved_or_chmodded(self):
        for old, new in installer.layout_pairs(self.home):
            old.mkdir(parents=True)
            (old / 'keep').write_text('old')
        _, conflicting = installer.layout_pairs(self.home)[1]
        conflicting.mkdir(parents=True)
        (conflicting / 'keep').write_text('new')
        with self.assertRaisesRegex(installer.InstallError, 'два отдельных'):
            with installer.installation_lock(self.home):
                self.fail('Conflicting layout accepted')
        for old, _ in installer.layout_pairs(self.home):
            self.assertFalse(old.is_symlink())
            self.assertEqual((old / 'keep').read_text(), 'old')
        self.assertFalse(installer.layout_pairs(self.home)[0][1].exists())
        self.assertEqual((conflicting / 'keep').read_text(), 'new')

    def test_external_or_unrelated_symlinks_are_rejected_without_writes(self):
        for index in (0, 1):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as external:
                old, _ = installer.layout_pairs(self.home)[index]
                old.parent.mkdir(parents=True, exist_ok=True)
                old.symlink_to(external)
                with self.assertRaises(installer.InstallError):
                    installer.migrate_layout(self.home)
                self.assertEqual(list(Path(external).iterdir()), [])
                old.unlink()

    def test_second_move_failure_reverses_first_move_and_preserves_files(self):
        for old, _ in installer.layout_pairs(self.home):
            old.mkdir(parents=True)
            (old / 'keep').write_text(str(old))
        second = installer.layout_pairs(self.home)[1][0]
        rename = Path.rename
        def fail_second(path, target):
            if path == second:
                raise OSError('simulated second rename failure')
            return rename(path, target)
        with patch.object(Path, 'rename', fail_second):
            with self.assertRaisesRegex(OSError, 'second rename'):
                installer.migrate_layout(self.home)
        for old, new in installer.layout_pairs(self.home):
            self.assertFalse(old.is_symlink())
            self.assertFalse(new.exists())
            self.assertEqual((old / 'keep').read_text(), str(old))
        self.assertEqual(list(self.home.rglob('*.migrate-*')), [])

    def test_alias_creation_failure_reverses_directory_rename(self):
        old, new = installer.layout_pairs(self.home)[0]
        old.mkdir(parents=True)
        (old / 'keep').write_text('old data')
        replace = Path.replace
        def fail_alias(path, target):
            if '.migrate-' in path.name:
                raise OSError('simulated alias failure')
            return replace(path, target)
        with patch.object(Path, 'replace', fail_alias):
            with self.assertRaisesRegex(OSError, 'alias failure'):
                installer.migrate_layout(self.home)
        self.assertFalse(old.is_symlink())
        self.assertFalse(new.exists())
        self.assertEqual((old / 'keep').read_text(), 'old data')

    def test_active_selection_prevents_migration(self):
        old, new = installer.layout_pairs(self.home)[0]
        old.mkdir(parents=True)
        with installer.migration_lock():
            with self.assertRaisesRegex(installer.InstallError, 'обрабатывает текст'):
                installer.migrate_layout(self.home)
        self.assertFalse(old.is_symlink())
        self.assertFalse(new.exists())

    def test_cosmetic_extension_rename_does_not_require_restart(self):
        extension = self.home / 'extension'
        extension.mkdir()
        source = installer.ROOT / 'payload/extension'
        (extension / 'extension.js').write_bytes((source / 'extension.js').read_bytes())
        metadata = json.loads((source / 'metadata.json').read_text())
        metadata['name'] = 'Previous display name'
        (extension / 'metadata.json').write_text(json.dumps(metadata))
        self.assertFalse(installer.extension_changed(extension))
        metadata['shell-version'] = ['44']
        (extension / 'metadata.json').write_text(json.dumps(metadata))
        self.assertTrue(installer.extension_changed(extension))
