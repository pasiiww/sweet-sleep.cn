"""Group-scoped image history. Only hashes and event metadata reach SQLite."""
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import ipaddress
import logging
import re
from urllib.parse import urlsplit

import aiohttp

LOG = logging.getLogger('knowledge-bot')
MAX_BYTES = 20 * 1024 * 1024
BEIJING = timezone(timedelta(hours=8))


def field(obj, key, default=''):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class PublicResolver(aiohttp.resolver.ThreadedResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        if any(not ipaddress.ip_address(row['host']).is_global for row in addresses):
            raise ValueError('Non-public image address')
        return addresses


def image_url(value):
    value = str(value or '')
    if value.startswith('//'):
        value = 'https:' + value
    elif '://' not in value:
        value = 'https://' + value
    parts = urlsplit(value)
    host = (parts.hostname or '').lower()
    if (parts.scheme not in ('http', 'https') or parts.username or parts.password
            or parts.port not in (None, 80, 443)
            or not any(host.endswith('.' + domain) for domain in ('qpic.cn', 'qq.com', 'qq.com.cn'))):
        raise ValueError('Unsupported image URL')
    # QQ may deliver scheme-less or HTTP URLs; only fetch with TLS.
    return parts._replace(scheme='https', netloc=host).geturl()


async def hash_image(url, api=None):
    url = image_url(url)
    headers = {'Accept-Encoding': 'identity'}
    # The media host requires bot auth. Never send credentials to other hosts or redirects.
    if urlsplit(url).hostname == 'multimedia.nt.qq.com.cn':
        token = getattr(getattr(api, '_http', None), '_token', None)
        if token is not None:
            await token.check_token()
            headers['Authorization'] = token.get_string()
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=1)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=15),
                                     auto_decompress=False) as session:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('Image HTTP failure')
            if response.content_length is not None and response.content_length > MAX_BYTES:
                raise ValueError('Image too large')
            digest, size = hashlib.sha256(), 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('Image too large')
                digest.update(chunk)
            if not size:
                raise ValueError('Empty image')
            return digest.hexdigest()


