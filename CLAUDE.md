# threadfin-py

Python 3.12+ port of [Threadfin](https://github.com/Threadfin/Threadfin) (Go IPTV M3U proxy / EPG
mapper). Status and roadmap are in `README.md`; every Go behaviour that was kept, fixed or changed is
in `docs/PARITY_NOTES.md`.

## Python Version

Python 3.12 minimum (`requires-python` in pyproject.toml). Use modern syntax:
- `X | None` instead of `Optional[X]`
- `list[str]` instead of `List[str]`
- `dict[str, Any]` instead of `Dict[str, Any]`
- `tuple[int, str]` instead of `Tuple[int, str]`
- `type X = ...` for type aliases (Python 3.12)
- Match statements for enum/type dispatch where appropriate

## Manage packages

This project uses uv. Do not use pip, pip-tools, poetry, or conda.

- Add runtime dependency: `uv add <package>` (writes to `[project.dependencies]`)
- Add dev dependency: `uv add --dev <package>` (writes to `[dependency-groups]` per PEP 735)
- Remove dependency: `uv remove <package>`
- Sync environment from lockfile: `uv sync`
- Regenerate lockfile from constraints: `uv lock`
- Upgrade locked versions: `uv lock --upgrade`
- Commit `uv.lock` to version control (current uv guidance is to commit it for applications, CLIs, and libraries)

## Run code

Always use `uv run` to execute Python code and tools. Never call `python`, `pytest`, `ruff`, or other tools directly. They may not resolve to the project's virtual environment.

- Run a script: `uv run python script.py`
- Run a module: `uv run python -m module_name`
- Run a tool: `uv run pytest`, `uv run ruff check .`
- One-off tool (not a project dependency): `uvx <tool>`

## Test code

- Framework: pytest
- Run tests: `uv run pytest`
- Test files go in `tests/` at the project root
- Test file naming: `test_*.py`
- Test function naming: `test_*`
- No `__init__.py` needed in `tests/`
- Coverage: `uv add --dev coverage`, then `uv run coverage run -m pytest` and `uv run coverage report`

## Lint and format code

- Tool: ruff (handles both linting and formatting)
- Lint: `uv run ruff check .`
- Lint and auto-fix: `uv run ruff check --fix .`
- Format: `uv run ruff format .`
- Check formatting: `uv run ruff format --check .`
- Configuration lives in `pyproject.toml` under `[tool.ruff]`

## Check types

- Tool: pyrefly (migrated from mypy; ty is the alternative)
- Run: `uv run pyrefly check` (checks `src/`; must report 0 diagnostics, warnings included)
- Configuration lives in `pyproject.toml` under `[tool.pyrefly]`: the `strict` preset plus the
  stricter checks mypy's strict mode had. Not run in CI (decided 2026-10-16)

## Avoid these practices

- Do not create or activate virtual environments manually. uv manages `.venv/` automatically.
- Do not install packages globally or with `pip install`.
- Do not create `requirements.txt` for dependency management. Use `pyproject.toml` and `uv.lock`.
- Do not run `python setup.py` commands.
- Do not add dependencies to pyproject.toml by hand. Use `uv add`.
- If you must edit pyproject.toml directly, write dev dependencies under `[dependency-groups]` (PEP 735), not the legacy `[tool.uv.dev-dependencies]` table.

## Development Standards

### Git Workflow
- Branch names: `feature/description` or `fix/description`
- Commit messages: Follow conventional commits
- PR required before merge
- All CI/CD checks must pass
- Minimum 1 approval required

### Testing Requirements
- Minimum 80% code coverage
- All critical paths must have tests

### API Standards
- RESTful endpoints only
- JSON request/response
- Use HTTP status codes correctly
- Version API endpoints: `/api/v1/`
- Document all endpoints with examples

## Setup and tests

    uv sync                                  # .venv from pyproject.toml + uv.lock (Python 3.12)
    PYTHONDEVMODE=1 uv run pytest            # warnings are errors (pyproject.toml)
    uv run ruff check                        # rules in pyproject.toml; must pass
    uv run ruff format                       # `# fmt: skip` keeps hand-aligned tables
    uv run pyrefly check                     # types (src/); not run in CI
    uv run coverage run -m pytest && uv run coverage report   # fails below 95% (lines + branches)

CI (`.github/workflows/ci.yml`) runs the same lint, format, test and coverage checks on every pull
request and push to `main`, with tests on Python 3.12 and 3.14 and `ffmpeg` installed.

A commit that only reformats goes in `.git-blame-ignore-revs` (once it is on `main`).

Runtime dependencies are pydantic, starlette, uvicorn, jinja2 and python-multipart. Tests are 
`unittest.TestCase` classes run by pytest (web tests use Starlette's `TestClient`, which needs
`httpx2`; shared set-up in `tests/webfixture.py`, on pytest's `pythonpath`) and providers use
stdlib `urllib`; keep it that way unless there is a reason to add a dependency. One integration test
needs `ffmpeg`/`ffprobe`. htmx is vendored in `src/threadfin/static/` (no CDN: the UI must work offline).

## Scope decisions

- No stream buffer: `/stream/<id>` is an HTTP 302 to the provider URL.
- No WebSocket UI: REST API + Jinja2/HTMX server-rendered pages. The Go WebSocket commands
  (`webserver.go`) are the feature inventory.
- Keep the ffprobe channel probe and store its results.
- Keep the existing JSON config formats (`~/.threadfin` compatible), including settings that no longer
  do anything (e.g. `ssdp`).
- Passwords are stored as scrypt hashes; Go's password hashes are accepted and replaced at the next
  successful login, after which the Go app cannot log that user in.
- Providers are http(s) URLs only (no local-file sources).
- No SSDP discovery.
- No HDHomeRun tuners as a channel source (`files.hdhr` is kept in the config but ignored). The
  HDHomeRun *emulation* that Plex / Emby / Jellyfin talk to stays.
- No binary self-update (`ThreadfinAutoUpdate` is kept in the config but ignored).
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
