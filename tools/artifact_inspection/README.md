# Inspect native build outputs

Check both the application binary and its deployed runtime files before moving a
build to another machine. Use the selected builder's emitted manifest or receipt
to identify the files that belong together.

## Linux

With `file` and binutils installed, supply the binary you built:

```sh
file /absolute/path/to/application
readelf -h /absolute/path/to/application
readelf -d /absolute/path/to/application
```

Check the architecture and shared-library requirements against the target host.
Inspect every shipped native library as well. Resolve runtime dependencies with
the intended loader configuration; a same-named library with a different ABI is
not an interchangeable replacement.

## macOS

With Apple command-line tools installed:

```sh
file /absolute/path/to/application
otool -L /absolute/path/to/application
otool -l /absolute/path/to/application
```

Review linked libraries and loader search paths. Keep non-system dylibs with the
application according to its builder's deployment layout. Recheck signatures
after changes to Mach-O load commands or bundled libraries.

## Windows

From a developer command prompt with the matching Windows toolchain:

```text
dumpbin /headers C:\path\to\application.exe
dumpbin /dependents C:\path\to\application.exe
```

Check architecture, imported DLLs and the runtime files needed by the destination
system. Static inspection complements execution; it cannot establish correct
application behavior or determine every dynamically loaded library.

## Compare a deployment

Record the source revision, compiler/SDK identity and hashes of the executable
and runtime files. Exercise the copied deployment with its intended configuration.
Keep signing credentials and runtime secrets outside the artifact bundle.
