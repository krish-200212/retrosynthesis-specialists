"""Owner-only receipt bridge for reviewed, privately installed specialists.

No model-supplied filesystem locations and no capability scope switching. Molecular
verification stays in the configured isolated backend, including original limits.
"""
import json
from pathlib import Path
import re

_PLUGIN = 'retrosynthesis_specialists'
_SOURCE = 'https://github.com/krish-200212/retrosynthesis-specialists'
_LEAF_TOOLS = frozenset({'read_retrosynthesis_task', 'inspect_target_stereochemistry',
    'search_route_literature', 'validate_route_hypotheses', 'verify_retrosynthesis_result'})


def _contained(root, path):
    """Reject symlinks at every component below the trusted owner root."""
    root, path = Path(root), Path(path)
    current = root
    for component in path.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise ValueError('Portable acceptance paths cannot contain symbolic links')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Portable acceptance path escapes owner profile')
    return path


def _reviewed_home(name):
    from sci_constants import get_sci_home
    from agent.specialist_policy import current_policy
    from agent.specialist_store import load
    from agent.specialist_extensions import extension_home, fingerprint
    from sci_cli.subagent_package_install import definition_digest
    from sci_cli.subagent_package_io import tree, verify, digest
    import sci_yaml
    if current_policy() is not None:
        raise ValueError('Portable acceptance is available only to Main')
    owner = Path(get_sci_home()).resolve()
    item = load(name, home=owner)
    info = item.package_info
    if (not item.enabled or item.needs_review or info.get('state') != 'ready'
            or info.get('id') != 'route-proposer' or info.get('missing')
            or info.get('definition_digest') != definition_digest(item)):
        raise ValueError('Specialist is not an unchanged reviewed route-proposer package')
    if set(item.private_tools) != _LEAF_TOOLS or set(item.tools) != _LEAF_TOOLS or item.children:
        raise ValueError('Specialist capability grants do not match the leaf contract')
    home = extension_home(item, owner=owner)
    if home is None:
        raise ValueError('Specialist has no private installation')
    _contained(owner, home)
    if fingerprint(home) != item.extension_revision:
        raise ValueError('Private capabilities changed since review')
    # Recheck exact already-reviewed bytes; admission belongs to the installer.
    files = tree(_contained(owner, home / 'package'))
    verify(files)
    retained_digest = digest(files)
    if retained_digest != info.get('digest'):
        raise ValueError('Retained package differs from reviewed bytes')
    manifest = sci_yaml.safe_load(files['manifest.yaml'].decode('utf-8-sig'))
    if (retained_digest != info.get('digest') or manifest['id'] != info['id']
            or manifest['version'] != info.get('version')):
        raise ValueError('Retained package does not match reviewed identity')
    return owner, home, manifest


def resolve_specialist_state(name):
    """Resolve the current owner's named package and pinned plugin state."""
    if not isinstance(name, str) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('Invalid saved specialist name')
    from sci_cli.plugins_state import _plugin_data_namespace
    from sci_cli.plugins_manifest import parse_manifest_file
    owner, home, manifest = _reviewed_home(name)
    dependencies = [d for d in manifest['dependencies'] if d.get('id') == 'retrosynthesis-support']
    if len(dependencies) != 1:
        raise ValueError('Expected one reviewed support dependency')
    dependency = dependencies[0]
    if (dependency.get('kind') != 'plugin' or dependency.get('source') != _SOURCE
            or not re.fullmatch('[a-f0-9]{40}', dependency.get('revision', ''))
            or set(dependency.get('tools', [])) != _LEAF_TOOLS):
        raise ValueError('Unexpected support dependency identity')
    metadata_path = _contained(owner, home / 'plugins' / '.install-metadata.json')
    if metadata_path.stat().st_size > 256000:
        raise ValueError('Installation metadata exceeds limit')
    metadata = json.loads(metadata_path.read_text())
    installed = metadata.get(_PLUGIN, {})
    if (installed.get('pinned') is not True or installed.get('source') != _SOURCE
            or installed.get('revision') != dependency['revision']):
        raise ValueError('Installed support dependency does not match package pin')
    plugin = _contained(owner, home / 'plugins' / _PLUGIN)
    if not plugin.is_dir():
        raise ValueError('Support plugin installation is missing')
    plugin_manifest = parse_manifest_file(plugin / 'plugin.yaml', plugin, 'user', '')
    if plugin_manifest is None or plugin_manifest.name != _PLUGIN or plugin_manifest.key != _PLUGIN:
        raise ValueError('Installed support plugin manifest identity mismatch')
    state = _contained(owner, home / 'plugin-data' / _plugin_data_namespace(_PLUGIN, plugin_manifest.skill_namespace))
    _contained(owner, state / 'receipts')
    return state


def acceptance_call(args, configuration):
    """Return an existing strict backend action with host-derived private state."""
    state = resolve_specialist_state(args['specialist'])
    python, timeout, max_bytes, trusted = configuration
    return ('accept_retrosynthesis_result', {k: v for k, v in args.items() if k != 'specialist'},
            (python, timeout, max_bytes, {**trusted, 'state_dir': str(state)}))