class ImageHistory:
    def __init__(self, connection):
        self.conn = connection
        self.capacity = asyncio.Semaphore(4)
        self.pending = {}
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS image_occurrences (
                group_id TEXT NOT NULL, message_id TEXT NOT NULL, slot INTEGER NOT NULL,
                msg_idx TEXT NOT NULL, hash TEXT, member_id TEXT NOT NULL,
                member_name TEXT NOT NULL, sent_at REAL NOT NULL,
                PRIMARY KEY (group_id, message_id, slot)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS image_hash_history
                ON image_occurrences(group_id, hash, sent_at);
            CREATE INDEX IF NOT EXISTS image_message_index
                ON image_occurrences(group_id, msg_idx);
        ''')

    async def observe(self, message):
        group, mid = field(message, 'group_openid'), field(message, 'id')
        author = field(message, 'author', None)
        member = field(author, 'member_openid')
        attachments = field(message, 'attachments', []) or []
        images = [(i, a) for i, a in enumerate(attachments)
                  if str(field(a, 'content_type')).lower().split('/')[0] == 'image']
        if not group or not mid or not member or not images:
            return
        key = (group, mid)
        if key in self.pending:
            await asyncio.shield(self.pending[key])
            return
        task = asyncio.create_task(self._record(message, images))
        self.pending[key] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                self.pending.pop(key, None)
            else:
                task.add_done_callback(lambda _: self.pending.pop(key, None))

    async def _record(self, message, images):
        group, mid = message.group_openid, message.id
        meta = field(message, 'sweet_learning', {}) or {}
        idx = meta.get('msg_idx', '')
        author = message.author
        name = field(message, 'sweet_sender_name') or field(author, 'username') or field(author, 'nickname')
        try:
            at = datetime.fromisoformat(str(message.timestamp).replace('Z', '+00:00'))
            if at.tzinfo is None:
                raise ValueError('Missing timezone')
            sent_at = at.timestamp()
        except (AttributeError, ValueError, TypeError, OverflowError):
            LOG.warning('IMAGE_RECORD_FAILED reason=invalid_timestamp')
            return
        for slot, attachment in images:
            with self.conn:
                self.conn.execute('''INSERT INTO image_occurrences VALUES(?,?,?,?,NULL,?,?,?)
                    ON CONFLICT(group_id,message_id,slot) DO UPDATE SET
                    msg_idx=CASE WHEN excluded.msg_idx<>'' THEN excluded.msg_idx ELSE image_occurrences.msg_idx END''',
                    (group, mid, slot, idx, author.member_openid, str(name or '')[:100], sent_at))
        for slot, attachment in images:
            if self.conn.execute('SELECT hash FROM image_occurrences WHERE group_id=? AND message_id=? AND slot=?',
                                 (group, mid, slot)).fetchone()[0]:
                continue
            try:
                async with self.capacity:
                    digest = await hash_image(field(attachment, 'url'), field(message, '_api', None))
                with self.conn:
                    self.conn.execute('UPDATE image_occurrences SET hash=? WHERE group_id=? AND message_id=? AND slot=?',
                                      (digest, group, mid, slot))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError, RuntimeError) as exc:
                LOG.warning('IMAGE_HASH_FAILED error=%s', type(exc).__name__)

    async def lookup(self, message):
        meta = field(message, 'sweet_learning', {}) or {}
        ref = meta.get('reference') or {}
        mid, idx = ref.get('message_id', ''), ref.get('msg_idx', '')
        quoted = field(message, 'sweet_quoted_images', []) or []
        if not mid and not idx and not quoted:
            return '请引用一条图片消息，再发送 /old。'
        group = field(message, 'group_openid')
        def rows():
            # Prefer exact message IDs; do not mix distinct messages on an index collision.
            found = self.conn.execute('SELECT message_id,slot,hash FROM image_occurrences WHERE group_id=? AND message_id=? ORDER BY slot', (group, mid)).fetchall() if mid else []
            return found or (self.conn.execute('SELECT message_id,slot,hash FROM image_occurrences WHERE group_id=? AND msg_idx=? ORDER BY slot', (group, idx)).fetchall() if idx else [])
        records = rows()
        for message_id in dict.fromkeys(r[0] for r in records):
            task = self.pending.get((group, message_id))
            if task:
                await asyncio.shield(task)
        records = rows()
        if not records and quoted:
            # QQ reference indices need not equal the original delivery index.
            # Match the quoted bytes against existing group history without incrementing it.
            for slot, attachment in enumerate(quoted):
                try:
                    async with self.capacity:
                        digest = await hash_image(field(attachment, 'url'), field(message, '_api', None))
                    records.append(('', slot, digest))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError, RuntimeError) as exc:
                    LOG.warning('IMAGE_QUOTE_HASH_FAILED error=%s', type(exc).__name__)
                    records.append(('', slot, None))
            LOG.info('IMAGE_QUOTE_LOOKUP images=%s', len(records))
        if not records:
            LOG.info('IMAGE_REFERENCE_MISS has_mid=%s has_idx=%s quoted_images=%s', bool(mid), bool(idx), len(quoted))
            return '没有找到这条引用消息的图片记录。只能查询启用后机器人在本群收到的图片。'
        parts = []
        for _, slot, digest in records[:10]:
            prefix = f'图片 {slot + 1}：' if len(records) > 1 else ''
            if not digest:
                parts.append(prefix + '图片未能完成哈希记录（下载失败或超过20MB），暂时无法统计。')
                continue
            count = self.conn.execute('SELECT count(*) FROM image_occurrences WHERE group_id=? AND hash=?', (group, digest)).fetchone()[0]
            if not count:
                parts.append(prefix + '本群还没有这张图的发送记录，无法确认首次发送者。请重新发送原图后再引用查询。')
                continue
            member, name, at = self.conn.execute('''SELECT member_id,member_name,sent_at FROM image_occurrences
                WHERE group_id=? AND hash=? ORDER BY sent_at,message_id,slot LIMIT 1''', (group, digest)).fetchone()
            safe_name = re.sub(r'[@<>\x00-\x1f]', '', name)[:60]
            identity = f'<qqbot-at-user id="{member}" />' if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', member) else '未知成员'
            first = '这是第一次发送。' if count == 1 else ''
            parts.append(f'{prefix}{first}这张图在本群已记录 {count} 次。\n最早发送：{safe_name + " " if safe_name else ""}{identity}\n首次时间：{datetime.fromtimestamp(at, BEIJING):%Y-%m-%d %H:%M:%S}（北京时间）')
        if len(records) > 10:
            parts.append('本条消息图片较多，仅展示前10张的统计。')
        return '\n\n'.join(parts)
