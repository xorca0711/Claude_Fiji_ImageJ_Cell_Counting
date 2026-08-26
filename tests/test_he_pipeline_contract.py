import ast
import importlib.util
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "he_pipeline.py"
SPEC = importlib.util.spec_from_file_location("he_pipeline", MODULE_PATH)
he_pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(he_pipeline)


class HePipelineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.study = json.loads(
            (ROOT / "config" / "studies" / "g_surf_he_20260812.json").read_text(
                encoding="utf-8"
            )
        )
        cls.rubric = json.loads(
            (
                ROOT
                / "config"
                / "brightfield"
                / "he_pathology_review_rubric.json"
            ).read_text(encoding="utf-8")
        )
        cls.profile = json.loads(
            (
                ROOT
                / "config"
                / "brightfield"
                / "he_stain_profiles"
                / "g_surf_he_20260812_reviewed_locked_v1.json"
            ).read_text(encoding="utf-8")
        )

    def test_runner_is_valid_python(self):
        ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    def test_study_records_approved_r1_and_exact_blinding(self):
        self.assertEqual(self.study["current_release"]["id"], "R1")
        self.assertEqual(
            self.study["current_release"]["decision"], "APPROVED_IMAGE_QC"
        )
        self.assertEqual(len(self.study["blind_section_map"]), 8)
        expected = set(he_pipeline.expected_sections(self.study))
        observed = {row["section_id"] for row in self.study["blind_section_map"]}
        self.assertEqual(observed, expected)
        packages = [sample["source_package"] for sample in self.study["samples"]]
        self.assertTrue(
            all(
                package["discovery_authority"]
                == "bioformats_ImageReader_getUsedFiles"
                and len(package["members"]) == 5
                for package in packages
            )
        )
        self.assertEqual(
            sum(len(package["members"]) for package in packages), 20
        )

    def test_package_roots_default_to_exact_relocated_study_contract_paths(self):
        approved = self.study["approved_packages"]
        expected_r1 = Path(
            r"D:\IFQ_Runs\H&E_20260812\legacy\pre_mapping_analysis_20260818"
            r"\10_R1_IMAGE_QC_APPROVED_FINAL"
        )
        expected_h4 = Path(
            r"D:\IFQ_Runs\H&E_20260812\legacy\pre_mapping_analysis_20260818"
            r"\Review\13_H4_SPATIALLY_BALANCED_REGION_REVIEW"
        )
        self.assertEqual(Path(approved["r1_root"]), expected_r1)
        self.assertEqual(Path(approved["h4_development_root"]), expected_h4)

        r1_root, h4_root, evidence = he_pipeline.resolve_package_roots(self.study)
        self.assertEqual(r1_root, expected_r1)
        self.assertEqual(h4_root, expected_h4)
        self.assertEqual(evidence["r1"]["selection_authority"], "study_contract")
        self.assertEqual(evidence["h4"]["selection_authority"], "study_contract")
        self.assertEqual(
            evidence["r1"]["contract_field"], "approved_packages.r1_root"
        )
        self.assertEqual(
            evidence["h4"]["contract_field"],
            "approved_packages.h4_development_root",
        )

    def test_package_root_overrides_are_explicit_and_relative_roots_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            override_r1 = root / "r1"
            override_h4 = root / "h4"
            r1_root, h4_root, evidence = he_pipeline.resolve_package_roots(
                self.study, override_r1, override_h4
            )
            self.assertEqual(r1_root, override_r1)
            self.assertEqual(h4_root, override_h4)
            self.assertEqual(evidence["r1"]["selection_authority"], "cli_override")
            self.assertEqual(evidence["h4"]["selection_authority"], "cli_override")

        invalid = json.loads(json.dumps(self.study))
        invalid["approved_packages"]["r1_root"] = "relative/latest-r1"
        with self.assertRaisesRegex(
            he_pipeline.ContractError, "approved_packages.r1_root must be an absolute"
        ):
            he_pipeline.resolve_package_roots(invalid)

    def test_status_cli_uses_contract_roots_unless_overridden(self):
        with mock.patch.object(
            he_pipeline.sys, "argv", ["he_pipeline.py", "status"]
        ):
            defaults = he_pipeline.parse_args()
        self.assertIsNone(defaults.r1_root)
        self.assertIsNone(defaults.h4_root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(
                he_pipeline.sys,
                "argv",
                [
                    "he_pipeline.py",
                    "status",
                    "--r1-root",
                    str(root / "r1"),
                    "--h4-root",
                    str(root / "h4"),
                ],
            ):
                overridden = he_pipeline.parse_args()
        self.assertEqual(overridden.r1_root, root / "r1")
        self.assertEqual(overridden.h4_root, root / "h4")

    def test_source_package_validation_is_exact_and_content_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_name = "slide.vsi"
            source_bytes = b"vsi-container"
            ets_bytes = b"ets-pixels"
            ets_two_bytes = b"ets-overview"
            (root / source_name).write_bytes(source_bytes)
            ets_path = root / "_slide_" / "stack1" / "frame_t.ets"
            ets_path.parent.mkdir(parents=True)
            ets_path.write_bytes(ets_bytes)
            ets_two_path = root / "_slide_" / "stack10000" / "frame_t.ets"
            ets_two_path.parent.mkdir(parents=True)
            ets_two_path.write_bytes(ets_two_bytes)
            members = [
                {
                    "relative_path": source_name,
                    "size_bytes": len(source_bytes),
                    "sha256": hashlib.sha256(source_bytes).hexdigest(),
                },
                {
                    "relative_path": "_slide_/stack1/frame_t.ets",
                    "size_bytes": len(ets_bytes),
                    "sha256": hashlib.sha256(ets_bytes).hexdigest(),
                },
                {
                    "relative_path": "_slide_/stack10000/frame_t.ets",
                    "size_bytes": len(ets_two_bytes),
                    "sha256": hashlib.sha256(ets_two_bytes).hexdigest(),
                },
            ]
            members.sort(key=lambda item: item["relative_path"])
            package_payload = "".join(
                f"{item['relative_path']}\t{item['size_bytes']}\t{item['sha256']}\n"
                for item in members
            ).encode("utf-8")
            study = {
                "modality": "brightfield_he",
                "source_root": str(root),
                "expected_mouse_count": 1,
                "expected_analytical_sections": 1,
                "samples": [
                    {
                        "mouse_id": "M1",
                        "source_file": source_name,
                        "section_ids": ["M1_BF_01"],
                        "source_package": {
                            "format": "olympus_vsi",
                            "discovery_authority": (
                                "bioformats_ImageReader_getUsedFiles"
                            ),
                            "package_hash_algorithm": (
                                "sha256_utf8_path_tab_size_tab_sha256_lf"
                            ),
                            "package_sha256": hashlib.sha256(
                                package_payload
                            ).hexdigest(),
                            "members": members,
                        },
                    }
                ],
                "blind_section_map": [
                    {"blind_section_id": "HE-001", "section_id": "M1_BF_01"}
                ],
            }
            result = he_pipeline.validate_study(study)
            self.assertEqual(result["source_packages"]["M1"]["member_count"], 3)
            self.assertEqual(
                result["source_package_authority"],
                "bioformats_used_files_members_with_size_and_sha256",
            )

            ets_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(he_pipeline.ContractError, "size mismatch"):
                he_pipeline.validate_study(study)

    def test_locked_profile_is_reviewer_approved_but_not_pathology_authorized(self):
        self.assertEqual(self.profile["status"], "REVIEWED_LOCKED")
        self.assertEqual(self.profile["review"]["decision"], "APPROVED_IMAGE_QC")
        restrictions = " ".join(self.profile["restrictions"])
        self.assertIn("Not authorized for lesion", restrictions)

    def test_whole_section_review_replaces_tile_as_primary_unit(self):
        self.assertEqual(self.rubric["review_unit"], "blinded_whole_section")
        fields, rows = he_pipeline.section_review_rows(self.study, self.rubric)
        self.assertEqual(len(rows), 8)
        self.assertEqual({row["blind_section_id"] for row in rows},
                         {f"HE-{index:03d}" for index in range(1, 9)})
        self.assertIn("whole_section_inflammation_extent_0_4_uncertain", fields)
        self.assertNotIn("mouse_id", fields)
        self.assertNotIn("genotype", fields)
        self.assertNotIn("condition", fields)

    def test_stage_contract_blocks_unavailable_claims(self):
        rows = he_pipeline.stage_rows(
            {"locked_profile_id": self.profile["profile_id"]},
            {"candidate_count": 96},
        )
        by_stage = {row["stage"]: row for row in rows}
        self.assertEqual(list(by_stage), [f"H{index}" for index in range(10)])
        self.assertEqual(by_stage["H2"]["status"], "APPROVED_R1")
        self.assertEqual(by_stage["H5"]["status"], "BLOCKED")
        self.assertEqual(by_stage["H8"]["status"], "BLOCKED")

    def test_rubric_preserves_interpretation_boundaries(self):
        hard_rules = " ".join(self.rubric["hard_rules"])
        self.assertIn("cannot identify immune lineage", hard_rules)
        self.assertIn("cannot identify a KRT5-positive pod", hard_rules)
        self.assertIn("supporting tiles are evidence locators", hard_rules)


if __name__ == "__main__":
    unittest.main()
