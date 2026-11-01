#!/usr/bin/env python3
"""Build native Studio with sealed package archives in an isolated consumer.

Build the real LayoutKit/LayoutCanvas archives with tools/build_layoutkit.py
first. Studio imports LayoutDocument/LayoutCatalog explicitly from LayoutKit;
LayoutCanvas supplies its self-named entry module. Opened source is data only.
"""
import argparse
import copy
import hashlib
import json
import os
import plistlib
import re
import platform
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import zipfile
from studio_build_info import write_build_info

from buoy_executables import (resolve_compiler, resolve_packager,
                              add_toolchain_arguments, configure_toolchain)

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ('controlsnapshot', 'editorkit', 'projectkit', 'agentkit', 'lspkit', 'processkit', 'agentsession', 'chatkit', 'layoutkit', 'layoutcanvas', 'guilintkit', 'previewkit', 'terminalkit', 'githubkit', 'forgejokit', 'atomicfilekit', 'progresskit', 'fontkit', 'diffkit', 'gitkit', 'fuzzyfind', 'keychain', 'secretstore', 'maskkit', 'desktopmenus', 'modelkit', 'chatgptauth')
# Fresh public checkouts contain package roots, not a seeded packages/ directory.
# All services are imported by Studio; seal every dependency unless the caller
# explicitly requests pinned archives with --sealed-packages-only.
FRESH_PACKAGES = PACKAGES
GTK_PREVIEW_OBJECT = 'studio-gtk-lifecycle.o'
SHELL_TERMINAL_OBJECT = 'studio-shell-terminal.o'
SHELL_TERMINAL_LIBRARY = 'libBuoyStudioShellTerminal.dylib'
SHELL_TERMINAL_WINDOWS_LIBRARY = 'BuoyStudioShellTerminal.dll'
SHELL_TERMINAL_WINDOWS_IMPORT = 'BuoyStudioShellTerminal.lib'
OPTIONAL_PACKAGES = ()
# Standalone native component modules reused verbatim from containers/integrations.
# They are copied into the isolated consumer and imported by module name.
COMPONENTS = ('treeview', 'searchfield', 'segmentedbutton')
# Multi-module integrations copied verbatim (directory -> modules). TextInputCanvas
# supplies the source editor's native IME adapters (macOS/Windows/GTK).
COMPONENT_MODULES = {'contextmenu': ('gtkcontextmenu',), 'textinput': ('textmodel', 'textinputcanvas', 'inputbridge', 'nativewindows', 'nativemacos')}
TARGETS = ('arm64-darwin', 'x86_64-darwin', 'arm64-linux', 'x86_64-linux', 'arm64-windows', 'x86_64-windows')


SEALABLE = ('layoutkit', 'layoutcanvas', 'chatkit')
DISCOVERY_HELPER = ROOT / 'tools' / 'discover_layout_packages.py'
# The documentation reader, its data-only helper, and Buoy's own documentation snapshot
# ship as Resources (resource name -> repository source).
DOCUMENTATION_RESOURCES = {
    'studio-script-primer.md': ROOT / 'apps/BuoyStudio/docs/BUOYSCRIPT.md',
    'unicode-LICENSE.txt': ROOT / 'apps/BuoyStudio/resources/unicode-LICENSE.txt',
    'studio_debug_adapter.py': ROOT / 'tools/studio_debug_adapter.py',
    'documentation.html': ROOT / 'apps/BuoyStudio/documentation.html',
    'discover_documentation.py': ROOT / 'tools/discover_documentation.py',
    'documentation-upstream.json': ROOT / 'apps/BuoyDocs/snapshot/docs.json',
}


def sealer():
    return resolve_packager()


def installed_package_folders():
    """Toolchain package folders: explicit override, standard installs, then the
    compiler's own install (e.g. a BuoyLab-managed macOS toolchain)."""
    folders = [Path(os.environ['BUOY_PACKAGES_DIR'])] if os.environ.get('BUOY_PACKAGES_DIR') else []
    folders += [Path('/usr/share/buoy/packages'), Path('/Applications/Buoy/packages')]
    resolved = shutil.which(resolve_compiler())
    if resolved:
        executable = Path(resolved).resolve()
        folders += [executable.parent / 'packages', executable.parent.parent / 'packages']
    return folders


def host_target():
    machine = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    system = 'darwin' if sys.platform == 'darwin' else 'windows' if os.name == 'nt' else 'linux'
    return machine + '-' + system


