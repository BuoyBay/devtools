#!/usr/bin/env python3
"""Seal the reusable macOS Keychain package."""
import json
from pathlib import Path
import shutil
import subprocess
from buoy_executables import resolve_packager
ROOT = Path(__file__).resolve().parents[1]

def build():
    packager = resolve_packager()
    source = ROOT / 'libraries/keychain'
    version = json.loads((source / 'manifest.json').read_text())['version']
    output = ROOT / 'packages' / f'keychain-{version}.buipkg'
    output.parent.mkdir(exist_ok=True)
    subprocess.run([packager, 'pkg', 'seal', str(source), '-o', str(output)], check=True)
    (ROOT / 'dist').mkdir(exist_ok=True)
    shutil.copyfile(output, ROOT / 'dist' / output.name)
    return output

if __name__ == '__main__':
    build()
