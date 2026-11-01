"""Publish complete font archives and verify their workspace inputs."""
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import zipfile
from build_support import publish_files

MANUALS = dict.fromkeys(('fontkit', 'textmetrics', 'shapingkit', 'fontcanvas'),
                      'resources/buoy-docs/reference.md')
NATIVE_SOURCES = {
    'fontkit': 'native/bridge.c', 'textmetrics': 'native/macos.c',
    'shapingkit': 'native/bridge.c', 'fontcanvas': 'native/bridge.cpp',
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def atomic_copy(source, destination):
    """Readers see the old or complete new file, never an in-place copy."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.' + destination.name + '.',
                                         dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            with source.open('rb') as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copymode(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def manual_bytes(package):
    """Keep the complete manual in the documentation tree compiler refresh retains."""
    package = Path(package)
    name = json.loads((package / 'manifest.json').read_text())['name']
    text = (package / 'README.md').read_text()
    def link(match):
        label, target = match.group(1), match.group(2)
        if target.startswith(('#', '/', '<')) or re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', target):
            return match.group(0)
        # Workspace evidence is not shipped with a library. Preserve its location
        # as text rather than emitting a broken link outside the sealed package.
        resolved = (package / target.split('#', 1)[0]).resolve()
        if not resolved.is_relative_to(package.resolve()):
            workspace = package.parent.parent.resolve()
            location = resolved.relative_to(workspace).as_posix() if resolved.is_relative_to(workspace) else target
            return label + ' (workspace file `' + location + '`)'
        return '[' + label + '](' + '../../' + target + ')'
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, text)
    front = ('---\n' + 'title: ' + json.dumps(name + ' reference') + '\n'
             + 'summary: "Capabilities, ownership, backends and verification."\n'
             + 'category: Reference\n'
             + 'related: ["index.md", "getting-started.md", "typed-api.md"]\n---\n\n')
    return (front + text).encode()


def prepare_manual(package):
    package = Path(package)
    if not (package / 'README.md').is_file():
        return
    name = json.loads((package / 'manifest.json').read_text())['name']
    manual = package / MANUALS[name]
    data = manual_bytes(package)
    manual.parent.mkdir(parents=True, exist_ok=True)
    if not manual.is_file() or manual.read_bytes() != data:
        manual.write_bytes(data)
    index = manual.parent / 'index.md'
    if index.is_file():
        text = index.read_text()
        for old in ('resources/fontkit.md', 'resources/textmetrics.md',
                    'resources/fontcanvas.md', 'resources/shapingkit-manual.md'):
            text = text.replace('`' + old + '`', '[reference manual](reference.md)')
        if '(reference.md)' not in text:
            text += '\n[Full manual and platform limits](reference.md).\n'
        if text != index.read_text():
            index.write_text(text)


def verify_archive(package, archive):
    """Reject stale source/native/manual content before updating a mirror."""
    package, archive = Path(package), Path(archive)
    name = json.loads((package / 'manifest.json').read_text())['name']
    archive_bytes = archive.read_bytes()
    record = {'sha256': sha256(archive_bytes), 'modules': {},
              'nativeArtifacts': {}, 'resources': {}}
    native_digest = sha256((package / NATIVE_SOURCES[name]).read_bytes())
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
        names = set(bundle.namelist())
        source_manifest = json.loads((package / 'manifest.json').read_text())
        manifest = json.loads(bundle.read('manifest.json'))
        for key, value in source_manifest.items():
            if manifest.get(key) != value:
                raise RuntimeError(f'{archive}: source manifest field differs: {key}')
        modules = {p.relative_to(package).as_posix(): p
                   for p in (package / 'modules').rglob('*.bui')}
        if {n for n in names if n.startswith('modules/') and n.endswith('.bui')} != modules.keys():
            raise RuntimeError(f'{archive}: module list differs from source')
        for relative, source in modules.items():
            data = bundle.read(relative)
            if data != source.read_bytes():
                raise RuntimeError(f'{archive}: stale module {relative}')
            record['modules'][relative] = sha256(data)
        for relative in sorted(names):
            if not relative.startswith(('runtime/', 'link/')) or relative.endswith('/'):
                continue
            source = package / relative
            data = bundle.read(relative)
            if not source.is_file() or data != source.read_bytes():
                raise RuntimeError(f'{archive}: stale native artifact {relative}')
            if relative.startswith('runtime/') and not relative.endswith('.source-sha256'):
                receipt = source.with_name(source.name + '.source-sha256')
                if not receipt.is_file() or receipt.read_text().strip() != native_digest:
                    raise RuntimeError(f'{archive}: native source receipt differs: {relative}')
            record['nativeArtifacts'][relative] = sha256(data)
        for source in (package / 'runtime').rglob('*'):
            if not source.is_file() or source.suffix not in ('.so', '.dll', '.dylib'):
                continue
            receipt = source.with_name(source.name + '.source-sha256')
            if receipt.is_file() and receipt.read_text().strip() == native_digest:
                relative = source.relative_to(package).as_posix()
                if relative not in names:
                    raise RuntimeError(f'{archive}: missing current native slice {relative}')
        for source in sorted((package / 'resources').rglob('*')):
            if not source.is_file():
                continue
            relative = source.relative_to(package).as_posix()
            if relative not in names or bundle.read(relative) != source.read_bytes():
                raise RuntimeError(f'{archive}: stale/missing source resource {relative}')
            record['resources'][relative] = sha256(bundle.read(relative))
        manual = MANUALS[name]
        if manual not in names or bundle.read(manual) != manual_bytes(package):
            raise RuntimeError(f'{archive}: stale/missing package manual {manual}')
        record['manual'] = {'path': manual, 'sha256': sha256(bundle.read(manual))}
    record['nativeSourceSha256'] = native_digest
    return record


def publish_archive(staged_archive, package, root):
    """Verify once, stage both mirrors, and roll back ordinary commit failures."""
    root, staged_archive = Path(root), Path(staged_archive)
    verify_archive(package, staged_archive)
    output = root / 'packages' / staged_archive.name
    publish_files(staged_archive, [output, root / 'dist' / staged_archive.name])
    return output


def verify_mirrors(root, names=tuple(MANUALS)):
    root = Path(root)
    result = {}
    for name in names:
        package = root / 'libraries' / name
        manifest = json.loads((package / 'manifest.json').read_text())
        archive = root / 'packages' / f'{name}-{manifest["version"]}.buipkg'
        mirror = root / 'dist' / archive.name
        record = verify_archive(package, archive)
        final_bytes = archive.read_bytes()
        if sha256(final_bytes) != record['sha256']:
            raise RuntimeError(f'{name}: archive changed while verifying; rerun after the writer finishes')
        if final_bytes != mirror.read_bytes():
            raise RuntimeError(f'{name}: package/dist archive differs')
        record['matchingDist'] = True
        result[name] = record
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--write-receipts', type=Path)
    args = parser.parse_args()
    records = verify_mirrors(args.root)
    if args.write_receipts:
        with tempfile.TemporaryDirectory(prefix='font-package-receipts-') as directory:
            source = Path(directory) / 'receipts.json'
            source.write_text(json.dumps(records, indent=2) + '\n')
            atomic_copy(source, args.write_receipts)
    for name, record in records.items():
        print(f'{name}: source/native/manual verified, mirrors identical, sha256={record["sha256"]}')
