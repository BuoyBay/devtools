#!/usr/bin/env python3
"""Check PreviewKit on six desktop targets and seal its reusable source package."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from buoy_executables import (resolve_compiler, resolve_packager,
                              add_toolchain_arguments, configure_toolchain)

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("arm64-darwin", "x86_64-darwin", "arm64-linux", "x86_64-linux", "arm64-windows", "x86_64-windows")


def packager(compiler):
    return resolve_packager(compiler=compiler)


def stage(destination, compiler=None):
    """Seal the current library into a consumer's isolated packages directory."""
    compiler = resolve_compiler(compiler)
    destination = Path(destination) / "packages"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "previewkit-0.1.0.buipkg"
    if archive.exists():
        raise ValueError("previewkit is supplied by the runner, not the job")
    subprocess.run([packager(compiler), "pkg", "seal", str(ROOT / "libraries/previewkit"), "-o", str(archive)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_toolchain_arguments(parser)
    args = parser.parse_args()
    try:
        configure_toolchain(args.compiler, args.packager)
    except RuntimeError as error:
        parser.exit(1, f'{error}\n')
    compiler = resolve_compiler()
    source = ROOT / "libraries/previewkit/modules/PreviewKit.bui"
    with tempfile.TemporaryDirectory(prefix="previewkit-build-") as temporary:
        work = Path(temporary)
        shutil.copy2(source, work / source.name)
        for target in TARGETS:
            subprocess.run([compiler, str(work / source.name), "--check", "--diagnostic-format", "json", "--target", target],
                           cwd=work, check=True, timeout=30)
        archive = stage(work, compiler)
        output = ROOT / "packages" / archive.name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, output)
        print(output)


if __name__ == "__main__":
    main()
