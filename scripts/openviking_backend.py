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
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import common
import openviking_service

MEMORY_ROOT = 'viking://user/default/memories'
CATEGORIES = {'profile', 'preferences', 'entities', 'events', 'identity', 'soul',
              'cases', 'trajectories', 'experiences', 'tools', 'skills'}
DEFAULT_CATEGORY = 'experiences'
_FOOTER = re.compile(r'\n\n<!--\s*MEMORY_FIELDS\s*\n(.*?)\n-->\s*$', re.DOTALL)
_NOT_READY = re.compile(r'\[[^\]]*is not ready\]')
_SAFE_SEGMENT = re.compile(r'^[^\x00-\x1f\x7f%?#\\]+$')
_LIST_CACHE = {'at': 0.0, 'items': None}
# One bounded metadata index for browser navigation: a recursive listing with
# per-file tags is cheap (a few hundred milliseconds for thousands of files),
# and it avoids reading every memory body for a page view.
_INDEX_CACHE = {'at': 0.0, 'index': None, 'lock': threading.Lock()}
INDEX_TTL_SECONDS = 30
_PREVIEW_LINES = 7
_PREVIEW_CHARS = 360


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

def validate_memory_uri(value, allow_root=False):
    """Strictly accept only descendants of the memory root.

    Management actions must never reach the sessions, resources, skills or
    another user's namespace, and encoded escapes or traversal segments are
    rejected before a URI is handed to OpenViking.
    """
    if not isinstance(value, str):
        raise ValueError('Invalid memory URI')
    uri = value.strip()
    if not uri or len(uri) > 1024 or uri != value:
        raise ValueError('Invalid memory URI')
    if '%' in uri or '?' in uri or '#' in uri or '\\' in uri:
        raise ValueError('Memory URIs cannot contain escapes or query parts')
    if uri == MEMORY_ROOT:
        if allow_root:
            return uri
        raise ValueError('Memory URI must name a file inside the memory root')
    if not uri.startswith(MEMORY_ROOT + '/'):
        raise ValueError('Memory URI must stay inside ' + MEMORY_ROOT)
    tail = uri[len(MEMORY_ROOT) + 1:]
    for segment in tail.split('/'):
        if segment in ('', '.', '..') or len(segment) > 160 or not _SAFE_SEGMENT.match(segment):
            raise ValueError('Invalid memory URI path segment')
    return uri


def validate_memory_file(value):
    """A concrete memory record: a non-hidden Markdown descendant of the root."""
    uri = validate_memory_uri(value)
    name = uri.rsplit('/', 1)[-1]
    if not name.endswith('.md') or name.startswith('.'):
        raise ValueError('Memory URI must name a memory file')
    return uri


def memory_category(uri):
    rel = uri.split('/memories/', 1)[-1]
    parts = [part for part in rel.split('/') if part]
    return parts[0] if len(parts) > 1 else 'root'


def _is_memory_file(uri):
    if not isinstance(uri, str) or not uri.startswith(MEMORY_ROOT + '/'):
        return False
    name = uri.rsplit('/', 1)[-1]
    return name.endswith('.md') and not name.startswith('.')


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
    # fs/ls(recursive=true) implicitly stops at depth 3 in OpenViking 0.5.
    # Native date-organized events live deeper; use the tree API explicitly.
    query = {'uri': MEMORY_ROOT, 'level_limit': 64, 'node_limit': 5000,
             'include_abstract': 'false'}
    if tag:
        query['tags'] = sanitize_tag('project=' + tag)
    while True:
        entries = request('/api/v1/fs/tree?' + urllib.parse.urlencode(query), timeout=120)
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
    with _INDEX_CACHE['lock']:
        _INDEX_CACHE.update(at=0.0, index=None)


# ----------------------------------------------------------------- metadata index

