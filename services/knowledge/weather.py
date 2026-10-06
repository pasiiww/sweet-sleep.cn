"""Small QWeather client and deterministic drink-temperature policy."""
import json
import math
import re
import zlib
from datetime import datetime
from urllib import parse, request
from zoneinfo import ZoneInfo

API_HOST_PATTERN = re.compile(r'^(?:[a-z0-9-]+\.)*(?:qweatherapi\.com|qweather\.com)$', re.I)
HANGZHOU_LATITUDE = 30.27
HANGZHOU_LONGITUDE = 120.15
HANGZHOU_TZ = ZoneInfo('Asia/Shanghai')


def validate_api_host(value):
    if not isinstance(value, str):
        raise ValueError('天气 API Host 格式不正确')
    host = value.strip().rstrip('.').lower()
    if len(host) > 253 or not API_HOST_PATTERN.fullmatch(host):
        raise ValueError('请输入和风天气控制台提供的 API Host 域名')
    return host


def parse_daily_forecast(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('days'), list):
        raise ValueError('天气接口返回格式不正确')
    forecasts = []
    for item in payload['days']:
        if not isinstance(item, dict) or not isinstance(item.get('forecastStartTime'), str):
            continue
        try:
            starts_at = datetime.fromisoformat(item['forecastStartTime'].replace('Z', '+00:00'))
            if starts_at.tzinfo is not None:
                date = starts_at.astimezone(HANGZHOU_TZ).date().isoformat()
            else:
                date = starts_at.date().isoformat()
            maximum = item['temperatureMax']['value']
            maximum = float(maximum)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(maximum) and -80 <= maximum <= 60:
            forecasts.append({'date': date, 'max_temp': maximum})
    if not forecasts:
        raise ValueError('天气接口没有返回有效的最高温')
    return forecasts


def fetch_hangzhou_forecast(api_host, api_key):
    host = validate_api_host(api_host)
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 500:
        raise ValueError('天气 API Key 未配置')
    query = parse.urlencode({'days': 3, 'localTime': 'true', 'lang': 'zh'})
    url = (f'https://{host}/weather/v1/daily/'
           f'{HANGZHOU_LATITUDE:.2f}/{HANGZHOU_LONGITUDE:.2f}?{query}')
    req = request.Request(url, headers={
        'X-QW-Api-Key': api_key.strip(),
        'Accept': 'application/json',
        'User-Agent': 'SweetSleepDrinkBot/1.0',
    })
    with request.urlopen(req, timeout=6) as response:
        if response.status != 200:
            raise RuntimeError('天气接口暂不可用')
        raw = response.read(128001)
        content_encoding = response.headers.get('Content-Encoding', '').lower()
    if len(raw) > 128000:
        raise ValueError('天气接口响应过大')
    if content_encoding == 'gzip' or raw.startswith(b'\x1f\x8b'):
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decompressor.decompress(raw, 128001)
        if len(raw) > 128000 or decompressor.unconsumed_tail or not decompressor.eof:
            raise ValueError('天气接口解压后的响应过大或不完整')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('天气接口返回格式不正确') from exc
    return parse_daily_forecast(payload)


def temperature_preference(today_max, yesterday_max=None):
    """Return cold, hot, or random based on Hangzhou daily high temperatures."""
    if today_max > 27:
        return 'cold'
    if today_max < 18:
        return 'hot'
    if yesterday_max is not None:
        change = today_max - yesterday_max
        if change >= 3:
            return 'cold'
        if change <= -3:
            return 'hot'
    return 'random'
