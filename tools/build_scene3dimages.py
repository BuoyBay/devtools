#!/usr/bin/env python3
"""Build the bounded, memory-only PNG/JPEG Scene3DKit decoder slice."""
from pathlib import Path
import argparse
import os
import platform
import shlex
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'libraries/scene3dkit'
TRIPLES={'x86_64-linux':'x86_64-unknown-linux-gnu','arm64-linux':'aarch64-unknown-linux-gnu','x86_64-darwin':'x86_64-apple-macos','arm64-darwin':'arm64-apple-macos','x86_64-windows':'x86_64-pc-windows-msvc','arm64-windows':'aarch64-pc-windows-msvc'}


def _host_target():
    machine=os.environ.get('PROCESSOR_ARCHITEW6432',platform.machine()).lower()
    arch={'aarch64':'arm64','amd64':'x86_64','x64':'x86_64'}.get(machine,machine)
    return arch+'-'+{'Linux':'linux','Darwin':'darwin','Windows':'windows'}[platform.system()]


def _command(value):
    if not isinstance(value,str):return list(value)
    return [part[1:-1] if len(part)>1 and part[0]==part[-1]=='"' else part for part in shlex.split(value,posix=os.name!='nt')]


def build(runtime_root=None,target=None,cc=None):
    """Return (short target, shared library path); explicit target/cc supports cross slices."""
    target=target or _host_target()
    if target not in TRIPLES:raise ValueError('Unsupported Scene3D images target: '+target)
    arch,system=target.split('-',1);triple=TRIPLES[target]
    name='buoy_scene3dimages.dll' if system=='windows' else 'libbuoy_scene3dimages.'+('dylib' if system=='darwin' else 'so')
    runtime_root=Path(runtime_root) if runtime_root is not None else PACKAGE/'runtime'
    output=(runtime_root/triple/name).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    selected=cc or os.environ.get('CC')
    if not selected:
        if system=='windows' and platform.system()=='Windows':selected=shutil.which('clang-cl') or shutil.which('cl') or shutil.which('gcc')
        elif system=='windows':selected={'x86_64':'x86_64-w64-mingw32-gcc','arm64':'aarch64-w64-mingw32-gcc'}[arch]
        else:selected='cc'
    if not selected:raise RuntimeError('A native C compiler is required; pass --cc or set CC')
    compiler=_command(selected);driver=Path(compiler[0]).name.lower().removesuffix('.exe');source=str(PACKAGE/'native/image_decode.c')
    if system=='windows':
        link=(runtime_root.parent/'link'/triple/'buoy_scene3dimages.lib').resolve();link.parent.mkdir(parents=True,exist_ok=True)
        if driver in ('cl','clang-cl'):
            flags=['/nologo','/std:c11','/O2','/D_CRT_SECURE_NO_WARNINGS','/LD']
            if driver=='clang-cl':flags+=['--target='+('aarch64' if arch=='arm64' else arch)+'-pc-windows-msvc']
            command=compiler+flags+[source,'/Fo'+str(output.parent/'image_decode.obj'),'/Fe'+str(output),'/link','/IMPLIB:'+str(link),'/MACHINE:'+('ARM64' if arch=='arm64' else 'X64')]
        else:command=compiler+['-std=c11','-O2','-Wall','-Wextra','-Werror','-shared','-static','-static-libgcc',source,'-Wl,--out-implib,'+str(link),'-o',str(output),'-lm']
    else:
        flags=['-std=c11','-O2','-Wall','-Wextra','-Werror','-fPIC','-fvisibility=hidden']
        if system=='darwin':flags+=['-dynamiclib','-arch','arm64' if arch=='arm64' else 'x86_64','-mmacosx-version-min=11.0','-Wl,-install_name,@rpath/'+name];libs=[]
        else:flags+=['-shared','-Wl,-soname,'+name];libs=['-lm']
        command=compiler+flags+[source]+libs+['-o',str(output)]
    subprocess.run(command,check=True,timeout=180)
    return target,output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--runtime-root',type=Path);parser.add_argument('--target',choices=TRIPLES);parser.add_argument('--cc');args=parser.parse_args()
    target,output=build(args.runtime_root,args.target,args.cc);print(target,output)
