#!/usr/bin/env python3
"""Build vendored zlib stream bridge and seal only current native slices."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/compressionkit'
TARGETS = {
    'arm64-darwin': 'arm64-apple-macos', 'x86_64-darwin': 'x86_64-apple-macos',
    'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-linux': 'x86_64-unknown-linux-gnu',
    'arm64-windows': 'aarch64-pc-windows-msvc', 'x86_64-windows': 'x86_64-pc-windows-msvc',
}
CORE = 'adler32 crc32 deflate infback inffast inflate inftrees trees zutil'.split()


def host_target():
    cpu = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    return cpu + '-' + {'linux': 'linux', 'darwin': 'darwin', 'win32': 'windows'}[sys.platform]


def digest():
    value = hashlib.sha256()
    for name in ['bridge.c', 'zlib-1.3.2.tar.gz', 'sources.json']:
        value.update((PACKAGE / 'native' / name).read_bytes())
    value.update(Path(__file__).read_bytes())
    return value.hexdigest()


def runtime_path(target):
    filename = ('buoy_compressionkit.dll' if target.endswith('windows') else
                'libbuoy_compressionkit.' + ('dylib' if target.endswith('darwin') else 'so'))
    return PACKAGE / 'runtime' / TARGETS[target] / filename


def build_native(target=None, cc=None):
    target = target or host_target()
    output = runtime_path(target)
    output.parent.mkdir(parents=True, exist_ok=True)
    archive = PACKAGE / 'native/zlib-1.3.2.tar.gz'
    source = json.loads((PACKAGE / 'native/sources.json').read_text())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != source['sha256']:
        raise RuntimeError('Vendored zlib archive hash mismatch')
    compiler = shlex.split(cc or os.environ.get('CC', 'cc'))
    with tempfile.TemporaryDirectory(prefix='compressionkit-native-') as directory:
        work = Path(directory)
        with tarfile.open(archive) as tar:
            if hasattr(tarfile, 'data_filter'):
                tar.extractall(work, filter='data')
            else:
                # Older Python has no extraction filters: allow only plain
                # files/directories contained in the staging directory.
                for member in tar.getmembers():
                    target_path = (work / member.name).resolve()
                    if not target_path.is_relative_to(work.resolve()) or not (member.isfile() or member.isdir()):
                        raise ValueError('Unsafe zlib archive member')
                tar.extractall(work)
        zlib = work / 'zlib-1.3.2'
        flags = ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-I' + str(zlib)]
        if target.endswith('windows'):
            link = PACKAGE / 'link' / TARGETS[target] / 'buoy_compressionkit.lib'
            link.parent.mkdir(parents=True, exist_ok=True)
            flags += ['-shared', '-static-libgcc', '-Wl,--out-implib,' + str(link)]
        elif target.endswith('darwin'):
            flags += ['-fPIC', '-fvisibility=hidden', '-dynamiclib', '-arch', target.split('-')[0], '-mmacosx-version-min=11.0',
                      '-Wl,-install_name,@rpath/' + output.name]
        else:
            flags += ['-fPIC', '-fvisibility=hidden', '-shared', '-Wl,-soname,' + output.name]
        subprocess.run(compiler + flags + [str(PACKAGE / 'native/bridge.c')] +
                       [str(zlib / (name + '.c')) for name in CORE] + ['-o', str(output)], check=True, timeout=180)
    output.with_name(output.name + '.source-sha256').write_text(digest() + '\n')
    print(f'Built {output}', flush=True)
    return output


def seal():
    compiler = os.environ.get('BUOY', 'buoy')
    for source in (PACKAGE / 'modules').glob('*.bui'):
        for target in TARGETS:
            value = subprocess.run([compiler, str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=ROOT, capture_output=True, text=True, timeout=120)
            data = json.loads(value.stdout)
            if value.returncode or data['errors']:
                raise RuntimeError(f'{target}: {data}\n{value.stderr}')
            if data['warnings']:
                print(source, target, data['warnings'])
    with tempfile.TemporaryDirectory(prefix='compressionkit-seal-') as directory:
        stage = Path(directory)
        for name in ['modules', 'resources']:
            shutil.copytree(PACKAGE / name, stage / name)
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        included = []
        for target, triple in TARGETS.items():
            binary = runtime_path(target)
            receipt = binary.with_name(binary.name + '.source-sha256')
            if not binary.is_file() or not receipt.is_file() or receipt.read_text().strip() != digest():
                continue
            destination = stage / 'runtime' / triple
            destination.mkdir(parents=True)
            shutil.copy2(binary, destination / binary.name)
            if target.endswith('windows'):
                link = PACKAGE / 'link' / triple / 'buoy_compressionkit.lib'
                if not link.is_file():
                    raise RuntimeError('Missing Windows import library: ' + str(link))
                destination = stage / 'link' / triple
                destination.mkdir(parents=True)
                shutil.copy2(link, destination / link.name)
            included.append(target)
        if len(included) != len(TARGETS):
            raise RuntimeError('Build at least one current native slice before sealing')
        (stage / 'resources/backend.json').write_text(json.dumps({
            'zlibVersion': '1.3.2', 'nativeSourceSha256': digest(), 'targets': included,
        }, indent=2) + '\n')
        output = ROOT / 'packages/compressionkit-0.1.0.buipkg'
        subprocess.run([os.environ.get('BPM_PACKAGER', '/usr/bin/buoy'), 'pkg', 'seal', str(stage), '-o', str(output)], check=True, timeout=180)
    shutil.copy2(output, ROOT / 'dist' / output.name)
    print(f'Sealed {len(included)} current native slices', flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--cc')
    parser.add_argument('--native-only', action='store_true')
    parser.add_argument('--seal-only', action='store_true')
    args = parser.parse_args()
    if not args.seal_only:
        build_native(args.target, args.cc)
    if not args.native_only:
        seal()


if __name__ == '__main__':
    main()
