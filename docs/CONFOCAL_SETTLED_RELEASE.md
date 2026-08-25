# Settled confocal cohort and release workflow

> **Status: CURRENT for G-SURF 260808.** This workflow reconciles an accepted
> confocal run. It does not reinterpret reviewed panels or silently change
> quantitative results.

## Authoritative cohort

The reviewed acquisition deck and its exported study configuration define 80
intended fields:

- four mice;
- LEFT and RIGHT marker panels;
- ten reviewed field positions per panel.

Folder discovery is not the cohort definition. The settled run discovered 82
analytical candidates, but two M4-1 candidates were not selected by the reviewed
map. They remain in the engine manifest as audit evidence rather than entering
the canonical denominator.

The checked-in contract is
`config/studies/g_surf_confocal_260808.json`. PowerPoint field roles are
transcribed there as reviewer-authored metadata. They may be used for
stratification and figure organization, but must not be used to tune thresholds
on the same fields.

## Build the settled release

From the repository root:

```powershell
python scripts\build_confocal_settled_release.py
```

The default destination is:

```text
D:\IFQ_Runs\confocal_260809_settled_release
```

The command refuses to reuse an existing directory. It reads, but never modifies:

- the source OIR files;
- `D:\IFQ_Runs\confocal_260809_rerun`;
- the reviewed v1.9.3 JPEG panels;
- the authoritative PowerPoint.

The release contains an 80-row manifest, an 80-row field summary with all engine
columns, 40 LEFT/RIGHT field-order pairs, corrected mouse descriptives, QC
exceptions, endpoint reportability, and a SHA-256 provenance manifest.

## Canonical preflight for a future rerun

A future run may use the release's `canonical_field_manifest.csv` as a strict
allowlist. In the launcher's **Advanced study options**, add:

```text
IFQ_CANONICAL_MANIFEST_PATH=D:\IFQ_Runs\confocal_260809_settled_release\canonical_field_manifest.csv
```

The launcher validates that the file exists before starting Fiji. The engine
then:

1. requires every canonical relative path to be present in the configured input
   scope;
2. analyzes only canonical entries;
3. records other matching microscope files as
   `not_in_canonical_manifest` audit-only skips;
4. copies the exact manifest into the new run output;
5. records canonical counts and the source path in `run_manifest.json`.

This is an optional rerun safeguard. It does not alter the settled 260809 run.

## Aggregation

Direct confocal images are fields, not histological sections:

```powershell
python aggregate_to_mouse.py D:\IFQ_Runs\<run>\analysis\run_summary.csv \
  --sampling-unit field --outdir D:\IFQ_Runs\<run>\stats
```

Use `--sampling-unit section` for slide/WSI inputs. The mouse remains the
biological replicate in both cases.

When a design group has fewer than two mice, SD and SEM are blank because they
are not estimable. The group row is marked `DESCRIPTIVE_ONLY`; a zero is never
substituted for unavailable variability.

## Current exceptions and interpretation boundary

- Canonical quantitative coverage is 80/80.
- M4-2 LEFT field order 6 was repaired after automatic DAPI tissue detection
  failed. The repair used explicit whole-field tissue handling with the original
  engine revision and locked KRT5 threshold 300; retain its QC override flag.
- M4-1 RIGHT field order 7 is quantified but flagged partial/truncated; perform
  an inclusion/exclusion sensitivity check before using a RIGHT-panel summary.
- LEFT/RIGHT pairing is by reviewed field order only. It is not pixel
  registration and cannot support same-cell colocalization.
- All 80 quantified rows have `compartment=unassigned`. Compartment-dependent
  negative calls, coexpression classifications, and the corrected
  dysplastic-over-damaged endpoint remain not reportable.
- With one mouse per genotype-condition design cell, group comparisons are
  descriptive and support no inferential statistics.
