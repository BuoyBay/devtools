#!/usr/bin/env python3
"""Build the private cancellable advanced-preview process bridge."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]

def build(work, target=None):
    host=('arm64' if platform.machine().lower() in ('arm64','aarch64') else 'x86_64')+'-'+('windows' if os.name=='nt' else 'darwin' if sys.platform=='darwin' else 'linux')
    target=target or host
    system=target.split('-')[-1]
    name='bbrender.dll' if system=='windows' else 'libbbrender.dylib' if system=='darwin' else 'libbbrender.so'
    folder=Path(work)/'nativebridge';folder.mkdir(exist_ok=True,parents=True)
    output=folder/name
    compiler=os.environ.get('CC','cc')
    if system=='windows':
        if os.name!='nt':compiler='aarch64-w64-mingw32-gcc' if target.startswith('arm64') else 'x86_64-w64-mingw32-gcc'
        flags=['-shared']
    elif system=='darwin':flags=['-dynamiclib','-pthread','-fPIC','-mmacosx-version-min=13.0','-install_name','@rpath/'+name]
    else:flags=['-shared','-pthread','-fPIC']
    if target!=host and system!='windows':
        if system=='darwin' and sys.platform=='darwin':flags+=['-arch','arm64' if target.startswith('arm64') else 'x86_64']
        else:raise RuntimeError('Build preview bridge on its native platform: '+target)
    subprocess.run([compiler,'-std=c11','-O2','-Wall','-Wextra','-Werror',*flags,str(Path(work)/'native/render_process.c'),'-o',str(output)],check=True)
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('work',type=Path);parser.add_argument('--target');args=parser.parse_args();print(build(args.work,args.target))
