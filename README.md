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
| Build pipeline, `/stream/` redirect, `/m3u/`, `/xmltv/` endpoints | done, tested end to end |
| HDHomeRun emulation (discover, lineup, device XML; XEPG and PMS modes) | done, golden-tested against Go (29 cases) |
| Authentication (users, `/m3u/` + `/xmltv/` URL credentials, `/lineup.json` Basic auth) | done, golden-tested against Go (14 step-by-step cases); web/API login comes with the UI |
| Backup / restore, scheduler, start-up update | done, golden-tested against Go (32 cases); restore is checked before anything is written; download/upload come with the UI |
| SSDP discovery | dropped (add the tuner in Plex / Emby / Jellyfin by URL) |
| REST API + HTMX UI | not started |

## Tests

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/python -m unittest discover -s tests

Needs Python 3.12+ and (for one integration test) `ffmpeg`/`ffprobe`. Golden files in
`tests/golden/` were produced by the real Go code; regenerate them with the scripts in
`tools/go_golden/` (needs Go and network access). See `docs/PARITY_NOTES.md` for the
Go quirks that were kept or fixed on purpose, and `CLAUDE.md` for the development method.

## Roadmap

1. REST API + HTMX UI, including the probe UI (inventory: `webserver.go` WebSocket
   commands, `webUI.go`, `html/`), with web login (cookie `Token`), the create-first-user
   page, API token auth and user management on top of `auth.py`, backup download
   (`create_backup`, Go served it from `/download/`) and restore upload (`Service.restore`). Add `jinja2` (and `python-multipart` if forms post)
   to `pyproject.toml`.
2. Image cache (Go: `images.go`, `internal/imgcache`): only an identity hook exists so far.
3. Entry point: no CLI, `__main__` or `[project.scripts]` yet to start the server
   (uvicorn on `web.create_app`), including Go's `-restore <archive>` (confirm, then
   `backup.restore_backup`).

## Open questions

- Live-event (PPV) start times never worked in Go (always 06:00 if the name contains a
  digit, else midnight). Implement the intended date / time / timezone parsing?
- Backup channels cannot fail over with a plain 302; they are kept in the registry only.
