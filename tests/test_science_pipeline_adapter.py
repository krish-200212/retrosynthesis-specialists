"""Offline lifecycle checks for the managed public science search adapter."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from retro_specialists.science_pipeline import ScienceSearch

PUBLIC = {'top0': {'path': {'type': 'mol', 'smiles': 'CCO', 'children': [
    {'type': 'reaction', 'children': [{'type': 'mol', 'smiles': 'CC=O', 'in_stock': True}]}]}, 'idx': 0}}


class SciencePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'server_python': sys.executable}))
        self.run = self.root / 'run'

    def adapter(self, code, **kwargs):
        helper = self.root / 'helper.py'
        helper.write_text(code)
        class FakeSearch(ScienceSearch):
            def _command(child):
                return [sys.executable, str(helper), str(child.run_dir)]
        return FakeSearch(self.config, self.run, 'CCO', **kwargs)

    def completed_code(self, change=''):
        return '''import json,sys,hashlib
from pathlib import Path
request=json.load(sys.stdin)
p=Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=True)
a=p/'routes.json';a.write_text(json.dumps(''' + repr(PUBLIC) + '''))
s={'state':'completed','target':request['experiment_data']['target_mol'],'job_name':'retrosynthesis','artifact_status':'available','artifacts':[{'path':str(a),'sha256':hashlib.sha256(a.read_bytes()).hexdigest(),'size_bytes':a.stat().st_size}]}
''' + change + '''
(p/'state.json').write_text(json.dumps(s))
print(json.dumps(s))
'''

    async def test_unbounded_helper_has_no_deadline_and_accepts_completed_result(self):
        search = self.adapter(self.completed_code(), timeout_seconds=None)
        self.assertIn('--no-timeout', ScienceSearch._command(search))
        self.assertEqual((await search())['status'], 'completed')

    async def test_returns_parsed_routes_and_original_public_output(self):
        result = await self.adapter(self.completed_code())()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['routes']), 1)
        self.assertEqual(result['public_output'], PUBLIC)
        self.assertEqual(result['inventory_observed'], ['CC=O'])
        self.assertFalse(result['inventory_is_full_catalog'])
        self.assertGreater(result['elapsed_seconds'], 0)

    async def test_helper_is_concurrent_and_reads_request_via_stdin(self):
        search = self.adapter(self.completed_code())
        task = asyncio.create_task(search())
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual((await task)['status'], 'completed')

    async def test_no_second_submission_when_durable_claim_exists(self):
        self.run.mkdir(); (self.run / 'claim.json').write_text('{}')
        result = await self.adapter("raise AssertionError('must not launch')")()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'existing_submission_requires_observation')

    async def test_completed_missing_artifact_preserves_remote_outcome(self):
        result = await self.adapter(self.completed_code("s['artifacts']=[];s['artifact_status']='unavailable'"), artifact_recovery_attempts=0)()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['remote_state'], 'completed')
        self.assertEqual(result['reason'], 'completed_artifact_unavailable')

    async def test_wrong_target_and_artifact_hash_rejected(self):
        for change in ["s['target']='CCC'", "s['artifacts'][0]['sha256']='0'*64"]:
            with self.subTest(change=change):
                result = await self.adapter(self.completed_code(change), action='observe')()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['reason'], 'invalid_durable_artifact')

    async def test_escaping_artifact_rejected(self):
        outside = self.root / 'outside.json'; outside.write_text('{}')
        change = "s['artifacts'][0]['path']=" + repr(str(outside))
        result = await self.adapter(self.completed_code(change))()
        self.assertEqual(result['reason'], 'invalid_durable_artifact')

    async def test_deadline_terminates_and_reaps_process(self):
        code = "import os,sys,time;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir();(p/'pid').write_text(str(os.getpid()));time.sleep(60)"
        result = await self.adapter(code, timeout_seconds=.12)()
        self.assertEqual(result['reason'], 'helper_deadline_exceeded')
        pid = int((self.run / 'pid').read_text())
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    async def test_cancel_terminates_and_reaps_process(self):
        code = "import os,sys,time;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir();(p/'pid').write_text(str(os.getpid()));time.sleep(60)"
        task = asyncio.create_task(self.adapter(code)())
        for _ in range(100):
            if (self.run / 'pid').exists(): break
            await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        with self.assertRaises(ProcessLookupError): os.kill(int((self.run / 'pid').read_text()), 0)

    @unittest.skipUnless(hasattr(os, 'fork'), 'Linux/POSIX process-group lifecycle')
    async def test_exited_helper_cannot_leave_descendant_holding_pipes(self):
        code = """import os,sys,time
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir()
child=os.fork()
if child==0:
    (p/'child-pid').write_text(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
os._exit(0)
"""
        search=self.adapter(code,timeout_seconds=.15)
        try:
            result=await search()
            self.assertEqual(result['reason'],'helper_deadline_exceeded')
            child=int((self.run/'child-pid').read_text())
            for _ in range(100):
                stat=Path('/proc')/str(child)/'stat'
                if not stat.exists() or stat.read_text().split()[2]=='Z':
                    break
                await asyncio.sleep(.01)
            else:
                self.fail('Descendant remains alive after helper cleanup')
        finally:
            path=self.run/'child-pid'
            if path.exists():
                try: os.kill(int(path.read_text()),9)
                except ProcessLookupError: pass

    async def test_excessive_output_is_bounded(self):
        result = await self.adapter("import sys;sys.stdout.write('x'*3000000)")()
        self.assertEqual(result['reason'], 'helper_output_exceeded')

    async def test_completed_artifact_recovery_observes_same_receipt_only(self):
        code = self.completed_code("""s['doc_id']='accepted-once'
log=p/'actions.json';actions=json.loads(log.read_text()) if log.exists() else []
action=sys.argv[2];actions.append(action);log.write_text(json.dumps(actions))
if action=='submit': s['artifacts']=[];s['artifact_status']='unavailable'
else: assert sys.argv[3]=='accepted-once'
""")
        helper = self.root/'recover.py';helper.write_text(code)
        class Recovery(ScienceSearch):
            def _command(child):
                return [sys.executable,str(helper),str(child.run_dir),child.action,child.doc_id or '']
        result=await Recovery(self.config,self.run,'CCO',timeout_seconds=5,artifact_recovery_delays=(.01,.02))()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(json.loads((self.run/'actions.json').read_text()),['submit','observe'])
        self.assertEqual(result['artifact_recovery']['attempted'],1)
        self.assertFalse(result['resubmitted'])

    async def test_missing_receipt_failed_state_and_deadline_never_recover(self):
        changes=["s['artifacts']=[]", "s['state']='failed';s['artifacts']=[];s['doc_id']='known'", "s['artifacts']=[];s['doc_id']='known'"]
        for index,change in enumerate(changes):
            with self.subTest(index=index):
                search=self.adapter(self.completed_code(change),action='observe',timeout_seconds=.5)
                result=await search()
                self.assertEqual(result['artifact_recovery']['attempted'],0)

    async def test_recovery_attempts_are_bounded(self):
        code=self.completed_code("s['doc_id']='known';s['artifacts']=[]")
        result=await self.adapter(code,artifact_recovery_attempts=1,timeout_seconds=5,artifact_recovery_delays=(.01,.02))()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['artifact_recovery']['attempted'],1)
        self.assertEqual(result['remote_state'],'completed')

    async def test_status_only_does_not_download_or_recover(self):
        result=await self.adapter(self.completed_code("s['doc_id']='known';s['artifacts']=[]"),action='status')()
        self.assertEqual(result['artifact_recovery']['attempted'],0)

    async def test_observation_cannot_switch_accepted_receipt(self):
        result=await self.adapter(self.completed_code("s['doc_id']='other';s['artifacts']=[]"),action='observe',doc_id='original')()
        self.assertEqual(result['reason'],'invalid_durable_artifact')
        self.assertEqual(result['artifact_recovery']['attempted'],0)

    def test_invalid_constructor_bounds(self):
        for kwargs in [{'timeout_seconds':0}, {'action':'retry'}, {'managed_python':'python3'}, {'artifact_recovery_attempts':3}, {'artifact_recovery_attempts':True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ScienceSearch(self.config, self.run, 'CCO', **kwargs)
