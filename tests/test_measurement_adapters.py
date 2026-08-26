import copy
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from ifquant.adapters import (
    CategoricalStateColumnMapping,
    MeasurementAdapterError,
    MeasurementRecordContext,
    OrdinalColumnMapping,
    RatioColumnMapping,
    build_categorical_state_measurement_record,
    build_ordinal_measurement_record,
    build_ratio_measurement_record,
    write_measurement_records_jsonl,
)
from ifquant.contracts import (
    MeasurementContractError,
    require_aggregation_batch_eligible,
    require_aggregation_eligible,
    validate_measurement_record,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "schemas" / "measurement-record.schema.json").read_text(
        encoding="utf-8"
    )
)
VALIDATOR = Draft202012Validator(SCHEMA)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def ratio_mapping():
    return RatioColumnMapping(
        endpoint_id="krt5_area_fraction",
        reference_space_id="tissue-region-v1",
        numerator_column="KRT5_pod_area_um2",
        numerator_unit="um2",
        denominator_column="region_area_um2",
        denominator_unit="um2",
        result_unit="fraction",
        value_column="KRT5_pod_area_frac",
    )


def record_context(
    *,
    track="area_wsi",
    record_level="region",
    identifiers=None,
    region_id="R1",
    measurement_profile_id="wsi-profile-v1",
    measurement_profile_sha256=SHA_C,
    config_sha256=SHA_A,
    channel_signature=None,
    segmentation_model=None,
    sampling=None,
    qc=None,
):
    return MeasurementRecordContext(
        track=track,
        record_level=record_level,
        identifiers=identifiers or {
            "mouse_id": "M1",
            "slide_id": "S1",
            "section_id": "SEC1",
            "field_id": None,
            "tile_id": "T1",
            "region_id": region_id,
            "cell_id": None,
        },
        measurement_profile_id=measurement_profile_id,
        channel_signature=channel_signature
        or [
            {"index": 1, "label": "DAPI", "role": "nuclear_context"},
            {"index": 2, "label": "KRT5", "role": "endpoint_numerator"},
        ],
        segmentation_model=segmentation_model,
        sampling=sampling
        or {
            "design": "exhaustive",
            "inclusion_probability": 1,
            "selection_source": "complete_tile_grid",
        },
        compartment={
            "status": "assigned",
            "labels": ["parenchyma"],
            "assignment_profile_id": "anatomy-v1",
        },
        provenance={
            "code_revision": "0123456789abcdef",
            "config_sha256": config_sha256,
            "measurement_profile_sha256": measurement_profile_sha256,
            "inputs": [{"role": "source_image", "sha256": SHA_B}],
            "run_id": "run-001",
        },
        qc=qc
        or {
            "status": "pass",
            "reason_codes": [],
            "review_status": "not_required",
        },
    )


def build_record(*, region_id="R1", context=None, numerator="25", denominator="100", value="0.25"):
    return build_ratio_measurement_record(
        {
            "KRT5_pod_area_um2": numerator,
            "region_area_um2": denominator,
            "KRT5_pod_area_frac": value,
        },
        ratio_mapping(),
        context or record_context(region_id=region_id),
    )


def confocal_cell_context(
    *, cell_id="C1", section_id="SEC1", slide_id=None, sampling=None
):
    return record_context(
        track="cell_confocal",
        record_level="cell",
        identifiers={
            "mouse_id": "M1",
            "slide_id": slide_id,
            "section_id": section_id,
            "field_id": "F1",
            "tile_id": None,
            "region_id": "R1",
            "cell_id": cell_id,
        },
        measurement_profile_id="confocal-state-profile-v1",
        sampling=sampling
        or {
            "design": "purposive",
            "inclusion_probability": None,
            "selection_source": "reviewer_selected_fields",
        },
    )


def he_section_context(*, section_id="SEC1", sampling=None):
    return record_context(
        track="he_pathology",
        record_level="section",
        identifiers={
            "mouse_id": "M1",
            "slide_id": "HE-S1",
            "section_id": section_id,
            "field_id": None,
            "tile_id": None,
            "region_id": None,
            "cell_id": None,
        },
        measurement_profile_id="he-review-profile-v1",
        channel_signature=[
            {"index": 1, "label": "brightfield", "role": "histology_source"}
        ],
        sampling=sampling
        or {
            "design": "exhaustive",
            "inclusion_probability": 1,
            "selection_source": "reviewed_section",
            "estimand_scope": "whole_section",
        },
        qc={
            "status": "pass",
            "reason_codes": [],
            "review_status": "accepted",
        },
    )


