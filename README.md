# Retrosynthesis specialists support plugin

This Agent Scientist plugin provides molecular graph validation, target stereochemistry inspection, bounded Europe PMC literature retrieval, immutable task context and evidence receipts for the standalone `route-proposer` specialist. Its optional host workflow runs an existing model-search service and the proposer concurrently. It does not establish experimental chemical feasibility or alter public model-search route objects.

The portable specialist package is installed separately through Agent Scientist's reviewed subagent package installer. This repository contains the supporting plugin, not a saved user profile.

## Installation and dependencies

Use Agent Scientist's reviewed plugin installation flow with this repository and a pinned commit. Installation must review plugin host access. Configure the plugin in the owning profile before enabling it. A recipient-selected Python interpreter with Python 3.11+, RDKit and jsonschema is required; see `pyproject.toml`. Development tests additionally require pytest. These dependencies are not installed by plugin discovery.

Set `backend_python` to that interpreter's absolute path, `enabled` to true, and optionally `backend_library_paths` to trusted package directories. The worker starts isolated Python; no model-controlled executable paths or shell commands are accepted. Review the full settings in `plugin.yaml`. Inventory, quotes and baseline inputs are optional operator-owned JSON files; missing inputs mean unknown availability/cost rather than a successful catalog check.

The independently installed leaf requires `read_retrosynthesis_task`, `inspect_target_stereochemistry`, `search_route_literature`, `validate_route_hypotheses` and `verify_retrosynthesis_result`. Main uses `accept_retrosynthesis_result` to validate the child's unchanged envelope and original constraints.

## Optional concurrent workflow

`start_retrosynthesis_workflow` and `retrosynthesis_workflow_status` require a recipient-owned pipeline template, trusted `scripts/run_pipeline.py`, and its SHA-256 configured as `pipeline_config_file`, `pipeline_runner_file`, and `pipeline_runner_sha256`. The template configures the scientific MCP endpoint and its managed interpreter, source checkout, owning SCI profile, provider/model, private output paths and limits. See `NativeConfig` in `retro_specialists/native_pipeline.py` and `scripts/run_pipeline.py` for the explicit configuration contract. There is no portable executable path hardcoded in this repository.

This workflow additionally requires Agent Scientist and the MCP Python SDK in the configured managed runtime. It uses the public Boltzmann submit/status/download tools under recipient-owned authorization. The model ensembles are external services and are not bundled here. Never copy another person's credentials, authorization receipts, inventory or experimental data into setup.

Pending scientific submissions are not automatically repeated. Local cancellation does not establish remote cancellation. Target-level admission retains uncertain jobs to avoid double submissions. The default-routing policy and pre-tool hook register only when the plugin is enabled and local workflow admission checks pass (configured backend, owned template, pinned runner digest). Standalone leaf installations therefore do not redirect native scientific submissions. This checks local setup, not remote availability. Configuration changes require a new SCI conversation.

## Data, permissions and costs

Plugins execute host Python with the installation's privileges. Molecular targets, route hypotheses, assigned instructions and selected source excerpts can be sent to the configured LLM provider. Literature queries contact Europe PMC; DOI identifiers link to their recorded sources. Model-based search sends molecular inputs to the operator-configured scientific service. Model and scientific-service usage may incur separate charges. Unknown usage and cost stay unknown. No keys or signed artifact URLs are included in the source.

Private task/receipt/job artifacts are written in the owning profile's plugin state directory. They are research records and are not release resources. Recipient configuration must use native credential setup; credentials are never constructed from molecular input.

## Development tests and scope

Run `python3 -m pytest tests -q`. The included tests exercise contracts, topology, canonicalization, stereochemical declarations, source binding, bounded provider/process behavior, workflow failure isolation and fake model/search overlap. They do not use paid scientific jobs or demonstrate improved experimental chemistry.

Tests for the old workspace-specific installer, archived cost experiments and local report generation are excluded because those utilities are not part of this support-plugin release. Runtime route-repair/cost-reducer roles are absent. Utility code for legacy arithmetic or parsing remains where current assessment paths import it.

Live model inference, service availability, fresh-profile installation and Main-to-specialist execution require separate evidence. A schema check or passing mocked test does not establish any of those outcomes. See `VALIDATION.md` for this staged source's observed test results.

## Attribution and license

Copyright 2026 Gopi Krishna. MIT license, matching the existing specialist project's declared license. This repository packages the authored support implementation and synthetic tests. It includes no article full text, published reaction datasets, model weights, installed environments or third-party runtime binaries. The MCP transport implementation notes its design origin in an earlier local prototype; it imports none of that prototype.
