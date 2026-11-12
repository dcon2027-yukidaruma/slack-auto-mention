# Slack to Discord live mirror

This optional extension forwards newly received messages from explicitly verified
Slack channels to same-name text channels inside the Discord `slack` category.
It uses the existing DCON Projects Notifications application and bot identity.
The mention bot's existing trigger, dynamic Sheet roster and receipt table are unchanged.

## Runtime

The signed Slack ingress commits a separate durable mirror queue before ACK.
It performs no Discord requests and receives no Discord or Projects credentials.
The host-only `mirror_worker.py` reads the existing Slack private runtime file and
extracts only `DISCORD_BOT_TOKEN` from the existing Projects runtime file, in memory.
No token is copied to a new file, Docker environment, repository or log.
The worker owns `events.sqlite3.mirror.lock`; ingress must use `recover=False` so
that restarting ingress cannot invalidate an active worker's in-flight receipt.

New `mirror_messages`, `mirror_events`, `mirror_parts`, `mirror_control` tables
share the existing WAL database. They never modify the original `events` table.
The worker's heartbeat is included in ingress health. Provision and start the
worker before restarting mirror-enabled ingress. The existing container image
and VM pull-CD application file list need no additional files or secret mounts.
Install the host worker separately from an exact tested revision, retaining a
manifest and a rollback release. Updating ingress does not self-update the host worker.

## Verify and activate

1. Use the existing authenticated VM session and pinned host key. Read the
   actual main/runtime revision, existing private file modes, service health,
   queue counts, and the Projects application/user IDs. Preserve user edits.
2. Read Slack `auth.test` and the complete paginated `conversations.list` with
   the existing bot token. Check the workspace, bot membership, source names,
   private/shared state and current permissions. Only ordinary channels qualify.
   DMs and group DMs are excluded. Shared channels require explicit per-channel
   opt-in in the mirror configuration's `shared_channels`, a unique subset of
   its verified route IDs. This is independent of the mention feature's shared
   allowlist; never expand mention scope merely to enable forwarding. The exact
   source channel and Discord destination must be covered by the owner's request.
   Do not join channels or grant Slack scopes.
3. Read Discord application identity, guild channels, bot member and roles with
   the existing Projects token. Find exactly one category named `slack`. Match
   each text channel by exact, unique name to a Slack source. Report missing or
   ambiguous matches. Never guess names, create channels or broaden permissions.
   Verify `VIEW_CHANNEL`, `SEND_MESSAGES`, and `ATTACH_FILES` for attachments.
   Native replies also require `CREATE_PUBLIC_THREADS` and
   `SEND_MESSAGES_IN_THREADS`. Missing permissions hold replies; they never
   silently flatten into the channel or grant new permissions.
   Own-message edits/deletes do not require `MANAGE_MESSAGES`.
4. Persist only the nonsecret verified route IDs/names and an activation `since`
   Unix second. Set `DISCORD_MIRROR_CONFIG` to JSON inside the existing private
   runtime file's JSON-string values, and `DISCORD_MIRROR_ENABLED` to `true`.
   The nested JSON must be encoded twice for that private file format. Keep all
   other keys/bytes, modes and fresh Sheet settings. Do not put real IDs here.
5. Under the existing CD deploy lock, retain a private config backup and SQLite
   backup, and record all original receipt rows/counts. Stage the exact tested
   `bot.py`, `deploy/container_entrypoint.py`, `deploy/mirror_worker.py`, and
   service template under `~/.local/share/slack-auto-mention/mirror-worker/`.
   Install the user service with the pinned `current` target. The worker and
   existing ingress must use the same owner and host database.
6. Start the mirror worker, verify its heartbeat, then apply the existing tested
   container deployment/restart procedure. Keep the roster socket mount and
   protected ingress unchanged. Verify config, revision, health, service PID,
   mirror heartbeat and original ledger preservation. No fabricated posts or
   replay of old events is needed. Observe natural deliveries when available.

## Message behavior

Human messages, bot/integration messages, thread replies and textual system
messages are accepted. Thread-broadcast duplicates share their original Slack
timestamp. Thread metadata updates are ignored. Edits use `message.ts`; deletes
use `deleted_ts`, including tombstones. Only source timestamps at or after the
activation boundary are eligible. When a new reply refers to an unmirrored parent,
only that exact parent is normalized from Slack's supplied root snapshot or read
with `conversations.history` (`oldest=latest=thread_ts`, inclusive, limit 1).
No paging or historical reply replay is performed. The targeted parent becomes a
tracked context message, so later live edits/deletes can update that one copy.
An unavailable parent gets an explicit context placeholder rather than an
unrelated history item. Other old unmirrored messages remain ineligible.

Each post starts with a clickable sender profile name, ` · `, then a `Slack`
link to that exact source message, followed by the message text. Both links are
constructed independently after body conversion, so neither passes through the
Slack-token converter again. Every multipart copy retains this header; the raw
source URL is not duplicated as a footer. The profile URL uses only the workspace origin returned
by authenticated `auth.test` and that sender's validated Slack user ID. Discord
masked-link labels preserve literal formatting characters such as `_`; they are
not rewritten as `\_`, which can appear visibly in Discord link labels. Only
backslashes and square brackets that delimit a link are structurally escaped.
No ID suffix is displayed. Outside backtick code spans/fences, `<@USERID>`
mentions use `[@display_name](profile URL)`; the sender header gains no `@` prefix.
Escaped literal angle tokens and code contents are not interpreted as mentions.
Unknown body users retain a linked `@Slack user` label. Profile links are
never split across multipart boundaries. Supplied Slack profile names are preferred; otherwise `users.info`
uses the existing bot token when `users:read` is available. Only display
names/IDs/timestamps are cached, bounded to 2000 entries, with no email lookup or
credential fallback. A missing profile scope/name uses the linked label
`Slack user` and does not drop the message; report that limit rather than claim
all names resolved. System messages without a valid user ID remain plain names.

