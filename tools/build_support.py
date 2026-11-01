"""Shared Buoy target, checking and atomic package publication operations.

This module never schedules processes in separate sessions. The native buoy-dev
executor owns the action's POSIX process group/Windows job, timeout and cancellation.
Adapters must not detach processes into independent POSIX sessions.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import struct
import re

from buoy_executables import resolve_compiler, resolve_packager

TARGETS = {
    'arm64-darwin': 'arm64-apple-macos', 'x86_64-darwin': 'x86_64-apple-macos',
    'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-linux': 'x86_64-unknown-linux-gnu',
    'arm64-windows': 'aarch64-pc-windows-msvc', 'x86_64-windows': 'x86_64-pc-windows-msvc',
}


def verify_architecture(binary, target):
    data = Path(binary).read_bytes()
    arm = target.startswith('arm64-')
    actual = None
    if target.endswith('linux'):
        expected = 183 if arm else 62
        if len(data) >= 20 and data[:4] == b'\x7fELF' and data[4] == 2 and data[5] in (1, 2):
            actual = struct.unpack_from('<H' if data[5] == 1 else '>H', data, 18)[0]
    elif target.endswith('windows'):
        expected = 0xaa64 if arm else 0x8664
        if len(data) >= 64 and data[:2] == b'MZ':
            offset = struct.unpack_from('<I', data, 60)[0]
            if offset + 6 <= len(data) and data[offset:offset + 4] == b'PE\0\0':
                actual = struct.unpack_from('<H', data, offset + 4)[0]
    else:
        expected = 0x100000c if arm else 0x1000007
        if len(data) >= 8 and data[:4] == b'\xcf\xfa\xed\xfe':
            actual = struct.unpack_from('<I', data, 4)[0]
    if actual != expected:
        raise RuntimeError(f'{binary}: architecture mismatch for {target}; use a matching native/cross compiler')


def host_target():
    cpu = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    target = cpu + '-' + {'darwin': 'darwin', 'linux': 'linux', 'win32': 'windows'}[sys.platform]
    if target not in TARGETS:
        raise ValueError(f'Unsupported host: {target}')
    return target


def check_source(source, target, work, compiler=None):
    command = [resolve_compiler(compiler), str(source), '--check', '--diagnostic-format', 'json']
    if target:
        command += ['--target', target]
    result = subprocess.run(command, cwd=work, capture_output=True, text=True)
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f'{source} ({target}): invalid diagnostics\n{result.stdout}\n{result.stderr}') from error
    if (result.returncode or not isinstance(value, dict) or value.get('errors') != []
            or not isinstance(value.get('warnings'), list)):
        raise RuntimeError(f'{source} ({target}): {value}\n{result.stderr}')
    if value['warnings']:
        print(json.dumps({'source': str(source), 'target': target, 'warnings': value['warnings']}), flush=True)
    return value


def atomic_copy(source, destination):
    """A failed build or interrupted copy leaves the last published file intact."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with Path(source).open('rb') as incoming:
                shutil.copyfileobj(incoming, stream)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def publish_files(source, destinations):
    """Stage every mirror before replacing any, with rollback on ordinary errors.

    Each rename is atomic. This is not a filesystem-wide atomic snapshot or a
    power-loss transaction; cooperating build clients hold the project lock.
    """
    prepared, committed = [], []
    retain = set()
    try:
        for destination in destinations:
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise ValueError(f'publication requires a regular destination: {destination}')
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.buoy-publish-', delete=False) as stream:
                staged = Path(stream.name)
            row = [destination, staged, None]
            prepared.append(row)
            with staged.open('wb') as stream, Path(source).open('rb') as incoming:
                shutil.copyfileobj(incoming, stream)
                stream.flush()
                os.fsync(stream.fileno())
            shutil.copymode(source, staged)
            if destination.exists():
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.buoy-backup-', delete=False) as stream:
                    row[2] = Path(stream.name)
                shutil.copy2(destination, row[2])
                with row[2].open('r+b') as stream:
                    os.fsync(stream.fileno())
        for row in prepared:
            os.replace(row[1], row[0])
            committed.append(row)
    except BaseException:
        for destination, staged, backup in reversed(committed):
            try:
                if backup:
                    os.replace(backup, destination)
                else:
                    destination.unlink(missing_ok=True)
            except OSError:
                if backup:
                    retain.add(backup)
        if retain:
            raise RuntimeError('publication rollback failed; backups retained: ' + ', '.join(map(str, retain)))
        raise
    finally:
        for destination, staged, backup in prepared:
            staged.unlink(missing_ok=True)
            if backup and backup not in retain:
                backup.unlink(missing_ok=True)


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(stream.read()).hexdigest()


