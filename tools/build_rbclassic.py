#!/usr/bin/env python3
"""Check and seal RBClassic, optionally running its isolated behavior suite."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from build_previewkit import TARGETS, packager

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "libraries/rbclassic"


def checked_run(command, *, cwd, timeout=120):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b""
        errors = error.stderr or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        if isinstance(errors, bytes):
            errors = errors.decode("utf-8", "replace")
        raise RuntimeError(f"Timed out after {timeout}s: {command!r}\n{output}\n{errors}") from error
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, command))}\n{result.stdout}\n{result.stderr}")
    return result


def check(compiler, source, work, target=None):
    command = [compiler, str(source), "--check", "--diagnostic-format", "json"]
    if target:
        command += ["--target", target]
    result = checked_run(command, cwd=work)
    diagnostics = json.loads(result.stdout)
    if diagnostics.get("errors"):
        raise RuntimeError(json.dumps(diagnostics, indent=2))
    # Deduplicate dependency warnings in the final report.
    return [json.dumps(warning, sort_keys=True) for warning in diagnostics.get("warnings", [])]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=2, help="parallel independent target checks (1-6)")
    parser.add_argument("--test", action="store_true", help="compile and execute behavior tests")
    parser.add_argument("--host-only", action="store_true", help="quick host check instead of six-target verification")
    parser.add_argument("--no-publish", action="store_true", help="verify without replacing packages/rbclassic archive")
    parser.add_argument("--report", type=Path, help="write successful verification counts, scope and source hashes as JSON")
    parser.add_argument("--processkit-slice-receipt", action="append", type=Path, default=[],
                        help="overlay a verified ProcessKit slice into temporary staging; repeat per target")
    parser.add_argument("--gui-image", default=os.environ.get("BUOY_GUI_IMAGE", "buoy:0.72.0-gui"),
                        help="Linux Docker image for GUI/image tests; use an empty string to run them on the host")
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.jobs > 6:
        parser.error("--jobs must be between 1 and 6")
    compiler = os.environ.get("BUOY", "buoy")
    manifest = json.loads((LIBRARY / "manifest.json").read_text())
    archive_name = f"{manifest['name']}-{manifest['version']}.buipkg"
    warnings = set()
    checks = 0
    executions = 0
    with tempfile.TemporaryDirectory(prefix="rbclassic-verify-") as temporary:
        work = Path(temporary)
        packages = work / "packages"
        packages.mkdir()
        # Always use the installed foundation, never an edited extracted cache.
        package_roots = [Path(os.environ.get("BUOY_PACKAGES_DIR", "/usr/share/buoy/packages")),
                         Path("/Applications/Buoy/packages")]
        installed = next((p for p in package_roots if list(p.glob("Xojo-*.buipkg"))), None)
        if installed is None:
            raise RuntimeError("Bundled Xojo package missing; set BUOY_PACKAGES_DIR")
        for dependency in installed.glob("*.buipkg"):
            shutil.copy2(dependency, packages / dependency.name)
        if args.test and (ROOT / "tests/classic_interop_test.bui").is_file():
            # The interop consumer uses maintained exact-key adapters. Seal
            # them privately instead of invoking their publishing verifier.
            for name in ("collectionskit", "variantkit"):
                dependency = ROOT / "libraries" / name
                dependency_manifest = json.loads((dependency / "manifest.json").read_text())
                filename = dependency_manifest["name"] + "-" + dependency_manifest["version"] + ".buipkg"
                checked_run([packager(compiler), "pkg", "seal", str(dependency),
                             "-o", str(packages / filename)], cwd=work)
        # Seal maintained ProcessKit source and its existing native payloads.
        # Rebuilding foreign native payloads needs the matching platform SDK.
        processkit = ROOT / "libraries/processkit"
        from build_support import native_source_digest, verify_native_receipt, TARGETS as NATIVE_TARGETS
        native_digest = native_source_digest(processkit)
        process_source = work / "processkit-source"
        process_source.mkdir()
        shutil.copy2(processkit / "manifest.json", process_source / "manifest.json")
        shutil.copytree(processkit / "modules", process_source / "modules")
        native_slices = 0
        for runtime in (processkit / "runtime").glob("*/*"):
            if runtime.suffix not in {".so", ".dll", ".dylib"}:
                continue
            receipt = runtime.with_name(runtime.name + ".source-sha256")
            if (not receipt.is_file() or receipt.read_text().strip() != native_digest
                    or not verify_native_receipt(runtime, next((target for target, triple in NATIVE_TARGETS.items() if triple == runtime.parent.name), ""), ROOT / "tools/build_processkit.py", native_digest)):
                print(f"excluded stale ProcessKit slice {runtime.parent.name}", flush=True)
                continue
            destination = process_source / "runtime" / runtime.parent.name
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(runtime, destination / runtime.name)
            if runtime.suffix == ".dll":
                link = processkit / "link" / runtime.parent.name / "buoy_processkit.lib"
                if not link.is_file():
                    raise RuntimeError(f"Missing ProcessKit import library: {link}")
                destination = process_source / "link" / runtime.parent.name
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(link, destination / link.name)
            native_slices += 1
        overlay_targets = set()
        for receipt_path in args.processkit_slice_receipt:
            from build_classic_processkit_slices import verify_receipt
            receipt_path = receipt_path.resolve()
            record = verify_receipt(receipt_path)
            if record["target"] in overlay_targets:
                raise RuntimeError(f"Duplicate ProcessKit slice: {record['target']}")
            overlay_targets.add(record["target"])
            for relative in record["artifacts"]:
                if Path(relative).parts[0] not in {"runtime", "link"}:
                    continue
                destination = process_source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(receipt_path.parent / relative, destination)
            native_slices += 1
            print(f"staged verified ProcessKit slice {record['target']}", flush=True)
        if not native_slices:
            raise RuntimeError("No current ProcessKit runtime; rebuild with tools/build_processkit.py")
        checked_run([packager(compiler), "pkg", "seal", str(process_source),
                     "-o", str(packages / "processkit-0.1.0.buipkg")], cwd=work)
        archive = packages / archive_name
        classic_source = work / "rbclassic-source"
        shutil.copytree(LIBRARY, classic_source)
        inputs = sorted(path for path in classic_source.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.name != "validation.json")
        source_hashes = {str(path.relative_to(classic_source)): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in inputs}
        # Build only this host's networking slice; semantic checks on other
        # targets do not establish a foreign native payload or execution.
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            import platform
            architecture = platform.machine().lower()
            cpu = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(architecture)
            if cpu is None:
                raise RuntimeError(f"Unsupported networking build architecture: {architecture}")
            darwin = sys.platform == "darwin"
            triple = (("arm64" if cpu == "aarch64" else cpu) + "-apple-macos"
                      if darwin else cpu + "-unknown-linux-gnu")
            native_name = "libClassicAsyncNet." + ("dylib" if darwin else "so")
            native_output = work / native_name
            command = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fPIC",
                       "-dynamiclib" if darwin else "-shared", "-pthread",
                       str(classic_source / "native/networking/classic_asyncnet.c"), "-o", str(native_output)]
            if darwin:
                command += ["-Wl,-install_name,@rpath/" + native_name]
            checked_run(command, cwd=work)
            native_destination = classic_source / "runtime" / triple
            native_destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(native_output, native_destination / native_name)
        if args.test and (sys.platform.startswith("linux") or sys.platform == "darwin"):
            checked_run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fPIC",
                         "-dynamiclib" if sys.platform == "darwin" else "-shared",
                         str(ROOT / "tests/classic_asyncnet_test_helper.c"),
                         "-o", str(work / ("libClassicAsyncNetTest.dylib" if sys.platform == "darwin" else "libClassicAsyncNetTest.so"))], cwd=work)
        checked_run([packager(compiler), "pkg", "seal", str(classic_source), "-o", str(archive)], cwd=work)
        sources = []
        for source in sorted((classic_source / "modules").glob("*.bui")):
            destination = work / source.name
            shutil.copy2(source, destination)
            sources.append(destination)
        tests = []
        locale_directory = work / "locales"
        if args.test:
            gtk_guard = None
            if sys.platform.startswith("linux"):
                from build_gtklifecycle import build_object
                gtk_guard = build_object()
            if sys.platform.startswith("linux"):
                locale_directory.mkdir()
                for identifier in ("en_US", "de_DE", "fr_FR"):
                    checked_run(["localedef", "--no-archive", "-i", identifier, "-f", "UTF-8",
                                 str(locale_directory / (identifier + ".UTF-8"))], cwd=work)
            shutil.copy2(ROOT / "tests/rbclassic_dialog_verify.py", work / "rbclassic_dialog_verify.py")
            shutil.copy2(ROOT / "tests/classic_mouse_verify.py", work / "classic_mouse_verify.py")
            shutil.copy2(ROOT / "tests/classic_desktop_verify.py", work / "classic_desktop_verify.py")
            shutil.copy2(ROOT / "tests/classic_asyncnet_verify.py", work / "classic_asyncnet_verify.py")
            shutil.copy2(ROOT / "tests/classic_clipboard_verify.py", work / "classic_clipboard_verify.py")
            shutil.copy2(ROOT / "tests/classic_controlsets_verify.py", work / "classic_controlsets_verify.py")
            shutil.copy2(ROOT / "tests/classic_list_native_verify.py", work / "classic_list_native_verify.py")
            for source in sorted([*(ROOT / "tests").glob("classic_*_test.bui"), *(ROOT / "tests").glob("rbclassic_*_test.bui")]):
                # Historical ports have their own fixture module, input files
                # and independent byte oracle; their verifier stages those.
                if source.name in {"classic_historical_test.bui", "classic_printing_test.bui"}:
                    continue
                destination = work / source.name
                shutil.copy2(source, destination)
                destination.write_text(destination.read_text().replace("Import classicxojo", "Import rbclassic"))
                if source.name == "classic_interop_test.bui":
                    destination.write_text(destination.read_text().replace("Import classicbridge\n", "Import rbclassic\n"))
                tests.append(destination)
        # The entry-point consumer must resolve the archive, not the loose
        # RBClassic.bui copied above for per-module checks.
        package_tests = [test for test in tests if test.name.endswith("_package_test.bui") or test.name == "classic_interop_test.bui"]
        if package_tests:
            consumer = work / "sealed-consumer"
            consumer.mkdir()
            shutil.copytree(packages, consumer / "packages")
            for package_test in package_tests:
                destination = consumer / package_test.name
                shutil.copy2(package_test, destination)
                tests[tests.index(package_test)] = destination
        targets = [None] if args.host_only else TARGETS
        for source in sources + tests:
            check_work = source.parent if source.parent.name == "sealed-consumer" else work
            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                for result_warnings in executor.map(lambda target: check(compiler, source, check_work, target), targets):
                    warnings.update(result_warnings)
                    checks += 1
            print(f"checked {source.name}", flush=True)
        if args.test:
            for source in tests:
                run_work = source.parent if source.parent.name == "sealed-consumer" else work
                executable = run_work / source.stem
                command = [compiler, str(source), "-o", str(executable)]
                if gtk_guard is not None and source.name in {"classic_controls_test.bui", "classic_list_native_test.bui"}:
                    command.append("--extra-obj=" + str(gtk_guard))
                if sys.platform.startswith("linux") or sys.platform == "darwin":
                    command.append("-L" + str(work))
                # The Linux timer test calls GLib directly to exercise the real
                # event loop. Its system library is outside Buoy's stub paths.
                if sys.platform.startswith("linux"):
                    import sysconfig
                    multiarch = sysconfig.get_config_var("MULTIARCH")
                    if multiarch:
                        command.append(f"-L/usr/lib/{multiarch}")
                checked_run(command, cwd=run_work)
                command = [str(executable)]
                if sys.platform.startswith("linux") and source.name in {"classic_data_test.bui", "classic_formatting_test.bui"}:
                    command = ["env", "LOCPATH=" + str(locale_directory), "LC_ALL=en_US.UTF-8", "TZ=UTC", *command]
                if sys.platform.startswith("linux") and source.name == "classic_desktop_test.bui":
                    command = ["python3", str(work / "classic_desktop_verify.py"), str(executable)]
                if source.name == "classic_asyncnet_test.bui":
                    if not sys.platform.startswith("linux"):
                        raise RuntimeError("Networking fixture verification currently requires Linux")
                    command = ["python3", str(work / "classic_asyncnet_verify.py"), "--exercise", str(executable)]
                if source.name == "classic_clipboard_test.bui":
                    if not sys.platform.startswith("linux"):
                        raise RuntimeError("Clipboard interchange verification currently requires Linux")
                    command = ["python3", str(work / "classic_clipboard_verify.py"), str(executable)]
                if sys.platform.startswith("linux") and source.name == "classic_controlsets_test.bui":
                    command = ["python3", str(work / "classic_controlsets_verify.py"), str(executable)]
                if sys.platform.startswith("linux") and source.name == "classic_list_native_test.bui":
                    command = ["python3", str(work / "classic_list_native_verify.py"), str(executable)]
                if sys.platform.startswith("linux") and source.name in {"classic_controls_test.bui", "classic_list_native_test.bui"}:
                    command = ["env", "GTK_A11Y=none", *command]
                container_name = None
                gui_test = source.name in {"classic_graphics_test.bui", "classic_controls_test.bui", "classic_files_test.bui",
                                           "classic_desktop_test.bui", "classic_package_test.bui", "classic_asyncnet_test.bui",
                                           "classic_clipboard_test.bui", "classic_data_test.bui", "classic_formatting_test.bui",
                                           "classic_controlsets_test.bui", "classic_list_native_test.bui", "rbclassic_aliases_package_test.bui"}
                if sys.platform.startswith("linux"):
                    if gui_test and args.gui_image:
                        if not shutil.which("docker"):
                            raise RuntimeError("GUI tests require Docker or --gui-image '' with host GUI libraries installed")
                        container_name = "rbclassic-test-" + uuid.uuid4().hex
                        command = ["docker", "run", "--rm", "--name", container_name, "--network", "none", "-v", f"{work}:{work}",
                                   "-w", str(run_work), "--entrypoint", "dbus-run-session", args.gui_image,
                                   "--", "xvfb-run", "-a", *command]
                        # Current compiler releases may require runtime libraries
                        # newer than those bundled into an existing GUI image.
                        runtime_cpu = "aarch64" if os.uname().machine in {"aarch64", "arm64"} else "x86_64"
                        host_runtime = Path(os.environ.get("BUOY_RUNTIME_DIR", "/usr/lib/buoy/runtime/" + runtime_cpu + "-unknown-linux-gnu"))
                        if host_runtime.is_dir():
                            command[2:2] = ["-v", str(host_runtime) + ":/classic-runtime:ro", "-e", "LD_LIBRARY_PATH=/classic-runtime"]
                        command[2:2] = ["--tmpfs", f"/tmp/runtime-buoy:rw,mode=700,uid={os.getuid()},gid={os.getgid()}"]
                    elif shutil.which("xvfb-run"):
                        command = ["xvfb-run", "-a", *command]
                try:
                    result = checked_run(command, cwd=run_work, timeout=60)
                    print(result.stdout.strip() or f"passed {source.name}", flush=True)
                    executions += 1
                    if sys.platform.startswith("linux") and source.name == "rbclassic_aliases_package_test.bui":
                        dialog_command = command[:-1] + ["python3", str(work / "rbclassic_dialog_verify.py"), str(executable)]
                        dialog = checked_run(dialog_command, cwd=run_work, timeout=45)
                        print(dialog.stdout.strip(), flush=True)
                        executions += 1
                    if sys.platform.startswith("linux") and source.name == "classic_graphics_test.bui":
                        mouse_command = command[:-1] + ["python3", str(work / "classic_mouse_verify.py"), str(executable)]
                        mouse_result = checked_run(mouse_command, cwd=run_work, timeout=30)
                        print(mouse_result.stdout.strip(), flush=True)
                        executions += 1
                finally:
                    if container_name is not None:
                        subprocess.run(["docker", "rm", "--force", container_name],
                                       capture_output=True, timeout=15)
            checked_run([sys.executable, "-B", str(ROOT / "tests/rbclassic_mapping_test.py")], cwd=ROOT)
            checked_run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
                         "-p", "test_classic_lint.py", "-q"], cwd=ROOT)
            historical_report = work / "historical-results.json"
            historical = checked_run([sys.executable, str(ROOT / "tests/classic_historical_verify.py"),
                                      "--report", str(historical_report)], cwd=ROOT, timeout=300)
            evidence = json.loads(historical_report.read_text())
            if any(item.get("exit_code") != 0 or item.get("errors") for item in evidence["checks"]):
                raise RuntimeError("Historical fixture compiler evidence is not clean")
            if any(item.get("exit_code") != 0 for item in evidence["behavior"]):
                raise RuntimeError("Historical fixture behavior evidence is not clean")
            checks += len(evidence["checks"])
            executions += len(evidence["behavior"])
            print(historical.stdout.strip(), flush=True)
            if sys.platform.startswith("linux"):
                # A plain execution covers configuration only; the independent
                # PTY driver supplies actual serial byte-flow evidence.
                serial_command = [sys.executable, str(ROOT / "tests/classic_serial_verify.py"),
                                  "--compiler", compiler]
                if args.host_only:
                    serial_command.append("--host-only")
                serial = checked_run(serial_command, cwd=ROOT, timeout=420)
                print(serial.stdout.strip(), flush=True)
                checks += sum(line.startswith("PASS check ") for line in serial.stdout.splitlines())
                executions += 17
                printing_report = work / "printing-results.json"
                printing = checked_run([sys.executable, str(ROOT / "tests/classic_printing_verify.py"),
                                        "--report", str(printing_report)], cwd=ROOT, timeout=720)
                evidence = json.loads(printing_report.read_text())
                if not evidence["modes"] or any(not item.get("passed") for item in evidence["modes"].values()):
                    raise RuntimeError("Printing behavior evidence is not clean")
                if any(item.get("errors") != 0 for item in evidence["checks"]):
                    raise RuntimeError("Printing compiler evidence is not clean")
                checks += len(evidence["checks"])
                executions += len(evidence["modes"])
                print(printing.stdout.strip(), flush=True)
        source_snapshot_current = all((LIBRARY / relative).is_file() and
                                      hashlib.sha256((LIBRARY / relative).read_bytes()).hexdigest() == digest
                                      for relative, digest in source_hashes.items())
        current_inputs = sorted(path for path in LIBRARY.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.name != "validation.json")
        source_snapshot_current = source_snapshot_current and set(source_hashes) == {
            str(path.relative_to(LIBRARY)) for path in current_inputs}
        if not args.no_publish:
            if not source_snapshot_current:
                raise RuntimeError("RBClassic sources changed during verification; refusing to publish an older snapshot")
            output = ROOT / "packages" / archive_name
            output.parent.mkdir(exist_ok=True)
            shutil.copy2(archive, output)
            print(output)
        report = {"schema": 2, "verifiedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "compiler": checked_run([compiler, "--version"], cwd=work).stdout.strip(),
                  "targets": targets, "compilerChecks": checks, "compilerErrors": 0,
                  "hostBehaviorExecutions": executions, "hostPlatform": sys.platform,
                  "behaviorTestsRequested": args.test, "foreignNativeExecution": False,
                  "sourceSnapshotCurrent": source_snapshot_current, "sourceHashes": source_hashes,
                  "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "published": not args.no_publish, "warnings": sorted(warnings),
                  "processkitNativeSourceSha256": native_digest,
                  "processkitSliceReceipts": [str(path.resolve()) for path in args.processkit_slice_receipt]}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n")
    for warning in sorted(warnings):
        print(f"warning: {warning}")
    print(f"RBClassic: {checks} compiler checks, {executions} host behavior executions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
