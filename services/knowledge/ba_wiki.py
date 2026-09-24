"""Bounded, read-only lookup for Blue Archive community wikis.

The module deliberately uses fixed public endpoints and never accepts a URL
from the caller. Responses live in a small disk cache shared by the API service
and MCP process.
"""
import hashlib
import json
import os
import re
import sqlite3
import ssl
import threading
import time
from pathlib import Path
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


GAMEKEE_HOST = 'www.gamekee.com'
WIKIRU_HOST = 'bluearchive.wikiru.jp'
GAMEKEE_GAME_ID = 829
GAMEKEE_STUDENT_ROOT_ID = 23941
USER_AGENT = 'SweetSleepBAWiki/1.0 (+https://sweet-sleep.cn/knowledge/)'
REQUEST_TIMEOUT = 8
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_RESULTS = 4
MAX_RESULT_CHARS = 4200
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 128
_CACHE_MAX_BYTES = 12 * 1024 * 1024
_CACHE_TTL = 30 * 24 * 3600


def _cache_key(kind, host, path, query):
    raw = json.dumps([kind, host, path, query], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _cache_path():
    data_dir = os.environ.get('KB_DATA_DIR', '').strip()
    if not data_dir:
        return None
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / 'ba-wiki-cache.sqlite3'


def _cache_get(key):
    now = time.time()
    path = _cache_path()
    if path is not None:
        with sqlite3.connect(path, timeout=10) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('''CREATE TABLE IF NOT EXISTS wiki_cache (
                cache_key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL,
                size INTEGER NOT NULL, accessed REAL NOT NULL)''')
            row = conn.execute('SELECT expires,payload FROM wiki_cache WHERE cache_key=?', (key,)).fetchone()
            if row and row[0] > now:
                conn.execute('UPDATE wiki_cache SET accessed=? WHERE cache_key=?', (now, key))
                return True, json.loads(row[1])
            if row:
                conn.execute('DELETE FROM wiki_cache WHERE cache_key=?', (key,))
        return False, None
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return True, cached[1]
        if cached:
            del _CACHE[key]
    return False, None


def _cache_set(key, value, ttl):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > _CACHE_MAX_BYTES:
        return
    now = time.time()
    ttl = min(max(0, ttl), _CACHE_TTL)
    path = _cache_path()
    if path is not None:
        with sqlite3.connect(path, timeout=10) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('''CREATE TABLE IF NOT EXISTS wiki_cache (
                cache_key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL,
                size INTEGER NOT NULL, accessed REAL NOT NULL)''')
            conn.execute('DELETE FROM wiki_cache WHERE expires<=?', (now,))
            conn.execute('''INSERT INTO wiki_cache(cache_key,expires,payload,size,accessed) VALUES(?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET expires=excluded.expires,payload=excluded.payload,
                size=excluded.size,accessed=excluded.accessed''',
                (key, now + ttl, raw.decode('utf-8'), len(raw), now))
            while True:
                count, size = conn.execute('SELECT count(*),coalesce(sum(size),0) FROM wiki_cache').fetchone()
                if count <= _CACHE_MAX and size <= _CACHE_MAX_BYTES:
                    break
                victim = conn.execute('SELECT cache_key FROM wiki_cache ORDER BY accessed,expires LIMIT 1').fetchone()
                if not victim:
                    break
                conn.execute('DELETE FROM wiki_cache WHERE cache_key=?', victim)
            free_pages = conn.execute('PRAGMA freelist_count').fetchone()[0]
            conn.commit()
            if free_pages > 64:
                conn.execute('VACUUM')
        try:
            # The data directory is service-only (0700); the MCP process may run as root.
            path.chmod(0o666)
        except OSError:
            pass
        return
    with _CACHE_LOCK:
        for expired in [cache_key for cache_key, item in _CACHE.items() if item[0] <= now]:
            del _CACHE[expired]
        _CACHE.pop(key, None)
        while _CACHE and (len(_CACHE) >= _CACHE_MAX or sum(item[2] for item in _CACHE.values()) + len(raw) > _CACHE_MAX_BYTES):
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            del _CACHE[oldest]
        _CACHE[key] = (now + ttl, value, len(raw))


def _json_get(host, path, params, *, headers=None, ttl=_CACHE_TTL):
    query = urlencode(params, doseq=True)
    url = f'https://{host}{path}' + (f'?{query}' if query else '')
    key = _cache_key('json', host, path, query)
    found, cached = _cache_get(key)
    if found:
        return cached
    request_headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        response = urlopen(request, timeout=REQUEST_TIMEOUT, context=_SSL_CONTEXT)
    except HTTPError as exc:
        raise ValueError(f'{host} 返回 HTTP {exc.code}') from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError(f'{host} 暂时无法访问') from exc
    with response:
        final = urlsplit(response.geturl())
        if final.scheme != 'https' or final.hostname != host:
            raise ValueError('Wiki 请求跳转到了未允许的站点')
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError(f'{host} 返回内容超过大小限制')
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{host} 返回了无法识别的数据') from exc
    _cache_set(key, value, ttl)
    return value


def _text_get(host, path, *, headers=None, ttl=_CACHE_TTL):
    key = _cache_key('text', host, path, '')
    found, cached = _cache_get(key)
    if found:
        return cached
    request_headers = {'User-Agent': USER_AGENT, 'Accept': 'text/html'}
    if headers:
        request_headers.update(headers)
    try:
        response = urlopen(Request(f'https://{host}{path}', headers=request_headers),
                           timeout=REQUEST_TIMEOUT, context=_SSL_CONTEXT)
    except HTTPError as exc:
        raise ValueError(f'{host} 返回 HTTP {exc.code}') from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError(f'{host} 暂时无法访问') from exc
    with response:
        final = urlsplit(response.geturl())
        if final.scheme != 'https' or final.hostname != host:
            raise ValueError('Wiki 请求跳转到了未允许的站点')
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError(f'{host} 返回内容超过大小限制')
    text = raw.decode('utf-8', 'replace')
    _cache_set(key, text, ttl)
    return text


def _gamekee_headers():
    return {'X-Requested-With': 'XMLHttpRequest', 'game-alias': 'ba', 'Lang': 'zh-cn',
            'Referer': 'https://www.gamekee.com/ba/'}


def _gamekee_data(path, params, *, ttl=_CACHE_TTL):
    payload = _json_get(GAMEKEE_HOST, path, params, headers=_gamekee_headers(), ttl=ttl)
    if not isinstance(payload, dict) or payload.get('code') != 0:
        raise ValueError('GameKee 没有返回可用资料')
    return payload.get('data')


def _flatten_entries(node):
    if isinstance(node, list):
        for item in node:
            yield from _flatten_entries(item)
    elif isinstance(node, dict):
        if node.get('content_id') and node.get('name'):
            yield node
        for key in ('child', 'children'):
            if key in node:
                yield from _flatten_entries(node[key])


def _student_tree():
    data = _gamekee_data('/v1/entry/treesByPidV1', {'pid': GAMEKEE_STUDENT_ROOT_ID})
    if not isinstance(data, dict) or data.get('id') != GAMEKEE_STUDENT_ROOT_ID:
        raise ValueError('GameKee 学生图鉴暂不可用')
    return list(_flatten_entries(data))


def _normalize(value):
    return re.sub(r'[^\w\u3400-\u9fff]+', '', str(value).casefold())


def _aliases(entry):
    values = [entry.get('name', '')]
    raw_aliases = entry.get('name_alias', '')
    if isinstance(raw_aliases, str):
        values.extend(re.split(r'[,，、;/|]+', raw_aliases))
    return [value.strip() for value in values if isinstance(value, str) and len(_normalize(value.strip())) >= 2]


def _student_matches(query, entries, limit):
    normalized_query = _normalize(query)
    if not normalized_query:
        return []
    matches = []
    for entry in entries:
        if entry.get('is_del') or not entry.get('content_id'):
            continue
        best = 0
        for alias in _aliases(entry):
            normalized_alias = _normalize(alias)
            if normalized_alias == normalized_query:
                best = max(best, 1000 + len(normalized_alias))
            elif normalized_alias in normalized_query:
                best = max(best, 500 + len(normalized_alias))
            elif normalized_query in normalized_alias and len(normalized_query) >= 3:
                best = max(best, 100 + len(normalized_query))
        if best:
            matches.append((best, entry))
    matches.sort(key=lambda row: (-row[0], str(row[1].get('name', '')), int(row[1].get('content_id', 0))))
    unique, seen = [], set()
    for _, entry in matches:
        content_id = entry.get('content_id')
        if content_id not in seen:
            seen.add(content_id)
            unique.append(entry)
        if len(unique) >= limit:
            break
    return unique


def _gamekee_student_result(entry):
    content_id = int(entry['content_id'])
    result = _gamekee_data(f'/v1/content/detail/{content_id}', {})
    if not isinstance(result, dict) or result.get('game_id') != GAMEKEE_GAME_ID:
        return None
    title = str(result.get('title') or entry.get('name') or '蔚蓝档案角色')[:120]
    summary = str(result.get('summary') or result.get('desc') or '').strip()
    try:
        seo = json.loads(result.get('seo') or '{}')
        if isinstance(seo, dict):
            summary = summary or str(seo.get('desc') or '')
    except (json.JSONDecodeError, TypeError):
        pass
    aliases = '、'.join(_aliases(entry)[:8])
    content = f'角色：{title}'
    if aliases:
        content += f'；别名：{aliases}'
    if summary:
        content += '\n' + summary
    url = f'https://www.gamekee.com/ba/tj/{content_id}.html'
    return {'title': title, 'content': content[:MAX_RESULT_CHARS], 'source': 'GameKee',
            'source_type': 'wiki', 'url': url, 'updated_at': result.get('updated_at', '')}


def _gamekee_search(query, limit):
    entries = _student_tree()
    matches = _student_matches(query, entries, min(limit, 3))
    results = []
    if matches:
        for entry in matches:
            try:
                result = _gamekee_student_result(entry)
                if result:
                    results.append(result)
            except ValueError:
                continue
        if results:
            return results, entries

    data = _gamekee_data('/v1/content/searchArticle', {'keyword': query[:120], 'page': 1, 'pageSize': 5})
    if not isinstance(data, list):
        return [], entries
    for row in data:
        if not isinstance(row, dict) or row.get('game_id') != GAMEKEE_GAME_ID:
            continue
        content_id = row.get('id')
        title = str(row.get('title') or 'GameKee 文章')[:120]
        summary = str(row.get('summary') or row.get('desc') or '').strip()
        if not summary:
            continue
        results.append({'title': title, 'content': summary[:MAX_RESULT_CHARS], 'source': 'GameKee',
                        'source_type': 'wiki', 'url': f'https://www.gamekee.com/ba/{int(content_id)}.html',
                        'updated_at': row.get('updated_at', '')})
        if len(results) >= limit:
            break
    return results, entries


def _wikiru_title(query, entries):
    matches = _student_matches(query, entries, 1) if entries else []
    if not matches:
        return query[:120]
    aliases = _aliases(matches[0])
    japanese = [alias for alias in aliases if re.search(r'[\u3040-\u30ff]', alias)]
    return japanese[0] if japanese else aliases[0] if aliases else query[:120]


class _WikiPageParser(HTMLParser):
    VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                 'meta', 'param', 'source', 'track', 'wbr'}
    BLOCK_TAGS = {'br', 'div', 'li', 'p', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'hr'}
    SKIP_TAGS = {'script', 'style', 'noscript', 'svg'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.body_parts = []
        self.in_title = False
        self.body_depth = 0
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title':
            self.in_title = True
        if tag in self.SKIP_TAGS:
            if self.body_depth:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if not self.body_depth and tag == 'div' and attrs.get('id') == 'body':
            self.body_depth = 1
            return
        if self.body_depth:
            if tag in self.BLOCK_TAGS:
                self.body_parts.append('\n')
            if tag not in self.VOID_TAGS:
                self.body_depth += 1

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth or not self.body_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.body_parts.append('\n')
        if tag not in self.VOID_TAGS:
            self.body_depth -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.body_depth and not self.skip_depth:
            self.body_parts.append(data)


def _wikiru_search(query, entries):
    page = _wikiru_title(query, entries)
    path = '/?' + quote(page, safe='')
    html = _text_get(WIKIRU_HOST, path, headers={'Referer': 'https://bluearchive.wikiru.jp/'})
    parser = _WikiPageParser()
    parser.feed(html)
    page_title = ' '.join(' '.join(parser.title_parts).split())
    if page_title.lower().startswith('runtime error'):
        return []
    content = '\n'.join(' '.join(line.split()) for line in ''.join(parser.body_parts).splitlines())
    content = re.sub(r'\n{3,}', '\n\n', content).strip()
    if len(content) < 100:
        return []
    title = page_title.split(' - ', 1)[0][:160] or page
    content = f'日文 Wiki 页面标题：{title}\n' + content[:MAX_RESULT_CHARS - 32]
    return [{'title': title, 'content': content, 'source': 'Blue Archive Wikiru',
             'source_type': 'wiki', 'url': f'https://{WIKIRU_HOST}{path}', 'updated_at': ''}]


def search(query, source='auto', limit=3):
    """Search GameKee, or the Japanese Blue Archive Wikiru wiki when requested/fallback."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query 不能为空')
    query = query.strip()[:120]
    if source not in ('auto', 'gamekee', 'bluearchivewiki'):
        raise ValueError('source 必须是 auto、gamekee 或 bluearchivewiki')
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f'limit 必须是 1 到 {MAX_RESULTS} 之间的整数')

    errors = []
    entries = []
    if source in ('auto', 'gamekee'):
        try:
            results, entries = _gamekee_search(query, limit)
            if results or source == 'gamekee':
                return {'query': query, 'source': 'GameKee', 'results': results[:limit], 'source_errors': errors}
        except ValueError as exc:
            errors.append({'source': 'GameKee', 'error': str(exc)})
            if source == 'gamekee':
                return {'query': query, 'source': 'GameKee', 'results': [], 'source_errors': errors}
    elif source == 'bluearchivewiki':
        try:
            entries = _student_tree()
        except ValueError as exc:
            errors.append({'source': 'GameKee', 'error': str(exc)})

    try:
        results = _wikiru_search(query, entries)
        return {'query': query, 'source': 'Blue Archive Wikiru', 'results': results[:limit], 'source_errors': errors}
    except ValueError as exc:
        errors.append({'source': 'Blue Archive Wikiru', 'error': str(exc)})
        return {'query': query, 'source': 'Blue Archive Wikiru', 'results': [], 'source_errors': errors}
