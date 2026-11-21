"""A specialist's final text cannot promote unvalidated or changed graphs."""
import json
import sys
from pathlib import Path
import tempfile
import unittest
from retro_specialists.finalization import verify_native_result, verify_native_envelope
from tests.test_plugin import Context
from retro_specialists.plugin import PrimitiveTools


def proposal():
    return {'route_id': 'r1', 'name': 'Fixture', 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']}],
            'rationale': 'Fixture hypothesis', 'feasibility_risks': ['Unassessed'],
            'evidence_ids': [], 'self_assessment': 'proposed',
            'stereo_plan': {'strategy': 'not_applicable', 'controls': [], 'limitations': []}}


class FinalizationTests(unittest.TestCase):
    def receipt(self, tmp):
        payload = {'routes': [proposal()], 'limitations': []}
        service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable,
            backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
        result = json.loads(service.handle('validate_route_hypotheses', {'target': 'CCO', 'raw_response': json.dumps(payload)}))
        self.assertEqual(result['status'], 'completed', result)
        return payload, result['receipt_id']
    def test_matching_final_uses_stored_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, receipt = self.receipt(tmp)
            result = verify_native_result(json.dumps(payload), receipt, 'CCO', tmp)
            self.assertEqual(result['status'], 'verified_hypotheses')
            self.assertFalse(result['chemical_feasibility_established'])
            self.assertEqual(len(result['routes']), 1)
    def test_changed_graph_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, receipt = self.receipt(tmp)
            payload['routes'][0]['reactions'][0]['reactants'] = ['CC']
            result = verify_native_result(json.dumps(payload), receipt, 'CCO', tmp)
            self.assertEqual(result['status'], 'quarantined')
            self.assertEqual(result['routes'], [])
    def test_changed_claims_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, receipt = self.receipt(tmp)
            payload['routes'][0]['rationale'] = 'Experimentally validated high-yield synthesis'
            self.assertEqual(verify_native_result(json.dumps(payload), receipt, 'CCO', tmp)['status'], 'quarantined')
    def test_skipped_validation_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_native_result(json.dumps({'routes': [proposal()], 'limitations': []}), 'a' * 32, 'CCO', tmp)
            self.assertEqual(result['status'], 'quarantined')
    def test_wrong_target_and_receipt_traversal_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, receipt = self.receipt(tmp)
            self.assertEqual(verify_native_result(json.dumps(payload), receipt, 'CCC', tmp)['status'], 'quarantined')
            self.assertEqual(verify_native_result(json.dumps(payload), '../secret', 'CCO', tmp)['status'], 'quarantined')
    def test_tampered_receipt_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, receipt = self.receipt(tmp)
            path = Path(tmp) / 'receipts' / (receipt + '.json')
            row = json.loads(path.read_text()); row['response']['result']['routes'][0]['rationale'] = 'changed'
            path.write_text(json.dumps(row))
            self.assertEqual(verify_native_result(json.dumps(payload), receipt, 'CCO', tmp)['status'], 'quarantined')

    def test_envelope_matches_backend_and_changed_status_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, receipt = self.receipt(tmp)
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable,
                backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
            raw = service.handle('verify_retrosynthesis_result', {'final_response': json.dumps(payload),
                'validation_receipt_id': receipt, 'target': 'CCO'})
            self.assertEqual(verify_native_envelope(raw, 'CCO', tmp, requested_count=5, max_depth=5, max_total_reactions=20)['status'], 'verified_hypotheses')
            forged = json.loads(raw); forged['result']['chemical_feasibility_established'] = True
            self.assertEqual(verify_native_envelope(json.dumps(forged), 'CCO', tmp, requested_count=5, max_depth=5, max_total_reactions=20)['status'], 'quarantined')
    def test_envelope_skipped_final_verification_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, receipt = self.receipt(tmp)
            raw = json.dumps({'status': 'completed', 'result': payload, 'receipt_id': receipt})
            self.assertEqual(verify_native_envelope(raw, 'CCO', tmp, requested_count=5, max_depth=5, max_total_reactions=20)['status'], 'quarantined')

if __name__ == '__main__':
    unittest.main()
