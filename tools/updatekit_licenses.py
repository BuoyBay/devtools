#!/usr/bin/env python3
"""Collect dependency license texts and SPDX metadata for updatekit redistribution."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'libraries/updatekit/native'


def collect():
    metadata = json.loads(subprocess.check_output([
        'cargo', 'metadata', '--manifest-path', str(NATIVE / 'Cargo.toml'),
        '--locked', '--format-version', '1']))
    destination = ROOT / 'libraries/updatekit/resources/third-party'
    inventory = []
    notices = []
    for package in sorted(metadata['packages'], key=lambda item: (item['name'], item['version'])):
        if not package.get('source'):
            continue
        source = Path(package['manifest_path']).parent
        name = package['name'] + '-' + package['version']
        texts = sorted(item for item in source.rglob('*') if item.is_file()
                       and item.name.lower().startswith(('license', 'copying', 'notice')))
        if package.get('license_file'):
            texts.append(source / package['license_file'])
        copied = []
        for item in texts:
            if item.is_file():
                relative = item.relative_to(source).as_posix()
                notices.append(f"\n{'=' * 72}\n{name}: {relative}\n{'=' * 72}\n" + item.read_text(errors='replace'))
                copied.append(relative)
        inventory.append({'name': package['name'], 'version': package['version'],
                          'license': package.get('license'), 'repository': package.get('repository'),
                          'texts': copied})
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    (destination / 'THIRD_PARTY_NOTICES.txt').write_text(''.join(notices))
    (destination / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
    print(f'Collected license notices for {len(inventory)} dependencies')
    return destination


if __name__ == '__main__':
    collect()
