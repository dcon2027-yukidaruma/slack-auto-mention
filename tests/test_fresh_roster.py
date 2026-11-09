import copy
import dataclasses
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import Config, FreshSheetRoster, RosterHeld, RosterMonitor, Store, process_one, roster_members, target
from test_bot import FakeSlack, config, event
from deploy.roster_projection import rows_from_columns
from deploy.roster_reader import FreshRosterReader, batch_url

NOW = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
SOURCE = 'fixture-sheet'


def snapshot(rows, request_id='fixture-request', event_id='Ev01'):
    return {'schema_version': 1, 'source_id': SOURCE, 'sheet': 'Sheet1',
            'request_id': request_id, 'event_id': event_id,
            'read_started_at': NOW.isoformat(), 'read_finished_at': NOW.isoformat(),
            'rows': rows}


class ProjectionTests(unittest.TestCase):
    def read(self, rows, **changes):
        value = snapshot(rows); value.update(changes)
        return roster_members(value, source_id=SOURCE, request_id='fixture-request', event_id='Ev01',
                              requested_at=NOW, now=NOW, bot_user='U00000099')

    def test_flags_blanks_invalid_and_self_exclusion(self):
        rows = [[True, 'U00000001'], ['TRUE', 'U00000002'], [False, 'U00000003'],
                ['FALSE', 'U00000004'], ['', 'U00000005'], [True, ''], [True, 'bad'],
                [True, 'U00000099']]
        self.assertEqual(self.read(rows), ('U00000001', 'U00000002'))

    def test_identical_duplicate_deduplicated_conflicting_duplicate_held(self):
        self.assertEqual(self.read([[True, 'U00000001'], ['TRUE', 'U00000001']]), ('U00000001',))
        with self.assertRaises(RosterHeld):
            self.read([[True, 'U00000001'], [False, 'U00000001']])

    def test_all_unjoined_or_inactive_returns_no_members(self):
        self.assertEqual(self.read([[True, ''], [False, 'U00000001']]), ())

    def test_bad_schema_flag_or_numeric_id_held(self):
        for rows in [[[1, 'U00000001']], [['yes', 'U00000001']],
                     [[True]], {'unexpected': True}]:
            with self.subTest(rows=rows), self.assertRaises(RosterHeld):
                self.read(rows)

    def test_numeric_id_skipped_without_losing_other_valid_members(self):
        self.assertEqual(self.read([[True,123],[True,'U00000001']]),('U00000001',))

    def test_request_event_source_and_schema_binding(self):
        for change in [{'request_id': 'old'}, {'event_id': 'other'}, {'source_id': 'other'},
                       {'sheet': 'Sheet2'}, {'schema_version': True}, {'extra': 'field'}]:
            with self.subTest(change=change), self.assertRaises(RosterHeld):
                self.read([], **change)

    def test_old_future_or_naive_read_rejected(self):
        for value in [(NOW-timedelta(seconds=1)).isoformat(), (NOW+timedelta(seconds=1)).isoformat(),
                      NOW.replace(tzinfo=None).isoformat(), 'invalid']:
            with self.subTest(value=value), self.assertRaises(RosterHeld):
                self.read([], read_started_at=value)

    def test_too_slow_or_too_many_rows_held(self):
        value = snapshot([])
        with self.assertRaises(RosterHeld):
            roster_members(value, source_id=SOURCE, request_id='fixture-request', event_id='Ev01',
                           requested_at=NOW, now=NOW+timedelta(seconds=16), bot_user='U00000099')
        with self.assertRaises(RosterHeld): self.read([[False, '']]*5001)
        with self.assertRaises(RosterHeld): self.read([[True, 'U%08d' % n] for n in range(51)])

    def test_same_batch_projection_only_two_verified_columns(self):
        response={'spreadsheetId':SOURCE,'valueRanges':[
            {'range':'Sheet1!D1:D10','majorDimension':'COLUMNS','values':[['メンバーフラグ',True,False,True]]},
            {'range':'Sheet1!J1:J10','majorDimension':'COLUMNS','values':[['Slackのmember ID','U00000001','U00000002']]}]}
        rows=rows_from_columns(response,source_id=SOURCE,row_count=10)
        self.assertEqual(rows,[[True,'U00000001'],[False,'U00000002'],[True,'']])
        self.assertEqual(self.read(rows),('U00000001',))
        response['valueRanges'].reverse()
        self.assertEqual(rows_from_columns(response,source_id=SOURCE,row_count=10),rows)

    def test_wrong_headers_duplicate_ranges_and_other_sheet_rejected(self):
        good={'spreadsheetId':SOURCE,'valueRanges':[
            {'range':'Sheet1!D1:D10','majorDimension':'COLUMNS','values':[['メンバーフラグ',True]]},
            {'range':'Sheet1!J1:J10','majorDimension':'COLUMNS','values':[['Slackのmember ID','U00000001']]}]}
        variants=[]
        value=copy.deepcopy(good);value['valueRanges'][0]['values'][0][0]='other';variants.append(value)
        value=copy.deepcopy(good);value['valueRanges'][1]=value['valueRanges'][0];variants.append(value)
        value=copy.deepcopy(good);value['valueRanges'][0]['range']='Sheet2!D1:D10';variants.append(value)
        value=copy.deepcopy(good);value['valueRanges'][0]['majorDimension']='ROWS';variants.append(value)
        value=copy.deepcopy(good);value['spreadsheetId']='other';variants.append(value)
        for value in variants:
            with self.subTest(value=value),self.assertRaises(ValueError):rows_from_columns(value,source_id=SOURCE,row_count=10)


