#!/usr/bin/env python3
"""Build the updatekit Rust bridge/helper and seal only current native slices."""
import argparse
import ctypes
import struct
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/updatekit'
NATIVE = PACKAGE / 'native'
TARGETS = {
    'arm64-darwin': ('aarch64-apple-darwin', 'arm64-apple-macos'),
    'x86_64-darwin': ('x86_64-apple-darwin', 'x86_64-apple-macos'),
    'arm64-linux': ('aarch64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu'),
    'x86_64-linux': ('x86_64-unknown-linux-gnu', 'x86_64-unknown-linux-gnu'),
    'arm64-windows': ('aarch64-pc-windows-msvc', 'aarch64-pc-windows-msvc'),
    'x86_64-windows': ('x86_64-pc-windows-msvc', 'x86_64-pc-windows-msvc'),
}


def host_target():
    arch = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    system = {'Darwin': 'darwin', 'Linux': 'linux', 'Windows': 'windows'}.get(platform.system())
    value = f'{arch}-{system}'
    if value not in TARGETS:
        raise RuntimeError(f'Unsupported host: {value}')
    return value


def source_digest():
    digest = hashlib.sha256()
    inputs = [("native/" + item.relative_to(NATIVE).as_posix(), item)
              for item in [NATIVE / 'Cargo.toml', NATIVE / 'Cargo.lock', *NATIVE.glob('src/*.rs')]]
    inputs.append(('tools/build_updatekit.py', Path(__file__)))
    for label, item in sorted(inputs):
        digest.update(label.encode())
        digest.update(item.read_bytes().replace(b'\r\n', b'\n'))
    return digest.hexdigest()


def align_macho_string_pool(binary):
    """Use existing LINKEDIT padding to align a thin dylib's string table.

    Some ld outputs have four-byte alignment, rejected by macOS 27 dyld. Never
    move code, symbols, signatures or any other LINKEDIT payload. Signing must
    follow this operation. Refuse files without provably unused trailing padding.
    """
    data = bytearray(Path(binary).read_bytes())
    if len(data) < 32 or struct.unpack_from('<II', data)[0] != 0xfeedfacf:
        raise RuntimeError('Expected a thin little-endian Mach-O dylib')
    if struct.unpack_from('<I', data, 12)[0] != 6:
        raise RuntimeError('Expected MH_DYLIB')
    commands = struct.unpack_from('<I', data, 16)[0]
    cursor, symbols, signature = 32, None, None
    other_offsets = []
    linkedit_commands = {0x1d, 0x1e, 0x26, 0x29, 0x2b, 0x2e, 0x80000033, 0x80000034}
    for _ in range(commands):
        if cursor + 8 > len(data): raise RuntimeError('Truncated Mach-O load command')
        command, size = struct.unpack_from('<II', data, cursor)
        if size < 8 or cursor + size > len(data): raise RuntimeError('Invalid Mach-O load command')
        if command == 2:
            if size < 24: raise RuntimeError('Invalid LC_SYMTAB')
            symbols = cursor, *struct.unpack_from('<IIII', data, cursor + 8)
        if command in linkedit_commands:
            if size < 16: raise RuntimeError('Invalid LINKEDIT command')
            offset, length = struct.unpack_from('<II', data, cursor + 8)
            if length: other_offsets.append(offset)
            if command == 0x1d: signature = offset, length
        cursor += size
    if symbols is None: raise RuntimeError('Missing LC_SYMTAB')
    command, symoff, count, offset, length = symbols
    padding = (-offset) % 8
    if not padding: return False
    end = offset + length
    if (symoff + count * 16 > offset or end > len(data) or signature is None
            or signature[0] < end + padding or end + padding > len(data)
            or any(data[end:end + padding])
            or any(end <= value < end + padding for value in other_offsets)):
        raise RuntimeError('Unsafe Mach-O string-table alignment: rebuild with a compatible linker')
    data[offset + padding:end + padding] = data[offset:end]
    data[offset:offset + padding] = bytes(padding)
    struct.pack_into('<I', data, command + 16, offset + padding)
    Path(binary).write_bytes(data)
    return True


def verify_native_load(binary, target):
    if target != host_target(): return False
    library = ctypes.CDLL(str(Path(binary).resolve()))
    library.bup_new.argtypes = [ctypes.c_char_p]
    library.bup_new.restype = ctypes.c_void_p
    library.bup_snapshot.argtypes = [ctypes.c_void_p]
    library.bup_snapshot.restype = ctypes.c_char_p
    library.bup_delete.argtypes = [ctypes.c_void_p]
    handle = library.bup_new(b'{}')
    if not handle: raise RuntimeError('Updater ABI did not allocate a handle')
    try:
        if json.loads(library.bup_snapshot(handle))['state'] != 'failed':
            raise RuntimeError('Updater ABI configuration validation failed')
    finally:
        library.bup_delete(handle)
    return True


def filenames(target):
    if target.endswith('windows'):
        return 'buoy_updatekit.dll', 'buoy-update-helper.exe'
    return 'libbuoy_updatekit.' + ('dylib' if target.endswith('darwin') else 'so'), 'buoy-update-helper'


