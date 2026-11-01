#!/usr/bin/env python3
"""Build/seal local read-only Git inspection with a pinned embedded libgit2."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from buoy_executables import resolve_packager
from build_support import publish_files

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/gitkit'
CACHE = ROOT / '.buoy/gitkit'
VERSION = '1.9.7'
SOURCE_SHA256 = '1a4fbe7589e814777ae76b64734ad80f4ecad22cd33a22682a2aaea4ae5375e7'
TARGETS = {
    'arm64-darwin': 'arm64-apple-macos', 'x86_64-darwin': 'x86_64-apple-macos',
    'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-linux': 'x86_64-unknown-linux-gnu',
    'arm64-windows': 'aarch64-pc-windows-msvc', 'x86_64-windows': 'x86_64-pc-windows-msvc',
}


def source_tree():
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / f'libgit2-{VERSION}.tar.gz'
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as temporary:
            partial = Path(temporary.name)
            try:
                with urllib.request.urlopen(f'https://codeload.github.com/libgit2/libgit2/tar.gz/refs/tags/v{VERSION}', timeout=120) as response:
                    shutil.copyfileobj(response, temporary)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        if hashlib.sha256(partial.read_bytes()).hexdigest() != SOURCE_SHA256:
            partial.unlink()
            raise RuntimeError('libgit2 source SHA256 mismatch')
        partial.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError('Cached libgit2 source SHA256 mismatch')
    destination = CACHE / f'libgit2-{VERSION}'
    # Re-extract verified sources, so edits in a prior build cannot silently enter a release.
    with tempfile.TemporaryDirectory(dir=CACHE, prefix='extract-') as directory:
        extraction = Path(directory)
        with tarfile.open(archive) as contents:
            members = []
            for member in contents.getmembers():
                if not (extraction / member.name).resolve().is_relative_to(extraction.resolve()):
                    raise RuntimeError(f'Unsafe archive member: {member.name}')
                # Upstream includes symlink test fixtures; do not extract links or special files.
                if member.isdir() or member.isfile():
                    members.append(member)
            contents.extractall(extraction, members=members, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(extraction / destination.name), destination)
    return destination


def host_target():
    architecture = platform.machine().lower()
    architecture = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(architecture, architecture)
    os_name = {'darwin': 'darwin', 'linux': 'linux', 'win32': 'windows'}.get(sys.platform)
    if architecture not in ('arm64', 'x86_64') or os_name is None:
        raise RuntimeError(f'Unsupported host: {architecture}/{sys.platform}')
    return f'{architecture}-{os_name}'


def verify_architecture(binary, target):
    data = binary.read_bytes()
    expected = target.split('-')[0]
    if data[:4] == b'\x7fELF':
        endian = '<' if data[5] == 1 else '>'
        machine = struct.unpack_from(endian+'H', data, 18)[0]
        actual = {62: 'x86_64', 183: 'arm64'}.get(machine)
    elif data[:4] == b'\xcf\xfa\xed\xfe':
        machine = struct.unpack_from('<I', data, 4)[0]
        actual = {0x01000007: 'x86_64', 0x0100000c: 'arm64'}.get(machine)
    elif data[:2] == b'MZ':
        offset = struct.unpack_from('<I', data, 60)[0]
        if data[offset:offset+4] != b'PE\0\0':
            raise RuntimeError('Invalid PE bridge binary')
        machine = struct.unpack_from('<H', data, offset+4)[0]
        actual = {0x8664: 'x86_64', 0xaa64: 'arm64'}.get(machine)
    else:
        raise RuntimeError('Unrecognized bridge binary format')
    if actual != expected:
        raise RuntimeError(f'Compiler produced {actual}, expected {expected}; configure the matching compiler/SDK')


def build_native(target=None, cc=None, cmake_args=()):
    target = target or host_target()
    source = source_tree()
    build = CACHE / f'build-{target}'
    arguments = ['cmake', '-S', str(PACKAGE / 'native'), '-B', str(build),
                 '-DCMAKE_BUILD_TYPE=Release', f'-DLIBGIT2_SOURCE={source}']
    if cc:
        arguments.append(f'-DCMAKE_C_COMPILER={cc}')
    if target.endswith('-darwin'):
        arguments += [f'-DCMAKE_OSX_ARCHITECTURES={target.split("-")[0]}', '-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0']
    elif target.endswith('-windows') and not host_target().endswith('-windows'):
        arguments += ['-DCMAKE_SYSTEM_NAME=Windows']
    elif target != host_target() and not any('CMAKE_TOOLCHAIN_FILE=' in item for item in cmake_args):
        raise RuntimeError('Non-native Linux targets require an explicit CMake toolchain through --cmake-arg')
    arguments += list(cmake_args)
    for command in [arguments, ['cmake', '--build', str(build), '--config', 'Release', '--target', 'buoy_gitkit', '-j', '4']]:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if completed.returncode:
            raise RuntimeError(completed.stdout)
    triple = TARGETS[target]
    runtime_dir = PACKAGE / 'runtime' / triple
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if target.endswith('-windows'):
        name = 'buoy_gitkit.dll'
        candidates = [build/name, build/('lib'+name), build/'Release'/name]
        runtime_source = next((p for p in candidates if p.is_file()), None)
        link_dir = PACKAGE / 'link' / triple
        link_dir.mkdir(parents=True, exist_ok=True)
        link_candidates = [build/'buoy_gitkit.lib', build/'Release/buoy_gitkit.lib', build/'libbuoy_gitkit.dll.a']
        import_source = next((p for p in link_candidates if p.is_file()), None)
        if import_source is None:
            raise RuntimeError('CMake produced no Windows import library')
    else:
        name = 'libbuoy_gitkit.dylib' if target.endswith('-darwin') else 'libbuoy_gitkit.so'
        runtime_source = build/name
    if runtime_source is None or not runtime_source.is_file():
        raise RuntimeError('CMake produced no bridge runtime')
    verify_architecture(runtime_source, target)
    runtime = runtime_dir/name
    shutil.copy2(runtime_source, runtime)
    if target.endswith('-windows'):
        shutil.copy2(import_source, link_dir/'buoy_gitkit.lib')
    licenses = PACKAGE/'resources/gitkit-licenses'
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source/'COPYING', licenses/'libgit2-COPYING.txt')
    shutil.copy2(source/'AUTHORS', licenses/'libgit2-AUTHORS.txt')
    shutil.copy2(source/'deps/zlib/LICENSE', licenses/'zlib-LICENSE.txt')
    print(f'Built embedded libgit2 {VERSION}: {runtime}', flush=True)
    return runtime


def check_sources():
    for source in sorted((PACKAGE/'modules').glob('*.bui')):
        for target in TARGETS:
            checked = subprocess.run([os.environ.get('BUOY', 'buoy'), str(source), '--check', '--diagnostic-format', 'json', '--target', target],
                                     cwd=ROOT, capture_output=True, text=True)
            diagnostics = json.loads(checked.stdout)
            if checked.returncode or diagnostics['errors']:
                raise RuntimeError(f'{source} ({target}): {checked.stdout}\n{checked.stderr}')
            if diagnostics.get('warnings'):
                print(diagnostics['warnings'])


def seal():
    check_sources()
    compiler = resolve_packager()
    output = ROOT/'packages/gitkit-0.1.0.buipkg'
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gitkit-seal-') as directory:
        sealed = Path(directory) / output.name
        subprocess.run([compiler, 'pkg', 'seal', str(PACKAGE), '-o', str(sealed)], cwd=ROOT, check=True)
        publish_files(sealed, [output, ROOT / 'dist' / output.name])
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-only', action='store_true')
    parser.add_argument('--seal-only', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--cc', help='C compiler executable (no embedded flags)')
    parser.add_argument('--cmake-arg', action='append', default=[])
    args = parser.parse_args()
    if args.check_only:
        if args.native_only or args.seal_only:
            parser.error('--check-only cannot be combined with native/seal modes')
        check_sources()
        return
    if args.native_only and args.seal_only:
        parser.error('--native-only and --seal-only are mutually exclusive')
    if not args.seal_only:
        build_native(args.target, args.cc, args.cmake_arg)
    if not args.native_only:
        seal()


if __name__ == '__main__':
    main()
