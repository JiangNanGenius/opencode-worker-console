"""Bounded, resumable migration from the legacy opencode-mem store to OpenViking.

Protocol:
  1. `backup()` writes a consistent page-level snapshot of every legacy SQLite
     database (WAL included) before any import.
  2. `run()` reads live memories through the legacy loopback API, writes each as
     an OpenViking memory file, and records `legacy-id -> viking-uri` in the
     bridge journal after every chunk, so an interrupted run resumes without
     duplicating records.
  3. `reconcile_deletions()` removes records that were deleted in the legacy
     store while migration ran, without touching records owned by OpenViking.
  4. A final delta pass after capture cutover imports anything captured in the
     meantime; tombstones keep deleted records deleted.
"""
import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import common
import memory_manager
import openviking_backend as backend
import openviking_service


def legacy_storage():
    memory_manager.init_backend()
    cfg = memory_manager.settings(common.config().get('memory'))
    return Path(cfg['storage_path'])


def backup_root():
    return openviking_service.state_dir() / 'migration'


def latest_backup():
    root = backup_root()
    if not root.is_dir():
        return None
    candidates = sorted((path for path in root.iterdir()
                         if path.is_dir() and (path / 'manifest.json').exists()), reverse=True)
    return str(candidates[0]) if candidates else None


def backup(dest=None, now=None):
    """Consistent backup of the legacy store; returns the backup directory."""
    source = legacy_storage()
    if not source.is_dir():
        raise ValueError('Legacy memory storage was not found')
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))
    target = Path(dest) if dest else backup_root() / ('backup-' + stamp)
    with common.locked('memory-backup'):
        if target.exists():
            raise ValueError('Backup directory already exists: ' + str(target))
        target.mkdir(parents=True, mode=0o700)
        databases = []
        for path in sorted(source.rglob('*.db')):
            relative = str(path.relative_to(source))
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as src, \
                     closing(sqlite3.connect(str(destination))) as dst:
                    with dst:
                        src.backup(dst)
                    # A snapshot is a self-contained DB, not a live WAL writer.
                    dst.execute('PRAGMA journal_mode=DELETE')
                # macOS SQLite may retain its now-unused shared-memory sidecar
                # after closing the snapshot connection. DELETE mode has no WAL.
                for suffix in ('-wal', '-shm'):
                    Path(str(destination) + suffix).unlink(missing_ok=True)
                databases.append(relative)
            except sqlite3.Error as error:
                raise ValueError('Consistent legacy backup failed for ' + relative) from error
        for path in sorted(source.rglob('*')):
            # SQLite backup already contains committed WAL pages. Copying the
            # old writer's WAL alongside that new DB can corrupt a restoration.
            if path.is_file() and path.suffix != '.db' and not path.name.endswith(('-wal', '-shm', '-journal')):
                destination = target / str(path.relative_to(source))
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(path, destination)
        manifest = {'created_at': time.time(),
                    'source': str(source),
                    'databases': databases,
                    'files': sum(1 for _ in target.rglob('*') if _.is_file())}
        common.write_json(target / 'manifest.json', manifest)
        return {'path': str(target), **manifest}


def legacy_items_from_disk():
    """Read the stopped legacy store, including WAL, without its old HTTP server.

    This is an explicit operator path for the final disposal/capture delta, not
    an automatic fallback for an API/authentication failure. An unreadable shard
    fails the whole read so reconciliation cannot mistake partial data for deletion.
    """
    storage = legacy_storage()
    if not storage.is_dir():
        raise ValueError('Legacy memory storage was not found')
    databases = sorted(storage.rglob('*.db'))
    items, ids, shards = [], set(), 0
    columns = ('id', 'content', 'type', 'tags', 'created_at', 'updated_at',
               'metadata', 'display_name', 'project_path', 'project_name', 'is_pinned')
    for path in databases:
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as con:
                con.row_factory = sqlite3.Row
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone():
                    continue
                shards += 1
                for row in con.execute('SELECT ' + ','.join(columns) + ',container_tag FROM memories'):
                    tag = row['container_tag'] or ''
                    if '_project_' not in tag and '_user_' not in tag:
                        continue
                    if row['id'] in ids:
                        raise ValueError('Duplicate legacy memory id across shards')
                    ids.add(row['id'])
                    items.append({'id': row['id'], 'type': 'memory', 'content': row['content'],
                                  'memoryType': row['type'], 'tags': row['tags'],
                                  'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
                                  'metadata': row['metadata'], 'displayName': row['display_name'],
                                  'projectPath': row['project_path'], 'projectName': row['project_name'],
                                  'isPinned': row['is_pinned'] == 1})
        except sqlite3.Error as error:
            raise ValueError('Legacy shard could not be read: ' + path.name) from error
    if not shards:
        raise ValueError('No readable legacy memory shards were found')
    return items


