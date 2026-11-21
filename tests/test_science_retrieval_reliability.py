"""Offline regressions for same-receipt artifact retry and helper failures."""
import asyncio
import json
import os
import unittest
import sys

from tests import test_science_pipeline_adapter as fixtures
from retro_specialists.science_pipeline import ScienceSearch


class RetrievalReliabilityTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SciencePipelineTests.setUp
    adapter = fixtures.SciencePipelineTests.adapter
    completed_code = fixtures.SciencePipelineTests.completed_code

    async def test_nonzero_exit_preserves_validated_artifact_with_diagnostic(self):
        search = self.adapter(self.completed_code() + '\nsys.exit(7)\n')
        result = await search()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['helper_error'], 'helper_exit_7')
        self.assertEqual(result['remote_state'], 'completed')

    async def test_nonzero_without_state_has_safe_error_and_never_retries(self):
        result = await self.adapter("import sys;print('secret-token');sys.exit(2)",startup_retry_delay=0)()
        self.assertEqual(result['reason'], 'helper_exit_2')
        self.assertNotIn('secret-token', json.dumps(result))
        self.assertEqual(result['artifact_recovery']['attempted'], 0)

    async def test_nonzero_completed_state_recovers_same_accepted_job(self):
        code = self.completed_code("""s['doc_id']='accepted-once'
log=p/'actions.json';actions=json.loads(log.read_text()) if log.exists() else []
actions.append([sys.argv[2],sys.argv[3]]);log.write_text(json.dumps(actions))
if len(actions)<3: s['artifacts']=[];s['artifact_status']='unavailable'
""") + "\nif len(actions)<3: sys.exit(2)\n"
        helper=self.root/'recover.py';helper.write_text(code)
        class Recovery(ScienceSearch):
            def _command(child):
                return [sys.executable,str(helper),str(child.run_dir),child.action,child.doc_id or '']
        search=Recovery(self.config,self.run,'CCO',timeout_seconds=3,
                        artifact_recovery_delays=(.01,.02))
        result=await search()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(json.loads((self.run/'actions.json').read_text()),
                         [['submit',''],['observe','accepted-once'],['observe','accepted-once']])
        recovery=result['artifact_recovery']
        self.assertEqual(recovery['initial_helper_error'],'helper_exit_2')
        self.assertEqual(recovery['attempts'][0]['helper_error'],'helper_exit_2')
        self.assertEqual([a['backoff_seconds'] for a in recovery['attempts']],[.01,.02])
        self.assertTrue(recovery['within_original_deadline'])

    async def test_configured_backoff_respects_remaining_original_deadline(self):
        search=self.adapter(self.completed_code("s['doc_id']='known';s['artifacts']=[]"),
                            timeout_seconds=.3,artifact_recovery_delays=(5,15))
        result=await search()
        self.assertEqual(result['remote_state'],'completed')
        self.assertEqual(result['artifact_recovery']['attempted'],0)
        self.assertLess(result['elapsed_seconds'],.3)

    async def test_cancellation_during_backoff_never_observes(self):
        search=self.adapter(self.completed_code("s['doc_id']='known';s['artifacts']=[]"),
                            timeout_seconds=30,artifact_recovery_delays=(5,15))
        task=asyncio.create_task(search())
        for _ in range(100):
            if (self.run/'state.json').exists(): break
            await asyncio.sleep(.01)
        await asyncio.sleep(.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task

    def test_recovery_delay_configuration_is_bounded(self):
        for delays in [(1,),(-1,2),(True,2),(float('nan'),2),(1,61),'5,15']:
            with self.subTest(delays=delays),self.assertRaises(ValueError):
                ScienceSearch(self.config,self.run,'CCO',artifact_recovery_delays=delays)

    async def test_nonzero_observer_cannot_reuse_another_job_receipt(self):
        result=await self.adapter(self.completed_code("s['doc_id']='wrong'")+'\nsys.exit(2)\n',
                                  action='observe',doc_id='original')()
        self.assertEqual(result['reason'],'invalid_durable_artifact')
        self.assertEqual(result['artifact_recovery']['attempted'],0)

    async def test_cancel_recovery_observation_reaps_its_subprocess(self):
        code=self.completed_code("""s['doc_id']='known';s['artifacts']=[]
if sys.argv[2]=='observe':
 import os,time
 (p/'observer-pid').write_text(str(os.getpid()))
 time.sleep(60)
""")
        helper=self.root/'observer.py';helper.write_text(code)
        class Recovery(ScienceSearch):
            def _command(child):
                return [sys.executable,str(helper),str(child.run_dir),child.action]
        task=asyncio.create_task(Recovery(self.config,self.run,'CCO',
                                artifact_recovery_delays=(0,0))())
        for _ in range(100):
            if (self.run/'observer-pid').exists(): break
            await asyncio.sleep(.01)
        self.assertTrue((self.run/'observer-pid').exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.run/'observer-pid').read_text()),0)
