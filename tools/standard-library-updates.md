# Standard-library update check

`tools/check_standard_library_updates.py` reads the pinned source records and
compares them with the current official stable releases. It writes a JSON
report. It does not download release archives, accept a new checksum, edit a
pin, open an issue, or publish a package.

This is separate from [standard-library native CI](standard_library_ci.md) and
`.github/workflows/standard-library-native.yml`.

## Pins

| Component | Record | Compared field |
| --- | --- | --- |
| IANA tzdata | `libraries/timezonekit/data/sources.json` | `version` (`2026f` today) and its `data.iana.org` archive URL |
| CLDR | `libraries/timezonekit/data/cldr-49/sources.json` | `version` plus the pinned `commit` |
| zlib | `libraries/compressionkit/native/sources.json` | `version` (`1.3.2`) and the published SHA-256 shape |
| Unicode UCD | `libraries/normalizationkit/data/unicode-18.0.0/sources.json` | `unicode_version` |

The recorded SHA-256 values are checked only for shape and, for IANA and zlib,
for agreement with the official archive URL of the pinned version. The checker
does not fetch those archives and does not treat a new archive as verified.

## Feeds

Each feed was selected from the publisher's own release page. Requests use
HTTPS, a 20 second timeout, at most 5 redirects, and a 2 MiB body cap. Redirects
must stay on the feed's host allowlist. Archive suffixes and archive content
types are refused before the body is accepted.
Interrupted HTTP reads become component failures, so the JSON report still
records the other feeds. When `GITHUB_TOKEN` is present, only the GitHub API
request receives it. CI supplies its automatic read-only token to avoid the
anonymous API rate limit; no repository secret needs configuring.

| Component | Stable release | Confirmation | Ignored |
| --- | --- | --- | --- |
| IANA | Latest-version link on <https://www.iana.org/time-zones> | Release page links `https://data.iana.org/time-zones/releases/tzdataYYYY[a-z].tar.gz` and is marked `LATEST` | Tokens such as `2026g-pre1` |
| zlib | `Current release` on <https://zlib.net/>, including its `zlib-X.Y.Z.tar.gz` link | First stable `Changes in X.Y.Z` line in <https://zlib.net/ChangeLog.txt> | `alpha`, `beta`, `rc`, and `pre` entries |
| CLDR | Highest numbered stable row on <https://cldr.unicode.org/index/downloads>, skipping `Latest` and `Dev` | Tag commit from `https://api.github.com/repos/unicode-org/cldr/commits/<tag>` | Tags containing `alpha`, `beta`, `rc`, `pre`, or `dev` |
| Unicode | `final data files` version in <https://www.unicode.org/Public/UCD/latest/ReadMe.txt> | <https://www.unicode.org/versions/UnicodeX.Y.Z/> over HTTPS, marked as superseding earlier versions | `X.Y.Z-beta` and similar. The version page's STATUS blockquote is boilerplate on released pages, including 17.0.0, and is not treated as a prerelease. |

`https://www.unicode.org/versions/latest/` is not a feed. That URL was observed
redirecting to plain HTTP. The versioned HTTPS announcement page is used instead.
A prerelease is reported under `ignoredPrereleases` and does not by itself make
the result `update-available`. A page that offers only a prerelease is a failed
check, not a current pin.

## Result policy

The top-level `result` is `check-failed` whenever any component could not be
read, even if another component found a newer release. An available update is
reported only when every component was read successfully.

| `result` | Process exit | CI |
| --- | --- | --- |
| `current` | 0 | Success. No newer stable release. |
| `update-available` | 2 | Warning, then success. The report artifact lists the newer release or drifted CLDR commit. Pins are not changed. |
| `check-failed` | 1 | Failure. Network, timeout, malformed feed, disagreeing zlib pages, an upstream version older than the pin, or an unreadable local record. This is not an available-update result. |

`.github/workflows/standard-library-updates.yml` runs the offline tests, then
the read-only online check, on pull requests and main pushes that touch this
checker or a pin, on manual dispatch, and every Monday at 12:17 UTC. It has
`contents: read` and uploads `standard-library-updates.json`. It uses no
custom secrets and does not create issues or pull requests.


Offline fixtures model the current Unicode 18 pin and a synthetic Unicode 19
release and prerelease. The Unicode 19 fixtures are test inputs, not claims that
such a release exists. Historical Unicode 17 announcement boilerplate remains
covered alongside the Unicode 18 announcement.

Exit 0 means the pins match the newest stable releases. Exit 2 from the second
command means a newer stable release was listed. Exit 1 means the check did not
complete. The source records are still unchanged. Updating TimeZoneKit, CLDR mappings,
CompressionKit, or NormalizationKit remains a separate reviewed change to those
records and their generated tables.

Test suites and fixtures are maintainer-local and are not part of this public repository.
