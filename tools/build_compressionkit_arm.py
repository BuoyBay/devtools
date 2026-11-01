#!/usr/bin/env python3
"""Build CompressionKit ARM64 slices with isolated, checksum-pinned Zig 0.14.1."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import build_compressionkit as builder

VERSION = '0.14.1'
URL = 'https://ziglang.org/download/0.14.1/zig-x86_64-linux-0.14.1.tar.xz'
SHA256 = '24aeeec8af16c381934a6cd7d95c807a8cb2cf7df9fa40d359aa884195c4716c'
TARGETS = {'arm64-linux': 'aarch64-linux-gnu.2.17', 'arm64-windows': 'aarch64-windows-gnu'}
CACHE = Path(os.environ.get('COMPRESSIONKIT_ARM_CACHE', Path.home() / '.cache/buoy-compressionkit-arm')).resolve()


def toolchain(extraction):
    """Verify the official archive before extraction; never install globally."""
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / 'zig.tar.xz'
    if not archive.exists():
        temporary = archive.with_suffix('.download')
        with urllib.request.urlopen(URL, timeout=120) as source, temporary.open('wb') as output:
            import shutil
            shutil.copyfileobj(source, output)
        temporary.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('Zig archive SHA256 mismatch; remove the cache archive and retry')
    # Only the verified archive is cached. Never execute an existing extracted tree.
    extraction.mkdir(parents=True)
    with tarfile.open(archive) as source:
        source.extractall(extraction, filter='data')
    directory = extraction / ('zig-x86_64-linux-' + VERSION)
    zig = directory / 'zig'
    if subprocess.check_output([str(zig), 'version'], text=True).strip() != VERSION:
        raise RuntimeError('Unexpected Zig version')
    return zig


def compile_bridge(target, arguments):
    # Zig links its compiler-rt statically; GCC's switch is inapplicable.
    arguments = [arg for arg in arguments if arg != '-static-libgcc']
    environment = os.environ.copy()
    environment['ZIG_GLOBAL_CACHE_DIR'] = str(CACHE / 'global')
    environment['ZIG_LOCAL_CACHE_DIR'] = str(CACHE / 'local')
    # Compile-time target ABI checks, including zlib's private platform layout.
    windows = target.endswith('windows')
    with tempfile.TemporaryDirectory(prefix='compressionkit-arm-abi-') as directory:
        zig = toolchain(Path(directory) / 'toolchain')
        header = Path(directory) / 'abi.h'
        header.write_text('#include <stdint.h>\n#include <stddef.h>\n#include "zlib.h"\n'
                          '_Static_assert(sizeof(void *) == 8, "pointer ABI");\n'
                          '_Static_assert(sizeof(int64_t) == 8, "scalar ABI");\n'
                          '_Static_assert(sizeof(int) == 4, "int ABI");\n'
                          f'_Static_assert(sizeof(long) == {4 if windows else 8}, "long ABI");\n'
                          f'_Static_assert(sizeof(z_stream) == {88 if windows else 112}, "z_stream ABI");\n')
        command = [str(zig), 'cc', '-target', TARGETS[target], '-fvisibility=hidden', '-g0', '-s',
                   '-include', str(header)] + arguments
        subprocess.run(command, check=True, env=environment, timeout=600)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == '--compiler':
        compile_bridge(sys.argv[2], sys.argv[3:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TARGETS, action='append')
    parser.add_argument('--native-only', action='store_true')
    args = parser.parse_args()
    for target in args.target or TARGETS:
        compiler = shlex.join([sys.executable, str(Path(__file__).resolve()), '--compiler', target])
        binary = builder.build_native(target, compiler)
        # Supplemental provenance does not change the existing source receipt contract.
        binary.with_name(binary.name + '.toolchain.json').write_text(json.dumps({
            'toolchain': 'Zig ' + VERSION, 'url': URL, 'sha256': SHA256,
            'license': 'MIT; bundled LLVM Apache-2.0 WITH LLVM-exception and libc component licenses',
            'licenseFiles': ['LICENSE', 'lib/libc/mingw/COPYING', 'lib/libc/glibc/LICENSES'],
            'target': TARGETS[target], 'nativeSourceSha256': builder.digest(),
            'helperSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'abiAssertions': {'pointer': 8, 'int64_t': 8, 'int': 4,
                              'long': 4 if target.endswith('windows') else 8,
                              'z_stream': 88 if target.endswith('windows') else 112},
            'binarySha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'execution': 'Cross-built; no native ARM64 execution claimed',
        }, indent=2) + '\n')
    if not args.native_only:
        builder.seal()


if __name__ == '__main__':
    main()
