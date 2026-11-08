"""Persistent, conversation-scoped deadline reminders."""
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))


def init(c):
    c.execute('''CREATE TABLE IF NOT EXISTS ddl_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT, kb TEXT NOT NULL, kind TEXT NOT NULL,
      scope TEXT NOT NULL, creator TEXT NOT NULL, owner TEXT NOT NULL,
      owner_name TEXT NOT NULL, title TEXT NOT NULL, due REAL NOT NULL,
      rule TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', request_id TEXT NOT NULL,
      UNIQUE(kb,kind,scope,request_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS ddl_reminders (
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES ddl_items(id),
      at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending', lease TEXT NOT NULL DEFAULT '',
      leased_until REAL NOT NULL DEFAULT 0, UNIQUE(item_id,at))''')
    c.execute('CREATE TABLE IF NOT EXISTS ddl_migrations (name TEXT PRIMARY KEY)')
    if c.execute("INSERT OR IGNORE INTO ddl_migrations(name) VALUES('default_half_hour_v1')").rowcount:
        c.execute('''INSERT OR IGNORE INTO ddl_reminders(item_id,at)
          SELECT id,due-1800 FROM ddl_items
          WHERE status='active' AND rule='{}' AND due-1800>?''', (time.time(),))
    if c.execute("INSERT OR IGNORE INTO ddl_migrations(name) VALUES('early_morning_v1')").rowcount:
        for row in c.execute("SELECT id,due,rule FROM ddl_items WHERE status='active'").fetchall():
            try:
                rule = json.loads(row['rule'])
                start = rule.get('created_at')
                if not rule.get('default_policy') or start is None or not default_rule(row['due'], start).get('early_morning'):
                    continue
                morning = datetime.fromtimestamp(start, TZ).replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
                rule['early_morning'] = True
                c.execute('UPDATE ddl_items SET rule=? WHERE id=?', (json.dumps(rule, ensure_ascii=False), row['id']))
                if morning > time.time():
                    c.execute('INSERT OR IGNORE INTO ddl_reminders(item_id,at) VALUES(?,?)', (row['id'], morning))
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                continue


def parse_date(value):
    value = datetime.fromisoformat(value)
    return (value.replace(tzinfo=TZ) if value.tzinfo is None else value).timestamp()


def describe_rule(rule):
    if not rule:
        return '原默认提醒规则（以已安排时间为准）'
    if rule.get('default_policy'):
        description = {'short': '剩余时间过半及到时提醒', 'medium': '截止前30分钟及到时提醒',
                       'long': '截止当天及提前1、2、4、7、15天的上午9点，以及到时提醒'}[rule['default_policy']]
        return description + ('；另在当天上午9点提醒' if rule.get('early_morning') else '')
    parts = []
    if rule.get('times'):
        values = rule['times']
        parts.append('指定时间：' + '、'.join(values[:6]) + (f'等共{len(values)}次' if len(values) > 6 else ''))
    if rule.get('every_minutes'):
        parts.append('自' + rule['start'] + '起每' + str(rule['every_minutes']) + '分钟')
    if rule.get('weekdays'):
        days = '一二三四五六日'
        parts.append('每周' + '、'.join(days[d] for d in rule['weekdays']) + ' ' + rule.get('time', '09:00'))
    return '；'.join(parts) or '没有设置提醒'


def owner_context(c, kb, kind, scope, owner):
    """Expose only the current speaker's deadlines within the current conversation."""
    if kind not in ('qq_group', 'qq_private') or not scope or not owner:
        return []
    init(c)
    rows = c.execute("""SELECT id,title,due,rule FROM ddl_items
        WHERE kb=? AND kind=? AND scope=? AND owner=? AND status='active'
        ORDER BY due LIMIT 20""", (kb, kind, scope, owner)).fetchall()
    return [{'id': r['id'], 'title': r['title'],
             'due': datetime.fromtimestamp(r['due'], TZ).isoformat(),
             'reminder_rule': describe_rule(json.loads(r['rule']))} for r in rows]


def default_rule(due, now):
    remaining = due - now
    rule = {'default_policy': 'short' if remaining < 3600 else 'medium' if remaining <= 86400 else 'long',
            'created_at': now}
    start, end = datetime.fromtimestamp(now, TZ), datetime.fromtimestamp(due, TZ)
    morning = start.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    if start.hour < 4 and start.date() == end.date() and now < morning < due:
        rule['early_morning'] = True
    return rule


