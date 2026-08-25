# Documentation index

> **Status: CURRENT.** Every document below carries a status banner at its top.
> The vocabulary is fixed and is used literally:
>
> | label | means |
> |---|---|
> | **VALIDATED** | a check was run against real data and its result is recorded in the document |
> | **PARTLY VALIDATED** | the mechanism was checked; the science it supports was not |
> | **PROPOSED** | designed and written down; not executed, or not checked against data |
> | **REFERENCE** | policy, vocabulary or interpretation rules — describes how the engine decides, not what it found |
> | **SUPERSEDED** | kept because it is the only record of work that was done; **do not build on it** |
>
> "Validated" never means "looks right". If a document says validated, the check
> and its number are in the document.

**Read the generated [`AUTHORITY_STATUS.md`](generated/AUTHORITY_STATUS.md)
first.** It is rendered from the machine-readable project authority. Resolve
any contradiction with narrative documents explicitly; no prose file silently
overrules the authority record.

---

## What this project is

Lung immunofluorescence quantification for an IFN-γ KO + PR8 influenza study,
across two acquisition routes measured by the **same** engine:

* **field / confocal route** — small calibrated images go straight to
  `IF_Quant_Pipeline.groovy`. Described in [`../WORKFLOW.md`](../WORKFLOW.md).
  This is the route the current data came through.
* **whole-slide route** — a fluorescence slide-scanner `.vsi` is opened and tiled by QuPath,
  then the same engine measures the tiles. Described in
  [`WSI_TILING_WORKFLOW.md`](WSI_TILING_WORKFLOW.md).
* **H&E brightfield route** — designed as a separate QuPath measurement module
  sharing identity and mouse aggregation, but deliberately disabled until its
  masks and endpoints are calibrated. See
  [`HE_BRIGHTFIELD_DECISION_HIERARCHY.md`](HE_BRIGHTFIELD_DECISION_HIERARCHY.md).

For fluorescence, QuPath never measures anything; it is a reader and tiler, and
all quantification stays in the Fiji engine so both fluorescence routes share
one validated decision model. The proposed H&E route is explicitly different:
its brightfield measurements belong in a separate QuPath module.

## The five-minute path

| # | read | why |
|---|---|---|
| 1 | [`PROJECT_STATE.md`](PROJECT_STATE.md) §0–§3 | what exists, what is calibrated, what the numbers are, and the one-token bug that nearly ate them |
| 2 | [`NEGATIVE_RESULTS.md`](NEGATIVE_RESULTS.md) | two markers tested and rejected, with the measurement that killed each |
| 3 | [`QUPATH_FIJI_INTEGRATION.md`](QUPATH_FIJI_INTEGRATION.md) | why two tools, and why the handoff is files |
| 4 | [`ECTOPIC_POD_ENDPOINT.md`](ECTOPIC_POD_ENDPOINT.md) banner + §4c | how an operating point was locked from controls only — and why the endpoint it served was still wrong |

---

## Every document, with its status

### Project state and findings

| document | status | what it is |
|---|---|---|
| [`generated/AUTHORITY_STATUS.md`](generated/AUTHORITY_STATUS.md) | **GENERATED CURRENT STATUS** | deterministic rendering of the machine-readable claim and route authority |
| [`AUDIT_REMEDIATION_ROADMAP.md`](AUDIT_REMEDIATION_ROADMAP.md) | **PROPOSED** | three-track architecture, problem-to-remedy map, and measurable acceptance gates |
| [`PROJECT_STATE.md`](PROJECT_STATE.md) | **NARRATIVE HANDOFF** | historical locations, provisional operating point, corrected endpoint, open debt, and decisions; subordinate to the generated authority |
| [`NEGATIVE_RESULTS.md`](NEGATIVE_RESULTS.md) | **VALIDATED** | AGER and KRT8 tested as discriminators and rejected, with the control-locked enrichment test that did it |
| [`ECTOPIC_POD_ENDPOINT.md`](ECTOPIC_POD_ENDPOINT.md) | **SUPERSEDED as a specification; retained as the calibration record** | the endpoint sign in it is wrong. Read its banner: it lists section by section what still holds. |

### Routes and architecture

