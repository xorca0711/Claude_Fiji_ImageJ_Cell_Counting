"""Fail-closed, content-addressed WSI Stage 2 run indexes.

The index is a local run artifact.  It records only portable paths relative to
one slide directory; workstation-absolute paths embedded in legacy Fiji
manifests are deliberately neither trusted nor copied.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_URI = "https://ifquant-lung.invalid/schemas/stage2-run-index.schema.json"
SCHEMA_VERSION = "1.0.0"
INDEX_TYPE = "ifquant_wsi_stage2_run_index"
INDEX_STATUS = "stage2_integrity_complete"
ANALYTICAL_IDENTITY = "section_id|region|panel"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHANNEL_TOKEN_RE = re.compile(r"^C([1-9][0-9]*)-([A-Za-z0-9.-]+)$")
# Acquisition file labels use wavelength suffixes (for example ``-488``).
# Restrict stripping to plausible three-digit visible/far-red wavelengths so a
# biological marker such as Ki-67 is not accidentally rewritten to Ki.
FLUOR_SUFFIX_RE = re.compile(r"-[4-8][0-9]{2}(?:NM)?$", re.IGNORECASE)
ADDITIVE_MARKER_SUFFIXES = (
    "_pod_area_um2_in_intact",
    "_positive_area_um2",
    "_pod_area_um2",
    "_n_components",
    "_n_pods",
    "_count",
)


def canonical_marker_id(label: str) -> str:
    """Normalize a channel/file label to the engine's marker identifier.

    Ordered signatures intentionally preserve acquisition labels such as
    ``KRT5-488`` and ``T1alpha-647``.  Summary columns use marker identifiers
    such as ``KRT5`` and ``T1A``.  This deliberately small normalization keeps
    those two representations comparable without pretending that the declared
    signature has been verified against source-image metadata.
    """

    without_fluor = FLUOR_SUFFIX_RE.sub("", str(label).strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "", without_fluor).upper()
    return {"T1ALPHA": "T1A"}.get(normalized, normalized)


def declared_marker_ids(signature: str) -> tuple[str, ...]:
    """Return ordered canonical marker IDs from a validated channel signature."""

    _validate_channel_signature(signature, "channel signature")
    matches = [CHANNEL_TOKEN_RE.fullmatch(token) for token in signature.split("_")]
    return tuple(
        canonical_marker_id(match.group(2))
        for match in matches
        if match is not None
    )


def additive_column_marker_ids(column: str) -> frozenset[str]:
    """Return marker dependencies for one additive Stage 2 summary column."""

    if column.startswith("class_") and column.endswith("_count"):
        body = column[len("class_"):]
        for suffix in ("_indeterminate_count", "_evaluable_count", "_count"):
            if body.endswith(suffix):
                body = body[:-len(suffix)]
                break
        return frozenset(
            canonical_marker_id(token.rstrip("+-"))
            for token in body.split("_")
            if token.rstrip("+-")
        )
    if not column.endswith(ADDITIVE_MARKER_SUFFIXES):
        return frozenset()
    marker_prefix = column.split("_", 1)[0]
    return frozenset({canonical_marker_id(marker_prefix)}) if marker_prefix else frozenset()


class Stage2IndexError(ValueError):
    """Raised when a Stage 2 declaration is incomplete, ambiguous, or stale."""


@dataclass(frozen=True)
class RunDeclaration:
    """One explicitly assigned Stage 2 process (full run or one shard)."""

    analysis_dir: Path
    samplesheet: Path
    process_exit_code: int = 0


@dataclass(frozen=True)
class ValidatedStage2Index:
    """Resolved artifacts from a fully revalidated index."""

    document: Mapping[str, Any]
    index_sha256: str
    summary_paths: tuple[Path, ...]
    analysis_dirs: tuple[Path, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Stage2IndexError(message)


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 of *path* without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    """Hash canonical JSON for sets/configuration/index integrity."""

    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_bytes_snapshot(path: Path, label: str) -> tuple[bytes, str]:
    _require(path.is_file(), f"{label} not found: {path}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise Stage2IndexError(f"{label} is unreadable: {path}: {exc}") from exc
    return payload, hashlib.sha256(payload).hexdigest()


def _read_json_snapshot(path: Path, label: str) -> tuple[Mapping[str, Any], str]:
    payload, digest = _read_bytes_snapshot(path, label)
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Stage2IndexError(f"{label} is unreadable JSON: {path}: {exc}") from exc
    _require(isinstance(value, dict), f"{label} must contain a JSON object: {path}")
    return value, digest


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    value, _ = _read_json_snapshot(path, label)
    return value


def _read_csv_snapshot(
    path: Path, label: str
) -> tuple[list[str], list[dict[str, str]], str]:
    payload, digest = _read_bytes_snapshot(path, label)
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        _require(reader.fieldnames is not None, f"{label} has no header: {path}")
        header = list(reader.fieldnames)
        _require(len(header) == len(set(header)), f"{label} repeats a column: {path}")
        rows = []
        for row_number, row in enumerate(reader, start=2):
            _require(
                None not in row,
                f"{label} row {row_number} has more fields than its header: {path}",
            )
            _require(
                all(value is None or isinstance(value, str) for value in row.values()),
                f"{label} row {row_number} contains a non-text CSV value: {path}",
            )
            if any((value or "").strip() for value in row.values()):
                rows.append(dict(row))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise Stage2IndexError(f"{label} is unreadable CSV: {path}: {exc}") from exc
    _require(rows, f"{label} has no data rows: {path}")
    return header, rows, digest


def _read_csv(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    header, rows, _ = _read_csv_snapshot(path, label)
    return header, rows


def _nonempty(row: Mapping[str, Any], column: str, label: str) -> str:
    value = str(row.get(column) or "").strip()
    _require(bool(value), f"{label} has a blank {column}")
    return value


def _plain_filename(value: str, label: str) -> str:
    _require(
        value not in {".", ".."}
        and value == value.strip()
        and not value.endswith(".")
        and "/" not in value
        and "\\" not in value
        and ":" not in value
        and PurePosixPath(value).name == value,
        f"{label} must be a plain filename without roots, traversal, or separators",
    )
    return value


def _require_columns(header: Sequence[str], required: Iterable[str], label: str) -> None:
    missing = [column for column in required if column not in header]
    _require(not missing, f"{label} is missing required columns: {', '.join(missing)}")


def _portable_relative(path: Path, slide_dir: Path, label: str) -> str:
    try:
        relative = path.resolve(strict=True).relative_to(slide_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise Stage2IndexError(
            f"{label} must resolve inside the slide directory: {path}"
        ) from exc
    token = relative.as_posix()
    _require(token not in {"", "."}, f"{label} cannot be the slide directory itself")
    _require(".." not in PurePosixPath(token).parts, f"{label} contains parent traversal")
    return token


def _resolve_portable(slide_dir: Path, token: Any, label: str) -> Path:
    _require(isinstance(token, str) and bool(token), f"{label} must be a relative path")
    _require("\\" not in token, f"{label} must use portable '/' separators")
    pure = PurePosixPath(token)
    _require(not pure.is_absolute(), f"{label} cannot be absolute")
    _require(".." not in pure.parts, f"{label} cannot contain parent traversal")
    _require(not re.match(r"^[A-Za-z]:", token), f"{label} cannot be a drive path")
    candidate = slide_dir.joinpath(*pure.parts)
    try:
        candidate.resolve(strict=True).relative_to(slide_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise Stage2IndexError(f"{label} escapes or is missing from the slide directory") from exc
    return candidate


def _artifact(
    path: Path, slide_dir: Path, label: str, *, snapshot_sha256: str | None = None
) -> dict[str, Any]:
    return {
        "path": _portable_relative(path, slide_dir, label),
        "sha256": snapshot_sha256 or sha256_file(path),
    }


def _csv_artifact(
    path: Path,
    slide_dir: Path,
    row_count: int,
    label: str,
    *,
    snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    result = _artifact(
        path, slide_dir, label, snapshot_sha256=snapshot_sha256
    )
    result["row_count"] = row_count
    return result


def _check_unique(values: Sequence[str], label: str) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    _require(not duplicates, f"duplicate {label}: {duplicates[:5]}")


def _validate_channel_signature(signature: str, label: str) -> None:
    tokens = signature.split("_")
    parsed = [CHANNEL_TOKEN_RE.fullmatch(token) for token in tokens]
    _require(all(parsed), f"{label} has an invalid ordered channel signature: {signature!r}")
    indices = [int(match.group(1)) for match in parsed if match is not None]
    labels = [match.group(2) for match in parsed if match is not None]
    _require(
        indices == sorted(set(indices)),
        f"{label} channel indices must be unique and strictly increasing",
    )
    _require(len(labels) == len(set(labels)), f"{label} repeats a channel label")


def _integer(mapping: Mapping[str, Any], key: str, label: str) -> int:
    value = mapping.get(key)
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{label}.{key} must be an integer",
    )
    return value


def _validate_datetime(value: Any, label: str) -> str:
    _require(
        isinstance(value, str) and value.endswith("Z"),
        f"{label} must be an RFC 3339 UTC date-time ending in Z",
    )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Stage2IndexError(
            f"{label} must be an RFC 3339 UTC date-time ending in Z"
        ) from exc
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed),
        f"{label} must use UTC",
    )
    return value


def _index_hash(document: Mapping[str, Any]) -> str:
    payload = dict(document)
    payload.pop("index_sha256", None)
    return canonical_sha256(payload)


def build_stage2_index(
    *,
    slide_dir: Path,
    stage1_manifest: Path,
    stage2_script: Path,
    runs: Sequence[RunDeclaration],
    tile_manifest: Path | None = None,
    generated_utc: str | None = None,
) -> dict[str, Any]:
    """Build a complete index in memory, failing before any output is written."""

    slide_dir = slide_dir.resolve(strict=True)
    _require(slide_dir.is_dir(), f"slide directory not found: {slide_dir}")
    stage1_manifest = stage1_manifest.resolve(strict=True)
    stage2_script = stage2_script.resolve(strict=True)
    _require(stage2_script.is_file(), f"Stage 2 script not found: {stage2_script}")
    _require(bool(runs), "at least one explicit Stage 2 run is required")
    tile_manifest = (tile_manifest or (slide_dir / "tile_manifest.csv")).resolve(strict=True)
    _require(
        tile_manifest.parent == slide_dir,
        "tile_manifest.csv must be directly inside the slide directory",
    )

    stage1, stage1_manifest_snapshot_sha256 = _read_json_snapshot(
        stage1_manifest, "Stage 1 manifest"
    )
    slides = stage1.get("slides")
    _require(isinstance(slides, list), "Stage 1 manifest must contain a slides array")
    stage1_matches = [
        slide for slide in slides
        if isinstance(slide, dict) and str(slide.get("slide_stem") or "") == slide_dir.name
    ]
    _require(
        len(stage1_matches) == 1,
        f"Stage 1 manifest must contain exactly one slide_stem={slide_dir.name!r}",
    )

    tile_header, tile_rows, tile_manifest_snapshot_sha256 = _read_csv_snapshot(
        tile_manifest, "tile manifest"
    )
    _require_columns(
        tile_header,
        ("tile_id", "tile_file", "roiset_file", "section_id", "panel"),
        "tile manifest",
    )
    tile_ids = [_nonempty(row, "tile_id", "tile manifest") for row in tile_rows]
    tile_files = [
        _plain_filename(_nonempty(row, "tile_file", "tile manifest"), "tile_file")
        for row in tile_rows
    ]
    roi_files = [
        _plain_filename(_nonempty(row, "roiset_file", "tile manifest"), "roiset_file")
        for row in tile_rows
    ]
    section_ids = [_nonempty(row, "section_id", "tile manifest") for row in tile_rows]
    _check_unique(tile_ids, "tile_id in tile manifest")
    _check_unique(tile_files, "tile_file in tile manifest")
    _check_unique(roi_files, "roiset_file in tile manifest")
    _check_unique(section_ids, "section_id in tile manifest")

    stage1_n_tiles = stage1_matches[0].get("n_tiles")
    if stage1_n_tiles is not None:
        _require(
            isinstance(stage1_n_tiles, int) and stage1_n_tiles == len(tile_rows),
            "Stage 1 n_tiles does not equal tile_manifest.csv row count",
        )
    if "n_written" in stage1_matches[0] or "n_resumed" in stage1_matches[0]:
        n_written = stage1_matches[0].get("n_written")
        n_resumed = stage1_matches[0].get("n_resumed")
        _require(
            isinstance(n_written, int) and not isinstance(n_written, bool)
            and isinstance(n_resumed, int) and not isinstance(n_resumed, bool),
            "Stage 1 n_written and n_resumed must both be integers",
        )
        _require(
            n_written + n_resumed == len(tile_rows),
            "Stage 1 n_written + n_resumed does not equal tile manifest row count",
        )

    tile_by_id = {tile_id: row for tile_id, row in zip(tile_ids, tile_rows)}
    tile_by_file = {tile_file: row for tile_file, row in zip(tile_files, tile_rows)}
    tile_by_section = {
        section_id: row for section_id, row in zip(section_ids, tile_rows)
    }
    tile_inputs: list[dict[str, Any]] = []
    for tile_id in sorted(tile_by_id):
        row = tile_by_id[tile_id]
        tile_path = slide_dir / "tiles" / _nonempty(row, "tile_file", "tile manifest")
        roi_path = slide_dir / "tiles" / _nonempty(row, "roiset_file", "tile manifest")
        _require(tile_path.is_file(), f"declared tile input is missing: {tile_path}")
        _require(roi_path.is_file(), f"declared tile ROI is missing: {roi_path}")
        tile_inputs.append(
            {
                "tile_id": tile_id,
                "tile": _artifact(tile_path, slide_dir, f"tile input {tile_id}"),
                "roi": _artifact(roi_path, slide_dir, f"tile ROI {tile_id}"),
            }
        )

    run_records: list[dict[str, Any]] = []
    all_assigned_ids: list[str] = []
    all_success_files: list[str] = []
    all_identities: list[tuple[str, str, str]] = []
    all_output_keys: dict[str, str] = {}
    header_authority: list[str] | None = None
    config_hashes: set[str] = set()
    signatures_by_panel: dict[str, str] = {}
    summary_rows_by_panel: dict[str, list[Mapping[str, str]]] = {}

    declarations = sorted(
        runs,
        key=lambda item: _portable_relative(
            item.analysis_dir.resolve(strict=True), slide_dir, "analysis directory"
        ),
    )
    _check_unique(
        [
            _portable_relative(item.analysis_dir.resolve(strict=True), slide_dir, "analysis directory")
            for item in declarations
        ],
        "analysis_dir in run declarations",
    )

    for declaration in declarations:
        analysis_dir = declaration.analysis_dir.resolve(strict=True)
        samplesheet = declaration.samplesheet.resolve(strict=True)
        _require(analysis_dir.is_dir(), f"analysis directory not found: {analysis_dir}")
        _require(
            isinstance(declaration.process_exit_code, int)
            and not isinstance(declaration.process_exit_code, bool),
            "process_exit_code must be an integer",
        )
        _require(
            declaration.process_exit_code == 0,
            f"Stage 2 process did not exit cleanly for {analysis_dir}: "
            f"exit={declaration.process_exit_code}",
        )
        analysis_rel = _portable_relative(analysis_dir, slide_dir, "analysis directory")

        sample_header, sample_rows, samplesheet_snapshot_sha256 = _read_csv_snapshot(
            samplesheet, "Stage 2 samplesheet"
        )
        _require_columns(
            sample_header, ("filename", "section_id", "panel"), "Stage 2 samplesheet"
        )
        sample_files = [
            _plain_filename(
                _nonempty(row, "filename", "Stage 2 samplesheet"),
                "Stage 2 samplesheet filename",
            )
            for row in sample_rows
        ]
        sample_sections = [
            _nonempty(row, "section_id", "Stage 2 samplesheet") for row in sample_rows
        ]
        _check_unique(sample_files, f"filename in {samplesheet}")
        _check_unique(sample_sections, f"section_id in {samplesheet}")
        unexpected_files = sorted(set(sample_files) - set(tile_by_file))
        _require(
            not unexpected_files,
            f"samplesheet declares files absent from tile manifest: {unexpected_files[:5]}",
        )
        expected_ids: list[str] = []
        for row in sample_rows:
            tile_row = tile_by_file[_nonempty(row, "filename", "Stage 2 samplesheet")]
            section_id = _nonempty(row, "section_id", "Stage 2 samplesheet")
            panel = _nonempty(row, "panel", "Stage 2 samplesheet")
            _require(
                section_id == _nonempty(tile_row, "section_id", "tile manifest"),
                f"samplesheet section_id disagrees with tile manifest for {row['filename']}",
            )
            _require(
                panel == _nonempty(tile_row, "panel", "tile manifest"),
                f"samplesheet panel disagrees with tile manifest for {row['filename']}",
            )
            for column in ("mouse_id", "genotype", "condition"):
                if column in sample_header and column in tile_header:
                    _require(
                        str(row.get(column) or "").strip()
                        == str(tile_row.get(column) or "").strip(),
                        f"samplesheet {column} disagrees with tile manifest for {row['filename']}",
                    )
            expected_ids.append(_nonempty(tile_row, "tile_id", "tile manifest"))
        expected_ids = sorted(expected_ids)
        all_assigned_ids.extend(expected_ids)

        manifest_path = analysis_dir / "run_manifest.json"
        summary_path = analysis_dir / "run_summary.csv"
        manifest, manifest_snapshot_sha256 = _read_json_snapshot(
            manifest_path, "Stage 2 run manifest"
        )
        summary_header, summary_rows, summary_snapshot_sha256 = _read_csv_snapshot(
            summary_path, "Stage 2 run summary"
        )
        _require_columns(
            summary_header,
            (
                "output_key",
                "region",
                "section_id",
                "panel",
                "region_area_um2",
                "n_nuclei",
            ),
            "Stage 2 run summary",
        )
        if header_authority is None:
            header_authority = summary_header
        else:
            _require(
                summary_header == header_authority,
                f"Stage 2 summary headers differ: {summary_path}",
            )

        status = str(manifest.get("status") or "")
        success_count = _integer(manifest, "success_count", "run manifest")
        failure_count = _integer(manifest, "failure_count", "run manifest")
        skipped_count = _integer(manifest, "skipped_count", "run manifest")
        output_failure_count = _integer(manifest, "output_failure_count", "run manifest")
        _require(status == "complete", f"run manifest status is not complete: {analysis_dir}")
        _require(failure_count == 0, f"run manifest reports failures: {analysis_dir}")
        _require(skipped_count == 0, f"run manifest reports skipped inputs: {analysis_dir}")
        _require(
            output_failure_count == 0,
            f"run manifest reports output failures: {analysis_dir}",
        )
        images = manifest.get("images")
        _require(isinstance(images, list), "run manifest images must be an array")
        _require(
            all(isinstance(image, dict) for image in images),
            f"run manifest images must contain only objects: {analysis_dir}",
        )
        successful = [image for image in images if isinstance(image, dict) and image.get("status") == "success"]
        _require(
            len(images) == len(successful),
            f"complete run manifest contains a non-success image record: {analysis_dir}",
        )
        _require(
            len(successful) == success_count == len(sample_rows),
            f"run manifest success count does not equal samplesheet assignment: {analysis_dir}",
        )
        for count_key in ("matched_input_count", "analytical_input_count"):
            if count_key in manifest:
                _require(
                    _integer(manifest, count_key, "run manifest") == len(sample_rows),
                    f"run manifest {count_key} disagrees with samplesheet: {analysis_dir}",
                )

        success_files = [
            _plain_filename(
                _nonempty(image, "file", "run manifest image"),
                "run manifest image file",
            )
            for image in successful
        ]
        _check_unique(success_files, f"successful image file in {manifest_path}")
        _require(
            set(success_files) == set(sample_files),
            f"run manifest successful files do not equal samplesheet assignment: {analysis_dir}",
        )
        all_success_files.extend(success_files)

        output_to_image: dict[str, Mapping[str, Any]] = {}
        for image in successful:
            output_key = _nonempty(image, "output_key", "run manifest image")
            _require(
                output_key not in all_output_keys,
                f"duplicate output_key across declared runs: {output_key}",
            )
            file_name = _nonempty(image, "file", "run manifest image")
            tile_row = tile_by_file[file_name]
            panel = _nonempty(image, "panel", "run manifest image")
            _require(
                panel == _nonempty(tile_row, "panel", "tile manifest"),
                f"run manifest panel disagrees with tile manifest for {file_name}",
            )
            signature = _nonempty(image, "channel_signature", "run manifest image")
            _validate_channel_signature(signature, f"run manifest image {file_name}")
            prior_signature = signatures_by_panel.get(panel)
            _require(
                prior_signature in {None, signature},
                f"declared channel signature differs within panel {panel}",
            )
            signatures_by_panel[panel] = signature
            output_to_image[output_key] = image
            all_output_keys[output_key] = _nonempty(tile_row, "section_id", "tile manifest")

        summary_output_keys: set[str] = set()
        run_identities: list[tuple[str, str, str]] = []
        for row in summary_rows:
            output_key = _nonempty(row, "output_key", "Stage 2 run summary")
            region = _nonempty(row, "region", "Stage 2 run summary")
            section_id = _nonempty(row, "section_id", "Stage 2 run summary")
            panel = _nonempty(row, "panel", "Stage 2 run summary")
            raw_area = _nonempty(row, "region_area_um2", "Stage 2 run summary")
            raw_nuclei = _nonempty(row, "n_nuclei", "Stage 2 run summary")
            try:
                region_area = float(raw_area)
            except ValueError as exc:
                raise Stage2IndexError(
                    f"run summary region_area_um2 is not numeric: {output_key}"
                ) from exc
            _require(
                math.isfinite(region_area) and region_area > 0,
                f"run summary region_area_um2 must be finite and positive: {output_key}",
            )
            try:
                n_nuclei = float(raw_nuclei)
            except ValueError as exc:
                raise Stage2IndexError(
                    f"run summary n_nuclei is not numeric: {output_key}"
                ) from exc
            _require(
                math.isfinite(n_nuclei) and n_nuclei >= 0,
                f"run summary n_nuclei must be finite and non-negative: {output_key}",
            )
            _require(
                output_key in output_to_image,
                f"run summary output_key is absent from its run manifest: {output_key}",
            )
            image = output_to_image[output_key]
            tile_row = tile_by_file[_nonempty(image, "file", "run manifest image")]
            _require(
                section_id == _nonempty(tile_row, "section_id", "tile manifest"),
                f"run summary section_id disagrees with its declared tile: {output_key}",
            )
            _require(
                panel == _nonempty(tile_row, "panel", "tile manifest"),
                f"run summary panel disagrees with its declared tile: {output_key}",
            )
            for column in ("mouse_id", "genotype", "condition"):
                if column in summary_header and column in tile_header:
                    _require(
                        str(row.get(column) or "").strip()
                        == str(tile_row.get(column) or "").strip(),
                        f"run summary {column} disagrees with tile manifest: {output_key}",
                    )
            summary_output_keys.add(output_key)
            run_identities.append((section_id, region, panel))
            summary_rows_by_panel.setdefault(panel, []).append(row)
        _require(
            summary_output_keys == set(output_to_image),
            f"not every successful image has a run summary row: {analysis_dir}",
        )
        _check_unique(
            ["|".join(identity) for identity in run_identities],
            f"analytical identity in {summary_path}",
        )
        all_identities.extend(run_identities)

        config = manifest.get("config")
        _require(isinstance(config, dict), f"run manifest config must be an object: {analysis_dir}")
        config_sha256 = canonical_sha256(config)
        config_hashes.add(config_sha256)
        input_pairs = sorted(
            {
                (
                    _nonempty(tile_by_file[file_name], "tile_id", "tile manifest"),
                    file_name,
                )
                for file_name in sample_files
            }
        )
        run_records.append(
            {
                "run_id": "stage2:" + analysis_rel.replace("/", ":"),
                "role": "shard",
                "analysis_dir": analysis_rel,
                "samplesheet": _csv_artifact(
                    samplesheet,
                    slide_dir,
                    len(sample_rows),
                    "Stage 2 samplesheet",
                    snapshot_sha256=samplesheet_snapshot_sha256,
                ),
                "expected_tile_ids": expected_ids,
                "run_manifest": _artifact(
                    manifest_path,
                    slide_dir,
                    "Stage 2 run manifest",
                    snapshot_sha256=manifest_snapshot_sha256,
                ),
                "run_summary": _csv_artifact(
                    summary_path,
                    slide_dir,
                    len(summary_rows),
                    "Stage 2 run summary",
                    snapshot_sha256=summary_snapshot_sha256,
                ),
                "manifest_status": status,
                "process_exit_code": declaration.process_exit_code,
                "config_sha256": config_sha256,
                "input_set_sha256": canonical_sha256(input_pairs),
                "successful_input_count": success_count,
                "failure_count": failure_count,
                "summary_identity_count": len(run_identities),
            }
        )

    _check_unique(all_assigned_ids, "tile assignment across Stage 2 runs")
    _check_unique(all_success_files, "successful tile file across Stage 2 runs")
    _check_unique(
        ["|".join(identity) for identity in all_identities],
        "analytical identity across Stage 2 runs",
    )
    expected_id_set = set(tile_ids)
    assigned_id_set = set(all_assigned_ids)
    _require(
        assigned_id_set == expected_id_set,
        "Stage 2 assignments do not exactly cover tile_manifest.csv; "
        f"missing={sorted(expected_id_set - assigned_id_set)[:5]} "
        f"extra={sorted(assigned_id_set - expected_id_set)[:5]}",
    )
    _require(
        set(all_success_files) == set(tile_files),
        "Stage 2 successful files do not exactly cover tile_manifest.csv",
    )
    _require(len(config_hashes) == 1, "Stage 2 resolved configs differ across runs")
    _require(bool(signatures_by_panel), "no declared ordered channel signatures were recorded")

    # A union run-summary schema can contain blank fields for markers absent
    # from a particular panel.  Blankness alone is not authority, however: a
    # marker declared in that panel's ordered signature cannot have an emitted
    # additive column with missing values.  Enforce this before publishing an
    # index whose status claims Stage 2 integrity is complete.  The signature
    # does not itself imply that every acquisition channel has an additive
    # measurement role, so no column is invented or required by name here.
    for panel, signature in sorted(signatures_by_panel.items()):
        declared_markers = {
            marker for marker in declared_marker_ids(signature) if marker != "DAPI"
        }
        panel_rows = summary_rows_by_panel.get(panel, [])
        _require(bool(panel_rows), f"panel {panel!r} has no Stage 2 summary rows")
        for column in header_authority or []:
            dependencies = additive_column_marker_ids(column)
            if not dependencies or not dependencies.issubset(declared_markers):
                continue
            missing = sum(
                1
                for row in panel_rows
                if str(row.get(column) or "").strip().upper() in {"", "NA", "N/A"}
            )
            _require(
                missing == 0,
                f"Stage 2 additive column {column!r} is missing in {missing}/"
                f"{len(panel_rows)} row(s) for declared panel marker(s) "
                f"{sorted(dependencies)} in panel {panel!r}",
            )

    if len(run_records) == 1 and set(run_records[0]["expected_tile_ids"]) == expected_id_set:
        run_records[0]["role"] = "full"

    resolved_config_sha256 = next(iter(config_hashes))
    declared_signatures = [
        {"panel": panel, "signature": signature}
        for panel, signature in sorted(signatures_by_panel.items())
    ]
    stage2_script_record = {
        "name": stage2_script.name,
        "sha256": sha256_file(stage2_script),
    }
    measurement_profile_sha256 = canonical_sha256(
        {
            "stage2_script_sha256": stage2_script_record["sha256"],
            "resolved_config_sha256": resolved_config_sha256,
            "declared_channel_signatures": declared_signatures,
        }
    )
    identity_tokens = sorted("|".join(identity) for identity in all_identities)
    generated_utc = generated_utc or datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    generated_utc = _validate_datetime(generated_utc, "generated_utc")
    result: dict[str, Any] = {
        "$schema": SCHEMA_URI,
        "schema_version": SCHEMA_VERSION,
        "index_type": INDEX_TYPE,
        "status": INDEX_STATUS,
        "generated_utc": generated_utc,
        "slide_id": slide_dir.name,
        "stage1_manifest_sha256": stage1_manifest_snapshot_sha256,
        "tile_manifest": {
            **_csv_artifact(
                tile_manifest,
                slide_dir,
                len(tile_rows),
                "tile manifest",
                snapshot_sha256=tile_manifest_snapshot_sha256,
            ),
            "tile_set_sha256": canonical_sha256(
                sorted((tile_id, tile_file) for tile_id, tile_file in zip(tile_ids, tile_files))
            ),
        },
        "stage2_script": stage2_script_record,
        "resolved_config_sha256": resolved_config_sha256,
        "measurement_profile_sha256": measurement_profile_sha256,
        "analytical_identity": ANALYTICAL_IDENTITY,
        "declared_channel_signatures": declared_signatures,
        "tile_inputs": tile_inputs,
        "runs": run_records,
        "coverage": {
            "expected_tile_count": len(tile_rows),
            "declared_input_count": len(all_assigned_ids),
            "successful_input_count": len(all_success_files),
            "summary_row_count": len(all_identities),
            "summary_identity_count": len(identity_tokens),
            "summary_identity_sha256": canonical_sha256(identity_tokens),
        },
    }
    result["index_sha256"] = _index_hash(result)
    return result


def _validate_index_shape(document: Mapping[str, Any]) -> None:
    required = {
        "$schema",
        "schema_version",
        "index_type",
        "status",
        "generated_utc",
        "slide_id",
        "stage1_manifest_sha256",
        "tile_manifest",
        "stage2_script",
        "resolved_config_sha256",
        "measurement_profile_sha256",
        "analytical_identity",
        "declared_channel_signatures",
        "tile_inputs",
        "runs",
        "coverage",
        "index_sha256",
    }
    _require(set(document) == required, "Stage 2 index has missing or unknown root fields")
    _require(document["$schema"] == SCHEMA_URI, "Stage 2 index has the wrong $schema")
    _require(document["schema_version"] == SCHEMA_VERSION, "unsupported Stage 2 index version")
    _require(document["index_type"] == INDEX_TYPE, "wrong Stage 2 index_type")
    _require(document["status"] == INDEX_STATUS, "Stage 2 index is not complete")
    _require(document["analytical_identity"] == ANALYTICAL_IDENTITY, "wrong analytical identity")
    _validate_datetime(document["generated_utc"], "Stage 2 index generated_utc")
    for field in (
        "stage1_manifest_sha256",
        "resolved_config_sha256",
        "measurement_profile_sha256",
        "index_sha256",
    ):
        _require(
            isinstance(document[field], str) and bool(SHA256_RE.fullmatch(document[field])),
            f"Stage 2 index {field} must be lowercase SHA-256",
        )
    _require(isinstance(document.get("runs"), list) and document["runs"], "Stage 2 index runs must be non-empty")
    for index, run in enumerate(document["runs"]):
        _require(isinstance(run, dict), f"Stage 2 index runs[{index}] must be an object")
        for field in ("analysis_dir", "samplesheet", "run_summary"):
            _require(field in run, f"Stage 2 index runs[{index}] lacks {field}")


def validate_stage2_index(
    index_path: Path,
    *,
    slide_dir: Path,
    stage1_manifest: Path,
    stage2_script: Path,
) -> ValidatedStage2Index:
    """Reload every declared artifact and prove it still matches the index."""

    slide_dir = slide_dir.resolve(strict=True)
    index_path = index_path.resolve(strict=True)
    _require(index_path.parent == slide_dir, "Stage 2 index must be inside its slide directory")
    document = _read_json(index_path, "Stage 2 run index")
    _validate_index_shape(document)
    _require(
        document["index_sha256"] == _index_hash(document),
        "Stage 2 index self-hash does not match",
    )
    _require(document["slide_id"] == slide_dir.name, "Stage 2 index slide_id does not match its directory")

    declarations: list[RunDeclaration] = []
    for index, run in enumerate(document["runs"]):
        analysis_dir = _resolve_portable(
            slide_dir, run["analysis_dir"], f"runs[{index}].analysis_dir"
        )
        samplesheet_record = run.get("samplesheet")
        _require(isinstance(samplesheet_record, dict), f"runs[{index}].samplesheet must be an object")
        samplesheet = _resolve_portable(
            slide_dir, samplesheet_record.get("path"), f"runs[{index}].samplesheet.path"
        )
        exit_code = run.get("process_exit_code")
        _require(
            isinstance(exit_code, int) and not isinstance(exit_code, bool),
            f"runs[{index}].process_exit_code must be an integer",
        )
        declarations.append(RunDeclaration(analysis_dir, samplesheet, exit_code))

    tile_record = document.get("tile_manifest")
    _require(isinstance(tile_record, dict), "tile_manifest must be an object")
    tile_manifest = _resolve_portable(
        slide_dir, tile_record.get("path"), "tile_manifest.path"
    )
    expected = build_stage2_index(
        slide_dir=slide_dir,
        stage1_manifest=stage1_manifest,
        stage2_script=stage2_script,
        tile_manifest=tile_manifest,
        runs=declarations,
        generated_utc=str(document["generated_utc"]),
    )
    _require(
        expected == document,
        "Stage 2 index content no longer matches its declared artifacts",
    )
    summary_paths = tuple(
        _resolve_portable(slide_dir, run["run_summary"]["path"], "run_summary.path")
        for run in document["runs"]
    )
    analysis_dirs = tuple(declaration.analysis_dir for declaration in declarations)
    return ValidatedStage2Index(
        document=document,
        index_sha256=str(document["index_sha256"]),
        summary_paths=summary_paths,
        analysis_dirs=analysis_dirs,
    )


def write_stage2_index_atomic(document: Mapping[str, Any], output_path: Path) -> None:
    """Atomically publish a previously validated index document."""

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
