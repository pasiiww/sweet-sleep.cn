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
        state.parsers['group_message_create']({'id':'event','d':{'id':'message','content':'普通发言','timestamp':'2026-09-09T20:00:00+08:00','group_openid':'group001','author':{'member_openid':'owner001'},'mentions':[{'id':'different_bot','bot':True}]}})
        event,msg=dispatched[0];self.assertEqual(event,'group_message_create');self.assertIsInstance(msg,GroupMessage)
        self.assertEqual(msg.author.member_openid,'owner001');self.assertFalse(msg.sweet_mentioned)
        await self.bot.on_group_message_create(msg);self.retriever.search.assert_not_awaited()
    async def test_mentioned_full_event_and_at_event_dedup(self):
        msg=self.message();msg.sweet_mentioned=True
        await self.bot.on_group_message_create(msg);await self.bot.on_group_at_message_create(msg)
        self.assertEqual(self.retriever.search.await_count,1);self.assertEqual(msg.reply.await_count,1)
    async def test_outbox_persistence_and_dedup(self):
        learner=Learner(self.seen.conn,'http://unused','token','kb');msg=self.message()
        learner.observe(msg);learner.observe(msg)
        rows=self.seen.conn.execute('SELECT payload FROM learning_outbox').fetchall();self.assertEqual(len(rows),1)
        data=json.loads(rows[0][0]);self.assertEqual(data['member_id'],'owner001');self.assertEqual(data['kb_id'],'kb')
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
            learner.observe(self.message());self.assertTrue(await learner.flush_once());self.assertEqual(received['member_id'],'owner001')
            self.assertFalse(await learner.flush_once())
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
    def test_forwarded_history_is_not_a_quoted_reply(self):
        self.assertFalse(compat.reference_metadata({'message_type':102,'msg_elements':[{'content':'凯伊售价100元'}]})['is_reply'])