| document | status | what it is |
|---|---|---|
| [`QUPATH_FIJI_INTEGRATION.md`](QUPATH_FIJI_INTEGRATION.md) | **REFERENCE** (claims validated elsewhere) | why the two tools are used together, the published pattern this follows (Chiaruttini et al. 2022), where we deliberately differ |
| [`WSI_TILING_WORKFLOW.md`](WSI_TILING_WORKFLOW.md) | **VALIDATED** (plumbing) / **PROPOSED** (thresholds) | the whole-slide route: series selection, global tissue detection, tiling, seam correctness, aggregation |
| [`HE_BRIGHTFIELD_PIPELINE.md`](HE_BRIGHTFIELD_PIPELINE.md) | **CURRENT R1 / H3** | operator entrypoint, fail-closed status audit, practical whole-section review, and reportability boundaries |
| [`HE_BRIGHTFIELD_DECISION_HIERARCHY.md`](HE_BRIGHTFIELD_DECISION_HIERARCHY.md) | **R1 APPROVED** / **H4-H7 DEVELOPMENT** | separate H&E hierarchy, endpoint tiers, QC gates, current 4-mouse/8-section study contract, and validation ladder |
| [`VISUAL_PANELS.md`](VISUAL_PANELS.md) | **VALIDATED** (v8 rendered) | figure generation as a first-class module, with the reason v1–v7 were all wrong |
| [`CONFOCAL_SETTLED_RELEASE.md`](CONFOCAL_SETTLED_RELEASE.md) | **CURRENT** | canonical 80-field reconciliation, immutable release packaging, field-level aggregation semantics and rerun allowlist |

### Interpretation and configuration

These four are engine policy. They predate the confocal data and describe how the
engine *decides*, not what this study *found*.

| document | status | what it is |
|---|---|---|
| [`MARKER_MORPHOLOGY_GUIDE.md`](MARKER_MORPHOLOGY_GUIDE.md) | **REFERENCE**; numeric gates are **PROPOSED** pilot defaults | the morphology-first decision hierarchy, per-marker roles, sectioning rules, literature basis |
| [`COMPARTMENT_TAGS_AND_PROGRESSION.md`](COMPARTMENT_TAGS_AND_PROGRESSION.md) | **REFERENCE** | anatomical tags vs subcellular roles, multi-tag precedence, the full image → call progression |
| [`UNIVERSAL_MARKER_CONFIGURATION.md`](UNIVERSAL_MARKER_CONFIGURATION.md) | **REFERENCE** (schema) / **PROPOSED** (research profiles) | the reusable marker/panel schema; the IPF and adenocarcinoma profiles have never been run on data in this repo |
| [`Z_STACK_ANALYSIS.md`](Z_STACK_ANALYSIS.md) | **REFERENCE**; exercised on the ALI pilot only | marker-specific Z policies. **Unused by the current study** — the confocal batch is single-plane. |

### Project mechanics

| document | status | what it is |
|---|---|---|
| [`BRANCHING.md`](BRANCHING.md) | **CURRENT** | branch roles, what was retired and why, the completed Z-stack merge gate |
| [`PRIVACY_AND_DATA_BOUNDARY.md`](PRIVACY_AND_DATA_BOUNDARY.md) | **CURRENT** | what may be public, what must remain local, and the pre-publication privacy checks |

### Entry points outside `docs/`

- [`../WORKFLOW.md`](../WORKFLOW.md) — operational sequence for the
  field / confocal route, plus the shared interpretation model.
- [`../README.md`](../README.md) — installation, configuration, output schema.
- `../launcher/README.md` — the four launcher routes and the legacy-equivalence
  harness.
- `../config/endpoints/dysplastic_over_damaged.json` — **the current endpoint
  specification.** It is JSON rather than prose on purpose: it is reviewable and
  diffable, and it carries its own `validation_status`.

## Archived

Point-in-time audits and pilot records that no longer describe the current
pipeline live in [`../legacy/docs/`](../legacy/docs/README.md). They are kept
because they are the only record of the validation they document — but **none of
their numbers are current**, and thresholds quoted there are pilot placeholders.

Historical diagrams are archived in
[`../legacy/figures/`](../legacy/figures/README.md); their intensity-centered
logic no longer matches the morphology-first engine.

## For an AI agent picking this up

[AI_HANDOFF.md](AI_HANDOFF.md) — machine-oriented context transfer: architecture invariants, environment traps, the catalogue of silent-failure modes seen in this project, ranked open items, and the division of labour between the operator and automated work. Read it before proposing changes.
