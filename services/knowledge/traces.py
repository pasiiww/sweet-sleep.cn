"""Seven-day, administrator-only answer traces. No provider credentials are serialized."""
import hashlib
import hmac
import json
import secrets
import time

RETENTION = 7 * 86400


def redact(value, secrets_to_hide):
    if isinstance(value, str):
        for secret in secrets_to_hide:
            if secret:
                value = value.replace(secret, '[redacted]')
        return value
    if isinstance(value, list):
        return [redact(item, secrets_to_hide) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets_to_hide) for key, item in value.items()}
    return value


def initialize(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS answer_traces (
        id TEXT PRIMARY KEY, created REAL NOT NULL,
        kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
        question TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL, session_id TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'running', reason TEXT NOT NULL DEFAULT '',
        elapsed_ms INTEGER NOT NULL DEFAULT 0, delivery TEXT NOT NULL,
        receipt_hash TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS trace_time ON answer_traces(created DESC,id);
        CREATE INDEX IF NOT EXISTS trace_kb_time ON answer_traces(kb_id,created DESC);
        CREATE INDEX IF NOT EXISTS trace_session ON answer_traces(session_id,created DESC);
    ''')
    cleanup(c)


def cleanup(c):
    c.execute('DELETE FROM answer_traces WHERE created<=?', (time.time() - RETENTION,))


def create(c, kb_id, question, meta):
    trace_id, receipt = secrets.token_hex(16), secrets.token_urlsafe(24)
    c.execute('INSERT INTO answer_traces(id,created,kb_id,question,origin,user_id,group_id,session_id,delivery,receipt_hash) VALUES(?,?,?,?,?,?,?,?,?,?)',
              (trace_id, time.time(), kb_id, question, meta['origin'], meta['user_id'], meta['group_id'], meta['session_id'],
               'pending' if meta['origin'].startswith('qq_') else 'not_applicable', hashlib.sha256(receipt.encode()).hexdigest()))
    return trace_id, receipt


def finish(c, trace_id, response, details, elapsed):
    c.execute('UPDATE answer_traces SET answer=?,mode=?,reason=?,elapsed_ms=?,details=? WHERE id=?',
              (response.get('answer', ''), response['mode'], response['reason'], elapsed,
               json.dumps(details, ensure_ascii=False), trace_id))


def delivery(c, trace_id, receipt, status, content, error, sticker=None):
    row = c.execute('SELECT receipt_hash,details,delivery FROM answer_traces WHERE id=? AND created>?', (trace_id, time.time() - RETENTION)).fetchone()
    if not row or not hmac.compare_digest(row['receipt_hash'], hashlib.sha256(receipt.encode()).hexdigest()):
        raise ValueError('trace 回执无效或已过期')
    if row['delivery'] == 'not_applicable':
        raise ValueError('此记录不是 QQ 消息')
    details = json.loads(row['details'])
    if sticker is not None:
        if not isinstance(sticker,dict) or sticker.get('status') not in ('sent','failed'):raise ValueError('表情包回执格式错误')
        details['sticker_delivery']={key:str(sticker.get(key,''))[:100] for key in ('name','status','error')}
    details['delivery'] = {'status': status, 'content': content, 'error': error, 'at': time.time()}
    c.execute('UPDATE answer_traces SET delivery=?,details=? WHERE id=?', (status, json.dumps(details, ensure_ascii=False), trace_id))
    return {'ok': True}


def query(c, filters, trace_id=None):
    where, args = ['t.created>?'], [time.time() - RETENTION]
    if trace_id:
        row = c.execute('SELECT t.*,b.name AS kb_name FROM answer_traces t JOIN bases b ON b.id=t.kb_id WHERE t.id=? AND t.created>?', (trace_id, args[0])).fetchone()
        if not row:
            return None
        item = dict(row)
        item.pop('receipt_hash')
        item['details'] = json.loads(item['details'])
        return item
    for key in ('kb_id', 'origin', 'mode', 'user_id', 'session_id', 'group_id'):
        if filters.get(key):
            where.append('t.' + key + '=?'); args.append(filters[key])
    for key, op in (('start', '>='), ('end', '<=')):
        if filters.get(key) is not None:
            where.append('t.created' + op + '?'); args.append(filters[key])
    if filters.get('q'):
        where.append('(instr(lower(t.question),lower(?))>0 OR instr(lower(t.answer),lower(?))>0 OR instr(lower(t.details),lower(?))>0)')
        args.extend([filters['q']] * 3)
    clause = ' AND '.join(where)
    total = c.execute('SELECT count(*) FROM answer_traces t WHERE ' + clause, args).fetchone()[0]
    rows = c.execute('''SELECT t.id,t.created,t.kb_id,b.name AS kb_name,t.question,t.answer,t.origin,t.user_id,t.group_id,t.session_id,t.mode,t.reason,t.elapsed_ms,t.delivery,t.details
        FROM answer_traces t JOIN bases b ON b.id=t.kb_id WHERE ''' + clause + ' ORDER BY t.created DESC,t.id DESC LIMIT 30 OFFSET ?', [*args, filters['offset']]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        details = json.loads(item.pop('details'))
        item['search_terms'] = details.get('search_terms', [])
        item['retrieval_count'] = sum(len(search.get('searches', [])) for search in details.get('retrievals', []))
        items.append(item)
    return {'items': items, 'total': total, 'offset': filters['offset'], 'limit': 30}
