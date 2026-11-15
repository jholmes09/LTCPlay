# Releasing LTC Player

A release is a tag named `ltc-YYYY.MM.DD.N` (for example `ltc-2026.11.13.1`) on `main`. The date is the day you tag; `N` counts releases that day. Do not use a 1.0 style number: the updater compares the dated number as a number.

1. The release PR (bench-build into main) is merged and its `ci` check was green.
2. On GitHub: Releases, Draft a new release.
3. Tag: `ltc-YYYY.MM.DD.N`. Target: `main`.
4. Title: `LTC Player YYYY.MM.DD.N`.
5. Untick "Set as a pre-release". Tick "Set as the latest release".
6. Publish. The windows-app workflow builds the installer and attaches it to that release.

A `gpl-` tag is refused on purpose. Bench builds (pushes to bench-build) are published as "BENCH ONLY" pre-releases and never reach a show PC.
