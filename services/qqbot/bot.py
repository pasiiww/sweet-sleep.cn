"""QQ knowledge retrieval demo using Tencent's qq-botpy 1.2.1."""
import asyncio
import hashlib
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
HELP = '我是午觉糖水铺的客服机器人。\n直接发送问题，或输入：/检索 你的问题\n我会根据知识库资料回答，资料不足时请群主或管理员确认。模型不可用时返回最相关文档。\n同一会话保留最近30分钟的问答，可发送 /新对话 清空。\n管理员可在群内发送 /身份，获取后台人工接管配置需要的 OpenID。'


def normalize(text):
    text = re.sub(r'<@!?[A-Za-z0-9_-]+>|<qqbot-at-user\s+id="[A-Za-z0-9_-]+"\s*/>', '', text or '').strip()
    return re.sub(r'^/(?:检索|搜索|search)(?:\s+|$)', '', text, flags=re.I).strip()


def plain(text):
    # Prevent retrieved text from becoming QQ mentions / message markup.
    return str(text).replace('@', '＠').replace('<', '＜').replace('>', '＞').replace('\x00', '')


def format_results(data):
    if isinstance(data.get('answer'), str):
        return plain(data['answer'])[:1700]
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


def format_reply(data, kind):
    text = format_results(data)
    if kind == 'group' and data.get('handoff'):
        ids = data.get('mention_openids', [])
        if isinstance(ids, list):
            for member in ids[:3]:
                if isinstance(member, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}', member):
                    text += f'\n<qqbot-at-user id="{member}" />'
    return text


class SeenMessages:
    """Persist claims before processing; favors at-most-once replies across restarts."""
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.execute('CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, expires REAL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS dialogue(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, query TEXT NOT NULL, reply TEXT NOT NULL, at REAL NOT NULL)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS dialogue_session ON dialogue(session,id)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS dialogue_time ON dialogue(at)')
        self.conn.commit()

    def claim(self, key):
        with self.conn:
            self.conn.execute('DELETE FROM seen WHERE expires<?', (time.time(),))
            return self.conn.execute('INSERT OR IGNORE INTO seen VALUES(?,?)', (key, time.time() + 86400)).rowcount == 1


    def history(self, session):
        if not session:
            return []
        with self.conn:
            self.conn.execute('DELETE FROM dialogue WHERE at<=?', (time.time() - 1800,))
            rows = self.conn.execute('SELECT query,reply FROM dialogue WHERE session=? ORDER BY id DESC LIMIT 10', (session,)).fetchall()
        selected, used = [], 0
        for query, reply in rows:
            if used + len(query) + len(reply) > 12000:
                break
            selected.append([{'role': 'user', 'content': query}, {'role': 'assistant', 'content': reply}])
            used += len(query) + len(reply)
        return [message for pair in reversed(selected) for message in pair]

    def remember(self, session, query, reply):
        if not session:
            return
        with self.conn:
            self.conn.execute('DELETE FROM dialogue WHERE at<=?', (time.time() - 1800,))
            self.conn.execute('INSERT INTO dialogue(session,query,reply,at) VALUES(?,?,?,?)', (session, query, reply, time.time()))
            self.conn.execute('DELETE FROM dialogue WHERE session=? AND id NOT IN (SELECT id FROM dialogue WHERE session=? ORDER BY id DESC LIMIT 10)', (session, session))

    def clear_history(self, session):
        with self.conn:
            self.conn.execute('DELETE FROM dialogue WHERE session=?', (session,))


def conversation_key(message, kind, kb_id):
    author = getattr(message, 'author', None)
    user = getattr(author, 'member_openid' if kind == 'group' else 'user_openid', '')
    group = getattr(message, 'group_openid', '') if kind == 'group' else ''
    if not user or (kind == 'group' and not group):
        return ''  # Missing identity must never fall into a shared anonymous conversation.
    return hashlib.sha256(json.dumps([kb_id, kind, group, user]).encode()).hexdigest()


class Retriever:
    def __init__(self, url, token, kb_id):
        self.url = url.removesuffix('/retrieve') + '/answer' if url.endswith('/retrieve') else url
        self.token, self.kb_id = token, kb_id

    async def search(self, query, group_id='', history=None, trace_meta=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=55)) as session:
            async with session.post(self.url, headers={'Authorization': 'Bearer ' + self.token},
                                    json={'kb_id': self.kb_id, 'query': query, 'group_id': group_id, 'history': history or [], **(trace_meta or {})}) as response:
                if response.status != 200:
                    raise RuntimeError(f'Knowledge HTTP {response.status}')
                return await response.json()


    async def report_delivery(self, trace, status, content, error=''):
        if not trace.get('trace_id') or not trace.get('trace_receipt'):
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(self.url.rsplit('/', 1)[0] + '/trace-delivery',
                    headers={'Authorization': 'Bearer ' + self.token},
                    json={'trace_id': trace['trace_id'], 'receipt': trace['trace_receipt'], 'status': status,
                          'content': content[:3000], 'error': error[:80]}) as response:
                    if response.status != 200:
                        LOG.warning('TRACE_REPORT_FAILED status=%s', response.status)
        except Exception as exc:
            LOG.warning('TRACE_REPORT_FAILED error=%s', type(exc).__name__)


