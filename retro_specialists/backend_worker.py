"""Fixed entry point for isolated native-tool execution; JSON only on stdin/stdout."""
import asyncio
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import re


def _read_json(path, maximum=4_000_000):
    source = Path(path)
    if not source.is_absolute() or not source.is_file() or source.stat().st_size > maximum:
        raise ValueError('Configured data file must be an existing bounded absolute path')
    return json.loads(source.read_text())


def _persist_evidence(state, records):
    folder = Path(state) / 'evidence'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(records, sort_keys=True, allow_nan=False).encode()
    file = folder / (sha256(encoded).hexdigest() + '.json')
    try:
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if file.read_bytes() != encoded:
            raise ValueError('Evidence record collision')
    else:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)


def _record_content(record):
    return json.dumps({key: value for key, value in record.items()
                       if key not in {'sha256', 'retrieved_at'}}, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _evidence(state, *, context_only=False):
    from retro_specialists.literature import validate_evidence
    by_id = {}
    files = sorted((Path(state) / 'evidence').glob('*.json'), key=lambda path: (path.stat().st_mtime_ns, path.name))[-100:]
    for file in files:
        records = _read_json(file)
        encoded = json.dumps(records, sort_keys=True, allow_nan=False).encode()
        if sha256(encoded).hexdigest() != file.stem:
            raise ValueError('Evidence file hash mismatch')
        for record in validate_evidence(records):
            if context_only and record['retrieval_scope'] != 'retrieved_context':
                continue
            previous = by_id.get(record['id'])
            if previous is not None:
                if any(previous[field] != record[field] for field in ('url', 'doi')):
                    raise ValueError('Conflicting source identity for ' + record['id'])
                scopes = {previous['retrieval_scope'], record['retrieval_scope']}
                if scopes == {'curated_exact_reactions'} and _record_content(previous) != _record_content(record):
                    raise ValueError('Conflicting curated evidence assertions for ' + record['id'])
                if scopes == {'curated_exact_reactions', 'retrieved_context'}:
                    if any(previous[field] != record[field] for field in ('title', 'excerpt')):
                        raise ValueError('Conflicting curated source context for ' + record['id'])
                    by_id[record['id']] = previous if previous['retrieval_scope'] == 'curated_exact_reactions' else record
                    continue
                if datetime.fromisoformat(previous['retrieved_at']) >= datetime.fromisoformat(record['retrieved_at']):
                    continue
            by_id[record['id']] = record
    latest = sorted(by_id.values(), key=lambda row: (datetime.fromisoformat(row['retrieved_at']), row['id']))[-100:]
    validated = list(validate_evidence(latest))
    return [row for row in validated if row['retrieval_scope'] == 'retrieved_context'] if context_only else validated


def _search(args, trusted):
    from retro_specialists.literature import cached_literature_search
    return cached_literature_search(args, trusted, lambda state: _evidence(state, context_only=True), _persist_evidence)


def _read_task(args, trusted):
    from retro_specialists.task_context import read_task_context
    return read_task_context(trusted['state_dir'], args['task_id'])


def _inspect_target_stereo(args, trusted):
    from retro_specialists.stereochemistry import target_stereo_requirements
    target = args['target']
    if not isinstance(target, str) or not target.strip() or len(target) > 4096:
        raise ValueError('Target must be a nonempty SMILES string of at most 4096 characters')
    return target_stereo_requirements(target)


def _validate(args, trusted):
    from retro_specialists.finalization import task_expectations
    from retro_specialists.task_context import validate_task_candidates
    context = task_expectations(trusted['state_dir'], args.get('task_id'), args['target'],
        **{key: args[key] for key in ('requested_count', 'max_depth', 'max_total_reactions') if key in args})
    if context is not None and context['role'] != 'route_proposer':
        raise ValueError('Retired specialist tasks are read-only; no new validation is permitted')
    result = (validate_task_candidates(context, args['raw_response']) if context is not None else
              _legacy_validate(args, trusted))
    from retro_specialists.literature import assess_citations
    from retro_specialists.stereochemistry import assess_stereo_evidence
    records = _evidence(trusted['state_dir'])
    routes, rejections = [], list(result['rejections'])
    for route in result['routes']:
        assessment = assess_stereo_evidence(route, records)
        if assessment['issues']:
            rejections.append({'route_id': route['route_id'], 'stage': 'stereo_evidence',
                               'reason': 'Stereochemical claims lack the declared source binding',
                               'issues': assessment['issues']})
        else:
            routes.append({**route, 'literature_assessment': assess_citations(route, records),
                           'stereochemical_assessment': assessment})
    counts = {**result['counts'], 'retained': len(routes),
              'rejected': result['counts']['proposed'] - len(routes)}
    return {**result, 'routes': routes, 'rejections': rejections, 'counts': counts}


def _legacy_validate(args, trusted):
    from retro_specialists.graphs import validate_response
    inventory = []
    if trusted['inventory_file']:
        data = _read_json(trusted['inventory_file'])
        if not isinstance(data, dict) or not isinstance(data.get('source'), str) or not data['source'].strip():
            raise ValueError('Inventory file requires source and molecules')
        inventory = data['molecules']
        if not isinstance(inventory, list) or any(not isinstance(v, str) for v in inventory):
            raise ValueError('Inventory molecules must be strings')
    return validate_response(args['raw_response'], args['target'],
        requested_count=args.get('requested_count', 5), max_depth=args.get('max_depth', 5),
        max_total_reactions=args.get('max_total_reactions', 20), inventory=inventory,
        require_stereo_plan=True,require_origin_declarations=True)


def _baseline(args, trusted, *, allow_subset=False):
    from retro_specialists.graphs import graph_key
    path, expected = trusted.get('baseline_file', ''), trusted.get('baseline_sha256', '')
    if not path and not expected:
        return args['baseline_routes'], {'status': 'unverified_user_supplied_baseline',
            'meaning': 'User-supplied baseline; not verified tool output. No improvement over the tool can be claimed.'}
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('Configured tool baseline requires an exact SHA-256 digest')
    source = Path(path)
    if not source.is_absolute() or not source.is_file() or source.stat().st_size > 4_000_000:
        raise ValueError('Configured tool baseline must be a bounded absolute file')
    raw = source.read_bytes()
    if len(raw) > 4_000_000 or sha256(raw).hexdigest() != expected:
        raise ValueError('Configured tool baseline artifact hash mismatch')
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('source'), str) or not data['source'].strip() or not isinstance(data.get('routes'), list):
        raise ValueError('Tool baseline artifact requires source and routes')
    supplied_keys = sorted(graph_key(route) for route in args['baseline_routes'])
    artifact_keys = sorted(graph_key(route) for route in data['routes'])
    if supplied_keys != artifact_keys:
        if allow_subset and set(supplied_keys) <= set(artifact_keys):
            return args['baseline_routes'], {'status': 'trusted_tool_artifact_subset', 'sha256': expected,
                'source': data['source'], 'meaning': 'Verified subset only; no improvement over the full tool baseline can be claimed.'}
        raise ValueError('Supplied baseline differs from trusted tool artifact')
    return data['routes'], {'status': 'trusted_tool_artifact', 'sha256': expected, 'source': data['source']}


