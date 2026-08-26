#!/usr/bin/env python3
"""Validate and render the canonical IFQuant-Lung project authority."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY_ROOT / "authority" / "project_state.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "docs" / "generated" / "AUTHORITY_STATUS.md"

WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^a-z0-9])(?:[a-z]:[\\/]|\\\\)")
POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[=\s\"'(:])/(?!/)")
HOME_PATH = re.compile(r"(?i)(?:^|[\\/])(?:users|home)[\\/][^\\/\s]+")
LOCAL_FILE_URI = re.compile(r"(?i)file://")
HOME_SHORTHAND = re.compile(
    r"(?i)(?:^|[=\s\"'(:])(?:~[\\/]|(?:\$(?:\{?home\}?|env:userprofile)|%(?:userprofile|homepath)%)(?:[\\/]|$))"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """Raised when the authority payload violates the repository contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    _require(isinstance(value, dict), f"{location} must be an object")
    return value


def _require_sequence(value: Any, location: str) -> Sequence[Any]:
    _require(isinstance(value, list), f"{location} must be an array")
    return value


def _require_keys(mapping: Mapping[str, Any], keys: Iterable[str], location: str) -> None:
    missing = [key for key in keys if key not in mapping]
    _require(not missing, f"{location} is missing required keys: {', '.join(missing)}")