def selected_target(target=None):
    target = target or host_target()
    if target not in TARGETS:
        raise RuntimeError('Unsupported Studio target: ' + target)
    if target != host_target() and target != 'x86_64-windows':
        raise RuntimeError('Studio cross-build currently supports x86_64-windows only; build ' + target + ' on its native host')
    return target


def build_shell_terminal(work, target=None):
    target = selected_target(target)
    flags = ['-std=c11', '-O2', '-U_FORTIFY_SOURCE', '-D_FORTIFY_SOURCE=0']
    output = Path(work) / SHELL_TERMINAL_OBJECT
    if target.endswith('-windows'):
        # Let the platform C toolchain resolve its CRT and complete Windows SDK.
        # Linking a raw MinGW object into Buoy needs absent kernel32 stubs (BUI-524)
        # and MinGW-private CRT symbols. Import the helper DLL instead.
        default_cc = ('clang-cl' if shutil.which('clang-cl') else 'gcc') if os.name == 'nt' else 'x86_64-w64-mingw32-gcc'
        output = Path(work) / SHELL_TERMINAL_WINDOWS_LIBRARY
        flags += ['-shared', '-Wl,--out-implib,' + str(Path(work) / SHELL_TERMINAL_WINDOWS_IMPORT)]
    elif target.endswith('-darwin'):
        default_cc = 'cc'
        flags += ['-fPIC', '-dynamiclib', '-mmacosx-version-min=' + os.environ.get('MACOSX_DEPLOYMENT_TARGET', '13.0'),
                  '-install_name', '@executable_path/../Frameworks/' + SHELL_TERMINAL_LIBRARY]
        output = Path(work) / SHELL_TERMINAL_LIBRARY
    else:
        default_cc = 'cc'
        flags += ['-fPIC', '-c']
    selector = 'STUDIO_SHELL_CC' if target != host_target() else 'CC'
    compiler = shlex.split(os.environ.get(selector, default_cc))
    if not compiler or not shutil.which(compiler[0]):
        raise RuntimeError('Studio shell terminal C compiler is missing for ' + target + '; set ' + selector)
    source = str(ROOT / 'apps/BuoyStudio/StudioShellTerminal.c')
    if target.endswith('-windows') and Path(compiler[0]).stem.lower() in ('cl', 'clang-cl'):
        command = compiler + ['/nologo', '/LD', '/O2', '/std:c11', '/D_CRT_SECURE_NO_WARNINGS', source,
                              '/Fo:' + str(Path(work) / 'studio-shell-terminal.obj'), '/Fe:' + str(output),
                              '/link', '/IMPLIB:' + str(Path(work) / SHELL_TERMINAL_WINDOWS_IMPORT)]
    else:
        command = compiler + flags + [source, '-o', str(output)]
    subprocess.run(command, cwd=work, check=True)
    return output


def stage_windows_shell_package(work, target):
    # App-private package gives the compiler both the import library and DLL,
    # so runtime deployment follows the same path as the other native packages.
    target = selected_target(target)
    work = Path(work)
    with tempfile.TemporaryDirectory(prefix='studio-shell-package-', dir=work) as directory:
        package = Path(directory)
        (package / 'manifest.json').write_text(json.dumps({
            'schema': 1, 'name': 'studioshellterminal', 'version': '0.1.0',
            'libraries': [{'libName': 'BuoyStudioShellTerminal', 'system': False}]}))
        (package / 'modules').mkdir()
        (package / 'modules/StudioShellNative.bui').write_text("' App-private native terminal transport.\n")
        # Cross-build target is captured from the actual DLL architecture below.
        data = (work / SHELL_TERMINAL_WINDOWS_LIBRARY).read_bytes()
        machine = int.from_bytes(data[int.from_bytes(data[60:64], 'little') + 4:][:2], 'little')
        expected = 0x8664 if target.startswith('x86_64-') else 0xaa64
        if machine != expected:
            raise RuntimeError('Studio shell helper architecture does not match ' + target)
        triple = {0x8664: 'x86_64-pc-windows-msvc', 0xaa64: 'aarch64-pc-windows-msvc'}[machine]
        for kind, name in (('runtime', SHELL_TERMINAL_WINDOWS_LIBRARY), ('link', SHELL_TERMINAL_WINDOWS_IMPORT)):
            destination = package / kind / triple
            destination.mkdir(parents=True)
            shutil.copy2(work / name, destination / name)
        archive = work / 'packages/studioshellterminal-0.1.0.buipkg'
        subprocess.run([sealer(), 'pkg', 'seal', str(package), '-o', str(archive)], check=True, capture_output=True)
        store_package(archive)


