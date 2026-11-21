"""Host-created immutable task handoffs; never expose creation as an LLM tool.

Digests detect changed content, not malicious filesystem-owner forgery. The host
owns this directory and selects source artifacts; molecular text supplies no paths.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .contracts import positive_limit
from .graphs import canonical_smiles, route_graph, validate_response

MAX_TASK_BYTES = 2_000_000
_ROLES = {'route_proposer', 'route_repair', 'cost_reducer'}
_BASE = {'schema_version', 'task_id', 'role', 'target', 'constraints', 'inventory', 'content_sha256'}
_BASELINE = {'baseline_routes', 'baseline_provenance'}
_PROPOSAL = {'proposal_requirements'}


def _encoded(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > MAX_TASK_BYTES:
        raise ValueError('Task context exceeds byte limit')
    return raw


def _digest(value):
    return sha256(_encoded(value)).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('Task identifier must be 32 lowercase hexadecimal characters')
    return value


def _inventory(values):
    if values is None:
        return {'status': 'unknown', 'molecules': [],
                'meaning': 'Inventory was not supplied; membership and commercial availability are unknown.'}
    if not isinstance(values, (list, tuple)) or len(values) > 10000:
        raise ValueError('Inventory must be a bounded molecular structure list')
    return {'status': 'configured_membership_set', 'molecules': sorted({canonical_smiles(v) for v in values}),
            'meaning': 'Host-supplied membership set only; catalog membership does not establish chemical feasibility.'}


def _baseline(routes, target, source_sha256, total):
    if not isinstance(routes, (list, tuple)) or len(routes) > 200:
        raise ValueError('Repair/cost task requires a bounded explicit baseline route list')
    if source_sha256 is not None and (not isinstance(source_sha256, str) or not re.fullmatch('[a-f0-9]{64}', source_sha256)):
        raise ValueError('Source artifact SHA-256 must be lowercase hexadecimal')
    if total is not None and (type(total) is not int or not len(routes) <= total <= 1_000_000):
        raise ValueError('Baseline total count cannot be smaller than the selected subset')
    canonical, identifiers, graphs = [], set(), set()
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError('Baseline route must be an object')
        identifier = route.get('route_id')
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128 or identifier in identifiers:
            raise ValueError('Baseline route identifiers must be bounded and unique')
        graph = route_graph({'route_id': identifier, 'reactions': route.get('reactions')}, target, 100, 200)
        if graph['graph_key'] in graphs:
            raise ValueError('Duplicate baseline reaction connectivity')
        identifiers.add(identifier); graphs.add(graph['graph_key'])
        canonical.append({key: graph[key] for key in ('route_id', 'target', 'reactions', 'graph_key', 'depth', 'total_reactions')})
    provenance = {'source_sha256': source_sha256, 'connectivity_sha256': _digest(sorted(graphs)),
                  'selected_route_count': len(canonical), 'total_route_count': total,
                  'meaning': 'Host-selected canonical baseline subset; source digest identifies an asserted original artifact, not verification of its contents. Unknown total count remains unknown.'}
    return {'baseline_routes': canonical, 'baseline_provenance': provenance}


def _payload(target, role, requested_count, max_depth, max_total_reactions,
             baseline_routes, source_sha256, inventory, baseline_total_count,
             require_stereo_plan=False, require_origin_declarations=False):
    if not isinstance(role, str) or role.replace('-', '_') not in _ROLES:
        raise ValueError('Unknown specialist role')
    role = role.replace('-', '_')
    limits = {'requested_count': positive_limit(requested_count, 'requested_count', 20),
              'max_depth': positive_limit(max_depth, 'max_depth', 30),
              'max_total_reactions': positive_limit(max_total_reactions, 'max_total_reactions', 100)}
    result = {'schema_version': 1, 'target': canonical_smiles(target), 'role': role,
              'constraints': limits, 'inventory': _inventory(inventory)}
    if role == 'route_proposer':
        if baseline_routes is not None or source_sha256 is not None or baseline_total_count is not None:
            raise ValueError('Independent proposer task must not receive model-search baseline data')
        if require_stereo_plan:
            from .stereochemistry import target_stereo_requirements
            return {**result, 'schema_version': 3 if require_origin_declarations else 2,
                    'proposal_requirements': {'stereo_plan_required': True,
                        **({'origin_declarations_required': True} if require_origin_declarations else {}),
                        'target_stereo': target_stereo_requirements(result['target'])}}
        return result
    return {**result, **_baseline(baseline_routes, result['target'], source_sha256, baseline_total_count)}


def _checked(value):
    if not isinstance(value, dict):
        raise ValueError('Task context must be an object')
    role = value.get('role')
    version = value.get('schema_version')
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError('Unsupported task context schema version')
    expected = _BASE if role == 'route_proposer' else _BASE | _BASELINE
    if version >= 2:
        if role != 'route_proposer':
            raise ValueError('Only the active proposer supports stereo-aware task versions')
        expected = expected | _PROPOSAL
    if set(value) != expected:
        raise ValueError('Unexpected or missing task context fields')
    _identifier(value['task_id'])
    unsigned = {key: item for key, item in value.items() if key != 'content_sha256'}
    if value['content_sha256'] != _digest(unsigned):
        raise ValueError('Task context digest mismatch')
    limits, inventory = value['constraints'], value['inventory']
    if not isinstance(limits, dict) or set(limits) != {'requested_count', 'max_depth', 'max_total_reactions'}:
        raise ValueError('Unexpected task limits')
    if not isinstance(inventory, dict) or set(inventory) != {'status', 'molecules', 'meaning'}:
        raise ValueError('Unexpected inventory fields')
    supplied = None if inventory['status'] == 'unknown' else inventory['molecules']
    provenance = value.get('baseline_provenance', {})
    if not isinstance(provenance, dict):
        raise ValueError('Unexpected baseline provenance')
    rebuilt = _payload(value['target'], role, **limits, baseline_routes=value.get('baseline_routes'),
        source_sha256=provenance.get('source_sha256'), inventory=supplied,
        baseline_total_count=provenance.get('total_route_count'),
        require_stereo_plan=version >= 2, require_origin_declarations=version >= 3)
    if _encoded(unsigned) != _encoded({**rebuilt, 'task_id': value['task_id']}):
        raise ValueError('Task context is not canonical or contains unsupported fields')
    return json.loads(_encoded(value))


def _directory(state_dir, create=False):
    """Open every directory component without following any symlink."""
    path = Path(state_dir)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Task state directory must be an absolute trusted path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        if create:
            try:
                os.mkdir('tasks', mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
        child = os.open('tasks', flags, dir_fd=descriptor)
        os.close(descriptor); descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Task directory must be owner-only')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_task_context(state_dir, target, role, requested_count, max_depth,
                        max_total_reactions, baseline_routes=None, source_sha256=None,
                        inventory=None, baseline_total_count=None, *, require_stereo_plan=True):
    """Create an exclusive host-owned record; return its ID, digest and context.

    source_sha256 is a host attestation of the original artifact, not a digest of
    this possibly selected subset. Candidate limits do not truncate the baseline.
    """
    if not isinstance(role, str) or role.replace('-', '_') != 'route_proposer':
        raise ValueError('Retired or inactive specialist role; only route_proposer can receive new tasks')
    if type(require_stereo_plan) is not bool:
        raise ValueError('Stereo requirement policy must be a host boolean')
    _encoded({'baseline': baseline_routes, 'inventory': inventory})
    payload = _payload(target, role, requested_count, max_depth, max_total_reactions,
        baseline_routes, source_sha256, inventory, baseline_total_count, require_stereo_plan,
        require_origin_declarations=require_stereo_plan)
    unsigned = {**payload, 'task_id': uuid4().hex}
    value = {**unsigned, 'content_sha256': _digest(unsigned)}
    raw = _encoded(value)
    directory = _directory(state_dir, create=True)
    try:
        fd = os.open(value['task_id'] + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    finally:
        os.close(directory)
    return json.loads(raw)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate task JSON key')
        result[key] = value
    return result


def read_task_context(state_dir, task_id):
    """Read exact immutable host context; reject traversal, tampering and aliases."""
    _identifier(task_id)
    directory = _directory(state_dir)
    try:
        fd = os.open(task_id + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_TASK_BYTES):
                raise ValueError('Task record must be a bounded owner-only regular file')
            raw = stream.read(MAX_TASK_BYTES + 1)
    finally:
        os.close(directory)
    if len(raw) > MAX_TASK_BYTES:
        raise ValueError('Task context exceeds byte limit')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        checked = _checked(value)
    except (UnicodeError, TypeError, KeyError, RecursionError) as error:
        raise ValueError('Malformed task context') from error
    if checked['task_id'] != task_id:
        raise ValueError('Task identifier does not match its immutable record')
    return checked


def validate_task_candidates(context, raw_response):
    """Validate against a freshly read context, never child-selected constraints."""
    checked = _checked(context)
    result = validate_response(raw_response, checked['target'], **checked['constraints'],
                               inventory=checked['inventory']['molecules'],
                               require_stereo_plan=checked['schema_version'] >= 2,
                               require_origin_declarations=checked['schema_version'] >= 3)
    return {**result, 'task_id': checked['task_id'], 'task_content_sha256': checked['content_sha256'],
            'inventory_status': checked['inventory']['status']}
