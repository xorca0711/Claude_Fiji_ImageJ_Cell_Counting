import copy
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from ifquant.stage2_index import (
    RunDeclaration,
    Stage2IndexError,
    build_stage2_index,
    canonical_sha256,
    declared_marker_ids,
    validate_stage2_index,
    write_stage2_index_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "stage2-run-index.schema.json"
CLI_PATH = ROOT / "scripts" / "build_stage2_run_index.py"
STAGE3_PATH = ROOT / "aggregate_tiles_to_slide.py"
GENERATED_UTC = "2026-08-25T00:00:00Z"
SUMMARY_HEADER = [
    "output_key",
    "region",
    "section_id",
    "mouse_id",
    "genotype",
    "condition",
    "panel",
    "region_area_um2",
    "n_nuclei",
]


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv_for_test(path):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def rehash_index(document):
    payload = copy.deepcopy(document)
    payload.pop("index_sha256", None)
    document["index_sha256"] = canonical_sha256(payload)


class SyntheticStage2Run:
    """Tiny, non-biological Stage 1/2 artifacts with the production layout."""

    def __init__(self, root):
        self.root = Path(root)
        self.slide_dir = self.root / "synthetic_slide"
        self.tiles_dir = self.slide_dir / "tiles"
        self.tiles_dir.mkdir(parents=True)
        self.stage1_manifest = self.root / "stage1_manifest.json"
        self.stage2_script = self.root / "fake_stage2.groovy"
        self.tile_manifest = self.slide_dir / "tile_manifest.csv"
        self.stage2_script.write_bytes(b"// deterministic fake Stage 2 script\n")

        self.tile_rows = [
            {
                "tile_id": "tile-001",
                "tile_file": "tile-001.tif",
                "roiset_file": "tile-001_rois.zip",
                "section_id": "section-001",
                "mouse_id": "mouse-01",
                "genotype": "WT",
                "condition": "mock",
                "panel": "LEFT",
                "core_tissue_area_um2": "300",
                "core_raster_area_um2": "300",
            },
            {
                "tile_id": "tile-002",
                "tile_file": "tile-002.tif",
                "roiset_file": "tile-002_rois.zip",
                "section_id": "section-002",
                "mouse_id": "mouse-01",
                "genotype": "WT",
                "condition": "mock",
                "panel": "LEFT",
                "core_tissue_area_um2": "100",
                "core_raster_area_um2": "100",
            },
        ]
        for index, row in enumerate(self.tile_rows, start=1):
            (self.tiles_dir / row["tile_file"]).write_bytes(
                f"fake-tile-{index}".encode("ascii")
            )
            (self.tiles_dir / row["roiset_file"]).write_bytes(
                f"fake-roi-{index}".encode("ascii")
            )
        self.write_stage1()
        self.write_tile_manifest()
        self.declarations = [
            self.write_run(
                "shard_01",
                [0],
                regions_by_tile={0: ["damaged_parenchyma", "intact_parenchyma"]},
            ),
            self.write_run("shard_02", [1]),
        ]

    def write_stage1(self):
        write_json(
            self.stage1_manifest,
            {
                "status": "complete",
                "slides": [
                    {
                        "slide_stem": self.slide_dir.name,
                        "n_tiles": len(self.tile_rows),
                        "coverage_complete": True,
                        "dry_run": False,
                        "n_skipped_low_tissue": 0,
                        "tissue_area_mm2": 0.0004,
                        "tissue_threshold_otsu": 1.0,
                        "source_vsi": "synthetic.vsi",
                        "series_index": 0,
                    }
                ],
            },
        )

    def write_tile_manifest(self):
        write_csv(self.tile_manifest, list(self.tile_rows[0]), self.tile_rows)

    def write_run(
        self,
        name,
        tile_indices,
        *,
        regions_by_tile=None,
        config=None,
        summary_header=None,
        manifest_patch=None,
        output_keys=None,
        signatures=None,
        process_exit_code=0,
    ):
        analysis_dir = self.slide_dir / "analysis" / name
        samplesheet = analysis_dir / "samplesheet.csv"
        selected = [self.tile_rows[index] for index in tile_indices]
        sample_header = [
            "filename",
            "section_id",
            "mouse_id",
            "genotype",
            "condition",
            "panel",
        ]
        sample_rows = [
            {
                "filename": row["tile_file"],
                "section_id": row["section_id"],
                "mouse_id": row["mouse_id"],
                "genotype": row["genotype"],
                "condition": row["condition"],
                "panel": row["panel"],
            }
            for row in selected
        ]
        write_csv(samplesheet, sample_header, sample_rows)

        output_keys = output_keys or {}
        signatures = signatures or {}
        images = []
        summary_rows = []
        for index in tile_indices:
            tile = self.tile_rows[index]
            output_key = output_keys.get(index, f"output-{index + 1:03d}")
            images.append(
                {
                    "status": "success",
                    "file": tile["tile_file"],
                    "output_key": output_key,
                    "panel": tile["panel"],
                    "channel_signature": signatures.get(
                        index, "C1-DAPI_C2-KRT5_C3-PDPN"
                    ),
                }
            )
            regions = (regions_by_tile or {}).get(index, ["damaged_parenchyma"])
            for region_number, region in enumerate(regions, start=1):
                summary_rows.append(
                    {
                        "output_key": output_key,
                        "region": region,
                        "section_id": tile["section_id"],
                        "mouse_id": tile["mouse_id"],
                        "genotype": tile["genotype"],
                        "condition": tile["condition"],
                        "panel": tile["panel"],
                        "region_area_um2": str(100 * region_number),
                        "n_nuclei": str(10 * region_number),
                    }
                )

        manifest = {
            "status": "complete",
            "success_count": len(selected),
            "failure_count": 0,
            "skipped_count": 0,
            "output_failure_count": 0,
            "matched_input_count": len(selected),
            "analytical_input_count": len(selected),
            "config": config
            or {
                "threshold_profile": "synthetic-v1",
                "minimum_region_area_um2": 10,
            },
            "images": images,
        }
        if manifest_patch:
            manifest.update(manifest_patch)
        write_json(analysis_dir / "run_manifest.json", manifest)
        header = summary_header or SUMMARY_HEADER
        for row in summary_rows:
            for column in header:
                row.setdefault(column, "synthetic")
        write_csv(analysis_dir / "run_summary.csv", header, summary_rows)
        return RunDeclaration(analysis_dir, samplesheet, process_exit_code)

    def build(self, declarations=None):
        return build_stage2_index(
            slide_dir=self.slide_dir,
            stage1_manifest=self.stage1_manifest,
            stage2_script=self.stage2_script,
            runs=self.declarations if declarations is None else declarations,
            generated_utc=GENERATED_UTC,
        )

    def publish(self, document=None):
        document = self.build() if document is None else document
        path = self.slide_dir / "stage2_run_index.json"
        write_stage2_index_atomic(document, path)
        return path


class Stage2RunIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.fixture = SyntheticStage2Run(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assertBuildFails(self, pattern, declarations=None):
        with self.assertRaisesRegex(Stage2IndexError, pattern):
            self.fixture.build(declarations)

    def test_valid_two_shard_index_and_multi_region_tile(self):
        document = self.fixture.build()

        self.assertEqual(document["status"], "stage2_integrity_complete")
        self.assertEqual([run["role"] for run in document["runs"]], ["shard", "shard"])
        self.assertEqual(document["coverage"]["expected_tile_count"], 2)
        self.assertEqual(document["coverage"]["declared_input_count"], 2)
        self.assertEqual(document["coverage"]["successful_input_count"], 2)
        self.assertEqual(document["coverage"]["summary_row_count"], 3)
        self.assertEqual(document["coverage"]["summary_identity_count"], 3)
        self.assertEqual(document["runs"][0]["summary_identity_count"], 2)
        self.assertEqual(
            document["declared_channel_signatures"],
            [{"panel": "LEFT", "signature": "C1-DAPI_C2-KRT5_C3-PDPN"}],
        )
        self.assertNotIn(str(self.fixture.root), json.dumps(document))

        index_path = self.fixture.publish(document)
        validated = validate_stage2_index(
            index_path,
            slide_dir=self.fixture.slide_dir,
            stage1_manifest=self.fixture.stage1_manifest,
            stage2_script=self.fixture.stage2_script,
        )
        self.assertEqual(validated.document, document)
        self.assertEqual(len(validated.summary_paths), 2)
        self.assertTrue(all(path.is_file() for path in validated.summary_paths))
        self.assertEqual(len(validated.index_sha256), 64)

    def test_schema_is_valid_and_accepts_built_index(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).validate(self.fixture.build())

    def test_schema_and_runtime_reject_invalid_or_non_utc_generation_time(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for timestamp in ("not-a-date", "2026-08-25T09:00:00+09:00"):
            with self.subTest(timestamp=timestamp):
                document = self.fixture.build()
                document["generated_utc"] = timestamp
                rehash_index(document)
                with self.assertRaises(ValidationError):
                    validator.validate(document)
                index_path = self.fixture.publish(document)
                with self.assertRaisesRegex(Stage2IndexError, "RFC 3339 UTC"):
                    validate_stage2_index(
                        index_path,
                        slide_dir=self.fixture.slide_dir,
                        stage1_manifest=self.fixture.stage1_manifest,
                        stage2_script=self.fixture.stage2_script,
                    )

    def test_schema_and_runtime_accept_double_digit_channel_indices(self):
        signature = "_".join(f"C{index}-M{index}" for index in range(1, 11))
        declarations = [
            self.fixture.write_run(
                "ten_channel_01", [0], signatures={0: signature}
            ),
            self.fixture.write_run(
                "ten_channel_02", [1], signatures={1: signature}
            ),
        ]
        document = self.fixture.build(declarations)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(document)
        self.assertEqual(
            document["declared_channel_signatures"][0]["signature"], signature
        )

    def test_channel_labels_normalize_to_summary_marker_ids(self):
        self.assertEqual(
            declared_marker_ids(
                "C1-DAPI_C2-KRT5-488_C3-Pro-SPC-555_C4-T1alpha-647"
            ),
            ("DAPI", "KRT5", "PROSPC", "T1A"),
        )
        self.assertEqual(declared_marker_ids("C1-DAPI_C2-Ki-67"), ("DAPI", "KI67"))

    def test_panel_signature_controls_which_blank_marker_columns_are_structural(self):
        self.fixture.tile_rows[1]["panel"] = "RIGHT"
        self.fixture.write_tile_manifest()
        declarations = [
            self.fixture.write_run(
                "mixed_left",
                [0],
                signatures={0: "C1-DAPI_C2-KRT5_C3-PDPN"},
            ),
            self.fixture.write_run(
                "mixed_right",
                [1],
                signatures={1: "C1-DAPI_C2-Pro-SPC-488_C3-AGER-555"},
            ),
        ]
        for declaration in declarations:
            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, rows = read_csv_for_test(summary_path)
            header.extend(["KRT5_pos_count", "ProSPC_pos_count"])
            for row in rows:
                if row["panel"] == "LEFT":
                    row.update(KRT5_pos_count="0", ProSPC_pos_count="")
                else:
                    row.update(KRT5_pos_count="", ProSPC_pos_count="0")
            write_csv(summary_path, header, rows)

        self.fixture.build(declarations)

        right_summary = declarations[1].analysis_dir / "run_summary.csv"
        header, rows = read_csv_for_test(right_summary)
        rows[0]["ProSPC_pos_count"] = ""
        write_csv(right_summary, header, rows)
        with self.assertRaisesRegex(
            Stage2IndexError, "ProSPC_pos_count.*declared panel marker"
        ):
            self.fixture.build(declarations)

    def test_schema_and_runtime_accept_ordered_sparse_channel_mapping(self):
        signature = "C2-DAPI_C3-KRT5_C4-PDPN"
        declarations = [
            self.fixture.write_run("sparse_01", [0], signatures={0: signature}),
            self.fixture.write_run("sparse_02", [1], signatures={1: signature}),
        ]
        document = self.fixture.build(declarations)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(document)
        self.assertEqual(
            document["declared_channel_signatures"][0]["signature"], signature
        )

    def test_schema_rejects_unsafe_paths_unknown_fields_and_partial_status(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        document = self.fixture.build()

        cases = []
        unsafe = copy.deepcopy(document)
        unsafe["runs"][0]["analysis_dir"] = "../outside"
        cases.append(unsafe)
        unknown = copy.deepcopy(document)
        unknown["undeclared"] = True
        cases.append(unknown)
        partial = copy.deepcopy(document)
        partial["status"] = "partial"
        cases.append(partial)

        for candidate in cases:
            with self.subTest(candidate=candidate.get("status", "unknown")):
                with self.assertRaises(ValidationError):
                    validator.validate(candidate)

    def test_changed_output_key_cannot_hide_duplicate_natural_identity(self):
        # The same input is deliberately redeclared under another output key.
        # Rejection must not depend on the mutable output filename/key.
        declarations = [
            self.fixture.write_run("shard_01", [0]),
            self.fixture.write_run(
                "shard_02",
                [0, 1],
                output_keys={0: "renamed-output-001", 1: "output-002"},
            ),
        ]
        self.assertBuildFails("duplicate tile assignment", declarations)

    def test_duplicate_natural_identity_in_summary_is_rejected(self):
        duplicate = self.fixture.write_run(
            "duplicate_identity",
            [0, 1],
            regions_by_tile={
                0: ["damaged_parenchyma", "damaged_parenchyma"],
                1: ["damaged_parenchyma"],
            },
        )
        self.assertBuildFails("duplicate analytical identity", [duplicate])

    def test_overlapping_shard_assignments_are_rejected(self):
        declarations = [
            self.fixture.write_run("shard_01", [0]),
            self.fixture.write_run(
                "shard_02",
                [0, 1],
                output_keys={0: "rerun-output-001", 1: "output-002"},
                regions_by_tile={0: ["intact_parenchyma"]},
            ),
        ]
        self.assertBuildFails("duplicate tile assignment", declarations)

    def test_missing_tile_assignment_is_rejected(self):
        declarations = [self.fixture.write_run("only_shard", [0])]
        self.assertBuildFails("do not exactly cover tile_manifest.csv", declarations)

    def test_config_header_and_channel_divergence_are_rejected(self):
        cases = (
            (
                "config",
                self.fixture.write_run(
                    "shard_02",
                    [1],
                    config={"threshold_profile": "different-v2"},
                ),
                "resolved configs differ",
            ),
            (
                "header",
                self.fixture.write_run(
                    "shard_02_header",
                    [1],
                    summary_header=SUMMARY_HEADER + ["unexpected_metric"],
                ),
                "summary headers differ",
            ),
            (
                "channel",
                self.fixture.write_run(
                    "shard_02_channel",
                    [1],
                    signatures={1: "C1-DAPI_C2-KRT5_C3-ACTA2"},
                ),
                "channel signature differs within panel",
            ),
        )

        for label, second_declaration, pattern in cases:
            with self.subTest(label=label):
                declarations = [
                    self.fixture.write_run("fresh_shard_01_" + label, [0]),
                    second_declaration,
                ]
                self.assertBuildFails(pattern, declarations)

    def test_nonzero_and_partial_runs_are_rejected(self):
        nonzero = self.fixture.write_run(
            "nonzero", [0, 1], process_exit_code=23
        )
        self.assertBuildFails("did not exit cleanly.*exit=23", [nonzero])

        partial = self.fixture.write_run(
            "partial",
            [0, 1],
            manifest_patch={"status": "partial"},
        )
        self.assertBuildFails("status is not complete", [partial])

        skipped = self.fixture.write_run(
            "skipped",
            [0, 1],
            manifest_patch={"skipped_count": 1},
        )
        self.assertBuildFails("reports skipped inputs", [skipped])

    def test_complete_manifest_cannot_hide_non_success_image_record(self):
        declaration = self.fixture.write_run("hidden_failure", [0, 1])
        manifest_path = declaration.analysis_dir / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["images"].append(
            {
                "status": "failed",
                "file": "undeclared-retry.tif",
                "error": "synthetic failure hidden by inconsistent counters",
            }
        )
        write_json(manifest_path, manifest)

        self.assertBuildFails("contains a non-success image record", [declaration])

    def test_malformed_csv_extra_field_is_controlled_rejection(self):
        summary_path = self.fixture.declarations[0].analysis_dir / "run_summary.csv"
        lines = summary_path.read_text(encoding="utf-8").splitlines()
        lines[1] += ",unexpected-extra-field"
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertBuildFails("more fields than its header")

    def test_nuclei_measurement_is_required_finite_and_nonnegative(self):
        without_nuclei = [c for c in SUMMARY_HEADER if c != "n_nuclei"]
        missing = self.fixture.write_run("missing_nuclei", [0, 1])
        missing_path = missing.analysis_dir / "run_summary.csv"
        _, missing_rows = read_csv_for_test(missing_path)
        for row in missing_rows:
            row.pop("n_nuclei")
        write_csv(missing_path, without_nuclei, missing_rows)
        self.assertBuildFails("missing required columns: n_nuclei", [missing])

        nonfinite = self.fixture.write_run("nonfinite_nuclei", [0, 1])
        path = nonfinite.analysis_dir / "run_summary.csv"
        header, rows = read_csv_for_test(path)
        rows[0]["n_nuclei"] = "NaN"
        write_csv(path, header, rows)
        self.assertBuildFails("n_nuclei must be finite and non-negative", [nonfinite])

    def test_unsafe_rehashed_index_is_rejected(self):
        document = self.fixture.build()
        document["runs"][0]["analysis_dir"] = "../outside"
        rehash_index(document)
        index_path = self.fixture.publish(document)

        with self.assertRaisesRegex(Stage2IndexError, "parent traversal"):
            validate_stage2_index(
                index_path,
                slide_dir=self.fixture.slide_dir,
                stage1_manifest=self.fixture.stage1_manifest,
                stage2_script=self.fixture.stage2_script,
            )

    def test_manifest_input_paths_must_be_plain_filenames(self):
        rows = [dict(row) for row in self.fixture.tile_rows]
        rows[0]["tile_file"] = "../tiles/tile-001.tif"
        write_csv(self.fixture.tile_manifest, list(rows[0]), rows)
        self.assertBuildFails("plain filename")

    def test_tampered_index_and_tampered_artifact_are_rejected(self):
        document = self.fixture.build()
        index_path = self.fixture.publish(document)
        altered = copy.deepcopy(document)
        altered["coverage"]["expected_tile_count"] = 999
        write_json(index_path, altered)
        with self.assertRaisesRegex(Stage2IndexError, "self-hash does not match"):
            validate_stage2_index(
                index_path,
                slide_dir=self.fixture.slide_dir,
                stage1_manifest=self.fixture.stage1_manifest,
                stage2_script=self.fixture.stage2_script,
            )

        index_path = self.fixture.publish(document)
        summary_path = self.fixture.slide_dir / document["runs"][0]["run_summary"]["path"]
        original = summary_path.read_text(encoding="utf-8")
        summary_path.write_text(
            original.replace(",100,10\n", ",101,10\n", 1), encoding="utf-8"
        )
        with self.assertRaisesRegex(Stage2IndexError, "no longer matches"):
            validate_stage2_index(
                index_path,
                slide_dir=self.fixture.slide_dir,
                stage1_manifest=self.fixture.stage1_manifest,
                stage2_script=self.fixture.stage2_script,
            )

    def test_cli_build_and_check(self):
        command = [
            sys.executable,
            str(CLI_PATH),
            "--slide-dir",
            str(self.fixture.slide_dir),
            "--stage1-manifest",
            str(self.fixture.stage1_manifest),
            "--stage2-script",
            str(self.fixture.stage2_script),
        ]
        for declaration in self.fixture.declarations:
            command.extend(
                [
                    "--run",
                    declaration.analysis_dir.relative_to(self.fixture.slide_dir).as_posix(),
                    declaration.samplesheet.relative_to(self.fixture.slide_dir).as_posix(),
                    "0",
                ]
            )
        built = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertIn("Wrote complete Stage 2 index", built.stdout)

        checked = subprocess.run(
            command[:8] + ["--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("Stage 2 index verified", checked.stdout)

    def test_stage3_consumes_only_indexed_outputs(self):
        self.fixture.publish()
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
        ]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        output = self.fixture.root / "stats" / "slide_level_summary.csv"
        self.assertTrue(output.is_file())
        with output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qc_status"], "ok")
        self.assertEqual(rows[0]["stage2_source_mode"], "explicit_hashed_index")
        self.assertEqual(rows[0]["n_tiles_expected"], "2")
        self.assertEqual(rows[0]["n_summary_rows"], "3")
        self.assertEqual(rows[0]["stage2_region_area_um2"], "400.0")

    def test_stage3_refuses_valid_subset_when_stage1_declares_missing_slide(self):
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["slides"].append({
            "slide_stem": "declared_but_missing",
            "n_tiles": 1,
            "coverage_complete": True,
            "dry_run": False,
            "n_skipped_low_tissue": 0,
        })
        write_json(self.fixture.stage1_manifest, stage1)
        self.fixture.publish()
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
        ]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("Stage 1 declares this slide", result.stdout)
        self.assertFalse(
            (self.fixture.root / "stats" / "slide_level_summary.csv").exists()
        )
        rejected = self.fixture.root / "stats" / "slide_level_summary.REJECTED.csv"
        self.assertTrue(rejected.is_file())
        with rejected.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertTrue(all(row["qc_status"] == "PROBLEM" for row in rows))
        self.assertTrue(all(row["dataset_qc_status"] == "PROBLEM" for row in rows))

    def test_stage3_rejects_nonfinite_additive_measurement(self):
        for declaration_number, declaration in enumerate(self.fixture.declarations):
            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, rows = read_csv_for_test(summary_path)
            header.append("KRT5_pod_area_um2")
            for row_number, row in enumerate(rows):
                row["KRT5_pod_area_um2"] = (
                    "NaN" if declaration_number == 0 and row_number == 0 else "1"
                )
            write_csv(summary_path, header, rows)
        self.fixture.publish()
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
        ]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("invalid/non-finite", result.stdout)
        self.assertFalse(
            (self.fixture.root / "stats" / "slide_level_summary.csv").exists()
        )

    def test_index_rejects_all_blank_measurement_for_declared_marker(self):
        for declaration in self.fixture.declarations:
            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, rows = read_csv_for_test(summary_path)
            header.append("KRT5_pod_area_um2")
            for row in rows:
                row["KRT5_pod_area_um2"] = ""
            write_csv(summary_path, header, rows)
        with self.assertRaisesRegex(
            Stage2IndexError, "KRT5_pod_area_um2.*declared panel marker"
        ):
            self.fixture.build()

    def test_measured_zero_for_declared_marker_is_not_missing(self):
        for declaration in self.fixture.declarations:
            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, rows = read_csv_for_test(summary_path)
            header.append("KRT5_pod_area_um2")
            for row in rows:
                row["KRT5_pod_area_um2"] = "0"
            write_csv(summary_path, header, rows)
        document = self.fixture.build()
        self.fixture.publish(document)
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
        ]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        output = self.fixture.root / "stats" / "slide_level_summary.csv"
        with output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["KRT5_pod_area_um2"], "0.0")

    def test_stage3_allows_blank_measurement_for_marker_absent_from_panel(self):
        for declaration in self.fixture.declarations:
            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, rows = read_csv_for_test(summary_path)
            header.append("ProSPC_pos_count")
            for row in rows:
                row["ProSPC_pos_count"] = ""
            write_csv(summary_path, header, rows)
        self.fixture.publish()
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
        ]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        output = self.fixture.root / "stats" / "slide_level_summary.csv"
        with output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertNotIn("ProSPC_pos_count", rows[0])

    def test_stage3_invalid_index_quarantines_prior_analytical_output(self):
        index_path = self.fixture.publish()
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
        ]
        first = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        canonical = self.fixture.root / "stats" / "slide_level_summary.csv"
        self.assertTrue(canonical.is_file())

        document = json.loads(index_path.read_text(encoding="utf-8"))
        document["coverage"]["expected_tile_count"] = 999
        write_json(index_path, document)
        second = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(second.returncode, 2, second.stderr + second.stdout)
        self.assertFalse(canonical.exists())
        stale = list((self.fixture.root / "stats").glob("slide_level_summary.STALE.*.csv"))
        self.assertEqual(len(stale), 1)
        self.assertIn("invalid Stage 2 index", second.stdout)

    def test_stage3_malformed_manifest_quarantines_prior_output(self):
        self.fixture.publish()
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
        ]
        first = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        canonical = self.fixture.root / "stats" / "slide_level_summary.csv"
        self.assertTrue(canonical.is_file())

        lines = self.fixture.tile_manifest.read_text(encoding="utf-8").splitlines()
        lines[1] += ",unexpected-extra-field"
        self.fixture.tile_manifest.write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        second = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(second.returncode, 2, second.stderr + second.stdout)
        self.assertIn("more fields than its header", second.stdout)
        self.assertFalse(canonical.exists())
        self.assertEqual(
            len(list((self.fixture.root / "stats").glob(
                "slide_level_summary.STALE.*.csv"
            ))),
            1,
        )

    def test_stage3_legacy_recursive_mode_is_diagnostic_only(self):
        command = [
            sys.executable,
            str(STAGE3_PATH),
            "--slide-root",
            str(self.fixture.root),
            "--stage2-script",
            str(self.fixture.stage2_script),
            "--seam-counts",
            "off",
            "--legacy-recursive-discovery",
        ]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertFalse((self.fixture.root / "stats" / "slide_level_summary.csv").exists())
        rejected = self.fixture.root / "stats" / "slide_level_summary.REJECTED.csv"
        self.assertTrue(rejected.is_file())
        with rejected.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["qc_status"], "PROBLEM")
        self.assertIn("diagnostic-only", rows[0]["qc_notes"])


if __name__ == "__main__":
    unittest.main()
