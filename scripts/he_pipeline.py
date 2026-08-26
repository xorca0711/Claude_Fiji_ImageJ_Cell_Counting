#!/usr/bin/env python3
"""Fail-closed status and review-package tooling for the G-SURF H&E pipeline.

This module does not make lesion calls. It reconciles the approved R1 image-QC
gate, inventories the exploratory H4 context, and builds a blinded whole-section
pathology review package for H5-H7 development.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STUDY = REPO_ROOT / "config" / "studies" / "g_surf_he_20260812.json"
DEFAULT_RUBRIC = REPO_ROOT / "config" / "brightfield" / "he_pathology_review_rubric.json"
DEFAULT_REPO_PROFILE = (
    REPO_ROOT
    / "config"
    / "brightfield"
    / "he_stain_profiles"
    / "g_surf_he_20260812_reviewed_locked_v1.json"
)
DEFAULT_REVIEW_OUTPUT = Path(
    r"D:\IFQ_Runs\H&E_20260812\14_H5_H7_PATHOLOGY_REVIEW_DEVELOPMENT"
)

LOCKED_REVIEW_FIELDS = (
    "blind_section_id",
    "reviewable_yes_no_uncertain",
    "whole_section_inflammation_extent_0_4_uncertain",
    "alveolar_interstitial_inflammation_0_4_uncertain",
    "peribronchial_inflammation_0_4_uncertain",
    "perivascular_inflammation_0_4_uncertain",
    "consolidation_airspace_loss_0_4_uncertain",
    "airway_epithelial_injury_debris_0_4_uncertain",
    "edema_hemorrhage_necrosis_present_yes_no_uncertain",
    "dominant_pattern",
    "representative_region_ids",
    "technical_limitation_none_minor_major",
    "confidence_low_medium_high",
    "reviewer_id",
    "reviewed_utc",
    "notes",
)
LOCKED_REVIEW_VOCABULARIES = {
    "reviewable_yes_no_uncertain": frozenset({"yes", "no", "uncertain"}),
    "whole_section_inflammation_extent_0_4_uncertain": frozenset(
        {"0", "1", "2", "3", "4", "uncertain"}
    ),
    "alveolar_interstitial_inflammation_0_4_uncertain": frozenset(
        {"0", "1", "2", "3", "4", "uncertain"}
    ),
    "peribronchial_inflammation_0_4_uncertain": frozenset(
        {"0", "1", "2", "3", "4", "uncertain"}
    ),
    "perivascular_inflammation_0_4_uncertain": frozenset(
        {"0", "1", "2", "3", "4", "uncertain"}
    ),
    "consolidation_airspace_loss_0_4_uncertain": frozenset(
        {"0", "1", "2", "3", "4", "uncertain"}
    ),
    "airway_epithelial_injury_debris_0_4_uncertain": frozenset(
        {"0", "1", "2", "3", "4", "uncertain"}
    ),
    "edema_hemorrhage_necrosis_present_yes_no_uncertain": frozenset(
        {"yes", "no", "uncertain"}
    ),
    "dominant_pattern": frozenset(
        {
            "none",
            "alveolar_interstitial",
            "peribronchial",
            "perivascular",
            "consolidative",
            "airway_epithelial",
            "mixed",
            "unresolved",
        }
    ),
    "technical_limitation_none_minor_major": frozenset(
        {"none", "minor", "major"}
    ),
    "confidence_low_medium_high": frozenset({"low", "medium", "high"}),
}
ORDINAL_REVIEW_ENDPOINTS = (
    {
        "source_column": "whole_section_inflammation_extent_0_4_uncertain",
        "endpoint_id": "he.whole_section_inflammation_extent",
        "reference_space_id": "he.usable_whole_section.v1",
        "compartment_label": "whole_section_usable_tissue",
        "scale_id": "g_surf-he-inflammation-extent-0-4-v1",
    },
    {
        "source_column": "alveolar_interstitial_inflammation_0_4_uncertain",
        "endpoint_id": "he.alveolar_interstitial_inflammation",
        "reference_space_id": "he.alveolar_interstitial.v1",
        "compartment_label": "alveolar_interstitial",
        "scale_id": "g_surf-he-morphology-severity-0-4-v1",
    },
    {
        "source_column": "peribronchial_inflammation_0_4_uncertain",
        "endpoint_id": "he.peribronchial_inflammation",
        "reference_space_id": "he.peribronchial.v1",
        "compartment_label": "peribronchial",
        "scale_id": "g_surf-he-morphology-severity-0-4-v1",
    },
    {
        "source_column": "perivascular_inflammation_0_4_uncertain",
        "endpoint_id": "he.perivascular_inflammation",
        "reference_space_id": "he.perivascular.v1",
        "compartment_label": "perivascular",
        "scale_id": "g_surf-he-morphology-severity-0-4-v1",
    },
    {
        "source_column": "consolidation_airspace_loss_0_4_uncertain",
        "endpoint_id": "he.consolidation_airspace_loss",
        "reference_space_id": "he.usable_whole_section.v1",
        "compartment_label": "whole_section_usable_tissue",
        "scale_id": "g_surf-he-morphology-severity-0-4-v1",
    },
    {
        "source_column": "airway_epithelial_injury_debris_0_4_uncertain",
        "endpoint_id": "he.airway_epithelial_injury_debris",
        "reference_space_id": "he.airway_epithelium.v1",
        "compartment_label": "airway_epithelium",
        "scale_id": "g_surf-he-morphology-severity-0-4-v1",
    },
)

SECTION_SCORES_FILENAME = "he_section_pathology_scores.csv"
MOUSE_SUMMARY_FILENAME = "he_mouse_pathology_summary.csv"
TECHNICAL_AGREEMENT_FILENAME = "he_technical_section_agreement.csv"
MEASUREMENT_RECORDS_FILENAME = "he_measurement_records.schema-v2.jsonl"
AGGREGATION_AUDIT_FILENAME = "he_review_aggregation.audit.json"
MEASUREMENT_RECORD_SCHEMA_PATH = (
    REPO_ROOT / "schemas" / "measurement-record.schema.json"
)


class ContractError(RuntimeError):
    """A fail-closed H&E contract violation."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    require(path.is_file(), f"Required JSON is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _literal_absolute_path(value: Any, label: str) -> Path:
    require(
        isinstance(value, str) and bool(value) and value == value.strip(),
        f"{label} must be a non-empty literal path without surrounding whitespace.",
    )
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    require(
        windows_path.is_absolute() or posix_path.is_absolute(),
        f"{label} must be an absolute path: {value!r}",
    )
    parts = windows_path.parts if windows_path.is_absolute() else posix_path.parts
    require(
        not any(part in {".", ".."} for part in parts),
        f"{label} must not contain traversal components: {value!r}",
    )
    return Path(value)


def resolve_package_roots(
    study: dict[str, Any],
    r1_override: Path | None = None,
    h4_override: Path | None = None,
) -> tuple[Path, Path, dict[str, dict[str, str]]]:
    """Select exact package roots without discovery or recency fallbacks."""

    approved = study.get("approved_packages")
    require(isinstance(approved, dict), "Study approved_packages must be an object.")
    selections: dict[str, dict[str, str]] = {}
    resolved: dict[str, Path] = {}
    for label, contract_key, override in (
        ("r1", "r1_root", r1_override),
        ("h4", "h4_development_root", h4_override),
    ):
        contract_label = f"approved_packages.{contract_key}"
        configured = _literal_absolute_path(approved.get(contract_key), contract_label)
        if override is None:
            effective = configured
            authority = "study_contract"
        else:
            effective = _literal_absolute_path(str(override), f"--{label}-root")
            authority = "cli_override"
        resolved[label] = effective
        selections[label] = {
            "contract_field": contract_label,
            "configured_root": str(configured),
            "effective_root": str(effective),
            "selection_authority": authority,
        }
    return resolved["r1"], resolved["h4"], selections


