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
from learner import Learner
from image_history import ImageHistory
from group_summary import GroupSummary, timestamp as summary_timestamp
import compat

LOG = logging.getLogger('knowledge-bot')
MEMORY_COMMANDS = ('/记忆', '/清除记忆', '/关闭记忆', '/开启记忆')
GROUP_COMMANDS = ('/old', '/总结', '/新对话', '/清空上下文', *MEMORY_COMMANDS)
HELP = '群内发送 /总结：总结上次成功总结后、最近10小时、最多400条和15000字以内的聊天。\n群内引用图片发送 /old，可查询本群记录次数、首次发送者和时间。\n长期记忆：群内共享、私聊按用户独立；发送 /记忆 查看，/清除记忆 删除，/关闭记忆 暂停，/开启记忆 恢复。\n我是午觉糖水铺的客服机器人。\n直接发送问题，或输入：/检索 你的问题\n我会根据知识库资料回答，资料不足时请群主或管理员确认。模型不可用时返回最相关文档。\n群聊回答默认参考本群最近10条发言；同一私聊保留最近30分钟的问答，可发送 /新对话 清空。\n每天共20次咨询额度，群聊和私聊共享，北京时间零点恢复。\n管理员可在群内发送 /身份，获取后台人工接管配置需要的 OpenID。'


def normalize(text):
    text = re.sub(r'<@!?[A-Za-z0-9_-]+>|<qqbot-at-user\s+id="[A-Za-z0-9_-]+"\s*/>', '', text or '').strip()
    return re.sub(r'^/(?:检索|搜索|search)(?:\s+|$)', '', text, flags=re.I).strip()


def plain(text):
    # Prevent retrieved text from becoming QQ mentions / message markup.
    return str(text).replace('@', '＠').replace('<', '＜').replace('>', '＞').replace('\x00', '')


