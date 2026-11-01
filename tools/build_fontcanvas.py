#!/usr/bin/env python3
"""Build Windows FontCanvas, verify Buoy targets, seal only current native slices."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from font_package_artifacts import atomic_copy, prepare_manual, publish_archive, verify_archive
PACKAGE = ROOT / 'libraries/fontcanvas'
TARGETS = ('x86_64-linux', 'arm64-linux', 'x86_64-darwin', 'arm64-darwin',
           'x86_64-windows', 'arm64-windows')
WINDOWS = {'x86_64-windows': ('x86_64-pc-windows-msvc', 0x8664),
           'arm64-windows': ('aarch64-pc-windows-msvc', 0xAA64)}


def digest():
    return hashlib.sha256((PACKAGE / 'native/bridge.cpp').read_bytes()).hexdigest()


def architecture(path, target):
    data = path.read_bytes()
    offset = struct.unpack_from('<I', data, 60)[0] if data[:2] == b'MZ' else 0
    machine = struct.unpack_from('<H', data, offset + 4)[0] if data[offset:offset+4] == b'PE\0\0' else 0
    if machine != WINDOWS[target][1]:
        raise RuntimeError(f'{path}: wrong architecture for {target}')


def native(target='x86_64-windows', cxx=None):
    triple, _ = WINDOWS[target]
    runtime = PACKAGE / 'runtime' / triple / 'buoy_fontcanvas.dll'
    link = PACKAGE / 'link' / triple / 'buoy_fontcanvas.lib'
    runtime.parent.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    receipt = runtime.with_suffix('.dll.source-sha256')
    receipt.unlink(missing_ok=True)
    compiler = shlex.split(cxx or os.environ.get('CXX', 'x86_64-w64-mingw32-g++'))
    subprocess.run(compiler + ['-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                               '-shared', '-static', '-static-libgcc', '-static-libstdc++',
                               str(PACKAGE / 'native/bridge.cpp'), '-o', str(runtime),
                               '-Wl,--out-implib,' + str(link), '-lgdiplus'], check=True)
    architecture(runtime, target)
    receipt.write_text(digest() + '\n')
    return runtime


def check(sources=None, cwd=ROOT):
    sources = sources or [PACKAGE / 'modules/FontCanvas.bui', ROOT / 'tests/fontcanvas_test.bui']
    for source in sources:
        for target in TARGETS:
            result = subprocess.run(['buoy', str(source), '--check', '--diagnostic-format',
                                     'json', '--target', target], cwd=cwd, capture_output=True,
                                    text=True, timeout=60)
            diagnostics = json.loads(result.stdout)
            if result.returncode or diagnostics.get('errors') != []:
                raise RuntimeError(f'{source} {target}: {diagnostics}\n{result.stderr}')
            for warning in diagnostics.get('warnings', []):
                print(source, target, warning)
    print(f'FontCanvas: {len(sources) * 6} compiler checks passed', flush=True)


def seal():
    prepare_manual(PACKAGE)
    output = ROOT / 'packages/fontcanvas-0.1.0.buipkg'
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fontcanvas-seal-') as directory:
        stage = Path(directory) / 'library'
        stage.mkdir()
        archive = Path(directory) / output.name
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        shutil.copytree(PACKAGE / 'resources', stage / 'resources')
        included = 0
        for target, (triple, _) in WINDOWS.items():
            runtime = PACKAGE / 'runtime' / triple / 'buoy_fontcanvas.dll'
            receipt = runtime.with_suffix('.dll.source-sha256')
            if not runtime.is_file() or not receipt.is_file() or receipt.read_text().strip() != digest():
                continue
            architecture(runtime, target)
            link = PACKAGE / 'link' / triple / 'buoy_fontcanvas.lib'
            if not link.is_file():
                raise RuntimeError(f'Missing import library: {link}')
            destination = stage / 'runtime' / triple
            destination.mkdir(parents=True)
            shutil.copy2(runtime, destination / runtime.name)
            destination = stage / 'link' / triple
            destination.mkdir(parents=True)
            shutil.copy2(link, destination / link.name)
            included += 1
        if included != len(WINDOWS):
            raise RuntimeError('No source-current native slices; build one before sealing')
        compiler = '/usr/bin/buoy' if Path('/usr/bin/buoy').exists() else 'buoy'
        subprocess.run([compiler, 'pkg', 'seal', str(stage), '-o', str(archive)], check=True, cwd=ROOT)
        return publish_archive(archive, PACKAGE, ROOT)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=WINDOWS, default='x86_64-windows')
    parser.add_argument('--cxx')
    parser.add_argument('--seal-only', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    check()
    if not args.check_only:
        if not args.seal_only:
            print(native(args.target, args.cxx))
        print(seal())
