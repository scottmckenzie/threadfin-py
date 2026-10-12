# threadfin-py

Python port of [Threadfin](https://github.com/Threadfin/Threadfin) (M3U proxy and EPG
mapper for Plex / Emby / Jellyfin). **Work in progress**: everything except a command to start
the server is done (see the roadmap).

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
| Authentication (users, `/m3u/` + `/xmltv/` URL credentials, `/lineup.json` Basic auth, web / API login) | done, golden-tested against Go (14 step-by-step cases) |
| Backup / restore, scheduler, start-up update | done, golden-tested against Go (32 cases); restore is checked before anything is written |
| SSDP discovery | dropped (add the tuner in Plex / Emby / Jellyfin by URL) |
| Web UI (`/web/`: status, playlists, XMLTV, filters, mapping with probe, users, settings, backup / restore, log) | done, htmx, tested through the app |
| REST API (`/api/v1/`), Go's `/api/`, `/ppv/`, `/data_images/` | done; settings and filter saving golden-tested against Go (56 cases) |
| Image cache (`cache.images`, `/images/`) | done; URL lookups golden-tested against Go (8 cases, 14 URLs each), downloads in the background |

## REST API

JSON under `/api/v1/`; errors are `{"error": "..."}` with a 4xx status. With web authentication on,
send `Authorization: Bearer <token>` from `POST /api/v1/login {"username", "password"}` (the user
needs web access). A `POST` must have a JSON body or an `X-Requested-With` header (CSRF guard).

| Endpoint | |
|---|---|
| `GET /status`, `GET` / `DELETE /log`, `POST /update` (download + build), `POST /build` | status, log, updates |
| `GET` / `PATCH /settings` | settings (the settings-page keys; unknown keys are refused) |
| `GET` / `POST /providers/{m3u,hdhr,xmltv}`, `GET` / `PATCH` / `DELETE /providers/{kind}/{id}`, `POST .../refresh` | playlists, tuners, guides |
| `GET` / `POST /filters`, `PATCH` / `DELETE /filters/{id}`, `GET /groups` | filters and M3U groups |
| `GET /channels[?active=&group=]`, `GET` / `PATCH /channels/{id}`, `POST /channels/bulk {"ids", "changes"}` | channel mapping |
| `POST /channels/{id}/probe[?force=1]`, `GET /channels/{id}/probes`, `GET /xmltv`, `POST /logos` | probe, guide channels, logo upload |
| `GET` / `POST /users`, `PATCH` / `DELETE /users/{id}` | users |
| `GET /backup`, `POST /restore` (multipart `file` or a zip body) | backup and restore |

Go's API (`POST /api/` with `{"cmd": "status" | "login" | "update.m3u" | ...}`, needs the API setting),
`/ppv/enable` / `/ppv/disable` and `/data_images/` work as before.

## Tests

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/python -m unittest discover -s tests

Needs Python 3.12+ and (for one integration test) `ffmpeg`/`ffprobe`. Golden files in
`tests/golden/` were produced by the real Go code; regenerate them with the scripts in
`tools/go_golden/` (needs Go and network access). See `docs/PARITY_NOTES.md` for the
Go quirks that were kept or fixed on purpose, and `CLAUDE.md` for the development method.

## Roadmap

1. Entry point: no CLI, `__main__` or `[project.scripts]` yet to start the server
   (uvicorn on `web.create_app`), including Go's `-restore <archive>` (confirm, then
   `backup.restore_backup`).

## Open questions

- Live-event (PPV) start times never worked in Go (always 06:00 if the name contains a
  digit, else midnight). Implement the intended date / time / timezone parsing?
- Backup channels cannot fail over with a plain 302; they are kept in the registry only.
