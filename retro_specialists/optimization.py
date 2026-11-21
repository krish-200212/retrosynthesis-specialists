"""Deterministic route assessment and conservative revision acceptance.

Axes describe corpus coverage, configured membership and scoped price estimates.
They are not chemical feasibility, full-catalog availability or manufacturing cost.
All valid alternatives and originals survive even when no revision is accepted.
Rejected/unlinked revisions remain exploratory and cannot enter final selection
unless their graph was already an independent member of the combined pool.
"""
from copy import deepcopy
from decimal import Decimal

from .costs import evaluate_route
from .graphs import canonical_smiles, graph_key, route_graph
from .literature import assess_citations, validate_evidence


def _bounded_limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError('Selection limit must be 1..100')
    return value


def _reaction_evidence(reactions, evidence):
    # Assess baseline and generated routes fairly against the same backend corpus,
    # independently of which IDs a model happened to cite. Each edge is explicit.
    identifiers = [record['id'] for record in evidence]
    rows = [assess_citations({'reactions': [reaction], 'evidence_ids': identifiers}, evidence)
            for reaction in reactions]
    unsupported = [index for index, row in enumerate(rows) if row['exact_supported_reactions'] == 0]
    return {'reactions': rows, 'exact_supported_reactions': len(rows) - len(unsupported),
            'total_reactions': len(rows), 'unsupported_reactions': unsupported,
            'coverage_basis': 'Exact precursor/product connectivity in the backend-reviewed corpus, not model citation claims.',
            'chemical_feasibility_established': False}


def assess_pool(routes, target, inventory=None, evidence=(), quotes=(), cost_assumptions=None):
    """Canonicalize and assess each distinct graph; invalid rows remain rejections.

    inventory=None means no membership set. An explicit set is only that supplied
    set, never an assertion that a full supplier/backend catalog was searched.
    """
    target = canonical_smiles(target)
    if isinstance(routes, (str, bytes, dict)):
        raise ValueError('Routes must be a bounded route collection')
    values = list(routes)
    if len(values) > 1000:
        raise ValueError('Route collection exceeds 1000 graphs')
    if isinstance(inventory, (str, bytes, dict)):
        raise ValueError('Inventory must be a molecular membership iterable')
    stock = None if inventory is None else frozenset(canonical_smiles(value) for value in inventory)
    records, quotes = validate_evidence(tuple(evidence)), tuple(quotes)
    assumptions = deepcopy(cost_assumptions or {})
    # Configuration errors must fail at the boundary, not masquerade as invalid
    # molecule rows. The cost backend validates quotes/assumptions below.
    rows, rejections, positions = [], [], {}
    for index, route in enumerate(values):
        try:
            checked = route_graph(route, target, 100, 200)
        except (ValueError, TypeError, KeyError) as error:
            rejections.append({'index': index, 'route_id': route.get('route_id') if isinstance(route, dict) else None,
                               'reason': str(error)})
            continue
        origin = route.get('origin', 'unspecified')
        key = checked['graph_key']
        if key in positions:
            row = rows[positions[key]]
            rows[positions[key]] = {**row, 'origins': sorted(set(row['origins']) | {str(origin)})}
            continue
        assessment = _assess(checked, stock, records, quotes, assumptions, str(origin))
        positions[key] = len(rows)
        rows.append(assessment)
    return {'target': target, 'assessments': rows, 'rejections': rejections,
            'inventory_scope': 'unknown' if stock is None else 'configured_membership_set_only',
            'chemical_feasibility_established': False}


