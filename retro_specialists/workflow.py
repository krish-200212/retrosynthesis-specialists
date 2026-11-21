"""Bounded independent proposer; honest comparison against model routes.

This controller does not modify the model search or its public route-tree format.
Its result is an internal evaluation artifact, not a frontend REBOLT response.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import time

from .contracts import response_schema
from .graphs import canonical_smiles, route_graph, validate_response
from .literature import assess_citations, validate_evidence
from .costs import compare_routes
from .prompts import PROMPT_VERSION, instructions


@dataclass(frozen=True)
class RunConfig:
    requested_count: int = 5
    max_depth: int = 5
    max_total_reactions: int = 20
    deadline_seconds: float = 180
    max_baseline_context: int = 30
    feedback_retries: int = 0

    def __post_init__(self):
        if type(self.requested_count) is not int or not 1 <= self.requested_count <= 20:
            raise ValueError('requested_count must be 1..20')
        for key in ('max_depth', 'max_total_reactions', 'max_baseline_context'):
            if type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= 100:
                raise ValueError(key + ' must be 1..100')
        if not math.isfinite(self.deadline_seconds) or not 0 < self.deadline_seconds <= 3600:
            raise ValueError('deadline_seconds must be positive and at most 3600')
        if type(self.feedback_retries) is not int or not 0 <= self.feedback_retries <= 2:
            raise ValueError('feedback_retries must be 0..2')


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')
    path.chmod(0o600)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _compact(routes, limit):
    return [{k: r[k] for k in ('route_id', 'reactions') if k in r} for r in routes[:limit]]


def _reaction_keys(route):
    return frozenset((r['product'], tuple(sorted(r['reactants']))) for r in route['reactions'])


def changes(candidate, baseline):
    """Exact connectivity comparison, not a chemistry or improvement score."""
    current = _reaction_keys(candidate)
    if not baseline:
        return {'changed': True, 'baseline_route_id': None,
                'removed_reactions': 0, 'added_reactions': len(current)}
    closest = min(baseline, key=lambda r: len(current ^ _reaction_keys(r)))
    before = _reaction_keys(closest)
    return {'changed': current != before, 'baseline_route_id': closest.get('route_id'),
            'removed_reactions': len(before - current),
            'added_reactions': len(current - before),
            'unchanged_reactions': len(before & current)}


async def _branch(role, target, name, baseline_task, evidence, provider, folder,
                  config, inventory, quotes, assumptions, deadline):
    if role != 'route_proposer':
        raise ValueError('Retired role; only route_proposer is active')
    from .stereochemistry import target_stereo_requirements
    started = time.monotonic()
    baseline = []
    request = {'prompt_version': PROMPT_VERSION, 'role': role, 'identity': {'name': name, 'smiles': target},
               'requested_route_count': config.requested_count,
               'project_specification': {'max_depth': config.max_depth,
                                         'max_total_reactions': config.max_total_reactions},
               'stereochemical_requirements': target_stereo_requirements(target),
               'starting_material_library': {'meaning': 'Backend checks membership. Do not claim availability.'},
               'literature_context': [{key: row[key] for key in ('id', 'title', 'url', 'doi', 'excerpt', 'retrieved_at', 'source_kind')} for row in evidence],
               'model_branch': 'Independent; model-search results unavailable'}
    branch_dir = folder / role
    _save(branch_dir / 'request.json', request)
    schema = response_schema(config.requested_count, config.max_total_reactions, require_stereo_plan=True,require_origin_declarations=True)
    _save(branch_dir / 'schema.json', schema)
    _save(branch_dir / 'instructions.json', {'version': PROMPT_VERSION, 'text': instructions(role)})
    responses, validations = [], []
    try:
        for attempt in range(config.feedback_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Branch inclusion deadline reached')
            async with asyncio.timeout(remaining):
                result = await provider.generate(request, instructions(role), schema,
                                                branch_dir / ('attempt-' + str(attempt)))
            responses.append(result)
            if result.get('status') == 'timed_out':
                raise TimeoutError('Provider deadline exceeded')
            if result.get('status') != 'completed':
                raise RuntimeError('Provider did not complete: ' + str(result.get('status')))
            validated = validate_response(result['raw_response'], target,
                requested_count=config.requested_count, max_depth=config.max_depth,
                max_total_reactions=config.max_total_reactions, inventory=inventory, require_stereo_plan=True,require_origin_declarations=True)
            validations.append(validated)
            if validated['routes'] or not validated.get('rejections'):
                break
            request = {**request, 'previous_attempt_feedback': {
                'rejections': validated['rejections'],
                'previous_agent_hypotheses': result['raw_response'][:20000]}}
        accepted, evidence_rejections = [], []
        known_ids = {record['id'] for record in evidence}
        for route in validated['routes']:
            unknown = set(route.get('evidence_ids', [])) - known_ids
            if unknown:
                evidence_rejections.append({'route_id': route['route_id'],
                                            'reason': 'Unknown evidence IDs', 'ids': sorted(unknown)})
            else:
                from .stereochemistry import assess_stereo_evidence
                stereo = assess_stereo_evidence(route, evidence)
                if stereo['issues']:
                    evidence_rejections.append({'route_id': route['route_id'],
                        'reason': 'Unbound stereochemical source claims', 'issues': stereo['issues']})
                else:
                    accepted.append({**route, 'origin': role, 'literature_assessment': assess_citations(route, evidence),
                                     'stereochemical_assessment': stereo})
        if time.monotonic() > deadline:
            raise TimeoutError('Validation finished after deadline')
        counts = {**validated['counts'], 'retained': len(accepted),
                  'rejected': validated['counts']['proposed'] - len(accepted),
                  'literature_assessed': len(accepted)}
        output = {'status': 'completed', 'routes': accepted, 'counts': counts,
                  'validation': validated, 'attempt_validations': validations,
                  'provider_receipts': responses, 'evidence_rejections': evidence_rejections,
                  'changes': [],
                  'started_monotonic': started, 'ended_monotonic': time.monotonic()}
    except (TimeoutError, asyncio.CancelledError):
        output = {'status': 'timed_out', 'routes': [], 'provider_receipts': responses,
                  'started_monotonic': started, 'ended_monotonic': time.monotonic()}
    except Exception as error:
        output = {'status': 'failed', 'error_type': type(error).__name__, 'routes': [],
                  'provider_receipts': responses, 'started_monotonic': started,
                  'ended_monotonic': time.monotonic()}
    _save(branch_dir / 'result.json', output)
    return output


async def run_specialists(target, name, baseline, provider, receipt_dir, *,
                          config=RunConfig(), inventory=(), evidence=(), quotes=(),
                          cost_assumptions=None):
    """baseline is a completed route list or async zero-argument search function.

    A provider timeout does not cancel successful model results. The deadline
    bounds specialist inclusion, not the model search's independent service budget.
    """
    target = canonical_smiles(target)
    if not isinstance(name, str) or not name.strip() or len(name) > 300:
        raise ValueError('Target name must be a bounded nonempty string')
    folder = Path(receipt_dir)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    assumptions = dict(cost_assumptions or {})
    inventory = tuple(canonical_smiles(value) for value in inventory)
    evidence = validate_evidence(tuple(evidence))
    quotes = tuple(quotes)
    started = time.monotonic()
    baseline_rejections = []
    baseline_timing = {}
    async def load_baseline():
        baseline_timing['started_monotonic'] = time.monotonic()
        values = await baseline() if callable(baseline) else baseline
        routes = []
        for index, row in enumerate(values):
            try:
                checked = route_graph(row, target, config.max_depth, config.max_total_reactions)
            except (ValueError, TypeError, KeyError) as error:
                baseline_rejections.append({'index': index, 'error': str(error)})
                continue
            routes.append({**checked, 'origin': 'model', 'route_id': row.get('route_id', str(index)),
                           'stock_connected': set(checked['leaves']).issubset(inventory)})
        baseline_timing['ended_monotonic'] = time.monotonic()
        return routes
    baseline_task = asyncio.create_task(load_baseline())
    tasks = {role: asyncio.create_task(_branch(role, target, name, baseline_task,
        list(evidence), provider, folder, config, list(inventory), list(quotes), assumptions,
        started + config.deadline_seconds)) for role in ('route_proposer',)}
    try:
        baseline_routes = await asyncio.shield(baseline_task)
        branches = dict(zip(tasks, await asyncio.gather(*tasks.values())))
    except BaseException:
        for task in [baseline_task, *tasks.values()]:
            task.cancel()
        await asyncio.gather(baseline_task, *tasks.values(), return_exceptions=True)
        raise
    merged = {r['graph_key']: r for r in baseline_routes}
    original_keys = frozenset(merged)
    for branch in branches.values():
        for route in branch['routes']:
            if route['graph_key'] not in merged:
                merged[route['graph_key']] = route
    candidates = [r for key, r in merged.items() if key not in original_keys]
    proposal_timing = branches['route_proposer']
    overlap = max(0, min(baseline_timing['ended_monotonic'], proposal_timing['ended_monotonic'])
                  - max(baseline_timing['started_monotonic'], proposal_timing['started_monotonic']))
    result = {'prompt_version': PROMPT_VERSION, 'target': target,
              'created_utc': datetime.now(timezone.utc).isoformat(), 'configuration': asdict(config),
              'baseline_count': len(original_keys), 'baseline_routes': baseline_routes,
              'branches': branches, 'merged_routes': list(merged.values()),
              'additional_unique_graphs': len(candidates),
              'additional_stock_connected_graphs': sum(bool(r.get('stock_connected')) for r in candidates),
              'chemically_validated_routes': 0, 'cost_improvement_demonstrated': False,
              'improvement_demonstrated': False,
              'baseline_rejections': baseline_rejections,
              'cost_comparison': compare_routes(baseline_routes, candidates, quotes, assumptions),
              'additional_exact_precedent_graphs': sum(r.get('literature_assessment', {}).get('status') == 'exact_precedent' for r in candidates),
              'additional_delay_seconds': max(0, time.monotonic() - baseline_timing['ended_monotonic']),
              'deadline_policy': 'Only the independent proposer is dispatched; its deadline is bounded from start. Repair and cost agents are retired.',
              'comparison_scope': 'Structural diversity only until independent evidence/cost assessment',
              'overlap_seconds': overlap, 'baseline_timing': baseline_timing,
              'total_seconds': time.monotonic() - started,
              'input_hash': _digest({'target': target, 'config': asdict(config),
                                     'evidence': evidence, 'inventory': list(inventory)})}
    _save(folder / 'result.json', result)
    return result
