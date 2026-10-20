# Hide URL Credentials and Limit Upload Sizes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop URL query strings and userinfo passwords from showing up in error messages and
logs, and refuse oversized requests and backup archives before they can exhaust memory or disk.

**Architecture:** A new `redact.py` turns a URL, or any text that contains URLs, into its
display form. `providers.py`, the request log filter, `imagecache.py` and the probe code all use
it. A `BodyLimit` ASGI middleware in `web.py` refuses oversized bodies with `413`. Both restore
endpoints stream their upload to a temp file. `restore_backup` checks sizes, free space and CRCs
before it writes anything, then streams each entry to disk.

**Tech Stack:** Python 3.12, Starlette (ASGI), uvicorn, stdlib `zipfile`, `logging`, `urllib.parse`, `re`, `shutil`.

**Spec:** `docs/superpowers/specs/2026-10-19-credentials-and-size-limits-design.md`

## Global Constraints

- Branch `fix/credentials-and-size-limits`. The spec is committed there as `21d0832`.
- Checks, all through uv: `PYTHONDEVMODE=1 uv run pytest`, `uv run ruff check`,
  `uv run ruff format --check`, `uv run pyrefly check --min-severity=warn` (0 diagnostics), and
  `uv run coverage run -m pytest && uv run coverage report` (at least 95%).
- No new dependencies. Tests are `unittest.TestCase` / `IsolatedAsyncioTestCase` classes, and
  web tests use `tests/webfixture.py`.
- How a URL is displayed: scheme, host, port and path, then `?…` if the URL had a query string.
  The fragment is dropped. A userinfo password becomes `***`, and the username stays. A URL with
  no query string, no fragment and no password comes back unchanged.
- Text that won't parse as a URL: everything from the first `?` becomes `?…`.
- A URL-shaped run in free text matches `[A-Za-z][A-Za-z0-9+.-]*://[^\s'"<>]+`.
- `MB` means 1024 × 1024 bytes. Limits: request body 8 MB (`MAX_BODY_BYTES`); restore upload
  64 MB (`MAX_RESTORE_BYTES`) on `POST /api/v1/restore` and `POST /web/restore`; total declared
  size of an archive's entries 256 MB (`MAX_RESTORE_TOTAL`); `settings.json` in an archive 8 MB
  (`MAX_SETTINGS_BYTES`).
- 413 text: `request body too large (limit 8 MB)` (`64 MB` on the restore paths). It is sent as
  JSON `{"error": …}` under `/api`, and as plain text everywhere else.
- Only how a URL is displayed changes. Fetching, the config files and the probe record's `url`
  field stay as they are.

## Review Focus

1. A redirect target with its own token in the error text. urllib follows redirects, so the
   text can name a URL we never passed in. Task 1 tests a 302 to `…?token=SECRET` that then 404s.
2. A chunked upload, with no `Content-Length`, must still stop at the limit. Task 4 tests it.
3. A probe error cut to 500 characters must not leave a query tail without its scheme. Task 3
   tests a URL at the start of a 600-character stderr.
4. A zip entry that declares a smaller size than its real data. zipfile raises `BadZipFile`, and
   that must become `BackupError` before any file is written. Task 6 tests it.
5. A real restore through the 8 MB general limit. The restore paths must use 64 MB, so a 9 MB
   archive restores through both endpoints. Task 5 tests it.

---

### Task 1: URL display helper, used by provider errors

**Files:**
- Create: `src/threadfin/redact.py`
- Modify: `src/threadfin/providers.py` (`download`, `_status_error`, `_fetch_source`)
- Test: `tests/test_redact.py` (new), `tests/test_providers.py`
- Docs: `docs/PARITY_NOTES.md` under "Providers"

**Interfaces:**
- Produces: `display_url(url: str) -> str` and `clean_text(text: str) -> str` in
  `threadfin.redact`. `clean_text` applies `display_url` to every URL-shaped run in `text`.

