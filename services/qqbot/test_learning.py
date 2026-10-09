import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from botpy.connection import ConnectionState
from botpy.message import GroupMessage
import compat
from learner import Learner
from bot import KnowledgeBot, SeenMessages


class LearningBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.seen=SeenMessages(Path(self.temp.name)/'test.db')
        self.retriever=SimpleNamespace(kb_id='kb',search=AsyncMock(return_value={'answer':'回答'}))
        self.bot=KnowledgeBot(self.retriever,self.seen);self.bot.learner=SimpleNamespace(observe=lambda m:None)
    async def asyncTearDown(self):self.seen.conn.close();self.temp.cleanup()
    def message(self,mid='m1'):
        return SimpleNamespace(id=mid,content='凯伊售价100元',timestamp='2026-09-09T20:00:00+08:00',group_openid='group001',author=SimpleNamespace(member_openid='owner001'),reply=AsyncMock(return_value={'id':'r'}))
    async def test_full_event_parser_and_no_ordinary_reply(self):
        dispatched=[];state=ConnectionState(lambda e,m:dispatched.append((e,m)),None);state.robot=SimpleNamespace(id='own_bot')
        state.parsers['group_message_create']({'id':'event','d':{'id':'message','content':'普通发言','timestamp':'2026-09-09T20:00:00+08:00','group_openid':'group001','author':{'member_openid':'owner001','username':'这是超过十二字的群友昵称测试'},'mentions':[{'id':'different_bot','bot':True}]}})
        event,msg=dispatched[0];self.assertEqual(event,'group_message_create');self.assertIsInstance(msg,GroupMessage)
        self.assertEqual(msg.author.member_openid,'owner001');self.assertFalse(msg.sweet_mentioned)
        self.assertEqual(compat.sender_name(msg), '这是超过十二字的群友昵称')
        await self.bot.on_group_message_create(msg);self.retriever.search.assert_not_awaited()
    async def test_mentioned_full_event_and_at_event_dedup(self):
        msg=self.message();msg.sweet_mentioned=True
        await self.bot.on_group_message_create(msg);await self.bot.on_group_at_message_create(msg)
        self.assertEqual(self.retriever.search.await_count,1);self.assertEqual(msg.reply.await_count,1)
    async def test_full_event_platform_self_mention(self):
        for mention, expected in [({'id':'group_scoped_bot','is_you':True},True),
                                  ({'id':'another_bot','bot':True},False),
                                  ({'id':'member','is_you':'false'},False),
                                  ({'id':'own_bot'},True)]:
            msg=compat.FullGroupMessage(None,'event',{'id':'message','content':'/帮助',
                'group_openid':'group001','author':{'member_openid':'user'},
                'mentions':[mention]},'own_bot')
            self.assertEqual(msg.sweet_mentioned,expected)
            if mention.get('is_you') is True:
                self.assertEqual(msg.sweet_learning['mentions'],[])

    async def test_mention_name_resolves_id_to_member_openid_cache(self):
        cached = self.message('cached-name')
        cached.author = SimpleNamespace(member_openid='member-openid')
        cached.sweet_sender_name = '缓存昵称'
        self.seen.observe_group_message(cached)
        msg = compat.FullGroupMessage(None, 'event', {
            'id': 'mention', 'content': '<@tag-id> 你好', 'group_openid': 'group001',
            'author': {'member_openid': 'owner001'},
            'mentions': [{'id': 'tag-id', 'member_openid': 'member-openid'}],
        }, 'own_bot')
        resolver = lambda member_id: self.seen.first_member_name('group001', member_id)
        self.assertEqual(compat.render_mention_tags(msg.content, msg, resolver), '@缓存昵称 你好')

    async def test_mention_name_resolves_tag_id_directly_from_cache_without_metadata(self):
        cached = self.message('cached-name-direct')
        cached.author = SimpleNamespace(member_openid='member-openid')
        cached.sweet_sender_name = '直查昵称'
        self.seen.observe_group_message(cached)
        msg = SimpleNamespace(content='<@member-openid> 你好', mentions=[],
                              sweet_mention_aliases={}, sweet_mention_names={},
                              sweet_you_mention_ids=set())
        resolver = lambda member_id: self.seen.first_member_name('group001', member_id)
        self.assertEqual(compat.render_mention_tags(msg.content, msg, resolver), '@直查昵称 你好')

    async def test_at_event_marks_unmatched_single_tag_as_bot_you(self):
        dispatched=[];state=ConnectionState(lambda e,m:dispatched.append((e,m)),None)
        state.robot=SimpleNamespace(id='own_bot')
        state.parsers['group_at_message_create']({'id':'event','d':{
            'id':'message','content':'<@opaque-bot-tag> /帮助','group_openid':'group001',
            'author':{'member_openid':'owner001'},'mentions':[],
        }})
        msg=dispatched[0][1]
        self.assertTrue(msg.sweet_mentioned)
        self.assertEqual(compat.render_mention_tags(msg.content,msg), '@你 /帮助')

    async def test_at_event_identifies_bot_among_multiple_mention_tags(self):
        dispatched=[];state=ConnectionState(lambda e,m:dispatched.append((e,m)),None)
        state.robot=SimpleNamespace(id='own_bot')
        state.parsers['group_at_message_create']({'id':'event','d':{
            'id':'message','content':'<@friend-tag> <@bot-tag> /帮助','group_openid':'group001',
            'author':{'member_openid':'owner001'},
            'mentions':[{'id':'friend-tag'},{'id':'bot-tag','bot':True}],
        }})
        msg=dispatched[0][1]
        self.assertEqual(compat.render_mention_tags(msg.content,msg), '@群友 @你 /帮助')

    async def test_raw_is_you_mention_renders_as_you(self):
        msg=compat.FullGroupMessage(None,'event',{
            'id':'message','content':'<@group-scoped-bot> 你好','group_openid':'group001',
            'author':{'member_openid':'owner001'},
            'mentions':[{'id':'group-scoped-bot','is_you':True}],
        },'own_bot')
        self.assertTrue(msg.sweet_mentioned)
        self.assertEqual(compat.render_mention_tags(msg.content,msg), '@你 你好')

    async def test_outbox_persistence_and_dedup(self):
        learner=Learner(self.seen.conn,'http://unused','token','kb');msg=self.message()
        msg.sweet_sender_name='这是超过十二字的群友昵称测试'
        learner.observe(msg);learner.observe(msg)
        rows=self.seen.conn.execute('SELECT payload FROM learning_outbox').fetchall();self.assertEqual(len(rows),1)
        data=json.loads(rows[0][0]);self.assertEqual(data['member_id'],'owner001');self.assertEqual(data['kb_id'],'kb')
        self.assertEqual(data['member_name'],'这是超过十二字的群友昵称')
        msg.id='command';msg.content='<@1905586446> /身份';learner.observe(msg)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM learning_outbox').fetchone()[0],1)
        second=SeenMessages(Path(self.temp.name)/'test.db');self.assertEqual(second.conn.execute('SELECT count(*) FROM learning_outbox').fetchone()[0],1);second.conn.close()
    async def test_outbox_http_contract(self):
        received={}
        async def handler(request):
            self.assertEqual(request.headers['Authorization'],'Bearer learning-secret')
            received.update(await request.json());return web.json_response({'accepted':True})
        app=web.Application();app.router.add_post('/learning/events',handler);runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        try:
            learner=Learner(self.seen.conn,f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/learning/events','learning-secret','kb')
            message=self.message();message.sweet_sender_name='群友昵称'
            learner.observe(message);self.assertTrue(await learner.flush_once())
            self.assertEqual(received['member_id'],'owner001');self.assertEqual(received['member_name'],'群友昵称')
            self.assertFalse(await learner.flush_once())
        finally:await runner.cleanup()

    async def test_owner_notification_private_delivery_and_failure_receipt(self):
        receipts=[]
        async def claim(request):
            self.assertEqual(request.headers['Authorization'],'Bearer learning-secret')
            return web.json_response({'ids':[1],'receipt':'r'*48,'openid':'owner-private','content':'知识库更新'})
        async def ack(request):receipts.append(await request.json());return web.json_response({'ok':True})
        app=web.Application();app.router.add_post('/owner-notifications/claim',claim);app.router.add_post('/owner-notifications/ack',ack)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        try:
            learner=Learner(self.seen.conn,f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/learning/events','learning-secret','kb')
            learner.api=SimpleNamespace(post_c2c_message=AsyncMock(return_value={'id':'sent'}))
            await learner.notify_once()
            learner.api.post_c2c_message.assert_awaited_once_with(openid='owner-private',msg_type=0,content='知识库更新')
            self.assertEqual(receipts[-1]['status'],'delivered')
            learner.api.post_c2c_message.side_effect=RuntimeError('private details')
            await learner.notify_once();self.assertEqual(receipts[-1]['status'],'failed');self.assertEqual(receipts[-1]['error'],'RuntimeError')
        finally:await runner.cleanup()

    async def test_reply_metadata_for_full_and_at_events(self):
        dispatched=[];state=ConnectionState(lambda e,m:dispatched.append((e,m)),None);state.robot=SimpleNamespace(id='own_bot')
        data={'id':'reply','content':'100元','author':{'member_openid':'owner001'},'group_openid':'group001','message_type':103,
              'message_scene':{'ext':['ref_msg_idx=ref001','msg_idx=own001','auth_token=do-not-copy']},
              'msg_elements':[{'content':'凯伊多少钱','author':{'member_openid':'visitor1'}}]}
        for event in ('group_message_create','group_at_message_create'):
            state.parsers[event]({'id':'event','d':data})
            meta=dispatched[-1][1].sweet_learning
            self.assertTrue(meta['is_reply']);self.assertEqual(meta['reference']['quotes'][0]['content'],'凯伊多少钱')
            self.assertNotIn('do-not-copy',json.dumps(meta))
        learner=Learner(self.seen.conn,'http://unused','token','kb');learner.observe(dispatched[-1][1])
        body=json.loads(self.seen.conn.execute('SELECT payload FROM learning_outbox').fetchone()[0]);self.assertTrue(body['is_reply'])

    async def test_quoted_image_event_is_forwarded_ephemerally_to_vision_model(self):
        data = {'id': 'question', 'content': '<@!own_bot> 这张图是什么？',
                'timestamp': '2026-09-28T12:00:00+08:00', 'group_openid': 'group001',
                'author': {'member_openid': 'owner001'}, 'message_type': 103,
                'msg_elements': [{'content': '这张图是什么？', 'attachments': [
                    {'content_type': 'image/jpeg', 'url': 'https://gchat.qpic.cn/quoted-image'}]}]}
        message = compat.FullGroupMessage(None, 'event', data, 'own_bot')
        self.assertEqual(message.sweet_quoted_images, [
            {'content_type': 'image/jpeg', 'url': 'https://gchat.qpic.cn/quoted-image'}])
        image = 'data:image/jpeg;base64,/9j/2Q=='
        self.bot.send_answer = AsyncMock(return_value={'id': 'response'})
        with patch('bot.read_image_data_url', new=AsyncMock(return_value=image)) as read_image:
            await self.bot.answer(message, 'group', mentioned=True)
        self.assertEqual(read_image.await_args.args[0], 'https://gchat.qpic.cn/quoted-image')
        self.assertEqual(self.retriever.search.call_args.kwargs['image_data_urls'], [image])
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM image_occurrences').fetchone()[0], 0)
    def test_forwarded_history_is_not_a_quoted_reply(self):
        self.assertFalse(compat.reference_metadata({'message_type':102,'msg_elements':[{'content':'凯伊售价100元'}]})['is_reply'])

    def test_member_mentions_preserved_without_bots_and_deduplicated(self):
        data={'mentions':[{'id':'own_bot'},{'id':'other_bot','bot':True},{'id':'guest001'},{'member_openid':'guest002','id':'different'},{'id':'guest001'}]}
        self.assertEqual(compat.reference_metadata(data,{'own_bot'})['mentions'],['guest001','guest002'])
        self.assertEqual(compat.reference_metadata({'content':'@guest001 hello'})['mentions'],[])

    async def test_bot_authors_never_reply_search_or_enqueue(self):
        from unittest.mock import Mock
        learner=Learner(self.seen.conn,'http://unused','token','kb')
        observed=Mock();self.bot.learner=SimpleNamespace(observe=observed)
        for kind in ('group','c2c'):
            msg=self.message('bot-'+kind);msg.sweet_author_bot=True;msg.sweet_mentioned=True
            learner.observe(msg)
            if kind=='group':
                await self.bot.on_group_at_message_create(msg);await self.bot.on_group_message_create(msg)
            else:await self.bot.on_c2c_message_create(msg)
            msg.reply.assert_not_awaited()
        observed.assert_not_called();self.retriever.search.assert_not_awaited()
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM learning_outbox').fetchone()[0],0)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM seen').fetchone()[0],0)

    def test_sdk_preserves_bot_flag_and_owner_role(self):
        dispatched=[];state=ConnectionState(lambda e,m:dispatched.append((e,m)),None);state.robot=SimpleNamespace(id='own_bot')
        data={'id':'m','content':'售价100元','group_openid':'group001','author':{'member_openid':'owner001','user_openid':'owner001','bot':True,'member_role':'owner'}}
        for event in ('group_message_create','group_at_message_create','c2c_message_create'):
            state.parsers[event]({'id':'event','d':data});m=dispatched[-1][1]
            self.assertTrue(compat.is_bot(m))
            if event!='c2c_message_create':self.assertEqual(m.sweet_learning['member_role'],'owner')
