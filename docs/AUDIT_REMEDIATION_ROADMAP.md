# Audit-driven evolution roadmap

> **Status: PROPOSED.** This document converts the August 2026 technical and
> scientific audit into implementation gates. It does not upgrade the validation
> status of any endpoint or create new study evidence.

## Current evidence boundary

- The settled confocal release contains 80 quantified fields. One field,
  `M4-2_LEFT_F06`, uses a 405,000 um2 whole-field denominator after automatic
  DAPI tissue detection failed. It is complete by row count but not directly
  comparable to the normal auto-DAPI tissue denominators.
- `M4-1_RIGHT_F07` is included with a partial/truncated flag. RIGHT-panel results
  require an explicit inclusion/exclusion sensitivity analysis.
- Infected confocal fields were purposively selected and have no sampling
  probabilities. They describe the measured fields, not whole-section or
  whole-lung prevalence.
- The design is a crossed 2 x 2 with one mouse per cell. Genotype, challenge,
  and their interaction have no replication-based error estimate and are not
  inferentially estimable from this cohort.
- WSI validation covers a six-tile engineering pilot, not a cohort endpoint.
- H&E currently provides image and artifact QC plus review preparation. It does
  not provide a validated lesion, lineage, topology, section, or mouse endpoint.

The generated machine-readable status is
[`generated/AUTHORITY_STATUS.md`](generated/AUTHORITY_STATUS.md). When narrative
documentation and that status differ, resolve the contradiction rather than
silently choosing one.

## Target architecture

All three analytical tracks emit the versioned shared record defined by
[`../schemas/measurement-record.schema.json`](../schemas/measurement-record.schema.json).
Schema conformance establishes identity, provenance, evaluability, and QC only;
it is not scientific validation.

**Implementation state:** the schema and its standard-library validator now
exist, but legacy CSV producers and aggregators do not emit or consume the new
record yet. This is a contract foundation, not a completed migration. Promotion
requires route adapters plus aggregation integration tests; until then the
legacy missing-to-zero and discovery risks remain open.

**2026-08-25 engineering progress:** provenance-safe WSI aggregation is now
implemented around a per-slide, content-addressed Stage 2 run index. The index
binds explicit shard assignments, samplesheets, tile/ROI inputs, engine,
run-manifest configuration, ordered declared channel mappings, manifests,
summaries, and natural
section/region identities. Stage 3 no longer recursively discovers analytical
summaries, and rejected attempts cannot leave an older accepted CSV at the
canonical filename. Synthetic multi-shard, partition, overlap, duplicate,
partial-run, path, and tamper cases fail closed. This closes roadmap step 2 at
the software-contract level only: no local WSI run is promoted, because every
inventoried WSI Stage 1 run is capped (`coverage_complete=false`). The shared
measurement-record adapter and source-metadata channel verification in step 3
remain open. Step 3 is partially implemented in the legacy CSV route:
non-finite and partially missing additive measurements fail, wholly unavailable
panel-specific markers remain blank instead of becoming zero, an emitted blank
marker column is rejected when the indexed panel signature declares that
marker, and Stage 4
requires uniform indexed provenance across every mouse in a panel.

The current index does not yet bind external panel/marker-registry file bytes or
per-image `__params.json` snapshots into the cross-slide measurement profile,
and it does not normalize ImageJ/Bio-Formats/Java versions for cohort-level
comparison. Arbitrary custom acquisition-label aliases also need a structured
marker ID before signature-to-summary evaluability can be enforced beyond the
built-in panel labels. Those are explicit provenance follow-ups before a
custom-panel or scientific WSI cohort is promoted.

| Track | Intended estimand | Required boundary |
|---|---|---|
| `area_wsi` | DAPI-independent whole-section area endpoint | global tissue and airway masks, modality-specific frozen profile, exhaustive coverage, exact pooled numerators and denominators |
| `cell_confocal` | marker-resolved cell and morphology evidence | headless deterministic labels, model/profile hash, classic benchmark, equivalent size/edge/rejection QC, positive/negative/indeterminate states |
| `he_pathology` | reviewed brightfield morphology endpoint | artifact/tissue QC, anatomical compartments, lesion candidates, topology, blinded correction, section-to-mouse aggregation |

The shared contract keeps `mouse_id`, `slide_id`, `section_id`, `field_id`,
`tile_id`, `region_id`, and `cell_id` distinct. A numerical zero is valid only
when `evaluability=measured`; unavailable, inapplicable, excluded, and failed
measurements remain nonnumeric with an explicit reason code.

## Problem-to-remedy map

### 1. Incomplete DAPI nuclei detection

Cause: the historical polarity bug was fixed, but DAPI-derived tissue and
classic particle segmentation remain sensitive to dim nuclei, field boundaries,
and morphology. A visually plausible overlay is traceability evidence, not a
segmentation-accuracy measurement.

Remedy: build blinded, mouse- and context-stratified manual ground truth; run
headless StarDist as a primary candidate; retain classic segmentation as a
benchmark/fallback; apply identical minimum-size, edge, split/merge, and
rejection accounting to both.

Acceptance gate: predeclared precision and recall each at least 0.90 on a held-out
set, absolute count bias no more than 10% in every stratum, deterministic
byte-identical label output on repeat execution, and exact model/profile hashes.

### 2. WSI intensity and denominator validity

Cause: confocal KRT5=300 is not transferable to scanner data; scanner threshold
curves cross near 400; current tissue logic is DAPI-dependent; airways are not
excluded; low-tissue tiles can disappear before the manifest.

Remedy: create DAPI-independent global tissue and airway masks, a frozen
scanner/batch normalization profile, an explicit Stage 2 run index, and a
corrected endpoint implementation that preserves evaluability.