- [ ] **Step 1: Write the failing tests** in `tests/test_redact.py`:
  - `DisplayURL.test_query_is_hidden`:
    `display_url("http://provider.tv:8080/get.php?username=U&password=P&type=m3u_plus") == "http://provider.tv:8080/get.php?…"`
  - `test_unchanged_without_query`: `"http://host/nope"` and `"http://h/a%20b"` come back unchanged.
  - `test_fragment_dropped`: `"http://h/x#frag"` gives `"http://h/x"`, and `"http://h/x?a=1#f"` gives `"http://h/x?…"`.
  - `test_userinfo_password`: `"http://user:pass@host/x"` gives `"http://user:***@host/x"`, and
    `"http://user@host/x"` is unchanged.
  - `test_relative_path`: `"/m3u/threadfin.m3u?username=u&password=p"` gives `"/m3u/threadfin.m3u?…"`.
  - `test_unparseable`: `"http://[::1/x?password=p"` gives `"http://[::1/x?…"`. `""` and `"no url"` are unchanged.
  - `CleanText.test_every_url_in_text`:
    `clean_text("http://a/x?p=1: redirected to 'https://b/y?token=T'") == "http://a/x?…: redirected to 'https://b/y?…'"`
  - `test_text_without_urls_unchanged`: `clean_text("Connection refused")` returns `"Connection refused"`.

- [ ] **Step 2: Run them and check that they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_redact.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'threadfin.redact'`

- [ ] **Step 3: Implement `display_url` and `clean_text`** with `urlsplit` and `urlunsplit`.
  Keep the path exactly as it was given, without re-quoting. On `ValueError`, use the
  first-`?` fallback. Give the module a docstring that cites spec finding 3.

- [ ] **Step 4: Run them and check that they pass**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_redact.py -q`
Expected: PASS

- [ ] **Step 5: Write the failing provider tests** in `tests/test_providers.py` `DownloadTests`. If
  the test `Handler` has no suitable route, add a `/redir-secret` route that 302s to
  `/nope?token=SECRET`.
  - `test_errors_hide_query_strings`: each of `f"{self.base}/nope?password=SECRET"` (404),
    `f"{self.base}/slow?password=SECRET"` (`timeout=0.3`), `"http://127.0.0.1:9/x?password=SECRET"`
    (refused) and `f"{self.base}/redir-secret"` raises a `ProviderError` without `SECRET` in it.
    The first one's message equals `f"404: {self.base}/nope?… Not Found"`.
  - `test_invalid_and_unsupported_sources_hide_passwords`: `download("ftp://u:SECRET@h/x?k=SECRET")`
    raises `ProviderError`, and so does `await self.manager.add("m3u", {"file.source": "ftp://u:SECRET@h/x"})`.
    Neither message contains `SECRET`.
  - Keep `test_error_status_message_matches_go_format` unchanged.

- [ ] **Step 6: Run them and check that they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_providers.py -q -k "hide"`
Expected: FAIL, because `SECRET` is in the messages

- [ ] **Step 7: Apply the helpers in `providers.py`.** Use `display_url(url)` in the invalid-URI,
  status, timeout and size messages, and in the `unsupported source` message in
  `_fetch_source`. Keep `!r` where it is used now. Use `f"{display_url(url)}: {clean_text(str(exc))}"`
  in the connection-error fallback. Status errors keep showing only the URL that was requested,
  as they do now.

- [ ] **Step 8: Run the full suite**

Run: `PYTHONDEVMODE=1 uv run pytest -q`
Expected: all pass

- [ ] **Step 9: Add a note under "Providers" in `docs/PARITY_NOTES.md`.** Error messages show
  URLs without their query string or userinfo password. Go showed the full URL.

- [ ] **Step 10: Commit**

```bash
git add src/threadfin/redact.py src/threadfin/providers.py tests/test_redact.py tests/test_providers.py docs/PARITY_NOTES.md
git commit -m "fix: hide URL query strings and passwords in provider errors"
```

---

### Task 2: Request log and image cache log

**Files:**
- Modify: `src/threadfin/redact.py`, `src/threadfin/cli.py` (`setup_logging`), `src/threadfin/imagecache.py` (`_download`)
- Test: `tests/test_redact.py`, `tests/test_cli.py`, `tests/test_imagecache.py`
- Docs: `docs/PARITY_NOTES.md` under "Command line"

**Interfaces:**
- Consumes: `display_url` and `clean_text` (Task 1).
- Produces: `class RedactAccessLog(logging.Filter)` in `threadfin.redact`. `filter()` always
  returns `True`. When `record.args` is a tuple with at least 3 items, it replaces `args[2]`,
  uvicorn's `full_path`, with `display_url(args[2])`.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_redact.py` `AccessLog.test_path_query_hidden`: build a record with msg
    `'%s - "%s %s HTTP/%s" %d'` and args
    `("1.2.3.4:5", "GET", "/m3u/threadfin.m3u?username=u&password=SECRET", "1.1", 200)`. After the
    filter, `getMessage()` equals `'1.2.3.4:5 - "GET /m3u/threadfin.m3u?… HTTP/1.1" 200'`.
  - `AccessLog.test_other_records_unchanged`: records whose `args` are `()` or a dict come through unchanged.
  - `tests/test_cli.py` `Commands.test_access_log_is_redacted`: call `setup_logging(2)` twice,
    then check that `logging.getLogger("uvicorn.access")` has exactly one `RedactAccessLog`.
    Log the record above through it with `assertLogs`, and check that the output has no
    `SECRET`. Remove the filter in `addCleanup`.
  - `tests/test_imagecache.py` `test_failure_log_hides_query`: a download that fails for
    `http://127.0.0.1:9/a.png?token=SECRET`. The `threadfin` debug output, from `assertLogs`, has no `SECRET`.

