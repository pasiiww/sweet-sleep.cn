import asyncio
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from bot import KnowledgeBot, SeenMessages
from compat import FullGroupMessage
from image_history import ImageHistory, PublicResolver, hash_image, image_url


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'seen.db'
        self.seen = SeenMessages(self.path)
        self.retriever = SimpleNamespace(kb_id='test', search=AsyncMock())
        self.bot = KnowledgeBot(self.retriever, self.seen)
        self.api = SimpleNamespace(post_group_message=AsyncMock(return_value={'id': 'reply'}))
        self.hash_patch = patch('image_history.hash_image', AsyncMock(return_value='a' * 64))
        self.hash = self.hash_patch.start()

    async def asyncTearDown(self):
        self.hash_patch.stop()
        self.seen.conn.close()
        self.temp.cleanup()

    def message(self, mid='picture', group='group', member='member', at='2026-09-17T02:00:00Z', images=1, content='', ref='', idx='', ref_idx=''):
        return FullGroupMessage(self.api, 'event', {
            'id': mid, 'group_openid': group, 'author': {'member_openid': member, 'username': '昵称'},
            'timestamp': at, 'content': content, 'message_reference': {'message_id': ref},
            'message_scene': {'ext': ['msg_idx=' + idx, 'ref_msg_idx=' + ref_idx]},
            'attachments': [{'content_type': 'image/jpeg', 'url': f'https://gchat.qpic.cn/{i}'} for i in range(images)],
        }, 'bot')

    async def test_count_first_by_timestamp_group_isolation_and_restart(self):
        await self.bot.on_group_message_create(self.message(mid='new'))
        await self.bot.on_group_message_create(self.message(mid='earlier', member='first', at='2026-09-17T01:00:00Z'))
        await self.bot.on_group_message_create(self.message(mid='elsewhere', group='other', at='2026-09-16T01:00:00Z'))
        reopened = SeenMessages(self.path)
        try:
            answer = await ImageHistory(reopened.conn).lookup(self.message(images=0, ref='new'))
            self.assertIn('2 次', answer)
            self.assertIn('id="first"', answer)
            self.assertIn('2026-09-17 09:00:00', answer)
            self.assertEqual(reopened.conn.execute('SELECT count(*) FROM image_occurrences').fetchone()[0], 3)
        finally:
            reopened.conn.close()
        self.api.post_group_message.assert_not_awaited()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])
        self.assertNotIn(b'https://', self.path.read_bytes())

    async def test_dual_events_concurrent_and_replay_dedup(self):
        message = self.message(content='/help')
        await asyncio.gather(self.bot.on_group_message_create(message), self.bot.on_group_at_message_create(message))
        await self.bot.on_group_message_create(message)
        self.hash.assert_awaited_once()
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM image_occurrences').fetchone()[0], 1)
        self.assertEqual(self.api.post_group_message.await_count, 1)

    async def test_old_without_mention_no_quota_no_history(self):
        await self.bot.on_group_message_create(self.message(idx='index123'))
        query = self.message(mid='command', content='/old', images=0, ref_idx='index123')
        await self.bot.on_group_message_create(query)
        await self.bot.on_group_at_message_create(query)
        self.assertIn('1 次', self.api.post_group_message.call_args.kwargs['content'])
        self.assertEqual(self.api.post_group_message.await_count, 1)
        self.retriever.search.assert_not_awaited()
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM dialogue').fetchone()[0], 0)

    async def test_multiple_images_distinct_hashes_and_occurrences(self):
        self.hash.side_effect = ['a' * 64, 'b' * 64, 'a' * 64]
        await self.bot.on_group_message_create(self.message(images=3))
        answer = await self.bot.images.lookup(self.message(images=0, ref='picture'))
        self.assertIn('图片 1：这张图在本群已记录 2 次', answer)
        self.assertIn('图片 2：这张图在本群已记录 1 次', answer)
        self.assertIn('图片 3：这张图在本群已记录 2 次', answer)

    async def test_missing_reference_unseen_foreign_group_and_failed_download(self):
        self.hash.side_effect = asyncio.TimeoutError()
        await self.bot.on_group_message_create(self.message())
        self.assertIn('下载失败', await self.bot.images.lookup(self.message(images=0, ref='picture')))
        self.assertIn('请引用', await self.bot.images.lookup(self.message(images=0)))
        self.assertIn('没有找到', await self.bot.images.lookup(self.message(images=0, ref='missing')))
        self.assertIn('没有找到', await self.bot.images.lookup(self.message(images=0, group='other', ref='picture')))
        self.hash.side_effect = None
        await self.bot.on_group_message_create(self.message())
        self.assertIn('1 次', await self.bot.images.lookup(self.message(images=0, ref='picture')))

    async def test_query_waits_for_in_progress_hash(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            started.set()
            await finish.wait()
            return 'a' * 64
        self.hash.side_effect = slow
        record = asyncio.create_task(self.bot.on_group_message_create(self.message()))
        await started.wait()
        query = asyncio.create_task(self.bot.images.lookup(self.message(images=0, ref='picture')))
        await asyncio.sleep(0)
        self.assertFalse(query.done())
        finish.set()
        await record
        self.assertIn('1 次', await query)

    async def test_bot_messages_not_recorded(self):
        message = self.message()
        message.sweet_author_bot = True
        await self.bot.on_group_message_create(message)
        await self.bot.on_group_at_message_create(message)
        self.hash.assert_not_awaited()

    async def test_quoted_images_are_not_new_occurrences(self):
        data = {'id': 'quote', 'group_openid': 'group', 'content': '/old', 'message_type': 103,
                'author': {'member_openid': 'member'}, 'msg_elements': [
                    {'msg_idx': 'quoted-index', 'attachments': [{'content_type': 'image/jpeg', 'url': 'https://gchat.qpic.cn/1'}]}]}
        message = FullGroupMessage(self.api, 'event', data, 'bot')
        self.assertEqual(message.sweet_learning['reference']['msg_idx'], 'quoted-index')
        await self.bot.on_group_message_create(message)
        self.hash.assert_not_awaited()
        self.assertIn('没有找到', self.api.post_group_message.call_args.kwargs['content'])


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def test_url_validation(self):
        self.assertEqual(image_url('//gchat.qpic.cn/image'), 'https://gchat.qpic.cn/image')
        self.assertEqual(image_url('http://gchat.qpic.cn/image'), 'https://gchat.qpic.cn/image')
        for url in ['file:///tmp/secret', 'http://127.0.0.1/a', 'https://evil.test/a',
                    'https://qq.com.evil.test/a', 'https://user:pass@gchat.qpic.cn/a']:
            with self.assertRaises(ValueError):
                image_url(url)

    async def test_streaming_digest_empty_oversize_and_redirect(self):
        async def chunks(size):
            for data in [b'abc', b'def']:
                yield data
        response = SimpleNamespace(status=200, content_length=None,
                                   content=SimpleNamespace(iter_chunked=chunks))
        request = AsyncMock()
        request.__aenter__.return_value = response
        session = SimpleNamespace(get=lambda *a, **kw: request)
        client = AsyncMock()
        client.__aenter__.return_value = session
        with patch('image_history.aiohttp.ClientSession', return_value=client), patch('image_history.aiohttp.TCPConnector'):
            self.assertEqual(await hash_image('https://gchat.qpic.cn/a'), hashlib.sha256(b'abcdef').hexdigest())
            with patch('image_history.MAX_BYTES', 4), self.assertRaises(ValueError):
                await hash_image('https://gchat.qpic.cn/a')
            response.status = 302
            with self.assertRaises(ValueError):
                await hash_image('https://gchat.qpic.cn/a')
            response.status = 200
            async def empty(size):
                if False:
                    yield b''
            response.content.iter_chunked = empty
            with self.assertRaises(ValueError):
                await hash_image('https://gchat.qpic.cn/a')

    async def test_auth_only_for_exact_media_host_and_no_redirects(self):
        async def chunks(size):
            yield b'image'
        response = SimpleNamespace(status=200, content_length=5,
                                   content=SimpleNamespace(iter_chunked=chunks))
        request = AsyncMock()
        request.__aenter__.return_value = response
        session = SimpleNamespace(get=Mock(return_value=request))
        client = AsyncMock()
        client.__aenter__.return_value = session
        token = SimpleNamespace(check_token=AsyncMock(), get_string=lambda: 'QQBot test-secret')
        api = SimpleNamespace(_http=SimpleNamespace(_token=token))
        with patch('image_history.aiohttp.ClientSession', return_value=client), patch('image_history.aiohttp.TCPConnector'):
            await hash_image('https://multimedia.nt.qq.com.cn/a', api)
            self.assertEqual(session.get.call_args.kwargs['headers']['Authorization'], 'QQBot test-secret')
            self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
            await hash_image('https://gchat.qpic.cn/a', api)
            self.assertNotIn('Authorization', session.get.call_args.kwargs['headers'])
        token.check_token.assert_awaited_once()

    async def test_private_dns_rejected(self):
        resolver = PublicResolver()
        try:
            with patch.object(aiohttp.resolver.ThreadedResolver, 'resolve', AsyncMock(return_value=[{'host': '127.0.0.1'}])):
                with self.assertRaises(ValueError):
                    await resolver.resolve('gchat.qpic.cn')
        finally:
            await resolver.close()


if __name__ == '__main__':
    unittest.main()
