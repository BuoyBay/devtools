"""Embed build provenance from the actual staged Studio inputs."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess

RESOURCE = 'studio-build-info.json'
PROVENANCE = 'studio-source-provenance.json'


def git_provenance(root):
    root = Path(root)
    def git(*args):
        result = subprocess.run(['git', '-C', str(root), *args],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        top = git('rev-parse', '--show-toplevel')
        if top and Path(top).resolve() == root.resolve():
            status = git('status', '--porcelain', '--untracked-files=normal')
            return {'commit': git('rev-parse', 'HEAD'),
                    'branch': git('branch', '--show-current') or 'detached HEAD',
                    'description': git('describe', '--always', '--tags', '--dirty'),
                    'commitTime': git('show', '-s', '--format=%cI', 'HEAD'),
                    'dirty': bool(status) if status is not None else None}
    except (OSError, subprocess.TimeoutExpired):
        pass
    archived = root / PROVENANCE
    if archived.is_file():
        value = json.loads(archived.read_text())
        if not isinstance(value, dict):
            raise ValueError('Studio source provenance must be an object')
        return value
    return {'commit': None, 'branch': None, 'dirty': None,
            'description': 'Source archive without git provenance'}


def tool_info(executable):
    """Identify the invoked executable; this can be a user-selected wrapper."""
    result = subprocess.run([str(executable), '--version'], capture_output=True,
                            text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Cannot identify Studio build tool: ' + str(executable))
    return {'version': result.stdout.strip(), 'path': str(Path(executable).absolute()),
            'sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest()}


def staged_inputs(work):
    """Hash staged inputs, not compiler caches or previously produced executables.

    The resource manifest is an input too. Its self-reference to the metadata
    resource is removed before hashing, making a metadata refresh idempotent.
    """
    work = Path(work)
    paths = list(work.glob('*.bui')) + list((work / 'packages').glob('*.buipkg'))
    paths += [p for p in (work / 'resources').rglob('*') if p.is_file()]
    for pattern in ('studio-shell-terminal.*', 'libBuoyStudioShellTerminal.*',
                    'BuoyStudioShellTerminal.*'):
        paths += [p for p in work.glob(pattern) if p.is_file()]
    inputs = []
    for path in sorted(set(paths)):
        relative = path.relative_to(work).as_posix()
        if path.name == RESOURCE:
            continue
        content = path.read_bytes()
        if relative == 'resources/app_resources.json':
            manifest = json.loads(content)
            manifest['resources'] = [entry for entry in manifest['resources']
                                     if entry.get('path') != RESOURCE]
            content = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        inputs.append({'path': relative, 'sha256': hashlib.sha256(content).hexdigest()})
    return inputs


def source_fingerprint(inputs):
    return hashlib.sha256(json.dumps(inputs, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def write_build_info(root, work, compiler, packager, target=None, credential_profile='production'):
    work = Path(work)
    inputs = staged_inputs(work)
    fingerprint = source_fingerprint(inputs)
    now = datetime.now(timezone.utc)
    machine = {'aarch64': 'arm64', 'AMD64': 'x86_64'}.get(platform.machine(), platform.machine())
    system = {'Darwin': 'darwin', 'Windows': 'windows', 'Linux': 'linux'}[platform.system()]
    packages = []
    for path in sorted((work / 'packages').glob('*.buipkg')):
        packages.append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    info = {'schema': 1, 'application': 'Buoy Studio', 'version': 'development',
            'credentialProfile': credential_profile,
            'buildId': now.strftime('%Y%m%dT%H%M%SZ') + '-' + fingerprint[:12],
            'builtAtUtc': now.isoformat(timespec='seconds'),
            'git': git_provenance(root), 'sourceFingerprint': fingerprint,
            'fingerprintScope': 'Staged Buoy sources, packages, resources, normalized resource manifest and native link inputs; excludes compiler caches, produced executables and generated identity metadata.',
            'target': target or machine + '-' + system,
            'buildOS': platform.system() + ' ' + platform.release(),
            'compiler': tool_info(compiler), 'packager': tool_info(packager),
            'packages': packages, 'inputs': inputs}
    (work / 'resources' / RESOURCE).write_text(json.dumps(info, indent=2) + '\n')
    manifest = work / 'resources/app_resources.json'
    data = json.loads(manifest.read_text())
    data['resources'] = [entry for entry in data['resources'] if entry.get('path') != RESOURCE]
    data['resources'].append({'kind': 'Resource', 'path': RESOURCE})
    manifest.write_text(json.dumps(data, indent=2) + '\n')
    return info
