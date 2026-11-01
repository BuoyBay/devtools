#!/usr/bin/env python3
"""Build the native offscreen OpenGL Scene3DKit bridge; no GPU context is opened."""
import argparse
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'libraries/scene3dkit'
TRIPLES = {'x86_64-linux':'x86_64-unknown-linux-gnu','arm64-linux':'aarch64-unknown-linux-gnu','x86_64-darwin':'x86_64-apple-macos','arm64-darwin':'arm64-apple-macos','x86_64-windows':'x86_64-pc-windows-msvc','arm64-windows':'aarch64-pc-windows-msvc'}

def _host_target():
    arch = {'AMD64':'x86_64','amd64':'x86_64','aarch64':'arm64','ARM64':'arm64'}.get(platform.machine(), platform.machine())
    return arch + '-' + {'Linux':'linux','Darwin':'darwin','Windows':'windows'}[platform.system()]

def build(runtime_root=None,target=None,cc=None):
    target=target or _host_target()
    if target not in TRIPLES:raise ValueError('Unsupported GPU target: '+target)
    arch,system=target.split('-',1)
    runtime_root=Path(runtime_root) if runtime_root else PACKAGE/'runtime'
    name='buoy_scene3dkit.dll' if system=='windows' else 'libbuoy_scene3dkit.'+('dylib' if system=='darwin' else 'so')
    output=(runtime_root/TRIPLES[target]/name).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    selected=cc or os.environ.get('CC')
    if not selected:
        if system=='windows' and platform.system()!='Windows':selected='x86_64-w64-mingw32-gcc' if arch=='x86_64' else 'aarch64-w64-mingw32-gcc'
        elif system=='windows':selected=shutil.which('clang-cl') or shutil.which('cl') or 'gcc'
        else:selected='cc'
    compiler=shlex.split(selected,posix=os.name!='nt') if isinstance(selected,str) else list(selected)
    sources=[str(PACKAGE/'native'/name) for name in ('bridge.c','platform.c')]
    driver=Path(compiler[0]).name.lower().removesuffix('.exe')
    if system=='windows':
        link=(runtime_root.parent/'link'/TRIPLES[target]/'buoy_scene3dkit.lib').resolve();link.parent.mkdir(parents=True,exist_ok=True)
        if driver in ('cl','clang-cl'):
            flags=['/nologo','/std:c11','/O2','/W4','/D_CRT_SECURE_NO_WARNINGS','/LD']
            if driver=='clang-cl':flags+=['--target='+('aarch64' if arch=='arm64' else arch)+'-pc-windows-msvc']
            command=compiler+flags+sources+['/Fo'+str(output.parent)+os.sep,'/Fe'+str(output),'/link','opengl32.lib','gdi32.lib','user32.lib','/IMPLIB:'+str(link),'/MACHINE:'+('ARM64' if arch=='arm64' else 'X64')]
        else:command=compiler+['-std=c11','-O2','-Wall','-Wextra','-Werror','-static','-static-libgcc','-shared']+sources+['-lopengl32','-lgdi32','-luser32','-lm','-Wl,--out-implib,'+str(link),'-o',str(output)]
    elif system=='darwin':command=compiler+['-std=c11','-O2','-Wall','-Wextra','-Werror','-Wno-deprecated-declarations','-fPIC','-dynamiclib','-arch',arch,'-mmacosx-version-min=11.0','-Wl,-install_name,@rpath/'+name]+sources+['-framework','OpenGL','-o',str(output)]
    else:command=compiler+['-std=c11','-O2','-Wall','-Wextra','-Werror','-fPIC','-shared','-Wl,-soname,'+name]+sources+['-lEGL','-lGLESv2','-lm','-o',str(output)]
    subprocess.run(command,check=True,timeout=180)
    return target,output

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--runtime-root',type=Path);parser.add_argument('--target',choices=TRIPLES);parser.add_argument('--cc');args=parser.parse_args();print(build(args.runtime_root,args.target,args.cc)[1])
