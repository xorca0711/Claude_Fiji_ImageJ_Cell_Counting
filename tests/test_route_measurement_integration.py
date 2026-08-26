import csv
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aggregate_to_mouse import (
    STAGE4_AUDIT_FILENAME,
    STAGE4_MEASUREMENT_RECORD_FILENAME,
    artifact_descriptor,
    build_aggregation_audit,
    repository_python_code_closure,
    validate_aggregation_audit,
    write_json_atomic,
)
from ifquant.contracts import validate_measurement_record
from ifquant.route_records import (
    RouteMeasurementSpecError,
    build_preaggregation_measurement_records,
    parse_measurement_record_spec,
)


ROOT = Path(__file__).resolve().parents[1]
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def direct_confocal_spec(*, qc=None, additional_inputs=None):
    return {
        "spec_version": "1.0.0",
        "track": "cell_confocal",
        "record_level": "region",
        "panel_column": "panel",
        "identifier_columns": {
            "mouse_id": "mouse_id",
            "slide_id": None,
            "section_id": None,
            "field_id": "section_id",
            "tile_id": None,
            "region_id": "region",
            "cell_id": None,
        },
        "target_estimand": "observed_units",
        "profiles": [
            {
                "panel": "LEFT",
                "measurement_profile_id": "confocal-left-v1",
                "channel_signature": [
                    {"index": 1, "label": "DAPI", "role": "nuclear_context"},
                    {"index": 2, "label": "KRT5", "role": "endpoint_numerator"},
                ],
                "segmentation_model": None,
                "sampling": {
                    "design": "purposive",
                    "inclusion_probability": None,
                    "selection_source": "reviewer_selected_fields",
                    "estimand_scope": "observed_units",
                },
                "compartment": {
                    "status": "unassigned",
                    "labels": [],
                    "assignment_profile_id": None,
                },
                "provenance": {
                    "code_revision": "0123456789abcdef",
                    "config_sha256": SHA_A,
                    "measurement_profile_sha256": SHA_B,
                    "run_id": "confocal-run-001",
                    "additional_inputs": additional_inputs or [],
                },
                "qc": qc
                or {
                    "status": "pass",
                    "reason_codes": [],
                    "review_status": "not_required",
                },
                "row_constraints": {"compartment": "unassigned"},
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
                        "value_column": "KRT5_final_positive_fraction_of_total_cells",
                    }
                ],
            }
        ],
    }


def direct_rows():
    base = {
        "image": "source",
        "panel": "LEFT",
        "region": "tissue",
        "mouse_id": "M1",
        "genotype": "WT",
        "condition": "control",
        "compartment": "unassigned",
        "region_area_um2": "100",
    }
    return [
        {
            **base,
            "image": "source_F01",
            "output_key": "M1_control_LEFT_F01",
            "section_id": "F01",
            "n_nuclei": "10",
            "KRT5_final_positive_cell_count": "2",
            "KRT5_final_positive_fraction_of_total_cells": "0.2",
        },
        {
            **base,
            "image": "source_F02",
            "output_key": "M1_control_LEFT_F02",
            "section_id": "F02",
            "n_nuclei": "20",
            "KRT5_final_positive_cell_count": "5",
            "KRT5_final_positive_fraction_of_total_cells": "0.25",
        },
    ]


