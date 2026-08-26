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
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_URI = "https://ifquant-lung.invalid/schemas/stage2-run-index.schema.json"
SCHEMA_VERSION = "1.3.0"
INDEX_TYPE = "ifquant_wsi_stage2_run_index"
INDEX_STATUS = "stage2_integrity_complete"
ANALYTICAL_IDENTITY = "section_id|region|panel"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_PACKAGE_AUTHORITY = "bioformats_ImageReader_getUsedFiles"
SOURCE_PACKAGE_HASH_ALGORITHM = "sha256_utf8_path_tab_size_tab_sha256_lf"
SOURCE_METADATA_AUTHORITY = "bioformats_used_files_content_verified_before_and_after_stage1"
REFERENCE_MASK_SCHEMA_URI = (
    "https://ifquant-lung.invalid/schemas/wsi-reference-mask-profile.schema.json"
)
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


def _strict_nonempty_text(mapping: Mapping[str, Any], key: str, label: str) -> str:
    """Return an identity string without silently normalizing whitespace."""

    value = mapping.get(key)
    _require(
        isinstance(value, str) and bool(value) and value == value.strip(),
        f"{label}.{key} must be a non-empty string without surrounding whitespace",
    )
    _require(
        all(character not in value for character in "\t\r\n"),
        f"{label}.{key} cannot contain control whitespace",
    )
    return value


def _portable_reference_path(value: Any, label: str) -> str:
    """Validate a profile-relative path without resolving workstation paths."""

    _require(
        isinstance(value, str) and bool(value) and value == value.strip(),
        f"{label} must be a non-empty portable relative path",
    )
    parts = value.split("/")
    _require(
        "\\" not in value
        and not value.startswith("/")
        and not re.match(r"^[A-Za-z]:", value)
        and all(part not in {"", ".", ".."} for part in parts)
        and all(character not in value for character in "\t\r\n"),
        f"{label} is not a safe portable relative path",
    )
    return value


def _content_identity(value: Any, label: str) -> dict[str, Any]:
    """Validate the portable name/size/SHA record emitted by the Fiji engine."""

    _require(isinstance(value, dict), f"{label} must be an object")
    _require(
        set(value) == {"name", "size_bytes", "sha256"},
        f"{label} must contain exactly name, size_bytes, and sha256",
    )
    name = _plain_filename(_nonempty(value, "name", label), f"{label}.name")
    size = value.get("size_bytes")
    _require(
        isinstance(size, int) and not isinstance(size, bool) and size >= 0,
        f"{label}.size_bytes must be a non-negative integer",
    )
    sha = value.get("sha256")
    _require(
        isinstance(sha, str) and bool(SHA256_RE.fullmatch(sha)),
        f"{label}.sha256 must be a lowercase SHA-256",
    )
    return {"name": name, "size_bytes": size, "sha256": sha}


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


def _finite_number(
    mapping: Mapping[str, Any], key: str, label: str, *, positive: bool = False
) -> float:
    value = mapping.get(key)
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        f"{label}.{key} must be numeric",
    )
    number = float(value)
    _require(math.isfinite(number), f"{label}.{key} must be finite")
    if positive:
        _require(number > 0, f"{label}.{key} must be positive")
    return number


def _stage1_script(stage1: Mapping[str, Any]) -> dict[str, Any]:
    record = stage1.get("stage1_script")
    _require(isinstance(record, dict), "Stage 1 manifest lacks stage1_script")
    name = _plain_filename(
        _nonempty(record, "name", "Stage 1 stage1_script"),
        "Stage 1 stage1_script.name",
    )
    size_bytes = _integer(record, "size_bytes", "Stage 1 stage1_script")
    _require(size_bytes > 0, "Stage 1 stage1_script.size_bytes must be positive")
    sha256 = _nonempty(record, "sha256", "Stage 1 stage1_script").lower()
    _require(
        SHA256_RE.fullmatch(sha256) is not None,
        "Stage 1 stage1_script.sha256 must be a lowercase SHA-256",
    )
    return {"name": name, "size_bytes": size_bytes, "sha256": sha256}


def _stage1_source_package(slide: Mapping[str, Any], source_vsi: str) -> dict[str, Any]:
    package = slide.get("source_package")
    _require(isinstance(package, dict), "Stage 1 slide lacks source_package")
    _require(
        package.get("format") == "olympus_vsi",
        "Stage 1 source_package.format must be olympus_vsi",
    )
    _require(
        package.get("discovery_authority") == SOURCE_PACKAGE_AUTHORITY,
        "Stage 1 source package was not discovered by Bio-Formats getUsedFiles",
    )
    _require(
        package.get("package_hash_algorithm") == SOURCE_PACKAGE_HASH_ALGORITHM,
        "Stage 1 source package hash algorithm is missing or unsupported",
    )
    _require(
        _plain_filename(
            _nonempty(package, "source_vsi", "Stage 1 source_package"),
            "Stage 1 source_package.source_vsi",
        )
        == source_vsi,
        "Stage 1 source_package.source_vsi disagrees with the slide source_vsi",
    )
    members = package.get("members")
    _require(
        isinstance(members, list) and bool(members),
        "Stage 1 source_package.members must be a non-empty array",
    )
    normalized_members: list[dict[str, Any]] = []
    for index, member in enumerate(members, start=1):
        label = f"Stage 1 source_package.members[{index}]"
        _require(isinstance(member, dict), f"{label} must be an object")
        token = _nonempty(member, "relative_path", label)
        pure = PurePosixPath(token)
        _require("\\" not in token, f"{label}.relative_path must use '/' separators")
        _require(not pure.is_absolute(), f"{label}.relative_path cannot be absolute")
        _require(
            token == pure.as_posix()
            and token not in {".", ".."}
            and ".." not in pure.parts
            and not re.match(r"^[A-Za-z]:", token)
            and all(character not in token for character in "\t\r\n"),
            f"{label}.relative_path is not a safe portable package path",
        )
        size_bytes = _integer(member, "size_bytes", label)
        _require(size_bytes > 0, f"{label}.size_bytes must be positive")
        sha256 = _nonempty(member, "sha256", label).lower()
        _require(
            SHA256_RE.fullmatch(sha256) is not None,
            f"{label}.sha256 must be a lowercase SHA-256",
        )
        normalized_members.append(
            {"relative_path": token, "size_bytes": size_bytes, "sha256": sha256}
        )
    paths = [member["relative_path"] for member in normalized_members]
    _check_unique(paths, "relative_path in Stage 1 source package")
    _require(
        paths == sorted(paths),
        "Stage 1 source_package.members must be sorted by relative_path",
    )
    _require(
        source_vsi in paths,
        "Stage 1 source package does not contain its source VSI",
    )
    package_lines = "".join(
        f"{member['relative_path']}\t{member['size_bytes']}\t{member['sha256']}\n"
        for member in normalized_members
    )
    package_sha256 = _nonempty(
        package, "package_sha256", "Stage 1 source_package"
    ).lower()
    _require(
        SHA256_RE.fullmatch(package_sha256) is not None,
        "Stage 1 source_package.package_sha256 must be a lowercase SHA-256",
    )
    _require(
        hashlib.sha256(package_lines.encode("utf-8")).hexdigest() == package_sha256,
        "Stage 1 source_package.package_sha256 does not match its member ledger",
    )
    return {
        "format": "olympus_vsi",
        "source_vsi": source_vsi,
        "discovery_authority": SOURCE_PACKAGE_AUTHORITY,
        "package_hash_algorithm": SOURCE_PACKAGE_HASH_ALGORITHM,
        "members": normalized_members,
        "package_sha256": package_sha256,
    }


def _stage1_profile(stage1: Mapping[str, Any]) -> dict[str, Any]:
    """Select only Stage 1 settings that define analytical measurement identity."""

    profile_fields = (
        "schema_version",
        "stage",
        "stage1_script",
        "qupath_series_selection",
        "tiling",
        "tissue",
        "export",
        "downstream",
    )
    profile: dict[str, Any] = {}
    for field in profile_fields:
        value = stage1.get(field)
        _require(value is not None, f"Stage 1 manifest lacks profile field {field}")
        if field in {"schema_version", "stage"}:
            _require(
                isinstance(value, str) and bool(value.strip()),
                f"Stage 1 profile field {field} must be a non-empty string",
            )
            value = value.strip()
        else:
            _require(
                isinstance(value, dict),
                f"Stage 1 profile field {field} must be an object",
            )
        profile[field] = _stage1_script(stage1) if field == "stage1_script" else value
    return profile


def _verified_reference_artifact(
    record: Any, *, base_dir: Path, label: str
) -> dict[str, Any]:
    _require(isinstance(record, dict), f"{label} must be an object")
    _require(
        set(record) == {"published_relative_path", "content"},
        f"{label} must contain exactly published_relative_path and content",
    )
    published_relative_path = _portable_reference_path(
        record.get("published_relative_path"), f"{label}.published_relative_path"
    )
    path = _resolve_portable(base_dir, published_relative_path, label)
    _require(path.is_file(), f"{label} must resolve to a regular file")
    identity = _content_identity(record.get("content"), f"{label}.content")
    _require(identity["size_bytes"] > 0, f"{label} cannot be empty")
    _require(identity["name"] == path.name, f"{label}.content.name disagrees with its path")
    _require(
        path.stat().st_size == identity["size_bytes"]
        and sha256_file(path) == identity["sha256"],
        f"{label} content does not match the referenced bytes",
    )
    return {
        "published_relative_path": published_relative_path,
        "content": identity,
    }


