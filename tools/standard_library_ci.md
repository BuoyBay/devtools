# Standard-library native CI

`.github/workflows/standard-library-native.yml` runs on relevant pull requests,
main-branch changes, manual dispatch, and Mondays at 07:23 UTC. It uses GitHub's
hosted `ubuntu-24.04`, `macos-15` (ARM64), and `windows-2025` (x64) runners.
It requires no private machine, SSH key, repository secret, or runner registration.
The workflow only reads repository contents and uploads test artifacts; it has no
publishing step. Hosted runner availability and billing remain repository settings.

The Linux job creates a fresh source snapshot and full fixtures from the vendored
Unicode 18 normalization data, pinned IANA timezone data, CLDR 49 Windows mappings,
strict date vectors, and Python gzip/zlib oracles. This same snapshot and the same
fixtures feed macOS. Windows gets executables cross-built from that snapshot on
Linux. Tests cover TestKit, FileSystemKit, TimeZoneKit, normalization, strict date
parsing, Windows-zone mappings, compression, and the six-package joint consumer.
Filesystem probes include Unicode names, symlinks and permission failures. macOS
also runs the existing ownership probe; Windows runs its calendar and reparse
probes. Unsupported creation of the custom Windows reparse tag is recorded by
the existing guest script, while its symlink/junction probes remain required.

An independent Linux `fuzz` job installs clang and runs
`python3 tools/fuzz_standard_library.py --smoke`. It compiler-checks the sealed
timestamp consumer on all six targets, executes Python-labeled timestamp cases
on Linux, and builds the pinned compression bridge with AddressSanitizer and
UndefinedBehaviorSanitizer. Deterministic scripts and capped libFuzzer runs cover
stream lifecycle and malformed input. Its logs and any saved failure inputs are
retained for 14 days, including failed jobs. See the
fuzzing guide for seeds, caps and scope.

## Toolchain and host prerequisites

`standard_library_ci_bootstrap.py` fetches official Buoy **0.72.0** release assets
and checks SHA256 against `containers/checksums.txt` before installation or
extraction. Linux installs the Debian package and `containers/bin/buoy`, whose
CRT, stubs and runtime overrides are required for both Linux and Windows linking.
`BPM_PACKAGER=/usr/bin/buoy` bypasses that wrapper for sealing. macOS expands the
official ARM64 installer using `pkgutil --expand-full`, copies its compiler and
bundled SDK together, and runs them without executing installer scripts or
altering an existing toolchain. It needs Apple command-line build tools already
present on the hosted runner. Updating Buoy requires updating the bootstrap pin
and reviewed checksums together.

Python **3.12** is selected explicitly on every runner. The existing compression
builder needs Python's `tarfile` extraction filter; macOS's system Python 3.9 is
insufficient. Linux installs build tools, MinGW x64 and timezone tooling, and
builds fresh Linux and Windows bridge slices from the checksum-verified vendored
zlib archive. macOS builds its ARM64 bridge locally. Builds happen in isolated
temporary workspaces, preserving checked-out sources and existing package archives.

Native Windows compilation is deliberately bypassed because of the documented
Buoy 0.72.0 compiler limitations. The Windows runner requires its normal VC++
runtime and an elevated account capable of symlink/ACL fixture creation, as
provided by the hosted Windows image. No compiler installation, stack patch,
registry policy change or private Windows VM is part of CI. The existing guest
script runs in its own PowerShell process and restores its temporary ACLs.

The Windows packet contains no `.buoy` caches, source files or sealed archives.
Its complete file set and per-file hashes are checked before execution; extra
files, duplicate paths and paths outside the extracted packet are rejected.
The Linux job publishes the bundle SHA-256 as a job output, separately from the
artifact, and Windows verifies it before extraction. A failed suite leaves
`complete` false. Five bounded packet-rejection cases run on Windows as well.
On macOS, package
dylibs are copied beside consumers before caches are removed; only the spawned
test process receives `DYLD_LIBRARY_PATH` for that folder. The installed SDK
continues to supply Buoy's standard runtime dylibs. Python independently decodes
compression output on all three hosts and rejects truncation, trailing data,
wrong bytes and missing rows.

## Evidence and local reproduction

`evidence/<target>/` contains build diagnostics, compiler version, package input
hashes, packet hashes, native transcripts and result JSON. `snapshot.json` records
the staged source and fixture hashes. Setup logs, build/native logs and reports
are retained for **14 days**, including failed jobs; transport inputs/packets are
retained for **7 days**. A job forcibly cancelled or killed by runner loss may
have only partial artifacts. The Windows script's `report.json` is complemented
by `verified-result.json` after independent Python decoding.

Before building fresh native slices, Linux also runs `verify-packages`, sealing
all six libraries in temporary directories and comparing every archive entry
against `packages/`. Compression runtime receipts and all six native payloads
must match current source inputs. An existing local `dist/` mirror must match
its package. ZIP metadata and compression differences alone do not fail archive
entry comparison. This catches stale committed packages independently of the
fresh-source native jobs.

A Linux test failure still permits the Windows packet build and downstream native
jobs when their prerequisite artifacts exist. An earlier setup failure can leave
those artifacts unavailable; downstream download steps then fail explicitly.

From the repository root, using the installed Linux wrapper and sealer:

```sh
python3 /tmp/buoy-native-ci/tools/build_compressionkit.py --target x86_64-linux --native-only
python3 /tmp/buoy-native-ci/tools/build_compressionkit.py --target x86_64-windows --cc x86_64-w64-mingw32-gcc --native-only
```

Use a new workspace path for every run. Transfer `bundle.zip` and `run.ps1` from
`evidence/x86_64-windows/` to a fresh Windows directory, execute
`powershell.exe -NoProfile -NonInteractive -File run.ps1 -Bundle <absolute-bundle-path> -ExpectedSha256 <trusted-bundle-sha256>`,
then run `python tools/standard_library_ci.py verify-windows --folder <directory>`
on a machine with the resulting `report.json` and `stage/compressionkit/` files.
The workflow shows the corresponding macOS bootstrap/build commands. Generate
fixtures on Linux first: the fixture generator uses the Linux `zic` command-line
options, and macOS consumes those generated vectors rather than regenerating them.

This workflow covers native Linux x64, macOS ARM64 and Windows x64 execution.
Other CPU/platform slices, Intel macOS execution, GUI tests and actual hosted-job
execution are separate validation. Local SSH/VM validation does not establish that
GitHub-hosted jobs passed. No hosted jobs were dispatched during implementation.

Test suites and fixtures are maintainer-local and are not part of this public repository.
