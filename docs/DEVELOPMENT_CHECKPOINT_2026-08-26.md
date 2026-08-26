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

## Resume completion update

The repository engineering work above was resumed with the evidence drive
attached and completed through the current fail-closed boundary:

- The launcher now has a separate H&E review-only surface for `status`,
  `build-review`, and `aggregate-review`. It does not enable H5/H6 execution or
  route H&E data through the fluorescence Fiji pipeline.
- H&E publication is staged, cancellation-serialized, and Windows-safe. Package
  validation leases deny write/delete/rename while open; the staging lease
  closes before the directory move, the moved package is fully reopened and
  revalidated against still-held input authorities, and only that verified
  final path can be marked committed.
- Each H&E operation uses an isolated CPython 3.10+ probe and binds the exact
  interpreter path, version, and SHA-256. Aggregation audits bind an exact
  12-role authority closure and measurement records bind an exact 13-role
  closure, including the interpreter and imported repository modules.
- The H&E launcher validator independently reconciles the selected blinded
  review CSV, all three published CSVs, all 48 JSONL measurement records,
  deterministic record IDs, section/mouse identities, ordinal vocabulary,
  endpoint counts, and source-package ledgers.
- WSI Stage 1 now writes atomic, fsynced canonical binary mask sidecars. Stage 2
  independently verifies every declared slide's dimensions, byte length,
  binary values, foreground counts, airway subset, and pixelwise
  tissue-minus-airway result before accepting an index.
- The incompatible sidecar contract is versioned explicitly: Stage 1 manifests
  are schema `1.3`; Stage 2 indexes are schema `1.4.0` with the versioned
  `stage2-run-index-1.4.0.schema.json` URI. Older `1.2`/`1.3.0` artifacts fail
  closed.
- Direct confocal preview runs no longer quarantine an existing quantitative
  manifest, Fiji-compatible manifest syncing is used, and the production
  wrapper exits zero only for an exact `complete` and `sealed_complete`
  manifest. Direct aggregation now closes sizes, paths, identities, engine,
  model/runtime roles, skip counts, and Classic-versus-StarDist authority.

### Final repository verification

- Complete repository unit suite: 224 tests passed; one Windows
  privilege-dependent directory-symlink test was skipped because the host
  cannot create that fixture.
- Launcher choke-point check, packaged self-test, real Windows
  validate/dispose/move/reopen regression, and hidden UI smoke all passed.
- Built launcher SHA-256:
  `15e275fa2a27d8dd8ba3e2dae41cf06c8d922592455b84a9d33178e2e23696da`.
- The executable Route 4 compatibility harness passed all 85 checks against
  launcher 1.9.7.0; its checked-in report was refreshed from that run.
- All 18 tracked PowerShell files parsed, all 19 tracked JSON files parsed, the
  generated authority view was current, and repository diff/whitespace checks
  passed.
- A fresh read-only H&E status scan reconciled 4 mice, 8 analytical sections,
  4 VSI plus 16 ETS files, 87 approved R1 files, and 96 H4 candidates. The
  highest authorized state remains R1/H3; H4 review completion remains 0/96,
  H5/H6/H8/H9 remain blocked, and the launcher biological route remains off.

### Real-data engineering replay evidence

- The fixed production-scope direct-confocal wrapper matched 95 inputs and
  correctly failed closed on the first corrupt OIR: 82 intended, 1 processed,
  0 successful, 1 failed, 94 explicitly skipped, and a sealed
  QC-only/incomplete manifest.
- A separate exact-two-input engineering replay processed one readable field
  successfully, explicitly skipped the capped second field, and emitted a
  sealed QC-only/incomplete manifest. Its raw input SHA-256 was
  `3153ff7e60e4116ef06342e7dbc0d1735aaaee194f85355d545a51c8164b53e5`.
- A fresh Stage 1 `1.3` QuPath external-profile dry run opened the exact VSI
  source package,
  selected series 2 (59,465 by 41,119; four channels), reconciled a 3,717 by
  2,570 reference grid with 630 cores and 384 candidates, and independently
  verified the emitted mask sidecars. The source tissue and analysis masks had
  2,954,901 foreground pixels with zero seam difference; the engineering airway
  fixture was empty and remains explicitly unreviewed. The Stage 1 script
  SHA-256 was
  `5b87879569021c1093e372f9e05a2028662029a169a24a12c0acad2c6f1eb897`
  and the resulting manifest SHA-256 was
  `81cb4324bdd1a21736d15c068248f32480232c5c63d6885d469c8df1fcc2520a`.

## Remaining work

Only external evidence and biological authorization gates remain:

1. Resolve the corrupt direct-confocal raw OIR by reviewed exclusion or
   authoritative re-export, establish a comparable tissue ROI, and complete the
   uncapped canonical replay.
2. Obtain expert-reviewed nonempty WSI tissue/airway masks, freeze the biological
   calibration, and complete the uncapped WSI run.
3. Validate the supported StarDist fixture/runtime and complete a blinded
   nucleus benchmark.
4. Complete the blinded H&E whole-section review. The present H4 candidate set
   is development context, not an endpoint.
5. Acquire adequate prospective biological replication before any group-level
   inference or scientific promotion.

No replay or repository test in this checkpoint constitutes biological
validation or scientific promotion.

## Preservation note

The pre-existing user edit to `scripts/export_vsi_overviews.groovy` is not part
of this checkpoint and must remain unstaged unless the user explicitly asks to
publish it.

## Post-checkpoint follow-up (2026-08-26, later the same day)

Engineering completed after this checkpoint was frozen:

- The `repository-validation` workflow now captures every gate's command
  transcript and uploads a per-commit `validation-evidence-<sha>` artifact
  with a machine-readable `validation-receipt.json`;
  [`VALIDATION_EVIDENCE.md`](VALIDATION_EVIDENCE.md) documents the artifact
  and its interpretation boundary.
- Documentation was reconciled with the tree: the legacy-equivalence count
  (85), the branch and tag topology as of 2026-08-26, the launcher version
  lineage without a byte-reproducibility implication, and the uncalibrated
  T1α direction relabelled as an exploratory observation in `README.md` and
  `WORKFLOW.md`.
- `scripts/run_confocal_260808.ps1` and `scripts/run_endpoint_confocal_260808.ps1`
  derive the repository root from `$PSScriptRoot` instead of a hard-coded
  `X:` path.
- Future commits use a GitHub no-reply author address (repository-local
  git configuration).
- CI run 32930355081 (`repository-validation`, commit 56291ee) completed
  successfully on 2026-08-26; it is the last run before the evidence artifact
  existed.
- Re-verification after these changes, on this machine: 224 unit tests passed
  with the one known environment-dependent skip, the generated authority view
  was current, 19 tracked JSON files and 18 tracked PowerShell scripts parsed,
  the H&E configuration contract passed, the launcher built with the
  choke-point scan, embedded self-test, and hidden UI smoke green, and the
  Route 4 harness passed 85 checks with 0 failures.
