#!/usr/bin/env python3
"""Build isolated native GPU/image bridges, seal Scene3DKit, and verify consumers."""
import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/scene3dkit'
TARGETS = ('x86_64-linux', 'arm64-linux', 'x86_64-darwin', 'arm64-darwin', 'x86_64-windows', 'arm64-windows')
TRIPLES = {'x86_64-linux': 'x86_64-unknown-linux-gnu', 'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-darwin': 'x86_64-apple-macos', 'arm64-darwin': 'arm64-apple-macos', 'x86_64-windows': 'x86_64-pc-windows-msvc', 'arm64-windows': 'aarch64-pc-windows-msvc'}


def build_native(runtime_root=None, target=None, cc=None):
    """Compile host (or selected target) bridges without opening a graphics context."""
    runtime_root = Path(runtime_root or PACKAGE / 'runtime').resolve()
    gpu = importlib.import_module('build_scene3dkit_native')
    images = importlib.import_module('build_scene3dimages')
    gpu_target, gpu_path = gpu.build(runtime_root, target=target, cc=cc)
    image_target, image_path = images.build(runtime_root, target=target, cc=cc)
    if gpu_target != image_target:
        raise RuntimeError(f'Native bridge targets differ: {gpu_target} / {image_target}')
    return gpu_target, [Path(gpu_path), Path(image_path)]


def seal_native(target, paths, archive=None, runtime_root=None):
    """Seal a fresh target plus explicitly supplied, freshness-verified runtime slices."""
    if target not in TRIPLES:
        raise ValueError(f'Unsupported Scene3DKit target: {target}')
    archive = Path(archive or ROOT / 'packages/scene3dkit-0.1.0.buipkg').resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='scene3dkit-seal-') as directory:
        stage = Path(directory)
        for name in ('manifest.json', 'README.md', 'GLTF.md', 'SOURCE-RENDERING-CONTRACT.md'):
            if (PACKAGE / name).is_file():
                shutil.copy2(PACKAGE / name, stage / name)
        shutil.copytree(PACKAGE / 'modules', stage / 'modules')
        if (PACKAGE / 'resources').is_dir():
            shutil.copytree(PACKAGE / 'resources', stage / 'resources')
        slices = {target: list(map(Path, paths))}
        if runtime_root is not None:
            for selected, triple in TRIPLES.items():
                directory = Path(runtime_root) / triple
                if not directory.is_dir():
                    continue
                suffix = '.dll' if selected.endswith('windows') else '.dylib' if selected.endswith('darwin') else '.so'
                prefix = '' if suffix == '.dll' else 'lib'
                names = ['buoy_scene3dkit', 'buoy_scene3dimages']
                natives = [directory / (prefix + name + suffix) for name in names]
                if not all(native.is_file() for native in natives):
                    raise RuntimeError(f'Incomplete runtime slice: {directory}')
                # The freshly built target takes precedence over supplied slices.
                if selected != target:
                    slices[selected] = natives
        if archive.is_file():
            with zipfile.ZipFile(archive) as existing:
                previous = {name.split('/')[1] for name in existing.namelist() if name.startswith('runtime/')}
            missing = previous - {TRIPLES[selected] for selected in slices}
            if missing:
                raise RuntimeError(f'Refusing to drop runtime targets {sorted(missing)}; supply runtime_root')
        for selected, natives in slices.items():
            runtime = stage / 'runtime' / TRIPLES[selected]
            runtime.mkdir(parents=True)
            for native in natives:
                if not native.is_file():
                    raise FileNotFoundError(native)
                shutil.copy2(native, runtime / native.name)
                link = native.parent.parent.parent / 'link' / TRIPLES[selected]
                if link.is_dir():
                    shutil.copytree(link, stage / 'link' / TRIPLES[selected], dirs_exist_ok=True)
        packager = os.environ.get('BPM_PACKAGER', '/usr/bin/buoy' if Path('/usr/bin/buoy').is_file() else 'buoy')
        subprocess.run([packager, 'pkg', 'seal', str(stage), '-o', str(archive)], cwd=stage, check=True, timeout=180)
    return archive


def build(output=None, verify=False, target=None, cc=None, runtime_root=None):
    compiler = os.environ.get('BUOY', 'buoy')
    output = Path(output or ROOT / 'packages/scene3dkit-0.1.0.buipkg').resolve()
    with tempfile.TemporaryDirectory(prefix='scene3dkit-') as temp:
        work = Path(temp)
        (work / 'packages').mkdir()
        archive = work / 'packages/scene3dkit-0.1.0.buipkg'
        native_target, paths = build_native(work / 'native' / 'runtime', target=target, cc=cc)
        seal_native(native_target, paths, archive, runtime_root=runtime_root)
        source_stage = work / 'sources'
        shutil.copytree(PACKAGE / 'modules', source_stage)
        sources = list(source_stage.glob('*.bui'))
        if verify:
            consumer = work / 'scene3dkit_test.bui'
            shutil.copyfile(ROOT / 'tests/scene3dkit_test.bui', consumer)
            sources.append(consumer)
        for source in sources:
            for check_target in TARGETS:
                run = subprocess.run([compiler, str(source), '--check', '--diagnostic-format', 'json', '--target', check_target], cwd=work, capture_output=True, text=True, timeout=180)
                diagnostics = json.loads(run.stdout)
                if run.returncode or diagnostics['errors']:
                    raise RuntimeError(f'{source.name} {check_target}: {run.stdout}\n{run.stderr}')
                for warning in diagnostics['warnings']:
                    print(source.name, check_target, warning)
        if verify:
            if target is not None:
                raise ValueError('--verify executes on the host; omit --target for runtime verification')
            binary = work / 'scene3dkit-test'
            flags = ['-L' + str(paths[0].parent)]
            link = paths[0].parent.parent.parent / 'link' / TRIPLES[native_target]
            if link.is_dir():
                flags.append('-L' + str(link))
            subprocess.run([compiler, str(consumer), '-o', str(binary), *flags], cwd=work, check=True, timeout=180)
            environment = dict(os.environ)
            for key in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH'):
                environment[key] = str(paths[0].parent) + (os.pathsep + environment[key] if environment.get(key) else '')
            environment['PATH'] = str(paths[0].parent) + os.pathsep + environment.get('PATH', '')
            run = subprocess.run([str(binary)], cwd=work, env=environment, capture_output=True, text=True, timeout=60)
            print(run.stdout, end='')
            if run.returncode or 'Scene3DKit camera' not in run.stdout:
                raise RuntimeError(f'native tests failed ({run.returncode}): {run.stdout}\n{run.stderr}')
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.is_file():
            with zipfile.ZipFile(output) as old, zipfile.ZipFile(archive) as new:
                previous = {n.split('/')[1] for n in old.namelist() if n.startswith('runtime/')}
                current = {n.split('/')[1] for n in new.namelist() if n.startswith('runtime/')}
                if previous - current:
                    raise RuntimeError(f'Refusing to drop runtime targets {sorted(previous - current)}; supply --runtime-root')
        shutil.copyfile(archive, output)
        print(f'{output}: {len(sources) * len(TARGETS)} compiler checks passed' + ('; native raster tests passed' if verify else ''))
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--cc')
    parser.add_argument('--runtime-root', type=Path, help='Additional runtime slices, verified current by the caller')
    args = parser.parse_args()
    build(args.output, args.verify, args.target, args.cc, args.runtime_root)
