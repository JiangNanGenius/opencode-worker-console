"""Cooperative file ownership, task baselines, and explicit worktree integration."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import time
from common import ACTIVE, STATE, artifact_dir, read_json, write_json

MAX_SNAPSHOT = 256 * 1024 * 1024


def run(argv, cwd, check=True, data=None):
    p = subprocess.run(argv, cwd=str(cwd), input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(argv[:3]) + '; inspect the workspace')
    return p


def git_root(directory):
    p = run(['git', 'rev-parse', '--show-toplevel'], directory, check=False)
    return Path(p.stdout.decode().strip()).resolve() if p.returncode == 0 else None


def relative_scope(value, root):
    p = Path(value)
    if p.is_absolute() or '..' in p.parts or any(x in value for x in '*?[]'):
        raise ValueError('Scopes must be literal relative files/directories, without .. or wildcards')
    p = (root / p).resolve()
    try:
        rel = p.relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ValueError('Scope escapes workspace') from None
    if '.git' in Path(rel).parts:
        raise ValueError('Git metadata is not a writable scope')
    return rel or '.'


def covers(scope, relative):
    return scope == '.' or relative == scope or relative.startswith(scope.rstrip('/') + '/')


def overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def conflicts(a, b):
    if set(a.get('resources', [])) & set(b.get('resources', [])):
        return True
    # Writes naming the same operational target mutate one remote system, which
    # local worktree isolation cannot partition, so serialize them like resources.
    if a['mode'] == 'write' and b['mode'] == 'write' and \
            set(a.get('targets', [])) & set(b.get('targets', [])):
        return True
    if a['workspace'] == 'isolated' or b['workspace'] == 'isolated':
        return False
    if a['mode'] != 'write' or b['mode'] != 'write':
        return False
    return any(overlap(Path(a['source_dir']) / x, Path(b['source_dir']) / y)
               for x in a['scopes'] for y in b['scopes'])


def source_files(root, scopes):
    if git_root(root) == root:
        raw = run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], root).stdout
        names = set(os.fsdecode(x) for x in raw.split(b'\0') if x)
        # Explicit ignored files are allowed only when individually scoped.
        names.update(s for s in scopes if (root / s).is_file())
    else:
        names = set()
        for scope in scopes:
            p = root / scope
            if p.is_file() or p.is_symlink():
                names.add(scope)
            elif p.is_dir():
                for base, dirs, files in os.walk(p, followlinks=False):
                    dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', '.venv', '__pycache__'}]
                    names.update((Path(base) / f).relative_to(root).as_posix() for f in files)
    return sorted(n for n in names if any(covers(s, n) for s in scopes))


def signature(p):
    if p.is_symlink():
        return {'symlink': os.readlink(p)}
    if not p.is_file():
        return None
    return {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'mode': p.stat().st_mode & 0o777}


def snapshot(root, scopes, destination):
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest, total = {}, 0
    for name in source_files(root, scopes):
        p = root / name
        try:
            p.parent.resolve().relative_to(root.resolve())
        except ValueError:
            raise ValueError('Snapshot path parent escapes workspace: ' + name) from None
        if not p.exists() and not p.is_symlink():
            continue
        # Snapshot symlinks as links, never follow them into unrelated directories.
        total += 0 if p.is_symlink() else p.stat().st_size
        if total > MAX_SNAPSHOT:
            raise ValueError('Baseline exceeds 256 MiB; narrow scopes before delegation')
        manifest[name] = signature(p)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink():
            target.symlink_to(os.readlink(p))
        else:
            shutil.copy2(p, target)
    return manifest


def git_status(root):
    if not git_root(root):
        return None
    return run(['git', 'status', '--porcelain=v1', '-z', '--untracked-files=normal'], root).stdout.decode(errors='replace')


def prepare(t):
    root = Path(t['source_dir'])
    dest = root
    art = artifact_dir(t['id'])
    metadata = {'source_dir': str(root), 'source_status': git_status(root), 'created_at': time.time()}
    if t['workspace'] == 'isolated':
        if git_root(root) != root:
            raise ValueError('Isolated tasks require a Git repository root')
        if run(['git', 'ls-files', '-u'], root).stdout:
            raise ValueError('Resolve existing merge conflicts before creating an isolated task')
        if (root / '.gitmodules').exists():
            raise ValueError('Delegate a submodule as its own repository; automatic submodule copying is disabled')
        dest = STATE / 'worktrees' / t['id']
        if dest.exists():
            raise ValueError('Partial worktree exists; inspect it before resubmitting')
        head = run(['git', 'rev-parse', 'HEAD'], root).stdout.decode().strip()
        patch = run(['git', 'diff', '--binary', 'HEAD', '--'], root).stdout
        untracked = run(['git', 'ls-files', '--others', '--exclude-standard', '-z'], root).stdout
        names = [os.fsdecode(x) for x in untracked.split(b'\0') if x]
        before = {n: signature(root / n) for n in names}
        if len(patch) + sum((root / n).lstat().st_size for n in names) > MAX_SNAPSHOT:
            raise ValueError('Uncommitted baseline exceeds 256 MiB; narrow the source snapshot first')
        run(['git', 'worktree', 'add', '--detach', str(dest), head], root)
        if patch:
            run(['git', 'apply', '--binary', '-'], dest, data=patch)
        for name in names:
            src, dst = root / name, dest / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                dst.symlink_to(os.readlink(src))
            else:
                shutil.copy2(src, dst)
        if patch != run(['git', 'diff', '--binary', 'HEAD', '--'], root).stdout or any(signature(root / n) != s for n, s in before.items()):
            raise ValueError('Source changed while preparing worktree; preserved worktree requires review')
        metadata.update(base_head=head, worktree=str(dest))
    if t['mode'] == 'write':
        metadata['files'] = snapshot(dest, t['scopes'], art / 'before')
    else:
        metadata['files'] = {}
    write_json(art / 'baseline.json', metadata)
    return str(dest)


def collect_changes(t, edited_paths=None):
    art = artifact_dir(t['id'])
    baseline = read_json(art / 'baseline.json', {})
    root = Path(t['directory'])
    outside = []
    for name in edited_paths or []:
        p = Path(name)
        if p.is_absolute():
            try:
                name = p.relative_to(root).as_posix()
            except ValueError:
                outside.append(str(p))
                continue
        if not any(covers(s, name) for s in t['scopes']):
            outside.append(name)
    if t['mode'] != 'write':
        return {'changed_files': [], 'outside_scope_tool_edits': outside}
    after_dir = art / 'after'
    if after_dir.exists():
        shutil.rmtree(after_dir)  # Own regenerable snapshot only; never a user workspace.
    after = snapshot(root, t['scopes'], after_dir)
    before = baseline['files']
    changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    p = run(['git', 'diff', '--no-index', '--binary', '--', 'before', 'after'], art, check=False)
    if p.returncode not in (0, 1):
        raise RuntimeError('Could not generate task patch')
    (art / 'changes.patch').write_bytes(p.stdout)
    write_json(art / 'after.json', after)
    return {'changed_files': changed, 'outside_scope_tool_edits': sorted(set(outside)),
            'patch': str(art / 'changes.patch'), 'after_status': git_status(root),
            'attribution': 'Scoped before/after delta; concurrent external edits still require Astra review'}


def integrate(t, apply=False):
    if t['workspace'] != 'isolated' or t['status'] != 'completed' or t.get('review_required'):
        raise ValueError('Only completed isolated tasks without outstanding flags may be integrated')
    art, root = artifact_dir(t['id']), Path(t['source_dir'])
    before = read_json(art / 'baseline.json')['files']
    after = read_json(art / 'after.json')
    changed = [n for n in set(before) | set(after) if before.get(n) != after.get(n)]
    for n in changed:
        if signature(root / n) != before.get(n):
            raise ValueError('Source changed since baseline: ' + n)
    patch = (art / 'changes.patch').read_bytes()
    if not patch:
        return {'applied': False, 'changed_files': [], 'check': 'empty_patch'}
    run(['git', 'apply', '--check', '--binary', '-p2', '-'], root, data=patch)
    if apply:
        run(['git', 'apply', '--binary', '-p2', '-'], root, data=patch)
    return {'applied': apply, 'changed_files': changed, 'check': 'passed',
            'note': 'Astra must inspect the diff before --apply and validate the integrated result afterwards'}
