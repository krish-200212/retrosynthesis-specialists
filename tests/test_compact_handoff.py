"""Compact receipts reduce copying without trusting model-written route data."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from retro_specialists.finalization import verify_native_envelope
from retro_specialists.plugin import PrimitiveTools, register
from retro_specialists.task_context import create_task_context
from tests.test_native_finalization import proposal
from tests.test_plugin import Context


class CompactHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = PrimitiveTools(Context(self.root, enabled=True, backend_python=sys.executable,
            backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
        self.task = create_task_context(self.root, 'CCO', 'route_proposer', 1, 2, 3)
        self.limits = self.task['constraints']
        self.payload = {'routes': [proposal()], 'limitations': ['Fixture only; chemistry unassessed']}

    def call(self, action, **args):
        return json.loads(self.service.handle(action, args))

    def verify(self, compact=True, payload=None):
        payload = self.payload if payload is None else payload
        validated = self.call('validate_route_hypotheses', raw_response=payload,
                              target='CCO', task_id=self.task['task_id'], **self.limits)
        args = {'final_response': payload, 'validation_receipt_id': validated['receipt_id'],
                'target': 'CCO', 'task_id': self.task['task_id']}
        if compact is not None:
            args['compact_handoff'] = compact
        return self.call('verify_retrosynthesis_result', **args)

    def accept(self, envelope, **changes):
        args = {'target': 'CCO', 'state_dir': self.root, 'task_id': self.task['task_id'], **self.limits, **changes}
        return verify_native_envelope(json.dumps(envelope), **args)

    def test_real_backend_compact_roundtrip_and_full_receipt_persistence(self):
        compact = self.verify()
        self.assertEqual(set(compact), {'status', 'receipt_id', 'response_sha256'})
        self.assertEqual(compact['status'], 'completed')
        self.assertRegex(compact['receipt_id'], '^[a-f0-9]{32}$')
        self.assertRegex(compact['response_sha256'], '^[a-f0-9]{64}$')
        receipt = json.loads((self.root/'receipts'/(compact['receipt_id']+'.json')).read_text())
        self.assertNotIn('compact_handoff', receipt['request'])
        self.assertEqual(receipt['response_sha256'], compact['response_sha256'])
        self.assertEqual(len(receipt['response']['result']['routes']), 1)
        accepted = self.call('accept_retrosynthesis_result', final_envelope=json.dumps(compact),
                            target='CCO', task_id=self.task['task_id'], **self.limits)
        self.assertEqual(accepted['result']['status'], 'verified_hypotheses', accepted)
        self.assertFalse(accepted['result']['chemical_feasibility_established'])
        self.assertEqual(len(accepted['result']['routes']), 1)

    def test_omitted_and_false_option_preserve_legacy_full_envelope(self):
        for flag in (None, False):
            with self.subTest(flag=flag):
                envelope = self.verify(flag)
                self.assertEqual(set(envelope), {'status', 'result', 'receipt_id'})
                self.assertEqual(self.accept(envelope)['status'], 'verified_hypotheses')

    def test_empty_result_also_has_valid_compact_handoff(self):
        envelope = self.verify(payload={'routes': [], 'limitations': []})
        self.assertEqual(set(envelope), {'status', 'receipt_id', 'response_sha256'})
        accepted = self.accept(envelope)
        self.assertEqual(accepted['status'], 'verified_hypotheses', accepted)
        self.assertEqual(accepted['routes'], [])

    def test_only_exact_boolean_option_and_only_verify_tool(self):
        for invalid in ('true', 'false', 1, 0, None, [], {}):
            with self.subTest(value=invalid), patch.object(self.service, '_execute') as execute:
                result = self.call('verify_retrosynthesis_result', final_response=self.payload,
                    validation_receipt_id='a'*32, target='CCO', compact_handoff=invalid)
                self.assertEqual(result['status'], 'invalid_request')
                execute.assert_not_called()
        result = self.call('validate_route_hypotheses', raw_response=self.payload,
                           target='CCO', compact_handoff=True)
        self.assertEqual(result['status'], 'invalid_request')
        context = Context(self.root); register(context)
        schema = context.registered['verify_retrosynthesis_result']['schema']['parameters']
        self.assertEqual(schema['properties']['compact_handoff'], {'type': 'boolean', 'default': False})
        self.assertNotIn('compact_handoff', schema['required'])

    def test_failed_verification_preserves_diagnostics_and_cannot_be_compacted(self):
        failed = self.call('verify_retrosynthesis_result', final_response=self.payload,
            validation_receipt_id='a'*32, target='CCO', compact_handoff=True)
        self.assertEqual(failed['result']['status'], 'quarantined', failed)
        self.assertIn('reason', failed['result'])
        receipt = json.loads((self.root/'receipts'/(failed['receipt_id']+'.json')).read_text())
        forged = {'status': 'completed', 'receipt_id': failed['receipt_id'],
                  'response_sha256': receipt['response_sha256']}
        self.assertEqual(self.accept(forged, task_id=None)['status'], 'quarantined')

    def test_compact_field_type_extra_key_and_hash_tampering_rejected(self):
        envelope = self.verify()
        variants = [{**envelope, 'response_sha256': '0'*64}, {**envelope, 'receipt_id': 'a'*32},
            {**envelope, 'status': 'verified_hypotheses'}, {**envelope, 'status': True},
            {**envelope, 'response_sha256': 123}, {**envelope, 'response_sha256': 'A'*64},
            {**envelope, 'receipt_id': '../secret'}, {**envelope, 'receipt_id': 123},
            {**envelope, 'result': {}}, {**envelope, 'extra': True},
            {key: value for key, value in envelope.items() if key != 'status'}]
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(self.accept(value)['status'], 'quarantined')

    def test_malformed_or_duplicate_key_compact_json_is_not_repaired(self):
        envelope = self.verify()
        raw = json.dumps(envelope)
        duplicate = raw[:-1] + ',"status":"completed"}'
        for text in (raw[:-1], 'answer: '+raw, '```json\n'+raw+'\n```', duplicate):
            result = verify_native_envelope(text, 'CCO', self.root,
                task_id=self.task['task_id'], **self.limits)
            self.assertEqual(result['status'], 'quarantined', text)

    def test_original_scope_task_target_and_bounds_required(self):
        envelope = self.verify()
        other = create_task_context(self.root, 'CCO', 'route_proposer', 1, 2, 3)
        for changes in ({'target': 'CCN'}, {'task_id': other['task_id']}, {'task_id': None},
                        {'requested_count': 2}, {'max_depth': 1}, {'max_total_reactions': 2},
                        {'requested_count': None}, {'state_dir': self.root/'other-owner'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.accept(envelope, **changes)['status'], 'quarantined')

    def test_stored_hashes_action_and_revalidation_are_required(self):
        envelope = self.verify()
        path = self.root/'receipts'/(envelope['receipt_id']+'.json')
        original = json.loads(path.read_text())
        cases = [(('request', 'target'), 'CCN'), (('response', 'status'), 'failed'),
                 (('action',), 'validate_route_hypotheses')]
        for keys, value in cases:
            changed = deepcopy(original)
            if len(keys) == 1: changed[keys[0]] = value
            else: changed[keys[0]][keys[1]] = value
            path.write_text(json.dumps(changed))
            self.assertEqual(self.accept(envelope)['status'], 'quarantined')
        path.write_text(json.dumps(original))
        validation = self.root/'receipts'/(original['request']['validation_receipt_id']+'.json')
        validation.unlink()
        self.assertEqual(self.accept(envelope)['status'], 'quarantined')

    def test_persistence_failure_never_returns_compact_success(self):
        with patch.object(self.service, '_execute', return_value={
                'status': 'completed', 'result': {'status': 'verified_hypotheses'}}), \
             patch('retro_specialists.plugin._write', side_effect=OSError('fixture')):
            value = self.call('verify_retrosynthesis_result', final_response=self.payload,
                validation_receipt_id='a'*32, target='CCO', compact_handoff=True)
        self.assertEqual(value['status'], 'receipt_failed')
        self.assertNotIn('response_sha256', value)
