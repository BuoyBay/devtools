#!/usr/bin/env python3
"""Build and seal the shell-free process package for an explicit native target."""
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
import tempfile

from buoy_executables import resolve_packager
from build_support import native_source_digest, write_native_receipt, verify_native_receipt, TARGETS, host_target, verify_architecture, publish_files, file_hash

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/processkit'
def runtime_path(target=None):
    target = target or host_target()
    filename = 'buoy_processkit.dll' if target.endswith('windows') else 'libbuoy_processkit.' + ('dylib' if target.endswith('darwin') else 'so')
    return PACKAGE / 'runtime' / TARGETS[target] / filename


def source_digest():
    return native_source_digest(PACKAGE)


def build_native(target=None, cc=None):
    target = target or host_target()
    fingerprint = source_digest()
    recipe_fingerprint = file_hash(Path(__file__))
    output = runtime_path(target)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_name(output.name + '.source-sha256').unlink(missing_ok=True)
    output.with_name(output.name + '.source.json').unlink(missing_ok=True)
    compiler = shlex.split(cc or os.environ.get('CC', 'cl' if sys.platform == 'win32' else 'cc'))
    source = PACKAGE / 'native/bridge.c'
    if target.endswith('windows'):
        link = PACKAGE / 'link' / TARGETS[target] / 'buoy_processkit.lib'
        link.parent.mkdir(parents=True, exist_ok=True)
        if Path(compiler[0]).stem.lower() in ('cl', 'clang-cl'):
            import tempfile
            with tempfile.TemporaryDirectory(prefix='processkit-cl-') as directory:
                command = compiler + ['/nologo', '/LD', '/O2', '/std:c11', '/D_CRT_SECURE_NO_WARNINGS', str(source), f'/Fe:{output}', '/link', f'/IMPLIB:{link}']
                subprocess.run(command, cwd=directory, check=True)
        else:
            command = compiler + ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', str(source), '-o', str(output), '-Wl,--out-implib,' + str(link)]
            subprocess.run(command, check=True)
    else:
        flags = ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', '-fPIC']
        if target.endswith('darwin'):
            flags += ['-dynamiclib', '-arch', target.split('-')[0], '-mmacosx-version-min=11.0', '-Wl,-install_name,@rpath/' + output.name]
        else:
            flags += ['-shared', '-Wl,-soname,' + output.name]
        command = compiler + flags + [str(source), '-o', str(output)]
        subprocess.run(command, check=True)
    verify_architecture(output, target)
    if source_digest() != fingerprint or file_hash(Path(__file__)) != recipe_fingerprint:
        raise RuntimeError('Native inputs changed during compilation; rebuild before sealing')
    print(f'Built {output}', flush=True)
    write_native_receipt(output, target, compiler, command, Path(__file__), fingerprint)
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
    check_sources()
    compiler = resolve_packager()
    output = ROOT / 'packages/processkit-0.1.0.buipkg'
    output.parent.mkdir(exist_ok=True)
    # Never label an old native slice as supporting a newly added scalar API.
    # Keep original artifacts intact; stage only slices rebuilt from this source.
    with tempfile.TemporaryDirectory(prefix='processkit-seal-') as directory:
        stage = Path(directory) / 'library'
        stage.mkdir()
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        documentation = PACKAGE / 'resources' / 'buoy-docs'
        if documentation.is_dir():
            shutil.copytree(documentation, stage / 'resources' / 'buoy-docs')
        included = set()
        for target, triple in TARGETS.items():
            runtime = runtime_path(target)
            receipt = runtime.with_name(runtime.name + '.source-sha256')
            if not runtime.is_file() or not receipt.is_file() or receipt.read_text().strip() != source_digest():
                continue
            if not verify_native_receipt(runtime, target, Path(__file__), source_digest()):
                continue
            verify_architecture(runtime, target)
            destination = stage / 'runtime' / triple
            destination.mkdir(parents=True)
            shutil.copy2(runtime, destination / runtime.name)
            if target.endswith('windows'):
                link = PACKAGE / 'link' / triple / 'buoy_processkit.lib'
                if not link.is_file():
                    raise RuntimeError(f'Missing import library: {link}')
                destination = stage / 'link' / triple
                destination.mkdir(parents=True)
                shutil.copy2(link, destination / link.name)
            included.add(target)
        # A host-only rebuild must never replace the multi-platform release.
        required = set(TARGETS)
        missing = required - included
        if missing:
            raise RuntimeError('Refusing incomplete processkit archive; rebuild current native slices with --native-only for: ' + ', '.join(sorted(missing)))
        sealed = Path(directory) / output.name
        subprocess.run([compiler, 'pkg', 'seal', str(stage), '-o', str(sealed)], cwd=ROOT, check=True)
        publish_files(sealed, [output, ROOT / 'dist' / output.name])
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--cc')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--native-only', action='store_true')
    modes.add_argument('--seal-only', action='store_true')
    modes.add_argument('--check-only', action='store_true', help='Check all six semantic targets without publishing')
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