CHATKIT_CONSUMER = '''Import chatkit
Sub RequireStudioChat(panel As ChatPanel, account As ChatPanelAccountHost, attachment As ChatAttachment)
    panel.SetEmptyState("Studio", "Ready", ["Ask"])
    panel.AddAttachment(attachment)
    account.SignOutOfChatGPT()
    account.OpenChatFile("file")
    account.OpenChatLink("https://example.com")
End Sub
Sub Main()
End Sub
'''


def seal_chatkit(work, archive):
    """Seal the exact private source snapshot recorded with the staged inputs.

    ChatKit has no package imports or native slices. Its seal inputs are the
    manifest, modules and resources; web development files are not seal inputs.
    """
    private = work / 'package-sources' / 'chatkit'
    private.mkdir(parents=True)
    source = ROOT / 'libraries/chatkit'
    shutil.copy2(source / 'manifest.json', private / 'manifest.json')
    for folder in ('modules', 'resources'):
        shutil.copytree(source / folder, private / folder)
    inputs = [{'path': p.relative_to(private).as_posix(),
               'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
              for p in sorted(private.rglob('*')) if p.is_file()]
    subprocess.run([sealer(), 'pkg', 'seal', str(private), '-o', str(archive)],
                   cwd=work, check=True, capture_output=True)
    return {'package': archive.name, 'mode': 'private-source-seal',
            'scope': 'Copied ChatKit manifest, modules and resources; not the whole repository library.',
            'inputs': inputs, 'packageSha256': hashlib.sha256(archive.read_bytes()).hexdigest()}


def check_sealed_chatkit(work, target):
    """Reject incompatible pinned archives before the full Studio diagnostic flood."""
    with tempfile.TemporaryDirectory(prefix='chatkit-contract-', dir=work) as directory:
        probe = Path(directory)
        (probe / 'packages').mkdir()
        archive = work / 'packages/chatkit-0.1.0.buipkg'
        shutil.copy2(archive, probe / 'packages' / archive.name)
        (probe / 'main.bui').write_text(CHATKIT_CONSUMER)
        result = subprocess.run([resolve_compiler(), 'main.bui', '--check',
                                 '--diagnostic-format', 'json', '--target', target],
                                cwd=probe, capture_output=True, text=True)
        diagnostics = json.loads(result.stdout)
        if result.returncode or diagnostics.get('errors') != []:
            raise RuntimeError('Incompatible sealed ChatKit archive: Studio requires '
                               'ChatAttachment, ChatPanelAccountHost and ChatPanel.SetEmptyState. '
                               'Update the pinned archive or omit --sealed-packages-only to seal '
                               'current private sources. Diagnostics: ' + result.stdout + result.stderr)


def build_private_gtklifecycle(work, target):
    """Run the existing guard recipe with its source and output inside this build."""
    private = work / 'gtk-lifecycle-build'
    (private / 'tools').mkdir(parents=True)
    shutil.copy2(ROOT / 'tools/build_gtklifecycle.py', private / 'tools/build_gtklifecycle.py')
    native = private / 'libraries/gtklifecycle/native'
    native.mkdir(parents=True)
    shutil.copy2(ROOT / 'libraries/gtklifecycle/native/gtk_lifecycle.c', native)
    built = subprocess.run([sys.executable, str(private / 'tools/build_gtklifecycle.py'),
                            '--object-target', target], cwd=private,
                           check=True, capture_output=True, text=True)
    return Path(built.stdout.strip())


def stage(work, seal_from_source=(), sealed_only=False, target=None, private_target_packages=False, profile='development'):
    if sealed_only and (seal_from_source or private_target_packages):
        raise ValueError('sealed-packages-only cannot be combined with source sealing or private target packages')
    target = selected_target(target)
    (work / 'packages').mkdir()
    source_seals = []
    for name in PACKAGES + (OPTIONAL_PACKAGES if sealed_only else ()):
        package = ROOT / 'packages' / f'{name}-0.1.0.buipkg'
        if not sealed_only and (name in seal_from_source or name in FRESH_PACKAGES):
            # Development builds use current service APIs; repository archives stay untouched.
            if name == 'chatkit':
                source_seals.append(seal_chatkit(work, work / 'packages' / package.name))
            else:
                subprocess.run([sealer(), 'pkg', 'seal', str(ROOT / 'libraries' / name), '-o', str(work / 'packages' / package.name)], check=True, capture_output=True)
            store_package(work / 'packages' / package.name)
            if name == 'chatkit':
                source_seals[-1]['packageSha256'] = hashlib.sha256((work / 'packages' / package.name).read_bytes()).hexdigest()
            continue
        if not package.is_file():
            raise RuntimeError(f'Missing {package}' + ('; --sealed-packages-only requires pinned archives; omit it for default fresh sources' if sealed_only else '; build the developer-tool packages and run tools/build_layoutkit.py first'))
        shutil.copy2(package, work / 'packages' / package.name)
        if target.endswith('-windows'):
            store_package(work / 'packages' / package.name)
        if sealed_only and name == 'chatkit':
            check_sealed_chatkit(work, target)
    for name in (() if sealed_only else OPTIONAL_PACKAGES):
        package = ROOT / 'packages' / f'{name}-0.1.0.buipkg'
        if name in seal_from_source:
            subprocess.run([sealer(), 'pkg', 'seal', str(ROOT / 'libraries' / name), '-o', str(work / 'packages' / package.name)], check=True, capture_output=True)
        elif package.is_file():
            shutil.copy2(package, work / 'packages' / package.name)
    for installed in ('crypto-1.0.0.buipkg', 'fluenticons-1.0.0.buipkg', 'regex-1.0.0.buipkg'):
        for folder in installed_package_folders():
            package = folder / installed
            if package.is_file():
                shutil.copy2(package, work / 'packages' / package.name)
                if target.endswith('-windows'):
                    store_package(work / 'packages' / package.name)
                break
        else:
            if installed == 'regex-1.0.0.buipkg':
                raise RuntimeError('Missing required installed Regex package: ' + installed)
    if private_target_packages:
        from studio_release_packages import stage_private_target_packages
        stage_private_target_packages(ROOT, work, target)
    # The package-discovery helper ships inside the app as a Resource, routed by the
    # compiler at link time (no post-codegen copy, so macOS signing stays intact).
    if not DISCOVERY_HELPER.is_file():
        raise RuntimeError(f'Missing {DISCOVERY_HELPER}')
    (work / 'resources').mkdir()
    if source_seals:
        (work / 'resources/studio-package-sources.json').write_text(
            json.dumps({'schema': 1, 'sourceSeals': source_seals}, indent=2) + '\n')
    build_shell_terminal(work, target)
    if target.endswith('-windows'):
        stage_windows_shell_package(work, target)
    shutil.copy2(DISCOVERY_HELPER, work / 'resources' / DISCOVERY_HELPER.name)
    resources = [{'kind': 'Resource', 'path': DISCOVERY_HELPER.name}]
    if source_seals:
        resources.append({'kind': 'Resource', 'path': 'studio-package-sources.json'})
    if target.endswith('-linux'):
        # Bundle the host object for both Studio and the preview compiler. The
        # installed app does not need a C compiler to launch guarded previews.
        shutil.copy2(build_private_gtklifecycle(work, target), work / 'resources' / GTK_PREVIEW_OBJECT)
        resources.append({'kind': 'Resource', 'path': GTK_PREVIEW_OBJECT})
    preview_archive = 'studio-previewkit-0.1.0.buipkg'
    shutil.copy2(work / 'packages/previewkit-0.1.0.buipkg', work / 'resources' / preview_archive)
    resources.append({'kind': 'Resource', 'path': preview_archive})
    for name, source in DOCUMENTATION_RESOURCES.items():
        if not source.is_file():
            raise RuntimeError(f'Missing documentation resource {source}')
        shutil.copy2(source, work / 'resources' / name)
        if name == 'documentation-upstream.json':
            # Add Studio's own searchable help without mutating the upstream snapshot.
            from discover_documentation import render
            destination = work / 'resources' / name
            snapshot = json.loads(destination.read_text())
            path = '/studio/buoyscript/'
            pages = [page for page in snapshot['pages'] if page['path'] != path]
            html, text = render((ROOT / 'apps/BuoyStudio/docs/BUOYSCRIPT.md').read_text(), 'BUOYSCRIPT.md', {'BUOYSCRIPT.md': path}, {}, [])
            pages.append(dict(path=path, title='BuoyScript — Studio scripts, playground and automation', section='documentation', relative='studio/BUOYSCRIPT.md', source='studio:buoyscript-primer', html=html, text=text))
            snapshot['pages'] = pages
            index = {}
            for number, page in enumerate(pages):
                for token in set(re.findall(r'\w+', ' '.join([page['title'], page['text'], page.get('summary', ''), page.get('category', ''), page.get('package', ''), *page.get('keywords', []), *page.get('symbols', []), *[d['name'] for d in page.get('definitions', [])]]).lower())):
                    index.setdefault(token, []).append(number)
            snapshot['index'] = index
            snapshot.setdefault('counts', {})['documentation'] = sum(page['section'] == 'documentation' for page in pages)
            destination.write_text(json.dumps(snapshot, ensure_ascii=False))
        resources.append({'kind': 'Resource', 'path': name})
    for notice in ('SCOWL-LICENSE.txt', 'CSPELL-LICENSE.txt'):
        shutil.copy2(ROOT / 'apps/BuoyStudio/spelling' / notice, work / 'resources' / notice)
        resources.append({'kind': 'Resource', 'path': notice})
    studio_assets = ROOT / 'apps/BuoyStudio/resources'
    for platform, extension in (('Mac','icns'),('Windows','ico'),('Linux','png')):
        name = 'AppIcon.' + extension
        shutil.copy2(studio_assets / name, work / 'resources' / name)
        resources.append({'kind':'AppIcon','path':name,'platforms':[platform]})
    for glyph in sorted((studio_assets / 'glyphs').glob('*')):
        if glyph.suffix not in ('.png','.ico'): continue
        shutil.copy2(glyph, work / 'resources' / glyph.name)
        resources.append({'kind':'Resource','path':glyph.name})
    (work / 'resources' / 'app_resources.json').write_text(json.dumps({'schema': 1, 'resources': resources}, indent=2) + '\n')
    # Shared by Studio and Xippets; keep the source module identical in both apps.
    shared_snippets = ROOT / 'apps/shared/snippets/SnippetTemplates.bui'
    if shared_snippets.is_file():
        shutil.copy2(shared_snippets, work / shared_snippets.name)
    for source in (ROOT / 'apps/BuoyStudio').glob('*.bui'):
        shutil.copy2(source, work / source.name)
    for name in COMPONENTS:
        module = ROOT / 'containers/integrations' / name / f'{name}.bui'
        if not module.is_file():
            raise RuntimeError(f'Missing native component module {module}')
        shutil.copy2(module, work / module.name)

    for folder, modules in COMPONENT_MODULES.items():
        for name in modules:
            module = ROOT / 'containers/integrations' / folder / f'{name}.bui'
            if not module.is_file():
                raise RuntimeError(f'Missing native component module {module}')
            shutil.copy2(module, work / module.name)

    from development_permissions import stage_studio, uses_broker
    stage_studio(work, target, profile)
    write_build_info(ROOT, work, resolve_compiler(), sealer(), target=target,
                     credential_profile='development' if uses_broker(profile, target) else 'production')


def store_package(package):
    """Preserve sealed payloads while supporting Buoy 0.72's Windows ZIP reader."""
    staged = package.with_suffix('.stored')
    try:
        with zipfile.ZipFile(package) as original, zipfile.ZipFile(staged, 'w') as stored:
            for entry in original.infolist():
                stored.writestr(copy.copy(entry), original.read(entry.filename), compress_type=zipfile.ZIP_STORED)
        staged.replace(package)
    finally:
        staged.unlink(missing_ok=True)


def native_link_args(work, target=None):
    """Give direct GTK Declares the real host library without altering system files."""
    if not selected_target(target).endswith('-linux'):
        return []
    directory = Path(work) / 'native-link'
    directory.mkdir(exist_ok=True)
    compiler = shlex.split(os.environ.get('CC', 'cc'))
    found = subprocess.run(compiler + ['-print-file-name=libgtk-4.so.1'],
                           check=True, capture_output=True, text=True).stdout.strip()
    library = Path(found)
    if not library.is_file():
        raise RuntimeError('GTK 4 runtime library not found by the host C compiler: ' + found)
    alias = directory / 'libgtk-4.so'
    if not alias.exists():
        # This is an unversioned link to the SAME GTK 4 SONAME, not an ABI shim.
        alias.symlink_to(library.resolve())
    return ['-L', str(directory)]


def check(work):
    compiler = resolve_compiler()
    for source in sorted(work.glob('*.bui')):
        for target in TARGETS:
            checked = subprocess.run([compiler, str(source), '--check', '--diagnostic-format', 'json', '--target', target], cwd=work, capture_output=True, text=True)
            diagnostics = json.loads(checked.stdout)
            if checked.returncode or diagnostics.get('errors') != []:
                raise RuntimeError(f'{source.name} ({target}): {diagnostics}\n{checked.stderr}')
            if diagnostics.get('warnings'):
                print(diagnostics['warnings'])


def shell_terminal_link_args(work, target=None):
    if selected_target(target).endswith('-windows'):
        return []  # Resolved and deployed from the private studioshellterminal package.
    name = SHELL_TERMINAL_LIBRARY if selected_target(target).endswith('-darwin') else SHELL_TERMINAL_OBJECT
    return ['--extra-obj', str(work / name)]


def bundle_script_runner(output, target):
    """Deploy the matching optional SDK helper; settings enable it by default."""
    from build_studio_script_runner import TRIPLES
    if target != host_target():
        print('BuoyScript helper requires a native build for ' + target)
        return
    package = next((p for folder in installed_package_folders()
                    for p in sorted(folder.glob('buoyscript-*.buipkg'), reverse=True)), None)
    if package is None:
        print('BuoyScript SDK not installed; scripting preferences will report the missing helper.')
        return
    with zipfile.ZipFile(package) as archive:
        available = any(name.startswith('runtime/' + TRIPLES[target] + '/')
                        and not name.endswith('/') for name in archive.namelist())
    if not available:
        print('BuoyScript SDK has no native runtime for ' + target + '; scripting preferences will report the missing helper.')
        return
    output = Path(output)
    if target.endswith('-darwin'):
        bundle = output if output.suffix == '.app' else output.with_suffix('.app')
        directory = bundle / 'Contents/Helpers/BuoyScript'
    else:
        directory = output.parent / 'helpers/BuoyScript'
    runner = directory / ('studio-script-runner.exe' if target.endswith('-windows') else 'studio-script-runner')
    environment = dict(os.environ, BUOY=resolve_compiler())
    subprocess.run([sys.executable, str(ROOT / 'tools/build_studio_script_runner.py'),
                    '--package', str(package), '--output', str(runner)],
                   cwd=ROOT, env=environment, check=True)


def compile_staged(work, output, target=None, profile='development'):
    """Build an already staged native consumer; shared with the preview launcher."""
    target = selected_target(target)
    work, output = Path(work), Path(output)
    from development_permissions import uses_broker
    if not uses_broker(profile, target) and (work / 'DevelopmentKeychain.bui').exists():
        raise RuntimeError('Production/direct-storage Studio builds cannot contain the development credential client')
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [resolve_compiler(), 'main.bui', '--target', target, '--app-type', 'desktop', '-o', str(output)]
    if target.endswith('-darwin'):
        if uses_broker(profile, target):
            command += ['--bundle-id', 'com.buoy.studio.development']
    command += shell_terminal_link_args(work, target)
    command += native_link_args(work, target)
    if target.endswith('-linux'):
        command += ['--extra-obj', str(work / 'resources' / GTK_PREVIEW_OBJECT)]
    subprocess.run(command, cwd=work, check=True)
    bundle_script_runner(output, target)
    if target.endswith('-darwin'):
        bundle = output if output.suffix == '.app' else output.with_suffix('.app')
        frameworks = bundle / 'Contents/Frameworks'
        frameworks.mkdir(exist_ok=True)
        shutil.copy2(work / SHELL_TERMINAL_LIBRARY, frameworks)
        identity = os.environ.get('BUOY_CODESIGN_IDENTITY') or '-'
        subprocess.run(['codesign', '--force', '--deep', '--sign', identity, str(bundle)], check=True)
    return output


def build(output=None, check_only=False, seal_from_source=(), sealed_only=False, target=None, private_target_packages=False, profile='development'):
    target = selected_target(target)
    if output is None:
        output = ROOT / 'dist' / ('BuoyStudio.app' if target.endswith('-darwin') else 'BuoyStudio/BuoyStudio.exe' if target.endswith('-windows') else 'BuoyStudio/BuoyStudio')
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='buoy-studio-build-') as directory:
        work = Path(directory)
        stage(work, seal_from_source, sealed_only, target, private_target_packages, profile)
        check(work)
        if not check_only:
            compile_staged(work, output, target, profile)
    if not check_only:
        print(f'Built native Studio: {output}')
    return output


