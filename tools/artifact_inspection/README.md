# Static artifact inspection

Two Python standard-library commands inspect release outputs and older REALbasic
Windows executables. They never execute an inspected binary, resolve a dependency
by loading it, patch code, extract executable files, or open paths found in an
input. They are separate from RBCodec's project/clipboard importer and do not
modify its modules or infer complete ports from executable evidence.

Run from the repository root with Python 3.9 or newer:

```sh
python3 -m tools.artifact_inspection audit dist/BuoyStudio > /tmp/studio-audit.json
python3 -m tools.artifact_inspection audit dist/bpm.exe > /tmp/bpm-audit.json
python3 -m tools.artifact_inspection legacy /path/to/legacy.exe > /tmp/legacy-inspection.json
```

Put reports **outside** the audited tree and use an immutable build snapshot.
The tool reads each file consistently but cannot make a changing directory into
an atomic snapshot. It is not a sandbox for an adversary concurrently replacing
parent directories. Root and leaf symlinks are refused for file reads; bundle
symlinks and Windows junction/reparse entries are recorded without following their targets under normal stable-tree
operation. Network filesystems may perform network I/O when explicitly selected;
the tools themselves make no network requests.

## Release audit

The JSON report inventories relative paths, byte counts and SHA-256 values for
regular files, plus symlink targets. It includes static binary metadata:

- PE32/PE32+: sections, directly declared import DLLs, debug-directory presence,
  and certificate-table presence.
- ELF32/ELF64, either byte order: sections, `DT_NEEDED`, `DT_RPATH`/`DT_RUNPATH`,
  debug sections and the static symbol table. Sectionless binaries explicitly
  report dynamic/debug metadata as unassessed; extended section numbering is not
  implemented. Sections without a name table remain inspectable by type, with
  name-based debug detection explicitly unassessed. Dynamic tables must terminate
  with `DT_NULL` and reference actual string-table sections.
- Mach-O32/Mach-O64 and universal containers: load-command dependencies, rpaths,
  debug section names and `LC_CODE_SIGNATURE` presence by architecture.

Static `hardening` metadata records PE ASLR/DEP/high-entropy-VA/CFG opt-in flags,
relocation-directory presence and writable/executable sections; ELF `ET_DYN`,
`PT_GNU_STACK` flags and writable/executable LOAD segments; and Mach-O PIE,
executable-stack and initial segment protections. ELF `ET_DYN` also describes
shared libraries and does not establish that a file is a PIE executable. An
absent ELF GNU_STACK header reports `null`, not a promise of a nonexecutable
stack. PE relocation presence alone does not validate relocation contents.
Findings flag requested executable stacks, writable/executable memory, and
missing PE ASLR/DEP opt-ins, contradictory PE ASLR/relocation metadata, non-PIE
executables and unspecified ELF stack policy for review. Mach-O signature absence
is checked per architecture, including universal files. No claim is made about effective runtime
mitigations, kernel policy or whether a particular product needs those flags.

`findings` include source/debug/template/credential file names, external symlinks,
non-system absolute loader paths, debug metadata, and heuristic private-key,
credential-assignment and developer-path text matches. A file inventory also
shows every bundled native library, so unexpected dependencies can be reviewed.
No universal dependency allowlist is assumed.

Each finding has a suggested review action. Text-indicator samples contain only
offsets and lengths, with no matched values or guessable per-match hashes. The
root field preserves the supplied path rather than expanding a relative path.
Whole-file hashes remain part of the artifact inventory and are not a privacy
mechanism. Binary dependency paths and file
names remain visible to make the inventory useful; reports are not guaranteed to
be free of sensitive metadata. Credential-like messages and test data can trigger
false positives. No finding is proof of a vulnerability, and no match is proof
that an artifact contains no secrets. Unicode/encoded/encrypted secrets are not
exhaustively searched.

Signature statuses are `absent`, `present-unverified`, `not-assessed`, or
`per-architecture-unverified`. A certificate table or load command does **not**
establish a valid signature. Cryptographic verification, notarization,
entitlements, trust chains, dependency resolution, transitive imports, PE delayed
imports, and dynamic library calls are outside this audit. The tool does not run
`ldd` or the inspected program. Archives/`.buipkg` files are hashed without
unpacking them. Unsupported file formats remain inventory entries.

The default limits are 256 MiB per file, 1 GiB of successfully read data per
audit, and 10,000 filesystem entries including directories. ELF additionally has
an aggregate 100,000 dynamic-entry budget and a 16 MiB string-output budget per
file; overlapping dynamic tables reject, and bounds-only validation avoids
copying aliased section payloads. ELF search paths are checked component by
component, retaining the original colon-separated field as evidence. Parsing also caps
resource directories, architectures, load commands and relocation entries.
Oversized, inaccessible, malformed or concurrently modified files make the audit
`incomplete`, with errors in JSON. Findings alone do not fail the command; review
them against the product's release policy before distribution. A complete audit
means the requested inventory finished, not that distribution is approved.

