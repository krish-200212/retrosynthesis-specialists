"""Immutable host task contexts bind the child to original molecules and limits."""
import json
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from retro_specialists.task_context import create_task_context, read_task_context, validate_task_candidates
from tests.test_native_finalization import proposal
from tests.legacy_task_fixtures import create_legacy_task_fixture

class TaskContextTests(unittest.TestCase):
    def create(self, directory, **changes):
        arguments = dict(state_dir=directory, target='OCC', role='route_repair',
            requested_count=2, max_depth=2, max_total_reactions=3,
            baseline_routes=[proposal()], source_sha256='a' * 64, baseline_total_count=29)
        return create_legacy_task_fixture(**{**arguments, **changes})

    def test_roundtrip_canonical_graph_limits_provenance_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = self.create(tmp)
            self.assertEqual(read_task_context(tmp, value['task_id']), value)
            self.assertEqual(value['target'], 'CCO')
            self.assertEqual(value['baseline_routes'][0]['reactions'][0]['product'], 'CCO')
            self.assertEqual(value['constraints'], {'requested_count': 2, 'max_depth': 2, 'max_total_reactions': 3})
            self.assertEqual(value['baseline_provenance']['selected_route_count'], 1)
            self.assertEqual(value['baseline_provenance']['total_route_count'], 29)
            self.assertEqual(value['inventory']['status'], 'unknown')
            self.assertEqual((Path(tmp)/'tasks').stat().st_mode & 0o777, 0o700)
            self.assertEqual((Path(tmp)/'tasks'/(value['task_id']+'.json')).stat().st_mode & 0o777, 0o600)

    def test_proposer_independence_and_role_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = self.create(tmp, role='route-proposer', baseline_routes=None, source_sha256=None, baseline_total_count=None)
            self.assertEqual(value['role'], 'route_proposer')
            self.assertNotIn('baseline_routes', value)
            self.assertNotIn('baseline_provenance', value)
            for changes in [{'baseline_routes': []}, {'source_sha256': 'a'*64}, {'baseline_total_count': 0}]:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.create(tmp, role='route_proposer', **changes)

    def test_inputs_copied_and_inventory_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            routes, inventory = [proposal()], ['O=CC', 'CC=O']
            value = self.create(tmp, baseline_routes=routes, inventory=inventory)
            routes[0]['reactions'][0]['product'] = 'CCN'; inventory.append('C')
            self.assertEqual(value['inventory']['molecules'], ['CC=O'])
            self.assertEqual(read_task_context(tmp, value['task_id']), value)
            value['constraints']['max_depth'] = 29
            self.assertEqual(read_task_context(tmp, value['task_id'])['constraints']['max_depth'], 2)

    def test_bad_inputs_rejected_before_write(self):
        for changes in [{'target': '*'}, {'role': 'critic'}, {'requested_count': True},
                        {'max_depth': 0}, {'max_total_reactions': 201}, {'source_sha256': '../bad'},
                        {'baseline_routes': None}, {'baseline_total_count': 0}, {'inventory': 'CCO'},
                        {'inventory': ['*']}, {'baseline_routes': [proposal(), proposal()]}]:
            with tempfile.TemporaryDirectory() as tmp, self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(tmp, **changes)

    def test_exact_target_and_acyclic_baseline_required(self):
        bad = {**proposal(), 'reactions': [{'product': 'CCO', 'reactants': ['CCO']}]}
        with tempfile.TemporaryDirectory() as tmp:
            for changes in [{'target':'CCN'}, {'baseline_routes': [bad]}]:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.create(tmp, **changes)

    def test_baseline_can_exceed_requested_candidate_depth(self):
        route = {**proposal(), 'reactions': [{'product':'CCO','reactants':['CC=O']}, {'product':'CC=O','reactants':['CC']}]}
        with tempfile.TemporaryDirectory() as tmp:
            value = self.create(tmp, baseline_routes=[route], max_depth=1)
            self.assertEqual(value['baseline_routes'][0]['depth'], 2)
            checked = validate_task_candidates(value, json.dumps({'routes':[route], 'limitations':[]}))
            self.assertEqual(checked['counts']['retained'], 0)
            self.assertEqual(checked['rejections'][0]['stage'], 'structure')

    def test_stock_checked_only_against_bound_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = self.create(tmp, inventory=['CC=O'])
            checked = validate_task_candidates(value, json.dumps({'routes':[proposal()], 'limitations':[]}))
            self.assertEqual(checked['counts']['stock_connected'], 1)
            self.assertFalse(checked.get('chemical_feasibility_established', False))

    def test_file_tamper_and_unexpected_fields_fail_closed(self):
        for tamper in ['target', 'extra', 'digest']:
            with tempfile.TemporaryDirectory() as tmp:
                value = self.create(tmp); path=Path(tmp)/'tasks'/(value['task_id']+'.json')
                changed = {**value, {'target':'target','extra':'unexpected','digest':'content_sha256'}[tamper]: 'changed'}
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): read_task_context(tmp,value['task_id'])

    def test_strict_identifier_and_task_file_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=self.create(tmp)
            for task_id in ['../x', 'A'*32, 'a'*31, '', 42]:
                with self.subTest(task_id=task_id), self.assertRaises(ValueError): read_task_context(tmp,task_id)
            path=Path(tmp)/'tasks'/(value['task_id']+'.json'); moved=Path(tmp)/'copy.json'; path.rename(moved); path.symlink_to(moved)
            with self.assertRaises((ValueError,OSError)): read_task_context(tmp,value['task_id'])

    def test_directory_symlinks_and_insecure_task_directory_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); actual=root/'actual'; actual.mkdir(); alias=root/'alias'; alias.symlink_to(actual,target_is_directory=True)
            with self.assertRaises((ValueError,OSError)):
                create_task_context(alias, 'CCO', 'route_proposer', 1, 1, 1)
            tasks=actual/'tasks'; tasks.mkdir(mode=0o755)
            with self.assertRaises((ValueError,OSError)):
                create_task_context(actual, 'CCO', 'route_proposer', 1, 1, 1)

    def test_exclusive_collision_preserves_existing_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('tests.legacy_task_fixtures.uuid4') as identifier:
                identifier.return_value.hex='b'*32
                value=self.create(tmp)
                with self.assertRaises(FileExistsError): self.create(tmp)
            self.assertEqual(read_task_context(tmp,value['task_id']), value)

    def test_oversized_and_nonregular_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=self.create(tmp);path=Path(tmp)/'tasks'/(value['task_id']+'.json')
            path.write_bytes(b' '*2_000_001)
            with self.assertRaises(ValueError): read_task_context(tmp,value['task_id'])
            path.unlink(); os.mkfifo(path, 0o600)
            with self.assertRaises(ValueError): read_task_context(tmp,value['task_id'])

    def test_hardlinks_and_public_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=self.create(tmp);path=Path(tmp)/'tasks'/(value['task_id']+'.json')
            path.chmod(0o644)
            with self.assertRaises(ValueError): read_task_context(tmp,value['task_id'])
            path.chmod(0o600);os.link(path,Path(tmp)/'copy.json')
            with self.assertRaises(ValueError): read_task_context(tmp,value['task_id'])

    def test_rehashed_noncanonical_or_extra_metadata_is_rejected(self):
        for modify in [lambda v: v.update(schema_version=True),
                       lambda v: v['baseline_routes'][0].update(depth=True),
                       lambda v: v['baseline_provenance'].update(extra='unknown'),
                       lambda v: v['inventory'].update(status='available'),
                       lambda v: v['constraints'].update(extra=1)]:
            with tempfile.TemporaryDirectory() as tmp:
                value=self.create(tmp);path=Path(tmp)/'tasks'/(value['task_id']+'.json')
                modify(value)
                unsigned={key:item for key,item in value.items() if key != 'content_sha256'}
                value['content_sha256']=sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError): read_task_context(tmp,value['task_id'])

    def test_valid_record_cannot_be_renamed_to_another_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=self.create(tmp);path=Path(tmp)/'tasks'/(value['task_id']+'.json')
            path.rename(path.with_name('c'*32+'.json'))
            with self.assertRaises(ValueError): read_task_context(tmp,'c'*32)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=self.create(tmp);path=Path(tmp)/'tasks'/(value['task_id']+'.json')
            path.write_text('{"task_id":"first",'+path.read_text()[1:])
            with self.assertRaises(ValueError): read_task_context(tmp,value['task_id'])

    def test_unknown_total_and_configured_empty_inventory_remain_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=self.create(tmp,baseline_total_count=None,inventory=[])
            self.assertIsNone(value['baseline_provenance']['total_route_count'])
            self.assertEqual(value['inventory']['status'],'configured_membership_set')
            self.assertEqual(read_task_context(tmp,value['task_id']),value)

if __name__ == '__main__': unittest.main()
