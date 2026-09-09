"""Preserve full-group events and quoted-reply metadata in qq-botpy 1.2.1."""
import os
from botpy.connection import ConnectionState
from botpy.message import GroupMessage


def reference_metadata(data):
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
    return {'is_reply':bool(is_reply),'msg_idx':indices.get('msg_idx',''),
            'reference':{'message_id':str(ref.get('message_id') or '')[:200] if isinstance(ref,dict) else '',
                         'msg_idx':indices.get('ref_msg_idx',''),'quotes':quotes}}


class FullGroupMessage(GroupMessage):
    __slots__ = ('sweet_mentioned','sweet_learning')

    def __init__(self, api, event_id, data, robot_id):
        super().__init__(api, event_id, data)
        own_ids = {str(v) for v in (robot_id, os.environ.get('QQ_APP_ID')) if v}
        mentions = data.get('mentions') or []
        self.sweet_mentioned = any(isinstance(m,dict) and str(m.get('id','')) in own_ids
                                   for m in (mentions if isinstance(mentions,list) else []))
        self.sweet_learning=reference_metadata(data)


def parse_full_group(self, payload):
    message = FullGroupMessage(self.api,payload.get('id'),payload.get('d',{}),getattr(self.robot,'id',None))
    self._dispatch('group_message_create',message)


def parse_at_group(self,payload):
    message=FullGroupMessage(self.api,payload.get('id'),payload.get('d',{}),getattr(self.robot,'id',None))
    message.sweet_mentioned=True
    self._dispatch('group_at_message_create',message)


def install():
    ConnectionState.parse_group_message_create = parse_full_group
    ConnectionState.parse_group_at_message_create = parse_at_group
