"""QQ knowledge retrieval demo using Tencent's qq-botpy 1.2.1."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import random
import re
import sqlite3
import ssl
import time
from datetime import datetime, timedelta, timezone
import unicodedata

import aiohttp
from aiohttp import web
import botpy
import botpy.gateway
import botpy.http
from botpy.http import Route
from learner import Learner
from image_history import ImageHistory, read_image_data_url
from group_summary import GroupSummary, timestamp as summary_timestamp
import compat

LOG = logging.getLogger('knowledge-bot')
MEMORY_COMMANDS = ('/记忆', '/清除记忆', '/关闭记忆', '/开启记忆')
DRINK_COMMAND = '/今天喝什么'
CREATIVE_COMMANDS = {'/对联': 'couplet', '/俳句': 'haiku'}
GROUP_COMMANDS = ('/old', '/总结', '/新对话', '/清空上下文', DRINK_COMMAND,
                  *MEMORY_COMMANDS, *CREATIVE_COMMANDS)
WELCOME_MESSAGE = '欢迎加入午觉糖水铺～我是小铺的社团娘兼客服机器人，主要陪大家聊《蔚蓝档案》同人周边。\n群规与制品相关请先阅读群公告哦～\n棉花娃娃征集请看群公告～\nkei娃开放全款预约中～\n商品、订单或其他问题都可以直接 @ 我提问哦，我会尽力帮忙！'
HELP = '群内发送 /总结：总结上次成功总结后、最近10小时、最多400条和15000字以内的聊天。\n群聊或私聊输入 /今天喝什么：结合杭州天气给出两款饮品候选。\n发送 /对联 +上联：请我创作下联；发送 /俳句 +一句话：请我写一首中文俳句。引用消息时优先使用引用内容。\n群内引用图片发送 /old，可查询本群记录次数、首次发送者和时间。\n长期记忆：群内共享、私聊按用户独立；发送 /记忆 查看，/清除记忆 删除，/关闭记忆 暂停，/开启记忆 恢复。\n我是午觉糖水铺的社团娘兼客服机器人，主要陪大家聊《蔚蓝档案》同人周边；有问题在群里直接 @ 我提问哦～\n直接发送问题，或输入：/检索 你的问题\n我会根据知识库资料回答，资料不足时请群主或管理员确认。模型不可用时返回最相关文档。\n群聊回答默认参考本群最近10条发言；同一私聊保留最近30分钟的问答，可发送 /新对话 清空。\n每天共40次咨询/创作额度，群聊和私聊共享，北京时间零点恢复。\n管理员可在群内发送 /身份，获取后台人工接管配置需要的 OpenID。'
SEXUAL_HARASSMENT_TERMS = (
    '🐍米青', '中出', '射你', '操你', '干你', '草你', '肏你', '强奸你',
    '上你', '日你', '睡你', '插你', '摸你胸', '摸你下面', '舔你', '和你做爱',
    '跟你做爱', '约你炮',
)
HARASSMENT_WINDOW_SECONDS = 10 * 60
HARASSMENT_WARNING_AFTER = 3
HARASSMENT_WARNING_MESSAGE = '请不要对机器人进行性骚扰哦～这是一次提醒，请友善交流。'
HARASSMENT_MUTE_THRESHOLD = 3
HARASSMENT_MUTE_DURATION_MINUTES = 10
AGENT_REQUEST_TIMEOUT = 90  # Knowledge agent: 75s including a reserved final answer; leave transport headroom.


def sexual_harassment_matches(content):
    return sensitive_word_matches(content, SEXUAL_HARASSMENT_TERMS)


def normalize(text):
    text = compat.strip_mention_tags(text or '').strip()
    return re.sub(r'^/(?:检索|搜索|search)(?:\s+|$)', '', text, flags=re.I).strip()


def normalize_model_text(text, message, name_resolver=None):
    if not isinstance(text, str):
        return ''
    mention_tag = r'(?:<@!?[A-Za-z0-9_-]{1,128}>|<qqbot-at-user\s+id="[A-Za-z0-9_-]{1,128}"\s*/>)'
    text = re.sub(r'^\s*(?:' + mention_tag + r'\s*)*/(?:检索|搜索|search)(?:\s+|$)', '', text, flags=re.I)
    return re.sub(r'\s+', ' ', compat.render_mention_tags(text, message, name_resolver)).strip()


def creative_request(text):
    """Parse a couplet/haiku request and its optional inline source text."""
    match = re.match(r'^/(对联|俳句)(?:\s*[+＋]\s*|\s+|$)(.*)$', text or '', re.S)
    if not match:
        return None
    return {'style': CREATIVE_COMMANDS['/' + match.group(1)], 'text': match.group(2).strip()}


def is_group_command(text):
    normalized = normalize(text).lower()
    return normalized in GROUP_COMMANDS or creative_request(normalized) is not None


BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def stable_drink_seed(user_openid, today=None):
    if not user_openid:
        return None
    day = today or datetime.now(BEIJING_TIMEZONE).date().isoformat()
    identity = f'{user_openid}\0{day}'.encode('utf-8')
    return int.from_bytes(hashlib.sha256(identity).digest(), 'big')


EMOJI_BASE_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2),
    (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE),
    (0x2600, 0x2604), (0x2614, 0x2615), (0x2622, 0x2623), (0x2626, 0x2626),
    (0x262A, 0x262A), (0x262E, 0x262F), (0x2638, 0x263A), (0x2640, 0x2640),
    (0x2642, 0x2642), (0x2648, 0x2653), (0x265F, 0x2660), (0x2663, 0x2663),
    (0x2665, 0x2666), (0x2668, 0x2668), (0x267B, 0x267B), (0x267E, 0x267F),
    (0x2692, 0x2697), (0x2699, 0x2699), (0x269B, 0x269C), (0x26A0, 0x26A1),
    (0x26A7, 0x26A7), (0x26AA, 0x26AB), (0x26B0, 0x26B1), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26C8, 0x26C8), (0x26CE, 0x26CF), (0x26D1, 0x26D1),
    (0x26D3, 0x26D4), (0x26E9, 0x26EA), (0x26F0, 0x26F5), (0x26F7, 0x26FA),
    (0x26FD, 0x26FD), (0x2702, 0x2702), (0x2705, 0x2705), (0x2708, 0x270D),
    (0x270F, 0x270F), (0x2712, 0x2712), (0x2714, 0x2714), (0x2716, 0x2716),
    (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734),
    (0x2744, 0x2744), (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E),
    (0x2753, 0x2755), (0x2757, 0x2757), (0x2763, 0x2767), (0x2795, 0x2797),
    (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
    (0x1F000, 0x1FAFF),
)
EMOJI_KEYCAP = re.compile(r'[#*0-9]\ufe0f?\u20e3')


def emoji_character_indexes(text):
    """Return codepoint positions belonging to common Unicode emoji sequences."""
    bases = {index for index, char in enumerate(text)
             if any(start <= ord(char) <= end for start, end in EMOJI_BASE_RANGES)}
    emoji = set(bases)
    for match in EMOJI_KEYCAP.finditer(text):
        emoji.update(range(match.start(), match.end()))
    for index, char in enumerate(text):
        codepoint = ord(char)
        if codepoint in (0xFE0E, 0xFE0F) and index and index - 1 in bases:
            emoji.add(index)
        elif 0xE0020 <= codepoint <= 0xE007F:
            emoji.add(index)
        elif codepoint == 0x200D:
            left = index - 1
            while left >= 0 and (ord(text[left]) in (0xFE0E, 0xFE0F)
                                 or 0x1F3FB <= ord(text[left]) <= 0x1F3FF):
                left -= 1
            right = index + 1
            while right < len(text) and (ord(text[right]) in (0xFE0E, 0xFE0F)
                                         or 0x1F3FB <= ord(text[right]) <= 0x1F3FF):
                right += 1
            if left in bases and right in bases:
                emoji.add(index)
    return emoji


def moderation_keyword_profile(text):
    normalized = unicodedata.normalize('NFKC', str(text or '')).casefold()
    if emoji_character_indexes(normalized):
        # Emoji rules preserve emoji, while punctuation and unrelated symbols are ignored.
        return 'emoji', frozenset()
    symbols = frozenset(char for char in normalized
                        if unicodedata.category(char)[0] in ('P', 'S'))
    return ('symbols', symbols) if symbols else ('plain', frozenset())


MODERATION_URL = re.compile(
    r'(?<![a-z0-9+.-])(?:[a-z][a-z0-9+.-]*://|www\.|//(?=[a-z0-9.-]+\.[a-z]{2,}(?:[/:?#\s]|$)))\S+', re.I)


def moderation_text_parts(text):
    # Match full-width URL spellings too. Keep a boundary where each URL was
    # removed, and mention IDs cannot join the text around a mention tag.
    normalized = unicodedata.normalize('NFKC', str(text or ''))
    return [part for segment in compat.split_mention_tags(normalized)
            for part in MODERATION_URL.split(segment)]


def strip_moderation_urls(text):
    """Ignore URLs so random IDs and signed attachment tokens cannot trigger rules."""
    return ' '.join(moderation_text_parts(text))


def normalize_moderation_text(text, profile=None):
    normalized = unicodedata.normalize('NFKC', str(text or '')).casefold()
    mode, symbols = profile or moderation_keyword_profile(normalized)
    emoji_indexes = emoji_character_indexes(normalized)
    result = []
    for index, char in enumerate(normalized):
        category = unicodedata.category(char)[0]
        if index in emoji_indexes:
            if mode == 'emoji':
                result.append(char)
        elif category in ('L', 'N', 'M'):
            result.append(char)
        elif mode == 'symbols' and category in ('P', 'S') and char in symbols:
            result.append(char)
    return ''.join(result)


def sensitive_word_matches(content, words, expansions=None):
    parts = moderation_text_parts(content)
    matches = []
    text_by_profile = {}
    for index, word in enumerate(words):
        if not isinstance(word, str) or not word:
            continue
        variants = (expansions[index] if isinstance(expansions, list) and index < len(expansions)
                    and isinstance(expansions[index], list) else [word])
        matched = False
        for variant in variants:
            if not isinstance(variant, str):
                continue
            profile = moderation_keyword_profile(variant)
            normalized_variant = normalize_moderation_text(variant, profile)
            if not normalized_variant:
                continue
            if profile not in text_by_profile:
                text_by_profile[profile] = [normalize_moderation_text(part, profile) for part in parts]
            if any(normalized_variant in text for text in text_by_profile[profile]):
                matched = True
                break
        if matched:
            matches.append(word)
    return matches


def reply_image_attachment(message):
    """Return one ephemeral image from the current or quoted group message."""
    sources = (getattr(message, 'attachments', []) or [],
               getattr(message, 'sweet_quoted_images', []) or [])
    for attachments in sources:
        if not isinstance(attachments, (list, tuple)):
            continue
        for attachment in attachments:
            content_type = (attachment.get('content_type', '') if isinstance(attachment, dict)
                            else getattr(attachment, 'content_type', ''))
            url = attachment.get('url', '') if isinstance(attachment, dict) else getattr(attachment, 'url', '')
            if str(content_type).lower().split('/')[0] == 'image' and isinstance(url, str) and url:
                return attachment
    return None


def plain(text):
    # Prevent retrieved text from becoming QQ mentions / message markup.
    return str(text).replace('@', '＠').replace('<', '＜').replace('>', '＞').replace('\x00', '')


def clean_group_context(text, message=None, name_resolver=None):
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
    text = compat.render_mention_tags(text, message, name_resolver) if message is not None else text
    text = compat.strip_mention_tags(text)
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
        self.conn.execute('''CREATE TABLE IF NOT EXISTS moderation_recall_outbox (
            event_hash TEXT PRIMARY KEY, terms TEXT NOT NULL, created_at REAL NOT NULL,
            candidates TEXT NOT NULL DEFAULT '[]', trace_meta TEXT NOT NULL DEFAULT '{}')''')
        outbox_columns = {row[1] for row in self.conn.execute('PRAGMA table_info(moderation_recall_outbox)')}
        if 'candidates' not in outbox_columns:
            self.conn.execute("ALTER TABLE moderation_recall_outbox ADD COLUMN candidates TEXT NOT NULL DEFAULT '[]'")
        if 'trace_meta' not in outbox_columns:
            self.conn.execute("ALTER TABLE moderation_recall_outbox ADD COLUMN trace_meta TEXT NOT NULL DEFAULT '{}'")
        self.conn.execute('''CREATE TABLE IF NOT EXISTS harassment_strikes (
            identity_hash TEXT NOT NULL, event_hash TEXT NOT NULL, at REAL NOT NULL,
            PRIMARY KEY(identity_hash,event_hash))''')
        self.conn.execute('CREATE INDEX IF NOT EXISTS harassment_strikes_time ON harassment_strikes(at)')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS harassment_warning_attempts (
            identity_hash TEXT PRIMARY KEY, at REAL NOT NULL)''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS harassment_mute_attempts (
            identity_hash TEXT PRIMARY KEY, at REAL NOT NULL)''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS group_context_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
            member_id TEXT NOT NULL, member_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
            content TEXT NOT NULL, at REAL NOT NULL,
            UNIQUE(group_id,message_id))''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS group_member_names (
            group_id TEXT NOT NULL, member_id TEXT NOT NULL, first_name TEXT NOT NULL,
            first_seen REAL NOT NULL, PRIMARY KEY(group_id,member_id))''')
        context_columns = {row[1] for row in self.conn.execute('PRAGMA table_info(group_context_messages)')}
        if 'member_name' not in context_columns:
            self.conn.execute("ALTER TABLE group_context_messages ADD COLUMN member_name TEXT NOT NULL DEFAULT ''")
        self.conn.execute('CREATE INDEX IF NOT EXISTS dialogue_session ON dialogue(session,id)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS dialogue_time ON dialogue(at)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS group_context_time ON group_context_messages(group_id,at,id)')
        self.conn.execute('''INSERT OR IGNORE INTO group_member_names(group_id,member_id,first_name,first_seen)
            SELECT g.group_id,g.member_id,g.member_name,g.at FROM group_context_messages g
            WHERE g.member_name<>'' AND g.id=(SELECT old.id FROM group_context_messages old
                WHERE old.group_id=g.group_id AND old.member_id=g.member_id AND old.member_name<>''
                ORDER BY old.at,old.id LIMIT 1)''')
        self.conn.commit()

    def claim(self, key):
        with self.conn:
            self.conn.execute('DELETE FROM seen WHERE expires<?', (time.time(),))
            return self.conn.execute('INSERT OR IGNORE INTO seen VALUES(?,?)', (key, time.time() + 86400)).rowcount == 1

    def record_moderation_recall(self, event_hash, terms, candidates=(), trace_meta=None):
        with self.conn:
            self.conn.execute('''INSERT OR IGNORE INTO moderation_recall_outbox
                (event_hash,terms,created_at,candidates,trace_meta) VALUES(?,?,?,?,?)''',
                (event_hash, json.dumps(terms, ensure_ascii=False), time.time(),
                 json.dumps(list(candidates), ensure_ascii=False),
                 json.dumps(trace_meta or {}, ensure_ascii=False)))

    def pending_moderation_recalls(self, limit=20):
        rows = self.conn.execute('SELECT event_hash,terms,candidates,trace_meta FROM moderation_recall_outbox ORDER BY created_at LIMIT ?',
                                 (limit,)).fetchall()
        return [(row[0], json.loads(row[1]), json.loads(row[2]), json.loads(row[3])) for row in rows]

    def acknowledge_moderation_recall(self, event_hash):
        with self.conn:
            self.conn.execute('DELETE FROM moderation_recall_outbox WHERE event_hash=?', (event_hash,))

    def record_harassment_strike(self, identity_hash, event_hash, at=None):
        at = time.time() if at is None else at
        with self.conn:
            self.conn.execute('DELETE FROM harassment_strikes WHERE at<?', (at-86400,))
            self.conn.execute('INSERT OR IGNORE INTO harassment_strikes VALUES(?,?,?)',
                              (identity_hash, event_hash, at))
            return self.conn.execute('SELECT count(*) FROM harassment_strikes WHERE identity_hash=? AND at>?',
                                     (identity_hash, at-HARASSMENT_WINDOW_SECONDS)).fetchone()[0]

    def claim_harassment_warning(self, identity_hash, at=None):
        at = time.time() if at is None else at
        with self.conn:
            row = self.conn.execute('SELECT at FROM harassment_warning_attempts WHERE identity_hash=?',
                                    (identity_hash,)).fetchone()
            if row and row[0] > at-60:
                return False
            self.conn.execute('INSERT OR REPLACE INTO harassment_warning_attempts VALUES(?,?)',
                              (identity_hash, at))
            return True

    def claim_harassment_mute(self, identity_hash, at=None):
        at = time.time() if at is None else at
        with self.conn:
            row = self.conn.execute('SELECT at FROM harassment_mute_attempts WHERE identity_hash=?',
                                    (identity_hash,)).fetchone()
            if row and row[0] > at-60:
                return False
            self.conn.execute('INSERT OR REPLACE INTO harassment_mute_attempts VALUES(?,?)',
                              (identity_hash, at))
            return True

    def clear_harassment_strikes(self, identity_hash):
        with self.conn:
            self.conn.execute('DELETE FROM harassment_strikes WHERE identity_hash=?', (identity_hash,))


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
        author = getattr(message, 'author', None)
        member = getattr(author, 'member_openid', '')
        member_name = compat.sender_name(message)
        if group and member and member_name:
            with self.conn:
                self.conn.execute('''INSERT OR IGNORE INTO group_member_names
                    (group_id,member_id,first_name,first_seen) VALUES(?,?,?,?)''',
                    (group, member, member_name[:12], summary_timestamp(message)))
        resolver = lambda member_id: self.first_member_name(group, member_id)
        content = clean_group_context(getattr(message, 'content', '') or '', message, resolver)
        if not group or not message_id or not member or not content:
            return
        at = summary_timestamp(message)
        with self.conn:
            self.conn.execute('DELETE FROM group_context_messages WHERE at<?', (time.time()-7*86400,))
            self.conn.execute('''INSERT OR IGNORE INTO group_context_messages
                (group_id,message_id,member_id,member_name,role,content,at) VALUES(?,?,?,?,'user',?,?)''',
                (group, str(message_id), member, member_name, content, at))
            self.conn.execute('''DELETE FROM group_context_messages WHERE group_id=? AND id NOT IN
                (SELECT id FROM group_context_messages WHERE group_id=? ORDER BY at DESC,id DESC LIMIT 400)''', (group, group))

    def first_member_name(self, group, member):
        if not group or not member:
            return ''
        row = self.conn.execute('SELECT first_name FROM group_member_names WHERE group_id=? AND member_id=?',
                                (group, member)).fetchone()
        return row[0] if row else ''

    def remember_group_reply(self, group, message_id, content):
        content = clean_group_context(content)
        if not group or not message_id or not content:
            return
        with self.conn:
            self.conn.execute('DELETE FROM group_context_messages WHERE at<?', (time.time()-7*86400,))
            self.conn.execute('''INSERT OR IGNORE INTO group_context_messages
                (group_id,message_id,member_id,member_name,role,content,at) VALUES(?,?,'bot','机器人','assistant',?,?)''',
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
        rows = self.conn.execute(f'''SELECT member_id,member_name,role,content,at FROM group_context_messages
            WHERE group_id=? AND {where} ORDER BY at DESC,id DESC LIMIT ?''', params).fetchall()
        rows.reverse()
        labels, result = {}, []
        for row in rows:
            member_id, member_name, role, content, created_at = row
            if role == 'assistant':
                speaker = '机器人'
            else:
                if member_name:
                    labels[member_id] = member_name
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
        self.moderation_token = os.environ.get('KB_MODERATION_TOKEN', '')
        self._moderation_words = []
        self._moderation_settings = {'sensitive_words': [], 'sensitive_word_expansions': [],
                                     'harassment_warning_enabled': False,
                                     'harassment_mute_enabled': False,
                                     'harassment_mute_threshold': HARASSMENT_MUTE_THRESHOLD,
                                     'harassment_mute_duration_minutes': HARASSMENT_MUTE_DURATION_MINUTES}
        self._moderation_expires = 0.0
        self._moderation_lock = asyncio.Lock()
        self._drink_menu = []
        self._drink_menu_expires = 0.0
        self._drink_menu_lock = asyncio.Lock()
        self._drink_weather = None
        self._drink_weather_expires = 0.0
        self._drink_weather_lock = asyncio.Lock()

    async def search(self, query, group_id='', history=None, trace_meta=None, group_context=None,
                     image_data_urls=None):
        payload = {'kb_id': self.kb_id, 'query': query, 'group_id': group_id,
                   'history': history or [], **(trace_meta or {})}
        if group_context is not None:
            payload['group_context'] = group_context
        if image_data_urls:
            payload['image_data_urls'] = image_data_urls
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AGENT_REQUEST_TIMEOUT)) as session:
            async with session.post(self.url, headers={'Authorization': 'Bearer ' + self.token},
                                    json=payload) as response:
                if response.status != 200:
                    raise RuntimeError(f'Knowledge HTTP {response.status}')
                return await response.json()

    async def creative(self, style, query, trace_meta=None, image_data_urls=None):
        payload = {'kb_id': self.kb_id, 'style': style, 'query': query,
                   **(trace_meta or {})}
        if image_data_urls:
            payload['image_data_urls'] = image_data_urls
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AGENT_REQUEST_TIMEOUT)) as session:
            async with session.post(self.url.rsplit('/', 1)[0] + '/creative',
                                    headers={'Authorization': 'Bearer ' + self.token},
                                    json=payload) as response:
                if response.status != 200:
                    raise RuntimeError(f'Creative HTTP {response.status}')
                return await response.json()

    async def memory(self, action, meta):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(self.api_root + '/agent/memory',
                headers={'Authorization': 'Bearer ' + self.token},
                json={'kb_id': self.kb_id, 'action': action, **meta}) as response:
                if response.status != 200:
                    raise RuntimeError('Memory HTTP ' + str(response.status))
                return await response.json()


    async def summarize(self, transcript, group_id='', members=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=55)) as session:
            async with session.post(self.api_root + '/group-summary',
                headers={'Authorization': 'Bearer ' + self.token},
                json={'kb_id': self.kb_id, 'group_id': group_id, 'transcript': transcript, 'members': members or []}) as response:
                if response.status != 200:
                    raise RuntimeError('Summary HTTP ' + str(response.status))
                return await response.json()

    async def group_welcome(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(self.api_root + '/group-welcome',
                headers={'Authorization': 'Bearer ' + self.token}) as response:
                if response.status != 200:
                    raise RuntimeError('Group welcome HTTP ' + str(response.status))
                data = await response.json()
        welcome = data.get('welcome') if isinstance(data, dict) else None
        if not isinstance(welcome, str) or not welcome.strip():
            raise RuntimeError('Invalid group welcome')
        return plain(welcome.strip())[:1000]

    async def drink_menu(self):
        now = time.monotonic()
        if now < self._drink_menu_expires:
            return [dict(item) for item in self._drink_menu]
        async with self._drink_menu_lock:
            now = time.monotonic()
            if now < self._drink_menu_expires:
                return [dict(item) for item in self._drink_menu]
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(self.api_root + '/drink-menu',
                    headers={'Authorization': 'Bearer ' + self.token}) as response:
                    if response.status != 200:
                        raise RuntimeError('Drink menu HTTP ' + str(response.status))
                    data = await response.json()
            items = data.get('items') if isinstance(data, dict) else None
            if (not isinstance(items, list) or len(items) > 300
                    or any(not isinstance(item, dict)
                           or not isinstance(item.get('brand'), str) or not item['brand'].strip()
                           or len(item['brand']) > 40
                           or not isinstance(item.get('product'), str) or not item['product'].strip()
                           or len(item['product']) > 80 for item in items)):
                raise RuntimeError('Invalid drink menu')
            self._drink_menu = [{'brand': item['brand'].strip(), 'product': item['product'].strip(),
                                 'temperature': item.get('temperature', 'both')}
                                for item in items]
            self._drink_menu_expires = time.monotonic() + 30
            return [dict(item) for item in self._drink_menu]

    async def drink_weather(self):
        now = time.monotonic()
        if now < self._drink_weather_expires:
            return dict(self._drink_weather) if self._drink_weather else None
        async with self._drink_weather_lock:
            now = time.monotonic()
            if now < self._drink_weather_expires:
                return dict(self._drink_weather) if self._drink_weather else None
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7)) as session:
                    async with session.get(self.api_root + '/drink-weather',
                        headers={'Authorization': 'Bearer ' + self.token}) as response:
                        if response.status != 200:
                            raise RuntimeError('Drink weather HTTP ' + str(response.status))
                        data = await response.json()
                if (not isinstance(data, dict) or data.get('available') is not True
                        or data.get('temperature_preference') not in ('cold', 'hot', 'random')
                        or not isinstance(data.get('today_max'), (int, float))):
                    self._drink_weather = None
                else:
                    self._drink_weather = data
            except Exception as exc:
                LOG.warning('DRINK_WEATHER_UNAVAILABLE error=%s', type(exc).__name__)
                self._drink_weather = None
            self._drink_weather_expires = time.monotonic() + 60
            return dict(self._drink_weather) if self._drink_weather else None

    async def moderation_settings(self):
        if not self.moderation_token:
            return dict(self._moderation_settings)
        now = time.monotonic()
        if now < self._moderation_expires:
            return dict(self._moderation_settings)
        async with self._moderation_lock:
            now = time.monotonic()
            if now < self._moderation_expires:
                return dict(self._moderation_settings)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                    async with session.get(self.api_root + '/moderation-settings',
                        headers={'Authorization': 'Bearer ' + self.moderation_token}) as response:
                        if response.status != 200:
                            raise RuntimeError('Moderation settings HTTP ' + str(response.status))
                        data = await response.json()
                words = data.get('sensitive_words') if isinstance(data, dict) else None
                if not isinstance(words, list) or any(not isinstance(word, str) for word in words):
                    raise RuntimeError('Invalid moderation settings')
                words = [word.strip() for word in words if word.strip()]
                expansions = data.get('sensitive_word_expansions')
                if expansions is None:
                    # Older knowledge services send only literal terms.
                    expansions = [[word] for word in words]
                if (not isinstance(expansions, list) or len(expansions) != len(words)
                        or any(not isinstance(group, list) or not group or len(group) > 256
                               or any(not isinstance(term, str) or not term or len(term) > 80 for term in group)
                               for group in expansions)
                        or sum(len(group) for group in expansions) > 2000):
                    raise RuntimeError('Invalid sensitive word expansions')
                self._moderation_words = words
                warning_enabled = data.get('harassment_warning_enabled', True)
                if type(warning_enabled) is not bool:
                    raise RuntimeError('Invalid harassment warning setting')
                mute_enabled = data.get('harassment_mute_enabled', False)
                mute_threshold = data.get('harassment_mute_threshold', HARASSMENT_MUTE_THRESHOLD)
                mute_minutes = data.get('harassment_mute_duration_minutes', HARASSMENT_MUTE_DURATION_MINUTES)
                if (type(mute_enabled) is not bool or type(mute_threshold) is not int
                        or not 1 <= mute_threshold <= 20 or type(mute_minutes) is not int
                        or not 1 <= mute_minutes <= 1440):
                    raise RuntimeError('Invalid harassment mute setting')
                self._moderation_settings = {'sensitive_words': list(self._moderation_words),
                                             'sensitive_word_expansions': expansions,
                                             'harassment_warning_enabled': warning_enabled,
                                             'harassment_mute_enabled': mute_enabled,
                                             'harassment_mute_threshold': mute_threshold,
                                             'harassment_mute_duration_minutes': mute_minutes}
                self._moderation_expires = time.monotonic() + 15
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                LOG.warning('MODERATION_SETTINGS_FAILED error=%s', type(exc).__name__)
                self._moderation_expires = time.monotonic() + 5
            return dict(self._moderation_settings)

    async def moderation_words(self):
        return list((await self.moderation_settings()).get('sensitive_words', []))

    async def report_moderation_recall(self, event_hash, terms, candidates=(), trace_meta=None):
        if not self.moderation_token:
            return False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(self.api_root + '/moderation-recalls',
                    headers={'Authorization': 'Bearer ' + self.moderation_token},
                    json={'event_hash': event_hash, 'terms': terms, 'candidates': list(candidates),
                          'trace_meta': trace_meta or {}}) as response:
                    if response.status != 200:
                        LOG.warning('MODERATION_REPORT_FAILED status=%s', response.status)
                        return False
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            LOG.warning('MODERATION_REPORT_FAILED error=%s', type(exc).__name__)
            return False

    async def maintain(self, query, user_id, message_id):
        token=os.environ.get('KB_LEARN_TOKEN','')
        if not token:return {'answer':'维护通道尚未配置，请到知识库后台处理。','active':False}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AGENT_REQUEST_TIMEOUT)) as session:
            async with session.post(self.api_root + '/agent/private-maintenance',
                headers={'Authorization':'Bearer '+token},json={'kb_id':self.kb_id,'query':query,'user_id':user_id,'message_id':message_id}) as response:
                if response.status!=200:raise RuntimeError('Maintenance HTTP '+str(response.status))
                return await response.json()

    async def sync_announcement(self, content, user_id, message_id):
        token = os.environ.get('KB_LEARN_TOKEN', '')
        if not token:
            return {'answer': '公告同步通道尚未配置，请联系管理员。', 'ok': False}

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:

            async with session.post(self.api_root + '/private-announcement',
                headers={'Authorization': 'Bearer ' + token},
                json={'kb_id': self.kb_id, 'content': content, 'user_id': user_id,
                      'message_id': message_id}) as response:
                if response.status != 200:
                    raise RuntimeError('Announcement sync HTTP ' + str(response.status))
                result = await response.json()
        if not isinstance(result, dict):
            raise RuntimeError('Invalid announcement sync response')
        return result

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
        super().__init__(intents=botpy.Intents(public_messages=True, group_member_event=True), timeout=15,
                         log_level=logging.INFO, ext_handlers=False, **kwargs)
        self.retriever, self.seen = retriever, seen
        self.images = ImageHistory(seen.conn)
        self.summaries = GroupSummary(seen.conn)
        self.capacity = asyncio.Semaphore(4)
        self.conversations = {}
        self.learner = None
        self._moderation_recall_lock = asyncio.Lock()
        self._moderation_report_lock = asyncio.Lock()
        self._last_moderation_recall = 0.0
        self._moderation_report_task = None
        self._moderation_http_task = None
        self._moderation_http_runner = None
        if os.environ.get("KB_LEARN_TOKEN"):
            self.learner = Learner(seen.conn, retriever.api_root+"/learning/events", os.environ["KB_LEARN_TOKEN"], retriever.kb_id)

    async def on_ready(self):
        if self.learner:
            self.learner.api=self.api
            self.learner.start()
        if os.environ.get('KB_MODERATION_TOKEN') and self._moderation_report_task is None:
            self._moderation_report_task = asyncio.create_task(self._moderation_report_loop())
            self._moderation_http_task = asyncio.create_task(self.start_moderation_control_server())
        LOG.info('QQ_CONNECTED app_id=%s knowledge_id=%s', os.environ.get('QQ_APP_ID'), self.retriever.kb_id)

    async def on_error(self, event_method, *args, **kwargs):
        LOG.error('EVENT_ERROR event=%s', event_method)

    async def start_moderation_control_server(self):
        runner = web.AppRunner(self.moderation_control_application(), access_log=None)
        try:
            await runner.setup()
            port = int(os.environ.get('QQ_MODERATION_PORT', '8766'))
            if not 1024 <= port <= 65535:
                raise ValueError('QQ_MODERATION_PORT must be between 1024 and 65535')
            await web.TCPSite(runner, '127.0.0.1', port).start()
            self._moderation_http_runner = runner
            LOG.info('QQ_MODERATION_CONTROL_READY port=%s', port)
        except Exception as exc:
            await runner.cleanup()
            LOG.error('QQ_MODERATION_CONTROL_FAILED error=%s', type(exc).__name__)

    def moderation_control_application(self):
        app = web.Application(client_max_size=8192)
        app.router.add_post('/mcp/recall', self.mcp_recall_group_message)
        app.router.add_post('/mcp/warn', self.mcp_send_group_warning)
        return app

    def moderation_request_authorized(self, request):
        expected = os.environ.get('KB_MODERATION_TOKEN', '')
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())

    async def mcp_recall_group_message(self, request):
        if not self.moderation_request_authorized(request):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
            result = await self.recall_group_message(data.get('group_id'), data.get('message_id'))
            return web.json_response(result)
        except (ValueError, AttributeError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except Exception as exc:
            LOG.warning('MCP_GROUP_RECALL_FAILED error=%s', type(exc).__name__)
            return web.json_response({'error': 'QQ 撤回接口调用失败'}, status=502)

    async def mcp_send_group_warning(self, request):
        if not self.moderation_request_authorized(request):
            raise web.HTTPUnauthorized()
        try:
            settings_reader = getattr(self.retriever, 'moderation_settings', None)
            if not callable(settings_reader):
                return web.json_response({'sent': False, 'reason': 'harassment_warning_disabled'}, status=409)
            settings = await settings_reader()
            if not isinstance(settings, dict) or settings.get('harassment_warning_enabled') is not True:
                return web.json_response({'sent': False, 'reason': 'harassment_warning_disabled'}, status=409)
            data = await request.json()
            result = await self.send_group_warning(data.get('group_id'), data.get('message_id'))
            return web.json_response(result)
        except (ValueError, AttributeError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except Exception as exc:
            LOG.warning('MCP_GROUP_WARNING_FAILED error=%s', type(exc).__name__)
            return web.json_response({'error': 'QQ 群提醒发送失败'}, status=502)

    async def recall_group_message(self, group, message_id):
        if not isinstance(group, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', group):
            raise ValueError('group_id 必须是有效群 OpenID')
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_.!:-]{1,200}', message_id):
            raise ValueError('message_id 格式不正确')
        sandbox = os.environ.get('QQ_SANDBOX', '').lower() in ('1', 'true', 'yes')
        route = Route('DELETE', '/v2/groups/{group_openid}/messages/{message_id}',
                      is_sandbox=sandbox, group_openid=group, message_id=message_id)
        async with self._moderation_recall_lock:
            delay = 0.11 - (time.monotonic() - self._last_moderation_recall)
            if delay > 0:
                await asyncio.sleep(delay)
            await self.api._http.request(route)
            self._last_moderation_recall = time.monotonic()
        return {'recalled': True, 'group_id': group, 'message_id': message_id}

    async def mute_group_member(self, group, member, duration_minutes):
        if not isinstance(group, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', group):
            raise ValueError('group_id 必须是有效群 OpenID')
        if not isinstance(member, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', member):
            raise ValueError('member_id 必须是有效群成员 OpenID')
        if type(duration_minutes) is not int or not 1 <= duration_minutes <= 1440:
            raise ValueError('禁言时长必须为1至1440分钟')
        expires = (datetime.now(timezone.utc) + timedelta(minutes=duration_minutes))
        mute_expire_at = expires.isoformat(timespec='seconds').replace('+00:00', 'Z')
        sandbox = os.environ.get('QQ_SANDBOX', '').lower() in ('1', 'true', 'yes')
        route = Route('POST', '/v2/groups/{group_openid}/restrict_chat_setting',
                      is_sandbox=sandbox, group_openid=group)
        await self.api._http.request(route, json={'members': [{
            'op': 'add', 'member_openid': member, 'mute_expire_at': mute_expire_at,
        }]})
        return {'muted': True, 'duration_minutes': duration_minutes}

    async def send_group_warning(self, group, message_id):
        if not isinstance(group, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', group):
            raise ValueError('group_id 必须是有效群 OpenID')
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_.!:-]{1,200}', message_id):
            raise ValueError('message_id 格式不正确')
        await self.api.post_group_message(group_openid=group, msg_id=message_id,
            content=HARASSMENT_WARNING_MESSAGE, msg_type=0, msg_seq=1)
        return {'sent': True}

    async def moderate_group_message(self, message, bot_mentioned=False):
        content = getattr(message, 'content', '') or ''
        group = str(getattr(message, 'group_openid', '') or '')
        resolver = lambda member_id: self.seen.first_member_name(group, member_id)
        trace_content = strip_moderation_urls(compat.render_mention_tags(content, message, resolver))
        sexual_matches = sexual_harassment_matches(content) if bot_mentioned else []
        moderation_config = {'sensitive_words': [], 'harassment_warning_enabled': False,
                             'harassment_mute_enabled': False,
                             'harassment_mute_threshold': HARASSMENT_MUTE_THRESHOLD,
                             'harassment_mute_duration_minutes': HARASSMENT_MUTE_DURATION_MINUTES}
        settings_reader = getattr(self.retriever, 'moderation_settings', None)
        if callable(settings_reader):
            try:
                loaded = await settings_reader()
                if isinstance(loaded, dict):
                    moderation_config.update(loaded)
            except Exception as exc:
                LOG.warning('MODERATION_SETTINGS_FAILED error=%s', type(exc).__name__)
        else:
            words_reader = getattr(self.retriever, 'moderation_words', None)
            if callable(words_reader):
                try:
                    moderation_config['sensitive_words'] = await words_reader()
                    moderation_config['harassment_warning_enabled'] = True
                except Exception as exc:
                    LOG.warning('MODERATION_SETTINGS_FAILED error=%s', type(exc).__name__)
        words = moderation_config.get('sensitive_words', [])
        expansions = moderation_config.get('sensitive_word_expansions')
        matches = sensitive_word_matches(content, words, expansions)
        if not matches and not sexual_matches:
            return False
        group = str(getattr(message, 'group_openid', '') or '')
        message_id = str(getattr(message, 'id', '') or '')
        if not group or not message_id:
            LOG.warning('MODERATION_RECALL_SKIPPED reason=missing_message_id')
            return True
        if not self.seen.claim('moderation:' + group + ':' + message_id):
            return True
        terms = list(dict.fromkeys([*matches, *sexual_matches]))[:100]
        configured = {normalize_moderation_text(variant)
                      for group in (expansions or [[word] for word in words])
                      for variant in group if isinstance(variant, str)}
        candidates = [term for term in sexual_matches
                      if normalize_moderation_text(term) not in configured]
        event_hash = hashlib.sha256((group + '\0' + message_id).encode()).hexdigest()
        author = getattr(message, 'author', None)
        member = str(getattr(author, 'member_openid', '') or '')
        trace_meta = {'kb_id': self.retriever.kb_id, 'group_id': group, 'user_id': member,
                      'message_id': message_id, 'content': trace_content[:3000]}
        # Persist the hit before attempting QQ moderation; failed recalls still count.
        self.seen.record_moderation_recall(event_hash, terms, candidates, trace_meta)
        report_task = asyncio.create_task(self._flush_moderation_reports_safely())
        identity_hash = None
        strike_count = 0
        if (sexual_matches or matches) and member:
            identity_hash = hashlib.sha256((group + '\0' + member).encode()).hexdigest()
            strike_count = self.seen.record_harassment_strike(identity_hash, event_hash)
        mute_enabled = moderation_config.get('harassment_mute_enabled') is True
        action_threshold = (moderation_config.get('harassment_mute_threshold', HARASSMENT_MUTE_THRESHOLD)
                            if mute_enabled else HARASSMENT_WARNING_AFTER)
        if identity_hash and strike_count >= action_threshold:
            if mute_enabled:
                if self.seen.claim_harassment_mute(identity_hash):
                    try:
                        duration = moderation_config.get('harassment_mute_duration_minutes',
                                                         HARASSMENT_MUTE_DURATION_MINUTES)
                        await self.mute_group_member(group, member, duration)
                        self.seen.clear_harassment_strikes(identity_hash)
                        LOG.info('HARASSMENT_MUTE_APPLIED group=%s duration_minutes=%s', group, duration)
                    except Exception as exc:
                        LOG.warning('HARASSMENT_MUTE_FAILED error=%s', type(exc).__name__)
                        if (moderation_config.get('harassment_warning_enabled') is True
                                and self.seen.claim_harassment_warning(identity_hash)):
                            try:
                                await message.reply(content=HARASSMENT_WARNING_MESSAGE, msg_type=0)
                                self.seen.clear_harassment_strikes(identity_hash)
                                LOG.info('HARASSMENT_WARNING_SENT group=%s', group)
                            except Exception as warning_exc:
                                LOG.warning('HARASSMENT_WARNING_FAILED error=%s', type(warning_exc).__name__)
            elif (moderation_config.get('harassment_warning_enabled') is True
                  and self.seen.claim_harassment_warning(identity_hash)):
                try:
                    await message.reply(content=HARASSMENT_WARNING_MESSAGE, msg_type=0)
                    self.seen.clear_harassment_strikes(identity_hash)
                    LOG.info('HARASSMENT_WARNING_SENT group=%s', group)
                except Exception as exc:
                    LOG.warning('HARASSMENT_WARNING_FAILED error=%s', type(exc).__name__)
        try:
            await self.recall_group_message(group, message_id)
        except Exception as exc:
            LOG.warning('MODERATION_RECALL_FAILED error=%s', type(exc).__name__)
        else:
            LOG.info('MODERATION_RECALLED terms=%s', json.dumps(terms, ensure_ascii=False))
        await report_task
        return True

    async def flush_moderation_reports(self):
        reporter = getattr(self.retriever, 'report_moderation_recall', None)
        if not callable(reporter):
            return
        async with self._moderation_report_lock:
            for event_hash, terms, candidates, trace_meta in self.seen.pending_moderation_recalls():
                if not await reporter(event_hash, terms, candidates, trace_meta):
                    break
                self.seen.acknowledge_moderation_recall(event_hash)

    async def _flush_moderation_reports_safely(self):
        try:
            await self.flush_moderation_reports()
        except Exception as exc:
            LOG.warning('MODERATION_REPORT_FAILED error=%s', type(exc).__name__)

    async def _moderation_report_loop(self):
        while True:
            try:
                await self.flush_moderation_reports()
            except Exception as exc:
                LOG.warning('MODERATION_REPORT_FAILED error=%s', type(exc).__name__)
            await asyncio.sleep(30)

    async def on_group_member_add(self, event):
        group = getattr(event, 'group_openid', '')
        event_id = getattr(event, 'event_id', '')
        member = getattr(event, 'member_openid', '')
        if not group or not event_id or not member:
            LOG.warning('GROUP_WELCOME_SKIPPED reason=missing_event_fields')
            return
        if not self.seen.claim('welcome:' + str(event_id)):
            return
        try:
            welcome = await self.retriever.group_welcome()
        except Exception as exc:
            LOG.warning('GROUP_WELCOME_CONFIG_FAILED error=%s', type(exc).__name__)
            welcome = plain(WELCOME_MESSAGE)
        try:
            await self.api.post_group_message(group_openid=group, content=welcome,
                msg_type=0, event_id=event_id, msg_seq=1)
            LOG.info('GROUP_WELCOME_SENT group=%s', group)
        except Exception as exc:
            LOG.warning('GROUP_WELCOME_FAILED error=%s', type(exc).__name__)

    async def on_c2c_message_create(self, message):
        stage = 'read_message_fields'
        try:
            content = getattr(message, 'content', '')
            content = content if isinstance(content, str) else ''
            learning = getattr(message, 'sweet_learning', None)
            learning = learning if isinstance(learning, dict) else {}
            quoted_images = getattr(message, 'sweet_quoted_images', [])
            quoted_images = quoted_images if isinstance(quoted_images, (list, tuple)) else []
            stage = 'classify_command'
            is_creative = creative_request(normalize(content)) is not None
            stage = 'log_received'
            LOG.info('C2C_RECEIVED creative_command=%s is_reply=%s quoted_images=%s',
                     is_creative, bool(learning.get('is_reply')), len(quoted_images))
            stage = 'answer'
            await self.answer(message, 'c2c')
        except Exception as exc:
            LOG.error('C2C_EVENT_FAILED stage=%s error=%s', stage, type(exc).__name__)
            raise

    async def on_group_at_message_create(self, message):
        LOG.info('GROUP_RECEIVED event=at group=%s bot=%s mentioned=True',
                 getattr(message, 'group_openid', ''), compat.is_bot(message))
        if compat.is_bot(message):return
        if await self.moderate_group_message(message, bot_mentioned=True): return
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
        stage = 'bot_check'
        try:
            if compat.is_bot(message):return
            stage = 'moderation'
            if await self.moderate_group_message(message, bot_mentioned=bool(getattr(message, 'sweet_mentioned', False))): return
            stage = 'summary'
            self.summaries.observe(message)
            stage = 'group_context'
            self.seen.observe_group_message(message)
            stage = 'image_history'
            await self.images.observe(message)
            stage = 'learning'
            if self.learner: self.learner.observe(message)
            stage = 'command_check'
            if getattr(message, 'sweet_mentioned', False) or is_group_command(message.content):
                stage = 'answer'
                await self.answer(message, 'group', mentioned=getattr(message, 'sweet_mentioned', False))
        except Exception as exc:
            # The SDK's on_error callback omits the exception; retain only safe diagnostics.
            LOG.error('GROUP_EVENT_FAILED stage=%s error=%s', stage, type(exc).__name__)
            raise

    async def answer(self, message, kind, mentioned=False):
        if compat.is_bot(message):return
        if kind == 'group':
            self.seen.observe_group_message(message)
        if kind == 'group' and not mentioned and not is_group_command(message.content):
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
                await self._answer(message, kind, session, mentioned)
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
            transcript, checkpoint, count, members = self.summaries.snapshot(group, summary_timestamp(message), include_members=True)
            if not transcript:
                await message.reply(content='当前范围内没有可总结的新内容（已过滤复读和表情包）。', msg_type=0, msg_seq=1)
                return
            result = await self.retriever.summarize(transcript, group, members)
            reply = ('刚刚群里主要聊了这些～\n' if result.get('ok') else '') + plain(result['answer'])[:1500]
            sent = await message.reply(content=reply, msg_type=0, msg_seq=1)
            if sent and result.get('ok'):
                self.summaries.delivered(group, checkpoint, reply)
            LOG.info('SUMMARY_DELIVERY ok=%s messages=%s memory_actions=%s',
                     bool(sent and result.get('ok')), count,
                     sum(1 for item in result.get('memory_actions', []) if item.get('ok')))
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

    async def _answer(self, message, kind, session, mentioned=False):
        raw_content = getattr(message, 'content', '') or ''
        group_id = getattr(message, 'group_openid', '') if kind == 'group' else ''
        resolver = lambda member_id: self.seen.first_member_name(group_id, member_id) if group_id else ''
        input_content = compat.strip_mention_tags(raw_content) if kind == 'group' and mentioned else raw_content
        route_query = normalize(input_content)
        model_content = (compat.strip_bot_mention_tags(raw_content, message, leading_fallback=True)
                         if kind == 'group' and mentioned else raw_content)
        query = normalize_model_text(model_content, message, resolver)
        member_message_text = query
        creative = creative_request(route_query)
        if creative:
            rendered_content = compat.render_mention_tags(raw_content, message, resolver)
            command = '/对联' if creative['style'] == 'couplet' else '/俳句'
            command_at = re.search(r'(?<!\S)' + re.escape(command), rendered_content)
            if command_at:
                rendered_creative = creative_request(rendered_content[command_at.start():])
                if rendered_creative and rendered_creative['style'] == creative['style']:
                    creative = rendered_creative
        vision_attachment = reply_image_attachment(message) if kind == 'group' or creative else None
        if not route_query and vision_attachment:
            query = '请看一下这张图片，理解图片内容后回答。'
        learning = getattr(message, 'sweet_learning', None)
        quotes = ((learning.get('reference') or {}).get('quotes') or []) if isinstance(learning, dict) else []
        quote_texts = list(dict.fromkeys(normalize(q.get('content', '')) for q in quotes if isinstance(q, dict)))
        if creative:
            reply_reference = next((t for t in quote_texts if t and t != route_query), '')[:1800]
        else:
            reply_reference = '\n'.join(t for t in quote_texts if t and t != route_query)[:1800]
        quoted_members = []
        if kind == 'group' and reply_reference:
            current_member = getattr(getattr(message, 'author', None), 'member_openid', '')
            seen_quoted_members = set()
            for quote in quotes:
                if not isinstance(quote, dict):
                    continue
                quoted_openid = quote.get('member_openid', '')
                quoted_text = normalize(quote.get('content', ''))
                if (not isinstance(quoted_openid, str) or not quoted_openid or len(quoted_openid) > 128
                        or quoted_openid == current_member or quoted_openid in seen_quoted_members
                        or not quoted_text or quoted_text not in reply_reference):
                    continue
                seen_quoted_members.add(quoted_openid)
                quoted_name = (compat.clean_display_name(quote.get('member_name', ''))
                               or self.seen.first_member_name(group_id, quoted_openid))
                quoted_members.append({'key': 'quoted' + str(len(quoted_members) + 1),
                                       'openid': quoted_openid, 'name': quoted_name,
                                       'reference': quoted_text[:600]})
                if len(quoted_members) >= 4:
                    break
        mentioned_members = []
        if kind == 'group':
            current_member = getattr(getattr(message, 'author', None), 'member_openid', '')
            quoted_openids = {item['openid'] for item in quoted_members}
            mentioned_members = [item for item in compat.mentioned_members(message, resolver)
                                 if item['openid'] != current_member and item['openid'] not in quoted_openids][:4]
        if kind == 'group' and mentioned and not query and not reply_reference and not vision_attachment:
            LOG.info('GROUP_MENTION_SKIPPED reason=empty_mention')
            return
        if kind == 'group' and mentioned and not query and reply_reference:
            query = '请回应引用内容：' + reply_reference
        remember = False
        trace, delivery, sent, delivery_error = None, 'failed', '', ''
        try:
            if route_query == DRINK_COMMAND:
                menu_reader = getattr(self.retriever, 'drink_menu', None)
                try:
                    items = await menu_reader() if callable(menu_reader) else []
                    if items:
                        weather_reader = getattr(self.retriever, 'drink_weather', None)
                        forecast = None
                        try:
                            forecast = await weather_reader() if callable(weather_reader) else None
                        except Exception as exc:
                            LOG.warning('DRINK_WEATHER_UNAVAILABLE error=%s', type(exc).__name__)
                        preference = forecast.get('temperature_preference', 'random') if forecast else 'random'
                        choices = [item for item in items if preference == 'random'
                                   or item.get('temperature', 'both') in (preference, 'both')]
                        if len(choices) < 2:
                            choices += [item for item in items if item not in choices][:2-len(choices)]
                        author = getattr(message, 'author', None)
                        identity = (getattr(author, 'member_openid', '') if kind == 'group'
                                    else getattr(author, 'user_openid', ''))
                        seed = stable_drink_seed(identity)
                        picker = random.Random(seed) if seed is not None else random
                        picks = picker.sample(choices, min(2, len(choices)))
                        lines = []
                        for index, item in enumerate(picks, 1):
                            serve = item.get('temperature', 'both')
                            if preference in ('cold', 'hot'):
                                if serve in ('both', preference):
                                    serve_label = '建议点冷的' if preference == 'cold' else '建议点热的'
                                else:
                                    serve_label = '与天气建议不同'
                            else:
                                serve_label = {'cold':'冷饮', 'hot':'热饮', 'both':'冷热均可'}.get(serve, '冷热均可')
                            lines.append(f'{index}.「{item["brand"]}」{item["product"]}（{serve_label}）')
                        if forecast:
                            max_temp = round(forecast['today_max'])
                            weather_line = f'杭州今天预报最高 {max_temp}℃。'
                            yesterday_max = forecast.get('yesterday_max')
                            if (forecast.get('temperature_preference') == 'cold' and yesterday_max is not None
                                    and 18 <= forecast['today_max'] <= 27
                                    and forecast['today_max'] - yesterday_max >= 3):
                                weather_line += f'比昨天预报高约 {round(forecast["today_max"] - yesterday_max)}℃，来点凉的～'
                            elif (forecast.get('temperature_preference') == 'hot' and yesterday_max is not None
                                    and 18 <= forecast['today_max'] <= 27
                                    and yesterday_max - forecast['today_max'] >= 3):
                                weather_line += f'比昨天预报低约 {round(yesterday_max - forecast["today_max"])}℃，适合来杯热的～'
                            elif forecast.get('temperature_preference') == 'cold':
                                weather_line += '今天更适合冷饮～'
                            elif forecast.get('temperature_preference') == 'hot':
                                weather_line += '今天更适合热饮～'
                            else:
                                weather_line += '今天就随缘随机挑两杯～'
                            reply = weather_line + ('\n给你两个候选，选一杯吧：\n' if len(picks) == 2 else '\n清单目前只有一款候选：\n') + '\n'.join(lines)
                        else:
                            reply = ('天气暂时读不到，我先随机挑两杯给你选：\n' if len(picks) == 2
                                     else '天气暂时读不到，清单目前只有一款候选：\n') + '\n'.join(lines)
                    else:
                        reply = '饮品推荐清单还空着呢，请管理员先到后台「饮品推荐」添加几杯～'
                except Exception as exc:
                    LOG.warning('DRINK_MENU_FAILED error=%s', type(exc).__name__)
                    reply = '我暂时读不到饮品清单，稍后再帮大家挑一杯吧～'
            elif route_query.lower() == '/old':
                reply = await self.images.lookup(message) if kind == 'group' else '请在群内引用图片消息并发送 /old。'
            elif creative:
                if len(creative['text']) > 2000:
                    reply = '创作内容请控制在 2000 字以内。'
                elif not creative['text'] and not reply_reference and not vision_attachment:
                    command = '/对联 +上联' if creative['style'] == 'couplet' else '/俳句 +一句话'
                    reply = f'请提供创作内容，例如：{command}。也可以引用一条消息后只发送 /对联 或 /俳句。'
                elif self.capacity.locked():
                    reply = '当前创作人数较多，请稍后重新发送指令。'
                else:
                    async with self.capacity:
                        author = getattr(message, 'author', None)
                        meta = {'origin': 'qq_group' if kind == 'group' else 'qq_private',
                                'user_id': getattr(author, 'member_openid' if kind == 'group' else 'user_openid', ''),
                                'group_id': group_id, 'session_id': session}
                        if reply_reference:
                            meta['reply_reference'] = reply_reference
                        image_data_urls = []
                        image_read_failed = False
                        if vision_attachment:
                            try:
                                image_url = (vision_attachment.get('url', '') if isinstance(vision_attachment, dict)
                                             else getattr(vision_attachment, 'url', ''))
                                image_data_urls = [await read_image_data_url(
                                    image_url, getattr(message, '_api', None))]
                            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError, RuntimeError) as exc:
                                image_read_failed = True
                                LOG.warning('CREATIVE_IMAGE_SKIPPED error=%s', type(exc).__name__)
                        if not creative['text'] and not reply_reference and not image_data_urls:
                            reply = ('引用图片读取失败，请重新发送图片后再试。' if image_read_failed else
                                     '请提供创作内容或引用内容。')
                        else:
                            trace = await self.retriever.creative(
                                creative['style'], creative['text'], meta, image_data_urls=image_data_urls)
                            reply = plain(trace.get('answer', '创作暂时失败，请稍后再试。'))[:1700]
                            remember = trace.get('mode') != 'quota'
            elif not query or route_query.lower() in ('帮助', '/帮助', '/help', 'help', '/start'):
                reply = HELP

                if kind=='c2c':reply+='\n\n私聊维护：\n/公告 公告内容（同步群公告）\n/modify 知识库 修改要求\n/modify qa 修改要求\n/add 商品库 商品信息\n/退出 结束维护（仅授权账号可写入）'
            elif kind == 'c2c' and re.match(r'^/公告(?:\s|$)', route_query):
                content = re.sub(r'^/公告(?:\s+|$)', '', route_query, count=1, flags=re.S).strip()

                if not content:
                    reply = '请在 /公告 后附上完整公告内容，例如：/公告 预约和发货说明……'
                elif len(content) > 5000:
                    reply = '公告内容请控制在 5000 字以内。'
                else:

                    author = getattr(message, 'author', None)
                    user_id = getattr(author, 'user_openid', '')
                    if not user_id:
                        reply = '没有读取到你的私聊 OpenID，公告没有写入。'
                    else:
                        result = await self.retriever.sync_announcement(content, user_id, message.id)
                        reply = plain(result.get('answer', '公告同步失败，请稍后重试。'))[:1700]
            elif re.match(r'^/公告(?:\s|$)', route_query):
                reply = '公告同步请私聊机器人发送 /公告 公告内容；只有已授权 OpenID 可以写入知识库。'
            elif kind=='c2c' and (re.match(r'^/(?:modify|add)(?:\s|$)',route_query,re.I) or route_query in ('/退出','/cancel') or self.seen.maintenance_active(session)):

                if len(query)>2000:reply='指令请控制在2000字以内。'
                else:
                    async with self.capacity:
                        trace=await self.retriever.maintain(query,getattr(message.author,'user_openid',''),message.id)
                    reply=plain(trace['answer'])[:1700]
                    self.seen.set_maintenance(session,trace.get('active',False))
            elif kind=='group' and re.match(r'^/(?:modify|add)(?:\s|$)',route_query,re.I):
                reply='维护指令请私聊机器人发送，仅已授权账号可使用。'
            elif route_query in ('/新对话', '/清空上下文'):
                self.seen.clear_history(session)
                if kind == 'group':
                    self.seen.clear_group_context(getattr(message, 'group_openid', ''))
                reply = '已清空当前对话的上下文，我们重新开始。'
            elif route_query in MEMORY_COMMANDS:
                author = getattr(message, 'author', None)
                meta = {'origin': 'qq_group' if kind == 'group' else 'qq_private',
                        'user_id': getattr(author, 'member_openid' if kind == 'group' else 'user_openid', ''),
                        'group_id': group_id}
                display_name = (self.seen.first_member_name(group_id, meta['user_id']) or compat.sender_name(message)[:12]) if kind == 'group' else compat.sender_name(message)[:12]
                if display_name:
                    meta['member_name'] = display_name
                actions = {'/记忆': 'list', '/清除记忆': 'clear', '/关闭记忆': 'disable', '/开启记忆': 'enable'}
                try:
                    result = await self.retriever.memory(actions[route_query], meta)
                    if route_query == '/记忆':
                        if result.get('enabled') is False:
                            reply = '长期记忆当前已关闭。发送 /开启记忆 可恢复。'
                        elif result.get('items'):
                            items = result['items'][:24]
                            reply = '当前长期记忆：\n' + '\n'.join(f'• {plain(item)}' for item in items)
                        else:
                            reply = '目前还没有保存长期记忆。你可以在对话中说“记住……”来保存。'
                    elif route_query == '/清除记忆':
                        reply = '已清除' + str(result.get('deleted', 0)) + '条长期记忆。'
                    elif route_query == '/关闭记忆':
                        reply = '已暂停长期记忆；已有内容会保留但不再读取或更新。'
                    else:
                        reply = '已开启长期记忆。'
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                    reply = '记忆服务暂时不可用，请稍后重试。'
            elif route_query in ('/身份', '/whoami'):
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
                    if kind == 'group':
                        display_name = self.seen.first_member_name(
                            group_id, meta['user_id']) or compat.sender_name(message)[:12]
                    else:
                        display_name = compat.sender_name(message)[:12]
                    if display_name:
                        meta['member_name'] = display_name
                    if reply_reference:meta['reply_reference']=reply_reference
                    if kind == 'group':meta['current_member_text']=member_message_text
                    if quoted_members:meta['quoted_members']=quoted_members
                    if mentioned_members:meta['mentioned_members']=mentioned_members
                    if self.seen.last_sticker_sent(session):meta['previous_sticker_sent']=True
                    history = [] if kind == 'group' else self.seen.history(session)
                    group_context = self.seen.group_history(group_id, message.id) if kind == 'group' else None
                    image_data_urls = []
                    if vision_attachment:
                        try:
                            image_url = (vision_attachment.get('url', '') if isinstance(vision_attachment, dict)
                                         else getattr(vision_attachment, 'url', ''))
                            image_data_urls = [await read_image_data_url(image_url, getattr(message, '_api', None))]
                        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError, RuntimeError) as exc:
                            LOG.warning('VISION_IMAGE_SKIPPED error=%s', type(exc).__name__)
                    if kind == 'group' and group_id:
                        search_args = {'group_id': group_id, 'history': history,
                                       'trace_meta': meta, 'group_context': group_context}
                    else:
                        search_args = {'group_id': group_id, 'history': history, 'trace_meta': meta}
                    if image_data_urls:
                        search_args['image_data_urls'] = image_data_urls
                    trace = await self.retriever.search(query, **search_args)
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

                fallback_reply = ('创作服务暂时不可用，请稍后重新发送指令。'
                                  if creative else '公告同步暂时不可用，请稍后重新发送 /公告 指令。'
                                  if kind == 'c2c' and re.match(r'^/公告(?:\s|$)', route_query)

                                  else '检索服务暂时不可用，请稍后重新发送问题。')
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
