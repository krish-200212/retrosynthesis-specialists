"""A failed helper can restart only when no submission could have begun."""
import asyncio
import json
import os
import unittest

from tests import test_science_pipeline_adapter as fixtures
from retro_specialists.science_pipeline import ScienceSearch


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SciencePipelineTests.setUp
    adapter = fixtures.SciencePipelineTests.adapter
    completed_code = fixtures.SciencePipelineTests.completed_code

    def startup_code(self, retry_body=None):
        prelude = '''import sys
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=True)
counter=p/'helper-invocations'
n=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(n))
if n==1:sys.exit(2)
'''
        return prelude + (retry_body if retry_body is not None else self.completed_code(
            "(p/'claim.json').write_text('{}');(p/'actual-submit-count').write_text('1')"))

    async def test_safe_startup_retry_has_one_actual_submission(self):
        result=await self.adapter(self.startup_code(),startup_retry_delay=.01)()
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.run/'helper-invocations').read_text(),'2')
        self.assertEqual((self.run/'actual-submit-count').read_text(),'1')
        self.assertFalse(result['resubmitted'])
        self.assertEqual(result['startup_recovery']['attempted'],1)
        self.assertEqual(result['startup_recovery']['initial_helper_error'],'helper_exit_2')
        self.assertEqual(result['helper_diagnostics'][0]['source'],'helper_process_exit_status')
        self.assertEqual(result['helper_diagnostics'][0]['phase'],'initial')

    async def test_claim_before_failure_forbids_retry_even_without_state(self):
        code="import sys;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir();(p/'claim.json').write_text('{}');sys.exit(2)"
        result=await self.adapter(code,startup_retry_delay=0)()
        self.assertEqual(result['startup_recovery']['attempted'],0)
        self.assertEqual(result['startup_recovery']['guard']['reason'],'submission_receipt_present')

    async def test_nonzero_retry_failure_keeps_both_original_diagnostics(self):
        result=await self.adapter(self.startup_code('sys.exit(7)'),startup_retry_delay=0)()
        self.assertEqual(result['reason'],'helper_exit_7')
        self.assertEqual([d['helper_error'] for d in result['helper_diagnostics']],['helper_exit_2','helper_exit_7'])
        self.assertEqual((self.run/'helper-invocations').read_text(),'2')
        self.assertEqual(result['startup_recovery']['initial_helper_error'],'helper_exit_2')

    async def test_short_deadline_skips_startup_retry(self):
        result=await self.adapter(self.startup_code(),startup_retry_delay=5,timeout_seconds=.3)()
        self.assertEqual(result['startup_recovery']['attempted'],0)
        self.assertEqual((self.run/'helper-invocations').read_text(),'1')

    async def test_cancel_during_startup_backoff(self):
        task=asyncio.create_task(self.adapter(self.startup_code(),startup_retry_delay=5)())
        for _ in range(100):
            if (self.run/'helper-invocations').exists():break
            await asyncio.sleep(.01)
        await asyncio.sleep(.03);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual((self.run/'helper-invocations').read_text(),'1')

    async def test_cancel_restart_process_kills_and_reaps(self):
        retry="import os,time;(p/'retry-pid').write_text(str(os.getpid()));time.sleep(60)"
        task=asyncio.create_task(self.adapter(self.startup_code(retry),startup_retry_delay=0)())
        for _ in range(100):
            if (self.run/'retry-pid').exists():break
            await asyncio.sleep(.01)
        self.assertTrue((self.run/'retry-pid').exists());task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with self.assertRaises(ProcessLookupError):os.kill(int((self.run/'retry-pid').read_text()),0)

    def test_startup_backoff_bounds(self):
        for delay in [-1,True,61,float('inf'),'5']:
            with self.subTest(delay=delay),self.assertRaises(ValueError):
                ScienceSearch(self.config,self.run,'CCO',startup_retry_delay=delay)

    async def test_receipt_created_during_backoff_blocks_restart(self):
        task=asyncio.create_task(self.adapter(self.startup_code(),startup_retry_delay=.2)())
        for _ in range(100):
            if (self.run/'helper-invocations').exists():break
            await asyncio.sleep(.01)
        await asyncio.sleep(.05)
        (self.run/'claim.json').write_text('{}')
        result=await task
        self.assertEqual(result['startup_recovery']['attempted'],0)
        self.assertEqual(result['startup_recovery']['guard']['reason'],'submission_receipt_present')
        self.assertEqual((self.run/'helper-invocations').read_text(),'1')

    async def test_invalid_retry_result_preserves_original_error_source(self):
        code=self.startup_code(self.completed_code("s['target']='CCC'"))
        result=await self.adapter(code,startup_retry_delay=0)()
        self.assertEqual(result['reason'],'invalid_durable_artifact')
        self.assertEqual(result['helper_diagnostics'][0],{
            'source':'helper_process_exit_status','phase':'initial','helper_error':'helper_exit_2'})
        self.assertEqual(result['startup_recovery']['initial_helper_error'],'helper_exit_2')

    async def test_dangling_claim_is_not_absence_proof(self):
        self.run.mkdir();(self.run/'claim.json').symlink_to(self.run/'missing')
        result=await self.adapter("raise AssertionError('must not invoke')",startup_retry_delay=0)()
        self.assertEqual(result['reason'],'existing_submission_requires_observation')

    async def test_explicit_observation_can_restart_without_submitting(self):
        code=self.startup_code(self.completed_code("s['doc_id']='known'"))
        result=await self.adapter(code,action='observe',doc_id='known',startup_retry_delay=0)()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['startup_recovery']['guard']['reason'],'explicit_receipt_observation')
        self.assertEqual(result['startup_recovery']['attempts'][0]['action'],'observe')
        self.assertEqual(result['startup_recovery']['attempts'][0]['doc_id'],'known')
