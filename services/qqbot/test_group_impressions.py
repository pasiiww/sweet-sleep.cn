import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot import SeenMessages, KnowledgeBot
from group_impressions import GroupImpressions


class GroupImpressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'seen.db'
        self.seen = SeenMessages(self.path)
        self.retriever = SimpleNamespace(kb_id='kb',
                                         review_harassment=AsyncMock(return_value={'ok': True, 'harassment_indices': [],
                                                                                   'harassment_recall_enabled': False}),
                                         update_impressions=AsyncMock(return_value={'ok': True, 'updated': 3}),
                                         record_impression_recall=AsyncMock())
        self.task_calls = Mock()
        self.task_calls.attach_mock(self.retriever.review_harassment, 'review')
        self.task_calls.attach_mock(self.retriever.update_impressions, 'memory')
        self.store = GroupImpressions(self.seen.conn, self.retriever)

    def tearDown(self):
        self.seen.conn.close()
        self.temp.cleanup()

    def message(self, i, group='g', content=None, at=None):
        timestamp = datetime.fromtimestamp(at, timezone.utc).isoformat() if at else None
        return SimpleNamespace(id=str(i), group_openid=group, content=content or f'喜欢角色{i}',
            author=SimpleNamespace(member_openid=f'owner-{i % 7}'), sweet_sender_name='同名',
            reply=AsyncMock(), timestamp=timestamp)

    async def test_100_message_threshold_dedup_restart_and_group_isolation(self):
        for i in range(99):
            self.store.observe(self.message(i))
            self.store.observe(self.message(i))
        self.store.observe(self.message(99, content='/总结'))
        self.store.observe(self.message(99, content='[图片] 😀'))
        self.store.observe(self.message(0, 'other'))
        self.assertFalse(await self.store.flush_once())
        self.retriever.review_harassment.assert_not_awaited()
        self.retriever.update_impressions.assert_not_awaited()
        self.seen.conn.close()
        self.seen = SeenMessages(self.path)
        self.store = GroupImpressions(self.seen.conn, self.retriever)
        self.store.observe(self.message(99))
        self.assertTrue(await self.store.flush_once())
        payload = self.retriever.update_impressions.call_args.args[0]
        self.assertEqual(len(payload['transcript'].splitlines()), 100)
        self.assertEqual(len(payload['members']), 7)
        self.assertNotIn('message_ids', payload)
        self.assertNotIn('message_times', payload)
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
        self.assertEqual(self.retriever.review_harassment.await_count, 2)
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

    async def test_maximum_roster_and_names_fit_backend_transcript_limit(self):
        for i in range(100):
            message = self.message(i, content='字' * 100)
            message.author = SimpleNamespace(member_openid=f'owner-{i}')
            message.sweet_sender_name = '名' * 12
            self.store.observe(message)
        self.assertTrue(await self.store.flush_once())
        payload = self.retriever.update_impressions.call_args.args[0]
        self.assertEqual(len(payload['members']), 100)
        self.assertLessEqual(len(payload['transcript']), 15000)

    async def test_flagged_tail_message_ids_are_recalled(self):
        recall = AsyncMock()
        self.store = GroupImpressions(self.seen.conn, self.retriever, recall)
        self.retriever.review_harassment.return_value = {
            'ok': True, 'harassment_indices': [2, 91, 100],
            'harassment_recall_enabled': True, 'trace_id': 'c' * 32}
        for i in range(100):
            sent_at = time.time() - 700 if i == 89 else time.time() - 540 if i == 88 else None
            self.store.observe(self.message(i, at=sent_at))
        self.assertTrue(await self.store.flush_once())
        review_payload = self.retriever.review_harassment.call_args.args[0]
        memory_payload = self.retriever.update_impressions.call_args.args[0]
        self.assertEqual(len(review_payload['message_times']), 100)
        self.assertTrue(memory_payload['transcript'].splitlines()[0].startswith('[消息1] '))
        self.assertTrue(memory_payload['transcript'].splitlines()[-1].startswith('[消息99] '))
        self.assertNotIn('message_ids', memory_payload)
        self.assertNotIn('message_times', memory_payload)
        self.assertEqual(recall.await_count, 3)
        recall.assert_any_await('g', '90')
        recall.assert_any_await('g', '99')
        self.retriever.record_impression_recall.assert_awaited_once_with('c' * 32, memory_payload['batch_id'], [
            {'index': 2, 'status': 'recalled'},
            {'index': 91, 'status': 'recalled'},
            {'index': 100, 'status': 'recalled'}])

    async def test_recall_failure_is_recorded_in_trace_with_platform_code(self):
        recall = AsyncMock()
        error = RuntimeError('QQ API rejected recall')
        error.code = 10031
        recall.side_effect = error
        self.store = GroupImpressions(self.seen.conn, self.retriever, recall)
        self.retriever.review_harassment.return_value = {
            'ok': True, 'harassment_indices': [5],
            'harassment_recall_enabled': True, 'trace_id': 'd' * 32}
        for i in range(100):
            self.store.observe(self.message(i))
        self.assertTrue(await self.store.flush_once())
        batch_id = self.retriever.review_harassment.call_args.args[0]['batch_id']
        self.retriever.record_impression_recall.assert_awaited_once_with('d' * 32, batch_id, [
            {'index': 5, 'status': 'failed', 'error_type': 'RuntimeError', 'error_code': '10031'}])

    async def test_future_messages_are_not_recalled(self):
        recall = AsyncMock()
        self.store = GroupImpressions(self.seen.conn, self.retriever, recall)
        self.retriever.review_harassment.return_value = {
            'ok': True, 'harassment_indices': [], 'harassment_recall_enabled': True}
        for i in range(100):
            sent_at = time.time() + 20 if i == 1 else None
            self.store.observe(self.message(i, at=sent_at))
        self.assertTrue(await self.store.flush_once())
        recall.assert_not_awaited()

    async def test_impression_harassment_recall_stays_off_until_enabled(self):
        recall = AsyncMock()
        self.store = GroupImpressions(self.seen.conn, self.retriever, recall)
        self.retriever.review_harassment.return_value = {
            'ok': True, 'harassment_indices': [1], 'harassment_recall_enabled': False}
        for i in range(100):
            self.store.observe(self.message(i))
        self.assertTrue(await self.store.flush_once())
        recall.assert_not_awaited()
        memory_payload = self.retriever.update_impressions.call_args.args[0]
        self.assertNotIn('[消息1] ', memory_payload['transcript'])

    async def test_review_precedes_memory_and_flagged_content_is_excluded(self):
        recall = AsyncMock()
        self.store = GroupImpressions(self.seen.conn, self.retriever, recall)
        self.retriever.review_harassment.return_value = {
            'ok': True, 'harassment_indices': [3], 'harassment_recall_enabled': False}
        for i in range(100):
            self.store.observe(self.message(i))
        self.assertTrue(await self.store.flush_once())
        self.assertEqual([call[0] for call in self.task_calls.mock_calls], ['review', 'memory'])
        memory_payload = self.retriever.update_impressions.call_args.args[0]
        self.assertNotIn('[消息3] ', memory_payload['transcript'])
        self.assertIn('[消息2] ', memory_payload['transcript'])
        self.assertNotIn('message_ids', memory_payload)
        self.assertNotIn('message_times', memory_payload)

    async def test_review_disabled_still_sends_the_complete_batch_to_memory(self):
        self.retriever.review_harassment.return_value = {
            'ok': True, 'reason': 'review_disabled', 'harassment_indices': [],
            'harassment_recall_enabled': False, 'harassment_review_enabled': False}
        for i in range(100):
            self.store.observe(self.message(i))
        self.assertTrue(await self.store.flush_once())
        self.assertEqual([call[0] for call in self.task_calls.mock_calls], ['review', 'memory'])
        memory_payload = self.retriever.update_impressions.call_args.args[0]
        self.assertEqual(len(memory_payload['transcript'].splitlines()), 100)
        self.assertIn('[消息3] ', memory_payload['transcript'])
        self.retriever.record_impression_recall.assert_not_awaited()

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
