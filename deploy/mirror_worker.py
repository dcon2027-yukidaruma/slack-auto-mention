"""Private host worker; reuse existing tokens in memory without credential copies.

The ingress container only commits events. This process owns the mirror lock,
recovery and Discord sends, and never parses or uses the Projects GitHub credential.
"""
import dataclasses
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import Config, MirrorConfig, MirrorStore, DiscordMirror, Slack, process_mirror_one
from deploy.container_entrypoint import load_private_environment


def existing_discord_token(path):
    if path.is_symlink() or path.resolve(strict=True) != path.absolute():
        raise ValueError('Invalid credential path')
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('Invalid private parent')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 16384):
            raise ValueError('Invalid credential permissions')
        token = None
        for line in stream:
            key, separator, value = line.rstrip('\n').partition('=')
            if key != 'DISCORD_BOT_TOKEN':continue
            if not separator or token is not None:raise ValueError('Invalid Discord credential')
            token = json.loads(value)
    if not isinstance(token, str) or not token or any(c in token for c in '\r\n\x00'):
        raise ValueError('Invalid Discord credential')
    return token


def run():
    import fcntl
    os.umask(0o077)
    home = Path.home()
    os.environ.update(load_private_environment(home / '.config/slack-auto-mention/runtime.env'))
    cfg = Config.load()
    mirror_cfg = MirrorConfig.load()
    if mirror_cfg is None or not cfg.enabled:raise ValueError('Mirror is disabled')
    cfg = dataclasses.replace(cfg, database=str(home / '.local/state/slack-auto-mention/events.sqlite3'))
    # The ingress process has a separate lock and must never recover mirror writes.
    lock = open(cfg.database + '.mirror.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = MirrorStore(cfg.database, mirror_cfg, cfg)
    slack = Slack(cfg.token)
    state, identity = slack.call('auth.test', {})
    if state != 'sent' or identity.get('team_id') != cfg.team or identity.get('user_id') != cfg.bot_user:
        raise ValueError('Slack identity mismatch')
    slack.workspace_url = identity.get('url', '')
    discord = DiscordMirror(mirror_cfg, existing_discord_token(home / '.config/dcon-projects/runtime.env'))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            paused = (home / '.config/slack-auto-mention/mirror-worker.pause').exists()
            if not paused:process_mirror_one(store, cfg, slack, discord)
            store.heartbeat(time.time(),paused=paused)
        except sqlite3.Error:
            pass  # A stale heartbeat makes ingress health report unavailable.
        stop.wait(1.1)


if __name__ == '__main__':
    try:run()
    except Exception:
        raise SystemExit('Discord mirror stopped; private details suppressed') from None
