import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aggregate_to_mouse import (
    AggregationAuditError,
    STAGE3_AUDIT_FILENAME,
    STAGE4_AUDIT_FILENAME,
    aggregate_mice,
    artifact_descriptor,
    build_aggregation_audit,
    classify_columns,
    group_stats,
    is_quarantined_summary,
    merge_endpoint_rows,
    repository_python_code_closure,
    validate_aggregation_audit,
    validate_rows,
    write_csv as write_mouse_csv,
    write_json_atomic,
)
from aggregate_tiles_to_slide import write_csv as write_slide_csv


class PartitionQcAggregationTests(unittest.TestCase):
    def setUp(self):
        self.header = [
            "image", "region", "section_id", "mouse_id", "genotype",
            "condition", "panel", "region_area_um2", "n_nuclei",
            "KRT5_pod_area_um2", "KRT5_n_pods",
            "damaged_area_um2", "intact_area_um2",
            "damaged_fraction_of_parenchyma",
            "KRT5_pod_area_um2_in_intact",
            "KRT5_pod_area_frac_of_intact",
        ]
        identity = {
            "region": "damaged_parenchyma", "mouse_id": "M1",
            "genotype": "IFNg_KO_hom", "condition": "PR8", "panel": "LEFT",
        }
        self.rows = [
            {
                **identity, "image": "slide_a", "section_id": "slide_a",
                "region_area_um2": "100", "n_nuclei": "10",
                "KRT5_pod_area_um2": "20", "KRT5_n_pods": "2",
                "damaged_area_um2": "100", "intact_area_um2": "200",
                "damaged_fraction_of_parenchyma": str(1 / 3),
                "KRT5_pod_area_um2_in_intact": "5",
                "KRT5_pod_area_frac_of_intact": "0.025",
            },
            {
                **identity, "image": "slide_b", "section_id": "slide_b",
                "region_area_um2": "300", "n_nuclei": "30",
                "KRT5_pod_area_um2": "30", "KRT5_n_pods": "3",
                "damaged_area_um2": "300", "intact_area_um2": "100",
                "damaged_fraction_of_parenchyma": "0.75",
                "KRT5_pod_area_um2_in_intact": "7",
                "KRT5_pod_area_frac_of_intact": "0.07",
            },
        ]

    def test_partition_qc_columns_are_classified_as_additive(self):
        cats = classify_columns(self.header)
        self.assertEqual(
            set(cats["partition_area"]), {"damaged_area_um2", "intact_area_um2"}
        )
        self.assertEqual(cats["intact_pod_area"], ["KRT5_pod_area_um2_in_intact"])

    def test_rejected_and_stale_stage3_names_are_quarantined_inputs(self):
        self.assertTrue(is_quarantined_summary("slide_level_summary.REJECTED.csv"))
        self.assertTrue(is_quarantined_summary("slide_level_summary.STALE.abc123.csv"))
        self.assertFalse(is_quarantined_summary("slide_level_summary.csv"))

    def test_atomic_csv_writers_do_not_follow_predictable_temp_hardlinks(self):
        for label, writer in (
            ("stage3", write_slide_csv),
            ("stage4", write_mouse_csv),
        ):
            with self.subTest(writer=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / "summary.csv"
                predictable = Path(str(output) + ".tmp")
                victim = root / "victim.txt"
                victim.write_text("do not overwrite\n", encoding="utf-8")
                try:
                    os.link(victim, output)
                    os.link(victim, predictable)
                except OSError as exc:
                    self.skipTest(f"hard links unavailable for atomic-writer test: {exc}")

                writer(str(output), [{"value": "safe"}])

                self.assertEqual(victim.read_text(encoding="utf-8"), "do not overwrite\n")
                self.assertEqual(predictable.read_text(encoding="utf-8"), "do not overwrite\n")
                self.assertEqual(output.read_text(encoding="utf-8"), "value\nsafe\n")

    def test_aggregation_audit_fails_closed_on_artifact_and_payload_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = root / "aggregator.py"
            source = root / "source.csv"
            output = root / "output.csv"
            audit_path = root / "aggregation.audit.json"
            code.write_text("print('fixed')\n", encoding="utf-8")
            source.write_text("value\n1\n", encoding="utf-8")
            output.write_text("total\n1\n", encoding="utf-8")
            audit = build_aggregation_audit(
                stage="test_aggregation_audit",
                arguments={"sampling_unit": "section"},
                code_artifacts=[
                    artifact_descriptor(code, "stage4_aggregator", audit_path)
                ],
                input_artifacts=[
                    artifact_descriptor(source, "aggregation_input_summary", audit_path)
                ],
                output_artifacts=[
                    artifact_descriptor(output, "stage4_mouse_summary", audit_path)
                ],
                generated_utc="2026-08-25T00:00:00Z",
                runtime={"python_implementation": "test", "python_version": "test"},
            )
            write_json_atomic(audit_path, audit)
            validate_aggregation_audit(
                audit_path, expected_stage="test_aggregation_audit"
            )

            output.write_text("total\n2\n", encoding="utf-8")
            with self.assertRaisesRegex(AggregationAuditError, "SHA-256 drift"):
                validate_aggregation_audit(audit_path)

            output.write_text("total\n1\n", encoding="utf-8")
            changed = dict(audit)
            changed["arguments"] = {"sampling_unit": "field"}
            audit_path.write_text(
                json.dumps(changed), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                    AggregationAuditError, "payload SHA-256 drift"):
                validate_aggregation_audit(audit_path)

    def test_stage4_direct_confocal_publishes_a_valid_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "run_summary.csv"
            write_mouse_csv(str(source), self.rows)
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "aggregate_to_mouse.py"),
                    str(source),
                    "--outdir",
                    str(root),
                    "--sampling-unit",
                    "field",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            audit_path = root / STAGE4_AUDIT_FILENAME
            document = validate_aggregation_audit(
                audit_path, expected_stage="stage4_mouse_aggregation"
            )
            self.assertEqual(document["arguments"]["input_mode"], "direct_confocal")
            self.assertEqual(document["arguments"]["sampling_unit"], "field")
            self.assertTrue((root / "mouse_level_summary.csv").is_file())
            self.assertTrue((root / "group_level_summary.csv").is_file())

    def test_stage4_wsi_requires_and_chains_a_valid_stage3_audit(self):
        provenance = {
            "aggregation_contract_version": "2.0.0",
            "stage2_source_mode": "explicit_hashed_index",
            "stage2_index_schema_version": "1.1.0",
            "stage2_index_sha256": "a" * 64,
            "stage1_manifest_sha256": "b" * 64,
            "stage1_profile_sha256": "3" * 64,
            "stage1_script_sha256": "0" * 64,
            "stage1_source_metadata_sha256": "4" * 64,
            "source_package_sha256": "1" * 64,
            "tile_candidate_manifest_sha256": "8" * 64,
            "tile_manifest_sha256": "c" * 64,
            "stage2_script_sha256": "d" * 64,
            "resolved_config_sha256": "e" * 64,
            "configuration_artifact_set_sha256": "9" * 64,
            "runtime_profile_sha256": "5" * 64,
            "parameter_set_sha256": "6" * 64,
            "measurement_profile_sha256": "f" * 64,
            "declared_channel_map_sha256": "7" * 64,
            "ordered_channel_signature": "LEFT=C1-DAPI_C2-KRT5",
            "channel_signature_authority": "declared_panel_mapping_not_source_verified",
            "qc_status": "ok",
            "dataset_qc_status": "ok",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "slide_level_summary.csv"
            write_mouse_csv(str(source), [dict(self.rows[0], **provenance)])
            command = [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "aggregate_to_mouse.py"),
                str(source),
                "--outdir",
                str(root),
            ]
            missing = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("Stage 3 audit", missing.stderr)

            stage3_script = Path(__file__).resolve().parents[1] / "aggregate_tiles_to_slide.py"
            fixture_input = root / "stage3_fixture_input.txt"
            fixture_input.write_text("bound upstream bytes\n", encoding="utf-8")
            stage3_audit_path = root / STAGE3_AUDIT_FILENAME
            stage3_audit = build_aggregation_audit(
                stage="stage3_slide_aggregation",
                arguments={"seam_counts": "off"},
                code_artifacts=repository_python_code_closure(
                    [
                        ("stage3_aggregator", stage3_script),
                        (
                            "aggregation_library",
                            Path(__file__).resolve().parents[1]
                            / "aggregate_to_mouse.py",
                        ),
                    ],
                    stage3_audit_path,
                )[0],
                input_artifacts=[
                    artifact_descriptor(
                        fixture_input, "stage1_manifest", stage3_audit_path
                    )
                ],
                output_artifacts=[
                    artifact_descriptor(
                        source, "stage3_slide_summary", stage3_audit_path
                    )
                ],
            )
            write_json_atomic(stage3_audit_path, stage3_audit)

            complete = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                complete.returncode, 0, complete.stderr + complete.stdout
            )
            stage4_audit_path = root / STAGE4_AUDIT_FILENAME
            stage4 = validate_aggregation_audit(
                stage4_audit_path,
                expected_stage="stage4_mouse_aggregation",
            )
            self.assertEqual(stage4["arguments"]["input_mode"], "wsi_stage3")
            self.assertIn(
                "upstream_stage3_audit",
                {item["role"] for item in stage4["input_artifacts"]},
            )

            source.write_bytes(source.read_bytes() + b"\n")
            with self.assertRaisesRegex(AggregationAuditError, "drift"):
                validate_aggregation_audit(stage4_audit_path)

    def test_stale_input_cannot_leave_prior_stage4_canonicals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_input = root / "slide_level_summary.STALE.abc123.csv"
            stale_input.write_text("image\nold\n", encoding="utf-8")
            for name in ("mouse_level_summary.csv", "group_level_summary.csv"):
                (root / name).write_text("old\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "aggregate_to_mouse.py"),
                    str(stale_input),
                    "--outdir",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("REJECTED or STALE", result.stderr)
            self.assertFalse((root / "mouse_level_summary.csv").exists())
            self.assertFalse((root / "group_level_summary.csv").exists())
            self.assertEqual(len(list(root.glob("mouse_level_summary.STALE.*.csv"))), 1)
            self.assertEqual(len(list(root.glob("group_level_summary.STALE.*.csv"))), 1)

    def test_mouse_level_partition_qc_is_pooled_not_averaged(self):
        result = aggregate_mice(self.header, self.rows)
        self.assertEqual(len(result), 1)
        mouse = result[0]
        self.assertEqual(mouse["damaged_area_um2"], 400.0)
        self.assertEqual(mouse["intact_area_um2"], 300.0)
        self.assertAlmostEqual(mouse["damaged_fraction_of_parenchyma"], 4 / 7)
        self.assertEqual(mouse["KRT5_pod_area_um2_in_intact"], 12.0)
        self.assertAlmostEqual(mouse["KRT5_pod_area_frac_of_intact"], 0.04)
        self.assertAlmostEqual(mouse["KRT5_pod_area_frac"], 0.125)

    def test_relational_endpoint_fraction_uses_pooled_denominator(self):
        header = self.header + [
            "KRT5dysplastic_pod_area_um2",
            "KRT5dysplastic_denominator_area_um2",
            "KRT5dysplastic_fraction",
        ]
        rows = [dict(row) for row in self.rows]
        rows[0].update({
            "KRT5dysplastic_pod_area_um2": "2",
            "KRT5dysplastic_denominator_area_um2": "6",
            "KRT5dysplastic_fraction": str(1 / 3),
        })
        rows[1].update({
            "KRT5dysplastic_pod_area_um2": "8",
            "KRT5dysplastic_denominator_area_um2": "10",
            "KRT5dysplastic_fraction": "0.8",
        })

        mouse = aggregate_mice(
            header,
            rows,
            endpoint_relation={
                "area_column": "KRT5dysplastic_pod_area_um2",
                "bare_area_column": "KRT5_pod_area_um2",
                "numerator_fraction_of_bare_column": "qc_krt5_pdpn_positive_fraction",
            },
        )[0]
        self.assertEqual(mouse["KRT5dysplastic_denominator_area_um2"], 16.0)
        self.assertAlmostEqual(mouse["KRT5dysplastic_fraction"], 10.0 / 16.0)
        self.assertAlmostEqual(mouse["qc_krt5_pdpn_positive_fraction"], 10.0 / 50.0)

    def test_endpoint_join_keeps_only_exact_evaluated_rows(self):
        header = self.header + ["output_key"]
        rows = [dict(row) for row in self.rows]
        rows[0]["output_key"] = "left_key"
        rows[1]["output_key"] = "right_key"
        rows[1]["panel"] = "RIGHT"

        with tempfile.TemporaryDirectory() as tmp:
            endpoint_path = Path(tmp) / "endpoint.csv"
            with endpoint_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "output_key", "region", "KRT5dysplastic_pod_area_um2",
                        "KRT5dysplastic_denominator_area_um2", "KRT5dysplastic_fraction",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "output_key": "left_key",
                    "region": "damaged_parenchyma",
                    "KRT5dysplastic_pod_area_um2": "2",
                    "KRT5dysplastic_denominator_area_um2": "6",
                    "KRT5dysplastic_fraction": str(1 / 3),
                })
            merged_header, merged_rows = merge_endpoint_rows(
                header, rows, str(endpoint_path)
            )

        self.assertIn("KRT5dysplastic_denominator_area_um2", merged_header)
        self.assertEqual(len(merged_rows), 1)
        self.assertEqual(merged_rows[0]["output_key"], "left_key")
        self.assertEqual(merged_rows[0]["panel"], "LEFT")

    def test_changed_output_key_does_not_make_a_retry_a_new_measurement(self):
        header = self.header + ["output_key"]
        rows = [dict(self.rows[0]), dict(self.rows[0])]
        rows[0]["output_key"] = "original-output"
        rows[1]["output_key"] = "renamed-retry-output"
        with self.assertRaisesRegex(SystemExit, "duplicate specimen-section-region-panel"):
            validate_rows(header, rows)

    def test_section_ids_may_repeat_for_different_mice(self):
        rows = [dict(self.rows[0]), dict(self.rows[0])]
        rows[1].update({
            "mouse_id": "M2",
            "genotype": "WT",
            "section_id": rows[0]["section_id"],
        })
        validate_rows(self.header, rows)

    def test_panel_specific_marker_is_blank_not_zero_when_unevaluable(self):
        header = self.header + ["ProSPC_pos_count"]
        left = dict(self.rows[0], ProSPC_pos_count="")
        right = dict(self.rows[1], panel="RIGHT", ProSPC_pos_count="7")
        validate_rows(header, [left, right])

        mice = aggregate_mice(header, [left, right])
        by_panel = {row["panel"]: row for row in mice}
        self.assertNotIn("ProSPC_pos_count_total", by_panel["LEFT"])
        self.assertEqual(by_panel["RIGHT"]["ProSPC_pos_count_total"], 7.0)

    def test_partial_missing_additive_measurement_within_panel_is_rejected(self):
        header = self.header + ["KRT5_pos_count"]
        rows = [
            dict(self.rows[0], KRT5_pos_count="3"),
            dict(self.rows[1], KRT5_pos_count=""),
        ]
        with self.assertRaisesRegex(SystemExit, "missing in 1/2 row.*panel 'LEFT'"):
            validate_rows(header, rows)

    def test_nonfinite_additive_measurement_is_rejected(self):
        header = self.header + ["KRT5_pos_count"]
        for token in ("NaN", "Inf", "-Inf", "not-a-number"):
            with self.subTest(token=token):
                rows = [dict(self.rows[0], KRT5_pos_count=token)]
                with self.assertRaisesRegex(SystemExit, "invalid/non-finite"):
                    validate_rows(header, rows)

    def test_negative_additive_measurement_is_rejected(self):
        for column in ("n_nuclei", "KRT5_pod_area_um2", "KRT5_n_pods"):
            with self.subTest(column=column):
                rows = [dict(self.rows[0])]
                rows[0][column] = "-1"
                with self.assertRaisesRegex(SystemExit, "negative additive"):
                    validate_rows(self.header, rows)

    def test_wsi_mouse_pooling_rejects_profile_drift_and_carries_provenance(self):
        provenance = {
            "aggregation_contract_version": "2.0.0",
            "stage2_source_mode": "explicit_hashed_index",
            "stage2_index_schema_version": "1.1.0",
            "stage2_index_sha256": "a" * 64,
            "stage1_manifest_sha256": "b" * 64,
            "stage1_profile_sha256": "3" * 64,
            "stage1_script_sha256": "0" * 64,
            "stage1_source_metadata_sha256": "4" * 64,
            "source_package_sha256": "1" * 64,
            "tile_candidate_manifest_sha256": "8" * 64,
            "tile_manifest_sha256": "c" * 64,
            "stage2_script_sha256": "d" * 64,
            "resolved_config_sha256": "e" * 64,
            "configuration_artifact_set_sha256": "9" * 64,
            "runtime_profile_sha256": "5" * 64,
            "parameter_set_sha256": "6" * 64,
            "measurement_profile_sha256": "f" * 64,
            "declared_channel_map_sha256": "7" * 64,
            "ordered_channel_signature": "LEFT=C1-DAPI_C2-KRT5",
            "channel_signature_authority": "declared_panel_mapping_not_source_verified",
            "qc_status": "ok",
            "dataset_qc_status": "ok",
        }
        header = self.header + list(provenance)
        rows = [dict(self.rows[0], **provenance), dict(self.rows[1], **provenance)]
        rows[1]["stage2_index_sha256"] = "1" * 64
        rows[1]["tile_manifest_sha256"] = "2" * 64
        validate_rows(header, rows)
        mouse = aggregate_mice(header, rows)[0]
        self.assertEqual(mouse["measurement_profile_sha256"], "f" * 64)
        self.assertEqual(
            mouse["stage2_index_sha256s"],
            ";".join(sorted(["1" * 64, "a" * 64])),
        )
        group = group_stats([mouse])
        self.assertTrue(group)
        self.assertTrue(all(
            row["measurement_profile_sha256"] == "f" * 64 for row in group
        ))
        self.assertTrue(all(
            row["stage2_index_sha256s"]
            == ";".join(sorted(["1" * 64, "a" * 64]))
            for row in group
        ))

        drifted = [dict(row) for row in rows]
        drifted[1]["measurement_profile_sha256"] = "0" * 64
        with self.assertRaisesRegex(SystemExit, "mix incompatible WSI provenance"):
            validate_rows(header, drifted)

        cross_mouse_drift = [dict(row) for row in rows]
        cross_mouse_drift[1].update({
            "mouse_id": "M2",
            "genotype": "WT",
            "measurement_profile_sha256": "0" * 64,
        })
        with self.assertRaisesRegex(SystemExit, "panel rows mix incompatible WSI provenance"):
            validate_rows(header, cross_mouse_drift)

        failed_qc = [dict(row) for row in rows]
        failed_qc[0]["qc_status"] = "PROBLEM"
        with self.assertRaisesRegex(SystemExit, "non-passing slide/region QC"):
            validate_rows(header, failed_qc)

        malformed = [dict(row) for row in rows]
        malformed[0]["stage2_index_sha256"] = "not-a-hash"
        with self.assertRaisesRegex(SystemExit, "malformed SHA-256 provenance"):
            validate_rows(header, malformed)

        unsupported = [dict(row) for row in rows]
        unsupported[0]["aggregation_contract_version"] = "999"
        with self.assertRaisesRegex(SystemExit, "unsupported aggregation contract"):
            validate_rows(header, unsupported)

    def test_legacy_wsi_shape_cannot_bypass_hashed_provenance(self):
        header = self.header + ["n_tiles_expected", "qc_status"]
        rows = [dict(self.rows[0], n_tiles_expected="1", qc_status="ok")]
        with self.assertRaisesRegex(SystemExit, "missing provenance columns"):
            validate_rows(header, rows)


if __name__ == "__main__":
    unittest.main()