def read_csv(path: Path) -> list[dict[str, str]]:
    require(path.is_file(), f"Required CSV is missing: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(path: Path) -> str:
    payload = read_json(path)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_value_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_lower_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_locked_review_rubric(
    rubric: dict[str, Any], study_id: str
) -> None:
    require(
        rubric.get("schema_version") == "1.0.0"
        and rubric.get("rubric_id")
        == "g_surf_he_pathology_review_development_v1"
        and rubric.get("study_id") == study_id
        and rubric.get("review_unit") == "blinded_whole_section",
        "Review rubric identity does not match the locked whole-section contract.",
    )
    require(
        tuple(rubric.get("section_form_fields", ())) == LOCKED_REVIEW_FIELDS,
        "Review rubric fields differ from the locked review header.",
    )
    require(
        frozenset(rubric.get("dominant_pattern_values", ()))
        == LOCKED_REVIEW_VOCABULARIES["dominant_pattern"],
        "Review rubric dominant-pattern vocabulary has drifted.",
    )
    sequence = rubric.get("review_sequence")
    require(isinstance(sequence, list), "Review rubric sequence is missing.")
    reviewability_steps = [
        step
        for step in sequence
        if isinstance(step, dict) and step.get("name") == "technical_acceptability"
    ]
    require(
        len(reviewability_steps) == 1
        and frozenset(reviewability_steps[0].get("allowed_values", ()))
        == LOCKED_REVIEW_VOCABULARIES["reviewable_yes_no_uncertain"],
        "Reviewability vocabulary has drifted from the locked rubric.",
    )
    ordinal_vocabulary = LOCKED_REVIEW_VOCABULARIES[
        "whole_section_inflammation_extent_0_4_uncertain"
    ]
    scales = [
        step["scale"]
        for step in sequence
        if isinstance(step, dict) and isinstance(step.get("scale"), dict)
    ]
    require(
        len(scales) == 3
        and all(frozenset(scale) == ordinal_vocabulary for scale in scales),
        "Ordinal review scale has drifted from the locked 0-4/uncertain vocabulary.",
    )


def _locked_blind_ids(study: dict[str, Any]) -> tuple[str, ...]:
    require(
        study.get("study_id") == "g_surf_he_20260812"
        and study.get("modality") == "brightfield_he"
        and study.get("biological_unit") == "mouse"
        and study.get("expected_mouse_count") == 4
        and study.get("expected_analytical_sections") == 8,
        "Study is not the locked four-mouse/eight-section H&E contract.",
    )
    blind_map = study.get("blind_section_map")
    require(
        isinstance(blind_map, list) and len(blind_map) == 8,
        "Study blind map must contain exactly eight rows.",
    )
    blind_ids = tuple(
        row.get("blind_section_id") if isinstance(row, dict) else None
        for row in blind_map
    )
    expected = tuple(f"HE-{index:03d}" for index in range(1, 9))
    require(
        all(isinstance(blind_id, str) for blind_id in blind_ids),
        "Study blind IDs must be strings.",
    )
    require(
        tuple(sorted(blind_ids)) == expected,
        "Study blind IDs differ from the locked HE-001 through HE-008 set.",
    )
    return expected


def _parse_utc(value: str, location: str) -> datetime:
    require(
        "T" in value and (value.endswith("Z") or value.endswith("+00:00")),
        f"{location} must be an explicit UTC ISO-8601 timestamp.",
    )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractError(
            f"{location} must be a valid UTC ISO-8601 timestamp."
        ) from exc
    require(
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.utcoffset().total_seconds() == 0,
        f"{location} must carry UTC offset zero.",
    )
    return parsed


def _read_locked_review(
    review_csv: Path, expected_blind_ids: tuple[str, ...]
) -> list[dict[str, str]]:
    require(review_csv.is_file(), f"Blinded review CSV is missing: {review_csv}")
    try:
        with review_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            table = list(csv.reader(handle, strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ContractError("Blinded review CSV is not valid UTF-8 CSV.") from exc
    require(bool(table), "Blinded review CSV is empty.")
    require(
        tuple(table[0]) == LOCKED_REVIEW_FIELDS,
        "Blinded review header must exactly match the locked ordered header.",
    )
    require(
        len(table) == 9,
        "Blinded review must contain exactly eight data rows and no blank rows.",
    )

    rows: list[dict[str, str]] = []
    ordinal_columns = tuple(
        endpoint["source_column"] for endpoint in ORDINAL_REVIEW_ENDPOINTS
    )
    for row_number, values in enumerate(table[1:], start=2):
        require(
            len(values) == len(LOCKED_REVIEW_FIELDS),
            f"Review row {row_number} does not have exactly 16 fields.",
        )
        row = dict(zip(LOCKED_REVIEW_FIELDS, values))
        for field, value in row.items():
            require("\x00" not in value, f"Review row {row_number} field {field} contains NUL.")
            require(
                value == value.strip(),
                f"Review row {row_number} field {field} has leading/trailing whitespace.",
            )
            if field != "notes":
                require(
                    bool(value),
                    f"Review row {row_number} field {field} is incomplete.",
                )
        for field, vocabulary in LOCKED_REVIEW_VOCABULARIES.items():
            require(
                row[field] in vocabulary,
                f"Review row {row_number} field {field} has invalid value {row[field]!r}.",
            )
        _parse_utc(row["reviewed_utc"], f"Review row {row_number} reviewed_utc")

        if row["reviewable_yes_no_uncertain"] != "yes":
            require(
                all(row[column] == "uncertain" for column in ordinal_columns)
                and row[
                    "edema_hemorrhage_necrosis_present_yes_no_uncertain"
                ]
                == "uncertain"
                and row["dominant_pattern"] == "unresolved",
                f"Review row {row_number} is nonreviewable but carries a lesion call.",
            )
        rows.append(row)

    observed_ids = [row["blind_section_id"] for row in rows]
    require(
        tuple(sorted(observed_ids)) == expected_blind_ids
        and len(set(observed_ids)) == 8,
        "Blinded review must contain each locked blind section exactly once.",
    )
    return rows


def _declared_package_sha256(sample: dict[str, Any]) -> str:
    package = sample.get("source_package")
    require(isinstance(package, dict), "Study sample lacks a source-package ledger.")
    package_sha256 = package.get("package_sha256")
    require(
        package.get("package_hash_algorithm")
        == "sha256_utf8_path_tab_size_tab_sha256_lf"
        and package.get("discovery_authority")
        == "bioformats_ImageReader_getUsedFiles"
        and _is_lower_sha256(package_sha256),
        "Study sample has an invalid source-package authority or digest.",
    )
    members = package.get("members")
    require(isinstance(members, list) and bool(members), "Source-package ledger is empty.")
    paths: list[str] = []
    lines: list[str] = []
    for member in members:
        require(
            isinstance(member, dict)
            and set(member) == {"relative_path", "size_bytes", "sha256"},
            "Source-package member schema is invalid.",
        )
        relative_path = member["relative_path"]
        size_bytes = member["size_bytes"]
        digest = member["sha256"]
        require(
            isinstance(relative_path, str)
            and relative_path
            and relative_path == relative_path.strip()
            and "\\" not in relative_path
            and not PurePosixPath(relative_path).is_absolute()
            and ".." not in PurePosixPath(relative_path).parts
            and isinstance(size_bytes, int)
            and not isinstance(size_bytes, bool)
            and size_bytes > 0
            and _is_lower_sha256(digest),
            "Source-package member declaration is invalid.",
        )
        paths.append(relative_path)
        lines.append(f"{relative_path}\t{size_bytes}\t{digest}\n")
    require(
        paths == sorted(paths) and len(paths) == len(set(paths)),
        "Source-package members must be sorted and unique.",
    )
    require(
        hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
        == package_sha256,
        "Source-package digest disagrees with its locked ledger.",
    )
    return package_sha256


def _unblinding_index(study: dict[str, Any]) -> dict[str, dict[str, Any]]:
    samples = study.get("samples")
    require(isinstance(samples, list) and len(samples) == 4, "Study must define four mice.")
    sections: dict[str, dict[str, Any]] = {}
    mouse_ids: set[str] = set()
    for sample in samples:
        require(isinstance(sample, dict), "Study sample must be an object.")
        mouse_id = sample.get("mouse_id")
        section_ids = sample.get("section_ids")
        require(
            isinstance(mouse_id, str)
            and bool(mouse_id)
            and sample.get("biological_unit_id") == mouse_id
            and mouse_id not in mouse_ids
            and isinstance(sample.get("genotype"), str)
            and bool(sample["genotype"])
            and isinstance(sample.get("condition"), str)
            and bool(sample["condition"])
            and isinstance(section_ids, list)
            and len(section_ids) == 2
            and len(set(section_ids)) == 2,
            "Study sample identity or paired technical-section declaration is invalid.",
        )
        mouse_ids.add(mouse_id)
        package_sha256 = _declared_package_sha256(sample)
        for order, section_id in enumerate(section_ids, start=1):
            require(
                isinstance(section_id, str)
                and bool(section_id)
                and section_id not in sections,
                "Study section identity is invalid or duplicated.",
            )
            sections[section_id] = {
                "section_id": section_id,
                "technical_section_order": order,
                "mouse_id": mouse_id,
                "genotype": sample["genotype"],
                "condition": sample["condition"],
                "source_package_sha256": package_sha256,
            }
    require(len(sections) == 8, "Study does not define exactly eight technical sections.")

    index: dict[str, dict[str, Any]] = {}
    for mapping in study["blind_section_map"]:
        require(
            isinstance(mapping, dict)
            and set(mapping) == {"blind_section_id", "section_id"}
            and mapping["blind_section_id"] not in index
            and mapping["section_id"] in sections,
            "Study blind map cannot be unblinded exactly.",
        )
        index[mapping["blind_section_id"]] = sections[mapping["section_id"]]
    require(
        len(index) == 8
        and len({value["section_id"] for value in index.values()}) == 8,
        "Study blind map is not one-to-one.",
    )
    return index


def expected_sections(study: dict[str, Any]) -> list[str]:
    return [
        section_id
        for sample in study["samples"]
        for section_id in sample["section_ids"]
    ]


def _validate_source_package(
    source_root: Path, sample: dict[str, Any]
) -> dict[str, Any]:
    """Verify the exact Bio-Formats-discovered VSI package content."""

    source_file = sample["source_file"]
    package = sample.get("source_package")
    require(isinstance(package, dict), f"{source_file} lacks source_package.")
    require(
        set(package)
        == {
            "format",
            "discovery_authority",
            "package_hash_algorithm",
            "package_sha256",
            "members",
        }
        and package.get("package_hash_algorithm")
        == "sha256_utf8_path_tab_size_tab_sha256_lf"
        and isinstance(package.get("package_sha256"), str)
        and len(package["package_sha256"]) == 64
        and all(char in "0123456789abcdef" for char in package["package_sha256"]),
        f"{source_file} has an invalid source-package schema or package hash.",
    )
    require(
        package.get("format") == "olympus_vsi"
        and package.get("discovery_authority")
        == "bioformats_ImageReader_getUsedFiles",
        f"{source_file} has unsupported source-package authority.",
    )
    members = package.get("members")
    require(
        isinstance(members, list) and members,
        f"{source_file} source_package.members must be non-empty.",
    )

    declared_paths: list[str] = []
    normalized_members: list[dict[str, Any]] = []
    for member in members:
        require(isinstance(member, dict), f"{source_file} package member is not an object.")
        require(
            set(member) == {"relative_path", "size_bytes", "sha256"},
            f"{source_file} package member fields are not exact.",
        )
        relative = member.get("relative_path")
        require(
            isinstance(relative, str)
            and relative == relative.strip()
            and relative
            and "\\" not in relative
            and not PurePosixPath(relative).is_absolute()
            and ".." not in PurePosixPath(relative).parts,
            f"{source_file} has an unsafe package path: {relative!r}",
        )
        size = member.get("size_bytes")
        digest = member.get("sha256")
        require(
            isinstance(size, int) and not isinstance(size, bool) and size > 0,
            f"{source_file} package member has an invalid size: {relative}",
        )
        require(
            isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest),
            f"{source_file} package member has an invalid SHA-256: {relative}",
        )
        path = source_root.joinpath(*PurePosixPath(relative).parts)
        try:
            path.resolve(strict=True).relative_to(source_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ContractError(
                f"{source_file} package member is missing or escapes the source root: {relative}"
            ) from exc
        require(path.is_file() and not path.is_symlink(), f"Package member is not a regular file: {path}")
        require(path.stat().st_size == size, f"Package size mismatch: {path}")
        require(sha256_file(path) == digest, f"Package hash mismatch: {path}")
        declared_paths.append(relative)
        normalized_members.append(
            {"relative_path": relative, "size_bytes": size, "sha256": digest}
        )

    require(
        declared_paths == sorted(declared_paths) and len(declared_paths) == len(set(declared_paths)),
        f"{source_file} package members must be sorted and unique.",
    )
    source = source_root / source_file
    companion = source_root / f"_{source.stem}_"
    observed_paths = {source_file}
    observed_paths.update(
        path.relative_to(source_root).as_posix() for path in companion.rglob("*.ets")
    )
    require(
        set(declared_paths) == observed_paths,
        f"{source_file} package ledger differs from the exact source/ETS inventory: "
        f"missing={sorted(observed_paths - set(declared_paths))}, "
        f"extra={sorted(set(declared_paths) - observed_paths)}",
    )
    package_payload = "".join(
        f"{member['relative_path']}\t{member['size_bytes']}\t{member['sha256']}\n"
        for member in normalized_members
    ).encode("utf-8")
    package_sha256 = hashlib.sha256(package_payload).hexdigest()
    require(
        package_sha256 == package["package_sha256"],
        f"{source_file} package_sha256 disagrees with its member ledger.",
    )
    return {
        "member_count": len(normalized_members),
        "package_sha256": package_sha256,
        "discovery_authority": package["discovery_authority"],
        "content_authority": "declared_members_size_and_sha256_verified",
    }


def validate_study(study: dict[str, Any]) -> dict[str, Any]:
    samples = study.get("samples", [])
    sections = expected_sections(study)
    blind_map = study.get("blind_section_map", [])
    source_root = Path(study["source_root"])

    require(study.get("modality") == "brightfield_he", "Study modality is not H&E.")
    require(
        len(samples) == study["expected_mouse_count"],
        "Declared mouse count does not match the sample list.",
    )
    require(
        len(sections) == study["expected_analytical_sections"],
        "Declared section count does not match the sample list.",
    )
    require(len(sections) == len(set(sections)), "Duplicate analytical section ID.")
    require(
        {row["section_id"] for row in blind_map} == set(sections),
        "Blind-section map does not cover the declared analytical sections exactly.",
    )
    require(
        len({row["blind_section_id"] for row in blind_map}) == len(sections),
        "Blind-section IDs are not unique.",
    )
    require(source_root.is_dir(), f"H&E source root is missing: {source_root}")

    expected_vsi = {sample["source_file"] for sample in samples}
    observed_vsi = {path.name for path in source_root.glob("*.vsi")}
    require(
        observed_vsi == expected_vsi,
        "VSI inventory differs from the four-slide study contract: "
        f"missing={sorted(expected_vsi - observed_vsi)}, "
        f"extra={sorted(observed_vsi - expected_vsi)}",
    )

    ets_counts: dict[str, int] = {}
    source_packages: dict[str, dict[str, Any]] = {}
    for sample in samples:
        source = source_root / sample["source_file"]
        require(source.is_file(), f"Declared VSI is missing: {source}")
        companion = source_root / f"_{source.stem}_"
        require(companion.is_dir(), f"VSI companion directory is missing: {companion}")
        ets_count = len(list(companion.rglob("*.ets")))
        require(ets_count >= 2, f"VSI companion data are incomplete: {companion}")
        ets_counts[sample["mouse_id"]] = ets_count
        source_packages[sample["mouse_id"]] = _validate_source_package(
            source_root, sample
        )

    return {
        "mouse_count": len(samples),
        "section_count": len(sections),
        "vsi_count": len(observed_vsi),
        "ets_files_by_mouse": ets_counts,
        "source_packages": source_packages,
        "source_package_authority": (
            "bioformats_used_files_members_with_size_and_sha256"
        ),
        "source_root": str(source_root),
    }


def verify_manifest_files(root: Path, manifest: dict[str, Any]) -> int:
    checked = 0
    for item in manifest.get("files", []):
        path = root / item["relative_path"]
        require(path.is_file(), f"Manifest file is missing: {path}")
        require(
            sha256_file(path).lower() == item["sha256"].lower(),
            f"Manifest hash mismatch: {path}",
        )
        checked += 1
    return checked


def validate_r1(
    study: dict[str, Any], r1_root: Path, repo_profile: Path = DEFAULT_REPO_PROFILE
) -> dict[str, Any]:
    require(r1_root.is_dir(), f"Approved R1 package is missing: {r1_root}")
    approved = study["approved_packages"]
    approval_path = r1_root / "R1_REVIEW_APPROVAL.json"
    manifest_path = r1_root / "PACKAGE_MANIFEST.json"
    source_profile = (
        r1_root
        / "INTERNAL_DO_NOT_SEND"
        / "04_LOCKED_R1_PROFILE"
        / "g_surf_he_20260812_reviewed_locked_v1.json"
    )

    require(
        sha256_file(approval_path) == approved["r1_approval_sha256"],
        "R1 approval hash differs from the study contract.",
    )
    require(
        sha256_file(manifest_path) == approved["r1_package_manifest_sha256"],
        "R1 package-manifest hash differs from the study contract.",
    )
    require(
        sha256_file(source_profile) == approved["locked_stain_profile_sha256"],
        "Approved stain-profile file hash differs from the study contract.",
    )
    require(
        canonical_json_sha256(source_profile) == canonical_json_sha256(repo_profile),
        "Repository stain profile is not semantically identical to the approved profile.",
    )

    approval = read_json(approval_path)
    manifest = read_json(manifest_path)
    require(approval.get("decision") == "APPROVED_IMAGE_QC", "R1 QC is not approved.")
    require(
        manifest.get("status") == "R1_IMAGE_QC_APPROVED_FINAL",
        "R1 package does not carry final-approved status.",
    )
    blind_ids = {row["blind_section_id"] for row in study["blind_section_map"]}
    require(set(manifest.get("sections", [])) == blind_ids, "R1 section set mismatch.")
    checked = verify_manifest_files(r1_root, manifest)

    metrics = read_csv(r1_root / "R1_APPROVED_QC_METRICS.csv")
    require(len(metrics) == len(blind_ids), "R1 QC metrics are incomplete.")
    require(
        {row["blind_id"] for row in metrics} == blind_ids,
        "R1 QC metrics do not match the blind-section set.",
    )
    return {
        "decision": approval["decision"],
        "approved_utc": approval["approved_utc"],
        "section_count": len(metrics),
        "manifest_files_verified": checked,
        "locked_profile_id": approved["locked_stain_profile_id"],
    }


def validate_h4(study: dict[str, Any], h4_root: Path) -> dict[str, Any]:
    require(h4_root.is_dir(), f"H4 development package is missing: {h4_root}")
    manifest_path = h4_root / "H4_REGION_PACKAGE_MANIFEST.json"
    require(
        sha256_file(manifest_path)
        == study["approved_packages"]["h4_package_manifest_sha256"],
        "H4 package-manifest hash differs from the study contract.",
    )
    manifest = read_json(manifest_path)
    require(
        manifest.get("status") == "H4_REGION_REVIEW_REQUIRED_NOT_R2_RESULT",
        "H4 package status is not the expected development-only state.",
    )

    candidates = read_csv(
        h4_root / "INTERNAL_PROVENANCE" / "H4_REGION_CANDIDATES__UNBLINDED.csv"
    )
    inventory = read_csv(
        h4_root / "INTERNAL_PROVENANCE" / "H4_REGION_EXPORT_INVENTORY.csv"
    )
    reviews = read_csv(h4_root / "04_REVIEW_FORMS" / "H4_REGION_REVIEW.csv")
    require(len(candidates) == 96, "H4 candidate inventory must contain 96 regions.")
    require(len(inventory) == len(candidates), "H4 export inventory is incomplete.")
    require(len(reviews) == len(candidates), "H4 review form is incomplete.")

    for row in inventory:
        path = h4_root / row["relative_path"]
        require(path.is_file(), f"H4 region export is missing: {path}")
        require(sha256_file(path) == row["sha256"], f"H4 region hash mismatch: {path}")

    editable = [
        field
        for field in reviews[0]
        if field not in {"candidate_id", "blind_id", "sampling_arm"}
    ]
    touched = sum(any((row.get(field) or "").strip() for field in editable) for row in reviews)
    required = [
        "reviewable_yes_no",
        "dominant_context",
        "accept_geometry_yes_no_edit",
        "reviewer_id",
        "reviewed_utc",
    ]
    completed = sum(all((row.get(field) or "").strip() for field in required) for row in reviews)
    return {
        "status": manifest["status"],
        "candidate_count": len(candidates),
        "primary_spatial_count": sum(
            row["sampling_arm"] == "CORE_SPATIAL" for row in candidates
        ),
        "diversity_supplement_count": sum(
            row["sampling_arm"] == "DIVERSITY_SUPPLEMENT" for row in candidates
        ),
        "review_rows_touched": touched,
        "review_rows_complete": completed,
        "use_as_endpoint": False,
    }


def stage_rows(r1: dict[str, Any], h4: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"stage": "H0", "status": "PASS", "evidence": "4 declared calibrated RGB VSI slides"},
        {"stage": "H1", "status": "PASS", "evidence": "4 mice / 8 analytical sections / blind map"},
        {"stage": "H2", "status": "APPROVED_R1", "evidence": r1["locked_profile_id"]},
        {"stage": "H3", "status": "APPROVED_R1", "evidence": "reviewed masks and artifact presentation"},
        {
            "stage": "H4",
            "status": "DEVELOPMENT_CONTEXT_AVAILABLE",
            "evidence": f"{h4['candidate_count']} regions; incomplete review; not an endpoint",
        },
        {
            "stage": "H5",
            "status": "BLOCKED",
            "evidence": "no validated nuclei or lesion-candidate engine",
        },
        {
            "stage": "H6",
            "status": "BLOCKED",
            "evidence": "no validated compartment/topology authorization",
        },
        {
            "stage": "H7",
            "status": "RUBRIC_DEFINED_REVIEW_REQUIRED",
            "evidence": "blinded whole-section development rubric",
        },
        {
            "stage": "H8",
            "status": "BLOCKED",
            "evidence": "section review and endpoint components incomplete",
        },
        {
            "stage": "H9",
            "status": "BLOCKED",
            "evidence": "n=1 per design cell; mouse-level descriptive join only",
        },
    ]


def _pipeline_status_for_study(
    study: dict[str, Any],
    r1_root: Path | None = None,
    h4_root: Path | None = None,
) -> dict[str, Any]:
    resolved_r1, resolved_h4, package_roots = resolve_package_roots(
        study, r1_root, h4_root
    )
    source = validate_study(study)
    r1 = validate_r1(study, resolved_r1)
    h4 = validate_h4(study, resolved_h4)
    return {
        "schema_version": "1.0.0",
        "checked_utc": utc_now(),
        "study_id": study["study_id"],
        "highest_authorized_release": "R1",
        "highest_authorized_stage": "H3",
        "launcher_route_enabled": False,
        "package_roots": package_roots,
        "source": source,
        "r1": r1,
        "h4": h4,
        "stages": stage_rows(r1, h4),
        "reportability": {
            "image_qc_and_denominator": "APPROVED_FOR_THIS_COHORT",
            "section_pathology_scores": "REVIEW_REQUIRED",
            "automated_lesion_burden": "NOT_AVAILABLE",
            "mouse_summary": "BLOCKED",
            "group_inference": "NOT_SUPPORTED_N1_PER_DESIGN_CELL",
            "he_identifies_krt5_pod": False,
            "immune_lineage_from_he": False,
        },
    }


def pipeline_status(
    study_path: Path = DEFAULT_STUDY,
    r1_root: Path | None = None,
    h4_root: Path | None = None,
) -> dict[str, Any]:
    return _pipeline_status_for_study(read_json(study_path), r1_root, h4_root)


def section_review_rows(
    study: dict[str, Any], rubric: dict[str, Any]
) -> tuple[list[str], list[dict[str, str]]]:
    fields = list(rubric["section_form_fields"])
    rows = []
    for mapping in sorted(
        study["blind_section_map"], key=lambda row: row["blind_section_id"]
    ):
        row = {field: "" for field in fields}
        row["blind_section_id"] = mapping["blind_section_id"]
        rows.append(row)
    return fields, rows


def _copy_required(source: Path, target: Path) -> None:
    require(source.is_file(), f"Review-package source is missing: {source}")
    shutil.copy2(source, target)


def build_review_package(
    output_root: Path,
    study_path: Path = DEFAULT_STUDY,
    rubric_path: Path = DEFAULT_RUBRIC,
    r1_root: Path | None = None,
    h4_root: Path | None = None,
) -> dict[str, Any]:
    require(not output_root.exists(), f"Refusing to overwrite existing output: {output_root}")
    study = read_json(study_path)
    rubric = read_json(rubric_path)
    status = _pipeline_status_for_study(study, r1_root, h4_root)
    require(status["highest_authorized_release"] == "R1", "R1 gate is not satisfied.")
    resolved_r1, resolved_h4, _package_roots = resolve_package_roots(
        study, r1_root, h4_root
    )

    directories = [
        "00_START_HERE",
        "01_BLINDED_SECTION_OVERVIEWS",
        "02_R1_APPROVED_QC_CONTEXT",
        "03_HIGH_RES_SUPPORTING_CONTEXT",
        "04_REVIEW_FORMS",
        "INTERNAL_DO_NOT_SEND",
    ]
    for relative in directories:
        (output_root / relative).mkdir(parents=True, exist_ok=False)

    reviewer_root = resolved_r1 / "SEND_TO_REVIEWER"
    blind_ids = [
        row["blind_section_id"]
        for row in sorted(
            study["blind_section_map"], key=lambda row: row["blind_section_id"]
        )
    ]
    for blind_id in blind_ids:
        _copy_required(
            reviewer_root / "01_RAW_REFERENCE" / f"{blind_id}__01_raw_reference.png",
            output_root
            / "01_BLINDED_SECTION_OVERVIEWS"
            / f"{blind_id}__whole_section_reference.png",
        )
        _copy_required(
            reviewer_root / "04_QC_OVERLAYS" / f"{blind_id}__qc_overlay__DISPLAY_ONLY.png",
            output_root
            / "02_R1_APPROVED_QC_CONTEXT"
            / f"{blind_id}__R1_approved_qc_display_only.png",
        )
        _copy_required(
            resolved_h4
            / "00_START_HERE"
            / f"CONTACT_SHEET_{blind_id}_HIGHRES.jpg",
            output_root
            / "03_HIGH_RES_SUPPORTING_CONTEXT"
            / f"{blind_id}__high_resolution_supporting_regions.jpg",
        )

    fields, rows = section_review_rows(study, rubric)
    write_csv(
        output_root / "04_REVIEW_FORMS" / "H7_SECTION_PATHOLOGY_REVIEW.csv",
        fields,
        rows,
    )
    write_csv(
        output_root / "04_REVIEW_FORMS" / "ENDPOINT_REPORTABILITY.csv",
        ["endpoint", "current_status", "maximum_claim"],
        rubric["reportability"],
    )
    shutil.copy2(
        rubric_path,
        output_root / "04_REVIEW_FORMS" / "PATHOLOGY_REVIEW_RUBRIC.json",
    )

    unblinding_source = (
        resolved_r1
        / "INTERNAL_DO_NOT_SEND"
        / "01_UNBLINDING_KEY"
        / "SECTION_UNBLINDING_KEY__DO_NOT_SEND.csv"
    )
    _copy_required(
        unblinding_source,
        output_root / "INTERNAL_DO_NOT_SEND" / "SECTION_UNBLINDING_KEY__DO_NOT_SEND.csv",
    )
    for source, name in (
        (resolved_r1 / "R1_REVIEW_APPROVAL.json", "R1_REVIEW_APPROVAL.json"),
        (resolved_r1 / "PACKAGE_MANIFEST.json", "R1_PACKAGE_MANIFEST.json"),
        (
            resolved_h4 / "H4_REGION_PACKAGE_MANIFEST.json",
            "H4_CONTEXT_PACKAGE_MANIFEST.json",
        ),
    ):
        _copy_required(source, output_root / "INTERNAL_DO_NOT_SEND" / name)
    write_json(output_root / "INTERNAL_DO_NOT_SEND" / "PIPELINE_STATUS.json", status)

    readme = """# H&E whole-section pathology review - development package

## What to review

Score each blinded whole section in 04_REVIEW_FORMS/H7_SECTION_PATHOLOGY_REVIEW.csv.
Start with the whole-section reference, use the approved R1 overlay only to recognize
the accepted denominator/artifacts, and consult the high-resolution contact sheet
only when morphology needs confirmation.

This replaces the low-value task of classifying every sampled tile as airway,
vessel, or alveolus. The primary decision is whether the section contains abnormal
inflammatory-cell-rich structural injury, how extensive it is, and where it occurs.

## Anatomy shorthand

- Airway: circular or branching lumen surrounded by a continuous epithelial
  nuclear lining.
- Alveolar parenchyma: sponge-like small airspaces separated by thin septa.
- Vessel: thin-walled elongated/slit-like or partly collapsed lumen with an
  endothelial nuclear lining.

## Interpretation boundary

H&E can support inflammatory-cell-rich infiltration, consolidation, cuffing,
airspace loss, and epithelial injury. It cannot identify immune lineage or a
KRT5-positive pod. Pod identity remains in the settled IF analysis; H&E supplies
whole-section injury context. The current n=4 cohort is descriptive only.
"""
    (output_root / "00_START_HERE" / "README_REVIEW.md").write_text(
        readme, encoding="utf-8"
    )

    files = []
    for package_file in sorted(output_root.rglob("*")):
        if not package_file.is_file() or package_file.name == "PACKAGE_MANIFEST.json":
            continue
        files.append(
            {
                "relative_path": package_file.relative_to(output_root).as_posix(),
                "bytes": package_file.stat().st_size,
                "sha256": sha256_file(package_file),
            }
        )
    package_manifest = {
        "schema_version": "1.0.0",
        "created_utc": utc_now(),
        "status": "H5_H7_DEVELOPMENT_REVIEW_REQUIRED_NOT_AN_ANALYSIS_RESULT",
        "study_id": study["study_id"],
        "highest_input_release": "R1",
        "section_count": len(blind_ids),
        "primary_review_unit": "blinded_whole_section",
        "supporting_region_role": "evidence_locator_not_replicate_or_prevalence_sample",
        "files": files,
    }
    write_json(output_root / "PACKAGE_MANIFEST.json", package_manifest)
    return package_manifest


def _unblinded_section_rows(
    blinded_rows: list[dict[str, str]],
    unblinding_index: dict[str, dict[str, Any]],
    study_id: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for review in sorted(blinded_rows, key=lambda row: row["blind_section_id"]):
        identity = unblinding_index[review["blind_section_id"]]
        reviewability = review["reviewable_yes_no_uncertain"]
        section_evaluability = {
            "yes": "evaluable",
            "no": "not_reviewable",
            "uncertain": "reviewability_uncertain",
        }[reviewability]
        output.append(
            {
                "study_id": study_id,
                **identity,
                "section_evaluability": section_evaluability,
                **review,
            }
        )
    return output


def _build_review_measurement_records(
    section_rows: list[dict[str, Any]],
    *,
    rubric: dict[str, Any],
    review_sha256: str,
    study_sha256: str,
    rubric_sha256: str,
    profile_sha256: str,
    measurement_schema_sha256: str,
    code_revision: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from ifquant.adapters import (  # pylint: disable=import-outside-toplevel
        MeasurementAdapterError,
        MeasurementRecordContext,
        OrdinalColumnMapping,
        build_ordinal_measurement_record,
    )
    from ifquant.contracts import (  # pylint: disable=import-outside-toplevel
        MeasurementContractError,
        require_aggregation_batch_eligible,
    )

    profile_id = f"{rubric['rubric_id']}:section-ordinal-schema-v2"
    profile_payload = {
        "schema_version": "1.0.0",
        "measurement_profile_id": profile_id,
        "rubric_sha256": rubric_sha256,
        "locked_stain_profile_sha256": profile_sha256,
        "review_unit": "whole_section",
        "record_level": "section",
        "sampling": {
            "design": "purposive",
            "estimand_scope": "observed_units",
            "selection_source": (
                "locked_two_technical_whole_sections_per_mouse"
            ),
        },
        "ordinal_endpoints": list(ORDINAL_REVIEW_ENDPOINTS),
        "aggregation_policy": {
            "retain_technical_section_order": True,
            "allow_scalar_ordinal_composite": False,
            "agreement_statistic": "exact_agreement",
        },
    }
    measurement_profile_sha256 = canonical_value_sha256(profile_payload)
    run_id = f"he-review-aggregate:{review_sha256[:20]}"
    records: list[dict[str, Any]] = []
    measured_records: list[dict[str, Any]] = []

    try:
        for row in section_rows:
            reviewability = row["reviewable_yes_no_uncertain"]
            for endpoint in ORDINAL_REVIEW_ENDPOINTS:
                source_column = endpoint["source_column"]
                token = row[source_column]
                if reviewability == "yes" and token != "uncertain":
                    evaluability = "measured"
                    reason_code = None
                    qc_status = "pass"
                    qc_reasons: list[str] = []
                elif reviewability == "no":
                    evaluability = "not_evaluable"
                    reason_code = "section_not_reviewable"
                    qc_status = "warning"
                    qc_reasons = [reason_code]
                elif reviewability == "uncertain":
                    evaluability = "not_evaluable"
                    reason_code = "section_reviewability_uncertain"
                    qc_status = "warning"
                    qc_reasons = [reason_code]
                else:
                    evaluability = "not_evaluable"
                    reason_code = "endpoint_score_uncertain"
                    qc_status = "warning"
                    qc_reasons = [reason_code]

                context = MeasurementRecordContext(
                    track="he_pathology",
                    record_level="section",
                    identifiers={
                        "mouse_id": row["mouse_id"],
                        "slide_id": None,
                        "section_id": row["section_id"],
                        "field_id": None,
                        "tile_id": None,
                        "region_id": None,
                        "cell_id": None,
                    },
                    measurement_profile_id=profile_id,
                    channel_signature=(
                        {
                            "index": 1,
                            "label": "RGB_brightfield_HE",
                            "role": "histology_source",
                        },
                    ),
                    segmentation_model=None,
                    sampling={
                        "design": "purposive",
                        "inclusion_probability": None,
                        "selection_source": (
                            "locked_two_technical_whole_sections_per_mouse"
                        ),
                        "estimand_scope": "observed_units",
                        "estimator_profile_id": None,
                    },
                    compartment={
                        "status": "assigned",
                        "labels": [endpoint["compartment_label"]],
                        "assignment_profile_id": (
                            f"{rubric['rubric_id']}:anatomy-reference-v1"
                        ),
                    },
                    provenance={
                        "code_revision": code_revision,
                        "config_sha256": rubric_sha256,
                        "measurement_profile_sha256": (
                            measurement_profile_sha256
                        ),
                        "inputs": [
                            {"role": "blinded_review_csv", "sha256": review_sha256},
                            {"role": "study_contract", "sha256": study_sha256},
                            {"role": "review_rubric", "sha256": rubric_sha256},
                            {
                                "role": "locked_stain_profile",
                                "sha256": profile_sha256,
                            },
                            {
                                "role": "measurement_record_schema",
                                "sha256": measurement_schema_sha256,
                            },
                            {
                                "role": "declared_source_package_ledger",
                                "sha256": row["source_package_sha256"],
                            },
                        ],
                        "run_id": run_id,
                    },
                    qc={
                        "status": qc_status,
                        "reason_codes": qc_reasons,
                        "review_status": "accepted",
                    },
                )
                mapping = OrdinalColumnMapping(
                    endpoint_id=endpoint["endpoint_id"],
                    reference_space_id=endpoint["reference_space_id"],
                    rank_column=source_column,
                    scale_id=endpoint["scale_id"],
                    minimum_rank=0,
                    maximum_rank=4,
                )
                record = build_ordinal_measurement_record(
                    row,
                    mapping,
                    context,
                    evaluability=evaluability,
                    reason_code=reason_code,
                )
                records.append(record)
                if evaluability == "measured":
                    measured_records.append(record)

        if measured_records:
            require_aggregation_batch_eligible(
                measured_records, target_estimand="observed_units"
            )
    except (MeasurementAdapterError, MeasurementContractError) as exc:
        raise ContractError(
            f"H&E schema-v2 measurement records are not aggregation-eligible: {exc}"
        ) from exc
    return records, measured_records, profile_id, measurement_profile_sha256


def _mouse_ordinal_summaries(
    section_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_mouse: dict[str, list[dict[str, Any]]] = {}
    for row in section_rows:
        rows_by_mouse.setdefault(row["mouse_id"], []).append(row)

    output: list[dict[str, Any]] = []
    for mouse_id, mouse_rows in rows_by_mouse.items():
        mouse_rows.sort(key=lambda row: row["technical_section_order"])
        require(
            len(mouse_rows) == 2
            and [row["technical_section_order"] for row in mouse_rows] == [1, 2],
            f"Mouse {mouse_id} does not have the locked ordered technical-section pair.",
        )
        for endpoint in ORDINAL_REVIEW_ENDPOINTS:
            source_column = endpoint["source_column"]
            ordered_values = [row[source_column] for row in mouse_rows]
            observed_ranks = [
                int(value) for value in ordered_values if value != "uncertain"
            ]
            n_evaluable = len(observed_ranks)
            paired_evaluability = (
                "both_evaluable"
                if n_evaluable == 2
                else "partial"
                if n_evaluable == 1
                else "none"
            )
            exact_agreement = (
                "true"
                if n_evaluable == 2 and observed_ranks[0] == observed_ranks[1]
                else "false"
                if n_evaluable == 2
                else "not_evaluable"
            )
            output.append(
                {
                    "study_id": mouse_rows[0]["study_id"],
                    "mouse_id": mouse_id,
                    "genotype": mouse_rows[0]["genotype"],
                    "condition": mouse_rows[0]["condition"],
                    "endpoint_id": endpoint["endpoint_id"],
                    "scale_id": endpoint["scale_id"],
                    "section_1_id": mouse_rows[0]["section_id"],
                    "section_1_reviewability": mouse_rows[0][
                        "reviewable_yes_no_uncertain"
                    ],
                    "section_1_value": ordered_values[0],
                    "section_2_id": mouse_rows[1]["section_id"],
                    "section_2_reviewability": mouse_rows[1][
                        "reviewable_yes_no_uncertain"
                    ],
                    "section_2_value": ordered_values[1],
                    "ordered_section_values_json": json.dumps(
                        ordered_values, separators=(",", ":")
                    ),
                    "n_evaluable_sections": n_evaluable,
                    "paired_evaluability": paired_evaluability,
                    "minimum_observed_rank": (
                        min(observed_ranks) if observed_ranks else ""
                    ),
                    "maximum_observed_rank": (
                        max(observed_ranks) if observed_ranks else ""
                    ),
                    "exact_agreement": exact_agreement,
                }
            )
    return output


def _technical_section_agreement(
    mouse_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for endpoint in ORDINAL_REVIEW_ENDPOINTS:
        endpoint_rows = [
            row
            for row in mouse_summaries
            if row["endpoint_id"] == endpoint["endpoint_id"]
        ]
        require(
            len(endpoint_rows) == 4,
            f"Endpoint {endpoint['endpoint_id']} lacks four technical-section pairs.",
        )
        both = [
            row for row in endpoint_rows if row["paired_evaluability"] == "both_evaluable"
        ]
        exact = [row for row in both if row["exact_agreement"] == "true"]
        pair_details = [
            {
                "mouse_id": row["mouse_id"],
                "ordered_section_values": json.loads(
                    row["ordered_section_values_json"]
                ),
                "paired_evaluability": row["paired_evaluability"],
                "exact_agreement": row["exact_agreement"],
            }
            for row in endpoint_rows
        ]
        output.append(
            {
                "study_id": endpoint_rows[0]["study_id"],
                "endpoint_id": endpoint["endpoint_id"],
                "scale_id": endpoint["scale_id"],
                "n_mouse_pairs_declared": 4,
                "n_pairs_both_evaluable": len(both),
                "n_pairs_not_both_evaluable": 4 - len(both),
                "n_exact_agreement": len(exact),
                "exact_agreement_fraction": (
                    len(exact) / len(both) if both else ""
                ),
                "ordered_mouse_pairs_json": json.dumps(
                    pair_details, separators=(",", ":"), ensure_ascii=False
                ),
            }
        )
    return output


def _review_output_artifacts(output_root: Path) -> list[dict[str, Any]]:
    roles = {
        SECTION_SCORES_FILENAME: "unblinded_section_scores",
        MOUSE_SUMMARY_FILENAME: "paired_technical_section_mouse_summary",
        TECHNICAL_AGREEMENT_FILENAME: "technical_section_exact_agreement",
        MEASUREMENT_RECORDS_FILENAME: "schema_v2_measurement_records",
    }
    artifacts = []
    for filename, role in roles.items():
        path = output_root / filename
        require(path.is_file(), f"Aggregation output is missing: {filename}")
        artifacts.append(
            {
                "role": role,
                "relative_path": filename,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return artifacts


def _audit_payload_sha256(audit: dict[str, Any]) -> str:
    return canonical_value_sha256(
        {key: value for key, value in audit.items() if key != "audit_payload_sha256"}
    )


def validate_review_aggregation_audit(output_root: Path) -> dict[str, Any]:
    require(output_root.is_dir(), f"Aggregation package is missing: {output_root}")
    audit_path = output_root / AGGREGATION_AUDIT_FILENAME
    audit = read_json(audit_path)
    expected_fields = {
        "schema_version",
        "audit_type",
        "status",
        "created_utc",
        "study_id",
        "rubric_id",
        "review_contract",
        "measurement_record_contract",
        "statistical_policy",
        "input_artifacts",
        "output_artifacts",
        "audit_payload_sha256",
    }
    require(set(audit) == expected_fields, "Aggregation audit fields are not exact.")
    require(
        audit.get("schema_version") == "1.0.0"
        and audit.get("audit_type") == "he_blinded_review_aggregation"
        and audit.get("status")
        == "ACCEPTED_REVIEW_AGGREGATED_DESCRIPTIVE_ONLY",
        "Aggregation audit identity or status is invalid.",
    )
    _parse_utc(audit["created_utc"], "Aggregation audit created_utc")
    require(
        _is_lower_sha256(audit.get("audit_payload_sha256"))
        and audit["audit_payload_sha256"] == _audit_payload_sha256(audit),
        "Aggregation audit payload hash mismatch.",
    )
    outputs = audit.get("output_artifacts")
    require(isinstance(outputs, list) and len(outputs) == 4, "Audit output ledger is incomplete.")
    expected_names = {
        SECTION_SCORES_FILENAME,
        MOUSE_SUMMARY_FILENAME,
        TECHNICAL_AGREEMENT_FILENAME,
        MEASUREMENT_RECORDS_FILENAME,
    }
    observed_names: set[str] = set()
    for artifact in outputs:
        require(
            isinstance(artifact, dict)
            and set(artifact) == {"role", "relative_path", "bytes", "sha256"},
            "Audit output artifact schema is invalid.",
        )
        relative_path = artifact["relative_path"]
        require(
            isinstance(relative_path, str)
            and relative_path in expected_names
            and relative_path not in observed_names,
            "Audit output path is unknown or duplicated.",
        )
        observed_names.add(relative_path)
        path = output_root / relative_path
        require(
            path.is_file()
            and path.stat().st_size == artifact["bytes"]
            and sha256_file(path) == artifact["sha256"],
            f"Published aggregation artifact failed integrity: {relative_path}",
        )
    require(observed_names == expected_names, "Audit output ledger is not exact.")
    observed_files = {
        path.name for path in output_root.iterdir() if path.is_file()
    }
    require(
        observed_files == expected_names | {AGGREGATION_AUDIT_FILENAME},
        "Aggregation package contains untracked or missing files.",
    )
    return audit


def aggregate_review(
    output_root: Path,
    review_csv: Path,
    study_path: Path = DEFAULT_STUDY,
    rubric_path: Path = DEFAULT_RUBRIC,
    profile_path: Path = DEFAULT_REPO_PROFILE,
    *,
    _before_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Validate, unblind, and publish descriptive H&E review outputs atomically."""

    output_root = output_root.resolve()
    review_csv = review_csv.resolve()
    study_path = study_path.resolve()
    rubric_path = rubric_path.resolve()
    profile_path = profile_path.resolve()
    script_path = Path(__file__).resolve()
    require(
        not output_root.exists(),
        f"Refusing to overwrite existing output: {output_root}",
    )
    require(
        output_root.parent.is_dir(),
        f"Aggregation output parent does not exist: {output_root.parent}",
    )
    input_paths = {
        "blinded_review_csv": review_csv,
        "study_contract": study_path,
        "review_rubric": rubric_path,
        "locked_stain_profile": profile_path,
        "measurement_record_schema": MEASUREMENT_RECORD_SCHEMA_PATH,
        "aggregation_code": script_path,
    }
    for role, path in input_paths.items():
        require(path.is_file(), f"Required aggregation input {role} is missing: {path}")
    input_hashes = {role: sha256_file(path) for role, path in input_paths.items()}

    study = read_json(study_path)
    rubric = read_json(rubric_path)
    profile = read_json(profile_path)
    expected_blind_ids = _locked_blind_ids(study)
    _validate_locked_review_rubric(rubric, study["study_id"])
    approved = study.get("approved_packages")
    require(
        isinstance(approved, dict)
        and profile.get("profile_id") == approved.get("locked_stain_profile_id")
        and profile.get("status") == "REVIEWED_LOCKED"
        and profile.get("study_id") == study["study_id"],
        "Repository stain profile does not carry the study's locked identity.",
    )

    # No section identity is looked up until the entire blinded table passes.
    blinded_rows = _read_locked_review(review_csv, expected_blind_ids)
    unblinding_index = _unblinding_index(study)
    section_rows = _unblinded_section_rows(
        blinded_rows, unblinding_index, study["study_id"]
    )
    (
        measurement_records,
        measured_records,
        measurement_profile_id,
        measurement_profile_sha256,
    ) = _build_review_measurement_records(
        section_rows,
        rubric=rubric,
        review_sha256=input_hashes["blinded_review_csv"],
        study_sha256=input_hashes["study_contract"],
        rubric_sha256=input_hashes["review_rubric"],
        profile_sha256=input_hashes["locked_stain_profile"],
        measurement_schema_sha256=input_hashes[
            "measurement_record_schema"
        ],
        code_revision=input_hashes["aggregation_code"],
    )
    # The eligibility gate above runs before these records are consumed into
    # paired descriptive summaries.
    mouse_summaries = _mouse_ordinal_summaries(section_rows)
    technical_agreement = _technical_section_agreement(mouse_summaries)

    section_fields = [
        "study_id",
        "mouse_id",
        "genotype",
        "condition",
        "section_id",
        "technical_section_order",
        "source_package_sha256",
        "section_evaluability",
        *LOCKED_REVIEW_FIELDS,
    ]
    mouse_fields = [
        "study_id",
        "mouse_id",
        "genotype",
        "condition",
        "endpoint_id",
        "scale_id",
        "section_1_id",
        "section_1_reviewability",
        "section_1_value",
        "section_2_id",
        "section_2_reviewability",
        "section_2_value",
        "ordered_section_values_json",
        "n_evaluable_sections",
        "paired_evaluability",
        "minimum_observed_rank",
        "maximum_observed_rank",
        "exact_agreement",
    ]
    agreement_fields = [
        "study_id",
        "endpoint_id",
        "scale_id",
        "n_mouse_pairs_declared",
        "n_pairs_both_evaluable",
        "n_pairs_not_both_evaluable",
        "n_exact_agreement",
        "exact_agreement_fraction",
        "ordered_mouse_pairs_json",
    ]

    staging: Path | None = None
    try:
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{output_root.name}.staging-", dir=output_root.parent
            )
        )
        write_csv(staging / SECTION_SCORES_FILENAME, section_fields, section_rows)
        write_csv(staging / MOUSE_SUMMARY_FILENAME, mouse_fields, mouse_summaries)
        write_csv(
            staging / TECHNICAL_AGREEMENT_FILENAME,
            agreement_fields,
            technical_agreement,
        )
        try:
            if str(REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(REPO_ROOT))
            from ifquant.adapters import (  # pylint: disable=import-outside-toplevel
                MeasurementAdapterError,
                write_measurement_records_jsonl,
            )

            write_measurement_records_jsonl(
                staging / MEASUREMENT_RECORDS_FILENAME, measurement_records
            )
        except MeasurementAdapterError as exc:
            raise ContractError(
                f"Schema-v2 measurement-record publication failed: {exc}"
            ) from exc

        output_artifacts = _review_output_artifacts(staging)
        audit: dict[str, Any] = {
            "schema_version": "1.0.0",
            "audit_type": "he_blinded_review_aggregation",
            "status": "ACCEPTED_REVIEW_AGGREGATED_DESCRIPTIVE_ONLY",
            "created_utc": utc_now(),
            "study_id": study["study_id"],
            "rubric_id": rubric["rubric_id"],
            "review_contract": {
                "review_unit": "blinded_whole_section",
                "locked_row_count": 8,
                "validated_row_count": len(blinded_rows),
                "locked_header": list(LOCKED_REVIEW_FIELDS),
                "header_sha256": canonical_value_sha256(
                    list(LOCKED_REVIEW_FIELDS)
                ),
                "all_required_cells_complete": True,
                "all_review_timestamps_explicit_utc": True,
                "unblinding_performed_after_full_validation": True,
            },
            "measurement_record_contract": {
                "schema_version": "2.0.0",
                "schema_sha256": input_hashes["measurement_record_schema"],
                "track": "he_pathology",
                "record_level": "section",
                "measurement_profile_id": measurement_profile_id,
                "measurement_profile_sha256": measurement_profile_sha256,
                "record_count": len(measurement_records),
                "measured_record_count": len(measured_records),
                "explicit_nonmeasured_record_count": (
                    len(measurement_records) - len(measured_records)
                ),
                "measured_records_aggregation_eligibility_checked": True,
                "estimand_scope": "observed_units",
                "review_status": "accepted",
            },
            "statistical_policy": {
                "biological_unit": "mouse",
                "technical_sections_per_mouse": 2,
                "technical_sections_are_biological_replicates": False,
                "ordinal_scalar_composite_emitted": False,
                "retained_descriptors": [
                    "ordered_section_values",
                    "minimum_observed_rank",
                    "maximum_observed_rank",
                    "exact_agreement",
                ],
                "group_inference_supported": False,
            },
            "input_artifacts": [
                {"role": role, "sha256": digest}
                for role, digest in input_hashes.items()
            ],
            "output_artifacts": output_artifacts,
            "audit_payload_sha256": "",
        }
        audit["audit_payload_sha256"] = _audit_payload_sha256(audit)
        # The audit is deliberately the last artifact written in the staging package.
        write_json(staging / AGGREGATION_AUDIT_FILENAME, audit)
        validate_review_aggregation_audit(staging)

        if _before_publish is not None:
            _before_publish()
        for role, path in input_paths.items():
            require(
                sha256_file(path) == input_hashes[role],
                f"Aggregation input changed before publication: {role}",
            )
        validate_review_aggregation_audit(staging)
        require(
            not output_root.exists(),
            f"Refusing to overwrite existing output: {output_root}",
        )
        staging.rename(output_root)
        staging = None
        return validate_review_aggregation_audit(output_root)
    finally:
        if staging is not None and staging.exists():
            require(
                staging.parent == output_root.parent
                and staging.name.startswith(f".{output_root.name}.staging-"),
                "Refusing to clean an unexpected staging path.",
            )
            shutil.rmtree(staging)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Validate and print H&E stage status.")
    status_parser.add_argument("--study", type=Path, default=DEFAULT_STUDY)
    status_parser.add_argument(
        "--r1-root",
        type=Path,
        help="Override approved_packages.r1_root from the selected study contract.",
    )
    status_parser.add_argument(
        "--h4-root",
        type=Path,
        help=(
            "Override approved_packages.h4_development_root from the selected "
            "study contract."
        ),
    )
    status_parser.add_argument("--output", type=Path)

    build_parser = subparsers.add_parser(
        "build-review", help="Build the blinded whole-section pathology review package."
    )
    build_parser.add_argument("--study", type=Path, default=DEFAULT_STUDY)
    build_parser.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    build_parser.add_argument(
        "--r1-root",
        type=Path,
        help="Override approved_packages.r1_root from the selected study contract.",
    )
    build_parser.add_argument(
        "--h4-root",
        type=Path,
        help=(
            "Override approved_packages.h4_development_root from the selected "
            "study contract."
        ),
    )
    build_parser.add_argument("--output-root", type=Path, default=DEFAULT_REVIEW_OUTPUT)

    aggregate_parser = subparsers.add_parser(
        "aggregate-review",
        help=(
            "Validate a complete blinded review, then publish descriptive "
            "section/mouse outputs and schema-v2 records."
        ),
    )
    aggregate_parser.add_argument("--review-csv", type=Path, required=True)
    aggregate_parser.add_argument("--study", type=Path, default=DEFAULT_STUDY)
    aggregate_parser.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    aggregate_parser.add_argument(
        "--profile", type=Path, default=DEFAULT_REPO_PROFILE
    )
    aggregate_parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "status":
            result = pipeline_status(args.study, args.r1_root, args.h4_root)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                write_json(args.output, result)
        elif args.command == "build-review":
            result = build_review_package(
                args.output_root, args.study, args.rubric, args.r1_root, args.h4_root
            )
        else:
            result = aggregate_review(
                args.output_root,
                args.review_csv,
                args.study,
                args.rubric,
                args.profile,
            )
        print(json.dumps(result, indent=2))
        return 0
    except ContractError as error:
        print(f"H&E PIPELINE CONTRACT ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