def file_content(path: Path):
    payload = path.read_bytes()
    import hashlib

    return {
        "name": path.name,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def direct_artifact_set_sha256(artifacts):
    import hashlib

    payload = "".join(
        f"{item['role']}\t{item['relative_path']}\t"
        f"{item['size_bytes']}\t{item['sha256']}\n"
        for item in sorted(
            artifacts, key=lambda item: (item["role"], item["relative_path"])
        )
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_sealed_classic_direct_run(root: Path, summary: Path, rows):
    """Build a minimal modern engine publication without running Fiji."""
    engine = root / "executed-engine.groovy"
    engine.write_text("// fixed synthetic engine\n", encoding="utf-8")
    engine_content = file_content(engine)
    workbook = root / "run_summary.xlsx"
    workbook.write_bytes(b"synthetic-workbook")
    config_sha256 = "8" * 64
    runtime_authority = {
        "active": False,
        "authority": "not_applicable_classic",
        "api_command": None,
        "model_choice": None,
        "model_content": None,
        "model_archive": None,
        "runtime_manifest_content": None,
        "runtime_profile_id": None,
        "runtime_artifacts": [],
        "class_bindings": [],
    }
    config = {
        "segmenter": "classic",
        "engineScript": engine_content,
        "resolvedConfigSha256": config_sha256,
        "stardistAuthority": runtime_authority,
        "stardistModelSha256": None,
        "stardistModelAuthority": "not_applicable_classic",
    }
    images = []
    artifacts = []
    rows_by_key = {}
    for row in rows:
        rows_by_key.setdefault(row["output_key"], []).append(row)
    for output_key, output_rows in sorted(rows_by_key.items()):
        row = output_rows[0]
        raw = root / (row["image"] + ".tif")
        raw.write_bytes(("raw:" + output_key).encode("utf-8"))
        raw_content = file_content(raw)
        params_dir = root / output_key
        params_dir.mkdir()
        params = params_dir / "C1-DAPI_C2-KRT5__params.json"
        params.write_text(
            json.dumps(
                {
                    "image": raw.name,
                    "output_key": output_key,
                    "panel": row["panel"],
                    "channel_signature": "C1-DAPI_C2-KRT5",
                    "source_content": raw_content,
                    "engine_script": engine_content,
                    "resolved_config_sha256": config_sha256,
                    "segmenter": "classic",
                    "stardist_model_sha256": None,
                    "stardist_model_authority": "not_applicable_classic",
                    "stardist_runtime": {**runtime_authority, "label_outputs": []},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        params_content = file_content(params)
        params_relative = params.relative_to(root).as_posix()
        images.append(
            {
                "file": raw.name,
                "relative_path": raw.name,
                "output_key": output_key,
                "panel": row["panel"],
                "status": "success",
                "channel_signature": "C1-DAPI_C2-KRT5",
                "params_relative_path": params_relative,
                "params_content": params_content,
                "source_content": raw_content,
                "source_content_verified_before_and_after": True,
            }
        )
        artifacts.append(
            {
                "role": "image_params",
                "relative_path": params_relative,
                "output_key": output_key,
                "size_bytes": params_content["size_bytes"],
                "sha256": params_content["sha256"],
            }
        )
    summary_content = file_content(summary)
    workbook_content = file_content(workbook)
    artifacts.extend(
        [
            {
                "role": "run_summary",
                "relative_path": summary.name,
                "size_bytes": summary_content["size_bytes"],
                "sha256": summary_content["sha256"],
            },
            {
                "role": "summary_workbook",
                "relative_path": workbook.name,
                "size_bytes": workbook_content["size_bytes"],
                "sha256": workbook_content["sha256"],
            },
        ]
    )
    artifacts.sort(key=lambda item: (item["role"], item["relative_path"]))
    manifest = {
        "run_manifest_schema_version": "2.0.0",
        "publication_contract": "sealed_outputs_manifest_published_last",
        "publication_status": "sealed_complete",
        "status": "complete",
        "input_dir": str(root),
        "config": config,
        "engine_script": engine_content,
        "engine_script_path": str(engine),
        "engine_script_verified_before_and_after": True,
        "engine_script_verified_at_manifest_publication": True,
        "input_content_verified_at_manifest_publication": True,
        "stardist_authority_verified_before_and_after": False,
        "stardist_authority_verified_at_manifest_publication": False,
        "analytical_coverage_complete": True,
        "success_count": len(images),
        "analytical_input_count": len(images),
        "intended_analytical_input_count": len(images),
        "failure_count": 0,
        "output_failure_count": 0,
        "max_images_excluded_count": 0,
        "skipped_count": 0,
        "summary_workbook": workbook.name,
        "summary_workbook_status": "complete",
        "run_summary_content": summary_content,
        "summary_workbook_content": workbook_content,
        "images": images,
        "output_artifact_digest_contract": (
            "sha256_utf8_lf_role_tab_relative_path_tab_size_bytes_tab_sha256_v1"
        ),
        "output_artifacts": artifacts,
        "output_artifact_set_sha256": direct_artifact_set_sha256(artifacts),
    }
    manifest_path = root / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    spec = direct_confocal_spec()
    spec["profiles"][0]["provenance"]["code_revision"] = engine_content["sha256"]
    spec["profiles"][0]["provenance"]["config_sha256"] = config_sha256
    return manifest_path, spec


def upgrade_sealed_run_to_stardist(root: Path, manifest_path: Path, spec):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model = root / "model.zip"
    model.write_bytes(b"synthetic-stardist-model")
    runtime_manifest = root / "stardist-runtime.json"
    runtime_manifest.write_text('{"synthetic":true}\n', encoding="utf-8")
    runtime_artifacts = []
    for index, role in enumerate(
        ("stardist_plugin", "csbdeep_plugin", "tensorflow_java", "tensorflow_native"),
        start=1,
    ):
        artifact_path = root / f"runtime-{index}.jar"
        artifact_path.write_bytes(("runtime:" + role).encode("utf-8"))
        runtime_artifacts.append(
            {
                "role": role,
                "path": str(artifact_path),
                "expected_classes": [],
                "content": file_content(artifact_path),
            }
        )
    model_content = file_content(model)
    authority = {
        "active": True,
        "authority": "explicit_model_and_closed_runtime_manifest_content_bound",
        "api_command": "de.csbdresden.stardist.StarDist2D",
        "model_choice": "Model (.zip) from File",
        "model_path": str(model),
        "model_content": model_content,
        "model_archive": {"archive_format": "zip", "entry_count": 1, "file_count": 1},
        "runtime_manifest_path": str(runtime_manifest),
        "runtime_manifest_content": file_content(runtime_manifest),
        "runtime_profile_id": "synthetic-runtime-v1",
        "runtime_artifacts": runtime_artifacts,
        "class_bindings": [],
    }
    config_sha256 = "9" * 64
    manifest["config"].update(
        {
            "segmenter": "stardist",
            "resolvedConfigSha256": config_sha256,
            "stardistAuthority": authority,
            "stardistModelSha256": model_content["sha256"],
            "stardistModelAuthority": authority["authority"],
        }
    )
    manifest["stardist_authority_verified_before_and_after"] = True
    manifest["stardist_authority_verified_at_manifest_publication"] = True
    artifacts_by_key = {
        item["output_key"]: item
        for item in manifest["output_artifacts"]
        if item["role"] == "image_params"
    }
    for image in manifest["images"]:
        params_path = root / image["params_relative_path"]
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params.update(
            {
                "resolved_config_sha256": config_sha256,
                "segmenter": "stardist",
                "stardist_model_sha256": model_content["sha256"],
                "stardist_model_authority": authority["authority"],
                "stardist_runtime": {**authority, "label_outputs": []},
            }
        )
        params_path.write_text(
            json.dumps(params, indent=2) + "\n", encoding="utf-8"
        )
        content = file_content(params_path)
        image["params_content"] = content
        artifacts_by_key[image["output_key"]].update(
            {"size_bytes": content["size_bytes"], "sha256": content["sha256"]}
        )
    manifest["output_artifact_set_sha256"] = direct_artifact_set_sha256(
        manifest["output_artifacts"]
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    spec = copy.deepcopy(spec)
    profile = spec["profiles"][0]
    profile["provenance"]["config_sha256"] = config_sha256
    profile["segmentation_model"] = {
        "provider": "StarDist",
        "model_id": "synthetic-model",
        "model_sha256": model_content["sha256"],
        "profile_id": authority["runtime_profile_id"],
    }
    return spec, model


def wsi_spec():
    return {
        "spec_version": "1.0.0",
        "track": "area_wsi",
        "record_level": "slide",
        "panel_column": "panel",
        "identifier_columns": {
            "mouse_id": "mouse_id",
            "slide_id": "slide",
            "section_id": None,
            "field_id": None,
            "tile_id": None,
            "region_id": None,
            "cell_id": None,
        },
        "target_estimand": "whole_section",
        "profiles": [
            {
                "panel": "LEFT",
                "measurement_profile_id": "wsi-left-v1",
                "channel_signature": [
                    {"index": 1, "label": "DAPI", "role": "nuclear_context"},
                    {"index": 2, "label": "KRT5", "role": "endpoint_numerator"},
                ],
                "segmentation_model": None,
                "sampling": {
                    "design": "exhaustive",
                    "inclusion_probability": 1,
                    "selection_source": "complete_stage1_tile_grid",
                    "estimand_scope": "whole_section",
                },
                "compartment": {
                    "status": "assigned",
                    "labels": ["damaged_parenchyma"],
                    "assignment_profile_id": "wsi-damage-partition-v1",
                },
                "provenance": {
                    "code_revision": SHA_D,
                    "config_sha256": SHA_E,
                    "measurement_profile_sha256": SHA_F,
                    "run_id": "wsi-stage3-run-001",
                    "additional_inputs": [],
                },
                "qc": {
                    "status": "pass",
                    "reason_codes": [],
                    "review_status": "not_required",
                },
                "row_constraints": {"region": "damaged_parenchyma"},
                "endpoints": [
                    {
                        "calculation": "ratio",
                        "evaluability": "measured",
                        "endpoint_id": "krt5_pod_area_fraction",
                        "reference_space_id": "damaged_parenchyma-v1",
                        "numerator_column": "KRT5_pod_area_um2",
                        "numerator_unit": "um2",
                        "denominator_column": "region_area_um2",
                        "denominator_unit": "um2",
                        "result_unit": "fraction",
                        "value_column": "KRT5_pod_area_frac",
                    }
                ],
            }
        ],
    }


def wsi_row():
    return {
        "slide": "slide-01",
        "image": "slide-01",
        "section_id": "slide-01",
        "region": "damaged_parenchyma",
        "mouse_id": "M1",
        "genotype": "WT",
        "condition": "control",
        "panel": "LEFT",
        "region_area_um2": "100",
        "n_nuclei": "10",
        "KRT5_pod_area_um2": "25",
        "KRT5_pod_area_frac": "0.25",
        "aggregation_contract_version": "2.0.0",
        "stage2_source_mode": "explicit_hashed_index",
        "stage2_index_schema_version": "1.3.0",
        "stage1_profile_sha256": "3" * 64,
        "stage1_script_sha256": "4" * 64,
        "stage1_source_metadata_sha256": "5" * 64,
        "tile_candidate_manifest_sha256": "6" * 64,
        "configuration_artifact_set_sha256": "7" * 64,
        "runtime_profile_sha256": "8" * 64,
        "parameter_set_sha256": "9" * 64,
        "declared_channel_map_sha256": "0" * 64,
        "channel_signature_authority": "declared_panel_mapping_not_source_verified",
        "qc_status": "ok",
        "dataset_qc_status": "ok",
        "measurement_profile_sha256": SHA_F,
        "resolved_config_sha256": SHA_E,
        "stage2_script_sha256": SHA_D,
        "ordered_channel_signature": "LEFT=C1-DAPI_C2-KRT5",
        "stage2_index_sha256": SHA_A,
        "source_package_sha256": SHA_B,
        "stage1_manifest_sha256": SHA_C,
        "tile_manifest_sha256": "1" * 64,
    }


class RouteMeasurementRecordTests(unittest.TestCase):
    def test_direct_confocal_rows_emit_explicit_eligible_records(self):
        spec = parse_measurement_record_spec(
            direct_confocal_spec(
                additional_inputs=[
                    {"role": "engine_manifest", "sha256": SHA_C}
                ]
            ),
            expected_track="cell_confocal",
        )
        records, pools = build_preaggregation_measurement_records(
            direct_rows(),
            spec,
            source_inputs=[
                {"role": "aggregation_input_summary", "sha256": SHA_D},
                {"role": "engine_manifest", "sha256": SHA_C},
            ],
            pool_columns=("mouse_id", "genotype", "condition", "panel"),
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(len(pools), 1)
        self.assertEqual(records[0]["track"], "cell_confocal")
        self.assertEqual(records[0]["record_level"], "region")
        self.assertEqual(records[0]["identifiers"]["field_id"], "F01")
        self.assertEqual(records[0]["endpoint"]["value"], 0.2)
        self.assertEqual(
            {item["role"] for item in records[0]["provenance"]["inputs"]},
            {"aggregation_input_summary", "engine_manifest"},
        )
        for record in records:
            validate_measurement_record(record)

    def test_blank_or_undeclared_endpoint_data_never_becomes_zero(self):
        rows = direct_rows()
        rows[0]["KRT5_final_positive_cell_count"] = ""
        with self.assertRaisesRegex(
            RouteMeasurementSpecError, "blank; missingness cannot be coerced"
        ):
            build_preaggregation_measurement_records(
                rows,
                direct_confocal_spec(),
                source_inputs=[
                    {"role": "aggregation_input_summary", "sha256": SHA_D}
                ],
                pool_columns=("mouse_id", "genotype", "condition", "panel"),
            )

    def test_route_estimands_cannot_be_promoted_by_configuration(self):
        confocal = direct_confocal_spec()
        confocal["target_estimand"] = "whole_lung"
        with self.assertRaisesRegex(
            RouteMeasurementSpecError, "requires target_estimand='observed_units'"
        ):
            parse_measurement_record_spec(confocal)

        wsi = wsi_spec()
        wsi["profiles"][0]["sampling"]["design"] = "probability"
        wsi["profiles"][0]["sampling"]["inclusion_probability"] = 0.5
        wsi["profiles"][0]["sampling"]["estimator_profile_id"] = (
            "unsupported-estimator"
        )
        with self.assertRaisesRegex(
            RouteMeasurementSpecError, "exhaustive whole-section coverage"
        ):
            parse_measurement_record_spec(wsi)

        missing_profile = direct_rows()
        missing_profile[1]["panel"] = "RIGHT"
        with self.assertRaisesRegex(RouteMeasurementSpecError, "no explicit profile"):
            build_preaggregation_measurement_records(
                missing_profile,
                direct_confocal_spec(),
                source_inputs=[
                    {"role": "aggregation_input_summary", "sha256": SHA_D}
                ],
                pool_columns=("mouse_id", "genotype", "condition", "panel"),
            )

    def test_ineligible_qc_is_rejected_before_pooling(self):
        spec = direct_confocal_spec(
            qc={
                "status": "fail",
                "reason_codes": ["manual_review_failed"],
                "review_status": "rejected",
            }
        )
        with self.assertRaisesRegex(
            RouteMeasurementSpecError, "is not eligible"
        ):
            build_preaggregation_measurement_records(
                direct_rows(),
                spec,
                source_inputs=[
                    {"role": "aggregation_input_summary", "sha256": SHA_D}
                ],
                pool_columns=("mouse_id", "genotype", "condition", "panel"),
            )

    def test_declared_additional_input_requires_bound_matching_artifact(self):
        spec = direct_confocal_spec(
            additional_inputs=[
                {"role": "engine_manifest", "sha256": SHA_C}
            ]
        )
        with self.assertRaisesRegex(
            RouteMeasurementSpecError, "did not bind that artifact"
        ):
            build_preaggregation_measurement_records(
                direct_rows(),
                spec,
                source_inputs=[
                    {"role": "aggregation_input_summary", "sha256": SHA_D}
                ],
                pool_columns=("mouse_id", "genotype", "condition", "panel"),
            )

        with self.assertRaisesRegex(
            RouteMeasurementSpecError, "disagrees with the bound artifact"
        ):
            build_preaggregation_measurement_records(
                direct_rows(),
                spec,
                source_inputs=[
                    {"role": "aggregation_input_summary", "sha256": SHA_D},
                    {"role": "engine_manifest", "sha256": "0" * 64},
                ],
                pool_columns=("mouse_id", "genotype", "condition", "panel"),
            )

    def test_wsi_records_must_match_indexed_provenance_and_channel_order(self):
        records, _ = build_preaggregation_measurement_records(
            [wsi_row()],
            wsi_spec(),
            source_inputs=[
                {"role": "aggregation_input_summary", "sha256": "2" * 64}
            ],
            pool_columns=("mouse_id", "genotype", "condition", "panel"),
        )
        record = records[0]
        self.assertEqual(record["track"], "area_wsi")
        self.assertEqual(record["record_level"], "slide")
        self.assertEqual(record["identifiers"]["slide_id"], "slide-01")
        self.assertEqual(record["endpoint"]["value"], 0.25)
        self.assertTrue(
            {"stage2_index", "source_package", "stage1_manifest", "tile_manifest"}
            <= {item["role"] for item in record["provenance"]["inputs"]}
        )

        changed = wsi_spec()
        changed["profiles"][0]["provenance"]["measurement_profile_sha256"] = (
            "0" * 64
        )
        with self.assertRaisesRegex(
            RouteMeasurementSpecError, "measurement_profile_sha256 disagrees"
        ):
            build_preaggregation_measurement_records(
                [wsi_row()],
                changed,
                source_inputs=[
                    {"role": "aggregation_input_summary", "sha256": "2" * 64}
                ],
                pool_columns=("mouse_id", "genotype", "condition", "panel"),
            )


class Stage4MeasurementRecordCliTests(unittest.TestCase):
    @staticmethod
    def write_csv(path: Path, rows):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def run_direct(root: Path, summary: Path, spec_path: Path, manifest_path=None):
        command = [
            sys.executable,
            str(ROOT / "aggregate_to_mouse.py"),
            str(summary),
            "--outdir",
            str(root),
            "--sampling-unit",
            "field",
            "--measurement-record-spec",
            str(spec_path),
        ]
        if manifest_path is not None:
            command.extend(("--direct-run-manifest", str(manifest_path)))
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def assert_no_stage4_publication(self, root: Path):
        for name in (
            "mouse_level_summary.csv",
            "group_level_summary.csv",
            STAGE4_MEASUREMENT_RECORD_FILENAME,
            STAGE4_AUDIT_FILENAME,
        ):
            self.assertFalse((root / name).exists(), name)

    def test_direct_confocal_cli_validates_before_pool_and_audits_jsonl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = root / "run_summary.csv"
            spec_path = root / "measurement-spec.json"
            self.write_csv(summary, direct_rows())
            manifest_path, spec = write_sealed_classic_direct_run(
                root, summary, direct_rows()
            )
            spec_path.write_text(
                json.dumps(spec, indent=2) + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "aggregate_to_mouse.py"),
                    str(summary),
                    "--outdir",
                    str(root),
                    "--sampling-unit",
                    "field",
                    "--measurement-record-spec",
                    str(spec_path),
                    "--direct-run-manifest",
                    str(manifest_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            record_path = root / STAGE4_MEASUREMENT_RECORD_FILENAME
            records = [
                json.loads(line)
                for line in record_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 2)
            audit = validate_aggregation_audit(
                root / STAGE4_AUDIT_FILENAME,
                expected_stage="stage4_mouse_aggregation",
            )
            self.assertEqual(audit["arguments"]["measurement_record_count"], 2)
            self.assertEqual(
                audit["arguments"]["measurement_record_track"], "cell_confocal"
            )
            self.assertIn(
                "stage4_measurement_records",
                {item["role"] for item in audit["output_artifacts"]},
            )
            self.assertIn(
                "measurement_record_schema",
                {item["role"] for item in audit["input_artifacts"]},
            )
            self.assertIn(
                "direct_run_manifest",
                {item["role"] for item in audit["input_artifacts"]},
            )

    def test_direct_schema_v2_requires_explicit_sealed_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = root / "run_summary.csv"
            spec_path = root / "measurement-spec.json"
            self.write_csv(summary, direct_rows())
            spec_path.write_text(
                json.dumps(direct_confocal_spec()) + "\n", encoding="utf-8"
            )
            result = self.run_direct(root, summary, spec_path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--direct-run-manifest", result.stderr)
            self.assert_no_stage4_publication(root)

    def test_direct_tamper_and_incomplete_runs_publish_nothing(self):
        cases = (
            "summary",
            "manifest",
            "params",
            "raw",
            "incomplete",
            "capped",
            "failure",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                summary = root / "run_summary.csv"
                spec_path = root / "measurement-spec.json"
                rows = direct_rows()
                self.write_csv(summary, rows)
                manifest_path, spec = write_sealed_classic_direct_run(
                    root, summary, rows
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if case == "summary":
                    summary.write_text(
                        summary.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8",
                    )
                elif case == "manifest":
                    manifest["publication_contract"] = "unsealed"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                elif case == "params":
                    params = root / manifest["images"][0]["params_relative_path"]
                    params.write_text(
                        params.read_text(encoding="utf-8") + " ", encoding="utf-8"
                    )
                elif case == "raw":
                    raw = root / manifest["images"][0]["relative_path"]
                    raw.write_bytes(raw.read_bytes() + b"tamper")
                elif case == "incomplete":
                    manifest["status"] = "incomplete_max_images_limit"
                    manifest["publication_status"] = "sealed_incomplete_qc_only"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                elif case == "capped":
                    manifest["max_images_excluded_count"] = 1
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                elif case == "failure":
                    manifest["failure_count"] = 1
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                spec_path.write_text(
                    json.dumps(spec, indent=2) + "\n", encoding="utf-8"
                )
                result = self.run_direct(
                    root, summary, spec_path, manifest_path
                )
                self.assertNotEqual(result.returncode, 0, case)
                self.assert_no_stage4_publication(root)

    def test_direct_stardist_model_identity_drift_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = root / "run_summary.csv"
            spec_path = root / "measurement-spec.json"
            rows = direct_rows()
            self.write_csv(summary, rows)
            manifest_path, spec = write_sealed_classic_direct_run(
                root, summary, rows
            )
            spec, model = upgrade_sealed_run_to_stardist(
                root, manifest_path, spec
            )
            spec_path.write_text(
                json.dumps(spec, indent=2) + "\n", encoding="utf-8"
            )
            model.write_bytes(model.read_bytes() + b"tamper")
            result = self.run_direct(root, summary, spec_path, manifest_path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("model", result.stderr.lower())
            self.assert_no_stage4_publication(root)

    def test_cli_fails_closed_before_csv_publication_on_bad_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = root / "run_summary.csv"
            spec_path = root / "measurement-spec.json"
            self.write_csv(summary, direct_rows())
            manifest_path, spec = write_sealed_classic_direct_run(
                root, summary, direct_rows()
            )
            spec = copy.deepcopy(spec)
            spec["profiles"][0]["endpoints"][0]["numerator_column"] = (
                "undeclared_numerator"
            )
            spec_path.write_text(json.dumps(spec), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "aggregate_to_mouse.py"),
                    str(summary),
                    "--outdir",
                    str(root),
                    "--sampling-unit",
                    "field",
                    "--measurement-record-spec",
                    str(spec_path),
                    "--direct-run-manifest",
                    str(manifest_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("undeclared_numerator", result.stderr)
            self.assertFalse((root / "mouse_level_summary.csv").exists())
            self.assertFalse((root / "group_level_summary.csv").exists())
            self.assertFalse((root / STAGE4_MEASUREMENT_RECORD_FILENAME).exists())

    def test_wsi_cli_binds_stage3_lineage_before_area_record_pooling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = root / "slide_level_summary.csv"
            spec_path = root / "measurement-spec.json"
            self.write_csv(summary, [wsi_row()])
            spec_path.write_text(
                json.dumps(wsi_spec(), indent=2) + "\n", encoding="utf-8"
            )

            stage3_script = ROOT / "aggregate_tiles_to_slide.py"
            stage3_fixture_input = root / "stage3-fixture-input.txt"
            stage3_fixture_input.write_text("bound input\n", encoding="utf-8")
            stage3_audit_path = root / "slide_level_summary.audit.json"
            stage3_audit = build_aggregation_audit(
                stage="stage3_slide_aggregation",
                arguments={"seam_counts": "off"},
                code_artifacts=repository_python_code_closure(
                    [
                        ("stage3_aggregator", stage3_script),
                        ("aggregation_library", ROOT / "aggregate_to_mouse.py"),
                    ],
                    stage3_audit_path,
                )[0],
                input_artifacts=[
                    artifact_descriptor(
                        stage3_fixture_input,
                        "stage1_manifest",
                        stage3_audit_path,
                    )
                ],
                output_artifacts=[
                    artifact_descriptor(
                        summary, "stage3_slide_summary", stage3_audit_path
                    )
                ],
            )
            write_json_atomic(stage3_audit_path, stage3_audit)

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "aggregate_to_mouse.py"),
                    str(summary),
                    "--outdir",
                    str(root),
                    "--sampling-unit",
                    "section",
                    "--measurement-record-spec",
                    str(spec_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            record_path = root / STAGE4_MEASUREMENT_RECORD_FILENAME
            records = [
                json.loads(line)
                for line in record_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["track"], "area_wsi")
            self.assertEqual(record["endpoint"]["value"], 0.25)
            self.assertTrue(
                {"stage2_index", "source_package", "upstream_stage3_audit"}
                <= {item["role"] for item in record["provenance"]["inputs"]}
            )


if __name__ == "__main__":
    unittest.main()
