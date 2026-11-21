"""Attach an intermediate synthesis while preserving every precursor branch.

This joins graph hypotheses by exact canonical molecular identity. It performs
no inventory lookup and establishes neither chemical feasibility nor stock.
"""
from copy import deepcopy

from .graphs import canonical_smiles, route_graph


def _evidence(route):
    values = route.get('evidence_ids', [])
    if (not isinstance(values, list) or len(values) > 64 or
            any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in values)):
        raise ValueError('Source evidence IDs must be bounded strings')
    return values


def _expansion_target(expansion, leaves):
    if not isinstance(expansion, dict):
        raise ValueError('Expansion must be an explicit reaction graph')
    if 'target' in expansion:
        target = canonical_smiles(expansion['target'])
        if target not in leaves:
            raise ValueError('Expansion target must match an exact parent terminal leaf')
        return target
    reactions = expansion.get('reactions')
    if not isinstance(reactions, list):
        raise ValueError('Expansion requires explicit reactions')
    products = set()
    for reaction in reactions:
        if not isinstance(reaction, dict) or 'product' not in reaction:
            raise ValueError('Malformed expansion reaction')
        products.add(canonical_smiles(reaction['product']))
    matches = products.intersection(leaves)
    if len(matches) != 1:
        raise ValueError('Expansion target is ambiguous or does not match a parent terminal leaf')
    return next(iter(matches))


def graft_route(parent, expansion, target, max_depth=5, max_total_reactions=20):
    """Return a complete canonical joined graph or raise on any invalid join.

    A supplied expansion target must be a parent leaf. Without that field only
    one matching product is permitted. Inputs are individually validated before
    union; identical shared producers coalesce while conflicting alternatives
    reject. Global limits and cycle/component-ancestor checks apply after union.
    """
    target = canonical_smiles(target)
    if not isinstance(parent, dict):
        raise ValueError('Parent must be an explicit reaction graph')
    if 'target' in parent and canonical_smiles(parent['target']) != target:
        raise ValueError('Parent target differs from the requested exact target')
    checked_parent = route_graph(parent, target, max_depth, max_total_reactions)
    leaf = _expansion_target(expansion, set(checked_parent['leaves']))
    checked_expansion = route_graph(expansion, leaf, max_depth, max_total_reactions)
    evidence = sorted(set(_evidence(parent)) | set(_evidence(expansion)))
    if len(evidence) > 64:
        raise ValueError('Joined evidence ID set exceeds route contract bound')
    producers = {}
    for reaction in [*checked_parent['reactions'], *checked_expansion['reactions']]:
        product = reaction['product']
        if product in producers and producers[product]['reactants'] != reaction['reactants']:
            raise ValueError('Conflicting producers in joined intermediate synthesis')
        producers[product] = deepcopy(reaction)
    # Regenerate all derived fields rather than inheriting stale stock, cost,
    # evidence-assessment or topology metadata from either source graph.
    result = route_graph({'route_id': 'joined', 'reactions': list(producers.values())},
                         target, max_depth, max_total_reactions)
    return {**result, 'route_id': 'graft-' + result['graph_key'][:24],
            'origin': 'intermediate_replanning', 'parent_graph_key': checked_parent['graph_key'],
            'expansion_graph_key': checked_expansion['graph_key'], 'expanded_leaf': leaf,
            'evidence_ids': evidence, 'chemical_feasibility_established': False,
            'meaning': 'Exact-identity graph composition only; assess chemistry, inventory and cost separately.'}
