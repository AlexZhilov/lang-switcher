#!/usr/bin/env python3
"""Check bundled Python dependencies in a disposable venv; no desktop writes."""
import importlib.util
from pathlib import Path
import tempfile

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', root / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
with tempfile.TemporaryDirectory(prefix='lang-switcher-runtime-check-') as directory:
    runtime = installer.make_runtime(Path(directory), offline=True)
    installer.execute([runtime / 'bin/python', root / 'payload/switcher.py', '--help'])
print('Temporary runtime checked and removed. Installed switcher and desktop settings unchanged.')