def _validate_published_reference_profile(
    *,
    profile_artifact: Mapping[str, Any],
    stage1_root: Path,
    stage1: Mapping[str, Any],
    slide: Mapping[str, Any],
    reference: Mapping[str, Any],
    normalized_sources: Mapping[str, Mapping[str, Any]],
) -> None:
    """Cross-check the published profile bytes against every Stage 1 claim.

    Merely hashing the copied JSON is insufficient: a Stage 1 manifest could
    otherwise claim a different slide/series or different source-mask ledger
    while continuing to point at an unrelated, content-addressed profile.
    """

    profile_path = _resolve_portable(
        stage1_root,
        profile_artifact.get("published_relative_path"),
        "Stage 1 reference-mask profile",
    )
    profile, profile_snapshot_sha256 = _read_json_snapshot(
        profile_path, "Stage 1 reference-mask profile"
    )
    _require(
        profile_snapshot_sha256 == profile_artifact["content"]["sha256"],
        "Stage 1 reference-mask profile changed while it was being validated",
    )
    root_fields = {
        "$schema",
        "schema_version",
        "profile_id",
        "review_state",
        "review_protocol_id",
        "coordinate_space",
        "mask_logic",
        "slides",
    }
    _require(
        set(profile) == root_fields,
        "Stage 1 reference-mask profile has missing or unknown root fields",
    )
    _require(
        profile.get("$schema") == REFERENCE_MASK_SCHEMA_URI
        and profile.get("schema_version") == "1.0.0",
        "Stage 1 reference-mask profile schema is unsupported",
    )
    for key in ("profile_id", "review_protocol_id"):
        _strict_nonempty_text(profile, key, "Stage 1 reference-mask profile")
    _require(
        profile.get("review_state") in {"engineering_unreviewed", "expert_reviewed"},
        "Stage 1 reference-mask profile review_state is unsupported",
    )
    _require(
        profile.get("coordinate_space") == "selected_series_downsample_grid"
        and profile.get("mask_logic")
        == "tissue_foreground_minus_airway_foreground",
        "Stage 1 reference-mask profile coordinate/mask semantics are unsupported",
    )
    for key in (
        "profile_id",
        "review_state",
        "review_protocol_id",
        "coordinate_space",
        "mask_logic",
    ):
        _require(
            profile.get(key) == reference.get(key),
            f"Stage 1 reference-mask profile {key} disagrees with the slide declaration",
        )

    profile_slides = profile.get("slides")
    _require(
        isinstance(profile_slides, list) and bool(profile_slides),
        "Stage 1 reference-mask profile slides must be a non-empty array",
    )
    slide_fields = {
        "source_vsi",
        "source_package_sha256",
        "series_index",
        "full_resolution_width",
        "full_resolution_height",
        "downsample",
        "mask_width",
        "mask_height",
        "tissue_mask",
        "airway_mask",
    }
    profile_by_source: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(profile_slides, start=1):
        label = f"Stage 1 reference-mask profile slides[{index}]"
        _require(isinstance(row, dict), f"{label} must be an object")
        _require(set(row) == slide_fields, f"{label} has missing or unknown fields")
        source_vsi = _plain_filename(
            _strict_nonempty_text(row, "source_vsi", label), f"{label}.source_vsi"
        )
        _require(
            source_vsi.lower().endswith(".vsi"),
            f"{label}.source_vsi must be a .vsi filename",
        )
        _require(
            source_vsi not in profile_by_source,
            f"Stage 1 reference-mask profile repeats source_vsi {source_vsi!r}",
        )
        profile_by_source[source_vsi] = row

    manifest_slides = stage1.get("slides")
    _require(
        isinstance(manifest_slides, list) and bool(manifest_slides),
        "Stage 1 manifest slides must be a non-empty array",
    )
    manifest_sources: list[str] = []
    for index, manifest_slide in enumerate(manifest_slides, start=1):
        label = f"Stage 1 manifest slides[{index}]"
        _require(isinstance(manifest_slide, dict), f"{label} must be an object")
        manifest_sources.append(
            _plain_filename(
                _strict_nonempty_text(manifest_slide, "source_vsi", label),
                f"{label}.source_vsi",
            )
        )
    _check_unique(manifest_sources, "source_vsi in Stage 1 manifest")
    _require(
        set(profile_by_source) == set(manifest_sources),
        "Stage 1 reference-mask profile slide set does not exactly match the Stage 1 manifest",
    )

    source_vsi = _plain_filename(
        _strict_nonempty_text(slide, "source_vsi", "Stage 1 slide"),
        "Stage 1 slide.source_vsi",
    )
    profile_slide = profile_by_source[source_vsi]
    source_package = slide.get("source_package")
    _require(isinstance(source_package, dict), "Stage 1 slide lacks source_package")
    _require(
        profile_slide.get("source_package_sha256")
        == source_package.get("package_sha256"),
        "Stage 1 reference-mask profile source package does not match the slide",
    )
    for profile_key, slide_key in (
        ("series_index", "series_index"),
        ("full_resolution_width", "width"),
        ("full_resolution_height", "height"),
    ):
        _require(
            _integer(profile_slide, profile_key, "Stage 1 reference-mask profile slide")
            == _integer(slide, slide_key, "Stage 1 slide"),
            f"Stage 1 reference-mask profile {profile_key} disagrees with the slide",
        )
    profile_downsample = _finite_number(
        profile_slide,
        "downsample",
        "Stage 1 reference-mask profile slide",
        positive=True,
    )
    reference_downsample = _finite_number(
        reference, "downsample", "Stage 1 reference_space", positive=True
    )
    _require(
        math.isclose(
            profile_downsample, reference_downsample, rel_tol=0.0, abs_tol=1e-12
        )
        and _integer(
            profile_slide, "mask_width", "Stage 1 reference-mask profile slide"
        )
        == _integer(reference, "mask_width", "Stage 1 reference_space")
        and _integer(
            profile_slide, "mask_height", "Stage 1 reference-mask profile slide"
        )
        == _integer(reference, "mask_height", "Stage 1 reference_space"),
        "Stage 1 reference-mask profile downsample grid disagrees with the slide",
    )

    artifact_fields = {"relative_path", "size_bytes", "sha256"}
    for profile_key, source_key in (
        ("tissue_mask", "source_tissue_mask"),
        ("airway_mask", "source_airway_mask"),
    ):
        label = f"Stage 1 reference-mask profile {profile_key}"
        declaration = profile_slide.get(profile_key)
        _require(isinstance(declaration, dict), f"{label} must be an object")
        _require(
            set(declaration) == artifact_fields,
            f"{label} has missing or unknown fields",
        )
        relative_path = _portable_reference_path(
            declaration.get("relative_path"), f"{label}.relative_path"
        )
        size_bytes = declaration.get("size_bytes")
        _require(
            isinstance(size_bytes, int)
            and not isinstance(size_bytes, bool)
            and size_bytes > 0,
            f"{label}.size_bytes must be a positive integer",
        )
        sha256 = declaration.get("sha256")
        _require(
            isinstance(sha256, str) and bool(SHA256_RE.fullmatch(sha256)),
            f"{label}.sha256 must be a lowercase SHA-256",
        )
        source_record = normalized_sources[source_key]
        _require(
            source_record["profile_relative_path"] == relative_path
            and source_record["content"]["size_bytes"] == size_bytes
            and source_record["content"]["sha256"] == sha256,
            f"Stage 1 {source_key} disagrees with the published reference-mask profile",
        )


def _stage1_reference_space(
    slide: Mapping[str, Any], stage1: Mapping[str, Any], *, slide_dir: Path,
    stage1_root: Path,
) -> dict[str, Any]:
    """Validate and rehash the exact raster defining the Stage 1 sampling frame."""

    tissue_profile = stage1.get("tissue")
    _require(isinstance(tissue_profile, dict), "Stage 1 tissue profile must be an object")
    mode = _nonempty(tissue_profile, "mode", "Stage 1 tissue profile")
    _require(
        mode in {"automatic_dapi_otsu_engineering", "external_binary_reference_masks"},
        "Stage 1 tissue profile has an unsupported reference-space mode",
    )
    reference = slide.get("reference_space")
    _require(isinstance(reference, dict), "Stage 1 slide lacks reference_space")
    _require(reference.get("mode") == mode, "Stage 1 reference-space modes disagree")
    _require(
        reference.get("content_verified_before_and_after") is True,
        "Stage 1 reference-space artifacts were not verified before and after tiling",
    )
    _require(
        reference.get("coordinate_space") == "selected_series_downsample_grid",
        "Stage 1 reference-space coordinate system is unsupported",
    )
    _require(
        reference.get("sampling_semantics")
        == "exhaustive_grid_over_declared_reference_space",
        "Stage 1 reference-space sampling semantics are missing or unsupported",
    )
    profile_id = _nonempty(reference, "profile_id", "Stage 1 reference_space")
    review_state = _nonempty(reference, "review_state", "Stage 1 reference_space")
    _require(
        review_state in {"engineering_unreviewed", "expert_reviewed"},
        "Stage 1 reference-space review_state is unsupported",
    )
    review_protocol_id = _nonempty(
        reference, "review_protocol_id", "Stage 1 reference_space"
    )
    authority = _nonempty(reference, "authority", "Stage 1 reference_space")
    mask_logic = _nonempty(reference, "mask_logic", "Stage 1 reference_space")
    downsample = _finite_number(
        reference, "downsample", "Stage 1 reference_space", positive=True
    )
    mask_width = _integer(reference, "mask_width", "Stage 1 reference_space")
    mask_height = _integer(reference, "mask_height", "Stage 1 reference_space")
    _require(mask_width > 0 and mask_height > 0, "Stage 1 mask dimensions must be positive")
    _require(
        mask_width == math.ceil(_integer(slide, "width", "Stage 1 slide") / downsample)
        and mask_height
        == math.ceil(_integer(slide, "height", "Stage 1 slide") / downsample),
        "Stage 1 reference-space raster dimensions do not match the declared series grid",
    )
    foreground = _integer(
        reference, "analysis_tissue_foreground_px", "Stage 1 reference_space"
    )
    _require(
        0 < foreground <= mask_width * mask_height,
        "Stage 1 analysis_tissue_foreground_px is outside the raster bounds",
    )
    normalized: dict[str, Any] = {
        "mode": mode,
        "authority": authority,
        "profile_id": profile_id,
        "review_state": review_state,
        "review_protocol_id": review_protocol_id,
        "coordinate_space": "selected_series_downsample_grid",
        "mask_logic": mask_logic,
        "sampling_semantics": "exhaustive_grid_over_declared_reference_space",
        "airway_excluded": reference.get("airway_excluded"),
        "downsample": downsample,
        "mask_width": mask_width,
        "mask_height": mask_height,
        "analysis_tissue_foreground_px": foreground,
        "tissue_mask": _verified_reference_artifact(
            reference.get("tissue_mask"),
            base_dir=slide_dir,
            label="Stage 1 analysis tissue mask",
        ),
        "content_verified_before_and_after": True,
    }

    if mode == "automatic_dapi_otsu_engineering":
        _require(
            reference.get("airway_excluded") is False,
            "automatic DAPI reference space cannot claim airway exclusion",
        )
        _require(
            profile_id == "automatic_dapi_otsu_engineering"
            and review_state == "engineering_unreviewed"
            and review_protocol_id == "automatic_dapi_otsu"
            and authority
            == "generated_dapi_otsu_raster_and_content_bound_tile_rois"
            and mask_logic == "dapi_otsu_tissue_without_airway_exclusion",
            "automatic DAPI reference-space declaration is inconsistent",
        )
        _require(
            tissue_profile.get("airway_exclusion") == "not_available"
            and tissue_profile.get("reference_mask_profile") is None,
            "automatic DAPI tissue profile must disclose that airway exclusion is unavailable",
        )
    else:
        _require(
            reference.get("airway_excluded") is True,
            "external reference space must subtract an explicit airway mask",
        )
        _require(
            authority == "content_bound_external_profile_and_binary_masks"
            and mask_logic == "tissue_foreground_minus_airway_foreground",
            "external reference-space mask logic is unsupported",
        )
        _require(
            tissue_profile.get("airway_exclusion") == "explicit_binary_mask_subtracted",
            "external tissue profile must declare explicit airway subtraction",
        )
        profile_record = tissue_profile.get("reference_mask_profile")
        _require(isinstance(profile_record, dict), "Stage 1 lacks reference-mask profile evidence")
        _require(
            set(profile_record)
            == {
                "profile_id",
                "review_state",
                "review_protocol_id",
                "coordinate_space",
                "mask_logic",
                "published_relative_path",
                "content",
                "content_verified_before_and_after",
            },
            "Stage 1 reference-mask profile evidence has missing or unknown fields",
        )
        _require(
            profile_record.get("content_verified_before_and_after") is True,
            "reference-mask profile was not verified before and after Stage 1",
        )
        _require(
            profile_record.get("profile_id") == profile_id
            and profile_record.get("review_state") == review_state
            and profile_record.get("review_protocol_id") == review_protocol_id
            and profile_record.get("coordinate_space") == reference.get("coordinate_space")
            and profile_record.get("mask_logic") == mask_logic,
            "root and slide reference-mask profile declarations disagree",
        )
        profile_artifact = _verified_reference_artifact(
            {
                "published_relative_path": profile_record.get("published_relative_path"),
                "content": profile_record.get("content"),
            },
            base_dir=stage1_root,
            label="Stage 1 reference-mask profile",
        )
        profile_sha = _nonempty(reference, "profile_sha256", "Stage 1 reference_space")
        _require(
            profile_sha == profile_artifact["content"]["sha256"],
            "Stage 1 slide profile_sha256 disagrees with the published profile bytes",
        )
        source_tissue = reference.get("source_tissue_mask")
        source_airway = reference.get("source_airway_mask")
        _require(
            isinstance(source_tissue, dict) and isinstance(source_airway, dict),
            "external reference space must bind source tissue and airway masks",
        )
        normalized_sources: dict[str, Any] = {}
        for key, record in (
            ("source_tissue_mask", source_tissue),
            ("source_airway_mask", source_airway),
        ):
            _require(
                set(record) == {"profile_relative_path", "published_relative_path", "content"},
                f"Stage 1 {key} has unexpected fields",
            )
            token = _nonempty(record, "profile_relative_path", f"Stage 1 {key}")
            pure = PurePosixPath(token)
            _require(
                "\\" not in token
                and not pure.is_absolute()
                and ".." not in pure.parts
                and token == pure.as_posix(),
                f"Stage 1 {key}.profile_relative_path is not portable",
            )
            artifact = _verified_reference_artifact(
                {
                    "published_relative_path": record.get("published_relative_path"),
                    "content": record.get("content"),
                },
                base_dir=slide_dir,
                label=f"Stage 1 {key}",
            )
            normalized_sources[key] = {
                "profile_relative_path": token,
                **artifact,
            }
        raw_tissue = _integer(
            reference,
            "tissue_foreground_px_before_airway_exclusion",
            "Stage 1 reference_space",
        )
        airway = _integer(reference, "airway_foreground_px", "Stage 1 reference_space")
        _require(
            0 < raw_tissue <= mask_width * mask_height
            and 0 <= airway < raw_tissue
            and foreground == raw_tissue - airway,
            "Stage 1 tissue/airway foreground counts do not reconcile exactly",
        )
        _validate_published_reference_profile(
            profile_artifact=profile_artifact,
            stage1_root=stage1_root,
            stage1=stage1,
            slide=slide,
            reference=reference,
            normalized_sources=normalized_sources,
        )
        normalized.update(
            {
                "profile_sha256": profile_sha,
                "reference_mask_profile": profile_artifact,
                "tissue_foreground_px_before_airway_exclusion": raw_tissue,
                "airway_foreground_px": airway,
                **normalized_sources,
            }
        )
    return normalized