def _assess(route, stock, evidence, quotes, assumptions, origin):
    literature = _reaction_evidence(route['reactions'], evidence)
    unknown = None if stock is None else sorted(set(route['leaves']) - stock)
    membership = {'status': 'unknown' if stock is None else 'configured_membership_set_only',
                  'connected': None if unknown is None else not unknown,
                  'unknown_leaves': unknown, 'full_catalog_available': False}
    cost = evaluate_route(route, quotes, assumptions)
    warnings = {row['reaction_index']: row['missing_elements'] for row in route['element_source_warnings']}
    weaknesses = [{'reaction_index': index, 'reaction': deepcopy(route['reactions'][index]),
                   'issue_codes': ['missing_product_elements'] if index in warnings else ['no_exact_precursor_precedent'],
                   'missing_elements': warnings.get(index, []),
                   'meaning': 'Evidence gap or element-source warning; not a verdict that the reaction is impossible.'}
                  for index in range(route['total_reactions'])
                  if index in literature['unsupported_reactions'] or index in warnings]
    return {'route': deepcopy(route), 'route_id': route.get('route_id'), 'graph_key': route['graph_key'],
            'origins': [origin], 'depth': route['depth'], 'total_reactions': route['total_reactions'],
            'evidence': literature, 'inventory': membership, 'cost': cost, 'weaknesses': weaknesses}


def shortlist_tasks(assessments, limit=3):
    """Target one explicit weak edge or expensive priced leaf per selected graph."""
    _bounded_limit(limit)
    rows = assessments['assessments'] if isinstance(assessments, dict) else list(assessments)
    repair, cost = [], []
    # Prefer partly-supported routes to arbitrary guesses; then element gaps and
    # fewer unresolved edges. This orders work, not predicted synthetic success.
    ordered = sorted(rows, key=lambda row: (
        not bool(row['evidence']['exact_supported_reactions']),
        -sum('missing_product_elements' in gap['issue_codes'] for gap in row['weaknesses']),
        len(row['evidence']['unsupported_reactions']), row['depth'], row['graph_key']))
    for row in ordered:
        if row['weaknesses'] and len(repair) < limit:
            gap = min(row['weaknesses'], key=lambda item: ('missing_product_elements' not in item['issue_codes'], item['reaction_index']))
            repair.append({**_task(row), **deepcopy(gap),
                'acceptance_criterion': 'Replace this specific weak edge with explicit supported reactions, preserve compatible portions, and avoid evidence, configured-membership or known-cost regressions.'})
    known = sorted((row for row in rows if row['cost']['status'] == 'known'),
                   key=lambda row: (-Decimal(row['cost']['amount']), row['graph_key']))
    for row in known[:limit]:
        estimate = row['cost']
        sources = {q['molecule']: q for q in estimate['quote_evidence']}
        contributions = {m: Decimal(demand) * Decimal(sources[m]['amount_per_mol'])
                         for m, demand in estimate['leaf_demands_mol'].items()}
        molecule = max(contributions, key=lambda m: (contributions[m], m))
        cost.append({**_task(row), 'focus_molecule': molecule, 'focus_cost': str(contributions[molecule]),
            'currency': estimate['currency'], 'cost_basis': estimate['basis'],
            'issue_codes': ['priced_precursor_material_burden'],
            'acceptance_criterion': 'Lower comparable precursor-material estimate under the same declared assumptions, without evidence or configured-membership regression; disclose process costs excluded.'})
    return {'route_repair': repair, 'cost_reducer': cost,
            'limitations': [] if known else ['No fully priced routes; cost-reduction benefit cannot be evaluated. Obtain comparable sourced quotes and reaction assumptions.']}


def _task(row):
    return {'parent_graph_key': row['graph_key'], 'parent_route_id': row['route_id'],
            'route': deepcopy(row['route'])}


def _quality(row):
    ev = row['evidence']
    return (ev['exact_supported_reactions'], ev['total_reactions'], len(ev['unsupported_reactions']))


def _edge_signatures(row, supported):
    unsupported = set(row['evidence']['unsupported_reactions'])
    return {(reaction['product'], tuple(reaction['reactants']))
            for index, reaction in enumerate(row['route']['reactions'])
            if (index not in unsupported) == supported}


