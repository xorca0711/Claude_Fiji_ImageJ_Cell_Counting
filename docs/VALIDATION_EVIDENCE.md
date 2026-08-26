# Validation evidence

> **Status: CURRENT.** Describes the machine-readable evidence trail produced
> by the `repository-validation` CI workflow: what each run verifies, what the
> downloadable evidence artifact contains, and what that evidence does and does
> not establish.

## What every CI run verifies

Every push and pull request runs
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) on a Windows runner.
The gates, in order:

1. The generated project authority view is current
   (`scripts/render_authority_status.py --check`).
2. The complete Python regression suite passes
   (`python -m unittest discover -s tests`).
3. Every tracked JSON file parses.
4. The H&E hierarchy and study contract hold
   (`scripts/Test-HeConfiguration.ps1`).
5. Every tracked PowerShell script parses.
6. The launcher builds; its choke-point scan, embedded self-test, and hidden
   UI smoke test all pass, and the binary is discarded on any failure
   (`launcher/build.ps1`).
7. The Route 4 legacy equivalence harness passes
   (`launcher/run_legacy_equivalence.ps1`).
8. The tracked tree is clean after everything above ran, so no gate silently
   modified a tracked file.

## The evidence artifact

Each run uploads a GitHub Actions artifact named
`validation-evidence-<commit sha>` containing:

- a per-gate record for gates 1 through 7: `authority-check.txt`,
  `python-unittest.txt`, `json-parse.txt`, `he-configuration.txt`,
  `powershell-parse.txt`, `launcher-build.txt`, and `legacy-equivalence.txt`
  (the last is the live stdout of the Route 4 harness for that run). Five of
  these are the teed live output of the gate's command; the two parse gates
  write their failure listing on failure and a one-line pass summary on
  success. The final clean-tree gate is recorded only by the job status;
- a copy of the committed Route 4 snapshot
  (`legacy_equivalence_report.txt`) so the artifact carries the report the
  documentation cites next to the live transcript;
- the built launcher's SHA-256 sidecar (`launcher.sha256.txt`);
- `validation-receipt.json` (schema 1.0.0) recording the repository, commit,
  run id and attempt, workflow name, job status, UTC timestamp, runner OS,
  Python version, the built launcher's file name and SHA-256, and a
  per-transcript presence table.

The receipt and upload steps run with `if: always()`, so a failing run still
leaves a receipt with the per-file presence table; gates 1 through 7 write
their file whether they pass or fail, so an absent file means that gate never
reached execution. Nothing in the artifact is hand-written: each file is the
gate's own output or listing, the receipt assembled by the workflow, or a copy
of a committed or built artefact.

To download the artifact, open the run under the repository's Actions tab, or:

```bash
gh run download <run id> --repo xorca0711/IFQuant-Lung --name validation-evidence-<commit sha>
```

GitHub's artifact retention window applies (90 days by default). The durable
records are the committed documents and the checked-in Route 4 snapshot, not
the artifact.

## Interpretation boundary

The receipt embeds its own boundary statement:
`Repository and engineering verification only; not biological validation or
scientific promotion.`

A green run establishes that the tracked tree at that commit passed the
engineering gates listed above. It does not calibrate any threshold, authorize
any biological claim, or promote any H&E stage. Claim status lives in the root
[`README.md`](../README.md) claims table and
[`generated/AUTHORITY_STATUS.md`](generated/AUTHORITY_STATUS.md).

## Evidence for commits before the artifact existed

The artifact is uploaded by runs at or after the commit that introduced it.
For earlier commits the evidence is the committed record:
[`DEVELOPMENT_CHECKPOINT_2026-08-26.md`](DEVELOPMENT_CHECKPOINT_2026-08-26.md)
records the 2026-08-26 verification (224 tests passed, the 85-check Route 4
harness against launcher 1.9.7.0, CI run 32930355081), and
[`../launcher/legacy_equivalence_report.txt`](../launcher/legacy_equivalence_report.txt)
is the checked-in Route 4 transcript from that verification.
