"""Current dispatch exposes one proposer; retired receipts remain readable."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retro_specialists.native_pipeline import NativeConfig, NativeSpecialistAdapter
from retro_specialists.pipeline import run_pipeline
from retro_specialists.task_context import create_task_context, read_task_context


def proposal():
    return {'route_id': 'new', 'name': 'General proposal',
        'reactions': [{'product': 'CC=O', 'reactants': ['CCO']}],
        'rationale': 'A hypothesis.', 'feasibility_risks': ['Unassessed chemistry'],
        'evidence_ids': [], 'self_assessment': 'proposed',
        'stereo_plan': {'strategy': 'not_applicable', 'controls': [], 'limitations': []}}


class ProposerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_calls_only_proposer_even_when_legacy_optimizer_tasks_exist(self):
        calls = []
        async def search():
            return [{**proposal(), 'reactions': [{'product': 'CC=O', 'reactants': ['CC']}]}]
        async def agent(role, request, folder):
            calls.append(role)
            self.assertNotIn('baseline_routes', request)
            return {'status': 'completed', 'raw_response': json.dumps({'routes': [proposal()], 'limitations': []})}
        with tempfile.TemporaryDirectory() as folder:
            result = await run_pipeline('CC=O', 'target', search, agent, folder)
        self.assertEqual(calls, ['route_proposer'])
        self.assertEqual(result['optimization_branches']['route_repair']['status'], 'retired')
        self.assertEqual(result['optimization_branches']['cost_reducer']['status'], 'retired')
        self.assertTrue(result['original_model_routes_preserved'])

    async def test_native_adapter_rejects_retired_roles_before_any_helper_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = NativeConfig(root, root, Path('/usr/bin/python3'), root, root, 'model', 'provider')
            request = {'identity': {'smiles': 'CC=O'}, 'requested_route_count': 1,
                       'project_specification': {'max_depth': 2, 'max_total_reactions': 3}}
            for role in ('route_repair', 'cost_reducer'):
                with patch('retro_specialists.native_pipeline._process') as process:
                    with self.assertRaisesRegex(ValueError, 'retired|active|Unknown'):
                        await NativeSpecialistAdapter(config)(role, request, root / role)
                    process.assert_not_called()

    async def test_new_host_context_has_immutable_stereo_requirements(self):
        with tempfile.TemporaryDirectory() as folder:
            task = create_task_context(folder, 'C[C@H](O)c1ccccc1', 'route_proposer', 1, 3, 5)
            self.assertEqual(task['schema_version'], 3)
            self.assertTrue(task['proposal_requirements']['stereo_plan_required'])
            self.assertTrue(task['proposal_requirements']['origin_declarations_required'])
            self.assertEqual(len(task['proposal_requirements']['target_stereo']['specified_elements']), 1)
            self.assertEqual(read_task_context(folder, task['task_id']), task)
            for role in ('route_repair', 'cost_reducer'):
                with self.assertRaisesRegex(ValueError, 'retired|active'):
                    create_task_context(folder, 'CC=O', role, 1, 2, 3, baseline_routes=[proposal()])

    async def test_provider_timeout_status_is_preserved_without_losing_model_routes(self):
        async def search():
            return [proposal()]
        async def agent(role, request, folder):
            return {'status': 'timed_out', 'routes': []}
        with tempfile.TemporaryDirectory() as folder:
            result = await run_pipeline('CC=O', 'target', search, agent, folder)
        self.assertEqual(result['proposer']['status'], 'timed_out')
        self.assertTrue(result['original_model_routes_preserved'])
        self.assertEqual(result['baseline_count'], 1)

    async def test_direct_specialist_request_receives_backend_numbered_target_stereo(self):
        captured = []
        async def search():
            return []
        async def agent(role, request, folder):
            captured.append(request)
            return {'status': 'completed', 'raw_response': '{"routes":[],"limitations":[]}'}
        target = 'C[C@H](O)c1ccccc1'
        with tempfile.TemporaryDirectory() as folder:
            await run_pipeline(target, 'target', search, agent, folder)
        from retro_specialists.stereochemistry import target_stereo_requirements
        self.assertEqual(captured[0]['stereochemical_requirements'], target_stereo_requirements(target))


if __name__ == '__main__':
    unittest.main()
