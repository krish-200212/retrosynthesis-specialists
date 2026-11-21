"""Bound stereochemical construction plans without claiming chemical validation.

Element IDs use one-based atom indices after parsing the canonical exact target
SMILES. They identify the requested target element, not an atom correspondence
across reactions. A changed CIP label does not prove physical inversion, and
unchanged labels do not prove retention. Neither a source citation nor a matching
quotation establishes substrate applicability or experimental selectivity.
"""
from copy import deepcopy
import re

from jsonschema import Draft202012Validator

STRATEGIES = ('not_applicable', 'not_specified', 'chiral_pool', 'asymmetric_synthesis',
              'stereochemical_relay', 'resolution', 'mixed', 'unresolved')
ACTIONS = ('inherited', 'asymmetric_creation', 'substrate_controlled_creation',
           'resolution', 'inversion', 'unresolved')
SCOPES = ('exact_substrate', 'analogue', 'mechanistic_inference', 'unresolved')
CONFIGURATIONS = ('R', 'S', 'r', 's', 'E', 'Z')
_MEANING = ('Structural plan checks and source-text binding only. CIP labels do '
            'not establish atom correspondence, physical retention/inversion, '
            'reaction selectivity, or chemical feasibility.')


def _canonical(value):
    from .graphs import canonical_smiles
    return canonical_smiles(value)


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def stereo_plan_schema(*, require_origin_declarations=False):
    """Allocate a fresh strict internal annotation schema for every request."""
    if type(require_origin_declarations) is not bool:
        raise ValueError('Origin-declaration requirement must be a boolean')
    text = {'type': 'string', 'minLength': 1, 'maxLength': 4000}
    ids = {'type': 'array', 'maxItems': 64, 'uniqueItems': True,
           'items': {'type': 'string', 'minLength': 1, 'maxLength': 256}}
    risks = {'type': 'array', 'maxItems': 32, 'items': deepcopy(text)}
    selectivity = _object({'kind': {'type': 'string', 'enum': ['ee', 'er', 'dr', 'E/Z']},
        'value': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'source_id': {'type': 'string', 'minLength': 1, 'maxLength': 256},
        'quote': {'type': 'string', 'minLength': 1, 'maxLength': 1000}})
    control = _object({'target_element': {'type': 'string', 'minLength': 1,
            'maxLength': 100, 'pattern': r'^(atom:[1-9][0-9]*|bond:[1-9][0-9]*:[1-9][0-9]*)$'},
        'configuration': {'type': 'string', 'enum': list(CONFIGURATIONS)},
        'established_at': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
        'action': {'type': 'string', 'enum': list(ACTIONS)}, 'control': deepcopy(text),
        'evidence_ids': ids, 'evidence_scope': {'type': 'string', 'enum': list(SCOPES)},
        'reported_selectivity': {'anyOf': [{'type': 'null'}, selectivity]}, 'risks': risks})
    control['properties'].update({
        'origin_element': {'type': 'string', 'minLength': 1, 'maxLength': 100,
            'pattern': r'^(atom:[1-9][0-9]*|bond:[1-9][0-9]*:[1-9][0-9]*)$',
            'description': 'Element ID in the separately canonicalized established_at molecule.'},
        'origin_configuration': {'type': 'string', 'enum': list(CONFIGURATIONS)}})
    control['dependentRequired'] = {'origin_element': ['origin_configuration'],
                                    'origin_configuration': ['origin_element']}
    if require_origin_declarations:
        control['allOf'] = [{'if': {'properties': {'action': {'enum': [
            value for value in ACTIONS if value != 'unresolved']}}, 'required': ['action']},
            'then': {'required': ['origin_element', 'origin_configuration']}}]
    return _object({'strategy': {'type': 'string', 'enum': list(STRATEGIES)},
        'controls': {'type': 'array', 'maxItems': 128, 'items': control},
        'limitations': deepcopy(risks)})


