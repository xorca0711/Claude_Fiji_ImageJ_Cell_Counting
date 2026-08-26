import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "he_pipeline.py"
SPEC = importlib.util.spec_from_file_location("he_pipeline_aggregation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
he_pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(he_pipeline)


class HeReviewAggregationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.review_csv = self.root / "H7_SECTION_PATHOLOGY_REVIEW.csv"
        self.rows = self._valid_rows()
        self._write_review(self.rows)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _valid_rows():
        rows = []
        ordinal_columns = [
            endpoint["source_column"]
            for endpoint in he_pipeline.ORDINAL_REVIEW_ENDPOINTS
        ]
        for index in range(8):
            row = {field: "" for field in he_pipeline.LOCKED_REVIEW_FIELDS}
            row.update(
                {
                    "blind_section_id": f"HE-{index + 1:03d}",
                    "reviewable_yes_no_uncertain": "yes",
                    "edema_hemorrhage_necrosis_present_yes_no_uncertain": "no",
                    "dominant_pattern": "mixed",
                    "representative_region_ids": f"H4-R{index + 1:03d}",
                    "technical_limitation_none_minor_major": "none",
                    "confidence_low_medium_high": "high",
                    "reviewer_id": "reviewer-locked-01",
                    "reviewed_utc": f"2026-08-25T01:{index:02d}:00Z",
                    "notes": "",
                }
            )
            for endpoint_index, column in enumerate(ordinal_columns):
                row[column] = str((index + endpoint_index) % 5)
            rows.append(row)

        # A completed nonreviewable row stays explicit and carries no negative
        # or zero-valued lesion calls.
        rows[-1]["reviewable_yes_no_uncertain"] = "no"
        for column in ordinal_columns:
            rows[-1][column] = "uncertain"
        rows[-1]["edema_hemorrhage_necrosis_present_yes_no_uncertain"] = "uncertain"
        rows[-1]["dominant_pattern"] = "unresolved"
        rows[-1]["representative_region_ids"] = "none"
        rows[-1]["technical_limitation_none_minor_major"] = "major"
        rows[-1]["confidence_low_medium_high"] = "low"
        rows[-1]["notes"] = "Section cannot be scored reliably."
        return rows

    def _write_review(self, rows, fieldnames=None):
        fields = list(fieldnames or he_pipeline.LOCKED_REVIEW_FIELDS)
        with self.review_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def _aggregate(self, output_name="aggregate", **kwargs):
        output = self.root / output_name
        audit = he_pipeline.aggregate_review(
            output,
            self.review_csv,
            he_pipeline.DEFAULT_STUDY,
            he_pipeline.DEFAULT_RUBRIC,
            he_pipeline.DEFAULT_REPO_PROFILE,
            **kwargs,
        )
        return output, audit

    def _validate(self, output):
        return he_pipeline.validate_review_aggregation_audit(
            output,
            self.review_csv,
            he_pipeline.DEFAULT_STUDY,
            he_pipeline.DEFAULT_RUBRIC,
            he_pipeline.DEFAULT_REPO_PROFILE,
        )

    @staticmethod
    def _write_audit(output, audit):
        audit["audit_payload_sha256"] = he_pipeline._audit_payload_sha256(audit)
        he_pipeline.write_json(
            output / he_pipeline.AGGREGATION_AUDIT_FILENAME, audit
        )

    @classmethod
    def _reseal_output(cls, output, audit, filename):
        path = output / filename
        for artifact in audit["output_artifacts"]:
            if artifact["relative_path"] == filename:
                artifact["bytes"] = path.stat().st_size
                artifact["sha256"] = he_pipeline.sha256_file(path)
                break
        else:
            raise AssertionError(f"Missing output ledger row for {filename}")
        cls._write_audit(output, audit)

    @staticmethod
    def _write_csv_mutation(path, column, value):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        rows[0][column] = value
        he_pipeline.write_csv(path, fieldnames, rows)

    def test_valid_review_publishes_descriptive_outputs_and_schema_v2_records(self):
        output, audit = self._aggregate()

        self.assertEqual(
            audit["status"], "ACCEPTED_REVIEW_AGGREGATED_DESCRIPTIVE_ONLY"
        )
        self.assertEqual(audit["review_contract"]["validated_row_count"], 8)
        self.assertFalse(
            audit["statistical_policy"]["ordinal_scalar_composite_emitted"]
        )
        self.assertEqual(
            audit["measurement_record_contract"]["record_count"], 48
        )
        self.assertEqual(
            audit["measurement_record_contract"]["measured_record_count"], 42
        )
        self.assertEqual(
            audit["measurement_record_contract"]["schema_sha256"],
            he_pipeline.sha256_file(
                he_pipeline.MEASUREMENT_RECORD_SCHEMA_PATH
            ),
        )
        self.assertIn(
            "measurement_record_schema",
            {item["role"] for item in audit["input_artifacts"]},
        )
        expected_code_paths = {
            "aggregation_code": MODULE_PATH.resolve(),
            "ifquant_package_init": ROOT / "ifquant" / "__init__.py",
            "measurement_record_builder": ROOT / "ifquant" / "adapters.py",
            "measurement_record_contract": ROOT / "ifquant" / "contracts.py",
            "measurement_record_route_adapter": (
                ROOT / "ifquant" / "route_records.py"
            ),
            "stage2_index_contract": ROOT / "ifquant" / "stage2_index.py",
        }
        self.assertEqual(
            he_pipeline.AGGREGATION_CODE_PATHS_BY_ROLE,
            expected_code_paths,
        )
        expected_runtime_paths = {
            **expected_code_paths,
            "python_interpreter": Path(sys.executable).resolve(),
        }
        self.assertEqual(
            he_pipeline.AGGREGATION_RUNTIME_PATHS_BY_ROLE,
            expected_runtime_paths,
        )
        expected_input_paths = {
            "blinded_review_csv": self.review_csv,
            "study_contract": he_pipeline.DEFAULT_STUDY,
            "review_rubric": he_pipeline.DEFAULT_RUBRIC,
            "locked_stain_profile": he_pipeline.DEFAULT_REPO_PROFILE,
            "measurement_record_schema": (
                he_pipeline.MEASUREMENT_RECORD_SCHEMA_PATH
            ),
            **expected_runtime_paths,
        }
        audit_input_hashes = {
            item["role"]: item["sha256"]
            for item in audit["input_artifacts"]
        }
        self.assertEqual(len(audit_input_hashes), 12)
        self.assertEqual(
            set(audit_input_hashes),
            he_pipeline.AGGREGATION_AUDIT_INPUT_ROLES,
        )
        self.assertEqual(
            audit_input_hashes,
            {
                role: he_pipeline.sha256_file(path)
                for role, path in expected_input_paths.items()
            },
        )
        self.assertEqual(
            audit["measurement_record_contract"][
                "explicit_nonmeasured_record_count"
            ],
            6,
        )

        with (output / he_pipeline.SECTION_SCORES_FILENAME).open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            sections = list(csv.DictReader(handle))
        self.assertEqual(len(sections), 8)
        self.assertEqual(sections[0]["section_id"], "M2_BF_01")
        self.assertEqual(sections[-1]["section_evaluability"], "not_reviewable")

        with (output / he_pipeline.MOUSE_SUMMARY_FILENAME).open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            summaries = list(reader)
            headers = set(reader.fieldnames or [])
        self.assertEqual(len(summaries), 24)
        self.assertTrue(
            {
                "ordered_section_values_json",
                "minimum_observed_rank",
                "maximum_observed_rank",
                "exact_agreement",
            }.issubset(headers)
        )
        self.assertFalse(
            any(
                forbidden in header.lower()
                for forbidden in ("mean", "median", "composite", "summed_score")
                for header in headers
            )
        )

        with (output / he_pipeline.TECHNICAL_AGREEMENT_FILENAME).open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            agreement = list(csv.DictReader(handle))
        self.assertEqual(len(agreement), 6)
        self.assertTrue(all(row["n_mouse_pairs_declared"] == "4" for row in agreement))

        records = [
            json.loads(line)
            for line in (output / he_pipeline.MEASUREMENT_RECORDS_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(records), 48)
        self.assertTrue(
            all(
                record["schema_version"] == "2.0.0"
                and record["track"] == "he_pathology"
                and record["record_level"] == "section"
                and record["sampling"]["estimand_scope"] == "observed_units"
                and record["qc"]["review_status"] == "accepted"
                and any(
                    item["role"] == "measurement_record_schema"
                    and item["sha256"]
                    == audit["measurement_record_contract"]["schema_sha256"]
                    for item in record["provenance"]["inputs"]
                )
                for record in records
            )
        )
        for record in records:
            record_input_hashes = {
                item["role"]: item["sha256"]
                for item in record["provenance"]["inputs"]
            }
            self.assertEqual(len(record_input_hashes), 13)
            self.assertEqual(
                set(record_input_hashes),
                he_pipeline.MEASUREMENT_RECORD_PROVENANCE_INPUT_ROLES,
            )
            self.assertEqual(
                record["provenance"]["code_revision"],
                audit_input_hashes["aggregation_code"],
            )
            for role, digest in audit_input_hashes.items():
                self.assertEqual(record_input_hashes[role], digest)
        nonmeasured = [
            record
            for record in records
            if record["endpoint"]["evaluability"] != "measured"
        ]
        self.assertEqual(len(nonmeasured), 6)
        self.assertTrue(
            all(
                record["endpoint"]["rank"] is None
                and record["endpoint"]["reason_code"] == "section_not_reviewable"
                for record in nonmeasured
            )
        )
        self._validate(output)

        with self.assertRaisesRegex(he_pipeline.ContractError, "Refusing to overwrite"):
            self._aggregate()

    def test_incomplete_review_fails_without_publication(self):
        self._write_review(self.rows[:-1])
        output = self.root / "incomplete-output"
        with self.assertRaisesRegex(he_pipeline.ContractError, "exactly eight"):
            he_pipeline.aggregate_review(output, self.review_csv)
        self.assertFalse(output.exists())
        self.assertFalse(any(self.root.glob(".incomplete-output.staging-*")))

    def test_invalid_vocabulary_utc_header_and_nonreviewable_call_fail_closed(self):
        mutations = []

        invalid_value = [dict(row) for row in self.rows]
        invalid_value[0][
            "whole_section_inflammation_extent_0_4_uncertain"
        ] = "5"
        mutations.append(("vocabulary", invalid_value, None, "invalid value"))

        invalid_utc = [dict(row) for row in self.rows]
        invalid_utc[0]["reviewed_utc"] = "2026-08-25T01:00:00+09:00"
        mutations.append(("utc", invalid_utc, None, "explicit UTC"))

        invalid_nonreviewable = [dict(row) for row in self.rows]
        invalid_nonreviewable[-1][
            "whole_section_inflammation_extent_0_4_uncertain"
        ] = "0"
        mutations.append(("nonreviewable", invalid_nonreviewable, None, "lesion call"))

        reordered_header = list(he_pipeline.LOCKED_REVIEW_FIELDS)
        reordered_header[1], reordered_header[2] = reordered_header[2], reordered_header[1]
        mutations.append(("header", self.rows, reordered_header, "ordered header"))

        for name, rows, header, message in mutations:
            with self.subTest(name=name):
                self._write_review(rows, header)
                output = self.root / f"invalid-{name}"
                with self.assertRaisesRegex(he_pipeline.ContractError, message):
                    he_pipeline.aggregate_review(output, self.review_csv)
                self.assertFalse(output.exists())
                self.assertFalse(any(self.root.glob(f".invalid-{name}.staging-*")))

    def test_input_tamper_before_publish_removes_staging_and_publishes_nothing(self):
        output = self.root / "tamper-output"

        def tamper_review():
            self.review_csv.write_bytes(self.review_csv.read_bytes() + b"\r\n")

        with self.assertRaisesRegex(he_pipeline.ContractError, "changed before publication"):
            he_pipeline.aggregate_review(
                output, self.review_csv, _before_publish=tamper_review
            )
        self.assertFalse(output.exists())
        self.assertFalse(any(self.root.glob(".tamper-output.staging-*")))

    def test_published_artifact_tamper_invalidates_audit(self):
        output, _ = self._aggregate("published-tamper")
        section_path = output / he_pipeline.SECTION_SCORES_FILENAME
        section_path.write_bytes(section_path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(he_pipeline.ContractError, "failed integrity"):
            self._validate(output)

    def test_resealed_audit_cannot_drop_or_rename_import_closure_roles(self):
        output, audit = self._aggregate("closure-role-tamper")
        for artifact in audit["input_artifacts"]:
            if artifact["role"] == "stage2_index_contract":
                artifact["role"] = "stage2_index"
                break
        audit["audit_payload_sha256"] = he_pipeline._audit_payload_sha256(audit)
        he_pipeline.write_json(
            output / he_pipeline.AGGREGATION_AUDIT_FILENAME, audit
        )
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "exact data and Python import closure"
        ):
            self._validate(output)

    def test_resealed_audit_cannot_substitute_runtime_hashes(self):
        output, audit = self._aggregate("closure-audit-hash-tamper")
        for artifact in audit["input_artifacts"]:
            if artifact["role"] == "python_interpreter":
                artifact["sha256"] = "0" * 64
                break
        audit["audit_payload_sha256"] = he_pipeline._audit_payload_sha256(audit)
        he_pipeline.write_json(
            output / he_pipeline.AGGREGATION_AUDIT_FILENAME, audit
        )
        with self.assertRaisesRegex(
            he_pipeline.ContractError,
            "input does not match its authority: python_interpreter",
        ):
            self._validate(output)

    def test_resealed_record_cannot_disagree_with_import_closure_hashes(self):
        output, audit = self._aggregate("closure-hash-tamper")
        records_path = output / he_pipeline.MEASUREMENT_RECORDS_FILENAME
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
        ]
        for artifact in records[0]["provenance"]["inputs"]:
            if artifact["role"] == "measurement_record_route_adapter":
                artifact["sha256"] = "0" * 64
                break
        records_path.write_text(
            "".join(
                json.dumps(record, separators=(",", ":"), ensure_ascii=False)
                + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        for artifact in audit["output_artifacts"]:
            if artifact["relative_path"] == he_pipeline.MEASUREMENT_RECORDS_FILENAME:
                artifact["bytes"] = records_path.stat().st_size
                artifact["sha256"] = he_pipeline.sha256_file(records_path)
                break
        audit["audit_payload_sha256"] = he_pipeline._audit_payload_sha256(audit)
        he_pipeline.write_json(
            output / he_pipeline.AGGREGATION_AUDIT_FILENAME, audit
        )
        with self.assertRaisesRegex(
            he_pipeline.ContractError,
            "measurement-record payload disagrees",
        ):
            self._validate(output)

    def test_standalone_validation_requires_the_blinded_review_authority(self):
        output, _ = self._aggregate("authority-required")
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "requires the authoritative blinded-review"
        ):
            he_pipeline.validate_review_aggregation_audit(output)

    def test_resealed_audit_cannot_widen_statistical_policy(self):
        output, audit = self._aggregate("policy-tamper")
        audit["statistical_policy"]["group_inference_supported"] = True
        audit["statistical_policy"]["ordinal_scalar_composite_emitted"] = True
        self._write_audit(output, audit)
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "statistical policy is not the exact locked value"
        ):
            self._validate(output)

    def test_resealed_audit_cannot_rename_an_output_role(self):
        output, audit = self._aggregate("output-role-tamper")
        audit["output_artifacts"][0]["role"] = "renamed_scientific_role"
        self._write_audit(output, audit)
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "output role disagrees with its path"
        ):
            self._validate(output)

    def test_untracked_directory_and_nested_file_invalidate_package(self):
        output, _ = self._aggregate("extra-directory")
        unexpected = output / "UNTRACKED"
        unexpected.mkdir()
        (unexpected / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(
            he_pipeline.ContractError,
            "exactly five direct regular non-symlink files",
        ):
            self._validate(output)

    def test_symlinked_package_root_is_rejected_when_supported(self):
        output, _ = self._aggregate("symlink-target")
        link = self.root / "symlink-package"
        try:
            link.symlink_to(output, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "package root must not be a symlink"
        ):
            self._validate(link)

    def test_resealed_data_authority_hash_cannot_be_substituted(self):
        output, audit = self._aggregate("data-authority-tamper")
        replacement = "0" * 64
        for artifact in audit["input_artifacts"]:
            if artifact["role"] == "blinded_review_csv":
                artifact["sha256"] = replacement
                break
        records_path = output / he_pipeline.MEASUREMENT_RECORDS_FILENAME
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
        ]
        for record in records:
            for artifact in record["provenance"]["inputs"]:
                if artifact["role"] == "blinded_review_csv":
                    artifact["sha256"] = replacement
                    break
        records_path.write_bytes(
            he_pipeline._canonical_measurement_jsonl_bytes(records)
        )
        self._reseal_output(
            output, audit, he_pipeline.MEASUREMENT_RECORDS_FILENAME
        )
        with self.assertRaisesRegex(
            he_pipeline.ContractError,
            "input does not match its authority: blinded_review_csv",
        ):
            self._validate(output)

    def test_resealed_record_cannot_substitute_source_package_ledger(self):
        output, audit = self._aggregate("source-ledger-tamper")
        records_path = output / he_pipeline.MEASUREMENT_RECORDS_FILENAME
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
        ]
        for artifact in records[0]["provenance"]["inputs"]:
            if artifact["role"] == "declared_source_package_ledger":
                artifact["sha256"] = "0" * 64
                break
        records_path.write_bytes(
            he_pipeline._canonical_measurement_jsonl_bytes(records)
        )
        self._reseal_output(
            output, audit, he_pipeline.MEASUREMENT_RECORDS_FILENAME
        )
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "measurement-record payload disagrees"
        ):
            self._validate(output)

    def test_resealed_invalid_ordinal_record_fails_shared_contract(self):
        output, audit = self._aggregate("ordinal-rank-tamper")
        records_path = output / he_pipeline.MEASUREMENT_RECORDS_FILENAME
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
        ]
        records[0]["endpoint"]["rank"] = 999
        records_path.write_bytes(
            he_pipeline._canonical_measurement_jsonl_bytes(records)
        )
        self._reseal_output(
            output, audit, he_pipeline.MEASUREMENT_RECORDS_FILENAME
        )
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "violates the shared schema-v2 contract"
        ):
            self._validate(output)

    def test_resealed_csv_payloads_must_match_authoritative_reconstruction(self):
        mutations = (
            (
                he_pipeline.SECTION_SCORES_FILENAME,
                "whole_section_inflammation_extent_0_4_uncertain",
                "4",
                "section-score CSV disagrees",
            ),
            (
                he_pipeline.MOUSE_SUMMARY_FILENAME,
                "minimum_observed_rank",
                "4",
                "mouse-summary CSV disagrees",
            ),
            (
                he_pipeline.TECHNICAL_AGREEMENT_FILENAME,
                "n_exact_agreement",
                "99",
                "technical-agreement CSV disagrees",
            ),
        )
        for index, (filename, column, value, message) in enumerate(mutations):
            with self.subTest(filename=filename):
                output, audit = self._aggregate(f"csv-tamper-{index}")
                self._write_csv_mutation(output / filename, column, value)
                self._reseal_output(output, audit, filename)
                with self.assertRaisesRegex(he_pipeline.ContractError, message):
                    self._validate(output)


if __name__ == "__main__":
    unittest.main()