def run_self_test(output):
    binary = Path(output)
    if binary.suffix == '.app':
        with (binary / 'Contents/Info.plist').open('rb') as file:
            executable = plistlib.load(file)['CFBundleExecutable']
        binary = binary / 'Contents/MacOS' / executable
    command = [str(binary), '--self-test']
    if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
        command = ['xvfb-run', '-a'] + command
    tested = subprocess.run(command, capture_output=True, text=True, timeout=60)
    print(tested.stdout, end='')
    if tested.returncode or 'BUOY STUDIO SELF-TEST PASSED:' not in tested.stdout or 'SELF-TEST FAILED:' in tested.stdout:
        raise RuntimeError(f'Studio self-test failed ({tested.returncode})\n{tested.stdout}\n{tested.stderr}')
    if tested.stderr:
        print(tested.stderr, file=sys.stderr, end='')
    run_discovery_tests(command[:-1])
    run_documentation_tests(command[:-1])
    run_testing_tests(binary)


def run_testing_tests(binary):
    """Native Tests window against a scripted runner, a missing runner, and (when built
    with the testing extension) the real dist/buoy-dev; see tests/test_studio_testing.py."""
    env = dict(os.environ, BUOY_STUDIO_BINARY=str(binary))
    tested = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'), '-p', 'test_studio_testing.py', '-v'],
                            capture_output=True, text=True, timeout=900, env=env, cwd=ROOT)
    print(tested.stderr[-4000:], end='')
    if tested.returncode:
        raise RuntimeError(f'Studio Tests window checks failed ({tested.returncode})\n{tested.stdout}\n{tested.stderr}')