def legacy_items(page_size=200, offline=False):
    """All live legacy memories through the loopback API (prompts excluded)."""
    if offline:
        return legacy_items_from_disk()
    items, page = [], 1
    while True:
        result = memory_manager.legacy_request(
            '/api/memories?' + '&'.join(['page=%d' % page, 'pageSize=%d' % page_size,
                                         'includePrompts=false']))
        batch = list(result.get('items') or [])
        items.extend(item for item in batch if isinstance(item, dict) and item.get('type') != 'prompt')
        pages = int(result.get('totalPages') or 1)
        if page >= pages or not batch:
            break
        page += 1
    return items


def _legacy_tags(item):
    raw = item.get('tags')
    if isinstance(raw, list):
        return [str(value) for value in raw]
    if isinstance(raw, str) and raw.strip().startswith('['):
        try:
            parsed = json.loads(raw.replace("'", '"'))
            if isinstance(parsed, list):
                return [str(value) for value in parsed]
        except ValueError:
            pass
    return []


def _legacy_metadata(item):
    raw = item.get('metadata')
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith('{'):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _epochs(item):
    created = backend._epoch(item.get('createdAt'))
    updated = backend._epoch(item.get('updatedAt')) or created
    now = time.time()
    if not created or created > now + 86400:
        created = now
    return created, max(created, updated)


def _project_for(item):
    path = item.get('projectPath')
    name = item.get('projectName')
    if isinstance(path, str) and path and Path(path).exists():
        try:
            info = memory_manager.project_info(path)
            return info['tag'], info.get('projectPath') or path, name or info.get('projectName')
        except ValueError:
            pass
    if isinstance(path, str) and path:
        return None, path, name
    return None, None, name


def _target_uri(item, category):
    name = item.get('displayName') or item.get('projectName') or (item.get('content') or '')[:48]
    short = hashlib.sha256(str(item.get('id') or '').encode()).hexdigest()[:8]
    return '{}/{}/{}-{}.md'.format(backend.MEMORY_ROOT, category, backend.slug(name), short)


def _record(item):
    """Build the OpenViking write and journal row for one legacy memory."""
    content = str(item.get('content') or '').strip()
    if not content:
        return None
    content, _ = backend.parse_memory(content)
    created, updated = _epochs(item)
    project, project_path, project_name = _project_for(item)
    metadata = _legacy_metadata(item)
    legacy_type = str(item.get('memoryType') or item.get('type') or '').strip()
    category = backend._category(legacy_type)
    fields = {'memory_type': category,
              'created_at': backend._iso(created),
              'updated_at': backend._iso(updated),
              'pinned': bool(item.get('isPinned')),
              'origin': 'legacy-migration',
              'legacy_id': str(item.get('id')),
              'legacy_type': legacy_type or None,
              'project': project or None,
              'project_path': project_path or None,
              'project_name': project_name or None,
              'display_name': item.get('displayName') or None,
              'session_id': metadata.get('sessionID') or metadata.get('session_id') or None,
              'prompt_id': metadata.get('promptId') or None}
    tags = [backend.sanitize_tag('origin=legacy-migration')]
    tags += [backend.sanitize_tag('legacy_type=' + legacy_type)] if legacy_type else []
    tags += [backend.sanitize_tag(tag) for tag in _legacy_tags(item)]
    tags += [backend.sanitize_tag('project=' + project)] if project else []
    if item.get('isPinned'):
        tags.append('pinned=true')
    uri = _target_uri(item, category)
    content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return {'id': str(item.get('id')), 'uri': uri, 'project_tag': project,
            'project_path': project_path, 'project_name': project_name,
            'category': category, 'memory_type': legacy_type or category,
            'created_at': created, 'updated_at': updated,
            'size': len(content.encode('utf-8')), 'pinned': 1 if item.get('isPinned') else 0,
            'origin': 'legacy-migration',
            'session_id': fields['session_id'], 'content_hash': content_hash,
            'content': backend.compose(content, fields),
            'tags': [tag for tag in tags if tag]}


