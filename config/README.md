# Configuration assets

## Backend-neutral measurement contracts

- [`measurement_definitions/krt5_positive_area_kernel_v1.json`](measurement_definitions/krt5_positive_area_kernel_v1.json)
  is the pure numerical KRT5 area kernel. It contains no threshold value,
  acquisition-channel mapping, backend, runtime, lifecycle, or claim prose.
- [`measurement_definitions/g_surf_confocal_260808_krt5_threshold_candidate_v1.json`](measurement_definitions/g_surf_confocal_260808_krt5_threshold_candidate_v1.json)
  binds threshold 300 to the exact scientific-definition hash and declares a
  narrow uint16 confocal reconstruction scope. Its scope-binding state is
  `identifier_only_unattested`: no acquisition bytes or scope profile are bound.
  It has no authorization and must not be used for WSI or transferred to
  another acquisition regime.

The schemas live under [`../schemas/`](../schemas/), the resolver is
[`../ifquant/measurement_definition.py`](../ifquant/measurement_definition.py),
and the full interpretation boundary and next-stage design are in
[`../docs/ARCHITECTURE_VNEXT.md`](../docs/ARCHITECTURE_VNEXT.md). See the
directory [`README`](measurement_definitions/README.md) for hashes, CLI usage,
and the legacy endpoint-name crosswalk.

## Brightfield and study contracts

- [`brightfield/he_decision_hierarchy.json`](brightfield/he_decision_hierarchy.json)
  is the fail-closed H&E decision order. It is **proposed**, not an enabled
  analysis engine.
- [`brightfield/he_endpoints.json`](brightfield/he_endpoints.json) defines
  quantitative, blinded-ordinal, and deliberately deferred H&E endpoint tiers.
- [`studies/g_surf_he_20260812.json`](studies/g_surf_he_20260812.json) is the
  verified four-mouse/eight-section identity and series contract for the
  current Olympus VSI cohort.

These files keep biological identity and endpoint semantics reviewable without
placing H&E into the fluorescence Fiji engine.

- [`lung_marker_registry.json`](lung_marker_registry.json) stores descriptive
  marker aliases, localization, analytical-role defaults, lineage/state notes,
  and research-context cautions. It is not a positivity cutoff table or
  diagnostic classifier.
- [`custom_panels.example.json`](custom_panels.example.json) demonstrates
  opt-in image channel maps for ALI Z-stacks, acute injury, IPF, stromal
  fibrosis, Red2-KrasG12D RFP/Ki-67/SOX9 analysis, and lung adenocarcinoma
  lineage research.

Copy the example to a study-controlled location before editing it. Load that
copy with `IFQ_PANEL_CONFIG`; do not alter the built-in panel definitions for a
new acquisition.

See
[`docs/UNIVERSAL_MARKER_CONFIGURATION.md`](../docs/UNIVERSAL_MARKER_CONFIGURATION.md)
for schema, ROI vocabulary, and validation rules. See
[`docs/Z_STACK_ANALYSIS.md`](../docs/Z_STACK_ANALYSIS.md) for `zPolicy`,
automatic slab selection, fixed study ranges, Z-profile QC, and the
visualization-only percentile/gamma enhancement branch. Enhanced PNGs are
never inputs to segmentation or marker calls.
