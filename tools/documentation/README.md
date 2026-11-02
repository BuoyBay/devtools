# Unified Buoy documentation

Studio and static websites use the same content format, search index, and reader.
The installed upstream reference, tutorials, course, and examples remain available
offline alongside package documentation. See [FORMAT.md](FORMAT.md) for the exact
version 1 contract, supported Markdown/front matter, and resource limits.

## Author package documentation

Add UTF-8 Markdown and relative PNG/JPEG/GIF/WebP images under a library's
`resources/buoy-docs/` directory. Start with `index.md`, link to a getting started
guide and an API reference, and use headings for the page outline. Folder
`index.md` files name breadcrumb parents. Code fences can specify `basic` (for
Buoy source), `json`, or another language; `basic` is the GitHub/Linguist alias
the reader maps to Buoy. The shared reader supplies code styling and copy buttons.
Packages never supply CSS, scripts, fonts, or reader layouts.

Make getting-started examples reproducible in a fresh consumer project. Name the
required `.buipkg` archives, including packages imported by the library itself,
and explain how to obtain them. For manual local use, put those archives in the
consumer's `packages/` directory before compiling; for BPM-managed projects,
link to the dependency manifest and install/sync instructions. Show the required
`Import` statements and a compiler command with its working directory. Include
native runtime prerequisites where the example needs them, and distinguish a
compiler check from a runnable application.

The compiler extracts archives into `.buoy/packages/`; that generated cache is
not the authoring or distribution location. Documentation front-matter
`dependencies` scopes reference navigation to other packages; it does not install
archives or declare compiler requirements. A source-page website preview also
does not make the documented packages available to the compiler.

Code blocks show line numbers by default. Cmd-click on macOS, or Ctrl-click on
Windows/Linux, opens reference links for known types, keywords, and API symbols.
Member clicks use declared receiver types where available; ambiguous exports do
not jump to an arbitrary package. Strings and comments remain plain source.
Click a line number to select it and copy its permalink; Shift-click another
to select and copy a range link. Copying leaves the viewport and history unchanged.
Use **Copy lines** for the selected source or **Copy link** to copy the link again. Full-code copying preserves source text without gutters.

Use metadata to describe content:

```yaml
---
title: Widget API
summary: Construct widgets and update their state.
category: Reference
order: 20
keywords: ["widget", "state"]
symbols: ["Widget", "Widget.Update"]
types: ["Widget"]
related: ["getting-started.md", "api.md"]
---
```

A type named in `types` must have a matching heading, such as `## Widget`, on
that page. `related` links to other local documentation pages and optional sections.

The metadata parser supports a documented YAML subset. Use JSON double-quoted
strings for punctuation and JSON arrays for keywords/symbols. Use relative links to other Markdown files inside this package. HTTP/HTTPS
references are supported as copyable URLs; the offline reader does not navigate
away or fetch their contents. External images and unsafe URL schemes are rejected.

`bpm pack` includes `resources/` by default. If `bpm.json` has an explicit `files`
list, include `resources` or `resources/buoy-docs` in it. Track these files in Git
before publication. Compiler sealing, BPM publication, synchronization, vendoring,
and locked recovery all preserve documentation as ordinary package resources.
No post-install hooks or generated package scripts are needed.
When the compiler manifest contains an explicit `resources` array, list every
documentation page and image there too, or omit that array to enable discovery.
BPM rejects selected documentation omitted by an explicit compiler resource list.
The current compiler delivers documentation resources into consumer applications
along with the package's other resources.

The [greetings example](https://github.com/BuoyBay/bpm/blob/main/src/examples/hello/greetings/resources/buoy-docs/index.md)
demonstrates an overview, guide, API hierarchy, metadata, links, and a code block.

## Index documentation

`tools/discover_documentation.bui` reads a project and a supplied upstream
snapshot and writes a searchable index. From an assembled integration workspace
with buoy-run and the required SDK archives available:

```sh
buoy-run tools/discover_documentation.bui --project "$PROJECT_ROOT" \
  --snapshot "$DOCUMENTATION_SNAPSHOT" --output "$OUTPUT_INDEX"
```

Set those paths to your project, a compatible documentation snapshot and the
output location. Use `--global-dir` for an additional package-documentation root.
The reader uses the format described in [FORMAT.md](FORMAT.md).

## Maintain typed package references

`tools/generate_api_documentation.bui` derives API reference material from package
source. Run its help from the combined source workspace to select a library,
root and output/check mode:

```sh
buoy-run tools/generate_api_documentation.bui --help
```

Review generated declarations, signatures, links and resource changes before
releasing the package. Rebuilding native backends remains the responsibility of
the package's native builder. The source documentation tooling does not establish
that a native library has been rebuilt or validated.

## Keyboard and accessibility review

The reader applies the command discoverability, keyboard, focus, and text-size
principles from the [BAMA Mac design skill](https://github.com/daveebbelaar/bama/blob/f0c2cdbf9e41d838c14d8b7596729581dd4833c0/skills/design/mac-design/SKILL.md)
and [UX accessibility guidance](https://github.com/daveebbelaar/bama/blob/f0c2cdbf9e41d838c14d8b7596729581dd4833c0/skills/design/ux-design/accessibility.md).
The native documentation window exposes Find, Find Next/Previous, global search,
history, text size, reserved words, and keyboard help through menus as well as the
reader controls. The complete bindings are documented in [FORMAT.md](FORMAT.md#search-keyboard-access-and-reading-controls).

Keyboard controls follow the platform's modifier conventions and preserve editing-field behavior. Copy controls
have accessible names; dialogs trap focus and close with Escape; navigation moves
focus into the article; API tables expose row/column headers and scroll regions.
Code references use a single keyboard-accessible chooser per block. Reading text
can scale to 200% without scaling the surrounding controls.

Validate screen-reader output, native accessibility properties and physical
keyboard behavior on the target platform when those features are required.