- [ ] **Step 2: Run them and check that they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_redact.py tests/test_cli.py tests/test_imagecache.py -q -k "access or redact or hides"`
Expected: FAIL (`RedactAccessLog` is not defined, and `SECRET` is in the image log)

- [ ] **Step 3: Implement.** Add `RedactAccessLog`. In `setup_logging`, add one instance to
  `uvicorn.access` unless one is already there. `uvicorn.Config(log_config=None)` keeps logger
  filters, so `serve` is unchanged. In `imagecache._download`, log
  `display_url(src), clean_text(str(exc))` and `display_url(src)`.

- [ ] **Step 4: Run the full suite**

Run: `PYTHONDEVMODE=1 uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Add a note under "Command line" in `PARITY_NOTES.md`.** At `-debug 2` and up, the
  request log shows paths without their query string.

- [ ] **Step 6: Commit**

```bash
git add src/threadfin/redact.py src/threadfin/cli.py src/threadfin/imagecache.py tests/test_redact.py tests/test_cli.py tests/test_imagecache.py docs/PARITY_NOTES.md
git commit -m "fix: hide URL query strings in the request and image logs"
```

---

### Task 3: Probe errors

**Files:**
- Modify: `src/threadfin/probe.py` (`run_ffprobe`), `src/threadfin/probe_store.py` (`_row_to_record`)
- Test: `tests/test_probe.py` (`RunnerTests`), `tests/test_probe_store.py` (`StoreTests`)
- Docs: `docs/PARITY_NOTES.md` under "Channel probe"

**Interfaces:**
- Consumes: `clean_text` (Task 1).

- [ ] **Step 1: Write the failing tests**
  - Add two cases to the fake ffprobe script in `RunnerTests.setUp`. `*leak*`:
    `echo "$url: Server returned 404 Not Found" >&2; exit 1`. `*long*`: `printf '%s ' "$url" >&2`,
    then 590 `x` characters to stderr, then `exit 1`.
  - `test_error_hides_query`: probe `"http://example.invalid/leak?token=SECRET"`. `run.error`
    contains `"http://example.invalid/leak?…: Server returned 404"` and not `SECRET`. `run.url`
    still ends with `?token=SECRET`.
  - `test_cut_error_hides_query`: probe `"http://example.invalid/long?token=SECRET"`. `run.error`
    has no `SECRET` and no `token=`.
  - `tests/test_probe_store.py` `test_old_errors_are_cleaned_on_read`: write a row with
    `error="ffprobe exited with status 1: http://h/x?token=SECRET: 404"`. Use the store's insert
    method, or raw SQL if it already cleans. `latest(url).error` has no `SECRET`, and `latest(url).url` is unchanged.

