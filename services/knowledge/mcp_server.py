#!/usr/bin/env python3
"""stdio MCP bridge for recent QQ group messages and knowledge CRUD.

Runs on the knowledge server and reads credentials from its protected env file.
No external Python package is required.
"""
from datetime import datetime, timedelta, timezone
from urllib import error as urlerror, request as urlrequest
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import traceback
from urllib.parse import quote


ENV_FILE = Path(os.environ.get('SWEET_MCP_ENV_FILE', '/etc/sweet-knowledge.env'))
ALLOWED_ENV = {'KB_ADMIN_TOKEN', 'KB_READ_TOKEN', 'KB_LEARN_TOKEN', 'KB_MODERATION_TOKEN', 'KB_DATA_DIR', 'KB_STATIC_DIR'}


def load_environment():
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if len(words) != 1 or '=' not in words[0]:
            continue
        key, value = words[0].split('=', 1)
        if key in ALLOWED_ENV:
            os.environ.setdefault(key, value)


load_environment()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as knowledge  # noqa: E402
import ba_wiki  # noqa: E402
import tool_service  # noqa: E402


PROTOCOL_VERSION = '2025-03-26'
TOOLS = [
    {'name': 'list_knowledge_bases', 'description': '列出知识库及文档、QA数量。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'get_knowledge_base', 'description': '读取一个知识库配置。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}}, 'required': ['kb_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'create_knowledge_base', 'description': '新建知识库。',
     'inputSchema': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'description': {'type': 'string'}, 'chunk_size': {'type': 'integer'}, 'overlap': {'type': 'integer'}, 'top_k': {'type': 'integer'}}, 'required': ['name'], 'additionalProperties': False}},
    {'name': 'update_knowledge_base', 'description': '修改知识库名称、说明或分块参数。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}, 'name': {'type': 'string'}, 'description': {'type': 'string'}, 'chunk_size': {'type': 'integer'}, 'overlap': {'type': 'integer'}, 'top_k': {'type': 'integer'}}, 'required': ['kb_id', 'name'], 'additionalProperties': False}},
    {'name': 'delete_knowledge_base', 'description': '永久删除知识库及其文档、QA和索引。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}}, 'required': ['kb_id'], 'additionalProperties': False},
     'annotations': {'destructiveHint': True}},
    {'name': 'search_knowledge', 'description': '在指定知识库按关键词检索文档和已生效问答，返回可引用原文片段。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}, 'query': {'type': 'string'}, 'original_query': {'type': 'string', 'maxLength': 2000}, 'top_k': {'type': 'integer', 'minimum': 1, 'maximum': 20}}, 'required': ['kb_id', 'query'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'search_ba_wiki', 'description': '只读检索蔚蓝档案角色、剧情和玩法资料。auto 优先 GameKee，未命中时回退日文 Blue Archive Wikiru；也可指定 wiki 来源。',
     'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 120},
         'source': {'type': 'string', 'enum': ['auto', 'gamekee', 'bluearchivewiki'], 'default': 'auto'},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 4}}, 'required': ['query'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'search_documents', 'description': '按标题或正文搜索文档，返回摘要与ID。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}, 'query': {'type': 'string'}}, 'required': ['kb_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'get_document', 'description': '读取文档正文及其已切分片段。',
     'inputSchema': {'type': 'object', 'properties': {'document_id': {'type': 'string'}}, 'required': ['document_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'create_document', 'description': '在指定知识库新增文档并建立检索索引。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}, 'title': {'type': 'string'}, 'content': {'type': 'string'}, 'source': {'type': 'string'}}, 'required': ['kb_id', 'title', 'content'], 'additionalProperties': False}},
    {'name': 'update_document', 'description': '替换文档标题、正文或来源，并重建其检索索引。',
     'inputSchema': {'type': 'object', 'properties': {'document_id': {'type': 'string'}, 'title': {'type': 'string'}, 'content': {'type': 'string'}, 'source': {'type': 'string'}}, 'required': ['document_id', 'title', 'content'], 'additionalProperties': False}},
    {'name': 'delete_document', 'description': '永久删除指定文档及其检索索引。',
     'inputSchema': {'type': 'object', 'properties': {'document_id': {'type': 'string'}}, 'required': ['document_id'], 'additionalProperties': False},
     'annotations': {'destructiveHint': True}},
    {'name': 'search_qa', 'description': '按问题关键词查找知识库问答，可分页。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}, 'query': {'type': 'string'}, 'offset': {'type': 'integer'}, 'publication': {'type': 'string'}}, 'required': ['kb_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'get_qa', 'description': '读取一条知识库问答。',
     'inputSchema': {'type': 'object', 'properties': {'qa_id': {'type': 'integer'}}, 'required': ['qa_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'create_qa', 'description': '在知识库新增问答。',
     'inputSchema': {'type': 'object', 'properties': {'kb_id': {'type': 'string'}, 'question': {'type': 'string'}, 'answer': {'type': 'string'}}, 'required': ['kb_id', 'question', 'answer'], 'additionalProperties': False}},
    {'name': 'update_qa', 'description': '修改问答；可附 revision 避免覆盖他人新改动。',
     'inputSchema': {'type': 'object', 'properties': {'qa_id': {'type': 'integer'}, 'question': {'type': 'string'}, 'answer': {'type': 'string'}, 'revision': {'type': 'string'}}, 'required': ['qa_id', 'question', 'answer'], 'additionalProperties': False}},
    {'name': 'delete_qa', 'description': '永久删除指定问答。',
     'inputSchema': {'type': 'object', 'properties': {'qa_id': {'type': 'integer'}}, 'required': ['qa_id'], 'additionalProperties': False},
     'annotations': {'destructiveHint': True}},
    {'name': 'list_recent_chat_groups', 'description': '列出最近7天有成员发言的群及发言数量。',
     'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'get_recent_chat_messages', 'description': '按条数读取群内最近聊天。消息最多保留7天；单次最多400条，按时间正序返回，附发送者昵称（最多12字）。可以只看置顶成员，或指定成员ID。',
     'inputSchema': {'type': 'object', 'properties': {'group_id': {'type': 'string'}, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 168}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 400}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 5000}, 'contains': {'type': 'string'}, 'pinned_only': {'type': 'boolean'}, 'member_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 50}}, 'required': ['group_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'recall_group_message', 'description': '撤回指定群的一条消息。QQ 通常只允许撤回较近的消息，机器人需要具备群管理员权限。',
     'inputSchema': {'type': 'object', 'properties': {'group_id': {'type': 'string'}, 'message_id': {'type': 'string'}}, 'required': ['group_id', 'message_id'], 'additionalProperties': False},
     'annotations': {'destructiveHint': True}},
    {'name': 'send_group_warning', 'description': '向指定群消息发送固定的友善交流提醒。提醒引用该消息，批量操作时必须放在撤回之前；后台“性骚扰提醒”开关关闭时不会发送。',
     'inputSchema': {'type': 'object', 'properties': {'group_id': {'type': 'string'}, 'message_id': {'type': 'string'}}, 'required': ['group_id', 'message_id'], 'additionalProperties': False}},
    {'name': 'record_harassment_count', 'description': '按群消息 ID 幂等记录一次敏感词或性骚扰命中，不保存消息正文；同一消息可同时提交多个词条和待审核候选词。',
     'inputSchema': {'type': 'object', 'properties': {'group_id': {'type': 'string'}, 'message_id': {'type': 'string'}, 'terms': {'type': 'array', 'items': {'type': 'string', 'maxLength': 80}, 'minItems': 1, 'maxItems': 20}, 'candidates': {'type': 'array', 'items': {'type': 'string', 'maxLength': 80}, 'maxItems': 20}}, 'required': ['group_id', 'message_id', 'terms'], 'additionalProperties': False},
     'annotations': {'destructiveHint': True}},
    {'name': 'execute_admin_actions', 'description': '在一次 MCP 调用中按顺序执行1至8项管理员操作，可组合知识库写入、记录命中、撤回和受后台开关控制的提醒。若同时提醒和撤回同一消息，提醒必须排在撤回之前。每项单独返回成功或失败；某项失败不会撤销此前操作，也不会阻止后续项。',
     'inputSchema': {'type': 'object', 'properties': {'actions': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'type': 'object', 'properties': {'name': {'type': 'string', 'enum': ['create_knowledge_base', 'update_knowledge_base', 'delete_knowledge_base', 'create_document', 'update_document', 'delete_document', 'create_qa', 'update_qa', 'delete_qa', 'record_harassment_count', 'recall_group_message', 'send_group_warning']}, 'arguments': {'type': 'object'}}, 'required': ['name', 'arguments'], 'additionalProperties': False}}}, 'required': ['actions'], 'additionalProperties': False},
     'annotations': {'destructiveHint': True}},
    {'name': 'pin_chat_member', 'description': '在指定群置顶一位成员，之后可用 get_recent_chat_messages 的 pinned_only 查看其发言。member_id 可从聊天记录结果取得。',
     'inputSchema': {'type': 'object', 'properties': {'group_id': {'type': 'string'}, 'member_id': {'type': 'string'}, 'label': {'type': 'string'}, 'note': {'type': 'string'}}, 'required': ['group_id', 'member_id'], 'additionalProperties': False}},
    {'name': 'unpin_chat_member', 'description': '取消置顶指定群成员。',
     'inputSchema': {'type': 'object', 'properties': {'group_id': {'type': 'string'}, 'member_id': {'type': 'string'}}, 'required': ['group_id', 'member_id'], 'additionalProperties': False}},
    {'name': 'list_pinned_chat_members', 'description': '查看指定群已置顶的成员。',
     'inputSchema': {'type': 'object', 'properties': {'group_id': {'type': 'string'}}, 'required': ['group_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
]


def require_admin():
    if not knowledge.ADMIN_TOKEN:
        raise ValueError('知识库管理令牌未配置；请在 /etc/sweet-knowledge.env 配置 KB_ADMIN_TOKEN')


def api(method, path, data=None, params=None):
    require_admin()
    return knowledge.api(method, '/knowledge/api/' + path.lstrip('/'), data or {}, params or {})


def qq_moderation_request(action, payload):
    require_admin()
    return tool_service.qq_moderation_request(knowledge, action, payload)


def bounded_int(args, key, default, low, high):
    value = args.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{key} 必须是 {low} 到 {high} 之间的整数')
    return value


ADMIN_BATCH_TOOLS = {
    'create_knowledge_base', 'update_knowledge_base', 'delete_knowledge_base',
    'create_document', 'update_document', 'delete_document',
    'create_qa', 'update_qa', 'delete_qa',
    'record_harassment_count', 'recall_group_message', 'send_group_warning',
}


def tool(name, args):
    if not isinstance(args, dict):
        raise ValueError('arguments 必须是对象')
    if name == 'execute_admin_actions':
        require_admin()
        if set(args) != {'actions'}:
            raise ValueError('execute_admin_actions 只接受 actions 参数')
        actions = args.get('actions')
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise ValueError('actions 必须包含1到8项管理员操作')
        results = []
        for index, action in enumerate(actions):
            if (not isinstance(action, dict) or set(action) != {'name', 'arguments'}
                    or action.get('name') not in ADMIN_BATCH_TOOLS
                    or not isinstance(action.get('arguments'), dict)):
                results.append({'index': index, 'ok': False, 'error': '操作名称或参数格式不支持'})
                continue
            try:
                result = tool(action['name'], action['arguments'])
                results.append({'index': index, 'name': action['name'], 'ok': True, 'result': result})
            except Exception as exc:
                results.append({'index': index, 'name': action['name'], 'ok': False,
                                'error': str(exc)[:300] or type(exc).__name__})
        return {'completed': sum(1 for row in results if row['ok']), 'total': len(results), 'results': results}
    if name in ('record_harassment_count', 'recall_group_message', 'send_group_warning'):
        return tool_service.moderation_action(knowledge, name, args, api_call=api, qq_call=qq_moderation_request)
    if name == 'search_ba_wiki':
        query = args.get('query')
        source = args.get('source', 'auto')
        limit = bounded_int(args, 'limit', 3, 1, 4)
        if not isinstance(query, str) or not query.strip() or len(query) > 120:
            raise ValueError('query 必须为1到120字符')
        return tool_service.search_ba_wiki(query, source=source, limit=limit)
    if name == 'list_knowledge_bases':
        return api('GET', 'bases')
    if name == 'get_knowledge_base':
        return api('GET', 'bases/' + quote(args['kb_id'], safe=''))
    if name == 'create_knowledge_base':
        return api('POST', 'bases', args)
    if name == 'update_knowledge_base':
        kb_id = quote(args['kb_id'], safe='')
        data = {key: args[key] for key in ('name', 'description', 'chunk_size', 'overlap', 'top_k') if key in args}
        # Match the REST API's validated full-replacement settings.
        current = api('GET', 'bases/' + kb_id)
        return api('PUT', 'bases/' + kb_id, {key: data.get(key, current.get(key)) for key in ('name', 'description', 'chunk_size', 'overlap', 'top_k')})
    if name == 'delete_knowledge_base':
        return api('DELETE', 'bases/' + quote(args['kb_id'], safe=''))
    if name == 'search_knowledge':
        top_k = bounded_int(args, 'top_k', 5, 1, 20)
        require_admin()
        return tool_service.search_knowledge(knowledge, args['kb_id'], args['query'],
                                             original_query=args.get('original_query', ''), top_k=top_k)
    if name == 'search_documents':
        query = str(args.get('query', ''))[:200]
        return api('GET', 'bases/' + quote(args['kb_id'], safe='') + '/documents', params={'q': [query]})
    if name == 'get_document':
        return api('GET', 'documents/' + quote(args['document_id'], safe=''))
    if name == 'create_document':
        return api('POST', 'bases/' + quote(args['kb_id'], safe='') + '/documents', args)
    if name == 'update_document':
        doc_id = quote(args['document_id'], safe='')
        current = api('GET', 'documents/' + doc_id)
        data = {key: args.get(key, current.get(key, '')) for key in ('title', 'content', 'source')}
        return api('PUT', 'documents/' + doc_id, data)
    if name == 'delete_document':
        return api('DELETE', 'documents/' + quote(args['document_id'], safe=''))
    if name == 'search_qa':
        offset = bounded_int(args, 'offset', 0, 0, 1000000)
        params = {'q': [str(args.get('query', ''))[:200]], 'offset': [str(offset)]}
        if args.get('publication'):
            params['publication'] = [str(args['publication'])[:30]]
        return api('GET', 'bases/' + quote(args['kb_id'], safe='') + '/qa', params=params)
    if name == 'get_qa':
        qa_id = bounded_int(args, 'qa_id', 0, 1, 2**63-1)
        return api('GET', 'qa/' + str(qa_id))
    if name == 'create_qa':
        return api('POST', 'bases/' + quote(args['kb_id'], safe='') + '/qa', args)
    if name == 'update_qa':
        qa_id = bounded_int(args, 'qa_id', 0, 1, 2**63-1)
        return api('PUT', 'qa/' + str(qa_id), {key: args[key] for key in ('question', 'answer', 'revision') if key in args})
    if name == 'delete_qa':
        qa_id = bounded_int(args, 'qa_id', 0, 1, 2**63-1)
        return api('DELETE', 'qa/' + str(qa_id))
    if name == 'pin_chat_member':
        group, member = args.get('group_id'), args.get('member_id')
        if not isinstance(group, str) or not group or len(group) > 128:
            raise ValueError('group_id 必须是有效群标识')
        if not isinstance(member, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', member):
            raise ValueError('member_id 必须是有效 QQ 成员 OpenID')
        label, note = args.get('label', ''), args.get('note', '')
        if not isinstance(label, str) or len(label) > 100 or not isinstance(note, str) or len(note) > 200:
            raise ValueError('label 最多100字符，note 最多200字符')
        require_admin()
        with knowledge.db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS mcp_pinned_members (
                group_id TEXT NOT NULL, member_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', pinned_at REAL NOT NULL,
                PRIMARY KEY(group_id,member_id))''')
            count = conn.execute('SELECT count(*) FROM mcp_pinned_members WHERE group_id=?', (group,)).fetchone()[0]
            exists = conn.execute('SELECT 1 FROM mcp_pinned_members WHERE group_id=? AND member_id=?', (group, member)).fetchone()
            if not exists and count >= 100:
                raise ValueError('每个群最多置顶100位成员')
            conn.execute('''INSERT INTO mcp_pinned_members(group_id,member_id,label,note,pinned_at) VALUES(?,?,?,?,?)
                ON CONFLICT(group_id,member_id) DO UPDATE SET label=excluded.label,note=excluded.note,pinned_at=excluded.pinned_at''',
                (group, member, label, note, datetime.now(timezone.utc).timestamp()))
            return {'pinned': True, 'group_id': group, 'member_id': member, 'label': label, 'note': note}
    if name == 'unpin_chat_member':
        group, member = args.get('group_id'), args.get('member_id')
        if not isinstance(group, str) or not group or len(group) > 128 or not isinstance(member, str) or len(member) > 128:
            raise ValueError('group_id 或 member_id 无效')
        require_admin()
        with knowledge.db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS mcp_pinned_members (
                group_id TEXT NOT NULL, member_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', pinned_at REAL NOT NULL,
                PRIMARY KEY(group_id,member_id))''')
            changed = conn.execute('DELETE FROM mcp_pinned_members WHERE group_id=? AND member_id=?', (group, member)).rowcount
            return {'unpinned': bool(changed), 'group_id': group, 'member_id': member}
    if name == 'list_pinned_chat_members':
        group = args.get('group_id')
        if not isinstance(group, str) or not group or len(group) > 128:
            raise ValueError('group_id 必须是有效群标识')
        with knowledge.db() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_pinned_members'").fetchone()
            if not exists:
                return {'group_id': group, 'items': []}
            rows = conn.execute('SELECT member_id,label,note,pinned_at FROM mcp_pinned_members WHERE group_id=? ORDER BY pinned_at DESC,member_id', (group,)).fetchall()
        return {'group_id': group, 'items': [{'member_id': r[0], 'label': r[1], 'note': r[2],
            'pinned_at': datetime.fromtimestamp(r[3], timezone.utc).isoformat()} for r in rows]}
    if name == 'list_recent_chat_groups':
        limit = bounded_int(args, 'limit', 30, 1, 100)
        db = knowledge.DATA / 'knowledge.db'
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5) as conn:
            rows = conn.execute('''SELECT group_id,count(*) AS messages,max(at) AS latest
                FROM learning_events WHERE at>? GROUP BY group_id ORDER BY latest DESC LIMIT ?''',
                (datetime.now(timezone.utc).timestamp()-7*86400, limit)).fetchall()
        return {'items': [{'group_id': row[0], 'messages': row[1],
                           'latest_at': datetime.fromtimestamp(row[2],timezone.utc).isoformat()} for row in rows]}
    if name == 'get_recent_chat_messages':
        return tool_service.get_recent_chat_messages(knowledge, args)
    raise LookupError('未知 MCP 工具：' + name)


def response(msg_id, result):
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def handle(message):
    method = message.get('method')
    msg_id = message.get('id')
    params = message.get('params') or {}
    if method == 'initialize':
        requested = params.get('protocolVersion')
        version = requested if requested in ('2025-06-18', '2025-03-26', '2024-11-05') else PROTOCOL_VERSION
        return response(msg_id, {'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}},
                                 'serverInfo': {'name': 'sweet-sleep-knowledge', 'version': '1.0.0'},
                                 'instructions': '提供 QQ 群最近聊天读取、群消息撤回与受后台开关控制的提醒、性骚扰命中统计和候选敏感词审核，以及蔚蓝档案公开 Wiki 只读检索、绑定知识库/文档/QA 增删改查和群成员置顶工具。execute_admin_actions 可在一次调用中按序组合最多8项写入或群管理操作；结果会逐项报告，系统不提供禁言工具；管理员权限仅通过 SSH MCP 通道提供。'})
    if method == 'ping':
        return response(msg_id, {})
    if method == 'tools/list':
        return response(msg_id, {'tools': TOOLS})
    if method == 'tools/call':
        name = params.get('name', '')
        try:
            result = tool(name, params.get('arguments', {}))
            text = json.dumps(result, ensure_ascii=False, default=str)
            return response(msg_id, {'content': [{'type': 'text', 'text': text}], 'isError': False})
        except knowledge.Problem as exc:
            return response(msg_id, {'content': [{'type': 'text', 'text': exc.message}], 'isError': True})
        except (KeyError, ValueError, TypeError, LookupError) as exc:
            return response(msg_id, {'content': [{'type': 'text', 'text': str(exc) or type(exc).__name__}], 'isError': True})
        except Exception as exc:
            return response(msg_id, {'content': [{'type': 'text', 'text': '工具调用失败：' + type(exc).__name__}], 'isError': True})
    if method in ('notifications/initialized', 'notifications/cancelled', 'notifications/progress'):
        return None
    if msg_id is None:
        return None
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': -32601, 'message': 'Method not found'}}


def main():
    for line in sys.stdin.buffer:
        if len(line) > 8 * 1024 * 1024:
            continue
        try:
            message = json.loads(line)
            result = handle(message)
        except Exception:
            result = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()
