"""Native leaf-specialist tools. No model calls, delegation or core imports."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4
from .plugin_process import bounded_run, WorkerLimitError
from .proposal_transport import normalize_proposal_arguments, proposal_argument_schema

_MAX_INPUT = 2_000_000
_MAX_OUTPUT = 4_000_000
_ACTIONS = {
    'accept_specialist_retrosynthesis_result': ({'specialist', 'final_envelope', 'target', 'requested_count', 'max_depth', 'max_total_reactions'},
        {'specialist', 'final_envelope', 'target', 'requested_count', 'max_depth', 'max_total_reactions', 'task_id'}),
    'start_retrosynthesis_workflow': ({'target', 'name'}, {'target', 'name'}),
    'retrosynthesis_workflow_status': ({'job_id'}, {'job_id'}),
    'read_retrosynthesis_task': ({'task_id'}, {'task_id'}),
    'inspect_target_stereochemistry': ({'target'}, {'target'}),
    'search_route_literature': ({'query'}, {'query', 'max_results'}),
    'validate_route_hypotheses': ({'raw_response', 'target'}, {'raw_response', 'target', 'requested_count', 'max_depth', 'max_total_reactions', 'task_id'}),
    'verify_retrosynthesis_result': ({'final_response', 'validation_receipt_id', 'target'}, {'final_response', 'validation_receipt_id', 'target', 'task_id', 'compact_handoff'}),
    'accept_retrosynthesis_result': ({'final_envelope', 'target', 'requested_count', 'max_depth', 'max_total_reactions'},
        {'final_envelope', 'target', 'requested_count', 'max_depth', 'max_total_reactions', 'task_id'}),
}

_DEFAULT_RETROSYNTHESIS_POLICY = """## Default retrosynthesis routing
For a request to generate or search retrosynthesis routes, use `start_retrosynthesis_workflow(target, name)` as the default entry point, then poll `retrosynthesis_workflow_status(job_id)` until it completes. This workflow runs the existing model-based retrosynthesis search and the independent `route-proposer` concurrently, then returns the checked results. Do not submit the native retrosynthesis job separately first: that creates a duplicate search and bypasses the proposer. Use the exact target and disclose any user-requested limits that the configured workflow cannot express; do not imply such limits were applied. For discussion about retrosynthesis, answer normally without launching a job. If the combined workflow is unavailable or fails, report that clearly; do not silently fall back to a direct native submission.
"""


def _retrosynthesis_native_submission(tool_name, args):
    """Recognize the original retrosynthesis submission, including deferred tool calls."""
    name = tool_name
    payload = args if isinstance(args, dict) else {}
    if name == 'tool_call':
        resolved = False
        try:
            from tools.tool_search import resolve_underlying_call
            resolved_name, resolved_args, error = resolve_underlying_call(payload)
            if not error and resolved_name:
                name, payload = resolved_name, resolved_args
                resolved = True
        except Exception:
            pass
        if not resolved:
            # Keep a narrow fallback for the bridge's documented single-call shape.
            calls = payload.get('calls')
            if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict):
                name = calls[0].get('name', '')
                candidate = calls[0].get('arguments', calls[0].get('args', {}))
                payload = candidate if isinstance(candidate, dict) else {}
    if name in ('boltzmann_submit', 'retrosynthesis', 'Retrosynthesis'):
        job_name = payload.get('job_name')
        if isinstance(job_name, str) and job_name.strip().lower() == 'retrosynthesis':
            return True
        if name in ('retrosynthesis', 'Retrosynthesis'):
            return True
    return False


def _default_retrosynthesis_guard(tool_name='', args=None, **_):
    if not _retrosynthesis_native_submission(tool_name, args):
        return None
    return {
        'action': 'block',
        'message': ('Use the configured `start_retrosynthesis_workflow` for this request. '
                    'Pass the exact target from experiment_data.target_mol as target and a readable name; '
                    'then poll its returned job_id. The workflow submits the existing retrosynthesis '
                    'search itself alongside route-proposer, so do not submit a second native job.'),
    }


def _encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _arguments(action, args):
    if action not in _ACTIONS:
        raise ValueError('Tool is unavailable in the proposer-only workflow')
    required, allowed = _ACTIONS[action]
    if not isinstance(args, dict) or not required <= set(args) or set(args) - allowed:
        raise ValueError('Missing or unexpected tool arguments')
    if 'compact_handoff' in args and type(args['compact_handoff']) is not bool:
        raise ValueError('compact_handoff must be a JSON boolean')
    if action == 'inspect_target_stereochemistry' and (
            not isinstance(args.get('target'), str) or not args['target'].strip()
            or len(args['target']) > 4096):
        raise ValueError('Inspector target must be bounded SMILES text')
    if 'specialist' in args and (not isinstance(args['specialist'], str) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', args['specialist'])):
        raise ValueError('Invalid saved specialist name')
    if 'job_id' in args and (not isinstance(args['job_id'], str) or not re.fullmatch('[a-f0-9]{32}', args['job_id'])):
        raise ValueError('Invalid workflow job identifier')
    if 'name' in args and (not isinstance(args['name'], str) or not args['name'].strip() or len(args['name']) > 300):
        raise ValueError('Invalid target name')
    if 'task_id' in args and (not isinstance(args['task_id'], str) or not re.fullmatch('[a-f0-9]{32}', args['task_id'])):
        raise ValueError('Invalid task identifier')
    if len(_encode(args).encode()) > _MAX_INPUT:
        raise ValueError('Tool input exceeds size limit')
    for key, cap in (('query', 2000), ('target', 8192), ('raw_response', 1_000_000), ('final_response', 1_000_000), ('final_envelope', 1_000_000)):
        if key in args and (not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > cap):
            raise ValueError('Invalid bounded text field: ' + key)
    for key, high in (('max_results', 10), ('requested_count', 20), ('max_depth', 30), ('max_total_reactions', 100)):
        if key in args and (type(args[key]) is not int or not 1 <= args[key] <= high):
            raise ValueError('Invalid bounded integer: ' + key)
    if 'validation_receipt_id' in args and (not isinstance(args['validation_receipt_id'], str) or not re.fullmatch('[a-f0-9]{32}', args['validation_receipt_id'])):
        raise ValueError('Invalid validation receipt identifier')
    for key in ('baseline_routes', 'candidate_routes'):
        if key in args and (not isinstance(args[key], list) or len(args[key]) > 100):
            raise ValueError('Routes must be a bounded array')


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(_encode(value))


class PrimitiveTools:
    def __init__(self, ctx):
        self.ctx = ctx

    def _configuration(self):
        python = Path(self.ctx.get_config('backend_python', ''))
        libraries = self.ctx.get_config('backend_library_paths', [])
        timeout = self.ctx.get_config('backend_timeout_seconds', 45)
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError('Configure an absolute existing backend_python')
        if not isinstance(libraries, list) or any(not isinstance(p, str) or not Path(p).is_absolute() or not Path(p).is_dir() for p in libraries):
            raise ValueError('backend_library_paths must name existing absolute directories')
        if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
            raise ValueError('backend_timeout_seconds must be between 1 and 120')
        max_bytes = self.ctx.get_config('backend_max_output_bytes', _MAX_OUTPUT)
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 8_000_000:
            raise ValueError('backend_max_output_bytes must be between 1024 and 8000000')
        literature_timeout = self.ctx.get_config('literature_timeout_seconds', min(30, timeout * .8))
        if type(literature_timeout) not in (int, float) or not 0 < literature_timeout <= 30 or literature_timeout >= timeout:
            raise ValueError('literature_timeout_seconds must be positive, at most30 and below backend timeout')
        trusted = {'library_paths': libraries, 'state_dir': str(Path(self.ctx.state.data_dir).resolve()),
                   'inventory_file': self.ctx.get_config('inventory_file', ''),
                   'literature_timeout_seconds': literature_timeout,
                   'literature_include_fulltext': self.ctx.get_config('literature_include_fulltext', True),
                   'literature_context_chars': self.ctx.get_config('literature_context_chars', 6000),
                   'literature_cache_seconds': self.ctx.get_config('literature_cache_seconds', 86400),
                   'literature_fulltext_timeout_seconds': self.ctx.get_config('literature_fulltext_timeout_seconds', 8),
                   'quotes_file': self.ctx.get_config('quotes_file', ''),
                   'baseline_file': self.ctx.get_config('baseline_file', ''),
                   'baseline_sha256': self.ctx.get_config('baseline_sha256', ''),
                   'cost_assumptions': self.ctx.get_config('cost_assumptions', {}),
                   'pipeline_config_file': self.ctx.get_config('pipeline_config_file', ''),
                   'pipeline_runner_file': self.ctx.get_config('pipeline_runner_file', ''),
                   'pipeline_runner_sha256': self.ctx.get_config('pipeline_runner_sha256', '')}
        return python, timeout, max_bytes, trusted

    def _execute(self, action, args, configuration):
        python, timeout, max_bytes, trusted = configuration
        request = _encode({'action': action, 'arguments': args, 'trusted': trusted}).encode()
        worker = Path(__file__).with_name('backend_worker.py').resolve()
        try:
            code, raw, errors = bounded_run([str(python), '-I', str(worker)], request, timeout, max_bytes)
        except WorkerLimitError as exc:
            return {'status': str(exc), 'error': 'Backend bound exceeded; local process group terminated and worker reaped'}
        if code:
            return {'status': 'backend_failed', 'error': 'Backend failed; check configured dependencies and data'}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Backend response is not an object')
        return value

    def handle(self, action, args):
        if self.ctx.get_config('enabled', False) is not True:
            return _encode({'status': 'disabled', 'error': 'Enable and configure retrosynthesis_specialists for this profile'})
        try:
            args = normalize_proposal_arguments(action, args)
            _arguments(action, args)
        except (TypeError, ValueError) as exc:
            return _encode({'status': 'invalid_request', 'error': str(exc)})
        try:
            configuration = self._configuration()
        except (TypeError, ValueError) as exc:
            return _encode({'status': 'configuration_error', 'error': str(exc)})
        compact = action == 'verify_retrosynthesis_result' and args.get('compact_handoff', False)
        # Transport selection is not scientific input; persist the original validator contract.
        args = {key: value for key, value in args.items() if key != 'compact_handoff'}
        started, receipt = time.monotonic(), uuid4().hex
        try:
            if action == 'accept_specialist_retrosynthesis_result':
                from .portable_acceptance import acceptance_call
                backend_action, backend_args, backend_config = acceptance_call(args, configuration)
                result = self._execute(backend_action, backend_args, backend_config)
            else:
                result = self._execute(action, args, configuration)
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            result = {'status': 'backend_failed', 'error': type(exc).__name__ + ': backend response unavailable'}
        record = {'action': action, 'request': args, 'response': result,
                  'request_sha256': sha256(_encode(args).encode()).hexdigest(),
                  'response_sha256': sha256(_encode(result).encode()).hexdigest(),
                  'duration_seconds': time.monotonic() - started,
                  'created_at': datetime.now(timezone.utc).isoformat()}
        try:
            _write(Path(self.ctx.state.data_dir) / 'receipts' / (receipt + '.json'), record)
        except OSError:
            return _encode({'status': 'receipt_failed', 'error': 'Cannot persist backend evidence receipt'})
        if (compact and result.get('status') == 'completed'
                and isinstance(result.get('result'), dict)
                and result['result'].get('status') == 'verified_hypotheses'):
            return _encode({'status': 'completed', 'receipt_id': receipt,
                            'response_sha256': record['response_sha256']})
        return _encode({**result, 'receipt_id': receipt})


def _schema(name, description, properties, required):
    return {'name': name, 'description': description, 'parameters': {'type': 'object',
        'properties': properties, 'required': required, 'additionalProperties': False}}


def _workflow_routing_ready(service):
    """Check local admission prerequisites without submitting work or probing services.

    Standalone leaf installs have no workflow template and must not intercept
    the recipient's native scientific submissions. Registration is frozen for
    the session; changing deployment configuration requires a new session.
    This checks configured admission, not remote availability or model access.
    """
    if service.ctx.get_config('enabled', False) is not True:
        return False
    try:
        from .pipeline_jobs import _deployment
        trusted = service._configuration()[-1]
        _deployment(trusted)
    except (OSError, TypeError, ValueError):
        return False
    return True


def register(ctx):
    service = PrimitiveTools(ctx)
    if _workflow_routing_ready(service):
        ctx.register_system_prompt_section('retrosynthesis.default-routing', _DEFAULT_RETROSYNTHESIS_POLICY,
                                           max_chars=1800)
        ctx.register_hook('pre_tool_call', _default_retrosynthesis_guard)
    text = {'type': 'string'}
    bounded = lambda high: {'type': 'integer', 'minimum': 1, 'maximum': high}
    task_id = {**text, 'pattern': '^[a-f0-9]{32}$', 'minLength': 32, 'maxLength': 32}
    schemas = [
        _schema('accept_specialist_retrosynthesis_result', 'Main-only portable acceptance: provide the saved specialist name (including any local rename), unchanged final envelope and original target and limits. Host resolves the reviewed private package receipt store; no paths accepted. Owner support plugin installation is required. Graph acceptance does not establish chemistry.',
            {'specialist': {**text, 'pattern': '^[a-z0-9][a-z0-9_-]{0,63}$'}, 'final_envelope': {**text, 'maxLength': 1000000}, 'target': text,
             'requested_count': bounded(20), 'max_depth': bounded(30), 'max_total_reactions': bounded(100), 'task_id': task_id},
            ['specialist', 'final_envelope', 'target', 'requested_count', 'max_depth', 'max_total_reactions']),
        _schema('start_retrosynthesis_workflow', 'Main-only: start one configured workflow for the exact target. Runs the existing tool search and sole independent literature route proposer concurrently, then checks and merges results. Returns job_id immediately; poll status and do not repeat submission for a pending request.',
            {'target': {**text, 'maxLength': 4096}, 'name': {**text, 'minLength': 1, 'maxLength': 300}}, ['target', 'name']),
        _schema('retrosynthesis_workflow_status', 'Main-only: observe an existing workflow job and retrieve its final checked shortlist, scoped benefits and timing. Unknown local observation does not mean remote cancellation; never automatically resubmit.',
            {'job_id': task_id}, ['job_id']),
        _schema('read_retrosynthesis_task', 'Read an exact host-created immutable task record. Read your assigned task_id first; use its target, limits and baseline rather than copied task text. This tool cannot create tasks.',
            {'task_id': task_id}, ['task_id']),
        _schema('inspect_target_stereochemistry', 'Read-only molecular inspection of an exact target or candidate precursor: pass its SMILES in target to obtain canonical identity and backend-numbered stereo elements. Inspect the target first for direct route-proposer delegation without task_id. Bound tasks already include target requirements; inspect a precursor separately when its stereo origin needs explicit numbering. It creates no task, route or scientific job and does not establish feasibility.',
            {'target': {**text, 'minLength': 1, 'maxLength': 4096}}, ['target']),
        _schema('search_route_literature', 'Retrieve public literature evidence for a synthesis query. Retrieved records are source text, not verified chemistry.',
            {'query': {**text, 'maxLength': 2000}, 'max_results': bounded(10)}, ['query']),
        _schema('validate_route_hypotheses', 'Validate exact-target reaction graphs and assess references against retrieved evidence; no chemical feasibility claim.',
            {'raw_response': proposal_argument_schema(), 'target': text,
             'requested_count': bounded(20), 'max_depth': bounded(30), 'max_total_reactions': bounded(100), 'task_id': task_id}, ['raw_response', 'target']),
        _schema('verify_retrosynthesis_result', 'Specialist-side finalization of RAW proposal-contract JSON against a validation receipt. Prefer compact_handoff=true: successful verification returns only status, receipt_id and response_sha256; full validated output remains in the persisted receipt. Omitted or false preserves the full envelope; failures retain diagnostics. Do not pass expanded validated routes. Main accepts an owner-scoped child with accept_retrosynthesis_result; for a reviewed privately installed child, Main uses accept_specialist_retrosynthesis_result with the actual saved specialist name.',
            {'final_response': proposal_argument_schema(), 'validation_receipt_id': {**text, 'pattern': '^[a-f0-9]{32}$'}, 'target': text, 'task_id': task_id, 'compact_handoff': {'type': 'boolean', 'default': False}},
            ['final_response', 'validation_receipt_id', 'target']),
        _schema('accept_retrosynthesis_result', "Main-agent acceptance: pass the saved specialist's entire final JSON unchanged, including its verification receipt, plus the exact target and original task requested_count, max_depth (longest-path depth), and max_total_reactions. Do not copy limits chosen by the child. Missing limits fail closed. When assigned task_id, include that original ID; target and limits must match the immutable host record. Legacy calls bind only explicit Main expectations. Do not extract expanded routes or reconstruct raw proposal JSON. This checks the stored receipt and returns verified hypotheses or quarantine; no new model call.",
            {'final_envelope': {**text, 'maxLength': 1000000}, 'target': text,
             'requested_count': bounded(20), 'max_depth': bounded(30), 'max_total_reactions': bounded(100), 'task_id': task_id},
            ['final_envelope', 'target', 'requested_count', 'max_depth', 'max_total_reactions']),

    ]
    for schema in schemas:
        name = schema['name']
        def handler(args, *, _action=name):
            return service.handle(_action, args)
        ctx.register_tool(name=name, toolset='retrosynthesis_specialists', schema=schema,
                          handler=handler, description=schema['description'])