def discovery_run(command, expect, project, extra_env=None):
    env = dict(os.environ)
    env.pop('BUOY_STUDIO_DISCOVERY_HELPER', None)
    env.pop('BUOY_STUDIO_PYTHON', None)
    env.update(extra_env or {})
    tested = subprocess.run(command + ['--discovery-test', expect, '--project', str(project)], capture_output=True, text=True, timeout=90, env=env)
    print(tested.stdout, end='')
    if tested.returncode or 'BUOY STUDIO DISCOVERY TEST PASSED:' not in tested.stdout or 'TEST FAILED:' in tested.stdout:
        raise RuntimeError(f'Studio discovery test ({expect}) failed ({tested.returncode})\n{tested.stdout}\n{tested.stderr}')
    return tested.stdout


def run_discovery_tests(command):
    """Async discovery through the helper bundled with the app: fresh success, package
    removal, a failing interpreter over a stale index, and a missing trusted helper."""
    widgets = ROOT / 'packages/designerwidgets-0.1.0.buipkg'
    with tempfile.TemporaryDirectory(prefix='studio-discovery-') as directory:
        project = Path(directory) / 'project'
        (project / 'packages').mkdir(parents=True)
        shutil.copy2(widgets, project / 'packages' / widgets.name)
        found = discovery_run(command, 'ok', project)
        if 'Discovered 1 package controls in 1 packages' not in found:
            raise RuntimeError('Discovery did not load the DesignerWidgets control')
        (project / 'packages' / widgets.name).unlink()
        removed = discovery_run(command, 'ok', project)
        if 'Discovered 0 package controls' not in removed:
            raise RuntimeError('Removed package remained in the discovered catalog')
        failing = '/bin/false' if Path('/bin/false').is_file() else '/usr/bin/false'
        discovery_run(command, 'fail', project, {'BUOY_STUDIO_PYTHON': failing})
        discovery_run(command, 'fail', project, {'BUOY_STUDIO_DISCOVERY_HELPER': str(Path(directory) / 'missing.py')})


