"""Transactional knowledge-update outbox for an explicitly bound bot owner."""
import json
import re
import secrets
import time


def config(c):
    return json.loads(c.execute("SELECT value FROM app_settings WHERE name='owner_notifications'").fetchone()[0])


def initialize(c):
    c.execute("INSERT OR IGNORE INTO app_settings VALUES('owner_notifications',?)",(json.dumps({'enabled':False,'owner_qq':'471718054','openid':''}),))
    c.execute('''CREATE TABLE IF NOT EXISTS owner_notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,
      kb_id TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,
      created REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',receipt TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '')''')
    enabled="coalesce(json_extract((SELECT value FROM app_settings WHERE name='owner_notifications'),'$.enabled'),0)=1"
    recipient="json_extract((SELECT value FROM app_settings WHERE name='owner_notifications'),'$.openid')"
    for table,title,content,extra in [('documents','title','content','1'),('qa_entries','question','answer',"new.publication='active' AND new.superseded_by IS NULL")]:
        for action in ('INSERT','UPDATE'):
            changed='1' if action=='INSERT' else (f'(new.{title}<>old.{title} OR new.{content}<>old.{content}'+(" OR new.publication<>old.publication" if table=='qa_entries' else '')+')')
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS notify_{table}_{action.lower()} AFTER {action} ON {table}
            WHEN {enabled} AND length({recipient})>=8 AND {extra} AND {changed}
            BEGIN INSERT INTO owner_notifications(kb_id,recipient,kind,title,content,created)
            VALUES(new.kb_id,{recipient},'{table}',new.{title},substr(new.{content},1,300),unixepoch()); END''')


def save(c,data):
    enabled=data.get('enabled');openid=data.get('openid','').strip()
    if type(enabled) is not bool or (openid and not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',openid)):
        raise ValueError('请填写有效的 owner 私聊 OpenID')
    if enabled and not openid:raise ValueError('启用前请填写 owner 私聊 OpenID，不能填写 QQ 号或群成员 OpenID')
    if openid.isdecimal():raise ValueError('请填写私聊 OpenID，不是 QQ 号')
    value={'enabled':enabled,'openid':openid,'owner_qq':'471718054'}
    c.execute("UPDATE app_settings SET value=? WHERE name='owner_notifications'",(json.dumps(value),))
    return value


def claim(c):
    cfg=config(c)
    c.execute('DELETE FROM owner_notifications WHERE created<?',(time.time()-7*86400,))
    if not cfg['enabled']:return {}
    # A crash after sending is ambiguous: never automatically resend a claimed batch.
    c.execute("UPDATE owner_notifications SET status='uncertain',error='delivery_unknown' WHERE status='sending' AND created<?",(time.time()-300,))
    first=c.execute("SELECT * FROM owner_notifications WHERE status='pending' AND recipient=? ORDER BY id LIMIT 1",(cfg['openid'],)).fetchone()
    if not first:return {}
    rows=c.execute("SELECT * FROM owner_notifications WHERE status='pending' AND recipient=? AND kb_id=? ORDER BY id LIMIT 8",(first['recipient'],first['kb_id'])).fetchall()
    receipt=secrets.token_hex(24);ids=[r['id'] for r in rows]
    c.executemany("UPDATE owner_notifications SET status='sending',receipt=? WHERE id=?",[(receipt,i) for i in ids])
    base=c.execute('SELECT name FROM bases WHERE id=?',(first['kb_id'],)).fetchone()
    text='知识库更新通知 · '+(base[0] if base else '知识库')+'\n本次 '+str(len(rows))+' 条知识已生效：\n'
    text+='\n'.join(str(n)+'. '+r['title'][:65]+'\n'+r['content'][:105] for n,r in enumerate(rows,1))
    return {'ids':ids,'receipt':receipt,'openid':first['recipient'],'content':text[:1700]}


def acknowledge(c,data):
    receipt=data.get('receipt','');status=data.get('status')
    if not isinstance(receipt,str) or len(receipt)!=48 or status not in ('delivered','failed'):raise ValueError('通知回执格式错误')
    error=str(data.get('error',''))[:80]
    c.execute("UPDATE owner_notifications SET status=?,error=? WHERE receipt=? AND status IN ('sending','uncertain')",(status,error,receipt))
    return {'ok':True}
