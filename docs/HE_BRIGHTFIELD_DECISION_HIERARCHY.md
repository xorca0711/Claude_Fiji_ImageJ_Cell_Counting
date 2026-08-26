# H&E brightfield decision hierarchy

> **Status: R1 IMAGE QC APPROVED / H4-H7 DEVELOPMENT.** The 2026-08-12
> four-mouse/eight-section cohort has a reviewer-approved stain/tissue/artifact
> denominator through H3. The fail-closed H7/H8 review-aggregation engineering
> gate is implemented, but the real ordinal, mouse, and multimodal outputs remain
> blocked on a complete accepted review. Route 3 remains disabled.

## 1. Scope

This module adds an orthogonal structural-injury axis to G-SURF. Fluorescence
answers where KRT5 pods and AT1/AT2 states occur; H&E supplies whole-section
architecture, consolidation and inflammatory-cell-rich morphology. H&E must
not be sent through `IF_Quant_Pipeline.groovy`: fluorescence assumes bright
signal on a dark background, whereas H&E is absorbance on a bright background.

The shared contract is sample identity, physical units, provenance and
section-to-mouse aggregation—not a shared measurement engine.

## 2. Data contract verified on the current cohort

Source: `<LOCAL_HE_DATA>`

| property | verified value |
|---|---|
| slides / mice | 4 |
| analytical series per slide | 2: `20x_BF_01`, `20x_BF_02` |
| analytical sections | 8 |
| image type | packed RGB brightfield |
| sampling | approximately 0.274 µm/px |
| biological n | 4 mice, not 8 sections |

The exact mapping is in
`config/studies/g_surf_he_20260812.json`. A future preflight command must reject
a slide rather than guess when the declared series count or identity differs.
The same study contract is the default authority for the exact approved R1 and
development H4 package roots. The H&E tooling performs no run-root discovery or
recency selection; an alternate package is used only through an explicit CLI
override and must still satisfy the locked package hashes.

## 3. Decision hierarchy

```text
H0 modality and analytical-series identity
  -> H1 mouse / slide / section identity
  -> H2 scan and frozen H&E stain-vector QC
  -> H3 usable-tissue and artifact masks
  -> H4 anatomical compartment
  -> H5 candidate injury regions
  -> H6 morphology and topology authorization
  -> H7 blinded review
  -> H8 pooled section-to-mouse aggregation
  -> H9 mouse-level H&E <-> IF association
```

### H0 — modality and series

Accept only a calibrated, non-thumbnail RGB brightfield series whose name is
declared by the study profile. Labels, macro images and slide overviews are not
analytical images. The existing fluorescence WSI rule—exactly one four-channel
series—is intentionally incompatible with this dataset and must stay separate.

### H1 — identity

Resolve `study_id`, `mouse_id`, `slide_id`, `section_id`, genotype and infection
before any measurement. Normalize filename variations such as `m4_1` to the
canonical `M4-1` only through the explicit study profile.

### H2 — stain and scan QC

Set the QuPath image type to Brightfield H&E. Estimate background and stain
vectors on representative reviewed tissue during pilot development; then freeze
one versioned acquisition-batch profile. Record QuPath version, reader, scanner,
objective, pixel calibration, background RGB and both stain vectors.

### H3 — tissue and artifacts

Create separate masks for detected tissue and excluded artifacts. The current
overview inspection identified pen marks, dust/black particles, folds or tears,
fragmented tissue and possible blur/illumination failures. A single opaque
"tissue classifier" is not enough: every excluded area must remain visible in
the QC overlay and denominator reconciliation.

`usable_tissue = detected_tissue - artifact_mask`

### H4 — anatomy

Assign airway, peribronchial, vascular, perivascular, alveolar, pleural or
unresolved geography before interpreting a lesion. Unresolved anatomy is not a
negative result. Airway and vascular boundary lengths must be retained when
cuff area is normalized to the structure it surrounds.

### H5 — candidates

Hematoxylin density, optical density, airspace loss and texture may nominate
candidate inflammatory or consolidated regions. Pilot thresholds are
exploratory and must be stamped as such; control- or reviewer-locked operating
points are required for confirmatory output.

### H6 — morphology authorization

Compartment-compatible morphology and topology authorize a final lesion call.
Examples include continuous peribronchial/perivascular cuffs, alveolar
consolidation, airway epithelial disruption and luminal debris. Candidate
density alone does not establish pathology.

### H7 — blinded review

A locked review set must include every mouse and both sections, balanced across
condition while hiding sample labels. A reviewer accepts, edits or rejects
candidate masks and ordinal scores. Store reviewer identity, timestamp, edit
provenance and original versus final masks.

### H8 — mouse aggregation

Two sections from one slide remain technical samples. For ordinal review
scores, retain both ordered section values plus their observed minimum,
maximum, and exact agreement. Do not average, sum, weight, or otherwise create
a scalar ordinal composite.

For a future quantitative fraction, pool its raw components:

`mouse_fraction = sum(section_numerators) / sum(section_denominators)`

Never average section fractions and never report `n=8`. Report between-section
agreement as QC. A missing review row blocks publication; a completed but
nonreviewable section remains explicitly non-evaluable and is not coerced to
zero. Denominator mismatch or incomplete artifact review continues to block
future quantitative summaries.

The implemented command is:

```powershell
python .\scripts\he_pipeline.py aggregate-review `
  --review-csv <completed-H7_SECTION_PATHOLOGY_REVIEW.csv> `
  --output-root <new-empty-output-path>
```