def _cost(args, trusted):
    from retro_specialists.costs import compare_routes
    quotes = []
    if trusted['quotes_file']:
        quotes = _read_json(trusted['quotes_file'])
        if isinstance(quotes, dict):
            quotes = quotes['quotes']
    baseline, candidates, provenance, binding = _cost_inputs(args, trusted)
    result = compare_routes(baseline, candidates, quotes, trusted['cost_assumptions'])
    verified = provenance['status'] == 'trusted_tool_artifact'
    return {**result, **binding, 'baseline_provenance': provenance, 'tool_baseline_verified': verified,
            'improvements': result['improvements'] if verified else [],
            'hypothetical_comparisons': [] if verified else result['improvements']}


def _cost_inputs(args, trusted):
    from retro_specialists.graphs import route_graph
    if 'task_id' not in args:
        baseline, provenance = _baseline(args, trusted)
        return baseline, args['candidate_routes'], provenance, {}
    context = _read_task(args, trusted)
    if context['role'] != 'cost_reducer':
        raise ValueError('Task-bound cost comparison requires a cost_reducer task')
    stored = context['baseline_routes']
    if 'baseline_routes' in args:
        supplied = [route_graph(route, context['target'], 100, 200) for route in args['baseline_routes']]
        signature = lambda routes: sorted((route['route_id'], route['graph_key']) for route in routes)
        if signature(supplied) != signature(stored):
            raise ValueError('Supplied baseline differs from immutable task context')
    limits = context['constraints']
    if len(args['candidate_routes']) > limits['requested_count']:
        raise ValueError('Cost candidate count exceeds immutable task limits')
    candidates = [route_graph(route, context['target'], limits['max_depth'], limits['max_total_reactions'])
                  for route in args['candidate_routes']]
    baseline, provenance = _baseline({'baseline_routes': stored}, trusted, allow_subset=True)
    total = context['baseline_provenance']['total_route_count']
    omitted = None if total is None else total - len(stored)
    provenance = {**context['baseline_provenance'], **provenance,
        'omitted_baseline_count': omitted,
        'source_digest_scope': 'Asserted original source artifact; may differ from canonical configured wrapper bytes',
        'configured_digest_scope': 'Exact configured artifact bytes, when sha256 is present'}
    if provenance['status'] == 'trusted_tool_artifact' and omitted != 0:
        provenance = {**provenance, 'status': 'trusted_tool_artifact_subset',
            'meaning': ('Configured artifact verifies the host selection only; the complete baseline '
                        'size is unknown.' if omitted is None else
                        'Configured artifact verifies the host selection only; omitted baseline routes '
                        'prevent a full-tool improvement claim.')}
    binding = {'task_id': context['task_id'], 'task_content_sha256': context['content_sha256']}
    return baseline, candidates, provenance, binding


