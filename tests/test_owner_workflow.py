import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from retro_specialists import pipeline_jobs
from retro_specialists import owner_workflow
from retro_specialists.plugin import PrimitiveTools, register
from tests.test_plugin import Context


def test_unconfigured_reviewed_owner_guards_direct_search(tmp_path):
    ctx = Context(tmp_path, enabled=True, workflow_specialist_name='local-proposer')
    register(ctx)
    blocked = ctx.hooks['pre_tool_call'](tool_name='boltzmann_submit', args={'job_name': 'retrosynthesis'})
    assert blocked['action'] == 'block'
    assert 'start_retrosynthesis_workflow' in blocked['message']
    response = json.loads(PrimitiveTools(ctx).handle('start_retrosynthesis_workflow', {'target': 'CCO', 'name': 'ethanol'}))
    assert response['status'] == 'configuration_error'
    assert 'backend_python' in response['error']


def test_admission_uses_active_parent_even_after_switch(tmp_path):
    service = PrimitiveTools(Context(tmp_path, enabled=True, backend_python=sys.executable))
    capture = Mock(return_value={'status': 'completed'})
    parent = SimpleNamespace(model='codex', provider='boltzmann_models')
    with patch.object(service, '_execute', capture), patch('retro_specialists.owner_workflow.current_parent', return_value=parent):
        for model, provider in [('codex', 'boltzmann_models'), ('qwen', 'other')]:
            parent.model, parent.provider = model, provider
            service.handle('start_retrosynthesis_workflow', {'target': 'CCO', 'name': 'ethanol'})
            trusted = capture.call_args.args[2][-1]
            assert trusted['main_route'] == {'model': model, 'provider': provider}
    with patch.object(service, '_execute', capture), patch('retro_specialists.owner_workflow.current_parent', return_value=None):
        result = json.loads(service.handle('start_retrosynthesis_workflow', {'target': 'CCO', 'name': 'ethanol'}))
        assert result['status'] == 'configuration_error'
        assert capture.call_count == 2


def test_job_uses_per_run_science_config_and_no_template_model(tmp_path):
    runner = tmp_path/'run.py'
    runner.write_text('')
    import hashlib
    old = {'model': 'DO-NOT-USE', 'provider': 'previous'}
    template = {'native': {**old, 'specialist_name':'renamed', 'timeout_seconds':360},
                'pipeline': {'search_timeout_seconds':1250,'proposal_timeout_seconds':370},
                'search_timeout_seconds':1200}
    science = {'sci_home': str(tmp_path/'home')}
    route = {'provider':'parent-provider', 'model':'parent-model'}
    trusted = {'portable_template':template,'portable_science':science,'main_route':route,
               'pipeline_runner_file':str(runner),'pipeline_runner_sha256':hashlib.sha256(b'').hexdigest(),
               'state_dir':str(tmp_path),'library_paths':[]}
    with patch.object(pipeline_jobs.subprocess, 'Popen') as launch:
        launch.return_value.pid = 12345
        opened = pipeline_jobs.start_workflow('CCO', 'test', trusted)
    config_file = tmp_path/'workflow-jobs'/opened['job_id']/'config.json'
    config = json.loads(config_file.read_text())
    assert config['native'] == {**route, 'specialist_name':'renamed'}
    assert config['pipeline'] == {'until_search_finishes': True}
    assert 'search_timeout_seconds' not in config
    assert config['science_config'] == str(config_file.with_name('science.json'))
    assert json.loads(config_file.with_name('science.json').read_text()) == science
    assert template['native']['model'] == 'DO-NOT-USE'
    assert 'credentials' not in config


def test_renamed_install_resolves_private_paths_and_pinned_runner(tmp_path, monkeypatch):
    owner = tmp_path/'profile'; source = tmp_path/'source'; runtime = tmp_path/'runtime'
    server = source/'plugins/boltzmann/boltzmann_mcp_server.py'
    server.parent.mkdir(parents=True); server.touch(); runtime.mkdir()
    state = owner/'subagent-data/private/plugin-data/namespace'
    owner_plugin = owner/'plugins/retrosynthesis_specialists'
    relative_runner = 'scripts/run_pipeline.py'
    for path in (owner_plugin/relative_runner, state.parents[1]/'plugins/retrosynthesis_specialists'/relative_runner):
        path.parent.mkdir(parents=True); path.write_text('pinned fixture')
    monkeypatch.setattr(owner_workflow, '__file__', str(owner_plugin/'retro_specialists/owner_workflow.py'))
    monkeypatch.setattr(owner_workflow, 'installation_paths', lambda:(source,owner,runtime))
    resolver = Mock(return_value=state)
    monkeypatch.setattr(owner_workflow, 'resolve_specialist_state', resolver)
    value = owner_workflow.portable_configuration('renamed', sys.executable)
    resolver.assert_called_once_with('renamed')
    native = value['portable_template']['native']
    assert native['specialist_name']=='renamed' and native['sci_home']==str(owner)
    assert 'provider' not in native and 'model' not in native
    assert value['portable_template']['pipeline']['until_search_finishes'] is True
    assert 'timeout_seconds' not in native
    assert 'search_timeout_seconds' not in value['portable_template']
    assert not any('timeout' in key for key in value['portable_template']['pipeline'])
    assert state.is_dir()
    (owner_plugin/relative_runner).write_text('changed')
    with pytest.raises(ValueError, match='differs'):
        owner_workflow.portable_configuration('renamed', sys.executable)
    server.unlink()
    with pytest.raises(ValueError, match='service'):
        owner_workflow.portable_configuration('renamed', sys.executable)

