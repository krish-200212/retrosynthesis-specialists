"""Read-only adapters for known REBOLT top-N route-tree representations.

Only original entries may be re-exported: this module cannot fabricate frontend
trees for LLM hypotheses. Inventory observations are local to the supplied model
artifact, not a replacement for the deployment's complete inventory.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re

from .contracts import positive_limit
from .graphs import canonical_smiles, route_graph

_STOCK_FLAGS = ('in_stock', 'building_block', 'commercial_route')
_MAX_ENTRIES = 1000
_MAX_NODES_PER_ROUTE = 10_000
_MAX_PAYLOAD_BYTES = 16_777_216


def _container(payload):
    if not isinstance(payload, dict) or len(payload) > _MAX_ENTRIES:
        raise ValueError('Expected a bounded REBOLT top-N object')
    if any(not isinstance(key, str) or re.fullmatch(r'top[0-9]{1,8}', key) is None for key in payload):
        raise ValueError('Unsupported top-N entry key')
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError('Payload must be finite acyclic JSON data') from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError('Baseline payload exceeds byte limit')
    return sha256(encoded).hexdigest()


def _flags(node):
    flags = {}
    for name in (*_STOCK_FLAGS, 'reference', 'terminal'):
        if name not in node:
            continue
        value = node[name]
        if type(value) is bool:
            flags[name] = value
        elif name in ('building_block', 'reference', 'terminal') and value in ('True', 'False'):
            flags[name] = value == 'True'
        else:
            raise ValueError('Unsupported flag value: ' + name)
    supplied = [flags[key] for key in _STOCK_FLAGS if key in flags]
    if supplied and any(value != supplied[0] for value in supplied):
        raise ValueError('Conflicting stock flags')
    return flags


def _children(node):
    values = node.get('children', [])
    if not isinstance(values, list):
        raise ValueError('Children must be an array')
    return values


def _schema(node):
    if not isinstance(node, dict):
        raise ValueError('Route nodes must be objects')
    if node.get('type') in ('mol', 'molecule') and 'smiles' in node and 'smile' not in node:
        return 'typed-tree-v1'
    if 'smile' in node and 'smiles' not in node and 'type' not in node:
        return 'native-reference-v1'
    raise ValueError('Unsupported or mixed molecule-node schema')


class _Tree:
    def __init__(self, schema, max_depth):
        self.schema, self.max_depth = schema, max_depth
        self.count, self.active = 0, set()
        self.reactions, self.stock = {}, set()

    def enter(self, node):
        if not isinstance(node, dict):
            raise ValueError('Route nodes must be objects')
        self.count += 1
        if self.count > _MAX_NODES_PER_ROUTE:
            raise ValueError('Route exceeds node limit')
        if id(node) in self.active:
            raise ValueError('Cyclic route-tree object')
        self.active.add(id(node))

    def visit(self, node, depth=0):
        if depth > self.max_depth:
            raise ValueError('Longest-path depth limit exceeded')
        self.enter(node)
        try:
            if _schema(node) != self.schema:
                raise ValueError('Mixed route-tree schemas')
            flags = _flags(node)
            if flags.get('reference', False):
                raise ValueError('Reference wrapper cannot be a molecule node')
            molecule = canonical_smiles(node['smile' if self.schema == 'native-reference-v1' else 'smiles'])
            children = _children(node)
            stock = any(flags.get(key, False) for key in _STOCK_FLAGS)
            if children:
                if stock or flags.get('terminal', False):
                    raise ValueError('Stock or terminal molecule has a disconnection')
                reactants = self.precursors(children, molecule, depth)
                reaction = {'product': molecule, 'reactants': sorted(reactants)}
                previous = self.reactions.get(molecule)
                if previous is not None and previous != reaction:
                    raise ValueError('Conflicting selected preparations of shared intermediate')
                self.reactions[molecule] = reaction
            elif stock:
                self.stock.add(molecule)
            return molecule
        finally:
            self.active.remove(id(node))

    def precursors(self, children, product, depth):
        if len(children) != 1:
            raise ValueError('Expected exactly one selected reaction per molecule')
        wrapper = children[0]
        self.enter(wrapper)
        try:
            flags = _flags(wrapper)
            if any(flags.get(key, False) for key in (*_STOCK_FLAGS, 'terminal')):
                raise ValueError('Reaction wrapper cannot be stock or terminal')
            if self.schema == 'typed-tree-v1':
                if wrapper.get('type') != 'reaction' or flags.get('reference', False) or 'smile' in wrapper:
                    raise ValueError('Expected typed reaction wrapper')
            elif (_schema(wrapper) != self.schema or not flags.get('reference', False)
                    or canonical_smiles(wrapper['smile']) != product):
                raise ValueError('Expected matching native reference reaction wrapper')
            reactants = _children(wrapper)
            if not 1 <= len(reactants) <= 32:
                raise ValueError('Reaction requires a bounded nonempty precursor list')
            return [self.visit(child, depth + 1) for child in reactants]
        finally:
            self.active.remove(id(wrapper))


def parse_baseline(payload, target, max_depth=5, max_total_reactions=20):
    """Import complete reaction graphs while preserving unresolved leaf status.

    A bad entry is reported without discarding other entries. Duplicate tree
    occurrences of the same preparation coalesce; conflicting producers reject
    the route. A procurement-only entry is not a synthesis hypothesis.
    """
    target = canonical_smiles(target)
    positive_limit(max_depth, 'max_depth', 100)
    positive_limit(max_total_reactions, 'max_total_reactions', 200)
    source_hash = _container(payload)
    routes, rejections, stock, formats = [], [], set(), set()
    for route_id, entry in payload.items():
        try:
            if not isinstance(entry, dict) or not {'path', 'idx'} <= entry.keys():
                raise ValueError('Top-N entry requires path and idx')
            if type(entry['idx']) is not int or entry['idx'] < 0:
                raise ValueError('Top-N idx must be a nonnegative integer')
            schema = _schema(entry['path'])
            tree = _Tree(schema, max_depth)
            actual = tree.visit(entry['path'])
            if actual != target:
                raise ValueError('Route does not end at the exact target')
            if not tree.reactions:
                raise ValueError('Procurement-only entry has no synthesis reaction')
            proposal = {'route_id': route_id, 'name': 'Original model route ' + route_id,
                        'reactions': list(tree.reactions.values()),
                        'rationale': 'Imported model-generated route; chemical feasibility is unverified.',
                        'feasibility_risks': ['Graph validity and source stock flags do not establish chemical feasibility.'],
                        'evidence_ids': [], 'self_assessment': 'proposed'}
            checked = route_graph(proposal, target, max_depth, max_total_reactions)
            leaves = set(checked['leaves'])
            checked.update(stock_connected=leaves <= tree.stock,
                           unknown_starting_materials=sorted(leaves - tree.stock),
                           source_inventory_observed=sorted(tree.stock),
                           source_provenance={'canonical_payload_sha256': source_hash,
                                              'entry_key': route_id, 'idx': entry['idx'],
                                              'schema': schema})
            routes.append(checked)
            stock.update(tree.stock)
            formats.add(schema)
        except (ValueError, RecursionError) as exc:
            rejections.append({'route_id': route_id, 'reason': str(exc)})
    return {'routes': routes, 'inventory_observed': sorted(stock), 'rejections': rejections,
            'provenance': {'canonical_payload_sha256': source_hash, 'container': 'rebolt-topk',
                           'formats': sorted(formats), 'raw_entry_count': len(payload),
                           'inventory_scope': 'source_local_observations_only',
                           'chemistry_assessment': 'not_performed'}}


def select_original(payload, route_ids):
    """Deep-copy selected original entries, preserving every key/type/sentinel."""
    _container(payload)
    if isinstance(route_ids, (str, bytes)):
        raise ValueError('Route IDs must be a bounded collection')
    try:
        selected = tuple(route_ids)
    except TypeError as exc:
        raise ValueError('Route IDs must be a collection') from exc
    if len(selected) > _MAX_ENTRIES or any(not isinstance(key, str) or key not in payload for key in selected):
        raise ValueError('Only original top-N entry IDs may be exported')
    keep = set(selected)
    return deepcopy({key: value for key, value in payload.items() if key in keep})
