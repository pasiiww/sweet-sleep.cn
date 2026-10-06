"""Business tools shared by LangChain and MCP; callers enforce identity/permissions.

This module never loads env files or imports the HTTP server. The caller supplies
its application instance, so adapters share the same data directory and locks.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import time
from urllib import request as urlrequest, error as urlerror

import ba_wiki
import entities
import memories
import execution_budget


def bounded_int(args, key, default, low, high):
    value = args.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{key} 必须是 {low} 到 {high} 之间的整数')
    return value


def search_knowledge(app, kb_id, query, *, original_query='', top_k=5, catalog=None, exclude_ids=None):
    started = time.monotonic()
    execution_budget.check()
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError('query 必须为1到2000字符')
    if not isinstance(original_query, str) or len(original_query) > 2000:
        raise ValueError('original_query 最多2000字符')
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError('top_k 必须为1到20')
    if catalog is None:
        with app.db() as conn:
            app.base(conn, kb_id)
            catalog = entities.Catalog(app.entity_catalog(conn, kb_id))
    result = app.search_terms(kb_id, [catalog.normalize(query)],
                              original_query=catalog.normalize(original_query or query), catalog=catalog,
                              max_results=top_k, context_chars=12000 if top_k > 8 else 6000,
                              exclude_ids=exclude_ids)
    execution_budget.check()
    rows = result['results']
    context = '\n\n'.join(f'[{row["citation"]}] {row["title"]} (document={row["document_id"]}, chunk={row["chunk_id"]})\n{row["content"]}'
                          for row in rows)
    return {**result, 'query': catalog.normalize(query), 'kb_id': kb_id, 'mode': 'keyword',
            'context': context, 'query_groups': [], 'score_type': 'reranked',
            'elapsed_ms': round((time.monotonic() - started) * 1000)}


def search_ba_wiki(query, source='auto', limit=3):
    execution_budget.check()
    if not isinstance(query, str) or not query.strip() or len(query) > 120:
        raise ValueError('query 必须为1到120字符')
    return ba_wiki.search(query, source=source, limit=limit)


def manage_memory(app, memory_scope, action, content='', *, member_openid='', subject='member'):
    if subject not in ('member', 'group'):
        raise ValueError('subject 必须为 member 或 group')
    execution_budget.check()
    with execution_budget.locked(app.WRITE_LOCK), app.db() as conn:
        execution_budget.check()
        return memories.apply(conn, memory_scope, action, content,
                              member_openid=member_openid if subject == 'member' else '')


def get_recent_chat_messages(app, args, *, kb_id=''):
    execution_budget.check()
    group = args.get('group_id')
    if not isinstance(group, str) or not group or len(group) > 128:
        raise ValueError('group_id 必须是有效群标识')
    hours = bounded_int(args, 'hours', 24, 1, 168)
    limit = bounded_int(args, 'limit', 100, 1, 400)
    offset = bounded_int(args, 'offset', 0, 0, 5000)
    contains = args.get('contains', '')
    if not isinstance(contains, str) or len(contains) > 100:
        raise ValueError('contains 最多100字符')
    pinned_only = args.get('pinned_only', False)
    if type(pinned_only) is not bool:
        raise ValueError('pinned_only 必须是布尔值')
    member_ids = args.get('member_ids', [])
    if not isinstance(member_ids, list) or len(member_ids) > 50 or any(not isinstance(m, str) or len(m) > 128 for m in member_ids):
        raise ValueError('member_ids 最多包含50个有效成员ID')
    if pinned_only and member_ids:
        raise ValueError('pinned_only 与 member_ids 只能选择一个')
    db = app.DATA / 'knowledge.db'
    current = datetime.now(timezone.utc).timestamp()
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=execution_budget.timeout(5)) as conn:
        if pinned_only:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_pinned_members'").fetchone()
            member_ids = [r[0] for r in conn.execute('SELECT member_id FROM mcp_pinned_members WHERE group_id=?', (group,)).fetchall()] if exists else []
            if not member_ids:
                return {'group_id': group, 'count': 0, 'total': 0, 'hours': hours, 'pinned_only': True, 'items': []}
        where = 'group_id=? AND at>? AND at>=?'
        params = [group, current-hours*3600, current-7*86400]
        if kb_id:
            where += ' AND kb_id=?'
            params.append(kb_id)
        if contains:
            where += ' AND instr(lower(content),lower(?))>0'
            params.append(contains)
        if member_ids:
            where += ' AND member_id IN (' + ','.join('?' for _ in member_ids) + ')'
            params.extend(member_ids)
        total = conn.execute('SELECT count(*) FROM learning_events WHERE ' + where, params).fetchone()[0]
        rows = conn.execute('SELECT message_id,member_id,member_name,content,at FROM learning_events WHERE ' + where + ' ORDER BY at DESC,id DESC LIMIT ? OFFSET ?',
            [*params, limit, offset]).fetchall()
    rows.reverse()
    return {'group_id': group, 'count': len(rows), 'total': total, 'hours': hours, 'offset': offset, 'pinned_only': pinned_only,
            'items': [{'message_id': row[0], 'member_id': row[1], 'member_name': row[2],
                       'at': datetime.fromtimestamp(row[4], timezone.utc).isoformat(), 'content': row[3]}
                      for row in rows]}


def qq_moderation_request(app, action, payload):
    execution_budget.check()
    token = getattr(app, 'MODERATION_TOKEN', '') or os.environ.get('KB_MODERATION_TOKEN', '')
    if not token:
        raise ValueError('QQ 群管理通道未配置 KB_MODERATION_TOKEN')
    if action not in ('recall', 'warn'):
        raise ValueError('不支持此群管理操作')
    req = urlrequest.Request('http://127.0.0.1:8766/mcp/' + action,
        data=json.dumps(payload, ensure_ascii=False).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urlrequest.urlopen(req, timeout=execution_budget.timeout(20)) as response:
            raw = execution_budget.read_response(response, 65536, socket_timeout=20)
        if len(raw) > 65536:
            raise ValueError('QQ 群管理接口返回过大')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('QQ 群管理接口返回格式错误')
        return result
    except urlerror.HTTPError as exc:
        raise ValueError('QQ 群管理操作失败，HTTP ' + str(exc.code)) from None
    except (urlerror.URLError, TimeoutError, json.JSONDecodeError):
        execution_budget.check()
        raise ValueError('QQ 机器人群管理通道暂时不可用') from None


def moderation_action(app, name, args, *, api_call=None, qq_call=None):
    execution_budget.check()
    if api_call is None:
        api_call = lambda method, path, data=None: app.api(method, '/knowledge/api/' + path, data or {}, {})
    if qq_call is None:
        qq_call = lambda action, payload: qq_moderation_request(app, action, payload)
    if name == 'record_harassment_count':
        if set(args) - {'group_id', 'message_id', 'terms', 'candidates'}:
            raise ValueError('record_harassment_count 包含未知参数')
        group, message_id = args.get('group_id'), args.get('message_id')
        if not isinstance(group, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', group):
            raise ValueError('group_id 必须是有效群 OpenID')
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_.!:-]{1,200}', message_id):
            raise ValueError('message_id 格式不正确')
        terms, candidates = args.get('terms'), args.get('candidates', [])
        if not isinstance(terms, list) or not 1 <= len(terms) <= 20:
            raise ValueError('terms 必须包含1到20个词条')
        if not isinstance(candidates, list) or len(candidates) > 20:
            raise ValueError('candidates 最多包含20个词条')
        event_hash = hashlib.sha256((group + '\0' + message_id).encode()).hexdigest()
        return api_call('POST', 'moderation-recalls', {'event_hash': event_hash,
                    'terms': terms, 'candidates': candidates})
    if name == 'recall_group_message':
        group, message_id = args.get('group_id'), args.get('message_id')
        if not isinstance(group, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', group):
            raise ValueError('group_id 必须是有效群 OpenID')
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_.!:-]{1,200}', message_id):
            raise ValueError('message_id 格式不正确')
        return qq_call('recall', {'group_id': group, 'message_id': message_id})
    if name == 'send_group_warning':
        group, message_id = args.get('group_id'), args.get('message_id')
        if not isinstance(group, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', group):
            raise ValueError('group_id 必须是有效群 OpenID')
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_.!:-]{1,200}', message_id):
            raise ValueError('message_id 格式不正确')
        settings = api_call('GET', 'moderation-settings')
        if not isinstance(settings, dict) or settings.get('harassment_warning_enabled') is not True:
            return {'sent': False, 'reason': 'harassment_warning_disabled'}
        return qq_call('warn', {'group_id': group, 'message_id': message_id})
    raise ValueError('不支持的群管理操作')