def schedule(due, rule, now):
    if due > now + 50 * 366 * 86400:
        raise ValueError('截止时间不能超过50年')
    if not isinstance(rule, dict):
        raise ValueError('提醒规则格式无效')
    end = datetime.fromtimestamp(due, TZ)
    times = set()
    if rule.get('default_policy'):
        policy = rule['default_policy']
        if policy not in ('short', 'medium', 'long') or not {'default_policy', 'created_at'} <= set(rule) or set(rule) - {'default_policy', 'created_at', 'early_morning'} or type(rule.get('early_morning', False)) is not bool:
            raise ValueError('默认提醒规则无效')
        start = float(rule['created_at'])
        times.add(due)
        if rule.get('early_morning'):
            times.add(datetime.fromtimestamp(start, TZ).replace(hour=9, minute=0, second=0, microsecond=0).timestamp())
        if policy == 'short':
            times.add(start + (due-start)/2)
        elif policy == 'medium':
            times.add(due - 1800)
        else:
            for day in (0, 1, 2, 4, 7, 15):
                date = end.date() - timedelta(days=day)
                times.add(datetime.combine(date, datetime.min.time(), TZ).replace(hour=9).timestamp())
    elif not rule:
        times.add(due - 1800)
        days = [0, 1, 2, 4, 7, 15, 30, 60, 90]
        n, step = 90, 60
        while n < (due-now)/86400 and n < 10000:
            n += step
            days.append(n)
            step *= 2
        for day in days:
            date = end.date() - timedelta(days=day)
            t = datetime.combine(date, datetime.min.time(), TZ).replace(hour=9).timestamp()
            if day == 0 and t >= due:
                t = due - 3600
            times.add(t)
    else:
        if not isinstance(rule, dict) or set(rule) - {'times', 'every_minutes', 'start', 'weekdays', 'time'}:
            raise ValueError('提醒规则格式无效')
        if not isinstance(rule.get('times', []), list) or len(rule.get('times', [])) > 2000:
            raise ValueError('提醒时间列表最多2000项')
        for value in rule.get('times', []):
            times.add(parse_date(value))
        if rule.get('every_minutes'):
            interval = int(rule['every_minutes'])
            if interval < 5 or interval > 525600:
                raise ValueError('提醒间隔须为5分钟至365天')
            t = parse_date(rule['start'])
            if t < now:
                t += max(0, int((now-t)//(interval*60))) * interval*60
            while t <= due and len(times) <= 2000:
                times.add(t)
                t += interval * 60
        if 'weekdays' in rule:
            weekdays = rule['weekdays']
            if not isinstance(weekdays, list) or not weekdays or any(type(d) is not int or d not in range(7) for d in weekdays):
                raise ValueError('星期必须为0至6（周一为0）')
            hour, minute = map(int, rule.get('time', '09:00').split(':'))
            day = datetime.fromtimestamp(max(now, parse_date(rule['start']) if rule.get('start') else now), TZ).date()
            while day <= end.date() and len(times) <= 2000:
                if day.weekday() in weekdays:
                    times.add(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp())
                day += timedelta(days=1)
    result = sorted(t for t in times if now < t <= due)
    if len(result) > 2000:
        raise ValueError('提醒次数不能超过2000次')
    return result


def manage(c, kb, kind, scope, creator, mentions, request_id, action, item_id=0, title='', due='', owner='current', rule_json='', creator_name=''):
    init(c)
    if not scope or not creator or not request_id:
        return {'error': '缺少会话或消息身份，无法操作'}
    if action == 'list':
        rows = c.execute("SELECT id,title,due,owner_name,status,rule FROM ddl_items WHERE kb=? AND kind=? AND scope=? AND status='active' ORDER BY due LIMIT 100", (kb, kind, scope)).fetchall()
        return {'items': [dict(r) | {'due': datetime.fromtimestamp(r['due'], TZ).isoformat()} for r in rows]}
    if action == 'add':
        if not title.strip() or len(title) > 200:
            return {'error': '事项名称须为1至200字'}
        owners = {'current': (creator, creator_name.strip()[:100] or '你')}
        owners.update({m['key']: (m['openid'], m['name']) for m in mentions})
        if owner not in owners:
            return {'error': '负责人无法确认，请让用户明确指定自己或在当前消息中@负责人'}
        try:
            end = parse_date(due)
            now = time.time()
            rule = json.loads(rule_json) if rule_json else default_rule(end, now)
            if rule == {}:
                rule = default_rule(end, now)
            times = schedule(end, rule, now)
            if end <= now:
                raise ValueError('截止时间必须在未来')
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return {'error': '时间或提醒规则无效：' + str(exc)}
        member, name = owners[owner]
        inserted = c.execute('INSERT OR IGNORE INTO ddl_items(kb,kind,scope,creator,owner,owner_name,title,due,rule,request_id) VALUES(?,?,?,?,?,?,?,?,?,?)', (kb,kind,scope,creator,member,name,title.strip(),end,json.dumps(rule,ensure_ascii=False),request_id)).rowcount
        row = c.execute('SELECT id,title,due,owner_name,rule FROM ddl_items WHERE kb=? AND kind=? AND scope=? AND request_id=?',(kb,kind,scope,request_id)).fetchone()
        if inserted:
            for t in times:
                c.execute('INSERT OR IGNORE INTO ddl_reminders(item_id,at) VALUES(?,?)',(row['id'],t))
        else:
            times = [r[0] for r in c.execute('SELECT at FROM ddl_reminders WHERE item_id=? ORDER BY at',(row['id'],))]
        return {'ok': True, 'id': row['id'], 'title': row['title'], 'due': datetime.fromtimestamp(row['due'], TZ).isoformat(), 'owner': row['owner_name'], 'reminder_rule': describe_rule(json.loads(row['rule'])), 'reminders': [datetime.fromtimestamp(t,TZ).isoformat() for t in times]}
    if action in ('complete', 'delete', 'update'):
        row = c.execute("SELECT * FROM ddl_items WHERE id=? AND kb=? AND kind=? AND scope=? AND status='active'", (item_id,kb,kind,scope)).fetchone()
        if not row:
            return {'error': '未找到该事项'}
        if row['creator'] != creator:
            return {'error': '只有创建者可以完成或删除事项'}
        if action == 'update':
            try:
                end = parse_date(due) if due else row['due']
                rule = json.loads(rule_json) if rule_json else json.loads(row['rule'])
                if rule == {} and rule_json:
                    rule = default_rule(end, time.time())
                if due and not rule_json and rule.get('default_policy'):
                    rule = default_rule(end, time.time())
                times = schedule(end, rule, time.time())
                if end <= time.time() or (title and len(title) > 200):
                    raise ValueError('截止时间或名称无效')
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                return {'error': '时间或提醒规则无效：' + str(exc)}
            c.execute('UPDATE ddl_items SET title=?,due=?,rule=? WHERE id=?', (title.strip() or row['title'],end,json.dumps(rule,ensure_ascii=False),item_id))
            c.execute("DELETE FROM ddl_reminders WHERE item_id=? AND state<>'sent'",(item_id,))
            for t in times:
                c.execute('INSERT OR IGNORE INTO ddl_reminders(item_id,at) VALUES(?,?)',(item_id,t))
            return {'ok': True, 'id': item_id, 'title': title.strip() or row['title'], 'due': datetime.fromtimestamp(end,TZ).isoformat(), 'reminders': [datetime.fromtimestamp(t,TZ).isoformat() for t in times]}
        c.execute('UPDATE ddl_items SET status=? WHERE id=?', ('completed' if action == 'complete' else 'deleted', item_id))
        return {'ok': True, 'id': item_id, 'title': row['title']}
    return {'error': '不支持的操作'}


def claim(c, kb, mode, kind='', scope=''):
    init(c)
    now = time.time()
    c.execute('UPDATE ddl_reminders SET state=CASE WHEN state="sending" THEN "fallback" ELSE state END,lease="" WHERE state="sending" AND leased_until<?', (now,))
    state = 'pending' if mode == 'proactive' else 'fallback'
    c.execute('BEGIN IMMEDIATE') if not c.in_transaction else None
    sql = '''SELECT r.id,r.at,i.kind,i.scope,i.owner,i.title,i.due FROM ddl_reminders r JOIN ddl_items i ON i.id=r.item_id WHERE i.kb=? AND i.status='active' AND r.state=? AND r.at<=?'''
    args = [kb,state,now]
    if mode == 'fallback':
        sql += ' AND i.kind=? AND i.scope=?'
        args += [kind,scope]
    rows = c.execute(sql+' ORDER BY r.at LIMIT 1',args).fetchall()
    token = secrets.token_urlsafe(24)
    for row in rows:
        c.execute("UPDATE ddl_reminders SET state='sending',lease=?,leased_until=? WHERE id=?",(token,now+120,row['id']))
    return {'lease': token, 'items': [dict(r) for r in rows]}


def ack(c, kb, token, sent):
    init(c)
    c.execute('''UPDATE ddl_reminders SET state=?,lease='',leased_until=0 WHERE lease=? AND item_id IN (SELECT id FROM ddl_items WHERE kb=?)''', ('sent' if sent else 'fallback',token,kb))
    return {'ok': True}
