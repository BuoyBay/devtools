"""Resolve compiler and package tools before builders enter temporary workspaces."""
import os
from pathlib import Path
import shutil


def _resolve(value, role, selector, cwd, environ):
    if not isinstance(value, str) or not value:
        raise RuntimeError(f'{role} from {selector}: executable must not be empty')
    base = Path(cwd or Path.cwd()).absolute()
    if '/' in value or '\\' in value or Path(value).is_absolute():
        path = Path(value)
        if not path.is_absolute():
            path = base / path
    else:
        search = os.pathsep.join(str(Path(part) if Path(part).is_absolute() else base / part)
                                 for part in environ.get('PATH', os.defpath).split(os.pathsep))
        found = shutil.which(value, path=search)
        if not found:
            raise RuntimeError(f'{role} from {selector}: executable {value!r} not found on PATH')
        path = Path(found)
    if not path.is_file():
        raise RuntimeError(f'{role} from {selector}: executable {value!r} is missing or not a file')
    if not os.access(path, os.X_OK):
        raise RuntimeError(f'{role} from {selector}: executable {value!r} is not executable')
    return str(path.absolute())


def resolve_compiler(value=None, *, cwd=None, environ=None):
    env = os.environ if environ is None else environ
    selector = '--compiler' if value is not None else 'BUOY' if 'BUOY' in env else 'PATH'
    selected = value if value is not None else env.get('BUOY', 'buoy')
    return _resolve(selected, 'compiler', selector, cwd, env)


def compiler_build_command(compiler, *arguments):
    """Supply the installed Windows runtime path missing in Buoy 0.72."""
    command = [compiler, *map(str, arguments)]
    if os.name == 'nt':
        import platform
        cpu = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x86_64'
        runtime = Path(resolve_compiler(compiler)).parent / 'lib/runtime' / (cpu + '-pc-windows-msvc')
        if runtime.is_dir():
            command += ['-L', str(runtime)]
    return command


def deploy_compiler_runtime(compiler, directory):
    """Copy missing SDK DLLs beside native Windows consumers."""
    if os.name != 'nt':
        return
    import platform
    cpu = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x86_64'
    runtime = Path(resolve_compiler(compiler)).parent / 'lib/runtime' / (cpu + '-pc-windows-msvc')
    for library in runtime.glob('*.dll'):
        destination = Path(directory) / library.name
        if not destination.exists():
            shutil.copy2(library, destination)


def resolve_packager(value=None, *, compiler=None, cwd=None, environ=None):
    env = os.environ if environ is None else environ
    if value is not None or 'BPM_PACKAGER' in env:
        return _resolve(value if value is not None else env['BPM_PACKAGER'], 'packager',
                        '--packager' if value is not None else 'BPM_PACKAGER', cwd, env)
    selected = resolve_compiler(compiler, cwd=cwd, environ=env)
    path = Path(selected)
    if path.resolve() == Path('/usr/local/bin/buoy'):
        try:
            with path.open('rb') as stream:
                launcher = stream.read(4096)
        except OSError as error:
            raise RuntimeError(f'Cannot inspect compiler launcher {path}: {error}') from error
        if launcher.startswith(b'#!') and b'/usr/bin/buoy' in launcher:
            try:
                return _resolve('/usr/bin/buoy', 'packager', 'recognized compiler launcher', cwd, env)
            except RuntimeError as error:
                raise RuntimeError(f'{error}; set BPM_PACKAGER or --packager to the package executable') from error
    return selected


def add_toolchain_arguments(parser):
    parser.add_argument('--compiler', metavar='EXECUTABLE', help='Compiler executable (overrides BUOY)')
    parser.add_argument('--packager', metavar='EXECUTABLE', help='Package executable (overrides BPM_PACKAGER)')


def configure_toolchain(compiler=None, packager=None):
    """Freeze invocation-relative selection and propagate it to child builders."""
    selected = resolve_compiler(compiler)
    sealer = resolve_packager(packager, compiler=selected)
    os.environ.update(BUOY=selected, BPM_PACKAGER=sealer)
    return selected, sealer
