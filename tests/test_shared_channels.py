import dataclasses
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import Config, Slack, Store, process_one, target
from test_bot import config, event


class SharedChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = config(self.temp.name + '/events.sqlite3')
        self.approved = dataclasses.replace(self.base, shared_channel_ids=self.base.channels)

    def test_explicit_shared_event_keeps_thread_and_all_other_guards(self):
        message = event()
        message['is_ext_shared_channel'] = True
        message['event']['thread_ts'] = '0000000001.000001'
        self.assertIsNone(target(message, self.base))
        self.assertEqual(target(message, self.approved)[3], '0000000001.000001')
        for change in [{'channel': 'C00000002'}, {'team': 'T00000002'},
                       {'user': self.base.bot_user}, {'subtype': 'message_changed'}]:
            with self.subTest(change=change):
                altered = message | {'event': message['event'] | change}
                self.assertIsNone(target(altered, self.approved))
        self.assertIsNone(target(message | {'team_id': 'T00000002'}, self.approved))
        self.assertEqual(target(message, dataclasses.replace(self.approved, channel_mode='all_joined'))[3], '0000000001.000001')

    def test_all_joined_only_expands_internal_membership_not_other_shared_channels_or_dms(self):
        cfg = dataclasses.replace(self.approved, channel_mode='all_joined')
        slack = Slack('fake-token')
        other = 'C00000002'
        info = {'id': other, 'context_team_id': cfg.team, 'is_group': True, 'is_member': True}
        with patch.object(slack, 'call', return_value=('sent', {'channel': info})):
            self.assertEqual(slack.conversation_allowed(cfg, other)[0], 'allowed')
        for change in [{'is_ext_shared': True}, {'is_pending_ext_shared': True},
                       {'is_im': True}, {'is_mpim': True}, {'context_team_id': 'T00000002'}]:
            with self.subTest(change=change), patch.object(slack, 'call', return_value=('sent', {'channel': info | change})):
                self.assertEqual(slack.conversation_allowed(cfg, other)[0], 'denied')
        with patch.object(slack, 'call', side_effect=[('sent', {'channel': info | {'is_member': False}}), ('sent', {'members': []})]):
            self.assertEqual(slack.conversation_allowed(cfg, other)[0], 'denied')
        message = event(); message['is_ext_shared_channel'] = True
        message['event']['channel'] = other
        self.assertIsNone(target(message, cfg))
        message['event']['channel'] = cfg.channels[0]
        self.assertIsNone(target(message, dataclasses.replace(cfg, shared_channel_ids=())))

    def test_runtime_metadata_rechecks_opt_in_even_if_event_flag_is_missing(self):
        info = {'id': self.base.channels[0], 'context_team_id': self.base.team,
                'is_group': True, 'is_private': True, 'is_ext_shared': True, 'is_member': True}
        slack = Slack('fake-token')
        with patch.object(slack, 'call', return_value=('sent', {'channel': info})):
            self.assertEqual(slack.conversation_allowed(self.base, info['id'])[0], 'denied')
            self.assertEqual(slack.conversation_allowed(self.approved, info['id'])[0], 'allowed')
        for change in [{'is_pending_ext_shared': True}, {'context_team_id': 'T00000002'},
                       {'is_archived': True}, {'is_im': True}, {'is_mpim': True},
                       {'id': 'C00000002'}]:
            with self.subTest(change=change), patch.object(slack, 'call', return_value=('sent', {'channel': info | change})):
                self.assertEqual(slack.conversation_allowed(self.approved, info['id'])[0], 'denied')

    def test_shared_membership_denial_never_posts_but_verified_member_replies_in_thread(self):
        for member in [False, True]:
            with self.subTest(member=member):
                cfg = dataclasses.replace(self.approved, database=self.temp.name + '/case-' + str(member) + '.sqlite3')
                store = Store(cfg.database)
                message = event()
                message['is_ext_shared_channel'] = True
                message['event']['thread_ts'] = '0000000001.000001'
                store.enqueue(target(message, cfg), cfg)
                info = {'id': cfg.channels[0], 'context_team_id': cfg.team, 'is_group': True,
                        'is_ext_shared': True, 'is_member': False}
                replies = [('sent', {'channel': info}), ('sent', {'members': [cfg.bot_user] if member else []})]
                if member:
                    replies.append(('sent', {'ts': '1.000001'}))
                slack = Slack('fake-token')
                with patch.object(slack, 'call', side_effect=replies) as calls:
                    process_one(store, cfg, slack)
                posts = [call for call in calls.call_args_list if call.args[0] == 'chat.postMessage']
                self.assertEqual(len(posts), int(member))
                if member:
                    self.assertEqual(posts[0].args[1]['thread_ts'], '0000000001.000001')
                    self.assertEqual(posts[0].args[1]['text'], '<@U00000001> <@U00000002>')
                    self.assertIs(posts[0].args[1]['reply_broadcast'], False)

    def test_policy_hash_is_compatible_by_default_and_revocation_cancels_queued_send(self):
        old = [self.base.team, sorted(self.base.channels), sorted(self.base.members), self.base.bot_user,
               self.base.channel_mode, sorted(self.base.group_dm_ids)]
        self.assertEqual(self.base.fingerprint, hashlib.sha256(json.dumps(old).encode()).hexdigest())
        self.assertNotEqual(self.approved.fingerprint, self.base.fingerprint)
        store = Store(self.base.database)
        store.enqueue(target(event(), self.approved), self.approved)
        with patch.object(Slack, 'call') as calls:
            self.assertFalse(process_one(store, self.base, Slack('fake-token')))
        calls.assert_not_called()
        self.assertEqual(store.counts(), {'failed': 1})

    def test_config_requires_explicit_ordinary_channel_subset(self):
        values = {'SLACK_TEAM_ID': self.base.team, 'SLACK_BOT_USER_ID': self.base.bot_user,
                  'SLACK_MEMBER_IDS': ','.join(self.base.members), 'SLACK_CHANNEL_IDS': ','.join(self.base.channels),
                  'SLACK_BOT_TOKEN': 'xoxb-' + 'dummy', 'SLACK_SIGNING_SECRET': 'dummy-signing-value',
                  'SLACK_SHARED_CHANNEL_IDS': ','.join(self.base.channels)}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(Config.load().shared_channel_ids, self.base.channels)
        with patch.dict(os.environ, values | {'SLACK_CHANNEL_MODE': 'all_joined'}, clear=True):
            self.assertTrue(Config.load().allows_shared(self.base.channels[0]))
        for change in [{'SLACK_SHARED_CHANNEL_IDS': 'C00000002'},
                       {'SLACK_GROUP_DM_IDS': self.base.channels[0]},
                       {'SLACK_SHARED_CHANNEL_IDS': self.base.channels[0] + ',' + self.base.channels[0]}]:
            with self.subTest(change=change), patch.dict(os.environ, values | change, clear=True), self.assertRaises(ValueError):
                Config.load()


if __name__ == '__main__':
    unittest.main()
