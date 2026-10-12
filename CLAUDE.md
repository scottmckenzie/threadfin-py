# threadfin-py

Python 3.12+ port of [Threadfin](https://github.com/Threadfin/Threadfin) (Go IPTV M3U proxy / EPG
mapper). Status and roadmap are in `README.md`; every Go behaviour that was kept, fixed or changed is
in `docs/PARITY_NOTES.md`.

## Setup and tests

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/python -W error -X dev -m unittest discover -s tests

`.venv/bin/pytest` also works. Runtime dependencies are pydantic, starlette and uvicorn. Tests use
`unittest` and providers use stdlib `urllib`; keep it that way unless there is a reason to add a
dependency. One integration test needs `ffmpeg`/`ffprobe`.

## Scope decisions

- No stream buffer: `/stream/<id>` is an HTTP 302 to the provider URL.
- No WebSocket UI: REST API + Jinja2/HTMX server-rendered pages. The Go WebSocket commands
  (`webserver.go`) are the feature inventory.
- Keep the ffprobe channel probe and store its results.
- Keep the existing JSON config formats (`~/.threadfin` compatible), including settings that no longer
  do anything (e.g. `ssdp`).
- Providers are http(s) URLs only (no local-file sources).
- No SSDP discovery.
- Unnamed channels are skipped and reported; apostrophe and double-quote name bugs are fixed.

## Method: golden-file parity

Go harnesses in `tools/go_golden/` (`zz_*.go`, injected into a copy of the Go `src`, run with
`GOFLAGS=-mod=mod GOPROXY=direct GOSUMDB=off`) generate goldens from the real Go code into
`tests/golden/<area>/`. Regenerate with `tools/go_golden/run_<area>_golden.sh /path/to/Threadfin`
(needs Go and network access; on current macOS use Go 1.24+ with `GOTOOLCHAIN=local`, since
Go 1.22 binaries abort with "missing LC_UUID"). The Go `authentication` package keeps state in
globals, so `authdump` runs each case in its own process.

Python tests compare against the goldens; deliberate divergences are listed explicitly in the tests
and in `docs/PARITY_NOTES.md`.

When porting a new area: add a Go harness and goldens first, then the Python code and tests, then
record kept/fixed/changed behaviour in `docs/PARITY_NOTES.md` and update the status table in
`README.md`. After writing code, mutation-check it (patch a source string, run the tests, restore in
`finally`) to confirm the tests actually catch changes.

## Gotchas

- Go map iteration is random: never put order-dependent output in goldens (use at most one filter
  in random cases, unique channel numbers, compare sets).
- `build_xepg` replaces channel objects with copies: read from the returned dict/result.
- Go `%g` formatting, per-character `ToLower` and strict JSON typing are reproduced in `xepg.go_g`,
  `filters.go_lower` and `settings`.
