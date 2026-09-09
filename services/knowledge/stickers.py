"""Administrator-managed sticker names and public image paths."""
import ipaddress
import re
from urllib.parse import urlsplit,quote,unquote


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS stickers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,
        path TEXT NOT NULL,url TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
        revision TEXT NOT NULL,updated_at TEXT NOT NULL)''')


def image_url(path):
    if not isinstance(path,str) or not 1<=len(path.strip())<=2000:raise ValueError('请填写图片路径或 HTTPS 地址')
    path=path.strip()
    if path.startswith('/www/wwwroot/myweb/'):path=path.removeprefix('/www/wwwroot/myweb')
    if path.startswith('/') and not path.startswith('//'):path='https://sweet-sleep.cn'+quote(path,safe='/%')
    try:
        parsed=urlsplit(path);host=parsed.hostname or ''
        if parsed.scheme!='https' or not host or parsed.username or parsed.password or parsed.port not in (None,443) or parsed.fragment:raise ValueError()
        if host in ('localhost','localhost.localdomain') or '.' not in host or host.endswith(('.local','.internal')):raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:raise ValueError()
        except ValueError:
            if re.fullmatch(r'[0-9.:]+',host):raise ValueError()
        if any(segment in ('.','..') for segment in unquote(parsed.path).split('/')):raise ValueError()
        if not parsed.path.lower().endswith(('.png','.jpg','.jpeg')):raise ValueError()
    except ValueError:raise ValueError('请使用公开 HTTPS 的 PNG/JPG 图片，或本站路径，例如 /stickers/开心.png') from None
    return path


def available(c):
    return [dict(r) for r in c.execute('SELECT id,name,path,url,revision FROM stickers WHERE enabled=1 ORDER BY id LIMIT 100')]
