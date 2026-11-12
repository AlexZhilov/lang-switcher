import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

installer = load('security_installer', ROOT / 'install.py')
builder = load('security_builder', ROOT / 'scripts/build_release.py')
switcher = load('security_switcher', ROOT / 'payload/zorin_switcher.py')


class SecurityTests(unittest.TestCase):
    def test_archive_has_neutral_ownership_and_only_allowlisted_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('install.sh', 'install.py', 'bootstrap.sh', 'requirements.txt', 'README.txt', 'README.md', 'SECURITY.md', 'VERSION'):
                (root / name).write_text('1.0.0' if name == 'VERSION' else 'public')
            (root / 'requirements.txt').write_text('pyperclip==1.10.0\n')
            for name in builder.PAYLOAD_FILES:
                path = root / 'payload' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('public payload')
            (root / 'payload/.env').write_text('should-not-publish')
            (root / 'wheels').mkdir()
            (root / 'wheels/pyperclip-1.10.0-py3-none-any.whl').write_bytes(b'wheel fixture')
            with patch.object(builder, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                builder.main(['--no-download'])
            with tarfile.open(root / 'dist/lang-switcher.tar.gz') as archive:
                for entry in archive.getmembers():
                    self.assertEqual((entry.uid, entry.gid, entry.uname, entry.gname, entry.mtime), (0, 0, '', '', 0))
                    self.assertNotIn('.env', entry.name)

    def test_restore_refuses_unrelated_files_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            folder = home / 'backup'
            folder.mkdir()
            target = home / '.ssh/authorized_keys'
            (folder / 'state.json').write_text(json.dumps({'version': 1, 'home': str(home),
                                                         'files': {str(target): {'kind': 'absent'}}, 'settings': []}))
            with self.assertRaisesRegex(installer.InstallError, 'посторонний путь'):
                installer.restore(folder, home, object())
            self.assertFalse(target.exists())

    def test_destination_parent_cannot_escape_home_via_symlink(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external:
            home = Path(temp)
            (home / '.local').symlink_to(external)
            with self.assertRaises(installer.InstallError):
                installer.validate_destination(home / '.local/bin/lang-switcher', home)
            self.assertEqual(list(Path(external).iterdir()), [])

    def test_backups_are_private(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            tx = installer.Transaction(home, object(), home / 'backups/snapshot')
            for path in (tx.directory.parent, tx.directory, tx.directory / 'files'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual((tx.directory / 'state.json').stat().st_mode & 0o777, 0o600)

    def test_unrelated_settings_are_not_accepted_in_snapshots(self):
        with self.assertRaises(installer.InstallError):
            installer.validate_setting('org.gnome.desktop.background', 'picture-uri')

    def test_lock_refuses_symlinks_without_touching_the_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / 'other-file'
            victim.write_text('unchanged')
            (root / f'zorin-switcher-{os.getuid()}.lock').symlink_to(victim)
            with patch.dict(os.environ, XDG_RUNTIME_DIR=str(root)):
                with self.assertRaises(OSError):
                    with switcher.selection_lock():
                        self.fail('Symlink lock was accepted')
            self.assertEqual(victim.read_text(), 'unchanged')

    def test_legacy_uuid_is_discovered_without_hardcoding_an_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            directory = home / '.local/share/gnome-shell/extensions/older-switcher@example.test'
            directory.mkdir(parents=True)
            (directory / 'metadata.json').write_text(json.dumps({'uuid': directory.name}))
            (directory / 'extension.js').write_text('org.gnome.Shell.Extensions.ZorinSwitcher /org/gnome/Shell/Extensions/ZorinSwitcher')
            self.assertEqual(installer.legacy_extension_ids(home), {directory.name})

    def test_migration_disables_previous_module_and_uninstall_restores_it(self):
        from test_installer import FakeSettings
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            previous = 'older-switcher@example.test'
            directory = home / '.local/share/gnome-shell/extensions' / previous
            directory.mkdir(parents=True)
            (directory / 'metadata.json').write_text(json.dumps({'uuid': previous}))
            (directory / 'extension.js').write_text('org.gnome.Shell.Extensions.ZorinSwitcher /org/gnome/Shell/Extensions/ZorinSwitcher')
            settings = FakeSettings()
            settings.set(installer.SHELL, 'enabled-extensions', ['unrelated@example', previous])
            tx = installer.Transaction(home, settings, home / 'backups/test')
            for schema, key, value in installer.settings_plan(settings, home):
                tx.set(schema, key, value)
            self.assertEqual(settings.get(installer.SHELL, 'enabled-extensions'), ['unrelated@example', installer.EXTENSION_UUID])
            self.assertIn(previous, settings.get(installer.SHELL, 'disabled-extensions'))
            installer.restore(tx.directory, home, settings, uninstall_records=tx.state['settings'])
            self.assertEqual(settings.get(installer.SHELL, 'enabled-extensions'), ['unrelated@example', previous])
            self.assertEqual(settings.get(installer.SHELL, 'disabled-extensions'), [])

    def test_uninstall_refuses_external_runtime_symlink_before_restoring_files(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external:
            home = Path(temp)
            data = home / '.local/share/zorin-switcher'
            data.mkdir(parents=True)
            (data / 'install.json').write_text(json.dumps({'base_backup': str(home / 'backup')}))
            (data / 'runtimes').symlink_to(external)
            sentinel = Path(external) / 'keep'
            sentinel.write_text('unchanged')
            with patch.object(installer, 'restore') as restore:
                with self.assertRaises(installer.InstallError):
                    installer.uninstall(home, object())
                restore.assert_not_called()
            self.assertEqual(sentinel.read_text(), 'unchanged')


if __name__ == '__main__':
    unittest.main()
