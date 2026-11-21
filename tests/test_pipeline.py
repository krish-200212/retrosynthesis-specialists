import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from retro_specialists.pipeline import PipelineConfig, run_pipeline


def route(reactants=('CCO',)):
    return {'route_id':'r','name':'hypothesis','reactions':[{'product':'CC=O','reactants':list(reactants)}], 'rationale':'Hypothesis only','feasibility_risks':['Unassessed chemistry'],'evidence_ids':[], 'self_assessment':'proposed', 'stereo_plan':{'strategy':'not_applicable','controls':[],'limitations':[]}}


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlap_and_combined_pool_reaches_optimizers(self):
        event=asyncio.Event(); calls=[]
        async def search():
            await asyncio.wait_for(event.wait(),1)
            return [route(('CC',))]
        async def specialist(role,request,folder):
            calls.append((role,request))
            if role=='route_proposer':
                self.assertNotIn('baseline_routes',request); event.set()
                await asyncio.sleep(.02)
                response=[route()]
            else:
                self.assertEqual(len(request['combined_pool_graph_keys']),2)
                response=[]
            return {'status':'completed','raw_response':json.dumps({'routes':response,'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp)
        self.assertGreater(result['timing']['search_proposer_overlap_seconds'],0)
        self.assertEqual(result['baseline_count'],1)
        self.assertEqual(len(result['combined_routes']),2)
        self.assertEqual({role for role,_ in calls},{'route_proposer'})
        self.assertTrue(result['original_model_routes_preserved'])

    async def test_failure_timeout_empty_and_invalid_preserve_baseline(self):
        for mode in ('failure','timeout','empty','invalid'):
            async def search():return [route()]
            async def specialist(role,request,folder):
                if mode=='failure':raise RuntimeError('provider failed')
                if mode=='timeout':await asyncio.sleep(10)
                return {'status':'completed','raw_response':json.dumps({'routes':[] if mode=='empty' else [route(('CC=O',))],'limitations':[]})}
            with tempfile.TemporaryDirectory() as tmp:
                result=await run_pipeline('CC=O','target',search,specialist,tmp, config=PipelineConfig(proposal_timeout_seconds=.03,optimization_timeout_seconds=.03))
            self.assertEqual(result['baseline_count'],1)
            self.assertEqual(result['additional_unique_graphs'],0)
            self.assertTrue(result['original_model_routes_preserved'])

    async def test_failed_search_still_returns_independent_proposal(self):
        async def search():raise RuntimeError('remote unavailable')
        async def specialist(role,request,folder):
            return {'status':'completed','raw_response':json.dumps({'routes':[route()] if role=='route_proposer' else [],'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp)
        self.assertEqual(result['model_branch']['status'],'failed')
        self.assertEqual(len(result['combined_routes']),1)

    async def test_unknown_citation_rejected(self):
        async def search():return [route()]
        async def specialist(role,request,folder):
            return {'status':'completed','raw_response':json.dumps({'routes':[{**route(('CC',)), 'evidence_ids':['invented']}], 'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp)
        self.assertEqual(result['additional_unique_graphs'],0)
        self.assertEqual(result['proposer']['counts']['retained'],0)

    async def test_retired_optimizers_are_not_reenabled_by_legacy_focus_results(self):
        ready=asyncio.Event(); started=set(); both=asyncio.Event()
        async def search():return [route(('CC',))]
        async def specialist(role,request,folder):
            if role=='route_proposer':
                await asyncio.sleep(.02); ready.set()
                return {'status':'completed','raw_response':json.dumps({'routes':[route()], 'limitations':[]})}
            self.assertTrue(ready.is_set())
            started.add(role)
            if len(started)==2:both.set()
            await asyncio.wait_for(both.wait(),1)
            return {'status':'completed','raw_response':json.dumps({'routes':[], 'limitations':[]})}
        from unittest.mock import patch
        def focus(assessments,limit):
            parent=assessments[0]
            task={'parent_graph_key':parent['graph_key'],'route':parent['route']}
            return {'route_repair':[task],'cost_reducer':[task]}
        with tempfile.TemporaryDirectory() as tmp, patch('retro_specialists.optimization.shortlist_tasks',side_effect=focus):
            result=await run_pipeline('CC=O','target',search,specialist,tmp)
        self.assertEqual(started,set())
        self.assertTrue(all(x['status']=='retired' for x in result['optimization_branches'].values()))

    async def test_search_timeout_and_cancelled_run_are_bounded(self):
        stopped=asyncio.Event()
        async def search():
            try:await asyncio.sleep(10)
            finally:stopped.set()
        async def specialist(role,request,folder):
            return {'status':'completed','raw_response':json.dumps({'routes':[route()], 'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp,config=PipelineConfig(search_timeout_seconds=.02))
        self.assertTrue(stopped.is_set())
        self.assertEqual(result['model_branch']['status'],'timed_out')
        self.assertEqual(len(result['combined_routes']),1)

    async def test_search_adapter_failure_is_not_successful_empty(self):
        async def search():return {'status':'failed','routes':[], 'remote_state':'observation_unknown','reason':'transport_error'}
        async def specialist(role,request,folder):return {'status':'completed','raw_response':json.dumps({'routes':[], 'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp)
        self.assertEqual(result['model_branch']['status'],'failed')
        self.assertEqual(result['model_branch']['original_response']['remote_state'],'observation_unknown')
        self.assertIsNone(result['timing']['first_model_result_seconds'])

    async def test_intermediate_search_requires_known_stock_and_grafts(self):
        async def search():return [route()]
        async def specialist(role,request,folder):return {'status':'completed','raw_response':json.dumps({'routes':[], 'limitations':[]})}
        calls=[]
        async def expand(leaf):
            calls.append(leaf)
            return [{'route_id':'expand','target':'CCO','reactions':[{'product':'CCO','reactants':['C=C','O']}]}]
        for inventory in (None,(),('C=C','O')):
            with tempfile.TemporaryDirectory() as tmp:
                result=await run_pipeline('CC=O','target',search,specialist,tmp,inventory=inventory,
                    intermediate_search=expand,config=PipelineConfig(max_intermediate_expansions=1))
            if inventory:
                self.assertEqual(result['intermediate_replanning']['attempts'][0]['status'],'completed')
                self.assertEqual(len(result['revised_routes']),1)
                self.assertEqual(result['revised_routes'][0]['depth'],2)
            else:self.assertFalse(result['intermediate_replanning']['attempts'])
        self.assertEqual(calls,['CCO'])

    async def test_failed_intermediate_search_preserves_model(self):
        async def search():return [route()]
        async def specialist(role,request,folder):return {'status':'completed','raw_response':json.dumps({'routes':[], 'limitations':[]})}
        async def expand(leaf):return {'status':'failed','routes':[]}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp,inventory=['C=C'],intermediate_search=expand,
                config=PipelineConfig(max_intermediate_expansions=1))
        self.assertTrue(result['original_model_routes_preserved'])
        self.assertEqual(result['intermediate_replanning']['attempts'][0]['status'],'failed')

    async def test_backend_evidence_loader_admits_retrieved_ids(self):
        from retro_specialists.literature import _make_record
        from datetime import datetime,timezone
        record=_make_record(id='backend-source',title='Retrieved context',url='https://example.org/source',doi=None,
            excerpt='Context only',retrieved_at=datetime.now(timezone.utc).isoformat(),source_kind='publication_metadata',
            retrieval_scope='retrieved_context',matching_reactions=[],reviewer='',locator='')
        async def search():return []
        async def specialist(role,request,folder):
            return {'status':'completed','raw_response':json.dumps({'routes':[{**route(),'evidence_ids':['backend-source']}], 'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp,evidence_loader=lambda:[record])
        self.assertEqual(result['proposer']['counts']['retained'],1)

    async def test_no_revision_parent_associations_are_created_for_retired_roles(self):
        from unittest.mock import patch
        async def search():return [route(('CC',)),{**route(('C=C',)),'route_id':'second'}]
        async def specialist(role,request,folder):
            expected=request['starting_material_library']
            self.assertEqual(expected['status'],'configured_membership_set')
            self.assertEqual(expected['molecules'],['CCO'])
            return {'status':'completed','raw_response':json.dumps({'routes':[] if role=='route_proposer' else [route()], 'limitations':[]})}
        def focus(assessments,limit):
            return {role:[{'parent_graph_key':assessments[index]['graph_key'],'route':assessments[index]['route']}]
                    for index,role in enumerate(('route_repair','cost_reducer'))}
        with tempfile.TemporaryDirectory() as tmp, patch('retro_specialists.optimization.shortlist_tasks',side_effect=focus):
            result=await run_pipeline('CC=O','target',search,specialist,tmp,inventory=['CCO'])
        self.assertEqual(len(result['ambiguous_revision_graph_keys']),0)
        self.assertEqual(len(result['revision_parent_associations']),0)
        self.assertEqual(result['final']['accepted_improvements'],[])

    async def test_curated_authority_survives_generation_safe_context_same_id(self):
        from retro_specialists.literature import curated_evidence,_make_record
        curated=curated_evidence(identifier='same-source',title='Primary source',url='https://example.org/primary',doi='10.1000/test',
            excerpt='Reviewed exact reaction',reactions=[{'product':'CC=O','reactants':['CCO']}], reviewer='Backend reviewer',locator='Methods')
        safe=_make_record(id=curated['id'],title='Source context',url=curated['url'],doi=curated['doi'],
            excerpt='An abstract excerpt differing from the reviewed methods.',retrieved_at=curated['retrieved_at'],source_kind='publication_metadata',retrieval_scope='retrieved_context',matching_reactions=[],reviewer='',locator='')
        async def search():return []
        async def specialist(role,request,folder):
            self.assertNotIn('matching_reactions',request['literature_context'][0])
            return {'status':'completed','raw_response':json.dumps({'routes':[{**route(),'evidence_ids':['same-source']}], 'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp,evidence=[curated],evidence_loader=lambda:[safe])
        self.assertEqual(result['proposer']['status'],'completed')
        self.assertEqual(result['proposer']['counts']['retained'],1)
        wrong={**safe,'url':'https://different.example.org/source'}
        from retro_specialists.literature import _hash
        wrong['sha256']=_hash(wrong)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'source identity'):
                await run_pipeline('CC=O','target',search,specialist,tmp,evidence=[curated],evidence_loader=lambda:[wrong])

    async def test_public_model_contract_saved_once_without_repeated_embedding(self):
        public={'top0':{'path':{'smiles':'CC=O','flag':'None','children':[]},'idx':0}}
        async def search():return {'status':'completed','routes':[route()], 'public_output':public,'artifact_sha256':'a'*64}
        async def specialist(role,request,folder):return {'status':'completed','raw_response':json.dumps({'routes':[], 'limitations':[]})}
        with tempfile.TemporaryDirectory() as tmp:
            result=await run_pipeline('CC=O','target',search,specialist,tmp)
            self.assertEqual(json.loads((Path(tmp)/'model-public-output.json').read_text()),public)
            model=result['model_branch']['original_response']
            self.assertNotIn('public_output',model)
            self.assertNotIn('routes',model)
            self.assertEqual(model['artifact_sha256'],'a'*64)
            self.assertEqual(model['public_output_artifact']['path'],'model-public-output.json')
            self.assertNotIn('top0',json.dumps(result))

    def test_invalid_config(self):
        for value in (0,-1,True,float('nan')):
            with self.assertRaises(ValueError):PipelineConfig(proposal_timeout_seconds=value)
