"""Bounded group transcripts and delivery-confirmed summary checkpoints."""
from datetime import datetime, timezone, timedelta
import json
import re
import time
import compat

LIMIT = 15000
ZONE = timezone(timedelta(hours=8))


def clean(text):
    if not isinstance(text, str):
        return ''
    text = text.strip()
    if text.startswith(('{', '[')):
        try:
            value = json.loads(text)
            def extract(v, depth=0):
                if depth > 5:
                    return []
                if isinstance(v, dict):
                    return [v[k] for k in ('text', 'content') if isinstance(v.get(k), str)]
                if isinstance(v, list):
                    return [t for item in v[:200] for t in extract(item, depth + 1)]
                return []
            text = '\n'.join(extract(value))
        except (ValueError, RecursionError):
            if text.startswith('{'):
                return ''
    text = re.sub(r'<[^>]*>|\[CQ:[^\]]*\]|\[/?(?:图片|表情包|动画表情|表情|image|emoji)[^\]]*\]', '', text, flags=re.I)
    text = re.sub(r'https?://\S+', '[链接]', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if text.startswith('/') or not any(c.isalnum() for c in text):
        return ''
    if text in ('[链接]', '哈哈', '哈哈哈', '哈哈哈哈', '笑死', '666', '收到', '+1'):
        return ''
    return text[:LIMIT]


def timestamp(message):
    try:
        value = datetime.fromisoformat(str(message.timestamp).replace('Z', '+00:00'))
        if value.tzinfo is not None:
            return value.timestamp()
    except (AttributeError, ValueError, OverflowError):
        pass
    return time.time()


class GroupSummary:
    def __init__(self, conn):
        self.conn = conn
        self.busy = set()
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS summary_messages (
            id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
                member TEXT NOT NULL, member_name TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, at REAL NOT NULL,
                UNIQUE(group_id,message_id));
            CREATE INDEX IF NOT EXISTS summary_group_time ON summary_messages(group_id,at,id);
            CREATE TABLE IF NOT EXISTS summary_checkpoint (
                group_id TEXT PRIMARY KEY, at REAL NOT NULL, last_id INTEGER NOT NULL);
        ''')
        if 'summary' not in {r[1] for r in conn.execute('PRAGMA table_info(summary_checkpoint)')}:
            conn.execute("ALTER TABLE summary_checkpoint ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        if 'member_name' not in {r[1] for r in conn.execute('PRAGMA table_info(summary_messages)')}:
            conn.execute("ALTER TABLE summary_messages ADD COLUMN member_name TEXT NOT NULL DEFAULT ''")
        conn.commit()

    def observe(self, message):
        group = getattr(message, 'group_openid', '')
        mid = getattr(message, 'id', '')
        member = getattr(getattr(message, 'author', None), 'member_openid', '')
        member_name = compat.sender_name(message)
        raw = getattr(message, 'content', '') or ''
        if not group or not mid or not member or re.sub(r'<[^>]*>', '', raw).strip().startswith('/'):
            return
        at = timestamp(message)
        if not time.time() - 36000 <= at <= time.time() + 60:
            return
        with self.conn:
            self.conn.execute('DELETE FROM summary_messages WHERE at<?', (time.time() - 36000,))
            self.conn.execute('INSERT OR IGNORE INTO summary_messages(group_id,message_id,member,member_name,content,at) VALUES(?,?,?,?,?,?)',
                              (group, mid, member, member_name, clean(raw), at))
            self.conn.execute('DELETE FROM summary_messages WHERE group_id=? AND id NOT IN (SELECT id FROM summary_messages WHERE group_id=? ORDER BY at DESC,id DESC LIMIT 400)', (group, group))

    def snapshot(self, group, end):
        checkpoint = self.conn.execute('SELECT at,last_id,summary FROM summary_checkpoint WHERE group_id=?', (group,)).fetchone() or (0, 0, '')
        rows = self.conn.execute('''SELECT id,member,member_name,content,at FROM summary_messages
            WHERE group_id=? AND at>=? AND at<=? AND (at>? OR (at=? AND id>?))
            ORDER BY at DESC,id DESC LIMIT 400''', (group, end-36000, end, checkpoint[0], checkpoint[0], checkpoint[1])).fetchall()
        last_id = max((r[0] for r in rows), default=checkpoint[1])
        selected, seen, used, names = [], set(), 0, {}
        clipped = False
        for _, member, member_name, content, at in rows:
            if not content:
                continue
            key = ''.join(c.lower() for c in content if c.isalnum())
            if key in seen:
                continue
            seen.add(key)
            if member_name:
                names[member] = member_name
            label = names.setdefault(member, '群友' + str(len(names) + 1))
            line = f'[{datetime.fromtimestamp(at, ZONE):%m-%d %H:%M}] {label}：{content}'
            if used + len(line) + 1 > LIMIT:
                clipped = True
                if not selected:
                    selected.append(line[:LIMIT-1])
                break
            used += len(line) + 1
            selected.append(line)
        transcript = '\n'.join(reversed(selected))
        if transcript and checkpoint[2] and checkpoint[0] >= end - 36000 and len(rows) < 400 and not clipped:
            context = '上次成功总结（历史背景）：\n' + checkpoint[2] + '\n\n本轮新发言：\n' + transcript
            if len(context) <= LIMIT:
                transcript = context
        return transcript, (end, last_id), len(selected)

    def delivered(self, group, checkpoint, summary=''):
        with self.conn:
            self.conn.execute('INSERT INTO summary_checkpoint(group_id,at,last_id,summary) VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET at=excluded.at,last_id=excluded.last_id,summary=excluded.summary', (group, *checkpoint, summary[:1700]))
