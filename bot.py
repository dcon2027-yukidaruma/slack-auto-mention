"""Slack Events API mention bot and opt-in Discord mirror. Standard library only."""
import dataclasses
from datetime import datetime, timezone
import hashlib
import html
import hmac
import http.server
import json
import os
import re
import signal
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path

TRIGGER = "@ゆきだるま"
TS = re.compile(r"[0-9]{1,20}\.[0-9]{6}")


def ids(value, prefix):
    values = value.split(",") if value else []
    if not values or any(not re.fullmatch(prefix + r"[A-Z0-9]{8,}", v) for v in values):
        raise ValueError("Missing or invalid explicit allowlist")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate allowlist entries")
    return tuple(values)


@dataclasses.dataclass(frozen=True)
class Config:
    team: str
    channels: tuple
    members: tuple
    bot_user: str
    token: str = dataclasses.field(repr=False)
    signing_secret: str = dataclasses.field(repr=False)
    database: str = "data/events.sqlite3"
    enabled: bool = False
    channel_mode: str = "explicit"
    group_dm_ids: tuple = ()
    shared_channel_ids: tuple = ()
    roster_mode: str = "static"
    roster_source_id: str = ""

    @classmethod
    def load(cls):
        team = ids(os.getenv("SLACK_TEAM_ID", ""), "T")
        bot = ids(os.getenv("SLACK_BOT_USER_ID", ""), "U")
        mode = os.getenv("SLACK_CHANNEL_MODE", "explicit")
        if mode not in ("explicit", "all_joined"):
            raise ValueError("Invalid channel mode")
        raw_channels = os.getenv("SLACK_CHANNEL_IDS", "")
        channels = ids(raw_channels, "[CG]") if raw_channels else ()
        raw_groups = os.getenv("SLACK_GROUP_DM_IDS", "")
        groups = ids(raw_groups, "[CG]") if raw_groups else ()
        raw_shared = os.getenv("SLACK_SHARED_CHANNEL_IDS", "")
        shared = ids(raw_shared, "[CG]") if raw_shared else ()
        if shared and (not set(shared) <= set(channels) or set(shared) & set(groups)):
            raise ValueError("Shared channels require explicit ordinary-channel approval")
        if mode == "explicit" and not channels and not groups:
            raise ValueError("No allowed conversations")
        roster_mode = os.getenv("SLACK_ROSTER_MODE", "static")
        source_id = os.getenv("SLACK_ROSTER_SOURCE_ID", "")
        if roster_mode not in ("static", "fresh_sheet") or (roster_mode == "fresh_sheet" and
                not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", source_id)):
            raise ValueError("Invalid roster source configuration")
        members = ids(os.getenv("SLACK_MEMBER_IDS", ""), "U")
        if len(team) != 1 or len(bot) != 1 or bot[0] in members or len(members) > 50:
            raise ValueError("Invalid workspace, bot identity or members")
        enabled = os.getenv("BOT_ENABLED", "false") == "true"
        if enabled and os.getenv("ALLOWLIST_VERIFIED") != "true":
            raise ValueError("Allowlist verification required before activation")
        token = os.environ["SLACK_BOT_TOKEN"]
        secret = os.environ["SLACK_SIGNING_SECRET"]
        if not token.startswith("xoxb-") or len(secret) < 16:
            raise ValueError("Invalid credentials")
        return cls(team[0], channels, members, bot[0], token, secret,
                   os.getenv("DATABASE_PATH", "data/events.sqlite3"), enabled, mode, groups, shared,
                   roster_mode, source_id)

    def allows_shared(self, channel):
        return (self.channel_mode in ("explicit", "all_joined") and channel in self.channels
                and channel in self.shared_channel_ids and channel not in self.group_dm_ids)

    @property
    def fingerprint(self):
        value = [self.team, sorted(self.channels), sorted(self.members), self.bot_user,
                 self.channel_mode, sorted(self.group_dm_ids)]
        # Preserve the existing policy hash when the new opt-in is unused.
        if self.shared_channel_ids:
            value.append(sorted(self.shared_channel_ids))
        if self.roster_mode != "static":
            value[2] = []  # Static members are never a fallback for a fresh roster.
            value.append(["fresh_sheet_v1", self.roster_source_id])
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()


class RosterHeld(ValueError):
    """A non-posting source failure. Never include source response text."""


def roster_members(snapshot, *, source_id, request_id, event_id, requested_at, now, bot_user):
    expected = {"schema_version", "source_id", "sheet", "request_id", "event_id",
                "read_started_at", "read_finished_at", "rows"}
    if (not isinstance(snapshot, dict) or set(snapshot) != expected
            or type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1
            or snapshot["source_id"] != source_id or snapshot["sheet"] != "Sheet1"
            or snapshot["request_id"] != request_id or snapshot["event_id"] != event_id):
        raise RosterHeld("roster_schema_invalid")
    try:
        started = datetime.fromisoformat(snapshot["read_started_at"])
        finished = datetime.fromisoformat(snapshot["read_finished_at"])
        valid_time = (all(v.tzinfo is not None for v in (requested_at, started, finished, now))
                      and requested_at <= started <= finished <= now
                      and 0 <= (now - started).total_seconds() <= 15)
    except (TypeError, ValueError):
        valid_time = False
    rows = snapshot["rows"]
    if not valid_time or not isinstance(rows, list) or len(rows) > 5000:
        raise RosterHeld("roster_time_or_bounds_invalid")
    flags = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise RosterHeld("roster_row_invalid")
        flag, ident = row
        if type(flag) is bool:
            active = flag
        elif isinstance(flag, str) and flag in ("TRUE", "FALSE", ""):
            active = flag == "TRUE"
        else:
            raise RosterHeld("roster_flag_invalid")
        if not isinstance(ident, str):
            continue  # Numeric cells are invalid Slack IDs, never coerced or guessed.
        ident = ident.strip()
        if not re.fullmatch(r"U[A-Z0-9]{8,}", ident):
            continue  # Missing/invalid IDs never become guessed recipients.
        if ident in flags and flags[ident] != active:
            raise RosterHeld("roster_conflicting_rows")
        flags[ident] = active
    members = tuple(sorted(ident for ident, active in flags.items() if active and ident != bot_user))
    if len(members) > 50:
        raise RosterHeld("roster_recipient_limit")
    return members


class FreshSheetRoster:
    """Request-scoped reads from an explicitly provisioned local reader, never a cache.

    No Google credential is loaded into this process. The fixed Unix socket is
    NOT provisioned by this client; deploying a reader/mount is a separate step.
    """
    PATH = "/run/slack-roster/reader.sock"

    def __init__(self, cfg, *, request=None, clock=None):
        self.cfg = cfg
        self.request = request or self._request
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _request(envelope):
        deadline = time.monotonic() + 15
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(15)
                stream.connect(FreshSheetRoster.PATH)
                stream.sendall(json.dumps(envelope, separators=(",", ":")).encode() + b"\n")
                raw = b""
                while not raw.endswith(b"\n"):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or len(raw) >= 131072:
                        raise RosterHeld("roster_response_bounds")
                    stream.settimeout(remaining)
                    part = stream.recv(min(4096, 131072 - len(raw)))
                    if not part:
                        raise RosterHeld("roster_response_incomplete")
                    raw += part
                return json.loads(raw)
        except Exception:
            raise RosterHeld("roster_read_unavailable") from None

    def __call__(self, event_id):
        started = self.clock()
        request_id = uuid.uuid4().hex
        snapshot = self.request({"schema_version": 1, "operation": "fresh_members",
                                 "source_id": self.cfg.roster_source_id, "sheet": "Sheet1",
                                 "request_id": request_id, "event_id": event_id})
        return roster_members(snapshot, source_id=self.cfg.roster_source_id,
                              request_id=request_id, event_id=event_id,
                              requested_at=started, now=self.clock(), bot_user=self.cfg.bot_user)


class RosterMonitor:
    """Bounded non-posting recovery probes; successful rows are never cached for sends."""
    def __init__(self, provider, health, *, clock=None):
        self.provider, self.health = provider, health
        self.clock = clock or time.monotonic
        self.next_probe, self.delay = 0, 30

    def read(self, event_id):
        try:
            members = self.provider(event_id)
        except Exception:
            self.health.update(status="held", member_count=0)
            self.next_probe = self.clock() + self.delay
            self.delay = min(self.delay * 2, 300)
            raise
        self.health.update(status="ready" if members else "empty", member_count=len(members))
        self.next_probe = self.clock() + self.delay
        self.delay = 30 if members else min(self.delay * 2, 300)
        return members

    def probe_if_due(self):
        if self.health.get("status") == "ready" or self.clock() < self.next_probe:
            return False
        try:self.read("health-dry-run")
        except Exception:pass
        return True


def target(payload, cfg):
    """Validate the outer workspace and ordinary human message. Never parse text as commands."""
    if not cfg.enabled or not isinstance(payload, dict):
        return None
    if payload.get("type") != "event_callback" or payload.get("team_id") != cfg.team:
        return None
    e = payload.get("event")
    if not isinstance(e, dict) or e.get("type") != "message":
        return None
    if e.get("subtype") or any(e.get(k) for k in ("bot_id", "bot_profile", "app_id", "hidden", "edited")):
        return None
    user = e.get("user")
    if not isinstance(user, str) or not re.fullmatch(r"U[A-Z0-9]{8,}", user) or user == cfg.bot_user:
        return None
    channel = e.get("channel")
    if not isinstance(channel, str) or not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel):
        return None
    if payload.get("is_ext_shared_channel") and not cfg.allows_shared(channel):
        return None
    if e.get("team", cfg.team) != cfg.team:
        return None
    if cfg.channel_mode != "all_joined" and channel not in cfg.channels + cfg.group_dm_ids:
        return None
    if not isinstance(e.get("text"), str) or TRIGGER not in e["text"]:
        return None
    ts = e.get("ts")
    thread = e.get("thread_ts", ts)
    if not all(isinstance(v, str) and TS.fullmatch(v) for v in (ts, thread)):
        return None
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"Ev[A-Za-z0-9]{1,100}", event_id):
        return None
    return event_id, e["channel"], ts, thread


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, team TEXT NOT NULL, channel TEXT NOT NULL,
                ts TEXT NOT NULL, thread_ts TEXT NOT NULL, config_hash TEXT NOT NULL,
                state TEXT NOT NULL, due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                result_ts TEXT, UNIQUE(team,channel,ts))""")
            c.execute("CREATE TABLE IF NOT EXISTS control (name TEXT PRIMARY KEY, due REAL NOT NULL)")
            # A crash between request and response is an unknown send outcome.
            c.execute("UPDATE events SET state='uncertain' WHERE state='sending'")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=2)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def enqueue(self, item, cfg):
        eid, channel, ts, thread = item
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO events(event_id,team,channel,ts,thread_ts,config_hash,state) "
                      "VALUES(?,?,?,?,?,?,'queued')", (eid, cfg.team, channel, ts, thread, cfg.fingerprint))

    def claim(self, cfg, now):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            gate = c.execute("SELECT due FROM control WHERE name='slack'").fetchone()
            if gate and gate[0] > now:
                return None
            row = c.execute("SELECT * FROM events WHERE state='queued' AND due<=? ORDER BY due,rowid LIMIT 1", (now,)).fetchone()
            if row:
                valid = cfg.enabled and row["config_hash"] == cfg.fingerprint
                c.execute("UPDATE events SET state=?,attempts=attempts+1 WHERE event_id=?",
                          ("sending" if valid else "failed", row["event_id"]))
                return row if valid else None

    def finish(self, eid, state, due=0, result_ts=None, cooldown_until=None):
        with self.connect() as c:
            c.execute("UPDATE events SET state=?,due=?,result_ts=? WHERE event_id=?",
                      (state, due, result_ts, eid))
            if cooldown_until is not None:
                c.execute("INSERT INTO control VALUES('slack',?) ON CONFLICT(name) DO UPDATE SET due=max(due,excluded.due)", (cooldown_until,))

    def counts(self):
        with self.connect() as c:
            return dict(c.execute("SELECT state,count(*) FROM events GROUP BY state").fetchall())


class Slack:
    def __init__(self, token):
        self.token = token

    def call(self, method, body):
        headers = {"Authorization": "Bearer " + self.token}
        url = "https://slack.com/api/" + method
        if method in ("conversations.info", "conversations.members", "conversations.list", "conversations.history", "users.info"):
            request = urllib.request.Request(url + "?" + urllib.parse.urlencode(body), headers=headers)
        else:
            headers["Content-Type"] = "application/json; charset=utf-8"
            request = urllib.request.Request(url, json.dumps(body).encode(), headers)
        # urllib performs no automatic POST retries. Never log requests, exceptions or bodies.
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
                if not isinstance(data, dict):
                    return "uncertain", None
                if data.get("ok") is True:
                    return "sent", data
                return "failed", None
        except urllib.error.HTTPError as error:
            if error.code == 429:
                try:
                    delay = max(1, int(error.headers["Retry-After"]))
                    if delay > 86400:
                        return "failed", None
                    return "rate_limited", delay
                except (ValueError, TypeError):
                    return "failed", None
            # Even 5xx responses might follow a successful write. Do not guess.
            return "uncertain", None
        except (OSError, ValueError):
            return "uncertain", None

    def conversation_allowed(self, cfg, channel):
        state, data = self.call("conversations.info", {"channel": channel})
        if state != "sent":
            return ("retry", data if state == "rate_limited" else 30) if state != "failed" else ("denied", None)
        info = data.get("channel", {})
        # Never infer type from the prefix or permit a pending external share.
        if info.get("id") != channel or info.get("is_archived") or info.get("is_pending_ext_shared"):
            return "denied", None
        if info.get("is_ext_shared") and not cfg.allows_shared(channel):
            return "denied", None
        if info.get("context_team_id") != cfg.team:
            return "denied", None
        if info.get("is_im"):
            return "denied", None
        if info.get("is_mpim"):
            if channel not in cfg.group_dm_ids:
                return "denied", None
        elif not (info.get("is_channel") or info.get("is_group")):
            return "denied", None
        elif cfg.channel_mode != "all_joined" and channel not in cfg.channels:
            return "denied", None
        # Membership is rechecked before every send, including queued events.
        if info.get("is_member") is True:
            return "allowed", None
        cursor = None
        for _ in range(20):
            body = {"channel": channel, "limit": 200}
            if cursor:
                body["cursor"] = cursor
            state, members = self.call("conversations.members", body)
            if state != "sent":
                return ("retry", members if state == "rate_limited" else 30) if state != "failed" else ("denied", None)
            if cfg.bot_user in members.get("members", []):
                return "allowed", None
            cursor = members.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return "denied", None


def process_one(store, cfg, slack, clock=None, *, member_provider=None, roster_health=None):
    clock = clock or time.time
    now = clock()
    row = store.claim(cfg, now)
    if row is None:
        return False
    try:
        permission, delay = slack.conversation_allowed(cfg, row["channel"])
    except Exception:
        permission, delay = "retry", 30
    if permission != "allowed":
        if permission == "retry":
            until = clock() + delay
            state = "queued" if row["attempts"] < 9 else "failed"
            store.finish(row["event_id"], state, until, cooldown_until=until)
        else:
            store.finish(row["event_id"], "failed")
        return True
    members = cfg.members
    if cfg.roster_mode == "fresh_sheet":
        roster_started = time.monotonic()
        try:
            if member_provider is None:
                raise RosterHeld("roster_provider_missing")
            members = member_provider(row["event_id"])
            if (not isinstance(members, (tuple, list)) or len(members) > 50
                    or any(not isinstance(v, str) or not re.fullmatch(r"U[A-Z0-9]{8,}", v) for v in members)):
                raise RosterHeld("roster_members_invalid")
            members = tuple(sorted(set(members) - {cfg.bot_user}))
            if roster_health is not None:
                roster_health.update(status="ready" if members else "empty", member_count=len(members))
        except Exception:
            until = clock() + 30
            store.finish(row["event_id"], "queued" if row["attempts"] < 9 else "failed",
                         until, cooldown_until=until)
            if roster_health is not None:
                roster_health.update(status="held", member_count=0)
            return True
        if not members:
            store.finish(row["event_id"], "failed")
            return True
        # The Sheet request may take time. Recheck conversation permission after it.
        try:
            permission, delay = slack.conversation_allowed(cfg, row["channel"])
        except Exception:
            permission, delay = "retry", 30
        if permission != "allowed":
            until = clock() + (delay if permission == "retry" else 0)
            store.finish(row["event_id"], "queued" if permission == "retry" and row["attempts"] < 9 else "failed",
                         until, cooldown_until=until if permission == "retry" else None)
            return True
        if not 0 <= time.monotonic() - roster_started <= 15:
            until = clock() + 30
            store.finish(row["event_id"], "queued" if row["attempts"] < 9 else "failed",
                         until, cooldown_until=until)
            if roster_health is not None:
                roster_health.update(status="held", member_count=0)
            return True
    body = {"channel": row["channel"], "thread_ts": row["thread_ts"],
            "text": " ".join("<@" + member + ">" for member in members),
            "reply_broadcast": False, "unfurl_links": False, "unfurl_media": False,
            "client_msg_id": str(uuid.uuid5(uuid.NAMESPACE_URL, cfg.team + ":" + row["event_id"]))}
    try:
        state, value = slack.call("chat.postMessage", body)
    except Exception:
        state, value = "uncertain", None
    if state == "rate_limited":
        # Retry-After begins when the response is received, not when claimed.
        # Even an exhausted event must leave the global gate in place.
        until = clock() + value
        state = "queued" if row["attempts"] < 9 else "failed"
        store.finish(row["event_id"], state, until, cooldown_until=until)
    elif state == "sent":
        ts = value.get("ts")
        if not isinstance(ts, str) or not TS.fullmatch(ts):
            store.finish(row["event_id"], "uncertain")
        else:
            store.finish(row["event_id"], "sent", result_ts=ts)
    else:
        store.finish(row["event_id"], "failed" if state == "failed" else "uncertain")
    return True


def verified_signature(secret, headers, body, now=None):
    stamp = headers.get("X-Slack-Request-Timestamp", "")
    if not re.fullmatch(r"[0-9]{10,12}", stamp):
        return False
    if abs((time.time() if now is None else now) - int(stamp)) > 300:
        return False
    digest = "v0=" + hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, headers.get("X-Slack-Signature", ""))


def handler(cfg, store, health, *, mirror=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/healthz":
                return self.reply(404, {})
            counts = store.counts()
            ready = cfg.enabled and health["authenticated"] and time.monotonic() - health["worker"] < 30
            if cfg.roster_mode == "fresh_sheet":
                ready = ready and health.get("roster", {}).get("status") == "ready"
            if mirror:
                ready = ready and mirror.worker_alive(time.time())
            self.reply(200 if ready else 503, {"ready": ready, "revision": os.getenv("APP_REVISION", "unknown"),
                                             "events": counts,
                                             **({"mirror": {"events": mirror.counts(),
                                                  "worker_alive": mirror.worker_alive(time.time()),
                                                  "worker_paused": mirror.worker_paused()}}
                                                if mirror else {}),
                                             **({"roster": health.get("roster", {"status": "not_read"})}
                                                if cfg.roster_mode == "fresh_sheet" else {})})

        def do_POST(self):
            if self.path != "/slack/events":
                return self.reply(404, {})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 1024 * 1024:
                    return self.reply(413, {})
                self.connection.settimeout(3)
                raw = self.rfile.read(size)
                if len(raw) != size or not verified_signature(cfg.signing_secret, self.headers, raw):
                    return self.reply(401, {})
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return self.reply(400, {})
                if payload.get("type") == "url_verification":
                    challenge = payload.get("challenge")
                    if not isinstance(challenge, str) or len(challenge) > 1024:
                        return self.reply(400, {})
                    return self.reply(200, {"challenge": challenge})
                item = target(payload, cfg)
                if item:
                    store.enqueue(item, cfg)
                if mirror:
                    mirror.enqueue(payload)
                self.reply(200, {})
            except (ValueError, UnicodeError):
                self.reply(400, {})
            except (OSError, sqlite3.Error):
                self.reply(503, {})
    return Handler


SNOWFLAKE = re.compile(r"[0-9]{17,20}\Z")


@dataclasses.dataclass(frozen=True)
class MirrorConfig:
    """Pinned, operator-verified routes. No discovery or channel creation while sending."""
    guild: str
    category: str
    application: str
    bot_user: str
    since: int
    routes: dict
    shared_channels: tuple = ()

    @classmethod
    def load(cls):
        if os.getenv("DISCORD_MIRROR_ENABLED", "false") != "true":
            return None
        data = json.loads(os.environ["DISCORD_MIRROR_CONFIG"])
        required = {"guild", "category", "application", "bot_user", "since", "routes"}
        if (not isinstance(data, dict) or set(data) not in (required, required | {"shared_channels"})
                or any(not isinstance(data[k], str) or not SNOWFLAKE.fullmatch(data[k])
                       for k in ("guild", "category", "application", "bot_user"))
                or type(data["since"]) is not int or not 1_700_000_000 <= data["since"] <= int(time.time())
                or not isinstance(data["routes"], dict) or not 1 <= len(data["routes"]) <= 100):
            raise ValueError("Invalid mirror activation")
        destinations = set()
        for channel, route in data["routes"].items():
            if (not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel) or not isinstance(route, dict)
                    or set(route) != {"name", "discord"} or not isinstance(route["name"], str)
                    or not route["name"] or len(route["name"]) > 100
                    or not isinstance(route["discord"], str) or not SNOWFLAKE.fullmatch(route["discord"])
                    or route["discord"] in destinations):
                raise ValueError("Invalid or duplicate mirror route")
            destinations.add(route["discord"])
        shared = data.get('shared_channels', [])
        if (not isinstance(shared, list) or any(not isinstance(v, str) or v not in data['routes'] for v in shared)
                or len(set(shared)) != len(shared)):
            raise ValueError('Invalid mirror shared-channel opt-in')
        data['shared_channels'] = tuple(shared)
        return cls(**data)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(dataclasses.asdict(self), sort_keys=True).encode()).hexdigest()

    def source_policy(self, policy):
        return dataclasses.replace(policy, channels=tuple(self.routes), channel_mode='explicit',
                                   group_dm_ids=(), shared_channel_ids=self.shared_channels)


def clean_mirror_name(value):
    return re.sub(r'[\r\n`*_<>@]', '', value).strip()[:100] if isinstance(value,str) else ''


def mirror_event(payload, cfg, policy, *, existing=False):
    """Only authenticated callbacks reach this function. Keep source text inert."""
    if (not policy.enabled or not isinstance(payload, dict) or payload.get('type') != 'event_callback'
            or payload.get('team_id') != policy.team):
        return None
    e = payload.get('event')
    if not isinstance(e, dict) or e.get('type') != 'message' or not isinstance(e.get('channel'),str) or e.get('channel') not in cfg.routes:
        return None
    if e.get('team', policy.team) != policy.team or (payload.get('is_ext_shared_channel')
            and e['channel'] not in cfg.shared_channels):
        return None
    eid = payload.get('event_id')
    if not isinstance(eid, str) or not re.fullmatch(r'Ev[A-Za-z0-9]{1,100}', eid):
        return None
    subtype = e.get('subtype')
    if subtype == 'message_replied':
        return None  # Thread metadata is not a second copy of its parent.
    deleted = subtype == 'message_deleted'
    message = e.get('message') if subtype == 'message_changed' else e
    if deleted:
        message = {'ts': e.get('deleted_ts')}
    if not isinstance(message, dict):
        return None
    if message.get('subtype') == 'tombstone':
        deleted = True
    ts = message.get('ts')
    version = e.get('event_ts', e.get('ts', ts))
    if (not all(isinstance(v, str) and re.fullmatch(r'[0-9]{10}\.[0-9]{6}', v) for v in (ts, version))
            or (int(ts.split('.')[0]) < cfg.since and not existing)):
        return None  # No backfill and no mirroring edits to old, unmirrored messages.
    thread = message.get('thread_ts', ts)
    if not isinstance(thread, str) or not re.fullmatch(r'[0-9]{10}\.[0-9]{6}', thread):
        return None
    text = message.get('text', '')
    if not isinstance(text, str):
        return None
    # text is the Slack accessibility fallback; preserve attachment and Block Kit text too.
    if not text:
        fragments = []
        def collect(value):
            if isinstance(value, dict):
                if value.get('type') in ('plain_text', 'mrkdwn', 'text') and isinstance(value.get('text'), str):
                    fragments.append(value['text'])
                else:
                    for child in value.values():collect(child)
            elif isinstance(value, list):
                for child in value:collect(child)
        collect(message.get('blocks', []))
        text = '\n'.join(fragments)
    attachments = message.get('attachments', [])
    if isinstance(attachments, list):
        for item in attachments[:50]:
            if isinstance(item, dict):
                fallback = item.get('text') or item.get('fallback') or item.get('title')
                if isinstance(fallback, str) and fallback and fallback not in text:
                    text += '\n' + fallback
    files = []
    if isinstance(message.get('files'), list):
        for f in message['files'][:100]:
            if not isinstance(f, dict):continue
            url = f.get('url_private_download') or f.get('url_private', '')
            name = f.get('name') or f.get('title') or 'attachment'
            size = f.get('size')
            if not isinstance(name, str):name = 'attachment'
            parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
            if (parsed and parsed.scheme == 'https' and parsed.hostname == 'files.slack.com'
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password
                    and type(size) is int and 0 <= size <= 10 * 1024 * 1024):
                files.append({'name': re.sub(r'[^\w. -]', '_', name)[:100] or 'attachment',
                              'url': url, 'size': size})
            else:
                # Keep the message even if an external/oversize file cannot be uploaded.
                text += '\n[Attachment unavailable for automatic upload: ' + name[:100] + ']'
    profile = message.get('user_profile')
    display = (profile.get('display_name') or profile.get('real_name')) if isinstance(profile, dict) else None
    bot_profile = message.get('bot_profile')
    bot_name = bot_profile.get('name') if isinstance(bot_profile, dict) else None
    author_id = message.get('user') or (bot_profile.get('user_id') if isinstance(bot_profile, dict) else None)
    if not isinstance(author_id, str) or not re.fullmatch(r'U[A-Z0-9]{8,}', author_id):author_id = ''
    supplied_name = display or message.get('username') or bot_name or ''
    author = supplied_name or author_id or message.get('bot_id') or 'Slack'
    author = re.sub(r'[\r\n`*_<>@]', '', str(author))[:100]
    data = {'text': text, 'files': files, 'author': author, 'thread': thread,
            'schema': 2, 'author_id': author_id, 'author_name': clean_mirror_name(supplied_name),
            'thread_explicit': 'thread_ts' in message,
            'broadcast': subtype == 'thread_broadcast' or message.get('subtype') == 'thread_broadcast'
                         or message.get('reply_broadcast') is True,
            'reference_only': subtype == 'thread_broadcast'}
    root = message.get('root') or e.get('root')
    if isinstance(root,dict) and root.get('ts') == thread and thread != ts:
        normalized = mirror_context(root,e['channel'],thread,cfg,policy)
        if normalized:data['root_data'] = normalized
    if len(json.dumps(data).encode()) > 512 * 1024:
        raise ValueError('Mirror message exceeds durable bounds')
    return {'event_id': eid, 'channel': e['channel'], 'ts': ts,
            'version': int(version.replace('.', '')), 'deleted': deleted,
            'create': subtype not in ('message_changed', 'message_deleted'), 'data': data}


def mirror_context(message,channel,ts,cfg,policy):
    """Normalize one verified parent snapshot; this never requests a history page."""
    if not isinstance(message,dict) or message.get('ts') != ts:return None
    selected = {k:v for k,v in message.items() if k not in ('root','team')}
    selected.update(type='message',channel=channel,thread_ts=ts)
    item = mirror_event({'type':'event_callback','team_id':policy.team,'event_id':'EvThreadContext','event':selected},
                        cfg,policy,existing=True)
    return item['data'] if item and not item['deleted'] else None


class MirrorStore(Store):
    """Separate tables preserve the existing mention receipts and policy hash."""
    def __init__(self, path, cfg, policy, *, recover=True):
        self.path, self.cfg, self.policy = path, cfg, policy
        with self.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS mirror_messages (
                channel TEXT, ts TEXT, version INTEGER NOT NULL, generation INTEGER NOT NULL,
                config_hash TEXT NOT NULL, data TEXT NOT NULL, create_seen INTEGER NOT NULL,
                deleted INTEGER NOT NULL, state TEXT NOT NULL, due REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(channel,ts))''')
            c.execute('CREATE TABLE IF NOT EXISTS mirror_events (event_id TEXT PRIMARY KEY)')
            c.execute('''CREATE TABLE IF NOT EXISTS mirror_parts (
                channel TEXT, ts TEXT, part INTEGER, discord_id TEXT, digest TEXT,
                state TEXT NOT NULL, PRIMARY KEY(channel,ts,part))''')
            c.execute('CREATE TABLE IF NOT EXISTS mirror_control (name TEXT PRIMARY KEY, due REAL NOT NULL)')
            # v1 receipts stay intact. Seed their channel copies once; never replay clean rows.
            c.execute('''CREATE TABLE IF NOT EXISTS mirror_copies (
                channel TEXT, ts TEXT, lane TEXT, part INTEGER, destination TEXT NOT NULL,
                discord_id TEXT, digest TEXT, state TEXT NOT NULL,
                PRIMARY KEY(channel,ts,lane,part))''')
            for channel, route in cfg.routes.items():
                c.execute("INSERT OR IGNORE INTO mirror_copies SELECT channel,ts,'channel',part,?,discord_id,digest,state "
                          'FROM mirror_parts WHERE channel=?', (route['discord'], channel))
            c.execute('''CREATE TABLE IF NOT EXISTS mirror_threads (
                channel TEXT, root_ts TEXT, starter_id TEXT NOT NULL, thread_id TEXT,
                state TEXT NOT NULL, PRIMARY KEY(channel,root_ts))''')
            c.execute('CREATE TABLE IF NOT EXISTS mirror_authors (user_id TEXT PRIMARY KEY,name TEXT NOT NULL,checked REAL NOT NULL)')
            if recover:
                # Only the independently locked worker owns crash recovery.
                c.execute("UPDATE mirror_messages SET state='queued' WHERE state='working'")
                c.execute("UPDATE mirror_parts SET state='uncertain' WHERE state='sending'")
                c.execute("UPDATE mirror_copies SET state='uncertain' WHERE state='sending'")
                c.execute("UPDATE mirror_threads SET state='uncertain' WHERE state='creating'")
                c.execute("UPDATE mirror_messages SET state='uncertain' WHERE EXISTS (SELECT 1 FROM mirror_parts p "
                          "WHERE p.channel=mirror_messages.channel AND p.ts=mirror_messages.ts AND p.state='uncertain')")
                c.execute("UPDATE mirror_messages SET state='uncertain' WHERE EXISTS (SELECT 1 FROM mirror_copies p "
                          "WHERE p.channel=mirror_messages.channel AND p.ts=mirror_messages.ts AND p.state='uncertain')")

    def heartbeat(self, now, *, paused=False):
        with self.connect() as c:
            c.execute("INSERT INTO mirror_control VALUES('worker',?) ON CONFLICT(name) DO UPDATE SET due=excluded.due", (now,))
            c.execute("INSERT INTO mirror_control VALUES('paused',?) ON CONFLICT(name) DO UPDATE SET due=excluded.due", (int(paused),))

    def worker_paused(self):
        with self.connect() as c:
            value=c.execute("SELECT due FROM mirror_control WHERE name='paused'").fetchone()
            return bool(value and value[0])

    def worker_alive(self, now):
        with self.connect() as c:
            value = c.execute("SELECT due FROM mirror_control WHERE name='worker'").fetchone()
            return bool(value and 0 <= now - value[0] < 300)

    def enqueue(self, payload):
        candidate = payload.get('event', {}) if isinstance(payload, dict) else {}
        if not isinstance(candidate,dict):return
        source = candidate.get('message', candidate) if isinstance(candidate, dict) else {}
        ts = candidate.get('deleted_ts') if candidate.get('subtype') == 'message_deleted' else source.get('ts') if isinstance(source, dict) else None
        with self.connect() as c:
            existing = c.execute('SELECT 1 FROM mirror_messages WHERE channel=? AND ts=?', (candidate.get('channel'), ts)).fetchone() if isinstance(candidate.get('channel'),str) and isinstance(ts,str) else None
        item = mirror_event(payload, self.cfg, self.policy, existing=bool(existing))
        if item is None:return
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('INSERT OR IGNORE INTO mirror_events VALUES(?)', (item['event_id'],)).rowcount:return
            row = c.execute('SELECT * FROM mirror_messages WHERE channel=? AND ts=?',
                            (item['channel'], item['ts'])).fetchone()
            if row:
                # Deletion is permanent. Delayed creates cannot overwrite newer edits.
                if row['deleted'] or row['state'] == 'uncertain' or row['config_hash'] != self.cfg.fingerprint:return
                create_seen = row['create_seen'] or item['create']
                previous = json.loads(row['data'])
                item['data']['broadcast'] = bool(previous.get('broadcast') or item['data']['broadcast'])
                if item['data'].get('reference_only') and not previous.get('reference_only'):
                    if not item['data']['files']:item['data']['files'] = previous.get('files',[])
                    if not item['data']['text']:item['data']['text'] = previous.get('text','')
                    item['data']['reference_only'] = False
                if previous.get('reference_only') and not item['data'].get('reference_only'):
                    item['version'] = max(item['version'],row['version'])
                if not item['data']['thread_explicit']:item['data']['thread'] = previous['thread']
                if not item['data']['author_id']:item['data']['author_id'] = previous.get('author_id', '')
                if not item['data']['author_name']:item['data']['author_name'] = previous.get('author_name', '')
                if 'root_data' not in item['data'] and 'root_data' in previous:item['data']['root_data']=previous['root_data']
                if len(json.dumps(item['data']).encode())>512*1024:item['data'].pop('root_data',None)
                if item['version'] < row['version'] and not item['deleted']:
                    if item['data']['broadcast'] and not previous.get('broadcast'):
                        previous['broadcast'] = True
                        previous['schema'] = item['data']['schema']
                        c.execute("UPDATE mirror_messages SET data=?,generation=generation+1,create_seen=?,state=CASE WHEN state='working' THEN state ELSE 'queued' END "
                                  'WHERE channel=? AND ts=?', (json.dumps(previous),int(create_seen),item['channel'],item['ts']))
                    else:
                        c.execute('UPDATE mirror_messages SET create_seen=? WHERE channel=? AND ts=?',
                                  (int(create_seen), item['channel'], item['ts']))
                    return
                c.execute('UPDATE mirror_messages SET version=?,generation=generation+1,data=?,create_seen=?,deleted=?,state=?,attempts=0 '
                          'WHERE channel=? AND ts=?', (item['version'], json.dumps(item['data']), int(create_seen),
                          int(item['deleted']), 'working' if row['state'] == 'working' else 'queued', item['channel'], item['ts']))
            else:
                c.execute('INSERT INTO mirror_messages VALUES(?,?,?,1,?,?,?,?,\'queued\',0,0)',
                          (item['channel'], item['ts'], item['version'], self.cfg.fingerprint,
                           json.dumps(item['data']), int(item['create']), int(item['deleted'])))

    def claim(self, now):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            gate = c.execute("SELECT due FROM mirror_control WHERE name='discord'").fetchone()
            if gate and gate[0] > now:return None
            row = c.execute("SELECT * FROM mirror_messages WHERE state='queued' AND due<=? "
                            "AND (create_seen=1 OR deleted=1) ORDER BY due,rowid LIMIT 1", (now,)).fetchone()
            if row:
                valid = self.policy.enabled and row['config_hash'] == self.cfg.fingerprint
                c.execute('UPDATE mirror_messages SET state=?,attempts=attempts+1 WHERE channel=? AND ts=?',
                          ('working' if valid else 'failed', row['channel'], row['ts']))
                return row if valid else None

    def parts(self, row):
        with self.connect() as c:
            return list(c.execute('SELECT * FROM mirror_copies WHERE channel=? AND ts=? ORDER BY lane,part', (row['channel'], row['ts'])))

    def finish(self, row, state, due=0, *, cooldown=True):
        with self.connect() as c:
            # If another event arrived during HTTP, process its newer desired state next.
            c.execute("UPDATE mirror_messages SET state=CASE WHEN generation!=? AND ?='clean' THEN 'queued' ELSE ? END,due=? "
                      'WHERE channel=? AND ts=?', (row['generation'], state, state, due, row['channel'], row['ts']))
            if due and cooldown:
                c.execute("INSERT INTO mirror_control VALUES('discord',?) ON CONFLICT(name) DO UPDATE SET due=max(due,excluded.due)", (due,))

    def begin_write(self, row, part, ident, digest, *, lane='channel', destination=None):
        with self.connect() as c:
            c.execute("INSERT INTO mirror_copies VALUES(?,?,?,?,?,?,?,'sending') ON CONFLICT(channel,ts,lane,part) "
                      "DO UPDATE SET state='sending'", (row['channel'], row['ts'], lane, part,
                      destination or self.cfg.routes[row['channel']]['discord'], ident, digest))

    def written(self, row, part, ident, digest, state='sent', *, lane='channel'):
        with self.connect() as c:
            c.execute('UPDATE mirror_copies SET discord_id=?,digest=?,state=? WHERE channel=? AND ts=? AND lane=? AND part=?',
                      (ident, digest, state, row['channel'], row['ts'], lane, part))

    def message(self, channel, ts):
        with self.connect() as c:return c.execute('SELECT * FROM mirror_messages WHERE channel=? AND ts=?', (channel,ts)).fetchone()

    def add_context(self, channel, ts, data):
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO mirror_messages VALUES(?,?,?,1,?,?,1,0,'queued',0,0)",
                      (channel,ts,int(ts.replace('.','')),self.cfg.fingerprint,json.dumps(data)))

    def thread(self, channel, ts):
        with self.connect() as c:return c.execute('SELECT * FROM mirror_threads WHERE channel=? AND root_ts=?', (channel,ts)).fetchone()

    def thread_state(self, channel, ts, starter, state, thread_id=None):
        with self.connect() as c:
            c.execute('INSERT INTO mirror_threads VALUES(?,?,?,?,?) ON CONFLICT(channel,root_ts) DO UPDATE SET thread_id=excluded.thread_id,state=excluded.state',
                      (channel,ts,starter,thread_id,state))

    def resolve_author(self, row, slack, now):
        data = json.loads(row['data'])
        ident = data.get('author_id') or (data.get('author') if re.fullmatch(r'U[A-Z0-9]{8,}', data.get('author','')) else '')
        if not ident or data.get('author_name'):return row
        with self.connect() as c:cached = c.execute('SELECT * FROM mirror_authors WHERE user_id=?', (ident,)).fetchone()
        name = cached['name'] if cached else ''
        if cached is None or now-cached['checked'] > (86400 if name else 300):
            status, value = slack.call('users.info', {'user':ident})
            if status == 'sent' and isinstance(value.get('user'),dict) and value['user'].get('id') == ident:
                profile = value['user'].get('profile',{})
                name = clean_mirror_name(profile.get('display_name') or profile.get('real_name') or value['user'].get('name')) or name
            with self.connect() as c:
                c.execute('INSERT INTO mirror_authors VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,checked=excluded.checked', (ident,name,now))
                c.execute('DELETE FROM mirror_authors WHERE user_id IN (SELECT user_id FROM mirror_authors ORDER BY checked DESC LIMIT -1 OFFSET 2000)')
        data.update(author_id=ident,author_name=name)
        with self.connect() as c:
            c.execute('UPDATE mirror_messages SET data=? WHERE channel=? AND ts=? AND generation=?',
                      (json.dumps(data),row['channel'],row['ts'],row['generation']))
        return dict(row,data=json.dumps(data))

    def counts(self):
        with self.connect() as c:
            return dict(c.execute('SELECT state,count(*) FROM mirror_messages GROUP BY state'))


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class DiscordMirror:
    def __init__(self, cfg, token):
        self.cfg, self.token = cfg, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoCredentialRedirect())

    def call(self, method, path, body=None, files=None):
        headers = {'Authorization': 'Bot ' + self.token, 'User-Agent': 'YukidarumaMirror/1'}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        if files:
            boundary = 'mirror-' + uuid.uuid4().hex
            headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
            data = ('--' + boundary + '\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                    'Content-Type: application/json\r\n\r\n').encode() + json.dumps(body).encode() + b'\r\n'
            for i, (name, raw) in enumerate(files):
                # Names were sanitized at ingress; never interpolate source headers.
                data += ('--' + boundary + '\r\nContent-Disposition: form-data; name="files[' + str(i)
                         + ']"; filename="' + name + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + raw + b'\r\n'
            data += ('--' + boundary + '--\r\n').encode()
        req = urllib.request.Request('https://discord.com/api/v10' + path, data, headers, method=method)
        try:
            with self.opener.open(req, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:return 'uncertain', None
                result = json.loads(raw) if raw else {}
                return ('sent', result) if isinstance(result, (dict, list)) else ('uncertain', None)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                try:
                    value = json.loads(error.read(4096)).get('retry_after')
                    if type(value) in (int, float) and 0 < value <= 86400:return 'rate_limited', max(1, value)
                except (ValueError, TypeError, AttributeError):pass
                return 'failed', None
            if error.code == 404 and method == 'DELETE':return 'sent', {}
            if error.code == 404 and method == 'GET':return 'missing', None
            return ('failed', None) if 400 <= error.code < 500 else ('uncertain', None)
        except (OSError, ValueError):
            return 'uncertain', None

    def allowed(self, channel, name, *, uploads=False, thread=None, create_thread=False):
        """Verify identity, guild/category/name and effective channel permissions each time."""
        def get(path):
            state, data = self.call('GET', path)
            if state != 'sent':raise RosterHeld('discord_verification_unavailable')
            return data
        cfg = self.cfg
        user, app = get('/users/@me'), get('/oauth2/applications/@me')
        if user.get('id') != cfg.bot_user or user.get('bot') is not True or app.get('id') != cfg.application:
            return False
        category = get('/channels/' + cfg.category)
        dest = get('/channels/' + channel)
        if (category.get('guild_id') != cfg.guild or category.get('type') != 4 or category.get('name') != 'slack'
                or dest.get('guild_id') != cfg.guild or dest.get('type') != 0
                or dest.get('parent_id') != cfg.category or dest.get('name') != name):return False
        member = get('/guilds/' + cfg.guild + '/members/' + cfg.bot_user)
        roles = get('/guilds/' + cfg.guild + '/roles')
        member_roles = set(member.get('roles', [])) | {cfg.guild}
        permissions = 0
        for role in roles:
            if role.get('id') in member_roles:permissions |= int(role['permissions'])
        if not permissions & 8:
            overrides = dest.get('permission_overwrites', [])
            for overwrite in overrides:
                if overwrite.get('id') == cfg.guild:
                    permissions = (permissions & ~int(overwrite['deny'])) | int(overwrite['allow'])
            deny = allow = 0
            for overwrite in overrides:
                if overwrite.get('type') == 0 and overwrite.get('id') in member_roles - {cfg.guild}:
                    deny |= int(overwrite['deny']);allow |= int(overwrite['allow'])
            permissions = (permissions & ~deny) | allow
            for overwrite in overrides:
                if overwrite.get('type') == 1 and overwrite.get('id') == cfg.bot_user:
                    permissions = (permissions & ~int(overwrite['deny'])) | int(overwrite['allow'])
            required = (1 << 10) | ((1 << 38) if thread or create_thread else (1 << 11)) | ((1 << 15) if uploads else 0)
            if create_thread:required |= 1 << 35
            if permissions & required != required:return False
        if thread:
            data = get('/channels/' + thread)
            if not valid_mirror_thread(data,cfg,channel,thread):return False
        return member.get('user', {}).get('id') == cfg.bot_user and not member.get('communication_disabled_until')

    def download(self, file, slack_token):
        request = urllib.request.Request(file['url'], headers={'Authorization': 'Bearer ' + slack_token})
        with self.opener.open(request, timeout=15) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024 or len(raw) != file['size']:
            raise RosterHeld('attachment_download_invalid')
        return file['name'], raw


def mirror_parts(row, cfg, *, workspace_url=''):
    data = json.loads(row['data'])
    text = html.unescape(data['text'])
    text = re.sub(r'<@([A-Z0-9]+)(?:\|[^>]+)?>', r'@\1', text)
    text = re.sub(r'<#[A-Z0-9]+\|([^>]+)>', r'#\1', text)
    text = re.sub(r'<!(?:here|channel|everyone)>', '@channel', text)
    text = re.sub(r'<(https?://[^>|]+)\|([^>]+)>', r'\2 (\1)', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    link = 'https://app.slack.com/archives/' + row['channel'] + '/p' + row['ts'].replace('.', '')
    ident = data.get('author_id') or (data.get('author') if re.fullmatch(r'U[A-Z0-9]{8,}',data.get('author','')) else '')
    name = data.get('author_name') or (data.get('author') if data.get('author') != ident else '') or 'Slack user'
    name = clean_mirror_name(name) or 'Slack user'
    # Only the workspace origin returned by authenticated auth.test is trusted.
    # Message-supplied names/URLs cannot choose the destination or break the label.
    if (isinstance(workspace_url,str) and re.fullmatch(r'https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.slack\.com/',workspace_url)
            and isinstance(ident,str) and re.fullmatch(r'U[A-Z0-9]{8,}',ident)):
        label = re.sub(r'([\\\[\]()])', r'\\\1', name)
        name = '[' + label + '](' + workspace_url + 'team/' + ident + ')'
    header = name + ' · Slack\n'
    # UTF-16 units, rather than Python characters, match Discord's content limit.
    chunks, chunk, units = [], '', 0
    budget = min(1600,2000-len((header+'\n'+link).encode('utf-16-le'))//2)
    if budget < 100:raise ValueError('Mirror header exceeds bounds')
    for char in text or '[Attachment / empty message]':
        width = len(char.encode('utf-16-le')) // 2
        if units + width > budget:
            chunks.append(chunk);chunk, units = '', 0
        chunk += char;units += width
    chunks.append(chunk)
    # Bound each upload request to 10 files / 10 MiB; extra batches are durable parts.
    groups, group, size = [], [], 0
    for file in data['files']:
        if group and (len(group) == 10 or size + file['size'] > 10 * 1024 * 1024):
            groups.append(group);group, size = [], 0
        group.append(file);size += file['size']
    if group:groups.append(group)
    result = []
    for i in range(max(len(chunks), len(groups))):
        part = {'content': header + (chunks[i] if i < len(chunks) else '[Attachments continued]') + '\n' + link,
                'files': groups[i] if i < len(groups) else []}
        part['digest'] = hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()
        result.append(part)
    return result


def valid_mirror_thread(data,cfg,parent,ident):
    return (isinstance(data,dict) and data.get('id') == ident and data.get('guild_id') == cfg.guild
            and data.get('parent_id') == parent and data.get('type') == 11)


def ensure_mirror_thread(store,row,policy,slack,discord):
    """One thread per recorded starter ID; unknown creates never trigger another POST."""
    cfg=store.cfg;route=cfg.routes[row['channel']];data=json.loads(row['data']);root_ts=data['thread']
    ledger=store.thread(row['channel'],root_ts)
    starter=ledger['starter_id'] if ledger else None
    if starter is None:
        root=store.message(row['channel'],root_ts)
        if root is None:
            context=data.get('root_data')
            if context is None:
                state,value=slack.call('conversations.history',{'channel':row['channel'],'oldest':root_ts,
                                      'latest':root_ts,'inclusive':'true','limit':1})
                if state == 'rate_limited':return 'rate_limited',value
                if state not in ('sent','failed'):return 'retry',None
                messages=value.get('messages',[]) if state == 'sent' and isinstance(value,dict) else []
                context=mirror_context(messages[0],row['channel'],root_ts,cfg,policy) if len(messages)==1 else None
            if context is None:
                context={'schema':2,'text':'[Slack parent message unavailable]','files':[], 'author':'Slack',
                         'author_name':'Slack','author_id':'','thread':root_ts,'broadcast':False,'thread_explicit':True}
            store.add_context(row['channel'],root_ts,context)
            return 'wait',None
        if root['deleted'] or root['state'] in ('uncertain','failed'):return 'blocked',None
        posted=next((p for p in store.parts(root) if p['lane']=='channel' and p['part']==0
                     and p['state']=='sent' and p['discord_id']),None)
        if posted is None:return 'wait',None
        starter=posted['discord_id']
        if not isinstance(starter,str) or not SNOWFLAKE.fullmatch(starter):return 'blocked',None
    status,info=discord.call('GET','/channels/'+starter)
    if status == 'rate_limited':return status,info
    if status == 'sent':
        if not valid_mirror_thread(info,cfg,route['discord'],starter):return 'blocked',None
        if info.get('thread_metadata',{}).get('locked'):return 'blocked',None
        store.thread_state(row['channel'],root_ts,starter,'ready',starter)
        if info.get('thread_metadata',{}).get('archived'):
            status,value=discord.call('PATCH','/channels/'+starter,{'archived':False})
            if status=='rate_limited':return status,value
            if status!='sent':return 'retry',None  # Re-read the idempotent state before another attempt.
            if not valid_mirror_thread(value,cfg,route['discord'],starter):return 'blocked',None
            return 'changed',starter
        return 'ready',starter
    if status != 'missing':return 'retry',None
    if ledger and ledger['state'] in ('uncertain','creating','ready'):return 'uncertain',None
    if not discord.allowed(route['discord'],route['name'],create_thread=True):return 'blocked',None
    root=store.message(row['channel'],root_ts)
    title=clean_mirror_name(re.sub(r'\s+',' ',json.loads(root['data'])['text']))[:80] or 'Slack thread'
    store.thread_state(row['channel'],root_ts,starter,'creating')
    try:status,value=discord.call('POST','/channels/'+route['discord']+'/messages/'+starter+'/threads',
                                {'name':title,'auto_archive_duration':1440})
    except Exception:status,value='uncertain',None
    if status == 'rate_limited':
        store.thread_state(row['channel'],root_ts,starter,'pending');return status,value
    if status=='sent' and valid_mirror_thread(value,cfg,route['discord'],starter):
        store.thread_state(row['channel'],root_ts,starter,'ready',starter);return 'changed',starter
    # Creating from a message has a deterministic ID, so readback can reconcile a lost response.
    check,thread=discord.call('GET','/channels/'+starter)
    if check=='sent' and valid_mirror_thread(thread,cfg,route['discord'],starter):
        store.thread_state(row['channel'],root_ts,starter,'ready',starter);return 'changed',starter
    store.thread_state(row['channel'],root_ts,starter,'failed' if status=='failed' else 'uncertain')
    return ('blocked' if status=='failed' else 'uncertain'),None


def process_mirror_one(store, policy, slack, discord, clock=None):
    clock = clock or time.time
    row = store.claim(clock())
    if row is None:return False
    cfg = store.cfg
    route = cfg.routes[row['channel']]
    parts = store.parts(row)
    desired = []
    # Only ordinary, mapped channels. Reuse the established membership/shared-workspace guard.
    scoped = cfg.source_policy(policy)
    try:
        permission, delay = slack.conversation_allowed(scoped, row['channel'])
        if permission != 'allowed':
            store.finish(row, 'queued' if permission == 'retry' else 'failed', clock() + (delay or 30) if permission == 'retry' else 0)
            return True
        state, info = slack.call('conversations.info', {'channel': row['channel']})
        if state != 'sent':raise RosterHeld('slack_route_read_unavailable')
        if info.get('channel', {}).get('name') != route['name'] or info['channel'].get('is_mpim'):
            store.finish(row, 'failed');return True
        if not row['deleted']:
            row=store.resolve_author(row,slack,clock())
            desired=mirror_parts(row,cfg,workspace_url=getattr(slack,'workspace_url',''))
        if not discord.allowed(route['discord'], route['name'], uploads=any(p['files'] for p in desired)):
            store.finish(row, 'failed');return True
    except Exception:
        store.finish(row, 'queued', clock() + 30);return True
    data=json.loads(row['data']);thread_id=None
    lanes={'channel':route['discord']}
    if desired and data.get('schema',1)>=2 and data['thread']!=row['ts']:
        try:
            status,value=ensure_mirror_thread(store,row,policy,slack,discord)
            if status!='ready':
                store.finish(row,'uncertain' if status=='uncertain' else 'queued',
                             clock()+(value if status=='rate_limited' else 2 if status in ('wait','changed') else 30),
                             cooldown=status=='rate_limited')
                return True
            thread_id=value
            if not discord.allowed(route['discord'],route['name'],thread=thread_id,uploads=any(p['files'] for p in desired)):
                store.finish(row,'queued',clock()+30,cooldown=False);return True
        except Exception:
            store.finish(row,'queued',clock()+30,cooldown=False);return True
        lanes={'thread':thread_id}
        if data.get('broadcast'):lanes['channel']=route['discord']
    targets={(lane,index):(destination,part) for lane,destination in lanes.items() for index,part in enumerate(desired)}
    by_index = {(p['lane'],p['part']):p for p in parts}
    operation = None
    # Delete surplus parts before updates, including multipart shrink and full deletion.
    for p in reversed(parts):
        if (p['lane'],p['part']) not in targets and p['state'] != 'deleted':
            operation = ('DELETE',p['lane'],p['part'],p['destination'],p,None);break
    if operation is None:
        for (lane,index),(destination,target_part) in targets.items():
            p = by_index.get((lane,index))
            if p and p['destination']!=destination:
                store.finish(row,'failed');return True
            if p is None or p['state'] == 'deleted' or p['digest'] != target_part['digest']:
                operation = ('PATCH' if p and p['discord_id'] and p['state'] != 'deleted' else 'POST',lane,index,destination,p,target_part);break
    if operation is None:
        store.finish(row, 'clean');return True
    method,lane,index,destination,old,target_part=operation
    if lane=='channel' and destination!=route['discord']:
        store.finish(row,'failed');return True
    if lane=='thread':
        ledger=store.thread(row['channel'],data['thread'])
        try:
            permitted=bool(ledger and ledger['thread_id']==destination and
                           discord.allowed(route['discord'],route['name'],thread=destination,uploads=bool(target_part and target_part['files'])))
        except Exception:permitted=False
        if not permitted:
            store.finish(row,'queued',clock()+30,cooldown=False);return True
    ident = old['discord_id'] if old else None
    digest = target_part['digest'] if target_part else ''
    if method == 'DELETE' and ident is None:
        # A definitive rejection/429 never created a Discord object.
        store.written(row, index, None, '', 'deleted',lane=lane)
        store.finish(row, 'queued');return True
    path = '/channels/' + destination + '/messages' + ('/' + ident if method != 'POST' else '')
    body, files = None, []
    if target_part:
        body = {'content': target_part['content'], 'allowed_mentions': {'parse': [], 'users': [], 'roles': [], 'replied_user': False},
                'flags': 4, 'attachments': []}
        if method == 'POST':
            body.update(nonce=hashlib.sha256((policy.team + ':' + row['channel'] + ':' + row['ts'] + ':'
                                            +lane+':'+str(index)+':'+str(row['generation'])).encode()).hexdigest()[:24], enforce_nonce=True)
        try:
            files = [discord.download(f, slack.token) for f in target_part['files']]
            body['attachments'] = [{'id': i, 'filename': f[0]} for i, f in enumerate(files)]
        except Exception:
            store.finish(row, 'queued', clock() + 30);return True
    store.begin_write(row,index,ident,digest,lane=lane,destination=destination)
    try:
        state, value = discord.call(method, path, body, files)
    except Exception:
        state, value = 'uncertain', None
    if state == 'sent':
        if method != 'DELETE':
            ident = value.get('id')
            if (not isinstance(ident, str) or not SNOWFLAKE.fullmatch(ident)
                    or value.get('channel_id') != destination or value.get('author', {}).get('id') != cfg.bot_user):
                state = 'uncertain'
        if state == 'sent':
            store.written(row,index,ident,digest,'deleted' if method=='DELETE' else 'sent',lane=lane)
            store.finish(row, 'queued');return True
    if state == 'rate_limited':
        store.written(row,index,old['discord_id'] if old else None,old['digest'] if old else None,old['state'] if old else 'pending',lane=lane)
        store.finish(row, 'queued', clock() + value)
    elif state == 'failed':
        store.written(row,index,ident,old['digest'] if old else None,'failed',lane=lane)
        store.finish(row, 'failed')
    else:
        store.written(row,index,ident,digest,'uncertain',lane=lane)
        store.finish(row, 'uncertain')
    return True


def listen_address():
    host = os.getenv("BIND_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("Unsupported bind address")
    port = int(os.getenv("PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    return host, port


def main():
    cfg = Config.load()
    # Linux production lock is acquired before recovery can alter in-flight rows.
    import fcntl
    Path(cfg.database).parent.mkdir(parents=True, exist_ok=True)
    lock = open(cfg.database + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another bot instance owns the database")
    store = Store(cfg.database)
    slack = Slack(cfg.token)
    # Validate credential identity without posting a message.
    status, identity = slack.call("auth.test", {})
    if status != "sent" or identity.get("team_id") != cfg.team or identity.get("user_id") != cfg.bot_user:
        raise SystemExit("Slack identity validation failed")
    stop = threading.Event()
    health = {"authenticated": True, "worker": time.monotonic()}
    mirror_cfg = MirrorConfig.load()
    mirror = MirrorStore(cfg.database, mirror_cfg, cfg, recover=False) if mirror_cfg else None
    monitor = None
    if cfg.roster_mode == "fresh_sheet":
        health["roster"] = {"status": "not_read", "member_count": 0}
        monitor = RosterMonitor(FreshSheetRoster(cfg), health["roster"])
        monitor.probe_if_due()

    def worker():
        while not stop.is_set():
            try:
                process_one(store, cfg, slack, member_provider=monitor.read if monitor else None,
                            roster_health=health.get("roster"))
                if monitor:monitor.probe_if_due()
                health["worker"] = time.monotonic()
            except sqlite3.Error:
                pass  # health goes stale if durable processing is unavailable
            stop.wait(1.1)  # one worker; no more than one post per second

    # Only one service instance may own this database (enforced by systemd deployment).
    server = http.server.ThreadingHTTPServer(listen_address(), handler(cfg, store, health, mirror=mirror))
    server.daemon_threads = True
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def shutdown(*_args):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        stop.set()
        thread.join(timeout=20)
        server.server_close()


if __name__ == "__main__":
    main()
