# Backup Channel Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When one client requests the same `/stream/<id>` N times within W seconds (default 3 in 5), redirect that client to the channel's next backup channel.

**Architecture:** Revert PR #16 so backups are resolved at build time and stored in `urls.json` again. A new in-memory `Failover` tracker (own module, owned by `Service`) counts requests per (client, stream id) and returns which step to serve (0 = main, 1..3 = backup). `Service.stream_target` picks the URL for that step and logs switches; `/stream/` passes the client address in. Two new settings control N and W.

**Tech Stack:** Python 3.12, Starlette, uvicorn, pydantic, unittest run by pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-17-backup-channel-failover-design.md`

## Global Constraints

- Run everything with `uv run`; tests with `PYTHONDEVMODE=1 uv run pytest` (warnings are errors).
- Must stay clean: `uv run ruff check`, `uv run ruff format --check`, `uv run pyrefly check --min-severity=warn` (0 diagnostics), coverage ≥ 95% (`uv run coverage run -m pytest && uv run coverage report`).
- `/stream/<id>` stays an HTTP 302; no proxying, no probing provider URLs.
- Setting keys exactly `backup.failover.requests` (int, default 3) and `backup.failover.seconds` (int, default 5).
- `backup.failover.requests`: 0 = off, otherwise ≥ 2. `backup.failover.seconds`: ≥ 1.
- Client = request IP (`access.client_address`), port ignored. State in memory only.
- Conventional commits; end each commit message with the session's `Co-Authored-By` / `Claude-Session` lines.
- Tests use an injectable clock, never `sleep`.

## Review Focus

1. A healthy player that requests a channel once, then again 6 s later, must stay on main (requests spread out never switch).
2. `settings.json` edited by hand to `backup.failover.requests: 1` or `seconds: 0` must not switch on every request or crash: treat requests < 2 as off and seconds < 1 as 1.
3. A rebuild that removes a backup while a client is on backup 2: the client gets the last usable backup, not an `IndexError`.
4. Two clients on the same channel are independent: client A's retries never move client B.
5. A backup with a `udp://@` URL gets the same UDPxy rewrite as the main stream.

Each has a test in the task that owns the code (Tasks 2, 3 and 4).

---

### Task 1: Bring back backup channels (revert PR #16)

**Files:**
- Modify (by revert): `src/threadfin/xepg.py`, `src/threadfin/playlist.py`, `src/threadfin/admin.py`, `src/threadfin/ui.py`, `src/threadfin/ui_text.py`, `src/threadfin/templates/partials/channel_form.html`, `tests/test_xepg.py`, `tests/test_playlist.py`, `tests/test_api.py`, `tests/test_ui.py`, `CLAUDE.md`, `README.md`, `docs/PARITY_NOTES.md`

**Interfaces:**
- Produces: `xepg.map_channels(channels, streams, *, filters, xmltv_mapping)` resolving `x-backup-channel-1/2/3` to `BackupStream(playlist_id, url)`; `StreamRegistry.add(playlist_id, number, name, url, backups=(None, None, None), *, keep=False)`; `StreamInfo.backup_channel_1/2/3: BackupStream | None` filled from the channel.

- [ ] **Step 1: Revert**

Run: `git revert --no-edit 3b3c692`
Expected: no conflicts (checked on `29a3afb`).

- [ ] **Step 2: Run the suite**

Run: `PYTHONDEVMODE=1 uv run pytest -q`
Expected: all pass. If a test written after PR #16 assumes no backups (e.g. `tests/test_playlist.py` channel-order test passes `StreamRegistry()` and does not care), adjust that test, not the restored code.

- [ ] **Step 3: Amend the commit message**

`git commit --amend` with the title `feat: bring back backup channels for request-based failover` and a body saying the revert restores name resolution, `urls.json` backups, the editor fields and API acceptance, and that the docs are rewritten in Task 5. The docs the revert restores still describe Go's stream buffer; leave them for Task 5.

---

### Task 2: Failover settings

