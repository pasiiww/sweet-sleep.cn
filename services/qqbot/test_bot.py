import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from botpy.message import C2CMessage, GroupMessage
from bot import KnowledgeBot, Retriever, SeenMessages, clean_group_context, format_results, format_reply, normalize, normalize_model_text, conversation_key, sensitive_word_matches, sexual_harassment_matches, stable_drink_seed, creative_request, is_group_command


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.seen = SeenMessages(Path(self.temp.name) / 'seen.db')
        self.retriever = SimpleNamespace(kb_id='test', search=AsyncMock(return_value={'results': [
            {'title': '说明', 'content': '机器人返回原文。', 'source': '演示', 'ordinal': 0}]}),
            memory=AsyncMock(return_value={'ok': True, 'enabled': True, 'items': []}))
        self.bot = KnowledgeBot(self.retriever, self.seen)

    async def asyncTearDown(self):
        self.seen.conn.close()
        self.temp.cleanup()

    def message(self, content, id='m1'):
        return SimpleNamespace(id=id, content=content, reply=AsyncMock(return_value={'id': 'reply1'}))

    def test_creative_commands_parse_without_group_mention(self):
        self.assertEqual(creative_request('/俳句'), {'style': 'haiku', 'text': ''})
        self.assertEqual(creative_request('/俳句 +春夜微雨'), {'style': 'haiku', 'text': '春夜微雨'})
        self.assertEqual(creative_request('/对联 +山色入帘青'), {'style': 'couplet', 'text': '山色入帘青'})
        self.assertTrue(is_group_command('/俳句'))

    async def test_sticker_real_sdk_routing_and_fallback(self):
        api = SimpleNamespace(post_group_file=AsyncMock(return_value={'file_info':'media'}),
            post_c2c_file=AsyncMock(return_value={'file_info':'media'}),
            post_group_message=AsyncMock(return_value={'id':'r'}),post_c2c_message=AsyncMock(return_value={'id':'r'}))
        group=GroupMessage(api,'e',{'id':'m','group_openid':'g','author':{'member_openid':'u'}})
        private=C2CMessage(api,'e',{'id':'m2','author':{'user_openid':'u2'}})
        for kind,msg in [('group',group),('c2c',private)]:
            trace={'sticker':{'name':'开心','url':'https://example.com/a.jpg'}}
            await self.bot.send_answer(msg,kind,'好呀',trace)
            self.assertEqual(trace['sticker_delivery']['status'],'sent')
        self.assertFalse(api.post_group_file.call_args.kwargs['srv_send_msg'])
        self.assertEqual(api.post_c2c_file.call_args.kwargs['openid'],'u2')
        self.assertEqual(api.post_group_message.call_args.kwargs['msg_type'],7)
        self.assertEqual(api.post_group_message.call_args.kwargs['media'],{'file_info':'media'})
        self.assertEqual(api.post_group_message.call_args.kwargs['msg_id'],'m')
        api.post_group_file.side_effect=RuntimeError('upload failed')
        await self.bot.send_answer(group,'group','保留文字',trace)
        self.assertEqual(trace['sticker_delivery']['status'],'failed')
        self.assertEqual(api.post_group_message.call_args.kwargs['content'],'保留文字')
        self.assertEqual(api.post_group_message.call_args.kwargs['msg_type'],0)
        api.post_group_file.side_effect=None
        api.post_group_message.side_effect=[RuntimeError('media failed'),{'id':'r'}]
        await self.bot.send_answer(group,'group','仍然保留文字',trace)
        self.assertEqual(api.post_group_message.call_args.kwargs['content'],'仍然保留文字')
        self.assertEqual(trace['sticker_delivery']['status'],'failed')

    async def test_sticker_state_persists_without_blocking_selection(self):
        api=SimpleNamespace(post_group_file=AsyncMock(return_value={'file_info':'media'}))
        message=self.message('价格');message._api=api;message.group_openid='g'
        def trace():return {'sticker':{'name':'开心','url':'https://example.com/a.jpg'}}
        await self.bot.send_answer(message,'group','回答',trace(),'s')
        self.assertTrue(self.seen.last_sticker_sent('s'))
        other=SeenMessages(Path(self.temp.name)/'seen.db')
        self.assertTrue(other.last_sticker_sent('s'));other.conn.close()
        await self.bot.send_answer(message,'group','回答',trace(),'s')
        self.assertEqual(api.post_group_file.await_count,2)
        self.assertEqual(message.reply.call_args.kwargs['msg_type'],7)
        self.assertTrue(self.seen.last_sticker_sent('s'))
        await self.bot.send_answer(message,'group','回答',trace(),'s')
        self.assertEqual(api.post_group_file.await_count,3)
        self.assertFalse(self.seen.last_sticker_sent('other-user'))
        self.seen.clear_history('s');self.assertFalse(self.seen.last_sticker_sent('s'))

    async def test_sticker_only_history_and_upload_failure(self):
        api=SimpleNamespace(post_c2c_file=AsyncMock(return_value={'file_info':'media'}),post_c2c_message=AsyncMock(return_value={'id':'r'}))
        message=C2CMessage(api,'e',{'id':'pure-image','content':'哈哈','author':{'user_openid':'user1'}})
        self.retriever.search.return_value={'answer':'','mode':'model','sticker':{'name':'开心','url':'https://example.com/a.jpg'}}
        await self.bot.on_c2c_message_create(message)
        self.assertEqual(api.post_c2c_message.call_args.kwargs['msg_type'],7)
        self.assertIsNone(api.post_c2c_message.call_args.kwargs['content'])
        session=conversation_key(message,'c2c','test')
        self.assertIn('[开心]',self.seen.history(session)[-1]['content'])
        message.id='failed-image';api.post_c2c_file.side_effect=RuntimeError('upload failed')
        self.retriever.search.return_value={'answer':'','mode':'model','sticker':{'name':'开心','url':'https://example.com/a.jpg'}}
        await self.bot.on_c2c_message_create(message)
        self.assertEqual(api.post_c2c_message.call_args.kwargs['msg_type'],0)
        self.assertIn('没发出去',api.post_c2c_message.call_args.kwargs['content'])
        self.assertIn('没发出去',self.seen.history(session)[-1]['content'])

    async def test_private_message_retrieval(self):
        message = self.message('/检索 机器人怎么使用')
        await self.bot.on_c2c_message_create(message)
        self.retriever.search.assert_awaited_once_with('机器人怎么使用', group_id='', history=[], trace_meta={'origin':'qq_private','user_id':'','session_id':''})
        kwargs = message.reply.call_args.kwargs
        self.assertIn('机器人返回原文', kwargs['content'])
        self.assertIn('来源：演示', kwargs['content'])
        self.assertEqual(kwargs['msg_seq'], 1)

    async def test_group_mention_and_dedup(self):
        message = self.message('<@!1905586446> /search 机器人')
        await asyncio.gather(self.bot.on_group_at_message_create(message), self.bot.on_group_at_message_create(message))
        self.retriever.search.assert_awaited_once_with('机器人', group_id='', history=[], trace_meta={
            'origin':'qq_group','user_id':'','session_id':'','current_member_text':'机器人'})
        self.assertEqual(message.reply.await_count, 1)

    async def test_mentioned_member_is_rendered_as_bounded_name_for_model(self):
        message = self.identified('请看看 <@target-01> 的发言', 'mention-model')
        message.mentions = [SimpleNamespace(id='target-01', username='超长昵称一二三四五六七八九十')]
        self.assertEqual(normalize_model_text(message.content, message), '请看看 @超长昵称一二三四五六七八 的发言')
        await self.bot.answer(message, 'group', mentioned=True)
        self.assertEqual(self.retriever.search.call_args.args[0], '请看看 @超长昵称一二三四五六七八 的发言')
        self.assertEqual(self.retriever.search.call_args.kwargs['trace_meta']['mentioned_members'], [
            {'openid': 'target-01', 'name': '超长昵称一二三四五六七八'}])
        stored = self.seen.conn.execute(
            'SELECT content FROM group_context_messages WHERE message_id=?', ('mention-model',)).fetchone()[0]
        self.assertIn('@超长昵称一二三四五六七八', stored)
        self.assertNotIn('target-01', self.retriever.search.call_args.args[0])

    async def test_mentioned_member_prefers_managed_first_nickname(self):
        first = self.identified('你好', 'target-first-name', user='target-01')
        first.sweet_sender_name = '最早记录昵称'
        self.seen.observe_group_message(first)
        message = self.identified('请看看 <@target-01> 的问题', 'mention-managed-name')
        message.mentions = [SimpleNamespace(id='target-01', username='平台当前昵称')]
        await self.bot.answer(message, 'group', mentioned=True)
        self.assertEqual(self.retriever.search.call_args.args[0], '请看看 @最早记录昵称 的问题')

    def test_mention_openid_is_excluded_from_sensitive_word_matching(self):
        self.assertEqual(sensitive_word_matches('提问 <@blockedword-user> 结束', ['blockedword']), [])

    async def test_mention_openid_does_not_trigger_group_moderation(self):
        self.retriever.moderation_settings = AsyncMock(return_value={
            'sensitive_words': ['blockedword'], 'harassment_warning_enabled': True})
        self.bot.recall_group_message = AsyncMock()
        message = self.identified('提问 <@blockedword-user> 结束', 'mention-moderation')
        self.assertFalse(await self.bot.moderate_group_message(message, bot_mentioned=True))
        self.bot.recall_group_message.assert_not_awaited()
        self.assertEqual(self.seen.conn.execute(
            'SELECT count(*) FROM moderation_recall_outbox').fetchone()[0], 0)

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
        self.assertIn('社团娘', message.reply.call_args.kwargs['content'])
        self.assertIn('@ 我提问', message.reply.call_args.kwargs['content'])
        self.retriever.search.return_value = {'results': []}
        message = self.message('不存在', 'm2')
        await self.bot.answer(message, 'c2c')
        self.assertIn('没有找到', message.reply.call_args.kwargs['content'])
        self.retriever.search.side_effect = asyncio.TimeoutError
        message = self.message('超时', 'm3')
        await self.bot.answer(message, 'c2c')
        self.assertIn('暂时不可用', message.reply.call_args.kwargs['content'])

    async def test_today_drink_command_works_without_mention_and_skips_rag(self):
        self.retriever.drink_menu = AsyncMock(return_value=[
            {'brand': '蜜雪冰城', 'product': '冰鲜柠檬水', 'temperature': 'cold'},
            {'brand': '1点点', 'product': '波霸奶茶', 'temperature': 'both'},
            {'brand': '1点点', 'product': '冰激凌红茶', 'temperature': 'hot'}])
        self.retriever.drink_weather = AsyncMock(return_value={
            'available': True, 'city': '杭州', 'today_max': 29, 'yesterday_max': 24,
            'temperature_preference': 'cold'})
        message = self.identified('/今天喝什么', 'drink-pick')
        await self.bot.on_group_message_create(message)
        self.retriever.drink_menu.assert_awaited_once()
        self.retriever.drink_weather.assert_awaited_once()
        self.retriever.search.assert_not_awaited()
        reply = message.reply.call_args.kwargs['content']
        self.assertIn('杭州今天预报最高 29℃', reply)
        self.assertIn('冰鲜柠檬水', reply)
        self.assertIn('波霸奶茶', reply)
        self.assertNotIn('冰激凌红茶', reply)
        self.assertIn('1.', reply)
        self.assertIn('2.', reply)
        self.assertTrue(reply.rstrip().endswith(('（建议点冷的）', '（建议点冷热均可）')))
        self.assertNotIn('天气服务由和风天气驱动', reply)
        self.assertNotIn('以门店实际在售为准', reply)

    async def test_today_drink_command_handles_empty_menu(self):
        self.retriever.drink_menu = AsyncMock(return_value=[])
        message = self.identified('/今天喝什么', 'drink-empty')
        await self.bot.answer(message, 'group')
        self.assertIn('清单还空着', message.reply.call_args.kwargs['content'])
        self.retriever.search.assert_not_awaited()

    async def test_today_drink_command_keeps_supplement_note_out_of_reply(self):
        self.retriever.drink_menu = AsyncMock(return_value=[
            {'brand': '品牌甲', 'product': '冷饮', 'temperature': 'cold'},
            {'brand': '品牌乙', 'product': '热饮', 'temperature': 'hot'}])
        self.retriever.drink_weather = AsyncMock(return_value={
            'available': True, 'city': '杭州', 'today_max': 29, 'yesterday_max': None,
            'temperature_preference': 'cold'})
        message = self.identified('/今天喝什么', 'drink-supplement')
        await self.bot.answer(message, 'group')
        reply = message.reply.call_args.kwargs['content']
        self.assertIn('冷饮', reply)
        self.assertIn('热饮', reply)
        self.assertIn('与天气建议不同', reply)
        self.assertNotIn('不足两项', reply)
        self.assertNotIn('管理员可以到后台', reply)
        self.assertRegex(reply.rstrip().splitlines()[-1], r'^2\..+）$')

    async def test_today_drink_command_is_stable_per_user_and_day(self):
        self.retriever.drink_menu = AsyncMock(return_value=[
            {'brand': '品牌', 'product': f'饮品{index}', 'temperature': 'both'}
            for index in range(12)])
        self.retriever.drink_weather = AsyncMock(return_value=None)
        first = self.identified('/今天喝什么', 'drink-stable-1', user='same-member')
        second = self.identified('/今天喝什么', 'drink-stable-2', user='same-member')
        await self.bot.answer(first, 'group')
        await self.bot.answer(second, 'group')
        first_picks = [line for line in first.reply.call_args.kwargs['content'].splitlines()
                       if line.startswith(('1.', '2.'))]
        second_picks = [line for line in second.reply.call_args.kwargs['content'].splitlines()
                        if line.startswith(('1.', '2.'))]
        self.assertEqual(first_picks, second_picks)

    async def test_today_drink_command_works_in_private_menu_and_is_stable_by_user(self):
        self.retriever.drink_menu = AsyncMock(return_value=[
            {'brand': '品牌', 'product': f'饮品{index}', 'temperature': 'both'}
            for index in range(12)])
        self.retriever.drink_weather = AsyncMock(return_value=None)
        first = self.message('/今天喝什么', 'drink-private-1')
        first.author = SimpleNamespace(user_openid='same-private-user')
        second = self.message('/今天喝什么', 'drink-private-2')
        second.author = SimpleNamespace(user_openid='same-private-user')
        await self.bot.answer(first, 'c2c')
        await self.bot.answer(second, 'c2c')
        first_picks = [line for line in first.reply.call_args.kwargs['content'].splitlines()
                       if line.startswith(('1.', '2.'))]
        second_picks = [line for line in second.reply.call_args.kwargs['content'].splitlines()
                        if line.startswith(('1.', '2.'))]
        self.assertEqual(first_picks, second_picks)
        self.assertIn('挑两杯', first.reply.call_args.kwargs['content'])
        self.retriever.search.assert_not_awaited()

    def test_drink_seed_uses_openid_and_beijing_date(self):
        today = '2026-09-27'
        seed = stable_drink_seed('member-1', today)
        self.assertEqual(seed, stable_drink_seed('member-1', today))
        self.assertNotEqual(seed, stable_drink_seed('member-2', today))
        self.assertNotEqual(seed, stable_drink_seed('member-1', '2026-09-28'))
        self.assertIsNone(stable_drink_seed('', today))

    async def test_group_member_join_gets_one_event_reply(self):
        self.bot.api.post_group_message = AsyncMock(return_value={'id': 'welcome'})
        self.retriever.group_welcome = AsyncMock(return_value='可编辑的新人欢迎词\n请先看群公告')
        event = SimpleNamespace(event_id='join-event-1', group_openid='group-1',
                                member_openid='member-1')
        await self.bot.on_group_member_add(event)
        await self.bot.on_group_member_add(event)
        self.bot.api.post_group_message.assert_awaited_once()
        kwargs = self.bot.api.post_group_message.call_args.kwargs
        self.assertEqual(kwargs['group_openid'], 'group-1')
        self.assertEqual(kwargs['event_id'], 'join-event-1')
        self.assertEqual(kwargs['content'], '可编辑的新人欢迎词\n请先看群公告')
        self.retriever.group_welcome.assert_awaited_once_with()

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
        app.router.add_post('/agent/answer', handler)
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

    async def test_http_moderation_report_sends_candidates_with_trigger_count(self):
        received = {}
        async def handler(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer moderation-test')
            received.update(await request.json())
            return web.json_response({'recorded': True})
        app = web.Application()
        app.router.add_post('/moderation-recalls', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            retriever = Retriever(f'http://127.0.0.1:{port}/retrieve', 'read-test', 'kb-test')
            retriever.moderation_token = 'moderation-test'
            trace_meta = {'kb_id': 'kb-test', 'group_id': 'group1', 'user_id': 'user1',
                          'message_id': 'message1', 'content': '命中消息'}
            result = await retriever.report_moderation_recall('a' * 64, ['中出'], ['新的短语'], trace_meta)
            self.assertTrue(result)
            self.assertEqual(received, {'event_hash':'a' * 64, 'terms':['中出'],
                                        'candidates':['新的短语'], 'trace_meta':trace_meta})
        finally:
            await runner.cleanup()

    async def test_moderation_outbox_keeps_trace_context_across_restart(self):
        trace_meta = {'kb_id': 'test', 'group_id': 'group1', 'user_id': 'user1',
                      'message_id': 'message1', 'content': '命中原文'}
        self.seen.record_moderation_recall('b' * 64, ['cbz'], ['候选'], trace_meta)
        other = SeenMessages(Path(self.temp.name) / 'seen.db')
        try:
            self.assertEqual(other.pending_moderation_recalls(),
                             [('b' * 64, ['cbz'], ['候选'], trace_meta)])
        finally:
            other.conn.close()

    async def test_group_welcome_uses_read_only_configuration_endpoint(self):
        async def handler(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer read-test')
            return web.json_response({'welcome': '后台设置的欢迎词'})
        app = web.Application()
        app.router.add_get('/group-welcome', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            retriever = Retriever(f'http://127.0.0.1:{port}/retrieve', 'read-test', 'kb-test')
            self.assertEqual(await retriever.group_welcome(), '后台设置的欢迎词')
        finally:
            await runner.cleanup()

    async def test_group_without_mention_never_enters_pipeline(self):
        message = self.message('普通群消息', 'plain-group')
        await self.bot.on_group_message_create(message)
        await self.bot.answer(message, 'group')
        self.retriever.search.assert_not_awaited()
        message.reply.assert_not_awaited()
        self.assertTrue(self.seen.claim('group:plain-group'))

    async def test_sensitive_word_recall_and_count_are_idempotent(self):
        self.retriever.moderation_words = AsyncMock(return_value=['cbz', 'jb'])
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        request = AsyncMock(return_value={})
        self.bot.api._http.request = request
        summary_observe = Mock()
        learner_observe = Mock()
        self.bot.summaries.observe = summary_observe
        self.bot.learner = SimpleNamespace(observe=learner_observe)
        message = self.identified(
            'This is CBZ https://gchat.qpic.cn/media.jpg?rkey=CAJb7f0a', 'sensitive-1')
        await asyncio.gather(self.bot.on_group_at_message_create(message),
                             self.bot.on_group_message_create(message))
        request.assert_awaited_once()
        route = request.call_args.args[0]
        self.assertEqual(route.method, 'DELETE')
        self.assertIn('/v2/groups/group1/messages/sensitive-1', route.url)
        self.retriever.search.assert_not_awaited()
        self.retriever.report_moderation_recall.assert_awaited_once()
        event_hash, terms, candidates, trace_meta = self.retriever.report_moderation_recall.call_args.args
        self.assertRegex(event_hash, r'^[a-f0-9]{64}$')
        self.assertEqual(terms, ['cbz'])
        self.assertEqual(candidates, [])
        self.assertEqual(trace_meta['group_id'], 'group1')
        self.assertEqual(trace_meta['user_id'], 'user1')
        self.assertEqual(trace_meta['message_id'], 'sensitive-1')
        self.assertNotIn('https://', trace_meta['content'])
        self.assertEqual(trace_meta['content'], 'This is CBZ  ')
        self.assertEqual(self.seen.pending_moderation_recalls(), [])
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM group_context_messages').fetchone()[0], 0)
        summary_observe.assert_not_called()
        learner_observe.assert_not_called()

    async def test_sensitive_word_recall_failure_is_still_counted_before_downstream_processing(self):
        self.retriever.moderation_words = AsyncMock(return_value=['cbz'])
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        self.bot.api._http.request = AsyncMock(side_effect=RuntimeError('recall unavailable'))
        message = self.identified('cbz', 'sensitive-failed')
        await self.bot.on_group_message_create(message)
        self.retriever.report_moderation_recall.assert_awaited_once()
        self.assertEqual(self.retriever.report_moderation_recall.call_args.args[1], ['cbz'])
        self.retriever.search.assert_not_awaited()
        self.assertEqual(self.seen.pending_moderation_recalls(), [])

    async def test_new_harassment_phrase_becomes_review_candidate_before_recall(self):
        self.retriever.moderation_words = AsyncMock(return_value=['中出'])
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        self.bot.api._http.request = AsyncMock(side_effect=RuntimeError('recall unavailable'))
        message = self.identified('<@!botid> 跟你做爱', 'sexual-candidate')
        await self.bot.on_group_at_message_create(message)
        self.retriever.report_moderation_recall.assert_awaited_once()
        _, terms, candidates, trace_meta = self.retriever.report_moderation_recall.call_args.args
        self.assertEqual(terms, ['跟你做爱'])
        self.assertEqual(candidates, ['跟你做爱'])
        self.assertEqual(trace_meta['content'], '@群友 跟你做爱')
        self.retriever.search.assert_not_awaited()

    async def test_mentioned_sexual_harassment_is_recalled_and_warned_after_three_strikes(self):
        self.retriever.moderation_words = AsyncMock(return_value=['cbz', '🐍米青', '中出'])
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        self.bot.api._http.request = AsyncMock(return_value={})
        messages=[]
        for index in range(3):
            message = self.identified(f'<@!botid> 中出{index}', f'sexual-{index}')
            messages.append(message)
            await self.bot.on_group_at_message_create(message)
            self.assertEqual(self.bot.api._http.request.await_count, index + 1)
            if index < 2:
                message.reply.assert_not_awaited()
            else:
                message.reply.assert_awaited_once()
                self.assertIn('这是一次提醒', message.reply.call_args.kwargs['content'])
                self.assertEqual(message.reply.call_args.kwargs['msg_type'], 0)
        calls=self.bot.api._http.request.await_args_list
        self.assertEqual([call.args[0].method for call in calls], ['DELETE','DELETE','DELETE'])
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM harassment_strikes').fetchone()[0],0)
        self.retriever.search.assert_not_awaited()

    async def test_configured_auto_mute_runs_at_threshold_and_clears_strikes(self):
        self.retriever.moderation_settings = AsyncMock(return_value={
            'sensitive_words': [], 'harassment_warning_enabled': True,
            'harassment_mute_enabled': True, 'harassment_mute_threshold': 2,
            'harassment_mute_duration_minutes': 7,
        })
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        self.bot.api._http.request = AsyncMock(return_value={})
        self.bot.mute_group_member = AsyncMock(return_value={'muted': True})
        messages = [self.identified(f'<@!botid> 中出{index}', f'auto-mute-{index}') for index in range(2)]

        for message in messages:
            await self.bot.on_group_at_message_create(message)
        self.bot.mute_group_member.assert_awaited_once_with('group1', 'user1', 7)
        for message in messages:
            message.reply.assert_not_awaited()
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM harassment_strikes').fetchone()[0], 0)
        # The matching messages are still recalled and counted before attempting the mute.
        self.assertEqual(self.bot.api._http.request.await_count, 2)

    async def test_configured_sensitive_word_hits_count_toward_auto_mute_threshold(self):
        self.retriever.moderation_settings = AsyncMock(return_value={
            'sensitive_words': ['cbz'], 'harassment_warning_enabled': True,
            'harassment_mute_enabled': True, 'harassment_mute_threshold': 2,
            'harassment_mute_duration_minutes': 7,
        })
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        self.bot.api._http.request = AsyncMock(return_value={})
        self.bot.mute_group_member = AsyncMock(return_value={'muted': True})
        messages = [self.identified(f'cbz mention {index}', f'auto-mute-sensitive-{index}') for index in range(2)]
        for message in messages:
            await self.bot.on_group_message_create(message)
        self.bot.mute_group_member.assert_awaited_once_with('group1', 'user1', 7)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM harassment_strikes').fetchone()[0], 0)

    async def test_auto_mute_failure_falls_back_to_configured_warning(self):
        self.retriever.moderation_settings = AsyncMock(return_value={
            'sensitive_words': [], 'harassment_warning_enabled': True,
            'harassment_mute_enabled': True, 'harassment_mute_threshold': 2,
            'harassment_mute_duration_minutes': 7,
        })
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        self.bot.api._http.request = AsyncMock(return_value={})
        self.bot.mute_group_member = AsyncMock(side_effect=RuntimeError('permission denied'))
        messages = [self.identified(f'<@!botid> 中出{index}', f'auto-mute-failed-{index}') for index in range(2)]
        for message in messages:
            await self.bot.on_group_at_message_create(message)
        messages[0].reply.assert_not_awaited()
        messages[1].reply.assert_awaited_once()
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM harassment_strikes').fetchone()[0], 0)

    async def test_mute_group_member_sends_expiring_member_rule(self):
        request = AsyncMock(return_value={})
        self.bot.api._http = SimpleNamespace(request=request)
        result = await self.bot.mute_group_member('group1', 'user1', 4)
        self.assertEqual(result, {'muted': True, 'duration_minutes': 4})
        route = request.call_args.args[0]
        self.assertEqual(route.method, 'POST')
        self.assertIn('/v2/groups/group1/restrict_chat_setting', route.url)
        payload = request.call_args.kwargs['json']['members'][0]
        self.assertEqual(payload['op'], 'add')
        self.assertEqual(payload['member_openid'], 'user1')
        expires = datetime.fromisoformat(payload['mute_expire_at'].replace('Z', '+00:00'))
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(remaining, 3 * 60)
        self.assertLessEqual(remaining, 4 * 60)

    def test_harassment_strikes_use_a_rolling_ten_minute_window(self):
        identity = 'same-group-member'
        self.assertEqual(self.seen.record_harassment_strike(identity, 'old', at=1000), 1)
        self.assertEqual(self.seen.record_harassment_strike(identity, 'recent', at=1500), 2)
        self.assertEqual(self.seen.record_harassment_strike(identity, 'new', at=1601), 2)

    async def test_harassment_warning_can_be_disabled_without_disabling_recall_or_counting(self):
        self.retriever.moderation_settings = AsyncMock(return_value={
            'sensitive_words': [], 'harassment_warning_enabled': False})
        self.retriever.report_moderation_recall = AsyncMock(return_value=True)
        self.bot.api._http.request = AsyncMock(return_value={})
        messages = [self.identified(f'<@!botid> 中出{index}', f'warning-disabled-{index}') for index in range(3)]
        for message in messages:
            await self.bot.on_group_at_message_create(message)
            message.reply.assert_not_awaited()
        self.assertEqual(self.bot.api._http.request.await_count, 3)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM harassment_strikes').fetchone()[0], 3)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM harassment_warning_attempts').fetchone()[0], 0)

    async def test_sexual_terms_without_bot_mention_do_not_trigger_harassment_actions(self):
        self.retriever.moderation_words = AsyncMock(return_value=[])
        self.bot.api._http.request = AsyncMock(return_value={})
        message=self.identified('中出', 'sexual-not-targeted')
        await self.bot.on_group_message_create(message)
        self.bot.api._http.request.assert_not_awaited()
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM harassment_strikes').fetchone()[0],0)

    async def test_local_mcp_moderation_endpoint_requires_token_and_calls_qq(self):
        self.bot.api._http.request = AsyncMock(return_value={})
        self.bot.api.post_group_message = AsyncMock(return_value={'id':'warning'})
        self.retriever.moderation_settings = AsyncMock(return_value={
            'sensitive_words': [], 'harassment_warning_enabled': False})
        with patch.dict('os.environ', {'KB_MODERATION_TOKEN':'moderation-test'}):
            client=TestClient(TestServer(self.bot.moderation_control_application()))
            await client.start_server()
            try:
                unauthorized=await client.post('/mcp/recall',json={'group_id':'group1','message_id':'message.1'})
                self.assertEqual(unauthorized.status,401)
                response=await client.post('/mcp/recall',headers={'Authorization':'Bearer moderation-test'},
                                           json={'group_id':'group1','message_id':'message.1'})
                self.assertEqual(response.status,200)
                self.assertTrue((await response.json())['recalled'])
                route=self.bot.api._http.request.call_args.args[0]
                self.assertEqual(route.method,'DELETE')
                self.assertIn('/v2/groups/group1/messages/message.1',route.url)
                disabled=await client.post('/mcp/warn',headers={'Authorization':'Bearer moderation-test'},
                                           json={'group_id':'group1','message_id':'message.2'})
                self.assertEqual(disabled.status,409)
                self.bot.api.post_group_message.assert_not_awaited()
                self.retriever.moderation_settings.return_value['harassment_warning_enabled']=True
                warning=await client.post('/mcp/warn',headers={'Authorization':'Bearer moderation-test'},
                                          json={'group_id':'group1','message_id':'message.2'})
                self.assertEqual(warning.status,200)
                self.assertTrue((await warning.json())['sent'])
                self.bot.api.post_group_message.assert_awaited_once_with(
                    group_openid='group1', msg_id='message.2',
                    content='请不要对机器人进行性骚扰哦～这是一次提醒，请友善交流。',
                    msg_type=0, msg_seq=1)
                removed=await client.post('/mcp/mute',headers={'Authorization':'Bearer moderation-test'},
                                          json={'group_id':'group1','member_id':'user1','duration_minutes':10})
                self.assertEqual(removed.status,404)
            finally:
                await client.close()

    def test_sexual_harassment_detector_uses_specific_phrases(self):
        self.assertEqual(sexual_harassment_matches('想 中出 你'), ['中出'])
        self.assertEqual(sexual_harassment_matches('想 * 中 ， 出 你'), ['中出'])
        self.assertEqual(sexual_harassment_matches('CBZ 攻略'), [])
        self.assertEqual(sexual_harassment_matches('无关内容 https://example.test/?token=jb'), [])

    def test_sensitive_words_use_case_insensitive_literal_substrings(self):
        self.assertEqual(sensitive_word_matches('contains CBZ here', ['cbz', 'other']), ['cbz'])
        self.assertEqual(sensitive_word_matches('🐍米青和中出', ['🐍米青', '中出']), ['🐍米青', '中出'])
        self.assertEqual(sensitive_word_matches('unrelated', ['cbz']), [])

    def test_sensitive_word_matching_removes_symbols_and_whitespace(self):
        self.assertEqual(sensitive_word_matches('C.B - Z', ['cbz']), ['cbz'])
        self.assertEqual(sensitive_word_matches('中， 出！', ['中出']), ['中出'])
        self.assertEqual(sensitive_word_matches('🐍 米 青', ['🐍米青']), ['🐍米青'])
        self.assertEqual(sensitive_word_matches('🐍.米青', ['🐍米青']), ['🐍米青'])
        self.assertEqual(sensitive_word_matches('🐍🔥. 米 青', ['🐍米青']), [])
        self.assertEqual(sensitive_word_matches('C🐍B.Z', ['cbz']), ['cbz'])
        self.assertEqual(sensitive_word_matches('一脸懵', ['🐍一脸']), [])
        self.assertEqual(sensitive_word_matches('图片.jpg https://example.test/?rkey=CAJb7f0a', ['jb']), [])
        self.assertEqual(sensitive_word_matches('文本里有 J-B', ['jb']), ['jb'])

    def test_sensitive_word_matches_generated_alternatives_as_one_configured_rule(self):
        pattern = '(蛇/🐍)(精/米青)'
        expansions = [['蛇精', '蛇米青', '🐍精', '🐍米青']]
        self.assertEqual(sensitive_word_matches('这张图里有🐍米青', [pattern], expansions), [pattern])
        self.assertEqual(sensitive_word_matches('普通聊天', [pattern], expansions), [])

    def test_parenthesized_variants_do_not_match_laser_ticket(self):
        pattern = r'(（/\()(插/射/捅)(入/)(）/\))'
        # Expanded literals include optional "入" and both widths of parentheses.
        variants = ['（插入）', '(插)', '（射入)', '(射)', '(捅入）', '（捅）']
        for text in ('只有吧唧和镭射票', '发射', '插入表格', '射', '（普通话）射', '射）', '（射'):
            with self.subTest(text=text):
                self.assertEqual(sensitive_word_matches(text, [pattern], [variants]), [])
        for text in ('这是（射）', '(射入)', '（ 射 入 ）', '（插）', '(捅入)', '（捅)'):
            with self.subTest(text=text):
                self.assertEqual(sensitive_word_matches(text, [pattern], [variants]), [pattern])

    def test_symbol_literals_preserve_only_matching_symbol_and_plain_variants_normalize(self):
        self.assertEqual(sensitive_word_matches('C-BZ', ['c+bz']), [])
        self.assertEqual(sensitive_word_matches('C*+B🐍Z', ['c+bz']), ['c+bz'])
        self.assertEqual(sensitive_word_matches('Ｃ＋ＢＺ', ['c+bz']), ['c+bz'])
        self.assertEqual(sensitive_word_matches('CBZ', ['c.bz']), [])
        self.assertEqual(sensitive_word_matches('C . B Z', ['c.bz']), ['c.bz'])
        self.assertEqual(sensitive_word_matches('❤️有词', ['❤️.有词']), ['❤️.有词'])
        self.assertEqual(sensitive_word_matches('👩‍💻有词', ['👩‍💻有词']), ['👩‍💻有词'])
        pattern='(CBZ/\\(射\\))'
        for text in ('C.B Z', '（射）'):
            self.assertEqual(sensitive_word_matches(text, [pattern], [['CBZ', '(射)']]), [pattern])
        self.assertEqual(sensitive_word_matches('镭射票', [pattern], [['CBZ', '(射)']]), [])
        self.assertEqual(sexual_harassment_matches('🐍🔥米青'), [])

    def test_urls_are_excluded_from_both_matching_paths_without_joining_text(self):
        words = ['cbz', '(射)', '🐍米青']
        for url in ('https://example.test/CBZ/(射)/🐍米青', 'HTTP://example.test/?token=cbz(射)',
                    'www.example.test/cbz/(射)', 'ftp://example.test/cbz/(射)',
                    '//example.test/cbz/(射)', '//cbz.test',
                    'ｈｔｔｐｓ：／／example.test/cbz/（射）'):
            with self.subTest(url=url):
                self.assertEqual(sensitive_word_matches('普通聊天 ' + url, words), [])
                self.assertEqual(sensitive_word_matches(url + ' （射）', words), ['(射)'])
                self.assertEqual(sexual_harassment_matches(url + '/射你'), [])
        self.assertEqual(sensitive_word_matches('c https://example.test/link bz', words), [])
        self.assertEqual(sensitive_word_matches('( https://example.test/link 射)', words), [])

    async def test_laser_ticket_message_does_not_recall_or_record_strike(self):
        pattern = r'(（/\()(插/射/捅)(入/)(）/\))'
        self.retriever.moderation_settings = AsyncMock(return_value={
            'sensitive_words': [pattern], 'sensitive_word_expansions': [['(射)', '(射入)']]})
        self.bot.recall_group_message = AsyncMock()
        with patch.object(self.seen, 'record_moderation_recall') as record, \
             patch.object(self.seen, 'record_harassment_strike') as strike:
            blocked = await self.bot.moderate_group_message(self.identified('只有吧唧和镭射票', 'laser-ticket'), bot_mentioned=True)
        self.assertFalse(blocked)
        self.bot.recall_group_message.assert_not_awaited()
        record.assert_not_called()
        strike.assert_not_called()

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

    async def test_group_uses_shared_recent_ten_and_records_bot_replies(self):
        for index in range(12):
            old = self.identified(f'群消息{index}', f'old-{index}', user='member'+str(index%2))
            if index == 11:
                old.sweet_sender_name = '昵称超出十二字的群友测试'
            self.seen.observe_group_message(old)
        current = self.identified('<@!1905586446> 这件事呢', 'group-current')
        await self.bot.answer(current, 'group', mentioned=True)
        call = self.retriever.search.call_args
        self.assertEqual(call.kwargs['history'], [])
        group_context = call.kwargs['group_context']
        self.assertEqual(len(group_context), 10)
        self.assertIn('群消息11', group_context[-1]['content'])
        self.assertIn('昵称超出十二字的', group_context[-1]['content'])
        self.assertNotIn('群消息0', str(group_context))
        self.assertEqual(self.bot.conversations, {})
        next_message = self.identified('<@!1905586446> 接着说', 'group-next')
        await self.bot.answer(next_message, 'group', mentioned=True)
        next_context = self.retriever.search.call_args.kwargs['group_context']
        self.assertTrue(any(row['role'] == 'assistant' and '机器人返回原文' in row['content'] for row in next_context))

    async def test_mentioned_image_is_sent_ephemerally_to_model(self):
        data_url = 'data:image/png;base64,ephemeral-input'
        current = self.identified('<@!botid> 这张图里是什么', 'image-current')
        current.attachments = [{'content_type': 'image/png', 'url': 'https://gchat.qpic.cn/current'}]
        quoted = self.identified('<@!botid> 帮我看看', 'image-quoted')
        quoted.sweet_quoted_images = [{'content_type': 'image/jpeg', 'url': 'https://gchat.qpic.cn/quoted'}]

        with patch('bot.read_image_data_url', new=AsyncMock(return_value=data_url)) as read_image:
            await self.bot.answer(current, 'group', mentioned=True)
            self.assertEqual(self.retriever.search.call_args.kwargs['image_data_urls'], [data_url])
            self.assertEqual(read_image.await_args.args[0], 'https://gchat.qpic.cn/current')
            await self.bot.answer(quoted, 'group', mentioned=True)
            self.assertEqual(self.retriever.search.call_args.kwargs['image_data_urls'], [data_url])
            self.assertEqual(read_image.await_args.args[0], 'https://gchat.qpic.cn/quoted')

        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM image_occurrences').fetchone()[0], 0)
        for (content,) in self.seen.conn.execute('SELECT content FROM group_context_messages'):
            self.assertNotIn(data_url, content)

    async def test_group_memory_identity_keeps_first_nickname_after_rename(self):
        first = self.identified('普通发言', 'identity-first', user='member-one')
        first.sweet_sender_name = '落落最初昵称'
        self.seen.observe_group_message(first)
        renamed = self.identified('之后的发言', 'identity-renamed', user='member-one')
        renamed.sweet_sender_name = '后来改掉的昵称'
        self.seen.observe_group_message(renamed)
        self.assertEqual(self.seen.first_member_name('group1', 'member-one'), '落落最初昵称')

        query = self.identified('我是落落', 'identity-query', user='member-one')
        query.sweet_sender_name = '现在昵称'
        await self.bot.answer(query, 'group', mentioned=True)
        self.assertEqual(self.retriever.search.call_args.kwargs['trace_meta']['member_name'], '落落最初昵称')

        restored = SeenMessages(Path(self.temp.name) / 'seen.db')
        try:
            self.assertEqual(restored.first_member_name('group1', 'member-one'), '落落最初昵称')
        finally:
            restored.conn.close()

    def test_group_context_keeps_search_queries_and_drops_transport_noise(self):
        self.assertEqual(clean_group_context('/检索 凯伊怎么预约'), '凯伊怎么预约')
        self.assertEqual(clean_group_context('[CQ:image,file=abc]'), '')
        self.assertEqual(clean_group_context('/总结'), '')

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

    async def test_memory_commands_call_scoped_service_without_search(self):
        msg = self.identified('/记忆', 'memory-list')
        await self.bot.answer(msg, 'group')
        self.retriever.memory.assert_awaited_once_with('list', {
            'origin': 'qq_group', 'user_id': 'user1', 'group_id': 'group1'})
        self.assertIn('还没有保存长期记忆', msg.reply.call_args.kwargs['content'])
        self.retriever.search.assert_not_awaited()
        clear = self.identified('/清除记忆', 'memory-clear')
        self.retriever.memory.return_value = {'ok': True, 'deleted': 3, 'items': []}
        await self.bot.answer(clear, 'group')
        self.assertIn('清除3条', clear.reply.call_args.kwargs['content'])

    async def test_group_memory_command_works_without_mention_and_new_chat_clears_shared_context(self):
        command = self.identified('/记忆', 'group-memory-command')
        await self.bot.on_group_message_create(command)
        self.retriever.memory.assert_awaited_once()
        self.retriever.search.assert_not_awaited()
        self.seen.observe_group_message(self.identified('旧上下文', 'old-context'))
        clear = self.identified('/新对话', 'group-clear-command')
        await self.bot.on_group_message_create(clear)
        self.assertEqual(self.seen.group_history('group1', 'future-message'), [])

    async def test_http_group_context_and_memory_contract(self):
        received = []
        async def handler(request):
            received.append((request.path, await request.json()))
            return web.json_response({'ok': True, 'items': []})
        app = web.Application()
        app.router.add_post('/agent/answer', handler)
        app.router.add_post('/agent/memory', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            retriever = Retriever(f'http://127.0.0.1:{port}/retrieve', 'read-test', 'kb-test')
            context = [{'role':'user','content':'[群友1] 上一句'}]
            image = 'data:image/png;base64,ephemeral'
            await retriever.search('这个呢', group_id='g1', history=[], group_context=context,
                trace_meta={'origin':'qq_group','user_id':'u1'}, image_data_urls=[image])
            await retriever.memory('list', {'origin':'qq_group','user_id':'u1','group_id':'g1'})
            self.assertEqual(received[0][1]['group_context'], context)
            self.assertEqual(received[0][1]['image_data_urls'], [image])
            self.assertEqual(received[1][0], '/agent/memory')
            self.assertEqual(received[1][1]['action'], 'list')
        finally:
            await runner.cleanup()

    async def test_private_maintenance_routing_and_isolation(self):
        self.retriever.maintain=AsyncMock(return_value={'answer':'要修改哪一条？','active':True})
        await self.bot.answer(self.identified('/modify qa', 'manage1'),'c2c')
        self.retriever.search.assert_not_awaited()
        await self.bot.answer(self.identified('凯伊下单说明', 'manage2'),'c2c')
        self.assertEqual(self.retriever.maintain.await_count,2)
        key=conversation_key(self.identified('x','x'),'c2c','test')
        self.assertEqual(self.seen.history(key),[])
        await self.bot.answer(self.identified('/add 商品库 凯伊', 'manage3'),'group',mentioned=True)
        self.assertEqual(self.retriever.maintain.await_count,2)
        self.retriever.search.assert_not_awaited()
        self.retriever.maintain.return_value={'answer':'已退出','active':False}
        await self.bot.answer(self.identified('/退出', 'manage4'),'c2c')
        self.assertFalse(self.seen.maintenance_active(key))

    async def test_quoted_question_in_context_and_history(self):
        msg = self.identified('这个怎么买', 'quote-question')
        msg.sweet_learning = {'reference':{'quotes':[{'content':'kei毛绒', 'member_id':'opaque-id'}, {'content':'kei毛绒'}]}}
        await self.bot.answer(msg, 'group', mentioned=True)
        self.assertEqual(self.retriever.search.call_args.kwargs['trace_meta']['reply_reference'], 'kei毛绒')
        saved = self.seen.history(conversation_key(msg, 'group', 'test'))
        self.assertIn('引用内容：kei毛绒', saved[0]['content'])
        self.assertNotIn('opaque-id', saved[0]['content'])

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
        self.assertEqual(len(history), 24)
        self.assertLessEqual(sum(len(m['content']) for m in history), 24000)
        self.assertEqual(history[-2]['content'], '11')

    async def test_trace_delivery_and_metadata(self):
        self.retriever.report_delivery = AsyncMock()
        trace = {'answer':'定金20元。','trace_id':'trace-1','trace_receipt':'receipt-secret'}
        self.retriever.search.return_value = trace
        msg = self.identified('定金呢', 'delivery-trace')
        await self.bot.answer(msg, 'group', mentioned=True)
        self.retriever.report_delivery.assert_awaited_once_with(trace,'delivered','定金20元。','')
        meta = self.retriever.search.call_args.kwargs['trace_meta']
        self.assertEqual(meta['origin'],'qq_group')
        self.assertEqual(meta['user_id'],'user1')
        self.assertEqual(meta['session_id'],conversation_key(msg,'group','test'))
        self.retriever.report_delivery.reset_mock()
        msg = self.identified('失败', 'failed-trace')
        msg.reply.side_effect = RuntimeError('send failed')
        await self.bot.answer(msg,'c2c')
        self.retriever.report_delivery.assert_awaited_once_with(trace,'failed','','RuntimeError')

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
