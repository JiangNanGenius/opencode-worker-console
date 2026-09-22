"""Manage opencode-mem through its loopback API without exposing its database or secrets."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import urllib.parse

import common

DEFAULTS = {
    'enabled': False,
    'api_url': 'http://127.0.0.1:4747',
    'embedding_api_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
    'embedding_model': 'doubao-embedding-vision',
    'embedding_dimensions': 2048,
    'capture_profile': 'volcengine-agent-plan/doubao-seed-evolving',
    'reserve': {'minimum_percent': 0.25, 'maximum_percent': 3.0, 'headroom_multiplier': 1.5},
}


def settings(value=None):
    raw = value if isinstance(value, dict) else {}
    out = dict(DEFAULTS)
    out.update({k: raw[k] for k in ('enabled', 'api_url', 'embedding_api_url',
                                    'embedding_model', 'embedding_dimensions', 'capture_profile')
                if k in raw})
    reserve = dict(DEFAULTS['reserve'])
    if isinstance(raw.get('reserve'), dict): reserve.update(raw['reserve'])
    out['reserve'] = reserve
    return out


def validate(value):
    if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
        raise ValueError('memory.enabled must be boolean')
    result = settings(value)
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
    plugin = install_plugin()
    _api_token(create=True)
    key = common.auth_key('volcengine-agent-plan')
    secret = _secret_path()
    secret.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret.write_text(key)
    secret.chmod(0o600)
    provider, model = cfg['capture_profile'].split('/', 1)
    payload = {
        'storagePath': str(common.STATE / 'opencode-mem'),
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


def _request(path, method='GET', data=None, timeout=30):
    cfg = settings(common.config().get('memory'))
    if not cfg['enabled']:
        raise ValueError('Project memory is disabled')
    token = _api_token()
    if not token:
        raise ValueError('OpenCode memory service token is unavailable')
    result = common.request(cfg['api_url'].rstrip('/') + path, method, data,
                            headers={'X-Opencode-Mem-Token': token}, timeout=timeout)
    if not isinstance(result, dict) or result.get('success') is not True:
        raise ValueError((result or {}).get('error', 'OpenCode memory service unavailable'))
    return result.get('data', result)


def status():
    cfg = settings(common.config().get('memory'))
    result = {'enabled': cfg['enabled'], 'url': cfg['api_url'],
              'embedding_model': cfg['embedding_model'], 'reserve': cfg['reserve']}
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


def projects():
    """Memory projects plus known bridge workspaces, including a fresh install.

    opencode-mem exposes only tags that already contain a memory. The bridge also
    contributes saved workspaces and task directories so the first memory can be
    created from the console without a CLI bootstrap step.
    """
    remote = _request('/api/tags').get('project', [])
    rows = [dict(item) for item in remote if isinstance(item, dict)]
    known_paths = {item.get('projectPath') for item in rows if item.get('projectPath')}
    candidates = []
    for item in common.read_json(common.STATE / 'workspaces.json', []) or []:
        if isinstance(item, dict): candidates.append(item.get('directory'))
    for task in common.tasks():
        if isinstance(task, dict): candidates.append(task.get('source_dir') or task.get('directory'))
    for directory in candidates:
        if not isinstance(directory, str) or directory in known_paths or not Path(directory).is_dir():
            continue
        info = project_info(directory)
        if info['projectPath'] in known_paths:
            continue
        rows.append(info)
        known_paths.add(info['projectPath'])
    return sorted(rows, key=lambda item: str(item.get('projectName') or item.get('projectPath') or '').lower())


def list_memories(directory=None, page=1, page_size=50, include_prompts=False):
    tag = project_info(directory)['tag'] if directory else None
    query = {'page': max(1, int(page)), 'pageSize': min(200, max(1, int(page_size))),
             'includePrompts': 'true' if include_prompts else 'false'}
    if tag: query['tag'] = tag
    return _request('/api/memories?' + urllib.parse.urlencode(query))


def search(query, directory=None, page=1, page_size=20):
    if not isinstance(query, str) or not query.strip(): raise ValueError('Memory search query is required')
    params = {'q': query.strip(), 'page': max(1, int(page)),
              'pageSize': min(100, max(1, int(page_size)))}
    if directory: params['tag'] = project_info(directory)['tag']
    return _request('/api/search?' + urllib.parse.urlencode(params), timeout=60)


def add(content, directory, tags=None, memory_type=None):
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise ValueError('Memory content must contain 1-100000 characters')
    info = project_info(directory)
    body = {'content': content.strip(), 'containerTag': info.pop('tag'), **info,
            'tags': [str(x).strip() for x in (tags or []) if str(x).strip()]}
    if memory_type: body['type'] = memory_type
    return _request('/api/memories', 'POST', body, timeout=60)


def update(memory_id, content):
    _id(memory_id)
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise ValueError('Memory content must contain 1-100000 characters')
    return _request('/api/memories/' + memory_id, 'PUT', {'content': content.strip()}, timeout=60)


def delete(memory_id):
    _id(memory_id)
    return _request('/api/memories/' + memory_id + '?cascade=true', 'DELETE')


def bulk_delete(ids):
    clean = [_id(value) for value in ids]
    return _request('/api/memories/bulk-delete', 'POST', {'ids': clean, 'cascade': True})


def profile(): return _request('/api/user-profile')
def stats(): return _request('/api/stats')


def _id(value):
    if not isinstance(value, str) or not value.startswith('mem_') or len(value) > 100:
        raise ValueError('Invalid memory ID')
    return value
