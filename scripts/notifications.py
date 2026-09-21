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


def observe_quota(config, snapshot):
    """Notify only on meaningful state transitions; the first sample seeds state."""
    settings = normalize(config.get('notifications'))
    if not settings['enabled'] or not settings['quota_transitions']:
        return []
    guidance = quota.tier_guidance(config, snapshot)
    level = int(guidance.get('conservation_level') or 0)
    providers = {}
    for name, value in (snapshot or {}).items():
        if not isinstance(value, dict):
            continue
        available = value.get('available')
        providers[name] = available if isinstance(available, bool) else None
    with common.locked('notifications'):
        state = _state()
        previous_level = state.get('conservation_level')
        previous_providers = state.get('providers') if isinstance(state.get('providers'), dict) else {}
        state.update(conservation_level=level, providers=providers, sampled_at=time.time())
        _save(state)
    if previous_level is None:
        return []
    events = []
    if level != previous_level:
        remaining = guidance.get('combined_remaining_percent')
        suffix = ('，综合续航约 ' + str(round(float(remaining))) + '%') if isinstance(remaining, (int, float)) and math.isfinite(remaining) else ''
        if level > previous_level:
            title = 'Worker Desk · ' + ('二级降载' if level == 2 else '一级降载')
            body = '套餐消耗进入保护区' + suffix + '。新任务与符合条件的长任务将按当前策略分流。'
            alert = 'timeSensitive'
        else:
            title = 'Worker Desk · 额度恢复'
            body = ('已退出降载' if level == 0 else '已回到一级降载') + suffix + '。后续任务恢复使用当前高质量路由。'
            alert = 'active'
        events.append(send(title, body, level=alert, config=settings))
    for provider, available in providers.items():
        old = previous_providers.get(provider)
        if old is True and available is False:
            events.append(send('Worker Desk · 额度终止', provider + ' 暂不可用，自动任务会选择同档或后续路由。',
                               level='timeSensitive', config=settings))
        elif old is False and available is True:
            events.append(send('Worker Desk · 额度恢复', provider + ' 已恢复可用。', config=settings))
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
