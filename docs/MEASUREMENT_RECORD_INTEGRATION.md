# Measurement-record integration at Stage 4

Stage 4 can opt into schema-v2 measurement records while retaining the
established CSV outputs. The bridge is deliberately configuration-driven:
`aggregate_to_mouse.py` does not derive endpoint meaning from suffixes, infer a
panel profile from available columns, or turn a blank into zero.

```powershell
python .\aggregate_to_mouse.py D:\IFQ_Runs\<run>\analysis\run_summary.csv `
  --sampling-unit field `
  --measurement-record-spec .\confocal-measurement-records.json `
  --direct-run-manifest D:\IFQ_Runs\<run>\analysis\run_manifest.json
```

Direct-confocal schema-v2 publication requires the explicit modern engine
manifest. There is no filename-based discovery fallback. Plain CSV-only
descriptive aggregation remains available for legacy direct runs, but an
unsealed legacy run cannot be promoted to schema-v2 measurement records.

For a WSI `slide_level_summary.csv`, use `--sampling-unit section` and an
`area_wsi` specification. A successful opt-in run publishes
`measurement_records.jsonl` together with the mouse CSV, group CSV, and the
Stage 4 audit. The audit is written last and content-binds the specification,
JSONL, aggregation input, adapter code, and any additional declared inputs.
Failure quarantines the publication set.

## What the gate enforces

Before `aggregate_mice()` pools any rows, the bridge:

1. requires one exact profile for every source `panel` and rejects unused
   profiles;
2. maps all seven identifier fields explicitly, using `null` when an identity
   truly does not exist;
3. builds only endpoints named in `profiles[].endpoints` from the declared
   numerator, denominator, optional derived-value, and units;
4. verifies that numerator and denominator are additive columns the production
   aggregator actually pools;
5. rejects absent, blank, unknown, non-finite, negative, inconsistent, or
   zero-denominator measurements;
6. calls `require_aggregation_batch_eligible()` for each mouse/genotype/
   condition/panel pool before numerical pooling;
7. rejects unsupported probability estimators, QC failure, pending/rejected
   review, duplicate analytical identity, or profile/channel/sampling/
   compartment drift; and
8. writes the validated records atomically only as part of a successful Stage 4
   publication.

For direct confocal, the pre-pooling gate additionally requires a complete,
uncapped, failure-free `run_manifest_schema_version: "2.0.0"` publication. It
matches the exact `run_summary.csv` size and SHA-256, validates every successful
image's params identity, and re-hashes the current raw images, executed engine,
and (for StarDist) model, runtime manifest, and runtime artifacts. The explicit
record profiles must agree with the sealed engine/config hashes, panel channel
order, and segmentation-model identity. Any drift leaves no current Stage 4
CSV, JSONL, or audit publication.

Both the Stage 4 audit and every emitted record bind the exact bytes of
`schemas/measurement-record.schema.json`. The H&E accepted-review record route
uses the same schema-byte binding. Aggregation audits also carry a portable
repository code-set digest over each Python source's role, repository path,
and SHA-256, in addition to their location-aware code artifact set.

The gate currently supports the ratio endpoints already pooled by the
production paths. It does not guess categorical or ordinal state from legacy
columns. Those record types remain available through the shared adapters but
need a route with an explicit state/review authority before integration.
The current direct-confocal bridge is confined to `observed_units`; indexed WSI
requires exhaustive coverage and a `whole_section` estimand. No probability
estimator is implemented by this bridge.

## Specification shape

The root and every nested object are closed: unknown or missing fields fail.
This abbreviated direct-confocal example shows the complete shape. Replace all
placeholder hashes and identifiers with the exact reviewed profile values.

```json
{
  "spec_version": "1.0.0",
  "track": "cell_confocal",
  "record_level": "region",
  "panel_column": "panel",
  "identifier_columns": {
    "mouse_id": "mouse_id",
    "slide_id": null,
    "section_id": null,
    "field_id": "section_id",
    "tile_id": null,
    "region_id": "region",
    "cell_id": null
  },
  "target_estimand": "observed_units",
  "profiles": [
    {
      "panel": "LEFT",
      "measurement_profile_id": "confocal-left-frozen-v1",
      "channel_signature": [
        {"index": 1, "label": "DAPI", "role": "nuclear_context"},
        {"index": 2, "label": "KRT5", "role": "endpoint_numerator"}
      ],
      "segmentation_model": null,
      "sampling": {
        "design": "purposive",
        "inclusion_probability": null,
        "selection_source": "reviewer_selected_fields",
        "estimand_scope": "observed_units"
      },
      "compartment": {
        "status": "unassigned",
        "labels": [],
        "assignment_profile_id": null
      },
      "provenance": {
        "code_revision": "<engine revision or content hash>",
        "config_sha256": "<64 lowercase hex characters>",
        "measurement_profile_sha256": "<64 lowercase hex characters>",
        "run_id": "<portable run identifier>",
        "additional_inputs": []
      },
      "qc": {
        "status": "warning",
        "reason_codes": ["descriptive_release_only"],
        "review_status": "accepted"
      },
      "row_constraints": {
        "compartment": "unassigned"
      },
      "endpoints": [
        {
          "calculation": "ratio",
          "evaluability": "measured",
          "endpoint_id": "krt5_positive_cell_fraction",
          "reference_space_id": "included_nuclei-v1",
          "numerator_column": "KRT5_final_positive_cell_count",
          "numerator_unit": "cells",
          "denominator_column": "n_nuclei",
          "denominator_unit": "cells",
          "result_unit": "fraction",
          "value_column": "KRT5_final_positive_fraction_of_total_cells"
        }
      ]
    }
  ]
}
```

`row_constraints` are exact source assertions, not filters. If any row in that
profile disagrees, the complete attempt fails; rows are never silently dropped.
Use a separate explicit profile when panels have different channel order,
endpoint availability, measurement hashes, or compartment contracts.

## Additional content-bound provenance

The aggregation summary, measurement-record specification, endpoint inputs,
and upstream Stage 3 audit (for WSI) are bound automatically. Extra artifacts
can be added by role:

```powershell
python .\aggregate_to_mouse.py .\run_summary.csv `
  --sampling-unit field `
  --measurement-record-spec .\records.json `
  --direct-run-manifest .\run_manifest.json `
  --measurement-record-input reviewed_thresholds=.\thresholds.json
