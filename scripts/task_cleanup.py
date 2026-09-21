"""Safe reclamation for terminal delegated tasks.

The task database and OpenCode session lifecycle are coordinated by management.
This module only removes paths owned by one validated task under the private state
root. It never follows a task-supplied path.
"""
import os
from pathlib import Path
import shutil

import common


def tree_size(path):
    total = 0
    if not path.exists() or path.is_symlink():
        return total
    for base, _, files in os.walk(str(path), followlinks=False):
        for name in files:
            item = Path(base) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                pass
    return total


def _artifact_root(task_id):
    common.task_path(task_id)  # validates the identifier
    root = common.STATE / 'artifacts'
    path = root / task_id
    if path.parent.resolve() != root.resolve() or path.is_symlink():
        raise ValueError('Unsafe task artifact path')
    return path


def clear_artifacts(task_id, preserve_integration=False):
    """Remove task evidence, retaining the minimum needed to integrate code.

    A retained isolated worktree keeps its baseline, after manifest, patch,
    summary and result. Completed/shared tasks need none of those once their
    compact usage ledger has been written, so their artifact directory is
    removed completely.
    """
    path = _artifact_root(task_id)
    if not path.exists():
        return {'removed': False, 'bytes': 0, 'preserved': []}
    before = tree_size(path)
    if not preserve_integration:
        shutil.rmtree(str(path))
        return {'removed': True, 'bytes': before, 'preserved': []}
    removable = ('before', 'after', 'messages.json', 'pending.json')
    for name in removable:
        item = path / name
        if item.is_symlink() or item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(str(item))
    after = tree_size(path)
    preserved = [name for name in ('baseline.json', 'after.json', 'changes.patch',
                                   'summary.md', 'result.json') if (path / name).exists()]
    return {'removed': before != after, 'bytes': max(0, before - after),
            'preserved': preserved}
