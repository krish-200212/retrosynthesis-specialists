"""CLI wiring must never turn replay or unknown stock into new remote jobs."""
import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.run_pipeline import execute


class PipelineCLITests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, inventory, replay=False, recovered=False, old_budgets=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);targets=[];observed={}
            config={'target':'CC=O','name':'target','science_config':'operator.json',
                    'native':{'model':'pinned','provider':'fake'},
                    'pipeline':{'max_intermediate_expansions':1}}
            if old_budgets:
                config['search_timeout_seconds'] = 1200
                config['native']['timeout_seconds'] = 360
                config['pipeline'].update(until_search_finishes=True, proposal_timeout_seconds=370,
                                          optimization_timeout_seconds=180, search_timeout_seconds=1250)
            if inventory is not None:
                path=root/'inventory.json';path.write_text(json.dumps({'source':'fixture','molecules':inventory}))
                config['inventory_file']=str(path)
            if replay:
                path=root/'baseline.json';path.write_text('{}');config['baseline_file']=str(path)
            if recovered:config['recovered_proposal_from']=str(root/'checkpoint')
            path=root/'config.json';path.write_text(json.dumps(config))
            class Search:
                def __init__(self,**kwargs):
                    self.target=kwargs['target']
                    observed.setdefault('search_timeouts', []).append(kwargs.get('timeout_seconds'))
                async def __call__(self):targets.append(self.target);return {'routes':[]}
            class Adapter:
                def __init__(self,config):
                    self.config=config
                    observed['native_timeout'] = getattr(config, 'timeout_seconds', None)
                def load_evidence(self):return []
                async def __call__(self,*args):raise AssertionError('Unexpected fresh generation')
            async def pipeline(target,name,search,dispatch,folder,**kwargs):
                observed['pipeline_timeouts'] = [kwargs['config'].search_timeout_seconds,
                                                kwargs['config'].proposal_timeout_seconds,
                                                kwargs['config'].optimization_timeout_seconds]
                observed['hook_enabled']=kwargs['intermediate_search'] is not None
                if kwargs['intermediate_search'] is not None:await kwargs['intermediate_search']('CCO')
                if recovered:observed['recovery']=await dispatch('route_proposer',{},root/'receipt')
                return {'baseline_count':0,'additional_unique_graphs':0}
            with patch('retro_specialists.science_pipeline.ScienceSearch',Search), \
                 patch('retro_specialists.native_pipeline.NativeConfig',lambda **kwargs:SimpleNamespace(**kwargs)), \
                 patch('retro_specialists.native_pipeline.NativeSpecialistAdapter',Adapter), \
                 patch('retro_specialists.native_pipeline.recover_native_proposal',return_value={'recovered':True}), \
                 patch('retro_specialists.pipeline.run_pipeline',side_effect=pipeline):
                await execute(argparse.Namespace(config=path,output=root/'output'))
            observed['manifest']=json.loads((root/'output/benchmark-manifest.json').read_text())
            return observed,targets

    async def test_manifest_reports_only_active_proposer_without_retired_generation(self):
        observed,_=await self.run_case(None)
        self.assertFalse(observed['manifest']['fresh_optimization_generation'])
        self.assertEqual(observed['manifest']['active_roles'],['route_proposer'])

    async def test_continuous_runner_discards_legacy_wall_clock_budgets(self):
        observed, _ = await self.run_case(['CC'], old_budgets=True)
        self.assertEqual(observed['search_timeouts'], [None, None])
        self.assertEqual(observed['pipeline_timeouts'], [None, None, None])
        self.assertIsNone(observed['native_timeout'])

    async def test_known_inventory_expansion_uses_exact_leaf(self):
        observed,targets=await self.run_case(['CC'])
        self.assertTrue(observed['hook_enabled']);self.assertEqual(targets,['CCO'])

    async def test_replay_and_unknown_inventory_never_submit_expansion(self):
        for stock,replay in [(None,False),([],False),(['CC'],True)]:
            with self.subTest(stock=stock,replay=replay):
                observed,targets=await self.run_case(stock,replay)
                self.assertFalse(observed['hook_enabled']);self.assertEqual(targets,[])

    async def test_checkpoint_recovery_does_not_generate_again(self):
        observed,targets=await self.run_case(None,recovered=True)
        self.assertEqual(observed['recovery'],{'recovered':True});self.assertEqual(targets,[])
