"""Preserve full-group events and quoted-reply metadata in qq-botpy 1.2.1."""
import os
from botpy.connection import ConnectionState
from botpy.message import GroupMessage, C2CMessage


def reference_metadata(data, own_ids=()):
    scene=data.get('message_scene') or {}
    ext=scene.get('ext',[]) if isinstance(scene,dict) else []
    indices={}
    for value in ext if isinstance(ext,list) else []:
        if isinstance(value,str) and '=' in value:
            key,val=value.split('=',1)
            if key in ('msg_idx','ref_msg_idx'):indices[key]=val[:200]
    ref=data.get('message_reference') or {}
    is_reply=data.get('message_type')==103 or bool(indices.get('ref_msg_idx')) or (isinstance(ref,dict) and bool(ref.get('message_id')))
    quotes=[]
    def collect(elements,depth=0):
        if depth>2 or not isinstance(elements,list):return
        for el in elements[:10]:
            if not isinstance(el,dict) or len(quotes)>=10:return
            content=el.get('content');author=el.get('author') or {}
            if isinstance(content,str) and content.strip():
                quotes.append({'content':content[:1500], 'member_id':str(author.get('member_openid') or author.get('id') or '')[:128] if isinstance(author,dict) else '', 'msg_idx':str(el.get('msg_idx') or '')[:200]})
            collect(el.get('msg_elements'),depth+1)
    if is_reply:collect(data.get('msg_elements'))
    mentions=[]
    raw_mentions=data.get('mentions') or []
    for m in (raw_mentions if isinstance(raw_mentions,list) else [])[:20]:
        if not isinstance(m,dict) or m.get('bot'):continue
        member=m.get('member_openid') or m.get('id')
        if isinstance(member,str) and member and len(member)<=128 and member not in own_ids and member not in mentions:mentions.append(member)
    author=data.get('author') or {}
    return {'author_bot':author.get('bot') is True,'member_role':author.get('member_role',''),'mentions':mentions,'is_reply':bool(is_reply),'msg_idx':indices.get('msg_idx',''),
            'reference':{'message_id':str(ref.get('message_id') or '')[:200] if isinstance(ref,dict) else '',
                         'msg_idx':indices.get('ref_msg_idx',''),'quotes':quotes}}


class FullGroupMessage(GroupMessage):
    __slots__ = ('sweet_mentioned','sweet_learning','sweet_author_bot')

    def __init__(self, api, event_id, data, robot_id):
        super().__init__(api, event_id, data)
        own_ids = {str(v) for v in (robot_id, os.environ.get('QQ_APP_ID')) if v}
        mentions = data.get('mentions') or []
        self.sweet_mentioned = any(isinstance(m,dict) and str(m.get('id','')) in own_ids
                                   for m in (mentions if isinstance(mentions,list) else []))
        self.sweet_learning=reference_metadata(data,own_ids)
        self.sweet_author_bot=self.sweet_learning['author_bot']


def parse_full_group(self, payload):
    message = FullGroupMessage(self.api,payload.get('id'),payload.get('d',{}),getattr(self.robot,'id',None))
    self._dispatch('group_message_create',message)


def parse_at_group(self,payload):
    message=FullGroupMessage(self.api,payload.get('id'),payload.get('d',{}),getattr(self.robot,'id',None))
    message.sweet_mentioned=True
    self._dispatch('group_at_message_create',message)


class FullC2CMessage(C2CMessage):
    __slots__=('sweet_author_bot',)
    def __init__(self,api,event_id,data):
        super().__init__(api,event_id,data)
        self.sweet_author_bot=(data.get('author') or {}).get('bot') is True


def parse_c2c(self,payload):
    self._dispatch('c2c_message_create',FullC2CMessage(self.api,payload.get('id'),payload.get('d',{})))


def is_bot(message):
    return getattr(message,'sweet_author_bot',False) or getattr(getattr(message,'author',None),'bot',False) is True


def install():
    ConnectionState.parse_c2c_message_create = parse_c2c
    ConnectionState.parse_group_message_create = parse_full_group
    ConnectionState.parse_group_at_message_create = parse_at_group