def _memory_index(use_cache=True):
    """One recursive metadata listing (sizes, mtimes, retrieval tags) for navigation.

    Bodies are never read here: browse reads only bounded previews for the
    visible page, and the inspector reads the raw body for the selected record.
    """
    now = time.time()
    if use_cache and _INDEX_CACHE['index'] is not None and now - _INDEX_CACHE['at'] < INDEX_TTL_SECONDS:
        return _INDEX_CACHE['index']
    query = {'uri': MEMORY_ROOT, 'level_limit': 64, 'node_limit': 5000,
             'include_tags': 'true', 'include_abstract': 'false'}
    while True:
        entries = request('/api/v1/fs/tree?' + urllib.parse.urlencode(query), timeout=120)
        if not isinstance(entries, list):
            raise ValueError('OpenViking returned an invalid memory listing')
        if len(entries) < query['node_limit']:
            break
        if query['node_limit'] >= 1000000:
            raise ValueError('Memory listing is incomplete; refusing partial navigation')
        query['node_limit'] *= 2
    files, dirs = [], []
    for entry in entries:
        uri = entry.get('uri') or ''
        if not uri.startswith(MEMORY_ROOT + '/'):
            continue
        rel = entry.get('rel_path') or uri[len(MEMORY_ROOT) + 1:]
        if entry.get('isDir'):
            parts = [part for part in rel.split('/') if part]
            dirs.append({'uri': uri, 'rel': rel, 'name': parts[-1] if parts else '',
                         'depth': len(parts)})
            continue
        if not _is_memory_file(uri):
            continue
        parts = [part for part in rel.split('/') if part]
        files.append({'uri': uri, 'rel': rel, 'name': parts[-1] if parts else '',
                      'category': parts[0] if len(parts) > 1 else 'root',
                      'folder': '/'.join(parts[:-1]),
                      'size': int(entry.get('size') or 0),
                      'mtime': _epoch(entry.get('modTime')),
                      'tags': [tag for tag in (entry.get('tags') or []) if isinstance(tag, str)]})
    files.sort(key=lambda item: item['mtime'], reverse=True)
    index = {'at': now, 'files': files, 'dirs': dirs,
             'journal': _journal_map(), 'pinned': _pinned_uris(files)}
    with _INDEX_CACHE['lock']:
        _INDEX_CACHE.update(at=now, index=index)
    return index


def _journal_map():
    """Project attribution, pins and provenance from the bridge journal."""
    out = {}
    with journal() as con:
        rows = con.execute('SELECT uri, id, pinned, project_tag, project_path, project_name, '
                           'origin, session_id, created_at, updated_at, memory_type FROM records').fetchall()
    for row in rows:
        uri = row[0]
        if not uri:
            continue
        out[uri] = {'id': row[1], 'pinned': bool(row[2]), 'project_tag': row[3], 'project_path': row[4],
                    'project_name': row[5], 'origin': row[6], 'session_id': row[7],
                    'created_at': row[8], 'updated_at': row[9], 'memory_type': row[10]}
    return out


def _pinned_uris(files=None):
    """Bridge journal pins unioned with native `pinned=true` retrieval tags.

    A record pinned directly in OpenViking (outside the console) must stay
    protected from rolling retention too.
    """
    out = set()
    with journal() as con:
        for row in con.execute('SELECT uri FROM records WHERE pinned=1'):
            if row[0]:
                out.add(row[0])
    for file in files or []:
        if 'pinned=true' in (file.get('tags') or []):
            out.add(file['uri'])
    return out


