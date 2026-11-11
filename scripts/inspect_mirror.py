"""Read-only VM route/permission inspection. Print metadata only; never post.

Run from a staged, reviewed checkout on the existing host, as its existing owner.
No credentials/configs are created, copied, changed or printed.
"""
import dataclasses
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import Config, MirrorConfig, DiscordMirror, Slack, SNOWFLAKE
from deploy.container_entrypoint import load_private_environment
from deploy.mirror_worker import existing_discord_token


def inspect():
    home = Path.home()
    os.environ.update(load_private_environment(home / '.config/slack-auto-mention/runtime.env'))
    policy = Config.load()
    slack = Slack(policy.token)
    state, identity = slack.call('auth.test', {})
    if state != 'sent' or identity.get('team_id') != policy.team or identity.get('user_id') != policy.bot_user:
        raise ValueError('Slack identity mismatch')
    # This existing file is nonsecret application identity/feature configuration.
    projects = json.loads((home / '.config/dcon-projects/config.json').read_text())
    identifiers = {key: projects[key] for key in ('application_id', 'bot_user_id', 'guild_id')}
    if any(not isinstance(v, str) or not SNOWFLAKE.fullmatch(v) for v in identifiers.values()):
        raise ValueError('Missing verified Projects identity')
    probe = MirrorConfig(identifiers['guild_id'], identifiers['guild_id'], identifiers['application_id'],
                         identifiers['bot_user_id'], int(time.time()), {})
    discord = DiscordMirror(probe, existing_discord_token(home / '.config/dcon-projects/runtime.env'))
    def get(path):
        state, data = discord.call('GET', path)
        if state != 'sent':raise ValueError('Discord metadata unavailable')
        return data
    user, app = get('/users/@me'), get('/oauth2/applications/@me')
    if (user.get('id') != probe.bot_user or user.get('bot') is not True or app.get('id') != probe.application
            or app.get('name') != 'DCON Projects Notifications'):
        raise ValueError('Existing Projects application identity mismatch')
    channels = get('/guilds/' + probe.guild + '/channels')
    categories = [c for c in channels if c.get('type') == 4 and c.get('name') == 'slack']
    if len(categories) != 1:raise ValueError('Expected exactly one slack category')
    category = categories[0]['id']
    discord.cfg = dataclasses.replace(probe, category=category)
    sources, cursor, seen = [], None, set()
    for _ in range(100):
        args = {'types': 'public_channel,private_channel', 'exclude_archived': 'true', 'limit': 200}
        if cursor:args['cursor'] = cursor
        state, data = slack.call('conversations.list', args)
        if state != 'sent' or not isinstance(data.get('channels'), list):
            raise ValueError('Complete Slack channel inventory unavailable with existing scopes')
        sources.extend(data['channels'])
        cursor = data.get('response_metadata', {}).get('next_cursor')
        if not cursor:break
        if cursor in seen:raise ValueError('Repeated Slack page cursor')
        seen.add(cursor)
    else:raise ValueError('Slack inventory exceeded bounds')
    results, routes, shared = [], {}, []
    destinations = [c for c in channels if c.get('parent_id') == category and c.get('type') == 0]
    for destination in destinations:
        name = destination['name']
        matches = [c for c in sources if c.get('name') == name and c.get('is_member') is True]
        entry = {'name': name, 'discord': destination['id'], 'matched_sources': len(matches)}
        if len(matches) == 1 and sum(c.get('name') == name for c in destinations) == 1:
            source = matches[0]
            scoped = dataclasses.replace(policy, channels=(source['id'],), channel_mode='explicit', group_dm_ids=(),
                                         shared_channel_ids=(source['id'],) if source.get('is_ext_shared') else ())
            permission, _ = slack.conversation_allowed(scoped, source['id'])
            allowed = discord.allowed(destination['id'], name, uploads=True)
            entry.update(slack=source['id'], private=bool(source.get('is_private')),
                         shared=bool(source.get('is_ext_shared')), source_allowed=permission == 'allowed', discord_allowed=allowed,
                         mention_shared_opt_in=policy.allows_shared(source['id']))
            if permission == 'allowed' and allowed and not source.get('is_mpim'):
                routes[source['id']] = {'name': name, 'discord': destination['id']}
                if source.get('is_ext_shared'):shared.append(source['id'])
        results.append(entry)
    return {'checked_at': int(time.time()), 'posts': 0, 'writes': 0, 'inventory_complete': True,
            'application': {'id': probe.application, 'name': app['name'], 'bot_user': probe.bot_user},
            'guild': probe.guild, 'category': category, 'channels': results,
            'all_category_text_channels_verified': bool(destinations) and len(routes) == len(destinations),
            'proposal': {'guild': probe.guild, 'category': category, 'application': probe.application,
                         'bot_user': probe.bot_user, 'routes': routes, 'shared_channels': sorted(shared)}}


if __name__ == '__main__':
    try:print(json.dumps(inspect(), ensure_ascii=True))
    except Exception:
        raise SystemExit('Mirror inspection held; existing identity, inventory or permission check failed; no writes performed') from None
