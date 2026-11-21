"""Construct historical version-one records for decoder/replay tests only.

Production task creation no longer accepts retired roles. These fixtures exercise
the existing archived record format without reintroducing an active role API.
"""
import os
from pathlib import Path
from uuid import uuid4
from retro_specialists.task_context import _payload, _digest, _encoded


def create_legacy_task_fixture(state_dir, target, role, requested_count, max_depth,
                               max_total_reactions, baseline_routes=None,
                               source_sha256=None, inventory=None, baseline_total_count=None):
    payload = _payload(target, role, requested_count, max_depth, max_total_reactions,
                       baseline_routes, source_sha256, inventory, baseline_total_count)
    unsigned = {**payload, 'task_id': uuid4().hex}
    result = {**unsigned, 'content_sha256': _digest(unsigned)}
    folder = Path(state_dir) / 'tasks'
    folder.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(folder / (result['task_id'] + '.json'),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(_encoded(result))
    return result
