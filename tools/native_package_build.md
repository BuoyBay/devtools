# Native package builds

`build_packages.bui` checks and seals local Buoy packages without Python or shell
execution. It uses ProcessKit argument arrays to invoke the installed compiler and
package sealer. It supports the assembled `libraries/<name>` layout and the
collection's standard `repos/<name>/manifest.json` source layout. A standalone
package repository can be passed directly as `--root`; adjacent source repositories
supply its local dependencies. Root layouts take precedence over legacy nested paths.

Compile the builder with the ProcessKit archive available to the compiler.
On the recorded Linux install, first set `export BPM_PACKAGER=/usr/bin/buoy` to
bypass its compile-only wrapper. Other installations can use `buoy pkg` directly:

```sh
mkdir -p local/native-package-build/bootstrap/packages dist/native-package-builder
cp repos/devtools/tools/build_packages.bui local/native-package-build/bootstrap/main.bui
"${BPM_PACKAGER:-buoy}" pkg seal repos/processkit -o local/native-package-build/bootstrap/packages/processkit-0.1.0.buipkg
buoy local/native-package-build/bootstrap/main.bui -o dist/native-package-builder/build-packages
```

Keep compiler-delivered native runtime libraries beside `dist/native-package-builder/build-packages`.
The ProcessKit archive is the bootstrap dependency; its source must already contain
a rebuilt, tested runtime for the host. The private seal above bootstraps this tool;
it is not a multi-platform release. This builder does not rebuild ProcessKit's native
host bridge. On this Linux installation, set `BPM_PACKAGER=/usr/bin/buoy` for the
package operation to bypass the compile-only wrapper. Paths in the following examples must be absolute:

```sh
./dist/native-package-builder/build-packages --root "$PWD" --compile-consumer algorithmkit mathkit formatkit
./dist/native-package-builder/build-packages --root "$PWD" --check-only --continue-on-error algorithmkit processkit
```

For each selected source package, the builder:

1. Rebuilds declared local dependencies first, detecting cycles and package-name
   mismatches. Dependency acquisition from remote coordinates remains BPM's job.
2. Copies the package's literal `bpm.json` file allowlist, including license,
   resources and notices, into an isolated local stage. It rejects symbolic links
   and unsafe relative paths. Without an allowlist it selects conventional package
   directories and license/provenance files. Legal texts are also copied under
   `resources/buoybay-licenses/` so Buoy's conventional-directory sealer retains
   them inside the compiler archive.
3. Seals that exact stage privately so self-package imports resolve to the source
   under test, then compiler-checks every standalone module entrypoint across all
   six desktop targets. Imported partial-class fragments are checked through their
   importing entrypoint, matching the existing layout builder's merge semantics. Compiler exit status and JSON diagnostics must both be clean.
4. With `--compile-consumer`, compiles a host executable importing the resulting
   package. Packages without a self-named entry module use qualified module imports.
   This checks code generation/linking; the generated consumer does not
   exercise the package API or run automatically. Package behavior suites remain
   necessary before publication.
5. Unless `--check-only` was selected, atomically replaces the completed archive in
   `dist/packages/`, or the absolute directory passed with `--output-dir`.

The isolated stage and JSON report remain in
`local/native-package-build/run-<process-id>/`. Each report records source package,
version, entrypoint count, target count, consumer-build state and failures.
`--continue-on-error` proceeds through requested packages, writes all encountered
failures, and still exits nonzero. A failed dependency prevents its dependents from
being published. Already-completed independent packages remain published.

`--compiler EXECUTABLE` / `BUOY` and `--packager EXECUTABLE` / `BPM_PACKAGER`
select executable paths or names on absolute `PATH` entries. Explicit executable
paths must be absolute. On the recorded Linux installation, the default package
sealer resolves to `/usr/bin/buoy` to avoid the compile-only `/usr/local/bin/buoy`
wrapper. An explicit `BPM_PACKAGER` or `--packager` takes precedence.

The runtime currently supports POSIX hosts and checks Windows target syntax and
semantics. Non-system native libraries, native link binaries, explicit toolchain
requirements and partial-class layouts without a resolvable standalone import
entrypoint need their dedicated builders.
This driver rejects those cases or reports their compiler errors; it does not
certify native slices, replace signing/release packaging, or execute API tests.
Only explicitly imported installed toolchain archives are staged, along with the
installed Xojo shim's documented regex/crypto/zip/sqlite3/postgres/xml closure. A known local
source-package import missing from dependency metadata is an error.
