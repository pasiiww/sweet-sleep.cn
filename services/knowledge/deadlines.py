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


def parse_date(value):
    value = datetime.fromisoformat(value)
    return (value.replace(tzinfo=TZ) if value.tzinfo is None else value).timestamp()


def schedule(due, rule, now):
    if due > now + 50 * 366 * 86400:
        raise ValueError('截止时间不能超过50年')
    end = datetime.fromtimestamp(due, TZ)
    times = set()
    if not rule:
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


def manage(c, kb, kind, scope, creator, mentions, request_id, action, item_id=0, title='', due='', owner='current', rule_json=''):
    init(c)
    if not scope or not creator or not request_id:
        return {'error': '缺少会话或消息身份，无法操作'}
    if action == 'list':
        rows = c.execute("SELECT id,title,due,owner_name,status,rule FROM ddl_items WHERE kb=? AND kind=? AND scope=? AND status='active' ORDER BY due LIMIT 100", (kb, kind, scope)).fetchall()
        return {'items': [dict(r) | {'due': datetime.fromtimestamp(r['due'], TZ).isoformat()} for r in rows]}
    if action == 'add':
        if not title.strip() or len(title) > 200:
            return {'error': '事项名称须为1至200字'}
        owners = {'current': (creator, '当前发言者')}
        owners.update({m['key']: (m['openid'], m['name']) for m in mentions})
        if owner not in owners:
            return {'error': '负责人无法确认，请让用户明确指定自己或在当前消息中@负责人'}
        try:
            end = parse_date(due)
            rule = json.loads(rule_json) if rule_json else {}
            times = schedule(end, rule, time.time())
            if end <= time.time():
                raise ValueError('截止时间必须在未来')
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return {'error': '时间或提醒规则无效：' + str(exc)}
        member, name = owners[owner]
        inserted = c.execute('INSERT OR IGNORE INTO ddl_items(kb,kind,scope,creator,owner,owner_name,title,due,rule,request_id) VALUES(?,?,?,?,?,?,?,?,?,?)', (kb,kind,scope,creator,member,name,title.strip(),end,json.dumps(rule,ensure_ascii=False),request_id)).rowcount
        row = c.execute('SELECT id,title,due FROM ddl_items WHERE kb=? AND kind=? AND scope=? AND request_id=?',(kb,kind,scope,request_id)).fetchone()
        if inserted:
            for t in times:
                c.execute('INSERT OR IGNORE INTO ddl_reminders(item_id,at) VALUES(?,?)',(row['id'],t))
        else:
            times = [r[0] for r in c.execute('SELECT at FROM ddl_reminders WHERE item_id=? ORDER BY at',(row['id'],))]
        return {'ok': True, 'id': row['id'], 'title': row['title'], 'due': datetime.fromtimestamp(row['due'], TZ).isoformat(), 'owner': name, 'reminders': [datetime.fromtimestamp(t,TZ).isoformat() for t in times]}
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
