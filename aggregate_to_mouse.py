#!/usr/bin/env python3
"""
aggregate_to_mouse.py
=====================================================================
Roll the per-region `run_summary.csv` produced by IF_Quant_Pipeline.groovy
up to the BIOLOGICAL replicate (mouse) level, then summarise per group.

Why this exists
---------------
The image pipeline emits one row per tissue/lesion region. Several sections
(and several regions per section) come from the SAME animal. Statistics for
this study must use n = MICE, not n = sections/regions. Averaging region rows
directly would pseudo-replicate and inflate significance.

Pooling is area-weighted (the statistically correct pooling for a section):
  * KRT5 pod area fraction   = sum(pod_area) / sum(tissue_area)      per mouse
  * marker density (/mm2)    = sum(morphology-authoritative pos_count)
                               / sum(tissue_area_mm2) per mouse
  * mean pod size            = sum(pod_area) / sum(n_pods)           per mouse
Counts and areas are summed; thresholds are averaged (QC only).

Outputs (written next to the input, or to --outdir):
  mouse_level_summary.csv  -- one row per (mouse_id, genotype, condition, panel)
  group_level_summary.csv  -- mean / sd / sem / n_mice per (genotype, condition,
                              panel, metric); n_mice is the real n for stats.

Usage
-----
  python3 aggregate_to_mouse.py /path/to/analysis_output/run_summary.csv
  python3 aggregate_to_mouse.py run_summary.csv --outdir ./stats
  python3 aggregate_to_mouse.py run_summary.csv --endpoint-csv endpoint_areas.csv \
      --endpoint-spec config/endpoints/dysplastic_over_damaged.json

No third-party dependencies (standard library only).
=====================================================================
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict

# Columns that identify a row rather than measure something.
KEY_COLS = ["mouse_id", "genotype", "condition", "panel"]
ROW_ID_COLS = ["image", "region", "section_id"]
WSI_UNIFORM_PROVENANCE = [
    "aggregation_contract_version",
    "stage2_source_mode",
    "stage2_script_sha256",
    "resolved_config_sha256",
    "measurement_profile_sha256",
    "ordered_channel_signature",
    "channel_signature_authority",
]
WSI_PER_SLIDE_PROVENANCE = [
    "stage2_index_sha256",
    "stage1_manifest_sha256",
    "tile_manifest_sha256",
]
WSI_INDICATOR_COLUMNS = {
    "slide",
    "n_tiles_expected",
    "n_tiles_analyzed",
    "n_tiles_missing",
    "tile_coverage_fraction",
    "stage1_core_tissue_area_um2",
    "stage2_region_area_um2",
    "stage2_source_mode",
    "stage2_index_sha256",
}


def _num(v):
    """Parse a cell to float; blanks / non-numeric -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.upper() == "NA":
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            sys.exit(f"ERROR: {path} is empty or has no header.")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            sys.exit(f"ERROR: {path} contains duplicate CSV header columns.")
        rows = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                sys.exit(
                    f"ERROR: {path} row {row_number} has more fields than its header."
                )
            if not all(value is None or isinstance(value, str) for value in row.values()):
                sys.exit(f"ERROR: {path} row {row_number} contains a non-text CSV value.")
            if any((value or "").strip() for value in row.values()):
                rows.append(row)
    return reader.fieldnames, rows


def is_quarantined_summary(path):
    """Recognize Stage 3 diagnostic and stale-output naming conventions."""
    name = os.path.basename(path).lower()
    return ".rejected" in name or ".stale." in name


