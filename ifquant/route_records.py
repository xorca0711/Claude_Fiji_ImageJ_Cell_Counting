"""Opt-in integration of legacy aggregation rows with measurement records.

The aggregation CSVs are intentionally generic and cannot identify endpoint
semantics from column suffixes.  This module therefore accepts only an
explicit, panel-scoped specification.  It maps named source columns to the
versioned measurement-record adapters, binds the exact aggregation inputs by
SHA-256, and checks aggregation eligibility before the caller pools rows.

This is a software-contract bridge.  It does not turn an exploratory endpoint
or a non-probability sample into biological validation.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .adapters import (
    MeasurementAdapterError,
    MeasurementRecordContext,
    RatioColumnMapping,
    build_ratio_measurement_record,
)
from .contracts import IDENTIFIER_FIELDS, require_aggregation_batch_eligible


SPEC_VERSION = "1.0.0"
SUPPORTED_ROUTE_LEVELS = {
    "area_wsi": "slide",
    "cell_confocal": "region",
}
MISSING_TOKENS = frozenset({"", "NA", "N/A", "UNKNOWN"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RouteMeasurementSpecError(MeasurementAdapterError):
    """Raised when an aggregation mapping would require invented semantics."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RouteMeasurementSpecError(message)


def _object(value: Any, location: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{location} must be an object")
    return value


def _exact_fields(
    value: Mapping[str, Any], expected: set[str], location: str
) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    _require(not missing, f"{location} is missing: {', '.join(missing)}")
    _require(not extra, f"{location} has unknown fields: {', '.join(extra)}")


def _nonempty(value: Any, location: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()),
        f"{location} must be a non-empty string",
    )
    return value.strip()


def _sha256(value: Any, location: str) -> str:
    token = _nonempty(value, location)
    _require(
        SHA256_RE.fullmatch(token) is not None,
        f"{location} must be a lowercase SHA-256",
    )
    return token


def _portable_scalar(value: Any, location: str) -> str:
    token = _nonempty(value, location)
    _require(
        "\t" not in token and "\r" not in token and "\n" not in token,
        f"{location} cannot contain tabs or line breaks",
    )
    return token