def _native_pinned_uris():
    """Native `pinned=true` listing for retention.

    Never degrades to an empty result: when this listing cannot be verified the
    caller must fail closed, because an unverified set would let externally
    pinned native memories be reclaimed.
    """
    query = {'uri': MEMORY_ROOT, 'level_limit': 64, 'node_limit': 5000,
             'include_abstract': 'false',
             'tags': sanitize_tag('pinned=true'), 'include_tags': 'true'}
    while True:
        entries = request('/api/v1/fs/tree?' + urllib.parse.urlencode(query), timeout=120)
        if not isinstance(entries, list):
            raise ValueError('OpenViking returned an invalid pinned-tag listing')
        if len(entries) < query['node_limit']:
            break
        if query['node_limit'] >= 1000000:
            raise ValueError('Pinned-tag listing is incomplete; refusing to reclaim')
        query['node_limit'] *= 2
    return {entry['uri'] for entry in entries
            if isinstance(entry.get('uri'), str) and 'pinned=true' in (entry.get('tags') or [])}


def _tag_value(tags, key):
    prefix = key + '='
    for tag in tags or []:
        if isinstance(tag, str) and tag.startswith(prefix):
            return tag.split('=', 1)[1]
    return None


def _file_project(file, journal_row):
    """Native retrieval tag wins; the journal covers records that predate tags."""
    value = _tag_value(file.get('tags'), 'project')
    if value:
        return value
    return (journal_row or {}).get('project_tag')


def _file_pinned(file, journal_row, pinned_uris):
    if 'pinned=true' in (file.get('tags') or []):
        return True
    if file['uri'] in (pinned_uris or set()):
        return True
    return bool((journal_row or {}).get('pinned'))


def _file_item(file, journal_row, project, pinned):
    row = journal_row or {}
    updated = row.get('updated_at') or file['mtime']
    created = row.get('created_at') or file['mtime']
    return {'id': file['uri'], 'uri': file['uri'], 'type': 'memory',
            'memoryType': row.get('memory_type') or file['category'],
            'category': file['category'], 'folder': file['folder'], 'rel': file['rel'],
            'displayName': file['name'],
            'projectTag': project, 'projectName': row.get('project_name') or None,
            'projectPath': row.get('project_path') or None,
            'tags': [tag for tag in file.get('tags') or []],
            'size': int(file.get('size') or 0),
            'createdAt': _iso(created), 'updatedAt': _iso(updated),
            'isPinned': bool(pinned), 'legacyId': row.get('id') if row.get('origin') == 'legacy-migration' else None,
            'sessionId': row.get('session_id') or None, 'origin': row.get('origin') or 'openviking'}


