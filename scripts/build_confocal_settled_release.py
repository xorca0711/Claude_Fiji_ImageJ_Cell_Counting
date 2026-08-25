#!/usr/bin/env python3
"""Build an immutable, canonical release around an already-completed confocal run.

This tool never edits the source run, microscope files, reviewed panels, or deck.
It reconciles folder discovery to a reviewer-approved 80-field study contract and
writes a small audit/release package containing CSV/JSON references and hashes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def fail(message: str) -> "NoReturn":
    raise SystemExit("ERROR: " + message)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            fail(f"{path} has no CSV header")
        return list(reader.fieldnames), list(reader)


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    columns: list[str] = []
    for row in materialized:
        for column in row:
            if column not in columns:
                columns.append(column)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not columns:
            return
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_relative(value: str) -> str:
    return value.replace("\\", "/").lstrip("./").casefold()


def required_path(value: str, label: str) -> Path:
    path = Path(value)
    if not path.exists():
        fail(f"{label} does not exist: {path}")
    return path

def resolve_source_file(
    recorded_value: str, source_root: Path, aliases: Iterable[str]
) -> tuple[Path, Path]:
    recorded = Path(recorded_value)
    roots = [source_root] + [Path(value) for value in aliases]
    for old_root in roots:
        try:
            relative = recorded.relative_to(old_root)
        except ValueError:
            continue
        candidate = source_root / relative
        if candidate.exists():
            return candidate, relative
    fail(
        "canonical source could not be resolved under current source_root or "
        f"configured aliases: {recorded}"
    )



def panel_from_annotation(value: str) -> str:
    upper = value.upper()
    if "KRT5" in upper and ("T1A" in upper or "T1ALPHA" in upper):
        return "LEFT"
    if "PROSPC" in upper or "PRO-SPC" in upper:
        return "RIGHT"
    fail(f"cannot map annotation panel to LEFT/RIGHT: {value!r}")


def unique_index(rows: Iterable[dict[str, str]], key, label: str) -> dict[Any, dict[str, str]]:
    result: dict[Any, dict[str, str]] = {}
    for row in rows:
        current = key(row)
        if current in result:
            fail(f"duplicate {label}: {current}")
        result[current] = row
    return result


def load_study(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8-sig") as handle:
        study = json.load(handle)
    expected = int(study.get("expected_fields", 0))
    if expected <= 0:
        fail("study config expected_fields must be positive")
    samples = study.get("samples") or []
    if len(samples) != int(study.get("expected_mouse_count", 0)):
        fail("study config sample count does not match expected_mouse_count")
    per_panel = int(study.get("expected_fields_per_panel", 0))
    for sample in samples:
        for panel in ("LEFT", "RIGHT"):
            roles = (sample.get("field_roles") or {}).get(panel) or []
            if len(roles) != per_panel:
                fail(
                    f"{sample.get('mouse_id')} {panel} has {len(roles)} field roles; "
                    f"expected {per_panel}"
                )
    return study


def visual_index(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        output_key = path.name.split("__", 1)[0]
        if output_key in result:
            fail(f"duplicate reviewed image for output_key {output_key}")
        result[output_key] = path
    return result


def build_release(config_path: Path, output_dir: Path) -> dict[str, Any]:
    study = load_study(config_path)
    source_root = required_path(study["source_root"], "source_root")
    run_root = required_path(study["source_run"], "source_run")
    annotations_path = required_path(study["field_annotations"], "field_annotations")
    legacy_visual_path = required_path(
        study["legacy_retouched_order"], "legacy_retouched_order"
    )
    visual_root = required_path(study["reviewed_visual_root"], "reviewed_visual_root")
    deck_path = required_path(study["authoritative_deck"], "authoritative_deck")
    samplesheet_path = required_path(str(run_root / "samplesheet.csv"), "samplesheet")
    run_summary_path = required_path(
        str(run_root / "analysis" / "run_summary.csv"), "run_summary"
    )
    run_manifest_path = required_path(
        str(run_root / "analysis" / "run_manifest.json"), "run_manifest"
    )
    mouse_summary_path = required_path(
        str(run_root / "stats" / "mouse_level_summary.csv"), "mouse_level_summary"
    )

    annotation_header, annotations = read_csv(annotations_path)
    samplesheet_header, samplesheet = read_csv(samplesheet_path)
    run_header, run_rows = read_csv(run_summary_path)
    _, legacy_visual_rows = read_csv(legacy_visual_path)
    _, mouse_summary_rows = read_csv(mouse_summary_path)
    with run_manifest_path.open(encoding="utf-8-sig") as handle:
        engine_manifest = json.load(handle)

    expected_count = int(study["expected_fields"])
    if len(annotations) != expected_count:
        fail(
            f"field_annotations has {len(annotations)} rows; "
            f"study contract requires {expected_count}"
        )
    if len(run_rows) != int(engine_manifest.get("success_count", -1)):
        fail("run_summary row count does not match run_manifest success_count")

    sample_specs = {sample["mouse_id"]: sample for sample in study["samples"]}
    exception_specs = {
        (item["mouse_id"], item["panel"], int(item["field_order"])): item
        for item in study.get("known_field_exceptions", [])
    }
    samplesheet_by_rel = unique_index(
        samplesheet, lambda row: normalized_relative(row["relative_path"]),
        "samplesheet relative_path",
    )
    run_by_identity = unique_index(
        run_rows,
        lambda row: (row["mouse_id"], row["panel"], row["section_id"]),
        "run_summary mouse/panel/section_id",
    )
    manifest_images = engine_manifest.get("images") or []
    manifest_by_rel = unique_index(
        manifest_images,
        lambda row: normalized_relative(row.get("relative_path") or ""),
        "run_manifest relative_path",
    )
    legacy_by_key = unique_index(
        legacy_visual_rows,
        lambda row: (row["Sample"], row["Side"], int(row["Order"])),
        "legacy visual Sample/Side/Order",
    )
    reviewed_visuals = visual_index(visual_root)

    counts_by_sample_panel: dict[tuple[str, str], int] = defaultdict(int)
    for annotation in annotations:
        side = panel_from_annotation(annotation["Panel"])
        counts_by_sample_panel[(annotation["Sample"], side)] += 1
    per_panel = int(study["expected_fields_per_panel"])
    for sample in study["samples"]:
        for side in ("LEFT", "RIGHT"):
            observed = counts_by_sample_panel[(sample["mouse_id"], side)]
            if observed != per_panel:
                fail(
                    f"annotations contain {observed} fields for "
                    f"{sample['mouse_id']} {side}; expected {per_panel}"
                )

    canonical_rows: list[dict[str, Any]] = []
    qc_rows: list[dict[str, Any]] = []
    canonical_source_keys: set[str] = set()
    annotation_order = {
        (row["Sample"], panel_from_annotation(row["Panel"]), int(row["Order"])): row
        for row in annotations
    }
    if len(annotation_order) != expected_count:
        fail("field_annotations has duplicate Sample/Panel/Order identities")

    for sample in study["samples"]:
        mouse_id = sample["mouse_id"]
        for side in ("LEFT", "RIGHT"):
            roles = sample["field_roles"][side]
            for field_order in range(1, per_panel + 1):
                annotation = annotation_order.get((mouse_id, side, field_order))
                if annotation is None:
                    fail(f"missing canonical annotation for {mouse_id} {side} {field_order}")
                recorded_source_file = annotation["SourceFile"]
                source_file, source_relative = resolve_source_file(
                    recorded_source_file, source_root,
                    study.get("source_root_aliases", []),
                )
                source_key = normalized_relative(str(source_relative))
                canonical_source_keys.add(source_key)
                samplesheet_row = samplesheet_by_rel.get(source_key)
                if samplesheet_row is None:
                    fail(f"canonical source has no samplesheet row: {source_relative}")
                if samplesheet_row["mouse_id"] != mouse_id or samplesheet_row["panel"] != side:
                    fail(f"samplesheet identity disagrees with annotation: {source_relative}")
                if (
                    samplesheet_row["genotype"] != sample["genotype"]
                    or samplesheet_row["condition"] != sample["condition"]
                ):
                    fail(f"samplesheet biological identity disagrees with config: {source_relative}")

                section_id = samplesheet_row["section_id"]
                run_row = run_by_identity.get((mouse_id, side, section_id))
                manifest_row = manifest_by_rel.get(source_key)
                if manifest_row is None:
                    fail(f"run_manifest has no record for canonical source: {source_relative}")
                expected_output_key = (
                    manifest_row.get("output_key")
                    or f"{mouse_id}_{sample['condition']}_{side}_{section_id}"
                )
                reviewed_visual = reviewed_visuals.get(expected_output_key)
                if reviewed_visual is None:
                    fail(f"no unique reviewed JPEG for {expected_output_key}")
                legacy_row = legacy_by_key.get((mouse_id, side, field_order))
                if legacy_row is None:
                    fail(f"legacy visual order lacks {mouse_id} {side} {field_order}")

                exception = exception_specs.get((mouse_id, side, field_order))
                if run_row:
                    quantification_status = "SUCCESS"
                    release_inclusion = "INCLUDED"
                    qc_status = "INCLUDED_SETTLED"
                else:
                    quantification_status = str(manifest_row.get("status") or "FAILED").upper()
                    release_inclusion = "MISSING_FROM_QUANTIFICATION"
                    qc_status = "MISSING_QUANTIFICATION"
                if exception:
                    qc_status = exception["qc_status"]
                    if qc_status == "PARTIAL_TRUNCATED_INCLUDED":
                        release_inclusion = "INCLUDED_WITH_PARTIAL_FLAG"
                    qc_rows.append(
                        {
                            "canonical_field_id": f"{mouse_id}_{side}_F{field_order:02d}",
                            "mouse_id": mouse_id,
                            "panel": side,
                            "field_order": field_order,
                            "qc_status": qc_status,
                            "action": release_inclusion,
                            "reason": exception["reason"],
                            "source_relative_path": str(source_relative),
                            "output_key": expected_output_key,
                            "engine_status": manifest_row.get("status", ""),
                            "engine_message": manifest_row.get("message") or manifest_row.get("error", ""),
                        }
                    )

                record: dict[str, Any] = {
                    "canonical_field_id": f"{mouse_id}_{side}_F{field_order:02d}",
                    "mouse_id": mouse_id,
                    "genotype": sample["genotype"],
                    "condition": sample["condition"],
                    "panel": side,
                    "marker_panel": study["panels"][side],
                    "field_order": field_order,
                    "field_role": roles[field_order - 1],
                    "field_role_source": "AUTHORITATIVE_DECK_REVIEWER",
                    "field_role_use": "STRATIFICATION_ONLY_NOT_THRESHOLD_CALIBRATION",
                    "source_relative_path": str(source_relative),
                    "source_file": str(source_file),
                    "recorded_source_file": recorded_source_file,
                    "source_file_exists": source_file.exists(),
                    "acquisition_section_id": section_id,
                    "acquisition_filename": source_file.name,
                    "output_key": expected_output_key,
                    "quantification_status": quantification_status,
                    "release_inclusion": release_inclusion,
                    "qc_status": qc_status,
                    "engine_status": manifest_row.get("status", ""),
                    "engine_message": manifest_row.get("message") or manifest_row.get("error", ""),
                    "quantification_included": bool(run_row),
                    "partial_sensitivity_flag": qc_status == "PARTIAL_TRUNCATED_INCLUDED",
                    "map_center_x": annotation.get("CenterX", ""),
                    "map_center_y": annotation.get("CenterY", ""),
                    "map_match_score": annotation.get("MatchScore", ""),
                    "map_evidence": annotation.get("Evidence", ""),
                    "annotated_overview": annotation.get("AnnotatedOverview", ""),
                    "reviewed_visual_panel": str(reviewed_visual),
                    "reviewed_visual_exists": reviewed_visual.exists(),
                    "reviewed_visual_sha256": sha256_file(reviewed_visual),
                    "reviewed_visual_usage": "DISPLAY_ONLY_NOT_QUANTIFIED",
                    "legacy_visual_panel_path": legacy_row.get("Path", ""),
                    "legacy_visual_path_exists": Path(legacy_row.get("Path", "")).exists(),
                }
                for column in run_header:
                    record[f"analysis_{column}"] = run_row.get(column, "") if run_row else ""
                canonical_rows.append(record)

    if len(canonical_rows) != expected_count:
        fail(f"built {len(canonical_rows)} canonical rows; expected {expected_count}")

    extras = [
        row for row in samplesheet
        if normalized_relative(row["relative_path"]) not in canonical_source_keys
    ]
    for row in extras:
        rel_key = normalized_relative(row["relative_path"])
        manifest_row = manifest_by_rel.get(rel_key, {})
        qc_rows.append(
            {
                "canonical_field_id": "",
                "mouse_id": row.get("mouse_id", ""),
                "panel": row.get("panel", ""),
                "field_order": "",
                "qc_status": "NONCANONICAL_ACQUISITION_EXCLUDED",
                "action": "AUDIT_ONLY_NOT_IN_CANONICAL_DENOMINATOR",
                "reason": (
                    "Discovery candidate not selected by the reviewed 80-field map; "
                    "its engine failure is retained in the source manifest."
                ),
                "source_relative_path": row.get("relative_path", ""),
                "output_key": manifest_row.get("output_key", ""),
                "engine_status": manifest_row.get("status", ""),
                "engine_message": manifest_row.get("message") or manifest_row.get("error", ""),
            }
        )

    stale_legacy = sum(not row["legacy_visual_path_exists"] for row in canonical_rows)
    if stale_legacy:
        qc_rows.append(
            {
                "canonical_field_id": "",
                "mouse_id": "",
                "panel": "",
                "field_order": "",
                "qc_status": "STALE_LEGACY_VISUAL_PATHS_REPLACED",
                "action": "REFER_TO_REVIEWED_V1_9_3_JPEG_PATHS",
                "reason": (
                    f"{stale_legacy} legacy retouched paths do not exist; canonical "
                    "rows now reference the finalized reviewed JPEG directory."
                ),
                "source_relative_path": "",
                "output_key": "",
            }
        )

    canonical_by_key = {
        (row["mouse_id"], row["panel"], int(row["field_order"])): row
        for row in canonical_rows
    }
    pair_rows: list[dict[str, Any]] = []
    for sample in study["samples"]:
        mouse_id = sample["mouse_id"]
        for field_order in range(1, per_panel + 1):
            left = canonical_by_key[(mouse_id, "LEFT", field_order)]
            right = canonical_by_key[(mouse_id, "RIGHT", field_order)]
            left_ok = left["quantification_included"]
            right_ok = right["quantification_included"]
            if left_ok and right_ok:
                pair_status = "COMPLETE_QUANTIFIED_PAIR"
            elif left_ok:
                pair_status = "RIGHT_QUANTIFICATION_MISSING"
            elif right_ok:
                pair_status = "LEFT_QUANTIFICATION_MISSING"
            else:
                pair_status = "BOTH_QUANTIFICATIONS_MISSING"
            if left["partial_sensitivity_flag"] or right["partial_sensitivity_flag"]:
                pair_status += "_PARTIAL_SENSITIVITY"
            try:
                map_distance = math.hypot(
                    float(left["map_center_x"]) - float(right["map_center_x"]),
                    float(left["map_center_y"]) - float(right["map_center_y"]),
                )
            except (TypeError, ValueError):
                map_distance = ""
            pair_rows.append(
                {
                    "pair_id": f"{mouse_id}_PAIR_F{field_order:02d}",
                    "mouse_id": mouse_id,
                    "genotype": sample["genotype"],
                    "condition": sample["condition"],
                    "field_order": field_order,
                    "pair_status": pair_status,
                    "pairing_scope": "FIELD_ORDER_ONLY_NOT_PIXEL_REGISTERED",
                    "same_cell_colocalization_allowed": False,
                    "map_center_distance_px": map_distance,
                    "left_canonical_field_id": left["canonical_field_id"],
                    "left_field_role": left["field_role"],
                    "left_quantification_status": left["quantification_status"],
                    "left_qc_status": left["qc_status"],
                    "left_output_key": left["output_key"],
                    "left_source_file": left["source_file"],
                    "left_reviewed_visual_panel": left["reviewed_visual_panel"],
                    "right_canonical_field_id": right["canonical_field_id"],
                    "right_field_role": right["field_role"],
                    "right_quantification_status": right["quantification_status"],
                    "right_qc_status": right["qc_status"],
                    "right_output_key": right["output_key"],
                    "right_source_file": right["source_file"],
                    "right_reviewed_visual_panel": right["reviewed_visual_panel"],
                }
            )

    quantified_by_mouse_panel: dict[tuple[str, str], int] = defaultdict(int)
    partial_by_mouse_panel: dict[tuple[str, str], int] = defaultdict(int)
    missing_by_mouse_panel: dict[tuple[str, str], int] = defaultdict(int)
    for row in canonical_rows:
        key = (row["mouse_id"], row["panel"])
        if row["quantification_included"]:
            quantified_by_mouse_panel[key] += 1
        else:
            missing_by_mouse_panel[key] += 1
        if row["partial_sensitivity_flag"]:
            partial_by_mouse_panel[key] += 1

    descriptive_mouse_rows: list[dict[str, Any]] = []
    for row in mouse_summary_rows:
        key = (row["mouse_id"], row["panel"])
        record = dict(row)
        old_n_sections = record.pop("n_sections", "")
        record["n_fields"] = quantified_by_mouse_panel[key]
        record["expected_n_fields"] = per_panel
        record["missing_n_fields"] = missing_by_mouse_panel[key]
        record["partial_n_fields"] = partial_by_mouse_panel[key]
        record["legacy_n_sections_value"] = old_n_sections
        record["statistical_unit"] = "mouse"
        record["reportability"] = "DESCRIPTIVE_ONLY"
        record["inferential_statistics_allowed"] = False
        descriptive_mouse_rows.append(record)

    success_count = sum(row["quantification_included"] for row in canonical_rows)
    endpoint_rows = [
        {
            "endpoint": "KRT5_pod_area",
            "panel": "LEFT",
            "status": "DESCRIPTIVE_ONLY",
            "reason": "Fixed threshold 300 is locked from one sound control; airway basal-cell KRT5 is not excluded.",
        },
        {
            "endpoint": "AGER",
            "panel": "LEFT_RIGHT",
            "status": "EXPLORATORY_ONLY",
            "reason": "Adaptive Otsu and compartment-unassigned context; not a frozen endpoint threshold.",
        },
        {
            "endpoint": "T1A_PDPN",
            "panel": "LEFT",
            "status": "EXPLORATORY_ONLY",
            "reason": "Threshold is not calibrated for the corrected dysplastic endpoint.",
        },
        {
            "endpoint": "ProSPC",
            "panel": "RIGHT",
            "status": "EXPLORATORY_ONLY",
            "reason": "Adaptive threshold and compartment-unassigned context.",
        },
        {
            "endpoint": "KRT8",
            "panel": "RIGHT",
            "status": "EXPLORATORY_ONLY",
            "reason": "Adaptive threshold and compartment-unassigned context.",
        },
        {
            "endpoint": "contextual_negative_and_coexpression_calls",
            "panel": "LEFT_RIGHT",
            "status": "NOT_REPORTABLE",
            "reason": f"All {success_count} quantified rows have compartment=unassigned.",
        },
        {
            "endpoint": "dysplastic_over_damaged",
            "panel": "LEFT",
            "status": "NOT_REPORTABLE",
            "reason": "Requires calibrated T1A/PDPN and validated damaged-alveolar/airway compartment annotations.",
        },
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths = {
        "canonical_field_manifest.csv": output_dir / "canonical_field_manifest.csv",
        "settled_field_summary.csv": output_dir / "settled_field_summary.csv",
        "left_right_pair_summary.csv": output_dir / "left_right_pair_summary.csv",
        "mouse_descriptive_summary.csv": output_dir / "mouse_descriptive_summary.csv",
        "qc_exclusions_and_exceptions.csv": output_dir / "qc_exclusions_and_exceptions.csv",
        "endpoint_reportability.csv": output_dir / "endpoint_reportability.csv",
    }
    canonical_columns = [
        key for key in canonical_rows[0] if not key.startswith("analysis_")
    ]
    write_csv(
        artifact_paths["canonical_field_manifest.csv"],
        [{column: row[column] for column in canonical_columns} for row in canonical_rows],
    )
    write_csv(artifact_paths["settled_field_summary.csv"], canonical_rows)
    write_csv(artifact_paths["left_right_pair_summary.csv"], pair_rows)
    write_csv(artifact_paths["mouse_descriptive_summary.csv"], descriptive_mouse_rows)
    write_csv(artifact_paths["qc_exclusions_and_exceptions.csv"], qc_rows)
    write_csv(artifact_paths["endpoint_reportability.csv"], endpoint_rows)

    partial_count = sum(row["partial_sensitivity_flag"] for row in canonical_rows)
    missing_count = expected_count - success_count
    source_files = {
        "study_config": config_path,
        "authoritative_deck": deck_path,
        "field_annotations": annotations_path,
        "samplesheet": samplesheet_path,
        "run_summary": run_summary_path,
        "run_manifest": run_manifest_path,
    }
    release_manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "study_id": study["study_id"],
        "release_status": "SETTLED_DESCRIPTIVE_RELEASE",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_run_status": engine_manifest.get("status"),
        "source_discovery": {
            "matched_input_count": engine_manifest.get("matched_input_count"),
            "analytical_input_count": engine_manifest.get("analytical_input_count"),
            "success_count": engine_manifest.get("success_count"),
            "skipped_count": engine_manifest.get("skipped_count"),
            "failure_count": engine_manifest.get("failure_count"),
        },
        "canonical_reconciliation": {
            "expected_fields": expected_count,
            "quantified_fields": success_count,
            "missing_fields": missing_count,
            "partial_quantified_fields": partial_count,
            "noncanonical_candidates": len(extras),
            "reviewed_display_panels": len(reviewed_visuals),
            "stale_legacy_visual_paths_replaced": stale_legacy,
            "coverage_fraction": success_count / expected_count,
        },
        "statistical_policy": {
            "biological_unit": "mouse",
            "design_cells": len(study["samples"]),
            "mice_per_design_cell": 1,
            "group_statistics": "DESCRIPTIVE_ONLY",
            "sd_sem_when_n_lt_2": "NOT_ESTIMABLE",
            "field_rows_are_independent_replicates": False,
        },
        "source_files": {
            name: {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for name, path in source_files.items()
        },
        "artifacts": {},
        "interpretation_constraints": study["interpretation_constraints"],
    }
    manifest_path = output_dir / "analysis_release_manifest.json"
    for name, path in artifact_paths.items():
        release_manifest["artifacts"][name] = {
            "path": str(path),
            "rows": len(read_csv(path)[1]),
            "sha256": sha256_file(path),
        }
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(release_manifest, handle, indent=2)
        handle.write("\n")

    missing_detail = (
        "- Missing canonical quantification: 0"
        if missing_count == 0
        else f"- Missing canonical quantification: {missing_count}"
    )
    readme = f"""# G-SURF confocal 260808 settled release

