"""Async adapter for the existing managed public science MCP lifecycle.

This launches only the checked-in helper using fixed argv and JSON stdin. It
never retries a scientific submission; one pre-claim helper startup retry is allowed. Cancellation stops observation locally;
the remote job may continue and its durable receipt must be resumed explicitly.
"""
import asyncio
from copy import copy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import signal
import time

from .baseline import parse_baseline
from .contracts import positive_limit
from .graphs import canonical_smiles
from .science import _read

MAX_OUTPUT = 2_000_000


class _OutputLimit(ValueError):
    pass


async def _bounded_read(stream):
    chunks, count = [], 0
    while True:
        block = await stream.read(65536)
        if not block:
            return b''.join(chunks)
        count += len(block)
        if count > MAX_OUTPUT:
            raise _OutputLimit('Managed helper output exceeded limit')
        chunks.append(block)


async def _stop(process):
    # The leader may already have exited while a descendant still owns a pipe.
    # Every helper starts a dedicated session; terminate its remaining group
    # even when the leader's return code is already known. Only the direct
    # subprocess is reaped here; orphaned descendants may briefly be zombies.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    async def discard(stream):
        while await stream.read(65536):
            pass
    async with asyncio.timeout(3):
        await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())


class ScienceSearch:
    """One public retrosynthesis submission or explicit observation.

    Config credentials remain managed by the existing public server. Constructor
    receives deployment paths, never molecular/LLM-supplied executable strings.
    The observed inventory is a subset only, never the complete catalog.
    """
    def __init__(self, config_file, run_dir, target, timeout_seconds=1800,
                 action='submit', doc_id=None, managed_python=None,
                 max_depth=5, max_total_reactions=20, artifact_recovery_attempts=2,
                 artifact_recovery_delays=(5, 15), startup_retry_delay=5):
        self.config_file = Path(config_file).absolute()
        if not self.config_file.is_file() or self.config_file.is_symlink() or self.config_file.stat().st_size > 65536:
            raise ValueError('Existing bounded deployment config is required')
        config = json.loads(self.config_file.read_text())
        interpreter = Path(managed_python or config['server_python'])
        if not interpreter.is_absolute() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise ValueError('Managed interpreter must be an existing absolute executable')
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400:
            raise ValueError('Search deadline must be finite, positive and at most one day')
        if action not in ('submit', 'observe', 'status') or action == 'submit' and doc_id is not None:
            raise ValueError('Choose a submission or explicit observation action')
        if doc_id is not None and (not isinstance(doc_id, str) or not doc_id or len(doc_id) > 512):
            raise ValueError('Invalid accepted job receipt')
        if type(artifact_recovery_attempts) is not int or not 0 <= artifact_recovery_attempts <= 2:
            raise ValueError('Artifact recovery attempts must be 0..2')
        if (not isinstance(artifact_recovery_delays, (list, tuple))
                or len(artifact_recovery_delays) != 2
                or any(type(delay) not in (int, float) or not math.isfinite(delay)
                       or not 0 <= delay <= 60 for delay in artifact_recovery_delays)):
            raise ValueError('Artifact recovery requires two finite backoffs in 0..60 seconds')
        if (type(startup_retry_delay) not in (int, float) or not math.isfinite(startup_retry_delay)
                or not 0 <= startup_retry_delay <= 60):
            raise ValueError('Startup retry backoff must be finite and in 0..60 seconds')
        self.startup_retry_delay = startup_retry_delay
        self.artifact_recovery_delays = tuple(artifact_recovery_delays)
        self.artifact_recovery_attempts = artifact_recovery_attempts
        self.managed_python, self.timeout_seconds = str(interpreter), timeout_seconds
        self.action, self.doc_id = action, doc_id
        self.target = canonical_smiles(target)
        self.run_dir = Path(run_dir).absolute()
        if self.run_dir == Path('/') or '..' in self.run_dir.parts:
            raise ValueError('Dedicated durable run directory is required')
        self.max_depth = positive_limit(max_depth, 'max_depth', 100)
        self.max_total_reactions = positive_limit(max_total_reactions, 'max_total_reactions', 200)

    def _command(self):
        helper = Path(__file__).resolve().parents[1] / 'scripts/run_science.py'
        command = [self.managed_python, str(helper), '--config', str(self.config_file),
                   '--run-dir', str(self.run_dir), '--action', self.action,
                   '--timeout', str(self.timeout_seconds)]
        return command + (['--doc-id', self.doc_id] if self.doc_id is not None else [])

    def _failure(self, reason, started, **details):
        return {'status': 'failed', 'reason': reason, 'routes': [],
                'elapsed_seconds': time.monotonic() - started,
                'resubmitted': False, **details}

    def _result(self, started):
        state = json.loads(_read(self.run_dir / 'state.json'))
        if canonical_smiles(state['target']) != self.target or state['job_name'] != 'retrosynthesis':
            raise ValueError('Durable state identity mismatch')
        if self.doc_id is not None and state.get('doc_id') != self.doc_id:
            raise ValueError('Durable accepted job receipt mismatch')
        common = {'remote_state': state['state'], 'artifact_status': state.get('artifact_status'),
                  'elapsed_seconds': time.monotonic() - started,
                  'cold_or_warm_loading': 'not_measured', 'pure_model_inference_seconds': None,
                  'resubmitted': False}
        if state['state'] != 'completed':
            return self._failure('remote_result_not_completed', started, **common)
        artifacts = state.get('artifacts', [])
        if not artifacts:
            return self._failure('completed_artifact_unavailable', started, **common)
        if not isinstance(artifacts, list) or len(artifacts) > 100:
            raise ValueError('Invalid artifact list')
        parsed = []
        for artifact in artifacts:
            path = Path(artifact['path'])
            if not path.is_absolute() or not path.is_relative_to(self.run_dir) or path.resolve() != path:
                raise ValueError('Artifact escaped durable directory')
            raw = _read(path)
            if len(raw) != artifact['size_bytes'] or sha256(raw).hexdigest() != artifact['sha256']:
                raise ValueError('Artifact identity mismatch')
            try:
                public = json.loads(raw)
                baseline = parse_baseline(public, self.target, self.max_depth, self.max_total_reactions)
            except (ValueError, TypeError, KeyError):
                continue
            parsed.append((public, baseline, artifact['sha256']))
        if len(parsed) != 1:
            return self._failure('no_unambiguous_route_artifact', started, **common)
        public, baseline, digest = parsed[0]
        return {**common, 'status': 'completed', 'routes': baseline['routes'],
                'public_output': public, 'artifact_sha256': digest,
                'inventory_observed': list(baseline['inventory_observed']),
                'inventory_is_full_catalog': False, 'parser_rejections': baseline['rejections'],
                'provenance': baseline['provenance'],
                'timing_scope': 'Managed helper startup, submission/observation and artifact download; not pure model inference.'}

    async def _invoke(self):
        process, tasks = None, []
        try:
            process = await asyncio.create_subprocess_exec(*self._command(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
            async def send():
                request = {'job_name': 'retrosynthesis', 'experiment_data': {'target_mol': self.target}}
                process.stdin.write(json.dumps(request).encode())
                await process.stdin.drain()
                process.stdin.close()
            tasks = [asyncio.create_task(send()), asyncio.create_task(_bounded_read(process.stdout)),
                     asyncio.create_task(_bounded_read(process.stderr)), asyncio.create_task(process.wait())]
            async with asyncio.timeout(self.timeout_seconds):
                await asyncio.gather(*tasks)
            return None if process.returncode == 0 else 'helper_exit_' + str(process.returncode)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return 'helper_deadline_exceeded'
        except _OutputLimit:
            return 'helper_output_exceeded'
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return 'invalid_durable_artifact'
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None:
                await _stop(process)

    def _invocation_result(self, error, started):
        # A nonzero helper exit can precede state creation, or follow a valid
        # download (for example cleanup failure). Keep the diagnostic without
        # discarding independently checked durable state/artifact identity.
        if error is None:
            return self._result(started)
        if error.startswith('helper_exit_') and (self.run_dir / 'state.json').is_file():
            return {**self._result(started), 'helper_error': error}
        return self._failure(error, started)

    def _recovery_receipt(self, result):
        if (self.action == 'status' or result.get('reason') != 'completed_artifact_unavailable'
                or result.get('remote_state') != 'completed'):
            return None
        state = json.loads(_read(self.run_dir / 'state.json'))
        receipt = state.get('doc_id')
        if (state.get('state') != 'completed' or state.get('artifacts')
                or not isinstance(receipt, str) or not 1 <= len(receipt) <= 512):
            return None
        if self.doc_id is not None and receipt != self.doc_id:
            raise ValueError('Recovery receipt differs from original observation')
        return receipt

    def _startup_guard(self, error):
        # run_job fsyncs its exclusive claim before scientific submission.
        # Broken symlinks count as receipts too; absence must be unambiguous.
        present = {name: os.path.lexists(self.run_dir / name) for name in ('claim.json', 'state.json')}
        nonzero_exit = isinstance(error, str) and error.startswith('helper_exit_')
        eligible = nonzero_exit and not any(present.values()) and (
            self.action == 'submit' or self.action == 'observe' and self.doc_id is not None)
        reason = ('submission_receipt_present' if any(present.values()) else
                  'known_not_started' if eligible and self.action == 'submit' else
                  'explicit_receipt_observation' if eligible else 'not_safe_initialization_failure')
        return {'eligible': eligible, 'reason': reason, 'action': self.action,
                'claim_present': present['claim.json'], 'state_present': present['state.json']}

    async def _recover_startup(self, error, deadline):
        guard = self._startup_guard(error)
        record = {'maximum': 1, 'attempted': 0, 'guard': guard, 'initial_helper_error': error,
                  'attempts': [], 'policy': 'Restart helper once only before submission claim/state, or for explicit receipt observation.'}
        if not guard['eligible'] or deadline - time.monotonic() <= self.startup_retry_delay + .05:
            return error, record
        await asyncio.sleep(self.startup_retry_delay)
        # Another actor may have created a durable receipt during the backoff.
        guard = self._startup_guard(error)
        remaining = deadline - time.monotonic()
        if not guard['eligible'] or remaining <= .05:
            return error, {**record, 'guard': guard}
        retry = copy(self)
        retry.timeout_seconds = remaining
        retry_error = await retry._invoke()
        return retry_error, {**record, 'guard': guard, 'attempted': 1,
            'attempts': [{'action': self.action, 'doc_id': self.doc_id,
                          'backoff_seconds': self.startup_retry_delay, 'helper_error': retry_error}]}

    async def __call__(self):
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        recovery = []
        initial_error = None
        diagnostics = []
        startup = {'maximum': 1, 'attempted': 0, 'attempts': []}
        if self.action == 'submit' and any(os.path.lexists(self.run_dir / name) for name in ('claim.json', 'state.json')):
            return self._failure('existing_submission_requires_observation', started)
        try:
            error = await self._invoke()
            initial_error = error
            if error is not None:
                diagnostics = [{'source': 'helper_process_exit_status' if error.startswith('helper_exit_') else 'helper_lifecycle',
                                'phase': 'initial', 'helper_error': error}]
            error, startup = await self._recover_startup(error, deadline)
            if startup['attempted'] and error is not None:
                diagnostics = [*diagnostics, {'source': 'helper_process_exit_status' if error.startswith('helper_exit_') else 'helper_lifecycle',
                                             'phase': 'startup_retry', 'helper_error': error}]
            result = self._invocation_result(error, started)
            for index in range(self.artifact_recovery_attempts):
                receipt = self._recovery_receipt(result)
                # Reserve time for observation after the bounded backoff.
                # There is no new search budget and no second submit action.
                delay = self.artifact_recovery_delays[index]
                if receipt is None or deadline - time.monotonic() <= delay + .05:
                    break
                await asyncio.sleep(delay)
                observer = copy(self)
                observer.action, observer.doc_id = 'observe', receipt
                observer.timeout_seconds = deadline - time.monotonic()
                if observer.timeout_seconds <= 0:
                    break
                error = await observer._invoke()
                if error is not None:
                    diagnostics = [*diagnostics, {'source': 'helper_process_exit_status' if error.startswith('helper_exit_') else 'helper_lifecycle',
                                                 'phase': 'artifact_observation', 'helper_error': error}]
                refreshed = {**{key: result[key] for key in ('remote_state', 'artifact_status') if key in result},
                    **observer._invocation_result(error, started)}
                recovery.append({'action': 'observe', 'doc_id': receipt, 'backoff_seconds': delay,
                    'remote_state': refreshed.get('remote_state'),
                    'artifact_status': refreshed.get('artifact_status'), 'helper_error': error})
                result = refreshed
            return {**result, 'startup_recovery': startup, 'helper_diagnostics': diagnostics, 'elapsed_seconds': time.monotonic() - started,
                'artifact_recovery': {'attempted': len(recovery), 'maximum': self.artifact_recovery_attempts,
                    'attempts': recovery, 'initial_helper_error': initial_error,
                    'within_original_deadline': time.monotonic() <= deadline,
                    'policy': 'Observe the same accepted completed job only; never resubmit.'}}
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return self._failure('invalid_durable_artifact', started, startup_recovery=startup,
                helper_diagnostics=diagnostics,
                artifact_recovery={'attempted': len(recovery), 'maximum': self.artifact_recovery_attempts,
                                   'attempts': recovery})
