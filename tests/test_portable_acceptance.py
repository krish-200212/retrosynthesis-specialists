"""Owner-side portable acceptance is name-scoped, never path-scoped."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from tests.test_plugin import Context
from tests import test_native_acceptance as acceptance_fixture
from retro_specialists.plugin import PrimitiveTools, register


def test_main_registered_bridge_accepts_renamed_private_receipt(tmp_path):
    child = tmp_path / 'child'
    child.mkdir()
    _, envelope = acceptance_fixture.AcceptanceTests().run_case(child)
    owner = Context(tmp_path / 'owner', enabled=True, backend_python=sys.executable,
        backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)])
    register(owner)
    args = dict(specialist='renamed-proposer', final_envelope=json.dumps(envelope),
                target='CCO', requested_count=5, max_depth=5, max_total_reactions=20)
    with patch('retro_specialists.portable_acceptance.resolve_specialist_state', return_value=child):
        answer = json.loads(owner.registered['accept_specialist_retrosynthesis_result']['handler'](args))
    assert answer['result']['status'] == 'verified_hypotheses'
    other = tmp_path / 'other'; other.mkdir()
    with patch('retro_specialists.portable_acceptance.resolve_specialist_state', return_value=other):
        wrong = json.loads(owner.registered['accept_specialist_retrosynthesis_result']['handler'](args))
    assert wrong['result']['status'] == 'quarantined'


@pytest.mark.parametrize('extra', [{'specialist':'../other'}, {'specialist':'Name'},
    {'specialist':'valid', 'state_dir':'/tmp'}, {'specialist':'valid', 'owner':'/tmp'}])
def test_paths_and_bad_names_fail_before_resolution(tmp_path, extra):
    ctx = Context(tmp_path, enabled=True, backend_python=sys.executable)
    args = dict(final_envelope='{}', target='CCO', requested_count=5, max_depth=5, max_total_reactions=20, **extra)
    result = json.loads(PrimitiveTools(ctx).handle('accept_specialist_retrosynthesis_result', args))
    assert result['status'] == 'invalid_request'


def _state_fixture(tmp_path):
    from types import ModuleType, SimpleNamespace
    from retro_specialists.portable_acceptance import _SOURCE, _LEAF_TOOLS
    namespace = ModuleType('sci_cli.plugins_state')
    namespace._plugin_data_namespace = lambda name, _: 'namespace-' + name
    namespace.parse_manifest_file = lambda *_: SimpleNamespace(name='retrosynthesis_specialists', key='retrosynthesis_specialists', skill_namespace='')
    manifest = {'dependencies': [{'id': 'retrosynthesis-support', 'kind': 'plugin',
        'source': _SOURCE, 'revision': 'a' * 40, 'tools': list(_LEAF_TOOLS)}]}
    home = tmp_path / 'subagent-data' / ('a' * 32)
    plugins = home / 'plugins'; plugins.mkdir(parents=True)
    (plugins / 'retrosynthesis_specialists').mkdir()
    metadata = {'retrosynthesis_specialists': {'pinned': True, 'source': _SOURCE, 'revision': 'a' * 40}}
    (plugins / '.install-metadata.json').write_text(json.dumps(metadata))
    return namespace, home, manifest


def test_resolver_checks_dependency_pin_and_state_path(tmp_path):
    from retro_specialists.portable_acceptance import resolve_specialist_state
    namespace, home, manifest = _state_fixture(tmp_path)
    with patch.dict(sys.modules, {'sci_cli.plugins_state': namespace, 'sci_cli.plugins_manifest': namespace}), patch(
            'retro_specialists.portable_acceptance._reviewed_home', return_value=(tmp_path, home, manifest)):
        state = resolve_specialist_state('renamed-child')
        assert state == home / 'plugin-data' / 'namespace-retrosynthesis_specialists'
        manifest['dependencies'][0]['revision'] = 'b' * 40
        with pytest.raises(ValueError, match='pin'):
            resolve_specialist_state('renamed-child')
        manifest['dependencies'][0]['revision'] = 'a' * 40
        (home / 'plugin-data').symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(ValueError, match='symbolic'):
            resolve_specialist_state('renamed-child')


@pytest.mark.parametrize('field,value', [('source', 'https://example.org/untrusted'),
    ('kind', 'mcp'), ('revision', 'main'), ('tools', [])])
def test_resolver_rejects_other_dependency_identity(tmp_path, field, value):
    from retro_specialists.portable_acceptance import resolve_specialist_state
    namespace, home, manifest = _state_fixture(tmp_path)
    manifest['dependencies'][0][field] = value
    with patch.dict(sys.modules, {'sci_cli.plugins_state': namespace, 'sci_cli.plugins_manifest': namespace}), patch(
            'retro_specialists.portable_acceptance._reviewed_home', return_value=(tmp_path, home, manifest)):
        with pytest.raises(ValueError, match='identity'):
            resolve_specialist_state('renamed-child')


@pytest.mark.parametrize('bad', ['policy', 'disabled', 'review', 'state', 'package_id',
    'digest', 'grants', 'children', 'home', 'fingerprint', 'retained_digest'])
def test_reviewed_owner_boundary_rejects_modified_definitions(tmp_path, bad):
    from types import ModuleType, SimpleNamespace
    from retro_specialists.portable_acceptance import _reviewed_home, _LEAF_TOOLS
    item = SimpleNamespace(enabled=True, needs_review=False, private_tools=_LEAF_TOOLS,
        tools=_LEAF_TOOLS, children=(), extension_revision='fingerprint',
        package_info={'state':'ready', 'id':'route-proposer', 'version':'0.6.0',
                      'definition_digest':'definition', 'digest':'retained', 'missing':[]})
    if bad == 'disabled': item.enabled = False
    if bad == 'review': item.needs_review = True
    if bad == 'state': item.package_info['state'] = 'setup_required'
    if bad == 'package_id': item.package_info['id'] = 'other'
    if bad == 'digest': item.package_info['definition_digest'] = 'forged'
    if bad == 'grants': item.tools = (*_LEAF_TOOLS, 'shell')
    if bad == 'children': item.children = ('unreviewed',)
    module = ModuleType('fake_framework')
    module.get_sci_home = lambda: tmp_path
    module.current_policy = lambda: object() if bad == 'policy' else None
    module.load = lambda name, home: item
    module.definition_digest = lambda item: 'definition'
    module.extension_home = lambda item, owner: None if bad == 'home' else tmp_path / 'subagent-data' / ('a' * 32)
    module.fingerprint = lambda home: 'changed' if bad == 'fingerprint' else 'fingerprint'
    module.tree = lambda path: {'manifest.yaml': b'{}'}
    module.verify = lambda files: None
    module.digest = lambda files: 'changed' if bad == 'retained_digest' else 'retained'
    module.safe_load = lambda text: {'id':'route-proposer','version':'0.6.0'}
    names = ['sci_constants', 'agent.specialist_policy', 'agent.specialist_store',
             'agent.specialist_extensions', 'sci_cli.subagent_package_install', 'sci_cli.subagent_package_io', 'sci_yaml']
    with patch.dict(sys.modules, {name:module for name in names}):
        with pytest.raises(ValueError):
            _reviewed_home('renamed')
