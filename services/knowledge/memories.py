"""Small, user-controlled long-term conversation memories."""
import hashlib
import json
import re
import time
import unicodedata

MAX_ITEMS = 24
MAX_ITEM_CHARS = 240
MAX_TOTAL_CHARS = 4000


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS conversation_memories (
      scope TEXT NOT NULL, normalized TEXT NOT NULL, content TEXT NOT NULL,
      created REAL NOT NULL, updated REAL NOT NULL,
      PRIMARY KEY(scope,normalized));
    CREATE INDEX IF NOT EXISTS conversation_memory_scope ON conversation_memories(scope,updated);
    CREATE TABLE IF NOT EXISTS conversation_memory_settings (
      scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
    ''')


def scope(kb_id, origin, user_id='', group_id=''):
    """Group chats share one memory; private chats remain per-user."""
    if origin == 'qq_group' and group_id:
        parts = ['qq_group', kb_id, group_id]
    elif origin == 'qq_private' and user_id:
        parts = ['qq_private', kb_id, user_id]
    else:
        return ''
    raw = json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def enabled(c, memory_scope):
    row = c.execute('SELECT enabled FROM conversation_memory_settings WHERE scope=?', (memory_scope,)).fetchone()
    return bool(row[0]) if row else True


def list_items(c, memory_scope):
    rows = c.execute('SELECT content FROM conversation_memories WHERE scope=? ORDER BY updated DESC,created DESC',
                     (memory_scope,)).fetchall()
    return [row[0] for row in rows]


def context(c, memory_scope):
    if not memory_scope or not enabled(c, memory_scope):
        return []
    return list(reversed(list_items(c, memory_scope)))


def normalize(content):
    folded = unicodedata.normalize('NFKC', content).casefold()
    return re.sub(r'[\W_]+', '', folded, flags=re.UNICODE)


def apply(c, memory_scope, action, content=''):
    if not memory_scope:
        return {'ok': False, 'message': '无法确认记忆范围'}
    current = time.time()
    if action == 'list':
        return {'ok': True, 'enabled': enabled(c, memory_scope), 'items': list_items(c, memory_scope)}
    if action in ('enable', 'disable'):
        value = int(action == 'enable')
        c.execute('INSERT INTO conversation_memory_settings(scope,enabled,updated) VALUES(?,?,?) '
                  'ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated',
                  (memory_scope, value, current))
        return {'ok': True, 'enabled': bool(value), 'items': list_items(c, memory_scope)}
    if action == 'clear':
        count = c.execute('DELETE FROM conversation_memories WHERE scope=?', (memory_scope,)).rowcount
        return {'ok': True, 'deleted': count, 'enabled': enabled(c, memory_scope), 'items': []}
    if action not in ('save', 'forget'):
        return {'ok': False, 'message': '不支持的记忆操作'}
    if not isinstance(content, str):
        return {'ok': False, 'message': '记忆内容格式不正确'}
    content = re.sub(r'[\x00-\x1f\x7f]', ' ', content)
    content = re.sub(r'\s+', ' ', content).strip()
    if not content:
        return {'ok': False, 'message': '记忆内容不能为空'}
    if len(content) > MAX_ITEM_CHARS:
        return {'ok': False, 'message': f'每条记忆最多{MAX_ITEM_CHARS}字'}
    normalized = normalize(content)
    if not normalized:
        return {'ok': False, 'message': '记忆内容无效'}
    if action == 'forget':
        rows = c.execute('SELECT normalized,content FROM conversation_memories WHERE scope=?',
                         (memory_scope,)).fetchall()
        matches = [row[0] for row in rows if row[0] == normalized or normalized in row[0] or row[0] in normalized]
        if len(matches) == 1:
            c.execute('DELETE FROM conversation_memories WHERE scope=? AND normalized=?', (memory_scope, matches[0]))
            return {'ok': True, 'deleted': 1, 'items': list_items(c, memory_scope)}
        if len(matches) > 1:
            return {'ok': False, 'message': '匹配到多条记忆，请说明要删除的具体内容', 'items': list_items(c, memory_scope)}
        return {'ok': True, 'deleted': 0, 'items': list_items(c, memory_scope)}
    if not enabled(c, memory_scope):
        return {'ok': False, 'message': '记忆功能已关闭'}
    old = c.execute('SELECT created FROM conversation_memories WHERE scope=? AND normalized=?',
                    (memory_scope, normalized)).fetchone()
    if old:
        c.execute('UPDATE conversation_memories SET content=?,updated=? WHERE scope=? AND normalized=?',
                  (content, current, memory_scope, normalized))
        return {'ok': True, 'saved': True, 'updated': True, 'items': list_items(c, memory_scope)}
    rows = c.execute('SELECT normalized,length(content) FROM conversation_memories WHERE scope=?',
                     (memory_scope,)).fetchall()
    total = sum(row[1] for row in rows)
    if len(rows) >= MAX_ITEMS or total + len(content) > MAX_TOTAL_CHARS:
        return {'ok': False, 'message': '记忆空间已满，请先删除不再需要的记忆'}
    c.execute('INSERT INTO conversation_memories VALUES(?,?,?,?,?)',
              (memory_scope, normalized, content, current, current))
    return {'ok': True, 'saved': True, 'updated': False, 'items': list_items(c, memory_scope)}


def request(app, data):
    kb_id = app.string(data, 'kb_id', 80, True)
    origin = app.string(data, 'origin', 20, True)
    user_id = app.string(data, 'user_id', 128)
    group_id = app.string(data, 'group_id', 128)
    action = app.string(data, 'action', 20, True)
    content = app.string(data, 'content', MAX_ITEM_CHARS)
    memory_scope = scope(kb_id, origin, user_id, group_id)
    if not memory_scope:
        app.fail(400, '缺少有效的群或用户身份，无法访问记忆')
    with app.WRITE_LOCK, app.db() as c:
        app.base(c, kb_id)
        result = apply(c, memory_scope, action, content)
    return result
