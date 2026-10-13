"""OpenViking-backed project memory for the bridge, console and CLI.

Records are memory files under `viking://user/default/memories/<category>/`, with a
`MEMORY_FIELDS` footer that preserves legacy provenance (source id, project tag,
session, timestamps, pin state). A local SQLite journal keeps the legacy-id to
viking-URI mapping, tombstones for deletions and pin state so migration stays
idempotent and deleted records never reappear.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import time
import urllib.parse

import common
import openviking_service

MEMORY_ROOT = 'viking://user/default/memories'
CATEGORIES = {'profile', 'preferences', 'entities', 'events', 'identity', 'soul',
              'cases', 'trajectories', 'experiences', 'tools', 'skills'}
DEFAULT_CATEGORY = 'experiences'
_FOOTER = re.compile(r'\n\n<!--\s*MEMORY_FIELDS\s*\n(.*?)\n-->\s*$', re.DOTALL)
_LIST_CACHE = {'at': 0.0, 'items': None}


def journal_path():
    return openviking_service.state_dir() / 'memory-state.db'


def journal():
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(str(path), timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS records('
                'id TEXT PRIMARY KEY, uri TEXT NOT NULL, project_tag TEXT, project_path TEXT,'
                'project_name TEXT, category TEXT, memory_type TEXT, created_at REAL, updated_at REAL,'
                'size INTEGER, pinned INTEGER DEFAULT 0, origin TEXT, session_id TEXT, content_hash TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS tombstones('
                'id TEXT PRIMARY KEY, uri TEXT, deleted_at REAL, reason TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
    return con


def meta_get(key, default=None):
    with journal() as con:
        row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return row[0]


def meta_set(key, value):
    with journal() as con:
        con.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, json.dumps(value)))
        con.commit()


def _settings():
    try:
        configured = common.config()
    except RuntimeError:
        configured = {}
    cfg = configured.get('memory') if isinstance(configured.get('memory'), dict) else {}
    ov = cfg.get('openviking') if isinstance(cfg.get('openviking'), dict) else {}
    return {'url': (ov.get('url') or openviking_service.URL).rstrip('/'),
            'rolling': cfg.get('rolling') or {},
            'dimensions': int(cfg.get('embedding_dimensions') or 2048)}


def request(path, method='GET', data=None, timeout=60):
    key = openviking_service.api_key()
    headers = {'X-API-Key': key} if key else None
    try:
        payload = common.request(_settings()['url'] + path, method, data,
                                 headers=headers, timeout=timeout)
    except common.HttpFailure as error:
        raise ValueError(str(error)) from None
    if not isinstance(payload, dict):
        raise ValueError('OpenViking returned an unexpected response')
    error = payload.get('error')
    if error:
        if isinstance(error, dict):
            raise ValueError(str(error.get('message') or error.get('code') or 'OpenViking error'))
        raise ValueError(str(error))
    if payload.get('status') not in (None, 'ok'):
        raise ValueError(str(payload.get('status')))
    return payload.get('result', payload)


# ----------------------------------------------------------------- slugs and tags

def slug(text, fallback='memory', limit=48):
    value = re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')
    value = re.sub(r'-{2,}', '-', value)[:limit].strip('-')
    return value or fallback


def sanitize_tag(tag):
    text = str(tag or '').strip()
    if not text:
        return None
    if '=' not in text:
        text = 'tag=' + text
    key, value = text.split('=', 1)
    key = re.sub(r'[^A-Za-z0-9_.-]', '-', key.strip())[:48]
    value = re.sub(r'[^A-Za-z0-9_.:/-]', '-', value.strip())[:160]
    if not key or not value:
        return None
    return key + '=' + value


def merge_tags(existing, added=None, keep=()):
    out = {}
    for tag in list(existing or []) + list(added or []):
        clean = sanitize_tag(tag)
        if clean:
            out[clean.split('=', 1)[0]] = clean
    for key in keep:
        out.pop(key, None)
    return list(out.values())


# ----------------------------------------------------------------- memory files

def parse_memory(raw):
    """Return (content, fields) with the MEMORY_FIELDS footer removed."""
    text = str(raw or '')
    match = _FOOTER.search(text)
    fields = {}
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                fields = parsed
                text = text[:match.start()]
        except ValueError:
            pass
    return text, fields


def compose(content, fields):
    clean = {k: v for k, v in (fields or {}).items() if v is not None}
    if not clean:
        return content
    return content.rstrip() + '\n\n<!-- MEMORY_FIELDS\n' + \
        json.dumps(clean, indent=2, ensure_ascii=False) + '\n-->\n'


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str) and value:
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _iso(epoch):
    from datetime import datetime, timezone
    value = _epoch(epoch)
    return datetime.fromtimestamp(value if value > 0 else time.time(), tz=timezone.utc).isoformat()


def project_tag(directory=None, tag=None):
    if tag:
        return str(tag)
    if not directory:
        return None
    import memory_manager
    return memory_manager.project_info(directory)['tag']


def _memory_files(tag=None, use_cache=True):
    now = time.time()
    if use_cache and tag is None and _LIST_CACHE['items'] is not None and now - _LIST_CACHE['at'] < 5:
        return _LIST_CACHE['items']
    query = {'uri': MEMORY_ROOT, 'recursive': 'true', 'node_limit': 5000}
    if tag:
        query['tags'] = sanitize_tag('project=' + tag)
    while True:
        entries = request('/api/v1/fs/ls?' + urllib.parse.urlencode(query), timeout=120)
        if not isinstance(entries, list):
            raise ValueError('OpenViking returned an invalid memory listing')
        if len(entries) < query['node_limit']:
            break
        if query['node_limit'] >= 1000000:
            raise ValueError('Memory listing is incomplete; refusing partial retention')
        query['node_limit'] *= 2
    files = []
    for entry in entries if isinstance(entries, list) else []:
        uri = entry.get('uri') or ''
        if entry.get('isDir') or not uri.endswith('.md') or uri.rsplit('/', 1)[-1] in ('.abstract.md', '.overview.md'):
            continue
        files.append({'uri': uri, 'size': int(entry.get('size') or 0),
                      'mtime': _epoch(entry.get('modTime')), 'rel': entry.get('rel_path') or ''})
    files.sort(key=lambda item: item['mtime'], reverse=True)
    if tag is None:
        _LIST_CACHE.update(at=now, items=files)
    return files


def _invalidate():
    _LIST_CACHE.update(at=0.0, items=None)


def read(uri, timeout=60):
    # The default visible read intentionally strips MEMORY_FIELDS; management
    # needs the raw owner-authorized representation to preserve provenance/pins.
    raw = request('/api/v1/content/read?' + urllib.parse.urlencode({'uri': uri, 'raw': 'true'}), timeout=timeout)
    content, fields = parse_memory(raw if isinstance(raw, str) else str(raw or ''))
    row = _journal_row(uri)
    if row:
        record = _row_dict(row)
        fields['pinned'] = bool(record.get('pinned'))
        for source, target in (('project_tag', 'project'), ('project_path', 'project_path'),
                               ('project_name', 'project_name'), ('session_id', 'session_id')):
            if record.get(source) is not None:
                fields[target] = record[source]
        if record.get('origin') == 'legacy-migration':
            fields['legacy_id'] = record['id']
        for key in ('created_at', 'updated_at'):
            if record.get(key):
                fields[key] = _iso(record[key])
    return content, fields


def attrs(uri):
    result = request('/api/v1/fs/attrs?' + urllib.parse.urlencode({'uri': uri}))
    attrs = result.get('attrs') if isinstance(result, dict) else {}
    return attrs or {}


def _journal_row(uri):
    with journal() as con:
        return con.execute('SELECT * FROM records WHERE uri=?', (uri,)).fetchone()


def _journal_by_id(memory_id):
    with journal() as con:
        return con.execute('SELECT * FROM records WHERE id=?', (memory_id,)).fetchone()


def _columns(con):
    return [row[1] for row in con.execute('PRAGMA table_info(records)')]


def _row_dict(row):
    if not row:
        return {}
    return dict(zip(_columns(journal()), row))


def _journal_upsert(record):
    columns = ['id', 'uri', 'project_tag', 'project_path', 'project_name', 'category',
               'memory_type', 'created_at', 'updated_at', 'size', 'pinned', 'origin',
               'session_id', 'content_hash']
    values = [record.get(name) for name in columns]
    with journal() as con:
        con.execute('INSERT INTO records(' + ','.join(columns) + ') VALUES(' + ','.join('?' * len(columns)) + ') '
                    'ON CONFLICT(id) DO UPDATE SET ' + ','.join(name + '=excluded.' + name for name in columns[1:]),
                    values)
        con.commit()


def _journal_delete(memory_id):
    with journal() as con:
        con.execute('DELETE FROM records WHERE id=? OR uri=?', (str(memory_id), str(memory_id)))
        con.commit()


def _tombstone(memory_id, uri, reason='deleted'):
    with journal() as con:
        con.execute('INSERT INTO tombstones(id,uri,deleted_at,reason) VALUES(?,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET uri=excluded.uri, deleted_at=excluded.deleted_at, reason=excluded.reason',
                    (str(memory_id), uri, time.time(), reason))
        con.commit()


def is_tombstoned(memory_id):
    with journal() as con:
        return bool(con.execute('SELECT 1 FROM tombstones WHERE id=?', (str(memory_id),)).fetchone())


def _category(memory_type):
    value = str(memory_type or '').strip().lower()
    return value if value in CATEGORIES else DEFAULT_CATEGORY


def write(uri, content, fields=None, tags=None, mode='replace', wait=True, timeout=120):
    body = {'uri': uri, 'content': content, 'mode': mode, 'wait': bool(wait), 'timeout': timeout}
    clean = [tag for tag in (sanitize_tag(t) for t in (tags or [])) if tag]
    if clean:
        body['tags'] = clean
    return request('/api/v1/content/write', 'POST', body, timeout=timeout + 30)


def _item(uri, fields=None, content=None, extra=None):
    fields = fields or {}
    extra = extra or {}
    tags = list(extra.get('tags') or [])
    item = {'id': uri, 'type': 'memory', 'uri': uri,
            'content': content if content is not None else '',
            'memoryType': fields.get('memory_type') or (uri.split('/memories/', 1)[-1].split('/', 1)[0]),
            'tags': tags,
            'createdAt': _iso(fields.get('created_at') or extra.get('mtime')),
            'updatedAt': _iso(fields.get('updated_at') or extra.get('mtime')),
            'displayName': fields.get('display_name') or None,
            'projectPath': fields.get('project_path') or None,
            'projectName': fields.get('project_name') or None,
            'legacyId': fields.get('legacy_id') or None,
            'sessionId': fields.get('session_id') or None,
            'isPinned': bool(fields.get('pinned')) or any(tag == 'pinned=true' for tag in tags)}
    if item['projectName'] is None and item['projectPath']:
        item['projectName'] = os.path.basename(str(item['projectPath']).rstrip('/')) or item['projectPath']
    return item


def _load_items(uris, with_tags=False):
    items = []
    for file in uris:
        try:
            content, fields = read(file['uri'])
        except ValueError as error:
            items.append({'id': file['uri'], 'uri': file['uri'], 'content': '',
                          'error': common.redact(str(error)), 'memoryType': '', 'tags': [],
                          'isPinned': False})
            continue
        extra = {'mtime': file['mtime'], 'tags': []}
        if with_tags:
            try:
                extra['tags'] = attrs(file['uri']).get('tags') or []
            except ValueError:
                pass
        items.append(_item(file['uri'], fields, content, extra))
    return items


def list_memories(directory=None, page=1, page_size=50, tag=None, include_prompts=False):
    """List memory files, newest first, optionally scoped by project tag."""
    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    scope = project_tag(directory, tag)
    files = _memory_files(tag=scope)
    total = len(files)
    start = (page - 1) * page_size
    page_files = files[start:start + page_size]
    items = _load_items(page_files, with_tags=True)
    return {'items': items, 'total': total, 'page': page, 'pageSize': page_size,
            'totalPages': max(1, (total + page_size - 1) // page_size), 'scope': scope}


def search(query, directory=None, page=1, page_size=20, tag=None):
    if not isinstance(query, str) or not query.strip():
        raise ValueError('Memory search query is required')
    limit = min(100, max(1, int(page_size)))
    body = {'query': query.strip(), 'limit': limit, 'read_content': False,
            'target_uri': MEMORY_ROOT, 'context_type': ['memory']}
    scope = project_tag(directory, tag)
    if scope:
        body['tags'] = [sanitize_tag('project=' + scope)]
    result = request('/api/v1/search/find', 'POST', body, timeout=120)
    hits = result.get('memories') if isinstance(result, dict) else []
    items = []
    for hit in hits or []:
        uri = hit.get('uri') or ''
        content = hit.get('content')
        fields = {}
        if content is None:
            try:
                content, fields = read(uri)
            except ValueError:
                content = hit.get('abstract') or ''
            content = content if content is not None else ''
        else:
            content, fields = parse_memory(content)
        item = _item(uri, fields, content, {'tags': hit.get('tags') or []})
        item['score'] = hit.get('score')
        items.append(item)
    return {'items': items, 'total': len(items), 'page': max(1, int(page)),
            'pageSize': limit, 'scope': scope}


def add(content, directory, tags=None, memory_type=None, created_at=None, uri=None):
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise ValueError('Memory content must contain 1-100000 characters')
    category = _category(memory_type)
    info = {}
    scope = None
    if directory:
        import memory_manager
        info = memory_manager.project_info(directory)
        scope = info['tag']
    now = time.time()
    fields = {'memory_type': category,
              'created_at': _iso(created_at or now),
              'updated_at': _iso(now),
              'pinned': False,
              'origin': 'bridge'}
    if scope:
        fields['project'] = scope
        fields['project_path'] = info.get('projectPath')
        fields['project_name'] = info.get('projectName')
    target = uri or '{}/{}/{}-{}.md'.format(MEMORY_ROOT, category, slug(content), secrets.token_hex(4))
    clean = merge_tags(tags)
    if scope:
        clean = merge_tags(clean, ['project=' + scope])
    write(target, compose(content.strip(), fields), tags=clean, wait=True)
    _invalidate()
    _journal_upsert({'id': target, 'uri': target, 'project_tag': scope,
                     'project_path': fields.get('project_path'), 'project_name': fields.get('project_name'),
                     'category': category, 'memory_type': category, 'created_at': now, 'updated_at': now,
                     'size': len(content.encode('utf-8')), 'pinned': 0, 'origin': 'bridge',
                     'session_id': None, 'content_hash': hashlib.sha256(content.encode()).hexdigest()})
    fresh, _ = read(target)
    return {'id': target, 'uri': target, 'content': fresh, 'tags': clean}


def _resolve(memory_id):
    if not isinstance(memory_id, str) or not memory_id.startswith('viking://') or len(memory_id) > 1024:
        raise ValueError('Invalid memory ID')
    return memory_id


def update(memory_id, content):
    uri = _resolve(memory_id)
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise ValueError('Memory content must contain 1-100000 characters')
    _, fields = read(uri)
    fields['updated_at'] = _iso(time.time())
    current = attrs(uri)
    tags = [tag for tag in current.get('tags') or [] if not tag.startswith('pinned=')]
    if fields.get('pinned'):
        tags.append('pinned=true')
    # Native memory replacement preserves its existing MEMORY_FIELDS trailer.
    # Sending another trailer nests metadata into the body and corrupts reads.
    write(uri, content.strip(), tags=tags, wait=True)
    _invalidate()
    row = _journal_row(uri)
    if row:
        record = _row_dict(row)
        record.update({'updated_at': time.time(), 'size': len(content.encode('utf-8')),
                       'content_hash': hashlib.sha256(content.encode()).hexdigest()})
        _journal_upsert(record)
    fresh, _ = read(uri)
    return {'id': uri, 'uri': uri, 'content': fresh}


def delete(memory_id):
    uri = _resolve(memory_id)
    request('/api/v1/fs?' + urllib.parse.urlencode({'uri': uri, 'recursive': 'true'}), 'DELETE', timeout=60)
    row = _journal_row(uri)
    if row:
        record = _row_dict(row)
        # Keep the legacy source id tombstoned so a later migration delta cannot
        # resurrect a record that was deleted here.
        _tombstone(record.get('id') or uri, uri, 'bridge-delete')
    _tombstone(uri, uri, 'bridge-delete')
    _journal_delete(uri)
    _invalidate()
    return {'deleted': uri}


def bulk_delete(ids):
    deleted, errors = [], []
    for memory_id in ids or []:
        try:
            delete(memory_id)
            deleted.append(memory_id)
        except ValueError as error:
            errors.append({'id': memory_id, 'error': common.redact(str(error))})
    return {'deleted': deleted, 'count': len(deleted), 'errors': errors}


def pin(memory_id, pinned=True):
    uri = _resolve(memory_id)
    current = attrs(uri)
    tags = merge_tags(current.get('tags'), ['pinned=true'] if pinned else None,
                      keep=() if pinned else ('pinned',))
    request('/api/v1/fs/attrs/set_tags', 'POST',
            {'uri': uri, 'tags': tags, 'mode': 'replace'}, timeout=60)
    content, fields = read(uri)
    # Pins are bridge policy, represented by retrieval tags + the journal.
    # OpenViking preserves its footer on content replacement; do not rewrite it.
    row = _journal_row(uri)
    if row:
        record = _row_dict(row)
        record['pinned'] = 1 if pinned else 0
        _journal_upsert(record)
    else:
        _journal_upsert({'id': uri, 'uri': uri, 'pinned': 1 if pinned else 0,
                         'origin': 'openviking', 'category': fields.get('memory_type'),
                         'project_tag': fields.get('project'), 'project_path': fields.get('project_path'),
                         'project_name': fields.get('project_name'),
                         'size': len(content.encode('utf-8')), 'updated_at': time.time()})
    _invalidate()
    return {'id': uri, 'pinned': bool(pinned)}


def projects():
    """Known project scopes from the journal plus live bridge workspaces."""
    rows = []
    with journal() as con:
        for row in con.execute('SELECT project_tag, project_path, project_name, COUNT(*) FROM records '
                               'WHERE project_tag IS NOT NULL GROUP BY project_tag'):
            tag, path, name, count = row
            rows.append({'tag': tag, 'projectPath': path, 'projectName': name or
                         (os.path.basename(str(path).rstrip('/')) if path else tag), 'memories': count})
    known = {row['tag'] for row in rows}
    import memory_manager
    for directory in memory_manager.known_directories():
        try:
            info = memory_manager.project_info(directory)
        except ValueError:
            continue
        if info['tag'] not in known:
            rows.append({'tag': info['tag'], 'projectPath': info['projectPath'],
                         'projectName': info['projectName'], 'memories': 0})
            known.add(info['tag'])
    return sorted(rows, key=lambda item: str(item.get('projectName') or '').lower())


def profile():
    uri = MEMORY_ROOT + '/profile.md'
    try:
        content, fields = read(uri)
    except ValueError:
        return {'profile': None}
    return {'profile': content, **{k: fields.get(k) for k in ('updated_at',)}}


def stats():
    with journal() as con:
        total = con.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        pinned = con.execute('SELECT COUNT(*) FROM records WHERE pinned=1').fetchone()[0]
        legacy = con.execute("SELECT COUNT(*) FROM records WHERE origin='legacy-migration'").fetchone()[0]
        tombstones = con.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0]
    live = len(_memory_files())
    return {'total': max(live, total), 'live': live, 'journal': total, 'pinned': pinned,
            'migrated': legacy, 'tombstones': tombstones,
            'byScope': {'user': 0, 'project': total},
            'migration': meta_get('migration', {})}


def _estimated_bytes(size, dimensions):
    return max(0, int(size)) + int(dimensions) * 4 + 4096


def maintain(force=False):
    """Capacity-based rolling retention; pinned records are never reclaimed."""
    cfg = _settings()
    policy = cfg['rolling'] if isinstance(cfg['rolling'], dict) else {}
    policy = {'enabled': True, 'max_storage_mb': 1024, 'max_memories': 100000,
              'target_percent': 90, 'check_interval_hours': 24, **policy}
    state_path = common.STATE / 'memory-maintenance.json'
    if not policy.get('enabled', True):
        return {'enabled': False, 'reason': 'disabled'}
    now = time.time()
    with common.locked('memory-maintenance'):
        previous = common.read_json(state_path, {}) or {}
        if not force and now - float(previous.get('checked_at') or 0) < policy['check_interval_hours'] * 3600:
            return {**previous, 'due': False}
        try:
            files = _memory_files(use_cache=False)
            pinned = set()
            with journal() as con:
                pinned = {row[0] for row in con.execute('SELECT uri FROM records WHERE pinned=1')}
            capacity = policy['max_storage_mb'] * 1024 * 1024
            target_bytes = max(1, int(capacity * policy['target_percent'] / 100))
            target_records = max(1, int(policy['max_memories'] * policy['target_percent'] / 100))
            total_bytes = sum(_estimated_bytes(item['size'], cfg['dimensions']) for item in files)
            if total_bytes <= capacity and len(files) <= policy['max_memories']:
                result = {'enabled': True, 'checked_at': now, 'before': len(files), 'deleted': 0,
                          'after': len(files), 'estimated_bytes_before': total_bytes,
                          'estimated_bytes_after': total_bytes, 'capacity_bytes': capacity,
                          'capacity_mb': policy['max_storage_mb'], 'record_safety_limit': policy['max_memories'],
                          'target_percent': policy['target_percent'], 'due': True}
            else:
                candidates = sorted((item for item in files if item['uri'] not in pinned),
                                    key=lambda item: item['mtime'])
                remove, remaining_bytes, remaining_count = [], total_bytes, len(files)
                for item in candidates:
                    if remaining_bytes <= target_bytes and remaining_count <= target_records:
                        break
                    remove.append(item)
                    remaining_bytes -= _estimated_bytes(item['size'], cfg['dimensions'])
                    remaining_count -= 1
                deleted = 0
                for start in range(0, len(remove), 100):
                    for item in remove[start:start + 100]:
                        delete(item['uri'])
                        deleted += 1
                _invalidate()
                result = {'enabled': True, 'checked_at': now, 'before': len(files), 'deleted': deleted,
                          'after': len(files) - deleted, 'estimated_bytes_before': total_bytes,
                          'estimated_bytes_after': max(0, total_bytes - sum(
                              _estimated_bytes(item['size'], cfg['dimensions']) for item in remove)),
                          'capacity_bytes': capacity, 'capacity_mb': policy['max_storage_mb'],
                          'record_safety_limit': policy['max_memories'],
                          'target_percent': policy['target_percent'], 'due': True}
        except Exception as error:
            result = {'enabled': True, 'checked_at': now, 'deleted': 0,
                      'error': common.redact(str(error)), 'due': True}
        common.write_json(state_path, result)
        return result


def status():
    """Console-facing status: backend identity, service readiness, counts and migration."""
    cfg = _settings()
    state = common.read_json(openviking_service.state_dir() / 'service.json', {}) or {}
    result = {'enabled': True, 'backend': 'openviking', 'url': cfg['url'],
              'storage_path': str(openviking_service._settings()['workspace']), 'ready': False,
              'embedding_model': openviking_service.embedding_settings()['model'],
              'rolling': None,
              'migration': meta_get('migration', {})}
    probe = openviking_service.probe(cfg['url'], timeout=3)
    result['healthy'] = probe['healthy']
    result['ready'] = probe['ready']
    if probe['healthy']:
        try:
            result['stats'] = stats()
        except ValueError as error:
            result['error'] = common.redact(str(error))
    else:
        result['error'] = common.redact(json.dumps(probe.get('health', {}))[:400])
    return result