def _page_slice(rows, page, page_size):
    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    total = len(rows)
    start = (page - 1) * page_size
    return rows[start:start + page_size], total, page, page_size, \
        max(1, (total + page_size - 1) // page_size)


def _preview(uri, lines=_PREVIEW_LINES, limit=_PREVIEW_CHARS):
    """Bounded, server-sanitized preview: OpenViking strips memory metadata itself."""
    query = {'uri': uri, 'offset': 0, 'limit': max(1, int(lines))}
    raw = request('/api/v1/content/read?' + urllib.parse.urlencode(query), timeout=20)
    text = raw if isinstance(raw, str) else str(raw or '')
    text = ' '.join(text.split())
    return text[:limit]


def _attach_previews(items, workers=6):
    if not items:
        return items
    def load(item):
        try:
            item['preview'] = _preview(item['uri'])
        except ValueError as error:
            item['preview'] = ''
            item['previewError'] = common.redact(str(error))
        return item
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        list(pool.map(load, items))
    return items


def browse(category=None, folder=None, project=None, directory=None, page=1, page_size=25,
           sort='newest', preview=True, query=None):
    """Paginated metadata browser for the console memory page.

    Filters intersect: a real category/folder in the native filesystem, an
    optional project retrieval tag, and an optional sort. Previews are bounded
    and read only for the visible page. A non-empty ``query`` switches to
    native semantic retrieval scoped to the same project.
    """
    if isinstance(query, str) and query.strip():
        result = search(query.strip(), page=1, page_size=page_size, tag=project)
        nav = browse(category=None, folder=None, project=project, directory=directory,
                     page=1, page_size=1, preview=False)
        result.update({key: nav[key] for key in
                       ('categories', 'directories', 'projects', 'totalRecords')})
        result.update({'semantic': True, 'sort': 'relevance', 'query': query.strip(),
                       'category': '', 'folder': '', 'generatedAt': time.time()})
        return result
    if category is not None and (not isinstance(category, str) or '/' in category or len(category) > 160):
        raise ValueError('Invalid memory category')
    if folder is not None and (not isinstance(folder, str) or len(folder) > 512):
        raise ValueError('Invalid memory folder')
    if category is not None:
        if not isinstance(category, str) or len(category) > 160:
            raise ValueError('Invalid memory category')
        if category:
            # Validate the real directory path but keep the short name used by
            # the index, so the filter compares like with like.
            validate_memory_uri(MEMORY_ROOT + '/' + category, allow_root=False)
        else:
            category = None
    if folder:
        folder = validate_memory_uri(MEMORY_ROOT + '/' + folder.lstrip('/'), allow_root=False)
        folder = folder[len(MEMORY_ROOT) + 1:]
    index = _memory_index()
    journal_map = index.get('journal') or {}
    pinned_uris = index.get('pinned') or set()
    scope = project or project_tag(directory, None)
    if scope is not None:
        scope = str(scope)
    all_rows = []
    for file in index['files']:
        row = journal_map.get(file['uri'])
        all_rows.append((file, row, _file_project(file, row), _file_pinned(file, row, pinned_uris)))
    # Faceted navigation counts: every count is the size of the result after
    # applying that selection on top of the *other* active filters, so a
    # visible count always matches the rows the user would get.
    def in_scope(file, file_project):
        return scope is None or file_project == scope
    def in_category(file):
        return not category or file['category'] == category
    def in_folder(file):
        return not folder or file['folder'] == folder or file['folder'].startswith(folder + '/')
    category_counts, project_counts, directory_counts = {}, {}, {}
    for file, _row, file_project, _pinned in all_rows:
        if in_scope(file, file_project) and in_folder(file):
            category_counts[file['category']] = category_counts.get(file['category'], 0) + 1
        if in_category(file) and in_folder(file) and file_project:
            project_counts[file_project] = project_counts.get(file_project, 0) + 1
        if in_scope(file, file_project) and in_category(file) and file['folder']:
            directory_counts[file['folder']] = directory_counts.get(file['folder'], 0) + 1
    if sort == 'oldest':
        ordered = sorted(all_rows, key=lambda entry: entry[0]['mtime'])
    elif sort == 'name':
        ordered = sorted(all_rows, key=lambda entry: entry[0]['rel'].lower())
    elif sort == 'size':
        ordered = sorted(all_rows, key=lambda entry: entry[0]['size'], reverse=True)
    elif sort in ('newest', None, ''):
        ordered = all_rows  # index order is newest-first
    else:
        raise ValueError('Unknown memory sort order')
    rows = []
    for file, row, file_project, pinned in ordered:
        if not in_category(file) or not in_folder(file) or not in_scope(file, file_project):
            continue
        rows.append(_file_item(file, row, file_project, pinned))
    page_items, total, page, page_size, total_pages = _page_slice(rows, page, page_size)
    if preview:
        _attach_previews(page_items)
    categories = [{'name': name, 'uri': MEMORY_ROOT + '/' + name, 'count': count}
                  for name, count in sorted(category_counts.items(), key=lambda kv: (-kv[1], kv[0]))
                  if name != 'root']
    if category_counts.get('root'):
        categories.append({'name': 'root', 'uri': MEMORY_ROOT, 'count': category_counts['root']})
    directories = []
    for entry in sorted(index['dirs'], key=lambda item: item['rel']):
        directories.append({'uri': entry['uri'], 'rel': entry['rel'], 'name': entry['name'],
                            'depth': entry['depth'],
                            'count': sum(count for rel, count in directory_counts.items()
                                         if rel == entry['rel'] or rel.startswith(entry['rel'] + '/'))})
    projects = [{'tag': tag, 'count': count, 'name': _project_label(tag, journal_map)}
                for tag, count in project_counts.items() if tag]
    projects.sort(key=lambda item: item['name'].lower())
    return {'items': page_items, 'total': total, 'page': page, 'pageSize': page_size,
            'totalPages': total_pages, 'scope': scope, 'category': category or '', 'folder': folder or '',
            'sort': sort, 'categories': categories, 'directories': directories, 'projects': projects,
            'totalRecords': len(all_rows), 'generatedAt': time.time()}


def _project_label(tag, journal_map):
    for row in (journal_map or {}).values():
        if row.get('project_tag') == tag and row.get('project_name'):
            return row['project_name']
        if row.get('project_tag') == tag and row.get('project_path'):
            return Path(str(row['project_path']).rstrip('/')).name or tag
    return tag



def read(uri, timeout=60):
    # The default visible read intentionally strips MEMORY_FIELDS; management
    # needs the raw owner-authorized representation to preserve provenance/pins.
    uri = validate_memory_uri(uri)
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
    # OpenViking 0.5.1+ rejects server-side wait timeout fields.
    # Keep the operation deadline exclusively on the HTTP client.
    body = {'uri': uri, 'content': content, 'mode': mode, 'wait': bool(wait)}
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
    try:
        index = _memory_index()
    except Exception:
        # Navigation metadata is an enrichment: search results must still be
        # served when the listing is unavailable or truncated.
        index = {'journal': {}, 'pinned': set()}
    journal_map = index.get('journal') or {}
    pinned_uris = index.get('pinned') or set()
    items = []
    for hit in hits or []:
        uri = hit.get('uri') or ''
        if not _is_memory_file(uri):
            continue  # directory summaries are navigation artifacts, not memories
        row = journal_map.get(uri) or {}
        file = {'uri': uri, 'rel': uri[len(MEMORY_ROOT) + 1:], 'name': uri.rsplit('/', 1)[-1],
                'category': memory_category(uri),
                'folder': '/'.join([part for part in uri[len(MEMORY_ROOT) + 1:].split('/')[:-1] if part]),
                'size': 0, 'mtime': 0.0, 'tags': []}
        fields = {}
        content = hit.get('content')
        if content is not None:
            content, fields = parse_memory(content)
        if fields.get('legacy_id'):
            # A record migrated from the legacy store keeps its source id.
            fields.setdefault('legacyId', fields['legacy_id'])
        extra = {}
        for key, target in (('legacy_id', 'legacyId'), ('session_id', 'sessionId'),
                            ('project_path', 'projectPath'), ('project_name', 'projectName'),
                            ('display_name', 'displayName')):
            if fields.get(key) not in (None, ''):
                extra[target] = fields[key]
        project = _tag_value(file['tags'], 'project') or row.get('project_tag')
        item = _file_item(file, row, project, _file_pinned(file, row, pinned_uris))
        if content is not None:
            item['content'] = content
        item.update(extra)
        item['tags'] = list(hit.get('tags') or [])
        abstract = hit.get('abstract')
        if content is None and isinstance(abstract, str) and abstract:
            item['preview'] = ' '.join(abstract.split())[:_PREVIEW_CHARS]
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
    if uri:
        uri = validate_memory_file(uri)
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
    """Legacy alias kept for callers; strict memory-root validation applies."""
    return validate_memory_file(memory_id)


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


def _retention_plan(files, pinned, policy, dimensions):
    """Pure, non-mutating reclaim plan shared by preview and the real run."""
    capacity = int(policy['max_storage_mb']) * 1024 * 1024
    target_bytes = max(1, int(capacity * policy['target_percent'] / 100))
    target_records = max(1, int(policy['max_memories'] * policy['target_percent'] / 100))
    total_bytes = sum(_estimated_bytes(item['size'], dimensions) for item in files)
    over = total_bytes > capacity or len(files) > policy['max_memories']
    remove, remaining_bytes, remaining_count = [], total_bytes, len(files)
    if over:
        for item in sorted((entry for entry in files if entry['uri'] not in pinned),
                           key=lambda entry: entry['mtime']):
            if remaining_bytes <= target_bytes and remaining_count <= target_records:
                break
            remove.append(item)
            remaining_bytes -= _estimated_bytes(item['size'], dimensions)
            remaining_count -= 1
    pinned_count = sum(1 for item in files if item['uri'] in pinned)
    return {'remove': remove, 'over': over, 'capacity_bytes': capacity,
            'target_bytes': target_bytes, 'target_records': target_records,
            'estimated_bytes': total_bytes, 'estimated_bytes_after': remaining_bytes,
            'records': len(files), 'records_after': remaining_count,
            'pinned': pinned_count, 'unpinned': len(files) - pinned_count}


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
            pinned = _pinned_uris()
            plan = _retention_plan(files, pinned, policy, cfg['dimensions'])
            if plan['over']:
                # A real reclaim needs the native pinned-tag listing; journal
                # pins alone are not proof that an external pin is absent, so a
                # failed listing fails closed and reclaims nothing.
                try:
                    native = _native_pinned_uris()
                except Exception as error:
                    result = {'enabled': True, 'checked_at': now, 'deleted': 0,
                              'planned': len(plan['remove']),
                              'error': 'Pinned-tag listing unavailable; no records were reclaimed: '
                                       + common.redact(str(error)),
                              'due': True}
                    common.write_json(state_path, result)
                    return result
                if native - pinned:
                    plan = _retention_plan(files, pinned | native, policy, cfg['dimensions'])
        except Exception as error:
            result = {'enabled': True, 'checked_at': now, 'deleted': 0,
                      'error': common.redact(str(error)), 'due': True}
            common.write_json(state_path, result)
            return result
        deleted, reclaimed, failures = 0, 0, []
        for item in plan['remove']:
            try:
                delete(item['uri'])
                deleted += 1
                reclaimed += _estimated_bytes(item['size'], cfg['dimensions'])
            except Exception as error:
                # One failed record must not hide the records that were already
                # reclaimed: the result keeps the real count and byte totals.
                failures.append({'uri': item['uri'], 'error': common.redact(str(error))})
        if deleted:
            _invalidate()
        result = {'enabled': True, 'checked_at': now, 'before': plan['records'], 'deleted': deleted,
                  'after': plan['records'] - deleted,
                  'estimated_bytes_before': plan['estimated_bytes'],
                  'estimated_bytes_after': max(0, plan['estimated_bytes'] - reclaimed),
                  'capacity_bytes': plan['capacity_bytes'], 'capacity_mb': policy['max_storage_mb'],
                  'record_safety_limit': policy['max_memories'],
                  'target_percent': policy['target_percent'], 'due': True}
        if failures:
            result['planned'] = len(plan['remove'])
            result['failed'] = len(failures)
            result['errors'] = failures[:20]
        common.write_json(state_path, result)
        return result


def _workspace_bytes():
    """Measured bytes of the live OpenViking workspace only.

    Migration backups and the legacy store sit elsewhere and are deliberately
    excluded: they are not live memory and are never presented as one cap.
    """
    path = Path(openviking_service._settings()['workspace'])
    if not path.is_dir():
        return {'path': str(path), 'bytes': None, 'files': 0}
    total, files = 0, 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.stat(os.path.join(root, name)).st_size
                files += 1
            except OSError:
                continue
    return {'path': str(path), 'bytes': total, 'files': files}


def capacity():
    """Capacity view: logical estimate, measured workspace, and a safe preview.

    The preview never deletes or journals anything; the confirmed maintenance
    run is the only mutating path.
    """
    cfg = _settings()
    policy = cfg['rolling'] if isinstance(cfg['rolling'], dict) else {}
    policy = {'enabled': True, 'max_storage_mb': 1024, 'max_memories': 100000,
              'target_percent': 90, 'check_interval_hours': 24, **policy}
    index = _memory_index()
    plan = _retention_plan(index['files'], index.get('pinned') or set(), policy, cfg['dimensions'])
    state = common.read_json(common.STATE / 'memory-maintenance.json', {}) or {}
    last = float(state.get('checked_at') or 0)
    interval = policy['check_interval_hours'] * 3600
    return {'enabled': bool(policy.get('enabled', True)),
            'backend': 'openviking',
            'policy': {'max_storage_mb': policy['max_storage_mb'], 'max_memories': policy['max_memories'],
                       'target_percent': policy['target_percent'],
                       'check_interval_hours': policy['check_interval_hours']},
            'capacity_bytes': plan['capacity_bytes'],
            'logical': {'bytes': plan['estimated_bytes'], 'records': plan['records'],
                        'dimensions': cfg['dimensions']},
            'physical': _workspace_bytes(),
            'over_capacity': plan['over'],
            'preview': {'candidates': len(plan['remove']),
                        'candidate_bytes': sum(_estimated_bytes(item['size'], cfg['dimensions'])
                                               for item in plan['remove']),
                        'pinned': plan['pinned'], 'unpinned': plan['unpinned'],
                        'records': plan['records'], 'records_after': plan['records_after'],
                        'estimated_bytes_after': plan['estimated_bytes_after'],
                        'target_bytes': plan['target_bytes'], 'target_records': plan['target_records']},
            'last': state or None, 'last_checked_at': last or None,
            'next_due_at': (last + interval) if last else None,
            'tombstones': _tombstone_count(),
            'generatedAt': time.time()}


def _tombstone_count():
    with journal() as con:
        return con.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0]


