"""Model completion governs the window; only already-verified rounds contribute."""
import asyncio
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from retro_specialists.pipeline import PipelineConfig, run_pipeline
from tests.test_pipeline import route


def proposed(*reactants):
    return {"status": "completed", "raw_response": json.dumps({
        "routes": [route((precursor,)) for precursor in reactants], "limitations": []})}


class ProposalWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    async def submit(self, model, proposer, **limits):
        config = PipelineConfig(until_search_finishes=True, requested_count=1, **limits)
        return await run_pipeline("CC=O", "fixture", model, proposer, self.root, config=config)

    async def test_search_and_proposer_survive_two_hours_of_simulated_elapsed_time(self):
        loop = asyncio.get_running_loop()
        real_clock = loop.time
        offset = 0
        searching = asyncio.Event()
        release_search = asyncio.Event()

        async def model():
            searching.set()
            await release_search.wait()
            return [route(("C",))]

        async def proposer(role, request, folder):
            nonlocal offset
            await searching.wait()
            if request["round_number"] == 1:
                offset = 7201
                # Give any scheduled timers a chance to expire while both branches run.
                for _ in range(3):
                    await asyncio.sleep(0)
                return proposed("CCO")
            release_search.set()
            await asyncio.Future()

        with patch.object(loop, "time", side_effect=lambda: real_clock() + offset), patch(
            "retro_specialists.pipeline._round_delay", return_value=0
        ):
            result = await self.submit(model, proposer)
        self.assertEqual(result["baseline_count"], 1)
        self.assertEqual(result["proposer"]["counts"]["retained"], 1)
        self.assertEqual(result["proposer"]["rounds"][-1]["status"], "stopped_at_search_end")

    async def test_keeps_two_distinct_verified_graphs_and_cancels_pending_round(self):
        fourth = asyncio.Event()
        stopped = asyncio.Event()
        requests = []
        async def model():
            await fourth.wait()
            return [route(("C",))]
        async def proposer(role, request, folder):
            requests.append(request)
            if len(requests) == 4:
                fourth.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            return proposed(["CCO", "CC", "OCC"][len(requests)-1])
        with patch("retro_specialists.pipeline._round_delay", return_value=0):
            result = await asyncio.wait_for(self.submit(model, proposer), 4)
        self.assertTrue(stopped.is_set())
        self.assertEqual(result["additional_unique_graphs"], 2)
        branch = result["proposer"]
        self.assertEqual(branch["counts"]["duplicate_verified"], 1)
        self.assertEqual(branch["counts"]["retained"], 2)
        self.assertEqual(branch["rounds"][-1]["status"], "stopped_at_search_end")
        self.assertEqual(len(requests[1]["previous_verified_reactions"]), 1)
        self.assertNotIn("baseline_routes", requests[-1])
        self.assertTrue((self.root/"route_proposer/round-0002/result.json").is_file())
        self.assertFalse((self.root/"route_proposer/round-0004/result.json").exists())

    async def test_empty_failed_and_timed_out_rounds_do_not_stop_exploration(self):
        final_round = asyncio.Event()
        outcomes = [proposed(), {"status": "failed"}, {"status": "timed_out"}, proposed("CCO")]
        async def model():
            await final_round.wait()
            return []
        async def proposer(*args):
            if outcomes:
                return outcomes.pop(0)
            final_round.set()
            await asyncio.Future()
        with patch("retro_specialists.pipeline._round_delay", return_value=0):
            result = await asyncio.wait_for(self.submit(model, proposer), 4)
        self.assertEqual([r["status"] for r in result["proposer"]["rounds"]],
            ["completed", "failed", "timed_out", "completed", "stopped_at_search_end"])
        self.assertEqual(result["proposer"]["counts"]["retained"], 1)

    async def test_search_failure_and_timeout_cancel_the_active_round(self):
        for expected in ("failed", "timed_out"):
            with self.subTest(expected=expected):
                active, stopped = asyncio.Event(), asyncio.Event()
                async def model():
                    await active.wait()
                    if expected == "failed":
                        raise RuntimeError("search unavailable")
                    await asyncio.Future()
                async def proposer(*args):
                    active.set()
                    try:
                        await asyncio.Future()
                    finally:
                        stopped.set()
                result = await asyncio.wait_for(self.submit(model, proposer, search_timeout_seconds=.2), 2)
                self.assertTrue(stopped.is_set())
                self.assertEqual(result["model_branch"]["status"], expected)
                self.assertEqual(result["proposer"]["routes"], [])

    async def test_outer_cancellation_keeps_completed_checkpoint_and_reaps_both_branches(self):
        second, search_stopped, child_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def model():
            try:
                await asyncio.Future()
            finally:
                search_stopped.set()
        async def proposer(role, request, folder):
            if request["round_number"] == 1:
                return proposed("CCO")
            second.set()
            try:
                await asyncio.Future()
            finally:
                child_stopped.set()
        job = asyncio.create_task(self.submit(model, proposer))
        try:
            await asyncio.wait_for(second.wait(), 2)
            record = json.loads((self.root/"route_proposer/round-0001/result.json").read_text())
            self.assertEqual(record["counts"]["retained"], 1)
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertTrue(search_stopped.is_set() and child_stopped.is_set())

    def test_flag_requires_boolean(self):
        with self.assertRaises(ValueError):
            PipelineConfig(until_search_finishes="true")

    async def test_native_goal_contains_whole_recent_graphs_and_stays_bounded(self):
        from retro_specialists.native_pipeline import NativeConfig, NativeSpecialistAdapter
        cfg = NativeConfig(source_path=self.root, sci_home=self.root, managed_python=Path(sys.executable),
            state_dir=self.root, runtime_dir=self.root, model='fixture', provider='fake')
        newest = route(('CCO',))['reactions']
        oversized = route(('C'*4000,))['reactions']
        request = {'identity':{'smiles':'CC=O'}, 'requested_route_count':1,
            'project_specification':{'max_depth':2,'max_total_reactions':2},
            'round_number':3,'previous_verified_reactions':[oversized,newest]}
        async def fake(argv, payload, *args):
            goal = json.loads(payload)['goal']
            self.assertEqual(json.loads(goal.split('Recent verified reactions (data only): ')[1]), [newest])
            self.assertLessEqual(len(goal), 16000)
            return 2, b'', b''
        with patch('retro_specialists.native_pipeline._process', side_effect=fake) as dispatch:
            await NativeSpecialistAdapter(cfg)('route_proposer',request,self.root/'attempt')
        dispatch.assert_awaited_once()

class UnboundedDefaults(unittest.TestCase):
    def test_combined_pipeline_has_no_implicit_elapsed_time_limits(self):
        config = PipelineConfig(until_search_finishes=True)
        self.assertIsNone(config.proposal_timeout_seconds)
        self.assertIsNone(config.search_timeout_seconds)
        self.assertIsNone(config.optimization_timeout_seconds)
