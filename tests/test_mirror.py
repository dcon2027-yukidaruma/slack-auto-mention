"""Offline synchronization and permission regressions; no real service calls."""
import copy
import dataclasses
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from bot import (DiscordMirror, MirrorConfig, MirrorStore, Store, mirror_event,
                 mirror_parts, process_mirror_one, target)
from test_bot import config, event

GUILD, CATEGORY, APP, BOT, DEST = (str(v) * 18 for v in range(1, 6))


def settings():
    return MirrorConfig(GUILD, CATEGORY, APP, BOT, 1793000000,
                        {'C00000001': {'name': 'general', 'discord': DEST}})


def message(text='hello', *, eid='Ev01', ts='1793091836.135859', version=None, kind=None):
    p = event()
    p['event_id'] = eid
    p['event'].update(text=text, ts=version or ts, event_ts=version or ts)
    if kind == 'edit':
        p['event'].update(subtype='message_changed', message={'ts': ts, 'text': text, 'user': 'U00000003'})
    elif kind == 'delete':
        p['event'].update(subtype='message_deleted', deleted_ts=ts)
    else:p['event']['ts'] = ts
    return p


class SlackFake:
    token = 'fixture'
    workspace_url = 'https://example.slack.com/'
    def __init__(self):self.permission, self.name, self.calls, self.profile_ok, self.roots = 'allowed', 'general', [], True, {}
    def conversation_allowed(self, cfg, channel):return self.permission, 30
    def call(self, method, body):
        self.calls.append((method,body))
        if method=='users.info':
            return ('sent',{'user':{'id':body['user'],'profile':{'display_name':'Alice'}}}) if self.profile_ok else ('failed',None)
        if method=='conversations.history':
            value=self.roots.get(body['oldest'])
            return 'sent',{'messages':[value] if value else []}
        return 'sent', {'channel': {'name': self.name}}


