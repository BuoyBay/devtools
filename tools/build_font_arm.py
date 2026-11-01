#!/usr/bin/env python3
"""Cross-build ARM64 Linux font bridges and execute API tests via private qemu.

Uses an isolated amd64 Docker image with ARM64 development/runtime packages.
No host packages, system binfmt handlers or installed Buoy files are changed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'buoy:font-arm-validation'
DOCKERFILE = '''FROM buoy:0.72.0
USER root
RUN dpkg --add-architecture arm64 && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gcc-aarch64-linux-gnu qemu-user pkg-config libfontconfig-dev:arm64 libpango1.0-dev:arm64 fonts-dejavu-core fonts-noto-core && rm -rf /var/lib/apt/lists/*
'''


def execute(command, *, env=None, timeout=180):
    result = subprocess.run(list(map(str, command)), env=env, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{shlex.join(list(map(str, command)))}\n{result.stdout}\n{result.stderr}')
    return {'command': list(map(str, command)), 'exitCode': result.returncode,
            'stdout': result.stdout, 'stderr': result.stderr}


def verify_arm_elf(path):
    data = path.read_bytes()
    if (len(data) < 20 or data[:6] != b'\x7fELF\x02\x01'
            or struct.unpack_from('<H', data, 18)[0] != 183):
        raise RuntimeError(f'Expected a little-endian ELF64 AArch64 artifact: {path}')


def inside(output):
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['PKG_CONFIG_LIBDIR'] = '/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig'
    common = ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror']
    record = {'hostArchitecture': platform.machine(), 'target': 'aarch64-unknown-linux-gnu',
              'execution': 'qemu-aarch64 user-mode emulation on x86_64; NOT native ARM hardware',
              'imageRecipe': DOCKERFILE, 'builds': {}, 'tests': {}, 'sources': {}}
    record['compiler'] = execute(['aarch64-linux-gnu-gcc', '--version'])
    record['qemu'] = execute(['qemu-aarch64', '--version'])
    record['packages'] = execute(['dpkg-query', '-W', 'libpango-1.0-0:arm64',
                                   'libfontconfig1:arm64', 'libc6:arm64'])
    for name, dependencies in (('fontkit', ['fontconfig', 'pangocairo', 'pangoft2']),
                               ('shapingkit', ['pangocairo'])):
        source = ROOT / 'libraries' / name / 'native/bridge.c'
        flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', *dependencies], env=env, text=True))
        binary = output / f'libbuoy_{name}.so'
        command = ['aarch64-linux-gnu-gcc', *common, '-pthread', '-fPIC', '-shared',
                   source, '-o', binary, '-Wl,-soname,' + binary.name, *flags]
        if name == 'shapingkit':
            command.append('-ldl')
        record['builds'][name] = execute(command, env=env)
        verify_arm_elf(binary)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        record['sources'][name] = digest
        binary.with_suffix('.so.source-sha256').write_text(digest + '\n')
        test_source = ROOT / 'tests' / ('font_arm_native.c' if name == 'fontkit' else 'shapingkit_native.c')
        test = output / (name + '-test')
        command = ['aarch64-linux-gnu-gcc', *common, test_source, '-o', test,
                   *flags, '-lm', '-ldl', '-rdynamic']
        record['builds'][name + 'Test'] = execute(command, env=env)
        verify_arm_elf(test)
        command = ['qemu-aarch64', '-L', '/', test]
        if name == 'fontkit':
            command.append(ROOT / 'libraries/fontkit/fixtures/BuoyFontKitFixture-Regular.ttf')
        record['tests'][name] = execute(command, env=env)
        print(record['tests'][name]['stdout'], end='')
    dso_test = output / 'shapingkit-dso-test'
    record['builds']['shapingkitDsoTest'] = execute([
        'aarch64-linux-gnu-gcc', *common, ROOT / 'tests/font_arm_shaping_dso.c',
        '-o', dso_test, '-ldl'])
    record['tests']['shapingkitDso'] = execute([
        'qemu-aarch64', '-L', '/', dso_test, output / 'libbuoy_shapingkit.so'])
    print(record['tests']['shapingkitDso']['stdout'], end='')
    (output / 'arm64-linux.json').write_text(json.dumps(record, indent=2) + '\n')


def buoy_consumer(output, image):
    """Exercise Buoy's real ARM64 FFI lowering, not just equivalent C calls."""
    with tempfile.TemporaryDirectory(prefix='font-arm-buoy-') as directory:
        work = Path(directory)
        library = work / 'library'
        library.mkdir()
        shutil.copy2(ROOT / 'libraries/fontkit/manifest.json', library / 'manifest.json')
        shutil.copytree(ROOT / 'libraries/fontkit/modules', library / 'modules')
        native = library / 'runtime/aarch64-unknown-linux-gnu'
        native.mkdir(parents=True)
        shutil.copy2(output / 'libbuoy_fontkit.so', native / 'libbuoy_fontkit.so')
        (work / 'packages').mkdir()
        sealed = execute(['/usr/bin/buoy', 'pkg', 'seal', library, '-o', work / 'packages/fontkit-0.1.0.buipkg'])
        shutil.copy2(ROOT / 'tests/fontkit_test.bui', work / 'main.bui')
        command = ['buoy', 'main.bui', '--check', '--diagnostic-format', 'json', '--target', 'arm64-linux']
        checked = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=60)
        diagnostic = json.loads(checked.stdout)
        if checked.returncode or diagnostic.get('errors') != []:
            raise RuntimeError(diagnostic)
        built = subprocess.run(['buoy', 'main.bui', '--target', 'arm64-linux', '-o', 'verify'],
                               cwd=work, capture_output=True, text=True, timeout=120)
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)
        (work / 'invalid.ttf').write_bytes(b'Not a font')
        fixture = ROOT / 'libraries/fontkit/fixtures/BuoyFontKitFixture-Regular.ttf'
        shutil.copy2(fixture, work / 'fixture.ttf')
        command = ['docker', 'run', '--rm', '--network', 'none',
                   '--user', f'{os.getuid()}:{os.getgid()}', '-v', f'{work}:/work:ro',
                   '-v', '/usr/lib/buoy/runtime/aarch64-unknown-linux-gnu:/buoy-runtime:ro',
                   image, 'qemu-aarch64', '-L', '/', '-E', 'LD_LIBRARY_PATH=/buoy-runtime:/work',
                   '/work/verify', '/work/fixture.ttf', '/work/invalid.ttf', '/work/./fixture.ttf']
        run = execute(command)
        print(run['stdout'], end='')
        return {'sourceSha256': hashlib.sha256((work / 'main.bui').read_bytes()).hexdigest(),
                'seal': sealed, 'compilerCheck': {'command': ['buoy', 'main.bui', '--check',
                 '--diagnostic-format', 'json', '--target', 'arm64-linux'],
                 'exitCode': checked.returncode, 'diagnostics': diagnostic},
                'build': {'exitCode': built.returncode, 'stdout': built.stdout, 'stderr': built.stderr},
                'runtime': run, 'binarySha256': hashlib.sha256((work / 'verify').read_bytes()).hexdigest()}