**Files:**
- Modify: `src/threadfin/settings.py` (append after `ignore_filters`), `src/threadfin/admin.py` (`SETTABLE`, `update_settings`), `src/threadfin/ui_text.py` (`SETTINGS_PAGE`)
- Test: `tests/test_settings.py`, `tests/test_admin.py`, `tests/test_ui.py`

**Interfaces:**
- Produces: `Settings.failover_requests: int` (alias `backup.failover.requests`, default 3), `Settings.failover_seconds: int` (alias `backup.failover.seconds`, default 5).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings.py
def test_failover_defaults_and_round_trip(self) -> None:
    s = Settings()
    self.assertEqual((s.failover_requests, s.failover_seconds), (3, 5))
    data = s.to_json_dict()
    self.assertEqual((data["backup.failover.requests"], data["backup.failover.seconds"]), (3, 5))

# tests/test_admin.py
def test_failover_settings_validated(self) -> None:
    new, changed = update_settings(Settings(), {"backup.failover.requests": 4, "backup.failover.seconds": 10})
    self.assertEqual((new.failover_requests, new.failover_seconds), (4, 10))
    self.assertEqual(changed, {"backup.failover.requests", "backup.failover.seconds"})
    self.assertEqual(update_settings(Settings(), {"backup.failover.requests": 0})[0].failover_requests, 0)
    for change in ({"backup.failover.requests": 1}, {"backup.failover.requests": -1},
                   {"backup.failover.seconds": 0}, {"backup.failover.requests": "3"}):  # fmt: skip
        with self.assertRaises(AdminError):
            update_settings(Settings(), change)

# tests/test_ui.py: the settings page shows both fields
self.assertIn('name="backup.failover.requests"', page)
self.assertIn('name="backup.failover.seconds"', page)
```

Use the existing classes and fixtures in each file.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_settings.py tests/test_admin.py tests/test_ui.py -q`
Expected: the three new tests FAIL (attribute / unknown setting / missing field).

- [ ] **Step 3: Implement**

- `settings.py`: two `int` fields with the aliases and defaults above.
- `admin.py`: both keys `"int"` in `SETTABLE`; after the loop in `update_settings`, raise `AdminError("backup.failover.requests: 0 (off) or at least 2")` and `AdminError("backup.failover.seconds: at least 1")`. Not in `REBUILD_KEYS`.
- `ui_text.py`: a new section `("Backup channels", (...))` after `"Backup"`, two `kind="int"` fields. Titles: "Failover requests", "Failover window (seconds)". Descriptions say: a client that requests the same channel this many times within the window is sent to the channel's next backup; 0 turns it off; behind a reverse proxy, see `FORWARDED_ALLOW_IPS` in the README.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_settings.py tests/test_admin.py tests/test_ui.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat: settings for backup channel failover`

---

### Task 3: The failover tracker

**Files:**
- Create: `src/threadfin/failover.py`
- Test: `tests/test_failover.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class Decision:
    step: int        # 0 = main stream, n = the n-th usable backup
    switched: bool   # this request moved the client on
    stuck: bool      # this request completed a burst with nowhere left to go (first time per burst)

class Failover:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None: ...
    def request(self, client: str, stream_id: str, *, backups: int, requests: int, seconds: int) -> Decision: ...
```

`backups` is the number of usable backups for this channel right now (0–3).

- [ ] **Step 1: Write the failing tests** in `tests/test_failover.py` with a fake clock (`now = [0.0]; Failover(clock=lambda: now[0])`), each calling `request("c", "s", backups=..., requests=3, seconds=5)` unless stated:

| Test | Requests at (s) | backups | Expected `(step, switched, stuck)` of the last call |
|---|---|---|---|
| `test_third_request_within_window_switches` | 0, 3, 4 | 2 | `(1, True, False)`; the first two return `(0, False, False)` |
| `test_spread_out_requests_never_switch` | 0, 6, 12, 18 | 2 | `(0, False, False)` for every call |
| `test_window_slides` | 0, 3, 6, 7 | 2 | 6 → step 0 (0 has expired); 7 → `(1, True, False)` |
| `test_moves_on_to_next_backup` | 0, 1, 2, 3, 4, 5 | 2 | 2 → step 1, 5 → `(2, True, False)` |
| `test_stays_on_last_backup` | 0..8, 1 s apart | 2 | 8 → `(2, False, True)`; later calls in the same burst `stuck=False` |
| `test_no_backups_stays_on_main` | 0, 1, 2 | 0 | `(0, False, True)` |
| `test_quiet_gap_resets_to_main` | 0, 1, 2, then 7.5 | 2 | 2 → step 1; 7.5 → `(0, False, False)` |
| `test_clients_are_independent` | "a" at 0, 1, 2; "b" at 2 | 2 | a → step 1; b → step 0 |
| `test_streams_are_independent` | "s1" at 0, 1, 2; "s2" at 2 | 2 | s2 → step 0 |
| `test_backups_shrink_after_rebuild` | 0..5 with backups=2 (step 2), then 6 with backups=1 | — | `(1, False, False)` |
| `test_off_and_bad_values` | 0, 0.1, 0.2, 0.3 | 2 | `requests=0` and `requests=1` → always step 0; `seconds=0` behaves as `seconds=1` |
| `test_idle_state_is_pruned` | 1000 clients once at 0, one more at 10 | 2 | internal state holds 1 entry |

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_failover.py -q`
Expected: FAIL, `ModuleNotFoundError: threadfin.failover`.

- [ ] **Step 3: Implement `Failover` in `src/threadfin/failover.py`**

Per (client, stream id): `step`, the recent request times, last request time, `warned`. On each call: prune every entry idle for ≥ `seconds` (this is also the reset to main); clamp `step` to `backups`; drop times older than `seconds`; append now; when `len(times) >= requests` clear times and either step on (`switched`) or, if `step == backups`, report `stuck` once (`warned`). `requests < 2` → return step 0 without recording; `seconds < 1` → use 1. Module docstring: what it detects and why (redirects cannot see a failed stream; players retry), one paragraph.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_failover.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat: track repeated stream requests per client`

---

### Task 4: Redirect to the backup

**Files:**
- Modify: `src/threadfin/service.py` (`__init__`, `stream_target`), `src/threadfin/web.py` (`stream` handler, module docstring)
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `Failover`, `Decision` (Task 3); `Settings.failover_requests/seconds` (Task 2); `StreamInfo.backup_channel_1/2/3` (Task 1).
- Produces: `Service.failover: Failover` (public so tests can swap in a fake clock); `Service.stream_target(stream_id: str, client: str = "") -> str | None`.

- [ ] **Step 1: Write the failing tests** in `tests/test_service.py`, a new class `Failover(Base)` with set-up that gives channel Alpha (`http://s/1`) backups `x-backup-channel-1 = "Gamma"` (`http://s/3`) and `x-backup-channel-2 = "Beta"` (`http://s/2`), rebuilds, and sets `self.service.failover = Failover(clock=lambda: self.now)`:

```python
async def test_retries_move_to_backups(self) -> None:
    uid = <Alpha's stream id from the registry>
    targets = [self.at(t, uid) for t in (0, 1, 2, 3, 4, 5, 6, 7, 8)]   # at(): set clock, call stream_target(uid, "10.0.0.1")
    self.assertEqual(targets, ["http://s/1"] * 2 + ["http://s/3"] * 3 + ["http://s/2"] * 4)

async def test_switch_is_logged(self) -> None:
    with self.assertLogs("threadfin", "INFO") as logs:
        for t in (0, 1, 2): self.at(t, uid)
    self.assertIn("10.0.0.1: 3 requests in 5s, switching to backup 1 (Gamma", "\n".join(logs.output))

async def test_no_backup_left_is_logged_once(self) -> None:
    # Beta has no backups: 6 quick requests -> one "no backup" line
    ...

async def test_other_client_and_endpoint(self) -> None:
    # 3 quick GETs on /stream/<uid> over HTTP (client 127.0.0.1) -> third Location is http://s/3;
    # stream_target(uid, "10.0.0.2") at the same time -> http://s/1
    ...

async def test_backup_gets_udpxy_rewrite(self) -> None:
    # settings.udpxy = "proxy:4022", Gamma's registry backup URL udp://@239.0.0.1:1234
    # -> after 3 quick requests: http://proxy:4022/udp/239.0.0.1:1234/
    ...

async def test_off(self) -> None:
    self.settings.failover_requests = 0
    # 5 quick requests -> always http://s/1
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_service.py -q -k Failover`
Expected: FAIL (no `Service.failover`, `stream_target` takes one argument).