NATIVE_ENVIRONMENT = ('CC', 'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET', 'INCLUDE', 'LIB', 'LIBPATH',
                      'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'LIBRARY_PATH')


def native_source_digest(package):
    package = Path(package)
    paths = sorted(p for base in ('native', 'modules') for p in (package / base).rglob('*')
                   if p.is_file() and p.suffix.lower() in ('.c', '.h', '.inc', '.cc', '.cpp', '.bui'))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(package).as_posix().encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def write_native_receipt(binary, target, compiler, command, recipe, source_digest):
    from buoydev.common import atomic_write
    executable = shutil.which(compiler[0])
    if not executable:
        raise ValueError(f'native compiler not found: {compiler[0]}')
    executable = str(Path(executable).resolve())
    value = dict(schema=2, target=target, source_digest=source_digest, recipe_digest=file_hash(recipe),
                 binary_digest=file_hash(binary), compiler=dict(path=executable, sha256=file_hash(executable)),
                 command=list(command), compiler_arguments=list(compiler),
                 build_host=host_target(), builder_machine=hashlib.sha256(platform.node().encode()).hexdigest(),
                 environment={k: os.environ.get(k) for k in NATIVE_ENVIRONMENT})
    atomic_write(Path(binary).with_name(Path(binary).name + '.source.json'), json.dumps(value, indent=2) + '\n')
    atomic_write(Path(binary).with_name(Path(binary).name + '.source-sha256'), source_digest)


def verify_native_receipt(binary, target, recipe, source_digest):
    from buoydev.common import load_json
    receipt = Path(binary).with_name(Path(binary).name + '.source.json')
    if not receipt.is_file():
        return False
    value = load_json(receipt)
    if (value.get('schema') != 2 or value.get('target') != target or value.get('source_digest') != source_digest
            or value.get('recipe_digest') != file_hash(recipe) or value.get('binary_digest') != file_hash(binary)):
        return False
    if (value.get('build_host') == host_target()
            and value.get('builder_machine') == hashlib.sha256(platform.node().encode()).hexdigest()):
        compiler = value.get('compiler', {})
        path = Path(compiler.get('path', ''))
        if not path.is_file() or file_hash(path) != compiler.get('sha256'):
            return False
        if value.get('environment') != {k: os.environ.get(k) for k in NATIVE_ENVIRONMENT}:
            return False
    return True


def bundled_package(name, compiler):
    """Resolve one explicitly requested toolchain archive, never workspace packages."""
    executable = Path(resolve_compiler(compiler)).resolve()
    folders = []
    if os.environ.get('BUOY_PACKAGES_DIR'):
        folders.append(Path(os.environ['BUOY_PACKAGES_DIR']).resolve())
    folders += [executable.parent / 'packages', executable.parent.parent / 'packages',
                Path('/usr/share/buoy/packages'), Path('/Applications/Buoy/packages'), Path('C:/Buoy/packages')]
    for folder in folders:
        matches = sorted(folder.glob(name + '-*.buipkg'))
        if len(matches) > 1:
            raise ValueError(f'ambiguous bundled package {name} in {folder}')
        if matches:
            package_manifest(matches[0], expected_name=name)
            return matches[0].resolve()
    raise ValueError(f'bundled package {name} not found; set BUOY_PACKAGES_DIR')


def package_manifest(archive, expected_name=None):
    import zipfile
    from buoydev.common import parse_json
    with zipfile.ZipFile(archive) as opened:
        if opened.namelist().count('manifest.json') != 1:
            raise ValueError(f'expected one package manifest: {archive}')
        value = parse_json(opened.read('manifest.json').decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'package manifest must be an object: {archive}')
    name, version = value.get('name'), value.get('version')
    if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', name)
            or not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9.+_-]+', version)
            or Path(archive).name != f'{name}-{version}.buipkg'
            or (expected_name is not None and name != expected_name)):
        raise ValueError(f'package filename/manifest identity mismatch: {archive}')
    return value