def categorical_mapping(*, vocabulary_id="cell-state-v1"):
    return CategoricalStateColumnMapping(
        endpoint_id="marker_state",
        reference_space_id="evaluable-cell-v1",
        state_column="marker_state",
        state_vocabulary_id=vocabulary_id,
        allowed_states=("positive", "negative", "indeterminate"),
    )


def ordinal_mapping(*, minimum_rank=0, maximum_rank=4, scale_id="review-rubric-v1"):
    return OrdinalColumnMapping(
        endpoint_id="reviewed_morphology_rank",
        reference_space_id="reviewed-section-v1",
        rank_column="review_rank",
        scale_id=scale_id,
        minimum_rank=minimum_rank,
        maximum_rank=maximum_rank,
    )


class RatioAdapterTests(unittest.TestCase):
    def assert_contract_valid(self, record):
        VALIDATOR.validate(record)
        validate_measurement_record(record)

    def test_measured_ratio_is_recomputed_and_schema_valid(self):
        record = build_record()

        self.assert_contract_valid(record)
        self.assertEqual(record["endpoint"]["numerator"]["value"], 25.0)
        self.assertEqual(record["endpoint"]["denominator"]["value"], 100.0)
        self.assertEqual(record["endpoint"]["value"], 0.25)
        self.assertEqual(record["endpoint"]["unit"], "fraction")
        self.assertFalse(record["endpoint"]["zero_is_observed"])
        self.assertRegex(record["record_id"], r"^ifqmr-[0-9a-f]{64}$")

    def test_observed_zero_is_not_missing(self):
        record = build_record(numerator="0", value="0")

        self.assert_contract_valid(record)
        self.assertEqual(record["endpoint"]["value"], 0.0)
        self.assertTrue(record["endpoint"]["zero_is_observed"])
        require_aggregation_eligible(record)

    def test_equal_unit_ratio_above_one_is_not_mislabeled_as_fraction(self):
        mapping = RatioColumnMapping(
            endpoint_id="yap_nuclear_cytoplasmic_ratio",
            reference_space_id="single-plane-cell-v1",
            numerator_column="YAP_nuclear_mean",
            numerator_unit="intensity_au",
            denominator_column="YAP_cytoplasmic_mean",
            denominator_unit="intensity_au",
            result_unit="ratio",
            value_column="YAP_nuclear_cytoplasmic_ratio",
        )
        record = build_ratio_measurement_record(
            {
                "YAP_nuclear_mean": "150",
                "YAP_cytoplasmic_mean": "100",
                "YAP_nuclear_cytoplasmic_ratio": "1.5",
            },
            mapping,
            record_context(),
        )

        self.assert_contract_valid(record)
        self.assertEqual(record["endpoint"]["unit"], "ratio")
        self.assertEqual(record["endpoint"]["value"], 1.5)

    def test_record_id_is_deterministic_and_not_source_dict_order_dependent(self):
        first = build_ratio_measurement_record(
            {
                "KRT5_pod_area_um2": "25",
                "region_area_um2": "100",
                "KRT5_pod_area_frac": "0.25",
            },
            ratio_mapping(),
            record_context(),
        )
        second = build_ratio_measurement_record(
            {
                "KRT5_pod_area_frac": "0.25",
                "region_area_um2": "100",
                "KRT5_pod_area_um2": "25",
            },
            ratio_mapping(),
            record_context(),
        )

        self.assertEqual(first["record_id"], second["record_id"])

    def test_declared_derived_value_must_reconcile(self):
        with self.assertRaisesRegex(MeasurementAdapterError, "does not equal"):
            build_record(value="0.99")

    def test_missing_invalid_and_impossible_measured_values_fail_closed(self):
        cases = [
            ("", "100", "0", "blank"),
            ("NaN", "100", "0", "finite"),
            ("-1", "100", "-0.01", "cannot be negative"),
            ("1", "0", "0", "greater than zero"),
        ]
        for numerator, denominator, value, message in cases:
            with self.subTest(numerator=numerator, denominator=denominator):
                with self.assertRaisesRegex(MeasurementAdapterError, message):
                    build_record(
                        numerator=numerator,
                        denominator=denominator,
                        value=value,
                    )

    def test_nonmeasured_state_requires_reason_and_never_carries_numbers(self):
        with self.assertRaisesRegex(MeasurementAdapterError, "reason_code"):
            build_ratio_measurement_record(
                {}, ratio_mapping(), record_context(), evaluability="excluded"
            )

        record = build_ratio_measurement_record(
            {
                "KRT5_pod_area_um2": "25",
                "region_area_um2": "100",
                "KRT5_pod_area_frac": "0.25",
            },
            ratio_mapping(),
            record_context(),
            evaluability="not_evaluable",
            reason_code="missing_reviewed_tissue_roi",
        )
        self.assert_contract_valid(record)
        self.assertIsNone(record["endpoint"]["numerator"]["value"])
        self.assertIsNone(record["endpoint"]["denominator"]["value"])
        self.assertIsNone(record["endpoint"]["value"])
        self.assertFalse(record["endpoint"]["zero_is_observed"])
        with self.assertRaisesRegex(MeasurementContractError, "only measured"):
            require_aggregation_eligible(record)