- [ ] **Step 3: Implement**

- `Service.__init__`: `self.failover = Failover()`.
- `stream_target(stream_id, client="")`: usable backups = `[b for b in (info.backup_channel_1, info.backup_channel_2, info.backup_channel_3) if b and b.url]`; ask `self.failover.request(client, stream_id, backups=len(usable), requests=self.settings.failover_requests, seconds=self.settings.failover_seconds)`; pick the main URL or `usable[step - 1].url`; apply the existing UDPxy rewrite to whichever URL is chosen. Log via the module logger (it is under `threadfin`) at INFO:
  - switched: `stream {id[:8]}… ({info.name}) for {client}: {N} requests in {W}s, switching to backup {step} ({backup name})` — the backup name is the channel's `x-backup-channel-<n>` text; look it up from `self.channels` by the stream's channel, or store the name alongside `BackupStream` if that is simpler (builder's choice, keep `urls.json` format unchanged).
  - stuck: `stream {id[:8]}… ({info.name}) for {client}: {N} requests in {W}s, no backup left to switch to`.
- `web.py` `stream`: `service.stream_target(request.path_params["stream_id"], client_address(request))`. Update the docstring line for `/stream/<id>` to mention the failover.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_service.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat: redirect repeated stream requests to the next backup channel`

---

### Task 5: Docs, full checks, mutation check

**Files:**
- Modify: `CLAUDE.md` (scope decisions), `README.md` (Running section and status table), `docs/PARITY_NOTES.md`, docstrings in `src/threadfin/xepg.py` and `src/threadfin/playlist.py` if the revert left them describing Go's stream buffer.

- [ ] **Step 1: Update the docs**

- `CLAUDE.md`: replace the restored "Keep backup channels…" / stream-buffer wording with: backup channels are used by request-based failover (N requests from one client within W seconds → next backup, see `failover.py`).
- `README.md`: in Running, after the `THREADFIN_ALLOWED_HOSTS` paragraph, a paragraph on reverse proxies: set `FORWARDED_ALLOW_IPS` to the proxy's address (e.g. `FORWARDED_ALLOW_IPS=192.0.2.1`; CIDR allowed) so failover, login throttling and the access log see the real client; list only the proxy, since any listed address can claim to be any client. Status table: backup channels → "changed: switched on repeated requests".
- `PARITY_NOTES.md`: backup channels row → **changed**: "Go switched inside its stream buffer; here N requests from one client within W seconds (default 3 in 5) redirect it to the next backup, staying on the last; a quiet gap of W returns it to the main stream". Update the playlist/xepg divergence counts the revert restored.

- [ ] **Step 2: Full checks**

Run each; all must pass:
`PYTHONDEVMODE=1 uv run pytest -q` · `uv run ruff check` · `uv run ruff format --check` · `uv run pyrefly check --min-severity=warn` · `uv run coverage run -m pytest && uv run coverage report`

- [ ] **Step 3: Mutation check** (CLAUDE.md requires it)

For each, patch the source, run `uv run pytest tests/test_failover.py tests/test_service.py -q -p no:cacheprovider` with `PYTHONDONTWRITEBYTECODE=1`, confirm a failure, restore in `finally`: (a) `>= requests` → `> requests`; (b) remove the idle prune; (c) skip the step clamp; (d) pass the main URL instead of the backup's to the UDPxy rewrite.

- [ ] **Step 4: Commit** — `docs: backup channel failover`
