"""Offline visible-name/mention regressions; never contact Slack or Discord."""
import json
import unittest

from bot import clean_mirror_name,mirror_parts,MirrorStore
import test_mirror as fixtures
from test_mirror import message

AUTHOR='U00000003'
MENTION='U00000004'
OTHER='U00000005'
ROOT='1793091836.135859'
REPLY='1793091900.000001'


class Profiles(fixtures.SlackFake):
    def __init__(self):
        super().__init__()
        self.names={AUTHOR:'alice_dev',MENTION:'Bob_Smith',OTHER:'Carol_Test','W00000006':'Global_User'}
        self.missing=set();self.wrong=False;self.raise_error=False

    def call(self,method,body):
        if method!='users.info':return super().call(method,body)
        self.calls.append((method,body))
        if self.raise_error:raise OSError('profile lookup unavailable')
        user=body['user']
        if user in self.missing:return 'failed',None
        return 'sent',{'user':{'id':OTHER if self.wrong and user==MENTION else user,
                              'profile':{'display_name':self.names.get(user,'Example_User')}}}


class NameTests(unittest.TestCase):
    tearDown=fixtures.MirrorTests.tearDown
    step=fixtures.MirrorTests.step
    drain=fixtures.MirrorTests.drain
    enqueue=fixtures.MirrorTests.enqueue
    def setUp(self):
        fixtures.MirrorTests.setUp(self);self.slack=Profiles()

    def contents(self):return [c[2]['content'] for c in self.discord.calls if c[2] and 'content' in c[2]]
    def lookups(self):return [body['user'] for method,body in self.slack.calls if method=='users.info']

    def test_sender_punctuation_survives_ingress_lookup_and_markdown_rendering(self):
        name=r'dev_ops_*~|`[x](y)\<tag>@team'
        p=message();p['event']['user_profile']={'display_name':name}
        self.store.enqueue(p);self.drain()
        data=json.loads(self.store.message('C00000001',ROOT)['data'])
        self.assertEqual(data['author_name'],name)
        self.assertTrue(self.contents()[0].startswith(r'[dev\_ops\_\*\~\|\`\[x\]\(y\)\\\<tag\>@team]('))
        self.assertEqual(self.lookups(),[])
        self.assertEqual(clean_mirror_name('a_b\r\n\x00*c*'),'a_b*c*')

    def test_looked_up_sender_underscores_are_retained(self):
        self.enqueue();self.drain()
        self.assertTrue(self.contents()[0].startswith(r'[alice\_dev](https://example.slack.com/team/U00000003)'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT name FROM mirror_profile_names WHERE user_id=?',(AUTHOR,)).fetchone()[0],'alice_dev')

    def test_multiple_mentions_deduplicate_lookup_and_reuse_sender_name(self):
        self.enqueue('<@U00000004> <@U00000003> <@U00000004|stale_label> <@W00000006>');self.drain()
        text=self.contents()[0]
        self.assertEqual(text.count(r'[Bob\_Smith](https://example.slack.com/team/U00000004)'),2)
        self.assertEqual(text.count(r'[alice\_dev](https://example.slack.com/team/U00000003)'),2)
        self.assertIn(r'[Global\_User](https://example.slack.com/team/W00000006)',text)
        self.assertCountEqual(self.lookups(),[AUTHOR,MENTION,'W00000006'])
        self.assertNotIn('@U00000004',text);self.assertNotIn('stale_label',text)

    def test_unknown_mention_keeps_profile_link_with_generic_label(self):
        self.slack.missing.add(MENTION);self.enqueue('<@U00000004|untrusted_name>');self.drain()
        self.assertIn('[Slack user](https://example.slack.com/team/U00000004)',self.contents()[0])
        self.assertNotIn('untrusted_name',self.contents()[0]);self.assertEqual(self.store.counts(),{'clean':1})

    def test_wrong_identity_and_lookup_exception_never_use_another_users_name(self):
        self.slack.wrong=True;self.enqueue('<@U00000004>');self.drain()
        self.assertIn('[Slack user](https://example.slack.com/team/U00000004)',self.contents()[0])
        self.slack.raise_error=True;self.enqueue('<@U00000005>',eid='EvOther',ts='1793091900.000002');self.drain()
        self.assertIn('[Slack user](https://example.slack.com/team/U00000005)',self.contents()[-1])
        self.assertEqual(self.store.counts(),{'clean':2})

    def test_mention_label_cannot_inject_markdown_links_or_mentions(self):
        self.slack.names[MENTION]=r'x_](https://evil.invalid/) *`<@123>|~\z'
        self.enqueue('<@U00000004>');self.drain()
        self.assertIn(r'[x\_\]\(https://evil.invalid/\) \*\`\<@123\>\|\~\\z](https://example.slack.com/team/U00000004)',self.contents()[0])

    def test_inline_double_and_fenced_code_are_preserved_without_lookups(self):
        text='outside <@U00000004> `one <@U00000005>` ``two <@U00000005>``\n```py\n<@U00000005> <!here> <https://example.com|code>\n```\n````<@U00000005>```still code````'
        self.enqueue(text);self.drain();content=self.contents()[0]
        self.assertIn('`one <@U00000005>`',content)
        self.assertIn('``two <@U00000005>``',content)
        self.assertIn('```py\n<@U00000005> <!here> <https://example.com|code>\n```',content)
        self.assertIn('````<@U00000005>```still code````',content)
        self.assertCountEqual(self.lookups(),[AUTHOR,MENTION])

    def test_unclosed_fence_stays_code_and_unmatched_inline_does_not_hide_mentions(self):
        self.enqueue('outside <@U00000004>\n```unclosed <@U00000005>');self.drain()
        self.assertIn('```unclosed <@U00000005>',self.contents()[0]);self.assertNotIn(OTHER,self.lookups())
        self.enqueue('unmatched ` <@U00000005>',eid='EvInline',ts='1793091900.000002');self.drain()
        self.assertIn(r'[Carol\_Test](https://example.slack.com/team/U00000005)',self.contents()[-1])

    def test_html_escaped_literals_are_not_interpreted_as_mentions(self):
        self.enqueue('&lt;@U00000004&gt; &amp; <#C00000001|general> <!here> <https://example.com?a=1&amp;b=2|a &amp; b>');self.drain()
        text=self.contents()[0]
        self.assertIn('<@U00000004> & #general @channel a & b (https://example.com?a=1&b=2)',text)
        self.assertEqual(self.lookups(),[AUTHOR])
        self.assertEqual(self.discord.calls[0][2]['allowed_mentions']['parse'],[])

    def test_escaped_backticks_do_not_hide_a_real_mention(self):
        self.enqueue(r'\`literal <@U00000004>\`');self.drain()
        self.assertIn(r'[Bob\_Smith](https://example.slack.com/team/U00000004)',self.contents()[0])

    def test_legacy_lossy_cache_is_retained_but_not_used_for_new_names(self):
        self.enqueue()
        with self.store.connect() as db:
            db.execute('INSERT INTO mirror_authors VALUES(?,?,?)',(AUTHOR,'alicedev',2100000000))
            data=json.loads(db.execute('SELECT data FROM mirror_messages').fetchone()[0])
            data.update(author_name='alicedev');data.pop('author_name_format')
            db.execute('UPDATE mirror_messages SET data=?',(json.dumps(data),))
        self.drain();self.assertIn(r'[alice\_dev](',self.contents()[0]);self.assertEqual(self.lookups(),[AUTHOR])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT name FROM mirror_authors').fetchone()[0],'alicedev')

    def test_future_edit_of_legacy_name_refreshes_instead_of_preserving_loss(self):
        self.enqueue();self.drain()
        with self.store.connect() as db:
            data=json.loads(db.execute('SELECT data FROM mirror_messages').fetchone()[0]);data.update(author_name='alicedev')
            data.pop('author_name_format');db.execute('UPDATE mirror_messages SET data=?',(json.dumps(data),))
        self.enqueue('edited',eid='EvEdit',kind='edit',version='1793092000.000000');self.drain()
        self.assertIn(r'[alice\_dev](',self.contents()[-1]);self.assertEqual(self.discord.calls[-1][0],'PATCH')

    def test_names_are_frozen_for_one_generation_and_cache_is_shared_after_restart(self):
        self.enqueue('<@U00000004>');row=self.store.claim(2000000000)
        resolved=self.store.resolve_author(row,self.slack,2000000000)
        self.slack.names[MENTION]='Changed_Name'
        again=self.store.resolve_author(resolved,self.slack,2000000200)
        self.assertEqual(json.loads(again['data'])['mention_names'][MENTION],'Bob_Smith')
        self.assertCountEqual(self.lookups(),[AUTHOR,MENTION])
        self.store.finish(resolved,'queued')
        self.store=MirrorStore(self.policy.database,self.cfg,self.policy);self.drain()
        self.assertIn(r'[Bob\_Smith](',self.contents()[0])

    def test_missing_name_cache_expires_for_a_future_event(self):
        self.slack.missing.add(MENTION)
        for index,now in enumerate([2000000000,2000000290,2000000301]):
            self.enqueue('<@U00000004>',eid='EvCache'+str(index),ts='1793091900.'+str(index).zfill(6))
            row=self.store.claim(now);resolved=self.store.resolve_author(row,self.slack,now)
            self.assertEqual(json.loads(resolved['data'])['mention_names'][MENTION],'' if index<2 else 'Bob_Smith')
            self.store.finish(resolved,'clean');self.slack.missing.discard(MENTION)
        self.assertEqual(self.lookups().count(MENTION),2)

    def test_new_name_cache_remains_bounded_without_erasing_legacy_cache(self):
        with self.store.connect() as db:
            db.executemany('INSERT INTO mirror_profile_names VALUES(?,?,?)',
                           [('U'+str(i).zfill(8),'Saved_Name',1) for i in range(100,2100)])
            db.execute('INSERT INTO mirror_authors VALUES(?,?,?)',(AUTHOR,'LegacyName',1))
        self.enqueue();self.drain()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM mirror_profile_names').fetchone()[0],2000)
            self.assertEqual(db.execute('SELECT name FROM mirror_authors').fetchone()[0],'LegacyName')

    def test_part_boundary_does_not_split_profile_link_and_stays_under_utf16_limit(self):
        self.enqueue('a'*1580+'<@U00000004>'+'😀'*1100);self.drain()
        parts=self.contents();self.assertGreater(len(parts),1)
        self.assertEqual(sum(r'[Bob\_Smith](https://example.slack.com/team/U00000004)' in p for p in parts),1)
        for part in parts:self.assertLessEqual(len(part.encode('utf-16-le'))//2,2000)

    def test_lookup_work_is_bounded_and_excess_mentions_remain_linked(self):
        users=['U'+str(i).zfill(8) for i in range(100,203)]
        self.enqueue(' '.join('<@'+u+'>' for u in users));self.drain()
        self.assertEqual(len(self.lookups()),101)
        self.assertIn('[Slack user](https://example.slack.com/team/'+users[-1]+')','\n'.join(self.contents()))

    def test_broadcast_mention_links_survive_edit_and_delete_in_both_destinations(self):
        self.enqueue('root');self.drain()
        reply=message('<@U00000004>',eid='EvReply',ts=REPLY)
        reply['event'].update(thread_ts=ROOT,subtype='thread_broadcast')
        self.store.enqueue(reply);self.drain()
        self.assertTrue(all(r'[Bob\_Smith](' in s for s in self.contents()[-2:]))
        edit=message('<@U00000005>',eid='EvEdit',ts=REPLY,kind='edit',version='1793092000.000000')
        edit['event']['message']['thread_ts']=ROOT
        self.store.enqueue(edit);self.drain()
        self.assertTrue(all(r'[Carol\_Test](' in s for s in self.contents()[-2:]))
        self.assertEqual([c[0] for c in self.discord.calls if c[0] in ('POST','PATCH','DELETE')][-2:],['PATCH','PATCH'])
        self.store.enqueue(message(eid='EvDelete',ts=REPLY,kind='delete',version='1793092100.000000'));self.drain()
        self.assertEqual([c[0] for c in self.discord.calls if c[0] in ('POST','PATCH','DELETE')][-2:],['DELETE','DELETE'])


if __name__=='__main__':unittest.main()
