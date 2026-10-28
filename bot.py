"""Fixed-purpose Slack Events API bot. Python 3.11+, standard library only."""
import dataclasses
import hashlib
import hmac
import http.server
import json
import os
import re
import signal
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
        if mode == "explicit" and not channels and not groups:
            raise ValueError("No allowed conversations")
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
                   os.getenv("DATABASE_PATH", "data/events.sqlite3"), enabled, mode, groups)

    @property
    def fingerprint(self):
        value = [self.team, sorted(self.channels), sorted(self.members), self.bot_user,
                 self.channel_mode, sorted(self.group_dm_ids)]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def target(payload, cfg):
    """Validate the outer workspace and ordinary human message. Never parse text as commands."""
    if not cfg.enabled or not isinstance(payload, dict):
        return None
    if payload.get("type") != "event_callback" or payload.get("team_id") != cfg.team:
        return None
    if payload.get("is_ext_shared_channel"):
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
        if method in ("conversations.info", "conversations.members"):
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
        # Do not infer type from the ID prefix. External/Slack Connect is excluded.
        if info.get("id") != channel or info.get("is_archived") or info.get("is_ext_shared") or info.get("is_pending_ext_shared"):
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


def process_one(store, cfg, slack, clock=None):
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
    body = {"channel": row["channel"], "thread_ts": row["thread_ts"],
            "text": " ".join("<@" + member + ">" for member in cfg.members),
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


def handler(cfg, store, health):
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
            self.reply(200 if ready else 503, {"ready": ready, "revision": os.getenv("APP_REVISION", "unknown"),
                                             "events": counts})

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
                self.reply(200, {})
            except (ValueError, UnicodeError):
                self.reply(400, {})
            except (OSError, sqlite3.Error):
                self.reply(503, {})
    return Handler


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

    def worker():
        while not stop.is_set():
            try:
                process_one(store, cfg, slack)
                health["worker"] = time.monotonic()
            except sqlite3.Error:
                pass  # health goes stale if durable processing is unavailable
            stop.wait(1.1)  # one worker; no more than one post per second

    # Only one service instance may own this database (enforced by systemd deployment).
    server = http.server.ThreadingHTTPServer(("127.0.0.1", int(os.getenv("PORT", "8765"))), handler(cfg, store, health))
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
