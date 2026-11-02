# Native package and app builders

These tools stage source and dependency archives, run the Buoy compiler with
literal argument arrays, and build packages or applications in isolated work
areas. The native drivers cover generic package builds and the Linux Studio path;
package-specific native backends retain their explicit prerequisites.

## Build a source package

Follow the [package builder bootstrap](tools/native_package_build.md) to compile
`tools/build_packages.bui` with ProcessKit. Once the driver is on your `PATH`:

```sh
build-packages --root /absolute/path/to/algorithmkit --compile-consumer algorithmkit
build-packages --root /absolute/path/to/workspace-tools --compile-consumer mathkit formatkit
```

The driver rebuilds declared local dependencies, checks source on all six targets
and can link a host import consumer. JSON reports distinguish failures. Native
backend/SDK packages need their specialized build path; import linking is not API
behavioral validation. Remote package acquisition belongs to
[BPM](https://github.com/BuoyBay/bpm).

## Application builders

`tools/build_studio.bui` stages Studio and its dependencies and builds the Linux
application. Its helper is `tools/studio_native_support.bui`; Studio's
[native guide](https://github.com/BuoyBay/studio/blob/main/src/docs/NATIVE_BUILD.md)
contains prerequisites and supported options. Ahoy and BPM own their command-line
build drivers in their respective repositories.

Keep one deployment directory per executable: the compiler places runtime
libraries beside the output binary. Source audits, documentation helpers and
remaining platform-specific adapters live under `tools/`; their names and
requirements are documented with each entrypoint.

[Contributing](CONTRIBUTING.md) · [License](LICENSE)
