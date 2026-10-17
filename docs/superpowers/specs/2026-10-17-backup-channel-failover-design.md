# Backup channel failover for redirected streams

Date: 2026-10-17. Status: design, not yet built.

## Why

Some provider channels go dead. When that happens, a player keeps re-requesting the channel and
never plays it. The case that prompted this: An Apple TV app showed a spinning wheel while it
requested the same stream every 1–3 seconds:

```
12:00:06 "GET /stream/312f77eb6817abc9d88535562f881b4a HTTP/1.1" 302
12:00:09 "GET /stream/312f77eb6817abc9d88535562f881b4a HTTP/1.1" 302
12:00:10 "GET /stream/312f77eb6817abc9d88535562f881b4a HTTP/1.1" 302
12:00:13 "GET /stream/312f77eb6817abc9d88535562f881b4a HTTP/1.1" 302
```

The provider URL redirects again to a URL served as `application/octet-stream` (a plain stream, not
HLS). A player holds one connection open on a stream like that, so requesting it again within
seconds is a reliable sign the stream failed.

Go Threadfin had backup channels (`x-backup-channel-1/2/3`), but they only worked inside its stream
buffer. This port has no buffer: `/stream/<id>` is a 302 to the provider. PR #16 (commit `3b3c692`)
therefore removed them. This feature brings them back with a trigger that works with a redirect:
**repeated requests mean failure, so redirect to the next backup.**

## Goals

- Bring back the backup channel code PR #16 removed: resolving `x-backup-channel-1/2/3` at build
  time, storing the backups in `urls.json`, the three fields in the channel editor, and
  `PATCH /api/v1/channels/{id}` accepting them.
- In `/stream/<id>`, detect a failing stream and redirect to its backup.
- Make the trigger configurable, defaulting to **3 requests within 5 seconds**.

## Non-goals

- No stream buffer or proxying: the endpoint stays a 302 (the scope decision in `CLAUDE.md` stands).
- No probing of provider URLs to decide whether they are alive.
- No detection of failures that do not cause repeated requests (a player that gives up after one
  try, or a stream that freezes without being re-requested).

## Anti-goals

- Switching a channel that is playing fine. A single request, or requests spread out over time,
  must never switch.
- Sticking a viewer on a backup: coming back to a channel later must try the main stream first.

## Behaviour (decided with the user)

**Same stream, same client.** Requests are counted per (client, stream id). The client is the
request's IP address (`access.client_address`); the source port is ignored, since it changes
between connections. Two devices behind one NAT address count as one client (accepted).

**Behind a reverse proxy.** The user runs threadfin-py in Docker behind a reverse proxy, so
today every request appears to come from the reverse proxy and all devices would share one count. No
code change is needed: uvicorn (0.54) already replaces the client address with the one in
`X-Forwarded-For` when the connection comes from an address in the `FORWARDED_ALLOW_IPS`
environment variable (default `127.0.0.1,::1`; addresses or CIDR ranges). The fix is
configuration plus docs:

- Document `FORWARDED_ALLOW_IPS` in the README's Docker section, with an example
  (`FORWARDED_ALLOW_IPS=192.0.2.1`), and warn that it must list only the proxy: any address listed
  can claim to be any client.
- This also corrects the failed-login throttling and the access log, which today see every
  client as the proxy.
- The user sets the variable in their compose file; that is outside this repository.

**Trigger.** When a client's request for a stream is the Nth within W seconds of the earliest one
counted (N = 3, W = 5 by default), that client moves one step down that channel's list:
main → backup 1 → backup 2 → backup 3. The request that triggers the switch is already redirected
to the new target. Counting then starts again on the new target, so another N requests within W
seconds move it on to the next backup.

**End of the list.** When the backups run out, the client stays on the last backup that exists. A
channel with no backups always redirects to its main stream.

**Going back to main.** If a client sends no request for that stream for W seconds, its state is
forgotten and the next request starts on the main stream. (Retries came 1–3 s apart in the log, so
a gap of W means the player stopped and came back.) Changing channel and returning therefore
always tries the main stream first.

**Restarts.** State is in memory only; a restart forgets it.

**Details settled by Claude (not discussed with the user):**

- State is keyed by the stream id the player requested, i.e. the main stream's id: a player on a
  backup keeps requesting `/stream/<main id>`, and that is what is counted.
- The window slides: on each request, drop the client's timestamps older than W seconds, add this
  one, and switch when N remain. A switch clears the timestamps.
- GET and HEAD both count.
- The backup index is stored, not the backup's URL: after a rebuild the index is looked up again in
  the fresh registry.

## Settings

Two new keys in `settings.json`, editable on the settings page and through the settings API like
the existing ones (`admin.py` type table, `ui_text.py` fields):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `backup.failover.requests` | int | 3 | Requests that count as a failure. 0 turns failover off; otherwise at least 2 (1 would switch every request). |
| `backup.failover.seconds` | int | 5 | The window W, in seconds (at least 1); also the quiet gap that resets a client to main. |

The Go app ignores these keys and drops them when it saves `settings.json`, like any other key it
does not know; the `Settings` model keeps unknown keys already, so loading is not a concern.

## Restoring PR #16's code

Most of it can be reverted from `3b3c692` (later commits may conflict in the docs):

- `xepg.map_channels` takes the stream list again and resolves each backup name to a
  `BackupStream(playlist_id, url)` (matched on `tvg-name`, else `name`; the last match wins, as in
  Go). This restores the earlier xepg divergence: backups that were removed are cleared instead of
  left as stale pointers.
- `StreamRegistry.register/add` stores the three backups in `urls.json`.
- The channel editor shows the three fields and the channel-name list again.
- `PATCH /api/v1/channels/{id}` accepts `x-backup-channel-1/2/3` again.
- The Go golden lists change back: the playlist and xepg `backup_channels` cases are no longer
  divergences.

## Redirect target

A backup's target goes through the same rules as the main stream (the UDPxy rewrite of `udp://@`).
A backup whose stream no longer exists in the registry is skipped.

## Logging

Each switch logs one INFO line naming the client, the stream and the channel, for example:

```
stream 312f77eb… (Channel Name) for 192.0.2.1: 3 requests in 5s, switching to backup 1 (Backup Name)
```

When a channel has no backup left to switch to, the line says so instead, so the user can see which
channels need a backup (once per burst, not on every further request).

## Docs to update

- `CLAUDE.md` scope decisions: replace "No backup channels" with this behaviour.
- `README.md` status table.
- `docs/PARITY_NOTES.md`: backup channels change from "dropped" to "changed: repeated requests
  trigger the switch, not the stream buffer"; playlist and xepg divergence counts.

## Left to the builder

- Where the per-client state lives (a small class in its own module, owned by `Service`, is the
  obvious place) and how it is pruned so it cannot grow without bound.
- Exact test layout. Tests should use an injectable clock rather than sleeping.
- Wording of the settings page text.
