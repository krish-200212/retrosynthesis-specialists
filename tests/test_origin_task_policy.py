"""New origin declarations cannot silently rewrite historical task policy."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import sys
from retro_specialists.task_context import (create_task_context, read_task_context,
    validate_task_candidates, _digest)
from tests.test_stereochemical_plans import TARGET, controlled


class OriginTaskPolicyTests(unittest.TestCase):
    def test_unbound_active_validation_also_requires_origin_declarations(self):
        from retro_specialists.backend_worker import _legacy_validate
        route=controlled()
        route['stereo_plan']['controls'][0]['action']='asymmetric_creation'
        raw=json.dumps({'routes':[route],'limitations':[]})
        checked=_legacy_validate({'target':TARGET,'raw_response':raw}, {'inventory_file':''})
        self.assertEqual(checked['counts']['retained'],0)

    def test_new_context_requires_origin_but_archived_v2_remains_replayable(self):
        route=controlled()
        route['stereo_plan']['controls'][0]['action']='asymmetric_creation'
        raw=json.dumps({'routes':[route],'limitations':[]})
        with tempfile.TemporaryDirectory() as state:
            current=create_task_context(state,TARGET,'route_proposer',1,3,5)
            self.assertEqual(current['schema_version'],3)
            self.assertTrue(current['proposal_requirements']['origin_declarations_required'])
            self.assertEqual(validate_task_candidates(current,raw)['counts']['retained'],0)
            corrected=deepcopy(route)
            control=corrected['stereo_plan']['controls'][0]
            control['origin_element']=control['target_element']
            control['origin_configuration']=control['configuration']
            fixed=json.dumps({'routes':[corrected],'limitations':[]})
            self.assertEqual(validate_task_candidates(current,fixed)['counts']['retained'],1)
            historical=deepcopy(current)
            historical['schema_version']=2
            historical['proposal_requirements'].pop('origin_declarations_required')
            historical['content_sha256']=_digest({k:v for k,v in historical.items() if k!='content_sha256'})
            path=Path(state)/'tasks'/(current['task_id']+'.json')
            path.write_text(json.dumps(historical))
            self.assertEqual(read_task_context(state,current['task_id']),historical)
            self.assertEqual(validate_task_candidates(historical,raw)['counts']['retained'],1)
            # Archived policy remains usable throughout the stored receipt chain.
            from retro_specialists.plugin import PrimitiveTools
            from retro_specialists.finalization import verify_native_result
            from tests.test_plugin import Context
            service=PrimitiveTools(Context(state,enabled=True,backend_python=sys.executable,
                backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
            validation=json.loads(service.handle('validate_route_hypotheses',
                {'target':TARGET,'task_id':historical['task_id'],'raw_response':raw}))
            self.assertEqual(validation['status'],'completed',validation)
            replay=verify_native_result(raw,validation['receipt_id'],TARGET,state,
                                       task_id=historical['task_id'])
            self.assertEqual(replay['status'],'verified_hypotheses',replay)
            self.assertEqual(len(replay['routes']),1)

    def test_current_origin_policy_cannot_be_downgraded_without_changing_version(self):
        with tempfile.TemporaryDirectory() as state:
            current=create_task_context(state,TARGET,'route_proposer',1,3,5)
            self.assertEqual(current['schema_version'],3)
            current['proposal_requirements']['origin_declarations_required']=False
            current['content_sha256']=_digest({k:v for k,v in current.items() if k!='content_sha256'})
            (Path(state)/'tasks'/(current['task_id']+'.json')).write_text(json.dumps(current))
            with self.assertRaises(ValueError):read_task_context(state,current['task_id'])