Java class version headers that share the universal Mach-O magic remain
unsupported inventory entries. Malformed recognized Mach-O headers still reject.

## Legacy executable inspector and migration reports

This implements a narrow subset of the
[2017 nullsecurity article](https://www.nullsecurity.org/article/reverse_engineering_realbasic_applications).
It accepts Windows x86 PE32 only and locates the resource structurally through
the PE resource directory. It does not scan for an arbitrary `PICKLE` string or
guess the end-of-image offset.

The assembly in the article pushes `PICKLE`, `0x65`, then `hInstance` before
`FindResourceA`. In conjunction with Microsoft's
[FindResourceA parameter order](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-findresourcea),
this means **type `PICKLE`, name/ID 101**, despite the prose calling it a resource
"named PICKLE". This interpretation is tested separately from the reversed
type/name layout. Multiple matching language variants reject as ambiguous.

The eight-byte resource holds an overlay file offset and the `POKE` marker.
Five DWORD-length-prefixed blocks follow at that offset: CODE, DATA, IMPORT,
RSRC and OPTIONS. Each block's location, length and hash is retained. An overlay
overlapping PE image sections or the certificate table rejects. Trailing bytes are counted and hashed.

IMPORT types 1, 2, 3 and 5 yield runtime API names/relocation slots, DATA references,
local CODE references and provisional string descriptors. API relocation chains
are traversed with range, overlap, cycle and work limits; bytes remain unchanged.
`!Xlibrary!symbol` is reported as an external dependency, without opening it.
Unknown record types stop semantic decoding and retain the entire remaining
IMPORT tail as an opaque offset/size/hash descriptor, returning `partial`.
An unknown one-byte tail is likewise opaque; a truncated known record rejects.
OPTIONS and resource payload semantics remain opaque; block metadata does not
claim to recover usable pictures, menus or UI definitions.

The article disagrees about `PICK` versus `POKE` as the external-file sentinel.
Zero and both sentinel integers return `external-overlay-unsupported`. No
`code.dat`, `data.dat` or other sidecar is opened automatically. Other `!` runtime
forms are retained by name with an unmapped migration hint.

`migrationHints` ties each action to an exact import name. Documented lifecycle,
file-type, object-lifetime/exception and external-library examples receive review
suggestions. Unknown names remain unmapped; imports do not prove those facilities
are exercised or have working Buoy replacements. Next steps are to obtain source,
compare RBCodec's project/resource evidence, inspect Declare ABIs, and validate
source-level behavior. No importer transformation or compatibility claim occurs.

Printable ASCII DATA candidates are limited to 100 and represented by offsets,
lengths and hashes by default. Opt in to text (up to 512 characters each) with:

```sh
python3 -m tools.artifact_inspection legacy owned-legacy.exe --include-strings > /tmp/legacy-text.json
```

These runs are **not** verified language string objects. The article's rbString
layout and type-5 interpretation are provisional, and no original REALbasic
executable has been independently validated in this work. The owned fixture
validates our implementation of the described format, not coverage of real
REALbasic versions. Modern Xojo, macOS/Linux REALbasic formats, framework address
table reconstruction, decompilation and original-source recovery are unsupported.

## Exit status and validation

Both commands write reports to stdout. Errors before a report is available are
JSON on stderr. Exit status 0 means inspection completed; 2 means an I/O,
malformed-input, limit or incomplete-audit error; 3 means legacy format
not recognized, external overlay unsupported, or partially decoded. Exit 0 on a
legacy report still leaves the explicitly documented opaque blocks uninterpreted.

```sh
python3 -m tools.artifact_inspection.verify \
  --artifact dist/buoy-dev --artifact dist/bpm.exe --artifact dist/BuoyStudio \
  > /tmp/artifact-validation.json
```

The verifier runs owned fixtures and then statically reads only the explicitly
selected built artifacts. It does not rebuild or launch them. The tests cover
every truncation of the owned PE overlay fixture, 500 deterministic mutations,
invalid resource trees/names, lengths, offsets, relocation cycles/overlaps,
opaque tails, unsupported sentinels, ELF/Mach-O/PE metadata, bounded inventories,
symlinks, redaction, CLI statuses and migration hints.

validation.json records the completed local verification,
artifact hashes/metadata and heuristic finding counts. It is a snapshot of those
builds, not a claim that later builds match. Mach-O coverage is synthetic; the
actual build validation in this Linux workspace covers ELF and PE. All added
implementation and fixture sources are Python; no Buoy sources were changed and
no compiler/runtime defect was encountered.

The Astra and Opus review record (private maintainer material) documents findings and their local
fixes. Windows binary-open flags and junction pruning have regression coverage
through simulation on Linux; native Windows execution of these Python tools has
not been verified. Reading a built PE file on Linux does not establish that
coverage.

Test suites and fixtures are maintainer-local and are not part of this public repository.