def verify_package_sources(folder, archive):
    """Enforce check's prerequisite instead of accepting stale dependency APIs."""
    import zipfile
    folder = Path(folder)
    with zipfile.ZipFile(archive) as opened:
        source_manifest = json.loads((folder / 'manifest.json').read_text())
        sealed_manifest = json.loads(opened.read('manifest.json'))
        if any(sealed_manifest.get(k) != v for k, v in source_manifest.items()):
            raise ValueError(f'stale dependency {archive}; build dependencies before checking')
        sources = {p.relative_to(folder).as_posix(): p for name in ('modules', 'resources', 'link')
                   for p in (folder / name).rglob('*') if p.is_file()}
        archived = {name for name in opened.namelist() if name.startswith(('modules/', 'resources/', 'link/')) and not name.endswith('/')}
        if set(sources) != archived or any(opened.read(name) != path.read_bytes() for name, path in sources.items()):
            raise ValueError(f'stale dependency {archive}; build dependencies before checking')


def verify_and_seal(folder, output, dependencies=(), targets=None, compiler=None,
                    packager=None, check_only=False, checks=None, mirrors=()):
    """Check an allowlisted package in isolation, then publish a completed seal.

    Native packages keep their specialized adapters. Selecting modules/resources
    explicitly avoids adding native build trees or ambient packages to archives.
    """
    folder, output = Path(folder).resolve(), Path(output).absolute()
    compiler = resolve_compiler(compiler)
    packager = resolve_packager(packager, compiler=compiler)
    from buoydev.common import load_json
    manifest = load_json(folder / 'manifest.json')
    if any(not library.get('system') for library in manifest.get('libraries', [])):
        raise ValueError(f'{folder}: native package requires a specialized build adapter')
    # Text system-library stubs carry no compiled slice. Preserve them (e.g.
    # Keychain's Security.framework alias), while rejecting native link binaries.
    if any(p.is_file() and p.suffix != '.tbd' for p in (folder / 'link').rglob('*')):
        raise ValueError(f'{folder}: native link binaries require a specialized build adapter')
    with tempfile.TemporaryDirectory(prefix='buoy-package-') as directory:
        work = Path(directory)
        stage = work / 'library'
        stage.mkdir()
        shutil.copy2(folder / 'manifest.json', stage / 'manifest.json')
        for name in ('modules', 'resources', 'link'):
            if (folder / name).exists():
                for path in (folder / name, *(folder / name).rglob('*')):
                    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                        raise ValueError(f'linked package input: {path}')
                    if not path.is_file() and not path.is_dir():
                        raise ValueError(f'unsupported package input: {path}')
                shutil.copytree(folder / name, stage / name)
        modules = [stage / name for name in checks] if checks is not None else sorted((stage / 'modules').rglob('*.bui'))
        if not modules:
            raise ValueError(f'{folder}: no Buoy modules')
        packages = work / 'packages'
        packages.mkdir()
        identities = set()
        import zipfile
        for archive in dependencies:
            archive = Path(archive)
            identity = package_manifest(archive)['name']
            if identity in identities or identity == manifest['name']:
                raise ValueError(f'duplicate dependency package: {identity}')
            identities.add(identity)
            shutil.copy2(archive, packages / archive.name)
        sealed = packages / f'{manifest["name"]}-{manifest["version"]}.buipkg'
        # Self-package imports must resolve to this source snapshot too.
        subprocess.run([packager, 'pkg', 'seal', str(stage), '-o', str(sealed)], cwd=work, check=True)
        for source in modules:
            for target in targets or TARGETS:
                check_source(source, target, work, compiler)
        if not check_only:
            publish_files(sealed, [output, *mirrors])
        return output
