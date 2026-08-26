import copy
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from aggregate_to_mouse import (
    STAGE3_AUDIT_FILENAME,
    validate_aggregation_audit,
)
from ifquant.stage2_index import (
    RunDeclaration,
    Stage2IndexError,
    build_stage2_index,
    canonical_marker_id,
    canonical_sha256,
    declared_marker_ids,
    sha256_file,
    validate_stage2_index,
    write_stage2_index_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "stage2-run-index.schema.json"
WSI_MASK_SCHEMA_PATH = ROOT / "schemas" / "wsi-reference-mask-profile.schema.json"
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
    "stardist_label_count",
    "stardist_label_canonical_pixel_sha256",
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
        self.stage1_script = self.root / "fake_stage1.groovy"
        self.stage2_script = self.root / "fake_stage2.groovy"
        self.tile_manifest = self.slide_dir / "tile_manifest.csv"
        self.tile_candidate_manifest = self.slide_dir / "tile_candidate_manifest.csv"
        self.reference_mask = (
            self.slide_dir / "reference_space" / "automatic_dapi_tissue_raster.tif"
        )
        self.reference_mask_pixels = (
            self.slide_dir
            / "reference_space"
            / "analysis_tissue_mask_pixels.uint8"
        )
        self.reference_mask.parent.mkdir(parents=True)
        self.reference_mask.write_bytes(b"synthetic-binary-reference-mask\n")
        self.reference_mask_pixels.write_bytes(
            bytes([255]) * 100 + bytes(256 * 128 - 100)
        )
        self.stage1_script.write_bytes(b"// deterministic fake Stage 1 script\n")
        self.stage2_script.write_bytes(b"// deterministic fake Stage 2 script\n")

        self.tile_rows = [
            {
                "tile_id": "x000000_y000000",
                "tile_file": "tile-001.tif",
                "roiset_file": "tile-001_rois.zip",
                "section_id": "section-001",
                "mouse_id": "mouse-01",
                "genotype": "WT",
                "condition": "mock",
                "panel": "LEFT",
                "source_vsi": "synthetic_slide.vsi",
                "series_index": "0",
                "pixel_size_um": "0.5",
                "pixel_size_um_y": "0.5",
                "core_x": "0",
                "core_y": "0",
                "core_w": "2048",
                "core_h": "2048",
                "export_x": "0",
                "export_y": "0",
                "export_w": "2176",
                "export_h": "2048",
                "halo_left": "0",
                "halo_top": "0",
                "halo_right": "128",
                "halo_bottom": "0",
                "core_tissue_area_px": "1200",
                "core_tissue_area_um2": "300",
                "core_raster_area_um2": "300",
            },
            {
                "tile_id": "x002048_y000000",
                "tile_file": "tile-002.tif",
                "roiset_file": "tile-002_rois.zip",
                "section_id": "section-002",
                "mouse_id": "mouse-01",
                "genotype": "WT",
                "condition": "mock",
                "panel": "LEFT",
                "source_vsi": "synthetic_slide.vsi",
                "series_index": "0",
                "pixel_size_um": "0.5",
                "pixel_size_um_y": "0.5",
                "core_x": "2048",
                "core_y": "0",
                "core_w": "2048",
                "core_h": "2048",
                "export_x": "1920",
                "export_y": "0",
                "export_w": "2176",
                "export_h": "2048",
                "halo_left": "128",
                "halo_top": "0",
                "halo_right": "0",
                "halo_bottom": "0",
                "core_tissue_area_px": "400",
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
        self.write_tile_candidate_manifest()
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

    def write_stage1(self, n_channels=4):
        self.source_n_channels = n_channels
        channel_names = ["DAPI", "FITC", "Cy3", "Cy5"]
        if n_channels > len(channel_names):
            channel_names.extend(
                f"Channel-{index}"
                for index in range(len(channel_names) + 1, n_channels + 1)
            )
        ordered_patterns = [
            f"(?i)^{re.escape(name)}$" for name in channel_names[:n_channels]
        ]
        source_members = [
            {
                "relative_path": "_synthetic_/stack1/frame_t.ets",
                "size_bytes": 11,
                "sha256": hashlib.sha256(b"fake-ets-01").hexdigest(),
            },
            {
                "relative_path": "synthetic_slide.vsi",
                "size_bytes": 12,
                "sha256": hashlib.sha256(b"fake-vsi-001").hexdigest(),
            },
        ]
        package_lines = "".join(
            f"{member['relative_path']}\t{member['size_bytes']}\t{member['sha256']}\n"
            for member in source_members
        )
        source_package = {
            "format": "olympus_vsi",
            "source_vsi": "synthetic_slide.vsi",
            "discovery_authority": "bioformats_ImageReader_getUsedFiles",
            "package_hash_algorithm": "sha256_utf8_path_tab_size_tab_sha256_lf",
            "members": source_members,
            "package_sha256": hashlib.sha256(package_lines.encode("utf-8")).hexdigest(),
        }
        write_json(
            self.stage1_manifest,
            {
                "schema_version": "1.3",
                "stage": "wsi_tile_export",
                "status": "complete",
                "stage1_script": {
                    "name": self.stage1_script.name,
                    "size_bytes": self.stage1_script.stat().st_size,
                    "sha256": sha256_file(self.stage1_script),
                },
                "qupath_series_selection": {
                    "expect_channels": n_channels,
                    "ordered_channel_patterns": ordered_patterns,
                    "channel_order_authority": (
                        "acquisition_order_pattern_only_not_biological_identity"
                    ),
                },
                "tiling": {"core_px": 2048, "halo_px": 128},
                "tissue": {
                    "mode": "automatic_dapi_otsu_engineering",
                    "threshold_method": "Otsu",
                    "downsample": 16.0,
                    "airway_exclusion": "not_available",
                },
                "export": {"format": "OME-TIFF", "compression": "ZLIB"},
                "downstream": {"panel": "LEFT", "partition_damage": False},
                "slides": [
                    {
                        "slide_stem": self.slide_dir.name,
                        "n_tiles": len(self.tile_rows),
                        "n_written": len(self.tile_rows),
                        "n_resumed": 0,
                        "coverage_complete": True,
                        "max_tiles_cap": 0,
                        "dry_run": False,
                        "n_skipped_low_tissue": 0,
                        "tissue_area_mm2": 0.0004,
                        "sum_core_tissue_mm2": 0.0004,
                        "seam_check_rel_diff": 0.0,
                        "tissue_threshold_otsu": 1.0,
                        "source_vsi": "synthetic_slide.vsi",
                        "source_package": source_package,
                        "series_index": 0,
                        "series_name": "synthetic analytical series",
                        "width": 4096,
                        "height": 2048,
                        "pixel_size_um": 0.5,
                        "pixel_size_um_y": 0.5,
                        "n_channels": n_channels,
                        "channel_names": channel_names[:n_channels],
                        "ordered_channel_names": channel_names[:n_channels],
                        "ordered_channel_patterns": ordered_patterns,
                        "channel_order_authority": (
                            "acquisition_order_pattern_only_not_biological_identity"
                        ),
                        "reference_space": {
                            "mode": "automatic_dapi_otsu_engineering",
                            "authority": (
                                "generated_dapi_otsu_raster_and_content_bound_tile_rois"
                            ),
                            "profile_id": "automatic_dapi_otsu_engineering",
                            "review_state": "engineering_unreviewed",
                            "review_protocol_id": "automatic_dapi_otsu",
                            "coordinate_space": "selected_series_downsample_grid",
                            "mask_logic": "dapi_otsu_tissue_without_airway_exclusion",
                            "sampling_semantics": (
                                "exhaustive_grid_over_declared_reference_space"
                            ),
                            "airway_excluded": False,
                            "downsample": 16.0,
                            "mask_width": 256,
                            "mask_height": 128,
                            "analysis_tissue_foreground_px": 100,
                            "tissue_mask": {
                                "published_relative_path": (
                                    "reference_space/automatic_dapi_tissue_raster.tif"
                                ),
                                "content": {
                                    "name": self.reference_mask.name,
                                    "size_bytes": self.reference_mask.stat().st_size,
                                    "sha256": sha256_file(self.reference_mask),
                                },
                            },
                            "analysis_tissue_mask_pixels": {
                                "encoding": "row_major_uint8_0_255",
                                "width": 256,
                                "height": 128,
                                "published_relative_path": (
                                    "reference_space/analysis_tissue_mask_pixels.uint8"
                                ),
                                "content": {
                                    "name": self.reference_mask_pixels.name,
                                    "size_bytes": self.reference_mask_pixels.stat().st_size,
                                    "sha256": sha256_file(self.reference_mask_pixels),
                                },
                            },
                            "content_verified_before_and_after": True,
                        },
                        "tile_candidate_manifest": self.tile_candidate_manifest.name,
                        "n_grid_cores_total": len(self.tile_rows),
                        "n_grid_cores_visited": len(self.tile_rows),
                        "n_outside_tissue": 0,
                        "n_skipped_empty_raster": 0,
                        "n_candidate_exported": len(self.tile_rows),
                        "n_candidate_resumed": 0,
                        "n_candidate_dry_run": 0,
                    }
                ],
            },
        )

    def write_tile_candidate_manifest(self):
        rows = [
            {
                "tile_id": row["tile_id"],
                "core_x": row["core_x"],
                "core_y": row["core_y"],
                "core_w": row["core_w"],
                "core_h": row["core_h"],
                "core_tissue_area_px": row["core_tissue_area_px"],
                "core_tissue_area_um2": row["core_tissue_area_um2"],
                "status": "exported",
                "reason": "synthetic fixture",
            }
            for index, row in enumerate(self.tile_rows)
        ]
        write_csv(self.tile_candidate_manifest, list(rows[0]), rows)

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
        resolved_config = copy.deepcopy(config) if config is not None else {
            "threshold_profile": "synthetic-v1",
            "minimum_region_area_um2": 10,
        }
        resolved_config.setdefault("samplesheetSha256", sha256_file(samplesheet))
        resolved_config.setdefault("panelMapSha256", None)
        resolved_config.setdefault("canonicalManifestSha256", None)
        inactive_stardist_authority = {
            "active": False,
            "authority": "not_applicable_classic",
            "api_command": None,
            "model_choice": None,
            "model_path": None,
            "model_content": None,
            "model_archive": None,
            "runtime_manifest_path": None,
            "runtime_manifest_content": None,
            "runtime_profile_id": None,
            "runtime_artifacts": [],
            "class_bindings": [],
        }
        resolved_config.setdefault("segmenter", "classic")
        resolved_config.setdefault("stardistModelChoice", None)
        resolved_config.setdefault("stardistModelSha256", None)
        resolved_config.setdefault("stardistModelAuthority", "not_applicable_classic")
        resolved_config.setdefault("stardistAuthority", inactive_stardist_authority)
        resolved_config.setdefault("prob", 0.5)
        resolved_config.setdefault("nms", 0.4)
        resolved_config.setdefault("tiles", 1)
        engine_script_content = {
            "name": self.stage2_script.name,
            "size_bytes": self.stage2_script.stat().st_size,
            "sha256": sha256_file(self.stage2_script),
        }
        resolved_config.setdefault("engineScript", engine_script_content)
        images = []
        summary_rows = []
        for index in tile_indices:
            tile = self.tile_rows[index]
            output_key = output_keys.get(index, f"output-{index + 1:03d}")
            signature = signatures.get(index, "C1-DAPI_C2-KRT5_C3-PDPN")
            images.append(
                {
                    "status": "success",
                    "file": tile["tile_file"],
                    "output_key": output_key,
                    "panel": tile["panel"],
                    "channel_signature": signature,
                    "params_relative_path": (
                        f"{output_key}/{signature}__params.json"
                    ),
                    "source_content": {
                        "name": tile["tile_file"],
                        "size_bytes": (self.tiles_dir / tile["tile_file"]).stat().st_size,
                        "sha256": sha256_file(self.tiles_dir / tile["tile_file"]),
                    },
                    "source_content_verified_before_and_after": True,
                }
            )
            channel_map = []
            for token in signature.split("_"):
                channel_token, file_label = token.split("-", 1)
                marker = canonical_marker_id(file_label)
                channel_map.append(
                    {
                        "idx": int(channel_token[1:]),
                        "fileLabel": file_label,
                        "marker": marker,
                        "role": "nuclear" if marker == "DAPI" else "cyto",
                    }
                )
            params_document = {
                "image": tile["tile_file"],
                "output_key": output_key,
                "source_content": images[-1]["source_content"],
                "engine_script": engine_script_content,
                "panel": tile["panel"],
                "channel_signature": signature,
                "segmenter": resolved_config["segmenter"],
                "stardist_model_choice": resolved_config["stardistModelChoice"],
                "stardist_model_sha256": resolved_config["stardistModelSha256"],
                "stardist_model_authority": resolved_config["stardistModelAuthority"],
                "stardist_prob": resolved_config["prob"],
                "stardist_nms": resolved_config["nms"],
                "stardist_tiles": resolved_config["tiles"],
                "stardist_runtime": {
                    key: copy.deepcopy(value)
                    for key, value in resolved_config["stardistAuthority"].items()
                    if key not in {"model_path", "runtime_manifest_path"}
                }
                | {"label_outputs": []},
                "calibration": {
                    "pixel_width_um": 0.5,
                    "pixel_height_um": 0.5,
                    "n_channels": self.source_n_channels,
                },
                "channel_map": channel_map,
                "routing_input_hashes": {
                    "samplesheet_sha256": resolved_config["samplesheetSha256"],
                    "panel_map_sha256": resolved_config["panelMapSha256"],
                    "canonical_manifest_sha256": resolved_config[
                        "canonicalManifestSha256"
                    ],
                },
            }
            marker_path = str(resolved_config.get("markerRegistryPath") or "")
            if marker_path and marker_path.lower() not in {"unavailable", "none", "null"}:
                params_document["marker_registry"] = {
                    "path": marker_path,
                    "status": "external_file_bound",
                    "sha256": resolved_config.get("markerRegistrySha256"),
                }
            panel_path = str(resolved_config.get("panelConfigPath") or "")
            if panel_path and panel_path.lower() not in {"built_in_only", "none", "null"}:
                params_document["custom_panel_config"] = panel_path
                params_document["custom_panel_config_status"] = "external_file_bound"
                params_document["custom_panel_config_sha256"] = resolved_config.get(
                    "panelConfigSha256"
                )
            write_json(
                analysis_dir / output_key / f"{signature}__params.json",
                params_document,
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
                        "stardist_label_count": "",
                        "stardist_label_canonical_pixel_sha256": "",
                    }
                )

        manifest = {
            "status": "complete",
            "engine_script": engine_script_content,
            "engine_script_verified_before_and_after": True,
            "input_content_authority": (
                "sha256_streamed_bytes_verified_before_and_after_analysis"
            ),
            "stardist_authority_verified_before_and_after": False,
            "success_count": len(selected),
            "failure_count": 0,
            "skipped_count": 0,
            "output_failure_count": 0,
            "matched_input_count": len(selected),
            "analytical_input_count": len(selected),
            "versions": {
                "imagej_version": "1.54p99",
                "bioformats_version": "8.5.0",
                "java_version": "21.0.7",
                "java_vendor": "Synthetic OpenJDK",
                "java_runtime": "Synthetic Runtime 21.0.7",
                "java_vm": "Synthetic VM 21.0.7",
                "timestamp": "ignored",
            },
            "config": resolved_config,
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

    def test_atomic_index_writer_does_not_follow_predictable_temp_hardlink(self):
        output = self.fixture.root / "stage2_run_index.json"
        predictable = output.with_name(output.name + ".tmp")
        victim = self.fixture.root / "victim.txt"
        victim.write_text("do not overwrite\n", encoding="utf-8")
        try:
            os.link(victim, output)
            os.link(victim, predictable)
        except OSError as exc:
            self.skipTest(f"hard links unavailable for atomic-writer test: {exc}")

        write_stage2_index_atomic({"status": "safe"}, output)

        self.assertEqual(victim.read_text(encoding="utf-8"), "do not overwrite\n")
        self.assertEqual(predictable.read_text(encoding="utf-8"), "do not overwrite\n")
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"status": "safe"})

    def assertBuildFails(self, pattern, declarations=None):
        with self.assertRaisesRegex(Stage2IndexError, pattern):
            self.fixture.build(declarations)

    def make_stardist_config(self, profile_id="synthetic-stardist-v1", token="a"):
        runtime_dir = self.fixture.root / f"stardist-runtime-{token}"
        runtime_dir.mkdir()

        def content(path):
            return {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }

        model_path = runtime_dir / f"model-{token}.zip"
        model_path.write_bytes(f"synthetic-model-{token}".encode("ascii"))
        role_classes = {
            "stardist_plugin": [
                "de.csbdresden.stardist.StarDist2D",
                "de.csbdresden.stardist.StarDist2DNMS",
            ],
            "csbdeep_plugin": [
                "de.csbdresden.csbdeep.commands.GenericNetwork"
            ],
            "tensorflow_java": ["org.tensorflow.Graph"],
            "tensorflow_native": [],
        }
        runtime_artifacts = []
        manifest_artifacts = []
        class_bindings = []
        for role, expected_classes in role_classes.items():
            suffix = ".dll" if role == "tensorflow_native" else ".jar"
            path = runtime_dir / f"{role}-{token}{suffix}"
            path.write_bytes(f"synthetic-{role}-{token}".encode("ascii"))
            identity = content(path)
            runtime_artifacts.append(
                {
                    "role": role,
                    "path": str(path.resolve()),
                    "expected_classes": expected_classes,
                    "content": identity,
                }
            )
            manifest_artifacts.append(
                {
                    "role": role,
                    "path": path.name,
                    "size_bytes": identity["size_bytes"],
                    "sha256": identity["sha256"],
                    "expected_classes": expected_classes,
                }
            )
            class_bindings.extend(
                {
                    "class_name": class_name,
                    "code_source_path": str(path.resolve()),
                }
                for class_name in expected_classes
            )
        runtime_manifest_path = runtime_dir / f"runtime-{token}.json"
        write_json(
            runtime_manifest_path,
            {
                "schema_version": "1.0.0",
                "profile_id": profile_id,
                "artifacts": manifest_artifacts,
            },
        )
        authority = {
            "active": True,
            "authority": "explicit_model_and_closed_runtime_manifest_content_bound",
            "api_command": "de.csbdresden.stardist.StarDist2D",
            "model_choice": "Model (.zip) from File",
            "model_path": str(model_path.resolve()),
            "model_content": content(model_path),
            "model_archive": {
                "archive_format": "zip",
                "entry_count": 2,
                "file_count": 2,
            },
            "runtime_manifest_path": str(runtime_manifest_path.resolve()),
            "runtime_manifest_content": content(runtime_manifest_path),
            "runtime_profile_id": profile_id,
            "runtime_artifacts": runtime_artifacts,
            "class_bindings": class_bindings,
        }
        return {
            "threshold_profile": "synthetic-v1",
            "minimum_region_area_um2": 10,
            "segmenter": "stardist",
            "stardistModelChoice": authority["model_choice"],
            "stardistModelSha256": authority["model_content"]["sha256"],
            "stardistModelAuthority": authority["authority"],
            "stardistAuthority": authority,
            "prob": 0.5,
            "nms": 0.4,
            "tiles": 1,
        }

    def populate_stardist_label_outputs(self, declarations):
        output_paths = []
        for declaration in declarations:
            manifest_path = declaration.analysis_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["stardist_authority_verified_before_and_after"] = True
            write_json(manifest_path, manifest)
            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, summary_rows = read_csv_for_test(summary_path)
            rows_by_output = {}
            for row in summary_rows:
                rows_by_output.setdefault(row["output_key"], []).append(row)
            for image in manifest["images"]:
                tile = next(
                    row for row in self.fixture.tile_rows if row["tile_file"] == image["file"]
                )
                params_path = declaration.analysis_dir / image["params_relative_path"]
                params = json.loads(params_path.read_text(encoding="utf-8"))
                label_outputs = []
                for row in rows_by_output[image["output_key"]]:
                    region = row["region"]
                    label_name = f"labels-{re.sub(r'[^A-Za-z0-9._-]+', '-', region)}.tif"
                    label_path = (
                        declaration.analysis_dir / image["output_key"] / label_name
                    )
                    label_path.write_bytes(
                        f"synthetic-labels-{image['output_key']}-{region}".encode(
                            "utf-8"
                        )
                    )
                    output_paths.append(label_path)
                    canonical_pixel_sha256 = hashlib.sha256(
                        f"pixels-{image['output_key']}-{region}".encode("utf-8")
                    ).hexdigest()
                    label_outputs.append(
                        {
                            "command_output": "label",
                            "output_type": "Label Image",
                            "pixel_encoding": "unsigned_16_bit_labels",
                            "canonical_hash_encoding": (
                                "ifq_stardist_label_u16le_row_major_v1"
                            ),
                            "width_pixels": int(tile["export_w"]),
                            "height_pixels": int(tile["export_h"]),
                            "label_count": 3,
                            "canonical_pixel_sha256": canonical_pixel_sha256,
                            "region": region,
                            "output_relative_path": (
                                f"{image['output_key']}/{label_name}"
                            ),
                            "output_content": {
                                "name": label_path.name,
                                "size_bytes": label_path.stat().st_size,
                                "sha256": sha256_file(label_path),
                            },
                            "output_content_verified_at_publication": True,
                            "output_content_verified_before_params": True,
                        }
                    )
                    row["stardist_label_count"] = "3"
                    row[
                        "stardist_label_canonical_pixel_sha256"
                    ] = canonical_pixel_sha256
                params["stardist_runtime"]["label_outputs"] = label_outputs
                write_json(params_path, params)
            write_csv(summary_path, header, summary_rows)
        return output_paths

    def test_valid_two_shard_index_and_multi_region_tile(self):
        document = self.fixture.build()

        self.assertEqual(document["status"], "stage2_integrity_complete")
        self.assertEqual(document["schema_version"], "1.4.0")
        self.assertEqual(
            document["$schema"],
            "https://ifquant-lung.invalid/schemas/"
            "stage2-run-index-1.4.0.schema.json",
        )
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
        self.assertEqual(len(document["parameter_artifacts"]), 2)
        self.assertEqual(document["runtime_profile"]["bioformats_version"], "8.5.0")
        self.assertEqual(document["runtime_profile"]["java_vendor"], "Synthetic OpenJDK")
        self.assertEqual(document["stage1_source_metadata"]["n_channels"], 4)
        self.assertEqual(
            document["stage1_source_metadata"]["source_metadata_authority"],
            "bioformats_used_files_content_verified_before_and_after_stage1",
        )
        reference_space = document["stage1_source_metadata"]["reference_space"]
        self.assertEqual(reference_space["mode"], "automatic_dapi_otsu_engineering")
        self.assertFalse(reference_space["airway_excluded"])
        self.assertEqual(
            reference_space["sampling_semantics"],
            "exhaustive_grid_over_declared_reference_space",
        )
        self.assertEqual(
            document["channel_mapping_authority"],
            "declared_panel_mapping_not_source_verified",
        )
        self.assertEqual(
            [channel["marker"] for channel in document["declared_channel_maps"][0]["channels"]],
            ["DAPI", "KRT5", "PDPN"],
        )
        for field in (
            "stage1_profile_sha256",
            "stage1_source_metadata_sha256",
            "runtime_profile_sha256",
            "parameter_set_sha256",
            "declared_channel_map_sha256",
        ):
            self.assertRegex(document[field], r"^[0-9a-f]{64}$")
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
        expected_uri = (
            "https://ifquant-lung.invalid/schemas/"
            "stage2-run-index-1.4.0.schema.json"
        )
        self.assertEqual(schema["$id"], expected_uri)
        self.assertEqual(schema["properties"]["$schema"]["const"], expected_uri)
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.4.0")
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).validate(self.fixture.build())

    def test_reference_space_raster_is_rehashed_before_index_publication(self):
        self.fixture.reference_mask.write_bytes(b"tampered reference mask\n")
        self.assertBuildFails("analysis tissue mask content does not match")

    def test_reference_space_pixel_sidecar_is_exact_binary_and_dimension_bound(self):
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        pixel_record = stage1["slides"][0]["reference_space"][
            "analysis_tissue_mask_pixels"
        ]

        nonbinary = bytearray(self.fixture.reference_mask_pixels.read_bytes())
        nonbinary[200] = 1
        self.fixture.reference_mask_pixels.write_bytes(nonbinary)
        pixel_record["content"]["sha256"] = sha256_file(
            self.fixture.reference_mask_pixels
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("must contain only 0 and 255")

        self.fixture.reference_mask_pixels.write_bytes(
            bytes([255]) * 100 + bytes(256 * 128 - 100)
        )
        self.fixture.reference_mask_pixels.write_bytes(
            self.fixture.reference_mask_pixels.read_bytes()[:-1]
        )
        pixel_record["content"]["size_bytes"] = (
            self.fixture.reference_mask_pixels.stat().st_size
        )
        pixel_record["content"]["sha256"] = sha256_file(
            self.fixture.reference_mask_pixels
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails(r"byte length does not equal width \* height")

        self.fixture.reference_mask_pixels.write_bytes(
            bytes([255]) * 100 + bytes(256 * 128 - 100)
        )
        pixel_record["content"]["size_bytes"] = (
            self.fixture.reference_mask_pixels.stat().st_size
        )
        pixel_record["content"]["sha256"] = sha256_file(
            self.fixture.reference_mask_pixels
        )
        pixel_record["width"] = 255
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("dimensions disagree with the selected-series")

    def test_reference_space_root_settings_and_downsample_ratio_are_fail_closed(self):
        original = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))

        changed = copy.deepcopy(original)
        changed["schema_version"] = "1.2"
        write_json(self.fixture.stage1_manifest, changed)
        self.assertBuildFails("unsupported Stage 1 manifest schema version")

        changed = copy.deepcopy(original)
        changed["tissue"]["downsample"] = 8.0
        write_json(self.fixture.stage1_manifest, changed)
        self.assertBuildFails("tissue profile downsample disagrees")

        changed = copy.deepcopy(original)
        changed["tissue"]["threshold_method"] = "external_binary_mask"
        write_json(self.fixture.stage1_manifest, changed)
        self.assertBuildFails("threshold_method must be Otsu")

        changed = copy.deepcopy(original)
        changed["tissue"]["downsample"] = 5e-324
        changed["slides"][0]["reference_space"]["downsample"] = 5e-324
        write_json(self.fixture.stage1_manifest, changed)
        self.assertBuildFails("downsample ratio is outside the supported range")

        changed = copy.deepcopy(original)
        changed["slides"][0]["width"] = 10**500
        write_json(self.fixture.stage1_manifest, changed)
        self.assertBuildFails("downsample ratio is outside the supported range")

    def test_automatic_mode_closes_three_slide_sibling_aliases(self):
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))

        def rebound_slide(source_slide, source_vsi, slide_stem):
            rebound = copy.deepcopy(source_slide)
            prior_source = rebound["source_vsi"]
            rebound["source_vsi"] = source_vsi
            rebound["slide_stem"] = slide_stem
            package = rebound["source_package"]
            package["source_vsi"] = source_vsi
            for member in package["members"]:
                if member["relative_path"] == prior_source:
                    member["relative_path"] = source_vsi
            package_lines = "".join(
                f"{member['relative_path']}\t{member['size_bytes']}\t{member['sha256']}\n"
                for member in package["members"]
            )
            package["package_sha256"] = hashlib.sha256(
                package_lines.encode("utf-8")
            ).hexdigest()
            return rebound

        sibling = rebound_slide(stage1["slides"][0], "sibling.vsi", "sibling")
        aliased_sibling = rebound_slide(sibling, "third.vsi", "sibling")
        stage1["slides"].extend([sibling, aliased_sibling])
        write_json(self.fixture.stage1_manifest, stage1)

        self.assertBuildFails("slide_stem disagrees with source_vsi")

        stage1["slides"][-1] = rebound_slide(
            sibling, "SIBLING.VSI", "SIBLING"
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("repeats source_vsi case-insensitively")

    def test_external_tissue_minus_airway_profile_is_bound_and_reconciled(self):
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        reference_dir = self.fixture.slide_dir / "reference_space"
        profile_mask_dir = self.fixture.root / "profile_masks"
        profile_mask_dir.mkdir()
        profile_tissue = profile_mask_dir / "tissue.tif"
        profile_airway = profile_mask_dir / "airway.tif"
        profile_tissue.write_bytes(b"binary-tissue-mask\n")
        profile_airway.write_bytes(b"binary-airway-mask\n")
        source_tissue = reference_dir / "source_tissue_mask.bin"
        source_airway = reference_dir / "source_airway_mask.bin"
        final_tissue = reference_dir / "analysis_tissue_minus_airway_mask.tif"
        source_tissue_pixels = reference_dir / "source_tissue_mask_pixels.uint8"
        source_airway_pixels = reference_dir / "source_airway_mask_pixels.uint8"
        final_tissue_pixels = self.fixture.reference_mask_pixels
        source_tissue.write_bytes(profile_tissue.read_bytes())
        source_airway.write_bytes(profile_airway.read_bytes())
        final_tissue.write_bytes(b"binary-final-mask\n")
        tissue_pixel_payload = bytes([255]) * 100 + bytes(256 * 128 - 100)
        airway_pixel_payload = bytes([255]) * 10 + bytes(256 * 128 - 10)
        analysis_pixel_payload = bytes(10) + bytes([255]) * 90 + bytes(
            256 * 128 - 100
        )
        source_tissue_pixels.write_bytes(tissue_pixel_payload)
        source_airway_pixels.write_bytes(airway_pixel_payload)
        final_tissue_pixels.write_bytes(analysis_pixel_payload)

        package_sha = stage1["slides"][0]["source_package"]["package_sha256"]
        profile = {
            "$schema": (
                "https://ifquant-lung.invalid/schemas/"
                "wsi-reference-mask-profile.schema.json"
            ),
            "schema_version": "1.0.0",
            "profile_id": "synthetic-reviewed-mask-v1",
            "review_state": "expert_reviewed",
            "review_protocol_id": "synthetic-review-protocol-v1",
            "coordinate_space": "selected_series_downsample_grid",
            "mask_logic": "tissue_foreground_minus_airway_foreground",
            "slides": [
                {
                    "source_vsi": "synthetic_slide.vsi",
                    "source_package_sha256": package_sha,
                    "series_index": 0,
                    "full_resolution_width": 4096,
                    "full_resolution_height": 2048,
                    "downsample": 16.0,
                    "mask_width": 256,
                    "mask_height": 128,
                    "tissue_mask": {
                        "relative_path": "profile_masks/tissue.tif",
                        "size_bytes": profile_tissue.stat().st_size,
                        "sha256": sha256_file(profile_tissue),
                    },
                    "airway_mask": {
                        "relative_path": "profile_masks/airway.tif",
                        "size_bytes": profile_airway.stat().st_size,
                        "sha256": sha256_file(profile_airway),
                    },
                }
            ],
        }
        mask_schema = json.loads(WSI_MASK_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(mask_schema)
        Draft202012Validator(mask_schema).validate(profile)
        profile_path = self.fixture.root / "reference_mask_profile.json"
        write_json(profile_path, profile)

        def content(path):
            return {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }

        stage1["tissue"] = {
            "mode": "external_binary_reference_masks",
            "threshold_method": "external_binary_mask",
            "downsample": 16.0,
            "airway_exclusion": "explicit_binary_mask_subtracted",
            "reference_mask_profile": {
                "profile_id": profile["profile_id"],
                "review_state": profile["review_state"],
                "review_protocol_id": profile["review_protocol_id"],
                "coordinate_space": profile["coordinate_space"],
                "mask_logic": profile["mask_logic"],
                "published_relative_path": profile_path.name,
                "content": content(profile_path),
                "content_verified_before_and_after": True,
            },
        }
        stage1["slides"][0]["reference_space"] = {
            "mode": "external_binary_reference_masks",
            "authority": "content_bound_external_profile_and_binary_masks",
            "profile_id": profile["profile_id"],
            "profile_sha256": sha256_file(profile_path),
            "review_state": profile["review_state"],
            "review_protocol_id": profile["review_protocol_id"],
            "coordinate_space": profile["coordinate_space"],
            "mask_logic": profile["mask_logic"],
            "sampling_semantics": "exhaustive_grid_over_declared_reference_space",
            "airway_excluded": True,
            "downsample": 16.0,
            "mask_width": 256,
            "mask_height": 128,
            "tissue_foreground_px_before_airway_exclusion": 100,
            "airway_foreground_px": 10,
            "analysis_tissue_foreground_px": 90,
            "source_tissue_mask": {
                "profile_relative_path": "profile_masks/tissue.tif",
                "published_relative_path": "reference_space/source_tissue_mask.bin",
                "content": content(source_tissue),
            },
            "source_airway_mask": {
                "profile_relative_path": "profile_masks/airway.tif",
                "published_relative_path": "reference_space/source_airway_mask.bin",
                "content": content(source_airway),
            },
            "source_tissue_mask_pixels": {
                "encoding": "row_major_uint8_0_255",
                "width": 256,
                "height": 128,
                "published_relative_path": (
                    "reference_space/source_tissue_mask_pixels.uint8"
                ),
                "content": content(source_tissue_pixels),
            },
            "source_airway_mask_pixels": {
                "encoding": "row_major_uint8_0_255",
                "width": 256,
                "height": 128,
                "published_relative_path": (
                    "reference_space/source_airway_mask_pixels.uint8"
                ),
                "content": content(source_airway_pixels),
            },
            "tissue_mask": {
                "published_relative_path": (
                    "reference_space/analysis_tissue_minus_airway_mask.tif"
                ),
                "content": content(final_tissue),
            },
            "analysis_tissue_mask_pixels": {
                "encoding": "row_major_uint8_0_255",
                "width": 256,
                "height": 128,
                "published_relative_path": (
                    "reference_space/analysis_tissue_mask_pixels.uint8"
                ),
                "content": content(final_tissue_pixels),
            },
            "content_verified_before_and_after": True,
        }
        write_json(self.fixture.stage1_manifest, stage1)

        document = self.fixture.build()
        reference = document["stage1_source_metadata"]["reference_space"]
        self.assertEqual(reference["mode"], "external_binary_reference_masks")
        self.assertTrue(reference["airway_excluded"])
        self.assertEqual(reference["analysis_tissue_foreground_px"], 90)
        Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
            format_checker=FormatChecker(),
        ).validate(document)

        stage1["tissue"]["threshold_method"] = "Otsu"
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("threshold_method must be external_binary_mask")
        stage1["tissue"]["threshold_method"] = "external_binary_mask"
        write_json(self.fixture.stage1_manifest, stage1)
        self.fixture.build()

        # A Stage 2 index targets one slide directory, but the external profile
        # is a closed multi-slide authority.  An inconsistent mask ledger on a
        # different slide must therefore fail the current slide's build too.
        second_slide_dir = self.fixture.root / "unrelated"
        second_reference_dir = second_slide_dir / "reference_space"
        second_reference_dir.mkdir(parents=True)
        second_source_tissue = second_reference_dir / source_tissue.name
        second_source_airway = second_reference_dir / source_airway.name
        second_final_tissue = second_reference_dir / final_tissue.name
        second_source_tissue_pixels = second_reference_dir / source_tissue_pixels.name
        second_source_airway_pixels = second_reference_dir / source_airway_pixels.name
        second_final_tissue_pixels = second_reference_dir / final_tissue_pixels.name
        second_source_tissue.write_bytes(source_tissue.read_bytes())
        second_source_airway.write_bytes(source_airway.read_bytes())
        second_final_tissue.write_bytes(final_tissue.read_bytes())
        second_source_tissue_pixels.write_bytes(source_tissue_pixels.read_bytes())
        second_source_airway_pixels.write_bytes(source_airway_pixels.read_bytes())
        second_final_tissue_pixels.write_bytes(final_tissue_pixels.read_bytes())

        second_slide = copy.deepcopy(stage1["slides"][0])
        second_slide["slide_stem"] = second_slide_dir.name
        second_slide["source_vsi"] = "unrelated.vsi"
        second_package = second_slide["source_package"]
        second_package["source_vsi"] = "unrelated.vsi"
        for member in second_package["members"]:
            if member["relative_path"] == "synthetic_slide.vsi":
                member["relative_path"] = "unrelated.vsi"
        package_lines = "".join(
            f"{member['relative_path']}\t{member['size_bytes']}\t{member['sha256']}\n"
            for member in second_package["members"]
        )
        second_package["package_sha256"] = hashlib.sha256(
            package_lines.encode("utf-8")
        ).hexdigest()
        second_reference = second_slide["reference_space"]
        second_reference["source_tissue_mask"]["content"] = content(
            second_source_tissue
        )
        second_reference["source_airway_mask"]["content"] = content(
            second_source_airway
        )
        second_reference["tissue_mask"]["content"] = content(second_final_tissue)
        second_reference["source_tissue_mask_pixels"]["content"] = content(
            second_source_tissue_pixels
        )
        second_reference["source_airway_mask_pixels"]["content"] = content(
            second_source_airway_pixels
        )
        second_reference["analysis_tissue_mask_pixels"]["content"] = content(
            second_final_tissue_pixels
        )

        second_profile_slide = copy.deepcopy(profile["slides"][0])
        second_profile_slide["source_vsi"] = "unrelated.vsi"
        second_profile_slide["source_package_sha256"] = second_package[
            "package_sha256"
        ]
        profile["slides"].append(second_profile_slide)
        stage1["slides"].append(second_slide)
        write_json(profile_path, profile)
        stage1["tissue"]["reference_mask_profile"]["content"] = content(
            profile_path
        )
        for slide in stage1["slides"]:
            slide["reference_space"]["profile_sha256"] = sha256_file(profile_path)
        write_json(self.fixture.stage1_manifest, stage1)
        self.fixture.build()

        # Three slides are required to expose an alias between two siblings
        # that does not collide with the slide currently being indexed.
        third_slide = copy.deepcopy(second_slide)
        third_slide["source_vsi"] = "third.vsi"
        third_slide["slide_stem"] = second_slide["slide_stem"]
        third_package = third_slide["source_package"]
        third_package["source_vsi"] = "third.vsi"
        for member in third_package["members"]:
            if member["relative_path"] == "unrelated.vsi":
                member["relative_path"] = "third.vsi"
        third_package_lines = "".join(
            f"{member['relative_path']}\t{member['size_bytes']}\t{member['sha256']}\n"
            for member in third_package["members"]
        )
        third_package["package_sha256"] = hashlib.sha256(
            third_package_lines.encode("utf-8")
        ).hexdigest()
        third_profile_slide = copy.deepcopy(second_profile_slide)
        third_profile_slide["source_vsi"] = "third.vsi"
        third_profile_slide["source_package_sha256"] = third_package["package_sha256"]
        stage1["slides"].append(third_slide)
        profile["slides"].append(third_profile_slide)
        write_json(profile_path, profile)
        stage1["tissue"]["reference_mask_profile"]["content"] = content(profile_path)
        for declared_slide in stage1["slides"]:
            declared_slide["reference_space"]["profile_sha256"] = sha256_file(
                profile_path
            )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("slide_stem disagrees with source_vsi")

        stage1["slides"].pop()
        profile["slides"].pop()
        write_json(profile_path, profile)
        stage1["tissue"]["reference_mask_profile"]["content"] = content(profile_path)
        for declared_slide in stage1["slides"]:
            declared_slide["reference_space"]["profile_sha256"] = sha256_file(
                profile_path
            )

        # Re-hashing a semantically wrong sibling sidecar cannot make it valid.
        wrong_airway_pixels = bytearray(second_source_airway_pixels.read_bytes())
        wrong_airway_pixels[100] = 255
        second_source_airway_pixels.write_bytes(wrong_airway_pixels)
        second_reference["source_airway_mask_pixels"]["content"] = content(
            second_source_airway_pixels
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("airway pixels are not a subset of tissue")
        second_source_airway_pixels.write_bytes(airway_pixel_payload)
        second_reference["source_airway_mask_pixels"]["content"] = content(
            second_source_airway_pixels
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.fixture.build()

        wrong_analysis_pixels = bytearray(second_final_tissue_pixels.read_bytes())
        wrong_analysis_pixels[10] = 0
        wrong_analysis_pixels[100] = 255
        second_final_tissue_pixels.write_bytes(wrong_analysis_pixels)
        second_reference["analysis_tissue_mask_pixels"]["content"] = content(
            second_final_tissue_pixels
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("analysis pixels do not equal tissue AND NOT airway")
        second_final_tissue_pixels.write_bytes(analysis_pixel_payload)
        second_reference["analysis_tissue_mask_pixels"]["content"] = content(
            second_final_tissue_pixels
        )

        nonbinary_tissue_pixels = bytearray(second_source_tissue_pixels.read_bytes())
        nonbinary_tissue_pixels[200] = 1
        second_source_tissue_pixels.write_bytes(nonbinary_tissue_pixels)
        second_reference["source_tissue_mask_pixels"]["content"] = content(
            second_source_tissue_pixels
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("must contain only 0 and 255")
        second_source_tissue_pixels.write_bytes(tissue_pixel_payload)
        second_reference["source_tissue_mask_pixels"]["content"] = content(
            second_source_tissue_pixels
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.fixture.build()

        second_reference["source_tissue_mask"]["profile_relative_path"] = (
            "profile_masks/different-tissue.tif"
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails(
            "unrelated.vsi.*source_tissue_mask disagrees with the published"
        )

        stage1["slides"].pop()
        profile["slides"].pop()
        write_json(profile_path, profile)
        stage1["tissue"]["reference_mask_profile"]["content"] = content(
            profile_path
        )
        stage1["slides"][0]["reference_space"]["profile_sha256"] = sha256_file(
            profile_path
        )
        write_json(self.fixture.stage1_manifest, stage1)

        source_airway.write_bytes(b"tampered-airway\n")
        self.assertBuildFails("source_airway_mask content does not match")

        source_airway.write_bytes(profile_airway.read_bytes())
        profile["slides"][0]["series_index"] = 1
        write_json(profile_path, profile)
        stage1["tissue"]["reference_mask_profile"]["content"] = content(profile_path)
        stage1["slides"][0]["reference_space"]["profile_sha256"] = sha256_file(
            profile_path
        )
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("profile slide .* series_index disagrees")

        profile["slides"][0]["series_index"] = 0
        write_json(profile_path, profile)
        stage1["tissue"]["reference_mask_profile"]["content"] = content(profile_path)
        stage1["slides"][0]["reference_space"]["profile_sha256"] = sha256_file(
            profile_path
        )
        stage1["slides"][0]["reference_space"]["source_tissue_mask"][
            "profile_relative_path"
        ] = "profile_masks/different-tissue.tif"
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("source_tissue_mask disagrees with the published")

    def test_reference_mask_profile_schema_rejects_nonportable_identity(self):
        schema = json.loads(WSI_MASK_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        base = {
            "$schema": (
                "https://ifquant-lung.invalid/schemas/"
                "wsi-reference-mask-profile.schema.json"
            ),
            "schema_version": "1.0.0",
            "profile_id": "reviewed-v1",
            "review_state": "expert_reviewed",
            "review_protocol_id": "review-v1",
            "coordinate_space": "selected_series_downsample_grid",
            "mask_logic": "tissue_foreground_minus_airway_foreground",
            "slides": [
                {
                    "source_vsi": "slide.vsi",
                    "source_package_sha256": "a" * 64,
                    "series_index": 0,
                    "full_resolution_width": 4096,
                    "full_resolution_height": 2048,
                    "downsample": 16.0,
                    "mask_width": 256,
                    "mask_height": 128,
                    "tissue_mask": {
                        "relative_path": "masks/tissue.tif",
                        "size_bytes": 1,
                        "sha256": "b" * 64,
                    },
                    "airway_mask": {
                        "relative_path": "masks/airway.tif",
                        "size_bytes": 1,
                        "sha256": "c" * 64,
                    },
                }
            ],
        }
        validator.validate(base)
        cases = (
            ("profile_id", " reviewed-v1"),
            ("source_vsi", "C:slide.vsi"),
            ("tissue_path", "./masks/tissue.tif"),
            ("tissue_path", "masks//tissue.tif"),
            ("tissue_path", "masks/tissue.tif/"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                candidate = copy.deepcopy(base)
                if field == "profile_id":
                    candidate[field] = value
                elif field == "source_vsi":
                    candidate["slides"][0][field] = value
                else:
                    candidate["slides"][0]["tissue_mask"]["relative_path"] = value
                with self.assertRaises(ValidationError):
                    validator.validate(candidate)

    def test_stage1_must_be_complete_non_dry_and_without_skips(self):
        original = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        cases = (
            ("coverage_complete", False, "coverage_complete must be true"),
            ("dry_run", True, "dry_run must be false"),
            ("max_tiles_cap", 1, "max_tiles_cap must be zero"),
            ("n_skipped_low_tissue", 1, "n_skipped_low_tissue must be zero"),
        )
        for field, value, pattern in cases:
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["slides"][0][field] = value
                write_json(self.fixture.stage1_manifest, changed)
                self.assertBuildFails(pattern)
        write_json(self.fixture.stage1_manifest, original)

    def test_stage1_source_metadata_and_tile_calibration_are_bound(self):
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["slides"][0]["channel_names"] = ["DAPI"]
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("one non-empty label per channel")

        self.fixture.write_stage1()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["slides"][0]["ordered_channel_names"][1] = "wrong-order-label"
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("ordered_channel_names must exactly equal")

        self.fixture.write_stage1()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["qupath_series_selection"]["ordered_channel_patterns"] = ["only-one"]
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("root ordered_channel_patterns")

        self.fixture.write_stage1()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["qupath_series_selection"]["expect_channels"] = 3
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("expect_channels must equal")

        self.fixture.write_stage1()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["qupath_series_selection"]["ordered_channel_patterns"][1] = "(?i)^wrong$"
        stage1["slides"][0]["ordered_channel_patterns"][1] = "(?i)^wrong$"
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("does not full-match the recorded channel name")

        self.fixture.write_stage1()
        rows = [dict(row) for row in self.fixture.tile_rows]
        rows[0]["pixel_size_um"] = "0.6"
        write_csv(self.fixture.tile_manifest, list(rows[0]), rows)
        self.assertBuildFails("pixel calibration disagrees with Stage 1")

    def test_stage1_script_and_bioformats_source_package_are_content_bound(self):
        document = self.fixture.build()
        source = document["stage1_source_metadata"]
        self.assertEqual(
            source["source_metadata_authority"],
            "bioformats_used_files_content_verified_before_and_after_stage1",
        )
        self.assertEqual(
            source["source_package"]["discovery_authority"],
            "bioformats_ImageReader_getUsedFiles",
        )
        self.assertEqual(
            source["source_package_sha256"],
            source["source_package"]["package_sha256"],
        )
        self.assertEqual(
            source["stage1_script_sha256"], document["stage1_script"]["sha256"]
        )

        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["slides"][0]["source_package"]["members"][0]["size_bytes"] += 1
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("package_sha256 does not match its member ledger")

        self.fixture.write_stage1()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1.pop("stage1_script")
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("lacks stage1_script")

    def test_stage2_engine_and_tile_bytes_are_bound_by_engine_records(self):
        analysis_dir = self.fixture.declarations[0].analysis_dir
        manifest_path = analysis_dir / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        first_image = manifest["images"][0]
        params_path = analysis_dir / first_image["params_relative_path"]
        params = json.loads(params_path.read_text(encoding="utf-8"))

        params["source_content"]["sha256"] = "0" * 64
        write_json(params_path, params)
        self.assertBuildFails("source content identity disagrees")

        params["source_content"] = first_image["source_content"]
        write_json(params_path, params)
        manifest["engine_script"]["sha256"] = "1" * 64
        write_json(manifest_path, manifest)
        self.assertBuildFails("engine_script disagrees")

    def test_stage1_profile_is_an_allowlisted_stable_settings_hash(self):
        before = self.fixture.build()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["future_workstation_audit_path"] = "C:/host-specific/path"
        write_json(self.fixture.stage1_manifest, stage1)
        after_host_field = self.fixture.build()
        self.assertEqual(
            before["stage1_profile_sha256"],
            after_host_field["stage1_profile_sha256"],
        )

        stage1["tiling"]["halo_px"] = 64
        write_json(self.fixture.stage1_manifest, stage1)
        self.fixture.tile_rows[0].update(
            {"export_w": "2112", "halo_right": "64"}
        )
        self.fixture.tile_rows[1].update(
            {"export_x": "1984", "export_w": "2112", "halo_left": "64"}
        )
        self.fixture.write_tile_manifest()
        after_setting = self.fixture.build()
        self.assertNotEqual(
            before["stage1_profile_sha256"], after_setting["stage1_profile_sha256"]
        )

    def test_tile_candidate_sampling_frame_is_required_and_reconciled(self):
        header, rows = read_csv_for_test(self.fixture.tile_candidate_manifest)
        rows[0]["status"] = "outside_tissue"
        rows[0]["core_tissue_area_px"] = "0"
        rows[0]["core_tissue_area_um2"] = "0"
        write_csv(self.fixture.tile_candidate_manifest, header, rows)
        self.assertBuildFails("n_outside_tissue disagrees")

        self.fixture.write_tile_candidate_manifest()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        header, rows = read_csv_for_test(self.fixture.tile_candidate_manifest)
        rows[0]["status"] = "resumed"
        stage1["slides"][0]["n_candidate_exported"] = 1
        stage1["slides"][0]["n_candidate_resumed"] = 1
        stage1["slides"][0]["n_written"] = 1
        stage1["slides"][0]["n_resumed"] = 1
        write_csv(self.fixture.tile_candidate_manifest, header, rows)
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("cannot contain.*resumed")

        self.fixture.write_stage1()
        self.fixture.write_tile_candidate_manifest()
        header, rows = read_csv_for_test(self.fixture.tile_candidate_manifest)
        rows = rows[:1]
        rows[0]["status"] = "exported"
        write_csv(self.fixture.tile_candidate_manifest, header, rows)
        self.assertBuildFails("does not exactly enumerate the declared Stage 1 grid")

        self.fixture.write_stage1()
        self.fixture.write_tile_candidate_manifest()
        header, rows = read_csv_for_test(self.fixture.tile_candidate_manifest)
        rows[1]["core_x"] = "1024"
        write_csv(self.fixture.tile_candidate_manifest, header, rows)
        self.assertBuildFails("coordinates do not match the exhaustive")

        self.fixture.write_tile_candidate_manifest()
        self.fixture.tile_rows[0]["core_tissue_area_um2"] = "301"
        self.fixture.write_tile_manifest()
        self.assertBuildFails("tissue area disagrees with calibration or candidate")

        self.fixture.tile_rows[0]["core_tissue_area_um2"] = "300"
        self.fixture.write_tile_manifest()
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["slides"][0]["tissue_area_mm2"] = 0.0005
        write_json(self.fixture.stage1_manifest, stage1)
        self.assertBuildFails("seam_check_rel_diff does not reconcile")

        self.fixture.tile_candidate_manifest.unlink()
        self.assertBuildFails("tile candidate manifest not found")

    def test_params_are_exact_content_addressed_identity_and_calibration_artifacts(self):
        declaration = self.fixture.declarations[0]
        params_path = (
            declaration.analysis_dir
            / "output-001"
            / "C1-DAPI_C2-KRT5_C3-PDPN__params.json"
        )
        original = json.loads(params_path.read_text(encoding="utf-8"))
        cases = (
            ("output_key", "wrong-output", "params output_key disagrees"),
            ("channel_signature", "C1-DAPI_C2-WRONG", "params channel_signature disagrees"),
        )
        for field, value, pattern in cases:
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                write_json(params_path, changed)
                self.assertBuildFails(pattern)
        write_json(params_path, original)

        changed = copy.deepcopy(original)
        changed["calibration"]["n_channels"] = 3
        write_json(params_path, changed)
        self.assertBuildFails("acquired channel count disagrees")

        write_json(params_path, original)
        changed = copy.deepcopy(original)
        changed["calibration"]["pixel_width_um"] = 0.6
        write_json(params_path, changed)
        self.assertBuildFails("params pixel calibration disagrees")

        write_json(params_path, original)
        changed = copy.deepcopy(original)
        changed["channel_map"][1]["fileLabel"] = "WRONG"
        write_json(params_path, changed)
        self.assertBuildFails(r"channel_map\[2\] disagrees")

        params_path.unlink()
        self.assertBuildFails("params_relative_path escapes or is missing")

    def test_run_manifest_explicit_params_path_is_required_and_identity_bound(self):
        manifest_path = self.fixture.declarations[0].analysis_dir / "run_manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))

        changed = copy.deepcopy(original)
        changed["images"][0].pop("params_relative_path")
        write_json(manifest_path, changed)
        self.assertBuildFails("blank params_relative_path")

        changed = copy.deepcopy(original)
        changed["images"][0]["params_relative_path"] = (
            "output-002/C1-DAPI_C2-KRT5_C3-PDPN__params.json"
        )
        write_json(manifest_path, changed)
        self.assertBuildFails("params_relative_path disagrees with image/output identity")

    def test_params_bytes_change_observation_provenance_not_measurement_profile(self):
        before = self.fixture.build()
        declaration = self.fixture.declarations[0]
        params_path = (
            declaration.analysis_dir
            / "output-001"
            / "C1-DAPI_C2-KRT5_C3-PDPN__params.json"
        )
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["audit_note"] = "same measurements, different bound params bytes"
        write_json(params_path, params)
        after = self.fixture.build()
        self.assertNotEqual(before["parameter_set_sha256"], after["parameter_set_sha256"])
        self.assertEqual(
            before["measurement_profile_sha256"], after["measurement_profile_sha256"]
        )

    def test_normalized_runtime_profile_must_be_known_and_identical(self):
        second_manifest = self.fixture.declarations[1].analysis_dir / "run_manifest.json"
        manifest = json.loads(second_manifest.read_text(encoding="utf-8"))
        manifest["versions"]["java_version"] = "17.0.1"
        write_json(second_manifest, manifest)
        self.assertBuildFails("runtime profiles differ")

        manifest["versions"]["java_version"] = "unknown"
        write_json(second_manifest, manifest)
        self.assertBuildFails("not an authoritative runtime identifier")

    def test_stardist_model_runtime_and_label_outputs_are_portably_bound(self):
        config = self.make_stardist_config()
        declarations = [
            self.fixture.write_run(
                "stardist_shard_01",
                [0],
                config=config,
                regions_by_tile={0: ["damaged_parenchyma", "intact_parenchyma"]},
            ),
            self.fixture.write_run("stardist_shard_02", [1], config=config),
        ]
        label_paths = self.populate_stardist_label_outputs(declarations)
        document = self.fixture.build(declarations)
        profile = document["segmentation_profile"]
        self.assertEqual(profile["segmenter"], "stardist")
        self.assertNotIn(str(self.fixture.root), json.dumps(profile))
        self.assertEqual(len(profile["runtime_artifacts"]), 4)
        self.assertEqual(len(document["stardist_label_outputs"]), 3)
        self.assertTrue(
            all(
                record["segmentation_profile_sha256"]
                == document["segmentation_profile_sha256"]
                for record in document["parameter_artifacts"]
            )
        )
        self.assertNotIn(
            str(self.fixture.root), json.dumps(document["stardist_label_outputs"])
        )
        Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
            format_checker=FormatChecker(),
        ).validate(document)

        original_label_bytes = label_paths[0].read_bytes()
        label_paths[0].write_bytes(b"tampered-label-output\n")
        self.assertBuildFails("artifact no longer matches", declarations)

        label_paths[0].write_bytes(original_label_bytes)
        first_manifest = json.loads(
            (declarations[0].analysis_dir / "run_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        params_path = (
            declarations[0].analysis_dir
            / first_manifest["images"][0]["params_relative_path"]
        )
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["stardist_runtime"]["label_outputs"][0]["width_pixels"] -= 1
        write_json(params_path, params)
        self.assertBuildFails("dimensions disagree", declarations)

    def test_stardist_runtime_identity_cannot_differ_across_shards(self):
        first_config = self.make_stardist_config(
            profile_id="synthetic-stardist-a", token="profile-a"
        )
        second_config = self.make_stardist_config(
            profile_id="synthetic-stardist-b", token="profile-b"
        )
        for key in ("model_path", "model_content", "model_archive", "model_choice"):
            second_config["stardistAuthority"][key] = copy.deepcopy(
                first_config["stardistAuthority"][key]
            )
        second_config["stardistModelChoice"] = first_config["stardistModelChoice"]
        second_config["stardistModelSha256"] = first_config["stardistModelSha256"]
        declarations = [
            self.fixture.write_run("mixed_stardist_01", [0], config=first_config),
            self.fixture.write_run("mixed_stardist_02", [1], config=second_config),
        ]
        self.populate_stardist_label_outputs(declarations)
        self.assertBuildFails(
            "segmentation/model/runtime profiles differ across runs", declarations
        )

    def test_classic_route_rejects_active_stardist_claims(self):
        declaration = self.fixture.declarations[0]
        params_path = (
            declaration.analysis_dir
            / "output-001"
            / "C1-DAPI_C2-KRT5_C3-PDPN__params.json"
        )
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["stardist_runtime"]["active"] = True
        write_json(params_path, params)
        self.assertBuildFails("stardist_runtime.active disagrees")

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
        self.fixture.write_stage1(n_channels=10)
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

    def test_params_channel_map_allows_engine_filelabel_fallback_and_sanitization(self):
        for declaration in self.fixture.declarations:
            output_key = "output-001" if declaration is self.fixture.declarations[0] else "output-002"
            params_path = (
                declaration.analysis_dir
                / output_key
                / "C1-DAPI_C2-KRT5_C3-PDPN__params.json"
            )
            params = json.loads(params_path.read_text(encoding="utf-8"))
            for channel in params["channel_map"]:
                channel.pop("fileLabel")
            write_json(params_path, params)
        document = self.fixture.build()
        self.assertEqual(
            [channel["file_label"] for channel in document["declared_channel_maps"][0]["channels"]],
            ["DAPI", "KRT5", "PDPN"],
        )

        signature = "C1-DAPI_C2-Pro-SPC-488_C3-AGER"
        declarations = [
            self.fixture.write_run("safe_label_01", [0], signatures={0: signature}),
            self.fixture.write_run("safe_label_02", [1], signatures={1: signature}),
        ]
        for index, declaration in enumerate(declarations, start=1):
            params_path = (
                declaration.analysis_dir
                / f"output-{index:03d}"
                / f"{signature}__params.json"
            )
            params = json.loads(params_path.read_text(encoding="utf-8"))
            params["channel_map"][1].pop("fileLabel")
            params["channel_map"][1]["marker"] = "Pro SPC / 488"
            write_json(params_path, params)
        document = self.fixture.build(declarations)
        declared = document["declared_channel_maps"][0]["channels"][1]
        self.assertEqual(declared["file_label"], "Pro-SPC-488")
        self.assertEqual(declared["marker"], "Pro SPC / 488")

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
        automatic_with_external_claim = copy.deepcopy(document)
        automatic_with_external_claim["stage1_source_metadata"]["reference_space"][
            "profile_sha256"
        ] = "a" * 64
        cases.append(automatic_with_external_claim)

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

    def test_workstation_config_paths_are_replaced_by_content_authority(self):
        provenance_dir = self.fixture.slide_dir / "provenance"
        provenance_dir.mkdir()
        registry_path = provenance_dir / "marker_registry.json"
        write_json(registry_path, {"schema_version": "synthetic", "markers": {}})
        bound_hash = sha256_file(registry_path)
        panel_path = provenance_dir / "panel_config.json"
        write_json(panel_path, {"schema_version": "synthetic", "panels": {}})
        panel_hash = sha256_file(panel_path)
        first_config = {
            "threshold_profile": "same-v1",
            "markerRegistryPath": str(registry_path),
            "markerRegistrySha256": bound_hash,
            "markerRegistryStatus": "external_file_bound",
            "panelConfigPath": str(panel_path),
            "panelConfigSha256": panel_hash,
            "panelConfigStatus": "external_file_bound",
            "canonicalManifestPath": None,
        }
        declarations = [
            self.fixture.write_run("bound_config_01", [0], config=first_config),
            self.fixture.write_run("bound_config_02", [1], config=first_config),
        ]
        document = self.fixture.build(declarations)
        self.assertNotIn(str(self.fixture.root), json.dumps(document))
        self.assertEqual(
            document["configuration_artifacts"],
            [
                {
                    "role": "marker_registry",
                    "artifact": {
                        "path": "provenance/marker_registry.json",
                        "sha256": bound_hash,
                    },
                },
                {
                    "role": "panel_config",
                    "artifact": {
                        "path": "provenance/panel_config.json",
                        "sha256": panel_hash,
                    },
                },
            ],
        )
        params_path = (
            declarations[0].analysis_dir
            / "output-001"
            / "C1-DAPI_C2-KRT5_C3-PDPN__params.json"
        )
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["marker_registry"]["sha256"] = "c" * 64
        write_json(params_path, params)
        self.assertBuildFails("marker_registry provenance disagrees", declarations)

        params["marker_registry"]["sha256"] = bound_hash
        params["custom_panel_config_sha256"] = "c" * 64
        write_json(params_path, params)
        self.assertBuildFails("custom panel provenance disagrees", declarations)

        params["custom_panel_config_sha256"] = panel_hash
        alternate_registry_path = provenance_dir / "alternate_same_bytes.json"
        alternate_registry_path.write_bytes(registry_path.read_bytes())
        params["marker_registry"]["path"] = str(alternate_registry_path)
        write_json(params_path, params)
        self.assertBuildFails("marker_registry path disagrees", declarations)

        unbound = dict(first_config)
        unbound.pop("markerRegistrySha256")
        declaration = self.fixture.write_run("unbound_config", [0, 1], config=unbound)
        self.assertBuildFails("external file without.*markerRegistrySha256", [declaration])

        wrong_claim = dict(first_config, markerRegistrySha256="b" * 64)
        declaration = self.fixture.write_run("wrong_config_hash", [0, 1], config=wrong_claim)
        self.assertBuildFails("markerRegistrySha256 does not match", [declaration])

        outside_path = self.fixture.root / "outside-registry.json"
        write_json(outside_path, {"markers": {}})
        outside_config = dict(
            first_config,
            markerRegistryPath=str(outside_path),
            markerRegistrySha256=sha256_file(outside_path),
        )
        declaration = self.fixture.write_run("outside_config", [0, 1], config=outside_config)
        self.assertBuildFails("must resolve inside the slide directory", [declaration])

        alternate_path = provenance_dir / "alternate_registry.json"
        alternate_path.write_bytes(registry_path.read_bytes())
        alternate_config = dict(first_config, markerRegistryPath=str(alternate_path))
        declarations = [
            self.fixture.write_run("config_path_01", [0], config=first_config),
            self.fixture.write_run("config_path_02", [1], config=alternate_config),
        ]
        self.assertBuildFails("configuration artifact differs across runs", declarations)

    def test_routing_input_hashes_are_bound_but_not_cross_shard_profile_fields(self):
        document = self.fixture.build()
        first_routing = document["runs"][0]["routing_input_hashes"]
        second_routing = document["runs"][1]["routing_input_hashes"]
        self.assertNotEqual(
            first_routing["samplesheet_sha256"],
            second_routing["samplesheet_sha256"],
        )
        self.assertEqual(
            document["runs"][0]["config_sha256"],
            document["runs"][1]["config_sha256"],
        )

        manifest_path = self.fixture.declarations[0].analysis_dir / "run_manifest.json"
        original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed_manifest = copy.deepcopy(original_manifest)
        changed_manifest["config"]["samplesheetSha256"] = "a" * 64
        write_json(manifest_path, changed_manifest)
        self.assertBuildFails("samplesheetSha256 does not match")

        write_json(manifest_path, original_manifest)
        params_path = (
            self.fixture.declarations[0].analysis_dir
            / "output-001"
            / "C1-DAPI_C2-KRT5_C3-PDPN__params.json"
        )
        original_params = json.loads(params_path.read_text(encoding="utf-8"))
        changed_params = copy.deepcopy(original_params)
        changed_params["routing_input_hashes"]["samplesheet_sha256"] = "b" * 64
        write_json(params_path, changed_params)
        self.assertBuildFails("routing_input_hashes.samplesheet_sha256 disagrees")

        write_json(params_path, original_params)
        panel_copy = self.fixture.declarations[0].analysis_dir / "auto_panel_assignments.csv"
        panel_copy.write_text("filename,panel\ntile-001.tif,LEFT\n", encoding="utf-8")
        panel_hash = sha256_file(panel_copy)
        changed_manifest = copy.deepcopy(original_manifest)
        changed_manifest["config"]["panelMapSha256"] = panel_hash
        changed_manifest["config"]["panelMapMode"] = "per_image_relative_path"
        changed_manifest["panel_map_record"] = panel_copy.name
        write_json(manifest_path, changed_manifest)
        changed_params = copy.deepcopy(original_params)
        changed_params["routing_input_hashes"]["panel_map_sha256"] = panel_hash
        write_json(params_path, changed_params)
        routed = self.fixture.build()
        self.assertEqual(
            routed["runs"][0]["routing_artifacts"][0]["role"], "panel_map"
        )

        panel_copy.write_text("filename,panel\ntile-001.tif,RIGHT\n", encoding="utf-8")
        self.assertBuildFails("panelMapSha256 does not match panel_map_record bytes")

    def test_canonical_routing_copy_is_content_bound(self):
        declaration = self.fixture.declarations[0]
        manifest_path = declaration.analysis_dir / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        canonical_copy = declaration.analysis_dir / "canonical_field_manifest.csv"
        canonical_copy.write_text("filename\ntile-001.tif\n", encoding="utf-8")
        canonical_hash = sha256_file(canonical_copy)
        source_path = str(self.fixture.slide_dir / "routing" / "canonical.csv")
        manifest["config"]["canonicalManifestPath"] = source_path
        manifest["config"]["canonicalManifestMode"] = "allowlist"
        manifest["config"]["canonicalManifestSha256"] = canonical_hash
        manifest["canonical_manifest_path"] = source_path
        write_json(manifest_path, manifest)

        params_path = (
            declaration.analysis_dir
            / "output-001"
            / "C1-DAPI_C2-KRT5_C3-PDPN__params.json"
        )
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["routing_input_hashes"]["canonical_manifest_sha256"] = canonical_hash
        write_json(params_path, params)
        document = self.fixture.build()
        self.assertEqual(
            document["runs"][0]["routing_artifacts"][0]["role"],
            "canonical_manifest",
        )

        canonical_copy.write_text("filename\ntile-002.tif\n", encoding="utf-8")
        self.assertBuildFails("canonicalManifestSha256 does not match copied artifact bytes")

    def test_params_marker_identity_controls_declared_marker_evaluability(self):
        signature = "C1-DAPI_C2-FITC_C3-Cy3"
        declarations = [
            self.fixture.write_run("mapped_marker_01", [0], signatures={0: signature}),
            self.fixture.write_run("mapped_marker_02", [1], signatures={1: signature}),
        ]
        for declaration in declarations:
            params_path = (
                declaration.analysis_dir
                / ("output-001" if declaration is declarations[0] else "output-002")
                / f"{signature}__params.json"
            )
            params = json.loads(params_path.read_text(encoding="utf-8"))
            params["channel_map"][1]["marker"] = "KRT5"
            params["channel_map"][2]["marker"] = "AGER"
            write_json(params_path, params)

            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, rows = read_csv_for_test(summary_path)
            header.append("KRT5_pos_count")
            for row in rows:
                row["KRT5_pos_count"] = ""
            write_csv(summary_path, header, rows)

        with self.assertRaisesRegex(
            Stage2IndexError, "KRT5_pos_count.*declared panel marker"
        ):
            self.fixture.build(declarations)

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

        legacy_version = copy.deepcopy(document)
        legacy_version["schema_version"] = "1.3.0"
        rehash_index(legacy_version)
        write_json(index_path, legacy_version)
        with self.assertRaisesRegex(Stage2IndexError, "unsupported Stage 2 index version"):
            validate_stage2_index(
                index_path,
                slide_dir=self.fixture.slide_dir,
                stage1_manifest=self.fixture.stage1_manifest,
                stage2_script=self.fixture.stage2_script,
            )

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
            original.replace(",100,10,,\n", ",101,10,,\n", 1), encoding="utf-8"
        )
        with self.assertRaisesRegex(Stage2IndexError, "no longer matches"):
            validate_stage2_index(
                index_path,
                slide_dir=self.fixture.slide_dir,
                stage1_manifest=self.fixture.stage1_manifest,
                stage2_script=self.fixture.stage2_script,
            )

    def test_tampered_params_and_candidate_ledger_are_rejected_after_publication(self):
        document = self.fixture.build()
        index_path = self.fixture.publish(document)
        params_path = self.fixture.slide_dir / document["parameter_artifacts"][0]["params"]["path"]
        params_path.write_text(
            params_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
        )
        with self.assertRaisesRegex(Stage2IndexError, "no longer matches"):
            validate_stage2_index(
                index_path,
                slide_dir=self.fixture.slide_dir,
                stage1_manifest=self.fixture.stage1_manifest,
                stage2_script=self.fixture.stage2_script,
            )

        self.fixture = SyntheticStage2Run(
            Path(self.temporary_directory.name) / "candidate_fixture"
        )
        document = self.fixture.build()
        index_path = self.fixture.publish(document)
        self.fixture.tile_candidate_manifest.write_text(
            self.fixture.tile_candidate_manifest.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Stage2IndexError, "no longer matches"):
            validate_stage2_index(
                index_path,
                slide_dir=self.fixture.slide_dir,
                stage1_manifest=self.fixture.stage1_manifest,
                stage2_script=self.fixture.stage2_script,
            )

        config_fixture = SyntheticStage2Run(
            Path(self.temporary_directory.name) / "config_tamper_fixture"
        )
        provenance_dir = config_fixture.slide_dir / "provenance"
        provenance_dir.mkdir()
        registry_path = provenance_dir / "marker_registry.json"
        write_json(registry_path, {"schema_version": "synthetic", "markers": {}})
        config = {
            "markerRegistryPath": str(registry_path),
            "markerRegistrySha256": sha256_file(registry_path),
            "markerRegistryStatus": "external_file_bound",
            "panelConfigPath": "built_in_only",
            "panelConfigStatus": "built_in_only",
        }
        declarations = [
            config_fixture.write_run("config_01", [0], config=config),
            config_fixture.write_run("config_02", [1], config=config),
        ]
        document = config_fixture.build(declarations)
        index_path = config_fixture.publish(document)
        write_json(registry_path, {"schema_version": "tampered", "markers": {}})
        with self.assertRaisesRegex(Stage2IndexError, "does not match the referenced bytes"):
            validate_stage2_index(
                index_path,
                slide_dir=config_fixture.slide_dir,
                stage1_manifest=config_fixture.stage1_manifest,
                stage2_script=config_fixture.stage2_script,
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
        self.assertTrue(output.is_file())
        audit_path = self.fixture.root / "stats" / STAGE3_AUDIT_FILENAME
        audit = validate_aggregation_audit(
            audit_path,
            expected_stage="stage3_slide_aggregation",
            expected_artifacts={
                "stage3_slide_summary": output,
                "stage3_aggregator": STAGE3_PATH,
            },
        )
        self.assertEqual(audit["arguments"]["seam_counts"], "off")
        self.assertEqual(audit["arguments"]["declared_slide_count"], 1)
        self.assertIn(
            "stage2_index:synthetic_slide",
            {item["role"] for item in audit["input_artifacts"]},
        )
        with output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qc_status"], "ok")
        self.assertEqual(rows[0]["stage2_source_mode"], "explicit_hashed_index")
        self.assertEqual(rows[0]["n_tiles_expected"], "2")
        self.assertEqual(rows[0]["n_summary_rows"], "3")
        self.assertEqual(rows[0]["stage2_region_area_um2"], "400.0")
        self.assertEqual(
            rows[0]["stage1_source_metadata_sha256"],
            document["stage1_source_metadata_sha256"],
        )
        self.assertEqual(
            rows[0]["stage1_script_sha256"],
            document["stage1_script"]["sha256"],
        )
        self.assertEqual(
            rows[0]["source_package_sha256"],
            document["stage1_source_metadata"]["source_package_sha256"],
        )
        self.assertEqual(
            rows[0]["parameter_set_sha256"], document["parameter_set_sha256"]
        )
        self.assertEqual(
            rows[0]["channel_signature_authority"],
            "declared_panel_mapping_not_source_verified",
        )

    def test_stage3_refuses_valid_subset_when_stage1_declares_missing_slide(self):
        stage1 = json.loads(self.fixture.stage1_manifest.read_text(encoding="utf-8"))
        stage1["slides"].append({
            "slide_stem": "declared_but_missing",
            "source_vsi": "declared_but_missing.vsi",
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

    def test_stage3_rejects_negative_additive_measurement(self):
        for declaration_number, declaration in enumerate(self.fixture.declarations):
            summary_path = declaration.analysis_dir / "run_summary.csv"
            header, rows = read_csv_for_test(summary_path)
            header.append("KRT5_pod_area_um2")
            for row_number, row in enumerate(rows):
                row["KRT5_pod_area_um2"] = (
                    "-1" if declaration_number == 0 and row_number == 0 else "1"
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
        self.assertIn("must be non-negative", result.stdout)
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
