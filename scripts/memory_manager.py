"""Manage opencode-mem through its loopback API without exposing its database or secrets."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.parse

import common

DEFAULTS = {
    'enabled': False,
    'backend': 'opencode-mem',
    'storage_path': None,
    'api_url': 'http://127.0.0.1:4747',
    'embedding_api_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
    'embedding_model': 'doubao-embedding-vision',
    'embedding_dimensions': 2048,
    'capture_profile': 'volcengine-agent-plan/doubao-seed-evolving',
    'reserve': {'minimum_percent': 0.25, 'maximum_percent': 3.0, 'headroom_multiplier': 1.5},
    'openviking': {'url': 'http://127.0.0.1:1933', 'vlm_model': 'deepseek-flash',
                   'vlm_api_base': 'https://api.deepseek.com'},
    # Capacity-based rolling retention keeps durable memory independent from
    # session age. Old unpinned records leave only after the configured ceiling
    # is crossed; a lower target avoids deleting one record on every capture.
    'rolling': {'enabled': True, 'max_storage_mb': 1024, 'max_memories': 100000,
                'target_percent': 90, 'check_interval_hours': 24},
}


def backend(value=None):
    if isinstance(value, dict):
        raw = value
    else:
        try:
            raw = common.config().get('memory')
        except RuntimeError:
            raw = None
    raw = raw if isinstance(raw, dict) else {}
    return 'openviking' if raw.get('backend') == 'openviking' else 'opencode-mem'


def init_backend():
    """Ensure backend state directories exist for whichever backend is active."""
    common.init()


def settings(value=None):
    raw = value if isinstance(value, dict) else {}
    out = dict(DEFAULTS)
    out.update({k: raw[k] for k in ('enabled', 'backend', 'storage_path', 'api_url', 'embedding_api_url',
                                    'embedding_model', 'embedding_dimensions', 'capture_profile')
                if k in raw})
    if not out.get('storage_path'):
        out['storage_path'] = str(common.STATE / 'opencode-mem')
    reserve = dict(DEFAULTS['reserve'])
    if isinstance(raw.get('reserve'), dict): reserve.update(raw['reserve'])
    out['reserve'] = reserve
    openviking = dict(DEFAULTS['openviking'])
    if isinstance(raw.get('openviking'), dict): openviking.update(raw['openviking'])
    out['openviking'] = openviking
    rolling = dict(DEFAULTS['rolling'])
    if isinstance(raw.get('rolling'), dict): rolling.update(raw['rolling'])
    out['rolling'] = rolling
    return out


def validate(value):
    if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
        raise ValueError('memory.enabled must be boolean')
    result = settings(value)
    if result.get('backend') not in ('opencode-mem', 'openviking'):
        raise ValueError('memory.backend must be opencode-mem or openviking')
    if not isinstance(result['storage_path'], str) or not result['storage_path'].strip():
        raise ValueError('memory.storage_path must be a non-empty absolute path')
    storage = Path(result['storage_path']).expanduser()
    if not storage.is_absolute():
        raise ValueError('memory.storage_path must be an absolute path')
    result['storage_path'] = str(storage)
    openviking = result['openviking']
    for key in ('url', 'vlm_model', 'vlm_api_base'):
        if not isinstance(openviking.get(key), str) or not openviking[key].strip():
            raise ValueError('memory.openviking.' + key + ' must be non-empty text')
    parsed = urllib.parse.urlsplit(openviking['url'])
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('memory.openviking.url must remain on loopback HTTP')
    if urllib.parse.urlsplit(openviking['vlm_api_base']).scheme not in ('http', 'https'):
        raise ValueError('memory.openviking.vlm_api_base must be an HTTP URL')
    for key in ('api_url', 'embedding_api_url', 'embedding_model', 'capture_profile'):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError('memory.' + key + ' must be non-empty text')
    for key in ('api_url', 'embedding_api_url'):
        parsed = urllib.parse.urlsplit(result[key])
        if parsed.scheme not in ('http', 'https'):
            raise ValueError('memory.' + key + ' must be an HTTP URL')
    parsed = urllib.parse.urlsplit(result['api_url'])
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('memory.api_url must remain on loopback HTTP')
    if isinstance(result['embedding_dimensions'], bool) or not isinstance(result['embedding_dimensions'], int) \
            or not 1 <= result['embedding_dimensions'] <= 65536:
        raise ValueError('memory.embedding_dimensions must be an integer between 1 and 65536')
    reserve = result['reserve']
    for key in ('minimum_percent', 'maximum_percent', 'headroom_multiplier'):
        number = reserve.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number < 0:
            raise ValueError('memory.reserve.' + key + ' must be a non-negative number')
    if reserve['maximum_percent'] > 10 or reserve['minimum_percent'] > reserve['maximum_percent']:
        raise ValueError('memory reserve must be ordered and at most 10 percent')
    rolling = result['rolling']
    if not isinstance(rolling.get('enabled'), bool):
        raise ValueError('memory.rolling.enabled must be boolean')
    for key, low, high in (('max_storage_mb', 64, 10240),
                           ('max_memories', 100, 100000),
                           ('target_percent', 50, 99),
                           ('check_interval_hours', 1, 168)):
        number = rolling.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError('memory.rolling.' + key + ' must be an integer between ' +
                             str(low) + ' and ' + str(high))
    return result


def _secret_path():
    return common.STATE / 'credentials/opencode-mem-ark-key'


def _plugin_dir():
    cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache')))
    return cache / 'opencode/packages/opencode-mem@latest'


def _api_token_path():
    # opencode-mem requires this local anti-CSRF token even on loopback. It owns
    # the filename; creating it before startup lets the bridge and plugin share
    # one owner-only token without exposing it through config or the console.
    return Path.home() / '.opencode-mem/.auth-token'


def _api_token(create=False):
    path = _api_token_path()
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(secrets.token_hex(32))
    if not path.exists():
        return None
    token = path.read_text().strip()
    if not token:
        return None
    path.chmod(0o600)
    return token


def install_plugin():
    """Provision the published package in OpenCode's own on-demand cache.

    OpenCode normally performs this install itself. Explicit provisioning makes a
    headless server restart deterministic and avoids leaving an empty cache directory
    when its background package fetch is interrupted.
    """
    root = _plugin_dir()
    package = root / 'node_modules/opencode-mem/package.json'
    try:
        current = json.loads(package.read_text()).get('version')
        if isinstance(current, str) and current:
            return {'installed': True, 'version': current, 'path': str(root)}
    except (OSError, ValueError):
        pass
    npm = shutil.which('npm')
    if not npm:
        # OpenCode can provision named plugins through its own package runtime on
        # startup. Keep this fallback for standalone OpenCode installs that do
        # not also expose npm on PATH; service verification will surface a real
        # plugin-load failure without making Node a bridge prerequisite.
        return {'installed': False, 'version': None, 'path': str(root),
                'managed_by': 'opencode'}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ, NPM_CONFIG_CACHE=str(common.STATE / 'runtime/npm-cache'))
    subprocess.run([npm, 'install', '--prefix', str(root), '--no-audit', '--no-fund',
                    '--ignore-scripts', '--registry=https://registry.npmjs.org',
                    'opencode-mem@latest'], check=True, stdout=subprocess.DEVNULL,
                   timeout=600, env=env)
    try:
        version = json.loads(package.read_text()).get('version')
    except (OSError, ValueError):
        version = None
    if not isinstance(version, str) or not version:
        raise RuntimeError('opencode-mem installation did not produce a package')
    return {'installed': True, 'version': version, 'path': str(root),
            'managed_by': 'bridge'}


def configure():
    """Write owner-only plugin config; provider key is copied without being returned."""
    c = common.config()
    cfg = settings(c.get('memory'))
    if not cfg['enabled']:
        return {'enabled': False, 'configured': False}
    if cfg.get('backend') == 'openviking':
        import openviking_service
        result = openviking_service.write_runtime_config()
        return {'enabled': True, 'configured': True, 'backend': 'openviking',
                'config': result['config'], 'cli_config': result['cli_config'],
                'runtime': str(openviking_service.venv_bin('openviking-server')),
                'embedding_model': cfg['embedding_model'], 'dimensions': cfg['embedding_dimensions'],
                'plugin': '@openviking/opencode-plugin'}
    plugin = install_plugin()
    _api_token(create=True)
    key = common.auth_key('volcengine-agent-plan')
    secret = _secret_path()
    secret.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret.write_text(key)
    secret.chmod(0o600)
    provider, model = cfg['capture_profile'].split('/', 1)
    payload = {
        'storagePath': cfg['storage_path'],
        'embeddingApiUrl': cfg['embedding_api_url'].rstrip('/'),
        'embeddingApiKey': 'file://' + str(secret),
        'embeddingModel': cfg['embedding_model'],
        'embeddingDimensions': cfg['embedding_dimensions'],
        'webServerEnabled': True,
        'webServerHost': '127.0.0.1',
        'webServerPort': urllib.parse.urlsplit(cfg['api_url']).port or 4747,
        'autoCaptureEnabled': True,
        'autoCaptureLanguage': 'auto',
        'opencodeProvider': provider,
        'opencodeModel': model,
        'showAutoCaptureToasts': False,
        'showUserProfileToasts': False,
        'showErrorToasts': True,
        # Worker Desk owns retention. Age alone must not delete project memory
        # after a laptop has been powered off for a month.
        'autoCleanupEnabled': False,
        'deduplicationEnabled': True,
        'memory': {'defaultScope': 'project'},
        'chatMessage': {'enabled': True, 'maxMemories': 3,
                        'excludeCurrentSession': True, 'injectOn': 'first'},
        'compaction': {'enabled': True, 'memoryLimit': 10},
    }
    target = Path.home() / '.config/opencode/opencode-mem.jsonc'
    common.write_json(target, payload)
    target.chmod(0o600)
    return {'enabled': True, 'configured': True, 'config': str(target),
            'embedding_model': cfg['embedding_model'], 'dimensions': cfg['embedding_dimensions'],
            'plugin_version': plugin['version']}


def _backend_module():
    import openviking_backend
    return openviking_backend


def legacy_request(path, method='GET', data=None, timeout=30):
    """Unwrapped opencode-mem loopback request (kept for migration and rollback)."""
    cfg = settings(common.config().get('memory'))
    token = _api_token()
    if not token:
        raise ValueError('OpenCode memory service token is unavailable')
    result = common.request(cfg['api_url'].rstrip('/') + path, method, data,
                            headers={'X-Opencode-Mem-Token': token}, timeout=timeout)
    if not isinstance(result, dict) or result.get('success') is not True:
        raise ValueError((result or {}).get('error', 'OpenCode memory service unavailable'))
    return result.get('data', result)


_request = legacy_request


def status():
    cfg = settings(common.config().get('memory'))
    if cfg['enabled'] and backend() == 'openviking':
        return _backend_module().status()
    result = {'enabled': cfg['enabled'], 'backend': 'opencode-mem', 'url': cfg['api_url'],
              'storage_path': cfg['storage_path'],
              'embedding_model': cfg['embedding_model'], 'reserve': cfg['reserve'],
              'rolling': {**cfg['rolling'],
                          'last': common.read_json(common.STATE / 'memory-maintenance.json', {})}}
    if not cfg['enabled']: return result
    try:
        health = common.request(cfg['api_url'].rstrip('/') + '/api/health', timeout=3)
        result['healthy'] = health.get('success') is True
        if result['healthy']:
            result['stats'] = _request('/api/stats', timeout=10)
    except Exception as error:
        result.update(healthy=False, error=common.redact(str(error)))
    return result


def _git(args, directory):
    try:
        return subprocess.run(['git', *args], cwd=str(directory), check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5).stdout.strip() or None
    except Exception:
        return None


def project_info(directory):
    path = Path(directory).expanduser().resolve()
    if not path.is_dir(): raise ValueError('Memory project directory does not exist')
    marker = next((parent for parent in (path, *path.parents)
                   if (parent / '.opencode-mem-project').exists()), None)
    if marker:
        root = marker.resolve(); identity = 'path:' + str(root); remote = None
    else:
        common_dir = _git(['rev-parse', '--git-common-dir'], path)
        if common_dir:
            common_path = Path(common_dir)
            if not common_path.is_absolute(): common_path = path / common_path
            common_path = common_path.resolve()
            identity = 'git-common:' + str(common_path)
            root = common_path.parent if common_path.name == '.git' else Path(
                _git(['rev-parse', '--show-toplevel'], path) or path).resolve()
        else:
            root = path; remote = _git(['config', '--get', 'remote.origin.url'], path)
            identity = 'remote:' + remote if remote else 'path:' + os.path.normpath(str(path))
        remote = _git(['config', '--get', 'remote.origin.url'], path)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return {'tag': 'opencode_project_' + digest, 'projectPath': str(root),
            'projectName': root.name, **({'gitRepoUrl': remote} if remote else {})}


def known_directories():
    """Candidate project directories from saved workspaces and task records."""
    candidates = []
    for item in common.read_json(common.STATE / 'workspaces.json', []) or []:
        if isinstance(item, dict): candidates.append(item.get('directory'))
    for task in common.tasks():
        if isinstance(task, dict): candidates.append(task.get('source_dir') or task.get('directory'))
    seen, out = set(), []
    for directory in candidates:
        if not isinstance(directory, str) or directory in seen or not Path(directory).is_dir():
            continue
        seen.add(directory)
        out.append(directory)
    return out


def projects():
    """Memory projects plus known bridge workspaces, including a fresh install.

    opencode-mem exposes only tags that already contain a memory. The bridge also
    contributes saved workspaces and task directories so the first memory can be
    created from the console without a CLI bootstrap step.
    """
    if backend() == 'openviking':
        return _backend_module().projects()
    remote = _request('/api/tags').get('project', [])
    rows = [dict(item) for item in remote if isinstance(item, dict)]
    known_paths = {item.get('projectPath') for item in rows if item.get('projectPath')}
    for directory in known_directories():
        if directory in known_paths:
            continue
        info = project_info(directory)
        if info['projectPath'] in known_paths:
            continue
        rows.append(info)
        known_paths.add(info['projectPath'])
    return sorted(rows, key=lambda item: str(item.get('projectName') or item.get('projectPath') or '').lower())


def list_memories(directory=None, page=1, page_size=50, include_prompts=False, tag=None):
    if backend() == 'openviking':
        return _backend_module().list_memories(directory, page, page_size, tag=tag,
                                               include_prompts=include_prompts)
    tag_value = project_info(directory)['tag'] if directory else None
    query = {'page': max(1, int(page)), 'pageSize': min(200, max(1, int(page_size))),
             'includePrompts': 'true' if include_prompts else 'false'}
    if tag_value: query['tag'] = tag_value
    return _request('/api/memories?' + urllib.parse.urlencode(query))


def browse(category=None, folder=None, project=None, directory=None, page=1, page_size=25,
           sort='newest', query=None):
    """Paginated memory browser; native OpenViking navigation with a legacy fallback."""
    if backend() == 'openviking':
        return _backend_module().browse(category, folder, project, directory, page, page_size,
                                        sort, query=query)
    if query:
        result = search(query, directory, 1, page_size, tag=project)
    else:
        result = list_memories(directory, page, page_size, tag=project)
    items = []
    for item in result.get('items') or []:
        preview = ' '.join(str(item.get('content') or '').split())[:360]
        items.append({'id': item.get('id'), 'uri': item.get('id'), 'type': 'memory',
                      'memoryType': item.get('memoryType') or '', 'category': '',
                      'folder': '', 'preview': preview,
                      'isPinned': bool(item.get('isPinned')), 'tags': item.get('tags') or [],
                      'projectName': item.get('projectName') or item.get('displayName'),
                      'projectPath': item.get('projectPath'),
                      'updatedAt': item.get('updatedAt'), 'createdAt': item.get('createdAt'),
                      'size': None})
    return {'items': items, 'total': int(result.get('total') or len(items)),
            'page': max(1, int(result.get('page') or page)),
            'pageSize': max(1, int(result.get('pageSize') or page_size)),
            'totalPages': max(1, int(result.get('totalPages') or 1)),
            'scope': result.get('scope'), 'category': category or '', 'folder': folder or '',
            'sort': sort, 'categories': [], 'directories': [], 'projects': [],
            'legacy': True, 'generatedAt': time.time()}


def detail(memory_id, scope=None):
    if backend() != 'openviking':
        raise ValueError('Memory detail requires the OpenViking backend')
    return _backend_module().detail(memory_id, scope=scope)


def activity():
    if backend() != 'openviking':
        raise ValueError('Memory activity requires the OpenViking backend')
    return _backend_module().activity()


def capacity():
    if backend() != 'openviking':
        raise ValueError('Memory capacity requires the OpenViking backend')
    return _backend_module().capacity()


def search(query, directory=None, page=1, page_size=20, tag=None):
    if backend() == 'openviking':
        return _backend_module().search(query, directory, page, page_size, tag=tag)
    if not isinstance(query, str) or not query.strip(): raise ValueError('Memory search query is required')
    params = {'q': query.strip(), 'page': max(1, int(page)),
              'pageSize': min(100, max(1, int(page_size)))}
    if directory: params['tag'] = project_info(directory)['tag']
    return _request('/api/search?' + urllib.parse.urlencode(params), timeout=60)


def add(content, directory, tags=None, memory_type=None):
    if backend() == 'openviking':
        return _backend_module().add(content, directory, tags, memory_type)
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise ValueError('Memory content must contain 1-100000 characters')
    info = project_info(directory)
    body = {'content': content.strip(), 'containerTag': info.pop('tag'), **info,
            'tags': [str(x).strip() for x in (tags or []) if str(x).strip()]}
    if memory_type: body['type'] = memory_type
    return _request('/api/memories', 'POST', body, timeout=60)


def update(memory_id, content):
    if backend() == 'openviking':
        return _backend_module().update(memory_id, content)
    _id(memory_id)
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise ValueError('Memory content must contain 1-100000 characters')
    return _request('/api/memories/' + memory_id, 'PUT', {'content': content.strip()}, timeout=60)


def delete(memory_id):
    if backend() == 'openviking':
        return _backend_module().delete(memory_id)
    _id(memory_id)
    return _request('/api/memories/' + memory_id + '?cascade=true', 'DELETE')


def bulk_delete(ids):
    if backend() == 'openviking':
        return _backend_module().bulk_delete(ids)
    clean = [_id(value) for value in ids]
    return _request('/api/memories/bulk-delete', 'POST', {'ids': clean, 'cascade': True})


def pin(memory_id, pinned=True):
    if backend() != 'openviking':
        raise ValueError('Pinning requires the OpenViking memory backend')
    return _backend_module().pin(memory_id, pinned)


def _memory_epoch(item):
    value = item.get('updatedAt') or item.get('createdAt') or ''
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return 0.0


def _estimated_memory_bytes(item, dimensions):
    # Logical capacity is stable across SQLite page allocation and external
    # volume availability: UTF-8 content + float32 vector + bounded row/index
    # overhead. Physical database pages can be compacted independently.
    content = len(str(item.get('content') or '').encode('utf-8'))
    return content + int(dimensions) * 4 + 4096


def _rolling_candidates(items, maximum_bytes, maximum_records, target_percent, dimensions=2048):
    """Return unpinned IDs only after capacity is crossed: duplicates, then oldest."""
    rows = [item for item in items if isinstance(item, dict) and item.get('type') != 'prompt' and
            isinstance(item.get('id'), str)]
    initial_bytes = sum(_estimated_memory_bytes(item, dimensions) for item in rows)
    if initial_bytes <= maximum_bytes and len(rows) <= maximum_records:
        return []
    newest = sorted(rows, key=_memory_epoch, reverse=True)
    seen = set()
    remove = []
    for item in newest:
        # Pinned copies remain independent durable facts and are never reclaimed.
        key = (item.get('projectPath') or item.get('displayName') or '', item.get('content') or '')
        if key in seen and not item.get('isPinned'):
            remove.append(item['id'])
        else:
            seen.add(key)
    removed = set(remove)
    remaining_rows = [item for item in rows if item['id'] not in removed]
    remaining_bytes = sum(_estimated_memory_bytes(item, dimensions) for item in remaining_rows)
    remaining_count = len(remaining_rows)
    target_bytes = max(1, int(maximum_bytes * target_percent / 100))
    target_records = max(1, int(maximum_records * target_percent / 100))
    oldest = sorted((item for item in remaining_rows if not item.get('isPinned')),
                    key=_memory_epoch)
    for item in oldest:
        if remaining_bytes <= target_bytes and remaining_count <= target_records:
            break
        remove.append(item['id'])
        remaining_bytes -= _estimated_memory_bytes(item, dimensions)
        remaining_count -= 1
    return remove


def maintain(force=False):
    """Apply capacity-based rolling retention without coupling memory to sessions."""
    if backend() == 'openviking':
        return _backend_module().maintain(force=force)
    cfg = settings(common.config().get('memory'))
    policy = cfg['rolling']
    state_path = common.STATE / 'memory-maintenance.json'
    if not cfg['enabled'] or not policy['enabled']:
        return {'enabled': False, 'reason': 'disabled'}
    now = time.time()
    with common.locked('memory-maintenance'):
        previous = common.read_json(state_path, {})
        if not force and now - float(previous.get('checked_at') or 0) < \
                policy['check_interval_hours'] * 3600:
            return {**previous, 'due': False}
        try:
            first = list_memories(page=1, page_size=200)
            items = list(first.get('items') or [])
            pages = min(1000, max(1, int(first.get('totalPages') or 1)))
            for page in range(2, pages + 1):
                items.extend((list_memories(page=page, page_size=200).get('items') or []))
            before_bytes = sum(_estimated_memory_bytes(item, cfg['embedding_dimensions'])
                               for item in items if isinstance(item, dict) and
                               item.get('type') != 'prompt')
            capacity_bytes = policy['max_storage_mb'] * 1024 * 1024
            ids = _rolling_candidates(items, capacity_bytes, policy['max_memories'],
                                      policy['target_percent'], cfg['embedding_dimensions'])
            deleted = 0
            for start in range(0, len(ids), 100):
                bulk_delete(ids[start:start + 100])
                deleted += len(ids[start:start + 100])
            result = {'enabled': True, 'checked_at': now, 'before': len(items),
                      'deleted': deleted, 'after': len(items) - deleted,
                      'estimated_bytes_before': before_bytes,
                      'estimated_bytes_after': max(0, before_bytes - sum(
                          _estimated_memory_bytes(item, cfg['embedding_dimensions'])
                          for item in items if isinstance(item, dict) and item.get('id') in set(ids))),
                      'capacity_bytes': capacity_bytes,
                      'capacity_mb': policy['max_storage_mb'],
                      'record_safety_limit': policy['max_memories'],
                      'target_percent': policy['target_percent'], 'due': True}
        except Exception as error:
            result = {'enabled': True, 'checked_at': now, 'deleted': 0,
                      'error': common.redact(str(error)), 'due': True}
        common.write_json(state_path, result)
        return result


def profile():
    if backend() == 'openviking':
        return _backend_module().profile()
    return _request('/api/user-profile')


def stats():
    if backend() == 'openviking':
        return _backend_module().stats()
    return _request('/api/stats')


def _id(value):
    if not isinstance(value, str) or not value.startswith('mem_') or len(value) > 100:
        raise ValueError('Invalid memory ID')
    return value