def _stage1_source_metadata(
    slide: Mapping[str, Any], stage1: Mapping[str, Any], *, slide_dir: Path,
    stage1_root: Path,
) -> dict[str, Any]:
    """Return the stable acquisition metadata declared by Stage 1."""

    source_vsi = _nonempty(slide, "source_vsi", "Stage 1 slide")
    source_vsi = _plain_filename(source_vsi, "Stage 1 slide.source_vsi")
    source_package = _stage1_source_package(slide, source_vsi)
    stage1_script = _stage1_script(stage1)
    reference_space = _stage1_reference_space(
        slide, stage1, slide_dir=slide_dir, stage1_root=stage1_root
    )
    series_name = _nonempty(slide, "series_name", "Stage 1 slide")
    series_index = _integer(slide, "series_index", "Stage 1 slide")
    _require(series_index >= 0, "Stage 1 slide.series_index must be non-negative")
    width = _integer(slide, "width", "Stage 1 slide")
    height = _integer(slide, "height", "Stage 1 slide")
    n_channels = _integer(slide, "n_channels", "Stage 1 slide")
    _require(width > 0 and height > 0, "Stage 1 source dimensions must be positive")
    _require(n_channels > 0, "Stage 1 slide.n_channels must be positive")
    channel_names = slide.get("channel_names")
    _require(
        isinstance(channel_names, list)
        and len(channel_names) == n_channels
        and all(isinstance(name, str) and bool(name.strip()) for name in channel_names),
        "Stage 1 slide.channel_names must contain one non-empty label per channel",
    )
    ordered_channel_names = slide.get("ordered_channel_names")
    _require(
        ordered_channel_names == channel_names,
        "Stage 1 ordered_channel_names must exactly equal channel_names",
    )
    selection = stage1.get("qupath_series_selection")
    _require(
        isinstance(selection, dict),
        "Stage 1 qupath_series_selection must be an object",
    )
    root_patterns = selection.get("ordered_channel_patterns")
    slide_patterns = slide.get("ordered_channel_patterns")
    expected_channel_count = _integer(
        selection, "expect_channels", "Stage 1 qupath_series_selection"
    )
    _require(
        expected_channel_count == n_channels,
        "Stage 1 expect_channels must equal the selected source channel count",
    )
    _require(
        isinstance(root_patterns, list)
        and len(root_patterns) == n_channels
        and all(isinstance(pattern, str) and bool(pattern.strip()) for pattern in root_patterns),
        "Stage 1 root ordered_channel_patterns must contain one pattern per channel",
    )
    _require(
        slide_patterns == root_patterns,
        "Stage 1 slide ordered_channel_patterns must equal the root declaration",
    )
    for index, (pattern, channel_name) in enumerate(
        zip(root_patterns, channel_names), start=1
    ):
        try:
            # QuPath compiles these declarations with Java CASE_INSENSITIVE
            # before applying whole-string matching (Groovy ==~).
            matches = re.fullmatch(pattern, channel_name, flags=re.IGNORECASE) is not None
        except re.error as exc:
            raise Stage2IndexError(
                f"Stage 1 ordered_channel_patterns[{index}] is not a supported "
                "regular expression"
            ) from exc
        _require(
            matches,
            f"Stage 1 ordered_channel_patterns[{index}] does not full-match "
            "the recorded channel name",
        )
    channel_order_authority = "acquisition_order_pattern_only_not_biological_identity"
    _require(
        selection.get("channel_order_authority") == channel_order_authority
        and slide.get("channel_order_authority") == channel_order_authority,
        "Stage 1 channel_order_authority is missing or unsupported",
    )
    return {
        "source_vsi": source_vsi,
        "source_package": source_package,
        "source_package_sha256": source_package["package_sha256"],
        "stage1_script_sha256": stage1_script["sha256"],
        "series_index": series_index,
        "series_name": series_name,
        "width": width,
        "height": height,
        "pixel_width_um": _finite_number(
            slide, "pixel_size_um", "Stage 1 slide", positive=True
        ),
        "pixel_height_um": _finite_number(
            slide, "pixel_size_um_y", "Stage 1 slide", positive=True
        ),
        "n_channels": n_channels,
        "expected_channel_count": expected_channel_count,
        "channel_names": [name.strip() for name in channel_names],
        "ordered_channel_names": [name.strip() for name in ordered_channel_names],
        "ordered_channel_patterns": [pattern.strip() for pattern in root_patterns],
        "channel_order_authority": channel_order_authority,
        "source_metadata_authority": SOURCE_METADATA_AUTHORITY,
        "reference_space": reference_space,
    }


def _runtime_profile(manifest: Mapping[str, Any], label: str) -> dict[str, str]:
    """Normalize stable runtime identifiers and exclude timestamps/host details."""

    versions = manifest.get("versions")
    _require(isinstance(versions, dict), f"{label} versions must be an object")
    profile = {
        key: _nonempty(versions, key, f"{label} versions")
        for key in (
            "imagej_version",
            "bioformats_version",
            "java_version",
            "java_vendor",
            "java_runtime",
            "java_vm",
        )
    }
    for key, value in profile.items():
        _require(
            value.lower() not in {"unknown", "unavailable", "na", "n/a"},
            f"{label} versions.{key} is not an authoritative runtime identifier",
        )
    return profile


def _optional_sha256(
    mapping: Mapping[str, Any], key: str, label: str
) -> str | None:
    _require(key in mapping, f"{label} lacks {key}")
    value = mapping.get(key)
    if value is None or value == "":
        return None
    _require(
        isinstance(value, str) and bool(SHA256_RE.fullmatch(value)),
        f"{label}.{key} must be null or a lowercase SHA-256",
    )
    return value


def _routing_input_hashes(
    config: Mapping[str, Any], *, samplesheet_sha256: str, label: str
) -> dict[str, str | None]:
    """Validate per-run routing claims before excluding them from the profile."""

    declared_samplesheet = _optional_sha256(config, "samplesheetSha256", label)
    _require(
        declared_samplesheet == samplesheet_sha256,
        f"{label}.samplesheetSha256 does not match the declared shard samplesheet",
    )
    return {
        "samplesheet_sha256": declared_samplesheet,
        "panel_map_sha256": _optional_sha256(config, "panelMapSha256", label),
        "canonical_manifest_sha256": _optional_sha256(
            config, "canonicalManifestSha256", label
        ),
    }


def _routing_artifacts(
    manifest: Mapping[str, Any],
    config: Mapping[str, Any],
    routing_hashes: Mapping[str, str | None],
    *,
    analysis_dir: Path,
    slide_dir: Path,
    label: str,
) -> list[dict[str, Any]]:
    """Bind copied panel/canonical routing bytes without treating them as settings."""

    artifacts: list[dict[str, Any]] = []
    panel_hash = routing_hashes["panel_map_sha256"]
    panel_record = manifest.get("panel_map_record")
    if panel_hash is None:
        _require(
            panel_record is None or panel_record == "",
            f"{label} has a panel_map_record without panelMapSha256",
        )
        _require(
            config.get("panelMapMode")
            in {None, "", "single_panel_or_samplesheet"},
            f"{label} panelMapMode requires panelMapSha256",
        )
    else:
        _require(
            config.get("panelMapMode") == "per_image_relative_path",
            f"{label} panelMapSha256 requires per_image_relative_path mode",
        )
        panel_name = _plain_filename(
            _nonempty(manifest, "panel_map_record", label),
            f"{label} panel_map_record",
        )
        panel_path = analysis_dir / panel_name
        _require(panel_path.is_file(), f"{label} panel map artifact is missing")
        actual_panel_hash = sha256_file(panel_path)
        _require(
            actual_panel_hash == panel_hash,
            f"{label} panelMapSha256 does not match panel_map_record bytes",
        )
        artifacts.append(
            {
                "role": "panel_map",
                "artifact": _artifact(
                    panel_path,
                    slide_dir,
                    f"{label} panel map artifact",
                    snapshot_sha256=actual_panel_hash,
                ),
            }
        )

    canonical_hash = routing_hashes["canonical_manifest_sha256"]
    config_canonical_path = config.get("canonicalManifestPath")
    manifest_canonical_path = manifest.get("canonical_manifest_path")
    if canonical_hash is None:
        _require(
            config_canonical_path is None
            or str(config_canonical_path).strip().lower() in {"", "none", "null"},
            f"{label} canonicalManifestPath lacks canonicalManifestSha256",
        )
        _require(
            manifest_canonical_path is None or manifest_canonical_path == "",
            f"{label} canonical_manifest_path lacks canonicalManifestSha256",
        )
        _require(
            config.get("canonicalManifestMode") in {None, "", "discovery"},
            f"{label} canonicalManifestMode requires canonicalManifestSha256",
        )
    else:
        _require(
            config.get("canonicalManifestMode") == "allowlist"
            and isinstance(config_canonical_path, str)
            and bool(config_canonical_path.strip())
            and manifest_canonical_path == config_canonical_path,
            f"{label} canonical manifest path/mode disagrees with its hash claim",
        )
        canonical_path = analysis_dir / "canonical_field_manifest.csv"
        _require(
            canonical_path.is_file(),
            f"{label} canonical manifest artifact is missing",
        )
        actual_canonical_hash = sha256_file(canonical_path)
        _require(
            actual_canonical_hash == canonical_hash,
            f"{label} canonicalManifestSha256 does not match copied artifact bytes",
        )
        artifacts.append(
            {
                "role": "canonical_manifest",
                "artifact": _artifact(
                    canonical_path,
                    slide_dir,
                    f"{label} canonical manifest artifact",
                    snapshot_sha256=actual_canonical_hash,
                ),
            }
        )
    return artifacts