def _shape(plan, *, require_origin_declarations=False):
    error = next(Draft202012Validator(stereo_plan_schema(
        require_origin_declarations=require_origin_declarations)).iter_errors(plan), None)
    if error:
        location = '/'.join(str(part) for part in error.absolute_path)
        raise ValueError('Invalid stereo plan at /' + location + ': ' + error.validator)


def _bond_id(bond):
    first, second = sorted((bond.GetBeginAtomIdx() + 1, bond.GetEndAtomIdx() + 1))
    return f'bond:{first}:{second}'


def _specified(molecule):
    from rdkit import Chem
    elements, unsupported = [], []
    tetrahedral = (Chem.ChiralType.CHI_TETRAHEDRAL_CW, Chem.ChiralType.CHI_TETRAHEDRAL_CCW)
    for atom in molecule.GetAtoms():
        tag = atom.GetChiralTag()
        if tag == Chem.ChiralType.CHI_UNSPECIFIED:
            continue
        identifier = f'atom:{atom.GetIdx() + 1}'
        code = atom.GetProp('_CIPCode') if atom.HasProp('_CIPCode') else None
        if tag in tetrahedral and code in CONFIGURATIONS[:4]:
            elements.append({'id': identifier, 'kind': 'tetrahedral', 'configuration': code})
        else:
            unsupported.append({'id': identifier, 'kind': str(tag),
                                'reason': 'Encoded stereo type or absolute configuration is unsupported.'})
    for bond in molecule.GetBonds():
        stereo = bond.GetStereo()
        if stereo in (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ):
            elements.append({'id': _bond_id(bond), 'kind': 'double_bond',
                             'configuration': 'E' if stereo == Chem.BondStereo.STEREOE else 'Z'})
        elif stereo not in (Chem.BondStereo.STEREONONE, Chem.BondStereo.STEREOANY):
            unsupported.append({'id': _bond_id(bond), 'kind': str(stereo),
                                'reason': 'Encoded bond stereo is unsupported.'})
    return elements, unsupported


def _unspecified(molecule):
    from rdkit import Chem
    values = []
    for info in Chem.FindPotentialStereo(molecule):
        if str(info.specified) != 'Unspecified':
            continue
        if str(info.type) == 'Atom_Tetrahedral':
            values.append({'id': f'atom:{info.centeredOn + 1}', 'kind': 'tetrahedral'})
        elif str(info.type) == 'Bond_Double':
            values.append({'id': _bond_id(molecule.GetBondWithIdx(info.centeredOn)),
                           'kind': 'double_bond'})
    return values


def target_stereo_requirements(target):
    """Describe encoded and unspecified stereo in the unchanged molecular ID."""
    from rdkit import Chem
    target = _canonical(target)
    molecule = Chem.MolFromSmiles(target)
    Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
    specified, unsupported = _specified(molecule)
    unspecified = _unspecified(molecule)
    mode = ('unsupported' if unsupported else 'specified' if specified else
            'unspecified' if unspecified else 'none')
    return {'target': target, 'mode': mode, 'specified_elements': specified,
        'unspecified_elements': unspecified, 'unsupported_elements': unsupported,
        'numbering': '1-based atom indices in canonical exact target SMILES; bond endpoints ascending.',
        'chemical_feasibility_established': False, 'meaning': _MEANING}


def _origin_declaration(control, origin, kind):
    if 'origin_element' not in control:
        return
    elements = {value['id']: value for value in target_stereo_requirements(origin)['specified_elements']}
    element = elements.get(control['origin_element'])
    if element is None or element['kind'] != kind:
        raise ValueError('Declared origin element is not an encoded stereo element of the requested type')
    if element['configuration'] != control['origin_configuration']:
        raise ValueError('Declared origin configuration differs from encoded origin at '
                         + element['id'] + ': expected ' + element['configuration'])