Acceptance gate: every declared tile has a manifest row including zero/low-
tissue tiles; keys are unique; Stage 1 and Stage 2 area differ by at most one
classification pixel or 0.1%, whichever is larger; every input/profile hash
matches; threshold-sensitivity results stay within a predeclared tolerance.

### 2-1. Selected-field statistics versus tissue pathology

Cause: field roles were assigned after review, infected fields were purposively
selected, fields cover only a small fraction of an imaged section, and no
inclusion probabilities exist.

Remedy: preserve the current selected-field result as a descriptive estimand.
For future studies use exhaustive whole-section measurement or a documented
systematic-uniform-random design, record inclusion probabilities, and aggregate
with the mouse as the biological unit.

Acceptance gate: every analyzed field is marked exhaustive or has a nonzero
selection probability and sampling-frame identifier. No whole-section or
whole-lung claim is emitted from purposive or unknown sampling.

### 3. Incomplete H&E module

Cause: the pipeline stops at QC/review preparation; anatomy, lesion candidates,
topology, blinded review, and aggregation are not validated end to end.

Remedy: advance sequentially through artifact/tissue, anatomy, candidate,
topology, review, section, and mouse stages. Each stage consumes the previous
stage's immutable manifest and fails closed on missing or duplicate identities.

Acceptance gate: source and profile hashes verified; 100% declared section
coverage; blinded reviewer decisions complete; held-out performance reported by
mouse; section and mouse numerators/denominators reconcile exactly. Until then,
outputs remain morphology/QC context rather than lesion results.

### 4. Partial marker morphology and biological context

Cause: all 80 released rows have `compartment=unassigned`; non-KRT5 negative and
coexpression states are unevaluable; AGER/T1A thresholds and airway/region masks
are uncalibrated; LEFT/RIGHT fields are nonregistered serial associations.

Remedy: integrate the marker registry with validated compartment masks and
explicit positive/negative/indeterminate states. Validate the corrected
KRT5-positive/PDPN-positive endpoint against manual anatomical outlines. Never
derive same-cell states across LEFT and RIGHT panels.

Acceptance gate: each compound call carries a calibrated marker profile,
compartment assignment, evaluable denominator, and held-out manual comparison.
Unassigned or serial-panel observations can only produce indeterminate/context
outputs.

### 5. Adaptive versus frozen thresholds

Cause: per-image Otsu is deterministic but refits to every image, region, tile,
and modality. Its divergence from a frozen profile is a measurement-definition
change, not random noise.

Remedy: reserve adaptive thresholds for exploratory calibration; freeze the
confirmatory profile before group-label analysis; report the complete plausible
threshold/normalization sensitivity curve.

Acceptance gate: profile derivation uses independent controls, carries a version
and hash, and is immutable during analysis. If direction or magnitude leaves a
predeclared stability interval, report sensitivity rather than a single primary
estimate.

### 6. StarDist as a validated segmentation route

Cause: current StarDist execution is interactive, hardcoded, and lacks model
hashing and QC equivalence; launcher probability/NMS settings are not consumed by
the engine.

Remedy: make StarDist headless and deterministic, wire settings end to end,
record the exact model hash, export labels, and use the same acceptance/rejection
ledger as classic segmentation.

Acceptance gate: configuration round-trips from launcher to manifest and engine;
unsupported settings fail; model/profile hash is mandatory; segmentation gates
from problem 1 pass before StarDist becomes a reportable primary route.

### 7. Inefficient, duplicated, or obsolete processing

Cause: recursive summary discovery permits stale rerun double-counting, resume
checks rely on file existence, channel order is not strictly verified, missing
values can become zero, status documentation is duplicated, and CI lacks image
fixtures.

Remedy: use one authoritative run index; reject duplicate analytical identities;
make resume content-addressed; enforce ordered channel signatures; preserve
evaluability; generate current status from one machine-readable source; add
small end-to-end Fiji/QuPath/StarDist/threshold fixtures when those runtimes are
available in CI.

Acceptance gate: duplicate, missing, overlapping, hash-drifted, channel-mismatched,
or unevaluable synthetic cases all fail closed; a valid sharded run produces one
and only one slide record; generated status is current; fixtures state whether
they validate mechanics or science.

## Implementation order

1. **Authority and contracts (foundation implemented).** Generate current status from one JSON source;
   validate privacy and critical claim states; introduce the shared record schema.
2. **Provenance-safe aggregation (implemented; prospective runs only).** Replace analytical recursive discovery with
   an explicit hashed Stage 2 run index and duplicate-key rejection.
3. **Evaluability and channel order (partially implemented).** Missing-to-zero
   coercion is blocked for additive panel-specific measurements and declared
   channel order is exact. Shared-record adapters, external-config/parameter
   snapshots, runtime-profile comparison, and source-metadata verification are
   still open.
4. **WSI masks and sampling.** Implement global tissue/airway masks and record
   exhaustive/probability sampling semantics.
5. **Segmentation validation.** Implement the headless StarDist/classic benchmark
   against frozen ground truth.
6. **H&E validation ladder.** Complete anatomy through blinded mouse aggregation.
7. **Prospective study.** Use biologically replicated design and prospectively
   account for attrition/survival before testing genotype or interaction effects.

## Scientific blockers versus engineering work

Scientific blockers are biological replication, survivor/attrition handling,
probability or exhaustive sampling, anatomical/airway ground truth, marker and
endpoint calibration, and blinded H&E validation. More software cannot repair
those properties in the current cohort.

Engineering priorities are authoritative state, explicit run indices, duplicate
rejection, content-addressed resume, ordered-channel validation, evaluability
propagation, deterministic model provenance, and executable fixtures. These make
future evidence auditable but do not by themselves make it scientifically valid.
