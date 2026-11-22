"""Object transport removes nested JSON syntax without changing route acceptance."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from retro_specialists.contracts import response_schema
from retro_specialists.plugin import PrimitiveTools, register
from retro_specialists.task_context import create_task_context
from tests.test_native_finalization import proposal
from tests.test_plugin import Context


class StructuredToolInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = PrimitiveTools(Context(self.root, enabled=True, backend_python=sys.executable,
            backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
        self.payload = {'routes': [proposal()], 'limitations': ['Unassessed fixture chemistry']}

    def call(self, action, **args):
        return json.loads(self.service.handle(action, args))

    def test_exact_strict_object_schema_and_legacy_text_are_both_advertised(self):
        ctx = Context(self.root)
        register(ctx)
        for name, field in [('validate_route_hypotheses', 'raw_response'),
                            ('verify_retrosynthesis_result', 'final_response')]:
            contract = ctx.registered[name]['schema']['parameters']['properties'][field]
            self.assertEqual(contract['oneOf'][0], response_schema(20, 100,
                require_stereo_plan=True, require_origin_declarations=True))
            self.assertEqual(contract['oneOf'][1], {'type': 'string', 'maxLength': 1000000})
        # Every call owns its schema; no shared request mutation.
        ctx.registered['validate_route_hypotheses']['schema']['parameters']['properties']['raw_response']['oneOf'][0]['properties']['routes']['maxItems'] = 1
        self.assertEqual(ctx.registered['verify_retrosynthesis_result']['schema']['parameters']['properties']['final_response']['oneOf'][0]['properties']['routes']['maxItems'], 20)

    def test_object_roundtrip_real_backend_receipts_and_host_acceptance(self):
        original = deepcopy(self.payload)
        task = create_task_context(self.root, 'CCO', 'route_proposer', 1, 2, 3)
        limits = task['constraints']
        validation = self.call('validate_route_hypotheses', raw_response=self.payload,
                               target='CCO', task_id=task['task_id'], **limits)
        self.assertEqual(validation['status'], 'completed', validation)
        self.assertEqual(validation['result']['counts']['retained'], 1)
        envelope = self.call('verify_retrosynthesis_result', final_response=self.payload,
            validation_receipt_id=validation['receipt_id'], target='CCO', task_id=task['task_id'])
        self.assertEqual(envelope['result']['status'], 'verified_hypotheses', envelope)
        accepted = self.call('accept_retrosynthesis_result', final_envelope=json.dumps(envelope),
                            target='CCO', task_id=task['task_id'], **limits)
        self.assertEqual(accepted['result']['status'], 'verified_hypotheses', accepted)
        self.assertFalse(accepted['result']['chemical_feasibility_established'])
        self.assertEqual(self.payload, original)
        for result, field in [(validation, 'raw_response'), (envelope, 'final_response')]:
            receipt = json.loads((self.root/'receipts'/(result['receipt_id']+'.json')).read_text())
            text = receipt['request'][field]
            self.assertIsInstance(text, str)
            self.assertEqual(json.loads(text), original)
            expected = json.dumps(receipt['request'], ensure_ascii=True, allow_nan=False, separators=(',', ':'))
            self.assertEqual(receipt['request_sha256'], sha256(expected.encode()).hexdigest())

    def test_empty_object_is_valid_completion(self):
        result = self.call('validate_route_hypotheses', raw_response={'routes': [], 'limitations': []}, target='CCO')
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['result']['counts']['retained'], 0)

    def test_legacy_strings_are_not_reserialized_or_repaired(self):
        for raw in [' { "routes" : [], "limitations" : [] } ', '{"routes": [']:
            with patch.object(self.service, '_execute', return_value={'status': 'completed'}) as execute:
                result = self.call('validate_route_hypotheses', raw_response=raw, target='CCO')
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(execute.call_args.args[1]['raw_response'], raw)
            receipt = json.loads((self.root/'receipts'/(result['receipt_id']+'.json')).read_text())
            self.assertEqual(receipt['request']['raw_response'], raw)

    def test_unrepresentable_and_excessive_objects_never_start_backend(self):
        deep = {}
        for _ in range(40):
            deep = {'nested': deep}
        cycle = {}; cycle['cycle'] = cycle
        for value in [[], None, True, 42, {'routes': [], 'limitations': ['x'*1000001]},
                      {'routes': [], 'limitations': ['é'*600000]}, deep, cycle,
                      {'routes': [], 'limitations': [float('nan')]}, {'bad': object()},
                      {'routes': [None]*100001}, {1: 'not a JSON key'}]:
            with self.subTest(kind=type(value).__name__), patch.object(self.service, '_execute') as execute:
                result = self.call('validate_route_hypotheses', raw_response=value, target='CCO')
                self.assertEqual(result['status'], 'invalid_request', result)
                execute.assert_not_called()

    def test_numeric_heavy_object_stops_encoding_at_byte_budget(self):
        from retro_specialists.proposal_transport import normalize_proposal_arguments
        original = json.JSONEncoder.iterencode
        chunks = []
        def counted(encoder, value, *args, **kwargs):
            for chunk in original(encoder, value, *args, **kwargs):
                chunks.append(len(chunk))
                yield chunk
        values = [10**3000] * 2000
        with patch.object(json.JSONEncoder, 'iterencode', counted):
            with self.assertRaises(ValueError):
                normalize_proposal_arguments('validate_route_hypotheses', {'raw_response': {'bad': values}})
        self.assertLess(sum(chunks), 1_010_000)

    def test_shape_violations_remain_rejected_by_backend(self):
        missing_stereo = deepcopy(self.payload); missing_stereo['routes'][0].pop('stereo_plan')
        for value in [{**self.payload, 'extra': True}, {'routes': 'wrong', 'limitations': []},
                      missing_stereo, {'routes': [proposal()]*21, 'limitations': []}]:
            with self.subTest(value=str(value)[:80]):
                result = self.call('validate_route_hypotheses', raw_response=value, target='CCO')
                self.assertEqual(result['result']['counts']['retained'], 0, result)

    def test_bound_limits_cycles_target_and_claim_changes_still_fail(self):
        task = create_task_context(self.root, 'CCO', 'route_proposer', 1, 1, 1)
        args = {'target': 'CCO', 'task_id': task['task_id'], **task['constraints']}
        invalid_routes = [
            {**proposal(), 'reactions': [{'product': 'CCO', 'reactants': ['CCO']}]},
            {**proposal(), 'reactions': [{'product': 'CCN', 'reactants': ['CC=O']}]},
            {**proposal(), 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']},
                                       {'product': 'CC=O', 'reactants': ['CC']}]}]
        for route in invalid_routes:
            result = self.call('validate_route_hypotheses', raw_response={'routes': [route], 'limitations': []}, **args)
            self.assertEqual(result['result']['counts']['retained'], 0, result)
        result = self.call('validate_route_hypotheses', raw_response=self.payload, **args)
        changed = deepcopy(self.payload); changed['routes'][0]['rationale'] = 'Changed claim'
        final = self.call('verify_retrosynthesis_result', final_response=changed,
            validation_receipt_id=result['receipt_id'], target='CCO', task_id=task['task_id'])
        self.assertEqual(final['result']['status'], 'quarantined', final)
        mismatch = self.call('validate_route_hypotheses', raw_response=self.payload,
                            **{**args, 'requested_count': 2})
        self.assertEqual(mismatch['status'], 'backend_error', mismatch)
