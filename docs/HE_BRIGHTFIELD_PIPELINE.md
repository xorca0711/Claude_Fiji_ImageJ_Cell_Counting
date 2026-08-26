# H&E brightfield pipeline

## Current authorized state

The 2026-08-12 G-SURF H&E cohort is at **R1 / H3**. Image QC is
reviewer-approved for four mice and eight technical sections. The approval
covers stain separation, the lung-section envelope, stained tissue material,
artifact presentation, and the usable-tissue denominator.

It does not authorize automated lesion burden, nuclear density, ordinal
pathology scores, immune lineage, KRT5-pod identification, or mouse-level
hypothesis tests.

Run the fail-closed status audit from the repository root:

```powershell
python .\scripts\he_pipeline.py status
```

By default, both package roots come only from `approved_packages.r1_root` and
`approved_packages.h4_development_root` in the selected `--study` contract. The
current contract points to the verified relocated R1 package under
`D:\IFQ_Runs\H&E_20260812\legacy\pre_mapping_analysis_20260818\10_R1_IMAGE_QC_APPROVED_FINAL`
and H4 package under
`D:\IFQ_Runs\H&E_20260812\legacy\pre_mapping_analysis_20260818\Review\13_H4_SPATIALLY_BALANCED_REGION_REVIEW`.
There is no filesystem discovery, timestamp selection, or fallback to an older
package. For an intentional alternate copy, pass both or either explicit
`--r1-root` and `--h4-root` override; the status output records whether each
effective root came from the study contract or the CLI.

The command validates the raw VSI/ETS inventory, blind-section mapping, locked
stain profile, approved R1 hashes, every approved package file, and the H4
development exports. Any identity, file, or hash mismatch is blocking.

## Analysis hierarchy

| Stage | Decision | Current state |
|---|---|---|
| H0 | RGB brightfield modality and declared analytical series | passed |
| H1 | mouse, slide, section, and blind identity | passed |
| H2 | frozen H&E stain/scan profile | R1 approved |
| H3 | tissue, artifact, and usable-denominator masks | R1 approved |
| H4 | airway, vessel, alveolar, pleural, or unresolved anatomy | development context only |
| H5 | inflammatory-cell-rich and structural-injury candidates | no validated engine |
| H6 | compartment/topology-compatible lesion authorization | unavailable |
| H7 | blinded whole-section pathology review | rubric and aggregation gate ready; review incomplete |
| H8 | technical-section QC and mouse aggregation | engineering ready; blocked on complete accepted review |
| H9 | H&E-to-IF association | blocked; descriptive mouse-level only when available |

Route 3 remains disabled because an approved denominator is not equivalent to
an approved pathology numerator.

## Practical review target

The next review is one row per blinded whole section, not one yes/no anatomy
decision per sampled tile. Score:

1. overall extent of abnormal inflammatory-cell-rich or consolidated tissue;
2. alveolar/interstitial inflammation;
3. peribronchial inflammation;
4. perivascular inflammation;
5. consolidation or airspace loss;
6. airway epithelial injury or luminal debris.

Supporting high-resolution tiles are evidence locators only. They are not
replicates and are not used to estimate prevalence.

Anatomy shorthand:

- **Airway:** circular or branching lumen with a continuous epithelial
  cell-nuclear lining.
- **Alveolar parenchyma:** sponge-like small airspaces separated by thin septa.
- **Vessel:** thin-walled elongated, slit-like, or partly collapsed lumen with an
  endothelial nuclear lining.

## Build the review package

```powershell
python .\scripts\he_pipeline.py build-review
```

`build-review` uses the same contract-root selection and optional explicit
overrides as `status`, and completes the full fail-closed status validation
before copying review inputs.

The command refuses to overwrite an existing package. Its default output is:

```text
D:\IFQ_Runs\H&E_20260812\14_H5_H7_PATHOLOGY_REVIEW_DEVELOPMENT
```

The package contains blinded whole-section references, approved R1 QC context,
high-resolution supporting contact sheets, the locked development rubric, a
single eight-row review form, reportability boundaries, and internal provenance.

## Aggregate an accepted review

After the reviewer has completed all eight locked rows, run:

```powershell
python .\scripts\he_pipeline.py aggregate-review `
  --review-csv <completed-H7_SECTION_PATHOLOGY_REVIEW.csv> `
  --output-root <new-empty-output-path>
```

The command fails before unblinding unless the CSV has the exact ordered
16-column header, every locked blind ID exactly once, complete required cells,
closed rubric vocabularies, and explicit UTC review timestamps. A section
marked `no` or `uncertain` for reviewability must retain `uncertain` lesion
fields; it is never converted to a zero score.

Publication is all-or-nothing and an existing destination is never replaced.
The output package contains:

- `he_section_pathology_scores.csv`: the unblinded section rows;
- `he_mouse_pathology_summary.csv`: one row per mouse and ordinal endpoint,
  retaining the ordered technical-section values, observed minimum/maximum,
  and exact agreement;
- `he_technical_section_agreement.csv`: endpoint-level exact-agreement counts
  across evaluable section pairs;
- `he_measurement_records.schema-v2.jsonl`: explicit `he_pathology` section
  records with accepted review, provenance, and `observed_units` scope;
- `he_review_aggregation.audit.json`: the integrity ledger, written last.

The audit and every schema-v2 record bind the complete Python import closure by
content hash. The software roles are `aggregation_code` (`he_pipeline.py`),
`ifquant_package_init`, `measurement_record_builder`,
`measurement_record_contract`, `measurement_record_route_adapter`, and
`stage2_index_contract`; `python_interpreter` binds the exact executable bytes
used to run them. Record `code_revision` remains the exact `he_pipeline.py`
SHA-256; the other five source hashes make the package imports explicit rather
than silently treating the entry script as the whole program. With the five
data authorities, the audit ledger has exactly 12 roles; each measurement
record adds its `declared_source_package_ledger` for exactly 13 provenance
inputs.

No mean, sum, median, weighted total, or other scalar ordinal composite is
created. The two sections remain technical observations and biological
`n` remains four mice. Input drift before publication or artifact tampering
invalidates the audit; an invalid or incomplete review publishes nothing.

## Endpoint interpretation

H&E supplies whole-section inflammatory and structural context for the settled
confocal result. It can support descriptions of cell-rich infiltration,
consolidation, cuffing, airspace loss, and epithelial injury. It cannot
establish immune-cell lineage or identify a KRT5-positive pod.

After complete blinded review, section scores may be described. Ordinal values
remain ordered categories: the paired-section mouse summary retains each value,
the observed minimum/maximum, and exact agreement without inventing an ordinal
average or composite. Any future quantitative fraction must instead pool its
raw numerator and denominator components across BF_01 and BF_02. Technical
sections never increase biological n. With one mouse in each
genotype-by-infection cell, the present cohort remains descriptive and cannot
support genotype, infection, or interaction inference.
