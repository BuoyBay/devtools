#!/usr/bin/env python3
"""Build/check the optional native BuoyScript helper without linking LLVM into Studio."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TRIPLES = {'arm64-darwin': 'arm64-apple-macos', 'x86_64-darwin': 'x86_64-apple-macos',
           'arm64-linux': 'aarch64-unknown-linux-gnu', 'x86_64-linux': 'x86_64-unknown-linux-gnu',
           'arm64-windows': 'arm64-pc-windows-msvc', 'x86_64-windows': 'x86_64-pc-windows-msvc'}


def deploy_runtime(package, host, output):
    """Console builds retain package rpaths; deploy before their staging tree goes away."""
    prefix = 'runtime/' + TRIPLES[host] + '/'
    deployed = []
    with zipfile.ZipFile(package) as archive:
        for member in archive.infolist():
            if not member.filename.startswith(prefix) or member.is_dir():
                continue
            relative = PurePosixPath(member.filename[len(prefix):])
            if relative.is_absolute() or '..' in relative.parts:
                raise RuntimeError('Invalid SDK runtime path: ' + member.filename)
            # Shared runtime payloads stay together with their relative layout.
            destination = output.parent.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
            deployed.append(str(relative))
    if not deployed:
        raise RuntimeError('SDK archive has no runtime files for ' + host)
    if host.endswith('-darwin'):
        # The compiler's console rpath otherwise points into TemporaryDirectory.
        subprocess.run(['install_name_tool', '-add_rpath', '@executable_path', str(output)], check=True)
        subprocess.run(['codesign', '--force', '--sign', '-', str(output)], check=True)
    elif host.endswith('-linux'):
        patcher = shutil.which('patchelf')
        if patcher is None:
            raise RuntimeError('Install patchelf to give the deployed Linux console helper an $ORIGIN runtime search path')
        subprocess.run([patcher, '--add-rpath', '$ORIGIN', str(output)], check=True)
    # Windows searches the executable directory for adjacent package DLLs.
    return deployed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, help='Matching SDK buoyscript archive')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/studio-script-runner')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    compiler = os.environ.get('BUOY', 'buoy')
    package = args.package
    if package is None:
        folders = ([Path(os.environ['BUOY_PACKAGES_DIR'])] if os.environ.get('BUOY_PACKAGES_DIR') else [])
        folders += [Path('/usr/share/buoy/packages'), Path('/Applications/Buoy/packages')]
        executable = shutil.which(compiler)
        if executable:
            folders += [Path(executable).resolve().parent / 'packages']
        package = next((p for folder in folders for p in sorted(folder.glob('buoyscript-*.buipkg'), reverse=True)), None)
    if package is None or not package.is_file():
        parser.error('BuoyScript SDK package missing; install a matching buoyscript archive or pass --package PATH. Studio project tools remain usable.')
    machine = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    host = machine + ('-darwin' if platform.system() == 'Darwin' else '-windows' if os.name == 'nt' else '-linux')
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        platforms = manifest.get('platforms', [])
        runtime_present = any(name.startswith('runtime/' + TRIPLES.get(host, host) + '/') for name in archive.namelist())
    with tempfile.TemporaryDirectory(prefix='studio-script-runner-') as directory:
        work = Path(directory)
        (work / 'packages').mkdir()
        shutil.copy2(package, work / 'packages' / package.name)
        source = work / 'studio_script_runner.bui'
        shutil.copy2(ROOT / 'tools/studio_script_runner.bui', source)
        for target in TRIPLES:
            checked = subprocess.run([compiler, str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=work, capture_output=True, text=True, timeout=60)
            diagnostics = json.loads(checked.stdout)
            if checked.returncode or diagnostics['errors']:
                raise RuntimeError(f'{target}: {diagnostics}\n{checked.stderr}')
            if diagnostics['warnings']:
                print(diagnostics['warnings'])
        print('Runner source passed six-target compiler checks.')
        if args.check_only:
            print(f'Native runtime available for {host}: {runtime_present}; archive platforms: {platforms}')
            return
        if not runtime_present:
            parser.error(f'BuoyScript native runtime missing for {host}; archive supports {platforms}. Obtain the matching native SDK archive. Six-target semantic checks do not verify JIT execution.')
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([compiler, str(source), '-o', str(output)], cwd=work, check=True)
        deployed = deploy_runtime(package, host, output)
        print(f'Built {output}; deployed runtime files: {deployed}. Keep the directory together. Set BUOY_STUDIO_SCRIPT_RUNNER to the absolute executable path.')


if __name__ == '__main__':
    main()