```

The direct run manifest, executed engine, successful params files, workbook,
and current raw/model/runtime bytes are bound automatically after validation;
do not re-declare those roles with `--measurement-record-input`.

To make an extra artifact mandatory for a profile, list its exact role and
SHA-256 in `provenance.additional_inputs`. The bridge verifies that the CLI
bound a real file under that role and that its bytes match. A hash written only
in the specification is not accepted as a substitute for a bound artifact.

## WSI-specific assertions

An `area_wsi` specification must use `record_level: "slide"` and explicitly map
`slide_id` (normally from the Stage 3 `slide` column). In addition to the common
checks, the bridge verifies each Stage 3 row against its indexed lineage:

- specification `measurement_profile_sha256` equals the carried Stage 2
  measurement-profile hash;
- specification `config_sha256` equals `resolved_config_sha256`;
- specification `code_revision` equals the exact Stage 2 script SHA-256;
- the structured channel indices and labels exactly reconstruct the carried
  ordered panel signature; and
- Stage 2 index, raw source package, Stage 1 manifest, and tile-manifest hashes
  enter each record as content-bound inputs.

This requires a valid Stage 3 audit as before. It does not prove that an
acquisition label corresponds to the biological stain, validate a tissue or
airway mask, calibrate an endpoint threshold, or promote a capped engineering
run.

## Scientific boundary

Schema conformance and eligibility establish that the software knows what was
measured, from which rows, under which declared profile, and whether those rows
may enter the stated descriptive pool. They do not establish segmentation
accuracy, stain identity, anatomical validity, sampling representativeness, or
biological reproducibility. Purposive confocal fields therefore remain an
`observed_units` estimand, and the current unreplicated cohort remains
descriptive even after successful JSONL emission.
