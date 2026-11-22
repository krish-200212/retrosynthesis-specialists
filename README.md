# Retrosynthesis specialist support

Agent Scientist plugin supporting one independent `route_proposer` LLM specialist. It provides bounded literature retrieval, molecular/graph/stereochemical checks and host-owned verification receipts. Generated routes remain hypotheses; validation does not establish reaction feasibility, enantioselectivity, availability or cost savings.

This repository contains supporting runtime code, not an installed specialist or a complete scientific-service deployment. Use the Agent Scientist [subagent contribution and installation guide](https://github.com/boltzmannlabs/agent-scientist/blob/main/subagent-catalog/README.md) for reviewed specialist packages. Current software and live evidence, limitations and pending publication gates are in [VALIDATION.md](VALIDATION.md). License: [MIT](LICENSE).

## Recipient setup

1. Provide Python 3.11 or newer with dependencies declared in `pyproject.toml` (RDKit and jsonschema). A separate virtual environment is recommended. Set the plugin's `backend_python` to that interpreter; use trusted `backend_library_paths` only when needed by an isolated interpreter.
2. Install/configure the support plugin in the owner profile and the reviewed specialist's private dependency scope. Set `enabled: true` only after backend setup. Recipient credentials stay in supported provider/profile storage; authenticate in the profile that will execute the model. No fallback to another profile's credentials is promised.
3. Configure an approved tool-capable model/provider explicitly. Literature retrieval uses Europe PMC and needs HTTPS access with certificate verification. Inventory and price files are optional operator-owned inputs; absent records mean unknown stock/cost.
4. Grant the leaf only `read_retrosynthesis_task`, `inspect_target_stereochemistry`, `search_route_literature`, `validate_route_hypotheses`, and `verify_retrosynthesis_result`. It proposes routes itself; these primitives do not generate them.

## Handoff and isolation

Tool proposal inputs accept schema-checked JSON objects or legacy JSON strings. Exact molecular identity, including encoded stereochemistry, remains authoritative. Target names are bounded untrusted context. Depth and total reaction count are distinct constraints.

The optional compact verification handoff returns receipt metadata instead of asking the model to copy a large route envelope. The host resolves stored verified content and checks original target and bounds. Malformed, forged or mismatched receipts are rejected; compact output does not weaken validation.

For an owner-scoped saved specialist, Main uses `accept_retrosynthesis_result`. For a reviewed privately installed specialist, Main uses the owner plugin's `accept_specialist_retrosynthesis_result`, supplying the actual installed specialist name (including a rename), unchanged final envelope and original constraints. The bridge resolves reviewed package state; the model cannot supply a receipt-store path. Installing only a private leaf dependency does not install or configure the owner bridge.

## Optional combined workflow

The shipped operator entry points are `scripts/run_pipeline.py` and `scripts/run_science.py`; their `--help` output describes arguments. They require recipient-owned deployment configuration and scientific-service access. Do not place credentials or model-controlled shell commands into configuration.

Default Main routing is conditional: the plugin must be enabled, and operator-owned workflow configuration plus a trusted runner with matching SHA-256 must be ready. Empty `pipeline_config_file` disables workflow submission. Installing this plugin alone does not make every retrosynthesis call use the specialist. The standalone leaf never submits a scientific search job. The configured combined workflow can run the existing model branch alongside the specialist, but the latest two live fixtures do not benchmark that combined request.

## Release status

The current staged source adds the owner bridge, structured inputs and compact receipt support. These changes are absent from previously published commit `9ab5897118feb851c01c7be40fbfaddba3997f31` until reviewed and published. Do not pin that old commit and assume it contains current staged behavior. Updated reviewed installation and exact merged-dev validation remain pending.
