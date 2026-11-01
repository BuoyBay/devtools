# Unified Buoy documentation

Studio and static websites use the same content format, search index, and reader.
The installed upstream reference, tutorials, course, and examples remain available
offline alongside package documentation. See [FORMAT.md](FORMAT.md) for the exact
version 1 contract, supported Markdown/front matter, and resource limits.

## Author package documentation

Add UTF-8 Markdown and relative PNG/JPEG/GIF/WebP images under a library's
`resources/buoy-docs/` directory. Start with `index.md`, link to a getting started
guide and an API reference, and use headings for the page outline. Folder
`index.md` files name breadcrumb parents. Code fences can specify `buoy`, `json`,
or another language; the shared reader supplies code styling and copy buttons.
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

## Index installed documentation

```sh
python3 tools/discover_documentation.py --project . \
  --snapshot apps/BuoyDocs/snapshot/docs.json --output .buoy/docs/index.json
```

The input upstream snapshot is checked into this repository. Refresh it from an
installed Buoy toolchain using `tools/index_buoy_docs.cjs` as described in the
Buoy Docs README (not included in this repository). Discovery itself needs only
Python's standard library and never fetches anything from the network.

## Build a documentation website

The result is one self-contained `index.html` with embedded content, images, and
search postings. It works offline or on static hosting, uses the same system
theme as Studio, and needs no JavaScript package installation or server runtime.
Builder filesystem paths are omitted from the public output. The source build
uses temporary documentation-only importer inputs; these are never installed or
published as compiler packages. The website is an export artifact, ready for a
future BPM website to host; this tool does not deploy it.

## Verify

```sh
PYTHONPATH=tests python3 -m unittest test_bpm_documentation -v
```

Package coverage requires an overview, guide, API reference, summaries, indexed
symbols, valid internal links, and a clean import beside the upstream corpus.

## Maintain typed package references

```sh
node tests/documentation_viewer_test.cjs
```

The generator reads declarations from all source package manifests, preserving
existing overview/guide/contracts and adding `typed-api.md` with property flags,
one parameter per row, exact defaults, and return types. It uses the compiler to
reseal archives so documentation resources and package digests stay consistent.
Review the generated changes before distribution. Native libraries are not rebuilt.

UpdateKit is excluded from bulk archive synchronization during native QA.
`tools/build_updatekit.py` owns its validated slice receipts and macOS dylib
assembly. Source documentation maintenance may continue; the documentation
generator leaves `packages/updatekit-0.1.0.buipkg` untouched until that ownership
hold is explicitly handed off.
See FORMAT.md for linked keywords/types, callouts, heading and line-range links,
clipboard controls, and the `shareBaseUrl` hosting option.


## Keyboard and accessibility review

The reader applies the command discoverability, keyboard, focus, and text-size
principles from the [BAMA Mac design skill](https://github.com/daveebbelaar/bama/blob/f0c2cdbf9e41d838c14d8b7596729581dd4833c0/skills/design/mac-design/SKILL.md)
and [UX accessibility guidance](https://github.com/daveebbelaar/bama/blob/f0c2cdbf9e41d838c14d8b7596729581dd4833c0/skills/design/ux-design/accessibility.md).
The native documentation window exposes Find, Find Next/Previous, global search,
history, text size, reserved words, and keyboard help through menus as well as the
reader controls. The complete bindings are documented in [FORMAT.md](FORMAT.md#search-keyboard-access-and-reading-controls).

Keyboard regression tests cover Mac, Windows, and Linux modifier conventions,
page/line scrolling, find navigation, and editing-field protection. Copy controls
have accessible names; dialogs trap focus and close with Escape; navigation moves
focus into the article; API tables expose row/column headers and scroll regions.
Code references use a single keyboard-accessible chooser per block. Reading text
can scale to 200% without scaling the surrounding controls.

DOM tests and cross-target compiler checks do not verify VoiceOver, native
Accessibility Inspector output, or physical macOS/Windows keyboard behavior.
Those live platform checks remain unverified on the Linux validation host.

Test suites and fixtures are maintainer-local and are not part of this public repository.