def _directory_levels(dir_uri, timeout=20):
    """Native L0/L1 for a directory; a placeholder is reported as unavailable."""
    result = {}
    for name, endpoint in (('l0', 'abstract'), ('l1', 'overview')):
        entry = {'available': False, 'text': None, 'uri': dir_uri, 'scope': 'directory',
                 'aggregate': True}
        try:
            text = request('/api/v1/content/' + endpoint + '?' +
                           urllib.parse.urlencode({'uri': dir_uri}), timeout=timeout)
            if isinstance(text, str) and text.strip():
                if _NOT_READY.search(text):
                    entry['reason'] = 'not ready'
                else:
                    entry.update(available=True, text=text)
        except ValueError as error:
            entry['error'] = common.redact(str(error))
        result[name] = entry
    return result


def _directory_detail(uri, scope=None):
    validate_memory_uri(uri, allow_root=True)
    index = _memory_index()
    journal_map = index.get('journal') or {}
    prefix = uri + '/'
    files = [file for file in index['files']
             if file['uri'].startswith(prefix) or file['uri'] == uri]
    if scope:
        files = [file for file in files
                 if _file_project(file, journal_map.get(file['uri'])) == scope]
    levels = _directory_levels(uri)
    levels['l2'] = {'available': False, 'text': None, 'uri': uri, 'reason': 'directory'}
    return {'id': uri, 'uri': uri, 'kind': 'directory',
            'category': memory_category(uri),
            'content': None, 'size': 0, 'records': len(files),
            'pinned': sum(1 for file in files
                          if _file_pinned(file, journal_map.get(file['uri']),
                                          index.get('pinned') or set())),
            'scope': scope, 'levels': levels, 'generatedAt': time.time()}


