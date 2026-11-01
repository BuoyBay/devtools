#!/usr/bin/env python3
"""Build host Linux ShapingKit, check six Buoy targets, and seal the package."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from font_package_artifacts import atomic_copy, prepare_manual, publish_archive, verify_archive
PACKAGE = ROOT / 'libraries/shapingkit'
TARGETS = ('x86_64-linux', 'arm64-linux', 'x86_64-darwin', 'arm64-darwin',
           'x86_64-windows', 'arm64-windows')


def check_sources(sources=None, cwd=ROOT):
    if sources is None:
        sources = list((PACKAGE / 'modules').glob('*.bui'))
        sources += list((ROOT / 'tests').glob('shapingkit_*.bui'))
    for source in sorted(sources):
        for target in TARGETS:
            result = subprocess.run(['buoy', str(source), '--check', '--diagnostic-format',
                                     'json', '--target', target], cwd=cwd,
                                    capture_output=True, text=True)
            diagnostics = json.loads(result.stdout)
            if result.returncode or diagnostics['errors']:
                raise RuntimeError(f'{source} {target}: {diagnostics}\n{result.stderr}')
            if diagnostics.get('warnings'):
                print(source, target, diagnostics['warnings'])
    print(f'Checked {len(sources)} Buoy files across six targets (compiler checks only).')


def build():
    if platform.system() != 'Linux':
        raise RuntimeError('The native helper supports Linux only')
    arch = {'x86_64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(platform.machine())
    if not arch:
        raise RuntimeError('Unsupported Linux architecture')
    output = PACKAGE / 'runtime' / f'{arch}-unknown-linux-gnu' / 'libbuoy_shapingkit.so'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.so.source-sha256').unlink(missing_ok=True)
    flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'pangocairo'], text=True))
    source = PACKAGE / 'native/bridge.c'
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) +
                   ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-fPIC', '-shared',
                    str(source), '-o', str(output), '-Wl,-soname,' + output.name] + flags + ['-ldl'], check=True)
    verify_architecture(output, f'{arch}-unknown-linux-gnu')
    output.with_suffix('.so.source-sha256').write_text(hashlib.sha256(source.read_bytes()).hexdigest() + '\n')
    return output


def verify_architecture(binary, triple):
    data = binary.read_bytes()
    expected = {'x86_64-unknown-linux-gnu': 62, 'aarch64-unknown-linux-gnu': 183}[triple]
    if len(data) < 20 or data[:4] != b'\x7fELF' or data[4] != 2 or data[5] not in (1, 2):
        raise RuntimeError(f'{binary}: expected a 64-bit ELF library')
    machine = struct.unpack_from('<H' if data[5] == 1 else '>H', data, 18)[0]
    if machine != expected:
        raise RuntimeError(f'{binary}: architecture mismatch for {triple}')


def stage_package(stage, package=PACKAGE):
    prepare_manual(package)
    digest = hashlib.sha256((package / 'native/bridge.c').read_bytes()).hexdigest()
    shutil.copy2(package / 'manifest.json', stage / 'manifest.json')
    shutil.copytree(package / 'modules', stage / 'modules')
    shutil.copytree(package / 'resources', stage / 'resources')
    count = 0
    for triple in ('x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu'):
        native = package / 'runtime' / triple / 'libbuoy_shapingkit.so'
        receipt = native.with_suffix('.so.source-sha256')
        if not native.is_file() or not receipt.is_file():
            continue
        try:
            receipt_digest = receipt.read_text().strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f'Cannot read native source receipt {receipt}: {exc}') from exc
        if receipt_digest != digest:
            continue
        verify_architecture(native, triple)
        destination = stage / 'runtime' / triple
        destination.mkdir(parents=True)
        shutil.copy2(native, destination / native.name)
        count += 1
    if count != 2:
        raise RuntimeError('No source-current native slices; build before sealing')


def seal():
    # The packaged compiler's pkg subcommand must be first, before wrapper flags.
    compiler = '/usr/bin/buoy' if Path('/usr/bin/buoy').exists() else 'buoy'
    output = ROOT / 'packages/shapingkit-0.1.0.buipkg'
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='shapingkit-seal-') as directory:
        stage = Path(directory) / 'library'
        stage.mkdir()
        archive = Path(directory) / output.name
        stage_package(stage)
        subprocess.run([compiler, 'pkg', 'seal', str(stage), '-o', str(archive)], cwd=ROOT, check=True)
        return publish_archive(archive, PACKAGE, ROOT)


def test():
    evidence = ROOT / 'tests/shapingkit_evidence'
    evidence.mkdir(exist_ok=True)
    runtime = Path('/usr/lib/buoy/runtime/x86_64-unknown-linux-gnu/buoy_ui.so')
    metadata = {'host': platform.platform(), 'architecture': platform.machine(),
                'buoy': subprocess.check_output(['buoy', '--version'], text=True).strip(),
                'pangocairo': subprocess.check_output(['pkg-config', '--modversion', 'pangocairo'], text=True).strip(),
                'cairo': subprocess.check_output(['pkg-config', '--modversion', 'cairo'], text=True).strip(),
                'LD_LIBRARY_PATH': os.environ.get('LD_LIBRARY_PATH', ''),
                'native_runtime_tested': 'Linux host only; six-target checks are compiler-only'}
    if runtime.is_file():
        metadata['buoy_ui_sha256'] = hashlib.sha256(runtime.read_bytes()).hexdigest()
        disassembly = []
        for symbol in ('bui_ui_graphics_draw_string_styled', 'bui_ui_graphics_measure_string_w_styled',
                       'bui_ui_graphics_measure_string_h_styled', 'bui_ui_graphics_new_for_picture'):
            disassembly.append(subprocess.check_output(['objdump', '-d', '--disassemble=' + symbol, str(runtime)], text=True))
        (evidence / 'runtime-disassembly.txt').write_text('\n'.join(disassembly))
    (evidence / 'environment.json').write_text(json.dumps(metadata, indent=2) + '\n')
    subprocess.run([sys.executable, str(ROOT / 'tests/shapingkit_packaging.py')], check=True)
    with tempfile.TemporaryDirectory(prefix='shapingkit-test-') as directory:
        work = Path(directory)
        flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'pangocairo'], text=True))
        native = work / 'native-test'
        subprocess.run(['cc', '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        str(ROOT / 'tests/shapingkit_native.c'), '-o', str(native), '-rdynamic'] + flags + ['-lm', '-ldl'], check=True)
        result = subprocess.run([str(native)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f'Process exit {result.returncode}: ' + result.stdout + result.stderr)
        print(result.stdout)
        (evidence / 'native.txt').write_text(result.stdout)
        # Isolate from unrelated app_resources.json files in this shared repo.
        (work / 'tests').mkdir()
        consumer = work / 'tests/shapingkit_smoke.bui'
        consumer.write_text((ROOT / 'tests/shapingkit_smoke.bui').read_text().replace(
            'Import "../libraries/shapingkit/modules/ShapingKit.bui" As Shaping',
            'Import shapingkit.ShapingKit As Shaping'))
        (work / 'packages').mkdir()
        shutil.copy2(ROOT / 'packages/shapingkit-0.1.0.buipkg', work / 'packages/shapingkit-0.1.0.buipkg')
        check_sources([consumer], cwd=work)
        executable = work / 'smoke'
        subprocess.run(['buoy', 'tests/shapingkit_smoke.bui', '-o', str(executable)], cwd=work, check=True)
        result = subprocess.run([str(executable)], cwd=work, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        print(result.stdout)
        (evidence / 'buoy.txt').write_text(result.stdout)
        shutil.copy2(work / 'shapingkit-comparison.png', evidence / 'comparison.png')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--test', action='store_true', help='Run host Linux native and Buoy integration checks')
    args = parser.parse_args()
    check_sources()
    if not args.check_only:
        print(build())
        print(seal())
        if args.test:
            test()
        # Package discovery may refresh generated package documentation while
        # checking consumers; mirror the final archive after all compiler calls.
        archive = ROOT / 'packages/shapingkit-0.1.0.buipkg'
        verify_archive(PACKAGE, archive)
        atomic_copy(archive, ROOT / 'dist' / archive.name)


if __name__ == '__main__':
    main()
