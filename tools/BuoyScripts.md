# Buoy workspace scripts

Run the native Buoy ports through [Ahoy](https://github.com/BuoyBay/buoy-run/blob/main/src/README.md). The retained Python
scripts are compatibility implementations and parity oracles. These ports run on
POSIX hosts; a successful Windows compiler check does not establish Windows
runtime support.

From the repository root:

```sh
buoy tools/ahoy/main.bui --check --diagnostic-format json
buoy tools/ahoy/main.bui -o dist/ahoy
export PATH="$PWD/dist:$PATH"
buoy-run tools/platform_audit.bui --help
buoy-run tools/run_workspace_audit.bui --list
buoy-run tools/check_package_sources.bui --json
buoy-run tools/upstream_issues.bui validate
buoy-run tools/run_example.bui examples/randomkit/main.bui --run
```

Use each script's `--help` for its arguments. Ahoy preserves the calling working
directory and passes literal arguments and standard streams. `AHOY_SCRIPT` lets
scripts locate original workspace data. Each invocation compiles afresh.

Scripts importing `helper_runtime` compile in a private tree containing the
workspace's Buoy tool/test sources. Its package directory links workspace archives
and installed bundled archives, avoiding reliance on an extracted workspace cache.
Workspace archives take precedence by filename. Bundled archives are found at
`/usr/share/buoy/packages` on Linux or `/Applications/Buoy/packages` on macOS;
set `AHOY_PACKAGES` to another bundle directory when needed. No package is added
to the repository by this staging operation.

| Entrypoint under `tools/` | Scope and dependencies |
| --- | --- |
| `platform_audit.bui` | Host/tool inventory, target checks and native console smoke; POSIX utilities including `realpath`. Cross checks remain distinct from native coverage. |
| `run_workspace_audit.bui` | Curated inventory, component selection, timeouts and JSON reporting; executes existing Python test payloads using `python3` and GNU `timeout` or `gtimeout`. |
| `check_package_sources.bui` | Package metadata, module/resource bytes and existing dist mirrors; bundled Zip/Crypto packages. `--source-ref HEAD` reads committed inputs through a private snapshot helper requiring Git and Python 3; archives remain current worktree files. Standalone compiled callers outside this workspace can set `BUOY_PACKAGE_SOURCE_SNAPSHOT_HELPER` to the helper path. Native ABI validation is outside this checker. |
| `upstream_issues.bui` | Read-only `list`, `render`, and `validate`, including readiness checks; GNU `realpath` and `sha256sum`. Submission commands are explicitly rejected. |
| `run_example.bui` | Isolated source/package staging, assets, cross-builds and optional execution; GNU/Linux utilities. Target builds still require their native SDK/runtime inputs. |
| `discover_layout_packages.bui` | Layout package discovery and descriptor validation; bundled Zip and POSIX hashing utility. |
| `discover_documentation.bui` | Documentation package discovery and semantic-content validation; bundled Regex/Zip. |
| `font_package_artifacts.bui` | Font package artifact inspection and receipt generation; bundled Zip. |
| `generate_api_documentation.bui` | Typed API documentation generation; rejects unsupported public declarations. |
| `generate_localekit_tables.bui` | Pinned CLDR LocaleKit generation and `--check`; bundled Crypto. |

Maintainers keep fixture-based parity checks locally. These checks do not
establish support for every malformed input or native host platform.
The LocaleKit generator covers LocaleKit only; other Unicode generators retain
their existing implementations.

The parity tests deliberately use Python as an independent oracle.

The aggregate builds each executable in a separate temporary directory, retaining
its runtime sidecars for the suite. Never rebuild different executables into the
same directory while one is running: replacing a mapped runtime library can
cause SIGBUS after otherwise correct output. Fresh isolated Ahoy runs have passed
archive discovery fixtures. The aggregate uses native binaries for validators
that accept them and Ahoy for the audit and LocaleKit suites; individual native
results do not imply every entrypoint was exercised through Ahoy.

Test suites and fixtures are maintainer-local and are not part of this public repository.
