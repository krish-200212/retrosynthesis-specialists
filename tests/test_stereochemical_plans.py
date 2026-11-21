"""Stereo construction claims remain distinct from molecular and source checks."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from retro_specialists.contracts import ContractError, parse_response, response_schema
from retro_specialists.finalization import _encoded, _fingerprint, verify_native_result
from retro_specialists.graphs import canonical_smiles, route_graph, validate_response
from retro_specialists.stereochemistry import (assess_stereo_evidence,
    stereo_plan_schema, target_stereo_requirements, validate_stereo_plan)

TARGET = 'C[C@H](O)c1ccccc1'


def hypothesis(target=TARGET):
    return {'route_id': 'fixture', 'name': 'Stereo hypothesis',
        'reactions': [{'product': target, 'reactants': ['CC(=O)c1ccccc1', '[H][H]']}],
        'rationale': 'Untested fixture with an unresolved stereochemical method.',
        'feasibility_risks': ['Not experimentally validated.'],
        'evidence_ids': [], 'self_assessment': 'proposed'}


def controlled(target=TARGET):
    element = target_stereo_requirements(target)['specified_elements'][0]
    return {**hypothesis(target), 'stereo_plan': {'strategy': 'unresolved',
        'controls': [{'target_element': element['id'],
            'configuration': element['configuration'], 'established_at': target,
            'action': 'unresolved', 'control': 'No stereoselective method established.',
            'evidence_ids': [], 'evidence_scope': 'unresolved',
            'reported_selectivity': None, 'risks': ['Stereochemical outcome unknown.']}],
        'limitations': ['These are declared control hypotheses, not chemical proof.']}}


def record(identifier='source:one', excerpt='The isolated alcohol was obtained with 99% ee.'):
    return {'id': identifier, 'excerpt': excerpt, 'retrieval_scope': 'retrieved_context'}


class TargetStereoTests(unittest.TestCase):
    def test_enantiomers_have_same_element_id_and_opposite_configuration(self):
        a = target_stereo_requirements(TARGET)
        b = target_stereo_requirements('C[C@@H](O)c1ccccc1')
        self.assertEqual(a['target'], canonical_smiles(TARGET))
        self.assertEqual(a['mode'], 'specified')
        self.assertEqual(a['specified_elements'][0]['id'], 'atom:2')
        self.assertEqual(a['specified_elements'][0]['configuration'], 'S')
        self.assertEqual(b['specified_elements'][0]['configuration'], 'R')
        self.assertFalse(a['chemical_feasibility_established'])

    def test_encoded_double_bond_geometry(self):
        a = target_stereo_requirements('C/C=C/C')['specified_elements'][0]
        b = target_stereo_requirements('C/C=C\\C')['specified_elements'][0]
        self.assertEqual((a['id'], a['configuration']), ('bond:2:3', 'E'))
        self.assertEqual(b['configuration'], 'Z')

    def test_unspecified_is_not_classified_as_achiral(self):
        for target in ('CC(O)c1ccccc1', 'CC=CC'):
            data = target_stereo_requirements(target)
            self.assertEqual(data['mode'], 'unspecified')
            self.assertEqual(data['specified_elements'], [])
            self.assertTrue(data['unspecified_elements'])
        self.assertEqual(target_stereo_requirements('CCO')['mode'], 'none')

    def test_multi_centre_target_reports_every_encoded_centre(self):
        target = 'N[C@@H](C)[C@H](O)C(=O)O'
        self.assertEqual(len(target_stereo_requirements(target)['specified_elements']), 2)

    def test_unsupported_explicit_stereo_is_disclosed(self):
        data = target_stereo_requirements('[Pt@SP1](Cl)(Br)(F)I')
        self.assertEqual(data['mode'], 'unsupported')
        self.assertTrue(data['unsupported_elements'])

    def test_exact_identity_preserves_components_and_isotopes(self):
        value = target_stereo_requirements('[13CH3][C@H](O)C(=O)O.[Na+]')
        self.assertIn('[13CH3]', value['target'])
        self.assertIn('.[Na+]', value['target'])


class StereoPlanContractTests(unittest.TestCase):
    def test_schema_construction_in_managed_runtime_does_not_import_rdkit(self):
        code = '''import sys
class BlockRDKit:
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='rdkit' or fullname.startswith('rdkit.'):raise ImportError('RDKit unavailable in managed runtime')
sys.meta_path.insert(0,BlockRDKit())
from retro_specialists.contracts import response_schema
assert 'stereo_plan' in response_schema(require_stereo_plan=True,require_origin_declarations=True)['properties']['routes']['items']['required']
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_optional_and_new_required_contract(self):
        payload = {'routes': [hypothesis()], 'limitations': []}
        self.assertEqual(len(parse_response(json.dumps(payload))['routes']), 1)
        with self.assertRaises(ContractError):
            parse_response(json.dumps(payload), require_stereo_plan=True)
        payload['routes'] = [controlled()]
        self.assertEqual(len(parse_response(json.dumps(payload), require_stereo_plan=True)['routes']), 1)

    def test_schema_allocations_do_not_share_nested_plan_fields(self):
        a = response_schema(require_stereo_plan=True)
        b = response_schema()
        a['properties']['routes']['items']['properties']['stereo_plan']['properties']['strategy']['enum'].append('forged')
        self.assertNotIn('stereo_plan', b['properties']['routes']['items']['required'])
        self.assertNotIn('forged', stereo_plan_schema()['properties']['strategy']['enum'])

    def test_strict_nested_fields_and_selectivity_shape(self):
        for change in ('extra', 'bad_action', 'bad_selectivity'):
            route = controlled()
            if change == 'extra': route['stereo_plan']['controls'][0]['confidence'] = 0.99
            if change == 'bad_action': route['stereo_plan']['controls'][0]['action'] = 'guaranteed'
            if change == 'bad_selectivity': route['stereo_plan']['controls'][0]['reported_selectivity'] = {'kind': 'ee', 'value': '99%'}
            with self.subTest(change=change), self.assertRaises(ContractError):
                parse_response(json.dumps({'routes': [route], 'limitations': []}))

    def test_requirement_keyword_type_is_strict(self):
        for value in (1, 'yes', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                response_schema(require_stereo_plan=value)


class StereoPlanValidationTests(unittest.TestCase):
    def test_valid_unresolved_control_is_retained_without_certification(self):
        route = controlled()
        checked = validate_response(json.dumps({'routes': [route], 'limitations': []}), TARGET,
                                    require_stereo_plan=True)
        self.assertEqual(checked['counts']['retained'], 1)
        self.assertEqual(checked['counts']['chemically_assessed'], 0)
        self.assertEqual(checked['routes'][0]['stereo_plan'], route['stereo_plan'])

    def test_missing_required_plan_is_rejected(self):
        checked = validate_response(json.dumps({'routes': [hypothesis()], 'limitations': []}), TARGET,
                                    require_stereo_plan=True)
        self.assertEqual(checked['counts']['retained'], 0)

    def test_controls_cannot_omit_duplicate_or_invent_elements(self):
        for change in ('missing', 'duplicate', 'invented', 'wrong_configuration'):
            route = controlled()
            control = route['stereo_plan']['controls'][0]
            if change == 'missing': route['stereo_plan']['controls'] = []
            if change == 'duplicate': route['stereo_plan']['controls'].append(deepcopy(control))
            if change == 'invented': control['target_element'] = 'atom:999'
            if change == 'wrong_configuration': control['configuration'] = 'R'
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_stereo_plan(route, TARGET, required=True)

    def test_control_origin_must_be_in_route_and_inherited_origin_stereo_specified(self):
        route = controlled()
        route['stereo_plan']['controls'][0]['established_at'] = 'CCC'
        with self.assertRaises(ValueError): validate_stereo_plan(route, TARGET)
        route['stereo_plan']['controls'][0].update(action='inherited', established_at='CC(=O)c1ccccc1')
        with self.assertRaises(ValueError): validate_stereo_plan(route, TARGET)

    def test_route_producing_opposite_enantiomer_still_rejected(self):
        route = controlled('C[C@@H](O)c1ccccc1')
        checked = validate_response(json.dumps({'routes': [route], 'limitations': []}), TARGET,
                                    require_stereo_plan=True)
        self.assertEqual(checked['counts']['retained'], 0)

    def test_claimed_creation_cannot_be_established_at_achiral_product(self):
        route = controlled()
        route['reactions'].append({'product': 'CC(=O)c1ccccc1', 'reactants': ['CC(O)c1ccccc1']})
        route['stereo_plan']['controls'][0].update(action='asymmetric_creation', established_at='CC(=O)c1ccccc1')
        with self.assertRaises(ValueError): validate_stereo_plan(route, TARGET)

    def test_mixed_specified_and_unspecified_target_elements_are_separate(self):
        target = 'N[C@@H](C)C(O)C(=O)O'
        data = target_stereo_requirements(target)
        self.assertEqual(data['mode'], 'specified')
        self.assertEqual(len(data['specified_elements']), 1)
        self.assertEqual(len(data['unspecified_elements']), 1)

    def test_plan_required_boolean_and_invalid_shape_fail_closed(self):
        with self.assertRaises(ValueError): validate_stereo_plan(hypothesis(), TARGET, required=1)
        route = controlled();route['stereo_plan']['extra'] = 'forged'
        with self.assertRaises(ValueError): validate_stereo_plan(route, TARGET)

    def test_general_and_unspecified_targets_have_explicit_nonclaim_plans(self):
        for target, strategy in (('CCO', 'not_applicable'), ('CC(O)c1ccccc1', 'not_specified')):
            route = hypothesis(target)
            route['stereo_plan'] = {'strategy': strategy, 'controls': [], 'limitations': ['No target configuration requested.']}
            validate_stereo_plan(route, target, required=True)
        route['stereo_plan']['strategy'] = 'not_applicable'
        with self.assertRaises(ValueError): validate_stereo_plan(route, route['reactions'][0]['product'])

    def test_unsupported_target_cannot_receive_false_complete_plan(self):
        target = '[Pt@SP1](Cl)(Br)(F)I'
        route = hypothesis(target)
        route['stereo_plan'] = {'strategy': 'not_applicable', 'controls': [], 'limitations': []}
        with self.assertRaises(ValueError): validate_stereo_plan(route, target, required=True)

    def test_claim_citations_must_be_declared_route_citations(self):
        route = controlled()
        route['stereo_plan']['controls'][0]['evidence_ids'] = ['forged:source']
        with self.assertRaises(ValueError): validate_stereo_plan(route, TARGET)

    def test_cip_comparison_is_not_a_physical_inversion_validator(self):
        precursor = 'F[C@H](CCBr)CCCl'
        target = 'F[C@H](CCO)CCCl'
        before = target_stereo_requirements(precursor)['specified_elements'][0]['configuration']
        after = target_stereo_requirements(target)['specified_elements'][0]['configuration']
        self.assertNotEqual(before, after)
        route = controlled(target)
        route['reactions'] = [{'product': target, 'reactants': [precursor, '[OH-]']}]
        route['stereo_plan']['strategy'] = 'chiral_pool'
        route['stereo_plan']['controls'][0].update(action='inherited', established_at=precursor)
        validate_stereo_plan(route, target)
        result = assess_stereo_evidence(route, [])
        self.assertFalse(result['stereochemical_outcome_established'])
        self.assertIn('CIP', result['meaning'])

    def test_inversion_claim_needs_an_encoded_incoming_stereo_element(self):
        route = controlled()
        route['stereo_plan']['controls'][0]['action'] = 'inversion'
        with self.assertRaises(ValueError): validate_stereo_plan(route, TARGET)

    def test_encoded_incoming_inversion_claim_remains_an_uncertified_declaration(self):
        route = controlled()
        route['reactions'][0]['reactants'] = ['C[C@@H](Br)c1ccccc1', '[OH-]']
        route['stereo_plan']['controls'][0]['action'] = 'inversion'
        validate_stereo_plan(route, TARGET)
        self.assertFalse(assess_stereo_evidence(route, [])['stereochemical_outcome_established'])

    def test_all_centres_must_be_accounted_for_in_multi_centre_route(self):
        target = 'N[C@@H](C)[C@H](O)C(=O)O'
        route = controlled(target)
        first = route['stereo_plan']['controls'][0]
        route['stereo_plan']['controls'] = [dict(first, target_element=e['id'], configuration=e['configuration'])
            for e in target_stereo_requirements(target)['specified_elements']]
        validate_stereo_plan(route, target)
        route['stereo_plan']['controls'].pop()
        with self.assertRaises(ValueError): validate_stereo_plan(route, target)


class StereoEvidenceTests(unittest.TestCase):
    def sourced(self):
        route = controlled()
        route['evidence_ids'] = ['source:one']
        route['stereo_plan']['controls'][0].update(evidence_ids=['source:one'],
            evidence_scope='exact_substrate', action='asymmetric_creation',
            control='Source-declared asymmetric reduction, applicability requires review.',
            reported_selectivity={'kind': 'ee', 'value': '99%', 'source_id': 'source:one',
                'quote': 'The isolated alcohol was obtained with 99% ee.'})
        return route

    def test_literal_source_binding_does_not_certify_selectivity(self):
        result = assess_stereo_evidence(self.sourced(), [record()])
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['source_bound_controls'], 1)
        self.assertFalse(result['chemical_feasibility_established'])
        self.assertFalse(result['stereochemical_outcome_established'])

    def test_unknown_citation_and_unsupported_number_are_flagged(self):
        for records, value in (([], '99%'), ([record()], '100%')):
            route = self.sourced()
            route['stereo_plan']['controls'][0]['reported_selectivity']['value'] = value
            result = assess_stereo_evidence(route, records)
            self.assertTrue(result['issues'])
            self.assertFalse(result['stereochemical_outcome_established'])

    def test_fabricated_quote_or_source_not_in_controls_is_flagged(self):
        for change in ('quote', 'source'):
            route = self.sourced()
            selectivity = route['stereo_plan']['controls'][0]['reported_selectivity']
            if change == 'quote': selectivity['quote'] = 'The opposite enantiomer was isolated at 99% ee.'
            else: selectivity['source_id'] = 'source:other'
            result = assess_stereo_evidence(route, [record(), record('source:other')])
            self.assertTrue(result['issues'])

    def test_exact_claim_without_source_has_no_source_binding(self):
        route = controlled()
        route['stereo_plan']['controls'][0]['evidence_scope'] = 'exact_substrate'
        result = assess_stereo_evidence(route, [])
        self.assertTrue(result['issues'])
        self.assertEqual(result['source_bound_controls'], 0)

    def test_selectivity_cannot_match_a_partial_numeric_token_or_wrong_metric(self):
        for value, kind in (('9%', 'ee'), ('99%', 'dr')):
            route = self.sourced()
            route['stereo_plan']['controls'][0]['reported_selectivity'].update(value=value, kind=kind)
            self.assertTrue(assess_stereo_evidence(route, [record()])['issues'])

    def test_quoted_yield_cannot_be_claimed_as_ee(self):
        route = self.sourced()
        quote = 'The isolated alcohol was obtained in 99% yield.'
        route['stereo_plan']['controls'][0]['reported_selectivity']['quote'] = quote
        self.assertTrue(assess_stereo_evidence(route, [record(excerpt=quote)])['issues'])

    def test_value_cannot_be_borrowed_from_another_metric_in_same_quote(self):
        for quote, kind in (('The result was 99% ee and a dr of 20:1.', 'dr'),
                            ('The product had 99% yield and 80% ee.', 'ee'),
                            ('The result was 99% conversion and an ee measurement.', 'ee'),
                            ('The result was 99% de and a dr measurement.', 'dr')):
            route = self.sourced()
            route['stereo_plan']['controls'][0]['reported_selectivity'].update(quote=quote, kind=kind)
            self.assertTrue(assess_stereo_evidence(route, [record(excerpt=quote)])['issues'])

    def test_source_collection_validation_and_empty_achiral_plan(self):
        for evidence in ({}, [{}], [record(), record()]):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                assess_stereo_evidence(controlled(), evidence)
        route = hypothesis('CCO')
        route['stereo_plan'] = {'strategy': 'not_applicable', 'controls': [], 'limitations': []}
        self.assertEqual(assess_stereo_evidence(route, [])['status'], 'not_applicable')

    def test_known_analogue_is_source_bound_but_not_exact_outcome_proof(self):
        route = self.sourced()
        route['stereo_plan']['controls'][0]['evidence_scope'] = 'analogue'
        result = assess_stereo_evidence(route, [record()])
        self.assertEqual(result['controls'][0]['declared_evidence_scope'], 'analogue')
        self.assertFalse(result['stereochemical_outcome_established'])

    def test_legacy_no_plan_is_explicit(self):
        result = assess_stereo_evidence(hypothesis(), [])
        self.assertEqual(result['status'], 'legacy_no_plan')
        self.assertFalse(result['stereochemical_outcome_established'])


class StereoReceiptTests(unittest.TestCase):
    def test_fingerprint_binds_plan_and_legacy_hash_is_unchanged(self):
        old = route_graph(hypothesis(), TARGET)
        fields = ('route_id', 'name', 'rationale', 'feasibility_risks', 'evidence_ids', 'self_assessment', 'graph_key')
        expected = sha256(json.dumps({k: old[k] for k in fields}, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.assertEqual(_fingerprint(old), expected)
        fresh = route_graph(controlled(), TARGET)
        changed = deepcopy(fresh)
        changed['stereo_plan']['controls'][0]['control'] = 'Changed claim after validation.'
        self.assertNotEqual(_fingerprint(fresh), _fingerprint(changed))

    def test_stored_plan_tampering_is_quarantined_and_legacy_still_replays(self):
        for with_plan in (False, True):
            route = controlled() if with_plan else hypothesis()
            payload = {'routes': [route], 'limitations': []}
            raw = json.dumps(payload)
            request = {'target': TARGET, 'raw_response': raw}
            response = {'status': 'completed', 'result': validate_response(raw, TARGET)}
            receipt = {'action': 'validate_route_hypotheses', 'request': request, 'response': response,
                'request_sha256': sha256(_encoded(request).encode()).hexdigest(),
                'response_sha256': sha256(_encoded(response).encode()).hexdigest()}
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / 'receipts'; folder.mkdir()
                (folder / ('a' * 32 + '.json')).write_text(json.dumps(receipt))
                self.assertEqual(verify_native_result(raw, 'a' * 32, TARGET, directory)['status'], 'verified_hypotheses')
                if with_plan:
                    payload['routes'][0]['stereo_plan']['limitations'].append('Changed later.')
                    self.assertEqual(verify_native_result(json.dumps(payload), 'a' * 32, TARGET, directory)['status'], 'quarantined')


class OriginDeclarationTests(unittest.TestCase):
    def inherited(self):
        precursor = 'F[C@H](CCBr)CCCl'
        target = 'F[C@H](CCO)CCCl'
        route = controlled(target)
        route['reactions'] = [{'product': target, 'reactants': [precursor, '[OH-]']}]
        route['stereo_plan']['strategy'] = 'chiral_pool'
        origin = target_stereo_requirements(precursor)['specified_elements'][0]
        control = route['stereo_plan']['controls'][0]
        control.update(action='inherited', established_at=precursor,
            origin_element=origin['id'], origin_configuration=origin['configuration'])
        return route, target

    def test_new_policy_requires_origins_but_legacy_v2_remains_valid(self):
        route, target = self.inherited()
        control = route['stereo_plan']['controls'][0]
        del control['origin_element']; del control['origin_configuration']
        validate_stereo_plan(route, target)
        with self.assertRaises(ValueError):
            validate_stereo_plan(route, target, require_origin_declarations=True)
        payload = json.dumps({'routes': [route], 'limitations': []})
        self.assertTrue(parse_response(payload)['routes'])
        with self.assertRaises(ContractError):
            parse_response(payload, require_origin_declarations=True)

    def test_origin_and_target_cip_can_differ_without_asserting_inversion(self):
        route, target = self.inherited()
        control = route['stereo_plan']['controls'][0]
        self.assertNotEqual(control['configuration'], control['origin_configuration'])
        validate_stereo_plan(route, target, require_origin_declarations=True)
        checked = validate_response(json.dumps({'routes': [route], 'limitations': []}), target,
            require_stereo_plan=True, require_origin_declarations=True)
        self.assertEqual(checked['counts']['retained'], 1)
        self.assertFalse(assess_stereo_evidence(route, [])['stereochemical_outcome_established'])

    def test_wrong_precursor_enantiomer_is_rejected(self):
        route, target = self.inherited()
        control = route['stereo_plan']['controls'][0]
        control['origin_configuration'] = 'R' if control['origin_configuration'] == 'S' else 'S'
        for required in (False, True):
            with self.subTest(required=required), self.assertRaises(ValueError):
                validate_stereo_plan(route, target, require_origin_declarations=required)

    def test_live_citronellal_configuration_contradiction_is_rejected(self):
        precursor = 'CC(C)=CCC[C@H](C)CC=O'
        origin = target_stereo_requirements(precursor)['specified_elements'][0]
        self.assertEqual(origin['configuration'], 'S')
        route = controlled()
        route['reactions'][0]['reactants'] = [precursor]
        route['stereo_plan']['controls'][0].update(action='inherited', established_at=precursor,
            origin_element=origin['id'], origin_configuration='R')
        checked = validate_response(json.dumps({'routes': [route], 'limitations': []}), TARGET,
            require_stereo_plan=True, require_origin_declarations=True)
        self.assertEqual(checked['counts']['retained'], 0)
        self.assertIn('origin', checked['rejections'][0]['reason'].lower())

    def test_missing_or_nonstereogenic_origin_id_is_rejected(self):
        for value in ('atom:999', 'atom:1', 'bond:1:2'):
            route, target = self.inherited()
            route['stereo_plan']['controls'][0]['origin_element'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_stereo_plan(route, target, require_origin_declarations=True)

    def test_unresolved_controls_and_no_stereo_targets_need_no_origin(self):
        validate_stereo_plan(controlled(), TARGET, require_origin_declarations=True)
        route = hypothesis('CCO')
        route['stereo_plan'] = {'strategy': 'not_applicable', 'controls': [], 'limitations': []}
        validate_stereo_plan(route, 'CCO', require_origin_declarations=True)

    def test_partial_origin_declarations_are_not_accepted(self):
        route, _ = self.inherited()
        del route['stereo_plan']['controls'][0]['origin_configuration']
        with self.assertRaises(ContractError):
            parse_response(json.dumps({'routes': [route], 'limitations': []}))

    def test_origin_policy_flag_is_strict_and_implies_plan_presence(self):
        for invalid in (1, None, 'true'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                response_schema(require_origin_declarations=invalid)
        with self.assertRaises(ValueError):
            validate_stereo_plan(hypothesis(), TARGET, require_origin_declarations=True)

    def test_origin_claims_are_bound_into_the_existing_receipt_fingerprint(self):
        route, target = self.inherited()
        checked = route_graph(route, target)
        changed = deepcopy(checked)
        changed['stereo_plan']['controls'][0]['origin_configuration'] = 'forged'
        self.assertNotEqual(_fingerprint(checked), _fingerprint(changed))

    def test_required_origin_schema_is_separately_allocated(self):
        first = response_schema(require_origin_declarations=True)
        second = response_schema(require_origin_declarations=True)
        control = first['properties']['routes']['items']['properties']['stereo_plan']['properties']['controls']['items']
        control['allOf'][0]['then']['required'].append('forged')
        other = second['properties']['routes']['items']['properties']['stereo_plan']['properties']['controls']['items']
        self.assertNotIn('forged', other['allOf'][0]['then']['required'])


if __name__ == '__main__': unittest.main()
