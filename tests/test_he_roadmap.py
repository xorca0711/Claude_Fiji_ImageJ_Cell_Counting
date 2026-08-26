import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HeRoadmapTests(unittest.TestCase):
    def setUp(self):
        self.roadmap = json.loads(
            (ROOT / "config" / "brightfield" / "he_feature_roadmap.json").read_text(
                encoding="utf-8"
            )
        )

    def test_features_are_structured_and_fail_closed_by_release(self):
        features = self.roadmap["features"]
        self.assertEqual(
            [item["id"] for item in features],
            [f"HE-F{i:02d}" for i in range(1, 15)],
        )
        self.assertFalse(self.roadmap["launcher_route_enabled"])
        self.assertEqual(self.roadmap["biological_unit"], "mouse")
        for item in features:
            self.assertIn(
                item["status"],
                {
                    "PARTIAL_FAIL_CLOSED_INVENTORY",
                    "REVIEWED_LOCKED_CURRENT_COHORT",
                    "COHORT_REVIEWED_R1",
                    "DEVELOPMENT_CONTEXT_ONLY",
                    "RUBRIC_DEFINED_REVIEW_REQUIRED",
                    "LOCKED_RUBRIC_AND_AGGREGATION_GATE_READY_REVIEW_REQUIRED",
                    "ORDINAL_ENGINEERING_READY_QUANTITATIVE_ENDPOINTS_UNAVAILABLE",
                    "ORDINAL_ENGINEERING_READY_REVIEW_REQUIRED",
                    "R1_PROVENANCE_VERIFIED",
                    "R1_PROVENANCE_VERIFIED_H7_H8_AUDIT_READY",
                    "UNAVAILABLE",
                },
            )
            self.assertTrue(item["stages"])
            self.assertTrue(item["implementation"])
            self.assertTrue(item["outputs"])
            self.assertTrue(item["acceptance"])
            self.assertTrue(item["remaining_blocker"])

    def test_launcher_levels_require_features_and_do_not_overclaim(self):
        levels = self.roadmap["release_levels"]
        self.assertEqual([item["id"] for item in levels], [f"R{i}" for i in range(5)])
        self.assertEqual(levels[0]["status"], "AVAILABLE_SUPERSEDED_BY_R1")
        self.assertEqual(levels[1]["status"], "AVAILABLE_APPROVED_CURRENT")
        self.assertEqual(levels[2]["status"], "DEVELOPMENT_REVIEW_REQUIRED")
        self.assertEqual(levels[3]["status"], "UNAVAILABLE")
        self.assertEqual(levels[4]["status"], "UNAVAILABLE")
        known = {item["id"] for item in self.roadmap["features"]}
        for gate in self.roadmap["launcher_enablement"].values():
            self.assertTrue(set(gate["required_features"]).issubset(known))
        self.assertTrue(
            any("immune lineage" in item.lower() for item in self.roadmap["non_goals"])
        )


if __name__ == "__main__":
    unittest.main()
