#!/usr/bin/env python3
"""Build host AudioCueKit bridge, verify the public module, seal an isolated package."""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/audiocuekit'
TARGETS = ('x86_64-linux', 'arm64-linux', 'x86_64-darwin', 'arm64-darwin', 'x86_64-windows', 'arm64-windows')
TRIPLES = {'x86_64-linux': 'x86_64-unknown-linux-gnu', 'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-darwin': 'x86_64-apple-macos', 'arm64-darwin': 'arm64-apple-macos', 'x86_64-windows': 'x86_64-pc-windows-msvc', 'arm64-windows': 'aarch64-pc-windows-msvc'}

def check(source):
    for target in TARGETS:
        result = subprocess.run([os.environ.get('BUOY', 'buoy'), str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=ROOT, capture_output=True, text=True, timeout=90)
        diagnostic = json.loads(result.stdout)
        if result.returncode or diagnostic['errors']:
            raise RuntimeError(f'{source} {target}: {diagnostic}\n{result.stderr}')
        if diagnostic.get('warnings'):
            print(diagnostic['warnings'])

def _command(value):
    """Accept argv directly; preserve quoted Windows compiler paths."""
    if not isinstance(value, str):
        return list(value)
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part
            for part in shlex.split(value, posix=os.name != 'nt')]


def _host_target():
    machine = os.environ.get('PROCESSOR_ARCHITEW6432', platform.machine()).lower()
    arch = {'aarch64': 'arm64', 'amd64': 'x86_64', 'x64': 'x86_64'}.get(machine, machine)
    system = {'Linux': 'linux', 'Darwin': 'darwin', 'Windows': 'windows'}[platform.system()]
    return f'{arch}-{system}'


def build(runtime_root=None, target=None, cc=None):
    """Build host by default; target/cc permit an explicitly selected cross slice."""
    target = target or _host_target()
    if target not in TRIPLES:
        raise ValueError(f'Unsupported AudioCueKit target: {target}')
    arch, system = target.split('-', 1)
    triple = TRIPLES[target]
    name = 'buoy_audiocuekit.dll' if system == 'windows' else 'libbuoy_audiocuekit.' + ('dylib' if system == 'darwin' else 'so')
    runtime_root = Path(runtime_root) if runtime_root is not None else PACKAGE / 'runtime'
    output = (runtime_root / triple / name).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    selected = cc or os.environ.get('CC')
    if not selected:
        if system == 'windows' and platform.system() == 'Windows':
            selected = shutil.which('clang-cl') or shutil.which('cl') or shutil.which('gcc')
            if not selected:
                raise RuntimeError('AudioCueKit needs clang-cl/cl in a Visual Studio developer shell, or MinGW gcc; pass --cc or set CC.')
        elif target != _host_target() and system == 'windows':
            selected = {'x86_64': 'x86_64-w64-mingw32-gcc', 'arm64': 'aarch64-w64-mingw32-gcc'}[arch]
        else:
            selected = 'cc'
    compiler = _command(selected)
    driver = Path(compiler[0]).name.lower().removesuffix('.exe')
    source = str(PACKAGE / 'native/bridge.c')
    if system == 'windows':
        link = (runtime_root.parent / 'link' / triple / 'buoy_audiocuekit.lib').resolve()
        link.parent.mkdir(parents=True, exist_ok=True)
        if driver in ('cl', 'clang-cl'):
            flags = ['/nologo', '/std:c11', '/O2', '/W4', '/WX', '/D_CRT_SECURE_NO_WARNINGS', '/LD']
            if driver == 'clang-cl':
                flags += ['--target=' + ('aarch64' if arch == 'arm64' else arch) + '-pc-windows-msvc']
            command = compiler + flags + [source, '/Fo' + str(output.parent / 'bridge.obj'),
                      '/Fe' + str(output), '/link', '/IMPLIB:' + str(link),
                      '/MACHINE:' + ('ARM64' if arch == 'arm64' else 'X64')]
        else:
            command = compiler + ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                       '-shared', source, '-Wl,--out-implib,' + str(link), '-o', str(output)]
    else:
        flags = ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-fPIC']
        if system == 'darwin':
            flags += ['-dynamiclib', '-arch', 'arm64' if arch == 'arm64' else 'x86_64',
                      '-mmacosx-version-min=11.0', '-Wl,-install_name,@rpath/' + name]
            libs = []
        else:
            flags += ['-shared', '-Wl,-soname,' + name]
            libs = ['-ldl', '-lm']
        command = compiler + flags + [source] + libs + ['-o', str(output)]
    subprocess.run(command, check=True, timeout=120)
    return target, output

def seal(target, output, archive=None):
    archive = Path(archive) if archive is not None else ROOT / 'packages/audiocuekit-0.1.0.buipkg'
    archive.parent.mkdir(parents=True, exist_ok=True)
    packager = os.environ.get('BPM_PACKAGER', '/usr/bin/buoy' if Path('/usr/bin/buoy').is_file() else 'buoy')
    with tempfile.TemporaryDirectory(prefix='audiocuekit-seal-') as tmp:
        stage = Path(tmp)
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        documentation = PACKAGE / 'resources' / 'buoy-docs'
        if documentation.is_dir():
            shutil.copytree(documentation, stage / 'resources' / 'buoy-docs')
        destination = stage / 'runtime' / TRIPLES[target]
        destination.mkdir(parents=True)
        shutil.copy2(output, destination / output.name)
        link = output.parent.parent.parent / 'link' / TRIPLES[target]
        if link.exists():
            shutil.copytree(link, stage / 'link' / TRIPLES[target])
        subprocess.run([packager, 'pkg', 'seal', str(stage), '-o', str(archive)], check=True, timeout=120)
    print(archive)
    return archive

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--target', choices=TARGETS, help='Native slice; defaults to host architecture/OS')
    parser.add_argument('--cc', help='C compiler command (cl, clang-cl or GCC-style driver)')
    parser.add_argument('--runtime-root', type=Path, help='Native output root')
    parser.add_argument('--archive', type=Path, help='Sealed package output')
    args = parser.parse_args()
    check(PACKAGE / 'modules/AudioCueKit.bui')
    if not args.check_only:
        seal(*build(args.runtime_root, target=args.target, cc=args.cc), archive=args.archive)
