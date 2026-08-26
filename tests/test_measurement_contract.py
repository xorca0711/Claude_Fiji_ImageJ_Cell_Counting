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
        "schema_version": "2.0.0",
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


def categorical_record(state="positive"):
    record = measured_record()
    record.update(
        {
            "record_id": "categorical-001",
            "track": "cell_confocal",
            "record_level": "cell",
            "measurement_profile_id": "confocal-state-profile-v1",
        }
    )
    record["identifiers"] = {
        "mouse_id": "M1",
        "slide_id": None,
        "section_id": "SEC1",
        "field_id": "F1",
        "tile_id": None,
        "region_id": "R1",
        "cell_id": "C1",
    }
    record["endpoint"] = {
        "endpoint_id": "marker_state",
        "calculation": "categorical_state",
        "reference_space_id": "evaluable-cell-v1",
        "evaluability": "measured",
        "state": state,
        "state_vocabulary_id": "cell-state-v1",
        "reason_code": None,
    }
    record["sampling"] = {
        "design": "purposive",
        "inclusion_probability": None,
        "selection_source": "reviewer_selected_fields",
    }
    return record


def ordinal_record(rank=2):
    record = measured_record()
    record.update(
        {
            "record_id": "ordinal-001",
            "track": "he_pathology",
            "record_level": "section",
            "measurement_profile_id": "he-review-profile-v1",
        }
    )
    record["identifiers"] = {
        "mouse_id": "M1",
        "slide_id": "HE-S1",
        "section_id": "SEC1",
        "field_id": None,
        "tile_id": None,
        "region_id": None,
        "cell_id": None,
    }
    record["channel_signature"] = [
        {"index": 1, "label": "brightfield", "role": "histology_source"}
    ]
    record["endpoint"] = {
        "endpoint_id": "reviewed_morphology_rank",
        "calculation": "ordinal",
        "reference_space_id": "reviewed-section-v1",
        "evaluability": "measured",
        "rank": rank,
        "minimum_rank": 0,
        "maximum_rank": 4,
        "scale_id": "review-rubric-v1",
        "reason_code": None,
    }
    record["sampling"] = {
        "design": "exhaustive",
        "inclusion_probability": 1,
        "selection_source": "reviewed_section",
        "estimand_scope": "whole_section",
    }
    record["qc"]["review_status"] = "accepted"
    return record


