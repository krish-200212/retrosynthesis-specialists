"""Primitive plugin boundaries use no models or scientific services."""
import json
import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from retro_specialists.plugin import PrimitiveTools, register

class Context:
    def __init__(self, root, **config):
        self.state = SimpleNamespace(data_dir=Path(root))
        self.config = config
        self.registered = {}
        self.hooks = {}
        self.prompt_sections = {}
    def get_config(self, key, default=None):
        return self.config.get(key, default)
    def register_tool(self, **kwargs):
        self.registered[kwargs['name']] = kwargs
    def register_hook(self, name, callback):
        self.hooks[name] = callback
    def register_system_prompt_section(self, name, content, **kwargs):
        self.prompt_sections[name] = content

class PluginTests(unittest.TestCase):
    def test_default_routing_blocks_only_new_native_retrosynthesis_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / 'runner.py'
            runner.write_text('# inert runner fixture\n')
            template = Path(tmp) / 'pipeline.json'
            template.write_text('{}')
            from hashlib import sha256
            ctx = Context(tmp, enabled=True, backend_python=sys.executable,
                          pipeline_runner_file=str(runner), pipeline_config_file=str(template),
                          pipeline_runner_sha256=sha256(runner.read_bytes()).hexdigest())
            register(ctx)
            guard = ctx.hooks['pre_tool_call']
            args = {'job_name': 'retrosynthesis', 'experiment_data': {'target_mol': 'CCO'}}
            result = guard(tool_name='boltzmann_submit', args=args)
            self.assertEqual(result['action'], 'block')
            self.assertIn('start_retrosynthesis_workflow', result['message'])
            self.assertEqual(args['experiment_data'], {'target_mol': 'CCO'})
            for name, payload in [('start_retrosynthesis_workflow', {'target': 'CCO', 'name': 'ethanol'}),
                                  ('boltzmann_status', args), ('boltzmann_download', args),
                                  ('boltzmann_submit', {'job_name': 'forward_reaction'})]:
                with self.subTest(name=name, payload=payload):
                    self.assertIsNone(guard(tool_name=name, args=payload))
            self.assertIn('start_retrosynthesis_workflow', ctx.prompt_sections['retrosynthesis.default-routing'])

    def test_leaf_install_and_disabled_plugin_do_not_redirect_native_search(self):
        for config in ({}, {'enabled': True, 'backend_python': sys.executable},
                       {'enabled': False, 'pipeline_config_file': '/nonexistent/template.json'}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as tmp:
                ctx = Context(tmp, **config)
                register(ctx)
                self.assertEqual(ctx.hooks, {})
                self.assertEqual(ctx.prompt_sections, {})
                self.assertIn('validate_route_hypotheses', ctx.registered)

    def test_invalid_workflow_configuration_does_not_redirect_native_search(self):
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / 'runner.py'
            runner.write_text('# inert runner fixture\n')
            template = Path(tmp) / 'pipeline.json'
            for text, digest in [('not JSON', sha256(runner.read_bytes()).hexdigest()),
                                 ('[]', sha256(runner.read_bytes()).hexdigest()), ('{}', '0' * 64)]:
                with self.subTest(text=text, digest=digest):
                    template.write_text(text)
                    ctx = Context(tmp, enabled=True, backend_python=sys.executable,
                                  pipeline_runner_file=str(runner), pipeline_config_file=str(template),
                                  pipeline_runner_sha256=digest)
                    register(ctx)
                    self.assertEqual(ctx.hooks, {})
                    self.assertEqual(ctx.prompt_sections, {})

    def test_deferred_direct_submission_is_guarded_without_starting_work(self):
        from retro_specialists.plugin import _default_retrosynthesis_guard
        args = {'calls': [{'name': 'boltzmann_submit', 'arguments': {
            'job_name': 'retrosynthesis', 'experiment_data': {'target_mol': 'CCO'}}}]}
        with patch.dict(sys.modules, {'tools.tool_search': None}), patch('retro_specialists.plugin.bounded_run') as worker:
            result = _default_retrosynthesis_guard(tool_name='tool_call', args=args)
        self.assertEqual(result['action'], 'block')
        worker.assert_not_called()

    def test_disabled_does_not_start_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Context(tmp)
            register(ctx)
            self.assertEqual(set(ctx.registered), {'start_retrosynthesis_workflow', 'retrosynthesis_workflow_status', 'read_retrosynthesis_task', 'inspect_target_stereochemistry', 'search_route_literature', 'validate_route_hypotheses', 'verify_retrosynthesis_result', 'accept_retrosynthesis_result', 'accept_specialist_retrosynthesis_result'})
            with patch('retro_specialists.plugin.bounded_run') as popen:
                result = json.loads(ctx.registered['search_route_literature']['handler']({'query': 'vanillin synthesis'}))
            self.assertEqual(result['status'], 'disabled')
            popen.assert_not_called()
    def test_input_cannot_override_trusted_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable))
            result = json.loads(service.handle('validate_route_hypotheses', {'raw_response': '{}', 'target': 'CCO', 'inventory': ['CC']}))
            self.assertEqual(result['status'], 'invalid_request')
    def test_real_worker_validates_empty_response_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable, backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
            result = json.loads(service.handle('validate_route_hypotheses', {'raw_response': json.dumps({'routes': [], 'limitations': []}), 'target': 'CCO'}))
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['result']['routes'], [])
            self.assertTrue((Path(tmp) / 'receipts' / (result['receipt_id'] + '.json')).is_file())
    def test_cost_cannot_supply_quotes_in_tool_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True))
            result = json.loads(service.handle('compare_route_costs', {'baseline_routes': [], 'candidate_routes': [], 'quotes': {'CC': 1}}))
            self.assertEqual(result['status'], 'invalid_request')
    def test_bad_backend_configuration_fails_without_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python='python3'))
            with patch('retro_specialists.plugin.bounded_run') as popen:
                result = json.loads(service.handle('search_route_literature', {'query': 'vanillin synthesis'}))
            self.assertEqual(result['status'], 'configuration_error')
            popen.assert_not_called()
    def test_literature_query_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True))
            result = json.loads(service.handle('search_route_literature', {'query': 'x' * 2001}))
            self.assertEqual(result['status'], 'invalid_request')

    def test_retired_cost_tool_cannot_start_a_worker(self):
        route = {'route_id': 'r1', 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']}]}
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable, backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
            with patch('retro_specialists.plugin.bounded_run') as worker:
                result = json.loads(service.handle('compare_route_costs', {'baseline_routes': [route], 'candidate_routes': [route]}))
            self.assertEqual(result['status'], 'invalid_request', result)
            worker.assert_not_called()

    def test_timeout_result_is_explicit(self):
        from retro_specialists.plugin_process import WorkerLimitError
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable))
            with patch('retro_specialists.plugin.bounded_run', side_effect=WorkerLimitError('timeout')):
                result = json.loads(service.handle('search_route_literature', {'query': 'vanillin synthesis'}))
            self.assertEqual(result['status'], 'timeout')

    def test_target_inspector_schema_is_bounded_and_has_no_path_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Context(tmp)
            register(ctx)
            tool = ctx.registered['inspect_target_stereochemistry']
            parameters = tool['schema']['parameters']
            self.assertEqual(parameters['required'], ['target'])
            self.assertEqual(set(parameters['properties']), {'target'})
            self.assertEqual(parameters['properties']['target']['maxLength'], 4096)
            self.assertFalse(parameters['additionalProperties'])
            self.assertIn('precursor', tool['description'])

    def test_target_inspection_rejects_shape_and_length_before_worker_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable))
            bad = [{}, {'target': ''}, {'target': 42}, {'target': 'C' * 4097},
                   {'target': 'CCO', 'path': '/private/file'}]
            for args in bad:
                with self.subTest(args=str(args)[:60]), patch('retro_specialists.plugin.bounded_run') as worker:
                    result = json.loads(service.handle('inspect_target_stereochemistry', args))
                    self.assertEqual(result['status'], 'invalid_request')
                    worker.assert_not_called()

    def test_real_target_inspection_returns_backend_ids_without_creating_a_job(self):
        from retro_specialists.graphs import canonical_smiles
        with tempfile.TemporaryDirectory() as tmp:
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable,
                backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
            target = 'C[C@H](O)c1ccccc1'
            result = json.loads(service.handle('inspect_target_stereochemistry', {'target': target}))
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['result']['target'], canonical_smiles(target))
            self.assertEqual(len(result['result']['specified_elements']), 1)
            self.assertFalse(result['result']['chemical_feasibility_established'])
            self.assertFalse((Path(tmp) / 'tasks').exists())
            self.assertFalse((Path(tmp) / 'jobs').exists())
            self.assertTrue((Path(tmp) / 'receipts' / (result['receipt_id'] + '.json')).is_file())

    def test_target_inspection_does_not_interpret_smiles_as_a_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'private.txt'
            source.write_text('PRIVATE_FILE_CONTENT')
            service = PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable,
                backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)]))
            result = json.loads(service.handle('inspect_target_stereochemistry', {'target': str(source)}))
            self.assertEqual(result['status'], 'backend_error')
            self.assertNotIn('PRIVATE_FILE_CONTENT', json.dumps(result))
            self.assertEqual(source.read_text(), 'PRIVATE_FILE_CONTENT')

if __name__ == '__main__':
    unittest.main()
