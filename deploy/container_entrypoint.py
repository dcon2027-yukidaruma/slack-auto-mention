"""Read the existing private VM credential file; never print its contents."""
import json
import os
import stat
import sys

ALLOWED = {
    'SLACK_BOT_TOKEN', 'SLACK_SIGNING_SECRET', 'SLACK_TEAM_ID', 'SLACK_BOT_USER_ID',
    'SLACK_APP_ID', 'SLACK_CHANNEL_MODE', 'SLACK_CHANNEL_IDS', 'SLACK_GROUP_DM_IDS',
    'SLACK_MEMBER_IDS', 'ALLOWLIST_VERIFIED', 'BOT_ENABLED', 'DATABASE_PATH', 'PORT',
}


def load_private_environment(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError('Private configuration ownership or mode invalid')
        values = {}
        for line in stream:
            key, separator, value = line.rstrip('\n').partition('=')
            if not separator or key not in ALLOWED or key in values:
                raise ValueError('Unexpected configuration key')
            parsed = json.loads(value)
            if not isinstance(parsed, str) or any(c in parsed for c in ('\x00', '\n', '\r')):
                raise ValueError('Invalid configuration value')
            values[key] = parsed
    if not {'SLACK_BOT_TOKEN', 'SLACK_SIGNING_SECRET', 'SLACK_TEAM_ID', 'SLACK_BOT_USER_ID', 'SLACK_MEMBER_IDS', 'BOT_ENABLED', 'ALLOWLIST_VERIFIED'} <= values.keys():
        raise ValueError('Configuration incomplete')
    return values


def main():
    try:
        values = load_private_environment('/run/secrets/slack_runtime')
        # Secrets enter only this process memory, not Docker's configured Env.
        os.environ.update(values)
        os.environ.update(BIND_HOST='0.0.0.0', PORT='8765', DATABASE_PATH='/data/events.sqlite3')
        sys.path.insert(0, '/app')
        import bot
        bot.main()
    except Exception:
        raise SystemExit('Slack bot stopped; configuration/runtime details suppressed') from None


if __name__ == '__main__':
    main()
