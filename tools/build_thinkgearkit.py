#!/usr/bin/env python3
"""Build native ThinkGear transport and seal isolated Buoy package."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/thinkgearkit'
TARGETS = ('x86_64-linux', 'arm64-linux', 'x86_64-darwin', 'arm64-darwin', 'x86_64-windows', 'arm64-windows')
TRIPLES = {'x86_64-linux': 'x86_64-unknown-linux-gnu', 'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-darwin': 'x86_64-apple-macos', 'arm64-darwin': 'arm64-apple-macos', 'x86_64-windows': 'x86_64-pc-windows-msvc', 'arm64-windows': 'aarch64-pc-windows-msvc'}

def check(source=None):
    source = Path(source or PACKAGE / 'modules/ThinkGearKit.bui')
    for target in TARGETS:
        result = subprocess.run([os.environ.get('BUOY', 'buoy'), str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=ROOT, capture_output=True, text=True, timeout=90)
        diagnostic = json.loads(result.stdout)
        if result.returncode or diagnostic['errors']:
            raise RuntimeError(f'{source} {target}: {diagnostic}\n{result.stderr}')
        if diagnostic.get('warnings'):
            print(diagnostic['warnings'])

def build(runtime_root=None, target=None, cc=None):
    spec = importlib.util.spec_from_file_location('thinkgear_native', PACKAGE / 'build_native.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if cc is None and target == 'x86_64-windows' and os.name != 'nt':
        cc = os.environ.get('CC') or 'x86_64-w64-mingw32-gcc'
    return module.build(runtime_root, target=target, cc=cc)

def seal(target, output, archive=None, runtime_root=None):
    output = Path(output)
    archive = Path(archive or ROOT / 'packages/thinkgearkit-0.1.0.buipkg')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='thinkgearkit-seal-') as directory:
        stage = Path(directory)
        for name in ('manifest.json', 'README.md'):
            shutil.copy2(PACKAGE / name, stage / name)
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        shutil.copytree(PACKAGE / 'resources', stage / 'resources')
        slices = {target: [output]}
        if runtime_root is not None:
            for selected, triple in TRIPLES.items():
                directory = Path(runtime_root) / triple
                if not directory.is_dir():
                    continue
                suffix = '.dll' if selected.endswith('windows') else '.dylib' if selected.endswith('darwin') else '.so'
                prefix = '' if suffix == '.dll' else 'lib'
                names = ['buoy_thinkgearkit']
                natives = [directory / (prefix + name + suffix) for name in names]
                if not all(native.is_file() for native in natives):
                    raise RuntimeError(f'Incomplete runtime slice: {directory}')
                # The freshly built target takes precedence over supplied slices.
                if selected != target:
                    slices[selected] = natives
        if archive.is_file():
            with zipfile.ZipFile(archive) as existing:
                previous = {name.split('/')[1] for name in existing.namelist() if name.startswith('runtime/')}
            missing = previous - {TRIPLES[selected] for selected in slices}
            if missing:
                raise RuntimeError(f'Refusing to drop runtime targets {sorted(missing)}; supply runtime_root')
        for selected, natives in slices.items():
            runtime = stage / 'runtime' / TRIPLES[selected]
            runtime.mkdir(parents=True)
            for native in natives:
                if not native.is_file():
                    raise FileNotFoundError(native)
                shutil.copy2(native, runtime / native.name)
                link = native.parent.parent.parent / 'link' / TRIPLES[selected]
                if link.is_dir():
                    shutil.copytree(link, stage / 'link' / TRIPLES[selected], dirs_exist_ok=True)
        subprocess.run([os.environ.get('BPM_PACKAGER', '/usr/bin/buoy'), 'pkg', 'seal', str(stage), '-o', str(archive)], check=True, timeout=120)
    return archive

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--runtime-root', type=Path, help='Additional runtime slices, verified current by the caller')
    args = parser.parse_args()
    check()
    if not args.check_only:
        print(seal(*build(), archive=args.output, runtime_root=args.runtime_root))