def _finalize(args, trusted):
    from retro_specialists.finalization import verify_native_result
    return verify_native_result(args['final_response'], args['validation_receipt_id'],
                                args['target'], trusted['state_dir'], task_id=args.get('task_id'))


def _accept(args, trusted):
    from retro_specialists.finalization import verify_native_envelope
    return verify_native_envelope(args['final_envelope'], args['target'], trusted['state_dir'],
        requested_count=args['requested_count'], max_depth=args['max_depth'],
        max_total_reactions=args['max_total_reactions'], task_id=args.get('task_id'))


def main():
    raw = sys.stdin.buffer.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Request too large')
    request = json.loads(raw)
    trusted = request['trusted']
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), *trusted['library_paths']]
    from retro_specialists.pipeline_jobs import start_workflow, workflow_status
    functions = {'start_retrosynthesis_workflow': lambda a, t: start_workflow(a['target'], a['name'], t),
                 'retrosynthesis_workflow_status': lambda a, t: workflow_status(a['job_id'], t),
                 'inspect_target_stereochemistry': _inspect_target_stereo,
                 'read_retrosynthesis_task': _read_task, 'search_route_literature': _search, 'validate_route_hypotheses': _validate,
                 'verify_retrosynthesis_result': _finalize,
                 'accept_retrosynthesis_result': _accept}
    try:
        result = functions[request['action']](request['arguments'], trusted)
        response = {'status': 'completed', 'result': result}
    except (ValueError, TypeError, KeyError, OSError, ImportError) as exc:
        response = {'status': 'backend_error', 'error': type(exc).__name__ + ': ' + str(exc)[:500]}
    print(json.dumps(response, ensure_ascii=True, allow_nan=False))


if __name__ == '__main__':
    main()
