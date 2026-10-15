# threadfin-py

Python port of [Threadfin](https://github.com/Threadfin/Threadfin) (M3U proxy and EPG
mapper for Plex / Emby / Jellyfin). Every part of the Go app in scope is ported; see the open
questions below.

Scope decisions: no stream buffer (`/stream/` redirects to the provider URL),
no WebSocket UI (REST API + server-rendered HTMX pages instead), channel probe kept
and extended with stored results. Existing `~/.threadfin` config folders stay compatible.

| Layer | Status |
|---|---|
| M3U parser | done, golden- and differential-tested against Go; skipped entries are reported |
| Config / settings / system paths | done, golden-tested against Go |
| Channel probe + stored results | done |
| Stream filters | done, golden-tested against Go (70 cases) |
| Providers (M3U and XMLTV URLs, gzip, local copies, counters) | done, tested against a local HTTP server |
| Stream database (active / inactive, previews, groups, compatibility) | done, golden-tested against Go (30 cases) |
| XMLTV channel mapping, channel database (XEPG) | done, golden-tested against Go (69 cases) |
| XMLTV guide output (provider, dummy and live-event guides) | done, golden-tested against Go (50 cases); live-event start times are read from the channel name (Go never managed to) |
| M3U playlist output + stream id registry | done, golden-tested against Go (39 cases) |
| Build pipeline, `/stream/` redirect, `/m3u/`, `/xmltv/` endpoints | done, tested end to end |
| HDHomeRun emulation (discover, lineup, device XML; XEPG and PMS modes) | done, golden-tested against Go (29 cases) |
| Authentication (users, `/m3u/` + `/xmltv/` URL credentials, `/lineup.json` Basic auth, web / API login) | done, golden-tested against Go (14 step-by-step cases) |
| Backup / restore, scheduler, start-up update | done, golden-tested against Go (32 cases); restore is checked before anything is written |
| SSDP discovery | dropped (add the tuner in Plex / Emby / Jellyfin by URL) |
| HDHomeRun tuners as a channel source | dropped (`files.hdhr` is kept in settings.json but ignored) |
| Web UI (`/web/`: status, playlists, XMLTV, filters, mapping with probe, users, settings, backup / restore, log) | done, htmx, tested through the app |
| REST API (`/api/v1/`), Go's `/api/`, `/ppv/`, `/data_images/` | done; settings and filter saving golden-tested against Go (56 cases) |
| `threadfin` command (start, `-restore`, `-info`) | done, tested with a real server process |
| Image cache (`cache.images`, `/images/`) | done; URL lookups golden-tested against Go (8 cases, 14 URLs each), downloads in the background |

## Running

    uv tool install .        # after an update: uv tool install --reinstall .
    threadfin                # or, from the checkout: uv run threadfin

Then open `http://<this machine>:34400/web/`, add a playlist (and, for the XMLTV mode, a guide),
and add the tuner in Plex / Emby / Jellyfin as `http://<this machine>:34400`. An existing
`~/.threadfin` folder from the Go app is used as it is. Passwords are stored as scrypt hashes: a
user's Go hash is replaced at their next login here, and the Go app can then no longer log that
user in.

| Option | |
|---|---|
| `-config DIR` | config folder (default `~/.threadfin`) |
| `-port PORT` | server port, saved to settings.json (default 34400) |
| `-bind IP` | listen address (default: the `bindIpAddress` setting, else all interfaces) |
| `-restore FILE` | restore a backup (asks first), then exit |
| `-info` | show the configuration, then exit |
| `-debug 0-3` | more logging; 2 and up logs every request |

Go's single-dash spelling and `--config` etc. both work. Stop the server with Ctrl+C or SIGTERM.

The web UI and the APIs answer only host names that cannot be used for DNS rebinding: IP addresses,
single-label names (`http://nas:34400`), `.local`, `.lan`, `.home.arpa`, `.internal` and
`.localhost` names, and the `httpThreadfinDomain` / `httpsThreadfinDomain` settings. To use another
name (for example through a reverse proxy), list it in the `THREADFIN_ALLOWED_HOSTS` environment
variable (comma-separated; `*` allows any name). Player and Plex / Emby / Jellyfin endpoints answer
any name.

## REST API

JSON under `/api/v1/`; errors are `{"error": "..."}` with a 4xx status. With web authentication on,
send `Authorization: Bearer <token>` from `POST /api/v1/login {"username", "password"}` (the user
needs web access). A `POST` must have a JSON body or an `X-Requested-With` header (CSRF guard).
After 10 failed logins within 15 minutes, an address gets `429` (with `Retry-After`) on every
login and credential check until the oldest failure is 15 minutes old.

| Endpoint | |
|---|---|
| `GET /status`, `GET` / `DELETE /log`, `POST /update` (download + build), `POST /build` | status, log, updates |
| `GET` / `PATCH /settings` | settings (the settings-page keys; unknown keys are refused) |
| `GET` / `POST /providers/{m3u,xmltv}`, `GET` / `PATCH` / `DELETE /providers/{kind}/{id}`, `POST .../refresh` | playlists, guides |
| `GET` / `POST /filters`, `PATCH` / `DELETE /filters/{id}`, `GET /groups` | filters and M3U groups |
| `GET /channels[?active=&group=]`, `GET` / `PATCH /channels/{id}`, `POST /channels/bulk {"ids", "changes"}` | channel mapping |
| `POST /channels/{id}/probe[?force=1]`, `GET /channels/{id}/probes`, `GET /xmltv`, `POST /logos` | probe, guide channels, logo upload |
| `GET` / `POST /users`, `PATCH` / `DELETE /users/{id}` | users |
| `GET /backup`, `POST /restore` (multipart `file` or a zip body) | backup and restore |

Go's API (`POST /api/` with `{"cmd": "status" | "login" | "update.m3u" | ...}`, needs the API setting),
`/ppv/enable` / `/ppv/disable` and `/data_images/` work as before.

## Tests

    uv sync
    uv run pytest

Needs Python 3.12+ and (for one integration test) `ffmpeg`/`ffprobe`. Golden files in
`tests/golden/` were produced by the real Go code; regenerate them with the scripts in
`tools/go_golden/` (needs Go and network access). See `docs/PARITY_NOTES.md` for the
Go quirks that were kept or fixed on purpose, and `CLAUDE.md` for the development method.

## Open questions

- Backup channels cannot fail over with a plain 302; they are kept in the registry only.
- Code review findings and recommended fixes (login throttling, password hashing, type errors ...):
  see `docs/REVIEW_2026-10.md`.
