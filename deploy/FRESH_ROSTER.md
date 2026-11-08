# Optional fresh Sheet roster

Static mode remains the default. Fresh mode requires a separately approved host
reader and private Unix socket; it cannot use static members as an error fallback.
Provisioning this access is not an automatic action of CI or application CD.

For each eligible event, after the existing workspace/channel/shared-channel checks,
the bot requests a new read immediately before its one Slack POST. The host reads
only the approved spreadsheet's visible `Sheet1`, columns D (メンバーフラグ) and
J (Slackのmember ID), in one bounded `values:batchGet` call. Source, headers, row
alignment, timestamp and request/event binding must all match. The Google token may
be cached in memory; roster rows are never reused between requests.

Only TRUE rows with valid Slack user IDs are selected. Blank/numeric/malformed IDs
are skipped without guessing or inviting users. Identical duplicates are deduplicated;
contradictory TRUE/FALSE rows hold the request. The bot excludes itself. Empty results
never produce empty messages. Failed reads/schema checks retry through the existing
bounded queue cooldown, without a POST. Unknown Slack send outcomes stay uncertain
and are not resent. A Slack 429 retry requests a fresh roster again.

## Host boundary

The host reader reuses an explicitly pinned existing Sheets read-only implementation
and the already-authorized key in its existing host location. It does not copy the
key, create credentials or change Google sharing. `roster_service.py` verifies the
dependency manifest/module hash and expected Sheet/scope before using the key.
Only fixed metadata, OAuth token and two-column batch endpoints are allowed, with
redirects disabled and response/time bounds. No names, email, complete sheets,
authentication responses or request bodies are logged.

The user service `slack-roster-reader.service` exposes only
`~/.local/state/slack-roster-reader/reader.sock` (directory0700/socket0600), checks
Linux peer UID and accepts one bounded request at a time. This directory is outside
the bot's writable `/data` mount. The separately approved `compose.roster.yml`
mounts it read-only at `/run/slack-roster`; no TCP port or public route is added.
Never mount the Google private key, host configuration tree or Docker socket into
the Slack container.

Private host config `~/.config/slack-auto-mention/roster-reader.json` has exactly
`source_id`, `key_id`, `reader_runtime`, `reader_module_sha256`. Values must identify
the existing approved source/key and immutable runtime dependency; the file and its
parent must be0600/0700. Real values belong only in private deployment settings.

The existing private Slack runtime config adds `SLACK_ROSTER_MODE="fresh_sheet"`
and `SLACK_ROSTER_SOURCE_ID` for that source. Keep `SLACK_MEMBER_IDS` solely for old
static-mode compatibility. It is ignored for fresh selection and excluded from the
fresh-mode policy hash. A mode/source change invalidates old queued policy hashes;
it does not rewrite or replay old events.

## Apply and verify

1. Obtain approval for the host reader/key reuse, private socket access and profile
   change. Verify the live VM, existing services, owner, image, settings and ledgers.
2. Test and pin this source through the existing branch/CI/main review path. Install
   the host reader into its own immutable release; do not execute downloaded files
   automatically through application CD. Create only its private state/config/unit.
3. Back up nonsecret deployment settings and SQLite with the appropriate locks.
   Preserve queued/sent/uncertain rows, source identity and unrelated service settings.
4. Run a non-posting host read and validate the actual source, expected recipients
   and blank IDs. Only then start the private reader and approve the roster Compose
   profile/hash for the existing CD mechanism. Its existing `SLACK_STATE_DIR` resolves
   the sibling socket directory; the CD runner needs no self-update.
5. Switch the bot to the exact CI-tested image plus fresh private mode. Verify socket
   mounts, host key separation, metadata-only dry-run, auth/health/revision and ledgers.
   Do not create a Slack test post, replay prior events or call a dry-run delivery API.

`healthz` reports fresh-roster status/member count, never IDs or rows. An unavailable
or empty fresh source is unhealthy and cannot post. A recovery must not silently
restore enabled static recipients: retain fresh mode/hold posts or stop only this
bot while repairing the new source. Never restore old SQLite over later receipts.

Existing application pull CD packages only `bot.py` and `container_entrypoint.py`;
the fresh client is contained there. Host reader code/unit/config and the approved
Compose profile are independently pinned/provisioned. Keep the profile hash check
and tested-main gate intact. A local edit or CI pass alone is not live completion;
record source/image revision, real source read and runtime checks separately from a
future natural notification receipt.
