import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from ifquant.contracts import (
    MeasurementContractError,
    measurement_identity,
    require_aggregation_eligible,
    validate_measurement_record,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "measurement-record.schema.json"
SHA_A = "a" * 64
SHA_B = "b" * 64


def measured_record(value=0.25):
    return {
        "schema_version": "1.0.0",
        "record_id": "record-001",
        "track": "area_wsi",
        "record_level": "region",
        "identifiers": {
            "mouse_id": "M1",
            "slide_id": "S1",
            "section_id": "SEC1",
            "field_id": None,
            "tile_id": "T1",
            "region_id": "R1",
            "cell_id": None,
        },
        "measurement_profile_id": "wsi-profile-v1",
        "channel_signature": [
            {"index": 1, "label": "DAPI", "role": "nuclear_context"},
            {"index": 2, "label": "KRT5", "role": "endpoint_numerator"},
        ],
        "segmentation_model": None,
        "endpoint": {
            "endpoint_id": "krt5_area_fraction",
            "calculation": "ratio",
            "reference_space_id": "global_tissue_minus_airway-v1",
            "evaluability": "measured",
            "numerator": {"value": value * 100.0, "unit": "um2"},
            "denominator": {"value": 100.0, "unit": "um2"},
            "value": value,
            "unit": "fraction",
            "zero_is_observed": value == 0,
            "reason_code": None,
        },
        "sampling": {
            "design": "exhaustive",
            "inclusion_probability": 1,
            "selection_source": "whole_section",
        },
        "compartment": {
            "status": "assigned",
            "labels": ["parenchyma"],
            "assignment_profile_id": "anatomy-v1",
        },
        "provenance": {
            "code_revision": "0123456789abcdef",
            "config_sha256": SHA_A,
            "measurement_profile_sha256": "c" * 64,
            "inputs": [{"role": "source_image", "sha256": SHA_B}],
            "run_id": "run-001",
        },
        "qc": {
            "status": "pass",
            "reason_codes": [],
            "review_status": "not_required",
        },
    }


class MeasurementContractTests(unittest.TestCase):
    def test_schema_has_three_tracks_and_explicit_identifiers(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(
            schema["properties"]["track"]["enum"],
            ["area_wsi", "cell_confocal", "he_pathology"],
        )
        self.assertEqual(
            schema["$defs"]["identifiers"]["required"],
            ["mouse_id", "slide_id", "section_id", "field_id", "tile_id", "region_id", "cell_id"],
        )

    def test_measured_record_and_identity_are_valid(self):
        record = measured_record()
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(record)
        validate_measurement_record(record)
        identity = measurement_identity(record)
        self.assertIn("T1", identity)
        self.assertEqual(
            identity[-3:],
            ("krt5_area_fraction", "global_tissue_minus_airway-v1", "wsi-profile-v1"),
        )
        require_aggregation_eligible(record)

    def test_observed_zero_is_distinct_from_unevaluable(self):
        zero = measured_record(0)
        validate_measurement_record(zero)
        self.assertTrue(zero["endpoint"]["zero_is_observed"])

        unevaluable = measured_record()
        unevaluable["endpoint"].update(
            {
                "evaluability": "not_evaluable",
                "numerator": {"value": None, "unit": "um2"},
                "denominator": {"value": None, "unit": "um2"},
                "value": None,
                "zero_is_observed": False,
                "reason_code": "missing_tissue_roi",
            }
        )
        validate_measurement_record(unevaluable)

        collapsed = copy.deepcopy(unevaluable)
        collapsed["endpoint"]["value"] = 0
        with self.assertRaisesRegex(MeasurementContractError, "must be null"):
            validate_measurement_record(collapsed)

    def test_ratio_value_and_units_are_reconciled(self):
        bad_value = measured_record()
        bad_value["endpoint"]["value"] = 999
        with self.assertRaisesRegex(MeasurementContractError, "does not equal"):
            validate_measurement_record(bad_value)

        bad_unit = measured_record()
        bad_unit["endpoint"]["denominator"]["unit"] = "cells"
        with self.assertRaisesRegex(MeasurementContractError, "endpoint.unit must be"):
            validate_measurement_record(bad_unit)

        impossible_fraction = measured_record(2)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(impossible_fraction)
        with self.assertRaisesRegex(MeasurementContractError, "cannot exceed"):
            validate_measurement_record(impossible_fraction)

    def test_record_level_requires_its_identifier_and_allows_mouse_rollup(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        tile = measured_record()
        tile["record_level"] = "tile"
        tile["identifiers"]["region_id"] = None
        validator.validate(tile)
        validate_measurement_record(tile)

        missing_tile = copy.deepcopy(tile)
        missing_tile["identifiers"]["tile_id"] = None
        with self.assertRaises(ValidationError):
            validator.validate(missing_tile)
        with self.assertRaisesRegex(MeasurementContractError, "requires tile_id"):
            validate_measurement_record(missing_tile)

        descendant = copy.deepcopy(tile)
        descendant["identifiers"]["region_id"] = "R1"
        with self.assertRaisesRegex(MeasurementContractError, "descendant identifier region_id"):
            validate_measurement_record(descendant)

        mouse = measured_record()
        mouse["record_level"] = "mouse"
        for name in ("slide_id", "section_id", "field_id", "tile_id", "region_id", "cell_id"):
            mouse["identifiers"][name] = None
        validator.validate(mouse)
        validate_measurement_record(mouse)

    def test_purposive_sampling_cannot_invent_probability(self):
        record = measured_record()
        record["sampling"] = {
            "design": "purposive",
            "inclusion_probability": 0.1,
            "selection_source": "reviewer_selected",
        }
        with self.assertRaisesRegex(MeasurementContractError, "cannot invent"):
            validate_measurement_record(record)

    def test_boolean_is_not_a_numeric_sampling_probability(self):
        record = measured_record()
        record["sampling"]["inclusion_probability"] = True
        with self.assertRaisesRegex(MeasurementContractError, "must be numeric"):
            validate_measurement_record(record)

    def test_nonpassing_qc_requires_reason(self):
        record = measured_record()
        record["qc"]["status"] = "warning"
        with self.assertRaisesRegex(MeasurementContractError, "requires at least one"):
            validate_measurement_record(record)

    def test_failed_or_rejected_record_cannot_enter_aggregation(self):
        failed = measured_record()
        failed["qc"].update({"status": "fail", "reason_codes": ["area_mismatch"]})
        validate_measurement_record(failed)
        with self.assertRaisesRegex(MeasurementContractError, "cannot enter aggregation"):
            require_aggregation_eligible(failed)

        rejected = measured_record()
        rejected["qc"]["review_status"] = "rejected"
        validate_measurement_record(rejected)
        with self.assertRaisesRegex(MeasurementContractError, "cannot enter aggregation"):
            require_aggregation_eligible(rejected)

    def test_local_paths_are_rejected(self):
        record = measured_record()
        record["provenance"]["run_id"] = "source=D:\\private\\run"
        with self.assertRaisesRegex(MeasurementContractError, "cannot contain"):
            validate_measurement_record(record)


if __name__ == "__main__":
    unittest.main()
