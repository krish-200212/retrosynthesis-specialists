"""Immutable host task records bind native tool calls and receipt acceptance."""
from hashlib import sha256
import json
import sys
from pathlib import Path
import tempfile
import unittest

from retro_specialists.plugin import PrimitiveTools, register
from retro_specialists.task_context import create_task_context
from tests.test_native_finalization import proposal
from tests.test_plugin import Context
from tests.legacy_task_fixtures import create_legacy_task_fixture


class TaskBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.service = PrimitiveTools(Context(self.root, enabled=True, backend_python=sys.executable,
            backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
        self.task = self.create()
        current = {**proposal(), 'stereo_plan': {'strategy': 'not_applicable', 'controls': [], 'limitations': []}}
        self.raw = json.dumps({'routes': [current], 'limitations': []})

    def create(self, role='route_proposer', **kwargs):
        factory = create_task_context if role == 'route_proposer' else create_legacy_task_fixture
        return factory(self.root, 'CCO', role, 1, 1, 1, **kwargs)

    def offline_cost(self, **args):
        from retro_specialists.backend_worker import _cost
        trusted = {'state_dir': self.root, 'quotes_file': '', 'baseline_file': '',
                   'baseline_sha256': '', 'cost_assumptions': {}}
        return _cost(args, trusted)

    def call(self, action, **args):
        return json.loads(self.service.handle(action, args))

    def validation(self, **changes):
        return self.call('validate_route_hypotheses', **{'raw_response': self.raw, 'target': 'CCO',
            'task_id': self.task['task_id'], **changes})

    def verification(self, receipt, **changes):
        return self.call('verify_retrosynthesis_result', **{'final_response': self.raw,
            'validation_receipt_id': receipt, 'target': 'CCO', 'task_id': self.task['task_id'], **changes})

    def acceptance(self, envelope, **changes):
        return self.call('accept_retrosynthesis_result', **{'final_envelope': json.dumps(envelope),
            'target': 'CCO', 'task_id': self.task['task_id'], **self.task['constraints'], **changes})

    def test_bound_roundtrip_reads_exact_record_and_preserves_binding(self):
        read = self.call('read_retrosynthesis_task', task_id=self.task['task_id'])
        self.assertEqual(read['result'], self.task)
        validation = self.validation()
        self.assertEqual(validation['status'], 'completed', validation)
        verified = self.verification(validation['receipt_id'])
        accepted = self.acceptance(verified)
        self.assertEqual(accepted['result']['status'], 'verified_hypotheses', accepted)
        for value in (validation, verified, accepted):
            self.assertEqual(value['result']['task_id'], self.task['task_id'])
            self.assertEqual(value['result']['task_content_sha256'], self.task['content_sha256'])
        self.assertIn('Immutable', accepted['result']['acceptance_constraint_source'])

    def test_validator_rejects_wrong_target_or_changed_limits(self):
        for changes in ({'target': 'CCC'}, {'requested_count': 2}, {'max_depth': 2}, {'max_total_reactions': 2}):
            with self.subTest(changes=changes):
                result = self.validation(**changes)
                self.assertEqual(result['status'], 'backend_error', result)

    def test_verifier_rejects_cross_task_and_missing_id(self):
        validation = self.validation()
        other = self.create()
        for changes in ({'task_id': other['task_id']}, {'target': 'CCC'}):
            result = self.verification(validation['receipt_id'], **changes)
            self.assertEqual(result['result']['status'], 'quarantined', result)
        omitted = self.call('verify_retrosynthesis_result', final_response=self.raw,
            validation_receipt_id=validation['receipt_id'], target='CCO')
        self.assertEqual(omitted['result']['status'], 'quarantined', omitted)

    def test_main_rejects_cross_task_missing_id_and_raised_limits(self):
        verified = self.verification(self.validation()['receipt_id'])
        for changes in ({'task_id': self.create()['task_id']}, {'max_depth': 2},
                        {'requested_count': 2}, {'max_total_reactions': 2}, {'target': 'CCC'}):
            result = self.acceptance(verified, **changes)
            self.assertEqual(result['result']['status'], 'quarantined', result)
        omitted = self.call('accept_retrosynthesis_result', final_envelope=json.dumps(verified),
            target='CCO', **self.task['constraints'])
        self.assertEqual(omitted['result']['status'], 'quarantined', omitted)

    def test_legacy_receipt_cannot_be_promoted_into_bound_task(self):
        validation = self.call('validate_route_hypotheses', raw_response=self.raw, target='CCO')
        result = self.verification(validation['receipt_id'])
        self.assertEqual(result['result']['status'], 'quarantined', result)

    def test_cost_uses_stored_baseline_and_rejects_model_corruption(self):
        task = self.create('cost_reducer', baseline_routes=[proposal()], source_sha256='a' * 64,
                           baseline_total_count=2)
        candidate = {**proposal(), 'route_id': 'candidate', 'reactions': [{'product': 'CCO', 'reactants': ['CC']}]}
        result = self.offline_cost(task_id=task['task_id'], candidate_routes=[candidate])
        self.assertFalse(result['tool_baseline_verified'])
        self.assertEqual(result['improvements'], [])
        self.assertEqual(result['baseline_provenance']['source_sha256'], 'a' * 64)
        with self.assertRaises(ValueError):
            self.offline_cost(task_id=task['task_id'], baseline_routes=[candidate], candidate_routes=[candidate])
        self.assertEqual(self.call('compare_route_costs', task_id=task['task_id'],
                                  candidate_routes=[candidate])['status'], 'invalid_request')

    def test_cost_enforces_role_exact_target_and_candidate_limits(self):
        with self.assertRaises(ValueError):
            self.offline_cost(task_id=self.task['task_id'], candidate_routes=[proposal()])
        task = self.create('cost_reducer', baseline_routes=[proposal()])
        for routes in ([{**proposal(), 'reactions': [{'product': 'CCC', 'reactants': ['CC']}]}],
                       [proposal(), {**proposal(), 'route_id': 'second'}],
                       [{**proposal(), 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']},
                                                    {'product': 'CC=O', 'reactants': ['CC']}]}]):
            with self.assertRaises(ValueError):
                self.offline_cost(task_id=task['task_id'], candidate_routes=routes)

    def test_configured_artifact_subset_remains_hypothetical(self):
        from retro_specialists.backend_worker import _cost
        candidate = {**proposal(), 'route_id': 'candidate', 'reactions': [{'product': 'CCO', 'reactants': ['CC']}]}
        artifact = Path(self.root) / 'baseline.json'
        artifact.write_text(json.dumps({'source': 'fixture tool output', 'routes': [proposal(), candidate]}))
        task = self.create('cost_reducer', baseline_routes=[proposal()],
            source_sha256=sha256(artifact.read_bytes()).hexdigest(), baseline_total_count=2)
        quotes = Path(self.root) / 'quotes.json'
        quotes.write_text(json.dumps([{'molecule': molecule, 'currency': 'USD', 'amount': amount,
            'quantity': 1, 'unit': 'mol', 'source': 'offline fixture',
            'retrieved_at': '2026-01-01T00:00:00Z'} for molecule, amount in [('CC=O', 10), ('CC', 1)]]))
        trusted = {'state_dir': self.root, 'quotes_file': str(quotes), 'baseline_file': str(artifact),
            'baseline_sha256': sha256(artifact.read_bytes()).hexdigest(), 'cost_assumptions': {
                'mode': 'inventory_catalog_price_proxy', 'currency': 'USD', 'target_mol': 1,
                'default_yield': 1, 'default_equivalents': 1}}
        result = _cost({'task_id': task['task_id'], 'candidate_routes': [candidate]}, trusted)
        self.assertFalse(result['tool_baseline_verified'])
        self.assertEqual(result['improvements'], [])
        self.assertEqual(len(result['hypothetical_comparisons']), 1)
        self.assertEqual(result['baseline_provenance']['status'], 'trusted_tool_artifact_subset')

    def test_configured_selection_cannot_claim_full_pool_improvement(self):
        from retro_specialists.backend_worker import _cost
        candidate = {**proposal(), 'route_id': 'candidate', 'reactions': [{'product': 'CCO', 'reactants': ['CC']}]}
        artifact = Path(self.root) / 'selection.json'
        artifact.write_text(json.dumps({'source': 'canonical selected tool routes', 'routes': [proposal()]}))
        artifact_sha = sha256(artifact.read_bytes()).hexdigest()
        quotes = Path(self.root) / 'quotes.json'
        quotes.write_text(json.dumps([{'molecule': molecule, 'currency': 'USD', 'amount': amount,
            'quantity': 1, 'unit': 'mol', 'source': 'offline fixture',
            'retrieved_at': '2026-01-01T00:00:00Z'} for molecule, amount in [('CC=O', 10), ('CC', 1)]]))
        trusted = {'state_dir': self.root, 'quotes_file': str(quotes), 'baseline_file': str(artifact),
            'baseline_sha256': artifact_sha, 'cost_assumptions': {
                'mode': 'inventory_catalog_price_proxy', 'currency': 'USD', 'target_mol': 1,
                'default_yield': 1, 'default_equivalents': 1}}
        for total, omitted, verified in ((3, 2, False), (None, None, False), (1, 0, True)):
            with self.subTest(total=total):
                task = self.create('cost_reducer', baseline_routes=[proposal()],
                    source_sha256='b' * 64, baseline_total_count=total)
                result = _cost({'task_id': task['task_id'], 'candidate_routes': [candidate]}, trusted)
                provenance = result['baseline_provenance']
                self.assertEqual(result['tool_baseline_verified'], verified)
                self.assertEqual(len(result['improvements']), int(verified))
                self.assertEqual(len(result['hypothetical_comparisons']), int(not verified))
                self.assertEqual(provenance['omitted_baseline_count'], omitted)
                self.assertEqual(provenance['source_sha256'], 'b' * 64)
                self.assertEqual(provenance['sha256'], artifact_sha)
                self.assertEqual(provenance['status'], 'trusted_tool_artifact' if verified else
                                 'trusted_tool_artifact_subset')

    def test_host_inventory_controls_validation_and_survives_finalization(self):
        self.task = self.create(inventory=['CC=O'])
        validated = self.validation()
        self.assertEqual(validated['result']['counts']['stock_connected'], 1)
        verified = self.verification(validated['receipt_id'])
        accepted = self.acceptance(verified)
        self.assertEqual(accepted['result']['routes'][0], validated['result']['routes'][0])

    def test_proposer_read_contains_no_baseline_and_creation_is_not_a_tool(self):
        context = Context(self.root)
        register(context)
        self.assertIn('read_retrosynthesis_task', context.registered)
        self.assertNotIn('create_task_context', context.registered)
        result = self.call('read_retrosynthesis_task', task_id=self.task['task_id'])
        self.assertNotIn('baseline_routes', result['result'])
        with self.assertRaises(ValueError):
            self.create(baseline_routes=[proposal()])

    def test_task_id_schema_and_runtime_reject_unbounded_identifiers(self):
        context = Context(self.root)
        register(context)
        self.assertNotIn('compare_route_costs', context.registered)
        for name in ('read_retrosynthesis_task', 'validate_route_hypotheses',
                     'verify_retrosynthesis_result', 'accept_retrosynthesis_result'):
            schema = context.registered[name]['schema']['parameters']['properties']['task_id']
            self.assertEqual(schema['pattern'], '^[a-f0-9]{32}$')
        for identifier in ('../secret', 'a' * 33, '', None):
            self.assertEqual(self.call('read_retrosynthesis_task', task_id=identifier)['status'], 'invalid_request')


if __name__ == '__main__':
    unittest.main()
