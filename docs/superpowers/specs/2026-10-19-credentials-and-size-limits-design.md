# Hide URL credentials and limit upload sizes

Date: 2026-10-19. Status: design, not yet built.

Implements priority 4 of `docs/REVIEW_2026-10.md`: findings 3 (credentials can end up in logs)
and 4 (request bodies and backup entries have no size limit).

## Why

Threadfin runs on a small device on a home network, and only devices on that network reach it
(the user's setup). So this is not about outside attackers. It covers two smaller risks:

- **Passwords where they should not be.** An IPTV provider's playlist URL usually holds the account
  password (`get.php?username=U&password=P`). Today a failed download puts the full URL on the Log
  page, in `/api/v1/log` and in the build report. At `-debug 2` and up, uvicorn's request log also
  records `/m3u/threadfin.m3u?username=…&password=…` from players. Logs get shared as screenshots
  or pasted into bug reports, and then the provider account goes with them.
- **One bad request or file taking the server down.** `api.py` reads request bodies into memory
  with no cap (the 5 MB logo limit is checked only after the whole body has arrived), and
  `restore_backup` unpacks each zip entry fully in memory. A huge upload, or a small zip that
  expands hugely, can exhaust the memory of a small device.

Sizes on the user's own install: the whole config folder is 35 MB, and a zipped backup is 40 KB.
The limits below are sized from that, with room for bigger setups (many uploaded logos, a large
probe database).

## Goals

- No URL query string and no userinfo password appears in an error message or log line.
- No request body over its limit is held in memory, and none reaches a handler.
- A restore whose archive is too big or damaged is refused **before anything is written**, so
  the config folder is left as it was.
- A restore upload is written to a temp file as it arrives, not held in memory.

## Non-goals

- The other open review items: 5 (local-network fetches), 6 (provider add skips the build
  lock) and P5 (`/lineup.json` rewrites `urls.json`).
- Changing what a backup contains or how it is made.
- Making the limits configurable.
- Hiding credentials in URL *paths*, such as Xtream stream URLs (`/live/user/pass/123.ts`). By the
  user's rule the path is shown. These URLs are only redirected to and never logged today.

## Anti-goals

- Hiding anything from what Threadfin *fetches*, or from the config it stores. Only how a URL is
  displayed changes, and the config format stays the same (`~/.threadfin` compatible). The one
  stored thing that changes is the text of probe error messages (see below).
- Refusing a real backup, logo or settings change. The limits must never get in the way of normal use.
- Losing what makes an error message useful: host, port and path stay visible.

## Behaviour (decided with the user)

### Displaying URLs

Wherever Threadfin puts a URL into an error message or a log line, it shows the scheme, host, port
and path, and replaces the query string with `?…`:

```
http://provider.tv:8080/get.php?username=U&password=P&type=m3u_plus
→ http://provider.tv:8080/get.php?…
```

- A URL with no query string is shown unchanged, so the existing Go-format error
  `404: http://host/nope Not Found` still matches exactly.
- A userinfo password is hidden too: `http://user:pass@host/x` → `http://user:***@host/x`. The
  username stays.
- The fragment (`#…`) is dropped along with the query. A fragment is never sent to the server,
  so it doesn't help with debugging. (My call.)
- Something that is not a parseable URL is shown with everything from the first `?` onwards
  replaced by `?…`. (My call: a URL that won't parse could still hold a password.)
- Free text, such as an exception's message, is cleaned too: every URL-shaped run in it
  (`scheme://` up to the next whitespace or quote) is shown by the same rule. This matters because
  urllib follows redirects, so an error can name the redirect target, which often has its own
  token in the query. (My call: matching only the URL we passed in would miss that case.)

This applies to:

1. Every message `providers.py` builds from a provider URL: the invalid-URL error, HTTP status
   errors, the timeout and size errors, connection errors (including the text of the underlying
   exception, which can repeat the URL), and the "unsupported source" error. Through these, the Log
   page, `/api/v1/log` and the build report.
2. uvicorn's request log (`uvicorn.access`, on at `-debug 2` and up): the logged path loses its
   query string the same way. The method, status and client address stay.
3. Channel probe errors (`probe.py`, decided with the user). A failed ffprobe run keeps the end
   of ffprobe's stderr as the error, and that usually includes the stream URL, often with a token
   in its query. The error is cleaned when the probe result is created, before it is cut to 500
   characters, so a cut can't leave a URL without its scheme. (My call on the order.) Rows already
   in `probes.sqlite3` are cleaned when read from the store, so the API and UI never show an old
   full URL and no migration is needed. (My call.) The probe record's `url` field is the probed
   channel's own address, returned as data like the provider URLs in the settings, and is left
   unchanged. (My call.)
4. The image cache's debug log line for a failed image (`imagecache.py`, `image %s: %s`): both
   the image URL and the exception text. (My call: the same rule, one line.)

### Size limits

| What | Limit |
|------|-------|
| Any request body | 8 MB |
| Restore upload (`POST /api/v1/restore`, `POST /web/restore`) | 64 MB |
| Total unpacked size of a backup archive (sum of entry sizes) | 256 MB |
| `settings.json` inside an archive (it is read into memory) | 8 MB (my call) |

MB here means 1024 × 1024 bytes. (My call.)

- **A request over its limit gets HTTP `413`**: `{"error": "request body too large (limit 8 MB)"}`
  under `/api`, and the same text as plain text elsewhere (the restore limit names 64 MB). If the
  request states its size (`Content-Length`), it is refused before any of it is read. If it
  doesn't (chunked), it is refused as soon as the data received passes the limit. Requests
  without a body (all the GETs that players make) are not affected.
- **Restore uploads go to a temp file as they arrive**, for both the raw-body API form and the
  web UI's multipart form. The temp file is deleted afterwards, as now.
- **Archive checks before writing.** The archive is refused with a `BackupError`, and nothing is
  written, if the sum of its entries' declared sizes is over 256 MB, if `settings.json` is over
  8 MB, or if any entry is damaged (bad checksum, or real data that doesn't match its declared
  size). Entries are then copied to disk in a stream, not read whole into memory. Today a damaged
  entry can fail halfway through and leave a mix of old and new files.
- **Free disk space is checked too.** If the config folder's disk has less free space than the
  archive's total declared size, the restore is refused before writing. (My call.) A failure
  while writing, such as a crash or a permission error, can still leave a partial restore, as it
  can today. Unpacking to a staging folder and swapping it in is a non-goal: it would need twice
  the disk space, and with backups of tens of KB the window is tiny. (My call.)

Verified while planning: when a zip entry declares a smaller size than its real data, Python's
`zipfile` stops reading at the declared size and raises `BadZipFile` (CRC). So checking declared
sizes and then verifying every entry's checksum before writing is enough to stop a zip bomb.

## Docs to update

- `docs/PARITY_NOTES.md`: one note each under "Providers" (URLs in errors), "Channel probe" (probe
  errors), "Command line" (request log), "Web UI and APIs" (413 limits) and "Backup, restore and scheduler" (archive checks). Each
  says what Go did (full URLs, no limits).
- `docs/REVIEW_2026-10.md`: tick priority 4, and add a "Done in `<sha>`" paragraph under findings
  3 and 4 that gives the limits and the path-credentials non-goal.

## Left to the builder

- Where the URL-display helper lives, and its name.
- How the request limit is enforced (an ASGI middleware alongside `HostCheck` fits the existing code).
- How restore uploads are streamed to the temp file, and how entries are streamed to disk (a
  streaming counterpart to `jsonio.write_bytes_atomic` fits).
- Test layout, following the existing `unittest.TestCase` style and `tests/webfixture.py`.