- [ ] **Step 2: Run them and check that they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_probe.py tests/test_probe_store.py -q -k "query or cleaned"`
Expected: FAIL (`SECRET` is in the errors)

- [ ] **Step 3: Implement.** In `run_ffprobe`, set
  `detail = clean_text(stderr.decode(...).strip())[-500:]`, so the text is cleaned before the
  cut. Also clean the `OSError` and parse-error texts. In `_row_to_record`, set
  `error=clean_text(row["error"]) if row["error"] else row["error"]`.

- [ ] **Step 4: Run the full suite**

Run: `PYTHONDEVMODE=1 uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Add a note under "Channel probe" in `PARITY_NOTES.md`.** Probe errors show URLs
  without their query string. Errors stored before this change are cleaned when they are read.

- [ ] **Step 6: Commit**

```bash
git add src/threadfin/probe.py src/threadfin/probe_store.py tests/test_probe.py tests/test_probe_store.py docs/PARITY_NOTES.md
git commit -m "fix: hide URL query strings in channel probe errors"
```

---

### Task 4: Request body limit

**Files:**
- Modify: `src/threadfin/web.py` (new constants, `BodyLimit` class, and the `create_app` middleware list)
- Test: `tests/test_bodylimit.py` (new)
- Docs: `docs/PARITY_NOTES.md` under "Web UI and APIs"

**Interfaces:**
- Produces: `MB = 1024 * 1024`, `MAX_BODY_BYTES = 8 * MB`, `MAX_RESTORE_BYTES = 64 * MB`,
  `RESTORE_PATHS = frozenset({"/api/v1/restore", "/web/restore"})`, and
  `class BodyLimit` (ASGI, `__init__(self, app: Any)`). The middleware list becomes
  `[Middleware(BodyLimit), Middleware(HostCheck, service=service)]`.

- [ ] **Step 1: Write the failing tests** in `tests/test_bodylimit.py`, with a logged-in
  webfixture client and real limits. A 9 MB body is fine in tests.
  - `test_declared_size_over_limit`: `POST /api/v1/users` with a body of `8 * MB + 1` bytes gives
    `413` and `{"error": "request body too large (limit 8 MB)"}`. No user is added.
  - `test_chunked_over_limit`: the same request with `content=` a generator of 9 × `MB` chunks,
    so no `Content-Length` is sent. Expect `413`.
  - `test_ui_gets_plain_text`: `POST /web/login` with `8 * MB + 1` bytes gives `413`, `text/plain`
    and the body `request body too large (limit 8 MB)`.
  - `test_under_limit_unaffected`: a normal `POST /api/v1/users` gives `201`.
  - `test_restore_limit`: `POST /api/v1/restore` with `9 * MB` of junk gives `400` (not a zip),
    not 413. `64 * MB + 1` bytes gives `413` with `(limit 64 MB)`.
  - `test_get_unaffected`: `GET /m3u/threadfin.m3u` gives `200`.

- [ ] **Step 2: Run them and check that they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_bodylimit.py -q`
Expected: FAIL (no 413 responses)

- [ ] **Step 3: Implement `BodyLimit`.** For `http` scopes, pick the limit from the path. If
  `Content-Length` parses as an int over the limit, send the 413 without calling the app.
  Otherwise wrap `receive` so that it counts the bytes of `http.request` bodies and raises a
  private `_BodyTooLarge` once the count passes the limit. Wrap `send` to record whether
  `http.response.start` was sent. Around `self.app`, catch `_BodyTooLarge`: send the 413 if no
  response has started, and re-raise otherwise. Build the 413 with `api.error(text, 413)` under
  `/api`, and with `PlainTextResponse(text, 413)` elsewhere.

- [ ] **Step 4: Run the full suite**

Run: `PYTHONDEVMODE=1 uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Add a note under "Web UI and APIs" in `PARITY_NOTES.md`.** Request bodies over
  8 MB, or 64 MB for a restore, get `413`. Go had no limit.

- [ ] **Step 6: Commit**

```bash
git add src/threadfin/web.py tests/test_bodylimit.py docs/PARITY_NOTES.md
git commit -m "fix: refuse request bodies over a size limit"
```

