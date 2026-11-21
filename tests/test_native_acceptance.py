"""Main accepts an intact specialist envelope rather than revalidating expanded routes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from retro_specialists.plugin import PrimitiveTools, register
from tests.test_plugin import Context
from tests.test_native_finalization import proposal


class AcceptanceTests(unittest.TestCase):
    def run_case(self, tmp):
        service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable,
            backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
        payload = {'routes': [proposal()], 'limitations': []}
        validation = json.loads(service.handle('validate_route_hypotheses', {'raw_response': json.dumps(payload), 'target': 'CCO'}))
        verified = json.loads(service.handle('verify_retrosynthesis_result', {'final_response': json.dumps(payload),
            'validation_receipt_id': validation['receipt_id'], 'target': 'CCO'}))
        return service, verified
    def test_main_accepts_full_backend_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, verified = self.run_case(tmp)
            accepted = json.loads(service.handle('accept_retrosynthesis_result', {'final_envelope': json.dumps(verified), 'target': 'CCO', 'requested_count': 5, 'max_depth': 5, 'max_total_reactions': 20}))
            self.assertEqual(accepted['status'], 'completed')
            self.assertEqual(accepted['result']['status'], 'verified_hypotheses')
            self.assertEqual(len(accepted['result']['routes']), 1)
    def test_observed_native_named_envelope_is_receipt_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, verified = self.run_case(tmp)
            final = {'verified_result': verified['result'], 'verification_receipt_id': verified['receipt_id']}
            accepted = json.loads(service.handle('accept_retrosynthesis_result', {'final_envelope': json.dumps(final), 'target': 'CCO', 'requested_count': 5, 'max_depth': 5, 'max_total_reactions': 20}))
            self.assertEqual(accepted['result']['status'], 'verified_hypotheses')
            final['verified_result']['chemical_feasibility_established'] = True
            rejected = json.loads(service.handle('accept_retrosynthesis_result', {'final_envelope': json.dumps(final), 'target': 'CCO', 'requested_count': 5, 'max_depth': 5, 'max_total_reactions': 20}))
            self.assertEqual(rejected['result']['status'], 'quarantined')
    def test_expanded_route_array_is_not_an_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, verified = self.run_case(tmp)
            accepted = json.loads(service.handle('accept_retrosynthesis_result', {'final_envelope': json.dumps(verified['result']), 'target': 'CCO', 'requested_count': 5, 'max_depth': 5, 'max_total_reactions': 20}))
            self.assertEqual(accepted['result']['status'], 'quarantined')
    def test_main_tool_description_distinguishes_envelope_from_raw_route_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = Context(tmp); register(context)
            tool = context.registered['accept_retrosynthesis_result']
            self.assertIn('entire final JSON', tool['description'])
            self.assertNotIn('final_response', tool['schema']['parameters']['properties'])

if __name__ == '__main__':
    unittest.main()
