#!/usr/bin/env python3
"""Build host ODBC bridge, verify the public module, seal an isolated package."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/odbckit'
TARGETS = ('x86_64-linux', 'arm64-linux', 'x86_64-darwin', 'arm64-darwin', 'x86_64-windows', 'arm64-windows')
TRIPLES = {'x86_64-linux': 'x86_64-unknown-linux-gnu', 'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-darwin': 'x86_64-apple-macos', 'arm64-darwin': 'arm64-apple-macos', 'x86_64-windows': 'x86_64-pc-windows-msvc', 'arm64-windows': 'aarch64-pc-windows-msvc'}

def input_hashes():
    inputs = [PACKAGE / 'native/bridge.c', PACKAGE / 'modules/ODBCKit.bui', Path(__file__).resolve()]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}

def receipt_path(output):
    return output.parent.parent.parent / 'resources/native-build' / ('odbckit-' + output.parent.name + '.json')

def verify_build(target, output):
    path = receipt_path(output)
    if not path.is_file():
        raise RuntimeError('ODBCKit build receipt missing; rebuild with tools/build_odbckit.py')
    if not output.is_file():
        raise RuntimeError('ODBCKit native binary missing; rebuild')
    receipt = json.loads(path.read_text())
    if receipt.get('target') != target or receipt.get('sourceSha256') != input_hashes():
        raise RuntimeError('ODBCKit build receipt does not match current source; rebuild')
    if receipt.get('binarySha256') != hashlib.sha256(output.read_bytes()).hexdigest():
        raise RuntimeError('ODBCKit native binary does not match build receipt; rebuild')
    return receipt

def verify_archive(archive):
    with zipfile.ZipFile(archive) as contents:
        receipts = [name for name in contents.namelist() if name.startswith('resources/native-build/odbckit-') and name.endswith('.json')]
        if len(receipts) != 1:
            raise RuntimeError('ODBCKit archive must contain its native build receipt')
        receipt = json.loads(contents.read(receipts[0]))
        if receipt.get('sourceSha256') != input_hashes():
            raise RuntimeError('ODBCKit archive receipt is stale for current source')
        target = receipt['target']
        names = [name for name in contents.namelist() if name.startswith('runtime/' + TRIPLES[target] + '/')]
        binaries = [name for name in names if name.endswith(('.so', '.dll', '.dylib'))]
        if len(binaries) != 1 or hashlib.sha256(contents.read(binaries[0])).hexdigest() != receipt['binarySha256']:
            raise RuntimeError('ODBCKit archive native binary differs from build receipt')
        module = 'libraries/odbckit/modules/ODBCKit.bui'
        if hashlib.sha256(contents.read('modules/ODBCKit.bui')).hexdigest() != receipt['sourceSha256'][module]:
            raise RuntimeError('ODBCKit archive module differs from build receipt')
    return receipt

def check(source):
    for target in TARGETS:
        result = subprocess.run([os.environ.get('BUOY', 'buoy'), str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=ROOT, capture_output=True, text=True, timeout=90)
        diagnostic = json.loads(result.stdout)
        if result.returncode or diagnostic['errors']:
            raise RuntimeError(f'{source} {target}: {diagnostic}\n{result.stderr}')
        if diagnostic.get('warnings'):
            print(diagnostic['warnings'])

def build(runtime_root=None):
    arch = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    system = {'Linux': 'linux', 'Darwin': 'darwin', 'Windows': 'windows'}[platform.system()]
    target = f'{arch}-{system}'
    triple = TRIPLES[target]
    name = 'buoy_odbckit.dll' if system == 'windows' else 'libbuoy_odbckit.' + ('dylib' if system == 'darwin' else 'so')
    runtime_root = Path(runtime_root) if runtime_root is not None else PACKAGE / 'runtime'
    output = runtime_root / triple / name
    output.parent.mkdir(parents=True, exist_ok=True)
    cc = shlex.split(os.environ.get('CC', 'cc'))
    flags = ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror']
    libs = ['-lodbc']
    if system == 'windows':
        link = runtime_root.parent / 'link' / triple / 'buoy_odbckit.lib'
        link.parent.mkdir(parents=True, exist_ok=True)
        flags += ['-shared', '-Wl,--out-implib,' + str(link)]
        libs = ['-lodbc32']
    elif system == 'darwin':
        flags += ['-fPIC', '-dynamiclib', '-Wl,-install_name,@rpath/' + name]
        if shutil.which('pkg-config'):
            libs = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'odbc'], text=True))
    else:
        flags += ['-fPIC', '-shared', '-Wl,-soname,' + name]
    sources = input_hashes()
    with tempfile.TemporaryDirectory(prefix='.odbckit-build-', dir=output.parent) as directory:
        staged = Path(directory) / output.name
        subprocess.run(cc + flags + [str(PACKAGE / 'native/bridge.c')] + libs + ['-o', str(staged)], check=True, timeout=120)
        if input_hashes() != sources:
            raise RuntimeError('ODBCKit source changed during native build; retry')
        staged.replace(output)
    receipt = {'schema': 1, 'target': target, 'sourceSha256': sources,
               'binarySha256': hashlib.sha256(output.read_bytes()).hexdigest()}
    path = receipt_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + '\n')
    return target, output

def seal(target, output, archive=None):
    verify_build(target, output)
    archive = Path(archive) if archive is not None else ROOT / 'packages/odbckit-0.1.0.buipkg'
    archive.parent.mkdir(parents=True, exist_ok=True)
    packager = os.environ.get('BPM_PACKAGER', '/usr/bin/buoy' if Path('/usr/bin/buoy').is_file() else 'buoy')
    with tempfile.TemporaryDirectory(prefix='odbckit-seal-') as tmp:
        stage = Path(tmp)
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        documentation = PACKAGE / 'resources' / 'buoy-docs'
        if documentation.is_dir():
            shutil.copytree(documentation, stage / 'resources' / 'buoy-docs')
        receipt_destination = stage / 'resources/native-build'
        receipt_destination.mkdir(parents=True)
        shutil.copy2(receipt_path(output), receipt_destination / receipt_path(output).name)
        destination = stage / 'runtime' / TRIPLES[target]
        destination.mkdir(parents=True)
        shutil.copy2(output, destination / output.name)
        link = output.parent.parent.parent / 'link' / TRIPLES[target]
        if link.exists():
            shutil.copytree(link, stage / 'link' / TRIPLES[target])
        verify_build(target, output)
        subprocess.run([packager, 'pkg', 'seal', str(stage), '-o', str(archive)], check=True, timeout=120)
    verify_archive(archive)
    print(archive)
    return archive

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    check(PACKAGE / 'modules/ODBCKit.bui')
    if not args.check_only:
        seal(*build())