The lossless `mirror_profile_names` cache is separate from the legacy
`mirror_authors` cache, whose names may already have lost punctuation. The old
cache and all receipts are retained; no clean message is requeued. Names and body
mention labels are frozen for one event generation so multipart retries remain
consistent. Lookup work is limited to 100 distinct body mentions per generation;
additional mentions use linked fallback labels. A future event retries unavailable
names after the existing five-minute negative-cache interval.

Top-level posts go to the mapped text channel. Replies go to the public Discord
thread attached to the recorded first part of their parent. The thread has the
same ID as that starter message and is recorded in `mirror_threads`. Subsequent
replies reuse it, including after restarts. Slack `thread_broadcast` / "also send
to channel" replies have both a thread copy and a channel copy. The broadcast
flag is retained across out-of-order pointers, duplicates and edits. Each copy
has its own destination, receipt and nonce in `mirror_copies`; edits/deletes
cover every copy and every text/file part. The worker never issues a whole-thread
delete when its parent is removed, because it may contain other replies or human messages.

Automatic archive is reopened before edits; moderator-locked or manually deleted
threads hold work without creating another thread or using a flat fallback.
Long text is split below Discord's
2000-character limit, accounting for UTF-16 units. Edits can grow/shrink the parts;
deletion removes all recorded parts, including after restart.

Slack-hosted attachments are downloaded with the Slack token without following
redirects, retained only in memory, and uploaded to Discord. Each part is limited
to ten files and 10 MiB total. Individual files above 10 MiB, external files and
unavailable URLs get an explicit omission notice; text still forwards. Files
without a usable Slack URL cannot be copied. Interactive Block Kit controls,
reactions, canvases, custom emoji assets and Slack-specific rich formatting are
not recreated. Block/attachment text fallbacks are retained where available.
No OAuth scope or Discord permission is granted automatically.

All POST/PATCH requests explicitly suppress user, role and everyone mentions;
embeds are suppressed. Source content cannot select a route, credential or API
endpoint. Before each operation the worker checks the Slack conversation guard,
source name, Discord application/bot identity, guild, category, destination name,
and effective channel permissions.

## Delivery and recovery

Stable nonces supplement SQLite deduplication. Only a definitive 429 is retried
after Discord's response-based Retry-After gate. Connection errors, 5xx, malformed
success or a crash during an HTTP write become `uncertain`, with no automatic
POST replay or fallback. A lost create response may therefore leave an unlinked
Discord message; human reconciliation is required. Later source events do not
clear that state. Definitive permission/name/identity failures become `failed`.
Thread creation is reconciled by a read of its deterministic starter-message ID;
an unknown create is never followed by a second create POST. A rate-limit
rejection can retry, and an idempotent unarchive is re-read before any next attempt.
Verification/download outages queue work for a later retry. Monitor these counts;
the system does not claim exactly-once delivery or a real-time SLA.

Out-of-order edits preserve the newest event version; a deletion is permanent and
suppresses late creates. An edit arriving before its create waits for that create.
Events received during a write are processed on a later pass. A changed routing
configuration holds older queued work instead of silently moving its content.
Do not delete the queue, reset activation time, restore old receipt data, or
manually replay unknown operations.

To disable, set `DISCORD_MIRROR_ENABLED=false`, restart ingress with the existing
safe procedure, then stop the mirror service. Retain all mirror receipt tables.
Rollback code/config only after both processes have stopped; never roll back
SQLite to stale receipts. The original mention and Projects services retain
their existing code, credentials, recipients and delivery ledgers.

## Upgrade from flat forwarding

Pause the existing CD timer and wait for the CD deploy lock before upgrading.
Create the private nonsecret `mirror-worker.pause` marker, stop the old host
worker gracefully, and take a SQLite backup. Install the exact tested new worker
source and start it with the marker present. This is the only owner of schema
recovery and copy-receipt migration; never let an old worker write v1 receipts
after the new worker has seeded `mirror_copies`.

The new worker initializes its tables and heartbeat while paused. Ingress remains
available and commits new callbacks. Promote the exact CI-eligible main image,
verify ingress/worker code hashes agree, and remove only the deployment-owned
pause marker. Confirm `worker_paused=false`, health, original mention receipts,
all old mirror IDs and held/uncertain states, then resume the CD timer.

Existing `mirror_parts` rows are retained and copied once into channel-lane
receipts. Clean old messages are not queued or relocated. Queued v1 payloads keep
their flat placement; new v2 callbacks use native threads. A future edit to an old
reply can move its known copy into its parent thread. Never restore a stale DB or
downgrade to a v1 host worker after v2 thread/copy writes: it cannot see those new
receipts. Hold the mirror and reconcile or fix forward if promotion fails.

Reference: [Slack edits](https://docs.slack.dev/reference/events/message/message_changed/),
[Slack deletes](https://docs.slack.dev/reference/events/message/message_deleted/),
[Discord message create/edit/delete and upload API](https://docs.discord.com/developers/resources/message).