---

### Task 5: Stream restore uploads to disk

**Files:**
- Modify: `src/threadfin/admin.py` (new helper), `src/threadfin/api.py` (`restore`), `src/threadfin/ui.py` (`restore`)
- Test: `tests/test_api.py`, `tests/test_ui.py`

**Interfaces:**
- Consumes: `MAX_RESTORE_BYTES` (Task 4) bounds what reaches these handlers.
- Produces: `async def receive_upload(request: Request, target: Path, field: str) -> int` in
  `threadfin.admin`. It writes the multipart file field `field`, or the raw body when the request
  is not `multipart/form-data`, to `target`, and returns the number of bytes written. A multipart
  request without the field raises `AdminError(f"missing file field {field!r}")`.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_api.py` `test_restore_over_the_general_limit`: take the archive from
    `GET /api/v1/backup` and rewrite it with an extra `ZIP_STORED` entry, `data/padding.bin`
    (`os.urandom(9 * MB)`). Post it as raw content to `/api/v1/restore`. Expect `200`, and the
    file in the config folder has 9 MB.
  - `tests/test_ui.py` `test_restore_over_the_general_limit`: the same archive as the multipart
    field `archive` to `/web/restore` with `HX`. The page contains "Backup restored".
  - `tests/test_api.py` `test_restore_does_not_read_the_whole_body`: with
    `mock.patch.object(Request, "body", side_effect=AssertionError)`, a raw-body restore still returns `200`.

- [ ] **Step 2: Run them and check that they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_api.py tests/test_ui.py -q -k "restore"`
Expected: `test_restore_does_not_read_the_whole_body` FAILS. The two 9 MB tests may already pass,
since Task 4 gives the restore paths 64 MB. They pin Review Focus 5.

- [ ] **Step 3: Implement `receive_upload`.** For a raw body, write `request.stream()` chunks to
  `target`. For multipart, take the `UploadFile` from `request.form()` (Starlette spools it to
  disk) and copy `upload.file` to `target` with `shutil.copyfileobj` in `asyncio.to_thread`.
  In both `restore` handlers: `mkstemp`, close the fd, call `receive_upload(request, Path(tmp), "file")`
  in the API or `"archive"` in the UI, and keep the existing empty-upload errors when it returns
  `0`. Keep the `finally` unlink as it is.

- [ ] **Step 4: Run the full suite**

Run: `PYTHONDEVMODE=1 uv run pytest -q`
Expected: all pass, including the existing `test_download_and_restore`, `test_restore_rejects_junk` and `test_backup_and_restore`

- [ ] **Step 5: Commit**

```bash
git add src/threadfin/admin.py src/threadfin/api.py src/threadfin/ui.py tests/test_api.py tests/test_ui.py
git commit -m "fix: stream restore uploads to a temp file"
```

---

### Task 6: Check archives before restoring

**Files:**
- Modify: `src/threadfin/backup.py` (`restore_backup`, constants), `src/threadfin/jsonio.py` (new `copy_atomic`)
- Test: `tests/test_backup.py` (`Restore`), `tests/test_settings.py` (the jsonio tests are there)
- Docs: `docs/PARITY_NOTES.md` under "Backup, restore and scheduler", and `docs/REVIEW_2026-10.md`

**Interfaces:**
- Produces: `copy_atomic(path: str | os.PathLike[str], source: IO[bytes], mode: int = 0o644) -> None`
  in `threadfin.jsonio`. `write_bytes_atomic` becomes `copy_atomic(path, io.BytesIO(data), mode)`.
- Produces: `MB`, `MAX_RESTORE_TOTAL = 256 * MB` and `MAX_SETTINGS_BYTES = 8 * MB` in `threadfin.backup`.