class CategoricalStateAdapterTests(unittest.TestCase):
    def assert_contract_valid(self, record):
        VALIDATOR.validate(record)
        validate_measurement_record(record)

    def test_closed_vocabulary_preserves_positive_negative_and_indeterminate(self):
        for state in ("positive", "negative", "indeterminate"):
            with self.subTest(state=state):
                record = build_categorical_state_measurement_record(
                    {"marker_state": state},
                    categorical_mapping(),
                    confocal_cell_context(cell_id=f"C-{state}"),
                )
                self.assert_contract_valid(record)
                self.assertEqual(record["endpoint"]["calculation"], "categorical_state")
                self.assertEqual(record["endpoint"]["state"], state)
                self.assertRegex(record["record_id"], r"^ifqmr-[0-9a-f]{64}$")

    def test_blank_unknown_and_case_drift_fail_closed(self):
        for state, message in (
            ("", "blank"),
            ("uncertain", "not in mapping.allowed_states"),
            ("Positive", "not in mapping.allowed_states"),
        ):
            with self.subTest(state=state):
                with self.assertRaisesRegex(MeasurementAdapterError, message):
                    build_categorical_state_measurement_record(
                        {"marker_state": state},
                        categorical_mapping(),
                        confocal_cell_context(),
                    )

    def test_nonmeasured_state_is_null_and_requires_reason(self):
        record = build_categorical_state_measurement_record(
            {"marker_state": "positive"},
            categorical_mapping(),
            confocal_cell_context(),
            evaluability="not_evaluable",
            reason_code="segmentation_qc_failed",
        )
        self.assert_contract_valid(record)
        self.assertIsNone(record["endpoint"]["state"])
        with self.assertRaisesRegex(MeasurementAdapterError, "reason_code"):
            build_categorical_state_measurement_record(
                {},
                categorical_mapping(),
                confocal_cell_context(),
                evaluability="excluded",
            )

    def test_route_discriminator_rejects_area_wsi_categorical_endpoint(self):
        with self.assertRaisesRegex(
            MeasurementContractError,
            "endpoint.calculation must be one of.*area_wsi",
        ):
            build_categorical_state_measurement_record(
                {"marker_state": "positive"},
                categorical_mapping(),
                record_context(),
            )


class OrdinalAdapterTests(unittest.TestCase):
    def assert_contract_valid(self, record):
        VALIDATOR.validate(record)
        validate_measurement_record(record)

    def test_integer_rank_and_declared_scale_are_preserved(self):
        for rank in (0, 2, 4):
            with self.subTest(rank=rank):
                record = build_ordinal_measurement_record(
                    {"review_rank": rank},
                    ordinal_mapping(),
                    he_section_context(section_id=f"SEC-{rank}"),
                )
                self.assert_contract_valid(record)
                self.assertEqual(record["endpoint"]["calculation"], "ordinal")
                self.assertEqual(record["endpoint"]["rank"], rank)
                self.assertEqual(record["endpoint"]["minimum_rank"], 0)
                self.assertEqual(record["endpoint"]["maximum_rank"], 4)

    def test_fractional_out_of_range_and_invalid_scale_fail_closed(self):
        cases = (
            ({"review_rank": "1.5"}, ordinal_mapping(), "must be an integer"),
            ({"review_rank": "5"}, ordinal_mapping(), "outside the declared"),
            (
                {"review_rank": "1"},
                ordinal_mapping(minimum_rank=4, maximum_rank=0),
                "cannot exceed",
            ),
        )
        for row, mapping, message in cases:
            with self.subTest(row=row, message=message):
                with self.assertRaisesRegex(MeasurementAdapterError, message):
                    build_ordinal_measurement_record(
                        row,
                        mapping,
                        he_section_context(),
                    )

    def test_nonmeasured_ordinal_rank_is_null(self):
        record = build_ordinal_measurement_record(
            {"review_rank": 2},
            ordinal_mapping(),
            he_section_context(),
            evaluability="excluded",
            reason_code="section_not_reviewable",
        )
        self.assert_contract_valid(record)
        self.assertIsNone(record["endpoint"]["rank"])

    def test_route_discriminator_rejects_confocal_ordinal_endpoint(self):
        with self.assertRaisesRegex(
            MeasurementContractError,
            "endpoint.calculation must be one of.*cell_confocal",
        ):
            build_ordinal_measurement_record(
                {"review_rank": 1},
                ordinal_mapping(),
                confocal_cell_context(),
            )