def validate_rows(header, rows):
    """Fail before aggregation when biological identity or row identity is unsafe."""
    missing_columns = [
        c for c in KEY_COLS + ROW_ID_COLS + ["region_area_um2", "n_nuclei"]
        if c not in header
    ]
    if missing_columns:
        sys.exit("ERROR: run_summary.csv is missing required columns: " +
                 ", ".join(missing_columns))

    invalid_mouse_rows = [
        r for r in rows
        if (r.get("mouse_id") or "").strip().upper() in {"", "NA", "N/A", "UNKNOWN"}
    ]
    if invalid_mouse_rows:
        examples = ", ".join((r.get("image") or "<unknown>") for r in invalid_mouse_rows[:5])
        sys.exit(
            f"ERROR: {len(invalid_mouse_rows)} row(s) lack a valid mouse_id ({examples}). "
            "Do not aggregate unknown animals into one pseudo-mouse; fix samplesheet.csv first."
        )

    identities = defaultdict(set)
    for r in rows:
        identities[(r.get("mouse_id") or "").strip()].add(
            ((r.get("genotype") or "NA").strip(), (r.get("condition") or "NA").strip())
        )
    conflicts = {mouse: values for mouse, values in identities.items() if len(values) > 1}
    if conflicts:
        details = "; ".join(f"{mouse}: {sorted(values)}" for mouse, values in list(conflicts.items())[:5])
        sys.exit("ERROR: a mouse_id maps to multiple genotype/condition identities: " + details)

    seen = set()
    duplicates = []
    for r in rows:
        # The biological/measurement identity must not depend on output_key:
        # a rerun can legitimately generate a different output name for the
        # same section and would otherwise evade duplicate detection.
        # section_id is reused across animals in the established confocal
        # route (for example G001). Include the specimen identity while keeping
        # output_key out of the key so a renamed retry still collides.
        key_columns = ["mouse_id", "section_id", "region", "panel"]
        key = tuple((r.get(c) or "").strip() for c in key_columns)
        if any(not value for value in key):
            sys.exit(
                "ERROR: blank mouse_id/section_id/region/panel in aggregation identity: "
                + repr(key)
            )
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        sys.exit(
            f"ERROR: {len(duplicates)} duplicate specimen-section-region-panel row(s) detected. "
            "Combine only one declared run per analyzed region; output_key changes do not "
            "make a retry a new measurement."
        )

    if "qc_status" in header:
        failed_qc = [
            (row.get("image") or row.get("section_id") or "<unknown>")
            for row in rows
            if (row.get("qc_status") or "").strip().lower() != "ok"
        ]
        if failed_qc:
            sys.exit(
                "ERROR: refusing to aggregate non-passing slide/region QC rows: "
                + ", ".join(failed_qc[:5])
            )

    numeric_problems = []
    rows_by_panel = defaultdict(list)
    for row in rows:
        rows_by_panel[(row.get("panel") or "").strip()].append(row)
    for panel, panel_rows in sorted(rows_by_panel.items()):
        for column in classify_columns(header)["sum_cols"]:
            missing, invalid, nonpositive = 0, [], 0
            available = 0
            for index, row in enumerate(panel_rows, start=1):
                raw = row.get(column)
                token = "" if raw is None else str(raw).strip()
                if token == "" or token.upper() in {"NA", "N/A"}:
                    missing += 1
                    continue
                available += 1
                value = _num(token)
                if value is None:
                    invalid.append((index, token))
                elif column == "region_area_um2" and value <= 0:
                    nonpositive += 1

            # The established confocal schema is a union of LEFT and RIGHT
            # measurements.  A marker that is absent from a panel is blank for
            # every row in that panel and is unevaluable, not numerically zero.
            # Once a column is present for a panel, however, it must be present
            # and finite for every row in that panel.
            if available == 0:
                if column in {"region_area_um2", "n_nuclei"}:
                    numeric_problems.append(
                        f"{column}: unavailable for every row in panel {panel!r}"
                    )
                continue
            if invalid:
                numeric_problems.append(
                    f"{column}: {len(invalid)} invalid/non-finite value(s) "
                    f"in panel {panel!r}"
                )
            if missing:
                numeric_problems.append(
                    f"{column}: missing in {missing}/{len(panel_rows)} row(s) "
                    f"in panel {panel!r}"
                )
            if nonpositive:
                numeric_problems.append(
                    f"{column}: {nonpositive} non-positive region area value(s) "
                    f"in panel {panel!r}"
                )
    if numeric_problems:
        sys.exit(
            "ERROR: additive measurements are incomplete or invalid; missing/non-finite "
            "values cannot be coerced to zero: " + " | ".join(numeric_problems[:8])
        )

    # Stage 3 v2 rows are authoritative only when every contributing slide was
    # produced from an explicit hashed index and passed QC. Direct confocal
    # run_summary inputs predate these columns and remain on their established
    # validation route.
    is_wsi_summary = (
        "aggregation_contract_version" in header
        or bool(WSI_INDICATOR_COLUMNS.intersection(header))
    )
    if is_wsi_summary:
        required_provenance = (
            WSI_UNIFORM_PROVENANCE
            + WSI_PER_SLIDE_PROVENANCE
            + ["qc_status", "dataset_qc_status"]
        )
        missing_provenance = [c for c in required_provenance if c not in header]
        if missing_provenance:
            sys.exit(
                "ERROR: WSI slide summary is missing provenance columns: "
                + ", ".join(missing_provenance)
            )
        for row in rows:
            slide = (row.get("image") or row.get("section_id") or "<unknown>").strip()
            if (row.get("aggregation_contract_version") or "").strip() != "2.0.0":
                sys.exit(
                    f"ERROR: WSI slide row has unsupported aggregation contract: {slide}"
                )
            if (row.get("stage2_source_mode") or "").strip() != "explicit_hashed_index":
                sys.exit(
                    f"ERROR: WSI slide row is not backed by an explicit Stage 2 index: {slide}"
                )
            if (row.get("dataset_qc_status") or "").strip().lower() != "ok":
                sys.exit(
                    f"ERROR: WSI slide row belongs to a rejected dataset: {slide}"
                )
            blanks = [
                column for column in WSI_UNIFORM_PROVENANCE + WSI_PER_SLIDE_PROVENANCE
                if not (row.get(column) or "").strip()
            ]
            if blanks:
                sys.exit(
                    f"ERROR: WSI slide row {slide} has blank provenance: {', '.join(blanks)}"
                )
            malformed_hashes = [
                column
                for column in WSI_UNIFORM_PROVENANCE + WSI_PER_SLIDE_PROVENANCE
                if column.endswith("_sha256")
                and re.fullmatch(r"[0-9a-f]{64}", (row.get(column) or "").strip()) is None
            ]
            if malformed_hashes:
                sys.exit(
                    f"ERROR: WSI slide row {slide} has malformed SHA-256 provenance: "
                    + ", ".join(malformed_hashes)
                )

        # A measurement profile must be comparable across the biological
        # groups that will appear together in the cohort output.  Checking
        # only within a mouse would allow genotype/condition groups to be
        # measured with different scripts or configs while still looking
        # like one valid comparison.  Panel is the appropriate boundary:
        # LEFT and RIGHT can legitimately have different channel mappings,
        # but every mouse within a panel must share one profile.
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row.get("panel") or "NA").strip()].append(row)
        for panel, group in grouped.items():
            for column in WSI_UNIFORM_PROVENANCE:
                values = {(row.get(column) or "").strip() for row in group}
                if len(values) != 1:
                    sys.exit(
                        "ERROR: panel rows mix incompatible WSI provenance "
                        f"for {column}: panel={panel!r}, values={sorted(values)}"
                    )