def _walk_strings(value: Any, location: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield location, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_strings(key, f"{location}.<key>")
            yield from _walk_strings(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{location}[{index}]")


def validate_privacy(payload: Mapping[str, Any]) -> None:
    """Reject workstation-specific paths and local file references."""

    violations: list[str] = []
    for location, value in _walk_strings(payload):
        stripped = value.strip()
        if (
            WINDOWS_ABSOLUTE_PATH.search(stripped)
            or POSIX_ABSOLUTE_PATH.search(stripped)
            or HOME_PATH.search(stripped)
            or LOCAL_FILE_URI.search(stripped)
            or HOME_SHORTHAND.search(stripped)
        ):
            violations.append(location)
    _require(
        not violations,
        "privacy validation rejected absolute or workstation-local paths at: "
        + ", ".join(violations),
    )


def _index_by_id(items: Sequence[Any], location: str) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        mapping = _require_mapping(item, f"{location}[{index}]")
        identifier = mapping.get("id")
        _require(isinstance(identifier, str) and identifier, f"{location}[{index}].id must be a non-empty string")
        _require(identifier not in indexed, f"duplicate id {identifier!r} in {location}")
        indexed[identifier] = mapping
    return indexed


def validate_contract(payload: Mapping[str, Any]) -> None:
    """Validate critical project invariants without third-party packages."""

    root = _require_mapping(payload, "$")
    root_keys = (
            "$schema",
            "schema_version",
            "authority_revision",
            "study_id",
            "title",
            "as_of",
            "authority",
            "evidence",
            "analysis_population",
            "study_design",
            "claim_boundary",
            "modalities",
            "project_gates",
    )
    _require_keys(root, root_keys, "$")
    _require(set(root) == set(root_keys), "$ contains unknown keys")
    _require(root["schema_version"] == "1.1.0", "unsupported schema_version")
    _require(
        root["$schema"] == "../schemas/project-state.schema.json",
        "$schema must reference the repository schema",
    )
    _require(
        isinstance(root["authority_revision"], str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.[1-9]\d*", root["authority_revision"]),
        "authority_revision must have YYYY-MM-DD.N form",
    )

    authority = _require_mapping(root["authority"], "$.authority")
    expected_paths = {
        "canonical_source": "authority/project_state.json",
        "generated_status": "docs/generated/AUTHORITY_STATUS.md",
        "schema": "schemas/project-state.schema.json",
    }
    for key, expected in expected_paths.items():
        _require(authority.get(key) == expected, f"authority.{key} must be {expected!r}")

    evidence = _require_sequence(root["evidence"], "$.evidence")
    evidence_by_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(evidence):
        record = _require_mapping(item, f"$.evidence[{index}]")
        _require_keys(record, ("artifact_id", "sha256", "verified_on", "verification"), f"$.evidence[{index}]")
        identifier = record.get("artifact_id")
        _require(isinstance(identifier, str) and identifier, f"$.evidence[{index}].artifact_id is required")
        _require(identifier not in evidence_by_id, f"duplicate evidence artifact_id {identifier!r}")
        _require(bool(SHA256.fullmatch(str(record.get("sha256", "")))), f"$.evidence[{index}].sha256 must be lowercase SHA-256")
        evidence_by_id[identifier] = record
    _require(
        set(evidence_by_id)
        == {"settled_csv_release_v1_0", "research_report_2026", "supplementary_materials_2026", "poster_final_90x120cm"},
        "evidence must bind exactly the four audited primary artifacts",
    )
    _require(
        evidence_by_id["settled_csv_release_v1_0"]["sha256"]
        == "0bd690fdb37ca763810c7e8451a3d92f6ef951e4bfa4e69c6bc27512ae8d1dd7",
        "settled CSV release evidence hash changed",
    )

    population = _require_mapping(root["analysis_population"], "$.analysis_population")
    _require(population.get("terminal_endpoint_population") == "DAY_28_IMAGED_SURVIVORS", "terminal endpoint must remain survivor-qualified")
    _require(population.get("additional_heterozygous_infected_deaths_disclosed") == 2, "two disclosed heterozygous infected deaths must remain recorded")
    _require(population.get("death_days") == "8-9_POST_INFECTION", "attrition timing changed")
    _require(population.get("survival_analysis_performed") is False, "no survival analysis was performed")

    design = _require_mapping(root["study_design"], "$.study_design")
    _require(design.get("factorial_structure") == "CROSSED_2X2", "study design must remain crossed 2x2")
    _require(design.get("population_scope") == "DAY_28_IMAGED_SURVIVORS", "study-design counts must be survivor-qualified")
    _require(design.get("terminal_imaged_animals_per_cell") == 1, "terminal_imaged_animals_per_cell must record the analyzed n=1/cell set")
    _require(design.get("biological_unit") == "mouse", "the biological unit must be mouse")
    _require(
        design.get("inference_status") == "DESCRIPTIVE_ONLY_NO_GROUP_INFERENCE",
        "the present design cannot be promoted to group inference",
    )

    claims = _require_mapping(root["claim_boundary"], "$.claim_boundary")
    _require(len(_require_sequence(claims.get("permitted"), "$.claim_boundary.permitted")) > 0, "permitted claims cannot be empty")
    _require(len(_require_sequence(claims.get("prohibited"), "$.claim_boundary.prohibited")) > 0, "prohibited claims cannot be empty")

    modalities = _index_by_id(_require_sequence(root["modalities"], "$.modalities"), "$.modalities")
    required_modalities = {"confocal_selected_fields", "he_histology", "wsi_threshold_pilot"}
    _require(set(modalities) == required_modalities, "modalities must contain exactly the three authoritative domains")
    for identifier, modality in modalities.items():
        _require(isinstance(modality.get("evidence_basis"), str) and modality["evidence_basis"], f"{identifier} requires an evidence_basis")

    confocal = modalities["confocal_selected_fields"]
    _require(confocal.get("status") == "SETTLED_WITH_COMPARABILITY_EXCEPTION", "confocal status lost its comparability exception")
    release = _require_mapping(confocal.get("release"), "confocal.release")
    _require(release.get("name") == "settled_external_csv_release", "unexpected confocal release name")
    _require(release.get("version") == "1.0", "unexpected confocal release version")
    _require(release.get("state") == "SETTLED", "confocal release must be settled")
    _require(release.get("expected_fields") == 80, "confocal expected_fields must be 80")
    _require(release.get("quantified_fields") == 80, "confocal quantified_fields must be 80")
    _require(release.get("sampling_frame") == "selected_fields_nonprobability", "confocal sampling limitation must remain explicit")

    exceptions = _index_by_id(
        [
            {**_require_mapping(item, "confocal.field_exceptions[]"), "id": item.get("field_id")}
            for item in _require_sequence(confocal.get("field_exceptions"), "confocal.field_exceptions")
        ],
        "confocal.field_exceptions",
    )
    _require(set(exceptions) == {"M4-2_LEFT_F06", "M4-1_RIGHT_F07"}, "confocal field exceptions are incomplete")
    override = exceptions["M4-2_LEFT_F06"]
    _require(override.get("release_state") == "WHOLE_FIELD_TISSUE_OVERRIDE_INCLUDED", "M4-2 LEFT F06 override state changed")
    _require(override.get("included_in_quantified_count") is True, "M4-2 LEFT F06 must remain included in the 80-field release")
    _require(override.get("tissue_denominator_um2") == 405000, "M4-2 LEFT F06 denominator must be 405000 um2")
    _require(
        override.get("comparability") == "NONCOMPARABLE_PENDING_COMPARABLE_TISSUE_ROI",
        "M4-2 LEFT F06 must remain non-comparable pending a comparable tissue ROI",
    )
    partial = exceptions["M4-1_RIGHT_F07"]
    _require(partial.get("release_state") == "PARTIAL_FIELD_INCLUDED", "M4-1 RIGHT F07 partial state changed")
    _require(partial.get("included_in_quantified_count") is True, "M4-1 RIGHT F07 must remain included in the 80-field release")
    _require(partial.get("comparability") == "PARTIAL_FLAG_REQUIRED", "M4-1 RIGHT F07 must retain its partial flag")

    he = modalities["he_histology"]
    _require(he.get("status") == "ENGINEERING_QC_ONLY", "H&E cannot be promoted beyond engineering/QC")
    stages = {
        item.get("range"): item.get("status")
        for item in _require_sequence(he.get("stages"), "he.stages")
        if isinstance(item, dict)
    }
    _require(stages == {"H0-H3": "ENGINEERING_QC_ONLY", "H4+": "UNVALIDATED_BLOCKED"}, "H&E stage boundary is invalid")

    wsi = modalities["wsi_threshold_pilot"]
    _require(wsi.get("status") == "ENGINEERING_PILOT_ONLY", "WSI must remain an engineering pilot")
    _require(wsi.get("tile_count") == 6, "WSI pilot must record exactly six tiles")

    gates = _index_by_id(_require_sequence(root["project_gates"], "$.project_gates"), "$.project_gates")
    expected_gates = {
        "G-CONTRACT-INTEGRATION": "ENGINEERING_COMPLETE",
        "G-CONFOCAL-DENOMINATOR": "OPEN_SCIENTIFIC_BLOCKER",
        "G-SAMPLING-INFERENCE": "OPEN_SCIENTIFIC_BLOCKER",
        "G-HE-VALIDATION": "OPEN_SCIENTIFIC_BLOCKER",
        "G-WSI-VALIDATION": "OPEN_SCIENTIFIC_BLOCKER",
        "G-SEGMENTATION-VALIDATION": "OPEN_SCIENTIFIC_BLOCKER",
    }
    _require(set(gates) == set(expected_gates), "project gates must contain the complete authoritative gate set")
    for gate_id, expected_status in expected_gates.items():
        _require(
            gates[gate_id].get("status") == expected_status,
            f"{gate_id} must have status {expected_status}",
        )

    validate_privacy(root)


def load_authority(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load authority JSON {path}: {exc}") from exc
    validate_contract(payload)
    return payload


def _escape_table(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _bullet_lines(items: Sequence[str]) -> list[str]:
    return [f"- {item}" for item in items]


def render_authority(payload: Mapping[str, Any]) -> str:
    """Render validated authority data to deterministic Markdown."""

    validate_contract(payload)
    authority = payload["authority"]
    design = payload["study_design"]
    claims = payload["claim_boundary"]
    population = payload["analysis_population"]
    modalities = {item["id"]: item for item in payload["modalities"]}
    confocal = modalities["confocal_selected_fields"]
    release = confocal["release"]
    he = modalities["he_histology"]
    wsi = modalities["wsi_threshold_pilot"]

    lines = [
        f"# {payload['title']}",
        "",
        "> Generated from `authority/project_state.json`; do not edit this file by hand.",
        "",
        f"Authority revision: `{payload['authority_revision']}`",
        f"State date: `{payload['as_of']}`",
        f"Contract version: `{payload['schema_version']}`",
        "",
        "## Authority",
        "",
        f"- Canonical source: `{authority['canonical_source']}`",
        f"- Schema: `{authority['schema']}`",
        f"- Generated view: `{authority['generated_status']}`",
        f"- Update rule: {authority['rule']}",
        "",
        "## Audited evidence bindings",
        "",
        "| Artifact ID | SHA-256 | Verified | Inspection |",
        "| --- | --- | --- | --- |",
    ]
    for item in payload["evidence"]:
        lines.append(
            f"| `{item['artifact_id']}` | `{item['sha256']}` | `{item['verified_on']}` | "
            f"`{item['verification']}` |"
        )

    lines.extend([
        "",
        "## Current modality boundary",
        "",
        "| Modality | State | Authoritative scope |",
        "| --- | --- | --- |",
    ])
    for item in payload["modalities"]:
        lines.append(f"| {_escape_table(item['label'])} | `{item['status']}` | {_escape_table(item['scope'])} |")

    lines.extend(
        [
            "",
            "## Settled confocal release",
            "",
            f"- Release: `{release['name']}` v{release['version']} (`{release['state']}`)",
            f"- Reconciliation: **{release['quantified_fields']} / {release['expected_fields']} fields quantified**",
            f"- Endpoint: `{release['endpoint']}`",
            f"- Sampling frame: `{release['sampling_frame']}`",
            f"- Scope: {confocal['scope']}",
            "",
            "### Included field exceptions",
            "",
            "| Field | Release state | Tissue denominator (µm²) | Comparability | Constraint |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for item in confocal["field_exceptions"]:
        denominator = item.get("tissue_denominator_um2", "—")
        lines.append(
            f"| `{item['field_id']}` | `{item['release_state']}` | {_escape_table(denominator)} | "
            f"`{item['comparability']}` | {_escape_table(item['constraint'])} |"
        )

    lines.extend(["", "## Scientific claim boundary", "", "### Permitted", ""])
    lines.extend(_bullet_lines(claims["permitted"]))
    lines.extend(["", "### Not permitted", ""])
    lines.extend(_bullet_lines(claims["prohibited"]))

    lines.extend(
        [
            "",
            "## Study-design boundary",
            "",
            f"- Terminal tissue population: `{population['terminal_endpoint_population']}`",
            f"- Additional heterozygous infected deaths disclosed: **{population['additional_heterozygous_infected_deaths_disclosed']}** ({population['death_days']})",
            f"- Survival analysis performed: **{str(population['survival_analysis_performed']).lower()}**",
            f"- Population constraint: {population['constraint']}",
            "",
            f"- Structure: `{design['factorial_structure']}`",
            f"- Biological unit: `{design['biological_unit']}`",
            f"- Terminal imaged survivors per genotype-by-condition cell: **{design['terminal_imaged_animals_per_cell']}**",
            f"- Inference state: `{design['inference_status']}`",
            "",
        ]
    )
    lines.extend(_bullet_lines(design["reasons"]))

    he_stages = {item["range"]: item["status"] for item in he["stages"]}
    lines.extend(
        [
            "",
            "## H&E and WSI boundaries",
            "",
            "| Workstream | Current state | Promotion gate |",
            "| --- | --- | --- |",
            f"| H&E H0-H3 | `{he_stages['H0-H3']}` | {_escape_table(he['promotion_gate'])} |",
            f"| H&E H4+ | `{he_stages['H4+']}` | {_escape_table(he['promotion_gate'])} |",
            f"| WSI ({wsi['tile_count']} tiles) | `{wsi['status']}` | {_escape_table(wsi['promotion_gate'])} |",
            "",
            "## Promotion gates",
            "",
            "| Gate | State | Acceptance condition |",
            "| --- | --- | --- |",
        ]
    )
    for gate in payload["project_gates"]:
        lines.append(f"| `{gate['id']}` | `{gate['status']}` | {_escape_table(gate['acceptance'])} |")

    return "\n".join(lines) + "\n"


def _resolve_cli_path(value: str, default: Path) -> Path:
    if value == str(default):
        return default
    candidate = Path(value)
    return candidate if candidate.is_absolute() else REPOSITORY_ROOT / candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="authority JSON path")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="generated Markdown path")
    parser.add_argument("--check", action="store_true", help="fail if output is missing or stale")
    parser.add_argument("--stdout", action="store_true", help="print rendered Markdown without writing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check and args.stdout:
        print("error: --check and --stdout are mutually exclusive", file=sys.stderr)
        return 2

    source = _resolve_cli_path(args.source, DEFAULT_SOURCE)
    output = _resolve_cli_path(args.output, DEFAULT_OUTPUT)
    try:
        rendered = render_authority(load_authority(source))
    except ContractError as exc:
        print(f"authority contract error: {exc}", file=sys.stderr)
        return 1

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        try:
            current = output.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"authority status is missing or unreadable: {output}: {exc}", file=sys.stderr)
            return 1
        if current != rendered:
            print(
                f"authority status is stale: run {Path(__file__).name} and commit {output}",
                file=sys.stderr,
            )
            return 1
        print(f"authority status is current: {output}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    print(f"rendered authority status: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
