# Build package backends in CI

Use the reusable [BuoyBay CI tools](https://github.com/BuoyBay/ci) to configure
workers, compiler images, native build steps and result receipts. Its example
profiles and plans describe the supplied interfaces.

## Select native workers

Match each worker's OS and architecture to the backend being built. Linux
containers need the project's compiler/SDK and native dependencies; macOS needs
Apple command-line tools and the matching SDK; Windows needs a supported native
toolchain and C/C++ runtime.

A cross-target check validates source for that target. It does not execute that
target's native implementation. Identify emulation separately from native runs.
GUI and operating-system service APIs need their actual desktop or service setup.

## Build current inputs

Use [the native package builder](native_package_build.md) for packages supported
by that driver. Native bridges use their specialized builders and documented
compiler, SDK and dependency inputs. The retained Python entrypoints and their
production responsibilities are listed in [the Python policy](../PYTHON.md).

Build in isolated source/output directories. Record the selected source revision,
compiler/SDK, dependency identities, exact commands and exit statuses. Keep each
executable's deployed runtime files together. Validate an archive's source and
native target before adding it to a package release.

## Evaluate results

Require both successful builds and application-level behavior checks for the
features being released. Record coverage limits, skips and missing platform
prerequisites explicitly. Collect hashes and logs while the job's outputs are
available, then pass the resulting receipts to the CI gate.

Image publication, artifact publication and deployment are explicit downstream
workflow actions. Review their selected versions and credentials independently
of the build job.