def clean_group_context(text):
    if not isinstance(text, str):
        return ''
    text = text.strip()
    if text.startswith(('{', '[')):
        try:
            value = json.loads(text)
            def extract(item, depth=0):
                if depth > 5:
                    return []
                if isinstance(item, dict):
                    return [item[k] for k in ('text', 'content') if isinstance(item.get(k), str)] + [
                        part for v in item.values() if isinstance(v, list) for part in extract(v, depth+1)]
                if isinstance(item, list):
                    return [part for child in item[:100] for part in extract(child, depth+1)]
                return []
            text = '\n'.join(extract(value))
        except (ValueError, RecursionError):
            if text.startswith('{'):
                return ''
    text = re.sub(r'<@!?[A-Za-z0-9_-]+>|<qqbot-at-user\s+id="[A-Za-z0-9_-]+"\s*/>', '', text)
    text = re.sub(r'<faceType=[^>]*>|<[^>]*>|\[CQ:[^\]]*\]|\[(?:图片|表情包|动画表情|表情|image|emoji)[^\]]*\]', '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^/(?:检索|搜索|search)(?:\s+|$)', '', text, flags=re.I).strip()
    if text.startswith('/') or not re.search(r'[\w\u3400-\u9fff]', text):
        return ''
    return text[:1200]


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
        self.conn.execute('CREATE TABLE IF NOT EXISTS sticker_state(session TEXT PRIMARY KEY, sent INTEGER NOT NULL, at REAL NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS private_maintenance_session (id TEXT PRIMARY KEY, at REAL NOT NULL)')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS group_context_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
            member_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, at REAL NOT NULL,
            UNIQUE(group_id,message_id))''')
        self.conn.execute('CREATE INDEX IF NOT EXISTS dialogue_session ON dialogue(session,id)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS dialogue_time ON dialogue(at)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS group_context_time ON group_context_messages(group_id,at,id)')
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
            rows = self.conn.execute('SELECT query,reply FROM dialogue WHERE session=? ORDER BY id DESC LIMIT 20', (session,)).fetchall()
        selected, used = [], 0
        for query, reply in rows:
            if used + len(query) + len(reply) > 24000:
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
            self.conn.execute('DELETE FROM dialogue WHERE session=? AND id NOT IN (SELECT id FROM dialogue WHERE session=? ORDER BY id DESC LIMIT 20)', (session, session))

    def maintenance_active(self, session):
        return bool(self.conn.execute('SELECT 1 FROM private_maintenance_session WHERE id=? AND at>?',(session,time.time()-1800)).fetchone())

    def set_maintenance(self, session, active):
        with self.conn:
            self.conn.execute('DELETE FROM private_maintenance_session WHERE at<=?',(time.time()-1800,))
            self.conn.execute('DELETE FROM private_maintenance_session WHERE id=?',(session,))
            if active:self.conn.execute('INSERT INTO private_maintenance_session VALUES(?,?)',(session,time.time()))

    def last_sticker_sent(self, session):
        if not session:return False
        row=self.conn.execute('SELECT sent FROM sticker_state WHERE session=? AND at>?',(session,time.time()-1800)).fetchone()
        return bool(row and row[0])

    def record_sticker(self, session, sent):
        if not session:return
        with self.conn:
            self.conn.execute('DELETE FROM sticker_state WHERE at<=?',(time.time()-1800,))
            self.conn.execute('INSERT OR REPLACE INTO sticker_state VALUES(?,?,?)',(session,int(sent),time.time()))

    def clear_history(self, session):
        with self.conn:
            self.conn.execute('DELETE FROM dialogue WHERE session=?', (session,))
            self.conn.execute('DELETE FROM sticker_state WHERE session=?',(session,))

    def observe_group_message(self, message):
        group = getattr(message, 'group_openid', '')
        message_id = getattr(message, 'id', '')
        member = getattr(getattr(message, 'author', None), 'member_openid', '')
        content = clean_group_context(getattr(message, 'content', '') or '')
        if not group or not message_id or not member or not content:
            return
        at = summary_timestamp(message)
        with self.conn:
            self.conn.execute('DELETE FROM group_context_messages WHERE at<?', (time.time()-7*86400,))
            self.conn.execute('''INSERT OR IGNORE INTO group_context_messages
                (group_id,message_id,member_id,role,content,at) VALUES(?,?,?,'user',?,?)''',
                (group, str(message_id), member, content, at))
            self.conn.execute('''DELETE FROM group_context_messages WHERE group_id=? AND id NOT IN
                (SELECT id FROM group_context_messages WHERE group_id=? ORDER BY at DESC,id DESC LIMIT 400)''', (group, group))

    def remember_group_reply(self, group, message_id, content):
        content = clean_group_context(content)
        if not group or not message_id or not content:
            return
        with self.conn:
            self.conn.execute('DELETE FROM group_context_messages WHERE at<?', (time.time()-7*86400,))
            self.conn.execute('''INSERT OR IGNORE INTO group_context_messages
                (group_id,message_id,member_id,role,content,at) VALUES(?,?,'bot','assistant',?,?)''',
                (group, str(message_id), content[:1200], time.time()))
            self.conn.execute('''DELETE FROM group_context_messages WHERE group_id=? AND id NOT IN
                (SELECT id FROM group_context_messages WHERE group_id=? ORDER BY at DESC,id DESC LIMIT 400)''', (group, group))

    def group_history(self, group, current_message_id, limit=10):
        if not group:
            return []
        current = self.conn.execute('SELECT id,at FROM group_context_messages WHERE group_id=? AND message_id=?',
                                    (group, str(current_message_id))).fetchone()
        if current:
            where = '(at<? OR (at=? AND id<?))'
            params = (group, current[1], current[1], current[0], limit)
        else:
            where = 'at<=?'
            params = (group, time.time(), limit)
        rows = self.conn.execute(f'''SELECT member_id,role,content,at FROM group_context_messages
            WHERE group_id=? AND {where} ORDER BY at DESC,id DESC LIMIT ?''', params).fetchall()
        rows.reverse()
        labels, result = {}, []
        for row in rows:
            member_id, role, content, created_at = row
            if role == 'assistant':
                speaker = '机器人'
            else:
                speaker = labels.setdefault(member_id, '群友'+str(len(labels)+1))
            at = time.strftime('%m-%d %H:%M', time.localtime(created_at))
            result.append({'role': role, 'content': f'[{at}] {speaker}：{content}'})
        return result

    def clear_group_context(self, group):
        with self.conn:
            self.conn.execute('DELETE FROM group_context_messages WHERE group_id=?', (group,))


def conversation_key(message, kind, kb_id):
    author = getattr(message, 'author', None)
    user = getattr(author, 'member_openid' if kind == 'group' else 'user_openid', '')
    group = getattr(message, 'group_openid', '') if kind == 'group' else ''
    if not user or (kind == 'group' and not group):
        return ''  # Missing identity must never fall into a shared anonymous conversation.
    return hashlib.sha256(json.dumps([kb_id, kind, group, user]).encode()).hexdigest()


class Retriever:
    def __init__(self, url, token, kb_id):
        self.api_root = url.rsplit('/', 1)[0] if url.endswith(('/retrieve', '/answer')) else url.rsplit('/agent/', 1)[0]
        self.url = self.api_root + '/agent/answer'
        self.token, self.kb_id = token, kb_id

    async def search(self, query, group_id='', history=None, trace_meta=None, group_context=None):
        payload = {'kb_id': self.kb_id, 'query': query, 'group_id': group_id,
                   'history': history or [], **(trace_meta or {})}
        if group_context is not None:
            payload['group_context'] = group_context
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=80)) as session:
            async with session.post(self.url, headers={'Authorization': 'Bearer ' + self.token},
                                    json=payload) as response:
                if response.status != 200:
                    raise RuntimeError(f'Knowledge HTTP {response.status}')
                return await response.json()

    async def memory(self, action, meta):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(self.api_root + '/agent/memory',
                headers={'Authorization': 'Bearer ' + self.token},
                json={'kb_id': self.kb_id, 'action': action, **meta}) as response:
                if response.status != 200:
                    raise RuntimeError('Memory HTTP ' + str(response.status))
                return await response.json()


    async def summarize(self, transcript):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=55)) as session:
            async with session.post(self.api_root + '/group-summary',
                headers={'Authorization': 'Bearer ' + self.token},
                json={'kb_id': self.kb_id, 'transcript': transcript}) as response:
                if response.status != 200:
                    raise RuntimeError('Summary HTTP ' + str(response.status))
                return await response.json()

    async def maintain(self, query, user_id, message_id):
        token=os.environ.get('KB_LEARN_TOKEN','')
        if not token:return {'answer':'维护通道尚未配置，请到知识库后台处理。','active':False}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=80)) as session:
            async with session.post(self.api_root + '/agent/private-maintenance',
                headers={'Authorization':'Bearer '+token},json={'kb_id':self.kb_id,'query':query,'user_id':user_id,'message_id':message_id}) as response:
                if response.status!=200:raise RuntimeError('Maintenance HTTP '+str(response.status))
                return await response.json()

    async def report_delivery(self, trace, status, content, error=''):
        if not trace.get('trace_id') or not trace.get('trace_receipt'):
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(self.api_root + '/trace-delivery',
                    headers={'Authorization': 'Bearer ' + self.token},
                    json={'trace_id': trace['trace_id'], 'receipt': trace['trace_receipt'], 'status': status,
                          'content': content[:3000], 'error': error[:80], 'sticker': trace.get('sticker_delivery')}) as response:
                    if response.status != 200:
                        LOG.warning('TRACE_REPORT_FAILED status=%s', response.status)
        except Exception as exc:
            LOG.warning('TRACE_REPORT_FAILED error=%s', type(exc).__name__)


class KnowledgeBot(botpy.Client):
    def __init__(self, retriever, seen, **kwargs):
        compat.install()
        super().__init__(intents=botpy.Intents(public_messages=True), timeout=15,
                         log_level=logging.INFO, ext_handlers=False, **kwargs)
        self.retriever, self.seen = retriever, seen
        self.images = ImageHistory(seen.conn)
        self.summaries = GroupSummary(seen.conn)
        self.capacity = asyncio.Semaphore(4)
        self.conversations = {}
        self.learner = None
        if os.environ.get("KB_LEARN_TOKEN"):
            self.learner = Learner(seen.conn, retriever.api_root+"/learning/events", os.environ["KB_LEARN_TOKEN"], retriever.kb_id)

    async def on_ready(self):
        if self.learner:
            self.learner.api=self.api
            self.learner.start()
        LOG.info('QQ_CONNECTED app_id=%s knowledge_id=%s', os.environ.get('QQ_APP_ID'), self.retriever.kb_id)

    async def on_error(self, event_method, *args, **kwargs):
        LOG.error('EVENT_ERROR event=%s', event_method)

    async def on_c2c_message_create(self, message):
        await self.answer(message, 'c2c')

    async def on_group_at_message_create(self, message):
        LOG.info('GROUP_RECEIVED event=at group=%s bot=%s mentioned=True',
                 getattr(message, 'group_openid', ''), compat.is_bot(message))
        if compat.is_bot(message):return
        self.summaries.observe(message)
        self.seen.observe_group_message(message)
        await self.images.observe(message)
        if self.learner: self.learner.observe(message)
        # The platform event certifies this bot was mentioned; text may omit the tag.
        await self.answer(message, 'group', mentioned=True)

    async def on_group_message_create(self, message):
        # Only delivered platform events can be observed; learning never sends a reply.
        LOG.info('GROUP_RECEIVED event=all group=%s bot=%s mentioned=%s',
                 getattr(message, 'group_openid', ''), compat.is_bot(message),
                 getattr(message, 'sweet_mentioned', False))
        if compat.is_bot(message):return
        self.summaries.observe(message)
        self.seen.observe_group_message(message)
        await self.images.observe(message)
        if self.learner: self.learner.observe(message)
        if getattr(message, 'sweet_mentioned', False) or normalize(message.content).lower() in GROUP_COMMANDS:
            await self.answer(message, 'group', mentioned=getattr(message, 'sweet_mentioned', False))

    async def answer(self, message, kind, mentioned=False):
        if compat.is_bot(message):return
        if kind == 'group':
            self.seen.observe_group_message(message)
        if kind == 'group' and not mentioned and normalize(message.content).lower() not in GROUP_COMMANDS:
            return
        if not message.id or not self.seen.claim(kind + ':' + message.id):
            LOG.info('MESSAGE_SKIPPED kind=%s reason=duplicate_or_missing_id', kind)
            return
        if normalize(message.content) == '/总结':
            await self.summarize(message, kind)
            return
        session = conversation_key(message, kind, self.retriever.kb_id)
        # Serialize generation AND delivery for each conversation; release unused locks.
        group_id = getattr(message, 'group_openid', '') if kind == 'group' else ''
        lock_key = ('group:' + group_id) if group_id else (session or kind + ':' + message.id)
        entry = self.conversations.setdefault(lock_key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                await self._answer(message, kind, session)
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.conversations.pop(lock_key, None)

    async def summarize(self, message, kind):
        group = getattr(message, 'group_openid', '')
        if kind != 'group' or not group:
            await message.reply(content='请在群内发送 /总结。', msg_type=0, msg_seq=1)
            return
        if group in self.summaries.busy:
            await message.reply(content='本群正在生成总结，请稍候。', msg_type=0, msg_seq=1)
            return
        self.summaries.busy.add(group)
        try:
            transcript, checkpoint, count = self.summaries.snapshot(group, summary_timestamp(message))
            if not transcript:
                await message.reply(content='当前范围内没有可总结的新内容（已过滤复读和表情包）。', msg_type=0, msg_seq=1)
                return
            result = await self.retriever.summarize(transcript)
            reply = ('群聊总结（清理后' + str(count) + '条发言）\n' if result.get('ok') else '') + plain(result['answer'])[:1500]
            sent = await message.reply(content=reply, msg_type=0, msg_seq=1)
            if sent and result.get('ok'):
                self.summaries.delivered(group, checkpoint, reply)
            LOG.info('SUMMARY_DELIVERY ok=%s messages=%s', bool(sent and result.get('ok')), count)
        except Exception as exc:
            LOG.warning('SUMMARY_FAILED error=%s', type(exc).__name__)
            try:
                await message.reply(content='总结暂时失败，请稍后重试；总结进度没有更新。', msg_type=0, msg_seq=1)
            except Exception:
                LOG.warning('SUMMARY_REPLY_FAILED')
        finally:
            self.summaries.busy.discard(group)

    async def send_answer(self, message, kind, reply, trace, session=''):
        sticker = (trace or {}).get('sticker')
        if sticker:
            try:
                if kind == 'group':
                    upload = message._api.post_group_file(group_openid=message.group_openid,
                        file_type=1, url=sticker['url'], srv_send_msg=False)
                else:
                    upload = message._api.post_c2c_file(openid=message.author.user_openid,
                        file_type=1, url=sticker['url'], srv_send_msg=False)
                media = await asyncio.wait_for(upload, timeout=15)
                if not media or not media.get('file_info'):
                    raise RuntimeError('QQ empty media response')
                result = await message.reply(content=reply or None, msg_type=7,
                    media={'file_info': media['file_info']}, msg_seq=1)
                if not result:
                    raise RuntimeError('QQ empty response')
                trace['sticker_delivery'] = {'name': sticker['name'], 'status': 'sent'}
                self.seen.record_sticker(session,True)
                return result
            except Exception as exc:
                trace['sticker_delivery'] = {'name': sticker['name'], 'status': 'failed', 'error': type(exc).__name__}
                LOG.warning('STICKER_FAILED error=%s', type(exc).__name__)
        # Preserve the generated answer when an optional image cannot be sent.
        sent_text=reply or '表情包刚刚没发出去呀～我还在这里陪你聊。'
        if trace is not None:trace['_sent_text']=sent_text
        result=await message.reply(content=sent_text, msg_type=0, msg_seq=1)
        if result:self.seen.record_sticker(session,False)
        return result

    async def _answer(self, message, kind, session):
        query = normalize(message.content)
        group_id = getattr(message, 'group_openid', '') if kind == 'group' else ''
        learning = getattr(message, 'sweet_learning', None)
        quotes = ((learning.get('reference') or {}).get('quotes') or []) if isinstance(learning, dict) else []
        quote_texts = list(dict.fromkeys(normalize(q.get('content', '')) for q in quotes if isinstance(q, dict)))
        reply_reference = '\n'.join(t for t in quote_texts if t and t != query)[:1800]
        remember = False
        trace, delivery, sent, delivery_error = None, 'failed', '', ''
        try:
            if query.lower() == '/old':
                reply = await self.images.lookup(message) if kind == 'group' else '请在群内引用图片消息并发送 /old。'
            elif not query or query.lower() in ('帮助', '/帮助', '/help', 'help', '/start'):
                reply = HELP
                if kind=='c2c':reply+='\n\n私聊维护：\n/modify 知识库 修改要求\n/modify qa 修改要求\n/add 商品库 商品信息\n/退出 结束维护（仅授权账号可写入）'
            elif kind=='c2c' and (re.match(r'^/(?:modify|add)(?:\s|$)',query,re.I) or query in ('/退出','/cancel') or self.seen.maintenance_active(session)):
                if len(query)>2000:reply='指令请控制在2000字以内。'
                else:
                    async with self.capacity:
                        trace=await self.retriever.maintain(query,getattr(message.author,'user_openid',''),message.id)
                    reply=plain(trace['answer'])[:1700]
                    self.seen.set_maintenance(session,trace.get('active',False))
            elif kind=='group' and re.match(r'^/(?:modify|add)(?:\s|$)',query,re.I):
                reply='维护指令请私聊机器人发送，仅已授权账号可使用。'
            elif query in ('/新对话', '/清空上下文'):
                self.seen.clear_history(session)
                if kind == 'group':
                    self.seen.clear_group_context(getattr(message, 'group_openid', ''))
                reply = '已清空当前对话的上下文，我们重新开始。'
            elif query in MEMORY_COMMANDS:
                author = getattr(message, 'author', None)
                meta = {'origin': 'qq_group' if kind == 'group' else 'qq_private',
                        'user_id': getattr(author, 'member_openid' if kind == 'group' else 'user_openid', ''),
                        'group_id': group_id}
                actions = {'/记忆': 'list', '/清除记忆': 'clear', '/关闭记忆': 'disable', '/开启记忆': 'enable'}
                try:
                    result = await self.retriever.memory(actions[query], meta)
                    if query == '/记忆':
                        if result.get('enabled') is False:
                            reply = '长期记忆当前已关闭。发送 /开启记忆 可恢复。'
                        elif result.get('items'):
                            items = result['items'][:24]
                            reply = '当前长期记忆：\n' + '\n'.join(f'• {plain(item)}' for item in items)
                        else:
                            reply = '目前还没有保存长期记忆。你可以在对话中说“记住……”来保存。'
                    elif query == '/清除记忆':
                        reply = '已清除' + str(result.get('deleted', 0)) + '条长期记忆。'
                    elif query == '/关闭记忆':
                        reply = '已暂停长期记忆；已有内容会保留但不再读取或更新。'
                    else:
                        reply = '已开启长期记忆。'
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                    reply = '记忆服务暂时不可用，请稍后重试。'
            elif query in ('/身份', '/whoami'):
                if kind == 'group':
                    reply = f'群 OpenID：{plain(message.group_openid)}\n你的成员 OpenID：{plain(message.author.member_openid)}\n请由管理员在知识库后台填写人工联系人。此命令不会自动赋予管理员身份。'
                else:
                    reply = '你的私聊 OpenID：'+plain(getattr(message.author,'user_openid',''))+'\n可由 owner 在知识库后台配置更新通知。此命令不会自动绑定身份。'
            elif len(query) > 2000:
                reply = '问题有点长，请缩短到 2000 字以内。'
            elif self.capacity.locked():
                reply = '当前检索人数较多，请稍后重新发送问题。'
            else:
                async with self.capacity:
                    author = getattr(message, 'author', None)
                    meta = {'origin': 'qq_group' if kind == 'group' else 'qq_private',
                            'user_id': getattr(author, 'member_openid' if kind == 'group' else 'user_openid', ''), 'session_id': session}
                    if reply_reference:meta['reply_reference']=reply_reference
                    if self.seen.last_sticker_sent(session):meta['previous_sticker_sent']=True
                    history = [] if kind == 'group' else self.seen.history(session)
                    group_context = self.seen.group_history(group_id, message.id) if kind == 'group' else None
                    if kind == 'group' and group_id:
                        trace = await self.retriever.search(query, group_id=group_id, history=history,
                            trace_meta=meta, group_context=group_context)
                    else:
                        trace = await self.retriever.search(query, group_id=group_id, history=history, trace_meta=meta)
                    reply = format_reply(trace, kind)
                    remember = trace.get('mode') != 'quota'
            response = await self.send_answer(message, kind, reply, trace, session)
            if not response:
                raise RuntimeError('QQ empty response')
            delivery, sent = 'delivered', (trace or {}).get('_sent_text',reply)
            if remember:
                history_reply=sent
                if (trace or {}).get('sticker_delivery',{}).get('status')=='sent':
                    history_reply+='['+trace['sticker']['name']+']'
                self.seen.remember(session, ('引用内容：'+reply_reference+'\n本次问题：' if reply_reference else '')+query, history_reply)
                if kind == 'group':
                    group_reply_id = response.get('id') if isinstance(response, dict) else getattr(response, 'id', '')
                    self.seen.remember_group_reply(getattr(message, 'group_openid', ''),
                        group_reply_id or ('bot:' + str(message.id)), sent)
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
            for key in ('QQ_APP_SECRET', 'KB_READ_TOKEN', 'KB_LEARN_TOKEN'):
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
