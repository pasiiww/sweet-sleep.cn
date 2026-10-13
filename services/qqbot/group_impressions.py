"""Persistent per-group 100-message batches; background updates never send QQ messages."""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime

import compat
from group_summary import clean, timestamp, ZONE

LOG = logging.getLogger('knowledge-bot')
BATCH_SIZE = 100
RETENTION = 7 * 86400


class GroupImpressions:
    def __init__(self, conn, retriever):
        self.conn, self.retriever, self.task = conn, retriever, None
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS impression_seen (
                group_id TEXT NOT NULL, message_id TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(group_id,message_id));
            CREATE INDEX IF NOT EXISTS impression_seen_time ON impression_seen(created);
            CREATE TABLE IF NOT EXISTS impression_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                message_id TEXT NOT NULL, member TEXT NOT NULL, name TEXT NOT NULL,
                content TEXT NOT NULL, at REAL NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS impression_group_messages ON impression_messages(group_id,id);
            CREATE INDEX IF NOT EXISTS impression_message_time ON impression_messages(created);
            CREATE TABLE IF NOT EXISTS impression_outbox (
                batch_id TEXT PRIMARY KEY, group_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0);
        ''')
        conn.commit()

    def observe(self, message):
        if compat.is_bot(message):
            return
        group, mid = getattr(message, 'group_openid', ''), getattr(message, 'id', '')
        member = getattr(getattr(message, 'author', None), 'member_openid', '')
        at, current = timestamp(message), time.time()
        if not group or not mid or not member or not current - RETENTION <= at <= current + 60:
            return
        def name_for(owner):
            row = self.conn.execute('SELECT first_name FROM group_member_names WHERE group_id=? AND member_id=?',
                                    (group, owner)).fetchone()
            return row[0] if row else ''
        content = clean(getattr(message, 'content', ''), message, name_for)
        if not content:
            return
        name = (name_for(member) or compat.sender_name(message))[:12]
        with self.conn:
            self.cleanup(current)
            if self.conn.execute('SELECT count(*) FROM impression_messages WHERE group_id=?', (group,)).fetchone()[0] >= 1000:
                LOG.warning('IMPRESSION_QUEUE_FULL')
                return
            if not self.conn.execute('INSERT OR IGNORE INTO impression_seen VALUES(?,?,?)', (group, mid, current)).rowcount:
                return
            self.conn.execute('INSERT INTO impression_messages(group_id,message_id,member,name,content,at,created) VALUES(?,?,?,?,?,?,?)',
                              (group, mid, member, name, content[:2000], at, current))

    def cleanup(self, current):
        for table in ('impression_seen', 'impression_messages', 'impression_outbox'):
            self.conn.execute('DELETE FROM ' + table + ' WHERE created<?', (current - RETENTION,))

    def prepare(self):
        with self.conn:
            self.cleanup(time.time())
            row = self.conn.execute('''SELECT group_id FROM impression_messages
                WHERE group_id NOT IN (SELECT group_id FROM impression_outbox)
                GROUP BY group_id HAVING count(*)>=? ORDER BY min(id) LIMIT 1''', (BATCH_SIZE,)).fetchone()
            if not row:
                return
            group = row[0]
            rows = self.conn.execute('SELECT id,message_id,member,name,content,at FROM impression_messages WHERE group_id=? ORDER BY id LIMIT ?',
                                     (group, BATCH_SIZE)).fetchall()
            members, lines = {}, []
            for _, _, owner, name, content, at in sorted(rows, key=lambda r: (r[5], r[0])):
                item = members.setdefault(owner, {'key': 'speaker' + str(len(members) + 1), 'openid': owner, 'name': name})
                text = content if len(content) <= 100 else content[:99] + '…'
                lines.append(f'[{datetime.fromtimestamp(at, ZONE):%m-%d %H:%M}] {name or item["key"]} [{item["key"]}]：{text}')
            batch = hashlib.sha256(json.dumps([group, [r[1] for r in rows]], ensure_ascii=False).encode()).hexdigest()
            payload = {'kb_id': self.retriever.kb_id, 'group_id': group, 'batch_id': batch,
                       'members': list(members.values()), 'transcript': '\n'.join(lines)}
            self.conn.execute('INSERT INTO impression_outbox(batch_id,group_id,payload,created) VALUES(?,?,?,?)',
                              (batch, group, json.dumps(payload, ensure_ascii=False), time.time()))
            self.conn.executemany('DELETE FROM impression_messages WHERE id=?', [(r[0],) for r in rows])

    async def flush_once(self):
        self.prepare()
        row = self.conn.execute('SELECT batch_id,payload,attempts FROM impression_outbox WHERE retry_at<=? ORDER BY created LIMIT 1',
                                (time.time(),)).fetchone()
        if not row:
            return False
        try:
            result = await self.retriever.update_impressions(json.loads(row[1]))
            if not result.get('ok'):
                raise RuntimeError('Impression generation failed')
        except Exception as exc:
            with self.conn:
                self.conn.execute('UPDATE impression_outbox SET attempts=attempts+1,retry_at=? WHERE batch_id=?',
                                  (time.time() + min(600, 15 * 2 ** min(row[2], 6)), row[0]))
            LOG.warning('IMPRESSION_BATCH_FAILED error=%s', type(exc).__name__)
            return False
        with self.conn:
            self.conn.execute('DELETE FROM impression_outbox WHERE batch_id=?', (row[0],))
        LOG.info('IMPRESSION_BATCH_OK updated=%s ignored=%s', result.get('updated', 0), result.get('ignored', 0))
        return True

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                if await self.flush_once():
                    continue
            except Exception as exc:
                LOG.warning('IMPRESSION_WORKER_FAILED error=%s', type(exc).__name__)
            await asyncio.sleep(3)
