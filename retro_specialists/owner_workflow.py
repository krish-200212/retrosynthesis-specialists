"""Resolve a reviewed installation and the active conversation at tool dispatch."""
import hashlib
from pathlib import Path

from .pipeline_jobs import _file
from .portable_acceptance import resolve_specialist_state


def current_parent():
    from agent.subagent_lifecycle import get_active_subagent_parent
    return get_active_subagent_parent()


def conversation_route():
    parent = current_parent()
    route = {key: getattr(parent, key, None) for key in ('model', 'provider')}
    if any(not isinstance(value, str) or not value.strip() for value in route.values()):
        raise ValueError("Main's active model and provider are unavailable; start the workflow from a conversation")
    return route


def installation_paths():
    import sci_constants
    from pm.environments import store_root
    source = Path(sci_constants.__file__).resolve().parent
    return source, Path(sci_constants.get_sci_home()).resolve(), Path(store_root(source)).resolve()


def portable_configuration(name, python):
    source, owner, runtime = installation_paths()
    if not (source/'plugins/boltzmann/boltzmann_mcp_server.py').is_file():
        raise ValueError('This SCI install has no Boltzmann retrosynthesis service; configure it before starting a workflow')
    if not runtime.is_dir():
        raise ValueError('The SCI runtime directory is unavailable; repair the installation before starting a workflow')
    state = resolve_specialist_state(name)
    runner = owner/'plugins/retrosynthesis_specialists/scripts/run_pipeline.py'
    runtime_runner = Path(__file__).resolve().parents[1]/'scripts/run_pipeline.py'
    if runner != runtime_runner:
        raise ValueError('Reviewed workflow must run from its owner plugin, outside the child capability scope')
    expected = _file(state.parents[1]/'plugins/retrosynthesis_specialists/scripts/run_pipeline.py', 2_000_000)
    if _file(runner, 2_000_000) != expected:
        raise ValueError('Owner workflow runner differs from the reviewed child dependency')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = {'source_path': str(source), 'sci_home': str(owner), 'runtime_dir': str(runtime)}
    template = {
        'native': {**paths, 'managed_python': str(python), 'specialist_name': name,
                   'state_dir': str(state)},
        'pipeline': {'requested_count': 1, 'max_depth': 3, 'max_total_reactions': 3,
                     'selection_limit': 3, 'max_intermediate_expansions': 0, 'until_search_finishes': True},
    }
    return {'pipeline_runner_file': str(runner), 'pipeline_runner_sha256': hashlib.sha256(expected).hexdigest(),
            'portable_template': template, 'portable_science': {**paths, 'server_python': str(python)}}