class MeasurementContractTests(unittest.TestCase):
    def test_schema_has_three_tracks_and_explicit_identifiers(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"]["const"], "2.0.0")
        self.assertEqual(
            schema["properties"]["track"]["enum"],
            ["area_wsi", "cell_confocal", "he_pathology"],
        )
        self.assertEqual(
            schema["$defs"]["identifiers"]["required"],
            ["mouse_id", "slide_id", "section_id", "field_id", "tile_id", "region_id", "cell_id"],
        )
        endpoint_refs = schema["$defs"]["endpoint"]["oneOf"]
        self.assertEqual(
            endpoint_refs,
            [
                {"$ref": "#/$defs/ratioEndpoint"},
                {"$ref": "#/$defs/categoricalStateEndpoint"},
                {"$ref": "#/$defs/ordinalEndpoint"},
            ],
        )

    def test_categorical_state_record_is_discriminated_and_schema_valid(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        record = categorical_record("indeterminate")
        validator.validate(record)
        validate_measurement_record(record)

        nonmeasured = categorical_record()
        nonmeasured["endpoint"].update(
            {
                "evaluability": "not_evaluable",
                "state": None,
                "reason_code": "classification_unavailable",
            }
        )
        validator.validate(nonmeasured)
        validate_measurement_record(nonmeasured)

        leaked_state = copy.deepcopy(nonmeasured)
        leaked_state["endpoint"]["state"] = "negative"
        with self.assertRaises(ValidationError):
            validator.validate(leaked_state)
        with self.assertRaisesRegex(MeasurementContractError, "must be null"):
            validate_measurement_record(leaked_state)

    def test_ordinal_record_uses_integer_rank_not_continuous_value(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        record = ordinal_record(2)
        validator.validate(record)
        validate_measurement_record(record)

        fractional = ordinal_record(1.5)
        with self.assertRaises(ValidationError):
            validator.validate(fractional)
        with self.assertRaisesRegex(MeasurementContractError, "must be an integer"):
            validate_measurement_record(fractional)

        outside = ordinal_record(5)
        with self.assertRaisesRegex(MeasurementContractError, "within the declared"):
            validate_measurement_record(outside)

        reversed_scale = ordinal_record(2)
        reversed_scale["endpoint"].update({"minimum_rank": 4, "maximum_rank": 0})
        with self.assertRaisesRegex(MeasurementContractError, "cannot exceed"):
            validate_measurement_record(reversed_scale)

    def test_endpoint_calculations_are_route_gated(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        area_categorical = categorical_record()
        area_categorical["track"] = "area_wsi"
        area_categorical["record_level"] = "region"
        area_categorical["identifiers"].update(
            {"slide_id": "S1", "tile_id": "T1", "cell_id": None}
        )
        with self.assertRaises(ValidationError):
            validator.validate(area_categorical)
        with self.assertRaisesRegex(MeasurementContractError, "for track area_wsi"):
            validate_measurement_record(area_categorical)

        confocal_ordinal = ordinal_record()
        confocal_ordinal["track"] = "cell_confocal"
        confocal_ordinal["record_level"] = "field"
        confocal_ordinal["identifiers"].update(
            {"slide_id": None, "field_id": "F1", "section_id": None}
        )
        with self.assertRaises(ValidationError):
            validator.validate(confocal_ordinal)
        with self.assertRaisesRegex(MeasurementContractError, "for track cell_confocal"):
            validate_measurement_record(confocal_ordinal)

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

        valid_ratio = measured_record(2)
        valid_ratio["endpoint"]["unit"] = "ratio"
        Draft202012Validator(schema).validate(valid_ratio)
        validate_measurement_record(valid_ratio)

    def test_sparse_acquisition_channel_indices_are_preserved(self):
        record = measured_record()
        record["channel_signature"] = [
            {"index": 2, "label": "KRT5", "role": "endpoint_numerator"},
            {"index": 3, "label": "AGER", "role": "context"},
            {"index": 4, "label": "T1A", "role": "context"},
        ]
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(record)
        validate_measurement_record(record)

    def test_sha256_fields_require_json_strings_at_runtime_and_in_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        mutations = []

        config = measured_record()
        config["provenance"]["config_sha256"] = int("1" * 64)
        mutations.append(config)

        profile = measured_record()
        profile["provenance"]["measurement_profile_sha256"] = int("2" * 64)
        mutations.append(profile)

        source = measured_record()
        source["provenance"]["inputs"][0]["sha256"] = int("3" * 64)
        mutations.append(source)

        model = measured_record()
        model["segmentation_model"] = {
            "provider": "example",
            "model_id": "model-v1",
            "model_sha256": int("4" * 64),
            "profile_id": "segmentation-v1",
        }
        mutations.append(model)

        for record in mutations:
            with self.subTest(record=record):
                with self.assertRaises(ValidationError):
                    validator.validate(record)
                with self.assertRaisesRegex(MeasurementContractError, "lowercase SHA-256"):
                    validate_measurement_record(record)

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

    def test_nonprobability_sampling_cannot_claim_population_estimand(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for design in ("purposive", "unknown"):
            with self.subTest(design=design):
                record = measured_record()
                record["sampling"] = {
                    "design": design,
                    "inclusion_probability": None,
                    "selection_source": f"{design}-selection",
                    "estimand_scope": "whole_lung",
                }
                with self.assertRaises(ValidationError):
                    validator.validate(record)
                with self.assertRaisesRegex(
                    MeasurementContractError,
                    "cannot declare a whole-section or whole-lung estimand",
                ):
                    validate_measurement_record(record)

    def test_probability_sampling_requires_named_estimator_profile(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        record = measured_record()
        record["sampling"] = {
            "design": "probability",
            "inclusion_probability": 0.25,
            "selection_source": "probability-frame-v1",
            "estimand_scope": "whole_section",
        }
        with self.assertRaises(ValidationError):
            validator.validate(record)
        with self.assertRaisesRegex(
            MeasurementContractError,
            "estimator_profile_id must be a non-empty string",
        ):
            validate_measurement_record(record)

        record["sampling"]["estimator_profile_id"] = "weighted-estimator-v1"
        validator.validate(record)
        validate_measurement_record(record)

    def test_exhaustive_sampling_can_declare_population_estimand_without_estimator(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        record = measured_record()
        record["sampling"]["estimand_scope"] = "whole_section"
        Draft202012Validator(schema).validate(record)
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
