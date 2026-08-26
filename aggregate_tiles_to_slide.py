#!/usr/bin/env python3
"""
aggregate_tiles_to_slide.py
=====================================================================
STAGE 3 of the whole-slide (WSI) route: roll per-TILE results up to the
SLIDE level, and prove that nothing was silently lost on the way.

Pipeline position
-----------------
  Stage 1  qupath_wsi_tile_export.groovy  -> tiles + tile_manifest.csv
  Stage 2  IF_Quant_Pipeline.groovy       -> run_summary.csv (one row per tile)
  Stage 3  THIS SCRIPT                    -> slide_level_summary.csv
  Stage 4  aggregate_to_mouse.py          -> mouse_level / group_level

Why this exists
---------------
Tiles overlap by a halo so objects at a core boundary are fully imaged. Stage 1
writes a per-tile `_RoiSet.zip` restricting every measurement to the tile CORE
intersected with tissue, so AREA endpoints sum exactly with no double counting.
But three things still need checking, and none of them are visible in
run_summary.csv alone:

  1. COVERAGE. A tile that failed in Stage 2 vanishes from run_summary.csv.
     Its tissue area disappears from the denominator and its KRT5 pod area from
     the numerator. The slide still produces a perfectly plausible number.
     This script reconciles run_summary.csv against tile_manifest.csv and
     REFUSES to emit a slide row when tiles are missing.
  2. AREA RECONCILIATION. sum(region_area_um2) over tiles must equal the global
     tissue area QuPath measured in Stage 1. A mismatch means the ROIs did not
     do what we think they did.
  3. SEAM COUNT INFLATION. ImageJ's ParticleAnalyzer CLIPS nuclei at the ROI
     edge rather than excluding them, so one nucleus straddling a core boundary
     can appear as a fragment in each neighbour. AREA endpoints are unaffected;
     CELL COUNTS can be inflated by a few percent. This script measures that
     inflation from the per-cell centroids and reports it. Correction is
     opt-in -- see --seam-counts.

Pooling reuses aggregate_to_mouse.classify_columns() so slide-level and
mouse-level pooling can never drift apart.

Usage
-----
  python3 aggregate_tiles_to_slide.py \\
      --stage1-manifest D:/wsi_stage1/stage1_manifest.json \\
      --slide-root      D:/wsi_stage1 \\
      --outdir          D:/wsi_stage1/stats

  # then
  python3 aggregate_to_mouse.py D:/wsi_stage1/stats/slide_level_summary.csv

Each slide folder under --slide-root is expected to look like:
  <slide>/tile_manifest.csv
  <slide>/analysis*/run_summary.csv     (one, or several if Stage 2 was sharded)

No third-party dependencies (standard library only).
=====================================================================
"""
import argparse
import csv
import glob
import hashlib
import io
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from collections import defaultdict

# Reuse the validated pooling classification so slide-level and mouse-level
# aggregation can never disagree about what is a sum vs a derived quantity.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from aggregate_to_mouse import (
        AggregationAuditError,
        STAGE3_AUDIT_FILENAME,
        _num,
        artifact_descriptor,
        build_aggregation_audit,
        classify_columns,
        marker_of,
        repository_python_code_closure,
        verify_artifact_descriptor,
        write_json_atomic,
    )
except ImportError:  # pragma: no cover
    sys.exit("ERROR: aggregate_to_mouse.py must sit beside this script "
             "(its column classification is reused so pooling cannot drift).")

try:
    from ifquant.stage2_index import (
        Stage2IndexError,
        additive_column_marker_ids,
        canonical_marker_id,
        sha256_file,
        validate_stage2_index,
    )
except ImportError:  # pragma: no cover
    sys.exit("ERROR: the ifquant package must be importable beside this script")

# Identity columns carried through to the slide row.
CARRY = ["mouse_id", "genotype", "condition", "panel"]


def read_csv_rows(path):
    header, rows, _, _ = read_csv_rows_snapshot(path)
    return header, rows


def read_csv_rows_snapshot(path):
    """Parse and hash one immutable byte snapshot of a CSV artifact."""
    payload = Path(path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        return [], [], digest, len(payload)
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError(f"{path} contains duplicate CSV header columns")
    rows = []
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(
                f"{path} row {row_number} has more fields than its header"
            )
        if not all(value is None or isinstance(value, str) for value in row.values()):
            raise ValueError(f"{path} row {row_number} contains a non-text CSV value")
        if any((value or "").strip() for value in row.values()):
            rows.append(row)
    return reader.fieldnames, rows, digest, len(payload)


def numeric_integrity_problems(rows, columns, declared_markers_by_panel=None):
    """Reject missing, malformed, or non-finite values in additive measures.

    Indexed WSI runs are checked panel by panel.  A wholly blank marker column
    is structurally unavailable only when its marker is absent from that
    panel's declared channel signature.  A declared acquisition channel alone
    does not imply a particular measurement role, so this check applies only
    to additive marker columns that the Stage 2 schema actually emits.
    """
    problems = []
    if declared_markers_by_panel is None:
        panel_groups = [(None, rows, None)]
    else:
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row.get("panel") or "").strip()].append(row)
        panel_groups = []
        for panel, panel_rows in sorted(grouped.items()):
            declared = declared_markers_by_panel.get(panel)
            if declared is None:
                problems.append(
                    f"panel {panel!r} has no marker contract in the validated "
                    "Stage 2 channel signatures"
                )
                declared = set()
            panel_groups.append((panel, panel_rows, set(declared)))

    for panel, panel_rows, declared_markers in panel_groups:
        for column in columns:
            missing, invalid = 0, []
            numeric = []
            dependencies = additive_column_marker_ids(column)
            for index, row in enumerate(panel_rows, start=1):
                raw = row.get(column)
                token = "" if raw is None else str(raw).strip()
                if token == "" or token.upper() in {"NA", "N/A"}:
                    missing += 1
                    continue
                value = _num(token)
                if value is None:
                    invalid.append((index, token))
                else:
                    numeric.append(value)
            context = f" in panel {panel!r}" if panel is not None else ""
            if invalid:
                problems.append(
                    f"additive column {column!r}{context} contains {len(invalid)} "
                    f"invalid/non-finite value(s), examples={invalid[:3]}")
            if missing == len(panel_rows):
                required_by_panel = (
                    declared_markers is not None
                    and bool(dependencies)
                    and dependencies.issubset(declared_markers)
                )
                if column in {"region_area_um2", "n_nuclei"} or required_by_panel:
                    reason = (
                        " despite every referenced marker being declared by the "
                        "indexed panel signature"
                        if required_by_panel else ""
                    )
                    problems.append(
                        f"additive column {column!r}{context} is missing for all rows"
                        f"{reason}"
                    )
                # A union schema may include marker measurements from another
                # panel.  The explicit signature, rather than blankness alone,
                # is the authority for treating those fields as inapplicable.
                continue
            if missing:
                problems.append(
                    f"additive column {column!r}{context} is missing in "
                    f"{missing}/{len(panel_rows)} row(s); partial measurements "
                    "cannot be coerced to zero")
            if column == "region_area_um2":
                nonpositive = [value for value in numeric if value <= 0]
                if nonpositive:
                    problems.append(
                        f"region_area_um2{context} must be positive for every row; "
                        f"found {len(nonpositive)} non-positive value(s)")
            else:
                negative = [value for value in numeric if value < 0]
                if negative:
                    problems.append(
                        f"additive column {column!r}{context} must be non-negative; "
                        f"found {len(negative)} negative value(s)")
    return problems