def merge_endpoint_rows(header, rows, endpoint_path):
    """Join endpoint measurements to exactly matching run rows.

    Endpoint evaluation is commonly panel-filtered. Return only matched rows so
    an endpoint computed on LEFT cannot become a plausible zero-valued endpoint
    on RIGHT during mouse aggregation.
    """
    if "output_key" not in header:
        sys.exit("ERROR: --endpoint-csv requires output_key in run_summary.csv")
    endpoint_header, endpoint_rows = read_rows(endpoint_path)
    required = [c for c in ("output_key", "region") if c not in endpoint_header]
    if required:
        sys.exit("ERROR: endpoint CSV is missing required columns: " + ", ".join(required))
    if not endpoint_rows:
        sys.exit("ERROR: endpoint CSV has no data rows")

    main_by_key = {}
    for row in rows:
        key = ((row.get("output_key") or "").strip(), (row.get("region") or "").strip())
        if key in main_by_key:
            sys.exit(f"ERROR: duplicate run_summary endpoint join key: {key}")
        main_by_key[key] = row

    endpoint_by_key = {}
    for row in endpoint_rows:
        key = ((row.get("output_key") or "").strip(), (row.get("region") or "").strip())
        if key in endpoint_by_key:
            sys.exit(f"ERROR: duplicate endpoint CSV join key: {key}")
        endpoint_by_key[key] = row

    unmatched = [key for key in endpoint_by_key if key not in main_by_key]
    if unmatched:
        sys.exit(
            f"ERROR: {len(unmatched)} endpoint row(s) do not match run_summary "
            f"by (output_key, region); examples: {unmatched[:5]}"
        )

    identity = {"output_key", "image", "region", "region_mode"}
    measurement_cols = [c for c in endpoint_header if c not in identity]
    conflicts = [c for c in measurement_cols if c in header]
    if conflicts:
        sys.exit("ERROR: endpoint CSV would overwrite run_summary columns: " + ", ".join(conflicts))

    merged = []
    for key, endpoint_row in endpoint_by_key.items():
        row = dict(main_by_key[key])
        for column in measurement_cols:
            row[column] = endpoint_row.get(column, "")
        merged.append(row)
    return list(header) + measurement_cols, merged

