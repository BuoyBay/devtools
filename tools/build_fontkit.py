#!/usr/bin/env python3
"""Build and seal the font discovery and registration package for an explicit native target."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from font_package_artifacts import atomic_copy, prepare_manual, publish_archive, verify_archive
from buoy_executables import resolve_packager
PACKAGE = ROOT / 'libraries/fontkit'
TARGETS = {
    'arm64-darwin': 'arm64-apple-macos', 'x86_64-darwin': 'x86_64-apple-macos',
    'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-linux': 'x86_64-unknown-linux-gnu',
    'arm64-windows': 'aarch64-pc-windows-msvc', 'x86_64-windows': 'x86_64-pc-windows-msvc',
}


def host_target():
    architecture = platform.machine().lower()
    architecture = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(architecture, architecture)
    system = {'darwin': 'darwin', 'linux': 'linux', 'win32': 'windows'}.get(sys.platform)
    target = f'{architecture}-{system}'
    if target not in TARGETS:
        raise RuntimeError(f'Unsupported host: {target}')
    return target


def runtime_path(target=None):
    target = target or host_target()
    filename = 'buoy_fontkit.dll' if target.endswith('windows') else 'libbuoy_fontkit.' + ('dylib' if target.endswith('darwin') else 'so')
    return PACKAGE / 'runtime' / TARGETS[target] / filename


def source_digest():
    return hashlib.sha256((PACKAGE / 'native/bridge.c').read_bytes()).hexdigest()


def verify_architecture(binary, target):
    data = binary.read_bytes()
    arm = target.startswith('arm64-')
    if target.endswith('linux'):
        expected = 183 if arm else 62
        actual = struct.unpack_from('<H' if data[5] == 1 else '>H', data, 18)[0] if len(data) >= 20 and data[:4] == b'\x7fELF' and data[4] == 2 and data[5] in (1, 2) else None
    elif target.endswith('windows'):
        expected = 0xAA64 if arm else 0x8664
        offset = struct.unpack_from('<I', data, 60)[0] if data[:2] == b'MZ' else 0
        actual = struct.unpack_from('<H', data, offset + 4)[0] if data[offset:offset+4] == b'PE\0\0' else None
    else:
        expected = 0x100000C if arm else 0x1000007
        actual = struct.unpack_from('<I', data, 4)[0] if data[:4] == b'\xcf\xfa\xed\xfe' else None
    if actual != expected:
        raise RuntimeError(f'{binary}: architecture mismatch for {target}; use a matching native/cross compiler')


def build_native(target=None, cc=None):
    target = target or host_target()
    output = runtime_path(target)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_name(output.name + '.source-sha256').unlink(missing_ok=True)
    compiler = shlex.split(cc or os.environ.get('CC', 'cl' if sys.platform == 'win32' else 'cc'))
    source = PACKAGE / 'native/bridge.c'
    if target.endswith('windows'):
        link = PACKAGE / 'link' / TARGETS[target] / 'buoy_fontkit.lib'
        link.parent.mkdir(parents=True, exist_ok=True)
        if Path(compiler[0]).stem.lower() in ('cl', 'clang-cl'):
            import tempfile
            with tempfile.TemporaryDirectory(prefix='fontkit-cl-') as directory:
                subprocess.run(compiler + ['/nologo', '/LD', '/O2', '/std:c11', '/D_CRT_SECURE_NO_WARNINGS', str(source), f'/Fe:{output}', '/link', f'/IMPLIB:{link}', 'gdi32.lib'], cwd=directory, check=True)
        else:
            subprocess.run(compiler + ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', str(source), '-o', str(output), '-Wl,--out-implib,' + str(link), '-lgdi32'], check=True)
    else:
        flags = ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', '-fPIC']
        if target.endswith('darwin'):
            flags += ['-dynamiclib', '-arch', target.split('-')[0], '-mmacosx-version-min=11.0', '-Wl,-install_name,@rpath/' + output.name]
        else:
            flags += ['-shared', '-Wl,-soname,' + output.name]
        libraries = ['-framework', 'CoreText', '-framework', 'CoreFoundation'] if target.endswith('darwin') else shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'fontconfig', 'pangocairo', 'pangoft2'], text=True))
        subprocess.run(compiler + flags + [str(source), '-o', str(output)] + libraries, check=True)
    verify_architecture(output, target)
    print(f'Built {output}', flush=True)
    output.with_name(output.name + '.source-sha256').write_text(source_digest())
    return output


def check_sources():
    for source in sorted((PACKAGE / 'modules').glob('*.bui')):
        for target in TARGETS:
            completed = subprocess.run([os.environ.get('BUOY', 'buoy'), str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=ROOT, capture_output=True, text=True)
            diagnostics = json.loads(completed.stdout)
            if completed.returncode or diagnostics['errors']:
                raise RuntimeError(f'{source} ({target}): {diagnostics}\n{completed.stderr}')
            if diagnostics.get('warnings'):
                print(diagnostics['warnings'])


def seal():
    prepare_manual(PACKAGE)
    check_sources()
    # Keep the older alias, with the shared explicit override taking precedence.
    compiler = resolve_packager(os.environ.get('BUOY_PACKAGER') if 'BPM_PACKAGER' not in os.environ else None)
    output = ROOT / 'packages/fontkit-0.1.0.buipkg'
    output.parent.mkdir(exist_ok=True)
    # Never label an old native slice as supporting a newly added scalar API.
    # Keep original artifacts intact; stage only slices rebuilt from this source.
    with tempfile.TemporaryDirectory(prefix='fontkit-seal-') as directory:
        stage = Path(directory) / 'library'
        stage.mkdir()
        archive = Path(directory) / output.name
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        shutil.copytree(PACKAGE / 'resources', stage / 'resources')
        count = 0
        for target, triple in TARGETS.items():
            runtime = runtime_path(target)
            receipt = runtime.with_name(runtime.name + '.source-sha256')
            if not runtime.is_file() or not receipt.is_file():
                continue
            try:
                receipt_digest = receipt.read_text().strip()
            except (OSError, UnicodeError) as exc:
                raise RuntimeError(f'Cannot read native source receipt {receipt}: {exc}') from exc
            if receipt_digest != source_digest():
                continue
            verify_architecture(runtime, target)
            destination = stage / 'runtime' / triple
            destination.mkdir(parents=True)
            shutil.copy2(runtime, destination / runtime.name)
            if target.endswith('windows'):
                link = PACKAGE / 'link' / triple / 'buoy_fontkit.lib'
                if not link.is_file():
                    raise RuntimeError(f'Missing import library: {link}')
                destination = stage / 'link' / triple
                destination.mkdir(parents=True)
                shutil.copy2(link, destination / link.name)
            count += 1
        if count != len(TARGETS):
            raise RuntimeError('No current native slices; rebuild with tools/build_fontkit.py first')
        subprocess.run([compiler, 'pkg', 'seal', str(stage), '-o', str(archive)], cwd=ROOT, check=True)
        return publish_archive(archive, PACKAGE, ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--cc')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--native-only', action='store_true')
    modes.add_argument('--seal-only', action='store_true')
    modes.add_argument('--check-only', action='store_true', help='Check six semantic targets without publishing')
    args = parser.parse_args()
    if args.check_only:
        check_sources()
        return
    if not args.seal_only:
        build_native(args.target, args.cc)
    if not args.native_only:
        seal()


if __name__ == '__main__':
    main()