def find_run_summaries_legacy(slide_dir):
    """Diagnostic-only recursive discovery retained for investigating old runs."""
    hits = sorted(glob.glob(os.path.join(slide_dir, "**", "run_summary.csv"), recursive=True))
    return [h for h in hits if os.path.getsize(h) > 0]


def load_stage1_manifest(path):
    payload = Path(path).read_bytes()
    return (
        json.loads(payload.decode("utf-8-sig")),
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )


def read_stage2_manifests(analysis_dirs):
    """
    Collect Stage 2 per-image failures from run_manifest.json.

    The engine catches every per-image Throwable, records it, and carries on;
    outputs are all written before the terminal failRun, and the process exits 1
    whenever ANY image failed. So a non-zero exit does NOT mean "no results",
    and run_summary.csv alone cannot tell you a tile is missing -- the row is
    simply absent. Reading the manifest turns that into a precise reason.
    """
    total_fail, reasons, statuses, snapshots = 0, [], [], []
    for d in analysis_dirs:
        p = os.path.join(d, "run_manifest.json")
        if not os.path.isfile(p):
            continue
        try:
            payload = Path(p).read_bytes()
            man = json.loads(payload.decode("utf-8-sig"))
        except Exception as exc:                       # noqa: BLE001
            reasons.append(f"{p}: unreadable ({exc})")
            continue
        snapshots.append(
            (p, hashlib.sha256(payload).hexdigest(), len(payload))
        )
        statuses.append(str(man.get("status", "unknown")))
        total_fail += int(man.get("failure_count", 0) or 0)
        for img in (man.get("images") or []):
            err = img.get("error")
            if err:
                reasons.append(f"{img.get('image', '<unknown>')}: {err}")
    return total_fail, reasons, statuses, snapshots


# --------------------------------------------------------------------------
# Seam diagnostics
# --------------------------------------------------------------------------
def collect_cell_centroids(analysis_dirs, manifest_by_section):
    """
    Read every per-tile __cells.csv and return global centroids in microns.

    The Fiji engine writes centroid_x_um / centroid_y_um in TILE-LOCAL
    calibrated coordinates, relative to the exported tile origin. tile_manifest
    gives that origin in full-resolution slide pixels, so
        global_um = (export_origin_px * pixel_size_um) + local_um

    Only the analysis folders that actually contributed a run_summary.csv are
    scanned. Globbing the whole slide folder would also pick up abandoned or
    partial Stage 2 output directories and count their cells a second time.
    """
    out = []
    missing_cells = 0
    paths = []
    snapshots = []
    for d in analysis_dirs:
        # A declared Stage 2 output writes cells one directory below its analysis
        # root. Never recurse into abandoned retries nested beneath that root.
        paths.extend(glob.glob(os.path.join(d, "*", "*__cells.csv")))
    for cells_path in sorted(set(paths)):
        # output folder name is <mouse>_<condition>_<panel>_<section_id>[__hash]
        folder = os.path.basename(os.path.dirname(cells_path))
        section = None
        for sec in manifest_by_section:
            if folder.endswith("_" + sec) or ("_" + sec + "__") in folder:
                section = sec
                break
        if section is None:
            missing_cells += 1
            continue
        tm = manifest_by_section[section]
        px = float(tm["pixel_size_um"])
        ox = float(tm["export_x"]) * px
        oy = float(tm["export_y"]) * px
        _, rows, digest, size_bytes = read_csv_rows_snapshot(cells_path)
        snapshots.append((cells_path, digest, size_bytes))
        for r in rows:
            cx, cy = _num(r.get("centroid_x_um")), _num(r.get("centroid_y_um"))
            if cx is None or cy is None:
                continue
            out.append((cx + ox, cy + oy, section))
    return out, missing_cells, snapshots