def _evidence_changes(parent, child):
    before = _edge_signatures(parent, True)
    after = _edge_signatures(child, True)
    all_before = _edge_signatures(parent, True) | _edge_signatures(parent, False)
    return {'removed_exact_supported_reactions': len(before - after),
            'added_exact_supported_reactions': len(after - before),
            'new_unsupported_transformations': len(_edge_signatures(child, False) - all_before),
            'meaning': 'Changed unsupported transformations require chemical review even when another measured axis improves.'}


def _decision(parent, child):
    p, c = _quality(parent), _quality(child)
    regressions = []
    if c[0] * p[1] < p[0] * c[1] or c[2] > p[2]:
        regressions.append('exact_precedent_regression')
    if len(child['route']['element_source_warnings']) > len(parent['route']['element_source_warnings']):
        regressions.append('element_source_warning_regression')
    if parent['inventory']['connected'] is True and child['inventory']['connected'] is not True:
        regressions.append('configured_membership_regression')
    pc, cc = parent['cost'], child['cost']
    if pc['status'] == 'known' and cc['status'] != 'known':
        regressions.append('previously_known_cost_now_unknown')
    elif pc['status'] == cc['status'] == 'known' and Decimal(cc['amount']) > Decimal(pc['amount']):
        regressions.append('precursor_cost_increase')
    axes = []
    if c[0] > p[0] and c[0] * p[1] >= p[0] * c[1] and c[2] <= p[2]:
        axes.append('exact_precedent_coverage')
    if parent['inventory']['connected'] is False and child['inventory']['connected'] is True:
        axes.append('configured_inventory_completion')
    if pc['status'] == cc['status'] == 'known' and Decimal(cc['amount']) < Decimal(pc['amount']):
        axes.append('precursor_cost_proxy' if cc['basis'] == 'inventory_catalog_price_proxy' else 'supported_precursor_material_estimate')
    if c[2] == p[2] == 0 and c[0] and (child['depth'] < parent['depth'] or child['total_reactions'] < parent['total_reactions']):
        if child['depth'] <= parent['depth'] and child['total_reactions'] <= parent['total_reactions']:
            axes.append('fully_precedented_route_simplification')
    return axes, regressions


def _rank(row):
    ev = row['evidence']
    connected = row['inventory']['connected']
    # Explicit preference order; unknown prices are never treated as zero. Cost
    # breaks ties only after evidence, membership and topology burden.
    return (-Decimal(ev['exact_supported_reactions']) / ev['total_reactions'],
            len(ev['unsupported_reactions']), len(row['route']['element_source_warnings']),
            0 if connected is True else 1 if connected is False else 2,
            row['depth'], row['total_reactions'], row['cost']['status'] != 'known',
            Decimal(row['cost']['amount']) if row['cost']['status'] == 'known' else Decimal('Infinity'),
            row['graph_key'])