def _origin(control, route, kind):
    products = {_canonical(row['product']) for row in route['reactions']}
    precursors = {_canonical(value) for row in route['reactions'] for value in row['reactants']}
    origin = _canonical(control['established_at'])
    if origin not in products | precursors:
        raise ValueError('Stereo control origin is not a molecule in the route')
    _origin_declaration(control, origin, kind)
    if control['action'] == 'inherited':
        if origin not in precursors - products:
            raise ValueError('Inherited stereochemistry must name an explicit starting-material leaf')
    elif control['action'] != 'unresolved' and origin not in products:
        raise ValueError('Stereo-forming operation must name a reaction product')
    if control['action'] != 'unresolved':
        elements = target_stereo_requirements(origin)['specified_elements']
        if not any(value['kind'] == kind for value in elements):
            raise ValueError('Control origin lacks encoded stereochemistry of the requested type')
    if control['action'] == 'inversion':
        incoming = [value for row in route['reactions'] if _canonical(row['product']) == origin
                    for value in row['reactants']]
        if not any(element['kind'] == kind for value in incoming
                   for element in target_stereo_requirements(value)['specified_elements']):
            raise ValueError('Inversion claim lacks an encoded incoming stereo element')


def validate_stereo_plan(route, target, *, required=False, require_origin_declarations=False):
    """Check plan representation/coverage only, never chemical outcome."""
    if type(required) is not bool:
        raise ValueError('Stereo-plan requirement must be a boolean')
    if type(require_origin_declarations) is not bool:
        raise ValueError('Origin-declaration requirement must be a boolean')
    if 'stereo_plan' not in route:
        if required or require_origin_declarations:
            raise ValueError('A stereochemical construction plan is required')
        return {'status': 'legacy_no_plan', 'chemical_feasibility_established': False}
    plan = route['stereo_plan']
    _shape(plan, require_origin_declarations=require_origin_declarations)
    requirements = target_stereo_requirements(target)
    if requirements['unsupported_elements']:
        raise ValueError('Encoded target stereochemistry requires unsupported-type review')
    expected = {value['id']: value for value in requirements['specified_elements']}
    identifiers = [value['target_element'] for value in plan['controls']]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(expected):
        raise ValueError('Stereo controls must cover each specified target element exactly once')
    if expected and plan['strategy'] in ('not_applicable', 'not_specified'):
        raise ValueError('Specified target stereochemistry requires a control strategy')
    if not expected:
        strategy = 'not_specified' if requirements['unspecified_elements'] else 'not_applicable'
        if plan['strategy'] != strategy:
            raise ValueError('Plan must distinguish unspecified target stereo from no stereo requirement')
    citations = set(route.get('evidence_ids', []))
    for control in plan['controls']:
        element = expected[control['target_element']]
        if control['configuration'] != element['configuration']:
            raise ValueError('Stereo control configuration differs from exact target')
        if set(control['evidence_ids']) - citations:
            raise ValueError('Stereo-control evidence must also be cited by the route')
        _origin(control, route, element['kind'])
    return {'status': 'plan_structure_checked', 'requirements': requirements,
            'chemical_feasibility_established': False, 'stereochemical_outcome_established': False,
            'meaning': _MEANING}


def _normalized(value):
    return ' '.join(value.split())


def _issue(control, code, message):
    return {'target_element': control['target_element'], 'code': code, 'message': message}


_METRICS = {'ee': r'\be\.?\s*e\.?\b|enantiomeric\s+excess',
    'er': r'\be\.?\s*r\.?\b|enantiomeric\s+ratio',
    'dr': r'\bd\.?\s*r\.?\b|diastereomeric\s+ratio', 'E/Z': r'\bE\s*[:/]\s*Z\b'}


def _metric_value_bound(quote, value_pattern, kind):
    """Prevent borrowing a yield/other-selectivity number from the same quote."""
    for value in re.finditer(value_pattern, quote):
        for metric in re.finditer(_METRICS[kind], quote, re.IGNORECASE):
            if value.end() <= metric.start():
                gap = quote[value.end():metric.start()]
            elif metric.end() <= value.start():
                gap = quote[metric.end():value.start()]
            else:
                continue
            if len(gap) > 80 or re.search(r'\d|\b(?:yield|conversion|purity)\b|\bd\.?\s*e\.?\b|diastereomeric\s+excess', gap, re.IGNORECASE):
                continue
            if any(re.search(pattern, gap, re.IGNORECASE) for pattern in _METRICS.values()):
                continue
            return True
    return False


