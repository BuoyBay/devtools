#!/usr/bin/env python3
"""Build the platform RS256 verifier and seal the ChatGPT OAuth package."""
import argparse
import importlib.util
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

from buoy_executables import add_toolchain_arguments, resolve_compiler, resolve_packager
from build_support import (native_source_digest, write_native_receipt, verify_native_receipt,
                           verify_architecture, publish_files, file_hash, check_source, bundled_package)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/chatgptauth'
TARGETS = {'arm64-darwin':'arm64-apple-macos', 'x86_64-darwin':'x86_64-apple-macos',
           'arm64-linux':'aarch64-unknown-linux-gnu', 'x86_64-linux':'x86_64-unknown-linux-gnu',
           'arm64-windows':'aarch64-pc-windows-msvc', 'x86_64-windows':'x86_64-pc-windows-msvc'}


def host_target():
    architecture = 'arm64' if platform.machine().lower() in ('arm64','aarch64') else 'x86_64'
    return architecture + '-' + {'darwin':'darwin','linux':'linux','win32':'windows'}[sys.platform]


def build_native(target=None, cc=None):
    target = target or host_target()
    fingerprint = native_source_digest(PACKAGE)
    recipe_fingerprint = file_hash(Path(__file__))
    runtime_dir = PACKAGE / 'runtime' / TARGETS[target]
    runtime_dir.mkdir(parents=True, exist_ok=True)
    compiler = shlex.split(cc or os.environ.get('CC', 'cl' if sys.platform == 'win32' else 'cc'))
    source = PACKAGE / 'native/verify.c'
    if target.endswith('windows'):
        output = runtime_dir / 'buoy_chatgptauth.dll'
        link_dir = PACKAGE / 'link' / TARGETS[target]
        link_dir.mkdir(parents=True,exist_ok=True)
        stub = link_dir / 'buoy_chatgptauth.lib'
        if Path(compiler[0]).stem.lower() in ('cl','clang-cl'):
            command = compiler + ['/nologo','/LD','/O2','/std:c11',str(source),f'/Fe:{output}',
                                   '/link','bcrypt.lib',f'/IMPLIB:{stub}']
        else:
            command = compiler + ['-shared','-O2','-std=c11',str(source),'-o',str(output),'-lbcrypt',
                                  '-Wl,--out-implib,'+str(stub)]
    elif target.endswith('darwin'):
        output = runtime_dir / 'libbuoy_chatgptauth.dylib'
        command = compiler + ['-dynamiclib','-O2','-std=c11','-arch',target.split('-')[0],
                              '-mmacosx-version-min=11.0',str(source),'-o',str(output),
                              '-framework','Security','-framework','CoreFoundation',
                              '-Wl,-install_name,@rpath/'+output.name]
    else:
        output = runtime_dir / 'libbuoy_chatgptauth.so'
        command = compiler + ['-shared','-fPIC','-O2','-std=c11','-Wno-deprecated-declarations',
                              str(source),'-o',str(output),'-lcrypto']
    output.with_name(output.name + '.source-sha256').unlink(missing_ok=True)
    output.with_name(output.name + '.source.json').unlink(missing_ok=True)
    subprocess.run(command,check=True)
    verify_architecture(output, target)
    if native_source_digest(PACKAGE) != fingerprint or file_hash(Path(__file__)) != recipe_fingerprint:
        raise RuntimeError('Native inputs changed during compilation; rebuild before sealing')
    write_native_receipt(output, target, compiler, command, Path(__file__), fingerprint)
    print('Built '+str(output))
    return output


def check_sources(compiler=None):
    compiler = resolve_compiler(compiler)
    with tempfile.TemporaryDirectory(prefix='chatgptauth-check-') as directory:
        work = Path(directory)
        (work / 'packages').mkdir()
        archive = bundled_package('crypto', compiler)
        shutil.copy2(archive, work / 'packages' / archive.name)
        shutil.copytree(PACKAGE / 'modules', work / 'modules')
        for source in sorted((work / 'modules').rglob('*.bui')):
            for target in TARGETS:
                check_source(source, target, work, compiler)


def seal(compiler=None, packager=None):
    check_sources(compiler)
    packager = resolve_packager(packager, compiler=resolve_compiler(compiler))
    output=ROOT/'packages/chatgptauth-0.1.0.buipkg'
    with tempfile.TemporaryDirectory(prefix='chatgptauth-seal-') as directory:
        stage = Path(directory) / 'library'
        stage.mkdir()
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        for folder in ('modules', 'resources'):
            if (PACKAGE / folder).is_dir():
                shutil.copytree(PACKAGE / folder, stage / folder)
        included = set()
        for target, triple in TARGETS.items():
            filename = ('buoy_chatgptauth.dll' if target.endswith('windows') else
                        'libbuoy_chatgptauth.' + ('dylib' if target.endswith('darwin') else 'so'))
            runtime = PACKAGE / 'runtime' / triple / filename
            if not runtime.is_file() or not verify_native_receipt(runtime, target, Path(__file__), native_source_digest(PACKAGE)):
                continue
            verify_architecture(runtime, target)
            destination = stage / 'runtime' / triple
            destination.mkdir(parents=True)
            shutil.copy2(runtime, destination / runtime.name)
            if target.endswith('windows'):
                link = PACKAGE / 'link' / triple / 'buoy_chatgptauth.lib'
                if not link.is_file():
                    raise RuntimeError(f'Missing import library: {link}')
                destination = stage / 'link' / triple
                destination.mkdir(parents=True)
                shutil.copy2(link, destination / link.name)
            included.add(target)
        # A host-only rebuild must never replace the multi-platform release.
        required = {'arm64-darwin', 'x86_64-darwin', 'x86_64-windows', 'x86_64-linux'}
        missing = required - included
        if missing:
            raise RuntimeError('Refusing incomplete chatgptauth archive; rebuild current native slices with --native-only for: ' + ', '.join(sorted(missing)))
        sealed = Path(directory) / output.name
        subprocess.run([packager,'pkg','seal',str(stage),'-o',str(sealed)],check=True)
        publish_files(sealed, [output, ROOT/'dist'/output.name])
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',choices=TARGETS)
    parser.add_argument('--cc')
    add_toolchain_arguments(parser)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--native-only',action='store_true')
    group.add_argument('--seal-only',action='store_true')
    group.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    if args.check_only:
        check_sources(args.compiler)
        return
    if not args.seal_only: build_native(args.target,args.cc)
    if not args.native_only: seal(args.compiler,args.packager)

if __name__=='__main__': main()