class ConfigurationTests(unittest.TestCase):
    def test_identifier_scan_allows_google_enum_but_rejects_slack_id(self):
        import re
        from scripts.scan_secrets import PATTERNS
        self.assertFalse(any(re.search(p,'UNFORMATTED_VALUE') for p in PATTERNS))
        self.assertTrue(any(re.search(p,'U'+'1234567890') for p in PATTERNS))

    def environment(self):
        return {'SLACK_TEAM_ID':'T00000001','SLACK_BOT_USER_ID':'U00000099','SLACK_CHANNEL_IDS':'C00000001',
                'SLACK_MEMBER_IDS':'U00000001','SLACK_BOT_TOKEN':'xoxb-fixture','SLACK_SIGNING_SECRET':'fixture-signing-value'}

    def test_static_hash_compatibility(self):
        with patch.dict(os.environ,self.environment(),clear=True):cfg=Config.load()
        self.assertEqual(cfg.roster_mode,'static')
        plain=[cfg.team,sorted(cfg.channels),sorted(cfg.members),cfg.bot_user,'explicit',[]]
        self.assertEqual(cfg.fingerprint,__import__('hashlib').sha256(json.dumps(plain).encode()).hexdigest())

    def test_fresh_configuration_rejects_missing_or_arbitrary_source(self):
        for source in ['', 'https://invalid.example/','../other']:
            with patch.dict(os.environ,self.environment()|{'SLACK_ROSTER_MODE':'fresh_sheet','SLACK_ROSTER_SOURCE_ID':source},clear=True),self.assertRaises(ValueError):Config.load()
        with patch.dict(os.environ,self.environment()|{'SLACK_ROSTER_MODE':'fresh_sheet','SLACK_ROSTER_SOURCE_ID':SOURCE},clear=True):
            self.assertEqual(Config.load().roster_mode,'fresh_sheet')


class HostReaderTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.meta={'spreadsheetId':SOURCE,'sheets':[{'properties':{'sheetId':0,'title':'Sheet1',
                   'gridProperties':{'rowCount':10,'columnCount':26}}}]}
        class ExistingReader:
            def metadata(inner):return copy.deepcopy(self.meta)
            def token(inner):return 'fixture-access'
        self.values=[[True,'U00000003']]
        def batch(source,ranges,token):
            self.calls.append((source,ranges))
            return {'spreadsheetId':source,'valueRanges':[
                {'range':ranges[0],'majorDimension':'COLUMNS','values':[['メンバーフラグ']+[v[0] for v in self.values]]},
                {'range':ranges[1],'majorDimension':'COLUMNS','values':[['Slackのmember ID']+[v[1] for v in self.values]]}]}
        self.reader=FreshRosterReader(ExistingReader(),batch,source_id=SOURCE,clock=lambda:NOW)
        self.envelope={'schema_version':1,'operation':'fresh_members','source_id':SOURCE,'sheet':'Sheet1',
                       'request_id':'1'*32,'event_id':'Ev01'}

    def test_each_request_reloads_same_batched_columns(self):
        first=self.reader.handle(self.envelope);self.values=[[True,'U00000004']]
        second=self.reader.handle(self.envelope|{'request_id':'2'*32,'event_id':'Ev02'})
        self.assertEqual(first['rows'],[[True,'U00000003']]);self.assertEqual(second['rows'],[[True,'U00000004']])
        self.assertEqual(self.calls,[(SOURCE,['Sheet1!D1:D10','Sheet1!J1:J10'])]*2)

    def test_client_and_host_contract_one_fresh_read(self):
        cfg=dataclasses.replace(config('fixture'),roster_mode='fresh_sheet',roster_source_id=SOURCE)
        provider=FreshSheetRoster(cfg,request=self.reader.handle,clock=lambda:NOW)
        self.assertEqual(provider('Ev01'),('U00000003',));self.assertEqual(len(self.calls),1)

    def test_wrong_source_or_arbitrary_operation_never_reads(self):
        for change in [{'source_id':'other-sheet'},{'sheet':'Sheet2'},{'operation':'read_all'},
                       {'request_id':'bad'},{'event_id':'../private'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.reader.handle(self.envelope|change)
        self.assertEqual(self.calls,[])

    def test_wrong_metadata_or_oversized_grid_held(self):
        for change in [{'sheetId':1},{'title':'Other'},{'hidden':True},
                       {'gridProperties':{'rowCount':5001,'columnCount':26}}]:
            original=copy.deepcopy(self.meta)
            self.meta['sheets'][0]['properties'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.reader.handle(self.envelope)
            self.meta=original
        self.assertEqual(self.calls,[])

    def test_source_failure_no_cached_rows_returned(self):
        self.reader.handle(self.envelope)
        self.reader.batch_get=lambda *args,**kw:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(TimeoutError):self.reader.handle(self.envelope|{'request_id':'2'*32})

    def test_bounded_google_url_contains_only_two_ranges(self):
        url=batch_url(SOURCE,['Sheet1!D1:D10','Sheet1!J1:J10'])
        self.assertTrue(url.startswith('https://sheets.googleapis.com/v4/spreadsheets/'+SOURCE+'/values:batchGet?'))
        self.assertIn('UNFORMATTED_VALUE',url)
        for ranges in [['Sheet1!A1:M10','Sheet1!J1:J10'],['Sheet1!D1:D5001','Sheet1!J1:J5001'],
                       ['Sheet1!D1:D10','Sheet1!J1:J11']]:
            with self.subTest(ranges=ranges),self.assertRaises(ValueError):batch_url(SOURCE,ranges)


class FreshDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = dataclasses.replace(config(self.tmp.name+'/events.sqlite3'),
                                       roster_mode='fresh_sheet', roster_source_id=SOURCE)
        self.store = Store(self.cfg.database); self.slack = FakeSlack()
        self.rows = [[True, 'U00000003'], [False, 'U00000004']]
        self.reads = []; self.health = {}
        def request(envelope):
            self.reads.append(envelope)
            return snapshot(copy.deepcopy(self.rows), envelope['request_id'], envelope['event_id'])
        self.provider = FreshSheetRoster(self.cfg, request=request, clock=lambda:NOW)

    def enqueue(self, ident='Ev01', ts='1793091836.135859'):
        value = event(); value['event_id'] = ident; value['event']['ts'] = ts
        self.store.enqueue(target(value, self.cfg), self.cfg)

    def send(self, at=100):
        return process_one(self.store, self.cfg, self.slack, lambda:at,
                           member_provider=self.provider, roster_health=self.health)

    def test_latest_roster_in_same_thread_no_static_fallback(self):
        self.enqueue(); self.send()
        body = self.slack.calls[0][1]
        self.assertEqual(body['text'], '<@U00000003>')
        self.assertEqual(body['channel'], self.cfg.channels[0])
        self.assertEqual(body['thread_ts'], event()['event']['ts'])
        self.assertNotIn('U00000001', body['text']); self.assertEqual(self.health['status'], 'ready')

    def test_every_event_reads_again_and_queued_change_used(self):
        self.enqueue(); self.enqueue('Ev02', '1793091836.135860')
        self.send(); self.rows = [[True, 'U00000004']]
        self.slack.responses.append(('sent', {'ts':'1793091836.999998'})); self.send()
        self.assertEqual([body['text'] for _,body in self.slack.calls], ['<@U00000003>', '<@U00000004>'])
        self.assertEqual(len(self.reads), 2)
        self.assertNotEqual(self.reads[0]['request_id'], self.reads[1]['request_id'])

    def test_fetch_failure_is_bounded_retry_not_post_or_static(self):
        self.enqueue(); self.provider.request = lambda _: (_ for _ in ()).throw(TimeoutError('private response'))
        self.send(); self.assertEqual(self.slack.calls, []); self.assertEqual(self.health, {'status':'held','member_count':0})
        self.assertEqual(self.store.counts(), {'queued':1})
        self.assertFalse(self.send(129))
        with self.store.connect() as db:db.execute('UPDATE events SET attempts=9')
        self.send(130); self.assertEqual(self.store.counts(), {'failed':1})

    def test_missing_provider_never_uses_static_members(self):
        self.enqueue(); process_one(self.store,self.cfg,self.slack,lambda:100)
        self.assertEqual(self.slack.calls, []); self.assertEqual(self.store.counts(), {'queued':1})

    def test_invalid_schema_or_no_members_never_posts(self):
        self.enqueue(); self.provider.request = lambda _: {'invalid':True}; self.send()
        self.assertEqual(self.slack.calls, []); self.assertEqual(self.store.counts(), {'queued':1})

    def test_empty_roster_is_terminal_no_empty_message(self):
        self.enqueue(); self.rows = [[True,''],[False,'U00000001']]; self.send()
        self.assertEqual(self.slack.calls, []); self.assertEqual(self.store.counts(), {'failed':1})
        self.assertEqual(self.health['status'],'empty')

    def test_429_retry_reloads_new_roster_same_dedup_key(self):
        self.enqueue(); self.slack.responses=[('rate_limited',30),('sent',{'ts':'1793091836.999999'})]
        self.send(); self.rows=[[True,'U00000004']]; self.send(130)
        self.assertEqual([body['text'] for _,body in self.slack.calls],['<@U00000003>','<@U00000004>'])
        self.assertEqual(self.slack.calls[0][1]['client_msg_id'],self.slack.calls[1][1]['client_msg_id'])
        self.assertEqual(len(self.reads),2);self.assertEqual(self.store.counts(),{'sent':1})

    def test_unknown_post_and_restart_never_retry(self):
        self.enqueue();self.slack.responses=[TimeoutError()];self.send()
        self.store=Store(self.cfg.database);self.assertFalse(self.send(200))
        self.assertEqual(len(self.reads),1);self.assertEqual(self.store.counts(),{'uncertain':1})

    def test_crash_during_fresh_read_remains_uncertain_after_restart(self):
        self.enqueue();self.store.claim(self.cfg,100);self.store=Store(self.cfg.database)
        self.assertFalse(self.send(200));self.assertEqual(self.store.counts(),{'uncertain':1});self.assertEqual(self.reads,[])

    def test_permission_revoked_during_read_blocks_post(self):
        self.enqueue()
        with patch.object(self.slack,'conversation_allowed',side_effect=[('allowed',None),('denied',None)]):self.send()
        self.assertEqual(self.slack.calls,[]);self.assertEqual(self.store.counts(),{'failed':1})

    def test_slow_permission_recheck_cannot_post_an_aged_roster(self):
        self.enqueue()
        with patch('bot.time.monotonic',side_effect=[100,116]):self.send()
        self.assertEqual(self.slack.calls,[]);self.assertEqual(self.store.counts(),{'queued':1})
        self.assertEqual(self.health,{'status':'held','member_count':0})

    def test_static_compatibility_and_mode_change_quarantines_queue(self):
        static=dataclasses.replace(self.cfg,roster_mode='static',roster_source_id='')
        self.store.enqueue(target(event(),static),static)
        self.assertFalse(self.send());self.assertEqual(self.reads,[]);self.assertEqual(self.store.counts(),{'failed':1})
        self.assertNotEqual(static.fingerprint,self.cfg.fingerprint)
        self.assertEqual(self.cfg.fingerprint,dataclasses.replace(self.cfg,members=('U00000006',)).fingerprint)


class MonitorRecoveryTests(unittest.TestCase):
    def test_health_endpoint_recovers_with_zero_events(self):
        import http.client,time,threading
        from http.server import ThreadingHTTPServer
        from bot import handler
        now=[0];responses=[TimeoutError(),('U00000001',)]
        def provider(_event):
            value=responses.pop(0)
            if isinstance(value,Exception):raise value
            return value
        with tempfile.TemporaryDirectory() as temp:
            cfg=dataclasses.replace(config(temp+'/events.sqlite3'),roster_mode='fresh_sheet',roster_source_id=SOURCE)
            store=Store(cfg.database);health={'authenticated':True,'worker':time.monotonic(),'roster':{}}
            monitor=RosterMonitor(provider,health['roster'],clock=lambda:now[0]);monitor.probe_if_due()
            server=ThreadingHTTPServer(('127.0.0.1',0),handler(cfg,store,health))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get():
                connection=http.client.HTTPConnection(*server.server_address)
                try:
                    connection.request('GET','/healthz');response=connection.getresponse()
                    return response.status,json.loads(response.read())
                finally:connection.close()
            try:
                self.assertEqual(get()[0],503);now[0]=30;monitor.probe_if_due()
                status,value=get();self.assertEqual(status,200);self.assertTrue(value['ready'])
                self.assertEqual(value['roster'],{'status':'ready','member_count':1});self.assertEqual(store.counts(),{})
            finally:server.shutdown();server.server_close();thread.join()

    def test_startup_failure_recovers_without_events_and_without_posting(self):
        times=[0];calls=[];health={};responses=[TimeoutError(),('U00000001',)]
        def provider(event_id):
            calls.append(event_id);value=responses.pop(0)
            if isinstance(value,Exception):raise value
            return value
        monitor=RosterMonitor(provider,health,clock=lambda:times[0])
        self.assertTrue(monitor.probe_if_due());self.assertEqual(health['status'],'held')
        times[0]=29;self.assertFalse(monitor.probe_if_due());self.assertEqual(len(calls),1)
        times[0]=30;self.assertTrue(monitor.probe_if_due());self.assertEqual(health,{'status':'ready','member_count':1})
        times[0]=1000;self.assertFalse(monitor.probe_if_due());self.assertEqual(calls,['health-dry-run']*2)

    def test_recovery_backoff_capped_and_probe_results_not_reused_for_events(self):
        now=[0];calls=[];healthy=[False]
        def provider(event_id):
            calls.append(event_id)
            if not healthy[0]:raise TimeoutError()
            return ('U00000001',)
        monitor=RosterMonitor(provider,{},clock=lambda:now[0])
        for expected in [30,60,120,240,300,300]:
            monitor.probe_if_due();self.assertEqual(monitor.next_probe-now[0],expected);now[0]=monitor.next_probe
        healthy[0]=True;monitor.probe_if_due();before=len(calls);monitor.read('EvNew')
        self.assertEqual(len(calls),before+1);self.assertEqual(calls[-1],'EvNew')


if __name__=='__main__':unittest.main()