class KnowledgeBot(botpy.Client):
    def __init__(self, retriever, seen, **kwargs):
        super().__init__(intents=botpy.Intents(public_messages=True), timeout=15,
                         log_level=logging.INFO, ext_handlers=False, **kwargs)
        self.retriever, self.seen = retriever, seen
        self.capacity = asyncio.Semaphore(4)
        self.conversations = {}

    async def on_ready(self):
        LOG.info('QQ_CONNECTED app_id=%s knowledge_id=%s', os.environ.get('QQ_APP_ID'), self.retriever.kb_id)

    async def on_error(self, event_method, *args, **kwargs):
        LOG.error('EVENT_ERROR event=%s', event_method)

    async def on_c2c_message_create(self, message):
        await self.answer(message, 'c2c')

    async def on_group_at_message_create(self, message):
        # The platform event certifies this bot was mentioned; text may omit the tag.
        await self.answer(message, 'group', mentioned=True)

    async def on_group_message_create(self, message):
        # Ordinary group messages must not enter deduplication, retrieval or model calls.
        return

    async def answer(self, message, kind, mentioned=False):
        if kind == 'group' and not mentioned:
            return
        if not message.id or not self.seen.claim(kind + ':' + message.id):
            return
        session = conversation_key(message, kind, self.retriever.kb_id)
        # Serialize generation AND delivery for each conversation; release unused locks.
        lock_key = session or kind + ':' + message.id
        entry = self.conversations.setdefault(lock_key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                await self._answer(message, kind, session)
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.conversations.pop(lock_key, None)

    async def _answer(self, message, kind, session):
        query = normalize(message.content)
        remember = False
        trace, delivery, sent, delivery_error = None, 'failed', '', ''
        try:
            if not query or query.lower() in ('帮助', '/帮助', '/help', 'help', '/start'):
                reply = HELP
            elif query in ('/新对话', '/清空上下文'):
                self.seen.clear_history(session)
                reply = '已清空当前对话的上下文，我们重新开始。'
            elif query in ('/身份', '/whoami'):
                if kind == 'group':
                    reply = f'群 OpenID：{plain(message.group_openid)}\n你的成员 OpenID：{plain(message.author.member_openid)}\n请由管理员在知识库后台填写人工联系人。此命令不会自动赋予管理员身份。'
                else:
                    reply = '请在需要配置人工接管的群里 @我发送 /身份。私聊 ID 不能代替群内成员 ID。'
            elif len(query) > 2000:
                reply = '问题有点长，请缩短到 2000 字以内。'
            elif self.capacity.locked():
                reply = '当前检索人数较多，请稍后重新发送问题。'
            else:
                async with self.capacity:
                    group_id = getattr(message, 'group_openid', '') if kind == 'group' else ''
                    author = getattr(message, 'author', None)
                    meta = {'origin': 'qq_group' if kind == 'group' else 'qq_private',
                            'user_id': getattr(author, 'member_openid' if kind == 'group' else 'user_openid', ''), 'session_id': session}
                    trace = await self.retriever.search(query, group_id=group_id, history=self.seen.history(session), trace_meta=meta)
                    reply = format_reply(trace, kind)
                    remember = True
            response = await message.reply(content=reply, msg_type=0, msg_seq=1)
            if not response:
                raise RuntimeError('QQ empty response')
            delivery, sent = 'delivered', reply
            if remember:
                self.seen.remember(session, query, reply)
            LOG.info('REPLY_OK kind=%s chars=%s', kind, len(reply))
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            delivery_error = type(exc).__name__
            LOG.error('REQUEST_FAILED kind=%s error=%s', kind, type(exc).__name__)
            # Same msg_seq prevents duplicate delivery if the first reply actually arrived.
            try:
                fallback_reply = '检索服务暂时不可用，请稍后重新发送问题。'
                fallback_result = await message.reply(content=fallback_reply, msg_type=0, msg_seq=1)
                if fallback_result: delivery, sent = 'delivered', fallback_reply
            except Exception as send_error:
                LOG.error('REPLY_FAILED error=%s', type(send_error).__name__)
        except Exception as exc:
            delivery_error = type(exc).__name__
            LOG.error('REPLY_FAILED kind=%s error=%s', kind, type(exc).__name__)
        finally:
            if trace and trace.get('trace_id'):
                await self.retriever.report_delivery(trace, delivery, sent, delivery_error)


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
