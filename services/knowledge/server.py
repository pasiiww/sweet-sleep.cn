#!/usr/bin/env python3
"""Small, authenticated knowledge service. Python 3.11+, SQLite FTS5, no pip dependencies."""
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error
from urllib.parse import urlsplit, parse_qs
import answers
import entities
import traces
import qa
import learning
import stickers
import sys

DATA = Path(os.environ.get('KB_DATA_DIR', '/var/lib/sweet-knowledge'))
STATIC = Path(os.environ.get('KB_STATIC_DIR', Path(__file__).resolve().parents[2] / 'knowledge'))
ADMIN_TOKEN = os.environ.get('KB_ADMIN_TOKEN', '')
READ_TOKEN = os.environ.get('KB_READ_TOKEN', '')
LEARN_TOKEN = os.environ.get('KB_LEARN_TOKEN', '')
WRITE_LOCK = threading.RLock()
VECTOR_LOCK = threading.Lock()
ANSWER_SLOTS = threading.BoundedSemaphore(4)
MAX_CHUNKS = 10000


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def fail(status, message):
    raise Problem(status, message)


@contextlib.contextmanager
def db():
    conn = sqlite3.connect(DATA / 'knowledge.db', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS bases (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
          chunk_size INTEGER NOT NULL, overlap INTEGER NOT NULL, top_k INTEGER NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
          title TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
          updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL, content TEXT NOT NULL,
          vector TEXT, fingerprint TEXT);
        CREATE INDEX IF NOT EXISTS chunk_kb ON chunks(kb_id);
        CREATE INDEX IF NOT EXISTS document_kb ON documents(kb_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(title, body);
        CREATE TRIGGER IF NOT EXISTS chunk_delete AFTER DELETE ON chunks BEGIN
          DELETE FROM chunk_fts WHERE rowid=old.id;
        END;
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entity_catalog (kb_id TEXT PRIMARY KEY REFERENCES bases(id) ON DELETE CASCADE, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_queries (user_id TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(user_id,day));
        CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        c.execute('INSERT OR IGNORE INTO settings VALUES(1, ?)', (json.dumps({
            'base_url': '', 'model': '', 'api_key': '', 'revision': secrets.token_hex(8)}),))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES(?,?)', ('answer', json.dumps(answers.defaults())))
        traces.initialize(c)
        qa.initialize(c)
        learning.initialize(c)
        stickers.initialize(c)


def now():
    return learning.utc(time.time())


def string(data, key, limit, required=False):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        fail(400, f'{key} 必须是有效文本，最多 {limit} 个字符')
    return value.strip()


def integer(data, key, default, low, high):
    value = data.get(key, default)
    if type(value) is not int or not low <= value <= high:
        fail(400, f'{key} 必须是 {low}–{high} 之间的整数')
    return value


def tokens(text):
    # Chinese unigrams + bigrams; Latin words. No raw user FTS syntax is executed.
    result = []
    for part in re.findall(r'[\u3400-\u9fff]+|[a-z0-9_]+', text.lower()):
        if '\u3400' <= part[0] <= '\u9fff':
            result.extend(part)
            result.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            result.append(part)
    return result


def split_text(text, size, overlap):
    parts, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind(mark, start + size // 2, end) for mark in ('\n', '。', '. ', '！', '？'))
            if boundary >= 0:
                end = boundary + 1
        if text[start:end].strip():
            parts.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return parts


def base(c, kb_id):
    row = c.execute('SELECT * FROM bases WHERE id=?', (kb_id,)).fetchone()
    if not row:
        fail(404, '知识库不存在')
    return dict(row)


def config(c):
    return json.loads(c.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])


def fingerprint(cfg):
    return hashlib.sha256((cfg['base_url'] + '\n' + cfg['model'] + '\n' + cfg['revision']).encode()).hexdigest()


def public_config(cfg):
    return {k: v for k, v in cfg.items() if k not in ('api_key', 'revision')} | {
        'has_key': bool(cfg['api_key']), 'ready': bool(cfg['base_url'] and cfg['model'])}


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        fail(400, 'Embedding Base URL 必须为 HTTPS 地址，不含账号、查询参数或片段')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            fail(400, 'Embedding 地址必须指向公网服务')
    except (OSError, ValueError):
        fail(400, 'Embedding 服务地址无法解析')


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def embed(texts, cfg):
    if not cfg['base_url'] or not cfg['model']:
        fail(409, '请先在模型设置中配置 Embedding 服务')
    validate_url(cfg['base_url'])
    vectors = []
    for start in range(0, len(texts), 16):
        batch = texts[start:start + 16]
        headers = {'Content-Type': 'application/json'}
        if cfg['api_key']:
            headers['Authorization'] = 'Bearer ' + cfg['api_key']
        payload = {'input': batch, 'model': cfg['model'], 'encoding_format': 'float'}
        req = request.Request(cfg['base_url'].rstrip('/') + '/embeddings',
                              data=json.dumps(payload).encode(), headers=headers)
        try:
            with request.build_opener(NoRedirect).open(req, timeout=30) as resp:
                raw = resp.read(8_000_001)
                if len(raw) > 8_000_000:
                    fail(502, 'Embedding 返回内容过大')
                rows = sorted(json.loads(raw)['data'], key=lambda row: row['index'])
                if [r['index'] for r in rows] != list(range(len(batch))):
                    raise ValueError('Invalid embedding indices')
                for row in rows:
                    v = row['embedding']
                    if not isinstance(v, list) or not 1 <= len(v) <= 8192:
                        raise ValueError('Invalid vector')
                    if any(type(x) not in (int, float) or not math.isfinite(x) for x in v):
                        raise ValueError('Invalid vector values')
                    norm = math.sqrt(sum(x * x for x in v))
                    if not norm or not math.isfinite(norm) or (vectors and len(v) != len(vectors[0])):
                        raise ValueError('Invalid vector norm or dimensions')
                    vectors.append([x / norm for x in v])
        except error.HTTPError as exc:
            fail(502, f'Embedding 服务返回 HTTP {exc.code}，请检查地址、模型及密钥')
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            fail(502, 'Embedding 调用失败或响应格式无效，请检查服务配置')
    return vectors


def index_document(c, document, kb):
    parts = split_text(document['content'], kb['chunk_size'], kb['overlap'])
    current = c.execute('SELECT count(*) FROM chunks WHERE kb_id=? AND doc_id<>?',
                        (kb['id'], document['id'])).fetchone()[0]
    if current + len(parts) > MAX_CHUNKS:
        fail(400, f'单个知识库最多 {MAX_CHUNKS} 个分段，请减少内容或增大分段长度')
    c.execute('DELETE FROM chunks WHERE doc_id=?', (document['id'],))
    for i, part in enumerate(parts):
        chunk_id = c.execute('INSERT INTO chunks(doc_id,kb_id,ordinal,content) VALUES(?,?,?,?)',
                             (document['id'], kb['id'], i, part)).lastrowid
        c.execute('INSERT INTO chunk_fts(rowid,title,body) VALUES(?,?,?)',
                  (chunk_id, ' '.join(tokens(document['title'])), ' '.join(tokens(part))))


def build_vectors(kb_id):
    if not VECTOR_LOCK.acquire(blocking=False):
        fail(409, '已有向量任务正在运行，请稍后再试')
    try:
        with db() as c:
            base(c, kb_id)
            cfg = config(c)
            fp = fingerprint(cfg)
            rows = c.execute('SELECT id,content FROM chunks WHERE kb_id=? AND (fingerprint IS NULL OR fingerprint<>?) ORDER BY id LIMIT 32', (kb_id, fp)).fetchall()
        if not rows:
            return {'processed': 0, 'remaining': 0}
        vectors = embed([r['content'] for r in rows], cfg)
        with WRITE_LOCK, db() as c:
            if fingerprint(config(c)) != fp:
                fail(409, '模型配置已更改，请重新生成向量')
            for row, vector in zip(rows, vectors):
                # AUTOINCREMENT IDs prevent stale writes when a document is edited during embedding.
                c.execute('UPDATE chunks SET vector=?,fingerprint=? WHERE id=?',
                          (json.dumps(vector), fp, row['id']))
            remaining = c.execute('SELECT count(*) FROM chunks WHERE kb_id=? AND (fingerprint IS NULL OR fingerprint<>?)', (kb_id, fp)).fetchone()[0]
        return {'processed': len(rows), 'remaining': remaining}
    finally:
        VECTOR_LOCK.release()


def entity_catalog(c, kb_id):
    row = c.execute('SELECT value FROM entity_catalog WHERE kb_id=?', (kb_id,)).fetchone()
    return json.loads(row[0]) if row else []


def retrieve(data):
    groups = None
    if 'query_groups' in data:
        try:
            groups = answers.normalize_query_groups(data['query_groups'])
        except ValueError as exc:
            fail(400, str(exc))
    query = string(data, 'query', 2000, not groups)
    if not query:
        query = ' / '.join(' '.join(group) for group in groups)
    kb_id = string(data, 'kb_id', 80, True)
    mode = data.get('mode', 'keyword')
    if mode not in ('keyword', 'vector', 'hybrid'):
        fail(400, 'mode 必须为 keyword、vector 或 hybrid')
    if groups and mode != 'keyword':
        fail(400, 'query_groups 当前支持 keyword 模式；向量和混合召回请使用 query')
    budget = integer(data, 'max_context_chars', 12000, 100, 40000)
    started = time.monotonic()
    with db() as c:
        kb = base(c, kb_id)
        k = integer(data, 'top_k', kb['top_k'], 1, 20)
        catalog = entities.Catalog(entity_catalog(c, kb_id))
        query = catalog.normalize(query)
        if groups:
            try:
                groups = answers.normalize_query_groups([[catalog.normalize(term) for term in group] for group in groups])
            except ValueError as exc:
                fail(400, str(exc))
        cfg, keyword, semantic = config(c), [], []
        expanded = [variant for name in catalog.referenced(query) for variant in catalog.expand(name)]
        ft = list(dict.fromkeys(tokens(query) + [token for variant in expanded for token in tokens(variant)]))[:512]
        if mode != 'vector' and groups:
            scores = {}
            for term in dict.fromkeys(term for group in groups for term in group):
                group=[term]
                # FTS narrows candidates; literal predicates ensure full terms, not scattered CJK characters.
                parts, conditions, values = [], [], []
                for term in group:
                    variants = catalog.expand(term)
                    alternatives, checks = [], []
                    for variant in variants:
                        ft_tokens = list(dict.fromkeys(tokens(variant)))
                        if not ft_tokens:
                            continue
                        alternatives.append('(' + ' AND '.join('"' + token + '"' for token in ft_tokens) + ')')
                        checks.append('(instr(lower(d.title), lower(?)) > 0 OR instr(lower(chunks.content), lower(?)) > 0)')
                        values.extend([variant, variant])
                    if not alternatives:
                        break
                    parts.append('(' + ' OR '.join(alternatives) + ')')
                    conditions.append('(' + ' OR '.join(checks) + ')')
                if len(parts) != len(group):
                    continue
                match = ' AND '.join(parts)
                predicates = ' AND '.join(conditions)
                params = [match, kb_id, *values, k * 4]
                rows = c.execute('''SELECT chunks.id,bm25(chunk_fts,2.0,1.0) AS rank
                    FROM chunk_fts JOIN chunks ON chunks.id=chunk_fts.rowid
                    JOIN documents d ON d.id=chunks.doc_id
                    WHERE chunk_fts MATCH ? AND chunks.kb_id=? AND ''' + predicates +
                    ' ORDER BY rank, chunks.id LIMIT ?', params)
                for rank, row in enumerate(rows, 1):
                    scores[row['id']] = scores.get(row['id'], 0) + 1 + 1 / (60 + rank)
            keyword = sorted(scores.items(), key=lambda row: (-row[1], row[0]))
        elif mode != 'vector' and ft:
            keyword = [(r['id'], -r['rank']) for r in c.execute('''
                SELECT chunks.id,bm25(chunk_fts,2.0,1.0) AS rank FROM chunk_fts
                JOIN chunks ON chunks.id=chunk_fts.rowid
                WHERE chunk_fts MATCH ? AND chunks.kb_id=? ORDER BY rank LIMIT ?
                ''', (' OR '.join('"' + t + '"' for t in ft), kb_id, k * 4))]
        if mode != 'keyword':
            if not public_config(cfg)['ready']:
                fail(409, '向量召回尚未配置，请先设置 Embedding 服务并生成向量')
            rows = c.execute('SELECT id,vector,fingerprint FROM chunks WHERE kb_id=?', (kb_id,)).fetchall()
            if any(r['fingerprint'] != fingerprint(cfg) or not r['vector'] for r in rows):
                fail(409, '部分分段尚未生成当前模型的向量，请先点击「生成向量」')
            if rows:
                qv = embed([query], cfg)[0]
                for row in rows:
                    v = json.loads(row['vector'])
                    if len(v) != len(qv):
                        fail(409, '向量维度不一致，请重新保存模型设置并生成向量')
                    score = sum(a * b for a, b in zip(qv, v))
                    if score > 0:
                        semantic.append((row['id'], score))
                semantic.sort(key=lambda row: row[1], reverse=True)
                semantic = semantic[:k * 4]
        if mode == 'hybrid':
            scores = {}
            for ranking in (keyword, semantic):
                for rank, (chunk_id, _) in enumerate(ranking, 1):
                    scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
            ranking = sorted(scores.items(), key=lambda row: row[1], reverse=True)
        else:
            ranking = keyword if mode == 'keyword' else semantic
        qa_ranking = qa.search(c, kb_id, query, groups, catalog, tokens, k * 4) if mode != 'vector' else []
        score_type = 'rrf' if groups and mode == 'keyword' else {'keyword':'bm25','vector':'cosine','hybrid':'rrf'}[mode]
        if qa_ranking:
            # Separate corpora have incomparable BM25 values; merge their ranks, not raw scores.
            combined = {('document', chunk_id): score if groups else 1/(60+rank) for rank,(chunk_id,score) in enumerate(ranking,1)}
            combined.update({('qa', qa_id): score if groups else 1/(60+rank) for rank,(qa_id,score) in enumerate(qa_ranking,1)})
            ranking = sorted(combined.items(),key=lambda item:(-item[1],item[0]))
            score_type = 'rrf'
        else:
            ranking = [(('document', chunk_id),score) for chunk_id,score in ranking]
        results, context, used = [], [], 0
        for (source_type, item_id), score in ranking[:k]:
            if source_type == 'qa':
                row = c.execute('SELECT * FROM qa_entries WHERE id=? AND kb_id=?',(item_id,kb_id)).fetchone()
                if not row: continue
                chunk_id = 'qa:' + str(item_id)
                result = {'chunk_id':chunk_id,'ordinal':0,'content':row['answer'][:2000],
                          'question':row['question'],'title':row['question'],'document_id':chunk_id,
                          'qa_id':item_id,'updated_at':row['updated_at'],'source':'','source_type':'qa','truncated':len(row['answer'])>2000}
            else:
                chunk_id = item_id
                row = c.execute('SELECT c.id AS chunk_id,c.ordinal,c.content,d.id AS document_id,d.title,d.source,d.updated_at FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=? AND c.kb_id=?', (chunk_id, kb_id)).fetchone()
                if not row: continue
                result = dict(row) | {'source_type':'document'}
            prefix = f'[{len(results) + 1}] {result["title"]} (document={result["document_id"]}, chunk={chunk_id})\n'
            available = budget - used - len(prefix) - (2 if context else 0)
            if available <= 0:
                break
            original = result['content']
            result['content'] = original[:available]
            result['truncated'] = result.get('truncated', False) or len(original) > available
            result['score'] = round(score, 8)
            result['citation'] = len(results) + 1
            block = prefix + result['content']
            used += len(block) + (2 if context else 0)
            context.append(block)
            results.append(result)
        return {'query': query, 'kb_id': kb_id, 'mode': mode, 'results': results,
                'context': '\n\n'.join(context), 'elapsed_ms': round((time.monotonic() - started) * 1000),
                'query_groups': groups or [],
                'score_type': score_type}


def answer_config(c):
    return answers.defaults() | json.loads(c.execute("SELECT value FROM app_settings WHERE name='answer'").fetchone()[0])


def answer_status(cfg, mode, reason):
    with WRITE_LOCK, db() as c:
        if answer_config(c)['revision'] == cfg['revision']:
            c.execute('INSERT OR REPLACE INTO app_settings VALUES(?,?)',
                      ('answer_status', json.dumps({'mode': mode, 'reason': reason, 'at': now()})))


def search_terms(kb_id, terms, original_query=''):
    # Fuse rankings, deduplicate chunk IDs and identical text, then apply a shared budget.
    candidates, searches = {}, []
    searches_to_run=([original_query] if original_query else [])+list(terms)
    unique=[]
    for term in searches_to_run:
        if term not in unique:unique.append(term)
    for term in unique:
        search = {'query_groups': [term]} if isinstance(term, list) else {'query': term}
        result = retrieve({'kb_id': kb_id, **search, 'mode': 'keyword',
                           'top_k': 12, 'max_context_chars': 12000})
        searches.append({'query': term, 'kind':'original' if isinstance(term,str) and term==original_query else 'generated', 'elapsed_ms': result.get('elapsed_ms', 0), 'hits': [{'chunk_id': row['chunk_id'], 'title': row['title'], 'source_type': row.get('source_type','document'), 'score': row.get('score')} for row in result['results']]})
        for rank, row in enumerate(result['results'], 1):
            key = row['chunk_id']
            if key not in candidates:
                candidates[key] = {'row': row, 'rank': 0}
            candidates[key]['rank'] += 1 / (60 + rank)
    catalog=entities.Catalog([])
    if original_query:
        with db() as c:catalog=entities.Catalog(entity_catalog(c,kb_id))
    generated=list(dict.fromkeys(t for group in terms if isinstance(group,list) for t in group))
    for item in candidates.values():
        row=item['row'];text=(row.get('question','') if row.get('source_type')=='qa' else row.get('title','')+' '+row['content']).casefold()
        item['matched_terms']=[term for term in generated if any(v.casefold() in text for v in catalog.expand(term))]
    selected, seen, remaining = [], set(), 6000
    for item in sorted(candidates.values(), key=lambda x:(len(x['matched_terms']),x['rank']), reverse=True):
        row = dict(item['row'])
        identity = ((row.get('question','') + '\n') if row.get('source_type') == 'qa' else '') + ' '.join(row['content'].split())
        key = hashlib.sha256(identity.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        text = row['content'][:remaining]
        if not text:
            break
        row.update(content=text, matched_terms=item['matched_terms'], citation=len(selected) + 1,
                   truncated=row.get('truncated', False) or len(text) < len(row['content']))
        remaining -= len(text)
        selected.append(row)
        if len(selected) >= 8:
            break
    return {'results': selected, 'searches': searches}


def reserve_daily_query(user_id):
    # Identity only: channels, groups, knowledge bases and cleared sessions share this counter.
    day=datetime.fromtimestamp(time.time(),timezone(timedelta(hours=8))).date().isoformat()
    with WRITE_LOCK,db() as c:
        c.execute('DELETE FROM daily_queries WHERE day<?',(day,))
        c.execute('INSERT OR IGNORE INTO daily_queries VALUES(?,?,0)',(user_id,day))
        allowed=c.execute('UPDATE daily_queries SET used=used+1 WHERE user_id=? AND day=? AND used<20',(user_id,day)).rowcount==1
        used=c.execute('SELECT used FROM daily_queries WHERE user_id=? AND day=?',(user_id,day)).fetchone()[0]
    return {'allowed':allowed,'used':used,'limit':20,'remaining':20-used,'day':day,'timezone':'Asia/Shanghai'}


def respond(data):
    query = string(data, 'query', 2000, True)
    kb_id = string(data, 'kb_id', 80, True)
    origin = string(data, 'origin', 20) or 'api'
    if origin not in ('api', 'preview', 'qq_group', 'qq_private'):
        fail(400, 'origin 格式不正确')
    meta = {key: string(data, key, 128) for key in ('user_id', 'group_id', 'session_id')}
    meta['origin'] = origin
    if origin.startswith('qq_') and not meta['user_id']:fail(400,'缺少用户身份，无法核验每日额度')
    try:entities.history(data.get('history', []))
    except ValueError as exc:fail(400,str(exc))
    with WRITE_LOCK, db() as c:
        base(c, kb_id)
        secrets_to_hide = [ADMIN_TOKEN, READ_TOKEN, LEARN_TOKEN, answer_config(c)['api_key'], config(c)['api_key']]
        trace_id, receipt = traces.create(c, kb_id, traces.redact(query, secrets_to_hide), meta)
    details, started = {'model_calls': [], 'retrievals': []}, time.monotonic()
    try:
        quota=reserve_daily_query(meta['user_id']) if meta['user_id'] else None
        if quota:details['quota']=quota
        if quota and not quota['allowed']:
            response={'mode':'quota','reason':'daily_quota_exhausted','answer':'今天的20次咨询额度已经用完啦～明天零点恢复，再来找我聊呀 ♡','handoff':False,'mention_openids':[],'results':[]}
        else:
            response = respond_pipeline(data, details)
        if quota:response['quota']=quota
    except Exception as exc:
        details['error_type'] = type(exc).__name__
        with WRITE_LOCK, db() as c:
            traces.finish(c, trace_id, {'mode': 'error', 'reason': 'internal_error'}, traces.redact(details, secrets_to_hide), round((time.monotonic() - started) * 1000))
        raise
    details['search_terms'] = response.get('search_terms', [])
    details['matched_aliases'] = response.get('matched_aliases', [])
    details['alias_context'] = response.get('alias_context', '')
    with WRITE_LOCK, db() as c:
        traces.finish(c, trace_id, traces.redact(response, secrets_to_hide), traces.redact(details, secrets_to_hide), round((time.monotonic() - started) * 1000))
    return response | {'trace_id': trace_id, 'trace_receipt': receipt if origin.startswith('qq_') else ''}


def trace_cleanup():
    while True:
        time.sleep(3600)
        try:
            with WRITE_LOCK, db() as c:
                traces.cleanup(c)
                learning.cleanup(c)
        except Exception as exc:
            print('Trace cleanup failed: ' + type(exc).__name__, flush=True)


def respond_pipeline(data, details):
    previous_sticker_sent=data.get('previous_sticker_sent',False)
    if type(previous_sticker_sent) is not bool:fail(400,'previous_sticker_sent 必须为布尔值')
    query = string(data, 'query', 2000, True)
    group_id = string(data, 'group_id', 128)
    kb_id = string(data, 'kb_id', 80, True)
    with db() as c:
        base(c, kb_id)
        cfg = answer_config(c) | {'stickers':stickers.available(c)}
        catalog = entities.Catalog(entity_catalog(c, kb_id))
    try:
        history = entities.history(data.get('history', []))
    except ValueError as exc:
        fail(400, str(exc))
    hints = catalog.hints([m['content'] for m in history] + [query])
    with db() as c:
        hint_query=catalog.normalize(query+' '+' '.join(m['content'] for m in history[-4:] if m['role']=='user'))
        ranking=qa.search(c,kb_id,hint_query,None,catalog,tokens,8)
        hint_ids=[r[0] for r in ranking]
        hint_ids += [r[0] for r in c.execute("SELECT id FROM qa_entries WHERE kb_id=? AND publication='active' AND superseded_by IS NULL ORDER BY updated_at DESC,id DESC LIMIT 8",(kb_id,)) if r[0] not in hint_ids]
        qa_hints=[c.execute('SELECT question FROM qa_entries WHERE id=?',(qid,)).fetchone()[0][:300] for qid in hint_ids[:8]]
    cfg = cfg | {'previous_sticker_sent':previous_sticker_sent,'current_date':answers.current_date(),'qa_hints':qa_hints,'conversation_history': history, 'alias_context': entities.context(hints), '_trace': details}
    details['sticker_policy']={'mode':'advisory','previous_sticker_sent':previous_sticker_sent,'guidance':'频率建议：店铺咨询类问题的50%以下的轮次带表情包。'}
    details.update(current_date=cfg['current_date'],qa_hints=qa_hints,history=history, model=cfg['model'], system_prompt=cfg['system_prompt'], keyword_prompt=cfg['keyword_prompt'])
    def search(terms):
        started = time.monotonic()
        result = search_terms(kb_id, terms, original_query=query)
        details['retrievals'].append({'original_query':query,'terms': terms, 'elapsed_ms': round((time.monotonic() - started) * 1000), **result})
        return result
    terms = [catalog.normalize(query)[:2000]]
    def finish(response):
        selected_name=response.pop('sticker_name',None)
        selected=next((s for s in cfg.get('stickers',[]) if s['name']==selected_name),None)
        if selected:response['sticker']={k:selected[k] for k in ('id','name','url','revision')}
        details['sticker']=response.get('sticker')
        response['alias_context'] = cfg['alias_context']
        response['matched_aliases'] = hints
        response['history_turns'] = len(history) // 2
        response['search_terms'] = terms
        response['query_groups'] = [term for term in terms if isinstance(term, list)]
        answer_status(cfg, response['mode'], response['reason'])
        return response
    def fallback(reason, result=None):
        nonlocal terms
        if result is None:
            groups = entities.fallback_groups(catalog, query, history)
            if not groups and history and entities.is_followup(query):
                return finish(answers.handoff(cfg, group_id, 'insufficient_evidence'))
            terms = groups or [catalog.normalize(query)[:2000]]
            result = search(terms)
        if not result['results']:
            return finish(answers.handoff(cfg, group_id, 'no_results'))
        return finish(answers.fallback(result, reason))
    if not cfg['enabled'] or not cfg['api_key']:
        return fallback('disabled' if not cfg['enabled'] else 'missing_key')
    if not ANSWER_SLOTS.acquire(blocking=False):
        return fallback('busy')
    result = None
    try:
        try:
            terms = answers.keywords(cfg, query)
            try:
                terms = answers.normalize_query_groups([[catalog.normalize(term) for term in group] for group in terms])
            except ValueError:
                raise answers.ModelError('invalid_keywords') from None
        except answers.ModelError as exc:
            details['keyword_error'] = str(exc)
            if str(exc) in ('invalid_key', 'insufficient_balance', 'access_denied'):
                return fallback(str(exc))
            # A planning failure must not skip the independent answer stage.
            terms = entities.fallback_groups(catalog, query, history) or [catalog.normalize(query)[:2000]]
        if not terms:
            cfg=cfg|{'retrieval_skipped':True}
            details['retrieval_skipped']='pe1_empty_query_groups'
            result={'results':[]}
        else:
            result = search(terms)
        if terms and not result['results']:
            retry_cfg=cfg|{'empty_retrieval':{'question':query,'failed_query_groups':terms,'result_count':0},'_stage':'keywords_retry'}
            try:
                retry_terms=answers.keywords(retry_cfg,query)
                terms=answers.normalize_query_groups([[catalog.normalize(t) for t in group] for group in retry_terms])
                if terms:result=search(terms)
                else:
                    cfg=cfg|{'retrieval_skipped':True}
                    details['retrieval_skipped']='pe1_retry_empty_query_groups'
            except answers.ModelError as exc:details['retry_error']=str(exc)
            except ValueError:details['retry_error']='invalid_keywords'
        model = answers.complete(cfg, query, result['results'])
        if not model['supported']:
            response = answers.handoff(cfg, group_id, 'insufficient_evidence' if result['results'] else 'no_results')
            if model.get('answer'):
                response['answer'] = answers.plain(model['answer'])
            if model.get('sticker_name'):response['sticker_name']=model['sticker_name']
            return finish(response)
        return finish({'mode': 'model', 'reason': 'ok', 'handoff': False, 'mention_openids': [],
                       'answer': answers.plain(model['answer']), 'sticker_name':model.get('sticker_name'), 'results': result['results']})
    except answers.ModelError as exc:
        if cfg.get('retrieval_skipped'):
            return finish({'mode':'fallback','reason':str(exc),'handoff':False,'mention_openids':[],'answer':'我在呀～你想了解什么呢？','results':[]})
        return fallback(str(exc), result)
    finally:
        ANSWER_SLOTS.release()


def api(method, path, data, params):
    segments = path.removeprefix('/knowledge/api/').strip('/').split('/')
    if segments == ['retrieve'] and method == 'POST':
        return retrieve(data)
    if segments == ['answer'] and method == 'POST':
        return respond(data)
    if segments == ['answer-settings', 'test'] and method == 'POST':
        with db() as c:
            cfg = answer_config(c)
        if not cfg['api_key']:
            fail(409, '请先保存 DeepSeek API Key')
        try:
            answers.complete(cfg, '测试代号是什么？', [{'citation': 1, 'title': '连接测试', 'content': '测试代号是午觉。'}])
            answer_status(cfg, 'test', 'ok')
            return {'ok': True, 'model': cfg['model']}
        except answers.ModelError as exc:
            answer_status(cfg, 'test', str(exc))
            fail(502, '模型测试失败：' + str(exc))
    if len(segments) == 3 and segments[0] == 'bases' and segments[2] == 'embed' and method == 'POST':
        return build_vectors(segments[1])
    if segments == ['settings', 'test'] and method == 'POST':
        with db() as c:
            cfg = config(c)
        v = embed(['连接测试'], cfg)
        return {'dimensions': len(v[0]), 'ok': True}
    auto_sticker_name=False
    if segments==['stickers'] and method=='POST' and not string(data,'name',60).strip():
        try:
            url=stickers.image_url(string(data,'path',2000,True))
            with db() as c:
                cfg=answer_config(c)
                if c.execute('SELECT count(*) FROM stickers').fetchone()[0]>=100:fail(400,'最多100个表情包')
            naming_image=url
            local=re.fullmatch(r'https://sweet-sleep.cn/knowledge/sticker-files/([a-f0-9]{32}\.(png|jpg|gif))',url)
            if local:
                import base64
                file=DATA/'stickers'/local.group(1)
                if not file.is_file():fail(400,'图片不存在')
                mime={'png':'image/png','jpg':'image/jpeg','gif':'image/gif'}[local.group(2)]
                naming_image='data:'+mime+';base64,'+base64.b64encode(file.read_bytes()).decode()
            data=dict(data,name=stickers.generate_name(cfg,naming_image));auto_sticker_name=True
        except ValueError as exc:fail(400,str(exc))
    with WRITE_LOCK, db() as c:
        if len(segments)==4 and segments[:2]==['learning','reviews'] and method=='POST':
            try:return learning.review(c,segments[2],segments[3])
            except ValueError as exc:fail(409,str(exc))
        if segments==['stickers'] and method=='GET':
            return {'items':[dict(r) for r in c.execute('SELECT * FROM stickers ORDER BY id')]}
        if segments and segments[0]=='stickers' and (len(segments)==1 or len(segments)==2):
            existing=c.execute('SELECT * FROM stickers WHERE id=?',(segments[1],)).fetchone() if len(segments)==2 else None
            if len(segments)==2 and not existing:fail(404,'表情包不存在')
            if method=='DELETE' and existing:
                c.execute('DELETE FROM stickers WHERE id=?',(existing['id'],));return {'deleted':True}
            if method not in ('POST','PUT') or (method=='PUT' and not existing):fail(405,'不支持此操作')
            if existing and data.get('revision')!=existing['revision']:fail(409,'表情包已被修改，请刷新后重试')
            name=string(data,'name',60,True);path=string(data,'path',2000,True)
            if any(ch in name for ch in '[]\n\r'):fail(400,'名称不能包含方括号或换行')
            if type(data.get('enabled',True)) is not bool:fail(400,'启用状态格式错误')
            try:url=stickers.image_url(path)
            except ValueError as exc:fail(400,str(exc))
            if auto_sticker_name:
                stem=name;number=2
                while c.execute('SELECT 1 FROM stickers WHERE name=?',(name,)).fetchone():
                    name=f'{stem}-{number}';number+=1
            if c.execute('SELECT 1 FROM stickers WHERE name=? AND id<>?',(name,existing['id'] if existing else -1)).fetchone():fail(409,'表情包名称不能重复')
            if not existing and c.execute('SELECT count(*) FROM stickers').fetchone()[0]>=100:fail(400,'最多100个表情包')
            if existing:
                sid=existing['id'];c.execute('UPDATE stickers SET name=?,path=?,url=?,enabled=?,revision=?,updated_at=? WHERE id=?',(name,path,url,int(data.get('enabled',True)),secrets.token_hex(8),now(),sid))
            else:sid=c.execute('INSERT INTO stickers(name,path,url,enabled,revision,updated_at) VALUES(?,?,?,?,?,?)',(name,path,url,int(data.get('enabled',True)),secrets.token_hex(8),now())).lastrowid
            return dict(c.execute('SELECT * FROM stickers WHERE id=?',(sid,)).fetchone())
        if segments == ['learning', 'events'] and method == 'POST':
            kb_id = string(data, 'kb_id', 80, True)
            base(c, kb_id)
            try: return learning.ingest(c, kb_id, traces.redact(data,[ADMIN_TOKEN,READ_TOKEN,LEARN_TOKEN,answer_config(c)['api_key'],config(c)['api_key']]))
            except ValueError as exc: fail(400, str(exc))
        if len(segments) == 3 and segments[0] == 'bases' and segments[2] == 'learning':
            kb_id = base(c, segments[1])['id']
            if method == 'PUT':
                try: learning.save_config(c, kb_id, data)
                except ValueError as exc: fail(400,str(exc))
            elif method != 'GET': fail(405,'不支持此操作')
            return learning.config(c,kb_id) | {'default_prompt':learning.PROMPT,'ingestion_ready':bool(LEARN_TOKEN)}
        if segments == ['learning', 'jobs'] and method == 'GET':
            kb_id = params.get('kb_id',[''])[0]
            base(c,kb_id)
            return {'items':[dict(r) for r in c.execute("SELECT id,created,status,attempts,error,group_id,json_extract(details,'$.trigger') AS trigger FROM learning_jobs WHERE kb_id=? AND created>? ORDER BY created DESC LIMIT 50",(kb_id,time.time()-7*86400))],
                    'pending_messages':c.execute("SELECT count(*) FROM learning_events WHERE kb_id=? AND qq<>'' AND job_id IS NULL AND at>?",(kb_id,time.time()-1800)).fetchone()[0]}
        if len(segments) == 3 and segments[:2] == ['learning','jobs'] and method == 'GET':
            row=c.execute('SELECT * FROM learning_jobs WHERE id=? AND created>?',(segments[2],time.time()-7*86400)).fetchone()
            if not row: fail(404,'学习任务不存在')
            return dict(row) | {'details':json.loads(row['details'])}
        if len(segments) == 4 and segments[:2] == ['learning','jobs'] and segments[3]=='retry' and method=='POST':
            row=c.execute("SELECT * FROM learning_jobs WHERE id=? AND status='error'",(segments[2],)).fetchone()
            if not row: fail(409,'只有失败的学习任务可以重试')
            if json.loads(row['details'])['settings'] != learning.config(c,row['kb_id']): fail(409,'学习配置已变更，不能重试旧任务')
            c.execute("UPDATE learning_jobs SET status='pending',attempts=0,next_try=0,error='' WHERE id=?",(segments[2],))
            return {'ok':True}
        if segments == ['trace-delivery'] and method == 'POST':
            status = string(data, 'status', 20, True)
            if status not in ('delivered', 'failed'):
                fail(400, 'status 必须为 delivered 或 failed')
            try:
                return traces.delivery(c, string(data, 'trace_id', 64, True), string(data, 'receipt', 100, True), status,
                                       traces.redact(string(data, 'content', 3000), [ADMIN_TOKEN, READ_TOKEN, LEARN_TOKEN, answer_config(c)['api_key'], config(c)['api_key']]), string(data, 'error', 80), data.get('sticker'))
            except ValueError as exc:
                fail(403, str(exc))
        if segments and segments[0] == 'traces' and method == 'GET':
            filters = {key: params.get(key, [''])[0][:200] for key in ('kb_id','origin','mode','user_id','group_id','session_id','q')}
            try:
                filters['offset'] = max(0, min(1000000, int(params.get('offset', ['0'])[0])))
                for key in ('start', 'end'):
                    if params.get(key, [''])[0]:
                        value = float(params[key][0])
                        if not math.isfinite(value): raise ValueError()
                        filters[key] = value
            except ValueError:
                fail(400, '时间或分页参数不正确')
            result = traces.query(c, filters, segments[1] if len(segments) == 2 else None)
            if result is None: fail(404, '记录不存在或已超过7天')
            return result
        if segments == ['answer-settings']:
            cfg = answer_config(c)
            if method == 'PUT':
                if type(data.get('enabled')) is not bool:
                    fail(400, 'enabled 必须为布尔值')
                model = string(data, 'model', 100, True)
                if not re.fullmatch(r'[a-zA-Z0-9_.-]+', model):
                    fail(400, '模型名格式不正确')
                try:
                    groups = answers.validate_groups(data.get('handoff_groups', {}))
                except ValueError as exc:
                    fail(400, str(exc))
                key = string(data, 'api_key', 2000)
                if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
                    fail(400, 'API Key 必须为不含空格的可打印 ASCII 字符')
                admin_qq = string(data, 'admin_qq', 12) or cfg['admin_qq']
                if not re.fullmatch(r'[1-9][0-9]{4,11}', admin_qq):
                    fail(400, '管理员QQ号格式不正确')
                cfg = {'enabled': data['enabled'], 'model': model, 'admin_qq': admin_qq,
                       'admin_name': string(data, 'admin_name', 40) or cfg['admin_name'],
                       'system_prompt': string(data, 'system_prompt', 12000, True),
                       'keyword_prompt': string(data, 'keyword_prompt', 12000, True) if 'keyword_prompt' in data else cfg['keyword_prompt'],
                       'api_key': key or ('' if data.get('clear_key') else cfg['api_key']),
                       'handoff_groups': groups, 'revision': secrets.token_hex(8)}
                c.execute('UPDATE app_settings SET value=? WHERE name=?', (json.dumps(cfg), 'answer'))
                c.execute("DELETE FROM app_settings WHERE name='answer_status'")
            elif method != 'GET':
                fail(405, '不支持此操作')
            status = c.execute("SELECT value FROM app_settings WHERE name='answer_status'").fetchone()
            return {k: v for k, v in cfg.items() if k not in ('api_key', 'revision')} | {
                'has_key': bool(cfg['api_key']), 'default_prompt': answers.DEFAULT_PROMPT, 'default_keyword_prompt': answers.KEYWORD_PROMPT,
                'last_status': json.loads(status[0]) if status else None}
        if segments == ['settings']:
            cfg = config(c)
            if method == 'PUT':
                url = string(data, 'base_url', 500).rstrip('/')
                model = string(data, 'model', 200)
                if bool(url) != bool(model):
                    fail(400, '服务地址和模型名必须一起填写或一起清空')
                if url:
                    validate_url(url)
                key = string(data, 'api_key', 2000)
                # A different endpoint must never inherit a previous provider's secret.
                key = key or (cfg['api_key'] if url == cfg['base_url'] and not data.get('clear_key') else '')
                cfg = {'base_url': url, 'model': model, 'api_key': key, 'revision': secrets.token_hex(8)}
                c.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(cfg),))
            elif method != 'GET':
                fail(405, '不支持此操作')
            return public_config(cfg)
        if segments == ['bases']:
            if method == 'GET':
                fp = fingerprint(config(c))
                return {'items': [dict(r) for r in c.execute('''SELECT b.*,
                    (SELECT count(*) FROM documents d WHERE d.kb_id=b.id) AS document_count,
                    (SELECT count(*) FROM chunks ch WHERE ch.kb_id=b.id) AS chunk_count,
                    (SELECT count(*) FROM chunks ch WHERE ch.kb_id=b.id AND ch.fingerprint=?) AS vector_count
                    FROM bases b ORDER BY b.created_at,b.id''', (fp,))]}
            if method == 'POST':
                kb_id = secrets.token_hex(8)
                name, desc, size, overlap, top_k = validate_base(data)
                c.execute('INSERT INTO bases VALUES(?,?,?,?,?,?,?)', (kb_id, name, desc, size, overlap, top_k, now()))
                return base(c, kb_id)
        if len(segments) >= 2 and segments[0] == 'bases':
            kb = base(c, segments[1])
            if len(segments) == 3 and segments[2] == 'qa':
                if method == 'GET':
                    query = params.get('q',[''])[0][:200]
                    try: offset = max(0,int(params.get('offset',['0'])[0]))
                    except ValueError: fail(400,'分页参数不正确')
                    publication=params.get('publication',[''])[0]
                    predicate=' AND publication=?' if publication else ''
                    args=[kb['id'],query]+([publication] if publication else [])
                    total=c.execute('SELECT count(*) FROM qa_entries WHERE kb_id=? AND instr(lower(question),lower(?))>0'+predicate,args).fetchone()[0]
                    items=[dict(row) for row in c.execute('SELECT * FROM qa_entries WHERE kb_id=? AND instr(lower(question),lower(?))>0'+predicate+' ORDER BY id DESC LIMIT 20 OFFSET ?',[*args,offset])]
                    return {'items':items,'total':total,'offset':offset,'limit':20}
                if method == 'POST':
                    if c.execute('SELECT count(*) FROM qa_entries WHERE kb_id=?',(kb['id'],)).fetchone()[0] >= 1000:
                        fail(400,'每个知识库最多1000条 QA')
                    return save_qa(c,kb['id'],data)
            if len(segments) == 3 and segments[2] == 'entities':
                if method == 'PUT':
                    if 'expected_items' in data and data['expected_items'] != entity_catalog(c, kb['id']):
                        fail(409, '别名表已在其他页面更新，请重试以保留最新修改')
                    try:
                        items = entities.validate(data.get('items'))
                    except ValueError as exc:
                        fail(400, str(exc))
                    c.execute('INSERT OR REPLACE INTO entity_catalog VALUES(?,?)', (kb['id'], json.dumps(items)))
                elif method != 'GET':
                    fail(405, '不支持此操作')
                return {'items': entity_catalog(c, kb['id'])}
            if len(segments) == 2:
                if method == 'GET':
                    return kb
                if method == 'DELETE':
                    c.execute('DELETE FROM bases WHERE id=?', (kb['id'],))
                    return {'deleted': True}
                if method == 'PUT':
                    name, desc, size, overlap, top_k = validate_base(data)
                    c.execute('UPDATE bases SET name=?,description=?,chunk_size=?,overlap=?,top_k=? WHERE id=?',
                              (name, desc, size, overlap, top_k, kb['id']))
                    updated = base(c, kb['id'])
                    if (size, overlap) != (kb['chunk_size'], kb['overlap']):
                        documents = c.execute('SELECT * FROM documents WHERE kb_id=?', (kb['id'],)).fetchall()
                        c.execute('DELETE FROM chunks WHERE kb_id=?', (kb['id'],))
                        for d in documents:
                            index_document(c, dict(d), updated)
                    return updated
            if len(segments) == 3 and segments[2] == 'documents':
                if method == 'GET':
                    query = params.get('q', [''])[0][:200]
                    return {'items': [dict(r) for r in c.execute('''SELECT d.id,d.kb_id,d.title,d.source,d.updated_at,
                        length(d.content) AS chars,(SELECT count(*) FROM chunks ch WHERE ch.doc_id=d.id) AS chunk_count
                        FROM documents d WHERE kb_id=? AND (instr(lower(title),lower(?))>0 OR instr(lower(content),lower(?))>0)
                        ORDER BY updated_at DESC,id''', (kb['id'], query, query))]}
                if method == 'POST':
                    return save_document(c, kb, data)
        if len(segments) == 2 and segments[0] == 'qa':
            item = c.execute('SELECT * FROM qa_entries WHERE id=?',(segments[1],)).fetchone()
            if not item: fail(404,'QA 不存在')
            if method == 'GET': return dict(item)
            if data.get('revision') and data['revision'] != item['revision']:
                fail(409,'此 QA 已在其他页面修改，请重新加载后编辑')
            if method == 'PUT': return save_qa(c,item['kb_id'],data,item['id'])
            if method == 'DELETE':
                c.execute('DELETE FROM qa_entries WHERE id=?',(item['id'],))
                return {'deleted':True}
        if len(segments) == 2 and segments[0] == 'documents':
            d = c.execute('SELECT * FROM documents WHERE id=?', (segments[1],)).fetchone()
            if not d:
                fail(404, '文档不存在')
            if method == 'GET':
                return dict(d) | {'chunks': [dict(r) for r in c.execute('SELECT id,ordinal,content FROM chunks WHERE doc_id=? ORDER BY ordinal', (d['id'],))]}
            if method == 'PUT':
                return save_document(c, base(c, d['kb_id']), data, d['id'])
            if method == 'DELETE':
                c.execute('DELETE FROM documents WHERE id=?', (d['id'],))
                return {'deleted': True}
    fail(404, '接口不存在')


def save_qa(c, kb_id, data, qa_id=None):
    question, answer = string(data,'question',1000,True), string(data,'answer',10000,True)
    revision = secrets.token_hex(8)
    if qa_id is None:
        qa_id = c.execute('INSERT INTO qa_entries(kb_id,question,answer,revision,updated_at) VALUES(?,?,?,?,?)',(kb_id,question,answer,revision,now())).lastrowid
    else:
        c.execute("UPDATE qa_entries SET question=?,answer=?,revision=?,updated_at=?,updated_by='manual' WHERE id=?",(question,answer,revision,now(),qa_id))
        c.execute('DELETE FROM qa_fts WHERE rowid=?',(qa_id,))
    # Never index A, including when updating or rebuilding a question.
    c.execute('INSERT INTO qa_fts(rowid,question) VALUES(?,?)',(qa_id,' '.join(tokens(question))))
    return dict(c.execute('SELECT * FROM qa_entries WHERE id=?',(qa_id,)).fetchone())


def validate_base(data):
    size = integer(data, 'chunk_size', 600, 100, 2000)
    overlap = integer(data, 'overlap', 80, 0, size // 2 - 1)
    return (string(data, 'name', 100, True), string(data, 'description', 1000), size, overlap,
            integer(data, 'top_k', 5, 1, 20))


def save_document(c, kb, data, doc_id=None):
    doc_id = doc_id or secrets.token_hex(8)
    title = string(data, 'title', 200, True)
    content = string(data, 'content', 100000, True)
    source = string(data, 'source', 1000)
    c.execute('''INSERT INTO documents VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        title=excluded.title,content=excluded.content,source=excluded.source,updated_at=excluded.updated_at''',
              (doc_id, kb['id'], title, content, source, now()))
    document = dict(c.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone())
    index_document(c, document, kb)
    return {'id': doc_id, 'kb_id': kb['id'], 'title': title}


class Handler(BaseHTTPRequestHandler):
    server_version = 'Knowledge/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(70)

    def log_message(self, fmt, *args):
        # Avoid logging user queries or credentials.
        print(f'{self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ""}', flush=True)

    def reply(self, status, payload, content_type='application/json; charset=utf-8'):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def handle_request(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.path.startswith('/knowledge/sticker-files/'):
                filename=parsed.path.removeprefix('/knowledge/sticker-files/')
                if self.command not in ('GET','HEAD') or not re.fullmatch(r'[a-f0-9]{32}\.(png|jpg|gif)',filename):fail(404,'图片不存在')
                file=DATA/'stickers'/filename
                if not file.is_file():fail(404,'图片不存在')
                self.reply(200,file.read_bytes(),'image/gif' if filename.endswith('.gif') else 'image/png' if filename.endswith('.png') else 'image/jpeg')
                return
            if not parsed.path.startswith('/knowledge/api/'):
                assets = {'/knowledge/': ('index.html', 'text/html'), '/knowledge/index.html': ('index.html', 'text/html'),
                          '/knowledge/app.js': ('app.js', 'text/javascript'), '/knowledge/style.css': ('style.css', 'text/css')}
                if parsed.path not in assets or self.command not in ('GET', 'HEAD'):
                    fail(404, '页面不存在')
                filename, mime = assets[parsed.path]
                self.reply(200, (STATIC / filename).read_bytes(), mime + '; charset=utf-8')
                return
            supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
            admin = bool(ADMIN_TOKEN) and hmac.compare_digest(supplied.encode(), ADMIN_TOKEN.encode())
            reader = bool(READ_TOKEN) and hmac.compare_digest(supplied.encode(), READ_TOKEN.encode())
            learner = bool(LEARN_TOKEN) and hmac.compare_digest(supplied.encode(), LEARN_TOKEN.encode())
            if not admin and not reader and not learner:
                fail(401, '请输入有效的访问密钥')
            if learner and not admin and not (parsed.path == '/knowledge/api/learning/events' and self.command == 'POST'):
                fail(403, '学习密钥仅可提交聊天事件')
            if not admin and not learner and not (parsed.path in ('/knowledge/api/retrieve', '/knowledge/api/answer', '/knowledge/api/trace-delivery') and self.command == 'POST'):
                fail(403, '召回密钥仅可调用检索接口')
            if parsed.path=='/knowledge/api/stickers/upload' and self.command=='POST':
                if self.headers.get('Transfer-Encoding'):fail(400,'不支持分块请求体')
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<=stickers.MAX_UPLOAD:fail(413,'图片不能超过 5 MB')
                name=parse_qs(parsed.query).get('name',[''])[0]
                with WRITE_LOCK:
                    try:target=stickers.save_upload(DATA/'stickers',self.rfile.read(length))
                    except ValueError as exc:fail(400,str(exc))
                try:result=api('POST','/knowledge/api/stickers',{'name':name,'path':'/knowledge/sticker-files/'+target.name},{})
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                self.reply(200,result)
                return
            data = {}
            if self.command in ('POST', 'PUT') or (self.command == 'DELETE' and self.headers.get('Content-Length', '0') != '0'):
                if self.headers.get('Transfer-Encoding'):
                    fail(400, '不支持分块请求体')
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 1_000_000:
                    fail(413, '请求体过大或为空')
                if self.headers.get_content_type() != 'application/json':
                    fail(415, '请使用 application/json')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    fail(400, '请求体必须是 JSON 对象')
            self.reply(200, api(self.command, parsed.path, data, parse_qs(parsed.query)))
        except Problem as exc:
            self.reply(exc.status, {'error': exc.message})
        except (ValueError, UnicodeError):
            self.reply(400, {'error': '请求格式不正确'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as exc:
            print(f'Internal error: {type(exc).__name__}', flush=True)
            self.reply(500, {'error': '服务暂时不可用，请稍后再试'})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_request


if __name__ == '__main__':
    if len(ADMIN_TOKEN) < 24 or len(READ_TOKEN) < 24 or ADMIN_TOKEN == READ_TOKEN:
        raise SystemExit('Set distinct KB_ADMIN_TOKEN and KB_READ_TOKEN (at least 24 characters each)')
    os.umask(0o077)
    initialize()
    threading.Thread(target=trace_cleanup, daemon=True).start()
    threading.Thread(target=learning.worker, args=(sys.modules[__name__],), daemon=True).start()
    port = int(os.environ.get('KB_PORT', '8765'))
    print(f'Knowledge listening on 127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
