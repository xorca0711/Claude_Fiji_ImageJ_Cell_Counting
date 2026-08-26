import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aggregate_to_mouse as aggregation_module

from aggregate_to_mouse import (
    AggregationAuditError,
    CODE_ROLE_PATHS_BY_STAGE,
    REQUIRED_CODE_ROLES_BY_STAGE,
    artifact_descriptor,
    build_aggregation_audit,
    repository_python_code_closure,
    validate_aggregation_audit,
    write_json_atomic,
)


ROOT = Path(__file__).resolve().parents[1]


class AggregationCodeProvenanceTests(unittest.TestCase):
    @staticmethod
    def _canonical_sha256(value):
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _reseal(self, audit):
        audit["code_set_sha256"] = self._canonical_sha256(
            audit["code_artifacts"]
        )
        unsealed = dict(audit)
        unsealed.pop("audit_payload_sha256", None)
        audit["audit_payload_sha256"] = self._canonical_sha256(unsealed)

    def test_repository_import_closures_are_exact_and_deterministic(self):
        audit_path = ROOT / "synthetic.audit.json"
        stage4_first, stage4_snapshots = repository_python_code_closure(
            [("stage4_aggregator", ROOT / "aggregate_to_mouse.py")], audit_path
        )
        stage4_second, _ = repository_python_code_closure(
            [("stage4_aggregator", ROOT / "aggregate_to_mouse.py")], audit_path
        )
        self.assertEqual(stage4_first, stage4_second)
        self.assertEqual(
            {artifact["role"] for artifact in stage4_first},
            set(REQUIRED_CODE_ROLES_BY_STAGE["stage4_mouse_aggregation"]),
        )
        self.assertEqual(
            {Path(path).resolve() for _, path in stage4_snapshots},
            {
                ROOT / "aggregate_to_mouse.py",
                ROOT / "ifquant" / "__init__.py",
                ROOT / "ifquant" / "stage2_index.py",
                ROOT / "ifquant" / "contracts.py",
                ROOT / "ifquant" / "adapters.py",
                ROOT / "ifquant" / "route_records.py",
            },
        )

        stage3, stage3_snapshots = repository_python_code_closure(
            [
                ("stage3_aggregator", ROOT / "aggregate_tiles_to_slide.py"),
                ("aggregation_library", ROOT / "aggregate_to_mouse.py"),
            ],
            audit_path,
        )
        self.assertEqual(
            {artifact["role"] for artifact in stage3},
            set(REQUIRED_CODE_ROLES_BY_STAGE["stage3_slide_aggregation"]),
        )
        self.assertEqual(len(stage3_snapshots), 7)
        for _, source_path in stage3_snapshots:
            resolved = Path(source_path).resolve(strict=True)
            self.assertEqual(resolved.suffix, ".py")
            resolved.relative_to(ROOT)

    def test_stage_contract_rejects_an_incomplete_code_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "aggregate_to_mouse.py"
            source = root / "input.csv"
            output = root / "output.csv"
            audit_path = root / "audit.json"
            code.write_text("# incomplete closure\n", encoding="utf-8")
            source.write_text("value\n1\n", encoding="utf-8")
            output.write_text("value\n1\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AggregationAuditError, "exact repository Python code closure"
            ):
                build_aggregation_audit(
                    stage="stage4_mouse_aggregation",
                    arguments={},
                    code_artifacts=[
                        artifact_descriptor(
                            code, "stage4_aggregator", audit_path
                        )
                    ],
                    input_artifacts=[
                        artifact_descriptor(
                            source, "aggregation_input_summary", audit_path
                        )
                    ],
                    output_artifacts=[
                        artifact_descriptor(
                            output, "stage4_mouse_summary", audit_path
                        )
                    ],
                )

    def test_transitive_module_tamper_invalidates_complete_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit_path = root / "audit.json"
            code_artifacts = []
            code_paths = {}
            code_payloads = {}
            for role, repository_path in sorted(
                CODE_ROLE_PATHS_BY_STAGE["stage4_mouse_aggregation"].items()
            ):
                path = root / repository_path
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = f"# {role}\n"
                path.write_text(payload, encoding="utf-8")
                code_paths[role] = path
                code_payloads[role] = payload
                descriptor = artifact_descriptor(path, role, audit_path)
                descriptor["repository_path"] = repository_path
                code_artifacts.append(descriptor)
            source = root / "input.csv"
            output = root / "output.csv"
            source.write_text("value\n1\n", encoding="utf-8")
            output.write_text("value\n1\n", encoding="utf-8")
            audit = build_aggregation_audit(
                stage="stage4_mouse_aggregation",
                arguments={"input_mode": "synthetic"},
                code_artifacts=code_artifacts,
                input_artifacts=[
                    artifact_descriptor(
                        source, "aggregation_input_summary", audit_path
                    )
                ],
                output_artifacts=[
                    artifact_descriptor(
                        output, "stage4_mouse_summary", audit_path
                    )
                ],
            )
            self.assertRegex(
                audit["repository_code_set_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertEqual(
                audit["repository_code_set_sha256"],
                aggregation_module._repository_code_set_sha256(
                    audit["code_artifacts"]
                ),
            )
            write_json_atomic(audit_path, audit)
            validate_aggregation_audit(
                audit_path, expected_stage="stage4_mouse_aggregation"
            )

            for role, path in code_paths.items():
                with self.subTest(role=role):
                    path.write_text(
                        code_payloads[role] + "# tampered\n", encoding="utf-8"
                    )
                    with self.assertRaisesRegex(
                        AggregationAuditError,
                        f"{role}.*(size drift|SHA-256 drift)",
                    ):
                        validate_aggregation_audit(audit_path)
                    path.write_text(code_payloads[role], encoding="utf-8")
                    validate_aggregation_audit(audit_path)

    def test_fully_resealed_missing_extra_or_moved_roles_still_fail_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit_path = root / "audit.json"
            code_artifacts = []
            for role, repository_path in sorted(
                CODE_ROLE_PATHS_BY_STAGE["stage4_mouse_aggregation"].items()
            ):
                path = root / repository_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {role}\n", encoding="utf-8")
                descriptor = artifact_descriptor(path, role, audit_path)
                descriptor["repository_path"] = repository_path
                code_artifacts.append(descriptor)
            source = root / "input.csv"
            output = root / "output.csv"
            source.write_text("value\n1\n", encoding="utf-8")
            output.write_text("value\n1\n", encoding="utf-8")
            baseline = build_aggregation_audit(
                stage="stage4_mouse_aggregation",
                arguments={},
                code_artifacts=code_artifacts,
                input_artifacts=[
                    artifact_descriptor(
                        source, "aggregation_input_summary", audit_path
                    )
                ],
                output_artifacts=[
                    artifact_descriptor(
                        output, "stage4_mouse_summary", audit_path
                    )
                ],
            )

            def missing_role(document):
                document["code_artifacts"] = [
                    artifact
                    for artifact in document["code_artifacts"]
                    if artifact["role"] != "stage2_index_contract"
                ]

            def extra_role(document):
                extra = dict(document["code_artifacts"][0])
                extra["role"] = "repository_python:unexpected.py"
                extra["repository_path"] = "unexpected.py"
                document["code_artifacts"].append(extra)

            def moved_role(document):
                for artifact in document["code_artifacts"]:
                    if artifact["role"] == "ifquant_package_init":
                        artifact["repository_path"] = "ifquant/not_init.py"
                        break

            for name, mutate, message in (
                ("missing", missing_role, "missing=stage2_index_contract"),
                ("extra", extra_role, "unexpected=repository_python"),
                ("moved", moved_role, "declares repository_path"),
            ):
                with self.subTest(name=name):
                    changed = copy.deepcopy(baseline)
                    mutate(changed)
                    self._reseal(changed)
                    write_json_atomic(audit_path, changed)
                    with self.assertRaisesRegex(AggregationAuditError, message):
                        validate_aggregation_audit(
                            audit_path,
                            expected_stage="stage4_mouse_aggregation",
                            verify_files=False,
                        )

    def test_prepublication_code_drift_leaves_no_canonical_stage4_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "run_summary.csv"
            aggregation_module.write_csv(
                source,
                [
                    {
                        "image": "field-01",
                        "region": "whole_tissue",
                        "section_id": "field-01",
                        "mouse_id": "M1",
                        "genotype": "G",
                        "condition": "C",
                        "panel": "P",
                        "region_area_um2": "100",
                        "n_nuclei": "10",
                    }
                ],
            )
            real_verify = aggregation_module.verify_artifact_descriptor
            calls = 0
            initial_closure_size = len(
                REQUIRED_CODE_ROLES_BY_STAGE["stage4_mouse_aggregation"]
            )

            def fail_on_prepublication_recheck(descriptor, path):
                nonlocal calls
                calls += 1
                if calls > initial_closure_size:
                    raise AggregationAuditError("synthetic code drift")
                return real_verify(descriptor, path)

            arguments = [
                "aggregate_to_mouse.py",
                str(source),
                "--outdir",
                str(root),
                "--sampling-unit",
                "field",
            ]
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(
                aggregation_module,
                "verify_artifact_descriptor",
                side_effect=fail_on_prepublication_recheck,
            ):
                with self.assertRaisesRegex(SystemExit, "synthetic code drift"):
                    aggregation_module.main()

            self.assertFalse((root / "mouse_level_summary.csv").exists())
            self.assertFalse((root / "group_level_summary.csv").exists())
            self.assertFalse(
                (root / aggregation_module.STAGE4_AUDIT_FILENAME).exists()
            )
            self.assertTrue(list(root.glob("mouse_level_summary.STALE.*.csv")))
            self.assertTrue(list(root.glob("group_level_summary.STALE.*.csv")))


if __name__ == "__main__":
    unittest.main()
