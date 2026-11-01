#!/usr/bin/env python3
"""Check TextMetrics and its consumer on six targets, test the host, seal package."""
import argparse
import hashlib
import platform
import shlex
import struct
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from font_package_artifacts import atomic_copy, prepare_manual, publish_archive, verify_archive
TARGETS = ('arm64-darwin', 'x86_64-darwin', 'arm64-linux',
           'x86_64-linux', 'arm64-windows', 'x86_64-windows')
ARCHIVE = 'textmetrics-0.1.0.buipkg'


PACKAGE = ROOT / 'libraries/textmetrics'
MAC_TARGETS = {'arm64-darwin': 'arm64-apple-macos', 'x86_64-darwin': 'x86_64-apple-macos'}


def source_digest():
    return hashlib.sha256((PACKAGE / 'native/macos.c').read_bytes()).hexdigest()


def verify_mac_architecture(binary, target):
    data = binary.read_bytes()
    expected = 0x100000C if target == 'arm64-darwin' else 0x1000007
    actual = struct.unpack_from('<I', data, 4)[0] if len(data) >= 8 and data[:4] == b'\xcf\xfa\xed\xfe' else None
    if actual != expected:
        raise RuntimeError(f'{binary}: architecture mismatch for {target}')


def build_native(target=None, cc=None):
    if sys.platform != 'darwin':
        raise RuntimeError('The Core Text bridge requires an Apple SDK host')
    target = target or ('arm64-darwin' if platform.machine() == 'arm64' else 'x86_64-darwin')
    if target not in MAC_TARGETS:
        raise RuntimeError('Only macOS bridge targets are supported')
    output = PACKAGE / 'runtime' / MAC_TARGETS[target] / 'libbuoy_textmetrics.dylib'
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = output.with_name(output.name + '.source-sha256')
    receipt.unlink(missing_ok=True)
    command = shlex.split(cc or os.environ.get('CC', 'cc')) + [
        '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-dynamiclib',
        '-arch', target.split('-')[0], '-mmacosx-version-min=11.0',
        '-Wl,-install_name,@rpath/' + output.name, str(PACKAGE / 'native/macos.c'),
        '-o', str(output), '-framework', 'CoreText', '-framework', 'CoreFoundation']
    subprocess.run(command, check=True)
    verify_mac_architecture(output, target)
    receipt.write_text(source_digest() + '\n')
    return output


def stage_library(destination):
    prepare_manual(PACKAGE)
    destination.mkdir()
    shutil.copy2(PACKAGE / 'manifest.json', destination / 'manifest.json')
    shutil.copytree(PACKAGE / 'modules', destination / 'modules')
    shutil.copytree(PACKAGE / 'resources', destination / 'resources')
    for target, triple in MAC_TARGETS.items():
        binary = PACKAGE / 'runtime' / triple / 'libbuoy_textmetrics.dylib'
        receipt = binary.with_name(binary.name + '.source-sha256')
        if not binary.is_file():
            continue
        if not receipt.is_file() or receipt.read_text().strip() != source_digest():
            raise RuntimeError(f'{binary}: stale native slice; rebuild before sealing')
        verify_mac_architecture(binary, target)
        folder = destination / 'runtime' / triple
        folder.mkdir(parents=True)
        shutil.copy2(binary, folder / binary.name)


def packager(compiler):
    executable = shutil.which(compiler)
    if executable and Path(executable).resolve() == Path('/usr/local/bin/buoy'):
        if b'/usr/bin/buoy' in Path(executable).read_bytes()[:4096]:
            return '/usr/bin/buoy'
    return compiler


def check(compiler, source, target, work):
    run = subprocess.run([compiler, str(source), '--check', '--diagnostic-format',
                          'json', '--target', target], cwd=work,
                         capture_output=True, text=True, timeout=60)
    try:
        diagnostics = json.loads(run.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'{source.name} / {target}: invalid diagnostics\n'
                           f'{run.stdout}\n{run.stderr}') from error
    # Buoy can exit zero despite semantic errors: inspect every diagnostic array.
    if run.returncode or diagnostics.get('errors') != []:
        raise RuntimeError(f'{source.name} / {target}: {diagnostics}\n{run.stderr}')
    for warning in diagnostics.get('warnings', []):
        print(f'{source.name} / {target} warning: {warning}')
    print(f'CHECK {source.name} {target}: errors=[]', flush=True)


def build(check_only=False, skip_runtime=False):
    compiler = os.environ.get('BUOY', 'buoy')
    if sys.platform == 'darwin':
        build_native()
    with tempfile.TemporaryDirectory(prefix='textmetrics-build-') as directory:
        work = Path(directory)
        folder = work / 'library'
        stage_library(folder)
        packages = work / 'packages'
        packages.mkdir()
        archive = packages / ARCHIVE
        subprocess.run([packager(compiler), 'pkg', 'seal', str(folder), '-o', str(archive)],
                       cwd=work, check=True, timeout=60)
        consumer = work / 'textmetrics_native_test.bui'
        shutil.copy2(ROOT / 'tests/textmetrics_native_test.bui', consumer)
        sources = sorted((folder / 'modules').glob('*.bui')) + [consumer]
        for target in TARGETS:
            for source in sources:
                check(compiler, source, target, work)
        if not skip_runtime:
            executable = work / ('textmetrics-test.exe' if os.name == 'nt' else 'textmetrics-test')
            subprocess.run([compiler, str(consumer), '-o', str(executable)],
                           cwd=work, check=True, timeout=120)
            if sys.platform.startswith("linux"):
                subprocess.run([sys.executable, str(ROOT / "tests/textmetrics_native_reference.py"),
                                str(executable)], cwd=work, check=True, timeout=60)
            else:
                subprocess.run([str(executable)], cwd=work, check=True, timeout=60)
        if not check_only:
            print(publish_archive(archive, PACKAGE, ROOT))
    print('Six-target compiler checks passed. ' +
          ('Host runtime skipped.' if skip_runtime else 'Host runtime passed.'), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-only', action='store_true', help='Build an Apple SDK native bridge only')
    parser.add_argument('--target', choices=tuple(MAC_TARGETS))
    parser.add_argument('--cc')
    parser.add_argument('--check-only', action='store_true', help='Do not publish archives')
    parser.add_argument('--skip-runtime', action='store_true', help='Compiler checks only')
    args = parser.parse_args()
    if args.native_only:
        print(build_native(args.target, args.cc))
    else:
        build(args.check_only, args.skip_runtime)
