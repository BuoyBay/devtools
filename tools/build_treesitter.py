#!/usr/bin/env python3
"""Build a pinned native tree-sitter package and a JSON grammar test fixture.

Run on each supported OS/architecture to populate its package runtime directory.
An explicitly selected target requires a compiler/SDK configured for that target.
"""
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
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/treesitter'
CACHE = ROOT / '.buoy/treesitter'
TARGETS = {
    'arm64-darwin': 'arm64-apple-macos',
    'x86_64-darwin': 'x86_64-apple-macos',
    'arm64-linux': 'aarch64-unknown-linux-gnu',
    'x86_64-linux': 'x86_64-unknown-linux-gnu',
    'arm64-windows': 'aarch64-pc-windows-msvc',
    'x86_64-windows': 'x86_64-pc-windows-msvc',
}
SOURCES = {
    'tree-sitter': ('0.25.10', 'ad5040537537012b16ef6e1210a572b927c7cdc2b99d1ee88d44a7dcdc3ff44c'),
    'tree-sitter-json': ('0.24.8', 'acf6e8362457e819ed8b613f2ad9a0e1b621a77556c296f3abea58f7880a9213'),
}


def source_tree(name):
    version, expected = SOURCES[name]
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / f'{name}-{version}.tar.gz'
    if not archive.exists():
        url = f'https://codeload.github.com/tree-sitter/{name}/tar.gz/refs/tags/v{version}'
        with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                with urllib.request.urlopen(url, timeout=120) as response:
                    shutil.copyfileobj(response, temporary)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
        if hashlib.sha256(temporary_path.read_bytes()).hexdigest() != expected:
            temporary_path.unlink()
            raise RuntimeError(f'{name}: downloaded source SHA256 mismatch')
        temporary_path.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f'{archive}: cached source SHA256 mismatch; remove it and retry')
    # Extract afresh from the verified archive; reject links and escaping paths.
    with tempfile.TemporaryDirectory(dir=CACHE, prefix='extract-') as directory:
        extraction = Path(directory)
        with tarfile.open(archive) as contents:
            for member in contents.getmembers():
                destination = (extraction / member.name).resolve()
                if not destination.is_relative_to(extraction.resolve()) or not (member.isdir() or member.isfile()):
                    raise RuntimeError(f'Unsafe source archive member: {member.name}')
            filtering = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
            contents.extractall(extraction, **filtering)
        destination = CACHE / f'{name}-{version}'
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(extraction / destination.name), destination)
    return destination


def host_target():
    architecture = platform.machine().lower()
    if architecture in ('aarch64', 'arm64'):
        architecture = 'arm64'
    elif architecture in ('amd64', 'x86_64'):
        architecture = 'x86_64'
    else:
        raise RuntimeError(f'Unsupported host architecture: {architecture}')
    os_name = {'darwin': 'darwin', 'linux': 'linux', 'win32': 'windows'}.get(sys.platform)
    if os_name is None:
        raise RuntimeError(f'Unsupported host OS: {sys.platform}')
    return f'{architecture}-{os_name}'