class DiscordFake:
    def __init__(self):
        self.calls, self.allowed_result, self.results, self.checks, self.threads = [], True, [], [], {}
    def allowed(self, *args, **kwargs):
        self.checks.append((args, kwargs));return self.allowed_result
    def download(self, file, token):return file['name'], b'x' * file['size']
    def call(self, method, path, body=None, files=None):
        self.calls.append((method, path, copy.deepcopy(body), files))
        if method=='GET':return ('sent',self.threads[path.split('/')[-1]]) if path.split('/')[-1] in self.threads else ('missing',None)
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):raise result
            return result
        if path.endswith('/threads'):
            ident=path.split('/')[-2]
            value={'id':ident,'guild_id':GUILD,'parent_id':DEST,'type':11,'thread_metadata':{'archived':False,'locked':False}}
            self.threads[ident]=value
            return 'sent',copy.deepcopy(value)
        if method=='PATCH' and '/messages' not in path:
            ident=path.split('/')[-1];self.threads[ident]['thread_metadata'].update(body)
            return 'sent',copy.deepcopy(self.threads[ident])
        ident = path.split('/')[-1] if method != 'POST' else str(600000000000000000 + len(self.calls))
        return 'sent', {'id': ident, 'channel_id': path.split('/')[2], 'author': {'id': BOT}}


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.policy = config(str(Path(self.temp.name) / 'events.sqlite3'))
        self.original = Store(self.policy.database)
        self.cfg = settings()
        self.store = MirrorStore(self.policy.database, self.cfg, self.policy)
        self.slack, self.discord = SlackFake(), DiscordFake()

    def tearDown(self):self.temp.cleanup()

    def step(self, now=2000000000):
        return process_mirror_one(self.store, self.policy, self.slack, self.discord, lambda: now)

    def drain(self):
        for index in range(50):
            if not self.step(now=2100000000+index*60):return
        self.fail('mirror did not drain')

    def enqueue(self, *args, **kwargs):self.store.enqueue(message(*args, **kwargs))

    def test_create_edit_delete_and_durable_dedup_preserve_mention_receipts(self):
        original = event()
        self.original.enqueue(target(original, self.policy), self.policy)
        self.enqueue();self.drain()
        first = self.discord.calls[0]
        self.assertEqual(first[0], 'POST')
        self.assertEqual(first[1], '/channels/' + DEST + '/messages')
        self.assertEqual(first[2]['allowed_mentions'], {'parse': [], 'users': [], 'roles': [], 'replied_user': False})
        self.assertTrue(first[2]['enforce_nonce'])
        self.assertIn('hello', first[2]['content'])
        self.store = MirrorStore(self.policy.database, self.cfg, self.policy)
        self.enqueue(eid='EvDuplicate');self.drain()
        self.assertEqual(len(self.discord.calls), 1)
        self.enqueue('changed', eid='EvEdit', version='1793091840.000000', kind='edit');self.drain()
        self.assertEqual(self.discord.calls[-1][0], 'PATCH')
        self.assertIn('changed', self.discord.calls[-1][2]['content'])
        self.enqueue(eid='EvDelete', version='1793091850.000000', kind='delete');self.drain()
        self.assertEqual(self.discord.calls[-1][0], 'DELETE')
        self.enqueue('late', eid='EvLate');self.drain()
        self.assertEqual(len(self.discord.calls), 3)
        self.assertEqual(self.original.counts(), {'queued': 1})

    def test_edit_before_create_keeps_newest_content_and_no_backfill(self):
        self.enqueue('edited', eid='EvEdit', kind='edit', version='1793091840.000000')
        self.drain();self.assertEqual(self.discord.calls, [])
        self.enqueue('old');self.drain()
        self.assertIn('edited', self.discord.calls[0][2]['content'])
        self.enqueue(ts='1700000000.000001', eid='EvHistory');self.drain()
        self.assertEqual(len(self.discord.calls), 1)

    def test_delete_before_create_never_posts(self):
        self.enqueue(eid='EvDelete', kind='delete', version='1793091840.000000')
        self.enqueue();self.drain()
        self.assertEqual(self.discord.calls, [])

    def test_late_edit_cannot_overwrite_newer_edit(self):
        self.enqueue();self.drain()
        self.enqueue('newer', eid='EvNew', kind='edit', version='1793091860.000000')
        self.enqueue('older', eid='EvOld', kind='edit', version='1793091850.000000');self.drain()
        self.assertIn('newer', self.discord.calls[-1][2]['content'])

    def test_long_unicode_split_growth_shrink_and_full_delete(self):
        self.enqueue('😀' * 2100);self.drain()
        self.assertEqual(len(self.discord.calls), 3)
        self.assertTrue(all(len(c[2]['content'].encode('utf-16-le')) // 2 <= 2000 for c in self.discord.calls))
        self.enqueue('short', eid='EvShort', kind='edit', version='1793091860.000000');self.drain()
        self.assertEqual([c[0] for c in self.discord.calls[3:]], ['DELETE', 'DELETE', 'PATCH'])
        self.enqueue('x' * 5000, eid='EvGrow', kind='edit', version='1793091870.000000');self.drain()
        self.assertEqual([c[0] for c in self.discord.calls[-4:]], ['PATCH', 'POST', 'POST', 'POST'])
        self.assertNotEqual(self.discord.calls[1][2]['nonce'], self.discord.calls[-3][2]['nonce'])
        self.enqueue(eid='EvDeleted', kind='delete', version='1793091880.000000');self.drain()
        self.assertEqual([c[0] for c in self.discord.calls[-4:]], ['DELETE'] * 4)

    def test_timeout_and_crash_never_repeat_or_patch_unowned_message(self):
        self.enqueue();self.discord.results = [TimeoutError()];self.step()
        self.assertEqual(self.store.counts(), {'uncertain': 1})
        self.enqueue('changed', eid='EvEdit', kind='edit', version='1793091860.000000')
        self.store = MirrorStore(self.policy.database, self.cfg, self.policy);self.drain()
        self.assertEqual(len(self.discord.calls), 1)
        self.assertEqual(self.store.counts(), {'uncertain': 1})

    def test_crash_inflight_is_uncertain_but_recorded_success_resumes(self):
        self.enqueue();row = self.store.claim(2000000000)
        self.store.begin_write(row, 0, None, 'digest')
        self.store = MirrorStore(self.policy.database, self.cfg, self.policy)
        self.assertEqual(self.store.counts(), {'uncertain': 1})
        self.enqueue(eid='EvOther', ts='1793091840.000000')
        row = self.store.claim(2000000000)
        row = self.store.resolve_author(row,self.slack,2000000000)
        part = mirror_parts(row, self.cfg,workspace_url=self.slack.workspace_url)[0]
        self.store.begin_write(row, 0, None, part['digest'])
        self.store.written(row, 0, '666666666666666666', part['digest'])
        self.store = MirrorStore(self.policy.database, self.cfg, self.policy);self.drain()
        self.assertEqual(self.discord.calls, [])
        self.assertEqual(self.store.counts(), {'clean': 1, 'uncertain': 1})

    def test_rate_limit_honors_gate_and_delete_rejected_create_is_local(self):
        self.enqueue();self.discord.results = [('rate_limited', 45)]
        self.step(now=2000000000)
        self.assertFalse(self.step(now=2000000044))
        self.enqueue(eid='EvDelete', kind='delete', version='1793091840.000000')
        self.step(now=2000000045);self.drain()
        self.assertEqual(len(self.discord.calls), 1)

    def test_retries_get_same_nonce_and_changed_config_is_held(self):
        self.enqueue();self.discord.results = [('rate_limited', 1)]
        self.step();self.step(now=2000000001)
        self.assertEqual(self.discord.calls[0][2]['nonce'], self.discord.calls[1][2]['nonce'])
        self.enqueue(eid='EvOther', ts='1793091900.000000')
        changed = dataclasses.replace(self.cfg, category='777777777777777777')
        self.store = MirrorStore(self.policy.database, changed, self.policy);self.drain()
        self.assertEqual(len(self.discord.calls), 2)
        self.assertIn('failed', self.store.counts())

    def test_permission_identity_route_and_membership_fail_closed(self):
        for which in ('discord', 'slack', 'name'):
            with self.subTest(which=which):
                self.discord.allowed_result = which != 'discord'
                self.slack.permission = 'denied' if which == 'slack' else 'allowed'
                self.slack.name = 'renamed' if which == 'name' else 'general'
                self.enqueue(eid='Ev' + which, ts='17930919' + str(len(which)).zfill(2) + '.000000')
                self.drain()
        self.assertEqual(self.discord.calls, [])

    def test_changed_event_during_write_is_eventually_updated(self):
        self.enqueue()
        original_call = self.discord.call
        def concurrent(*args):
            self.enqueue('new', eid='EvNew', kind='edit', version='1793091900.000000')
            self.discord.call = original_call
            return original_call(*args)
        self.discord.call = concurrent
        self.drain()
        self.assertEqual([c[0] for c in self.discord.calls], ['POST', 'PATCH'])
        self.assertIn('new', self.discord.calls[-1][2]['content'])

    def test_bad_success_response_is_uncertain(self):
        self.enqueue();self.discord.results = [('sent', {'id': DEST, 'channel_id': GUILD, 'author': {'id': BOT}})]
        self.drain()
        self.assertEqual(self.store.counts(), {'uncertain': 1})

    def test_ingress_restart_does_not_recover_workers_active_write(self):
        self.enqueue();row = self.store.claim(2000000000)
        self.store.begin_write(row, 0, None, 'digest')
        ingress = MirrorStore(self.policy.database, self.cfg, self.policy, recover=False)
        self.assertEqual(ingress.counts(), {'working': 1})
        self.assertEqual(ingress.parts(row)[0]['state'], 'sending')
        ingress.heartbeat(2000000000)
        self.assertTrue(ingress.worker_alive(2000000001))
        self.assertFalse(ingress.worker_alive(2000000301))

    def test_files_are_uploaded_and_attachment_removal_is_patched(self):
        p = message('file')
        p['event']['files'] = [{'name': 'safe.txt', 'size': 3, 'url_private': 'https://files.slack.com/a'}]
        self.store.enqueue(p);self.drain()
        self.assertEqual(self.discord.calls[0][3], [('safe.txt', b'xxx')])
        self.assertTrue(self.discord.checks[0][1]['uploads'])
        self.enqueue('no file', eid='EvEdit', kind='edit', version='1793091900.000000');self.drain()
        self.assertEqual(self.discord.calls[-1][2]['attachments'], [])

    def test_source_filters_and_bot_thread_messages(self):
        for update in ({'team_id': 'T00000002'}, {'type': 'other'}):
            p = message();p.update(update)
            self.assertIsNone(mirror_event(p, self.cfg, self.policy))
        for update in ({'channel': 'D00000001'}, {'channel': 'C00000002'}, {'team': 'T00000002'},
                       {'subtype': 'message_replied'}, {'ts': 1793091836.0}):
            p = message();p['event'].update(update)
            self.assertIsNone(mirror_event(p, self.cfg, self.policy))
        p = message('<@U00000003> <!here> <https://example.com|link>')
        p['event'].update(subtype='bot_message', bot_id='B00000001', thread_ts='1793091800.000000')
        self.store.enqueue(p);self.drain()
        text = next(c[2]['content'] for c in reversed(self.discord.calls) if c[2] and 'content' in c[2])
        self.assertIn('[Alice](https://example.slack.com/team/U00000003)', text)
        self.assertEqual(text.count('[Alice](https://example.slack.com/team/U00000003)'),1)
        self.assertIn('[@Alice](https://example.slack.com/team/U00000003)',text)
        self.assertNotIn('<@', text)

    def test_shared_mirror_opt_in_does_not_expand_mention_policy(self):
        p = message();p['is_ext_shared_channel'] = True
        self.assertIsNone(mirror_event(p, self.cfg, self.policy))
        shared_cfg = dataclasses.replace(self.cfg, shared_channels=('C00000001',))
        self.assertIsNotNone(mirror_event(p, shared_cfg, self.policy))
        self.assertFalse(self.policy.allows_shared('C00000001'))
        self.assertTrue(shared_cfg.source_policy(self.policy).allows_shared('C00000001'))
        self.assertIsNone(target(p, self.policy))
        original_hash = self.policy.fingerprint
        self.store = MirrorStore(self.policy.database, shared_cfg, self.policy)
        self.store.enqueue(p);self.drain()
        self.assertEqual(self.discord.calls[0][0], 'POST')
        self.assertEqual(self.policy.fingerprint, original_hash)

    def test_shared_metadata_guard_remains_active_when_event_flag_is_absent(self):
        from bot import Slack
        p = message();self.store.enqueue(p)
        original = Slack('fixture')
        info = {'id': 'C00000001', 'context_team_id': self.policy.team, 'is_group': True,
                'is_member': True, 'is_private': True, 'is_ext_shared': True, 'name': 'general'}
        with patch.object(original, 'call', return_value=('sent', {'channel': info})):
            process_mirror_one(self.store, self.policy, original, self.discord, lambda: 2000000000)
        self.assertEqual(self.discord.calls, [])
        self.assertEqual(self.store.counts(), {'failed': 1})

    def test_oversize_external_and_unsafe_filenames_never_leak_token(self):
        p = message()
        p['event']['files'] = [
            {'name': 'bad', 'size': 1, 'url_private': 'https://example.com/private'},
            {'name': 'large', 'size': 11 * 1024 * 1024, 'url_private': 'https://files.slack.com/a'},
            {'name': 'x"\r\nHeader.txt', 'size': 1, 'url_private': 'https://files.slack.com/a'}]
        self.store.enqueue(p);self.drain()
        request = self.discord.calls[0]
        self.assertEqual(len(request[3]), 1)
        self.assertNotIn('\r', request[3][0][0]);self.assertNotIn('"', request[3][0][0])
        self.assertIn('Attachment unavailable', request[2]['content'])


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = settings();self.client = DiscordMirror(self.cfg, 'fixture')
        self.data = {
            '/users/@me': {'id': BOT, 'bot': True}, '/oauth2/applications/@me': {'id': APP},
            '/channels/' + CATEGORY: {'guild_id': GUILD, 'name': 'slack', 'type': 4},
            '/channels/' + DEST: {'guild_id': GUILD, 'parent_id': CATEGORY, 'name': 'general', 'type': 0, 'permission_overwrites': []},
            '/guilds/' + GUILD + '/members/' + BOT: {'user': {'id': BOT}, 'roles': []},
            '/guilds/' + GUILD + '/roles': [{'id': GUILD, 'permissions': str((1 << 10) | (1 << 11))}]}
        self.client.call = lambda method, path: ('sent', self.data[path])

    def test_effective_overwrite_precedence_and_upload_permission(self):
        self.assertTrue(self.client.allowed(DEST, 'general'))
        self.assertFalse(self.client.allowed(DEST, 'general', uploads=True))
        channel = self.data['/channels/' + DEST]
        channel['permission_overwrites'] = [{'id': GUILD, 'type': 0, 'deny': str(1 << 11), 'allow': '0'}]
        self.assertFalse(self.client.allowed(DEST, 'general'))
        channel['permission_overwrites'].append({'id': BOT, 'type': 1, 'deny': '0', 'allow': str((1 << 11) | (1 << 15))})
        self.assertTrue(self.client.allowed(DEST, 'general', uploads=True))

    def test_identity_guild_category_and_name_guards(self):
        for path, field, value in [('/users/@me', 'id', GUILD), ('/oauth2/applications/@me', 'id', GUILD),
                                   ('/channels/' + DEST, 'guild_id', CATEGORY), ('/channels/' + DEST, 'parent_id', GUILD),
                                   ('/channels/' + DEST, 'name', 'other'), ('/channels/' + CATEGORY, 'name', 'other')]:
            with self.subTest(path=path, field=field):
                old = self.data[path][field];self.data[path][field] = value
                self.assertFalse(self.client.allowed(DEST, 'general'))
                self.data[path][field] = old


class TransportTests(unittest.TestCase):
    def test_real_multipart_encoding_and_exact_response_validation_input(self):
        client = DiscordMirror(settings(), 'fixture')
        response = io.BytesIO(json.dumps({'id': DEST, 'channel_id': DEST, 'author': {'id': BOT}}).encode())
        with patch.object(client.opener, 'open', return_value=response) as call:
            state, value = client.call('POST', '/channels/' + DEST + '/messages',
                                       {'content': 'attachment', 'attachments': [{'id': 0, 'filename': 'a.txt'}]}, [('a.txt', b'abc')])
            self.assertEqual(state, 'sent')
            request = call.call_args.args[0]
            self.assertEqual(request.full_url, 'https://discord.com/api/v10/channels/' + DEST + '/messages')
            self.assertIn(b'name="payload_json"', request.data)
            self.assertIn(b'name="files[0]"; filename="a.txt"', request.data)
            self.assertIn(b'abc', request.data)
            self.assertEqual(call.call_count, 1)

    def test_no_redirects_and_http_outcomes(self):
        client = DiscordMirror(settings(), 'fixture')
        for status, expected in [(403, 'failed'), (500, 'uncertain'), (302, 'uncertain')]:
            with self.subTest(status=status), patch.object(client.opener, 'open', side_effect=urllib.error.HTTPError('fixture', status, '', {}, io.BytesIO(b''))):
                self.assertEqual(client.call('POST', '/channels/' + DEST + '/messages', {})[0], expected)
        with patch.object(client.opener, 'open', side_effect=urllib.error.HTTPError('fixture', 429, '', {}, io.BytesIO(b'{"retry_after":2.5}'))):
            self.assertEqual(client.call('POST', '/channels/' + DEST + '/messages', {}), ('rate_limited', 2.5))

    def test_mirror_config_disabled_default_and_duplicate_destination_rejected(self):
        with patch.dict(os.environ, {}, clear=True):self.assertIsNone(MirrorConfig.load())
        data = dataclasses.asdict(settings())
        data['routes']['C00000002'] = {'name': 'second', 'discord': DEST}
        with patch.dict(os.environ, {'DISCORD_MIRROR_ENABLED': 'true', 'DISCORD_MIRROR_CONFIG': json.dumps(data)}, clear=True):
            with self.assertRaises(ValueError):MirrorConfig.load()

    def test_shared_opt_in_must_be_unique_subset_of_verified_routes(self):
        data = dataclasses.asdict(settings())
        for shared in (['C00000002'], ['C00000001', 'C00000001'], 'C00000001'):
            data['shared_channels'] = shared
            with patch.dict(os.environ, {'DISCORD_MIRROR_ENABLED': 'true', 'DISCORD_MIRROR_CONFIG': json.dumps(data)}, clear=True):
                with self.assertRaises(ValueError):MirrorConfig.load()
        data['shared_channels'] = ['C00000001']
        with patch.dict(os.environ, {'DISCORD_MIRROR_ENABLED': 'true', 'DISCORD_MIRROR_CONFIG': json.dumps(data)}, clear=True):
            self.assertEqual(MirrorConfig.load().shared_channels, ('C00000001',))


if __name__ == '__main__':unittest.main()