def windows_slice(linker):
    """Cross-link a plain-C ARM64 DLL; does not claim Windows execution."""
    source = ROOT / 'libraries/fontkit/native/bridge.c'
    stubs = Path('/usr/lib/buoy/stubs/arm64-pc-windows-msvc')
    headers = Path('/usr/x86_64-w64-mingw32/include')
    with tempfile.TemporaryDirectory(prefix='font-arm-windows-') as directory:
        work = Path(directory)
        obj, dll, lib = work / 'bridge.obj', work / 'buoy_fontkit.dll', work / 'buoy_fontkit.lib'
        # The existing SDK import set is deliberately sparse; add private import
        # declarations for the documented GDI font APIs, never patch its files.
        definition = work / 'gdi-fonts.def'
        definition.write_text('LIBRARY gdi32.dll\nEXPORTS\nEnumFontFamiliesExW\nAddFontResourceExW\nRemoveFontResourceExW\n')
        logs = [execute(['clang', '-target', 'aarch64-w64-windows-gnu', '-isystem', headers,
                         '-O2', '-Wall', '-Wextra', '-Werror', '-c', source, '-o', obj]),
                execute(['llvm-dlltool-19', '-m', 'arm64', '-d', definition, '-l', work / 'gdi-fonts.lib']),
                execute([linker, '/dll', '/noentry', '/machine:arm64', '/nodefaultlib', '/timestamp:0',
                         '/out:' + str(dll), '/implib:' + str(lib), obj, work / 'gdi-fonts.lib',
                         stubs / 'kernel32.lib', stubs / 'gdi32.lib', stubs / 'msvcrt.lib', stubs / 'chkstk.obj'])]
        data = dll.read_bytes()
        offset = struct.unpack_from('<I', data, 60)[0]
        if data[:2] != b'MZ' or data[offset:offset+4] != b'PE\0\0' or struct.unpack_from('<H', data, offset+4)[0] != 0xAA64:
            raise RuntimeError('Cross linker did not produce an ARM64 PE DLL')
        destination = ROOT / 'libraries/fontkit/runtime/aarch64-pc-windows-msvc'
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dll, destination / dll.name)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        (destination / (dll.name + '.source-sha256')).write_text(digest + '\n')
        import_dir = ROOT / 'libraries/fontkit/link/aarch64-pc-windows-msvc'
        import_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib, import_dir / lib.name)
        library = work / 'library'
        library.mkdir()
        shutil.copy2(ROOT / 'libraries/fontkit/manifest.json', library / 'manifest.json')
        shutil.copytree(ROOT / 'libraries/fontkit/modules', library / 'modules')
        runtime_dir = library / 'runtime/aarch64-pc-windows-msvc'
        runtime_dir.mkdir(parents=True)
        shutil.copy2(dll, runtime_dir / dll.name)
        link_dir = library / 'link/aarch64-pc-windows-msvc'
        link_dir.mkdir(parents=True)
        shutil.copy2(lib, link_dir / lib.name)
        (work / 'packages').mkdir()
        logs.append(execute(['/usr/bin/buoy', 'pkg', 'seal', library,
                             '-o', work / 'packages/fontkit-0.1.0.buipkg']))
        shutil.copy2(ROOT / 'tests/fontkit_test.bui', work / 'main.bui')
        checked = subprocess.run(['buoy', 'main.bui', '--check', '--diagnostic-format',
                                  'json', '--target', 'arm64-windows'], cwd=work,
                                 capture_output=True, text=True, timeout=60)
        diagnostic = json.loads(checked.stdout)
        if checked.returncode or diagnostic.get('errors') != []:
            raise RuntimeError(diagnostic)
        linked = subprocess.run(['buoy', 'main.bui', '--target', 'arm64-windows', '-o', 'consumer.exe'],
                                cwd=work, capture_output=True, text=True, timeout=120)
        if linked.returncode:
            raise RuntimeError(linked.stdout + linked.stderr)
        return {'scope': 'ARM64 Windows cross-compilation/linking only; not executed',
                'sourceSha256': digest, 'artifactSha256': hashlib.sha256(data).hexdigest(), 'commands': logs,
                'compiler': execute(['clang', '--version']), 'linker': execute([linker, '--version']),
                'headerSha256': hashlib.sha256((headers / 'windows.h').read_bytes()).hexdigest(),
                'buoyConsumer': {'sourceSha256': hashlib.sha256((work / 'main.bui').read_bytes()).hexdigest(),
                                 'diagnostics': diagnostic, 'compilerExit': checked.returncode,
                                 'linkExit': linked.returncode, 'stdout': linked.stdout, 'stderr': linked.stderr,
                                 'binarySha256': hashlib.sha256((work / 'consumer.exe').read_bytes()).hexdigest()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--build-image', action='store_true')
    parser.add_argument('--seal', action='store_true', help='Reseal FontKit/ShapingKit after adding verified slices')
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--windows-linker', type=Path,
                        help='Optional lld-link executable for ARM64 Windows FontKit cross-link')
    args = parser.parse_args()
    if args.inside:
        inside(args.output)
        return
    if args.build_image:
        with tempfile.TemporaryDirectory(prefix='font-arm-image-') as directory:
            (Path(directory) / 'Dockerfile').write_text(DOCKERFILE)
            subprocess.run(['docker', 'build', '-t', args.image, directory], check=True, timeout=1200)
    with tempfile.TemporaryDirectory(prefix='font-arm-output-') as directory:
        output = Path(directory)
        command = ['docker', 'run', '--rm', '--network', 'none', '--user', f'{os.getuid()}:{os.getgid()}',
                   '-v', f'{ROOT}:/workspace:ro', '-v', f'{output}:/artifacts', args.image,
                   'python3', '/workspace/tools/build_font_arm.py', '--inside', '--output', '/artifacts']
        subprocess.run(command, check=True, timeout=600)
        for name in ('fontkit', 'shapingkit'):
            source = output / f'libbuoy_{name}.so'
            verify_arm_elf(source)
            receipt = source.with_suffix('.so.source-sha256')
            current = hashlib.sha256((ROOT / 'libraries' / name / 'native/bridge.c').read_bytes()).hexdigest()
            if receipt.read_text().strip() != current:
                raise RuntimeError('Native source changed during build; refusing stale receipt')
            destination = ROOT / 'libraries' / name / 'runtime/aarch64-unknown-linux-gnu'
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination / source.name)
            shutil.copy2(receipt, destination / receipt.name)
        evidence = ROOT / 'tests/font_arm_evidence'
        evidence.mkdir(exist_ok=True)
        record = json.loads((output / 'arm64-linux.json').read_text())
        record['tests']['fontkitBuoy'] = buoy_consumer(output, args.image)
        record['dockerImage'] = execute(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'])['stdout'].strip()
        record['artifactSha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob('*.so')}
        (evidence / 'arm64-linux.json').write_text(json.dumps(record, indent=2) + '\n')
        if args.windows_linker:
            # lld chooses its driver by argv[0]; preserve the lld-link symlink.
            (evidence / 'arm64-windows.json').write_text(json.dumps(windows_slice(args.windows_linker.absolute()), indent=2) + '\n')
    if args.seal:
        import build_fontkit
        import build_shapingkit
        build_fontkit.seal()
        build_shapingkit.seal()
    print('Added ARM64 Linux slices after cross-build and qemu API tests. ' +
          ('Packages resealed.' if args.seal else 'Reseal packages before distribution.'))


if __name__ == '__main__':
    main()
