"""Idempotent, bounded member-impression maintenance from reviewed group batches."""
import json
import re
import time

import answers
import memories
import traces
from group_task_prompts import COMMON_RULES

MODEL_TIMEOUT = 60
MAX_COMPLETION_TOKENS = 4096
MAX_UPDATES = 15
MAX_CHARS = 80
RETENTION = 7 * 86400
TRANSCRIPT_LINE = re.compile(r'^\[消息([1-9][0-9]{0,2})\] \[(speaker[1-9][0-9]{0,2})\] .+：.+$')

PROMPT = f'''本次任务：只依据本批提供的群成员本人发言更新群友印象。只输出 JSON：{{"member_impressions":[{{"key":"speaker1","action":"append","content":"一句新观察"}}]}}。不要生成聊天总结、群公共记忆或审核结论。
每轮最多{MAX_UPDATES}名成员，每人1项，每项 content 最多{MAX_CHARS}字，所有 content 合计最多{MAX_UPDATES * MAX_CHARS}字。只选择有明确新依据、值得长期保留的信息，优先信息增量明显的少数成员；不逐一为所有成员生成印象，不重复已有印象，不凑满名额，没有新依据则返回空数组。
只针对 members 中本批实际发言的编号，依据本人发言更新兴趣、表达习惯和互动偏好。新增独立事实用 append，只新增一句；合并、纠错或压缩用 replace，在字数上限内保留完整含义，做不到时跳过并保留旧印象。不要按同名、引用、他人发言或历史编号推断归属，不记录敏感信息、贬损标签或无依据性格判断。聊天可能截短，不补写缺失内容。'''
PROMPT += '\n\n' + COMMON_RULES


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS group_impression_batches (
        kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
        group_id TEXT NOT NULL, batch_id TEXT NOT NULL, created REAL NOT NULL,
        result TEXT NOT NULL, PRIMARY KEY(kb_id,group_id,batch_id))''')
    cleanup(c)


def cleanup(c):
    c.execute('DELETE FROM group_impression_batches WHERE created<=?', (time.time() - RETENTION,))


def _cached(c, kb, group, batch):
    row = c.execute('SELECT result FROM group_impression_batches WHERE kb_id=? AND group_id=? AND batch_id=?',
                    (kb, group, batch)).fetchone()
    return json.loads(row[0]) if row else None


def _members(app, data, transcript):
    raw = data.get('members', [])
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        app.fail(400, 'members 格式无效')
    result, owners = {}, set()
    active_keys = {match.group(2) for line in transcript.splitlines()
                   if (match := TRANSCRIPT_LINE.fullmatch(line))}
    for item in raw:
        if not isinstance(item, dict):
            app.fail(400, '成员格式无效')
        key, owner = item.get('key'), item.get('openid')
        if (not isinstance(key, str) or not re.fullmatch(r'speaker[1-9][0-9]{0,2}', key)
                or not isinstance(owner, str) or not owner or len(owner) > 128
                or key in result or owner in owners):
            app.fail(400, '成员标识无效')
        owners.add(owner)
        if key in active_keys:
            result[key] = {'openid': owner, 'name': memories.clean_member_name(item.get('name', ''))}
    if transcript and not result:
        app.fail(400, '没有可映射的发言成员')
    return result


def _apply(c, scope, members, updates):
    processed = set()
    for item in updates[:100]:
        if len(processed) >= MAX_UPDATES or not isinstance(item, dict):
            continue
        key, action, content = item.get('key'), item.get('action'), item.get('content')
        if not isinstance(key, str) or key not in members or key in processed:
            continue
        if action not in ('append', 'replace') or not isinstance(content, str):
            continue
        content = content.strip()
        if not content or len(content) > MAX_CHARS:
            continue
        member = members[key]
        update = memories.append_impression if action == 'append' else memories.update_impression
        if update(c, scope, member['openid'], content):
            processed.add(key)
            memories.remember_member(c, scope, member['openid'], member['name'])
    return len(processed), len(updates) - len(processed)


def respond(app, data):
    kb = app.string(data, 'kb_id', 80, True)
    group = app.string(data, 'group_id', 128, True)
    batch = app.string(data, 'batch_id', 64, True)
    if not re.fullmatch(r'[a-f0-9]{64}', batch):
        app.fail(400, 'batch_id 格式无效')
    if 'transcript' not in data:
        app.fail(400, 'transcript 必须提供')
    transcript = app.string(data, 'transcript', 15000)
    lines = transcript.splitlines()
    matches = [TRANSCRIPT_LINE.fullmatch(line) for line in lines]
    indices = [int(match.group(1)) for match in matches if match]
    if (len(lines) > 100 or any(not match for match in matches)
            or indices != sorted(set(indices)) or any(index > 100 for index in indices)):
        app.fail(400, 'transcript 消息格式无效')
    members = _members(app, data, transcript)
    scope = memories.scope(kb, 'qq_group', '', group)
    started = time.monotonic()

    with app.WRITE_LOCK, app.db() as c:
        app.base(c, kb)
        cleanup(c)
        cached = _cached(c, kb, group, batch)
        if cached:
            return cached
        cfg = app.answer_config(c)
        secrets_to_hide = [app.ADMIN_TOKEN, app.READ_TOKEN, app.LEARN_TOKEN, app.MODERATION_TOKEN,
                           cfg.get('api_key', ''), app.config(c).get('api_key', '')]
        maintenance_enabled = cfg.get('impression_maintenance_enabled', True) is True
        trace_id, _ = traces.create(c, kb, '自动维护群友印象（100条发言）', {
            'origin': 'qq_group', 'user_id': '', 'group_id': group, 'session_id': batch})
        c.execute('UPDATE answer_traces SET delivery=? WHERE id=?', ('not_applicable', trace_id))
        enabled = maintenance_enabled and memories.enabled(c, scope)
        context = [{'key': key, 'name': member['name'],
                    'impression': memories.impression(c, scope, member['openid'])}
                   for key, member in members.items()] if enabled else []

    details = {'model_calls': [], 'retrievals': [], 'model': cfg.get('model', ''),
               'impressions': {'task': 'memory_only', 'member_count': len(members),
                               'task_enabled': maintenance_enabled,
                               'transcript_chars': len(transcript), 'message_count': len(lines),
                               'update_limit': MAX_UPDATES, 'max_chars': MAX_CHARS,
                               'updated': 0, 'ignored': 0,
                               'model_timeout_seconds': MODEL_TIMEOUT,
                               'max_completion_tokens': MAX_COMPLETION_TOKENS}}
    acquired, updates = False, []
    try:
        if not maintenance_enabled:
            response = {'ok': True, 'mode': 'impressions', 'reason': 'impressions_disabled',
                        'answer': '每100条的群友印象维护已关闭，跳过本批写入。'}
        elif not transcript:
            response = {'ok': True, 'mode': 'impressions', 'reason': 'no_safe_messages',
                        'answer': '本批发言均已由独立审核任务排除，未更新印象。'}
        elif not enabled:
            response = {'ok': True, 'mode': 'impressions', 'reason': 'memory_disabled',
                        'answer': '群聊记忆已关闭，跳过印象写入。'}
        elif not cfg.get('enabled') or not cfg.get('api_key'):
            response = {'ok': False, 'mode': 'unavailable', 'reason': 'model_disabled',
                        'answer': '印象模型未启用或配置。'}
        elif not app.ANSWER_SLOTS.acquire(blocking=False):
            response = {'ok': False, 'mode': 'busy', 'reason': 'model_busy', 'answer': '印象模型当前繁忙。'}
        else:
            acquired = True
            output = answers.model_call(cfg | {'_stage': 'group_impressions', '_trace': details,
                                               '_trace_hide_content': True,
                                               '_timeout': MODEL_TIMEOUT}, [
                {'role': 'system', 'content': PROMPT},
                {'role': 'user', 'content': json.dumps({'members': context, 'transcript': transcript},
                                                       ensure_ascii=False)}],
                json_mode=True, max_tokens=MAX_COMPLETION_TOKENS, allow_small_max_tokens=True)
            try:
                parsed = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', output.strip(), flags=re.I))
            except (ValueError, AttributeError):
                raise answers.ModelError('invalid_impressions') from None
            if not isinstance(parsed, dict) or not isinstance(parsed.get('member_impressions'), list):
                raise answers.ModelError('invalid_impressions')
            updates = parsed['member_impressions']
            response = {'ok': True, 'mode': 'impressions', 'reason': 'impressions_updated', 'answer': ''}
    except answers.ModelError as exc:
        response = {'ok': False, 'mode': 'error', 'reason': str(exc),
                    'answer': '本批印象更新失败，等待后台重试。'}
    except Exception as exc:
        details['error_type'] = type(exc).__name__
        response = {'ok': False, 'mode': 'error', 'reason': 'internal_error', 'answer': '印象服务内部异常。'}
    finally:
        if acquired:
            app.ANSWER_SLOTS.release()

    try:
        with app.WRITE_LOCK, app.db() as c:
            cached = _cached(c, kb, group, batch)
            if cached:
                response = cached
            elif response['ok']:
                if response['reason'] == 'impressions_updated':
                    current_enabled = memories.enabled(c, scope)
                    if enabled and current_enabled:
                        memories.register_scope(c, kb, 'qq_group', '', group)
                        updated, ignored = _apply(c, scope, members, updates)
                        details['impressions'].update(updated=updated, ignored=ignored)
                        response['answer'] = f'已更新{updated}位成员的印象，忽略{ignored}项。'
                    elif not current_enabled:
                        response.update(reason='memory_disabled', answer='群聊记忆已关闭，跳过印象写入。')
                response.update(trace_id=trace_id, updated=details['impressions']['updated'],
                                ignored=details['impressions']['ignored'])
                c.execute('INSERT INTO group_impression_batches VALUES(?,?,?,?,?)',
                          (kb, group, batch, time.time(), json.dumps(response, ensure_ascii=False)))
            traces.finish(c, trace_id, traces.redact(response, secrets_to_hide),
                          traces.redact(details, secrets_to_hide), round((time.monotonic() - started) * 1000))
    except Exception as exc:
        details['error_type'] = type(exc).__name__
        response = {'ok': False, 'mode': 'error', 'reason': 'internal_error',
                    'answer': '印象写入失败，等待后台重试。', 'trace_id': trace_id}
        with app.WRITE_LOCK, app.db() as c:
            traces.finish(c, trace_id, response, traces.redact(details, secrets_to_hide),
                          round((time.monotonic() - started) * 1000))
    return response | {'trace_id': response.get('trace_id', trace_id)}
