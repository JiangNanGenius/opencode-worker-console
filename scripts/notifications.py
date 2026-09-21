"""Best-effort operator notifications without serializing notification secrets."""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

import common
import credentials
import quota


STATE_FILE = 'notifications.json'
DEFAULTS = {
    'enabled': False,
    'credential': 'bark-endpoint',
    'group': 'Worker Desk',
    'quota_transitions': True,
    'model_switches': False,
}


def normalize(value):
    raw = value if isinstance(value, dict) else {}
    result = dict(DEFAULTS)
    for key in ('enabled', 'quota_transitions', 'model_switches'):
        if key in raw:
            result[key] = raw[key] is True
    credential = raw.get('credential')
    if isinstance(credential, str) and credential.strip():
        result['credential'] = credential.strip()
    group = raw.get('group')
    if isinstance(group, str) and group.strip():
        result['group'] = group.strip()[:64]
    return result


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('notifications must be an object')
    for key in ('enabled', 'quota_transitions', 'model_switches'):
        if key in value and not isinstance(value[key], bool):
            raise ValueError('notifications.' + key + ' must be boolean')
    result = normalize(value)
    if len(result['credential']) > 64:
        raise ValueError('notifications.credential is too long')
    if not result['group']:
        raise ValueError('notifications.group cannot be empty')
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


def send(title, body, *, level='active', config=None):
    """Send one bounded Bark notification and return only safe delivery metadata."""
    settings = normalize(config if config is not None else common.config().get('notifications'))
    if not settings['enabled']:
        return {'sent': False, 'reason': 'disabled'}
    payload = json.dumps({'title': str(title)[:120], 'body': str(body)[:1000],
                          'group': settings['group'], 'level': level}, ensure_ascii=False).encode()
    try:
        request = urllib.request.Request(_endpoint(settings['credential']), data=payload,
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
        return {'sent': True, 'at': time.time()}
    except (credentials.CredentialError, ValueError, urllib.error.URLError, TimeoutError, OSError) as error:
        # Never persist the endpoint, response body, headers, or OS error text.
        return {'sent': False, 'reason': type(error).__name__, 'at': time.time()}


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
