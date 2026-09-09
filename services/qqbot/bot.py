"""QQ knowledge retrieval demo using Tencent's qq-botpy 1.2.1."""
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import ssl
import time

import aiohttp
import botpy
import botpy.gateway
import botpy.http

LOG = logging.getLogger('knowledge-bot')
HELP = '我是知识库检索机器人。\n直接发送问题，或输入：/检索 你的问题\n我会返回最多 3 段知识库原文和来源。\n演示问题：机器人怎么使用？\n目前只检索资料，不调用大模型。'


def normalize(text):
    text = re.sub(r'<@!?\d+>', '', text or '').strip()
    return re.sub(r'^/(?:检索|搜索|search)(?:\s+|$)', '', text, flags=re.I).strip()


def plain(text):
    # Prevent retrieved text from becoming QQ mentions / message markup.
    return str(text).replace('@', '＠').replace('<', '＜').replace('>', '＞').replace('\x00', '')


def format_results(data):
    results = data.get('results', [])[:3]
    if not results:
        return '知识库中没有找到相关内容。可以换一组关键词，或请管理员补充相关文档。'
    parts = ['为你找到以下知识库原文：']
    for i, row in enumerate(results, 1):
        content = plain(row.get('content', ''))
        if len(content) > 360:
            content = content[:360] + '…（片段已截断）'
        source = plain(row.get('source') or row.get('title', '未命名文档'))[:100]
        title = plain(row.get('title', '未命名文档'))[:80]
        parts.append(f'【{i}】{title}\n{content}\n来源：{source} · 分段 {int(row.get("ordinal", 0)) + 1}')
    parts.append('以上为检索原文，未经过大模型改写。')
    return '\n\n'.join(parts)


class SeenMessages:
    """Persist claims before processing; favors at-most-once replies across restarts."""
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.execute('CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, expires REAL)')
        self.conn.commit()

    def claim(self, key):
        with self.conn:
            self.conn.execute('DELETE FROM seen WHERE expires<?', (time.time(),))
            return self.conn.execute('INSERT OR IGNORE INTO seen VALUES(?,?)', (key, time.time() + 86400)).rowcount == 1


class Retriever:
    def __init__(self, url, token, kb_id):
        self.url, self.token, self.kb_id = url, token, kb_id

    async def search(self, query):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            async with session.post(self.url, headers={'Authorization': 'Bearer ' + self.token},
                                    json={'kb_id': self.kb_id, 'query': query, 'mode': 'keyword',
                                          'top_k': 3, 'max_context_chars': 1800}) as response:
                if response.status != 200:
                    raise RuntimeError(f'Knowledge HTTP {response.status}')
                return await response.json()


class KnowledgeBot(botpy.Client):
    def __init__(self, retriever, seen, **kwargs):
        super().__init__(intents=botpy.Intents(public_messages=True), timeout=15,
                         log_level=logging.INFO, ext_handlers=False, **kwargs)
        self.retriever, self.seen = retriever, seen
        self.capacity = asyncio.Semaphore(4)

    async def on_ready(self):
        LOG.info('QQ_CONNECTED app_id=%s knowledge_id=%s', os.environ.get('QQ_APP_ID'), self.retriever.kb_id)

    async def on_error(self, event_method, *args, **kwargs):
        LOG.error('EVENT_ERROR event=%s', event_method)

    async def on_c2c_message_create(self, message):
        await self.answer(message, 'c2c')

    async def on_group_at_message_create(self, message):
        await self.answer(message, 'group')

    async def answer(self, message, kind):
        if not message.id or not self.seen.claim(kind + ':' + message.id):
            return
        query = normalize(message.content)
        try:
            if not query or query.lower() in ('帮助', '/帮助', '/help', 'help', '/start'):
                reply = HELP
            elif len(query) > 2000:
                reply = '问题有点长，请缩短到 2000 字以内。'
            elif self.capacity.locked():
                reply = '当前检索人数较多，请稍后重新发送问题。'
            else:
                async with self.capacity:
                    reply = format_results(await self.retriever.search(query))
            response = await message.reply(content=reply, msg_type=0, msg_seq=1)
            if not response:
                raise RuntimeError('QQ empty response')
            LOG.info('REPLY_OK kind=%s chars=%s', kind, len(reply))
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            LOG.error('REQUEST_FAILED kind=%s error=%s', kind, type(exc).__name__)
            # Same msg_seq prevents duplicate delivery if the first reply actually arrived.
            try:
                await message.reply(content='检索服务暂时不可用，请稍后重新发送问题。', msg_type=0, msg_seq=1)
            except Exception as send_error:
                LOG.error('REPLY_FAILED error=%s', type(send_error).__name__)
        except Exception as exc:
            LOG.error('REPLY_FAILED kind=%s error=%s', kind, type(exc).__name__)


def configure_logging():
    # qq-botpy debug logging includes auth headers and event bodies. Do not enable it.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', force=True)
    sdk = logging.getLogger('botpy')
    sdk.setLevel(logging.INFO)
    class SafeSDK(logging.Filter):
        def filter(self, record):
            if record.levelno < logging.INFO:
                return False
            text = record.getMessage()
            for key in ('QQ_APP_SECRET', 'KB_READ_TOKEN'):
                value = os.environ.get(key)
                if value:
                    text = text.replace(value, '[redacted]')
            if record.levelno >= logging.WARNING:
                # Platform response bodies may contain message contents; keep only a code.
                status = re.search(r'(?:错误代码|返回码):\s*(\d+)', text)
                text = 'SDK_WARNING' + (' status=' + status.group(1) if status else '')
            record.msg, record.args, record.exc_info = text, (), None
            return True
    sdk.addFilter(SafeSDK())


def main():
    required = ('QQ_APP_ID', 'QQ_APP_SECRET', 'KB_ID', 'KB_READ_TOKEN')
    if any(not os.environ.get(key) for key in required):
        raise SystemExit('Missing required environment configuration')
    os.umask(0o077)
    configure_logging()
    # SDK 1.2.1 constructs bare SSLContext objects. Keep certificate verification enabled.
    botpy.http.SSLContext = ssl.create_default_context
    botpy.gateway.SSLContext = ssl.create_default_context
    directory = Path(os.environ.get('QQ_STATE_DIR', '/var/lib/sweet-qqbot'))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    retriever = Retriever(os.environ.get('KB_API_URL', 'http://127.0.0.1:8765/knowledge/api/retrieve'),
                          os.environ['KB_READ_TOKEN'], os.environ['KB_ID'])
    client = KnowledgeBot(retriever, SeenMessages(directory / 'seen.db'),
                          is_sandbox=os.environ.get('QQ_SANDBOX', 'false').lower() == 'true')
    try:
        client.run(appid=os.environ['QQ_APP_ID'], secret=os.environ['QQ_APP_SECRET'])
    except Exception as exc:
        LOG.error('STARTUP_FAILED error=%s', type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
