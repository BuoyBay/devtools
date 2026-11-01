# Native Buoy previews

Compile a real declarative layout, populate it through a typed fixture, and
capture native OS pixels. The runner uses the reusable
[PreviewKit](https://github.com/BuoyBay/previewkit/blob/main/docs/API.md) package. LayoutCanvas and the
language server retain their existing responsibilities.

## Run

From the repository root, with Buoy 0.72 on PATH:

```sh
  --backend windows --output dist/preview-windows

  --backend linux --output dist/preview-linux

  --backend macos --host m4-vi --output dist/preview-macos
```

Each output directory must be new. It contains `preview.png`, `result.json`,
`build.json`, the generated `harness.bui`, and runtime logs. Remote jobs also
produce `transport.json`. The result records the source revision, source/input
hashes, compiler version, PreviewKit archive hash, runtime bounds, capture scope,
pixel dimensions, `imageSha256`, artifact directory and environment information. A failed render has `status:
"failed"`, logs and an error, with no successful `image` field. Any intermediate
frame is renamed `diagnostic.png`. Validation errors before staging exit with
code 2; failed jobs exit 1; successful jobs exit 0.

The runner seals the current PreviewKit source into each isolated consumer, so
rendering does not depend on a stale repository package. To distribute the
library separately, run `python3 tools/build_previewkit.py`.

## Jobs and fixtures

The example job (not included in this repository) names a root-level
layout module, a public root type, and an explicit file list relative to the job.
`kind` is `window` (default) or `view`. Include imported modules, packages and
assets in `files`; the compiler resolves the original layout normally, including
constraints and custom controls. The editor's literal-frame subset does not
limit this runner. Roots need a parameterless constructor; use an adapter layout
or class for other constructors. A View is hosted in a generated Window using
its declared dimensions. Source files are staged unchanged.

Job JSON is limited to 32 MiB of UTF-8 bytes. The runner checks this bound before
decoding or parsing and reports the input path for oversized or malformed jobs.

An optional fixture names a module and public procedure accepting the typed
root and a `JSONItem`, as shown in Fixture.bui (not included in this repository).
Its JSON `data` is supplied before the window is shown. Fixture code can populate
lists, select tabs or set other native state using ordinary typed APIs. Named
job files can represent empty, populated, error and long-text cases. Built-in
generators and a fixture expression language are not implemented.

`controls` lists typed member paths to include in the bounds report. These are
references relative to the selected root, not LayoutKit's snapshot-local IDs.
The report identifies the layout file and root, but does not yet provide
compiler-generated source spans or enumerate controls automatically.

Jobs are trusted executable code. Staging prevents accidental dependency pickup;
it is not a security sandbox. Constructors, imported initialization and fixture
code can access the worker's files and services.

## Platform adapters

- **Windows:** uses the existing `containers/windows/vm` upload and console
  controls. The VM must already be ready and unlocked. The coordinator
  cross-compiles `x86_64-windows`; it launches the worker through the interactive
  console rather than the separate SSH session. A PowerShell adapter captures
  the foreground client area, checks screen containment, and cleans up its own
  JobObject, assigned before the preview starts, including descendants whose
  parent exits first. It rejects focus loss. The VM's resolution/theme are recorded,
  not changed. Failed or late console launches retain their unique exchange
  directory and receive a cancellation marker.
- **Linux:** requires X11 `DISPLAY`, ImageMagick `import`, `xwininfo` and `xprop`.
  `xvfb-run -a` supplies a private virtual display without touching a user's
  desktop. It selects the named, mapped top-level window by PID, rejects ambiguous
  matches, and captures its client area.
  Environment variables such as `GTK_THEME` and `GDK_SCALE` pass through. On this
  development host, prepend `LD_LIBRARY_PATH=/tmp/buoy-chatui-jpeg8/build` to use
  the existing JPEG 8 compatibility library required by the installed runtime.
- **macOS:** runs locally on a Mac, or through `--host SSH_ALIAS`. It calls the
  installed `~/.local/bin/buoy-mac-control` client and reuses Buoy Test Controls'
  existing Screen Recording authorization, queue and ScreenCaptureKit window
  capture. Same-title host windows are rejected as ambiguous. It neither installs/rebuilds the helper nor changes its grants. No
  keyboard, mouse or explicit activation commands are sent. The preview opens
  a native window, which can affect application focus. macOS output captures
  the **window**, including its frame; its scope is recorded distinctly from
  Windows/Linux client captures. ScreenCaptureKit output dimensions are those
  returned by the existing helper, not assumed Retina pixel dimensions.

For SSH workers, key-based SSH/SCP access must already work. Only this tool and
the explicitly listed inputs are copied into a private random `/tmp` directory.
The remote runner compiles locally. `BUOY_MAC_COMPILER` selects its compiler
(default `$HOME/.local/bin/buoy-0.72.0`); `BUOY_MAC_CONTROL` selects a local Mac
helper client. An independent remote supervisor enforces the complete phase budget and a
30-second renewable client lease. Disconnects expire the lease and cancel owned
process groups. Remote directories are removed only after complete artifact
transfer and acknowledged process cleanup; otherwise `remoteDirectory` is kept
in `result.json` for diagnosis. Cancellation failures remain visible in the report.

Three matching PNG frames are required before accepting a capture. This is a
bounded settling heuristic, not proof of complete application quiescence. Stop
animations in fixtures. Focus, OS build, fonts, appearance and scale affect
pixels; cross-platform images are expected to differ. There is no image cache
or pixel-diff assertion. Desktop leases serialize cooperating preview runners;
other desktop tools do not automatically participate in those leases. Windows
capture needs an unobscured desktop; macOS capture is targeted by PID/window ID.

## Reuse and MCP

Python callers can import `preview.runner.render(job, output, backend, timeout,
host=None)` with `tools` on their module path. The function returns the same
result stored on disk. `preview.mac_control.MacControl` is a reusable client for
the existing permission holder; it does not duplicate its permission logic.

Run `python3 tools/buoy_preview.py mcp` for the serialized stdio server. It exposes
`preview_capabilities` and `render_layout`. A render takes `job`, `output`, optional
`backend`, `host`, and `timeout` (2–120 seconds for the native phase). It returns
structured metadata as text plus MCP PNG image content on success. Compilation
and transport have separate bounded deadlines. Images above 8 MiB remain on disk.
The server uses MCP newline-delimited JSON-RPC and reserves stdout for protocol
messages. Oversized or unavailable inline images retain the successful render
metadata with an `imageDelivery` explanation and artifact path. Build/runtime
logs go to artifacts. POSIX output is continuously drained with bounded retention.
Windows checks log growth at readiness/frame polls, terminates an over-limit job,
and caps retained logs; transient disk growth can exceed the cap between polls.

The workspace `.mcp.json` registers `buoy-preview`. Reload MCP connections in the
agent client after adding the registration. For Linux MCP captures, launch the
server under `xvfb-run -a`, or provide an existing private X11 display.

Current scope is native Window/View capture and typed fixtures. Open-menu
capture, display capture, automatic source maps, generators and Studio preview
UI integration remain future work.

## Verify

```sh
python3 tools/build_previewkit.py
LD_LIBRARY_PATH=/tmp/buoy-chatui-jpeg8/build xvfb-run -a \
BUOY_PREVIEW_TEST_WINDOWS=1 python3 -m unittest discover \
  -s tests -p test_buoy_preview.py -v
BUOY_PREVIEW_TEST_MACOS_HOST=m4-vi python3 -m unittest discover \
  -s tests -p test_buoy_preview.py -v
```

The Windows suite is opt-in because it uses the actual VM console. The normal
suite covers strict job validation, leases, timeout cleanup, compiler failures,
MCP framing/image responses, runtime failures and hosted View capture. Linux
native tests skip without `DISPLAY`.

Test suites and fixtures are maintainer-local and are not part of this public repository.
