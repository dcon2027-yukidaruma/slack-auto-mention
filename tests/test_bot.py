import copy
import dataclasses
import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from bot import Config, Slack, Store, handler, process_one, target, verified_signature
from http.server import ThreadingHTTPServer


def config(path):
    return Config('T00000001', ('C00000001',), ('U00000001', 'U00000002'),
                  'U00000099', 'test-token', 'test-signing-secret', path, True)


def event():
    return {'type': 'event_callback', 'team_id': 'T00000001', 'event_id': 'Ev01',
            'event': {'type': 'message', 'channel': 'C00000001', 'user': 'U00000003',
                      'text': 'お願いします @ゆきだるま', 'ts': '1793091836.135859'}}


class FakeSlack:
    def conversation_allowed(self, cfg, channel):
        return 'allowed', None

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [('sent', {'ts': '1793091836.999999'})])

    def call(self, method, body):
        self.calls.append((method, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = config(self.temp.name + '/events.sqlite3')
        self.store = Store(self.cfg.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_top_level_and_existing_thread(self):
        for thread in [None, '0000000001.000001']:
            with self.subTest(thread=thread):
                e = event()
                if thread:
                    e['event']['thread_ts'] = thread
                result = target(e, self.cfg)
                self.assertEqual(result[3], thread or e['event']['ts'])
                self.assertIsInstance(result[2], str)

    def test_reject_untrusted_events(self):
        changes = [('subtype', 'bot_message'), ('subtype', 'message_changed'),
                   ('subtype', 'message_deleted'), ('subtype', 'thread_broadcast'),
                   ('bot_id', 'B00000001'), ('app_id', 'A00000001'), ('hidden', True),
                   ('edited', {'ts': '1.000001'}), ('user', self.cfg.bot_user),
                   ('user', 'B00000001'), ('channel', 'C00000002'), ('team', 'T00000002'),
                   ('text', 'ゆきだるま'), ('text', '<@U00000001>'), ('ts', 1793091836.135859),
                   ('thread_ts', 1793091836.135859), ('thread_ts', None)]
        for key, val in changes:
            with self.subTest(key=key, val=val):
                e = event()
                e['event'][key] = val
                self.assertIsNone(target(e, self.cfg))
        for key, val in [('team_id', 'T00000002'), ('is_ext_shared_channel', True),
                         ('event_id', ''), ('type', 'url_verification')]:
            e = event()
            e[key] = val
            self.assertIsNone(target(e, self.cfg))
        self.assertIsNone(target(event(), dataclasses.replace(self.cfg, enabled=False)))

    def test_durable_dedup_and_fixed_reply(self):
        self.store.enqueue(target(event(), self.cfg), self.cfg)
        e = event()
        e['event_id'] = 'EvDifferentDelivery'
        self.store.enqueue(target(e, self.cfg), self.cfg)
        self.store = Store(self.cfg.database)
        slack = FakeSlack()
        self.assertTrue(process_one(self.store, self.cfg, slack))
        self.store.enqueue(target(event(), self.cfg), self.cfg)
        self.assertFalse(process_one(self.store, self.cfg, slack))
        body = slack.calls[0][1]
        self.assertEqual(body['text'], '<@U00000001> <@U00000002>')
        self.assertEqual(body['channel'], 'C00000001')
        self.assertEqual(body['thread_ts'], '1793091836.135859')
        self.assertIs(body['reply_broadcast'], False)
        self.assertNotIn('@channel', body['text'])
        self.assertNotIn('@here', body['text'])
        self.assertEqual(self.store.counts(), {'sent': 1})

    def test_rate_limit_is_persistent_and_global(self):
        self.store.enqueue(target(event(), self.cfg), self.cfg)
        slack = FakeSlack([('rate_limited', 60), ('sent', {'ts': '1.000001'}), ('sent', {'ts': '1.000002'})])
        process_one(self.store, self.cfg, slack, lambda: 100)
        e = event()
        e['event_id'] = 'Ev02'
        e['event']['ts'] = '1793091836.135860'
        self.store.enqueue(target(e, self.cfg), self.cfg)
        self.store = Store(self.cfg.database)
        self.assertFalse(process_one(self.store, self.cfg, slack, lambda: 159))
        self.assertTrue(process_one(self.store, self.cfg, slack, lambda: 160))
        self.assertTrue(process_one(self.store, self.cfg, slack, lambda: 162))
        self.assertEqual(self.store.counts(), {'sent': 2})

    def test_retry_after_starts_at_response_receipt(self):
        self.store.enqueue(target(event(), self.cfg), self.cfg)
        slack = FakeSlack([('rate_limited', 60), ('sent', {'ts': '1.000001'})])
        ticks = iter([100, 115])  # the request took 15 seconds
        process_one(self.store, self.cfg, slack, lambda: next(ticks))
        self.store = Store(self.cfg.database)
        self.assertFalse(process_one(self.store, self.cfg, slack, lambda: 174))
        self.assertTrue(process_one(self.store, self.cfg, slack, lambda: 175))
        self.assertEqual(len(slack.calls), 2)

    def test_exhausted_event_retains_global_retry_after_gate(self):
        self.store.enqueue(target(event(), self.cfg), self.cfg)
        with self.store.connect() as c:
            c.execute('UPDATE events SET attempts=9')
        second = event()
        second['event_id'] = 'Ev02'
        second['event']['ts'] = '1793091836.135860'
        self.store.enqueue(target(second, self.cfg), self.cfg)
        slack = FakeSlack([('rate_limited', 60), ('sent', {'ts': '1.000001'})])
        ticks = iter([100, 115])
        process_one(self.store, self.cfg, slack, lambda: next(ticks))
        self.assertEqual(self.store.counts(), {'failed': 1, 'queued': 1})
        self.store = Store(self.cfg.database)
        self.assertFalse(process_one(self.store, self.cfg, slack, lambda: 174))
        self.assertEqual(len(slack.calls), 1)
        self.assertTrue(process_one(self.store, self.cfg, slack, lambda: 175))
        self.assertEqual(self.store.counts(), {'failed': 1, 'sent': 1})

    def test_unknown_outcome_and_crash_never_auto_retry(self):
        for response in [('uncertain', None), TimeoutError(), ('sent', {'ts': 1.1})]:
            with self.subTest(response=response):
                with self.store.connect() as c:
                    c.execute('DELETE FROM events')
                self.store.enqueue(target(event(), self.cfg), self.cfg)
                slack = FakeSlack([response])
                process_one(self.store, self.cfg, slack)
                self.store = Store(self.cfg.database)
                self.assertFalse(process_one(self.store, self.cfg, slack))
                self.assertEqual(self.store.counts(), {'uncertain': 1})
        with self.store.connect() as c:
            c.execute("UPDATE events SET state='sending'")
        self.store = Store(self.cfg.database)
        self.assertEqual(self.store.counts(), {'uncertain': 1})

    def test_config_change_quarantines_queue(self):
        self.store.enqueue(target(event(), self.cfg), self.cfg)
        slack = FakeSlack()
        changed = dataclasses.replace(self.cfg, members=('U00000004',))
        self.assertFalse(process_one(self.store, changed, slack))
        self.assertEqual(slack.calls, [])
        self.assertEqual(self.store.counts(), {'failed': 1})

    def test_post_transport_never_retries_unknown_error(self):
        slack = Slack('test-token')
        cases = [(TimeoutError(), 'uncertain'),
                 (urllib.error.HTTPError('https://slack.com', 500, 'error', {}, None), 'uncertain'),
                 (urllib.error.HTTPError('https://slack.com', 429, 'limit', {'Retry-After': '45'}, None), 'rate_limited')]
        for error, expected in cases:
            with patch('urllib.request.urlopen', side_effect=error) as request:
                result = slack.call('chat.postMessage', {})
                self.assertEqual(result[0], expected)
                self.assertEqual(request.call_count, 1)

    def test_conversation_type_and_membership_policy(self):
        slack = Slack('test-token')
        cfg = dataclasses.replace(self.cfg, channel_mode='all_joined', group_dm_ids=('C00000002',))
        base = {'id': 'C00000002', 'context_team_id': cfg.team, 'is_mpim': True, 'is_channel': True, 'is_member': True}
        for change, allowed in [({}, True), ({'is_mpim': False}, True),
                                ({'is_ext_shared': True}, False), ({'context_team_id': 'T00000002'}, False),
                                ({'id': 'C00000003'}, False), ({'is_im': True}, False)]:
            info = base | change
            with patch.object(slack, 'call', return_value=('sent', {'channel': info})):
                self.assertEqual(slack.conversation_allowed(cfg, 'C00000002')[0] == 'allowed', allowed)
        with patch.object(slack, 'call', return_value=('sent', {'channel': base})):
            self.assertEqual(slack.conversation_allowed(dataclasses.replace(cfg, group_dm_ids=()), 'C00000002')[0], 'denied')
        with patch.object(slack, 'call', side_effect=[('sent', {'channel': base | {'is_member': False}}), ('sent', {'members': ['U00000003']})]):
            self.assertEqual(slack.conversation_allowed(cfg, 'C00000002')[0], 'denied')
        with patch.object(slack, 'call', side_effect=[('sent', {'channel': base | {'is_member': False}}), ('sent', {'members': [cfg.bot_user]})]):
            self.assertEqual(slack.conversation_allowed(cfg, 'C00000002')[0], 'allowed')

    def test_read_calls_use_query_parameters(self):
        with patch('urllib.request.urlopen', side_effect=TimeoutError()) as request:
            Slack('test-token').call('conversations.info', {'channel': 'C00000001'})
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_method(), 'GET')
            self.assertTrue(sent.full_url.endswith('?channel=C00000001'))

    def test_membership_revoked_before_send(self):
        self.store.enqueue(target(event(), self.cfg), self.cfg)
        slack = FakeSlack()
        with patch.object(slack, 'conversation_allowed', return_value=('denied', None)):
            process_one(self.store, self.cfg, slack)
        self.assertEqual(slack.calls, [])
        self.assertEqual(self.store.counts(), {'failed': 1})

    def test_signature_replay_and_tampering(self):
        stamp, raw = '1793091836', b'{}'
        signature = 'v0=' + hmac.new(self.cfg.signing_secret.encode(), b'v0:' + stamp.encode() + b':' + raw, hashlib.sha256).hexdigest()
        headers = {'X-Slack-Request-Timestamp': stamp, 'X-Slack-Signature': signature}
        self.assertTrue(verified_signature(self.cfg.signing_secret, headers, raw, int(stamp)))
        self.assertFalse(verified_signature(self.cfg.signing_secret, headers, raw, int(stamp) + 301))
        self.assertFalse(verified_signature(self.cfg.signing_secret, headers, b'evil', int(stamp)))

    def test_http_ack_requires_persistence_and_valid_signature(self):
        import time
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.cfg, self.store, {'authenticated': True, 'worker': time.monotonic()}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            raw = json.dumps(event()).encode()
            stamp = str(int(time.time()))
            signature = 'v0=' + hmac.new(self.cfg.signing_secret.encode(), b'v0:' + stamp.encode() + b':' + raw, hashlib.sha256).hexdigest()
            connection = http.client.HTTPConnection(*server.server_address)
            connection.request('POST', '/slack/events', raw, {'X-Slack-Request-Timestamp': stamp, 'X-Slack-Signature': signature})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            self.assertEqual(self.store.counts(), {'queued': 1})
            with patch.object(self.store, 'enqueue', side_effect=__import__('sqlite3').OperationalError()):
                connection.request('POST', '/slack/events', raw, {'X-Slack-Request-Timestamp': stamp, 'X-Slack-Signature': signature})
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
            connection.request('POST', '/slack/events', raw)
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
