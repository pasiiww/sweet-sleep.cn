import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from botpy.message import C2CMessage, GroupMessage
from bot import KnowledgeBot, Retriever, SeenMessages, format_results, format_reply, normalize


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
        self.retriever.search.assert_awaited_once_with('机器人怎么使用', group_id='')
        kwargs = message.reply.call_args.kwargs
        self.assertIn('机器人返回原文', kwargs['content'])
        self.assertIn('来源：演示', kwargs['content'])
        self.assertEqual(kwargs['msg_seq'], 1)

    async def test_group_mention_and_dedup(self):
        message = self.message('<@!1905586446> /search 机器人')
        await asyncio.gather(self.bot.on_group_at_message_create(message), self.bot.on_group_at_message_create(message))
        self.retriever.search.assert_awaited_once_with('机器人', group_id='')
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
            self.assertEqual(result['results'][0]['content'], 'test')
        finally:
            await runner.cleanup()

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
        await self.bot.answer(message, 'group')
        self.assertIn('member123456', message.reply.call_args.kwargs['content'])
        self.retriever.search.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
