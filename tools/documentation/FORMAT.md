# Offline package documentation, version 1

`python3 tools/discover_documentation.py --project PROJECT --snapshot UPSTREAM.json --output UNIFIED.json`

The Python standard-library helper reads data from installed `.buipkg` ZIPs;
there is no Node runtime, package import, execution, extraction, or network access.
BPM already carries `resources/` in these archives. Publication `.bpm.tar` wrappers
and live source directories are not discovery inputs: install/sync packages first.
Studio owns the viewer, theme, syntax highlighting, and per-code-block copy buttons.
Packages supply semantic content and metadata only, never CSS, scripts, or layouts.

## Discovery and rebuild

Read `PROJECT/packages/*.buipkg`, then the global compiler package directory:

- macOS: `~/Library/Application Support/buoy/packages`
- Windows: `%LOCALAPPDATA%/buoy/packages` (fallback `~/AppData/Local`)
- Linux: `$XDG_DATA_HOME/buoy/packages` (fallback `~/.local/share`)

`--global-dir PATH` overrides the latter for testing or explicit installations.
Only direct children are inspected, in filename order. Identity is the compiler
manifest `name`, not the archive filename or registry coordinate. A valid local
name claims that identity even without docs or with invalid documentation/version;
it shadows all global versions. Within one root the first sorted archive wins.
An unreadable archive or invalid manifest name cannot claim an identity.

When `bpm.lock` is present, its `install` closure selects the exact artifact
filename before other local versions, and the helper verifies its locked SHA-256.
A missing or modified locked artifact produces a diagnostic and blocks fallback
to another version or global copy of that identity. Unmanaged packages remain
discoverable. An invalid lock is diagnosed, with ordinary directory discovery
retained. After resolving changes, run `bpm sync` before refreshing documentation.
Workspace discovery uses the selected project/member's `packages` directory;
opening a workspace root does not combine every member's independent installation.

Every invocation rebuilds pages, counts, and token postings, so removal and upgrades
cannot retain stale docs. Previously generated package pages are removed if a unified
snapshot is reused as input. Upstream page records are preserved verbatim; their
aliases, unresolved-link records, and start page are retained when valid. The
upstream snapshot is trusted installed content, not sanitized package input.
Output uses a same-directory temporary file, flush/fsync, and atomic replacement;
fatal input/output failures leave the previous output intact and return status 1.
Individual bad archives, pages, images, and links produce `diagnostics` and allow
other documentation to remain available (CLI status 0). The CLI also prints a JSON
summary. Consumers should surface diagnostics without interpreting them as HTML.

## Package authoring

Place UTF-8 Markdown at `resources/buoy-docs/**/*.md`, including root `index.md`.
Use `manifest.json` schema 1 with a lowercase compiler package identifier and
`major.minor.patch` version (optional prerelease/build suffix). These manifest
fields, never front matter, determine provenance and namespace.

Optional front matter starts on the first line with `---`, ends with `---`, and
contains at most 128 lines. This is an intentionally restricted YAML subset:

```yaml
---
title: Widget Guide
summary: "Widgets: construction and use"
category: Controls
order: 10
keywords: ["widget", "drawing"]
symbols: ["Widget.Draw", "Widget.Color"]
types: ["Widget"]
related: ["getting-started.md", "api.md#properties"]
---
```

Only these nine keys are allowed; duplicate or unknown keys reject the page.
`title`, `summary`, and `category` are nonempty strings of at most 2048 characters.
Use a JSON double-quoted string for punctuation/escaping. Plain strings cannot
contain `: # [ ] { } & * ! | > ' "` or surrounding whitespace; all plain values
are strings, without YAML boolean/null/number coercion. `keywords`, `symbols`, `types`, `related`, and `dependencies`
are JSON arrays of at most 64 nonempty strings, each at most 256 characters.
`types` contains declared type identifiers, including qualified names. Each must
have a matching heading anchor on this page (using the normal heading slug).
Missing type anchors produce diagnostics and are excluded from navigation.
`dependencies` contains compiler package identifiers used to disambiguate imported
types. Generated references populate it from BPM dependency declarations; discovery
shares the package dependency scope with its other pages.
`related` contains relative page paths with optional fragments, resolved and
validated with the same restrictions as Markdown links; missing or unsafe targets
produce diagnostics. The snapshot stores their canonical documentation paths.
`order` is a signed integer with at most six digits. Strings cannot contain control
characters. Blank lines are accepted. YAML comments, nested mappings, block lists,
single quotes, tags, anchors, aliases, and multiline scalars are unsupported and
diagnosed. Invalid metadata skips that page; it never silently changes meaning.