def select_results(baseline_routes, combined_routes, revised_routes, target, *, inventory=None,
                   evidence=(), quotes=(), cost_assumptions=None, limit=3, revision_parents=None):
    """Retain all valid hypotheses; accept improvements only against explicit parents.

    revision_parents maps candidate graph keys to host-selected parent graph keys.
    Without that map, a parent_graph_key metadata field can link a revision, but
    it is a declared comparison reference, not proof that an LLM obeyed its task.
    No nearest-route inference is used. Accepted axes remain scoped observations.
    """
    _bounded_limit(limit)
    baseline, combined, revised = list(baseline_routes), list(combined_routes), list(revised_routes)
    inventory = None if inventory is None else tuple(inventory)
    options = {'inventory': inventory, 'evidence': tuple(evidence), 'quotes': tuple(quotes),
               'cost_assumptions': cost_assumptions}
    before = assess_pool([*baseline, *combined], target, **options)
    after = assess_pool(revised, target, **options)
    parents = {row['graph_key']: row for row in before['assessments']}
    explicit = {} if revision_parents is None else dict(revision_parents)
    declarations = {row['graph_key']: row['route'].get('parent_graph_key')
                    for row in after['assessments'] if 'parent_graph_key' in row['route']}
    original_keys = {row['graph_key'] for row in assess_pool(baseline, target, **options)['assessments']}
    baseline_costs = [row['cost'] for key, row in parents.items() if key in original_keys]
    priced = [cost for cost in baseline_costs if cost['status'] == 'known']
    best_amount = min((Decimal(cost['amount']) for cost in priced), default=None)
    decisions, accepted, existing_alternatives = [], [], []
    for row in after['assessments']:
        key = row['graph_key']
        parent_key = explicit.get(key, declarations.get(key))
        status, axes, tradeoffs = 'unlinked', [], []
        if parent_key is not None:
            status = 'unknown_parent' if parent_key not in parents else 'unchanged' if parent_key == key else 'assessed'
            if status == 'assessed':
                axes, tradeoffs = _decision(parents[parent_key], row)
                status = ('reused_existing_alternative' if key in parents else 'accepted_scoped_improvement') if axes and not tradeoffs else 'retained_hypothesis_only'
        decision = {'graph_key': key, 'parent_graph_key': parent_key, 'status': status,
                    'improved_axes': axes, 'tradeoffs': tradeoffs,
                    'new_graph_contribution': key not in parents,
                    'reaction_evidence_changes': _evidence_changes(parents[parent_key], row) if parent_key in parents else None,
                    'comparison_scope': 'explicit_parent_route_only',
                    'lower_than_best_fully_priced_baseline': (best_amount is not None and row['cost']['status'] == 'known' and Decimal(row['cost']['amount']) < best_amount),
                    'unknown_baseline_cost_count': len(baseline_costs) - len(priced),
                    'lowest_cost_overall_established': False,
                    'chemical_feasibility_established': False}
        decisions.append(decision)
        if status == 'accepted_scoped_improvement':
            accepted.append(deepcopy(decision))
        elif status == 'reused_existing_alternative':
            existing_alternatives.append(deepcopy(decision))
    full = assess_pool([*baseline, *combined, *revised], target, **options)
    # A revision that fails its comparison gate remains exploratory. It must
    # not displace an original just because the ranking prefers its topology.
    eligible_keys = set(parents) | {row['graph_key'] for row in accepted}
    eligible = [row for row in full['assessments'] if row['graph_key'] in eligible_keys]
    exploratory = [row for row in full['assessments'] if row['graph_key'] not in eligible_keys]
    ranked = sorted(eligible, key=_rank)
    selected = [{**deepcopy(row), 'selection_reason': 'Explicit evidence coverage, supplied membership, topology burden and scoped-cost preference order; no chemical-success probability.'}
                for row in ranked[:limit]]
    return {'target': canonical_smiles(target), 'original_routes': deepcopy(baseline),
            'combined_routes': [deepcopy(row['route']) for row in before['assessments']],
            'all_routes': [deepcopy(row['route']) for row in full['assessments']],
            'eligible_routes': [deepcopy(row['route']) for row in eligible],
            'exploratory_revision_routes': [deepcopy(row['route']) for row in exploratory],
            'selected_routes': selected, 'accepted_improvements': accepted,
            'existing_alternatives': existing_alternatives,
            'revision_decisions': decisions, 'rejections': {'combined': before['rejections'], 'revised': after['rejections']},
            'selection_policy': 'separate_axes_lexicographic_shortlist_not_feasibility_score',
            'chemical_superiority_established': False, 'total_synthesis_cost_reduction_established': False,
            'limitations': ['Exact reaction connectivity precedent does not establish executable conditions or whole-route success.',
                'Configured membership is not a full-catalog availability claim; unknown costs are not zero.',
                'Material estimates exclude process burden; preserve original routes when tradeoffs remain unresolved.']}