def classify_columns(header):
    """Group measurement columns by how they must be pooled."""
    # The plain <marker>_pos_count field is morphology-authoritative. Keep the
    # explicitly named raw-mean and state-audit fields in separate categories so
    # they cannot silently become the statistical endpoint.
    pos_count = [
        c for c in header
        if c.endswith("_pos_count")
        and not c.endswith("_morphology_pos_count")
        and not c.endswith("_raw_mean_pos_count")
        and not c.endswith("_true_pos_count")
        and not c.endswith("_marker_evidence_pos_count")
    ]
    raw_mean_pos_count = [c for c in header if c.endswith("_raw_mean_pos_count")]
    morphology_pos_count = [c for c in header if c.endswith("_morphology_pos_count")]
    morphology_negative_count = [c for c in header if c.endswith("_morphology_negative_count")]
    marker_indeterminate_count = [
        c for c in header if c.endswith("_indeterminate_count")
        and not c.startswith("class_")
    ]
    morphology_evaluable_count = [c for c in header if c.endswith("_morphology_evaluable_count")]
    final_cell_state_count = [
        c for c in header
        if c.endswith((
            "_final_positive_cell_count",
            "_final_negative_cell_count",
            "_final_indeterminate_cell_count",
            "_context_resolved_positive_count",
            "_context_resolved_evaluable_count",
            "_context_unresolved_positive_count",
            "_marker_evidence_pos_count",
        ))
    ]
    marker_audit_count = [
        c for c in header
        if c.endswith((
            "_raw_positive_final_negative_count",
            "_raw_negative_final_positive_count",
            "_intensity_morphology_discordant_count",
            "_review_burden_proxy_count",
        ))
    ]
    nucleus_qc_count = [
        c for c in header
        if c in {
            "n_rejected_nucleus_candidates", "n_rejected_below_min_area",
            "n_rejected_at_image_edge", "n_rejected_by_particle_filter",
            "n_nucleus_candidates_total",
        }
    ]
    positive_area = [c for c in header if c.endswith("_positive_area_um2")]
    n_components = [c for c in header if c.endswith("_n_components")]
    # Slide-level partition QC has its own denominator semantics. These columns
    # are raw additive areas, not region-level fractions, so they are safe to
    # carry to mouse level by summing and recomputing the fractions below.
    # Keeping the names explicit prevents a future arbitrary ``*_area_um2``
    # column from silently acquiring sum semantics.
    partition_area = [
        c for c in header if c in {"damaged_area_um2", "intact_area_um2"}
    ]
    intact_pod_area = [c for c in header if c.endswith("_pod_area_um2_in_intact")]
    endpoint_denominator_area = [
        c for c in header if c.endswith("_denominator_area_um2")
    ]
    # total pod area only -- exclude the derived per-region MEAN pod size, which
    # also ends in "_pod_area_um2" and would otherwise be summed as an area.
    pod_area = [c for c in header
                if c.endswith("_pod_area_um2") and not c.endswith("_mean_pod_area_um2")]
    n_pods = [c for c in header if c.endswith("_n_pods")]
    class_count = [
        c for c in header if c.startswith("class_") and c.endswith("_count")
        and not c.endswith("_evaluable_count")
        and not c.endswith("_indeterminate_count")
    ]
    class_evaluable_count = [c for c in header if c.startswith("class_") and c.endswith("_evaluable_count")]
    class_indeterminate_count = [c for c in header if c.startswith("class_") and c.endswith("_indeterminate_count")]
    # everything else numeric-ish that we simply sum or average
    state_count_set = (set(raw_mean_pos_count) | set(morphology_pos_count) |
                       set(morphology_negative_count) | set(marker_indeterminate_count) |
                       set(morphology_evaluable_count) | set(class_evaluable_count) |
                       set(class_indeterminate_count) | set(marker_audit_count) |
                       set(final_cell_state_count))
    sum_col_set = (set(["region_area_um2", "n_nuclei"]) | set(pos_count) |
                   set(pod_area) | set(n_pods) | set(class_count) | state_count_set |
                   set(nucleus_qc_count) | set(positive_area) | set(n_components) |
                   set(partition_area) | set(intact_pod_area) |
                   set(endpoint_denominator_area))
    # Preserve the source schema order. Iterating a set made output column order
    # depend on Python hash randomization, which undermines byte-level rerun
    # comparisons even when every numerical value is identical.
    state_counts = [column for column in header if column in state_count_set]
    sum_cols = [column for column in header if column in sum_col_set]
    # derived columns we recompute (do NOT sum): fractions, densities, mean pod size, thresholds
    return {
        "pos_count": pos_count,
        "raw_mean_pos_count": raw_mean_pos_count,
        "state_counts": state_counts,
        "final_cell_state_count": final_cell_state_count,
        "marker_audit_count": marker_audit_count,
        "nucleus_qc_count": nucleus_qc_count,
        "positive_area": positive_area,
        "n_components": n_components,
        "partition_area": partition_area,
        "intact_pod_area": intact_pod_area,
        "endpoint_denominator_area": endpoint_denominator_area,
        "pod_area": pod_area,
        "n_pods": n_pods,
        "class_count": class_count,
        "sum_cols": sum_cols,
    }