## Markdown and assets

The deliberately limited renderer supports ATX headings (levels 1–6, stable slug
IDs with duplicate suffixes), paragraphs, flat bullet/numbered lists, pipe tables
with a separator row, inline code, single/double-asterisk emphasis, inline links
and images, blockquotes, thematic breaks, and fenced code using backticks or tildes.
Consecutive ordinary source lines form one paragraph; the viewer controls wrapping.
Fences retain an optional
ASCII language identifier as `class="language-buoy"`, for example. Code is escaped
and emitted as `pre > code`; the viewer supplies highlighting and copy controls
from code text. Source headings remain in the page. Nested Markdown, reference
links, escaped table pipes, link titles, and nested blockquotes are
not interpreted as full CommonMark. Unsupported constructs remain text.

Raw HTML is escaped, including authored style/script tags; there are no authored
attributes or styles. Links resolve only to included Markdown pages in the same
package, relative to the current page (fragments supported). Percent-decoded paths
are checked before resolution; traversal outside the docs root, absolute paths,
backslashes, protocol-relative URLs, and queries on local page links are disallowed.
URL schemes other than HTTP/HTTPS reference links are disallowed.
Invalid or missing links become plain labels with diagnostics. HTTP/HTTPS
reference links are preserved as data; activating one in the reader copies its
URL for use outside the offline reader. They never fetch content or navigate
automatically. External images remain disallowed. There is no
cross-package or upstream-link resolution in this first format.

Relative PNG/JPEG/GIF/WebP images beneath the docs root are checked for matching
extension and byte signature, bounded, and embedded as data URIs. These checks are
not a complete raster decoder; the system viewer decodes raster content. All image
attributes are generated and alt text escaped. SVG, other formats, external URLs,
and missing/invalid assets become alt-text placeholders with diagnostics. Images
inside Markdown link labels and paths containing literal spaces/parentheses are
outside the inline syntax; percent-encode such asset paths. Viewer CSS controls
image sizing, tables, code, and overall presentation consistently.

## Viewer contract

Existing `{pages,index,counts,aliases,startPath,missing,captured,...}` remains valid.
Every package page includes `path,title,section,relative,source,html,text` plus:

- `section: "packages"`, `sourceId: "package:<name>"`, `sourceName: "<name>"`.
- `package`, `version`: authoritative manifest values.
- `path: "/package/<name>/<version>/<relative.md>"` with URL-encoded components.
- `source`: provenance URI `file:///.../archive.buipkg!/resources/buoy-docs/...`;
  this is descriptive, never a browser navigation target.
- Optional validated metadata `summary,category,order,keywords,symbols,types,related,dependencies`.
- `breadcrumbs: [{title,path}, ...]`: package root, folders, current page.
  Root/folder `index.md` titles name their crumb and supply its navigable path;
  otherwise the package/folder name is used and `path` is empty (render as text).
  A current index page is not duplicated. No authored parent/page ID is needed.

Pages sort by `order` (default 0) then relative path within each package. `sources`
contains `{id,name,kind,origin,pageCount}` for upstream (`id: "upstream"`) and each
accepted package (also `package,version`). Upstream pages need no new fields;
viewers can infer upstream provenance when `sourceId` is absent. Source IDs stay
stable across upgrades; page paths contain the version to avoid collisions.
`index` maps lowercase Unicode word tokens (letters/numbers/underscore) to page
array offsets. Title, body text, summary, category, package name, keywords, and
symbol components participate. Qualified symbols such as `Widget.Draw` contribute
`widget` and `draw` tokens. Search clients should tokenize queries equivalently.

## Resource limits and trust boundaries

Filesystem package paths and ancestors may not be symlinks. Archives reject unsafe
paths, case-insensitive duplicate entries, symlinks, and other special files.
Limits: 4096 archives per root, 256 MiB archive, 16384 entries, 1 MiB manifest or
Markdown page, 4 MiB raster, 16 MiB uncompressed docs subtree, 200:1 read-entry
compression ratio, 16 MiB embedded image data per page, 32 MiB rendered HTML per
package, 64 MiB combined serialized package pages, and 128 MiB input/output snapshot.
Encrypted entries are rejected when read. No package content is extracted.
These are resource ceilings, not proof that an archive is authentic; BPM owns
installation provenance. Concurrent hostile filesystem replacement is outside this
helper's static-path checks; callers should use package roots they control.

Maintainers run the private validation suite before release. See the repository README for public build instructions.

## API layout and reader controls

