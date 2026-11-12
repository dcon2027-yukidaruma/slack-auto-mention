"""No real messages: native thread routing, broadcasts, profile names and v1 migration."""
import copy
import dataclasses
import json
import sqlite3
import unittest
from unittest.mock import patch

from bot import MirrorStore,DiscordMirror,mirror_event,mirror_parts,valid_mirror_thread,process_mirror_one
import test_mirror as fixtures
from test_mirror import message,DEST,GUILD,BOT,CATEGORY

ROOT='1793091836.135859'
REPLY='1793091900.000001'


class ThreadTests(unittest.TestCase):
    setUp=fixtures.MirrorTests.setUp
    tearDown=fixtures.MirrorTests.tearDown
    step=fixtures.MirrorTests.step
    drain=fixtures.MirrorTests.drain
    enqueue=fixtures.MirrorTests.enqueue
    def reply(self,text='reply',*,eid='EvReply',kind=None,version=None,broadcast=False,root=ROOT,ts=REPLY):
        p=message(text,eid=eid,ts=ts,kind=kind,version=version)
        target=p['event']['message'] if kind=='edit' else p['event']
        if kind!='delete':target['thread_ts']=root
        if broadcast:target['subtype']='thread_broadcast'
        return p

    def root_and_reply(self,broadcast=False,text='reply'):
        self.enqueue('root');self.drain();self.store.enqueue(self.reply(text,broadcast=broadcast));self.drain()

    def writes(self):return [c for c in self.discord.calls if c[0] in ('POST','PATCH','DELETE')]

    def content_writes(self):return [c for c in self.writes() if c[2] and 'content' in c[2]]

    def test_native_reply_uses_one_thread_and_never_parent_channel(self):
        self.root_and_reply()
        root=self.content_writes()[0];starter=self.store.parts(self.store.message('C00000001',ROOT))[0]['discord_id']
        creates=[c for c in self.writes() if c[1].endswith('/threads')]
        self.assertEqual(len(creates),1)
        self.assertEqual(creates[0][1],'/channels/'+DEST+'/messages/'+starter+'/threads')
        self.assertEqual(self.content_writes()[-1][1],'/channels/'+starter+'/messages')
        self.assertEqual(len(self.content_writes()),2)
        self.assertIn('[Alice](https://example.slack.com/team/U00000003)',self.content_writes()[-1][2]['content'])

    def test_profile_label_is_escaped_and_message_urls_cannot_choose_profile_origin(self):
        p=message();p['event']['user_profile']={'display_name':r'Alice](https://evil.invalid/) [x\y'}
        p['event']['author_url']='https://evil.invalid/'
        self.store.enqueue(p);self.drain()
        content=self.content_writes()[0][2]['content']
        self.assertTrue(content.startswith(r'[Alice\](https://evil.invalid/) \[x\\y](https://example.slack.com/team/U00000003)'))
        self.assertNotIn('(U00000003)',content)

    def test_untrusted_workspace_origin_or_invalid_user_id_never_forms_profile_link(self):
        self.enqueue();row=self.store.message('C00000001',ROOT)
        for url in ('https://example.slack.com.evil.invalid/','https://example.slack.com/@other/',
                    'https://user'+chr(64)+'example.slack.com/','http://example.slack.com/'):
            content=mirror_parts(row,self.cfg,workspace_url=url)[0]['content']
            self.assertNotIn('/team/',content)
        data=json.loads(row['data']);data['author_id']='U00000003/evil'
        content=mirror_parts(dict(row,data=json.dumps(data)),self.cfg,workspace_url=self.slack.workspace_url)[0]['content']
        self.assertNotIn('/team/',content)

    def test_linked_author_with_escaped_label_keeps_utf16_message_limit(self):
        p=message('😀'*3000);p['event']['user_profile']={'display_name':'['*100}
        self.store.enqueue(p);self.drain()
        self.assertGreater(len(self.content_writes()),1)
        for item in self.content_writes():
            self.assertLessEqual(len(item[2]['content'].encode('utf-16-le'))//2,2000)

    def test_broadcast_duplicates_in_channel_and_thread_with_distinct_nonces(self):
        self.root_and_reply(broadcast=True)
        copies=self.content_writes()[1:]
        self.assertEqual(len(copies),2)
        self.assertEqual(len({c[1] for c in copies}),2)
        self.assertEqual(len({c[2]['nonce'] for c in copies}),2)
        self.assertTrue(any(c[1]=='/channels/'+DEST+'/messages' for c in copies))
        self.assertTrue(all(c[2]['allowed_mentions']['parse']==[] for c in copies))

    def test_edit_delete_syncs_both_copies_after_restart(self):
        self.root_and_reply(broadcast=True)
        self.store=MirrorStore(self.policy.database,self.cfg,self.policy)
        start=len(self.writes());self.store.enqueue(self.reply('changed',eid='EvEdit',kind='edit',version='1793092000.000000'));self.drain()
        self.assertEqual([c[0] for c in self.writes()[start:]],['PATCH','PATCH'])
        start=len(self.writes());self.store.enqueue(self.reply(eid='EvDelete',kind='delete',version='1793092100.000000'));self.drain()
        self.assertEqual([c[0] for c in self.writes()[start:]],['DELETE','DELETE'])

    def test_plain_then_broadcast_then_duplicate_never_sends_second_thread_copy(self):
        self.root_and_reply()
        start=len(self.content_writes())
        self.store.enqueue(self.reply(eid='EvBroadcast',broadcast=True,version='1793091950.000000'));self.drain()
        self.store.enqueue(self.reply(eid='EvDuplicate',broadcast=True,version='1793091950.000000'));self.drain()
        copies=self.content_writes()[start:]
        self.assertEqual(len(copies),1);self.assertEqual(copies[0][1],'/channels/'+DEST+'/messages')

    def test_late_broadcast_flag_keeps_newer_edit_content(self):
        self.root_and_reply()
        self.store.enqueue(self.reply('latest',eid='EvEdit',kind='edit',version='1793092100.000000'));self.drain()
        self.store.enqueue(self.reply('old',eid='EvBroadcast',broadcast=True,version='1793092000.000000'));self.drain()
        self.assertIn('latest',self.content_writes()[-1][2]['content'])
        self.assertEqual(self.content_writes()[-1][1],'/channels/'+DEST+'/messages')

    def test_broadcast_reference_keeps_full_reply_attachments(self):
        self.enqueue('root');self.drain()
        p=self.reply();p['event']['files']=[{'name':'a.txt','size':1,'url_private':'https://files.slack.com/a'}]
        self.store.enqueue(p)
        self.store.enqueue(self.reply(eid='EvBroadcast',broadcast=True,version='1793091950.000000'));self.drain()
        self.assertTrue(all(c[3]==[('a.txt',b'x')] for c in self.content_writes()[1:]))

    def test_broadcast_first_then_older_full_reply_preserves_files_and_both_routes(self):
        self.enqueue('root');self.drain()
        self.store.enqueue(self.reply(eid='EvBroadcast',broadcast=True,version='1793091950.000000'))
        p=self.reply();p['event']['files']=[{'name':'a.txt','size':1,'url_private':'https://files.slack.com/a'}]
        self.store.enqueue(p);self.drain()
        self.assertEqual(len(self.content_writes()),3)
        self.assertTrue(all(c[3]==[('a.txt',b'x')] for c in self.content_writes()[1:]))

    def test_two_replies_share_single_native_thread(self):
        self.root_and_reply();self.store.enqueue(self.reply(eid='EvReply2',ts='1793091901.000001'));self.drain()
        self.assertEqual(len([c for c in self.writes() if c[1].endswith('/threads')]),1)
        self.assertEqual(self.content_writes()[-1][1],self.content_writes()[-2][1])

    def test_parent_before_activation_is_targeted_once_without_history_replay(self):
        old='1700000000.000001'
        self.slack.roots[old]={'type':'message','text':'old root','ts':old,'user':'U00000003'}
        self.store.enqueue(self.reply(root=old));self.drain()
        history=[body for method,body in self.slack.calls if method=='conversations.history']
        self.assertEqual(history,[{'channel':'C00000001','oldest':old,'latest':old,'inclusive':'true','limit':1}])
        self.assertEqual(len(self.content_writes()),2)
        self.assertIn('old root',self.content_writes()[0][2]['content'])
        self.store.enqueue(message('edited old',eid='EvOldEdit',kind='edit',ts=old,version='1793092200.000000'));self.drain()
        self.assertEqual(self.content_writes()[-1][0],'PATCH')

    def test_reply_before_parent_create_waits_for_one_copy(self):
        self.store.enqueue(self.reply());self.enqueue('root');self.drain()
        self.assertEqual(len(self.content_writes()),2)
        self.assertIn('root',self.content_writes()[0][2]['content'])

    def test_missing_parent_uses_metadata_context_without_copying_unrelated_history(self):
        self.slack.roots[ROOT]={'text':'wrong','ts':'1700000001.000000','user':'U00000003'}
        self.store.enqueue(self.reply());self.drain()
        self.assertIn('parent message unavailable',self.content_writes()[0][2]['content'])
        self.assertNotIn('wrong',self.content_writes()[0][2]['content'])

    def test_event_parent_snapshot_avoids_history_request(self):
        p=self.reply(broadcast=True)
        p['event']['root']={'text':'snapshot root','ts':ROOT,'user':'U00000003'}
        self.store.enqueue(p);self.drain()
        self.assertFalse(any(method=='conversations.history' for method,_ in self.slack.calls))
        self.assertIn('snapshot root',self.content_writes()[0][2]['content'])

    def test_thread_create_unknown_is_not_repeated_after_restart(self):
        self.enqueue('root');self.drain()
        original=self.discord.call
        def unknown(method,path,body=None,files=None):
            if path.endswith('/threads'):
                self.discord.calls.append((method,path,body,files));raise TimeoutError()
            return original(method,path,body,files)
        self.discord.call=unknown;self.store.enqueue(self.reply());self.drain()
        self.store=MirrorStore(self.policy.database,self.cfg,self.policy);self.drain()
        self.assertEqual(len([c for c in self.writes() if c[1].endswith('/threads')]),1)
        self.assertEqual(self.store.message('C00000001',REPLY)['state'],'uncertain')

    def test_lost_create_response_reconciles_deterministic_thread_id(self):
        self.enqueue('root');self.drain();original=self.discord.call
        def lost(method,path,body=None,files=None):
            result=original(method,path,body,files)
            return ('uncertain',None) if path.endswith('/threads') else result
        self.discord.call=lost;self.store.enqueue(self.reply());self.drain()
        self.assertEqual(len([c for c in self.writes() if c[1].endswith('/threads')]),1)
        self.assertEqual(self.store.message('C00000001',REPLY)['state'],'clean')

    def test_archived_thread_unarchives_for_edit_without_an_extra_message(self):
        self.root_and_reply()
        ident=next(iter(self.discord.threads));self.discord.threads[ident]['thread_metadata']['archived']=True
        before=len(self.content_writes())
        self.store.enqueue(self.reply('updated',eid='EvEdit',kind='edit',version='1793092000.000000'));self.drain()
        self.assertEqual(len(self.content_writes()),before+1)
        self.assertEqual(self.content_writes()[-1][0],'PATCH')
        self.assertTrue(any(c[0]=='PATCH' and c[1]=='/channels/'+ident for c in self.writes()))

    def test_moderator_locked_thread_holds_reply_and_does_not_flatten(self):
        self.root_and_reply();ident=next(iter(self.discord.threads))
        self.discord.threads[ident]['thread_metadata'].update(archived=True,locked=True)
        before=len(self.content_writes());self.store.enqueue(self.reply(eid='EvReply2',ts='1793092300.000001'))
        self.step(now=2200000000)
        self.assertEqual(len(self.content_writes()),before)
        self.assertEqual(self.store.message('C00000001','1793092300.000001')['state'],'queued')

    def test_parent_deletion_does_not_remove_other_messages_or_thread(self):
        self.root_and_reply();ident=next(iter(self.discord.threads))
        self.store.enqueue(message(eid='EvRootDelete',kind='delete',version='1793092000.000000'));self.drain()
        self.store.enqueue(self.reply(eid='EvReply2',ts='1793092300.000001'));self.drain()
        self.assertIn(ident,self.discord.threads)
        self.assertEqual(self.content_writes()[-1][1],'/channels/'+ident+'/messages')
        self.assertFalse(any(c[0]=='DELETE' and c[1]=='/channels/'+ident for c in self.writes()))

    def test_name_lookup_is_cached_and_fallback_still_keeps_id(self):
        self.enqueue('root');self.drain()
        self.enqueue('next',eid='EvNext',ts='1793091900.000002');self.drain()
        self.assertEqual(sum(method=='users.info' for method,_ in self.slack.calls),1)
        self.assertIn('[Alice](https://example.slack.com/team/U00000003)',self.content_writes()[-1][2]['content'])

    def test_missing_profile_scope_does_not_drop_messages_or_request_other_credentials(self):
        self.slack.profile_ok=False;self.enqueue();self.drain()
        self.assertIn('[Slack user](https://example.slack.com/team/U00000003)',self.content_writes()[0][2]['content'])
        self.assertEqual(self.store.counts(),{'clean':1})

    def test_supplied_profile_name_and_id_need_no_profile_scope(self):
        p=message();p['event']['user_profile']={'display_name':'Example Name'}
        self.store.enqueue(p);self.drain()
        self.assertIn('[Example Name](https://example.slack.com/team/U00000003)',self.content_writes()[0][2]['content'])
        self.assertFalse(any(method=='users.info' for method,_ in self.slack.calls))

    def test_v1_migration_retains_ids_without_reposting_old_clean_rows(self):
        self.enqueue('old')
        with self.store.connect() as c:
            c.execute("UPDATE mirror_messages SET state='clean'")
            c.execute("INSERT INTO mirror_parts VALUES('C00000001',?,0,?,'legacy','sent')",(ROOT,'666666666666666666'))
        self.store=MirrorStore(self.policy.database,self.cfg,self.policy);self.drain()
        self.assertEqual(self.discord.calls,[])
        self.store.enqueue(self.reply());self.drain()
        self.assertTrue(any(c[1]=='/channels/'+DEST+'/messages/666666666666666666/threads' for c in self.writes()))
        self.assertFalse(any(c[0]=='POST' and c[1]=='/channels/'+DEST+'/messages' for c in self.writes()))

    def test_old_queued_payload_keeps_placement_while_new_ingress_is_promoted(self):
        self.store.enqueue(self.reply())
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM mirror_messages').fetchone();data=json.loads(row[0]);data.pop('schema')
            c.execute('UPDATE mirror_messages SET data=?',(json.dumps(data),))
        self.drain()
        self.assertEqual(self.content_writes()[0][1],'/channels/'+DEST+'/messages')
        self.assertFalse(self.discord.threads)

    def test_broadcast_multipart_shrink_removes_surplus_from_both_destinations(self):
        self.root_and_reply(broadcast=True,text='x'*3500)
        before=len(self.writes())
        self.store.enqueue(self.reply('short',eid='EvShort',kind='edit',version='1793092200.000000'));self.drain()
        later=self.writes()[before:]
        self.assertEqual(sum(c[0]=='DELETE' for c in later),4)
        self.assertEqual(sum(c[0]=='PATCH' for c in later),2)

    def test_partial_broadcast_unknown_never_repeats_the_successful_copy(self):
        self.enqueue('root');self.drain();original=self.discord.call
        def fail_channel(method,path,body=None,files=None):
            if method=='POST' and path=='/channels/'+DEST+'/messages':
                self.discord.calls.append((method,path,body,files));return 'uncertain',None
            return original(method,path,body,files)
        self.discord.call=fail_channel;self.store.enqueue(self.reply(broadcast=True));self.drain()
        before=len(self.writes());self.store=MirrorStore(self.policy.database,self.cfg,self.policy);self.drain()
        self.assertEqual(len(self.writes()),before)
        self.assertEqual(self.store.message('C00000001',REPLY)['state'],'uncertain')

    def test_pause_heartbeat_is_explicit_and_does_not_change_receipts(self):
        self.enqueue();self.store.heartbeat(2000000000,paused=True)
        self.assertTrue(self.store.worker_alive(2000000001));self.assertTrue(self.store.worker_paused())
        self.assertEqual(self.store.counts(),{'queued':1})
        self.store.heartbeat(2000000001)
        self.assertFalse(self.store.worker_paused())


class ThreadPermissions(unittest.TestCase):
    setUp=fixtures.PermissionTests.setUp
    def test_thread_specific_permissions_and_parent_type_guard(self):
        self.assertFalse(self.client.allowed(DEST,'general',create_thread=True))
        roles=self.data['/guilds/'+GUILD+'/roles']
        roles[0]['permissions']=str((1<<10)|(1<<35)|(1<<38))
        self.assertTrue(self.client.allowed(DEST,'general',create_thread=True))
        self.assertFalse(self.client.allowed(DEST,'general'))
        ident='666666666666666666'
        self.data['/channels/'+ident]={'id':ident,'guild_id':GUILD,'parent_id':DEST,'type':11}
        self.assertTrue(self.client.allowed(DEST,'general',thread=ident))
        for field,value in [('type',12),('guild_id',CATEGORY),('parent_id',GUILD),('id',BOT)]:
            old=self.data['/channels/'+ident][field];self.data['/channels/'+ident][field]=value
            self.assertFalse(self.client.allowed(DEST,'general',thread=ident))
            self.data['/channels/'+ident][field]=old


if __name__=='__main__':unittest.main()