def detail(memory_id, scope=None):
    """Inspector payload: sanitized full body, provenance, and L0/L1/L2 levels.

    For a file, OpenViking's native summaries belong to its folder; the payload
    says so instead of presenting an aggregate folder summary as file-level
    content, and never invents a missing summary.
    """
    uri = validate_memory_uri(memory_id, allow_root=True)
    if uri == MEMORY_ROOT or not uri.endswith('.md'):
        return _directory_detail(uri, scope=scope)
    uri = validate_memory_file(uri)
    content, fields = read(uri)
    native = {'available': False}
    try:
        # attrs() already returns the unwrapped attrs object ({tags, memory}).
        native_attrs = attrs(uri)
        native = {'available': True,
                  'tags': [tag for tag in (native_attrs or {}).get('tags') or [] if isinstance(tag, str)],
                  'memory': (native_attrs or {}).get('memory') or {}}
    except ValueError as error:
        native['error'] = common.redact(str(error))
    parent = uri.rsplit('/', 1)[0]
    levels = _directory_levels(parent)
    levels['l2'] = {'available': True, 'text': None, 'uri': uri, 'chars': len(content)}
    return {'id': uri, 'uri': uri, 'kind': 'file', 'category': memory_category(uri),
            'displayName': fields.get('display_name') or uri.rsplit('/', 1)[-1],
            'content': content, 'size': len(content.encode('utf-8')),
            'memoryType': fields.get('memory_type') or memory_category(uri),
            'project': {'tag': fields.get('project'), 'path': fields.get('project_path'),
                        'name': fields.get('project_name')},
            'sessionId': fields.get('session_id'), 'legacyId': fields.get('legacy_id'),
            'origin': fields.get('origin') or 'openviking',
            # Native extraction provenance; OpenViking exposes no supported
            # reverse link from an extraction id to a session, so it is shown
            # as an id and never turned into a fabricated session association.
            'sourceId': (native.get('memory') or {}).get('source_extraction_id'),
            'createdAt': fields.get('created_at'), 'updatedAt': fields.get('updated_at'),
            'pinned': bool(fields.get('pinned')),
            'tags': native.get('tags') or [],
            'native': native, 'levels': levels, 'generatedAt': time.time()}


