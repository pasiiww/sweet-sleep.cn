"""Independent, idempotent sexual-harassment review for recent group messages."""
import json
import math
import re
import time

import answers
from group_task_prompts import COMMON_PREFIX
import traces

MODEL_TIMEOUT = 60
MAX_COMPLETION_TOKENS = 2048
SCAN_WINDOW_SECONDS = 600
RETENTION = 7 * 86400

PROMPT = COMMON_PREFIX + f'''

本次任务：只判断给定聊天中最近{SCAN_WINDOW_SECONDS // 60}分钟的候选发言是否构成性骚扰或性邀约。只输出 JSON：{{"sexual_harassment_indices":[]}}，不要总结聊天、维护成员印象或输出解释。
重点识别邀请他人开房、明确邀请性行为或性服务、对群友提出露骨性要求或评价，以及用谐音、拆字、空格、符号、外语、缩写或暗示刻意回避敏感词表达的同类意思。结合前后文理解隐晦表达，但只标记 sexual_harassment_scan_indices 中给出的序号；其他消息只能用于理解语境。
对中性健康/科普讨论、反对或举报骚扰、普通亲密表达、与性无关的歧义词不要标记。只返回确实命中的候选序号，不要凑数。'''


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS group_harassment_batches (
        kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
        group_id TEXT NOT NULL, batch_id TEXT NOT NULL, created REAL NOT NULL,
        result TEXT NOT NULL, PRIMARY KEY(kb_id,group_id,batch_id))''')
    cleanup(c)


def cleanup(c):
    c.execute('DELETE FROM group_harassment_batches WHERE created<=?', (time.time() - RETENTION,))


def _cached(c, kb, group, batch):
    row = c.execute('SELECT result FROM group_harassment_batches WHERE kb_id=? AND group_id=? AND batch_id=?',
                    (kb, group, batch)).fetchone()
    return json.loads(row[0]) if row else None


def _message_times(app, data, transcript):
    lines = transcript.splitlines()
    if len(lines) > 100 or any(not line.startswith(f'[消息{index}] ')
                               for index, line in enumerate(lines, 1)):
        app.fail(400, 'transcript 消息序号无效')
    raw = data.get('message_times', [])
    if raw == []:
        return [0.0] * len(lines)
    if not isinstance(raw, list) or len(raw) != len(lines):
        app.fail(400, 'message_times 与 transcript 不匹配')
    now = time.time()
    result = []
    for value in raw:
        if type(value) not in (int, float):
            app.fail(400, 'message_times 格式无效')
        try:
            timestamp = float(value)
        except (OverflowError, ValueError):
            app.fail(400, 'message_times 格式无效')
        if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > now + 60:
            app.fail(400, 'message_times 格式无效')
        result.append(timestamp)
    return result


def _harassment_indices(value, allowed_indices):
    if not isinstance(value, list):
        raise answers.ModelError('invalid_harassment_review')
    return sorted({index for index in value
                   if type(index) is int and index in allowed_indices})


def respond(app, data):
    kb = app.string(data, 'kb_id', 80, True)
    group = app.string(data, 'group_id', 128, True)
    batch = app.string(data, 'batch_id', 64, True)
    if not re.fullmatch(r'[a-f0-9]{64}', batch):
        app.fail(400, 'batch_id 格式无效')
    transcript = app.string(data, 'transcript', 15000, True)
    message_times = _message_times(app, data, transcript)
    now = time.time()
    scan_indices = [index for index, sent_at in enumerate(message_times, 1)
                    if 0 <= now - sent_at <= SCAN_WINDOW_SECONDS]
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
        trace_id, _ = traces.create(c, kb, '自动群聊性骚扰判断（100条发言）', {
            'origin': 'qq_group', 'user_id': '', 'group_id': group, 'session_id': batch})
        c.execute('UPDATE answer_traces SET delivery=? WHERE id=?', ('not_applicable', trace_id))

    details = {'model_calls': [], 'retrievals': [], 'model': cfg.get('model', ''),
               'harassment_review': {'transcript_chars': len(transcript),
                                     'scan_window_seconds': SCAN_WINDOW_SECONDS,
                                     'scan_count': len(scan_indices), 'scan_indices': scan_indices,
                                     'flagged': 0, 'indices': [],
                                     'recall_enabled': cfg.get('impression_harassment_recall_enabled', False) is True}}
    acquired, indices = False, []
    try:
        if not scan_indices:
            response = {'ok': True, 'mode': 'harassment_review', 'reason': 'no_recent_messages',
                        'answer': '最近10分钟内没有需要审核的发言。'}
        elif not cfg.get('enabled') or not cfg.get('api_key'):
            response = {'ok': False, 'mode': 'unavailable', 'reason': 'model_disabled',
                        'answer': '性骚扰判断模型未启用或配置。'}
        elif not app.ANSWER_SLOTS.acquire(blocking=False):
            response = {'ok': False, 'mode': 'busy', 'reason': 'model_busy',
                        'answer': '性骚扰判断模型当前繁忙。'}
        else:
            acquired = True
            output = answers.model_call(cfg | {'_stage': 'group_harassment_review', '_trace': details,
                                               '_trace_hide_content': True,
                                               '_timeout': MODEL_TIMEOUT}, [
                {'role': 'system', 'content': PROMPT},
                {'role': 'user', 'content': json.dumps({'transcript': transcript,
                                                       'sexual_harassment_scan_indices': scan_indices},
                                                      ensure_ascii=False)}],
                json_mode=True, max_tokens=MAX_COMPLETION_TOKENS, allow_small_max_tokens=True)
            try:
                parsed = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', output.strip(), flags=re.I))
            except (ValueError, AttributeError):
                raise answers.ModelError('invalid_harassment_review') from None
            if not isinstance(parsed, dict) or not isinstance(parsed.get('sexual_harassment_indices'), list):
                raise answers.ModelError('invalid_harassment_review')
            indices = _harassment_indices(parsed['sexual_harassment_indices'], set(scan_indices))
            response = {'ok': True, 'mode': 'harassment_review', 'reason': 'harassment_reviewed', 'answer': ''}
    except answers.ModelError as exc:
        response = {'ok': False, 'mode': 'error', 'reason': str(exc),
                    'answer': '本批性骚扰判断失败，等待后台重试。'}
    except Exception as exc:
        details['error_type'] = type(exc).__name__
        response = {'ok': False, 'mode': 'error', 'reason': 'internal_error', 'answer': '性骚扰判断服务内部异常。'}
    finally:
        if acquired:
            app.ANSWER_SLOTS.release()

    details['harassment_review'].update(flagged=len(indices), indices=indices)
    response.update(trace_id=trace_id, harassment_indices=indices,
                    harassment_flagged=len(indices), harassment_scan_count=len(scan_indices),
                    harassment_recall_enabled=details['harassment_review']['recall_enabled'])
    with app.WRITE_LOCK, app.db() as c:
        cached = _cached(c, kb, group, batch)
        if cached:
            response = cached
        elif response['ok']:
            c.execute('INSERT INTO group_harassment_batches VALUES(?,?,?,?,?)',
                      (kb, group, batch, time.time(), json.dumps(response, ensure_ascii=False)))
        traces.finish(c, trace_id, traces.redact(response, secrets_to_hide),
                      traces.redact(details, secrets_to_hide), round((time.monotonic() - started) * 1000))
    return response
