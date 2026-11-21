"""Main's limits must not be inherited from an untrusted child's validation call."""
import json
import sys
from pathlib import Path
import tempfile
import unittest
from retro_specialists.finalization import verify_native_envelope
from retro_specialists.plugin import PrimitiveTools, register
from tests.test_plugin import Context
from tests.test_native_finalization import proposal

LIMITS = {'requested_count': 5, 'max_depth': 5, 'max_total_reactions': 20}

class AcceptanceConstraintsTests(unittest.TestCase):
    def envelope(self, directory, routes):
        service = PrimitiveTools(Context(directory, enabled=True, backend_python=sys.executable,
            backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
        raw = json.dumps({'routes': routes, 'limitations': []})
        validation = json.loads(service.handle('validate_route_hypotheses', {'target': 'CCO',
            'raw_response': raw, 'requested_count': 20, 'max_depth': 30, 'max_total_reactions': 100}))
        self.assertEqual(validation['result']['counts']['retained'], len(routes))
        final = service.handle('verify_retrosynthesis_result', {'target': 'CCO',
            'final_response': raw, 'validation_receipt_id': validation['receipt_id']})
        self.assertEqual(json.loads(final)['result']['status'], 'verified_hypotheses')
        return service, final

    def test_missing_main_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, envelope = self.envelope(tmp, [proposal()])
            result = verify_native_envelope(envelope, 'CCO', tmp)
            self.assertEqual(result['status'], 'quarantined')
            self.assertIn('Main acceptance requires explicit', result['reason'])
            result = json.loads(service.handle('accept_retrosynthesis_result',
                {'final_envelope': envelope, 'target': 'CCO'}))
            self.assertEqual(result['status'], 'invalid_request')

    def test_child_cannot_raise_depth_limit(self):
        route = {**proposal(), 'reactions': [
            {'product': 'CCO', 'reactants': ['CC=O']},
            {'product': 'CC=O', 'reactants': ['CC']}]}
        with tempfile.TemporaryDirectory() as tmp:
            _, envelope = self.envelope(tmp, [route])
            rejected = verify_native_envelope(envelope, 'CCO', tmp, **{**LIMITS, 'max_depth': 1})
            self.assertEqual(rejected['status'], 'quarantined')
            accepted = verify_native_envelope(envelope, 'CCO', tmp, **{**LIMITS, 'max_depth': 2})
            self.assertEqual(accepted['status'], 'verified_hypotheses')
            self.assertEqual(accepted['acceptance_constraints']['max_depth'], 2)

    def test_depth_and_total_reactions_are_separate(self):
        route = {**proposal(), 'reactions': [
            {'product': 'CCO', 'reactants': ['CC=O', 'O']},
            {'product': 'CC=O', 'reactants': ['CC']},
            {'product': 'O', 'reactants': ['OO']}]}
        with tempfile.TemporaryDirectory() as tmp:
            _, envelope = self.envelope(tmp, [route])
            rejected = verify_native_envelope(envelope, 'CCO', tmp,
                **{**LIMITS, 'max_depth': 2, 'max_total_reactions': 2})
            self.assertEqual(rejected['status'], 'quarantined')
            accepted = verify_native_envelope(envelope, 'CCO', tmp,
                **{**LIMITS, 'max_depth': 2, 'max_total_reactions': 3})
            self.assertEqual(accepted['status'], 'verified_hypotheses')

    def test_child_cannot_raise_requested_count(self):
        other = {**proposal(), 'route_id': 'second',
                 'reactions': [{'product': 'CCO', 'reactants': ['CC']}]}
        with tempfile.TemporaryDirectory() as tmp:
            service, envelope = self.envelope(tmp, [proposal(), other])
            result = json.loads(service.handle('accept_retrosynthesis_result', {
                'final_envelope': envelope, 'target': 'CCO', **LIMITS, 'requested_count': 1}))
            self.assertEqual(result['result']['status'], 'quarantined')

    def test_empty_response_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, envelope = self.envelope(tmp, [])
            result = verify_native_envelope(envelope, 'CCO', tmp, **LIMITS)
            self.assertEqual(result['status'], 'verified_hypotheses')
            for name in LIMITS:
                with self.subTest(name=name):
                    result = verify_native_envelope(envelope, 'CCO', tmp, **{**LIMITS, name: True})
                    self.assertEqual(result['status'], 'quarantined')

    def test_schema_requires_original_task_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = Context(tmp)
            register(context)
            schema = context.registered['accept_retrosynthesis_result']['schema']['parameters']
            self.assertTrue(set(LIMITS).issubset(schema['required']))

if __name__ == '__main__':
    unittest.main()