def build_native(target=None, rust_target=None):
    target = target or host_target()
    rust, triple = TARGETS[target]
    if rust_target:
        allowed = {'x86_64-windows': 'x86_64-pc-windows-gnu', 'arm64-windows': 'aarch64-pc-windows-gnullvm'}
        if allowed.get(target) != rust_target: raise ValueError('Rust override is only supported for the matching Windows GNU target')
        rust = rust_target
    compiled_digest = source_digest()
    environment = os.environ.copy()
    if target.endswith('darwin'):
        environment['RUSTFLAGS'] = environment.get('RUSTFLAGS', '') + ' -C link-arg=-Wl,-install_name,@rpath/libbuoy_updatekit.dylib'
        environment.setdefault('MACOSX_DEPLOYMENT_TARGET', '11.0')
    subprocess.run(['cargo', 'build', '--manifest-path', str(NATIVE / 'Cargo.toml'), '--locked', '--release', '--target', rust], env=environment, check=True)
    if source_digest() != compiled_digest:
        raise RuntimeError('Updater sources changed during compilation; rebuild before sealing')
    source = NATIVE / 'target' / rust / 'release'
    library, helper = filenames(target)
    runtime = PACKAGE / 'runtime' / triple
    resources = PACKAGE / 'resources' / target
    runtime.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / library, runtime / library)
    shutil.copy2(source / helper, resources / helper)
    if target.endswith('windows'):
        link = PACKAGE / 'link' / triple
        link.mkdir(parents=True, exist_ok=True)
        import_library = source / ('libbuoy_updatekit.dll.a' if rust.endswith(('gnu', 'gnullvm')) else 'buoy_updatekit.dll.lib')
        shutil.copy2(import_library, link / 'buoy_updatekit.lib')
    repaired = align_macho_string_pool(runtime / library) if target.endswith('darwin') else False
    if target.endswith('darwin') and os.environ.get('BUOY_CODESIGN_IDENTITY'):
        for binary in (runtime / library, resources / helper):
            subprocess.run(['codesign', '--force', '--options', 'runtime', '--timestamp', '--sign', os.environ['BUOY_CODESIGN_IDENTITY'], str(binary)], check=True)
    elif repaired:
        subprocess.run(['codesign', '--force', '--sign', '-', str(runtime / library)], check=True)
    loaded = verify_native_load(runtime / library, target)
    (runtime / 'source-sha256').write_text(compiled_digest + '\n')
    info = {'source_sha256': compiled_digest, 'target': target, 'rust_target': rust,
            'build_host': platform.platform(), 'native_dlopen_checked': loaded,
            'macho_string_pool_aligned': repaired,
            'library_sha256': hashlib.sha256((runtime / library).read_bytes()).hexdigest(),
            'helper_sha256': hashlib.sha256((resources / helper).read_bytes()).hexdigest()}
    (runtime / 'build-info.json').write_text(json.dumps(info, indent=2) + '\n')
    print(f'Built {target}: {runtime / library}, {resources / helper}', flush=True)
    return runtime / library


def check_sources():
    sources = sorted((PACKAGE / 'modules').glob('*.bui'))
    for source in sources:
        for target in TARGETS:
            result = subprocess.run([os.environ.get('BUOY', 'buoy'), str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=ROOT, capture_output=True, text=True)
            diagnostics = json.loads(result.stdout)
            if result.returncode or diagnostics['errors']:
                raise RuntimeError(f'{source} ({target}): {result.stdout}\n{result.stderr}')
            if diagnostics.get('warnings'):
                print(diagnostics['warnings'])


def package_compiler():
    compiler = os.environ.get('BUOY', 'buoy')
    located = shutil.which(compiler)
    if located and Path(located).resolve() == Path('/usr/local/bin/buoy'):
        if Path(located).read_bytes()[:4096].startswith(b'#!'):
            return '/usr/bin/buoy'
    return compiler


def seal():
    check_sources()
    from updatekit_licenses import collect
    notices = collect()
    output = ROOT / 'packages/updatekit-0.1.0.buipkg'
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='updatekit-seal-') as directory:
        stage = Path(directory)
        shutil.copy2(PACKAGE / 'manifest.json', stage / 'manifest.json')
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        documentation = PACKAGE / 'resources' / 'buoy-docs'
        if documentation.is_dir():
            shutil.copytree(documentation, stage / 'resources' / 'buoy-docs')
        shutil.copytree(notices, stage / 'resources' / 'third-party')
        count = 0
        for target, (_, triple) in TARGETS.items():
            runtime = PACKAGE / 'runtime' / triple
            receipt = runtime / 'source-sha256'
            library, helper = filenames(target)
            resource = PACKAGE / 'resources' / target / helper
            if not receipt.is_file() or receipt.read_text().strip() != source_digest():
                continue
            if not (runtime / library).is_file() or not resource.is_file():
                raise RuntimeError(f'Incomplete native slice: {target}')
            destination = stage / 'runtime' / triple
            destination.mkdir(parents=True)
            shutil.copy2(runtime / library, destination / library)
            destination = stage / 'resources' / target
            destination.mkdir(parents=True)
            shutil.copy2(resource, destination / helper)
            if target.endswith('windows'):
                link = stage / 'link' / triple
                link.mkdir(parents=True)
                shutil.copy2(PACKAGE / 'link' / triple / 'buoy_updatekit.lib', link / 'buoy_updatekit.lib')
            count += 1
        if count != len(TARGETS):
            raise RuntimeError('No current native slices. Build a native target before sealing.')
        subprocess.run([package_compiler(), 'pkg', 'seal', str(stage), '-o', str(output)], cwd=ROOT, check=True)
    destination = ROOT / 'dist' / output.name
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(output, destination)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--rust-target', help='Explicit matching Windows GNU cross toolchain')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--native-only', action='store_true')
    mode.add_argument('--seal-only', action='store_true')
    args = parser.parse_args()
    if not args.seal_only:
        build_native(args.target, args.rust_target)
    if not args.native_only:
        print(seal())


if __name__ == '__main__':
    main()
