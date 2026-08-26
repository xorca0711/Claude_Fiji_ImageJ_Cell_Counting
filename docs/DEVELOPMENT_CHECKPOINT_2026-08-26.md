# Development checkpoint — 2026-08-26

This checkpoint was created before the local `D:` evidence/run drive was
detached. It records repository state only; it does not copy raw images, run
outputs, review packages, or other confidential artifacts into Git.

## Completed engineering in this checkpoint

- Versioned measurement records and fail-closed aggregation eligibility for
  WSI area, direct confocal, and H&E ordinal review records.
- Content-addressed Stage 1/2 WSI authority: exact VSI/ETS package members,
  Stage 1 and engine scripts, candidate ledger, channel order, parameters,
  runtime identity, reference-space artifacts, and StarDist label evidence.
- Automatic engineering and explicit external tissue-minus-airway reference
  mask routes, with binary/subset/grid and area reconciliation.
- Deterministic Stage 3/4 code-closure and input/output audit records, published
  last after atomic canonical outputs.
- Modern direct-confocal sealed output manifest and manifest-required schema-v2
  production aggregation; legacy descriptive CSV aggregation remains supported.
- Headless StarDist execution with explicit model ZIP, sealed runtime manifest,
  loaded-class origin checks, unsigned-16 label export, and exact output hashes.
- H&E raw/R1/H4 contract reconciliation plus strict blinded review-package and
  audit-last ordinal section/mouse aggregation mechanics.
- Launcher Route 1/2 StarDist authority and direct Stage 2 sharder propagation.
  Route 4 refuses StarDist because its frozen legacy environment cannot carry
  the required authority. H&E biological execution remains disabled.
- Canonical project authority now distinguishes engineering completion from
  scientific promotion; all open gates are validation, calibration, sampling,
  expert-review, or biological-replication checks.

## Verification available at checkpoint time

- WSI/Stage 1/StarDist focused matrix: 78 tests passed.
- Direct-manifest/aggregation focused matrix: 45 tests passed.
- StarDist runtime-focused matrix: 31 tests passed, including Groovy parsing and
  repository-local synthetic/SciJava bridge checks.
- Authority contract: 6 tests passed and generated status current.
- H&E configuration contract passed for 10 stages, 4 mice, 8 sections, and the
  declared BF_01/BF_02 series.
- Complete repository unit suite: 205 tests passed after the checkpoint freeze.
- Launcher three-source C# compilation, 7 repository-local launcher static
  tests, all 18 PowerShell parses, all 19 JSON parses, and `git diff --check`
  passed.

The launcher binary/UI-smoke build and new real-data smokes are intentionally
separated from these repository/source checks. Real Fiji/QuPath replay must wait
until the exact evidence/run drive is reattached.

## Remaining work after resume

1. Run the launcher binary/UI-smoke build against this checkpoint; fix only
   reproducible repository defects.
2. Optionally add the isolated launcher H&E status/build-review/aggregate-review
   screen. It must remain separate from fluorescence Fiji execution and cannot
   enable H5/H6 biological analysis.
3. With `D:` reattached, replay the capped confocal/WSI engineering smokes and
   verify the sealed manifests against the original raw bytes.
4. Complete the external validation gates: comparable confocal tissue ROI,
   reviewed WSI tissue/airway masks and frozen calibration, supported StarDist
   fixture plus blinded nucleus benchmark, completed blinded H&E review, and an
   adequately replicated/prospectively sampled biological study.

## Preservation note

The pre-existing user edit to `scripts/export_vsi_overviews.groovy` is not part
of this checkpoint and must remain unstaged unless the user explicitly asks to
publish it.
