import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def exercise(self, failure=None, flags=()):
        with tempfile.TemporaryDirectory(prefix='lang switcher bootstrap ') as directory:
            root = Path(directory)
            archive = root / 'test.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                code = b'#!/bin/sh\nprintf installed > "$TEST_MARKER"\nprintf "%s\\n" "$@" > "$TEST_ARGS"\n'
                member = tarfile.TarInfo('lang-switcher/install.sh')
                member.size, member.mode = len(code), 0o755
                bundle.addfile(member, io.BytesIO(code))
                if failure == 'traversal':
                    member = tarfile.TarInfo('../escaped')
                    member.size = 1
                    bundle.addfile(member, io.BytesIO(b'x'))
                if failure == 'symlink':
                    member = tarfile.TarInfo('lang-switcher/escape')
                    member.type, member.linkname = tarfile.SYMTYPE, '/etc'
                    bundle.addfile(member)
            checksum = root / 'test.sha256'
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if failure == 'checksum':
                digest = '0' * 64
            checksum.write_text(digest + '  lang-switcher.tar.gz\n')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            curl = bin_dir / 'curl'
            curl.write_text('''#!/usr/bin/env python3
import os, shutil, sys
if os.environ.get('TEST_FAIL_DOWNLOAD'):
    sys.exit(22)
url = next(arg for arg in sys.argv if arg.startswith('https://'))
source = os.environ['TEST_CHECKSUM' if url.endswith('.sha256') else 'TEST_ARCHIVE']
destination = sys.argv[sys.argv.index('-o') + 1]
shutil.copyfile(source, destination)
''')
            curl.chmod(0o755)
            temp_dir = root / 'downloads'
            temp_dir.mkdir()
            marker = root / 'installed'
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                       TMPDIR=str(temp_dir), TEST_ARCHIVE=str(archive),
                       TEST_CHECKSUM=str(checksum), TEST_MARKER=str(marker), TEST_ARGS=str(root/'args'))
            if failure == 'download':
                env['TEST_FAIL_DOWNLOAD'] = '1'
            result = subprocess.run(['bash', str(ROOT / 'bootstrap.sh'), '--repo', 'example/lang-switcher', *flags],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(list(temp_dir.iterdir()), [], result.stderr)
            self.assertFalse((root / 'escaped').exists())
            if failure:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(marker.read_text(), 'installed')
                for flag in flags:
                    self.assertIn(flag, (root/'args').read_text().splitlines())

    def test_success_removes_only_temporary_downloads(self):
        self.exercise()

    def test_verbose_and_check_only_are_forwarded(self):
        self.exercise(flags=('--verbose', '--check-only'))

    def test_failed_download_is_cleaned(self):
        self.exercise('download')

    def test_checksum_mismatch_does_not_install(self):
        self.exercise('checksum')

    def test_archive_traversal_is_rejected(self):
        self.exercise('traversal')

    def test_archive_symlinks_are_rejected(self):
        self.exercise('symlink')


if __name__ == '__main__':
    unittest.main()
