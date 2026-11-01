"""Rebuild Studio SDK payloads in a private consumer, never in the source tree."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from build_support import host_target, TARGETS, verify_architecture, verify_native_receipt, native_source_digest
from buoy_executables import resolve_compiler, resolve_packager

PACKAGES = ('processkit', 'terminalkit', 'chatgptauth')


def stage_private_target_packages(root, work, target):
    """Keep recipes, copied inputs and native receipts under work for input hashing.

    SDK CLIs build a verified target slice and check modules on six targets.
    Seal that slice only into this consumer; public SDK release packagers still
    require all six current native slices. No prebuilt slice is copied into it.
    """
    root, work = Path(root), Path(work)
    if target not in TARGETS:
        raise ValueError('Unsupported private SDK target: ' + target)
    if target != host_target() and target != 'x86_64-windows':
        raise ValueError('Private SDK cross-build supports x86_64-windows; other targets require a native host')
    selector = 'STUDIO_NATIVE_CC' if target != host_target() else 'CC'
    default = 'x86_64-w64-mingw32-gcc' if target != host_target() else ('cl' if os.name == 'nt' else 'cc')
    compiler = shlex.split(os.environ.get(selector, default))
    if not compiler or not shutil.which(compiler[0]):
        raise RuntimeError('Private SDK compiler missing; set ' + selector)
    compiler[0] = str(Path(shutil.which(compiler[0])).resolve())
    private = work / 'native-target-build'
    private.mkdir()
    scripts = private / 'tools'
    scripts.mkdir()
    for name in ('build_support.py', 'buoy_executables.py', *('build_' + name + '.py' for name in PACKAGES)):
        shutil.copy2(root / 'tools' / name, scripts / name)
    shutil.copytree(root / 'tools/buoydev', scripts / 'buoydev', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    environment = dict(os.environ, BUOY=resolve_compiler(), BPM_PACKAGER=resolve_packager(), PYTHONDONTWRITEBYTECODE='1')
    for name in PACKAGES:
        source, copied = root / 'libraries' / name, private / 'libraries' / name
        copied.mkdir(parents=True)
        shutil.copy2(source / 'manifest.json', copied / 'manifest.json')
        for folder in ('native', 'modules', 'resources'):
            if (source / folder).is_dir():
                shutil.copytree(source / folder, copied / folder)
        command = [sys.executable, str(scripts / ('build_' + name + '.py')),
                   '--target', target, '--cc', shlex.join(compiler), '--native-only']
        subprocess.run(command, cwd=private, env=environment, check=True)
        subprocess.run([sys.executable, str(scripts / ('build_' + name + '.py')), '--check-only'],
                       cwd=private, env=environment, check=True)
        # A private consumer needs one target; do not invoke the public release
        # sealer, which intentionally refuses incomplete multi-platform archives.
        filename = ('buoy_' + name + '.dll' if target.endswith('windows') else
                    'libbuoy_' + name + ('.dylib' if target.endswith('darwin') else '.so'))
        runtime = copied / 'runtime' / TARGETS[target] / filename
        verify_architecture(runtime, target)
        recipe = scripts / ('build_' + name + '.py')
        if not verify_native_receipt(runtime, target, recipe, native_source_digest(copied)):
            raise RuntimeError('Private SDK native receipt does not match current inputs: ' + name)
        sealable = private / 'sealed-sources' / name
        sealable.mkdir(parents=True)
        shutil.copy2(copied / 'manifest.json', sealable / 'manifest.json')
        for folder in ('modules', 'resources'):
            if (copied / folder).is_dir():
                shutil.copytree(copied / folder, sealable / folder)
        destination = sealable / 'runtime' / TARGETS[target]
        destination.mkdir(parents=True)
        shutil.copy2(runtime, destination / filename)
        if target.endswith('windows'):
            library = copied / 'link' / TARGETS[target] / ('buoy_' + name + '.lib')
            if not library.is_file():
                raise RuntimeError('Private SDK import library missing: ' + str(library))
            destination = sealable / 'link' / TARGETS[target]
            destination.mkdir(parents=True)
            shutil.copy2(library, destination / library.name)
        archive = private / 'packages' / (name + '-0.1.0.buipkg')
        archive.parent.mkdir(exist_ok=True)
        subprocess.run([resolve_packager(), 'pkg', 'seal', str(sealable), '-o', str(archive)],
                       cwd=private, env=environment, check=True)
        shutil.copy2(archive, work / 'packages' / archive.name)