class AggregationBatchContractTests(unittest.TestCase):
    def test_compatible_distinct_records_are_eligible(self):
        require_aggregation_batch_eligible(
            [build_record(region_id="R1"), build_record(region_id="R2")]
        )

    def test_purposive_and_unknown_records_are_observed_units_only(self):
        for design in ("purposive", "unknown"):
            with self.subTest(design=design):
                record = build_record(
                    context=record_context(
                        sampling={
                            "design": design,
                            "inclusion_probability": None,
                            "selection_source": f"{design}-selection",
                        }
                    )
                )
                require_aggregation_eligible(
                    record, target_estimand="observed_units"
                )
                with self.assertRaisesRegex(
                    MeasurementContractError,
                    "requested aggregation estimand does not match",
                ):
                    require_aggregation_eligible(
                        record, target_estimand="whole_section"
                    )

    def test_exhaustive_population_estimands_are_accepted(self):
        for estimand in ("whole_section", "whole_lung"):
            with self.subTest(estimand=estimand):
                record = build_record(
                    context=record_context(
                        sampling={
                            "design": "exhaustive",
                            "inclusion_probability": 1,
                            "selection_source": f"complete-{estimand}-frame",
                            "estimand_scope": estimand,
                        }
                    )
                )
                require_aggregation_eligible(
                    record, target_estimand=estimand
                )

    def test_probability_estimator_must_be_explicitly_supported_by_route(self):
        record = build_record(
            context=record_context(
                sampling={
                    "design": "probability",
                    "inclusion_probability": 0.25,
                    "selection_source": "probability-frame-v1",
                    "estimand_scope": "whole_section",
                    "estimator_profile_id": "weighted-estimator-v1",
                }
            )
        )
        VALIDATOR.validate(record)
        with self.assertRaisesRegex(
            MeasurementContractError,
            "not explicitly supported",
        ):
            require_aggregation_eligible(
                record, target_estimand="whole_section"
            )
        with self.assertRaisesRegex(
            MeasurementContractError,
            "not explicitly supported",
        ):
            require_aggregation_eligible(
                record,
                target_estimand="whole_section",
                supported_probability_estimators={"different-estimator"},
            )
        require_aggregation_eligible(
            record,
            target_estimand="whole_section",
            supported_probability_estimators={"weighted-estimator-v1"},
        )

    def test_whole_section_estimand_requires_section_or_slide_identity(self):
        context = confocal_cell_context(
            section_id=None,
            slide_id=None,
            sampling={
                "design": "exhaustive",
                "inclusion_probability": 1,
                "selection_source": "all-cells-in-unnamed-frame",
                "estimand_scope": "whole_section",
            }
        )
        record = build_categorical_state_measurement_record(
            {"marker_state": "positive"},
            categorical_mapping(),
            context,
        )
        with self.assertRaisesRegex(
            MeasurementContractError,
            "requires a non-mouse record with a section or slide identity",
        ):
            require_aggregation_eligible(
                record, target_estimand="whole_section"
            )

    def test_duplicate_analytical_identity_is_rejected(self):
        record = build_record()
        with self.assertRaisesRegex(MeasurementContractError, "duplicate measurement identity"):
            require_aggregation_batch_eligible([record, copy.deepcopy(record)])

    def test_channel_order_and_roles_cannot_drift(self):
        first = build_record(region_id="R1")
        changed = record_context(
            region_id="R2",
            channel_signature=[
                {"index": 1, "label": "KRT5", "role": "endpoint_numerator"},
                {"index": 2, "label": "DAPI", "role": "nuclear_context"},
            ],
        )
        second = build_record(region_id="R2", context=changed)

        with self.assertRaisesRegex(MeasurementContractError, "channel_signature"):
            require_aggregation_batch_eligible([first, second])

    def test_profile_hash_config_and_model_drift_are_rejected(self):
        first = build_record(region_id="R1")
        cases = {
            "measurement_profile_sha256": record_context(
                region_id="R2", measurement_profile_sha256="d" * 64
            ),
            "config_sha256": record_context(
                region_id="R2", config_sha256="e" * 64
            ),
            "segmentation_model": record_context(
                region_id="R2",
                segmentation_model={
                    "provider": "example",
                    "model_id": "model-v1",
                    "model_sha256": "f" * 64,
                    "profile_id": "segmentation-v1",
                },
            ),
        }
        for field, context in cases.items():
            with self.subTest(field=field):
                second = build_record(region_id="R2", context=context)
                with self.assertRaisesRegex(MeasurementContractError, field):
                    require_aggregation_batch_eligible([first, second])

    def test_categorical_vocabulary_drift_is_rejected(self):
        first = build_categorical_state_measurement_record(
            {"marker_state": "positive"},
            categorical_mapping(vocabulary_id="cell-state-v1"),
            confocal_cell_context(cell_id="C1"),
        )
        second = build_categorical_state_measurement_record(
            {"marker_state": "positive"},
            categorical_mapping(vocabulary_id="cell-state-v2"),
            confocal_cell_context(cell_id="C2"),
        )
        with self.assertRaisesRegex(MeasurementContractError, "endpoint_contract"):
            require_aggregation_batch_eligible([first, second])

    def test_ordinal_scale_drift_is_rejected(self):
        first = build_ordinal_measurement_record(
            {"review_rank": 2},
            ordinal_mapping(maximum_rank=4),
            he_section_context(section_id="SEC1"),
        )
        second = build_ordinal_measurement_record(
            {"review_rank": 2},
            ordinal_mapping(maximum_rank=5),
            he_section_context(section_id="SEC2"),
        )
        with self.assertRaisesRegex(MeasurementContractError, "endpoint_contract"):
            require_aggregation_batch_eligible(
                [first, second], target_estimand="whole_section"
            )

    def test_failed_or_pending_record_cannot_enter_batch(self):
        failed = build_record(
            context=record_context(
                qc={
                    "status": "fail",
                    "reason_codes": ["area_mismatch"],
                    "review_status": "not_required",
                }
            )
        )
        with self.assertRaisesRegex(MeasurementContractError, "cannot enter aggregation"):
            require_aggregation_batch_eligible([failed])

        pending = build_record(
            context=record_context(
                qc={
                    "status": "pass",
                    "reason_codes": [],
                    "review_status": "pending",
                }
            )
        )
        with self.assertRaisesRegex(MeasurementContractError, "pending or rejected"):
            require_aggregation_batch_eligible([pending])

    def test_probability_sampling_rate_cannot_drift(self):
        first = build_record(
            region_id="R1",
            context=record_context(
                region_id="R1",
                sampling={
                    "design": "probability",
                    "inclusion_probability": 0.1,
                    "selection_source": "systematic-grid-v1",
                    "estimator_profile_id": "weighted-estimator-v1",
                },
            ),
        )
        second = build_record(
            region_id="R2",
            context=record_context(
                region_id="R2",
                sampling={
                    "design": "probability",
                    "inclusion_probability": 0.9,
                    "selection_source": "systematic-grid-v1",
                    "estimator_profile_id": "weighted-estimator-v1",
                },
            ),
        )

        with self.assertRaisesRegex(MeasurementContractError, "sampling_contract"):
            require_aggregation_batch_eligible(
                [first, second],
                supported_probability_estimators={"weighted-estimator-v1"},
            )


