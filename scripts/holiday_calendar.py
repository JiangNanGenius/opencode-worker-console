"""Cached China public-holiday subscription for DeepSeek price-band routing.

The provider treats every weekend as off-peak, including make-up work weekends.
This feed is only needed to identify weekday public holidays. Network failure keeps
the last verified cache; bundled 2026 ranges provide a small fail-safe baseline.
"""
from datetime import date, datetime, timedelta
import json
import math
import time
import urllib.request

from common import STATE, config, read_json, write_json


CACHE = STATE / 'calendar' / 'china-public-holidays.json'
DEFAULT_URLS = [
    'https://www.shuyz.com/githubfiles/china-holiday-calender/master/holidayAPI.json',
    'https://raw.githubusercontent.com/lanceliao/china-holiday-calender/master/holidayAPI.json',
]
# Official State Council 2026 holiday ranges. The subscription remains primary;
# this baseline prevents a first-start network failure from mispricing weekdays.
FALLBACK_RANGES = [
    ('2026-01-01', '2026-01-03'), ('2026-02-15', '2026-02-23'),
    ('2026-04-04', '2026-04-06'), ('2026-05-01', '2026-05-05'),
    ('2026-06-19', '2026-06-21'), ('2026-09-25', '2026-09-27'),
    ('2026-10-01', '2026-10-07'),
]


def validate(value):
    """Strict editable subscription settings; URLs are public data sources only."""
    if not isinstance(value, dict) or set(value) != {'enabled', 'urls', 'refresh_hours'}:
        raise ValueError('deepseek_holiday_calendar requires enabled, urls and refresh_hours')
    if not isinstance(value.get('enabled'), bool):
        raise ValueError('deepseek_holiday_calendar.enabled must be boolean')
    urls = value.get('urls')
    if not isinstance(urls, list) or not 1 <= len(urls) <= 5 or any(
            not isinstance(item, str) or not item.startswith('https://') or len(item) > 1000
            for item in urls):
        raise ValueError('deepseek_holiday_calendar.urls requires 1 to 5 HTTPS URLs')
    hours = value.get('refresh_hours')
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or \
            not math.isfinite(hours) or not 1 <= hours <= 168:
        raise ValueError('deepseek_holiday_calendar.refresh_hours must be between 1 and 168')
    return {'enabled': value['enabled'], 'urls': list(dict.fromkeys(urls)),
            'refresh_hours': float(hours)}


def settings(value=None):
    source = value if isinstance(value, dict) else {}
    enabled = source.get('enabled', False) is True
    urls = source.get('urls')
    if not isinstance(urls, list) or not urls or any(not isinstance(x, str) or
            not x.startswith('https://') or len(x) > 1000 for x in urls):
        urls = list(DEFAULT_URLS)
    hours = source.get('refresh_hours', 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or \
            not math.isfinite(hours) or not 1 <= hours <= 168:
        hours = 24
    return {'enabled': enabled, 'urls': urls, 'refresh_hours': float(hours)}


def _dates_between(start, end):
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first or (last - first).days > 31:
        raise ValueError('invalid holiday range')
    while first <= last:
        yield first.isoformat()
        first += timedelta(days=1)


def _normalize(payload):
    years = payload.get('Years') if isinstance(payload, dict) else None
    if not isinstance(years, dict):
        raise ValueError('holiday feed has no Years object')
    holidays = set()
    sources = set()
    for records in years.values():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                holidays.update(_dates_between(record.get('StartDate'), record.get('EndDate')))
            except (TypeError, ValueError):
                continue
            if isinstance(record.get('URL'), str) and record['URL'].startswith('https://'):
                sources.add(record['URL'])
    if not holidays:
        raise ValueError('holiday feed is empty')
    return {'holidays': sorted(holidays), 'authority_urls': sorted(sources),
            'generated': payload.get('Generated')}


def refresh(force=False, now=None):
    """Refresh the configured subscription, retaining a good cache on failure."""
    now = time.time() if now is None else float(now)
    cfg = settings((config() or {}).get('deepseek_holiday_calendar'))
    cached = read_json(CACHE, {})
    if not cfg['enabled']:
        return cached
    fetched = cached.get('fetched_at') if isinstance(cached, dict) else None
    if not force and isinstance(fetched, (int, float)) and \
            now - fetched < cfg['refresh_hours'] * 3600:
        return cached
    last_error = None
    for url in cfg['urls']:
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'WorkerDesk/1 holiday-calendar'})
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('holiday feed is too large')
            normalized = _normalize(json.loads(raw.decode('utf-8')))
            normalized.update(fetched_at=now, subscription_url=url)
            write_json(CACHE, normalized)
            return normalized
        except Exception as error:
            last_error = type(error).__name__
    if isinstance(cached, dict) and cached.get('holidays'):
        return cached
    return {'holidays': sorted({item for start, end in FALLBACK_RANGES
                                for item in _dates_between(start, end)}),
            'fetched_at': None, 'subscription_error': last_error,
            'authority_urls': ['https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm']}


def is_public_holiday(day):
    """Return True only for a cached or bundled public-holiday date."""
    if isinstance(day, datetime):
        day = day.date()
    if not isinstance(day, date):
        return False
    cached = read_json(CACHE, {})
    holidays = cached.get('holidays') if isinstance(cached, dict) else None
    if not isinstance(holidays, list):
        holidays = [item for start, end in FALLBACK_RANGES for item in _dates_between(start, end)]
    return day.isoformat() in set(holidays)