def _normalized_resolved_config(
    config: Mapping[str, Any], *, slide_dir: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove workstation paths while retaining their content authority.

    Historical manifests use sentinel values for inputs that were not used.
    A real external path is accepted only when the engine also recorded a
    lowercase SHA-256 for the bytes it consumed.
    """

    normalized = dict(config)
    # The raw authority record contains workstation paths for the model,
    # runtime manifest, plugin jars, native library, and class code sources.
    # Per-params validation below rehashes those paths and contributes only the
    # portable content identities to the segmentation profile.
    normalized.pop("stardistAuthority", None)
    external_fields = (
        (
            "marker_registry",
            "markerRegistryPath",
            "markerRegistrySha256",
            "markerRegistryStatus",
            {"", "unavailable", "none", "null"},
            "unavailable",
            {"", "not_used_unavailable"},
        ),
        (
            "panel_config",
            "panelConfigPath",
            "panelConfigSha256",
            "panelConfigStatus",
            {"", "built_in_only", "none", "null"},
            "built_in_only",
            {"", "built_in_only"},
        ),
    )
    artifacts: list[dict[str, Any]] = []
    for (
        role,
        path_key,
        sha_key,
        status_key,
        no_file_values,
        no_file_status,
        accepted_no_file_statuses,
    ) in external_fields:
        raw_path = normalized.pop(path_key, None)
        raw_sha = normalized.pop(sha_key, None)
        raw_status = str(normalized.pop(status_key, "") or "").strip()
        token = "" if raw_path is None else str(raw_path).strip()
        if token.lower() in no_file_values:
            _require(
                raw_sha is None or raw_sha == "",
                f"run manifest config {path_key} declares no external file but has {sha_key}",
            )
            _require(
                raw_status in accepted_no_file_statuses,
                f"run manifest config {status_key} disagrees with {path_key}",
            )
            normalized[path_key] = {"use_status": no_file_status}
            continue
        _require(
            isinstance(raw_sha, str) and bool(SHA256_RE.fullmatch(raw_sha)),
            f"run manifest config {path_key} claims an external file without "
            f"a lowercase {sha_key}",
        )
        _require(
            raw_status in {"", "external_file_bound"},
            f"run manifest config {status_key} disagrees with external {path_key}",
        )
        external_path = Path(token)
        if not external_path.is_absolute():
            external_path = slide_dir / external_path
        try:
            external_path = external_path.resolve(strict=True)
            external_path.relative_to(slide_dir)
        except (OSError, ValueError) as exc:
            raise Stage2IndexError(
                f"run manifest config {path_key} must resolve inside the slide directory"
            ) from exc
        _require(
            external_path.is_file(),
            f"run manifest config {path_key} is not a file",
        )
        actual_sha = sha256_file(external_path)
        _require(
            actual_sha == raw_sha,
            f"run manifest config {sha_key} does not match the referenced bytes",
        )
        normalized[path_key] = {
            "use_status": "external_content_bound",
            "sha256": raw_sha,
        }
        artifacts.append(
            {
                "role": role,
                "artifact": _artifact(
                    external_path,
                    slide_dir,
                    f"Stage 2 {role} configuration",
                    snapshot_sha256=actual_sha,
                ),
            }
        )

    # These hashes identify the per-run assignment/input set. Their source
    # artifacts are already bound elsewhere and must not make sharded
    # measurement profiles diverge merely because each shard has different rows.
    for key in (
        "samplesheetSha256",
        "panelMapSha256",
        "panelMapMode",
        "canonicalManifestSha256",
        "canonicalManifestPath",
        "canonicalManifestMode",
    ):
        normalized.pop(key, None)
    return normalized, artifacts


def _engine_file_safe(value: Any) -> str:
    token = "NA" if value is None or not str(value).strip() else str(value).strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", token)
    cleaned = re.sub(r"^-+|-+$", "", cleaned)
    return cleaned or "NA"


def _require_configuration_snapshot_path(
    raw_path: Any,
    *,
    role: str,
    configuration_artifacts: Mapping[str, Mapping[str, Any]],
    slide_dir: Path,
    label: str,
) -> None:
    record = configuration_artifacts.get(role)
    _require(record is not None, f"{label} lacks the bound {role} artifact")
    expected = _resolve_portable(
        slide_dir,
        record["artifact"]["path"],
        f"{label} bound {role} artifact path",
    ).resolve(strict=True)
    _require(
        isinstance(raw_path, str) and bool(raw_path.strip()),
        f"{label} {role} path must identify the bound snapshot",
    )
    claimed = Path(raw_path.strip())
    if not claimed.is_absolute():
        claimed = slide_dir / claimed
    try:
        claimed = claimed.resolve(strict=True)
        claimed.relative_to(slide_dir)
    except (OSError, ValueError) as exc:
        raise Stage2IndexError(
            f"{label} {role} path must resolve inside the slide directory"
        ) from exc
    _require(
        claimed == expected,
        f"{label} {role} path disagrees with the run manifest snapshot",
    )


def _declared_channel_map(
    params: Mapping[str, Any], signature: str, label: str
) -> list[dict[str, Any]]:
    """Validate and normalize the declared panel map recorded in params JSON."""

    raw_channels = params.get("channel_map")
    _require(
        isinstance(raw_channels, list) and bool(raw_channels),
        f"{label}.channel_map must be a non-empty array",
    )
    signature_matches = [
        CHANNEL_TOKEN_RE.fullmatch(token) for token in signature.split("_")
    ]
    _require(all(signature_matches), f"{label} has an invalid channel signature")
    _require(
        len(raw_channels) == len(signature_matches),
        f"{label}.channel_map count disagrees with channel_signature",
    )
    normalized: list[dict[str, Any]] = []
    for position, (raw, match) in enumerate(
        zip(raw_channels, signature_matches), start=1
    ):
        _require(isinstance(raw, dict), f"{label}.channel_map[{position}] must be an object")
        assert match is not None
        channel_index = _integer(raw, "idx", f"{label}.channel_map[{position}]")
        marker = _nonempty(raw, "marker", f"{label}.channel_map[{position}]")
        role = _nonempty(raw, "role", f"{label}.channel_map[{position}]")
        declared_label = str(raw.get("fileLabel") or "").strip() or marker
        file_label = _engine_file_safe(declared_label)
        _require(
            channel_index == int(match.group(1)) and file_label == match.group(2),
            f"{label}.channel_map[{position}] disagrees with channel_signature",
        )
        normalized.append(
            {
                "channel_index": channel_index,
                "file_label": file_label,
                "marker": marker,
                "role": role,
            }
        )
    return normalized


def _validate_params_configuration(
    params: Mapping[str, Any],
    normalized_config: Mapping[str, Any],
    configuration_artifacts: Mapping[str, Mapping[str, Any]],
    slide_dir: Path,
    routing_input_hashes: Mapping[str, str | None],
    label: str,
) -> None:
    marker_expected = normalized_config["markerRegistryPath"]
    marker_record = params.get("marker_registry")
    if marker_expected["use_status"] == "external_content_bound":
        marker_path = marker_record.get("path") if isinstance(marker_record, dict) else None
        _require(
            isinstance(marker_record, dict)
            and marker_record.get("status") == "external_file_bound"
            and marker_record.get("sha256") == marker_expected["sha256"]
            and isinstance(marker_path, str)
            and marker_path.strip().lower() not in {"", "unavailable", "none", "null"},
            f"{label} marker_registry provenance disagrees with run manifest config",
        )
        _require_configuration_snapshot_path(
            marker_path,
            role="marker_registry",
            configuration_artifacts=configuration_artifacts,
            slide_dir=slide_dir,
            label=label,
        )
    else:
        _require(
            marker_record is None or isinstance(marker_record, dict),
            f"{label} marker_registry provenance must be absent or an object",
        )
        marker_path = marker_record.get("path") if isinstance(marker_record, dict) else None
        marker_status = (
            marker_record.get("status") if isinstance(marker_record, dict) else None
        )
        marker_sha = marker_record.get("sha256") if isinstance(marker_record, dict) else None
        _require(
            (marker_sha is None or marker_sha == "")
            and (
                marker_status is None
                or (
                    isinstance(marker_status, str)
                    and marker_status in {"", "not_used_unavailable"}
                )
            )
            and (
                marker_path is None
                or str(marker_path).strip().lower()
                in {"", "unavailable", "none", "null"}
            ),
            f"{label} marker_registry provenance claims an undeclared external file",
        )

    panel_expected = normalized_config["panelConfigPath"]
    panel_path = params.get("custom_panel_config")
    panel_status = params.get("custom_panel_config_status")
    panel_sha = params.get("custom_panel_config_sha256")
    if panel_expected["use_status"] == "external_content_bound":
        _require(
            panel_status == "external_file_bound"
            and panel_sha == panel_expected["sha256"]
            and isinstance(panel_path, str)
            and panel_path.strip().lower()
            not in {"", "built_in_only", "none", "null"},
            f"{label} custom panel provenance disagrees with run manifest config",
        )
        _require_configuration_snapshot_path(
            panel_path,
            role="panel_config",
            configuration_artifacts=configuration_artifacts,
            slide_dir=slide_dir,
            label=label,
        )
    else:
        _require(
            (panel_sha is None or panel_sha == "")
            and (
                panel_status is None
                or (
                    isinstance(panel_status, str)
                    and panel_status in {"", "built_in_only"}
                )
            )
            and (
                panel_path is None
                or str(panel_path).strip().lower()
                in {"", "built_in_only", "none", "null"}
            ),
            f"{label} custom panel provenance claims an undeclared external file",
        )

    params_routing = params.get("routing_input_hashes")
    _require(
        isinstance(params_routing, dict),
        f"{label}.routing_input_hashes must be an object",
    )
    for field, expected in routing_input_hashes.items():
        actual = _optional_sha256(params_routing, field, f"{label}.routing_input_hashes")
        _require(
            actual == expected,
            f"{label}.routing_input_hashes.{field} disagrees with run manifest config",
        )


def _verify_external_content_path(
    raw_path: Any, identity: Mapping[str, Any], label: str
) -> Path:
    """Rehash an engine-sealed external file without retaining its host path."""

    _require(
        isinstance(raw_path, str) and bool(raw_path) and raw_path == raw_path.strip(),
        f"{label} path must be an absolute non-empty string",
    )
    path = Path(raw_path)
    _require(path.is_absolute(), f"{label} path must be absolute")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise Stage2IndexError(f"{label} path is missing or unreadable") from exc
    _require(path.is_file(), f"{label} path must resolve to a regular file")
    _require(
        path.name == identity["name"]
        and path.stat().st_size == identity["size_bytes"]
        and sha256_file(path) == identity["sha256"],
        f"{label} path no longer matches its portable content identity",
    )
    return path


def _normalize_stardist_provenance(
    *,
    params: Mapping[str, Any],
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
    analysis_dir: Path,
    slide_dir: Path,
    tile_id: str,
    output_key: str,
    expected_width_pixels: int,
    expected_height_pixels: int,
    label: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate segmenter authority and return path-free method/output records."""

    segmenter = _strict_nonempty_text(params, "segmenter", label)
    _require(segmenter in {"classic", "stardist"}, f"{label}.segmenter is unsupported")
    _require(
        config.get("segmenter") == segmenter,
        f"{label}.segmenter disagrees with the run manifest config",
    )
    runtime = params.get("stardist_runtime")
    _require(isinstance(runtime, dict), f"{label}.stardist_runtime must be an object")
    runtime_fields = {
        "active",
        "authority",
        "api_command",
        "model_choice",
        "model_content",
        "model_archive",
        "runtime_manifest_content",
        "runtime_profile_id",
        "runtime_artifacts",
        "class_bindings",
        "label_outputs",
    }
    _require(
        set(runtime) == runtime_fields,
        f"{label}.stardist_runtime has missing or unknown fields",
    )
    config_authority = config.get("stardistAuthority")
    _require(
        isinstance(config_authority, dict),
        f"{label} run manifest config lacks stardistAuthority",
    )
    config_authority_fields = runtime_fields - {"label_outputs"} | {
        "model_path",
        "runtime_manifest_path",
    }
    _require(
        set(config_authority) == config_authority_fields,
        f"{label} run manifest stardistAuthority has missing or unknown fields",
    )
    for key in runtime_fields - {"label_outputs"}:
        _require(
            runtime.get(key) == config_authority.get(key),
            f"{label}.stardist_runtime.{key} disagrees with run manifest config",
        )

    probability = _finite_number(params, "stardist_prob", label)
    nms = _finite_number(params, "stardist_nms", label)
    tiles = _integer(params, "stardist_tiles", label)
    _require(
        0.0 <= probability <= 1.0 and 0.0 <= nms <= 1.0 and tiles >= 1,
        f"{label} has invalid StarDist command parameters",
    )
    _require(
        config.get("prob") == params.get("stardist_prob")
        and config.get("nms") == params.get("stardist_nms")
        and config.get("tiles") == params.get("stardist_tiles"),
        f"{label} StarDist command parameters disagree with run manifest config",
    )

    if segmenter == "classic":
        _require(runtime.get("active") is False, f"{label} classic route cannot activate StarDist")
        _require(
            runtime.get("authority") == "not_applicable_classic"
            and all(
                runtime.get(key) is None
                for key in (
                    "api_command",
                    "model_choice",
                    "model_content",
                    "model_archive",
                    "runtime_manifest_content",
                    "runtime_profile_id",
                )
            )
            and runtime.get("runtime_artifacts") == []
            and runtime.get("class_bindings") == []
            and runtime.get("label_outputs") == [],
            f"{label} classic route contains active StarDist authority claims",
        )
        _require(
            config_authority.get("model_path") is None
            and config_authority.get("runtime_manifest_path") is None,
            f"{label} classic route contains StarDist workstation paths",
        )
        _require(
            params.get("stardist_model_choice") is None
            and params.get("stardist_model_sha256") is None
            and params.get("stardist_model_authority") == "not_applicable_classic"
            and config.get("stardistModelChoice") is None
            and config.get("stardistModelSha256") is None
            and config.get("stardistModelAuthority") == "not_applicable_classic",
            f"{label} classic route has inconsistent StarDist model fields",
        )
        _require(
            manifest.get("stardist_authority_verified_before_and_after") is False,
            f"{label} classic run manifest has an invalid StarDist verification state",
        )
        return {
            "segmenter": "classic",
            "authority": "not_applicable_classic",
        }, []

    _require(runtime.get("active") is True, f"{label} StarDist route is not active")
    _require(
        runtime.get("authority")
        == "explicit_model_and_closed_runtime_manifest_content_bound"
        and runtime.get("api_command") == "de.csbdresden.stardist.StarDist2D"
        and runtime.get("model_choice") == "Model (.zip) from File",
        f"{label} StarDist command authority is unsupported",
    )
    model_content = _content_identity(runtime.get("model_content"), f"{label} model_content")
    runtime_manifest_content = _content_identity(
        runtime.get("runtime_manifest_content"), f"{label} runtime_manifest_content"
    )
    _require(model_content["size_bytes"] > 0, f"{label} StarDist model cannot be empty")
    _require(
        runtime_manifest_content["size_bytes"] > 0,
        f"{label} StarDist runtime manifest cannot be empty",
    )
    _require(
        model_content["name"].lower().endswith(".zip")
        and runtime_manifest_content["name"].lower().endswith(".json"),
        f"{label} StarDist model/runtime-manifest filenames are unsupported",
    )
    _verify_external_content_path(
        config_authority.get("model_path"), model_content, f"{label} StarDist model"
    )
    runtime_manifest_path = _verify_external_content_path(
        config_authority.get("runtime_manifest_path"),
        runtime_manifest_content,
        f"{label} StarDist runtime manifest",
    )
    _require(
        manifest.get("stardist_authority_verified_before_and_after") is True,
        f"{label} run manifest did not verify StarDist authority before and after",
    )
    _require(
        params.get("stardist_model_choice") == runtime["model_choice"]
        and params.get("stardist_model_sha256") == model_content["sha256"]
        and params.get("stardist_model_authority") == runtime["authority"]
        and config.get("stardistModelChoice") == runtime["model_choice"]
        and config.get("stardistModelSha256") == model_content["sha256"]
        and config.get("stardistModelAuthority") == runtime["authority"],
        f"{label} StarDist model identity fields disagree",
    )

    archive = runtime.get("model_archive")
    _require(
        isinstance(archive, dict)
        and set(archive) == {"archive_format", "entry_count", "file_count"}
        and archive.get("archive_format") == "zip",
        f"{label}.stardist_runtime.model_archive is invalid",
    )
    entry_count = _integer(archive, "entry_count", f"{label} model_archive")
    file_count = _integer(archive, "file_count", f"{label} model_archive")
    _require(
        entry_count >= file_count >= 1,
        f"{label}.stardist_runtime.model_archive counts are invalid",
    )
    runtime_profile_id = _strict_nonempty_text(
        runtime, "runtime_profile_id", f"{label}.stardist_runtime"
    )
    _require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", runtime_profile_id)
        is not None,
        f"{label}.stardist_runtime.runtime_profile_id is unsafe",
    )

    raw_artifacts = runtime.get("runtime_artifacts")
    _require(
        isinstance(raw_artifacts, list) and len(raw_artifacts) >= 4,
        f"{label}.stardist_runtime.runtime_artifacts must contain sealed files",
    )
    normalized_artifacts: list[dict[str, Any]] = []
    artifact_paths: dict[str, Path] = {}
    classes_to_role: dict[str, str] = {}
    for index, raw in enumerate(raw_artifacts, start=1):
        artifact_label = f"{label}.stardist_runtime.runtime_artifacts[{index}]"
        _require(isinstance(raw, dict), f"{artifact_label} must be an object")
        _require(
            set(raw) == {"role", "path", "expected_classes", "content"},
            f"{artifact_label} has missing or unknown fields",
        )
        role = _strict_nonempty_text(raw, "role", artifact_label)
        _require(
            re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role) is not None
            and role not in artifact_paths,
            f"{artifact_label}.role must be a unique lowercase token",
        )
        content = _content_identity(raw.get("content"), f"{artifact_label}.content")
        _require(content["size_bytes"] > 0, f"{artifact_label} cannot be empty")
        artifact_path = _verify_external_content_path(
            raw.get("path"), content, artifact_label
        )
        expected_classes = raw.get("expected_classes")
        _require(
            isinstance(expected_classes, list)
            and len(expected_classes) == len(set(expected_classes))
            and all(
                isinstance(class_name, str)
                and class_name == class_name.strip()
                and bool(class_name)
                for class_name in expected_classes
            ),
            f"{artifact_label}.expected_classes must be a unique string array",
        )
        for class_name in expected_classes:
            _require(
                class_name not in classes_to_role,
                f"{label} StarDist class is declared by multiple artifacts: {class_name}",
            )
            classes_to_role[class_name] = role
        artifact_paths[role] = artifact_path
        normalized_artifacts.append(
            {
                "role": role,
                "expected_classes": sorted(expected_classes),
                "content": content,
            }
        )
    required_roles = {
        "stardist_plugin",
        "csbdeep_plugin",
        "tensorflow_java",
        "tensorflow_native",
    }
    _require(
        required_roles.issubset(artifact_paths),
        f"{label} StarDist runtime is missing required artifact roles",
    )
    runtime_manifest_document, runtime_manifest_snapshot_sha256 = _read_json_snapshot(
        runtime_manifest_path, f"{label} StarDist runtime manifest"
    )
    _require(
        runtime_manifest_snapshot_sha256 == runtime_manifest_content["sha256"],
        f"{label} StarDist runtime manifest changed while it was being validated",
    )
    _require(
        set(runtime_manifest_document) == {"schema_version", "profile_id", "artifacts"}
        and runtime_manifest_document.get("schema_version") == "1.0.0"
        and runtime_manifest_document.get("profile_id") == runtime_profile_id,
        f"{label} StarDist runtime manifest root identity disagrees",
    )
    manifest_artifacts = runtime_manifest_document.get("artifacts")
    _require(
        isinstance(manifest_artifacts, list)
        and len(manifest_artifacts) == len(raw_artifacts),
        f"{label} StarDist runtime manifest artifact count disagrees",
    )
    manifest_roles: set[str] = set()
    normalized_by_role = {
        artifact["role"]: artifact for artifact in normalized_artifacts
    }
    for index, declaration in enumerate(manifest_artifacts, start=1):
        declaration_label = f"{label} StarDist runtime manifest artifacts[{index}]"
        _require(isinstance(declaration, dict), f"{declaration_label} must be an object")
        _require(
            set(declaration)
            == {"role", "path", "size_bytes", "sha256", "expected_classes"},
            f"{declaration_label} has missing or unknown fields",
        )
        role = _strict_nonempty_text(declaration, "role", declaration_label)
        _require(
            role in normalized_by_role and role not in manifest_roles,
            f"{declaration_label}.role is unknown or duplicated",
        )
        manifest_roles.add(role)
        declared_path_token = _strict_nonempty_text(
            declaration, "path", declaration_label
        )
        declared_size = declaration.get("size_bytes")
        _require(
            isinstance(declared_size, int)
            and not isinstance(declared_size, bool)
            and declared_size > 0,
            f"{declaration_label}.size_bytes must be a positive integer",
        )
        declared_sha256 = declaration.get("sha256")
        _require(
            isinstance(declared_sha256, str)
            and bool(SHA256_RE.fullmatch(declared_sha256)),
            f"{declaration_label}.sha256 must be a lowercase SHA-256",
        )
        declared_classes = declaration.get("expected_classes")
        _require(
            isinstance(declared_classes, list)
            and len(declared_classes) == len(set(declared_classes))
            and all(
                isinstance(class_name, str)
                and bool(class_name)
                and class_name == class_name.strip()
                and all(character not in class_name for character in "\t\r\n")
                for class_name in declared_classes
            ),
            f"{declaration_label}.expected_classes must be a unique string array",
        )
        declared_path = Path(declared_path_token)
        if not declared_path.is_absolute():
            _portable_reference_path(
                declared_path_token.replace("\\", "/"),
                f"{declaration_label}.path",
            )
            declared_path = runtime_manifest_path.parent / declared_path
        try:
            declared_path = declared_path.resolve(strict=True)
        except OSError as exc:
            raise Stage2IndexError(f"{declaration_label}.path is missing") from exc
        normalized_artifact = normalized_by_role[role]
        _require(
            declared_path == artifact_paths[role]
            and declared_size == normalized_artifact["content"]["size_bytes"]
            and declared_sha256 == normalized_artifact["content"]["sha256"]
            and sorted(declared_classes) == normalized_artifact["expected_classes"],
            f"{declaration_label} disagrees with the sealed runtime artifact",
        )
    _require(
        manifest_roles == set(artifact_paths),
        f"{label} StarDist runtime manifest does not cover every sealed artifact",
    )
    required_classes = {
        "de.csbdresden.stardist.StarDist2D": "stardist_plugin",
        "de.csbdresden.stardist.StarDist2DNMS": "stardist_plugin",
        "de.csbdresden.csbdeep.commands.GenericNetwork": "csbdeep_plugin",
        "org.tensorflow.Graph": "tensorflow_java",
    }
    _require(
        all(classes_to_role.get(name) == role for name, role in required_classes.items()),
        f"{label} StarDist runtime lacks required class-to-artifact declarations",
    )

    raw_bindings = runtime.get("class_bindings")
    _require(isinstance(raw_bindings, list), f"{label}.class_bindings must be an array")
    normalized_bindings: list[dict[str, str]] = []
    seen_binding_classes: set[str] = set()
    for index, raw in enumerate(raw_bindings, start=1):
        binding_label = f"{label}.stardist_runtime.class_bindings[{index}]"
        _require(isinstance(raw, dict), f"{binding_label} must be an object")
        _require(
            set(raw) == {"class_name", "code_source_path"},
            f"{binding_label} has missing or unknown fields",
        )
        class_name = _strict_nonempty_text(raw, "class_name", binding_label)
        _require(
            class_name in classes_to_role and class_name not in seen_binding_classes,
            f"{binding_label}.class_name is undeclared or duplicated",
        )
        role = classes_to_role[class_name]
        bound_path = Path(_strict_nonempty_text(raw, "code_source_path", binding_label))
        _require(bound_path.is_absolute(), f"{binding_label}.code_source_path must be absolute")
        try:
            bound_path = bound_path.resolve(strict=True)
        except OSError as exc:
            raise Stage2IndexError(f"{binding_label}.code_source_path is missing") from exc
        _require(
            bound_path == artifact_paths[role],
            f"{binding_label} does not resolve to its sealed runtime artifact",
        )
        seen_binding_classes.add(class_name)
        normalized_bindings.append({"class_name": class_name, "artifact_role": role})
    _require(
        seen_binding_classes == set(classes_to_role),
        f"{label} StarDist class bindings do not cover every declared class",
    )

    raw_outputs = runtime.get("label_outputs")
    _require(isinstance(raw_outputs, list), f"{label}.label_outputs must be an array")
    normalized_outputs: list[dict[str, Any]] = []
    seen_regions: set[str] = set()
    output_fields = {
        "command_output",
        "output_type",
        "pixel_encoding",
        "canonical_hash_encoding",
        "width_pixels",
        "height_pixels",
        "label_count",
        "canonical_pixel_sha256",
        "region",
        "output_relative_path",
        "output_content",
        "output_content_verified_at_publication",
        "output_content_verified_before_params",
    }
    for index, raw in enumerate(raw_outputs, start=1):
        output_label = f"{label}.stardist_runtime.label_outputs[{index}]"
        _require(isinstance(raw, dict), f"{output_label} must be an object")
        _require(set(raw) == output_fields, f"{output_label} has missing or unknown fields")
        region = _strict_nonempty_text(raw, "region", output_label)
        _require(region not in seen_regions, f"{output_label} repeats region {region!r}")
        seen_regions.add(region)
        _require(
            raw.get("command_output") == "label"
            and raw.get("output_type") == "Label Image"
            and raw.get("pixel_encoding") == "unsigned_16_bit_labels"
            and raw.get("canonical_hash_encoding")
            == "ifq_stardist_label_u16le_row_major_v1"
            and raw.get("output_content_verified_at_publication") is True
            and raw.get("output_content_verified_before_params") is True,
            f"{output_label} has unsupported or unverified label semantics",
        )
        width = _integer(raw, "width_pixels", output_label)
        height = _integer(raw, "height_pixels", output_label)
        label_count = _integer(raw, "label_count", output_label)
        _require(
            width > 0 and height > 0 and label_count >= 0,
            f"{output_label} dimensions/count are invalid",
        )
        _require(
            width == expected_width_pixels and height == expected_height_pixels,
            f"{output_label} dimensions disagree with the Stage 1 export window",
        )
        canonical_pixel_sha256 = raw.get("canonical_pixel_sha256")
        _require(
            isinstance(canonical_pixel_sha256, str)
            and bool(SHA256_RE.fullmatch(canonical_pixel_sha256)),
            f"{output_label}.canonical_pixel_sha256 must be lowercase SHA-256",
        )
        output_token = _portable_reference_path(
            raw.get("output_relative_path"), f"{output_label}.output_relative_path"
        )
        output_parts = PurePosixPath(output_token).parts
        _require(
            len(output_parts) == 2 and output_parts[0] == output_key,
            f"{output_label}.output_relative_path disagrees with output_key",
        )
        output_path = _resolve_portable(
            analysis_dir, output_token, f"{output_label}.output_relative_path"
        )
        _require(
            output_path.suffix.lower() in {".tif", ".tiff"},
            f"{output_label} artifact must be a TIFF label image",
        )
        output_content = _content_identity(
            raw.get("output_content"), f"{output_label}.output_content"
        )
        _require(output_content["size_bytes"] > 0, f"{output_label} artifact is empty")
        _require(
            output_path.is_file()
            and output_path.name == output_content["name"]
            and output_path.stat().st_size == output_content["size_bytes"]
            and sha256_file(output_path) == output_content["sha256"],
            f"{output_label} artifact no longer matches its content identity",
        )
        normalized_outputs.append(
            {
                "tile_id": tile_id,
                "output_key": output_key,
                "region": region,
                "command_output": "label",
                "output_type": "Label Image",
                "pixel_encoding": "unsigned_16_bit_labels",
                "canonical_hash_encoding": "ifq_stardist_label_u16le_row_major_v1",
                "width_pixels": width,
                "height_pixels": height,
                "label_count": label_count,
                "canonical_pixel_sha256": canonical_pixel_sha256,
                "artifact": {
                    "path": _portable_relative(
                        output_path, slide_dir, f"{output_label} artifact"
                    ),
                    "content": output_content,
                },
            }
        )

    profile = {
        "segmenter": "stardist",
        "authority": runtime["authority"],
        "api_command": runtime["api_command"],
        "model_choice": runtime["model_choice"],
        "model_content": model_content,
        "model_archive": {
            "archive_format": "zip",
            "entry_count": entry_count,
            "file_count": file_count,
        },
        "runtime_manifest_content": runtime_manifest_content,
        "runtime_profile_id": runtime_profile_id,
        "runtime_artifacts": sorted(
            normalized_artifacts, key=lambda item: item["role"]
        ),
        "class_bindings": sorted(
            normalized_bindings, key=lambda item: item["class_name"]
        ),
        "probability_threshold": probability,
        "nms_threshold": nms,
        "tiles": tiles,
    }
    return profile, normalized_outputs


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
    stage2_script_content = {
        "name": stage2_script.name,
        "size_bytes": stage2_script.stat().st_size,
        "sha256": sha256_file(stage2_script),
    }
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
    stage1_slide = stage1_matches[0]
    _require(
        stage1_slide.get("coverage_complete") is True,
        "Stage 1 slide coverage_complete must be true",
    )
    _require(
        stage1_slide.get("dry_run") is False,
        "Stage 1 slide dry_run must be false",
    )
    _require(
        _integer(stage1_slide, "max_tiles_cap", "Stage 1 slide") == 0,
        "Stage 1 slide max_tiles_cap must be zero for an integrity-complete index",
    )
    skipped_low_tissue = stage1_slide.get("n_skipped_low_tissue")
    _require(
        isinstance(skipped_low_tissue, int)
        and not isinstance(skipped_low_tissue, bool)
        and skipped_low_tissue == 0,
        "Stage 1 slide n_skipped_low_tissue must be zero",
    )
    stage1_script_record = _stage1_script(stage1)
    stage1_profile_sha256 = canonical_sha256(_stage1_profile(stage1))
    source_metadata = _stage1_source_metadata(
        stage1_slide,
        stage1,
        slide_dir=slide_dir,
        stage1_root=stage1_manifest.parent,
    )
    source_metadata_sha256 = canonical_sha256(source_metadata)

    tile_header, tile_rows, tile_manifest_snapshot_sha256 = _read_csv_snapshot(
        tile_manifest, "tile manifest"
    )
    _require_columns(
        tile_header,
        (
            "tile_id",
            "tile_file",
            "roiset_file",
            "section_id",
            "panel",
            "source_vsi",
            "series_index",
            "pixel_size_um",
            "pixel_size_um_y",
            "core_x",
            "core_y",
            "core_w",
            "core_h",
            "export_x",
            "export_y",
            "export_w",
            "export_h",
            "halo_left",
            "halo_top",
            "halo_right",
            "halo_bottom",
            "core_tissue_area_px",
            "core_tissue_area_um2",
        ),
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

    candidate_manifest_name = _plain_filename(
        _nonempty(stage1_slide, "tile_candidate_manifest", "Stage 1 slide"),
        "Stage 1 tile_candidate_manifest",
    )
    candidate_manifest = slide_dir / candidate_manifest_name
    (
        candidate_header,
        candidate_rows,
        candidate_manifest_snapshot_sha256,
    ) = _read_csv_snapshot(candidate_manifest, "tile candidate manifest")
    _require_columns(
        candidate_header,
        (
            "tile_id",
            "core_x",
            "core_y",
            "core_w",
            "core_h",
            "core_tissue_area_px",
            "core_tissue_area_um2",
            "status",
            "reason",
        ),
        "tile candidate manifest",
    )
    candidate_ids = [
        _nonempty(row, "tile_id", "tile candidate manifest")
        for row in candidate_rows
    ]
    _check_unique(candidate_ids, "tile_id in tile candidate manifest")
    candidate_statuses = [
        _nonempty(row, "status", "tile candidate manifest")
        for row in candidate_rows
    ]
    allowed_candidate_statuses = {
        "outside_tissue",
        "below_minimum_tissue",
        "empty_raster",
        "dry_run",
        "exported",
        "resumed",
    }
    _require(
        set(candidate_statuses).issubset(allowed_candidate_statuses),
        "tile candidate manifest contains an unknown status",
    )

    tiling_profile = stage1.get("tiling")
    _require(isinstance(tiling_profile, dict), "Stage 1 tiling profile must be an object")
    core_px = _integer(tiling_profile, "core_px", "Stage 1 tiling profile")
    _require(core_px > 0, "Stage 1 tiling core_px must be positive")
    halo_px = _integer(tiling_profile, "halo_px", "Stage 1 tiling profile")
    _require(halo_px >= 0, "Stage 1 tiling halo_px must be non-negative")
    source_width = source_metadata["width"]
    source_height = source_metadata["height"]
    expected_grid = {
        f"x{core_x:06d}_y{core_y:06d}": (
            core_x,
            core_y,
            min(core_px, source_width - core_x),
            min(core_px, source_height - core_y),
        )
        for core_y in range(0, source_height, core_px)
        for core_x in range(0, source_width, core_px)
    }
    _require(
        set(candidate_ids) == set(expected_grid),
        "tile candidate manifest does not exactly enumerate the declared Stage 1 grid",
    )

    def csv_integer(row: Mapping[str, str], key: str, label: str) -> int:
        token = _nonempty(row, key, label)
        _require(
            re.fullmatch(r"-?(?:0|[1-9][0-9]*)", token) is not None,
            f"{label}.{key} must be an integer",
        )
        return int(token)

    def csv_number(row: Mapping[str, str], key: str, label: str) -> float:
        token = _nonempty(row, key, label)
        try:
            value = float(token)
        except ValueError as exc:
            raise Stage2IndexError(f"{label}.{key} must be numeric") from exc
        _require(math.isfinite(value), f"{label}.{key} must be finite")
        return value

    candidate_by_id = {
        tile_id: row for tile_id, row in zip(candidate_ids, candidate_rows)
    }
    pixel_area_um2 = (
        source_metadata["pixel_width_um"] * source_metadata["pixel_height_um"]
    )
    candidate_area_um2: dict[str, float] = {}
    for tile_id in sorted(candidate_by_id):
        row = candidate_by_id[tile_id]
        label = f"tile candidate {tile_id}"
        coordinates = tuple(
            csv_integer(row, key, label)
            for key in ("core_x", "core_y", "core_w", "core_h")
        )
        _require(
            coordinates == expected_grid[tile_id],
            f"{label} coordinates do not match the exhaustive Stage 1 grid",
        )
        area_px = csv_number(row, "core_tissue_area_px", label)
        area_um2 = csv_number(row, "core_tissue_area_um2", label)
        _require(
            area_px >= 0.0 and area_um2 >= 0.0,
            f"{label} tissue areas must be non-negative",
        )
        _require(
            math.isclose(
                area_um2,
                area_px * pixel_area_um2,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ),
            f"{label} pixel and calibrated tissue areas do not reconcile",
        )
        status = _nonempty(row, "status", label)
        if status == "outside_tissue":
            _require(
                area_px == 0.0 and area_um2 == 0.0,
                f"{label} outside_tissue row must declare zero tissue area",
            )
        elif status == "exported":
            _require(
                area_px > 0.0 and area_um2 > 0.0,
                f"{label} exported row must declare positive tissue area",
            )
        candidate_area_um2[tile_id] = area_um2

    for key in (
        "n_grid_cores_total",
        "n_grid_cores_visited",
        "n_outside_tissue",
        "n_skipped_empty_raster",
        "n_candidate_exported",
        "n_candidate_resumed",
        "n_candidate_dry_run",
    ):
        _require(_integer(stage1_slide, key, "Stage 1 slide") >= 0, f"{key} must be non-negative")
    _require(
        stage1_slide["n_grid_cores_total"]
        == stage1_slide["n_grid_cores_visited"]
        == len(candidate_rows),
        "Stage 1 grid-core counts do not equal tile candidate manifest rows",
    )
    _require(
        stage1_slide["n_grid_cores_total"] == len(expected_grid),
        "Stage 1 grid-core count does not match the declared dimensions/core size",
    )
    status_count_fields = {
        "outside_tissue": "n_outside_tissue",
        "below_minimum_tissue": "n_skipped_low_tissue",
        "empty_raster": "n_skipped_empty_raster",
        "dry_run": "n_candidate_dry_run",
        "exported": "n_candidate_exported",
        "resumed": "n_candidate_resumed",
    }
    for status_name, count_field in status_count_fields.items():
        _require(
            candidate_statuses.count(status_name) == stage1_slide[count_field],
            f"Stage 1 {count_field} disagrees with tile candidate manifest",
        )
    _require(
        stage1_slide["n_skipped_empty_raster"] == 0
        and stage1_slide["n_candidate_resumed"] == 0
        and stage1_slide["n_candidate_dry_run"] == 0,
        "Stage 1 complete slide cannot contain empty-raster, resumed, or dry-run candidates",
    )
    materialized_candidate_ids = {
        tile_id
        for tile_id, status_name in zip(candidate_ids, candidate_statuses)
        if status_name == "exported"
    }
    _require(
        materialized_candidate_ids == set(tile_ids),
        "materialized tile candidates do not exactly equal tile_manifest.csv",
    )

    tile_by_id = {tile_id: row for tile_id, row in zip(tile_ids, tile_rows)}
    tile_export_dimensions: dict[str, tuple[int, int]] = {}
    tile_tissue_areas_um2: list[float] = []
    for tile_id in sorted(tile_by_id):
        row = tile_by_id[tile_id]
        label = f"tile manifest {tile_id}"
        candidate = candidate_by_id[tile_id]
        for key in ("core_x", "core_y", "core_w", "core_h"):
            _require(
                csv_integer(row, key, label)
                == csv_integer(candidate, key, f"tile candidate {tile_id}"),
                f"{label}.{key} disagrees with the tile candidate ledger",
            )
        core_x, core_y, core_w, core_h = expected_grid[tile_id]
        export_x = max(0, core_x - halo_px)
        export_y = max(0, core_y - halo_px)
        export_x2 = min(source_width, core_x + core_w + halo_px)
        export_y2 = min(source_height, core_y + core_h + halo_px)
        expected_export = (
            export_x,
            export_y,
            export_x2 - export_x,
            export_y2 - export_y,
            core_x - export_x,
            core_y - export_y,
            export_x2 - (core_x + core_w),
            export_y2 - (core_y + core_h),
        )
        actual_export = tuple(
            csv_integer(row, key, label)
            for key in (
                "export_x",
                "export_y",
                "export_w",
                "export_h",
                "halo_left",
                "halo_top",
                "halo_right",
                "halo_bottom",
            )
        )
        _require(
            actual_export == expected_export,
            f"{label} export window/halo does not match the declared Stage 1 grid",
        )
        tile_export_dimensions[tile_id] = (expected_export[2], expected_export[3])
        tile_area_px = csv_number(row, "core_tissue_area_px", label)
        tile_area_um2 = csv_number(row, "core_tissue_area_um2", label)
        _require(
            tile_area_px > 0.0
            and math.isclose(
                tile_area_um2,
                tile_area_px * pixel_area_um2,
                rel_tol=1e-9,
                abs_tol=1e-9,
            )
            and math.isclose(
                tile_area_um2,
                candidate_area_um2[tile_id],
                rel_tol=1e-12,
                abs_tol=1e-9,
            ),
            f"{label} tissue area disagrees with calibration or candidate ledger",
        )
        tile_tissue_areas_um2.append(tile_area_um2)

    tiled_area_um2 = math.fsum(tile_tissue_areas_um2)
    stage1_tissue_area_um2 = (
        _finite_number(
            stage1_slide, "tissue_area_mm2", "Stage 1 slide", positive=True
        )
        * 1_000_000.0
    )
    declared_tile_area_um2 = (
        _finite_number(
            stage1_slide, "sum_core_tissue_mm2", "Stage 1 slide", positive=True
        )
        * 1_000_000.0
    )
    _require(
        math.isclose(
            tiled_area_um2,
            declared_tile_area_um2,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ),
        "Stage 1 sum_core_tissue_mm2 does not equal the tile/candidate area ledger",
    )
    computed_seam_difference = abs(tiled_area_um2 - stage1_tissue_area_um2) / (
        stage1_tissue_area_um2
    )
    declared_seam_difference = _finite_number(
        stage1_slide, "seam_check_rel_diff", "Stage 1 slide"
    )
    _require(
        declared_seam_difference >= 0.0
        and math.isclose(
            declared_seam_difference,
            computed_seam_difference,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ),
        "Stage 1 seam_check_rel_diff does not reconcile with tile/reference areas",
    )
    _require(
        computed_seam_difference <= 1e-6,
        "Stage 1 complete coverage exceeds the allowed tile/reference area difference",
    )
    candidate_set_sha256 = canonical_sha256(
        sorted(zip(candidate_ids, candidate_statuses))
    )

    stage1_n_tiles = stage1_slide.get("n_tiles")
    if stage1_n_tiles is not None:
        _require(
            isinstance(stage1_n_tiles, int) and stage1_n_tiles == len(tile_rows),
            "Stage 1 n_tiles does not equal tile_manifest.csv row count",
        )
    n_written = _integer(stage1_slide, "n_written", "Stage 1 slide")
    n_resumed = _integer(stage1_slide, "n_resumed", "Stage 1 slide")
    _require(
        n_resumed == 0,
        "Stage 1 resumed tiles are not authoritative because resume is not content-bound",
    )
    _require(
        n_written == len(tile_rows),
        "Stage 1 n_written does not equal tile manifest row count",
    )

    tile_by_file = {tile_file: row for tile_file, row in zip(tile_files, tile_rows)}
    tile_by_section = {
        section_id: row for section_id, row in zip(section_ids, tile_rows)
    }
    tile_inputs: list[dict[str, Any]] = []
    for tile_id in sorted(tile_by_id):
        row = tile_by_id[tile_id]
        _require(
            _nonempty(row, "source_vsi", "tile manifest")
            == source_metadata["source_vsi"],
            f"tile {tile_id} source_vsi disagrees with Stage 1 source metadata",
        )
        try:
            tile_series_index = int(_nonempty(row, "series_index", "tile manifest"))
            tile_pixel_width = float(_nonempty(row, "pixel_size_um", "tile manifest"))
            tile_pixel_height = float(
                _nonempty(row, "pixel_size_um_y", "tile manifest")
            )
        except ValueError as exc:
            raise Stage2IndexError(
                f"tile {tile_id} has invalid source series/calibration metadata"
            ) from exc
        _require(
            tile_series_index == source_metadata["series_index"],
            f"tile {tile_id} series_index disagrees with Stage 1 source metadata",
        )
        _require(
            math.isfinite(tile_pixel_width)
            and math.isfinite(tile_pixel_height)
            and math.isclose(
                tile_pixel_width,
                source_metadata["pixel_width_um"],
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
            and math.isclose(
                tile_pixel_height,
                source_metadata["pixel_height_um"],
                rel_tol=1e-9,
                abs_tol=1e-12,
            ),
            f"tile {tile_id} pixel calibration disagrees with Stage 1 source metadata",
        )
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
    tile_input_by_id = {record["tile_id"]: record["tile"] for record in tile_inputs}

    run_records: list[dict[str, Any]] = []
    all_assigned_ids: list[str] = []
    all_success_files: list[str] = []
    all_identities: list[tuple[str, str, str]] = []
    all_output_keys: dict[str, str] = {}
    header_authority: list[str] | None = None
    config_hashes: set[str] = set()
    configuration_artifacts_by_role: dict[str, dict[str, Any]] = {}
    signatures_by_panel: dict[str, str] = {}
    channel_maps_by_panel: dict[str, list[dict[str, Any]]] = {}
    summary_rows_by_panel: dict[str, list[Mapping[str, str]]] = {}
    parameter_artifacts: list[dict[str, Any]] = []
    runtime_profiles: dict[str, dict[str, str]] = {}
    segmentation_profiles: dict[str, dict[str, Any]] = {}
    segmentation_profile_by_output: dict[str, dict[str, Any]] = {}
    stardist_label_outputs_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    observed_stardist_summary_identities: set[tuple[str, str]] = set()

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
        _require(
            manifest.get("engine_script_verified_before_and_after") is True,
            f"run manifest did not verify the engine script before and after: {analysis_dir}",
        )
        _require(
            manifest.get("input_content_authority")
            == "sha256_streamed_bytes_verified_before_and_after_analysis",
            f"run manifest input content authority is missing or unsupported: {analysis_dir}",
        )
        manifest_engine_script = _content_identity(
            manifest.get("engine_script"), f"run manifest engine_script {analysis_rel}"
        )
        _require(
            manifest_engine_script == stage2_script_content,
            f"run manifest engine_script disagrees with the declared Stage 2 script: {analysis_dir}",
        )
        runtime_profile = _runtime_profile(manifest, f"run manifest {analysis_rel}")
        runtime_profile_sha256 = canonical_sha256(runtime_profile)
        runtime_profiles[runtime_profile_sha256] = runtime_profile
        config = manifest.get("config")
        _require(
            isinstance(config, dict),
            f"run manifest config must be an object: {analysis_dir}",
        )
        routing_hashes = _routing_input_hashes(
            config,
            samplesheet_sha256=samplesheet_snapshot_sha256,
            label=f"run manifest config {analysis_rel}",
        )
        run_routing_artifacts = _routing_artifacts(
            manifest,
            config,
            routing_hashes,
            analysis_dir=analysis_dir,
            slide_dir=slide_dir,
            label=f"run manifest {analysis_rel}",
        )
        normalized_config, run_configuration_artifacts = _normalized_resolved_config(
            config, slide_dir=slide_dir
        )
        run_configuration_by_role = {
            record["role"]: record for record in run_configuration_artifacts
        }
        config_sha256 = canonical_sha256(normalized_config)
        config_hashes.add(config_sha256)
        for configuration_record in run_configuration_artifacts:
            role = configuration_record["role"]
            prior_configuration = configuration_artifacts_by_role.get(role)
            _require(
                prior_configuration is None
                or prior_configuration == configuration_record,
                f"Stage 2 {role} configuration artifact differs across runs",
            )
            configuration_artifacts_by_role[role] = configuration_record
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
            output_key = _plain_filename(
                _nonempty(image, "output_key", "run manifest image"),
                "run manifest image output_key",
            )
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
            signature_matches = [
                CHANNEL_TOKEN_RE.fullmatch(token) for token in signature.split("_")
            ]
            signature_indices = [
                int(match.group(1))
                for match in signature_matches
                if match is not None
            ]
            _require(
                max(signature_indices) <= source_metadata["n_channels"],
                f"declared channel signature for {file_name} exceeds the Stage 1 "
                "source channel count",
            )

            params_relative_path = _nonempty(
                image, "params_relative_path", "run manifest image"
            )
            expected_params_relative_path = (
                PurePosixPath(output_key) / f"{signature}__params.json"
            ).as_posix()
            _require(
                params_relative_path == expected_params_relative_path,
                f"run manifest params_relative_path disagrees with image/output identity "
                f"for {file_name}",
            )
            params_path = _resolve_portable(
                analysis_dir,
                params_relative_path,
                f"run manifest image {file_name} params_relative_path",
            )
            params, params_snapshot_sha256 = _read_json_snapshot(
                params_path, f"Stage 2 params for {file_name}"
            )
            params_engine_script = _content_identity(
                params.get("engine_script"),
                f"Stage 2 params engine_script for {file_name}",
            )
            _require(
                params_engine_script == stage2_script_content,
                f"Stage 2 params engine_script disagrees with the declared script for {file_name}",
            )
            _validate_params_configuration(
                params,
                normalized_config,
                run_configuration_by_role,
                slide_dir,
                routing_hashes,
                f"Stage 2 params for {file_name}",
            )
            for field, expected_value in (
                ("image", file_name),
                ("output_key", output_key),
                ("panel", panel),
                ("channel_signature", signature),
            ):
                _require(
                    _nonempty(params, field, f"Stage 2 params for {file_name}")
                    == expected_value,
                    f"Stage 2 params {field} disagrees with run manifest for {file_name}",
                )
            calibration = params.get("calibration")
            _require(
                isinstance(calibration, dict),
                f"Stage 2 params calibration must be an object for {file_name}",
            )
            params_n_channels = _integer(
                calibration, "n_channels", f"Stage 2 params calibration for {file_name}"
            )
            declared_map = _declared_channel_map(
                params, signature, f"Stage 2 params for {file_name}"
            )
            _require(
                params_n_channels == source_metadata["n_channels"],
                f"Stage 2 params acquired channel count disagrees with Stage 1 source "
                f"metadata for {file_name}",
            )
            _require(
                all(
                    channel["channel_index"] <= params_n_channels
                    for channel in declared_map
                ),
                f"Stage 2 params channel_map index exceeds acquired channel count for "
                f"{file_name}",
            )
            params_pixel_width = _finite_number(
                calibration,
                "pixel_width_um",
                f"Stage 2 params calibration for {file_name}",
                positive=True,
            )
            params_pixel_height = _finite_number(
                calibration,
                "pixel_height_um",
                f"Stage 2 params calibration for {file_name}",
                positive=True,
            )
            tile_pixel_width = float(
                _nonempty(tile_row, "pixel_size_um", "tile manifest")
            )
            tile_pixel_height = float(
                _nonempty(tile_row, "pixel_size_um_y", "tile manifest")
            )
            _require(
                math.isclose(
                    params_pixel_width,
                    tile_pixel_width,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                )
                and math.isclose(
                    params_pixel_height,
                    tile_pixel_height,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                ),
                f"Stage 2 params pixel calibration disagrees with tile manifest for {file_name}",
            )
            prior_map = channel_maps_by_panel.get(panel)
            _require(
                prior_map is None or prior_map == declared_map,
                f"declared channel map differs within panel {panel}",
            )
            channel_maps_by_panel[panel] = declared_map
            tile_id = _nonempty(tile_row, "tile_id", "tile manifest")
            expected_tile_artifact = tile_input_by_id[tile_id]
            expected_source_content = {
                "name": file_name,
                "size_bytes": (slide_dir / expected_tile_artifact["path"]).stat().st_size,
                "sha256": expected_tile_artifact["sha256"],
            }
            image_source_content = _content_identity(
                image.get("source_content"),
                f"run manifest source_content for {file_name}",
            )
            _require(
                image.get("source_content_verified_before_and_after") is True,
                f"run manifest did not verify source content before and after for {file_name}",
            )
            params_source_content = _content_identity(
                params.get("source_content"),
                f"Stage 2 params source_content for {file_name}",
            )
            _require(
                image_source_content == params_source_content == expected_source_content,
                f"Stage 2 source content identity disagrees with the indexed tile for {file_name}",
            )
            segmentation_profile, label_outputs = _normalize_stardist_provenance(
                params=params,
                config=config,
                manifest=manifest,
                analysis_dir=analysis_dir,
                slide_dir=slide_dir,
                tile_id=tile_id,
                output_key=output_key,
                expected_width_pixels=tile_export_dimensions[tile_id][0],
                expected_height_pixels=tile_export_dimensions[tile_id][1],
                label=f"Stage 2 params for {file_name}",
            )
            segmentation_profile_sha256 = canonical_sha256(segmentation_profile)
            segmentation_profiles[segmentation_profile_sha256] = segmentation_profile
            segmentation_profile_by_output[output_key] = segmentation_profile
            for label_output in label_outputs:
                label_identity = (
                    label_output["output_key"],
                    label_output["region"],
                )
                _require(
                    label_identity not in stardist_label_outputs_by_identity,
                    "StarDist label output repeats an output_key/region identity",
                )
                stardist_label_outputs_by_identity[label_identity] = label_output
            parameter_artifacts.append(
                {
                    "tile_id": tile_id,
                    "output_key": output_key,
                    "panel": panel,
                    "segmentation_profile_sha256": segmentation_profile_sha256,
                    "params": _artifact(
                        params_path,
                        slide_dir,
                        f"Stage 2 params for {file_name}",
                        snapshot_sha256=params_snapshot_sha256,
                    ),
                }
            )
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
            segmentation_profile = segmentation_profile_by_output.get(output_key)
            _require(
                segmentation_profile is not None,
                f"run summary lacks a segmenter profile: {output_key}",
            )
            label_identity = (output_key, region)
            label_output = stardist_label_outputs_by_identity.get(label_identity)
            raw_label_count = str(row.get("stardist_label_count") or "").strip()
            raw_label_sha256 = str(
                row.get("stardist_label_canonical_pixel_sha256") or ""
            ).strip()
            if segmentation_profile["segmenter"] == "stardist":
                _require(
                    label_output is not None,
                    f"run summary lacks StarDist label evidence for {output_key}/{region}",
                )
                _require(
                    re.fullmatch(r"(?:0|[1-9][0-9]*)", raw_label_count) is not None
                    and int(raw_label_count) == label_output["label_count"]
                    and raw_label_sha256 == label_output["canonical_pixel_sha256"],
                    f"run summary StarDist label evidence disagrees for {output_key}/{region}",
                )
                observed_stardist_summary_identities.add(label_identity)
            else:
                _require(
                    label_output is None
                    and raw_label_count == ""
                    and raw_label_sha256 == "",
                    f"classic run contains StarDist label evidence for {output_key}/{region}",
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
                "routing_input_hashes": routing_hashes,
                "routing_artifacts": run_routing_artifacts,
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
    _require(
        len(runtime_profiles) == 1,
        "Stage 2 normalized runtime profiles differ across runs",
    )
    _require(
        len(segmentation_profiles) == 1,
        "Stage 2 segmentation/model/runtime profiles differ across runs",
    )
    _require(
        set(stardist_label_outputs_by_identity)
        == observed_stardist_summary_identities,
        "StarDist label outputs do not exactly cover the StarDist summary identities",
    )
    _require(
        set(channel_maps_by_panel) == set(signatures_by_panel),
        "Stage 2 declared channel maps do not cover every declared panel",
    )
    _check_unique(
        [record["tile_id"] for record in parameter_artifacts],
        "tile_id in Stage 2 parameter artifacts",
    )
    _require(
        {record["tile_id"] for record in parameter_artifacts} == expected_id_set,
        "Stage 2 parameter artifacts do not exactly cover tile_manifest.csv",
    )

    # A union run-summary schema can contain blank fields for markers absent
    # from a particular panel.  Blankness alone is not authority, however: a
    # marker declared in that panel's ordered signature cannot have an emitted
    # additive column with missing values.  Enforce this before publishing an
    # index whose status claims Stage 2 integrity is complete.  The signature
    # does not itself imply that every acquisition channel has an additive
    # measurement role, so no column is invented or required by name here.
    for panel, signature in sorted(signatures_by_panel.items()):
        declared_markers = {
            canonical_marker_id(channel["marker"])
            for channel in channel_maps_by_panel[panel]
            if channel["role"].lower() != "nuclear"
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
    declared_channel_maps = [
        {"panel": panel, "channels": channel_maps_by_panel[panel]}
        for panel in sorted(channel_maps_by_panel)
    ]
    declared_channel_map_sha256 = canonical_sha256(declared_channel_maps)
    configuration_artifacts = [
        configuration_artifacts_by_role[role]
        for role in sorted(configuration_artifacts_by_role)
    ]
    configuration_artifact_set_sha256 = canonical_sha256(
        [
            (record["role"], record["artifact"]["sha256"])
            for record in configuration_artifacts
        ]
    )
    parameter_artifacts = sorted(
        parameter_artifacts, key=lambda record: (record["tile_id"], record["output_key"])
    )
    parameter_set_sha256 = canonical_sha256(
        [
            (record["tile_id"], record["output_key"], record["params"]["sha256"])
            for record in parameter_artifacts
        ]
    )
    runtime_profile_sha256, runtime_profile = next(iter(runtime_profiles.items()))
    segmentation_profile_sha256, segmentation_profile = next(
        iter(segmentation_profiles.items())
    )
    stardist_label_outputs = sorted(
        stardist_label_outputs_by_identity.values(),
        key=lambda record: (record["tile_id"], record["output_key"], record["region"]),
    )
    stardist_label_output_set_sha256 = canonical_sha256(
        [
            (
                record["tile_id"],
                record["output_key"],
                record["region"],
                record["canonical_pixel_sha256"],
                record["artifact"]["content"]["sha256"],
            )
            for record in stardist_label_outputs
        ]
    )
    _require(
        stage2_script.stat().st_size == stage2_script_content["size_bytes"]
        and sha256_file(stage2_script) == stage2_script_content["sha256"],
        "Stage 2 script changed while the index was being built",
    )
    stage2_script_record = {
        "name": stage2_script_content["name"],
        "sha256": stage2_script_content["sha256"],
    }
    measurement_profile_sha256 = canonical_sha256(
        {
            "stage2_script_sha256": stage2_script_record["sha256"],
            "stage1_script_sha256": stage1_script_record["sha256"],
            "resolved_config_sha256": resolved_config_sha256,
            "stage1_profile_sha256": stage1_profile_sha256,
            "runtime_profile_sha256": runtime_profile_sha256,
            "segmentation_profile_sha256": segmentation_profile_sha256,
            "declared_channel_map_sha256": declared_channel_map_sha256,
            "configuration_artifact_set_sha256": configuration_artifact_set_sha256,
            "declared_channel_signatures": declared_signatures,
            "channel_mapping_authority": (
                "declared_panel_mapping_not_source_verified"
            ),
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
        "stage1_script": stage1_script_record,
        "stage1_profile_sha256": stage1_profile_sha256,
        "stage1_source_metadata": source_metadata,
        "stage1_source_metadata_sha256": source_metadata_sha256,
        "tile_candidate_manifest": {
            **_csv_artifact(
                candidate_manifest,
                slide_dir,
                len(candidate_rows),
                "tile candidate manifest",
                snapshot_sha256=candidate_manifest_snapshot_sha256,
            ),
            "candidate_set_sha256": candidate_set_sha256,
        },
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
        "configuration_artifacts": configuration_artifacts,
        "configuration_artifact_set_sha256": configuration_artifact_set_sha256,
        "runtime_profile": runtime_profile,
        "runtime_profile_sha256": runtime_profile_sha256,
        "segmentation_profile": segmentation_profile,
        "segmentation_profile_sha256": segmentation_profile_sha256,
        "stardist_label_outputs": stardist_label_outputs,
        "stardist_label_output_set_sha256": stardist_label_output_set_sha256,
        "parameter_artifacts": parameter_artifacts,
        "parameter_set_sha256": parameter_set_sha256,
        "measurement_profile_sha256": measurement_profile_sha256,
        "analytical_identity": ANALYTICAL_IDENTITY,
        "channel_mapping_authority": "declared_panel_mapping_not_source_verified",
        "declared_channel_signatures": declared_signatures,
        "declared_channel_maps": declared_channel_maps,
        "declared_channel_map_sha256": declared_channel_map_sha256,
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
        "stage1_script",
        "stage1_profile_sha256",
        "stage1_source_metadata",
        "stage1_source_metadata_sha256",
        "tile_candidate_manifest",
        "tile_manifest",
        "stage2_script",
        "resolved_config_sha256",
        "configuration_artifacts",
        "configuration_artifact_set_sha256",
        "runtime_profile",
        "runtime_profile_sha256",
        "segmentation_profile",
        "segmentation_profile_sha256",
        "stardist_label_outputs",
        "stardist_label_output_set_sha256",
        "parameter_artifacts",
        "parameter_set_sha256",
        "measurement_profile_sha256",
        "analytical_identity",
        "channel_mapping_authority",
        "declared_channel_signatures",
        "declared_channel_maps",
        "declared_channel_map_sha256",
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
    _require(
        document["channel_mapping_authority"]
        == "declared_panel_mapping_not_source_verified",
        "wrong channel mapping authority",
    )
    _validate_datetime(document["generated_utc"], "Stage 2 index generated_utc")
    stage1_script = document["stage1_script"]
    _require(isinstance(stage1_script, dict), "stage1_script must be an object")
    _plain_filename(
        _nonempty(stage1_script, "name", "stage1_script"),
        "stage1_script.name",
    )
    _require(
        _integer(stage1_script, "size_bytes", "stage1_script") > 0,
        "stage1_script.size_bytes must be positive",
    )
    _require(
        SHA256_RE.fullmatch(_nonempty(stage1_script, "sha256", "stage1_script"))
        is not None,
        "stage1_script.sha256 must be a lowercase SHA-256",
    )
    for field in (
        "stage1_manifest_sha256",
        "stage1_profile_sha256",
        "stage1_source_metadata_sha256",
        "resolved_config_sha256",
        "configuration_artifact_set_sha256",
        "runtime_profile_sha256",
        "segmentation_profile_sha256",
        "stardist_label_output_set_sha256",
        "parameter_set_sha256",
        "measurement_profile_sha256",
        "declared_channel_map_sha256",
        "index_sha256",
    ):
        _require(
            isinstance(document[field], str) and bool(SHA256_RE.fullmatch(document[field])),
            f"Stage 2 index {field} must be lowercase SHA-256",
        )
    _require(isinstance(document.get("runs"), list) and document["runs"], "Stage 2 index runs must be non-empty")
    _require(
        isinstance(document.get("parameter_artifacts"), list)
        and bool(document["parameter_artifacts"]),
        "Stage 2 index parameter_artifacts must be non-empty",
    )
    _require(
        isinstance(document.get("declared_channel_maps"), list)
        and bool(document["declared_channel_maps"]),
        "Stage 2 index declared_channel_maps must be non-empty",
    )
    for index, run in enumerate(document["runs"]):
        _require(isinstance(run, dict), f"Stage 2 index runs[{index}] must be an object")
        for field in (
            "analysis_dir",
            "samplesheet",
            "run_summary",
            "routing_input_hashes",
            "routing_artifacts",
        ):
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

    output_path = Path(os.path.abspath(os.fspath(output_path)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