def plan(offline=False):
    if not openviking_service.probe()['ready']:
        raise ValueError('OpenViking is not ready; start it before migrating')
    items = legacy_items(offline=offline)
    with backend.journal() as con:
        mapped = {row[0] for row in con.execute("SELECT id FROM records WHERE origin='legacy-migration'")}
        tombstones = {row[0] for row in con.execute('SELECT id FROM tombstones')}
    pending = [item for item in items if isinstance(item, dict) and item.get('id')
               and str(item.get('id')) not in mapped and str(item.get('id')) not in tombstones]
    return {'live': len(items), 'mapped': len(mapped), 'tombstoned': len(tombstones),
            'pending': len(pending), 'items': pending}


def reconcile_deletions(offline=False):
    """Tombstone and remove migrated records that no longer exist in the legacy store."""
    live = {str(item.get('id')) for item in legacy_items(offline=offline)}
    removed = []
    with backend.journal() as con:
        rows = con.execute("SELECT id, uri FROM records WHERE origin='legacy-migration'").fetchall()
    for legacy_id, uri in rows:
        if legacy_id in live:
            continue
        try:
            backend.delete(uri)
        except ValueError:
            backend._tombstone(legacy_id, uri, 'legacy-missing')
        backend._journal_delete(legacy_id)
        removed.append({'id': legacy_id, 'uri': uri})
    return {'removed': removed, 'count': len(removed)}


def run(limit=None, batch=50, dry_run=False, offline=False):
    """Import pending legacy memories; journal after every chunk for resumption."""
    state = plan(offline=offline)
    pending = state['items'] if limit is None else state['items'][:int(limit)]
    report = {'planned': len(state['items']), 'attempted': len(pending), 'imported': 0,
              'failed': [], 'skipped': 0, 'live': state['live'], 'mapped': state['mapped'],
              'tombstoned': state['tombstoned'], 'dry_run': bool(dry_run)}
    if dry_run:
        report['sample'] = [{'id': item.get('id'), 'projectPath': item.get('projectPath')}
                            for item in pending[:5]]
        return report
    failures = backend.meta_get('migration.failures', []) or []
    chunk = max(1, int(batch))
    for start in range(0, len(pending), chunk):
        records = [record for record in (_record(item) for item in pending[start:start + chunk]) if record]
        groups = {}
        for record in records:
            groups.setdefault(record['category'], []).append(record)
        for category, group in groups.items():
            operations = [{'uri': record['uri'], 'content': record['content'],
                           'mode': 'replace'} for record in group]
            try:
                backend.request('/api/v1/content/batch-write', 'POST',
                                {'root_uri': backend.MEMORY_ROOT + '/' + category,
                                 'operations': operations, 'wait': True, 'timeout': 600}, timeout=900)
                written = group
            except ValueError:
                # Fall back to individual writes so one bad record cannot fail a chunk.
                written, failed = [], []
                for record in group:
                    try:
                        backend.write(record['uri'], record['content'], tags=record['tags'], wait=True)
                        written.append(record)
                    except ValueError as error:
                        failed.append({'id': record['id'], 'error': common.redact(str(error))})
                failures.extend(failed)
                report['failed'].extend(failed)
            for record in written:
                # This server version's batch API has no per-operation tags; apply
                # explicit retrieval tags separately (no model calls involved).
                try:
                    backend.request('/api/v1/fs/attrs/set_tags', 'POST',
                                    {'uri': record['uri'], 'tags': record['tags'], 'mode': 'replace'},
                                    timeout=60)
                except ValueError as error:
                    failures.append({'id': record['id'], 'error': 'tags: ' + common.redact(str(error))})
                    report['failed'].append(failures[-1])
                backend._journal_upsert({key: value for key, value in record.items()
                                         if key not in ('content', 'tags')})
                report['imported'] += 1
            backend.meta_set('migration.failures', failures[-200:])
            backend.meta_set('migration', {'updated_at': time.time(), 'status': 'running',
                                           'imported': report['imported'], 'planned': report['planned'],
                                           'live': report['live'], 'failed': len(failures)})
    backend._invalidate()
    final = plan(offline=offline)
    summary = {'updated_at': time.time(), 'status': 'complete' if final['pending'] == 0 else 'partial',
               'imported': report['imported'], 'planned_total': final['live'],
               'mapped': final['mapped'], 'pending': final['pending'],
               'tombstoned': final['tombstoned'], 'failed': len(failures),
               'last_failures': failures[-20:]}
    backend.meta_set('migration', summary)
    report['pending_after'] = final['pending']
    report['status'] = summary['status']
    return report