def _selectivity_issues(control, records):
    claim = control['reported_selectivity']
    if claim is None:
        return []
    source = claim['source_id']
    if source not in control['evidence_ids'] or source not in records:
        return [_issue(control, 'unbound_selectivity_source', 'Selectivity source is not a known cited control reference.')]
    quote = _normalized(claim['quote'])
    excerpt = _normalized(records[source].get('excerpt', ''))
    issues = []
    if quote not in excerpt:
        issues.append(_issue(control, 'selectivity_quote_not_found', 'Quoted selectivity text is absent from the recorded source excerpt.'))
    value = r'(?<![\d.])' + re.escape(_normalized(claim['value'])) + r'(?![\d.])'
    if not re.search(value, quote):
        issues.append(_issue(control, 'selectivity_value_not_found', 'Claimed selectivity value is absent from its quotation.'))
    if not re.search(_METRICS[claim['kind']], quote, re.IGNORECASE):
        issues.append(_issue(control, 'selectivity_metric_not_found', 'Quotation does not identify the claimed selectivity metric.'))
    elif not _metric_value_bound(quote, value, claim['kind']):
        issues.append(_issue(control, 'selectivity_value_metric_unbound', 'The quoted value is not locally associated with the claimed selectivity metric.'))
    return issues


def _control_assessment(control, records):
    known = [identifier for identifier in control['evidence_ids'] if identifier in records]
    issues = [_issue(control, 'unknown_evidence_id', 'Control cites an unknown evidence ID: ' + identifier)
              for identifier in control['evidence_ids'] if identifier not in records]
    if control['evidence_scope'] in ('exact_substrate', 'analogue') and not control['evidence_ids']:
        issues.append(_issue(control, 'unsupported_evidence_scope', 'Exact/analogue precedent requires a recorded source reference.'))
    issues.extend(_selectivity_issues(control, records))
    return {'target_element': control['target_element'],
        'declared_evidence_scope': control['evidence_scope'], 'known_evidence_ids': known,
        'source_scopes': [records[key].get('retrieval_scope', 'unknown') for key in known],
        'source_text_bound': bool(known) and not issues,
        'reported_selectivity_quote_bound': control['reported_selectivity'] is not None and not issues,
        'stereochemical_outcome_established': False, 'issues': issues}


def assess_stereo_evidence(route, evidence):
    """Bind reported text to recorded excerpts; applicability remains unverified."""
    if 'stereo_plan' not in route:
        return {'status': 'legacy_no_plan', 'controls': [], 'issues': [],
            'declared_supported_controls': 0, 'source_bound_controls': 0,
            'chemical_feasibility_established': False, 'stereochemical_outcome_established': False,
            'meaning': _MEANING}
    _shape(route['stereo_plan'])
    if not isinstance(evidence, (list, tuple)) or len(evidence) > 100:
        raise ValueError('Stereo evidence must be a bounded recorded-source collection')
    records = {}
    for row in evidence:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not isinstance(row.get('excerpt'), str):
            raise ValueError('Stereo evidence requires recorded IDs and text excerpts')
        if row['id'] in records:
            raise ValueError('Duplicate stereo evidence ID')
        records[row['id']] = row
    controls = [_control_assessment(control, records) for control in route['stereo_plan']['controls']]
    issues = [issue for control in controls for issue in control['issues']]
    bound = sum(control['source_text_bound'] for control in controls)
    status = ('unsupported_claims' if issues else 'source_bound_claims' if bound else
              route['stereo_plan']['strategy'] if not controls else 'unresolved')
    return {'status': status, 'controls': controls, 'issues': issues,
        'declared_supported_controls': sum(value['evidence_scope'] in ('exact_substrate', 'analogue')
                                           for value in route['stereo_plan']['controls']),
        'source_bound_controls': bound, 'chemical_feasibility_established': False,
        'stereochemical_outcome_established': False, 'meaning': _MEANING}