- [ ] **Step 1: Write the failing tests.** Every `Restore` test below also checks that the
  config folder is unchanged afterwards, the way `test_zip_slip_is_refused_and_nothing_changes` does.
  - `test_total_over_limit_refused`: with `MAX_RESTORE_TOTAL` patched to `1500`, two 1000-byte
    entries raise `BackupError("archive contents exceed 256 MB")`. Because the limit is patched,
    assert only the `"archive contents exceed"` prefix.
  - `test_large_settings_refused`: with `MAX_SETTINGS_BYTES` patched to `10`, a normal archive raises a `BackupError`.
  - `test_not_enough_disk_space_refused`: patch `threadfin.backup.shutil.disk_usage` to report
    `free=10`. The restore raises a `BackupError` whose message contains `"not enough free space"`.
  - `test_understated_entry_refused_before_writing`: deflate a 10,000-byte `data/x.bin`, then set
    its declared size to 1000 in the local header (offset 22) and the central directory
    (`PK\x01\x02` + 24) with `struct.pack_into("<I", …)`. The restore raises a `BackupError`,
    and no file changes, `settings.json` included.
  - `test_entries_are_streamed`: patch `zipfile.ZipFile.read` so that it raises `AssertionError`
    for every name except `settings.json`. The `test_round_trip` archive still restores.
  - `tests/test_settings.py` `test_copy_atomic`: copy from a `BytesIO` with mode `0o600`, and
    check the content and the mode. A source whose `read` raises leaves no `.tmp` file and
    leaves the target unchanged.

- [ ] **Step 2: Run them and check that they fail**

Run: `PYTHONDEVMODE=1 uv run pytest tests/test_backup.py tests/test_settings.py -q -k "Restore or copy_atomic"`
Expected: FAIL (`copy_atomic` is missing, and no `BackupError` is raised)

- [ ] **Step 3: Implement**, in this order inside `restore_backup`, all before the write loop:
  1. While building the plan, keep a running total of `info.file_size`. Once it passes
     `MAX_RESTORE_TOTAL`, raise `"archive contents exceed 256 MB"`, with the number taken from
     the constant.
  2. If the size of `settings.json` is over `MAX_SETTINGS_BYTES`, raise
     `"settings.json in the archive exceeds 8 MB"` before `zf.read`.
  3. After the settings checks, if `shutil.disk_usage(system.config).free < total`, raise
     `f"not enough free space to restore ({total} bytes needed)"`.
  4. Run `zf.testzip()`. If it returns a name, raise `f"corrupt archive entry: {name}"`. If it
     raises `zipfile.BadZipFile` (or `EOFError`), turn that into `BackupError(f"corrupt archive: {exc}")`.

  In the write loop, replace `write_bytes_atomic(target, zf.read(info), …)` with
  `copy_atomic(target, zf.open(info), _mode(info))`. Update the docstring: size, space and
  corrupt-entry checks all happen before anything is written.

- [ ] **Step 4: Run every check**

Run: `PYTHONDEVMODE=1 uv run pytest -q && uv run ruff check && uv run ruff format --check && uv run pyrefly check --min-severity=warn && uv run coverage run -m pytest && uv run coverage report`
Expected: all pass, 0 pyrefly diagnostics, coverage at least 95%

- [ ] **Step 5: Mutation-check** (CLAUDE.md). One at a time: drop the `testzip()` call, make
  `display_url` return its input, skip the filter registration in `setup_logging`, move the
  probe `[-500:]` cut in front of `clean_text`, and stop `BodyLimit`'s wrapped `receive` from counting
  (`test_chunked_over_limit` must fail). Run the matching test file after each change,
  check that it fails, and restore in `finally`.

- [ ] **Step 6: Docs.** Add a note under "Backup, restore and scheduler" in `PARITY_NOTES.md`:
  archives over 256 MB, archives that don't fit on the disk, and corrupt archives are refused
  before anything is written, and entries are streamed to disk. In `docs/REVIEW_2026-10.md`,
  tick priority 4. Under findings 3 and 4, add "Done in …" paragraphs that give the limits and
  name the commits. Finding 3's paragraph also states the non-goal: credentials in URL paths
  are not hidden.

- [ ] **Step 7: Commit**

```bash
git add src/threadfin/backup.py src/threadfin/jsonio.py tests/test_backup.py tests/test_settings.py docs/PARITY_NOTES.md docs/REVIEW_2026-10.md
git commit -m "fix: check size, space and CRCs before restoring a backup"
```
