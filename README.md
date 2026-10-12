# threadfin-py

Python port of [Threadfin](https://github.com/Threadfin/Threadfin) (M3U proxy and EPG
mapper for Plex / Emby / Jellyfin). **Work in progress**: the data layer is done.

Scope decisions: no stream buffer (`/stream/` will redirect to the provider URL),
no WebSocket UI (REST API + server-rendered HTMX pages instead), channel probe kept
and extended with stored results. Existing `~/.threadfin` config folders stay compatible.

| Layer | Status |
|---|---|
| M3U parser | done, golden- and differential-tested against Go; skipped entries are reported |
| Config / settings / system paths | done, golden-tested against Go |
| Channel probe + stored results | done |
| Stream filters | done, golden-tested against Go (70 cases) |
| Providers (URL / file / HDHomeRun fetch, gzip, local copies, counters) | done, tested against a local HTTP server |
| Stream database (active / inactive, previews, groups, compatibility) | done, golden-tested against Go (30 cases) |
| XMLTV channel mapping, channel database (XEPG) | done, golden-tested against Go (69 cases) |
| XMLTV guide output (provider, dummy and live-event guides) | done, golden-tested against Go (50 cases) |
| M3U playlist output + stream id registry | done, golden-tested against Go (39 cases) |
| Orchestration, `/stream/` redirect, HDHR emulation, SSDP, auth, backup | not started |
| REST API + HTMX UI | not started |

## Tests

    PYTHONPATH=src python3 -m unittest discover -s tests

Needs Python 3.12+ and (for one integration test) `ffmpeg`/`ffprobe`. Golden files in
`tests/golden/` were produced by the real Go code; regenerate them with the scripts in
`tools/go_golden/` (needs Go and network access). See `docs/PARITY_NOTES.md` for the
Go quirks that were kept or fixed on purpose.