def parse_measurement_record_spec(
    document: Any,
    *,
    expected_track: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize one explicit aggregation-record specification."""

    root = _object(document, "measurement record spec")
    _exact_fields(
        root,
        {
            "spec_version",
            "track",
            "record_level",
            "panel_column",
            "identifier_columns",
            "target_estimand",
            "profiles",
        },
        "measurement record spec",
    )
    _require(
        root["spec_version"] == SPEC_VERSION,
        f"measurement record spec_version must be {SPEC_VERSION}",
    )
    track = _nonempty(root["track"], "measurement record spec.track")
    _require(
        track in SUPPORTED_ROUTE_LEVELS,
        "measurement record spec.track must be area_wsi or cell_confocal",
    )
    if expected_track is not None:
        _require(
            track == expected_track,
            f"measurement record spec.track={track!r} does not match "
            f"aggregation input track {expected_track!r}",
        )
    record_level = _nonempty(
        root["record_level"], "measurement record spec.record_level"
    )
    _require(
        record_level == SUPPORTED_ROUTE_LEVELS[track],
        f"{track} aggregation input must emit "
        f"record_level={SUPPORTED_ROUTE_LEVELS[track]!r}",
    )
    panel_column = _portable_scalar(
        root["panel_column"], "measurement record spec.panel_column"
    )
    target_estimand = _nonempty(
        root["target_estimand"], "measurement record spec.target_estimand"
    )
    _require(
        target_estimand in {"observed_units", "whole_section", "whole_lung"},
        "measurement record spec.target_estimand is unsupported",
    )
    route_estimand = {
        "area_wsi": "whole_section",
        "cell_confocal": "observed_units",
    }[track]
    _require(
        target_estimand == route_estimand,
        f"{track} Stage 4 integration requires "
        f"target_estimand={route_estimand!r}",
    )

    identifier_columns = _object(
        root["identifier_columns"], "measurement record spec.identifier_columns"
    )
    _exact_fields(
        identifier_columns,
        set(IDENTIFIER_FIELDS),
        "measurement record spec.identifier_columns",
    )
    normalized_identifiers: dict[str, str | None] = {}
    for name in IDENTIFIER_FIELDS:
        column = identifier_columns[name]
        normalized_identifiers[name] = (
            None
            if column is None
            else _portable_scalar(
                column, f"measurement record spec.identifier_columns.{name}"
            )
        )
    _require(
        normalized_identifiers["mouse_id"] is not None,
        "measurement record spec must map mouse_id explicitly",
    )

    raw_profiles = root["profiles"]
    _require(
        isinstance(raw_profiles, list) and bool(raw_profiles),
        "measurement record spec.profiles must be a non-empty array",
    )
    normalized_profiles: list[dict[str, Any]] = []
    seen_panels: set[str] = set()
    for profile_index, raw_profile in enumerate(raw_profiles):
        location = f"measurement record spec.profiles[{profile_index}]"
        profile = _object(raw_profile, location)
        _exact_fields(
            profile,
            {
                "panel",
                "measurement_profile_id",
                "channel_signature",
                "segmentation_model",
                "sampling",
                "compartment",
                "provenance",
                "qc",
                "row_constraints",
                "endpoints",
            },
            location,
        )
        panel = _portable_scalar(profile["panel"], f"{location}.panel")
        _require(panel not in seen_panels, f"duplicate profile panel {panel!r}")
        seen_panels.add(panel)

        row_constraints = _object(
            profile["row_constraints"], f"{location}.row_constraints"
        )
        normalized_constraints: dict[str, str] = {}
        for column, expected in row_constraints.items():
            normalized_column = _portable_scalar(
                column, f"{location}.row_constraints column"
            )
            _require(
                normalized_column not in normalized_constraints,
                f"{location}.row_constraints contains duplicate normalized "
                f"column {normalized_column!r}",
            )
            normalized_constraints[normalized_column] = _portable_scalar(
                expected, f"{location}.row_constraints[{column!r}]"
            )

        provenance = _object(profile["provenance"], f"{location}.provenance")
        _exact_fields(
            provenance,
            {
                "code_revision",
                "config_sha256",
                "measurement_profile_sha256",
                "run_id",
                "additional_inputs",
            },
            f"{location}.provenance",
        )
        additional_inputs = provenance["additional_inputs"]
        _require(
            isinstance(additional_inputs, list),
            f"{location}.provenance.additional_inputs must be an array",
        )
        normalized_inputs: list[dict[str, str]] = []
        seen_roles: set[str] = set()
        for input_index, raw_input in enumerate(additional_inputs):
            input_location = (
                f"{location}.provenance.additional_inputs[{input_index}]"
            )
            input_record = _object(raw_input, input_location)
            _exact_fields(input_record, {"role", "sha256"}, input_location)
            role = _portable_scalar(input_record["role"], f"{input_location}.role")
            _require(role not in seen_roles, f"duplicate provenance input role {role!r}")
            seen_roles.add(role)
            normalized_inputs.append(
                {
                    "role": role,
                    "sha256": _sha256(
                        input_record["sha256"], f"{input_location}.sha256"
                    ),
                }
            )

        sampling = _object(profile["sampling"], f"{location}.sampling")
        if track == "area_wsi":
            inclusion_probability = sampling.get("inclusion_probability")
            _require(
                sampling.get("design") == "exhaustive"
                and isinstance(inclusion_probability, (int, float))
                and not isinstance(inclusion_probability, bool)
                and inclusion_probability == 1
                and sampling.get("estimand_scope") == "whole_section",
                f"{location}.sampling must declare exhaustive whole-section "
                "coverage for indexed WSI Stage 4 integration",
            )

        endpoints = profile["endpoints"]
        _require(
            isinstance(endpoints, list) and bool(endpoints),
            f"{location}.endpoints must be a non-empty array",
        )
        normalized_endpoints: list[dict[str, Any]] = []
        endpoint_keys: set[tuple[str, str]] = set()
        for endpoint_index, raw_endpoint in enumerate(endpoints):
            endpoint_location = f"{location}.endpoints[{endpoint_index}]"
            endpoint = _object(raw_endpoint, endpoint_location)
            _exact_fields(
                endpoint,
                {
                    "calculation",
                    "evaluability",
                    "endpoint_id",
                    "reference_space_id",
                    "numerator_column",
                    "numerator_unit",
                    "denominator_column",
                    "denominator_unit",
                    "result_unit",
                    "value_column",
                },
                endpoint_location,
            )
            _require(
                endpoint["calculation"] == "ratio",
                f"{endpoint_location}.calculation must be ratio",
            )
            _require(
                endpoint["evaluability"] == "measured",
                f"{endpoint_location}.evaluability must be measured for aggregation",
            )
            normalized_endpoint = {
                "calculation": "ratio",
                "evaluability": "measured",
                "endpoint_id": _portable_scalar(
                    endpoint["endpoint_id"], f"{endpoint_location}.endpoint_id"
                ),
                "reference_space_id": _portable_scalar(
                    endpoint["reference_space_id"],
                    f"{endpoint_location}.reference_space_id",
                ),
                "numerator_column": _portable_scalar(
                    endpoint["numerator_column"],
                    f"{endpoint_location}.numerator_column",
                ),
                "numerator_unit": _portable_scalar(
                    endpoint["numerator_unit"],
                    f"{endpoint_location}.numerator_unit",
                ),
                "denominator_column": _portable_scalar(
                    endpoint["denominator_column"],
                    f"{endpoint_location}.denominator_column",
                ),
                "denominator_unit": _portable_scalar(
                    endpoint["denominator_unit"],
                    f"{endpoint_location}.denominator_unit",
                ),
                "result_unit": _portable_scalar(
                    endpoint["result_unit"], f"{endpoint_location}.result_unit"
                ),
                "value_column": (
                    None
                    if endpoint["value_column"] is None
                    else _portable_scalar(
                        endpoint["value_column"],
                        f"{endpoint_location}.value_column",
                    )
                ),
            }
            endpoint_key = (
                normalized_endpoint["endpoint_id"],
                normalized_endpoint["reference_space_id"],
            )
            _require(
                endpoint_key not in endpoint_keys,
                f"duplicate endpoint/reference-space mapping {endpoint_key!r} "
                f"for panel {panel!r}",
            )
            endpoint_keys.add(endpoint_key)
            normalized_endpoints.append(normalized_endpoint)

        normalized_profiles.append(
            {
                "panel": panel,
                "measurement_profile_id": _portable_scalar(
                    profile["measurement_profile_id"],
                    f"{location}.measurement_profile_id",
                ),
                "channel_signature": profile["channel_signature"],
                "segmentation_model": profile["segmentation_model"],
                "sampling": sampling,
                "compartment": profile["compartment"],
                "provenance": {
                    "code_revision": _portable_scalar(
                        provenance["code_revision"],
                        f"{location}.provenance.code_revision",
                    ),
                    "config_sha256": _sha256(
                        provenance["config_sha256"],
                        f"{location}.provenance.config_sha256",
                    ),
                    "measurement_profile_sha256": _sha256(
                        provenance["measurement_profile_sha256"],
                        f"{location}.provenance.measurement_profile_sha256",
                    ),
                    "run_id": _portable_scalar(
                        provenance["run_id"], f"{location}.provenance.run_id"
                    ),
                    "additional_inputs": normalized_inputs,
                },
                "qc": profile["qc"],
                "row_constraints": normalized_constraints,
                "endpoints": normalized_endpoints,
            }
        )

    return {
        "spec_version": SPEC_VERSION,
        "track": track,
        "record_level": record_level,
        "panel_column": panel_column,
        "identifier_columns": normalized_identifiers,
        "target_estimand": target_estimand,
        "profiles": normalized_profiles,
    }


def load_measurement_record_spec(
    payload: bytes,
    *,
    expected_track: str | None = None,
) -> dict[str, Any]:
    """Decode a byte snapshot and validate its explicit route mapping."""

    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RouteMeasurementSpecError(
            f"measurement record spec is unreadable JSON: {exc}"
        ) from exc
    return parse_measurement_record_spec(document, expected_track=expected_track)


def _row_value(row: Mapping[str, Any], column: str, location: str) -> str:
    _require(column in row, f"{location} column {column!r} is absent")
    raw = row[column]
    _require(isinstance(raw, str), f"{location} column {column!r} must be text")
    token = raw.strip()
    _require(
        token.upper() not in MISSING_TOKENS,
        f"{location} column {column!r} is blank or unknown",
    )
    return token


def _identifiers(
    row: Mapping[str, Any],
    identifier_columns: Mapping[str, str | None],
    row_number: int,
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for identifier in IDENTIFIER_FIELDS:
        column = identifier_columns[identifier]
        result[identifier] = (
            None
            if column is None
            else _row_value(
                row,
                column,
                f"source row {row_number} identifier {identifier}",
            )
        )
    return result


def _normalized_source_inputs(
    source_inputs: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    _require(
        isinstance(source_inputs, Sequence)
        and not isinstance(source_inputs, (str, bytes))
        and bool(source_inputs),
        "source_inputs must be a non-empty sequence",
    )
    result: list[dict[str, str]] = []
    seen_roles: set[str] = set()
    for index, raw in enumerate(source_inputs):
        location = f"source_inputs[{index}]"
        item = _object(raw, location)
        _exact_fields(item, {"role", "sha256"}, location)
        role = _portable_scalar(item["role"], f"{location}.role")
        _require(role not in seen_roles, f"duplicate source input role {role!r}")
        seen_roles.add(role)
        result.append({"role": role, "sha256": _sha256(item["sha256"], f"{location}.sha256")})
    return result


def _wsi_row_assertions(
    row: Mapping[str, Any], profile: Mapping[str, Any], panel: str, row_number: int
) -> list[dict[str, str]]:
    """Bind record semantics to the indexed WSI provenance carried by Stage 3."""

    expected = {
        "measurement_profile_sha256": profile["provenance"][
            "measurement_profile_sha256"
        ],
        "resolved_config_sha256": profile["provenance"]["config_sha256"],
        "stage2_script_sha256": profile["provenance"]["code_revision"],
    }
    for column, value in expected.items():
        observed = _row_value(
            row, column, f"WSI source row {row_number} provenance"
        )
        _require(
            observed == value,
            f"WSI source row {row_number} {column} disagrees with the explicit "
            "measurement profile",
        )

    signature = profile["channel_signature"]
    _require(
        isinstance(signature, list) and bool(signature),
        f"profile {panel!r} channel_signature must be a non-empty array",
    )
    signature_tokens: list[str] = []
    for index, channel in enumerate(signature):
        channel = _object(channel, f"profile {panel!r} channel_signature[{index}]")
        _exact_fields(
            channel,
            {"index", "label", "role"},
            f"profile {panel!r} channel_signature[{index}]",
        )
        channel_index = channel["index"]
        _require(
            isinstance(channel_index, int)
            and not isinstance(channel_index, bool)
            and channel_index >= 1,
            f"profile {panel!r} channel index must be a positive integer",
        )
        label = _portable_scalar(
            channel["label"], f"profile {panel!r} channel label"
        )
        _portable_scalar(channel["role"], f"profile {panel!r} channel role")
        signature_tokens.append(f"C{channel_index}-{label}")
    expected_signature = f"{panel}=" + "_".join(signature_tokens)
    observed_signature = _row_value(
        row,
        "ordered_channel_signature",
        f"WSI source row {row_number} channel signature",
    )
    _require(
        observed_signature == expected_signature,
        f"WSI source row {row_number} ordered_channel_signature disagrees with "
        f"profile {panel!r}",
    )

    bound_inputs: list[dict[str, str]] = []
    for role, column in (
        ("stage2_index", "stage2_index_sha256"),
        ("source_package", "source_package_sha256"),
        ("stage1_manifest", "stage1_manifest_sha256"),
        ("tile_manifest", "tile_manifest_sha256"),
    ):
        bound_inputs.append(
            {
                "role": role,
                "sha256": _sha256(
                    _row_value(
                        row, column, f"WSI source row {row_number} provenance"
                    ),
                    f"WSI source row {row_number} {column}",
                ),
            }
        )
    return bound_inputs


def build_preaggregation_measurement_records(
    rows: Sequence[Mapping[str, Any]],
    spec: Mapping[str, Any],
    *,
    source_inputs: Sequence[Mapping[str, Any]],
    pool_columns: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[tuple[str, ...], list[dict[str, Any]]]]:
    """Build records and reject every ineligible pool before aggregation.

    The caller supplies the exact columns defining one numerical pooling batch.
    Every source row must select exactly one declared panel profile, every
    profile endpoint is built from explicitly named columns, and each resulting
    batch passes :func:`require_aggregation_batch_eligible` before return.
    """

    _require(bool(rows), "cannot emit measurement records from an empty row set")
    _require(
        isinstance(pool_columns, Sequence)
        and not isinstance(pool_columns, (str, bytes))
        and bool(pool_columns),
        "pool_columns must be a non-empty sequence of source column names",
    )
    normalized_pool_columns = tuple(
        _portable_scalar(column, f"pool_columns[{index}]")
        for index, column in enumerate(pool_columns)
    )
    _require(
        len(normalized_pool_columns) == len(set(normalized_pool_columns)),
        "pool_columns must be unique",
    )
    normalized_spec = parse_measurement_record_spec(spec)
    normalized_inputs = _normalized_source_inputs(source_inputs)
    profiles = {
        profile["panel"]: profile for profile in normalized_spec["profiles"]
    }
    panel_column = normalized_spec["panel_column"]
    used_panels: set[str] = set()
    records: list[dict[str, Any]] = []
    records_by_pool: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)

    for row_number, row in enumerate(rows, start=1):
        _require(isinstance(row, Mapping), f"source row {row_number} must be an object")
        panel = _row_value(row, panel_column, f"source row {row_number} panel")
        profile = profiles.get(panel)
        _require(
            profile is not None,
            f"source row {row_number} panel {panel!r} has no explicit profile",
        )
        used_panels.add(panel)
        for column, expected in profile["row_constraints"].items():
            observed = _row_value(
                row, column, f"source row {row_number} constraint"
            )
            _require(
                observed == expected,
                f"source row {row_number} column {column!r} does not equal "
                f"the declared constraint {expected!r}",
            )

        row_inputs = list(normalized_inputs)
        if normalized_spec["track"] == "area_wsi":
            row_inputs.extend(_wsi_row_assertions(row, profile, panel, row_number))
        roles = [item["role"] for item in row_inputs]
        _require(
            len(roles) == len(set(roles)),
            "route-bound provenance input roles must be unique",
        )
        inputs_by_role = {item["role"]: item["sha256"] for item in row_inputs}
        for declared_input in profile["provenance"]["additional_inputs"]:
            role = declared_input["role"]
            _require(
                role in inputs_by_role,
                f"profile {panel!r} requires provenance input role {role!r}, "
                "but the route did not bind that artifact",
            )
            _require(
                inputs_by_role[role] == declared_input["sha256"],
                f"profile {panel!r} provenance input {role!r} SHA-256 "
                "disagrees with the bound artifact",
            )

        context = MeasurementRecordContext(
            track=normalized_spec["track"],
            record_level=normalized_spec["record_level"],
            identifiers=_identifiers(
                row, normalized_spec["identifier_columns"], row_number
            ),
            measurement_profile_id=profile["measurement_profile_id"],
            channel_signature=profile["channel_signature"],
            segmentation_model=profile["segmentation_model"],
            sampling=profile["sampling"],
            compartment=profile["compartment"],
            provenance={
                "code_revision": profile["provenance"]["code_revision"],
                "config_sha256": profile["provenance"]["config_sha256"],
                "measurement_profile_sha256": profile["provenance"][
                    "measurement_profile_sha256"
                ],
                "inputs": row_inputs,
                "run_id": profile["provenance"]["run_id"],
            },
            qc=profile["qc"],
        )
        pool_key = tuple(
            _row_value(
                row,
                column,
                f"source row {row_number} pooling identity",
            )
            for column in normalized_pool_columns
        )
        for endpoint in profile["endpoints"]:
            mapping = RatioColumnMapping(
                endpoint_id=endpoint["endpoint_id"],
                reference_space_id=endpoint["reference_space_id"],
                numerator_column=endpoint["numerator_column"],
                numerator_unit=endpoint["numerator_unit"],
                denominator_column=endpoint["denominator_column"],
                denominator_unit=endpoint["denominator_unit"],
                result_unit=endpoint["result_unit"],
                value_column=endpoint["value_column"],
            )
            try:
                record = build_ratio_measurement_record(row, mapping, context)
            except MeasurementAdapterError as exc:
                raise RouteMeasurementSpecError(
                    f"source row {row_number}, panel {panel!r}, endpoint "
                    f"{endpoint['endpoint_id']!r}: {exc}"
                ) from exc
            records.append(record)
            records_by_pool[pool_key].append(record)

    unused = sorted(set(profiles) - used_panels)
    _require(
        not unused,
        "measurement record spec contains unused panel profile(s): "
        + ", ".join(unused),
    )
    for pool_key, pool_records in sorted(records_by_pool.items()):
        try:
            # This is deliberately before aggregate_mice() in the production
            # caller.  Probability estimators remain unsupported here; merely
            # naming one in a record never authorizes weighted pooling.
            require_aggregation_batch_eligible(
                pool_records,
                target_estimand=normalized_spec["target_estimand"],
            )
        except MeasurementAdapterError:
            raise
        except ValueError as exc:
            raise RouteMeasurementSpecError(
                f"aggregation pool {pool_key!r} is not eligible: {exc}"
            ) from exc

    return records, dict(records_by_pool)
