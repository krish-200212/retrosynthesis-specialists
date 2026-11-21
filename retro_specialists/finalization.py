"""Accept native final routes only against a successful backend validation receipt."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from .contracts import parse_response
from .graphs import canonical_smiles, validate_response
from .task_context import read_task_context, validate_task_candidates


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _fingerprint(route):
    fields = ('route_id', 'name', 'rationale', 'feasibility_risks', 'evidence_ids', 'self_assessment', 'graph_key')
    if 'stereo_plan' in route:
        fields = (*fields, 'stereo_plan')
    return sha256(json.dumps({key: route[key] for key in fields}, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def task_expectations(state_dir, task_id, target, **limits):
    """Read host context and reject caller attempts to replace its expectations."""
    if task_id is None:
        return None
    context = read_task_context(state_dir, task_id)
    if canonical_smiles(target) != context['target']:
        raise ValueError('Target differs from immutable task context')
    if any(type(value) is not int or value != context['constraints'][key]
           for key, value in limits.items()):
        raise ValueError('Limits differ from immutable task context')
    return context


def _receipt_binding(request, result, context):
    expected = context['task_id'] if context is not None else None
    if request.get('task_id') != expected:
        raise ValueError('Receipt task binding requires the original task_id')
    if context is not None and (result.get('task_id') != expected or
            result.get('task_content_sha256') != context['content_sha256']):
        raise ValueError('Receipt differs from immutable task content')
    if context is None and ('task_id' in result or 'task_content_sha256' in result):
        raise ValueError('Bound receipt requires the original task_id')


def _verified(final_response, receipt_id, target, state_dir, task_id=None):
    if not isinstance(receipt_id, str) or not re.fullmatch('[a-f0-9]{32}', receipt_id):
        raise ValueError('Invalid validation receipt identifier')
    path = Path(state_dir).resolve() / 'receipts' / (receipt_id + '.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError('Validation receipt missing or unavailable')
    receipt = json.loads(path.read_text())
    if receipt['action'] != 'validate_route_hypotheses' or receipt['response']['status'] != 'completed':
        raise ValueError('Receipt is not a successful route validation')
    for key in ('request', 'response'):
        if sha256(_encoded(receipt[key]).encode()).hexdigest() != receipt[key + '_sha256']:
            raise ValueError('Validation receipt hash mismatch')
    request, original = receipt['request'], receipt['response']['result']
    if canonical_smiles(target) != canonical_smiles(request['target']):
        raise ValueError('Validation receipt belongs to another target')
    context = task_expectations(state_dir, task_id, target,
        **{key: request[key] for key in ('requested_count', 'max_depth', 'max_total_reactions') if key in request})
    _receipt_binding(request, original, context)
    limits = context['constraints'] if context is not None else {
        'requested_count': request.get('requested_count', 5), 'max_depth': request.get('max_depth', 5),
        'max_total_reactions': request.get('max_total_reactions', 20)}
    count = limits['requested_count']
    parsed = parse_response(final_response, count, limits['max_total_reactions'])
    earlier = parse_response(request['raw_response'], count, limits['max_total_reactions'])
    if parsed['limitations'] != earlier['limitations']:
        raise ValueError('Final limitations differ from validated proposal')
    checked = (validate_task_candidates(context, final_response) if context is not None else
               validate_response(final_response, target, **limits))
    if checked['counts']['retained'] != len(parsed['routes']):
        raise ValueError('Final output contains a rejected or withdrawn proposal')
    allowed = {_fingerprint(route): route for route in original['routes']}
    accepted = []
    for route in checked['routes']:
        key = _fingerprint(route)
        if key not in allowed:
            raise ValueError('Final graph or claims differ from successful validation')
        accepted.append(deepcopy(allowed[key]))
    binding = {} if context is None else {'task_id': context['task_id'],
        'task_content_sha256': context['content_sha256']}
    return {**binding, 'status': 'verified_hypotheses', 'routes': accepted,
            'limitations': deepcopy(original['limitations']), 'validation_receipt_id': receipt_id,
            'chemical_feasibility_established': False,
            'meaning': 'Final hypotheses match backend validation; this does not establish chemical feasibility'}


def verify_native_result(final_response, receipt_id, target, state_dir, *, task_id=None):
    """Host-only state_dir; callers cannot supply trusted validated route objects."""
    try:
        return _verified(final_response, receipt_id, target, state_dir, task_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'quarantined', 'routes': [], 'reason': str(exc)[:300],
                'chemical_feasibility_established': False}


def verify_native_envelope(final_envelope, target, state_dir, *, requested_count=None,
                           max_depth=None, max_total_reactions=None, task_id=None):
    """Host acceptance gate for a saved specialist's final verification-tool envelope.

    Call this after native delegation with the original Main task limits.
    They are independent of child-selected validator arguments. This does not
    bind Main to an immutable application request unless task_id names a host record.
    Never accept the LLM's copied status alone.
    """
    try:
        if any(value is None for value in (requested_count, max_depth, max_total_reactions)):
            raise ValueError('Main acceptance requires explicit requested_count, max_depth and max_total_reactions from the original task')
        context = task_expectations(state_dir, task_id, target, requested_count=requested_count,
            max_depth=max_depth, max_total_reactions=max_total_reactions)
        if not isinstance(final_envelope, str) or len(final_envelope) > 4_000_000:
            raise ValueError('Final envelope must be bounded JSON text')
        value = json.loads(final_envelope)
        # The first real saved-role run returned this explicit named wrapper.
        # Normalize only its field names; all content must still match the receipt.
        if isinstance(value, dict) and set(value) == {'verified_result', 'verification_receipt_id'}:
            value = {'status': 'completed', 'result': value['verified_result'],
                     'receipt_id': value['verification_receipt_id']}
        if not isinstance(value, dict) or set(value) != {'status', 'result', 'receipt_id'}:
            raise ValueError('Final answer is not a backend verification envelope')
        receipt_id = value['receipt_id']
        if not isinstance(receipt_id, str) or not re.fullmatch('[a-f0-9]{32}', receipt_id):
            raise ValueError('Invalid final receipt identifier')
        path = Path(state_dir).resolve() / 'receipts' / (receipt_id + '.json')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
            raise ValueError('Final verification receipt unavailable')
        receipt = json.loads(path.read_text())
        if receipt['action'] != 'verify_retrosynthesis_result':
            raise ValueError('Final answer skipped backend verification')
        for key in ('request', 'response'):
            if sha256(_encoded(receipt[key]).encode()).hexdigest() != receipt[key + '_sha256']:
                raise ValueError('Final receipt hash mismatch')
        if {key: value[key] for key in ('status', 'result')} != receipt['response']:
            raise ValueError('Final answer changed the backend verification result')
        request = receipt['request']
        if canonical_smiles(target) != canonical_smiles(request['target']):
            raise ValueError('Final answer belongs to another target')
        _receipt_binding(request, receipt['response']['result'], context)
        original = verify_native_result(request['final_response'], request['validation_receipt_id'], target, state_dir, task_id=task_id)
        if original['status'] != 'verified_hypotheses':
            return original
        checked = (validate_task_candidates(context, request['final_response']) if context is not None else
            validate_response(request['final_response'], target, requested_count=requested_count,
                max_depth=max_depth, max_total_reactions=max_total_reactions))
        if checked['rejections'] or checked['counts']['retained'] != len(original['routes']):
            raise ValueError('Final hypotheses exceed Main task limits or fail Main acceptance checks')
        return {**original, 'acceptance_constraints': {'requested_count': requested_count,
            'max_depth': max_depth, 'max_total_reactions': max_total_reactions},
            'acceptance_constraint_source': ('Immutable host task context and matching explicit Main expectations' if context is not None else
                'Explicit Main expectations; not an immutable application request binding')}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'quarantined', 'routes': [], 'reason': str(exc)[:300],
                'chemical_feasibility_established': False}