def marker_of(col, suffix):
    return col[: -len(suffix)]


def aggregate_mice(header, rows, endpoint_relation=None, sampling_unit="section"):
    if sampling_unit not in {"field", "section"}:
        raise ValueError(
            "sampling_unit must be 'field' for microscope fields or "
            "'section' for histological/whole-slide sections"
        )
    cats = classify_columns(header)
    groups = defaultdict(list)
    for r in rows:
        key = tuple(r.get(k, "NA") for k in KEY_COLS)
        groups[key].append(r)

    out_rows = []
    for key, grp in sorted(groups.items()):
        mouse_id, genotype, condition, panel = key
        rec = {"mouse_id": mouse_id, "genotype": genotype,
               "condition": condition, "panel": panel}
        if "aggregation_contract_version" in header:
            for column in WSI_UNIFORM_PROVENANCE:
                rec[column] = (grp[0].get(column) or "").strip()
            for column in WSI_PER_SLIDE_PROVENANCE:
                values = sorted({(row.get(column) or "").strip() for row in grp})
                rec[column + "s"] = ";".join(values)
        rec["n_regions"] = len(grp)
        rec[f"n_{sampling_unit}s"] = len(
            {(g.get("section_id") or "NA") for g in grp})
        rec["sampling_unit"] = sampling_unit

        # --- sums ---
        # The input schema is a union across panels.  Do not turn a marker that
        # is wholly unavailable in this panel into a plausible zero-valued
        # measurement; omit it from this mouse row so CSV output leaves it
        # blank and group statistics do not count it as evaluated.
        applicable_sum_cols = [
            c for c in cats["sum_cols"]
            if any(
                str(g.get(c) or "").strip().upper() not in {"", "NA", "N/A"}
                for g in grp
            )
        ]
        sums = {}
        for c in applicable_sum_cols:
            vals = [_num(g.get(c)) for g in grp]
            vals = [v for v in vals if v is not None]
            if vals:
                sums[c] = math.fsum(vals)

        total_area_um2 = sums.get("region_area_um2", 0.0)
        total_area_mm2 = total_area_um2 / 1e6
        rec["total_tissue_area_um2"] = total_area_um2
        rec["total_nuclei"] = sums.get("n_nuclei", 0.0)

        # --- WSI damaged/intact partition QC ------------------------------
        # These values used to stop at slide level because classify_columns()
        # did not recognize them. Pool the additive areas first, then recompute
        # every fraction from the mouse-level denominator.
        if any(c in sums for c in cats["partition_area"]):
            damaged_area = sums.get("damaged_area_um2", 0.0)
            intact_area = sums.get("intact_area_um2", 0.0)
            parenchyma_area = damaged_area + intact_area
            rec["damaged_area_um2"] = damaged_area
            rec["intact_area_um2"] = intact_area
            rec["damaged_fraction_of_parenchyma"] = (
                damaged_area / parenchyma_area if parenchyma_area > 0 else 0.0
            )
        for c in (column for column in cats["intact_pod_area"] if column in sums):
            marker = marker_of(c, "_pod_area_um2_in_intact")
            area = sums[c]
            intact_area = sums.get("intact_area_um2", 0.0)
            rec[c] = area
            rec[f"{marker}_pod_area_frac_of_intact"] = (
                area / intact_area if intact_area > 0 else 0.0
            )

        # --- relational endpoint denominators ----------------------------
        # Endpoint CSVs can be joined to run_summary before aggregation. A
        # denominator is additive; its fraction must be rebuilt from pooled
        # numerator/denominator areas, never averaged across unequal regions.
        for c in (column for column in cats["endpoint_denominator_area"] if column in sums):
            endpoint = marker_of(c, "_denominator_area_um2")
            numerator_col = f"{endpoint}_pod_area_um2"
            denominator_area = sums[c]
            numerator_area = sums.get(numerator_col, 0.0)
            rec[c] = denominator_area
            rec[f"{endpoint}_fraction"] = (
                numerator_area / denominator_area if denominator_area > 0 else 0.0
            )
        if endpoint_relation:
            numerator_col = endpoint_relation["area_column"]
            bare_col = endpoint_relation["bare_area_column"]
            companion_fraction_col = endpoint_relation["numerator_fraction_of_bare_column"]
            numerator_area = sums.get(numerator_col, 0.0)
            bare_area = sums.get(bare_col, 0.0)
            rec[companion_fraction_col] = (
                numerator_area / bare_area if bare_area > 0 else 0.0
            )

        # --- nucleus-candidate QC totals and pooled fractions ---
        for c in (column for column in cats["nucleus_qc_count"] if column in sums):
            rec[f"{c}_total"] = sums[c]
        candidate_total = sums.get(
            "n_nucleus_candidates_total",
            sums.get("n_nuclei", 0.0) + sums.get("n_rejected_nucleus_candidates", 0.0),
        )
        rejected_total = sums.get("n_rejected_nucleus_candidates", 0.0)
        rec["nucleus_candidate_acceptance_fraction"] = (
            sums.get("n_nuclei", 0.0) / candidate_total if candidate_total > 0 else 0.0
        )
        rec["nucleus_candidate_rejection_fraction"] = (
            rejected_total / candidate_total if candidate_total > 0 else 0.0
        )
        for source, target in (
            ("n_rejected_below_min_area", "rejected_below_min_fraction_of_rejected"),
            ("n_rejected_at_image_edge", "rejected_edge_fraction_of_rejected"),
            ("n_rejected_by_particle_filter", "rejected_particle_filter_fraction_of_rejected"),
        ):
            rec[target] = sums.get(source, 0.0) / rejected_total if rejected_total > 0 else 0.0

        # --- marker positive counts + pooled density ---
        for c in (column for column in cats["pos_count"] if column in sums):
            m = marker_of(c, "_pos_count")
            rec[f"{m}_pos_count_total"] = sums[c]
            rec[f"{m}_density_per_mm2"] = (sums[c] / total_area_mm2) if total_area_mm2 > 0 else 0.0

        # --- explicit audit/state totals; never substitute for the endpoint ---
        for c in (column for column in cats["raw_mean_pos_count"] if column in sums):
            m = marker_of(c, "_raw_mean_pos_count")
            rec[f"{m}_raw_mean_pos_count_total"] = sums[c]
            rec[f"{m}_raw_mean_density_per_mm2"] = (
                sums[c] / total_area_mm2 if total_area_mm2 > 0 else 0.0
            )
        for c in (column for column in cats["state_counts"] if column in sums):
            if c in cats["raw_mean_pos_count"]:
                continue
            rec[f"{c}_total"] = sums[c]

        # Recompute morphology/QC fractions from pooled counts. Region-level
        # percentages must never be averaged because region sizes differ.
        for c in [
            x for x in header
            if x.endswith("_morphology_evaluable_count") and x in sums
        ]:
            marker = marker_of(c, "_morphology_evaluable_count")
            evaluable = sums.get(c, 0.0)
            included = sums.get("n_nuclei", 0.0)
            positive = sums.get(f"{marker}_morphology_pos_count", 0.0)
            negative = sums.get(f"{marker}_morphology_negative_count", 0.0)
            indeterminate = sums.get(f"{marker}_indeterminate_count", 0.0)
            discordant = sums.get(f"{marker}_intensity_morphology_discordant_count", 0.0)
            review = sums.get(f"{marker}_review_burden_proxy_count", indeterminate + discordant)
            rec[f"{marker}_morphology_positive_fraction_of_evaluable"] = positive / evaluable if evaluable > 0 else 0.0
            rec[f"{marker}_morphology_negative_fraction_of_evaluable"] = negative / evaluable if evaluable > 0 else 0.0
            rec[f"{marker}_indeterminate_fraction_of_included"] = indeterminate / included if included > 0 else 0.0
            rec[f"{marker}_intensity_morphology_discordant_fraction_of_evaluable"] = discordant / evaluable if evaluable > 0 else 0.0
            rec[f"{marker}_review_burden_proxy_fraction_of_included"] = review / included if included > 0 else 0.0

        # --- explicit final cell counts and fractions among all included cells ---
        # These are the human-facing "eventual quantification" fields used by
        # the Excel workbook. Pool counts first, then divide by the pooled
        # nucleus denominator; never average region-level fractions.
        for c in [
            x for x in header
            if x.endswith("_final_positive_cell_count") and x in sums
        ]:
            marker = marker_of(c, "_final_positive_cell_count")
            included = sums.get("n_nuclei", 0.0)
            for state in ("positive", "negative", "indeterminate"):
                source = f"{marker}_final_{state}_cell_count"
                count = sums.get(source, 0.0)
                rec[f"{source}_total"] = count
                rec[f"{marker}_final_{state}_fraction_of_total_cells"] = (
                    count / included if included > 0 else 0.0
                )
            context_positive = sums.get(f"{marker}_context_resolved_positive_count", 0.0)
            context_evaluable = sums.get(f"{marker}_context_resolved_evaluable_count", 0.0)
            rec[f"{marker}_context_resolved_positive_fraction_of_total_cells"] = (
                context_positive / included if included > 0 else 0.0
            )
            rec[f"{marker}_context_resolved_positive_fraction"] = (
                context_positive / context_evaluable if context_evaluable > 0 else 0.0
            )

        # --- generic regional area endpoints (AcTub, membranes, reporter, ECM) ---
        for c in (column for column in cats["positive_area"] if column in sums):
            marker = marker_of(c, "_positive_area_um2")
            area = sums[c]
            components = sums.get(f"{marker}_n_components", 0.0)
            rec[f"{marker}_positive_area_um2_total"] = area
            rec[f"{marker}_positive_area_fraction"] = area / total_area_um2 if total_area_um2 > 0 else 0.0
            rec[f"{marker}_n_components_total"] = components
            rec[f"{marker}_mean_component_area_um2"] = area / components if components > 0 else 0.0

        # --- pod area, fraction, count, mean size (per area-marker, e.g. KRT5) ---
        for c in (column for column in cats["pod_area"] if column in sums):
            m = marker_of(c, "_pod_area_um2")
            pod_area = sums[c]
            npods = sums.get(f"{m}_n_pods", 0.0)
            rec[f"{m}_pod_area_um2_total"] = pod_area
            rec[f"{m}_pod_area_frac"] = (pod_area / total_area_um2) if total_area_um2 > 0 else 0.0
            rec[f"{m}_n_pods_total"] = npods
            rec[f"{m}_mean_pod_area_um2"] = (pod_area / npods) if npods > 0 else 0.0

        # --- classification counts + pooled density ---
        for c in (column for column in cats["class_count"] if column in sums):
            base = c[: -len("_count")]  # e.g. class_KRT5+_AGER-
            rec[f"{base}_count_total"] = sums[c]
            rec[f"{base}_density_per_mm2"] = (sums[c] / total_area_mm2) if total_area_mm2 > 0 else 0.0

        out_rows.append(rec)
    return out_rows


