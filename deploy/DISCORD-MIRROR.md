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
   DMs and group DMs are excluded. Shared channels still require the existing
   explicit per-channel approval. Do not join channels or grant Slack scopes.
3. Read Discord application identity, guild channels, bot member and roles with
   the existing Projects token. Find exactly one category named `slack`. Match
   each text channel by exact, unique name to a Slack source. Report missing or
   ambiguous matches. Never guess names, create channels or broaden permissions.
   Verify `VIEW_CHANNEL`, `SEND_MESSAGES`, and `ATTACH_FILES` for attachments.
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
activation boundary are eligible; edits to older unmirrored posts do not backfill.

Each post includes the Slack author ID (or supplied bot name), thread timestamp
when applicable, message text and source link. It posts in the mapped text
channel rather than creating Discord threads. Long text is split below Discord's
2000-character limit, accounting for UTF-16 units. Edits can grow/shrink the parts;
deletion removes all recorded parts, including after restart.

Slack-hosted attachments are downloaded with the Slack token without following
redirects, retained only in memory, and uploaded to Discord. Each part is limited
to ten files and 10 MiB total. Individual files above 10 MiB, external files and
unavailable URLs get an explicit omission notice; text still forwards. Files
without a usable Slack URL cannot be copied. Interactive Block Kit controls,
reactions, canvases, custom emoji assets and Slack-specific rich formatting are
not recreated. Block/attachment text fallbacks are retained where available.
Slack profile lookups requiring a new scope are not performed.

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

Reference: [Slack edits](https://docs.slack.dev/reference/events/message/message_changed/),
[Slack deletes](https://docs.slack.dev/reference/events/message/message_deleted/),
[Discord message create/edit/delete and upload API](https://docs.discord.com/developers/resources/message).