def estimate_seam_duplicates(centroids, merge_dist_um):
    """
    Count cells from DIFFERENT tiles whose global centroids are closer than
    merge_dist_um. Those are the same physical nucleus clipped by a shared core
    boundary. Uses a uniform grid so it stays linear in cell count.
    """
    if merge_dist_um <= 0 or not centroids:
        return 0, 0
    cell = merge_dist_um
    grid = defaultdict(list)
    for i, (x, y, sec) in enumerate(centroids):
        grid[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(i)

    seen_pair = set()
    dup = 0
    d2 = merge_dist_um * merge_dist_um
    for (gx, gy), idxs in grid.items():
        neigh = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neigh.extend(grid.get((gx + dx, gy + dy), ()))
        for i in idxs:
            xi, yi, si = centroids[i]
            for j in neigh:
                if j <= i:
                    continue
                xj, yj, sj = centroids[j]
                if si == sj:            # same tile -> genuinely two nuclei
                    continue
                if (xi - xj) ** 2 + (yi - yj) ** 2 <= d2:
                    key = (i, j)
                    if key not in seen_pair:
                        seen_pair.add(key)
                        dup += 1
    return dup, len(centroids)


# --------------------------------------------------------------------------
def aggregate_slide(slide_name, manifest_rows, header, tile_rows, stage1_slide,
                    seam_dup, n_cells, stage2_failures=0, stage2_reasons=(),
                    stage2_provenance=None, integrity_problems=(),
                    declared_markers_by_panel=None):
    """Sum tile rows into one slide row, after reconciling coverage."""
    cats = classify_columns(header)

    # --- partition awareness ------------------------------------------------
    # When Stage 1 partitions each tile into damaged/intact parenchyma the engine
    # emits TWO rows per tile. The endpoint denominator is then the DAMAGED area
    # only (KRT5+ area / damaged alveolar area), not the whole tile.
    def _region(r):
        return (r.get("region") or "").strip().lower()

    damaged_rows = [r for r in tile_rows if "damaged" in _region(r)]
    intact_rows = [r for r in tile_rows if "intact" in _region(r)]
    partitioned = bool(damaged_rows or intact_rows)
    endpoint_rows = damaged_rows if partitioned else tile_rows

    expected = {r["section_id"] for r in manifest_rows}
    got = {(r.get("section_id") or "").strip() for r in tile_rows}
    missing = sorted(expected - got)
    extra = sorted(got - expected)

    problems = list(integrity_problems)
    problems.extend(numeric_integrity_problems(
        tile_rows, cats["sum_cols"], declared_markers_by_panel
    ))
    natural_keys = []
    for row in tile_rows:
        natural_keys.append(tuple((row.get(c) or "").strip()
                                  for c in ("section_id", "region", "panel")))
    duplicate_keys = []
    seen_keys = set()
    for key in natural_keys:
        if key in seen_keys and key not in duplicate_keys:
            duplicate_keys.append(key)
        seen_keys.add(key)
    if duplicate_keys:
        problems.append(
            f"{len(duplicate_keys)} duplicate section-region-panel analytical identity "
            f"key(s) detected: {duplicate_keys[:4]}")
    if missing:
        detail = ""
        if stage2_reasons:
            detail = " Stage 2 reported: " + " || ".join(stage2_reasons[:4])
        problems.append(
            f"{len(missing)} tile(s) in tile_manifest.csv have NO run_summary row "
            f"(their tissue area and KRT5 pod area are missing from this slide): "
            + ", ".join(missing[:8]) + ("..." if len(missing) > 8 else "") + detail)
    if stage2_failures and not missing:
        problems.append(
            f"Stage 2 run_manifest.json reports {stage2_failures} per-image failure(s) even "
            "though every tile has a summary row. Inspect before trusting this slide.")
    if extra:
        problems.append(f"{len(extra)} run_summary row(s) are not in tile_manifest.csv: "
                        + ", ".join(extra[:8]))
    if stage1_slide is not None and not stage1_slide.get("coverage_complete", True):
        problems.append("Stage 1 recorded coverage_complete=false "
                        "(IFQ_WSI_MAX_TILES_PER_SLIDE was set) -- this is a smoke test, not an analysis.")
    if stage1_slide is not None and stage1_slide.get("dry_run", False):
        problems.append("Stage 1 recorded dry_run=true -- no tiles were actually exported.")
    if stage1_slide is not None:
        skipped_low_tissue = stage1_slide.get("n_skipped_low_tissue")
        if skipped_low_tissue is None:
            problems.append(
                "Stage 1 did not record n_skipped_low_tissue; exhaustive candidate coverage "
                "cannot be established.")
        else:
            try:
                skipped_low_tissue = int(skipped_low_tissue)
            except (TypeError, ValueError):
                problems.append("Stage 1 n_skipped_low_tissue is not an integer.")
            else:
                if skipped_low_tissue > 0:
                    problems.append(
                        f"Stage 1 omitted {skipped_low_tissue} low-tissue candidate tile(s) "
                        "before tile_manifest.csv; their identities/areas are unavailable.")

    rec = {"slide": slide_name, "aggregation_contract_version": "2.0.0"}
    provenance = stage2_provenance or {}
    rec["stage2_source_mode"] = provenance.get("stage2_source_mode", "unknown")
    rec["stage2_index_sha256"] = provenance.get("stage2_index_sha256", "")
    rec["stage2_index_schema_version"] = provenance.get(
        "stage2_index_schema_version", "")
    rec["stage1_manifest_sha256"] = provenance.get("stage1_manifest_sha256", "")
    rec["stage1_profile_sha256"] = provenance.get("stage1_profile_sha256", "")
    rec["stage1_script_sha256"] = provenance.get("stage1_script_sha256", "")
    rec["stage1_source_metadata_sha256"] = provenance.get(
        "stage1_source_metadata_sha256", "")
    rec["source_package_sha256"] = provenance.get(
        "source_package_sha256", "")
    rec["tile_candidate_manifest_sha256"] = provenance.get(
        "tile_candidate_manifest_sha256", "")
    rec["tile_manifest_sha256"] = provenance.get("tile_manifest_sha256", "")
    rec["stage2_script_sha256"] = provenance.get("stage2_script_sha256", "")
    rec["resolved_config_sha256"] = provenance.get("resolved_config_sha256", "")
    rec["configuration_artifact_set_sha256"] = provenance.get(
        "configuration_artifact_set_sha256", "")
    rec["runtime_profile_sha256"] = provenance.get("runtime_profile_sha256", "")
    rec["parameter_set_sha256"] = provenance.get("parameter_set_sha256", "")
    rec["measurement_profile_sha256"] = provenance.get("measurement_profile_sha256", "")
    rec["declared_channel_map_sha256"] = provenance.get(
        "declared_channel_map_sha256", "")
    rec["ordered_channel_signature"] = provenance.get("ordered_channel_signature", "")
    rec["channel_signature_authority"] = provenance.get(
        "channel_signature_authority", "unverified")
    for c in CARRY:
        vals = {(r.get(c) or "").strip() for r in tile_rows if (r.get(c) or "").strip()}
        if len(vals) > 1:
            problems.append(f"tiles disagree on '{c}': {sorted(vals)}")
        rec[c] = sorted(vals)[0] if vals else "NA"

    # aggregate_to_mouse keys rows on (image|output_key, region, section_id, panel).
    # One row per slide, so give it stable slide-scoped identity columns.
    rec["image"] = slide_name
    rec["section_id"] = slide_name
    rec["region"] = ((endpoint_rows[0].get("region") if endpoint_rows else None)
                     or (tile_rows[0].get("region") if tile_rows else None)
                     or "parenchyma_core")

    # Sums are taken over the ENDPOINT rows. When partitioned that is the damaged
    # parenchyma only, so region_area_um2 -- and therefore every fraction derived
    # from it here and again in aggregate_to_mouse.py -- uses the damaged area as
    # the denominator, which is the endpoint definition.
    applicable_sum_cols = [
        c for c in cats["sum_cols"]
        if any(
            str(r.get(c) or "").strip().upper() not in {"", "NA", "N/A"}
            for r in tile_rows
        )
    ]
    sums = {}
    for c in applicable_sum_cols:
        vals = [_num(r.get(c)) for r in endpoint_rows]
        vals = [v for v in vals if v is not None]
        if vals:
            sums[c] = math.fsum(vals)
    for c, v in sums.items():
        rec[c] = v

    total_area = sums.get("region_area_um2", 0.0)

    # Preserve both additive compartment areas and the intact-compartment pod
    # area. Stage 4 recognizes these explicit columns, pools them independently,
    # and recomputes their fractions at mouse level; it never averages these
    # per-slide fractions or folds intact area into the damaged endpoint
    # denominator.
    rec["partitioned"] = "true" if partitioned else "false"
    if partitioned:
        def _area(rows):
            return sum(v for v in (_num(r.get("region_area_um2")) for r in rows) if v is not None)

        dmg_area, int_area = _area(damaged_rows), _area(intact_rows)
        rec["damaged_area_um2"] = dmg_area
        rec["intact_area_um2"] = int_area
        whole = dmg_area + int_area
        rec["damaged_fraction_of_parenchyma"] = (dmg_area / whole) if whole > 0 else 0.0
        # KRT5 in the INTACT compartment is a QC readout, not the endpoint:
        # dysplastic pods should sit in damaged parenchyma. A large value here
        # means the damage mask, the KRT5 threshold, or both are wrong.
        for c in (column for column in cats["pod_area"] if column in sums):
            m = marker_of(c, "_pod_area_um2")
            iv = sum(v for v in (_num(r.get(c)) for r in intact_rows) if v is not None)
            rec[f"{m}_pod_area_um2_in_intact"] = iv
            rec[f"{m}_pod_area_frac_of_intact"] = (iv / int_area) if int_area > 0 else 0.0

    # Recompute every derived quantity from POOLED numerators. Never average
    # per-tile fractions: tiles differ in tissue area.
    for c in (column for column in cats["pod_area"] if column in sums):
        m = marker_of(c, "_pod_area_um2")
        rec[f"{m}_pod_area_frac"] = (sums[c] / total_area) if total_area > 0 else 0.0
        npods = sums.get(f"{m}_n_pods", 0.0)
        rec[f"{m}_mean_pod_area_um2"] = (sums[c] / npods) if npods > 0 else 0.0
    for c in (column for column in cats["positive_area"] if column in sums):
        m = marker_of(c, "_positive_area_um2")
        rec[f"{m}_positive_area_frac"] = (sums[c] / total_area) if total_area > 0 else 0.0

    # ---- QC / provenance ------------------------------------------------
    # Count TILES, not rows: a partitioned tile contributes two rows.
    n_tiles = len({(r.get("section_id") or "").strip() for r in tile_rows})
    rec["n_tiles_expected"] = len(manifest_rows)
    rec["n_tiles_analyzed"] = n_tiles
    rec["n_summary_rows"] = len(tile_rows)
    rec["n_tiles_missing"] = len(missing)
    rec["tile_coverage_fraction"] = (n_tiles / len(manifest_rows)) if manifest_rows else 0.0

    # Reconciliation uses EVERY row. damaged + intact partition the tile core, so
    # their sum must still equal the Stage 1 core area even when the endpoint
    # denominator above is only the damaged part.
    all_area = math.fsum(
        v for v in (_num(r.get("region_area_um2")) for r in tile_rows) if v is not None)
    # Prefer the rasterised area: the engine measures ROI pixels, so that is the
    # like-for-like comparison. Fall back for manifests written before it existed.
    key = ("core_raster_area_um2" if manifest_rows and "core_raster_area_um2" in manifest_rows[0]
           else "core_tissue_area_um2")
    manifest_core_values = []
    for row_index, row in enumerate(manifest_rows, start=1):
        try:
            value = float(row[key])
        except (KeyError, TypeError, ValueError):
            problems.append(
                f"tile manifest {key} row {row_index} is missing or nonnumeric")
            continue
        if not math.isfinite(value) or value <= 0:
            problems.append(
                f"tile manifest {key} row {row_index} must be finite and positive")
            continue
        manifest_core_values.append(value)
    manifest_core_um2 = math.fsum(manifest_core_values)
    rec["stage1_core_tissue_area_um2"] = manifest_core_um2
    rec["stage1_area_column"] = key
    rec["stage2_region_area_um2"] = all_area
    total_area_for_reconcile = all_area
    rec["tissue_area_rel_diff"] = (
        abs(total_area_for_reconcile - manifest_core_um2) / manifest_core_um2
        if manifest_core_um2 > 0 else 0.0)
    if stage1_slide is not None:
        rec["stage1_slide_tissue_mm2"] = stage1_slide.get("tissue_area_mm2")
        rec["stage1_tissue_threshold_otsu"] = stage1_slide.get("tissue_threshold_otsu")
        rec["source_vsi"] = stage1_slide.get("source_vsi")
        rec["series_index"] = stage1_slide.get("series_index")

    rec["seam_duplicate_cell_pairs"] = seam_dup
    rec["n_cells_scanned"] = n_cells
    rec["seam_duplicate_fraction"] = (seam_dup / n_cells) if n_cells else 0.0

    if rec["tissue_area_rel_diff"] > 0.01:
        problems.append(
            f"Stage 2 tissue area ({total_area:.0f} um2) differs from the Stage 1 core "
            f"tissue area ({manifest_core_um2:.0f} um2) by "
            f"{rec['tissue_area_rel_diff'] * 100:.2f}%. The per-tile _RoiSet.zip may not "
            "have been picked up -- check that each tile has a companion "
            "'<stem>.ome_RoiSet.zip' beside it.")

    rec["qc_status"] = "ok" if not problems else "PROBLEM"
    rec["qc_notes"] = " | ".join(problems)
    return rec, problems


def write_csv(path, rows):
    path = os.path.abspath(path)
    descriptor, temporary = tempfile.mkstemp(
        dir=os.path.dirname(path),
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as fh:
            descriptor = -1
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
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def quarantine_if_exists(path):
    """Move a stale canonical output aside without destroying its bytes."""
    if not os.path.isfile(path):
        return None
    digest = sha256_file(Path(path))[:12]
    stem, extension = os.path.splitext(path)
    stale = f"{stem}.STALE.{digest}{extension}"
    os.replace(path, stale)
    return stale


def main():
    ap = argparse.ArgumentParser(
        description="Roll per-tile Fiji results up to slide level, with coverage reconciliation.")
    ap.add_argument("--slide-root", required=True,
                    help="Stage 1 output root (contains one folder per slide)")
    ap.add_argument("--stage1-manifest", default=None,
                    help="stage1_manifest.json (default: <slide-root>/stage1_manifest.json)")
    ap.add_argument("--outdir", default=None, help="output folder (default: <slide-root>/stats)")
    ap.add_argument(
        "--stage2-script",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "IF_Quant_Pipeline.groovy"),
        help="exact Fiji engine used by Stage 2 (hash must match every run index)")
    ap.add_argument("--stage2-index-name", default="stage2_run_index.json",
                    help="per-slide index filename (default: stage2_run_index.json)")
    ap.add_argument("--seam-merge-um", type=float, default=4.0,
                    help="two centroids from different tiles closer than this are treated as "
                         "one nucleus clipped at a seam (default 4.0 um, ~one nuclear radius)")
    ap.add_argument("--seam-counts", choices=["report", "off"], default="report",
                    help="'report' measures seam count inflation and records it (default); "
                         "'off' skips reading per-cell CSVs. Counts are NOT silently altered.")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="deprecated compatibility flag; rejected rows remain diagnostic-only")
    ap.add_argument(
        "--legacy-recursive-discovery", action="store_true",
        help="inspect recursively discovered legacy summaries, but only emit a REJECTED "
             "diagnostic table; this can never produce analytical output")
    args = ap.parse_args()

    root = os.path.abspath(args.slide_root)
    if not os.path.isdir(root):
        sys.exit(f"ERROR: --slide-root not found: {root}")
    outdir = args.outdir or os.path.join(root, "stats")
    os.makedirs(outdir, exist_ok=True)
    out_path = os.path.join(outdir, "slide_level_summary.csv")
    audit_path = os.path.join(outdir, STAGE3_AUDIT_FILENAME)
    for prior in (out_path, audit_path):
        stale = quarantine_if_exists(prior)
        if stale:
            print(f"Quarantined prior Stage 3 publication artifact -> {stale}")

    stage3_script_path = os.path.abspath(__file__)
    aggregation_library_path = os.path.join(
        os.path.dirname(stage3_script_path), "aggregate_to_mouse.py"
    )
    try:
        code_artifacts, tracked_snapshots = repository_python_code_closure(
            [
                ("stage3_aggregator", stage3_script_path),
                ("aggregation_library", aggregation_library_path),
            ],
            audit_path,
        )
    except (OSError, AggregationAuditError, ValueError) as exc:
        sys.exit(f"ERROR: Stage 3 code-provenance closure failed: {exc}")
    input_artifacts = []
    validated_index_checks = []

    manifest_path = args.stage1_manifest or os.path.join(root, "stage1_manifest.json")
    if not os.path.isfile(manifest_path):
        stale = quarantine_if_exists(out_path)
        if stale:
            print(f"Quarantined stale analytical output -> {stale}")
        sys.exit(f"ERROR: Stage 1 manifest is required: {manifest_path}")
    try:
        stage1, stage1_snapshot_sha256, stage1_snapshot_size = (
            load_stage1_manifest(manifest_path)
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        stale = quarantine_if_exists(out_path)
        if stale:
            print(f"Quarantined stale analytical output -> {stale}")
        sys.exit(f"ERROR: Stage 1 manifest is unreadable: {manifest_path}: {exc}")
    if not isinstance(stage1, dict):
        stale = quarantine_if_exists(out_path)
        if stale:
            print(f"Quarantined stale analytical output -> {stale}")
        sys.exit(f"ERROR: Stage 1 manifest root must be a JSON object: {manifest_path}")
    stage1_slides = stage1.get("slides")
    if not isinstance(stage1_slides, list) or not stage1_slides:
        stale = quarantine_if_exists(out_path)
        if stale:
            print(f"Quarantined stale analytical output -> {stale}")
        sys.exit("ERROR: Stage 1 manifest must declare a non-empty slides array")
    declared_stems = [
        str(slide.get("slide_stem") or "").strip()
        for slide in stage1_slides if isinstance(slide, dict)
    ]
    if (len(declared_stems) != len(stage1_slides)
            or any(not stem or os.path.basename(stem) != stem or stem in {".", ".."}
                   for stem in declared_stems)
            or len(declared_stems) != len(set(declared_stems))):
        stale = quarantine_if_exists(out_path)
        if stale:
            print(f"Quarantined stale analytical output -> {stale}")
        sys.exit("ERROR: Stage 1 slide_stem values must be unique, non-empty directory names")
    stage1_by_stem = {
        stem: slide for stem, slide in zip(declared_stems, stage1_slides)
    }
    stage1_descriptor = artifact_descriptor(
        manifest_path,
        "stage1_manifest",
        audit_path,
        digest=stage1_snapshot_sha256,
        size_bytes=stage1_snapshot_size,
    )
    input_artifacts.append(stage1_descriptor)
    tracked_snapshots.append((stage1_descriptor, os.path.abspath(manifest_path)))
    stage2_script = os.path.abspath(args.stage2_script)
    if not os.path.isfile(stage2_script):
        stale = quarantine_if_exists(out_path)
        if stale:
            print(f"Quarantined stale analytical output -> {stale}")
        sys.exit(f"ERROR: --stage2-script not found: {stage2_script}")
    stage2_script_descriptor = artifact_descriptor(
        stage2_script, "stage2_engine", audit_path
    )
    input_artifacts.append(stage2_script_descriptor)
    tracked_snapshots.append((stage2_script_descriptor, stage2_script))
    if args.allow_incomplete:
        print("WARNING: --allow-incomplete is deprecated. Failed rows are written only to "
              "slide_level_summary.REJECTED.csv and never to the analytical filename.")

    slide_rows, all_problems = [], []
    actual_manifest_stems = {
        entry for entry in os.listdir(root)
        if os.path.isfile(os.path.join(root, entry, "tile_manifest.csv"))
    }
    declared_manifest_stems = set(stage1_by_stem)
    for entry in sorted(declared_manifest_stems - actual_manifest_stems):
        msg = (f"{entry}: Stage 1 declares this slide, but its directory or "
               "tile_manifest.csv is missing")
        print("ERROR: " + msg)
        all_problems.append(msg)
    for entry in sorted(actual_manifest_stems - declared_manifest_stems):
        msg = f"{entry}: tile_manifest.csv exists, but Stage 1 does not declare this slide"
        print("ERROR: " + msg)
        all_problems.append(msg)

    for entry in sorted(declared_manifest_stems & actual_manifest_stems):
        slide_dir = os.path.join(root, entry)
        tm_path = os.path.join(slide_dir, "tile_manifest.csv")
        if not os.path.isfile(tm_path):
            continue
        try:
            (
                manifest_header,
                manifest_rows,
                manifest_snapshot_sha256,
                manifest_snapshot_size,
            ) = (
                read_csv_rows_snapshot(tm_path)
            )
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            msg = f"{entry}: tile_manifest.csv is unreadable or malformed: {exc}"
            print("ERROR: " + msg)
            all_problems.append(msg)
            continue
        tile_manifest_descriptor = artifact_descriptor(
            tm_path,
            f"tile_manifest:{entry}",
            audit_path,
            digest=manifest_snapshot_sha256,
            size_bytes=manifest_snapshot_size,
        )
        input_artifacts.append(tile_manifest_descriptor)
        tracked_snapshots.append(
            (tile_manifest_descriptor, os.path.abspath(tm_path))
        )
        if "section_id" not in manifest_header:
            msg = f"{entry}: tile_manifest.csv is missing required section_id"
            print("ERROR: " + msg)
            all_problems.append(msg)
            continue
        if not manifest_rows:
            msg = f"{entry}: tile_manifest.csv has no rows"
            print("ERROR: " + msg)
            all_problems.append(msg)
            continue

        integrity_problems = []
        if args.legacy_recursive_discovery:
            summaries = find_run_summaries_legacy(slide_dir)
            analysis_dirs = sorted({os.path.dirname(s) for s in summaries})
            stage2_provenance = {
                "stage2_source_mode": "legacy_recursive_diagnostic",
                "channel_signature_authority": "unverified",
            }
            integrity_problems.append(
                "legacy recursive Stage 2 discovery is ambiguous and diagnostic-only; "
                "build an explicit hashed stage2_run_index.json")
            declared_markers_by_panel = None
        else:
            index_path = os.path.join(slide_dir, args.stage2_index_name)
            if not os.path.isfile(index_path):
                msg = (f"{entry}: missing required Stage 2 index {index_path}. "
                       "No run_summary.csv was guessed.")
                print("ERROR: " + msg)
                all_problems.append(msg)
                continue
            try:
                validated_index = validate_stage2_index(
                    Path(index_path),
                    slide_dir=Path(slide_dir),
                    stage1_manifest=Path(manifest_path),
                    stage2_script=Path(stage2_script),
                )
            except (OSError, Stage2IndexError) as exc:
                msg = f"{entry}: invalid Stage 2 index: {exc}"
                print("ERROR: " + msg)
                all_problems.append(msg)
                continue
            summaries = [str(path) for path in validated_index.summary_paths]
            analysis_dirs = [str(path) for path in validated_index.analysis_dirs]
            index_document = validated_index.document
            index_descriptor = artifact_descriptor(
                index_path,
                f"stage2_index:{entry}",
                audit_path,
            )
            input_artifacts.append(index_descriptor)
            tracked_snapshots.append(
                (index_descriptor, os.path.abspath(index_path))
            )
            validated_index_checks.append(
                (
                    index_path,
                    slide_dir,
                    index_document["index_sha256"],
                    index_descriptor["sha256"],
                )
            )
            if stage1_snapshot_sha256 != index_document["stage1_manifest_sha256"]:
                msg = (
                    f"{entry}: loaded Stage 1 manifest byte snapshot does not match "
                    "the validated Stage 2 index"
                )
                print("ERROR: " + msg)
                all_problems.append(msg)
                continue
            if manifest_snapshot_sha256 != index_document["tile_manifest"]["sha256"]:
                msg = (
                    f"{entry}: tile_manifest.csv byte snapshot does not match the "
                    "validated Stage 2 index"
                )
                print("ERROR: " + msg)
                all_problems.append(msg)
                continue
            signatures = ";".join(
                f"{item['panel']}={item['signature']}"
                for item in index_document["declared_channel_signatures"])
            declared_markers_by_panel = {
                item["panel"]: {
                    canonical_marker_id(channel["marker"])
                    for channel in item["channels"]
                    if channel["role"].lower() != "nuclear"
                }
                for item in index_document["declared_channel_maps"]
            }
            stage2_provenance = {
                "stage2_source_mode": "explicit_hashed_index",
                "stage2_index_sha256": index_document["index_sha256"],
                "stage2_index_schema_version": index_document["schema_version"],
                "stage1_manifest_sha256": index_document["stage1_manifest_sha256"],
                "stage1_profile_sha256": index_document["stage1_profile_sha256"],
                "stage1_script_sha256": index_document["stage1_script"]["sha256"],
                "stage1_source_metadata_sha256": index_document[
                    "stage1_source_metadata_sha256"
                ],
                "source_package_sha256": index_document[
                    "stage1_source_metadata"
                ]["source_package_sha256"],
                "tile_candidate_manifest_sha256": index_document[
                    "tile_candidate_manifest"
                ]["sha256"],
                "tile_manifest_sha256": index_document["tile_manifest"]["sha256"],
                "stage2_script_sha256": index_document["stage2_script"]["sha256"],
                "resolved_config_sha256": index_document["resolved_config_sha256"],
                "configuration_artifact_set_sha256": index_document[
                    "configuration_artifact_set_sha256"
                ],
                "runtime_profile_sha256": index_document["runtime_profile_sha256"],
                "parameter_set_sha256": index_document["parameter_set_sha256"],
                "measurement_profile_sha256": index_document["measurement_profile_sha256"],
                "declared_channel_map_sha256": index_document[
                    "declared_channel_map_sha256"
                ],
                "ordered_channel_signature": signatures,
                "channel_signature_authority": index_document[
                    "channel_mapping_authority"
                ],
            }
        if not summaries:
            msg = (f"{entry}: no non-empty run_summary.csv found in the selected source. "
                   "Stage 2 has not run, or every tile failed.")
            print("ERROR: " + msg)
            all_problems.append(msg)
            continue

        expected_summary_hashes = {}
        if not args.legacy_recursive_discovery:
            expected_summary_hashes = {
                str((Path(slide_dir) / run["run_summary"]["path"]).resolve()):
                    run["run_summary"]["sha256"]
                for run in index_document["runs"]
            }
        header, tile_rows = [], []
        for summary_number, s in enumerate(summaries, start=1):
            try:
                h, rws, snapshot_sha256, snapshot_size = read_csv_rows_snapshot(s)
            except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                integrity_problems.append(
                    f"run_summary.csv is unreadable or malformed: {s}: {exc}"
                )
                continue
            summary_descriptor = artifact_descriptor(
                s,
                f"stage2_run_summary:{entry}:{summary_number:04d}",
                audit_path,
                digest=snapshot_sha256,
                size_bytes=snapshot_size,
            )
            input_artifacts.append(summary_descriptor)
            tracked_snapshots.append(
                (summary_descriptor, os.path.abspath(s))
            )
            if expected_summary_hashes:
                expected_sha256 = expected_summary_hashes.get(str(Path(s).resolve()))
                if snapshot_sha256 != expected_sha256:
                    integrity_problems.append(
                        f"run_summary.csv byte snapshot changed after Stage 2 index "
                        f"validation: {s}"
                    )
            for c in h:
                if c not in header:
                    header.append(c)
            tile_rows.extend(rws)
        print(f"{entry}: {len(manifest_rows)} tiles expected, {len(tile_rows)} run_summary rows "
              f"from {len(summaries)} Stage 2 output folder(s)")

        (
            stage2_failures,
            stage2_reasons,
            stage2_statuses,
            stage2_manifest_snapshots,
        ) = read_stage2_manifests(analysis_dirs)
        for manifest_number, (path, digest, size_bytes) in enumerate(
                stage2_manifest_snapshots, start=1):
            descriptor = artifact_descriptor(
                path,
                f"stage2_run_manifest:{entry}:{manifest_number:04d}",
                audit_path,
                digest=digest,
                size_bytes=size_bytes,
            )
            input_artifacts.append(descriptor)
            tracked_snapshots.append((descriptor, os.path.abspath(path)))
        if stage2_failures:
            print(f"  Stage 2 reported {stage2_failures} per-image failure(s); "
                  f"status={','.join(sorted(set(stage2_statuses))) or 'unknown'}")

        seam_dup, n_cells = 0, 0
        if args.seam_counts == "report":
            try:
                by_section = {r["section_id"]: r for r in manifest_rows}
                centroids, unmatched, cell_snapshots = collect_cell_centroids(
                    analysis_dirs, by_section
                )
            except (KeyError, OSError, UnicodeError, csv.Error, ValueError) as exc:
                integrity_problems.append(
                    f"cell-centroid seam diagnostics are unreadable or malformed: {exc}"
                )
                centroids, unmatched, cell_snapshots = [], 0, []
            for cell_number, (path, digest, size_bytes) in enumerate(
                    cell_snapshots, start=1):
                descriptor = artifact_descriptor(
                    path,
                    f"stage2_cells:{entry}:{cell_number:06d}",
                    audit_path,
                    digest=digest,
                    size_bytes=size_bytes,
                )
                input_artifacts.append(descriptor)
                tracked_snapshots.append((descriptor, os.path.abspath(path)))
            if unmatched:
                print(f"  note: {unmatched} __cells.csv file(s) could not be matched to a tile")
            seam_dup, n_cells = estimate_seam_duplicates(centroids, args.seam_merge_um)
            if n_cells:
                print(f"  seam diagnostic: {seam_dup} duplicate cell pair(s) of {n_cells} cells "
                      f"({100.0 * seam_dup / n_cells:.2f}%) within {args.seam_merge_um} um "
                      "across a tile boundary")

        rec, problems = aggregate_slide(entry, manifest_rows, header, tile_rows,
                                        stage1_by_stem.get(entry), seam_dup, n_cells,
                                        stage2_failures, stage2_reasons,
                                        stage2_provenance, integrity_problems,
                                        declared_markers_by_panel)
        for p in problems:
            print(f"  QC: {p}")
        all_problems.extend(f"{entry}: {p}" for p in problems)
        slide_rows.append(rec)

    blocking = [r for r in slide_rows if r["qc_status"] != "ok"]
    if all_problems or blocking or not slide_rows:
        rejected_path = os.path.join(outdir, "slide_level_summary.REJECTED.csv")
        if slide_rows:
            dataset_notes = " | ".join(all_problems) or "declared dataset did not pass QC"
            for row in slide_rows:
                row["dataset_qc_status"] = "PROBLEM"
                row["dataset_qc_notes"] = dataset_notes
                if row["qc_status"] == "ok":
                    row["qc_status"] = "PROBLEM"
                    row["qc_notes"] = (
                        "dataset-level rejection: " + dataset_notes
                    )
            write_csv(rejected_path, slide_rows)
        stale = quarantine_if_exists(out_path)
        print("")
        print("REFUSING to write slide_level_summary.csv: the complete declared slide set "
              "did not pass provenance and QC.")
        if slide_rows:
            print(f"Diagnostic rows only -> {rejected_path}")
        if stale:
            print(f"Quarantined stale analytical output -> {stale}")
        print("A valid subset, a recursive discovery result, or an incomplete slide can all "
              "produce plausible numbers; none is published under the analytical filename.")
        sys.exit(2)

    for row in slide_rows:
        row["dataset_qc_status"] = "ok"
        row["dataset_qc_notes"] = ""
    rejected_path = os.path.join(outdir, "slide_level_summary.REJECTED.csv")
    stale_rejected = quarantine_if_exists(rejected_path)
    if stale_rejected:
        print(f"Quarantined stale rejected diagnostic -> {stale_rejected}")
    try:
        # The Stage 2 index validator follows and re-hashes the complete
        # transitive provenance graph (manifests, configuration, parameters,
        # runtime profile, tiles, ROIs and summaries). Re-run it immediately
        # before publication, then verify every direct byte snapshot parsed by
        # this process.
        for (
                index_path,
                slide_dir,
                expected_canonical_index_sha256,
                expected_index_byte_sha256,
        ) in validated_index_checks:
            checked = validate_stage2_index(
                Path(index_path),
                slide_dir=Path(slide_dir),
                stage1_manifest=Path(manifest_path),
                stage2_script=Path(stage2_script),
            )
            if (
                    checked.document["index_sha256"]
                    != expected_canonical_index_sha256
            ):
                raise AggregationAuditError(
                    "Stage 2 index canonical content drifted before Stage 3 "
                    f"publication: {index_path}"
                )
            if sha256_file(Path(index_path)) != expected_index_byte_sha256:
                raise AggregationAuditError(
                    "Stage 2 index bytes drifted before Stage 3 publication: "
                    f"{index_path}"
                )
        for descriptor, source_path in tracked_snapshots:
            verify_artifact_descriptor(descriptor, source_path)

        write_csv(out_path, slide_rows)

        # Recheck direct inputs after the atomic CSV replace so the audit never
        # seals an output computed while an input changed mid-publication.
        for descriptor, source_path in tracked_snapshots:
            verify_artifact_descriptor(descriptor, source_path)
        output_artifacts = [
            artifact_descriptor(
                out_path, "stage3_slide_summary", audit_path
            )
        ]
        audit = build_aggregation_audit(
            stage="stage3_slide_aggregation",
            arguments={
                "stage2_index_name": args.stage2_index_name,
                "seam_merge_um": args.seam_merge_um,
                "seam_counts": args.seam_counts,
                "allow_incomplete": bool(args.allow_incomplete),
                "legacy_recursive_discovery": bool(
                    args.legacy_recursive_discovery
                ),
                "declared_slide_count": len(stage1_slides),
            },
            code_artifacts=code_artifacts,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
        )
        # The audit is deliberately the final member published in the set.
        write_json_atomic(audit_path, audit)
    except (OSError, Stage2IndexError, AggregationAuditError, ValueError) as exc:
        for partial in (out_path, audit_path):
            quarantine_if_exists(partial)
        sys.exit(f"ERROR: Stage 3 publication failed closed: {exc}")
    print("")
    print(f"Wrote {len(slide_rows)} slide row(s) -> {out_path}")
    print(f"Wrote content-addressed aggregation audit last -> {audit_path}")
    print("Next:  python3 aggregate_to_mouse.py " + out_path)
    print("Reminder: n = MICE. With one slide per mouse, n equals the number of slides, "
          "not the number of tiles.")


if __name__ == "__main__":
    main()
