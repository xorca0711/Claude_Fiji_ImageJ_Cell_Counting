import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from scripts.render_authority_status import (
    ContractError,
    main,
    render_authority,
    validate_contract,
)


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_PATH = ROOT / "authority" / "project_state.json"
SCHEMA_PATH = ROOT / "schemas" / "project-state.schema.json"
GENERATED_PATH = ROOT / "docs" / "generated" / "AUTHORITY_STATUS.md"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class AuthorityContractTests(unittest.TestCase):
    def setUp(self):
        self.authority = load_json(AUTHORITY_PATH)

    def test_schema_basics_and_canonical_payload(self):
        schema = load_json(SCHEMA_PATH)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.1.0")
        self.assertTrue(
            {
                "authority",
                "evidence",
                "analysis_population",
                "study_design",
                "claim_boundary",
                "modalities",
                "project_gates",
            }.issubset(schema["required"])
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(self.authority)
        validate_contract(self.authority)

    def test_render_is_deterministic_and_checked_in_view_is_current(self):
        first = render_authority(self.authority)
        second = render_authority(copy.deepcopy(self.authority))
        self.assertEqual(first, second)
        self.assertEqual(first, GENERATED_PATH.read_text(encoding="utf-8"))
        self.assertEqual(main(["--check"]), 0)

    def test_privacy_validation_rejects_local_absolute_path(self):
        path_forms = [
            "Source artifact: C:\\Users\\researcher\\private\\release.csv",
            "source=D:\\private\\release.csv",
            "source=/mnt/private/release.csv",
            "source=~/private/release.csv",
            "source=$HOME/private/release.csv",
            "source=%USERPROFILE%\\private\\release.csv",
        ]
        for path_form in path_forms:
            with self.subTest(path_form=path_form):
                contaminated = copy.deepcopy(self.authority)
                contaminated["claim_boundary"]["permitted"].append(path_form)
                with self.assertRaisesRegex(ContractError, "privacy validation rejected"):
                    validate_contract(contaminated)

    def test_unknown_root_property_is_rejected(self):
        contaminated = copy.deepcopy(self.authority)
        contaminated["unreviewed_claim"] = True
        with self.assertRaisesRegex(ContractError, "unknown keys"):
            validate_contract(contaminated)

    def test_he_stage_rendering_is_order_independent(self):
        reordered = copy.deepcopy(self.authority)
        modalities = {item["id"]: item for item in reordered["modalities"]}
        modalities["he_histology"]["stages"].reverse()
        rendered = render_authority(reordered)
        self.assertIn("| H&E H0-H3 | `ENGINEERING_QC_ONLY` |", rendered)
        self.assertIn("| H&E H4+ | `UNVALIDATED_BLOCKED` |", rendered)

    def test_exact_critical_states(self):
        modalities = {item["id"]: item for item in self.authority["modalities"]}
        confocal = modalities["confocal_selected_fields"]
        release = confocal["release"]
        self.assertEqual((release["quantified_fields"], release["expected_fields"]), (80, 80))
        self.assertEqual(release["sampling_frame"], "selected_fields_nonprobability")

        exceptions = {item["field_id"]: item for item in confocal["field_exceptions"]}
        override = exceptions["M4-2_LEFT_F06"]
        self.assertEqual(override["release_state"], "WHOLE_FIELD_TISSUE_OVERRIDE_INCLUDED")
        self.assertTrue(override["included_in_quantified_count"])
        self.assertEqual(override["tissue_denominator_um2"], 405000)
        self.assertEqual(
            override["comparability"],
            "NONCOMPARABLE_PENDING_COMPARABLE_TISSUE_ROI",
        )
        self.assertEqual(
            exceptions["M4-1_RIGHT_F07"]["release_state"],
            "PARTIAL_FIELD_INCLUDED",
        )
        self.assertTrue(exceptions["M4-1_RIGHT_F07"]["included_in_quantified_count"])

        design = self.authority["study_design"]
        self.assertEqual(design["factorial_structure"], "CROSSED_2X2")
        self.assertEqual(design["population_scope"], "DAY_28_IMAGED_SURVIVORS")
        self.assertEqual(design["terminal_imaged_animals_per_cell"], 1)
        self.assertEqual(design["inference_status"], "DESCRIPTIVE_ONLY_NO_GROUP_INFERENCE")

        evidence = {item["artifact_id"]: item for item in self.authority["evidence"]}
        self.assertEqual(
            evidence["settled_csv_release_v1_0"]["sha256"],
            "0bd690fdb37ca763810c7e8451a3d92f6ef951e4bfa4e69c6bc27512ae8d1dd7",
        )
        population = self.authority["analysis_population"]
        self.assertEqual(population["terminal_endpoint_population"], "DAY_28_IMAGED_SURVIVORS")
        self.assertEqual(population["additional_heterozygous_infected_deaths_disclosed"], 2)
        self.assertFalse(population["survival_analysis_performed"])

        self.assertEqual(modalities["he_histology"]["status"], "ENGINEERING_QC_ONLY")
        self.assertEqual(
            modalities["he_histology"]["stages"],
            [
                {"range": "H0-H3", "status": "ENGINEERING_QC_ONLY"},
                {"range": "H4+", "status": "UNVALIDATED_BLOCKED"},
            ],
        )
        self.assertEqual(modalities["wsi_threshold_pilot"]["tile_count"], 6)
        self.assertEqual(modalities["wsi_threshold_pilot"]["status"], "ENGINEERING_PILOT_ONLY")
        gates = {item["id"]: item for item in self.authority["project_gates"]}
        self.assertEqual(gates["G-CONTRACT-INTEGRATION"]["status"], "ENGINEERING_COMPLETE")
        self.assertEqual(
            {gate["status"] for gate in self.authority["project_gates"] if gate["id"] != "G-CONTRACT-INTEGRATION"},
            {"OPEN_SCIENTIFIC_BLOCKER"},
        )
        self.assertIn("G-SEGMENTATION-VALIDATION", gates)


if __name__ == "__main__":
    unittest.main()
