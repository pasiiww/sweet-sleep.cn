"""Durable event outbox; never replies to group messages."""
import asyncio
from datetime import datetime
import json
import logging
import re
import time

import aiohttp
import compat

LOG = logging.getLogger('knowledge-bot')


class Learner:
    def __init__(self, connection, url, token, kb_id):
        self.conn, self.url, self.token, self.kb_id = connection, url, token, kb_id
        self.task = None
        self.api = None
        self.conn.execute('CREATE TABLE IF NOT EXISTS learning_outbox(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL)')
        self.conn.commit()

    def observe(self, message):
        if compat.is_bot(message):return
        group = getattr(message,'group_openid','')
        member = getattr(getattr(message,'author',None),'member_openid','')
        mid, content = getattr(message,'id',''), getattr(message,'content','')
        content = re.sub(r'<@!?[A-Za-z0-9_-]+>|<qqbot-at-user\s+id="[A-Za-z0-9_-]+"\s*/>', '', content or '').strip()
        if content.startswith('/') or len(content)>2000: return
        if not group or not member or not mid or not isinstance(content,str) or not content.strip(): return
        at = getattr(message,'timestamp',None)
        try: at = datetime.fromisoformat(str(at).replace('Z','+00:00')).timestamp() if at else time.time()
        except (ValueError,TypeError): return
        payload={'kb_id':self.kb_id,'group_id':group,'member_id':member,'message_id':mid,'content':content[:2000],'raw_content':getattr(message,'content','')[:4000],'at':at, **getattr(message,'sweet_learning',{})}
        with self.conn:
            self.conn.execute('DELETE FROM learning_outbox WHERE created<?',(time.time()-1800,))
            if self.conn.execute('SELECT count(*) FROM learning_outbox').fetchone()[0]>=1000:
                LOG.warning('LEARNING_OUTBOX_FULL');return
            self.conn.execute('INSERT OR IGNORE INTO learning_outbox VALUES(?,?,?)',(group+':'+mid,json.dumps(payload),time.time()))

    def start(self):
        if self.task is None or self.task.done(): self.task=asyncio.create_task(self.run())

    async def flush_once(self):
        with self.conn: self.conn.execute('DELETE FROM learning_outbox WHERE created<?',(time.time()-1800,))
        row=self.conn.execute('SELECT id,payload FROM learning_outbox ORDER BY created LIMIT 1').fetchone()
        if not row:return False
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.post(self.url,headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},data=row[1]) as response:
                if response.status in (400,404,413):
                    with self.conn:self.conn.execute('DELETE FROM learning_outbox WHERE id=?',(row[0],))
                    LOG.warning('LEARNING_EVENT_REJECTED status=%s',response.status)
                    return True
                if response.status!=200: raise RuntimeError('Learning HTTP '+str(response.status))
        with self.conn:self.conn.execute('DELETE FROM learning_outbox WHERE id=?',(row[0],))
        return True

    async def notify_once(self):
        if self.api is None:return
        endpoint=self.url.rsplit('/learning/events',1)[0]+'/owner-notifications'
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            headers={'Authorization':'Bearer '+self.token}
            async with session.post(endpoint+'/claim',headers=headers,json={}) as response:
                if response.status!=200:raise RuntimeError('Notification claim failed')
                item=await response.json()
            if not item:return
            status,error='failed',''
            try:
                # Owner explicitly bound in admin UI; no user msg_id is invented for a proactive notification.
                content=item['content'].replace('@','＠').replace('<','＜').replace('>','＞')
                result=await self.api.post_c2c_message(openid=item['openid'],msg_type=0,content=content)
                if not result:raise RuntimeError('Empty QQ response')
                status='delivered'
            except Exception as exc:
                error=type(exc).__name__
                LOG.warning('OWNER_NOTIFICATION_FAILED error=%s',error)
            async with session.post(endpoint+'/ack',headers=headers,json={'receipt':item['receipt'],'status':status,'error':error}) as response:
                if response.status!=200:raise RuntimeError('Notification receipt failed')

    async def run(self):
        last_notification=0
        while True:
            uploaded=False
            try:
                uploaded=await self.flush_once()
            except Exception as exc:LOG.warning('LEARNING_UPLOAD_FAILED error=%s',type(exc).__name__)
            if time.monotonic()-last_notification>=3:
                try:await self.notify_once()
                except Exception as exc:LOG.warning('OWNER_NOTIFICATION_FAILED error=%s',type(exc).__name__)
                last_notification=time.monotonic()
            if not uploaded:await asyncio.sleep(3)
