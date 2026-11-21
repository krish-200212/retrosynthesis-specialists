import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from retro_specialists.workflow import RunConfig, run_specialists


def proposal(product='CC=O', reactants=None):
    return {'route_id': 'p1', 'name': 'Hypothesis',
            'reactions': [{'product': product, 'reactants': reactants or ['CCO']}],
            'rationale': 'Proposed transformation, not experimentally established.',
            'feasibility_risks': ['Selectivity unknown'], 'evidence_ids': [],
            'self_assessment': 'proposed',
            'stereo_plan': {'strategy': 'not_applicable', 'controls': [], 'limitations': []}}


class FakeProvider:
    def __init__(self, response=None, fail=()):
        self.response = response or {'routes': [proposal()], 'limitations': []}
        self.fail = fail
        self.requests = []

    async def generate(self, request, instructions, schema, receipt_dir):
        self.requests.append(request)
        await asyncio.sleep(0.01)
        if request['role'] in self.fail:
            raise RuntimeError('provider unavailable')
        return {'status': 'completed', 'raw_response': json.dumps(self.response),
                'model': 'deterministic-fixture', 'provider': 'fake',
                'usage': None, 'cost': None}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_only_proposer_is_called_and_has_no_model_results(self):
        provider = FakeProvider()
        baseline = [proposal(reactants=['CC'])]
        result = await run_specialists('CC=O', 'acetaldehyde', baseline, provider,
            self.folder, config=RunConfig(requested_count=2), inventory=['CCO', 'CC'])
        self.assertEqual({r['role'] for r in provider.requests},
                         {'route_proposer'})
        independent = next(r for r in provider.requests if r['role'] == 'route_proposer')
        self.assertNotIn('baseline_routes', independent)
        self.assertEqual(result['baseline_count'], 1)
        self.assertEqual(result['additional_unique_graphs'], 1)
        self.assertEqual(result['chemically_validated_routes'], 0)
        self.assertNotIn('route_repair',result['branches'])
        self.assertNotIn('cost_reducer',result['branches'])

    async def test_failed_agents_preserve_model_result(self):
        result = await run_specialists('CC=O', 'acetaldehyde', [proposal()],
            FakeProvider(fail=('route_proposer', 'route_repair', 'cost_reducer')),
            self.folder, inventory=['CCO'])
        self.assertEqual(len(result['merged_routes']), 1)
        self.assertEqual(result['additional_unique_graphs'], 0)
        self.assertTrue(all(b['status'] == 'failed' for b in result['branches'].values()))

    async def test_empty_provider_is_valid_and_missing_cost_is_not_zero(self):
        result = await run_specialists('CC=O', 'acetaldehyde', [proposal()],
            FakeProvider({'routes': [], 'limitations': ['No defensible alternatives']}),
            self.folder, inventory=['CCO'])
        self.assertEqual(result['branches']['route_proposer']['status'], 'completed')
        self.assertEqual(result['additional_unique_graphs'], 0)
        self.assertFalse(result['cost_improvement_demonstrated'])

    async def test_actual_overlap_and_baseline_independence(self):
        started = asyncio.Event()
        class OverlapProvider(FakeProvider):
            async def generate(self, request, instructions, schema, receipt_dir):
                if request['role'] == 'route_proposer':
                    started.set()
                return await super().generate(request, instructions, schema, receipt_dir)
        async def baseline():
            await asyncio.wait_for(started.wait(), 1)
            return [proposal()]
        result = await run_specialists('CC=O', 'acetaldehyde', baseline,
            OverlapProvider(), self.folder, inventory=['CCO'])
        self.assertGreater(result['overlap_seconds'], 0)

    async def test_deadline_cancels_provider_without_losing_baseline(self):
        stopped = asyncio.Event()
        class Hanging(FakeProvider):
            async def generate(self, *args, **kwargs):
                try:
                    await asyncio.sleep(60)
                finally:
                    stopped.set()
        result = await run_specialists('CC=O', 'acetaldehyde', [proposal()],
            Hanging(), self.folder, config=RunConfig(deadline_seconds=0.08),
            inventory=['CCO'])
        self.assertTrue(stopped.is_set())
        self.assertEqual(len(result['merged_routes']), 1)
        self.assertEqual(result['additional_unique_graphs'], 0)

    async def test_same_routes_not_counted_as_improvement_and_unknown_citation_rejected(self):
        bad = {**proposal(), 'evidence_ids': ['made-up-source']}
        result = await run_specialists('CC=O', 'acetaldehyde', [proposal()],
            FakeProvider({'routes': [bad], 'limitations': []}), self.folder,
            inventory=['CCO'])
        self.assertEqual(result['additional_unique_graphs'], 0)
        self.assertFalse(result['improvement_demonstrated'])
        self.assertTrue(result['branches']['route_proposer']['evidence_rejections'])
        self.assertEqual(result['branches']['route_proposer']['counts']['retained'], 0)

    async def test_invalid_baseline_row_does_not_discard_valid_model_routes(self):
        result = await run_specialists('CC=O', 'acetaldehyde',
            [proposal(), proposal(reactants=['CC=O'])], FakeProvider(), self.folder,
            inventory=['CCO'])
        self.assertEqual(result['baseline_count'], 1)
        self.assertEqual(len(result['baseline_rejections']), 1)

    async def test_inventory_iterator_is_frozen_for_all_roles(self):
        result = await run_specialists('CC=O', 'acetaldehyde', [proposal()],
            FakeProvider(), self.folder, inventory=iter(['CCO']))
        self.assertTrue(all(b['routes'][0]['stock_connected'] for b in result['branches'].values()))

    async def test_slow_baseline_does_not_spawn_any_dependent_roles(self):
        async def baseline():
            await asyncio.sleep(0.06)
            return [proposal()]
        result = await run_specialists('CC=O', 'acetaldehyde', baseline,
            FakeProvider(), self.folder, config=RunConfig(deadline_seconds=0.04), inventory=['CCO'])
        self.assertEqual(set(result['branches']), {'route_proposer'})


if __name__ == '__main__':
    unittest.main()