class MeasurementJsonlWriterTests(unittest.TestCase):
    def test_valid_records_are_written_as_deterministic_json_lines(self):
        records = [build_record(region_id="R1"), build_record(region_id="R2")]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "measurement-records.jsonl"
            write_measurement_records_jsonl(output, records)

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            parsed = [json.loads(line) for line in lines]
            self.assertEqual(parsed, records)
            for record in parsed:
                VALIDATOR.validate(record)
                validate_measurement_record(record)

    def test_invalid_later_record_cannot_replace_existing_output(self):
        valid = build_record(region_id="R1")
        invalid = build_record(region_id="R2")
        invalid["endpoint"]["value"] = 999

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "measurement-records.jsonl"
            output.write_text("prior canonical\n", encoding="utf-8")
            with self.assertRaisesRegex(MeasurementAdapterError, "record 1 is invalid"):
                write_measurement_records_jsonl(output, [valid, invalid])

            self.assertEqual(
                output.read_text(encoding="utf-8"), "prior canonical\n"
            )
            self.assertEqual(
                list(output.parent.glob(f".{output.name}.*.tmp")), []
            )

    def test_duplicate_identity_is_rejected_before_write(self):
        record = build_record()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "measurement-records.jsonl"
            with self.assertRaisesRegex(MeasurementAdapterError, "duplicate record_id"):
                write_measurement_records_jsonl(output, [record, copy.deepcopy(record)])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
