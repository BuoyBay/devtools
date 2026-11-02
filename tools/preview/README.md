# Native layout previews

[PreviewKit](https://github.com/BuoyBay/previewkit) hosts Buoy windows and publishes
named native-control snapshots. Use it when your application or development tool
needs runtime bounds and readiness information for a typed layout.

## Host a layout

Install the package and construct the layout with its ordinary Buoy constructor.
Create a `PreviewSession` around the host Window, register the controls you want
reported with `Track`, set `ReadyPath`, then call `Show()` on the UI thread and
run the application event loop. Keep the session alive for the window's lifetime.
For View previews, create a host Window and attach the View before creating the
session. See the [package API](https://github.com/BuoyBay/previewkit/blob/main/docs/API.md)
for the exact members and ownership rules.

Snapshots describe layout-unit bounds and readiness. Wait for a complete ready
record before consuming it. A ready record does not establish that a compositor
has presented a settled image. Apply your fixture state through ordinary typed
application code before taking a capture.

## Capture native pixels

Choose a capture mechanism supported by the target desktop. Record whether an
image contains the client area or the whole window, and record scale, fonts,
appearance and display settings when comparing outputs.

On X11, tools such as `xwininfo` and ImageMagick's `import` can capture a selected
window. Obtain its actual window ID, then supply it explicitly:

```sh
import -window "$WINDOW_ID" preview.png
```

`xvfb-run -a` can provide a headless X11 display. The application still needs the
matching GTK, image and other native runtime libraries. Configure their installed
library search paths for your environment.

On macOS and Windows, use a capture mechanism with the required desktop/session
access and permissions. Validate focus, containment and window selection when
capturing a particular application's pixels.

## Windows worker implementation

`windows.ps1` is a Windows capture worker. It runs from an interactive desktop,
reads `request.json` in its directory, and starts `bin/preview.exe`. The request's
`timeout` bounds the operation. The application publishes
`_buoy_preview_ready.json`; the worker writes `preview.png` and `result.json`.
Prepare a job directory with that worker, the application and its deployed runtime
files before launching it. Read the script for its cancellation and process-job
ownership protocol when integrating it into a coordinator.

## Limits

Constructors, imports and fixture code execute with the worker's permissions.
Use trusted source and configuration. Screenshots can vary across platforms and
rendering environments; compare them with an explicit tolerance and scope.
Interactive keyboard, accessibility and lifecycle behavior require their own
checks in addition to bounds or pixel observations.
