# Retrosynthesis specialist support

This plugin lets Main start a REBOLT search and an independent `route-proposer` for the same target. The specialist uses literature and molecular checks to propose connected synthesis hypotheses; it cannot submit a REBOLT job itself.

## Install and use

Use the Agent Scientist catalog installer for `route-proposer` in a profile that is authorized to use the scientific service and a tool-capable model. Review the specialist's five private tools and the separate permission for Main's combined workflow. A renamed specialist must still be addressed by its installed name. See the [catalog guide](https://github.com/boltzmannlabs/agent-scientist/blob/dev/subagent-catalog/README.md).

Start a new conversation and ask, for example, “Run retrosynthesis for vanillin (`COc1cc(C=O)ccc1O`) and show me the routes.” Main should call `start_retrosynthesis_workflow` and poll `retrosynthesis_workflow_status` using the returned ID. A direct REBOLT submission alone is not a combined run.

- New workflows use the active Main model and provider with the recipient profile's authentication. No second model selection is required.
- REBOLT searches independently. The combined workflow has no elapsed-time budget. It observes the same submitted REBOLT job through pending states while the specialist makes successive proposals. A completed assignment can return an empty result. Verified graphs from completed assignments are retained and deduplicated; the workflow cancels an unfinished assignment when search ends.
- Runtime dependencies include Python, RDKit, jsonschema and the SCI scientific service client. Literature retrieval contacts Europe PMC. Model inference and scientific searches may incur charges; repeated rounds can consume more tokens than a single proposal. An install does not grant service credentials.
- Check the model branch status, proposer status, and `timing.search_proposer_overlap_seconds` in the result. Overlap measures concurrent execution; it does not establish that a route is feasible, cheaper, experimentally validated or in stock.

## Verification receipts

The child reads the exact task, inspects stereochemistry, retrieves literature, validates candidate graphs and verifies its result. A compact receipt refers to the stored result by ID and hash. Main uses the reviewed installation and original task constraints to revalidate it. The child is never granted the owner's scientific submission capabilities.

Keep existing single-assignment or replay benchmarks explicitly identified. Prior results in [VALIDATION.md](VALIDATION.md) describe the version and environment used for those tests. License: [MIT](LICENSE).