def _stats(values):
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return 0, None, None, None
    mean = sum(vals) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        sd = math.sqrt(var)
        sem = sd / math.sqrt(n)
    else:
        sd = None
        sem = None
    return n, mean, sd, sem


def group_stats(mouse_rows):
    """Group descriptives; variability is not estimable for a one-mouse group."""
    metric_cols = []
    seen = set()
    skip = set(KEY_COLS) | {
        "n_regions", "n_fields", "n_sections", "sampling_unit",
    }
    for r in mouse_rows:
        for c in r:
            if c in skip or c in seen:
                continue
            if isinstance(r[c], (int, float)):
                metric_cols.append(c)
                seen.add(c)

    groups = defaultdict(list)
    for r in mouse_rows:
        groups[(r["genotype"], r["condition"], r["panel"])].append(r)

    out = []
    for (geno, cond, panel), grp in sorted(groups.items()):
        provenance = {}
        if any("aggregation_contract_version" in row for row in grp):
            for column in WSI_UNIFORM_PROVENANCE:
                values = {(row.get(column) or "").strip() for row in grp}
                if len(values) == 1:
                    provenance[column] = next(iter(values))
            for column in WSI_PER_SLIDE_PROVENANCE:
                list_column = column + "s"
                values = sorted({
                    value
                    for row in grp
                    for value in (row.get(list_column) or "").split(";")
                    if value
                })
                provenance[list_column] = ";".join(values)
        for metric in metric_cols:
            vals = [r[metric] for r in grp if isinstance(r.get(metric), (int, float))]
            if not vals:
                continue
            n, mean, sd, sem = _stats(vals)
            out.append({
                "genotype": geno,
                "condition": cond,
                "panel": panel,
                "metric": metric,
                "n_mice": n,
                "mean": mean,
                "sd": sd,
                "sem": sem,
                "reportability": (
                    "DESCRIPTIVE_ONLY" if n < 2 else "VARIABILITY_ESTIMABLE"
                ),
                **provenance,
            })
    return out