`python3 tools/generate_api_documentation.py --sync-archives` maintains a typed
`typed-api.md` for every source package, keeps authored API contracts, adds related
topics, and reseals updated packages with the compiler (including resource hashes).
`--check` fails when declarations and generated tables differ. Declaration reading
preserves generic types, defaults, passing modes, overloads, computed read-only
properties, nested enums, closures, delegates, and platform guards. It is not a
replacement for compiler verification or authored behavioral documentation.

Properties use **Name | Type | Read-Only | Shared | Default | Description**.
Parameters have one row each: **Name | Type | Passing | Default**. “Required” means
there is no parameter default; an em dash in a property/value table means no
explicit initializer. Functions have a separate Returns table; Subs explicitly
return no value. Type metadata indexes declared types, not every member, to keep
metadata bounded. The reader links types in tables and inline signatures, preferring
the same package and version, then declared dependencies, then upstream, then an
unambiguous other package.
Ambiguous and undocumented names remain text. Explicit declarations override
upstream fallback names. Keywords link to their language reference.

Inside Buoy code blocks, Cmd-click (Mac) or Ctrl-click (other systems) opens type
and keyword references; keyboard activation also works. Ordinary clicks preserve
text selection. Other code languages, string literals, and comments are excluded.
RGB color literals (`&cRRGGBB` and `&cRRGGBBAA`) show individually colored channels
and an RGB swatch. The trailing alpha byte is displayed without changing the RGB
preview. This is display support for legacy examples, not a compiler feature.

Every heading has a keyboard-accessible copy-link control. Code blocks provide
optional line numbers, whole-block copying, and copy-link controls. Click a line
number to select it and copy its link, then Shift-click another to select and copy
a range link. These actions leave the viewport, URL, and history unchanged.
Copy lines copies only that
range without numbers. Line fragments are `code-1-L2` or `code-1-L2-L5` (block
ordinals follow document order); opening a range link restores its selection.
These anchors are stable while block ordering and source lines remain unchanged.
A saved line-number preference applies to subsequently displayed blocks.

Static-site links use the current page URL. Hosts may pass an absolute HTTPS
`shareBaseUrl` to `loadDocumentation` to use a stable public viewer URL. Without
that option the native offline viewer copies portable documentation fragments;
it cannot invent a public hosting destination.

Use GitHub-style blockquote callouts in both upstream and package Markdown:

```markdown
> [!NOTE]
> Additional context.

> [!WARNING]
> A behavior callers need to account for.

> [!DEPRECATED]
> Name the supported replacement and migration path.
```

Supported labels: NOTE, TIP, IMPORTANT, WARNING, CAUTION, DEPRECATED. Labels remain
visible and accessible independent of color. Callouts do not execute authored HTML.


### Search, keyboard access, and reading controls

Global search ranks exact definitions before prose matches and opens member results
at their definition anchors. Cmd/Ctrl+Shift+F or Cmd/Ctrl+K focuses global search;
Cmd/Ctrl+F opens find within the current page. Cmd/Ctrl+G and F3 move to the next
page match; add Shift for the previous match. Enter/Shift+Enter also work in the
page-find field. Escape closes find and returns focus to the article.

Outside editable fields, Space/Shift+Space and Page Down/Up scroll by a page,
arrow keys scroll by a line, and Home/End reach the page boundaries. macOS uses
Cmd+Left/Right or Cmd+[/] for Back/Forward; Windows/Linux use Alt+Left/Right or
Ctrl+[/]. Text editing keeps its usual arrow and modifier behavior.
Cmd/Ctrl+plus, minus, and zero control reading text size (80–200%). The Aa menu
provides the same controls. A single References button in each linked Buoy code
block exposes its references to keyboard users without a tab stop for every token.

The generated snapshot carries `definitions` entries (`name`, `anchor`, `kind`),
plus a top-level `keywordIndex` and `keywordAudit`. Refreshing installed docs runs
both definition indexing and keyword catalog generation. Run
`python3 tools/audit_documentation_keywords.py --verify` after a toolchain change
to recheck reserved status using the compiler. Reserved status alone does not
imply implemented statement syntax.


Large source-derived references use `typed-api.md` as an index and place each
type or module-function group under `typed-api/`. Each page keeps its complete
type metadata within the 64-entry bound; types are never dropped from the index.
The index retains type headings for older shared fragments. Generated child pages
link back to package contracts and guides using relative paths.
For disjoint maintenance, `generate_api_documentation.py --library NAME` limits
writes to that library; repeat the flag for several libraries. Archive syncing
remains opt-in and must not run during an integration hold.
