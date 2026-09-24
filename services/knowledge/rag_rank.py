"""Lightweight lexical reranking for the small shop knowledge base.

FTS supplies candidates; this module ranks the actual question intent without
using answer text as a retrieval key or requiring another model call.
"""
import re
from difflib import SequenceMatcher


INTENTS = {
    'stock': r'几个|多少个|多少份|余量|库存|剩余|有货|现货|卖完|售罄|还有吗',
    'stock_count': r'几个|多少个|多少份|余量|库存|剩余|剩几个|剩多少',
    'availability': r'有货|现货|卖完|售罄|还有吗',
    'progress': r'生产进度|进度|做了几个|做了多少|制作进度',
    'price': r'多少钱|价格|售价|多少元|定价|贵|全款|总价|费用|价钱|\d+(?:元|块|r)',
    'deposit': r'意向金|定金|预付款',
    'balance': r'尾款|补款',
    'deadline': r'截止|到什么时候|到几号|何时结束',
    'purchase': r'哪里买|在哪买|怎么买|购买|下单|链接|上架|哪里销售',
    'shipping': r'发货|到货|工期|寄出|配送',
    'address': r'收货地址|改地址|修改地址',
    'start': r'开工|开团|开做|制作',
}
FILLER = re.compile(r'现在|目前|请问|想问|一下|这个|那个|的话|到底|呀|啊|呢|喵|[？?，,。！!\s]')
PRODUCT_TYPES = ('翻面猫', '娃娃', '手偶')


def conflicts(query, row, catalog):
    """Reject evidence that explicitly names a different entity or product type."""
    question = catalog.normalize(query)
    title = catalog.normalize(row.get('question') or row.get('title') or '')
    named_query = set(catalog.referenced(question)) - {'翻面猫'}
    named_title = set(catalog.referenced(title)) - {'翻面猫'}
    if named_query and named_title and not named_query & named_title:
        return True
    if row.get('source_type') == 'qa' and re.search(r'第一批|首批|场贩', title) and not re.search(r'第一批|首批|场贩', question):
        return True
    if named_query and '新企划' in title and '新企划' not in question and not named_query & named_title:
        return True
    requested = {kind for kind in PRODUCT_TYPES if kind in question}
    described = {kind for kind in PRODUCT_TYPES if kind in title}
    if requested and described and not requested & described:
        return True
    question_intents, title_intents = intents(question), intents(title)
    return bool(row.get('source_type') == 'qa' and question_intents and title_intents and not question_intents & title_intents)


def compact(text):
    return FILLER.sub('', text.casefold())


def grams(text):
    result = set()
    for part in re.findall(r'[\u3400-\u9fff]+|[a-z0-9_]+', compact(text)):
        if '\u3400' <= part[0] <= '\u9fff':
            result.update(part[i:i + 2] for i in range(len(part) - 1))
        else:
            result.add(part)
    return result


def intents(text):
    return {name for name, pattern in INTENTS.items() if re.search(pattern, text)}


def score(query, row, catalog, rank=0):
    """Score a candidate title against the current question, not old answers."""
    question = catalog.normalize(query)
    title = catalog.normalize(row.get('question') or row.get('title') or '')
    qgrams = grams(question)
    tgrams = grams(title)
    overlap = len(qgrams & tgrams) / max(1, len(qgrams))
    similarity = SequenceMatcher(None, compact(question), compact(title)).ratio()
    qintents, tintents = intents(question), intents(title)
    intent = (0.45 * len(qintents & tintents) / len(qintents) -
              (0.25 if qintents and not qintents & tintents else 0)) if qintents else 0
    named = [name for name in catalog.referenced(question) if name not in ('翻面猫',)]
    entity = 0.18 if named and any(name in title for name in named) else (-1.0 if named else 0)
    requested_type = next((kind for kind in PRODUCT_TYPES if kind in question), '')
    title_types = {kind for kind in PRODUCT_TYPES if kind in title}
    product = -0.7 if requested_type and title_types and requested_type not in title_types else 0
    scope = -0.45 if re.search(r'第一批|首批|场贩', title) and not re.search(r'第一批|首批|场贩', question) else 0
    source = 0.23 if row.get('source_type') == 'qa' else 0
    return round(1.1 * overlap + 0.35 * similarity + intent + entity + product + scope + source + min(rank, 0.1), 6)
