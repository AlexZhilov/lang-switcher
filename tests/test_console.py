import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock, patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('console_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class TTY(io.StringIO):
    def isatty(self):
        return True


class ConsoleTests(unittest.TestCase):
    def test_color_is_limited_to_terminals_and_respects_no_color(self):
        for stream, environment, colored in (
            (io.StringIO(), {'TERM': 'xterm'}, False),
            (TTY(), {'TERM': 'dumb'}, False),
            (TTY(), {'TERM': 'xterm', 'NO_COLOR': '1'}, False),
            (TTY(), {'TERM': 'xterm'}, True),
        ):
            with self.subTest(environment=environment, tty=stream.isatty()):
                ui = installer.Console(stream)
                with patch.dict(os.environ, environment, clear=True):
                    ui.title('1.0.1')
                    ui.ok('Ready')
                    ui.warn('Next step')
                    ui.error('Failure')
                self.assertEqual('\033[' in stream.getvalue(), colored)

    def test_success_summary_omits_private_paths_and_obsolete_installer_command(self):
        stream = io.StringIO()
        with patch.object(installer, 'UI', installer.Console(stream)):
            installer.installation_summary(True, Path('/private/backup'))
        result = stream.getvalue()
        self.assertIn('Alt+F2', result)
        self.assertIn('требуется перезапуск', result)
        self.assertNotIn('можно пользоваться', result)
        self.assertIn('lang-switcher rollback', result)
        self.assertNotIn('/private/backup', result)
        self.assertNotIn('./install.sh --restore', result)
        self.assertLessEqual(len(result.splitlines()), 10)

    def test_status_is_compact_and_verbose_reveals_recovery_information(self):
        config = {'version': '1.0.1', 'repository': 'example/lang-switcher',
                  'last_backup': '/private/backup', 'last_log': '/private/install.log'}
        for verbose in (False, True):
            stream = io.StringIO()
            ui = installer.Console(stream)
            ui.verbose = verbose
            with patch.object(installer, 'UI', ui), \
                 patch.object(installer, 'read_config', return_value=config), \
                 patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                installer.show_status(Path('/home/example'))
            result = stream.getvalue()
            self.assertIn('Модуль GNOME активен', result)
            self.assertEqual('/private/backup' in result, verbose)
            self.assertEqual('/private/install.log' in result, verbose)

    def test_unresponsive_module_reports_activation_required_instead_of_success(self):
        stream = io.StringIO()
        with patch.object(installer, 'UI', installer.Console(stream)), \
             patch.object(installer, 'read_config', return_value={'version': '1.0.1'}), \
             patch.object(installer.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gdbus', 3)):
            installer.show_status(Path('/home/example'))
        self.assertIn('не загружен', stream.getvalue())
        self.assertNotIn('Модуль GNOME активен', stream.getvalue())

    def test_quiet_commands_keep_diagnostics_in_private_log(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stream = io.StringIO()
            ui = installer.Console(stream)
            with patch.object(installer, 'UI', ui):
                with installer.operation_log(home):
                    installer.run_logged([sys.executable, '-c', 'print("diagnostic detail")'], 'Test command')
                    path = ui.log_path
                self.assertIsNone(ui.log_file)
            self.assertNotIn('diagnostic detail', stream.getvalue())
            self.assertIn('diagnostic detail', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_command_errors_remain_errors_and_preserve_details(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = io.StringIO()
            ui = installer.Console(stream)
            with patch.object(installer, 'UI', ui):
                with installer.operation_log(Path(temp)):
                    with self.assertRaisesRegex(installer.InstallError, 'код 7'):
                        installer.run_logged([sys.executable, '-c', 'print("failure detail"); raise SystemExit(7)'], 'Test command')
                self.assertIn('failure detail', ui.log_path.read_text())
            self.assertNotIn('failure detail', stream.getvalue())

    def test_verbose_commands_show_and_log_details(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = io.StringIO()
            ui = installer.Console(stream)
            ui.verbose = True
            with patch.object(installer, 'UI', ui), installer.operation_log(Path(temp)):
                installer.run_logged([sys.executable, '-c', 'print("full output")'], 'Test command')
            self.assertIn('full output', stream.getvalue())
            self.assertIn('full output', ui.log_path.read_text())

    def test_sudo_authentication_is_interactive_and_package_commands_are_noninteractive(self):
        from test_installer import FakeSettings
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(installer, 'UI', installer.Console(stream)), \
             patch.object(installer, 'preflight', return_value='GNOME Shell 43.9'), \
             patch.object(installer, 'missing_packages', return_value=['xclip']), \
             patch.object(installer, 'execute') as interactive, \
             patch.object(installer, 'run_logged') as logged, \
             patch.object(installer, 'make_runtime', side_effect=RuntimeError('stop before deployment')):
            with self.assertRaisesRegex(RuntimeError, 'stop before deployment'):
                installer.install(FakeSettings(), Path(temp), False)
            interactive.assert_called_once_with(['sudo', '-v'])
            self.assertEqual([call.args[0][:3] for call in logged.call_args_list],
                             [['sudo', '-n', 'apt-get'], ['sudo', '-n', 'apt-get']])

    def test_update_forwards_verbose_to_downloaded_installer(self):
        with patch.object(installer, 'UI', installer.Console(io.StringIO())), \
             patch.object(installer.os, 'geteuid', return_value=1000), \
             patch.object(installer, 'read_config', return_value={'repository': 'example/lang-switcher'}), \
             patch.object(installer, 'execute') as execute:
            self.assertEqual(installer.main(['--update', '--verbose']), 0)
            self.assertEqual(execute.call_args.args[0][-1], '--verbose')


if __name__ == '__main__':
    unittest.main()