def documentation_package(path, name='docfixture', version='1.2.3'):
    """A minimal installed package carrying documentation as data only."""
    import zipfile
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'schema': 1, 'name': name, 'version': version}))
        archive.writestr('resources/buoy-docs/index.md', '---\ntitle: Fixture Guide\nsummary: "Native documentation smoke test"\nsymbols: ["Fixture.Render"]\n---\n# Fixture Guide\n\nUse `Fixture.Render` to draw.\n\n```buoy\nDim f As New Fixture()\nf.Render()\n```\n\n- [Details](guide/details.md)\n')
        archive.writestr('resources/buoy-docs/guide/details.md', '# Details\n\nZephyrine details with <script>alert(1)</script> escaped.\n')


def documentation_run(command, expect, project, extra_env=None, query=''):
    env = dict(os.environ)
    for key in ('BUOY_STUDIO_DOCUMENTATION_HELPER', 'BUOY_STUDIO_DOCUMENTATION_SNAPSHOT', 'BUOY_STUDIO_DOCUMENTATION_VIEWER', 'BUOY_STUDIO_PYTHON'):
        env.pop(key, None)
    env.update(extra_env or {})
    arguments = command + ['--documentation-test', expect, '--project', str(project)]
    if query:
        arguments += ['--documentation-query', query]
    tested = subprocess.run(arguments, capture_output=True, text=True, timeout=120, env=env)
    print(tested.stdout, end='')
    if tested.returncode or 'BUOY STUDIO DOCUMENTATION TEST PASSED:' not in tested.stdout or 'TEST FAILED:' in tested.stdout:
        raise RuntimeError(f'Studio documentation test ({expect}) failed ({tested.returncode})\n{tested.stdout}\n{tested.stderr}')
    return tested.stdout


