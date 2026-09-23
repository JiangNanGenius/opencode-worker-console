"""Best-effort operator notifications without serializing notification secrets."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import common
import credentials
import quota


STATE_FILE = 'notifications.json'
DEFAULT_ICON = ('https://raw.githubusercontent.com/JiangNanGenius/'
                'opencode-worker-console/main/web/worker-desk-icon.png')
DEFAULTS = {
    'enabled': False,
    'credential': 'bark-endpoint',
    'credentials': ['bark-endpoint'],
    'group': 'Worker Desk',
    'icon': DEFAULT_ICON,
    'quota_transitions': True,
    'model_switches': False,
}
MAX_CLIENTS = 8
_CREDENTIAL_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')


def normalize(value):
    raw = value if isinstance(value, dict) else {}
    result = dict(DEFAULTS)
    for key in ('enabled', 'quota_transitions', 'model_switches'):
        if key in raw:
            result[key] = raw[key] is True
    configured = raw.get('credentials')
    if isinstance(configured, list):
        names = [item.strip() for item in configured
                 if isinstance(item, str) and item.strip()]
    else:
        credential = raw.get('credential')
        names = [credential.strip()] if isinstance(credential, str) and credential.strip() else []
    names = list(dict.fromkeys(names))[:MAX_CLIENTS] or ['bark-endpoint']
    result['credentials'] = names
    # Preserve the legacy scalar for older installed clients during rolling upgrades.
    result['credential'] = names[0]
    group = raw.get('group')
    if isinstance(group, str) and group.strip():
        result['group'] = group.strip()[:64]
    icon = raw.get('icon')
    if isinstance(icon, str):
        result['icon'] = icon.strip()
    return result


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('notifications must be an object')
    for key in ('enabled', 'quota_transitions', 'model_switches'):
        if key in value and not isinstance(value[key], bool):
            raise ValueError('notifications.' + key + ' must be boolean')
    result = normalize(value)
    raw_names = value.get('credentials')
    if raw_names is not None and (not isinstance(raw_names, list) or not raw_names or
                                  len(raw_names) > MAX_CLIENTS):
        raise ValueError('notifications.credentials must contain 1-8 credential names')
    if isinstance(raw_names, list) and any(not isinstance(name, str) or not name.strip()
                                           for name in raw_names):
        raise ValueError('notifications.credentials must contain non-empty strings')
    if any(not _CREDENTIAL_NAME.fullmatch(name) for name in result['credentials']):
        raise ValueError('notifications.credentials contains an invalid credential name')
    if not result['group']:
        raise ValueError('notifications.group cannot be empty')
    if len(result['icon']) > 2048:
        raise ValueError('notifications.icon is too long')
    if result['icon']:
        parsed = urllib.parse.urlsplit(result['icon'])
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.fragment):
            raise ValueError('notifications.icon must be an HTTPS URL without user info or fragment')
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(name):
    endpoint = credentials._resolve_reference(name)
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Bark endpoint must be an HTTPS URL without user info or fragment')
    return endpoint


def _send_one(credential, payload):
    """Send to one private endpoint and return value-free delivery metadata."""
    try:
        request = urllib.request.Request(_endpoint(credential), data=payload,
                                         headers={'Content-Type': 'application/json; charset=utf-8'},
                                         method='POST')
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=6) as response:
            raw = response.read(32768)
            if response.status != 200:
                raise ValueError('Bark returned HTTP ' + str(response.status))
            try:
                reply = json.loads(raw) if raw else {}
            except ValueError:
                reply = {}
            if isinstance(reply, dict) and reply.get('code') not in (None, 200):
                raise ValueError('Bark rejected the notification')
        return {'credential': credential, 'sent': True, 'at': time.time()}
    except (credentials.CredentialError, ValueError, urllib.error.URLError, TimeoutError, OSError) as error:
        # Never persist the endpoint, response body, headers, or OS error text.
        return {'credential': credential, 'sent': False, 'reason': type(error).__name__, 'at': time.time()}


def send(title, body, *, level='active', config=None):
    """Fan one bounded Bark message out to every configured client."""
    settings = normalize(config if config is not None else common.config().get('notifications'))
    if not settings['enabled']:
        return {'sent': False, 'reason': 'disabled', 'sent_count': 0, 'failed_count': 0}
    message = {'title': str(title)[:120], 'body': str(body)[:1000],
               'group': settings['group'], 'level': level}
    if settings['icon']:
        message['icon'] = settings['icon']
    payload = json.dumps(message, ensure_ascii=False).encode()
    names = settings['credentials']
    # One unreachable phone must not make every other delivery wait for its
    # network timeout. map preserves configured order in the safe result.
    with ThreadPoolExecutor(max_workers=len(names)) as executor:
        deliveries = list(executor.map(lambda name: _send_one(name, payload), names))
    sent_count = sum(item['sent'] is True for item in deliveries)
    return {'sent': sent_count > 0, 'sent_count': sent_count,
            'failed_count': len(deliveries) - sent_count, 'deliveries': deliveries}


def _state():
    value = common.read_json(common.STATE / STATE_FILE, {})
    return value if isinstance(value, dict) else {}


def _save(value):
    common.write_json(common.STATE / STATE_FILE, value)


def _threshold_alert(settings, key, severity, title, body):
    """Persist independent threshold edges; invalid telemetry never calls this."""
    with common.locked('notifications'):
        state = _state()
        alert = state.get(key) or {}
        sent_level = int(alert.get('severity') or 0)
        if severity < sent_level:
            alert = {'severity': severity}
            state[key] = alert
            _save(state)
        if severity <= int(alert.get('severity') or 0):
            return []
        if alert.get('attempt_severity') == severity and time.time() - alert.get('attempted_at', 0) < 900:
            return []
        alert.update(attempted_at=time.time(), attempt_severity=severity)
        state[key] = alert
        _save(state)
    result = send(title, body, level='timeSensitive', config=settings)
    with common.locked('notifications'):
        state = _state()
        latest = state.get(key) or {}
        if latest.get('attempted_at') == alert.get('attempted_at') and result.get('sent'):
            latest['severity'] = severity
            state[key] = latest
            _save(state)
    return [result]


def _observe_balance_and_windows(settings, snapshot):
    events = []
    record = (snapshot or {}).get('deepseek') or {}
    if record.get('state') == 'ok' and record.get('stale', True) is False:
        amounts = [quota.number(row.get('remaining')) for row in record.get('balances') or []
                   if isinstance(row, dict) and row.get('currency') == 'CNY']
        if amounts and all(v is not None and v >= 0 for v in amounts):
            balance = sum(amounts)
            severity = 3 if balance == 0 else 2 if balance < 30 else 1 if balance < 40 else 0
            label = '余额耗尽' if severity == 3 else '低于 ¥30' if severity == 2 else '低于 ¥40'
            events += _threshold_alert(settings, 'deepseek_balance_threshold', severity,
                'Worker Desk · DeepSeek ' + label,
                'DeepSeek 当前余额 ¥%.2f，请及时充值以保持兜底任务可用。' % balance)
    return events


def _observe_long_conservation(settings, config, snapshot):
    """Notify one durable shortage episode after 30 minutes of confirmation."""
    import economics
    guidance = quota.tier_guidance(config, snapshot)
    events = []
    now = time.time()
    for provider in guidance.get('provider_conservation_level', {}):
        record = snapshot.get(provider) or {}
        if record.get('state') != 'ok' or record.get('stale', True):
            continue
        window = economics._selected_window(provider, record) or {}
        if (quota.number(window.get('duration_minutes')) or 0) <= 1440:
            continue
        fit = economics._window_fit(provider, record, now)
        runway = quota.number(fit.get('pressure_runway'))
        if fit.get('status') != 'ok' or fit.get('stale') or runway is None:
            continue
        # Derive from the durable window, never the short-window availability flag.
        level = quota._provider_conservation_level(guidance, True, runway)
        reset = quota.routing._iso_seconds(window.get('resets_at'))
        if reset and reset - now <= 3600:
            continue  # imminent refill is not an actionable long-term warning
        key = 'long_conservation:' + provider
        with common.locked('notifications'):
            state = _state()
            episode = state.get(key) or {}
            marker = 'low_since' if level else 'healthy_since'
            episode.pop('healthy_since' if level else 'low_since', None)
            episode.setdefault(marker, now)
            confirmed = now - episode[marker] >= 1800
            state[key] = episode
            _save(state)
        if not confirmed:
            continue
        name = {'kimi-for-coding': 'Kimi', 'volcengine-agent-plan': '方舟 Agent Plan'}.get(provider, provider)
        note = ''
        if reset:
            from datetime import datetime, timezone, timedelta
            note = ' 下次额度刷新：北京时间 ' + datetime.fromtimestamp(
                reset, timezone(timedelta(hours=8))).strftime('%m/%d %H:%M') + '。'
        events += _threshold_alert(settings, key + ':notice', int(level > 0),
            'Worker Desk · ' + name + ' 持续降载',
            name + ' 的长期套餐额度持续紧张，已减少该套餐的任务分配，服务继续通过其他可用路由运行。' + note)
    return events


def observe_quota(config, snapshot):
    """Cash needs action; transparent provider failovers do not need a push."""
    settings = normalize(config.get('notifications'))
    if not settings['enabled'] or not settings['quota_transitions']:
        return []
    events = _observe_balance_and_windows(settings, snapshot)
    events += _observe_long_conservation(settings, config, snapshot)
    # Probe the same admission path as dispatch, without advancing routing counters.
    # Do not infer an outage from a single empty window or conservation level.
    if not config.get('profiles'):
        return events
    blocked = []
    for tier in ('fast', 'normal', 'deep'):
        profile, _ = quota.route({'requested_profile': 'auto', 'tier': tier,
            'complexity': 'deep' if tier == 'deep' else 'normal'}, config, snapshot)
        if not profile:
            blocked.append(tier)
    events += _threshold_alert(settings, 'service_route_unavailable', int(bool(blocked)),
        'Worker Desk · 任务路由暂不可用',
        '以下任务档位暂无可用模型：' + '、'.join(blocked) +
        '。请检查套餐额度或充值备用余额；已有任务状态请在控制台查看。')
    return events


def model_switch(task, previous_profile, next_profile, trigger):
    settings = normalize(common.config().get('notifications'))
    if not settings['enabled'] or not settings['model_switches']:
        return {'sent': False, 'reason': 'disabled'}
    reason = '额度或限流终止' if 'usage' in trigger or 'billing' in trigger or 'rate_limit' in trigger else '长任务额度保护'
    return send('Worker Desk · 任务已换模型',
                str(task.get('title') or task.get('id'))[:120] + '\n' +
                str(previous_profile) + ' → ' + str(next_profile) + ' · ' + reason,
                level='active', config=settings)


def task_completed(task):
    """Send the one explicit completion alert requested on a key task."""
    settings = normalize(common.config().get('notifications'))
    if not settings['enabled'] or task.get('notify_on_complete') is not True:
        return {'sent': False, 'reason': 'disabled'}
    return send('Worker Desk · 关键任务已完成',
                str(task.get('title') or task.get('id'))[:120],
                level='timeSensitive', config=settings)
