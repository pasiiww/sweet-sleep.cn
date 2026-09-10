"""Admin-maintained circle merchandise, optionally indexed as knowledge documents."""
import json
import secrets
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

FIELDS = {'series':150, 'character':2000, 'image':2000, 'notes':4000}
LABELS = {'series':'制品系列', 'character':'角色名字', 'image':'制品图片', 'notes':'备注'}

def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS circle_products (
      id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
      value TEXT NOT NULL, revision TEXT NOT NULL, updated_at TEXT NOT NULL,
      document_id TEXT REFERENCES documents(id) ON DELETE SET NULL)''')

def public(row):
    result=dict(row) | json.loads(row['value'])
    result.setdefault('characters',[result['character']])
    result.setdefault('variants',[])
    return result

def money(app,price):
    if price:
        try:
            amount=Decimal(price)
            if not amount.is_finite() or amount<0 or amount>10000000 or amount.as_tuple().exponent < -2:raise InvalidOperation()
            return format(amount,'f')
        except InvalidOperation:app.fail(400,'金额需为非负数字，最多两位小数；未知请留空')
    return ''

def handle(app,c,method,segments,data,params):
    if len(segments)==1 and method=='GET':
        kb=params.get('kb_id',[''])[0]; app.base(c,kb)
        query=params.get('q',[''])[0].casefold().strip()
        rows=[public(r) for r in c.execute('SELECT * FROM circle_products WHERE kb_id=? ORDER BY updated_at DESC,id',(kb,))]
        return {'items':[r for r in rows if not query or query in json.dumps({k:r.get(k) for k in (*FIELDS,'types','links')},ensure_ascii=False).casefold()]}
    old=c.execute('SELECT * FROM circle_products WHERE id=?',(segments[1],)).fetchone() if len(segments)==2 else None
    if len(segments)==2 and not old:app.fail(404,'制品不存在')
    if method in ('PUT','DELETE') and old and data.get('revision')!=old['revision']:
        app.fail(409,'制品已被修改，请刷新后重试')
    if method=='DELETE' and old:
        if old['document_id']: c.execute('DELETE FROM documents WHERE id=?',(old['document_id'],))
        c.execute('DELETE FROM circle_products WHERE id=?',(old['id'],))
        return {'deleted':True}
    if not ((method=='POST' and len(segments)==1) or (method=='PUT' and old)):
        app.fail(405,'不支持此操作')
    kb=app.base(c,old['kb_id'] if old else app.string(data,'kb_id',80,True))
    item={k:app.string(data,k,limit) for k,limit in FIELDS.items()}
    characters=data.get('characters',[item['character']] if item['character'] else [])
    if not isinstance(characters,list) or not 1<=len(characters)<=30:app.fail(400,'请填写1–30个角色')
    if any(not isinstance(v,str) or not v.strip() or len(v)>60 for v in characters):app.fail(400,'角色名字不能为空，最多60字')
    item['characters']=list(dict.fromkeys(v.strip() for v in characters))
    item['character']='、'.join(item['characters'])
    types=data.get('types',[]);links=data.get('links',[])
    if not isinstance(types,list) or not 1<=len(types)<=30:app.fail(400,'请添加1–30种制品类型')
    if not isinstance(links,list) or len(links)>20:app.fail(400,'最多20个平台链接')
    item['types']=[];names=set()
    for entry in types:
        if not isinstance(entry,dict):app.fail(400,'制品类型格式错误')
        name=app.string(entry,'name',80,True);price=app.string(entry,'price',30)
        if name.casefold() in names:app.fail(400,'制品类型不能重复')
        names.add(name.casefold())
        price=money(app,price)
        item['types'].append({'name':name,'price':price})
    variants=data.get('variants',[])
    if not isinstance(variants,list) or len(variants)>900:app.fail(400,'最多900个角色与类型组合')
    item['variants']=[];pairs=set();type_names={t['name'] for t in item['types']}
    for v in variants:
        if not isinstance(v,dict):app.fail(400,'规格格式错误')
        character=app.string(v,'character',60,True);kind=app.string(v,'type',80,True)
        pair=(character,kind)
        if character not in item['characters'] or kind not in type_names:app.fail(400,'规格中的角色或类型不在当前列表，请修改或删除对应规格')
        if pair in pairs:app.fail(400,'同一角色与类型不能重复配置')
        pairs.add(pair);status=app.string(v,'status',20,True)
        if status not in ('待确认','在售','售罄','不售卖'):app.fail(400,'规格状态无效')
        item['variants'].append({'character':character,'type':kind,'price':money(app,app.string(v,'price',30)),'status':status})
    def valid_link(value):
        url=urlsplit(value)
        if url.scheme not in ('https','http') or not url.hostname or url.username or url.password:
            app.fail(400,'平台链接和图片地址需为有效的 HTTP / HTTPS 地址')
    item['links']=[]
    for entry in links:
        if not isinstance(entry,dict):app.fail(400,'平台链接格式错误')
        name=app.string(entry,'name',80,True);url=app.string(entry,'url',2000,True);valid_link(url)
        item['links'].append({'name':name,'url':url})
    if item['image'] and not item['image'].startswith('/knowledge/sticker-files/'):valid_link(item['image'])
    if item['image'].startswith('/knowledge/sticker-files/'):
        import re
        if not re.fullmatch(r'/knowledge/sticker-files/[a-f0-9]{32}\.(png|jpg|gif)',item['image']):app.fail(400,'图片路径无效')
    if type(data.get('searchable',False)) is not bool:app.fail(400,'检索开关格式错误')
    item['searchable']=data.get('searchable',False)
    pid=old['id'] if old else secrets.token_hex(8)
    doc=old['document_id'] if old else None
    if not old and c.execute('SELECT count(*) FROM circle_products WHERE kb_id=?',(kb['id'],)).fetchone()[0]>=1000:
        app.fail(400,'每个知识库最多维护1000条制品')
    if item['searchable']:
        content='\n'.join(LABELS[k]+'：'+item[k] for k in FIELDS if item[k])
        content+='\n参考价格（人民币元，仅供参考，具体价格请到平台查询）：\n'+'\n'.join(t['name']+'：'+(t['price']+'元' if t['price'] else '请查询平台') for t in item['types'])
        content+='\n角色和类型是商品选项，不代表任意组合都有货。未维护的组合价格与售卖情况请到平台确认；不从起步价或优惠价推断具体规格价格。'
        content+='\n单独维护的规格（优先于类型参考价）：\n'+'\n'.join(v['character']+' / '+v['type']+'：'+v['status']+'；'+('参考价'+v['price']+'元' if v['price'] else '未单独报价，参考类型价格，具体以平台为准') for v in item['variants'])
        content+='\n平台链接：\n'+'\n'.join(t['name']+'：'+t['url'] for t in item['links'])
        doc=app.save_document(c,kb,{'title':((item['series']+' · ' if item['series'] else '')+item['character'])[:200],'content':content,
                                  'source':'社团制品后台'},doc)['id']
    elif doc:
        c.execute('DELETE FROM documents WHERE id=?',(doc,));doc=None
    c.execute('INSERT INTO circle_products VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value,revision=excluded.revision,updated_at=excluded.updated_at,document_id=excluded.document_id',
              (pid,kb['id'],json.dumps(item,ensure_ascii=False),secrets.token_hex(8),app.now(),doc))
    return public(c.execute('SELECT * FROM circle_products WHERE id=?',(pid,)).fetchone())