def run_documentation_tests(command):
    """Native HTMLView bridge with bundled resources: the bundled snapshot loads, the
    bundled helper refreshes package docs asynchronously, a code block copies through the
    native clipboard, and a failing helper keeps the previous library."""
    with tempfile.TemporaryDirectory(prefix='studio-documentation-') as directory:
        # macOS temp roots may use /var aliases; package discovery deliberately
        # rejects symlink ancestors, so the fixture must use its canonical path.
        directory = str(Path(directory).resolve())
        project = Path(directory) / 'project'
        (project / 'packages').mkdir(parents=True)
        documentation_package(project / 'packages' / 'docfixture-1.2.3.buipkg')
        # An empty global package folder keeps the user's installed packages out of the test.
        empty = Path(directory) / 'global'
        empty.mkdir()
        isolated = {'XDG_DATA_HOME': str(empty), 'LOCALAPPDATA': str(empty)}
        documentation_run(command, 'docfixture', project, isolated, query='zephyrine')
        if not (project / '.buoy/documentation/index-v1.json').is_file():
            raise RuntimeError('Documentation refresh did not write the project index')
        failing = '/bin/false' if Path('/bin/false').is_file() else '/usr/bin/false'
        documentation_run(command, 'fail', project, dict(isolated, BUOY_STUDIO_PYTHON=failing))
        # An unreadable cache must fall back to bundled docs even when package
        # indexing cannot start. Exercise the real production cache path.
        (project / '.buoy/documentation/index-v1.json').write_text('{broken cache')
        documentation_run(command, 'fail', project, dict(isolated,
            BUOY_STUDIO_PYTHON=failing, BUOY_STUDIO_DOCUMENTATION_TEST_CACHE='1'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', choices=('development', 'production', 'debug', 'release'), default='development', help='macOS development/debug uses stable Development Services; production/release uses native credentials directly')
    parser.add_argument('--target', choices=TARGETS, help='Native host by default; x86_64-windows also supports cross-builds with MinGW')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--sealed-packages-only', action='store_true', help='Require declared prebuilt archives; do not reseal sources or discover optional packages')
    parser.add_argument('--private-target-packages', action='store_true', help='Rebuild ProcessKit, TerminalKit and ChatGPTAuth from copied source for this target only; never writes repository packages or runtimes (STUDIO_NATIVE_CC selects cross C compiler)')
    parser.add_argument('--self-test', action='store_true', help='Build, run native GUI actions, and require the success marker')
    parser.add_argument('--seal-from-source', action='append', choices=SEALABLE, default=[], help='Seal this library from libraries/ into the isolated build instead of using packages/ (repeatable; never writes packages/)')
    add_toolchain_arguments(parser)
    args = parser.parse_args()
    try:
        configure_toolchain(args.compiler, args.packager)
    except RuntimeError as error:
        parser.exit(1, f'{error}\n')
    if args.self_test and args.check_only:
        parser.error("--self-test cannot be combined with --check-only")
    if args.sealed_packages_only and args.seal_from_source:
        parser.error('--sealed-packages-only cannot be combined with --seal-from-source')
    if args.sealed_packages_only and args.private_target_packages:
        parser.error('--sealed-packages-only cannot be combined with --private-target-packages')
    if args.self_test and selected_target(args.target) != host_target():
        parser.error('--self-test requires a native host target; run the cross-built app on Windows')
    output = build(args.output, args.check_only, tuple(args.seal_from_source), args.sealed_packages_only, args.target, args.private_target_packages, args.profile)
    if args.self_test:
        run_self_test(output)


if __name__ == '__main__':
    main()
