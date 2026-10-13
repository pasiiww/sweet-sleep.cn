import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import SeenMessages, KnowledgeBot
from group_impressions import GroupImpressions


class GroupImpressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'seen.db'
        self.seen = SeenMessages(self.path)
        self.retriever = SimpleNamespace(kb_id='kb', update_impressions=AsyncMock(return_value={'ok': True, 'updated': 3}))
        self.store = GroupImpressions(self.seen.conn, self.retriever)

    def tearDown(self):
        self.seen.conn.close()
        self.temp.cleanup()

    def message(self, i, group='g', content=None):
        return SimpleNamespace(id=str(i), group_openid=group, content=content or f'喜欢角色{i}',
            author=SimpleNamespace(member_openid=f'owner-{i % 7}'), sweet_sender_name='同名',
            reply=AsyncMock())

    async def test_100_message_threshold_dedup_restart_and_group_isolation(self):
        for i in range(99):
            self.store.observe(self.message(i))
            self.store.observe(self.message(i))
        self.store.observe(self.message(99, content='/总结'))
        self.store.observe(self.message(99, content='[图片] 😀'))
        self.store.observe(self.message(0, 'other'))
        self.assertFalse(await self.store.flush_once())
        self.retriever.update_impressions.assert_not_awaited()
        self.seen.conn.close()
        self.seen = SeenMessages(self.path)
        self.store = GroupImpressions(self.seen.conn, self.retriever)
        self.store.observe(self.message(99))
        self.assertTrue(await self.store.flush_once())
        payload = self.retriever.update_impressions.call_args.args[0]
        self.assertEqual(len(payload['transcript'].splitlines()), 100)
        self.assertEqual(len(payload['members']), 7)
        self.assertNotIn('owner-', payload['transcript'])
        self.assertEqual(len(payload['batch_id']), 64)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM impression_messages').fetchone()[0], 1)
        self.store.observe(self.message(1))
        self.assertFalse(await self.store.flush_once())
        self.assertEqual(self.retriever.update_impressions.await_count, 1)

    async def test_failed_batch_retries_identically_and_preserves_inflight_messages(self):
        for i in range(100):
            self.store.observe(self.message(i))
        async def fail(payload):
            self.store.observe(self.message(100))
            return {'ok': False, 'reason': 'network_timeout'}
        self.retriever.update_impressions.side_effect = fail
        self.assertFalse(await self.store.flush_once())
        first = self.retriever.update_impressions.call_args.args[0]
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM impression_outbox').fetchone()[0], 1)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM impression_messages').fetchone()[0], 1)
        self.assertFalse(await self.store.flush_once())
        self.assertEqual(self.retriever.update_impressions.await_count, 1)
        with self.seen.conn:
            self.seen.conn.execute('UPDATE impression_outbox SET retry_at=0')
        self.retriever.update_impressions.side_effect = None
        self.assertTrue(await self.store.flush_once())
        self.assertEqual(first, self.retriever.update_impressions.call_args.args[0])
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM impression_messages').fetchone()[0], 1)

    async def test_batch_text_is_bounded_and_each_100_messages_remain_separate(self):
        for i in range(200):
            self.store.observe(self.message(i, content=('文字' * 1000) + str(i)))
        self.assertTrue(await self.store.flush_once())
        first = self.retriever.update_impressions.call_args.args[0]
        self.assertLessEqual(len(first['transcript']), 15000)
        self.assertTrue(await self.store.flush_once())
        second = self.retriever.update_impressions.call_args.args[0]
        self.assertNotEqual(first['batch_id'], second['batch_id'])
        self.assertFalse(await self.store.flush_once())

    async def test_qq_events_collect_once_and_moderated_text_never_enters_batches(self):
        self.retriever.search = AsyncMock(return_value={'answer': '回复'})
        bot = KnowledgeBot(self.retriever, self.seen)
        msg = self.message(1)
        await bot.on_group_message_create(msg)
        await bot.on_group_at_message_create(msg)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM impression_messages').fetchone()[0], 1)
        bot.moderate_group_message = AsyncMock(return_value=True)
        await bot.on_group_message_create(self.message(2))
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM impression_messages').fetchone()[0], 1)
        self.retriever.update_impressions.assert_not_awaited()
