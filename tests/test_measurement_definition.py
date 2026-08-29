import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from ifquant.measurement_definition import (
    ScientificMeasurementDefinitionError,
    frozen_threshold_set_sha256,
    load_frozen_threshold_set,
    load_scientific_measurement_definition,
    resolve_measurement_method,
    scientific_definition_sha256,
    validate_frozen_threshold_set,
    validate_scientific_measurement_definition,
)


ROOT = Path(__file__).resolve().parents[1]
DEFINITION_PATH = (
    ROOT
    / "config"
    / "measurement_definitions"
    / "krt5_positive_area_kernel_v1.json"
)
THRESHOLD_SET_PATH = (
    ROOT
    / "config"
    / "measurement_definitions"
    / "g_surf_confocal_260808_krt5_threshold_candidate_v1.json"
)
DEFINITION_SCHEMA_PATH = (
    ROOT / "schemas" / "scientific-measurement-definition.schema.json"
)
THRESHOLD_SCHEMA_PATH = ROOT / "schemas" / "frozen-threshold-set.schema.json"
CLI_PATH = ROOT / "scripts" / "validate_measurement_definition.py"

EXPECTED_DEFINITION_SHA256 = (
    "086d69cf98379a9d685685a3b4785b58c1df163bdd571884f3ebde99978b9e16"
)
EXPECTED_THRESHOLD_SET_SHA256 = (
    "5301ce71ff693f5975eceeaea6dfac7da66469d002984a229c26f1b21ea513aa"
)
EXPECTED_METHOD_INSTANCE_SHA256 = (
    "94ec5add390ece6d3a318a56d4b03fc40535183e151f0f11f32a00e086bf7628"
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


class ScientificMeasurementDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition = read_json(DEFINITION_PATH)
        cls.threshold_set = read_json(THRESHOLD_SET_PATH)
        cls.definition_schema = read_json(DEFINITION_SCHEMA_PATH)
        cls.threshold_schema = read_json(THRESHOLD_SCHEMA_PATH)
        cls.definition_schema_validator = Draft202012Validator(
            cls.definition_schema
        )
        cls.threshold_schema_validator = Draft202012Validator(cls.threshold_schema)

    def assert_definition_rejected_by_both(self, document):
        with self.assertRaises(ValidationError):
            self.definition_schema_validator.validate(document)
        with self.assertRaises(ScientificMeasurementDefinitionError):
            validate_scientific_measurement_definition(document)

    def assert_threshold_rejected_by_both(self, document):
        with self.assertRaises(ValidationError):
            self.threshold_schema_validator.validate(document)
        with self.assertRaises(ScientificMeasurementDefinitionError):
            validate_frozen_threshold_set(document)

    def test_checked_in_contracts_pass_schema_and_runtime_validation(self):
        Draft202012Validator.check_schema(self.definition_schema)
        Draft202012Validator.check_schema(self.threshold_schema)
        self.definition_schema_validator.validate(self.definition)
        self.threshold_schema_validator.validate(self.threshold_set)
        validate_scientific_measurement_definition(self.definition)
        validate_frozen_threshold_set(self.threshold_set)

    def test_checked_in_hashes_and_method_instance_are_stable(self):
        self.assertEqual(
            scientific_definition_sha256(self.definition),
            EXPECTED_DEFINITION_SHA256,
        )
        self.assertEqual(
            frozen_threshold_set_sha256(self.threshold_set),
            EXPECTED_THRESHOLD_SET_SHA256,
        )
        resolved = resolve_measurement_method(self.definition, self.threshold_set)
        self.assertEqual(
            resolved.method_instance_sha256, EXPECTED_METHOD_INSTANCE_SHA256
        )
        self.assertEqual(
            resolved.threshold_bindings,
            (("krt5_positive_intensity", 300),),
        )

    def test_scientific_definition_excludes_threshold_and_envelope_fields(self):
        keys = set(walk_keys(self.definition))
        self.assertNotIn("value", keys)
        self.assertNotIn("lifecycle", keys)
        self.assertNotIn("claim_boundary", keys)
        self.assertNotIn("conformance", keys)
        self.assertNotIn("backend", keys)
        self.assertNotIn("channel_index", keys)
        self.assertNotIn("track", keys)
        self.assertNotIn("record_level", keys)

    def test_definition_endpoint_uses_canonical_authority_name(self):
        authority = read_json(ROOT / "authority" / "project_state.json")
        release_endpoint = next(
            modality["release"]["endpoint"]
            for modality in authority["modalities"]
            if modality["id"] == "confocal_selected_fields"
        )
        self.assertEqual(
            self.definition["endpoint"]["endpoint_id"], release_endpoint
        )

    def test_canonical_hash_ignores_object_order_formatting_and_integral_float(self):
        reordered = dict(reversed(list(self.definition.items())))
        reordered["endpoint"] = dict(
            reversed(list(reordered["endpoint"].items()))
        )
        reordered["endpoint"]["numerator_pipeline"][1]["parameters"][
            "sigma_x_pixels"
        ] = 2.0
        self.assertEqual(
            scientific_definition_sha256(reordered),
            EXPECTED_DEFINITION_SHA256,
        )

        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "pretty.json"
            second = Path(temporary) / "compact.json"
            first.write_text(
                json.dumps(self.definition, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(reordered, separators=(",", ":"), ensure_ascii=False),
                encoding="utf-8",
            )
            loaded_first = load_scientific_measurement_definition(first)
            loaded_second = load_scientific_measurement_definition(second)
            self.assertNotEqual(loaded_first.file_sha256, loaded_second.file_sha256)
            self.assertEqual(
                loaded_first.canonical_sha256, loaded_second.canonical_sha256
            )

    def test_semantic_change_changes_definition_and_method_hashes(self):
        changed = copy.deepcopy(self.definition)
        changed["endpoint"]["numerator_pipeline"][1]["parameters"][
            "sigma_x_pixels"
        ] = 2.5
        changed_definition_hash = scientific_definition_sha256(changed)
        self.assertNotEqual(changed_definition_hash, EXPECTED_DEFINITION_SHA256)

        rebound_thresholds = copy.deepcopy(self.threshold_set)
        rebound_thresholds["scientific_definition"][
            "canonical_sha256"
        ] = changed_definition_hash
        resolved = resolve_measurement_method(changed, rebound_thresholds)
        self.assertNotEqual(
            resolved.method_instance_sha256, EXPECTED_METHOD_INSTANCE_SHA256
        )

    def test_threshold_change_does_not_change_scientific_hash(self):
        changed = copy.deepcopy(self.threshold_set)
        changed["thresholds"][0]["value"] = 301
        self.assertEqual(
            scientific_definition_sha256(self.definition),
            EXPECTED_DEFINITION_SHA256,
        )
        self.assertNotEqual(
            frozen_threshold_set_sha256(changed),
            EXPECTED_THRESHOLD_SET_SHA256,
        )
        resolved = resolve_measurement_method(self.definition, changed)
        self.assertNotEqual(
            resolved.method_instance_sha256, EXPECTED_METHOD_INSTANCE_SHA256
        )

    def test_acquisition_and_governance_fields_are_rejected(self):
        for field, value in (
            ("channel_index", 2),
            ("backend", "Fiji"),
            ("lifecycle", {"status": "prospective_locked"}),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.definition)
                changed["input_semantics"][field] = value
                self.assert_definition_rejected_by_both(changed)

    def test_v1_rejects_declared_semantic_inputs_not_consumed_by_pipeline(self):
        changed = copy.deepcopy(self.definition)
        changed["input_semantics"]["semantic_inputs"].insert(
            0, {"input_id": "ager_signal", "marker_id": "AGER"}
        )
        self.assert_definition_rejected_by_both(changed)

    def test_pipeline_order_and_filter_frame_fail_closed(self):
        reordered = copy.deepcopy(self.definition)
        pipeline = reordered["endpoint"]["numerator_pipeline"]
        pipeline[3], pipeline[4] = pipeline[4], pipeline[3]
        self.assert_definition_rejected_by_both(reordered)

        clipped_first = copy.deepcopy(self.definition)
        clipped_first["endpoint"]["numerator_pipeline"][3]["parameters"][
            "processing_extent"
        ] = "reference_space_only"
        self.assert_definition_rejected_by_both(clipped_first)

    def test_cross_object_reference_mismatch_is_rejected_at_runtime(self):
        changed = copy.deepcopy(self.definition)
        changed["endpoint"]["numerator_pipeline"][4]["parameters"][
            "reference_space_id"
        ] = "other_roi"
        with self.assertRaisesRegex(
            ScientificMeasurementDefinitionError, "wrong reference_space_id"
        ):
            validate_scientific_measurement_definition(changed)

    def test_unknown_and_missing_fields_fail_closed(self):
        unknown = copy.deepcopy(self.definition)
        unknown["unexpected"] = True
        self.assert_definition_rejected_by_both(unknown)

        missing = copy.deepcopy(self.definition)
        del missing["processing_frame"]
        self.assert_definition_rejected_by_both(missing)

    def test_duplicate_json_key_is_rejected_before_hashing(self):
        raw = DEFINITION_PATH.read_text(encoding="utf-8")
        duplicate = raw.replace(
            '  "schema_version": "1.0.0",',
            '  "schema_version": "1.0.0",\n  "schema_version": "1.0.0",',
            1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text(duplicate, encoding="utf-8")
            with self.assertRaisesRegex(
                ScientificMeasurementDefinitionError, "duplicate key"
            ):
                load_scientific_measurement_definition(path)

    def test_loaded_documents_are_deeply_immutable(self):
        loaded_definition = load_scientific_measurement_definition(DEFINITION_PATH)
        loaded_thresholds = load_frozen_threshold_set(THRESHOLD_SET_PATH)
        with self.assertRaises(TypeError):
            loaded_definition.document["definition_id"] = "changed"
        with self.assertRaises(TypeError):
            loaded_thresholds.document["thresholds"][0]["value"] = 301

    def test_threshold_set_definition_marker_and_parameter_must_match(self):
        wrong_hash = copy.deepcopy(self.threshold_set)
        wrong_hash["scientific_definition"]["canonical_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            ScientificMeasurementDefinitionError, "hash does not match"
        ):
            resolve_measurement_method(self.definition, wrong_hash)

        wrong_marker = copy.deepcopy(self.threshold_set)
        wrong_marker["thresholds"][0]["marker_id"] = "AGER"
        with self.assertRaisesRegex(
            ScientificMeasurementDefinitionError, "marker_id disagrees"
        ):
            resolve_measurement_method(self.definition, wrong_marker)

        wrong_parameter = copy.deepcopy(self.threshold_set)
        wrong_parameter["thresholds"][0][
            "threshold_parameter_id"
        ] = "unused_parameter"
        with self.assertRaisesRegex(
            ScientificMeasurementDefinitionError, "exactly.*parameter IDs"
        ):
            resolve_measurement_method(self.definition, wrong_parameter)

    def test_threshold_values_are_integer_and_bounded_by_bit_depth(self):
        boolean = copy.deepcopy(self.threshold_set)
        boolean["thresholds"][0]["value"] = True
        self.assert_threshold_rejected_by_both(boolean)

        out_of_range = copy.deepcopy(self.threshold_set)
        out_of_range["applicability"]["bit_depth"] = 8
        with self.assertRaisesRegex(
            ScientificMeasurementDefinitionError, "exceeds.*bit depth"
        ):
            validate_frozen_threshold_set(out_of_range)

    def test_malformed_enum_values_fail_closed_without_type_errors(self):
        gaussian = copy.deepcopy(self.definition)
        gaussian["endpoint"]["numerator_pipeline"][1]["parameters"][
            "output_quantization"
        ] = []
        self.assert_definition_rejected_by_both(gaussian)

        for field in ("minimum_boundary", "edge_policy"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.definition)
                changed["endpoint"]["numerator_pipeline"][3]["parameters"][field] = {}
                self.assert_definition_rejected_by_both(changed)

        maximum = copy.deepcopy(self.definition)
        maximum_parameters = maximum["endpoint"]["numerator_pipeline"][3][
            "parameters"
        ]
        maximum_parameters["maximum_area_um2"] = 100
        maximum_parameters["maximum_boundary"] = []
        self.assert_definition_rejected_by_both(maximum)

        scope = copy.deepcopy(self.threshold_set)
        scope["applicability"]["scope_binding"] = []
        self.assert_threshold_rejected_by_both(scope)

    def test_scope_binding_state_and_profile_hash_must_agree(self):
        identifier_only = copy.deepcopy(self.threshold_set)
        identifier_only["applicability"]["scope_profile_sha256"] = "0" * 64
        self.assert_threshold_rejected_by_both(identifier_only)

        content_addressed = copy.deepcopy(self.threshold_set)
        content_addressed["applicability"][
            "scope_binding"
        ] = "content_addressed_profile"
        content_addressed["applicability"]["scope_profile_sha256"] = None
        self.assert_threshold_rejected_by_both(content_addressed)

    def test_negative_zero_and_unsafe_numbers_cannot_be_hashed(self):
        negative_zero = copy.deepcopy(self.definition)
        negative_zero["endpoint"]["numerator_pipeline"][1]["parameters"][
            "kernel_accuracy"
        ] = -0.0
        with self.assertRaisesRegex(
            ScientificMeasurementDefinitionError, "negative zero"
        ):
            scientific_definition_sha256(negative_zero)

        unsafe = copy.deepcopy(self.definition)
        unsafe["endpoint"]["numerator_pipeline"][3]["parameters"][
            "minimum_area_um2"
        ] = 2**54
        self.assert_definition_rejected_by_both(unsafe)

        raw = DEFINITION_PATH.read_text(encoding="utf-8")
        lexical_negative_zero = raw.replace(
            '"background_value": 0', '"background_value": -0', 1
        )
        nonfinite = raw.replace('"kernel_accuracy": 0.0002', '"kernel_accuracy": NaN', 1)
        underflow = raw.replace('"kernel_accuracy": 0.0002', '"kernel_accuracy": 1e-400', 1)
        with tempfile.TemporaryDirectory() as temporary:
            negative_zero_path = Path(temporary) / "negative-zero.json"
            negative_zero_path.write_text(lexical_negative_zero, encoding="utf-8")
            with self.assertRaisesRegex(
                ScientificMeasurementDefinitionError, "lexical negative zero"
            ):
                load_scientific_measurement_definition(negative_zero_path)

            nonfinite_path = Path(temporary) / "nonfinite.json"
            nonfinite_path.write_text(nonfinite, encoding="utf-8")
            with self.assertRaisesRegex(
                ScientificMeasurementDefinitionError, "non-JSON numeric constant"
            ):
                load_scientific_measurement_definition(nonfinite_path)

            underflow_path = Path(temporary) / "underflow.json"
            underflow_path.write_text(underflow, encoding="utf-8")
            with self.assertRaisesRegex(
                ScientificMeasurementDefinitionError, "underflows binary64"
            ):
                load_scientific_measurement_definition(underflow_path)

    def test_cli_validates_bundle_and_supports_expected_hash_gates(self):
        command = [
            sys.executable,
            "-B",
            str(CLI_PATH),
            str(DEFINITION_PATH),
            "--threshold-set",
            str(THRESHOLD_SET_PATH),
            "--expect-definition-sha256",
            EXPECTED_DEFINITION_SHA256,
            "--expect-threshold-set-sha256",
            EXPECTED_THRESHOLD_SET_SHA256,
            "--expect-method-instance-sha256",
            EXPECTED_METHOD_INSTANCE_SHA256,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["authorization"], "none")
        self.assertEqual(
            summary["method_instance_sha256"], EXPECTED_METHOD_INSTANCE_SHA256
        )
        self.assertIn("not backend equivalence", summary["validation_scope"])

    def test_cli_hash_mismatch_fails_without_traceback(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(CLI_PATH),
                str(DEFINITION_PATH),
                "--expect-definition-sha256",
                "0" * 64,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("MEASUREMENT_DEFINITION_ERROR", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_malformed_enum_fails_without_traceback(self):
        changed = copy.deepcopy(self.definition)
        changed["endpoint"]["numerator_pipeline"][1]["parameters"][
            "output_quantization"
        ] = []
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "malformed-enum.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-B", str(CLI_PATH), str(path)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("MEASUREMENT_DEFINITION_ERROR", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_extreme_integer_fails_without_traceback(self):
        raw = DEFINITION_PATH.read_text(encoding="utf-8")
        extreme = raw.replace(
            '"minimum_area_um2": 50',
            '"minimum_area_um2": ' + ("9" * 5000),
            1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "extreme-integer.json"
            path.write_text(extreme, encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-B", str(CLI_PATH), str(path)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("MEASUREMENT_DEFINITION_ERROR", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