Status: **SETTLED DESCRIPTIVE RELEASE**

This directory is a reconciliation/reporting layer. It does not modify or
replace the source engine run, reviewed visual panels, microscope files, or
authoritative PowerPoint.

## Canonical coverage

- Intended reviewed fields: {expected_count}
- Quantified canonical fields: {success_count}
{missing_detail}
- Quantified partial/truncated fields: {partial_count} (M4-1 RIGHT field order 7)
- Noncanonical discovery candidates retained for audit: {len(extras)}
- Reviewed display-only panels resolved: {len(reviewed_visuals)}
- Stale historical display paths replaced in the manifest: {stale_legacy}

## Interpretation boundary

The mouse is the biological unit. With one mouse in each genotype-condition
design cell, all group summaries are descriptive only. Fields are technical
sampling units, not independent biological replicates. LEFT/RIGHT pairing is by
reviewed field order only and does not imply pixel registration or same-cell
colocalization.

All {success_count} quantified rows are compartment-unassigned. Compartment-dependent
negative calls, coexpression classifications, and the corrected
dysplastic-over-damaged endpoint remain not reportable. Reviewed panels and
POD/NORMAL labels are display/stratification metadata and are not quantitative
inputs or threshold-calibration data.

See analysis_release_manifest.json for source hashes and exact provenance.
"""
    readme_path = output_dir / "README.md"
    with readme_path.open("w", encoding="utf-8") as handle:
        handle.write(readme)
    release_manifest["artifacts"]["README.md"] = {
        "path": str(readme_path),
        "sha256": sha256_file(readme_path),
    }
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(release_manifest, handle, indent=2)
        handle.write("\n")
    return release_manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the immutable G-SURF settled confocal release package."
    )
    parser.add_argument(
        "--config",
        default="config/studies/g_surf_confocal_260808.json",
        help="study configuration JSON",
    )
    parser.add_argument(
        "--output-dir",
        default=r"D:\IFQ_Runs\confocal_260809_settled_release",
        help="new or empty release directory",
    )
    parser.add_argument(
        "--allow-existing-empty",
        action="store_true",
        help="allow an existing directory only when it is empty",
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    if output_dir.exists():
        contents = list(output_dir.iterdir())
        if contents or not args.allow_existing_empty:
            fail(f"refusing to reuse output directory: {output_dir}")
    manifest = build_release(Path(args.config).resolve(), output_dir)
    reconciliation = manifest["canonical_reconciliation"]
    print(
        "Settled release complete: "
        f"{reconciliation['quantified_fields']}/{reconciliation['expected_fields']} "
        f"canonical fields quantified; "
        f"{reconciliation['partial_quantified_fields']} partial; "
        f"{reconciliation['noncanonical_candidates']} noncanonical candidates audited."
    )
    print(output_dir)


if __name__ == "__main__":
    main()
