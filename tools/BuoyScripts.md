# Buoy workspace scripts

Run source entrypoints with [buoy-run](https://github.com/BuoyBay/buoy-run), the
POSIX Buoy script runner. It preserves arguments, working directory and standard
streams while compiling each invocation. Use a compatible compiler and the SDK
packages required by the selected script.

For shared-source helpers, run from the assembled BuoyBay integration workspace:

```sh
buoy-run tools/platform_audit.bui --help
buoy-run tools/discover_documentation.bui --help
```

Scripts importing `helper_runtime` use the shared helper from the runner checkout.
Workspace assembly supplies that logical source path. Installed compiler archives
are normally found at `/usr/share/buoy/packages` on Linux or
`/Applications/Buoy/packages` on macOS; `AHOY_PACKAGES` selects another directory.

| Entrypoint under `tools/` | Purpose |
| --- | --- |
| `platform_audit.bui` | Inspect host tools and compiler targets; POSIX utilities are required. |
| `check_package_sources.bui` | Compare package metadata, module/resource bytes and archive inputs; requires Zip/Crypto. |
| `run_example.bui` | Stage and compile a supplied example, with optional execution and explicit target inputs. |
| `discover_layout_packages.bui` | Discover layout packages and validate descriptors; requires Zip and POSIX hashing tools. |
| `discover_documentation.bui` | Discover and index supplied documentation; requires Regex/Zip/Crypto. |
| `font_package_artifacts.bui` | Inspect font package artifacts and generate receipts; requires Zip. |
| `generate_api_documentation.bui` | Generate source-derived API documentation. |
| `generate_localekit_tables.bui` | Generate LocaleKit tables from its pinned source data; requires Crypto. |

Use an entrypoint's `--help` to select its source roots, output directories and
other required inputs. Source checks do not establish native ABI correctness.
`check_package_sources.bui --source-ref` additionally needs a snapshot helper;
supply one with `BUOY_PACKAGE_SOURCE_SNAPSHOT_HELPER` when using that mode.

Keep each compiled executable and its runtime sidecars in an isolated directory.
Replacing runtime libraries while another executable uses them can invalidate a
running process. Package-specific C/Rust builders retain their documented native
dependencies and supported-platform boundaries.
