# Validation status — 2026-11-21

**Current staged source has successful software checks and two live accepted hypotheses; release integration remains partial.** This evidence does not establish experimentally correct chemistry or production readiness.

## Software checks

- Current local source: 616 backend tests plus 278 subtests and 8 native tests passed (624 tests total); 90.63% statement coverage, 3548/3915 statements. Backend/native test times 42.74/28.00 seconds.
- Current remote staged backend: 482 tests passed in 32.6 seconds using the canonical runner; RDKit 2025.9.6. This is a different suite scope; counts are not additive.
- Native fake-provider tests exercised Main delegation to a renamed private child and separate owner receipt acceptance. Deterministic results are separate from live model evidence.
- Earlier official fresh-profile installation and backend checks passed with local upstream URL-validation/version-detection fixes. That installation used old published 9ab5897, not all current staged changes.

## Fresh remote live fixtures

Both used `glm-5.3` through `boltzmann_models`, existing authorized remote authentication and a temporary private test specialist. No credentials are distributed. Each requested one route, maximum depth 3 and total reactions 3.

| Fixture | Outer/native seconds | Calls | Input/output tokens | Strictly accepted hypotheses |
|---|---:|---:|---:|---:|
|2-phenylethanol|116.18/111.38|6|73,793/5,010|1|
|(R)-1-phenylethanol|233.02/228.31|8|102,943/8,506|1|

Both final handoffs were 160-byte compact receipts. Host acceptance resolved the stored hypotheses and enforced original constraints. Monetary cost is unknown.

The achiral hypothesis has abstract-level substrate support for its final transformation; the preceding step and entire sequence remain untested. The stereo hypothesis has analogue-only catalyst-class evidence; exact-substrate selectivity and catalyst-enantiomer-to-product mapping are unknown. Graph/stereo declarations and receipt authenticity do not prove those claims.

Earlier attempts failed through authorization, timeout, JSON syntax, malformed final handoffs and a chemistry error in a structurally accepted graph. Those failures were preserved. The successful achiral trial changed model; the stereo trial also changed source/prompt/concurrency conditions. No controlled speedup, chemical superiority, synthesis cost reduction or fresh combined model-search benchmark is claimed.

## Remaining gates

Publish the reviewed current runtime and update the specialist dependency revision; old 9ab5897 does not include these changes. Repeat a clean reviewed-package installation and live renamed private-child handoff using that exact published revision. Perform exact merged-dev validation. Independently assess route chemistry before treating hypotheses as experimentally executable syntheses. The current successful live tests used staged source, not the final published package.

The supporting plugin release does not bundle private transcripts, credentials or scientific-service datasets. Exact artifacts and source/result hashes are retained in the development workspace for review; their absence from this source-only release must not be represented as a fresh reproduced benchmark.
