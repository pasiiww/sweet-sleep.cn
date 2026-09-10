"""Lossless identity mapping and conservative noise removal for learning inputs."""
import re


def clean_text(value):
    text = re.sub(r'<faceType=[^>]*>', '', value or '')
    text = re.sub(r'<@!?[A-Za-z0-9_-]+>|<qqbot-at-user\s+id="[A-Za-z0-9_-]+"\s*/>', '', text).strip()
    if not re.search(r'[\w\u3400-\u9fff]', text):
        return ''
    if re.fullmatch(r'[哈呵嘿嘻啦啊呀哦嗯~～!！?？\s]{3,}', text):
        return ''
    return text


def prepare(details):
    rows = details['context']
    messages = {}; members = {}; removed = []; kept = []
    def msg(value):
        if not value: return None
        return messages.setdefault(value, 'm'+str(len(messages)+1))
    def member(value):
        if not value: return None
        return members.setdefault(value, 'u'+str(len(members)+1))
    # Allocate all IDs, including filtered messages, so references remain unambiguous.
    for row in rows:
        msg(row['message_id']); member(row['member_id'])
    previous = None
    for row in rows:
        text = clean_text(row['content'])
        ref = row.get('reference') or {}
        signature = (text, ref.get('message_id'), ref.get('msg_idx'), tuple(row.get('mentions') or []))
        # Never replace a trusted author's confirmation with an ordinary user's claim.
        duplicate = bool(text and previous and signature == previous[0] and
                         (row['member_id'] == previous[1] or not row.get('qq')))
        previous = (signature, row['member_id'])
        if not text or duplicate:
            removed.append({'message_id':row['message_id'], 'reason':'duplicate_previous' if duplicate else 'empty_or_noise'})
            continue
        kept.append((row, text))
    index_ids = {r.get('msg_idx'):r['message_id'] for r in rows if r.get('msg_idx')}
    context = []
    for row, text in kept:
        ref = row.get('reference') or {}
        reference = {}
        target = ref.get('message_id') or index_ids.get(ref.get('msg_idx'))
        if target: reference['message_id'] = msg(target)
        quotes = []
        for q in ref.get('quotes', []):
            content = clean_text(q.get('content'))
            if not content: continue
            quote = {'content':content}
            if q.get('member_id'): quote['member_id'] = member(q['member_id'])
            target = q.get('message_id') or index_ids.get(q.get('msg_idx'))
            if target: quote['message_id'] = msg(target)
            quotes.append(quote)
        if quotes: reference['quotes'] = quotes
        context.append({'message_id':msg(row['message_id']), 'member_id':member(row['member_id']),
                        'trusted':bool(row.get('qq')), 'member_role':row.get('member_role',''),
                        'content':text, 'at':row['at'], 'is_reply':bool(row.get('is_reply')),
                        'reference':reference, 'mentions':[member(v) for v in row.get('mentions',[])]})
    kept_ids = {r['message_id'] for r, _ in kept}
    batch = [v for v in details['batch_source_ids'] if v in kept_ids]
    payload = {'context':context, 'batch_source_ids':[msg(v) for v in batch],
               'context_by_source':{msg(k):[msg(v) for v in values if v in kept_ids]
                                    for k, values in details['context_by_source'].items() if k in batch}}
    debug = {'before_count':len(rows), 'after_count':len(context), 'removed':removed,
             'message_ids':{v:k for k,v in messages.items()}, 'member_ids':{v:k for k,v in members.items()}}
    return payload, debug