def write_csv(path, rows):
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", newline="", encoding="utf-8") as fh:
            if rows:
                cols = []
                for r in rows:
                    for c in r:
                        if c not in cols:
                            cols.append(c)
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow(r)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def quarantine_if_exists(path):
    """Move a previous canonical Stage 4 output aside before a new attempt."""
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stem, extension = os.path.splitext(path)
    stale = f"{stem}.STALE.{digest.hexdigest()[:12]}{extension}"
    os.replace(path, stale)
    return stale


def main():
    ap = argparse.ArgumentParser(description="Aggregate per-region run_summary.csv to mouse and group level.")
    ap.add_argument("run_summary", help="path to run_summary.csv from the Fiji pipeline")
    ap.add_argument("--outdir", default=None, help="output folder (default: alongside input)")
    ap.add_argument(
        "--sampling-unit",
        choices=("field", "section"),
        default="section",
        help=(
            "identity represented by section_id: use field for direct confocal "
            "images and section for slide/WSI inputs (default: section)"
        ),
    )
    ap.add_argument(
        "--endpoint-csv", default=None,
        help="optional endpoint_areas.csv; joins exact output_key/region matches and "
             "aggregates only evaluated rows",
    )
    ap.add_argument(
        "--endpoint-spec", default=None,
        help="endpoint JSON used to recompute its numerator/bare companion fraction; "
             "requires --endpoint-csv",
    )
    args = ap.parse_args()

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.run_summary))
    os.makedirs(outdir, exist_ok=True)

    prefix = "endpoint_" if args.endpoint_csv else ""
    mouse_path = os.path.join(outdir, prefix + "mouse_level_summary.csv")
    group_path = os.path.join(outdir, prefix + "group_level_summary.csv")
    for prior in (mouse_path, group_path):
        stale = quarantine_if_exists(prior)
        if stale:
            print(f"Quarantined prior canonical output -> {stale}")

    if not os.path.isfile(args.run_summary):
        sys.exit(f"ERROR: not found: {args.run_summary}")
    if is_quarantined_summary(args.run_summary):
        sys.exit(
            "ERROR: refusing to aggregate a REJECTED or STALE diagnostic table "
            "to mouse/group level"
        )

    header, rows = read_rows(args.run_summary)
    if not rows:
        sys.exit("ERROR: no data rows in run_summary.csv")
    validate_rows(header, rows)
    if args.endpoint_csv:
        if not os.path.isfile(args.endpoint_csv):
            sys.exit(f"ERROR: not found: {args.endpoint_csv}")
        header, rows = merge_endpoint_rows(header, rows, args.endpoint_csv)
        validate_rows(header, rows)

    endpoint_relation = None
    if args.endpoint_spec:
        if not args.endpoint_csv:
            sys.exit("ERROR: --endpoint-spec requires --endpoint-csv")
        if not os.path.isfile(args.endpoint_spec):
            sys.exit(f"ERROR: not found: {args.endpoint_spec}")
        with open(args.endpoint_spec, encoding="utf-8-sig") as handle:
            spec = json.load(handle)
        output = spec.get("output") or {}
        required_outputs = [
            "area_column", "bare_area_column", "numerator_fraction_of_bare_column"
        ]
        missing_outputs = [name for name in required_outputs if not output.get(name)]
        if missing_outputs:
            sys.exit("ERROR: endpoint spec output is missing: " + ", ".join(missing_outputs))
        endpoint_relation = {name: output[name] for name in required_outputs}

    mouse_rows = aggregate_mice(
        header,
        rows,
        endpoint_relation=endpoint_relation,
        sampling_unit=args.sampling_unit,
    )
    grp_rows = group_stats(mouse_rows)

    try:
        write_csv(mouse_path, mouse_rows)
        write_csv(group_path, grp_rows)
    except OSError:
        # The two CSVs are one publication set. If the second atomic replace
        # fails, do not leave the first looking like a complete current run.
        for partial in (mouse_path, group_path):
            quarantine_if_exists(partial)
        raise

    n_mice = len({(r["mouse_id"], r["genotype"], r["condition"]) for r in mouse_rows})
    print(f"Read {len(rows)} region rows.")
    print(f"Wrote {len(mouse_rows)} mouse x panel rows -> {mouse_path}")
    print(f"Wrote {len(grp_rows)} group x metric rows -> {group_path}")
    print(f"Distinct animals: {n_mice}  (this is your statistical n, split by group)")
    print("Reminder: compare groups on the mouse-level metrics; n = mice.")
    print(f"Sampling units are reported as n_{args.sampling_unit}s.")


if __name__ == "__main__":
    main()
