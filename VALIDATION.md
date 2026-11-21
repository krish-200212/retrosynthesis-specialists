# Validation status

Release staging prepared on 2026-11-21 from support-plugin version 0.6.0.

- Source scan before publication: no contributor home paths, private server address, token-shaped secrets, credential assignments or symlinks found in staged source. This pattern scan is limited; it is not a comprehensive security audit.
- Routing regression tests: `python3 -m pytest tests/test_plugin.py -q --disable-warnings` passed with 16 tests and 15 subtests in the source workspace after the readiness fix. The identical files were copied into staging and the staging targeted run also passed.
- A sandboxed staging full-suite invocation stopped making progress after approximately 99 cases; no full-suite pass is claimed here. The full suite requires a subsequent completed run in a runtime that supports its subprocess/asyncio tests.
- Previous model-account rejection and scientific-service observation failures remain live-integration limitations. A short non-Codex provider request completed, but that alone is not a successful full route generation or portable-install result.
- Clean-profile reviewed package installation, actual tool-using route generation, two fixture outcomes, public plugin source installation and exact merged-dev testing need their own recorded evidence. Local component results do not establish these.

This repository deliberately includes no private run logs or credentials. Final contribution evidence belongs to the portable specialist package's validation record after actual installed-package tests.