def _commit_row(task):
    result = task.get('result') if isinstance(task.get('result'), dict) else {}
    extracted = result.get('memories_extracted') if isinstance(result.get('memories_extracted'), dict) else {}
    written = sum(int(value) for value in extracted.values() if isinstance(value, (int, float)))
    error = task.get('error')
    return {'id': task.get('task_id'), 'status': task.get('status'), 'stage': task.get('stage'),
            'sessionId': task.get('resource_id'), 'updatedAt': _iso(task.get('updated_at')),
            'memories': written,
            'error': common.redact(str(error))[:240] if error else None}


def activity():
    """Truthful ingestion telemetry from the native task queue and sessions."""
    result = {'available': True,
              'queue': {'pending': 0, 'running': 0, 'cancelling': 0, 'failed': 0,
                        'completed': 0, 'total': 0},
              'recent': [], 'sessions': {'registered': 0}, 'indexed': None,
              'truncated': False, 'generatedAt': time.time()}
    try:
        tasks = request('/api/v1/tasks?' + urllib.parse.urlencode({'limit': 200}), timeout=30)
    except ValueError as error:
        result['available'] = False
        result['error'] = common.redact(str(error))
        return result
    if isinstance(tasks, list):
        result['truncated'] = len(tasks) >= 200
        commits = [task for task in tasks if isinstance(task, dict)
                   and task.get('task_type') == 'session_commit']
        for task in commits:
            status = str(task.get('status') or 'unknown')
            if status in result['queue']:
                result['queue'][status] += 1
            result['queue']['total'] += 1
        recent = sorted(commits, key=lambda task: float(task.get('updated_at') or 0), reverse=True)
        result['recent'] = [_commit_row(task) for task in recent[:5]]
    for name, path in (('sessions', '/api/v1/sessions'), ('indexed', '/api/v1/stats/memories')):
        try:
            value = request(path, timeout=30)
        except ValueError:
            continue
        if name == 'sessions' and isinstance(value, list):
            result['sessions']['registered'] = len(value)
        elif name == 'indexed' and isinstance(value, dict):
            result['indexed'] = value
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