It validates the locked blinded table in full before using the study map,
eligibility-checks measured schema-v2 `he_pathology` section records at the
`observed_units` estimand, publishes through a staging directory, refuses an
existing destination, and writes the hash audit last. This is an engineering
capability, not evidence that the current biological review has been completed.

### H9 — multimodal association

The first defensible integration is mouse-level: compare H&E injury burden with
confocal KRT5 pod area and AT1/AT2-state measurements. Region- or cell-level
registration remains deferred until section correspondence and registration
accuracy are independently verified.

## 4. Endpoint tiers

The machine-readable definitions are in
`config/brightfield/he_endpoints.json`.

### Tier 1 — quantitative candidates

| endpoint | denominator / reference | interpretation constraint |
|---|---|---|
| usable tissue fraction | detected tissue | acquisition and artifact QC |
| consolidated/dense-lesion fraction | usable tissue | reviewed lesion mask |
| hematoxylin-positive nuclear density | usable tissue mm² | cellularity, not immune lineage |
| inflammatory-hotspot fraction | usable tissue | density geography, not lineage |
| airspace profile-area fraction | alveolar reference area | 2D profile fraction only |
| peribronchial cuff burden | airway boundary length | reviewed airway and cuff boundaries |
| perivascular cuff burden | vascular boundary length | reviewed vessel and cuff boundaries |

### Tier 2 — blinded ordinal pathology

Use a locked 0–4 rubric for alveolitis, bronchiolitis, peribronchial cuffing,
perivascular cuffing, consolidation and epithelial injury/debris. These scores
support interpretation and calibration; they are not silently generated from a
pixel classifier.

### Tier 3 — deliberately deferred

- CD4, CD8, macrophage, neutrophil or other immune-lineage calls from H&E alone.
- Cell-level H&E/IF transfer without validated serial-section registration.
- Alveolar number or volume inferred from simple two-dimensional profiles.
- Fully automated pathology grading without pathologist-labelled development
  and independent validation data.

## 5. Required outputs

```text
he_run_manifest.json
he_section_summary.csv
he_region_summary.csv
he_mouse_summary.csv
he_review_queue.csv
qc/<section>__tissue_artifact_overlay.png
qc/<section>__compartment_overlay.png
qc/<section>__lesion_overlay.png
```

Every summary carries the applicable study, mouse, section, scale/profile,
review-state, and provenance fields. The ordinal review package uses
`he_section_pathology_scores.csv`, `he_mouse_pathology_summary.csv`,
`he_technical_section_agreement.csv`, schema-v2 measurement JSONL, and a final
audit ledger. It is emitted only when the declared blinded section set is
complete and every required review gate passes.

## 6. Validation ladder before Route 3 can be enabled

1. **Preflight:** all four slides resolve to the declared eight analytical
   series and canonical sample identities.
2. **Mask QC:** tissue/artifact denominator reconciliation on every section.
3. **Detection QC:** blinded point-count and nuclear-detection review across
   normal, consolidated, airway, vascular and artifact-rich regions.
4. **Endpoint calibration:** lock operating points using reviewer-labelled
   regions without looking at genotype comparisons.
5. **Technical repeatability:** quantify agreement between BF_01 and BF_02.
6. **Aggregation test:** prove pooled numerators/denominators and fail-closed
   incomplete-section behavior.
7. **Biological restraint:** keep results descriptive because this cohort has
   one mouse in each genotype-by-infection cell.
8. **Launcher gate:** only then set `BrightfieldRouteEnabled=true` and wire an
   actual QuPath H&E runner. Flipping the flag alone must continue to fail.

## 7. Scientific references

- G-SURF research scheme: IFN-gamma KO/PR8, KRT5 pod validation and immune/
  regenerative tracking: internal research plan (not public)
- G-SURF histology-module design: internal histology-module plan (not public)
- QuPath 0.7 H&E stain separation and cell-detection documentation:
  https://qupath.readthedocs.io/en/stable/docs/tutorials/separating_stains.html
  and https://qupath.readthedocs.io/en/stable/docs/tutorials/cell_detection.html
- ATS/ERS standards for quantitative lung structure and unbiased stereology:
  https://pmc.ncbi.nlm.nih.gov/articles/PMC5455840/

## 8. Executed exploratory pilot (2026-08-12)

The H0-H3 engineering pilot is implemented in
`brightfield/qupath_he_exploratory_pilot.groovy` and launched with
`scripts/Invoke-HePilot.ps1`. It ran against all eight declared analytical
series. The reviewed second pass is organized at:

`<LOCAL_RUN_ROOT>\he_20260812\02_pilot_r2_od018`

The run uses downsample 64, a provisional H&E stain matrix, and a tissue OD-sum
threshold of 0.18. Its control-locked dense-hematoxylin candidate threshold is
the maximum section p90 among the two uninfected mice (0.26025390625). All eight
raw previews and all eight H3 overlays were generated. Visual review confirmed
that candidates remain on stained section material and do not spread across the
broad glass background.

This advances the hierarchy only through an **exploratory, review-gated H3
prototype**. Dense hematoxylin is a cellularity candidate, not immune lineage;
the fixed stain vectors, tissue/artifact masks, compartment classifier, lesion
classifier, full-resolution measurements, technical-section repeatability and
mouse aggregation remain unvalidated. The pilot deliberately emits no
mouse-level biological summary.

Validate any organized pilot output with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Test-HePilotOutput.ps1 `
  -OutputRoot <LOCAL_RUN_ROOT>\he_20260812\02_pilot_r2_od018
```
