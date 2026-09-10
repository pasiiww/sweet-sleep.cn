"""Transactional knowledge-update outbox for an explicitly bound bot owner."""
import json
import re
import secrets
import time


def config(c):
    value=json.loads(c.execute("SELECT value FROM app_settings WHERE name='owner_notifications'").fetchone()[0])
    value.setdefault('openids',[value['openid']] if value.get('openid') else [])
    return value


def initialize(c):
    c.execute("INSERT OR IGNORE INTO app_settings VALUES('owner_notifications',?)",(json.dumps({'enabled':False,'owner_qq':'471718054','openid':''}),))
    c.execute('''CREATE TABLE IF NOT EXISTS owner_notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,
      kb_id TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,
      created REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',receipt TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '')''')
    value=config(c)
    c.execute("UPDATE app_settings SET value=? WHERE name='owner_notifications'",(json.dumps(value),))
    enabled="coalesce(json_extract((SELECT value FROM app_settings WHERE name='owner_notifications'),'$.enabled'),0)=1"
    recipients="json_each(json_extract((SELECT value FROM app_settings WHERE name='owner_notifications'),'$.openids'))"
    for table,title,content,extra in [('documents','title','content','1'),('qa_entries','question','answer',"new.publication='active' AND new.superseded_by IS NULL")]:
        for action in ('INSERT','UPDATE'):
            changed='1' if action=='INSERT' else (f'(new.{title}<>old.{title} OR new.{content}<>old.{content}'+(" OR new.publication<>old.publication" if table=='qa_entries' else '')+')')
            c.execute(f'DROP TRIGGER IF EXISTS notify_{table}_{action.lower()}')
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS notify_{table}_{action.lower()} AFTER {action} ON {table}
            WHEN {enabled} AND {extra} AND {changed}
            BEGIN INSERT INTO owner_notifications(kb_id,recipient,kind,title,content,created)
            SELECT new.kb_id,value,'{table}',new.{title},substr(new.{content},1,300),unixepoch() FROM {recipients} WHERE length(value)>=8; END''')


def save(c,data):
    enabled=data.get('enabled')
    ids=data.get('openids',[data['openid']] if data.get('openid') else [])
    if type(enabled) is not bool or not isinstance(ids,list) or len(ids)>20 or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',v) or v.isdecimal() for v in ids):
        raise ValueError('请填写最多20个有效的 Owner 私聊 OpenID，不是 QQ 号')
    ids=list(dict.fromkeys(ids))
    if enabled and not ids:raise ValueError('启用前请至少填写一个 Owner 私聊 OpenID')
    value={'enabled':enabled,'openids':ids,'openid':ids[0] if ids else ''}
    c.execute("UPDATE app_settings SET value=? WHERE name='owner_notifications'",(json.dumps(value),))
    return value


def claim(c):
    cfg=config(c)
    c.execute('DELETE FROM owner_notifications WHERE created<?',(time.time()-7*86400,))
    if not cfg['enabled'] or not cfg['openids']:return {}
    # A crash after sending is ambiguous: never automatically resend a claimed batch.
    c.execute("UPDATE owner_notifications SET status='uncertain',error='delivery_unknown' WHERE status='sending' AND created<?",(time.time()-300,))
    marks=','.join('?' for _ in cfg['openids'])
    first=c.execute(f"SELECT * FROM owner_notifications WHERE status='pending' AND recipient IN ({marks}) ORDER BY id LIMIT 1",cfg['openids']).fetchone()
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
