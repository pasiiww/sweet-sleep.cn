import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from botpy.message import C2CMessage, GroupMessage
from bot import KnowledgeBot, Retriever, SeenMessages, format_results, format_reply, normalize, conversation_key


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.seen = SeenMessages(Path(self.temp.name) / 'seen.db')
        self.retriever = SimpleNamespace(kb_id='test', search=AsyncMock(return_value={'results': [
            {'title': '说明', 'content': '机器人返回原文。', 'source': '演示', 'ordinal': 0}]}))
        self.bot = KnowledgeBot(self.retriever, self.seen)

    async def asyncTearDown(self):
        self.seen.conn.close()
        self.temp.cleanup()

    def message(self, content, id='m1'):
        return SimpleNamespace(id=id, content=content, reply=AsyncMock(return_value={'id': 'reply1'}))

    async def test_private_message_retrieval(self):
        message = self.message('/检索 机器人怎么使用')
        await self.bot.on_c2c_message_create(message)
        self.retriever.search.assert_awaited_once_with('机器人怎么使用', group_id='', history=[])
        kwargs = message.reply.call_args.kwargs
        self.assertIn('机器人返回原文', kwargs['content'])
        self.assertIn('来源：演示', kwargs['content'])
        self.assertEqual(kwargs['msg_seq'], 1)

    async def test_group_mention_and_dedup(self):
        message = self.message('<@!1905586446> /search 机器人')
        await asyncio.gather(self.bot.on_group_at_message_create(message), self.bot.on_group_at_message_create(message))
        self.retriever.search.assert_awaited_once_with('机器人', group_id='', history=[])
        self.assertEqual(message.reply.await_count, 1)

    async def test_real_sdk_reply_routing(self):
        api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={'id': 'r1'}),
                              post_group_message=AsyncMock(return_value={'id': 'r2'}))
        private = C2CMessage(api, 'e1', {'id': 'private-1', 'content': '如何使用',
                                        'author': {'user_openid': 'user-1'}})
        group = GroupMessage(api, 'e2', {'id': 'group-1', 'content': '<@1905586446> 如何使用',
                                         'group_openid': 'group-1', 'author': {'member_openid': 'member-1'}})
        await self.bot.on_c2c_message_create(private)
        await self.bot.on_group_at_message_create(group)
        self.assertEqual(api.post_c2c_message.call_args.kwargs['openid'], 'user-1')
        self.assertEqual(api.post_c2c_message.call_args.kwargs['msg_id'], 'private-1')
        self.assertEqual(api.post_group_message.call_args.kwargs['group_openid'], 'group-1')
        self.assertEqual(api.post_group_message.call_args.kwargs['msg_id'], 'group-1')

    async def test_help_no_hits_and_failure(self):
        message = self.message('/help')
        await self.bot.answer(message, 'c2c')
        self.retriever.search.assert_not_awaited()
        self.assertIn('直接发送问题', message.reply.call_args.kwargs['content'])
        self.retriever.search.return_value = {'results': []}
        message = self.message('不存在', 'm2')
        await self.bot.answer(message, 'c2c')
        self.assertIn('没有找到', message.reply.call_args.kwargs['content'])
        self.retriever.search.side_effect = asyncio.TimeoutError
        message = self.message('超时', 'm3')
        await self.bot.answer(message, 'c2c')
        self.assertIn('暂时不可用', message.reply.call_args.kwargs['content'])

    async def test_bounded_plain_text_and_persistent_claim(self):
        text = format_results({'results': [{'title': '@everyone <tag>', 'content': '长' * 10000, 'ordinal': 0}] * 10})
        self.assertLess(len(text), 1800)
        self.assertNotIn('@', text)
        self.assertNotIn('<tag>', text)
        self.assertTrue(self.seen.claim('persisted'))
        second = SeenMessages(Path(self.temp.name) / 'seen.db')
        self.assertFalse(second.claim('persisted'))
        second.conn.close()

    async def test_http_retrieval_contract(self):
        received = {}
        async def handler(request):
            received.update(await request.json())
            self.assertEqual(request.headers['Authorization'], 'Bearer read-test')
            return web.json_response({'results': [{'content': 'test'}]})
        app = web.Application()
        app.router.add_post('/answer', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            result = await Retriever(f'http://127.0.0.1:{port}/retrieve', 'read-test', 'kb-test').search('问题')
            self.assertEqual(received['kb_id'], 'kb-test')
            self.assertEqual(received['group_id'], '')
            self.assertEqual(received['history'], [])
            self.assertEqual(result['results'][0]['content'], 'test')
        finally:
            await runner.cleanup()

    async def test_group_without_mention_never_enters_pipeline(self):
        message = self.message('普通群消息', 'plain-group')
        await self.bot.on_group_message_create(message)
        await self.bot.answer(message, 'group')
        self.retriever.search.assert_not_awaited()
        message.reply.assert_not_awaited()
        self.assertTrue(self.seen.claim('group:plain-group'))

    def identified(self, content, id, user='user1', group='group1'):
        message = self.message(content, id)
        message.author = SimpleNamespace(user_openid=user, member_openid=user)
        message.group_openid = group
        return message

    async def test_history_delivery_order_and_conversation_isolation(self):
        self.retriever.search.return_value = {'answer': '总价100元。'}
        first = self.identified('kei多少钱', 'history1')
        second = self.identified('定金呢', 'history2')
        await asyncio.gather(self.bot.answer(first, 'c2c'), self.bot.answer(second, 'c2c'))
        history = self.retriever.search.call_args.kwargs['history']
        self.assertEqual(history, [{'role':'user','content':'kei多少钱'}, {'role':'assistant','content':'总价100元。'}])
        self.assertEqual(self.bot.conversations, {})
        for index, (kind, user, group) in enumerate([('c2c', 'user2', ''), ('group', 'user1', 'group1'), ('group', 'user1', 'group2')]):
            msg = self.identified('还有呢', f'isolated{index}', user, group)
            await self.bot.answer(msg, kind, mentioned=True)
            self.assertEqual(self.retriever.search.call_args.kwargs['history'], [])
        self.assertNotEqual(conversation_key(first, 'c2c', 'kb1'), conversation_key(first, 'c2c', 'kb2'))
        self.assertEqual(conversation_key(self.message('匿名'), 'c2c', 'kb1'), '')

    async def test_failed_delivery_not_saved_and_clear_command(self):
        msg = self.identified('不会发送成功', 'failed-send')
        msg.reply.side_effect = RuntimeError('send failed')
        await self.bot.answer(msg, 'c2c')
        key = conversation_key(msg, 'c2c', 'test')
        self.assertEqual(self.seen.history(key), [])
        self.seen.remember(key, '之前的问题', '之前的回答')
        clear = self.identified('/新对话', 'clear')
        count = self.retriever.search.await_count
        await self.bot.answer(clear, 'c2c')
        self.assertEqual(self.retriever.search.await_count, count)
        self.assertEqual(self.seen.history(key), [])

    async def test_history_expiry_budgets_and_restart(self):
        with patch('bot.time.time', return_value=1000):
            self.seen.remember('session', 'old question', 'old answer')
        with patch('bot.time.time', return_value=2700):
            self.seen.remember('session', 'recent question', 'recent answer')
        second = SeenMessages(Path(self.temp.name) / 'seen.db')
        with patch('bot.time.time', return_value=2800):
            history = second.history('session')
        second.conn.close()
        self.assertEqual(history, [{'role':'user','content':'recent question'}, {'role':'assistant','content':'recent answer'}])
        with patch('bot.time.time', return_value=4500):
            self.assertEqual(self.seen.history('session'), [])
        for i in range(12):
            self.seen.remember('session', str(i), 'a' * 1700)
        history = self.seen.history('session')
        self.assertLessEqual(len(history), 20)
        self.assertLessEqual(sum(len(m['content']) for m in history), 12000)
        self.assertEqual(history[-2]['content'], '11')

    async def test_model_text_and_trusted_mentions(self):
        data = {'answer': '请管理员确认 <qqbot-at-user id="evil-user" />', 'handoff': True,
                'mention_openids': ['admin123456', 'bad"/><x>']}
        group = format_reply(data, 'group')
        self.assertIn('<qqbot-at-user id="admin123456" />', group)
        self.assertNotIn('<qqbot-at-user id="evil-user" />', group)
        self.assertNotIn('bad"/><x>', group)
        self.assertNotIn('<qqbot-at-user', format_reply(data, 'c2c'))
        message = self.message('/身份', 'identity')
        message.group_openid = 'group123456'
        message.author = SimpleNamespace(member_openid='member123456')
        await self.bot.answer(message, 'group', mentioned=True)
        self.assertIn('member123456', message.reply.call_args.kwargs['content'])
        self.retriever.search.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