def build_native(target=None, cc=None, cflags=()):
    target = target or host_target()
    if target != host_target() and not (cc or os.environ.get('CC') or cflags):
        if not (sys.platform == 'darwin' and target.endswith('-darwin')):
            raise RuntimeError('A non-host target requires --cc/CC or --cflag with a configured cross toolchain')
    triple = TARGETS[target]
    runtime_source = source_tree('tree-sitter')
    grammar_source = source_tree('tree-sitter-json')
    runtime_dir = PACKAGE / 'runtime' / triple
    fixture_dir = ROOT / 'dist/treesitter' / triple
    runtime_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    bridge = PACKAGE / 'native/bridge.c'
    if not bridge.is_file():
        raise RuntimeError(f'Missing native bridge: {bridge}')
    compiler = shlex.split(cc or os.environ.get('CC', 'cl' if sys.platform == 'win32' else 'cc'))
    runtime_inputs = [bridge, runtime_source / 'lib/src/lib.c']
    includes = [runtime_source / 'lib/include', runtime_source / 'lib/src']
    windows = target.endswith('-windows')
    msvc = Path(compiler[0]).stem.lower() in ('cl', 'clang-cl')
    if windows:
        runtime = runtime_dir / 'buoy_treesitter.dll'
        grammar = fixture_dir / 'tree_sitter_json.dll'
    elif target.endswith('-darwin'):
        runtime = runtime_dir / 'libbuoy_treesitter.dylib'
        grammar = fixture_dir / 'libtree_sitter_json.dylib'
    else:
        runtime = runtime_dir / 'libbuoy_treesitter.so'
        grammar = fixture_dir / 'libtree_sitter_json.so'
    with tempfile.TemporaryDirectory(prefix='buoy-treesitter-build-') as directory:
        work = Path(directory)
        if msvc:
            if not windows:
                raise RuntimeError('cl/clang-cl requires a Windows target')
            link_dir = PACKAGE / 'link' / triple
            link_dir.mkdir(parents=True, exist_ok=True)
            command = compiler + ['/nologo', '/LD', '/O2', '/std:c11', '/D_CRT_SECURE_NO_WARNINGS']
            subprocess.run(command + list(cflags) + [f'/I{path}' for path in includes] +
                           [str(path) for path in runtime_inputs] + [f'/Fe:{runtime}', '/link',
                           f'/IMPLIB:{link_dir / "buoy_treesitter.lib"}'], cwd=work, check=True)
            subprocess.run(command + list(cflags) + [f'/I{grammar_source / "src"}',
                           str(grammar_source / 'src/parser.c'), f'/Fe:{grammar}'], cwd=work, check=True)
        else:
            common = compiler + ['-O2', '-std=c11', '-D_DEFAULT_SOURCE'] + list(cflags)
            if windows:
                # MinGW emits an import archive understood by Buoy's COFF
                # linker; keep its .lib name consistent with the MSVC build.
                link_dir = PACKAGE / 'link' / triple
                link_dir.mkdir(parents=True, exist_ok=True)
                shared = ['-shared']
                runtime_link = ['-Wl,--out-implib,' + str(link_dir / 'buoy_treesitter.lib')]
                grammar_link = []
            elif target.endswith('-darwin'):
                common.append('-fPIC')
                shared = ['-dynamiclib', '-arch', target.split('-')[0], '-mmacosx-version-min=11.0']
                runtime_link = ['-Wl,-install_name,@rpath/' + runtime.name]
                grammar_link = ['-Wl,-install_name,@rpath/' + grammar.name]
            else:
                common.append('-fPIC')
                shared = ['-shared']
                runtime_link = ['-Wl,-soname,' + runtime.name, '-ldl']
                grammar_link = ['-Wl,-soname,' + grammar.name]
            subprocess.run(common + shared + [f'-I{path}' for path in includes] +
                           [str(path) for path in runtime_inputs] + ['-o', str(runtime)] + runtime_link,
                           cwd=work, check=True)
            subprocess.run(common + shared + [f'-I{grammar_source / "src"}',
                           str(grammar_source / 'src/parser.c'), '-o', str(grammar)] + grammar_link,
                           cwd=work, check=True)
    licenses = PACKAGE / 'resources/treesitter-licenses'
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(runtime_source / 'LICENSE', licenses / 'tree-sitter-LICENSE.txt')
    shutil.copyfile(grammar_source / 'LICENSE', fixture_dir / 'tree-sitter-json-LICENSE.txt')
    print(f'Built runtime: {runtime}\nBuilt test grammar: {grammar}')
    return runtime, grammar


def check_sources():
    sources = sorted((PACKAGE / 'modules').glob('*.bui'))
    if not sources:
        raise RuntimeError('No tree-sitter Buoy modules found')
    for source in sources:
        for target in TARGETS:
            completed = subprocess.run([os.environ.get('BUOY', 'buoy'), str(source), '--check', '--diagnostic-format', 'json',
                                        '--target', target], cwd=ROOT, capture_output=True, text=True)
            diagnostics = json.loads(completed.stdout)
            if completed.returncode or diagnostics.get('errors'):
                raise RuntimeError(f'{source} ({target}): {completed.stdout}\n{completed.stderr}')
            if diagnostics.get('warnings'):
                print(f'{source.name} ({target}): {diagnostics["warnings"]}')


def seal():
    check_sources()
    output = ROOT / 'packages/treesitter-0.1.0.buipkg'
    output.parent.mkdir(exist_ok=True)
    # The local Linux launcher prepends linker flags to every command, including
    # pkg; package subcommands must reach the actual compiler before any flags.
    compiler = os.environ.get('BUOY', 'buoy')
    located = shutil.which(compiler)
    if located and Path(located).resolve() == Path('/usr/local/bin/buoy'):
        launcher = Path(located).read_bytes()[:4096]
        if launcher.startswith(b'#!') and b'/usr/bin/buoy' in launcher:
            compiler = '/usr/bin/buoy'
    subprocess.run([compiler, 'pkg', 'seal', str(PACKAGE), '-o', str(output)], cwd=ROOT, check=True)
    (ROOT / 'dist').mkdir(exist_ok=True)
    shutil.copyfile(output, ROOT / 'dist' / output.name)
    print(f'Packaged: {output}')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TARGETS, help='Native build target; defaults to the host')
    parser.add_argument('--cc', help='C compiler command; defaults to CC or the host compiler')
    parser.add_argument('--cflag', action='append', default=[], help='Additional C compiler flag (repeatable)')
    parser.add_argument('--native-only', action='store_true', help='Build native libraries without checking/sealing')
    parser.add_argument('--seal-only', action='store_true', help='Check and seal existing runtime slices without rebuilding')
    args = parser.parse_args()
    if args.native_only and args.seal_only:
        parser.error('--native-only and --seal-only are mutually exclusive')
    if not args.seal_only:
        build_native(args.target, args.cc, args.cflag)
    if not args.native_only:
        seal()


if __name__ == '__main__':
    main()
