"""Run the staged specialist workflow with explicit deployment configuration.

JSON configuration is operator-owned. Never supply credentials or molecular
strings through shell interpolation. An existing run directory is not resubmitted.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _read(path):
    path = Path(path)
    if path.stat().st_size > 4_000_000:
        raise ValueError('Configuration or evidence file exceeds size limit')
    return json.loads(path.read_text())


async def execute(args):
    from retro_specialists.pipeline import PipelineConfig, run_pipeline
    from retro_specialists.native_pipeline import NativeSpecialistAdapter, NativeConfig, recover_native_proposal
    from retro_specialists.science_pipeline import ScienceSearch
    config = _read(args.config)
    folder = args.output.resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = _read(config['evidence_file']) if config.get('evidence_file') else []
    quotes = _read(config['quotes_file']) if config.get('quotes_file') else []
    inventory = _read(config['inventory_file'])['molecules'] if config.get('inventory_file') else None
    if config.get('pipeline', {}).get('until_search_finishes'):
        from retro_specialists.pipeline_jobs import continuous_configuration
        config = continuous_configuration(config)
    pipeline_options = config.get('pipeline', {})
    search_timeout = config.get('search_timeout_seconds')
    science_search = ScienceSearch(config_file=Path(config['science_config']),
        run_dir=folder / 'model-search', target=config['target'],
        timeout_seconds=search_timeout,
        action=config.get('search_action', 'submit'), doc_id=config.get('existing_doc_id'),
        max_depth=config.get('pipeline', {}).get('max_depth', 5),
        max_total_reactions=config.get('pipeline', {}).get('max_total_reactions', 20))
    async def search():
        if config.get('baseline_file'):
            from retro_specialists.baseline import parse_baseline
            source = Path(config['baseline_file'])
            public = _read(source)
            parsed = parse_baseline(public, config['target'],
                config.get('pipeline', {}).get('max_depth', 5),
                config.get('pipeline', {}).get('max_total_reactions', 20))
            return {**parsed, 'status': 'completed', 'public_output': public,
                    'baseline_kind': 'explicit_cached_artifact_replay',
                    'artifact_sha256': sha256(source.read_bytes()).hexdigest()}
        return await science_search()
    specialist = NativeSpecialistAdapter(NativeConfig(**config['native']))
    async def dispatch(role, request, receipt_dir):
        if role == 'route_proposer' and config.get('recovered_proposal_from'):
            return recover_native_proposal(Path(config['recovered_proposal_from']), request, specialist.config)
        return await specialist(role, request, receipt_dir)
    async def intermediate_search(leaf):
        key = sha256(leaf.encode()).hexdigest()
        search_leaf = ScienceSearch(config_file=Path(config['science_config']),
            run_dir=folder / 'intermediate-searches' / key, target=leaf,
            timeout_seconds=search_timeout,
            max_depth=config.get('pipeline', {}).get('max_depth', 5),
            max_total_reactions=config.get('pipeline', {}).get('max_total_reactions', 20))
        return await search_leaf()
    manifest = {'started_utc': datetime.now(timezone.utc).isoformat(),
        'target': config['target'], 'name': config['name'],
        'model': config['native']['model'], 'provider': config['native']['provider'],
        'configuration_sha256': sha256(args.config.read_bytes()).hexdigest(),
        'evidence_sha256': sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
        'fresh_model_search': not config.get('baseline_file') and config.get('search_action', 'submit') == 'submit',
        'observing_existing_search': not config.get('baseline_file') and config.get('search_action', 'submit') == 'observe',
        'baseline_artifact_replay': bool(config.get('baseline_file')),
        'fresh_proposer_generation': not bool(config.get('recovered_proposal_from')),
        'fresh_optimization_generation': False,
        'active_roles': ['route_proposer'],
        'verified_proposal_checkpoint_reused': bool(config.get('recovered_proposal_from')),
        'cold_or_warm_loading': 'not_measured',
        'inventory_scope': 'operator_configured' if inventory is not None else 'unknown',
        'literature_scope': 'Bounded available source context and native Europe PMC retrieval; not exhaustive public-data coverage.'}
    (folder / 'benchmark-manifest.json').write_text(json.dumps(manifest, indent=2))
    result = await run_pipeline(config['target'], config['name'], search, dispatch,
        folder / 'pipeline', config=PipelineConfig(**pipeline_options),
        inventory=inventory, evidence=evidence, quotes=quotes,
        cost_assumptions=config.get('cost_assumptions', {}),
        evidence_loader=specialist.load_evidence,
        intermediate_search=(intermediate_search if inventory and
            config.get('pipeline', {}).get('max_intermediate_expansions', 0) > 0 and
            not config.get('baseline_file') else None))
    (folder / 'benchmark-result.json').write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({'state': 'completed', 'output': str(folder),
        'baseline_count': result.get('baseline_count'),
        'additional_unique_graphs': result.get('additional_unique_graphs')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(execute(args))


if __name__ == '__main__':
    main()
