# Studio Linux preview build contract

`build_studio.stage()` compiles the existing GTK lifecycle guard for the build
host and bundles it as the resource `studio-gtk-lifecycle.o` on Linux. Studio's
own executable links the same staged object. macOS and Windows do not carry it.

`StudioPreview.bui` must, inside `#If TargetLinux Then`, resolve
`FileSystemItem.AppResource("studio-gtk-lifecycle.o")` before launching a native
preview build. Require a non-Nil existing file; otherwise fail the preview with
an actionable request to rebuild Studio. Append `"--extra-obj"` and the resource's
`FullPath()` to the Buoy compiler argument array used for the **build** phase.
The semantic `--check` phase needs no object. The resource can be passed directly:
it lives as long as the Studio installation, independently of the temporary
preview folder. No runtime C compiler is required.

The object matches Studio's host Linux architecture and current bridge workaround.
This builder creates host applications; cross-target packaging would need a guard
object built for the selected target using `build_gtklifecycle.build_object` with
the appropriate sysroot. A compiler producing a preview for another architecture
must not reuse this object.
