"""Preserve full-group events and quoted-reply metadata in qq-botpy 1.2.1."""
import os
import logging
import re
from types import SimpleNamespace
from botpy.connection import ConnectionState
from botpy.flags import Flag, Intents
from botpy.message import GroupMessage, C2CMessage

MENTION_TAG = re.compile(
    r'<@!?([A-Za-z0-9_-]{1,128})>|<qqbot-at-user\s+id="([A-Za-z0-9_-]{1,128})"\s*/>', re.I)


def _field(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def clean_display_name(value, limit=12):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'[\x00-\x1f\x7f<>]', ' ', value).replace('@', '＠')
    return re.sub(r'\s+', ' ', value).strip()[:limit]


def mention_tag_ids(text):
    if not isinstance(text, str):
        return []
    return [match.group(1) or match.group(2) for match in MENTION_TAG.finditer(text)]


def _mention_aliases(message, member_id):
    aliases = getattr(message, 'sweet_mention_aliases', {}) or {}
    values = aliases.get(member_id, ()) if isinstance(aliases, dict) else ()
    if isinstance(values, str):
        values = (values,)
    return tuple(dict.fromkeys([member_id, *(v for v in values if isinstance(v, str) and v)]))


def _safe_mention_name(value, identifiers):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    # Never pass an OpenID or the mention tag itself to the model as a display name.
    if (value in identifiers or re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value)):
        return ''
    return clean_display_name(value)


def _resolve_mention_name(identifiers, fallback, name_resolver):
    if callable(name_resolver):
        for identifier in identifiers:
            name = _safe_mention_name(name_resolver(identifier), identifiers)
            if name:
                return name
    return _safe_mention_name(fallback, identifiers)


def mention_name_map(message, name_resolver=None):
    names = {}
    mentions = getattr(message, 'mentions', []) or []
    for mention in mentions[:50] if isinstance(mentions, (list, tuple)) else []:
        member_id = _field(mention, 'id') or _field(mention, 'member_openid')
        name = (_field(mention, 'username') or _field(mention, 'nickname')
                or _field(mention, 'nick') or _field(mention, 'display_name'))
        if isinstance(member_id, str) and 0 < len(member_id) <= 128:
            identifiers = _mention_aliases(message, member_id)
            display_name = _resolve_mention_name(identifiers, name, name_resolver)
            if display_name:
                names.update((identifier, display_name) for identifier in identifiers)

    # Preserve aliases and platform names from the raw event. qq-botpy's User
    # wrapper retains `id` but drops fields such as `member_openid`.
    raw_names = getattr(message, 'sweet_mention_names', {}) or {}
    if isinstance(raw_names, dict):
        for member_id, name in raw_names.items():
            if not isinstance(member_id, str):
                continue
            identifiers = _mention_aliases(message, member_id)
            display_name = _resolve_mention_name(identifiers, name, name_resolver)
            if display_name:
                names.update((identifier, display_name) for identifier in identifiers)
    return names