def verify(sample=25, offline=False):
    """Count and representative-body verification for the migration."""
    live_items = {str(item.get('id')): item for item in legacy_items(offline=offline) if isinstance(item, dict)}
    with backend.journal() as con:
        rows = con.execute("SELECT id, uri, content_hash, project_tag FROM records "
                           "WHERE origin='legacy-migration'").fetchall()
        tombstones = {row[0] for row in con.execute('SELECT id FROM tombstones')}
    mapped = {row[0]: {'uri': row[1], 'hash': row[2], 'project': row[3]} for row in rows}
    missing = [legacy_id for legacy_id in live_items if legacy_id not in mapped and legacy_id not in tombstones]
    checked, mismatched = 0, []
    ranked = sorted(mapped.items(), key=lambda pair: (pair[1]['project'] or '', pair[0]))
    step = max(1, len(ranked) // max(1, sample))
    for legacy_id, record in ranked[::step][:sample]:
        item = live_items.get(legacy_id)
        if not item:
            continue
        content, _ = backend.parse_memory(str(item.get('content') or '').strip())
        expected = hashlib.sha256(content.encode('utf-8')).hexdigest()
        try:
            remote, _ = backend.read(record['uri'])
        except ValueError as error:
            mismatched.append({'id': legacy_id, 'error': common.redact(str(error))})
            continue
        checked += 1
        if hashlib.sha256(remote.encode('utf-8')).hexdigest() != expected:
            mismatched.append({'id': legacy_id, 'uri': record['uri'], 'reason': 'content-hash'})
    with backend.journal() as con:
        projects = con.execute("SELECT project_tag, COUNT(*) FROM records WHERE origin='legacy-migration' "
                               "GROUP BY project_tag ORDER BY 2 DESC").fetchall()
    live_count = len(live_items)
    report = {'legacy_live': live_count, 'mapped': len(mapped), 'tombstoned': len(tombstones),
              'unmigrated': len(missing), 'checked': checked, 'mismatched': mismatched,
              'projects': [{'tag': tag, 'count': count} for tag, count in projects],
              'sample_paths': {legacy_id: mapped[legacy_id]['uri'] for legacy_id in list(mapped)[:3]}}
    report['ok'] = not missing and not mismatched and len(mapped) + len(tombstones) >= live_count
    return report


def status():
    with backend.journal() as con:
        total = con.execute("SELECT COUNT(*) FROM records WHERE origin='legacy-migration'").fetchone()[0]
        tombstones = con.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0]
    return {'migrated': total, 'tombstones': tombstones, 'journal': str(backend.journal_path()),
            'meta': backend.meta_get('migration', {}),
            'failures': backend.meta_get('migration.failures', [])[-10:]}


def _backend_config(target):
    """Atomically flip memory.backend while preserving every other configuration field."""
    path = common.CONFIG
    with common.locked():
        configured = common.read_json(path, {}) or {}
        memory = configured.get('memory') if isinstance(configured.get('memory'), dict) else {}
        if memory.get('backend') == target:
            return {'changed': False, 'backend': target, 'config': str(path)}
        memory['backend'] = target
        configured['memory'] = memory
        configured['restart_required'] = True
        common.write_json(path, configured)
    return {'changed': True, 'backend': target, 'config': str(path)}


def cutover(apply=False, rollback=False, skip_delta=False, offline=False):
    """Switch capture and management to OpenViking (or back) with a safe delta pass.

    The managed OpenCode server must be restarted for the plugin swap; this
    command verifies the environment, flips the configuration atomically and
    performs the delta import/verification around the restart. Restart itself is
    left to `service.reload_runtime`, which refuses while tasks or sessions are
    active, so an operator runs this at a safe boundary.
    """
    if apply and rollback:
        raise ValueError('Choose either --apply or --rollback')
    if not apply and not rollback:
        configured = common.read_json(common.CONFIG, {}) or {}
        memory = configured.get('memory') if isinstance(configured.get('memory'), dict) else {}
        return {'backend': memory.get('backend') or 'opencode-mem',
                'restart_required': bool(configured.get('restart_required')),
                'journal': backend.meta_get('migration', {})}
    if not openviking_service.probe()['ready']:
        raise ValueError('OpenViking is not ready; keep the current backend until it is')
    if not skip_delta and not rollback:
        reconcile = reconcile_deletions(offline=offline)
        delta = run(batch=50, offline=offline)
        verification = verify(sample=25, offline=offline)
        if not verification['ok']:
            raise ValueError('Migration verification failed; keeping the current backend')
    else:
        reconcile, delta, verification = {'count': 0}, None, None
    result = _backend_config('openviking' if apply else 'opencode-mem')
    result.update({'delta': delta, 'verification': verification, 'reconcile': reconcile,
                   'restart': 'Run `delegate-opencode service` restart at an idle boundary '
                              'to apply the plugin and backend change'})
    return result
