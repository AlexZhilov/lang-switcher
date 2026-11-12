#!/usr/bin/env python3
"""Build a portable, allowlisted GitHub Release bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_FILES = ('switcher.py', 'keyboard_x11.py', 'extension/extension.js', 'extension/metadata.json',
                 'lang-switcher', 'lang-switcher.bash', 'switch_layout.sh', 'switch_case.sh')


def public_tar_metadata(info):
    """Do not publish the builder's username, numeric UID/GID or timestamps."""
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    info.mtime = 0
    info.pax_headers = {}
    info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
    return info


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='AlexZhilov/lang-switcher', help='GitHub owner/repository embedded in updates')
    parser.add_argument('--version', help='Expected Git tag, for example v1.0.0')
    parser.add_argument('--no-download', action='store_true', help='Use the already built wheels/ cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args(argv)
    version = (ROOT / 'VERSION').read_text().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        parser.error('VERSION must be X.Y.Z')
    if args.version and args.version != 'v' + version:
        parser.error('Git tag does not match VERSION')
    if args.repo and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('--repo must be owner/repository')
    wheels = ROOT / 'wheels'
    wheels.mkdir(exist_ok=True)
    if not args.no_download:
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--disable-pip-version-check',
                        '--wheel-dir', str(wheels), '-r', str(ROOT / 'requirements.txt')], check=True)
    if not list(wheels.glob('*.whl')):
        parser.error('No wheels found; build without --no-download first')
    build = ROOT / '.build'
    build.mkdir(exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=build) as temp:
        bundle = Path(temp) / 'lang-switcher'
        bundle.mkdir()
        for name in ('install.sh', 'install.py', 'bootstrap.sh', 'requirements.txt',
                     'README.txt', 'README.md', 'SECURITY.md', 'VERSION',
                     'docs/REFERENCE.md', 'docs/DEVELOPMENT.md'):
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        for name in PAYLOAD_FILES:
            source, target = ROOT / 'payload' / name, bundle / 'payload' / name
            if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
                parser.error(f'Refusing symlink or external payload: {name}')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (bundle / 'wheels').mkdir()
        required = {line.split('==')[0].strip().lower().replace('_', '-')
                    for line in (ROOT / 'requirements.txt').read_text().splitlines()
                    if line.strip() and not line.startswith('#')}
        selected = [path for path in sorted(wheels.glob('*.whl'))
                    if path.name.split('-')[0].lower().replace('_', '-') in required]
        available = {path.name.split('-')[0].lower().replace('_', '-') for path in selected}
        if required != available:
            parser.error('Missing wheels: ' + ', '.join(sorted(required - available)))
        for path in selected:
            if path.is_symlink():
                parser.error(f'Refusing a symlink wheel: {path.name}')
            shutil.copy2(path, bundle / 'wheels' / path.name)
        (bundle / 'REPOSITORY').write_text(args.repo + '\n')
        manifest = {str(path.relative_to(bundle)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(bundle.rglob('*')) if path.is_file()}
        (bundle / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
        archive = args.output / 'lang-switcher.tar.gz'
        with tarfile.open(archive, 'w:gz') as result:
            result.add(bundle, arcname='lang-switcher', filter=public_tar_metadata)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (args.output / 'lang-switcher.tar.gz.sha256').write_text(checksum + '  lang-switcher.tar.gz\n')
    print(archive)
    print(f'SHA256: {checksum}')


if __name__ == '__main__':
    main()