def mentioned_members(message, name_resolver=None, limit=4):
    """Return mentioned group members with OpenIDs, resolving SDK ID aliases."""
    result, seen = [], set()
    openids_by_id = getattr(message, 'sweet_mention_openids', {}) or {}
    own_ids = getattr(message, 'sweet_you_mention_ids', ()) or ()
    bot_ids = getattr(message, 'sweet_bot_mention_ids', ()) or ()
    mentions = getattr(message, 'mentions', []) or []
    for mention in mentions[:50] if isinstance(mentions, (list, tuple)) else []:
        if _field(mention, 'bot') is True:
            continue
        mention_id = _field(mention, 'id')
        explicit_openid = _field(mention, 'member_openid')
        identifiers = tuple(dict.fromkeys(
            value for value in (explicit_openid, mention_id) if isinstance(value, str) and value))
        if not identifiers or any(value in own_ids or value in bot_ids for value in identifiers):
            continue
        # Prefer the QQ member_openid field or raw-event alias map. Recent
        # production events also confirm id == member_openid when only id exists.
        member_openid = (explicit_openid if isinstance(explicit_openid, str) and explicit_openid
                         else openids_by_id.get(mention_id) or mention_id)
        if (not isinstance(member_openid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', member_openid)
                or member_openid in seen):
            continue
        name = (_field(mention, 'username') or _field(mention, 'nickname')
                or _field(mention, 'nick') or _field(mention, 'display_name'))
        display_name = _resolve_mention_name(
            tuple(dict.fromkeys((member_openid, *identifiers))), name, name_resolver)
        seen.add(member_openid)
        result.append({'openid': member_openid, 'name': display_name})
        if len(result) >= limit:
            break
    return result


def render_mention_tags(text, message, name_resolver=None):
    if not isinstance(text, str):
        return ''
    names = mention_name_map(message, name_resolver)
    you_ids = getattr(message, 'sweet_you_mention_ids', ()) or ()
    def replace(match):
        member_id = match.group(1) or match.group(2)
        if member_id in you_ids:
            name = '你'
        else:
            name = next((names.get(identifier) for identifier in _mention_aliases(message, member_id)
                         if names.get(identifier)), '')
            if not name:
                name = _resolve_mention_name(_mention_aliases(message, member_id), '', name_resolver)
            name = name or '群友'
        separator = ' ' if match.end() < len(text) and not text[match.end()].isspace() else ''
        return '@' + name + separator
    return MENTION_TAG.sub(replace, text)


def strip_mention_tags(text):
    if not isinstance(text, str):
        return ''
    return MENTION_TAG.sub(' ', text)


def strip_bot_mention_tags(text, message, *, leading_fallback=False):
    """Remove only the bot's mention tag so other mentioned names reach the model."""
    if not isinstance(text, str):
        return ''
    bot_ids = set(getattr(message, 'sweet_you_mention_ids', ()) or ())
    bot_ids.update(getattr(message, 'sweet_bot_mention_ids', ()) or ())
    if bot_ids:
        return MENTION_TAG.sub(lambda match: ' ' if (match.group(1) or match.group(2)) in bot_ids
                               else match.group(0), text)
    if leading_fallback:
        return re.sub(r'^\s*(?:' + MENTION_TAG.pattern + r'\s*)+', ' ', text, count=1)
    return text


def split_mention_tags(text):
    if not isinstance(text, str):
        return ['']
    parts, start = [], 0
    for match in MENTION_TAG.finditer(text):
        parts.append(text[start:match.start()])
        start = match.end()
    parts.append(text[start:])
    return parts


def sender_name(message):
    author = getattr(message, 'author', None)
    value = (getattr(message, 'sweet_sender_name', '') or getattr(author, 'username', '')
             or getattr(author, 'nickname', ''))
    return clean_display_name(value)


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
                quotes.append({'content':content[:1500], 'member_id':str(author.get('member_openid') or author.get('id') or '')[:128] if isinstance(author,dict) else '', 'member_openid':str(author.get('member_openid') or '')[:128] if isinstance(author,dict) else '', 'member_name':str(author.get('username') or author.get('nickname') or '')[:40] if isinstance(author,dict) else '', 'msg_idx':str(el.get('msg_idx') or '')[:200]})
            collect(el.get('msg_elements'),depth+1)
    if is_reply:collect(data.get('msg_elements'))
    mentions=[]
    raw_mentions=data.get('mentions') or []
    for m in (raw_mentions if isinstance(raw_mentions,list) else [])[:20]:
        if not isinstance(m,dict) or m.get('bot') or m.get('is_you') is True:continue
        member=m.get('member_openid') or m.get('id')
        if isinstance(member,str) and member and len(member)<=128 and member not in own_ids and member not in mentions:mentions.append(member)
    author=data.get('author') or {}
    return {'author_bot':author.get('bot') is True,'member_role':author.get('member_role',''),'mentions':mentions,'is_reply':bool(is_reply),'msg_idx':indices.get('msg_idx',''),
            'reference':{'message_id':str(ref.get('message_id') or '')[:200] if isinstance(ref,dict) else '',
                         'msg_idx':indices.get('ref_msg_idx',''),'quotes':quotes}}


class FullGroupMessage(GroupMessage):
    __slots__ = ('sweet_mentioned','sweet_learning','sweet_author_bot','sweet_sender_name','sweet_quoted_images',
                 'sweet_mention_aliases','sweet_mention_names','sweet_mention_openids',
                 'sweet_you_mention_ids','sweet_bot_mention_ids')

    def __init__(self, api, event_id, data, robot_id):
        super().__init__(api, event_id, data)
        own_ids = {str(v) for v in (robot_id, os.environ.get('QQ_APP_ID')) if v}
        mentions = data.get('mentions') or []
        raw_mentions = [m for m in mentions if isinstance(m, dict)] if isinstance(mentions, list) else []
        self.sweet_mention_aliases = {}
        self.sweet_mention_names = {}
        self.sweet_mention_openids = {}
        self.sweet_you_mention_ids = set(own_ids)
        self.sweet_bot_mention_ids = set()
        for mention in raw_mentions:
            aliases = tuple(dict.fromkeys(
                value for key in ('id', 'member_openid')
                if isinstance((value := mention.get(key)), str) and 0 < len(value) <= 128))
            if not aliases:
                continue
            mention_id, member_openid = mention.get('id'), mention.get('member_openid')
            if isinstance(mention_id, str) and isinstance(member_openid, str) and member_openid:
                self.sweet_mention_openids[mention_id] = member_openid
            for alias in aliases:
                self.sweet_mention_aliases[alias] = aliases
            name = next((mention.get(key) for key in ('username', 'nickname', 'nick', 'display_name')
                         if isinstance(mention.get(key), str) and mention.get(key).strip()), '')
            if name:
                for alias in aliases:
                    self.sweet_mention_names[alias] = name
            if mention.get('is_you') is True or any(alias in own_ids for alias in aliases):
                self.sweet_you_mention_ids.update(aliases)
            if mention.get('bot') is True:
                self.sweet_bot_mention_ids.update(aliases)
        self.sweet_mentioned = any(
            m.get('is_you') is True or any(alias in own_ids for alias in
                                           (m.get('id'), m.get('member_openid')) if isinstance(alias, str))
            for m in raw_mentions)
        tag_ids = set(mention_tag_ids(data.get('content', '')))
        mention_ids = {m.get('id') for m in raw_mentions if isinstance(m.get('id'), str)}
        member_openids = {m.get('member_openid') for m in raw_mentions
                           if isinstance(m.get('member_openid'), str)}
        paired_ids = [(m.get('id'), m.get('member_openid')) for m in raw_mentions
                      if isinstance(m.get('id'), str) and isinstance(m.get('member_openid'), str)]
        logging.getLogger('knowledge-bot').info(
            'GROUP_MENTION_META mentions=%s bot_mentioned=%s tags=%s tag_id_matches=%s '
            'tag_member_openid_matches=%s id_openid_pairs=%s id_openid_mismatches=%s named_mentions=%s',
            len(raw_mentions), self.sweet_mentioned, len(tag_ids), len(tag_ids & mention_ids),
            len(tag_ids & member_openids), len(paired_ids),
            sum(1 for member_id, member_openid in paired_ids if member_id != member_openid),
            sum(1 for m in raw_mentions if any(
                isinstance(m.get(key), str) and m.get(key).strip()
                for key in ('username', 'nickname', 'nick', 'display_name'))))
        self.sweet_sender_name=str((data.get('author') or {}).get('username') or (data.get('author') or {}).get('nickname') or '')[:100]
        self.sweet_learning=reference_metadata(data,own_ids)
        if self.sweet_learning['is_reply'] and not self.sweet_learning['reference']['msg_idx']:
            elements=data.get('msg_elements') or []
            if isinstance(elements,list) and elements and isinstance(elements[0],dict):
                self.sweet_learning['reference']['msg_idx']=str(elements[0].get('msg_idx') or '')[:200]
        # Quoted attachments are ephemeral lookup inputs, never new sends or learning data.
        self.sweet_quoted_images=[]
        if self.sweet_learning['is_reply']:
            elements=data.get('msg_elements') or []
            if isinstance(elements,list) and elements and isinstance(elements[0],dict):
                attachments=elements[0].get('attachments') or []
                if isinstance(attachments,list):
                    self.sweet_quoted_images=[{'content_type':a.get('content_type',''), 'url':a.get('url','')}
                        for a in attachments if isinstance(a,dict) and str(a.get('content_type','')).lower().split('/')[0]=='image'][:10]
        self.sweet_author_bot=self.sweet_learning['author_bot']


def parse_full_group(self, payload):
    message = FullGroupMessage(self.api,payload.get('id'),payload.get('d',{}),getattr(self.robot,'id',None))
    self._dispatch('group_message_create',message)


def parse_at_group(self,payload):
    message=FullGroupMessage(self.api,payload.get('id'),payload.get('d',{}),getattr(self.robot,'id',None))
    message.sweet_mentioned=True
    # This event is emitted specifically for a bot mention. If there is only
    # one mention tag, its identity is unambiguous even if QQ omits metadata.
    tag_ids = mention_tag_ids(getattr(message, 'content', ''))
    if len(tag_ids) == 1:
        message.sweet_you_mention_ids.add(tag_ids[0])
    else:
        bot_tags = set(tag_ids) & message.sweet_bot_mention_ids
        if len(bot_tags) == 1:
            message.sweet_you_mention_ids.update(bot_tags)
    self._dispatch('group_at_message_create',message)


def parse_group_member_add(self,payload):
    data=payload.get('d') or {}
    event=SimpleNamespace(
        event_id=str(payload.get('id') or data.get('event_id') or ''),
        group_openid=str(data.get('group_openid') or ''),
        member_openid=str(data.get('member_openid') or ''),
        user_openid=str(data.get('user_openid') or ''),
        username=str(data.get('username') or ''),
        timestamp=str(data.get('timestamp') or ''),
        raw=data,
    )
    self._dispatch('group_member_add',event)


class FullC2CMessage(C2CMessage):
    __slots__=('sweet_author_bot','sweet_learning','sweet_quoted_images')
    def __init__(self,api,event_id,data):
        super().__init__(api,event_id,data)
        self.sweet_author_bot=(data.get('author') or {}).get('bot') is True
        self.sweet_learning=reference_metadata(data)
        self.sweet_quoted_images=[]
        if self.sweet_learning['is_reply']:
            elements=data.get('msg_elements') or []
            if isinstance(elements,list) and elements and isinstance(elements[0],dict):
                attachments=elements[0].get('attachments') or []
                if isinstance(attachments,list):
                    self.sweet_quoted_images=[{'content_type':a.get('content_type',''), 'url':a.get('url','')}
                        for a in attachments if isinstance(a,dict) and str(a.get('content_type','')).lower().split('/')[0]=='image'][:10]


def parse_c2c(self,payload):
    self._dispatch('c2c_message_create',FullC2CMessage(self.api,payload.get('id'),payload.get('d',{})))


def is_bot(message):
    return getattr(message,'sweet_author_bot',False) or getattr(getattr(message,'author',None),'bot',False) is True


def install():
    # qq-botpy 1.2.1 predates QQ group-member events. Keep the newer gateway
    # intent and parser available without replacing the deployed SDK.
    if 'group_member_event' not in Intents.VALID_FLAGS:
        Intents.group_member_event=Flag(lambda _: 1 << 24)
        Intents.VALID_FLAGS['group_member_event']=1 << 24
    ConnectionState.parse_c2c_message_create = parse_c2c
    ConnectionState.parse_group_message_create = parse_full_group
    ConnectionState.parse_group_at_message_create = parse_at_group
    ConnectionState.parse_group_member_add = parse_group_member_add
