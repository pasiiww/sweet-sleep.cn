"""Small, user-controlled long-term conversation memories."""
import hashlib
import json
import re
import time
import unicodedata

IMPRESSION_KEY = '__member_impression__'
MAX_ITEMS = 24
MAX_ITEM_CHARS = 240
MAX_TOTAL_CHARS = 4000


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS conversation_memories (
      scope TEXT NOT NULL, normalized TEXT NOT NULL, content TEXT NOT NULL,
      created REAL NOT NULL, updated REAL NOT NULL, owner_openid TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(scope,owner_openid,normalized));
    CREATE INDEX IF NOT EXISTS conversation_memory_scope ON conversation_memories(scope,updated);
    CREATE TABLE IF NOT EXISTS conversation_member_names (
      scope TEXT NOT NULL, member_openid TEXT NOT NULL, first_name TEXT NOT NULL,
      first_seen REAL NOT NULL, PRIMARY KEY(scope,member_openid));
    CREATE TABLE IF NOT EXISTS conversation_memory_settings (
      scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS conversation_memory_scopes (
      scope TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
      origin TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '', group_id TEXT NOT NULL DEFAULT '',
      created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS conversation_memory_scope_groups
      ON conversation_memory_scopes(kb_id,origin,group_id,updated);
    ''')
    columns = {row[1] for row in c.execute('PRAGMA table_info(conversation_memories)')}
    if 'owner_openid' not in columns:
        c.execute("ALTER TABLE conversation_memories ADD COLUMN owner_openid TEXT NOT NULL DEFAULT ''")
    primary_key = [row[1] for row in sorted(c.execute('PRAGMA table_info(conversation_memories)'),
                                           key=lambda row: row[5]) if row[5]]
    if primary_key != ['scope', 'owner_openid', 'normalized']:
        # Preserve existing attribution and timestamps; past merged owners cannot be inferred.
        c.execute('SAVEPOINT memory_owner_migration')
        try:
            c.execute('''CREATE TABLE conversation_memories_new (
                scope TEXT NOT NULL, normalized TEXT NOT NULL, content TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, owner_openid TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(scope,owner_openid,normalized))''')
            c.execute('INSERT INTO conversation_memories_new SELECT scope,normalized,content,created,updated,owner_openid FROM conversation_memories')
            c.execute('DROP TABLE conversation_memories')
            c.execute('ALTER TABLE conversation_memories_new RENAME TO conversation_memories')
            c.execute('RELEASE memory_owner_migration')
        except Exception:
            c.execute('ROLLBACK TO memory_owner_migration')
            c.execute('RELEASE memory_owner_migration')
            raise
    c.execute('CREATE INDEX IF NOT EXISTS conversation_memory_scope ON conversation_memories(scope,updated)')
    c.execute('CREATE INDEX IF NOT EXISTS conversation_memory_owner ON conversation_memories(scope,owner_openid)')
    _migrate_ambiguous_identity(c)
    _seed_member_names(c)
    _backfill_scopes(c)


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


def register_scope(c, kb_id, origin, user_id='', group_id=''):
    """Keep the reversible group identity needed by the administrator view."""
    memory_scope = scope(kb_id, origin, user_id, group_id)
    if not memory_scope or origin != 'qq_group':
        return ''
    current = time.time()
    c.execute('''INSERT INTO conversation_memory_scopes
                 (scope,kb_id,origin,user_id,group_id,created,updated)
                 VALUES(?,?,?,?,?,?,?)
                 ON CONFLICT(scope) DO UPDATE SET updated=excluded.updated''',
              (memory_scope, kb_id, origin, user_id if origin != 'qq_group' else '',
               group_id, current, current))
    return memory_scope


def enabled(c, memory_scope):
    row = c.execute('SELECT enabled FROM conversation_memory_settings WHERE scope=?', (memory_scope,)).fetchone()
    return bool(row[0]) if row else True


def list_items(c, memory_scope):
    rows = c.execute('SELECT content FROM conversation_memories WHERE scope=? ORDER BY updated DESC,created DESC',
                     (memory_scope,)).fetchall()
    return [row[0] for row in rows]


def context(c, memory_scope, member_openid=None, include_public=True):
    if not memory_scope or not enabled(c, memory_scope):
        return []

    rows = c.execute('SELECT content,owner_openid,normalized FROM conversation_memories WHERE scope=? ORDER BY updated DESC,created DESC',
                     (memory_scope,)).fetchall()
    if not include_public:
        rows = [row for row in rows if row[1]]
    rows = [row for row in rows if row[2] != IMPRESSION_KEY or row[1] == member_openid]
    return _render_items(c, memory_scope, reversed(rows))


def member_context(c, memory_scope, member_openid, include_impression=True):
    """Read only this speaker's entries from a conversation scope."""
    if not memory_scope or not member_openid or not enabled(c, memory_scope):
        return []
    rows = c.execute('''SELECT content,owner_openid,normalized FROM conversation_memories
                        WHERE scope=? AND owner_openid=? ORDER BY updated DESC,created DESC''',
                     (memory_scope, member_openid)).fetchall()
    if not include_impression:
        rows = [row for row in rows if row[2] != IMPRESSION_KEY]
    return _render_items(c, memory_scope, reversed(rows))


def group_member_context_for_member(c, kb_id, member_openid):
    """Read this member's own facts/impression from groups where they were seen."""
    if not kb_id or not member_openid:
        return {'items': [], 'identity': None}
    rows = c.execute('''SELECT s.scope,s.group_id,n.first_name FROM conversation_memory_scopes s
                        JOIN conversation_member_names n ON n.scope=s.scope
                        WHERE s.kb_id=? AND s.origin='qq_group' AND n.member_openid=?
                        ORDER BY n.first_seen,s.updated DESC,s.group_id''', (kb_id, member_openid)).fetchall()
    result, seen, identity = [], set(), None
    for memory_scope, _group_id, _nickname in rows:
        if not enabled(c, memory_scope):
            continue
        if identity is None:
            identity = member_identity(c, memory_scope, member_openid)
        group_rows = c.execute('''SELECT content,owner_openid,normalized FROM conversation_memories
                                 WHERE scope=? AND owner_openid=?
                                 ORDER BY updated DESC,created DESC''',
                               (memory_scope, member_openid)).fetchall()
        for item in _render_items(c, memory_scope, reversed(group_rows)):
            rendered = f'【你在群里的个人记忆】{item}'
            if rendered not in seen:
                seen.add(rendered)
                result.append(rendered)
    return {'items': result, 'identity': identity}


def search_public_memories(c, scopes, query, limit=12):
    """Search enabled public memories in a caller-authorized set of scopes."""
    if not scopes or not isinstance(query, str):
        return []
    query_key = normalize(query)
    if not query_key:
        return []
    query_chars = set(query_key)
    query_bigrams = {query_key[i:i + 2] for i in range(len(query_key) - 1)}
    ranked = []
    seen = set()
    for memory_scope in dict.fromkeys(scopes):
        if not enabled(c, memory_scope):
            continue
        rows = c.execute('''SELECT content,updated FROM conversation_memories
                            WHERE scope=? AND owner_openid='' ORDER BY updated DESC''',
                         (memory_scope,)).fetchall()
        for content, updated in rows:
            content_key = normalize(content)
            if query_key in content_key:
                score = 100 + len(query_key)
            else:
                overlap = len(query_chars & set(content_key))
                bigram_overlap = len(query_bigrams & {content_key[i:i + 2] for i in range(len(content_key) - 1)})
                score = overlap + bigram_overlap * 2
                if overlap < 2 and not bigram_overlap:
                    continue
            if score and content not in seen:
                seen.add(content)
                ranked.append((score, updated, content))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [f'【群聊公共记忆】{content}' for _, _, content in ranked[:max(1, min(limit, 20))]]


def search_group_memories_for_member(c, kb_id, member_openid, query, limit=12):
    """Search public memories in enabled groups this member has participated in."""
    if not kb_id or not member_openid:
        return []
    rows = c.execute('''SELECT s.scope FROM conversation_memory_scopes s
                        JOIN conversation_member_names n ON n.scope=s.scope
                        WHERE s.kb_id=? AND s.origin='qq_group' AND n.member_openid=?
                        ORDER BY s.updated DESC,s.group_id''', (kb_id, member_openid)).fetchall()
    return search_public_memories(c, [row[0] for row in rows], query, limit)



def _render_items(c, memory_scope, rows):
    result = []
    for row in rows:
        content, owner = row[0], row[1]
        if owner:
            identity = member_identity(c, memory_scope, owner)
            label = identity['member_key']
            if identity['first_nickname']:
                label += f'，首次记录昵称“{identity["first_nickname"]}”'
            result.append(f'【由{label}提供的记忆】{content}')
        else:
            result.append(content)
    return result


def normalize(content):
    folded = unicodedata.normalize('NFKC', content).casefold()
    return re.sub(r'[\W_]+', '', folded, flags=re.UNICODE)


def clean_member_name(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'[\x00-\x1f\x7f<>]', ' ', value).replace('@', '＠')
    return re.sub(r'\s+', ' ', value).strip()[:12]


def remember_member(c, memory_scope, member_openid, name):
    name = clean_member_name(name)
    if not memory_scope or not isinstance(member_openid, str) or not member_openid or len(member_openid) > 128 or not name:
        return
    c.execute('''INSERT OR IGNORE INTO conversation_member_names(scope,member_openid,first_name,first_seen)
                 VALUES(?,?,?,?)''', (memory_scope, member_openid, name, time.time()))


def member_identity(c, memory_scope, member_openid):
    if not memory_scope or not member_openid:
        return None
    row = c.execute('SELECT first_name FROM conversation_member_names WHERE scope=? AND member_openid=?',
                    (memory_scope, member_openid)).fetchone()
    stable = hashlib.sha256((memory_scope + '\0' + member_openid).encode()).hexdigest()[:8]
    return {'member_key': '成员-' + stable, 'first_nickname': row[0] if row else ''}


def supports_identity_query(c, memory_scope, member_openid, query):
    if not memory_scope or not isinstance(query, str):
        return False
    if not re.search(r'(是谁|什么人|什么身份|哪位|叫什么|叫啥|名字|我是谁|记得我)', query):
        return False
    query_key = normalize(query)
    for stop in ('请问', '我是谁', '是谁', '什么人', '什么身份', '哪位', '叫什么', '叫啥', '名字', '记得我吗', '记得我'):
        query_key = query_key.replace(normalize(stop), '')
    rows = c.execute('SELECT content,owner_openid FROM conversation_memories WHERE scope=?',
                     (memory_scope,)).fetchall()
    for row in rows:
        content_key = normalize(row[0])
        if query_key and len(query_key) >= 2 and query_key in content_key:
            return True
        if (member_openid and row[1] == member_openid
                and re.search(r'(自称|我是|我叫|本人是|本人叫)', row[0])):
            return True
    return False


def _migrate_ambiguous_identity(c):
    old = '群里提到的“落落”是当前这位群友（本人自称）。'
    new = '本人自称“落落”。'
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_traces'").fetchone():
        return
    memories = c.execute("SELECT scope,normalized FROM conversation_memories WHERE content=? AND owner_openid=''", (old,)).fetchall()
    if not memories:
        return
    traces = c.execute("SELECT kb_id,origin,user_id,group_id FROM answer_traces WHERE origin='qq_group' AND question LIKE '%落落是我%' ORDER BY created DESC").fetchall()
    for item in memories:
        owner = next((trace[2] for trace in traces
                      if scope(trace[0], trace[1], trace[2], trace[3]) == item[0]), '')
        new_normalized = normalize(new)
        duplicate = c.execute('SELECT 1 FROM conversation_memories WHERE scope=? AND normalized=? AND owner_openid=?',
                              (item[0], new_normalized, owner)).fetchone()
        if duplicate:
            c.execute("DELETE FROM conversation_memories WHERE scope=? AND normalized=? AND owner_openid=''", (item[0], item[1]))
        else:
            c.execute("UPDATE conversation_memories SET normalized=?,content=?,owner_openid=?,updated=? WHERE scope=? AND normalized=? AND owner_openid=''",
                      (new_normalized, new, owner, time.time(), item[0], item[1]))


def _seed_member_names(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_events'").fetchone():
        return
    rows = c.execute("SELECT kb_id,group_id,member_id,member_name FROM learning_events WHERE member_name<>'' ORDER BY at,id").fetchall()
    for row in rows:
        memory_scope = scope(row[0], 'qq_group', '', row[1])
        remember_member(c, memory_scope, row[2], row[3])


def _backfill_scopes(c):
    """Recover group identities from durable traces/events after the admin view is added."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_traces'").fetchone():
        rows = c.execute("SELECT DISTINCT kb_id,group_id FROM answer_traces WHERE origin='qq_group' AND group_id<>''").fetchall()
        for row in rows:
            register_scope(c, row[0], 'qq_group', '', row[1])
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_events'").fetchone():
        rows = c.execute("SELECT DISTINCT kb_id,group_id FROM learning_events WHERE group_id<>''").fetchall()
        for row in rows:
            register_scope(c, row[0], 'qq_group', '', row[1])


def apply(c, memory_scope, action, content='', member_openid=''):
    if not memory_scope:
        return {'ok': False, 'message': '无法确认记忆范围'}
    current = time.time()
    if action == 'list':
        rows = c.execute('SELECT content,owner_openid FROM conversation_memories WHERE scope=? ORDER BY updated DESC,created DESC',
                         (memory_scope,)).fetchall()
        return {'ok': True, 'enabled': enabled(c, memory_scope), 'items': _render_items(c, memory_scope, rows)}
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
        rows = c.execute('SELECT normalized,content FROM conversation_memories WHERE scope=? AND owner_openid=?',
                         (memory_scope, member_openid)).fetchall()

        matches = [row[0] for row in rows if row[0] == normalized or normalized in normalize(row[1]) or normalize(row[1]) in normalized]

        if len(matches) == 1:
            c.execute('DELETE FROM conversation_memories WHERE scope=? AND normalized=? AND owner_openid=?',
                      (memory_scope, matches[0], member_openid))
            return {'ok': True, 'deleted': 1, 'items': list_items(c, memory_scope)}
        if len(matches) > 1:
            return {'ok': False, 'message': '匹配到多条记忆，请说明要删除的具体内容', 'items': list_items(c, memory_scope)}
        return {'ok': True, 'deleted': 0, 'items': list_items(c, memory_scope)}
    if not enabled(c, memory_scope):
        return {'ok': False, 'message': '记忆功能已关闭'}
    old = c.execute('SELECT created FROM conversation_memories WHERE scope=? AND normalized=? AND owner_openid=?',
                    (memory_scope, normalized, member_openid)).fetchone()
    if old:
        c.execute('UPDATE conversation_memories SET content=?,updated=? WHERE scope=? AND normalized=? AND owner_openid=?',
                  (content, current, memory_scope, normalized, member_openid))
        return {'ok': True, 'saved': True, 'updated': True, 'items': list_items(c, memory_scope)}
    rows = c.execute('SELECT normalized,length(content) FROM conversation_memories WHERE scope=? AND normalized<>?',
                     (memory_scope, IMPRESSION_KEY)).fetchall()
    total = sum(row[1] for row in rows)
    if len(rows) >= MAX_ITEMS or total + len(content) > MAX_TOTAL_CHARS:
        return {'ok': False, 'message': '记忆空间已满，请先删除不再需要的记忆'}
    c.execute('INSERT INTO conversation_memories(scope,normalized,content,created,updated,owner_openid) VALUES(?,?,?,?,?,?)',
              (memory_scope, normalized, content, current, current, member_openid))
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
        register_scope(c, kb_id, origin, user_id, group_id)
        member_name = clean_member_name(data.get('member_name', ''))
        remember_member(c, memory_scope, user_id, member_name)
        result = apply(c, memory_scope, action, content, member_openid=user_id)

    return result


def _admin_item_id(memory_scope, owner_openid, normalized):
    return hashlib.sha256((memory_scope + '\0' + owner_openid + '\0' + normalized).encode()).hexdigest()[:24]


def _admin_item(c, memory_scope, row):
    normalized, content, owner, created, updated = row
    identity = member_identity(c, memory_scope, owner) if owner else None
    return {'id': _admin_item_id(memory_scope, owner, normalized),
            'content': content,
            'subject': 'member' if owner else 'group',
            'member_key': identity['member_key'] if identity else '',
            'first_nickname': identity['first_nickname'] if identity else '',
            'created': created, 'updated': updated}


def admin_groups(c, kb_id=''):
    where = "s.origin='qq_group'"
    args = []
    if kb_id:
        where += ' AND s.kb_id=?'
        args.append(kb_id)
    rows = c.execute('''SELECT s.kb_id,s.group_id,s.scope,s.updated,
                               b.name AS kb_name,
                               (SELECT enabled FROM conversation_memory_settings WHERE scope=s.scope) AS setting_enabled,
                               (SELECT count(*) FROM conversation_memories m WHERE m.scope=s.scope) AS memory_count,
                               (SELECT max(updated) FROM conversation_memories m WHERE m.scope=s.scope) AS memory_updated
                        FROM conversation_memory_scopes s JOIN bases b ON b.id=s.kb_id
                        WHERE ''' + where + ' ORDER BY COALESCE(memory_updated,s.updated) DESC,s.group_id', args).fetchall()
    return [{'kb_id': row['kb_id'], 'kb_name': row['kb_name'], 'group_id': row['group_id'],
             'memory_count': row['memory_count'],
             'enabled': bool(row['setting_enabled']) if row['setting_enabled'] is not None else True,
             'updated': row['memory_updated'] or row['updated']} for row in rows]


def admin_list(c, kb_id, group_id):
    memory_scope = scope(kb_id, 'qq_group', '', group_id)
    if not memory_scope:
        return {'ok': False, 'message': '缺少有效的群聊范围'}
    row = c.execute('SELECT 1 FROM conversation_memory_scopes WHERE scope=? AND kb_id=? AND group_id=? AND origin=?',
                    (memory_scope, kb_id, group_id, 'qq_group')).fetchone()
    if not row:
        return {'ok': True, 'enabled': True, 'items': []}
    rows = c.execute('''SELECT normalized,content,owner_openid,created,updated
                        FROM conversation_memories WHERE scope=? ORDER BY updated DESC,created DESC''',
                     (memory_scope,)).fetchall()
    return {'ok': True, 'enabled': enabled(c, memory_scope),
            'items': [_admin_item(c, memory_scope, row) for row in rows]}


def admin_apply(c, kb_id, group_id, action, item_id=''):
    memory_scope = scope(kb_id, 'qq_group', '', group_id)
    if not memory_scope:
        return {'ok': False, 'message': '缺少有效的群聊范围'}
    if action == 'clear':
        count = c.execute('DELETE FROM conversation_memories WHERE scope=?', (memory_scope,)).rowcount
    elif action in ('enable', 'disable'):
        value = int(action == 'enable')
        c.execute('INSERT INTO conversation_memory_settings(scope,enabled,updated) VALUES(?,?,?) '
                  'ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated',
                  (memory_scope, value, time.time()))
        count = 0
    elif action == 'delete':
        if not isinstance(item_id, str) or not re.fullmatch(r'[a-f0-9]{24}', item_id):
            return {'ok': False, 'message': '记忆条目标识无效'}
        rows = c.execute('SELECT normalized,owner_openid FROM conversation_memories WHERE scope=?',
                         (memory_scope,)).fetchall()
        matches = [(normalized, owner) for normalized, owner in rows
                   if _admin_item_id(memory_scope, owner, normalized) == item_id]
        if not matches:
            return {'ok': False, 'message': '记忆条目不存在'}
        normalized, owner = matches[0]
        count = c.execute('DELETE FROM conversation_memories WHERE scope=? AND normalized=? AND owner_openid=?',
                           (memory_scope, normalized, owner)).rowcount
    else:
        return {'ok': False, 'message': '不支持的管理操作'}
    result = admin_list(c, kb_id, group_id)
    result['deleted'] = count

    return result


def append_impression(c, memory_scope, member_openid, sentence):
    if not isinstance(sentence, str):
        return False
    sentence = re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f<>]', ' ', sentence)).strip()
    sentence = sentence.rstrip('。！？!?')
    if not sentence or re.search(r'[。！？!?]', sentence):
        return False
    existing = impression(c, memory_scope, member_openid).removeprefix('群友印象：').strip()
    combined = (existing + ('。' if existing and not existing.endswith(('。', '！', '？', '.', '!', '?')) else '')
                + sentence + '。')
    return update_impression(c, memory_scope, member_openid, combined)


def _admin_item_id(memory_scope, owner_openid, normalized):
    return hashlib.sha256((memory_scope + '\0' + owner_openid + '\0' + normalized).encode()).hexdigest()[:24]


def _admin_item(c, memory_scope, row):
    normalized, content, owner, created, updated = row
    identity = member_identity(c, memory_scope, owner) if owner else None
    return {'id': _admin_item_id(memory_scope, owner, normalized),
            'content': content,
            'subject': 'member' if owner else 'group',
            'member_key': identity['member_key'] if identity else '',
            'first_nickname': identity['first_nickname'] if identity else '',
            'created': created, 'updated': updated}


def admin_groups(c, kb_id=''):
    where = "s.origin='qq_group'"
    args = []
    if kb_id:
        where += ' AND s.kb_id=?'
        args.append(kb_id)
    rows = c.execute('''SELECT s.kb_id,s.group_id,s.scope,s.updated,
                               b.name AS kb_name,
                               (SELECT enabled FROM conversation_memory_settings WHERE scope=s.scope) AS setting_enabled,
                               (SELECT count(*) FROM conversation_memories m WHERE m.scope=s.scope) AS memory_count,
                               (SELECT max(updated) FROM conversation_memories m WHERE m.scope=s.scope) AS memory_updated
                        FROM conversation_memory_scopes s JOIN bases b ON b.id=s.kb_id
                        WHERE ''' + where + ' ORDER BY COALESCE(memory_updated,s.updated) DESC,s.group_id', args).fetchall()
    return [{'kb_id': row['kb_id'], 'kb_name': row['kb_name'], 'group_id': row['group_id'],
             'memory_count': row['memory_count'],
             'enabled': bool(row['setting_enabled']) if row['setting_enabled'] is not None else True,
             'updated': row['memory_updated'] or row['updated']} for row in rows]


def admin_list(c, kb_id, group_id):
    memory_scope = scope(kb_id, 'qq_group', '', group_id)
    if not memory_scope:
        return {'ok': False, 'message': '缺少有效的群聊范围'}
    row = c.execute('SELECT 1 FROM conversation_memory_scopes WHERE scope=? AND kb_id=? AND group_id=? AND origin=?',
                    (memory_scope, kb_id, group_id, 'qq_group')).fetchone()
    if not row:
        return {'ok': True, 'enabled': True, 'items': []}
    rows = c.execute('''SELECT normalized,content,owner_openid,created,updated
                        FROM conversation_memories WHERE scope=? ORDER BY updated DESC,created DESC''',
                     (memory_scope,)).fetchall()
    return {'ok': True, 'enabled': enabled(c, memory_scope),
            'items': [_admin_item(c, memory_scope, row) for row in rows]}


def admin_apply(c, kb_id, group_id, action, item_id='', content=''):
    memory_scope = scope(kb_id, 'qq_group', '', group_id)
    if not memory_scope:
        return {'ok': False, 'message': '缺少有效的群聊范围'}
    if action == 'clear':
        count = c.execute('DELETE FROM conversation_memories WHERE scope=?', (memory_scope,)).rowcount
    elif action in ('enable', 'disable'):
        value = int(action == 'enable')
        c.execute('INSERT INTO conversation_memory_settings(scope,enabled,updated) VALUES(?,?,?) '
                  'ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated',
                  (memory_scope, value, time.time()))
        count = 0
    elif action in ('append_impression', 'replace_impression'):
        if not isinstance(item_id, str) or not re.fullmatch(r'[a-f0-9]{24}', item_id):
            return {'ok': False, 'message': '印象条目标识无效'}
        rows = c.execute('SELECT normalized,owner_openid FROM conversation_memories WHERE scope=?',
                         (memory_scope,)).fetchall()
        match = next(((normalized, owner) for normalized, owner in rows
                      if _admin_item_id(memory_scope, owner, normalized) == item_id), None)
        if not match or match[0] != IMPRESSION_KEY or not match[1]:
            return {'ok': False, 'message': '群友印象不存在'}
        normalized, owner = match
        existing = c.execute('SELECT content FROM conversation_memories WHERE scope=? AND normalized=? AND owner_openid=?',
                             (memory_scope, normalized, owner)).fetchone()[0]
        if action == 'append_impression':
            sentence = re.sub(r'\s+', ' ', str(content or '')).strip()
            if not sentence or len(sentence) > 235 or re.search(r'[。！？!?；;]$', sentence):
                return {'ok': False, 'message': '请填写一句不带句末标点的印象'}
            result = append_impression(c, memory_scope, owner, sentence)
        else:
            result = update_impression(c, memory_scope, owner, content)
        if not result:
            return {'ok': False, 'message': '印象内容无效或超过235字'}
        count = 1
    elif action == 'delete':
        if not isinstance(item_id, str) or not re.fullmatch(r'[a-f0-9]{24}', item_id):
            return {'ok': False, 'message': '记忆条目标识无效'}
        rows = c.execute('SELECT normalized,owner_openid FROM conversation_memories WHERE scope=?',
                         (memory_scope,)).fetchall()
        matches = [(normalized, owner) for normalized, owner in rows
                   if _admin_item_id(memory_scope, owner, normalized) == item_id]
        if not matches:
            return {'ok': False, 'message': '记忆条目不存在'}
        normalized, owner = matches[0]
        count = c.execute('DELETE FROM conversation_memories WHERE scope=? AND normalized=? AND owner_openid=?',
                           (memory_scope, normalized, owner)).rowcount
    else:
        return {'ok': False, 'message': '不支持的管理操作'}
    result = admin_list(c, kb_id, group_id)
    result['deleted'] = count
    return result


def impression(c, memory_scope, member_openid):
    if not memory_scope or not member_openid or not enabled(c, memory_scope):
        return ''
    row = c.execute('SELECT content FROM conversation_memories WHERE scope=? AND owner_openid=? AND normalized=?',
                    (memory_scope, member_openid, IMPRESSION_KEY)).fetchone()
    return row[0] if row else ''


def update_impression(c, memory_scope, member_openid, content):
    """One bounded, replaceable impression per member; existing memory controls apply."""
    if not memory_scope or not member_openid or not enabled(c, memory_scope):
        return False
    if not isinstance(content, str):
        return False
    content = re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f<>]', ' ', content)).strip()
    if not content:
        return False
    content = '群友印象：' + content.removeprefix('群友印象：')
    if len(content) > MAX_ITEM_CHARS:
        return False
    if re.search(r'(openid|api\s*key|密码|密钥|手机号|身份证|住址|心理疾病|政治倾向|性取向)', content, re.I):
        return False
    current = time.time()
    c.execute('INSERT INTO conversation_memories(scope,normalized,content,created,updated,owner_openid) '
              'VALUES(?,?,?,?,?,?) ON CONFLICT(scope,owner_openid,normalized) '
              'DO UPDATE SET content=excluded.content,updated=excluded.updated',
              (memory_scope, IMPRESSION_KEY, content, current, current, member_openid))
    return True
