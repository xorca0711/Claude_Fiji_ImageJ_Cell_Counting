"""Backend-neutral numerical method contracts for IFQuant.

The existing measurement-record contract describes results and execution
provenance. This module describes the numerical area-fraction kernel and a
separate frozen threshold set that instantiates its threshold parameters.

The split is deliberate:

* the scientific-definition hash contains numerical semantics only;
* the threshold-set hash contains scope-declared intensity cutoffs; and
* the method-instance hash binds those two hashes without hiding backend
  software, channel mapping, governance, or execution provenance inside the
  scientific definition.

This module is intentionally not imported from :mod:`ifquant.__init__`.
Prospective code must not alter the import closure or hashes of the settled
Stage 3/4 aggregation implementation.

Validation proves contract closure and deterministic identity. It does not
authorize an endpoint, establish biological validity, or prove two backends
numerically equivalent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable


SCHEMA_VERSION = "1.0.0"
DEFINITION_SCHEMA_URI = (
    "https://github.com/xorca0711/IFQuant-Lung/"
    "schemas/scientific-measurement-definition.schema.json"
)
THRESHOLD_SET_SCHEMA_URI = (
    "https://github.com/xorca0711/IFQuant-Lung/"
    "schemas/frozen-threshold-set.schema.json"
)
DEFINITION_TYPE = "ifquant_scientific_measurement_definition"
THRESHOLD_SET_TYPE = "ifquant_frozen_threshold_set"
METHOD_INSTANCE_CONTRACT = "ifquant_method_instance/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SAFE_JSON_INTEGER = (1 << 53) - 1

DEFINITION_ROOT_FIELDS = frozenset(
    {
        "$schema",
        "schema_version",
        "definition_type",
        "definition_id",
        "definition_version",
        "measurement_kind",
        "input_semantics",
        "processing_frame",
        "reference_space",
        "endpoint",
    }
)

THRESHOLD_SET_ROOT_FIELDS = frozenset(
    {
        "$schema",
        "schema_version",
        "threshold_set_type",
        "threshold_set_id",
        "threshold_set_version",
        "scientific_definition",
        "applicability",
        "thresholds",
    }
)

FORBIDDEN_DEFINITION_KEYS = frozenset(
    {
        "acquisition_label",
        "authorization",
        "backend",
        "backend_id",
        "backend_version",
        "channel_index",
        "channel_label",
        "claim_boundary",
        "code_revision",
        "conformance",
        "executable",
        "lifecycle",
        "path",
        "permitted_uses",
        "record_level",
        "run_id",
        "runtime",
        "script",
        "software",
        "status",
        "threshold_set_id",
        "track",
    }
)


class ScientificMeasurementDefinitionError(ValueError):
    """Raised when a numerical method contract is not closed and valid."""


@dataclass(frozen=True)
class LoadedScientificMeasurementDefinition:
    """One immutable, validated scientific-definition file snapshot."""

    document: Mapping[str, Any]
    file_sha256: str
    canonical_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class LoadedFrozenThresholdSet:
    """One immutable, validated frozen-threshold-set file snapshot."""

    document: Mapping[str, Any]
    file_sha256: str
    canonical_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ResolvedMeasurementMethod:
    """Cross-validated identities for one fully parameterized numerical method."""

    scientific_definition_sha256: str
    threshold_set_sha256: str
    method_instance_sha256: str
    threshold_bindings: tuple[tuple[str, int | float], ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScientificMeasurementDefinitionError(message)


def _object(value: Any, location: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{location} must be an object")
    return value


def _array(value: Any, location: str) -> list[Any]:
    _require(
        isinstance(value, Sequence) and not isinstance(value, (str, bytes)),
        f"{location} must be an array",
    )
    return list(value)


def _exact_fields(
    value: Mapping[str, Any], expected: set[str] | frozenset[str], location: str
) -> None:
    _require(
        all(isinstance(key, str) for key in value),
        f"{location} keys must be strings",
    )
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    _require(not missing, f"{location} is missing: {', '.join(missing)}")
    _require(not extra, f"{location} has unknown fields: {', '.join(extra)}")


def _nonempty(value: Any, location: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()),
        f"{location} must be a non-empty string",
    )
    return value


def _string_enum(value: Any, allowed: frozenset[str], location: str) -> str:
    _require(isinstance(value, str), f"{location} must be a string")
    _require(
        value in allowed,
        f"{location} must be one of: {', '.join(sorted(allowed))}",
    )
    return value


def _semver(value: Any, location: str) -> str:
    text = _nonempty(value, location)
    _require(bool(SEMVER_RE.fullmatch(text)), f"{location} must be MAJOR.MINOR.PATCH")
    return text


def _sha256(value: Any, location: str) -> str:
    _require(
        isinstance(value, str) and bool(SHA256_RE.fullmatch(value)),
        f"{location} must be lowercase SHA-256",
    )
    return value


def _finite(value: Any, location: str) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        f"{location} must be numeric",
    )
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ScientificMeasurementDefinitionError(
            f"{location} must be representable as a finite number"
        ) from exc
    _require(math.isfinite(numeric), f"{location} must be finite")
    _require(
        abs(numeric) <= SAFE_JSON_INTEGER,
        f"{location} exceeds the safe JSON numeric range",
    )
    _require(
        not (numeric == 0 and math.copysign(1.0, numeric) < 0),
        f"{location} cannot be negative zero",
    )
    return numeric


def _integer(value: Any, location: str, *, minimum: int = 0) -> int:
    numeric = _finite(value, location)
    _require(numeric.is_integer(), f"{location} must be an integer")
    integer = int(numeric)
    _require(integer >= minimum, f"{location} must be at least {minimum}")
    return integer


def _walk_keys(value: Any, location: str = "definition"):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield location, key
            yield from _walk_keys(child, f"{location}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _walk_keys(child, f"{location}[{index}]")


def _reject_definition_envelope_fields(value: Mapping[str, Any]) -> None:
    for location, key in _walk_keys(value):
        _require(isinstance(key, str), f"{location} keys must be strings")
        _require(
            key not in FORBIDDEN_DEFINITION_KEYS,
            f"{location} contains non-scientific field {key!r}; acquisition, "
            "execution, governance, and conformance envelopes must remain separate",
        )


def _validate_semantic_inputs(value: Any) -> dict[str, str]:
    inputs = _array(value, "input_semantics.semantic_inputs")
    _require(
        len(inputs) == 1,
        "input_semantics.semantic_inputs must contain exactly one v1 input",
    )
    result: dict[str, str] = {}
    ids: list[str] = []
    for index, item in enumerate(inputs):
        location = f"input_semantics.semantic_inputs[{index}]"
        semantic_input = _object(item, location)
        _exact_fields(semantic_input, {"input_id", "marker_id"}, location)
        input_id = _nonempty(semantic_input["input_id"], f"{location}.input_id")
        marker_id = _nonempty(semantic_input["marker_id"], f"{location}.marker_id")
        _require(input_id not in result, f"duplicate semantic input_id {input_id!r}")
        result[input_id] = marker_id
        ids.append(input_id)
    _require(
        ids == sorted(ids),
        "input_semantics.semantic_inputs must be sorted by input_id",
    )
    return result


def _validate_numerator_pipeline(
    value: Any,
    *,
    semantic_inputs: Mapping[str, str],
    reference_space_id: str,
    processing_extent: str,
) -> tuple[str, str]:
    steps = _array(value, "endpoint.numerator_pipeline")
    expected_order = [
        "select_input",
        "gaussian_blur",
        "apply_frozen_threshold",
        "connected_component_filter",
        "intersect_reference_space",
    ]
    _require(
        len(steps) == len(expected_order),
        "endpoint.numerator_pipeline must contain exactly five v1 operations",
    )

    operations: list[tuple[str, Mapping[str, Any]]] = []
    for index, item in enumerate(steps):
        location = f"endpoint.numerator_pipeline[{index}]"
        operation = _object(item, location)
        _exact_fields(operation, {"operation", "parameters"}, location)
        name = _nonempty(operation["operation"], f"{location}.operation")
        params = _object(operation["parameters"], f"{location}.parameters")
        operations.append((name, params))
    names = [name for name, _ in operations]
    _require(
        names == expected_order,
        "endpoint.numerator_pipeline must be select_input -> gaussian_blur -> "
        "apply_frozen_threshold -> connected_component_filter -> "
        "intersect_reference_space",
    )

    selected = operations[0][1]
    _exact_fields(selected, {"input_id"}, "endpoint.numerator_pipeline[0].parameters")
    input_id = _nonempty(
        selected["input_id"], "endpoint.numerator_pipeline[0].parameters.input_id"
    )
    _require(input_id in semantic_inputs, f"pipeline selects undeclared input {input_id!r}")

    gaussian = operations[1][1]
    _exact_fields(
        gaussian,
        {
            "sigma_x_pixels",
            "sigma_y_pixels",
            "border_mode",
            "kernel_accuracy",
            "intermediate_precision",
            "output_quantization",
        },
        "endpoint.numerator_pipeline[1].parameters",
    )
    _require(
        _finite(gaussian["sigma_x_pixels"], "gaussian sigma_x_pixels") > 0,
        "gaussian sigma_x_pixels must be positive",
    )
    _require(
        _finite(gaussian["sigma_y_pixels"], "gaussian sigma_y_pixels") > 0,
        "gaussian sigma_y_pixels must be positive",
    )
    accuracy = _finite(gaussian["kernel_accuracy"], "gaussian kernel_accuracy")
    _require(0 < accuracy <= 0.02, "gaussian kernel_accuracy must be in (0, 0.02]")
    _require(gaussian["border_mode"] == "nearest", "gaussian border_mode must be 'nearest'")
    _require(
        gaussian["intermediate_precision"] == "float32",
        "gaussian intermediate_precision must be 'float32'",
    )
    _string_enum(
        gaussian["output_quantization"],
        frozenset({"source_type", "float32"}),
        "gaussian output_quantization",
    )

    threshold = operations[2][1]
    _exact_fields(
        threshold,
        {"threshold_parameter_id", "comparison", "foreground"},
        "endpoint.numerator_pipeline[2].parameters",
    )
    threshold_parameter_id = _nonempty(
        threshold["threshold_parameter_id"],
        "endpoint.numerator_pipeline[2].parameters.threshold_parameter_id",
    )
    _require(
        threshold["comparison"] == "greater_than_or_equal",
        "threshold comparison must be 'greater_than_or_equal'",
    )
    _require(threshold["foreground"] == "bright", "threshold foreground must be 'bright'")

    component_filter = operations[3][1]
    _exact_fields(
        component_filter,
        {
            "connectivity",
            "minimum_area_um2",
            "maximum_area_um2",
            "minimum_boundary",
            "maximum_boundary",
            "processing_extent",
            "edge_policy",
        },
        "endpoint.numerator_pipeline[3].parameters",
    )
    connectivity = _integer(component_filter["connectivity"], "component connectivity")
    _require(connectivity in {4, 8}, "component connectivity must be 4 or 8")
    minimum = _finite(component_filter["minimum_area_um2"], "component minimum_area_um2")
    _require(minimum >= 0, "component minimum_area_um2 cannot be negative")
    maximum = component_filter["maximum_area_um2"]
    if maximum is None:
        _require(
            component_filter["maximum_boundary"] is None,
            "component maximum_boundary must be null when maximum_area_um2 is null",
        )
    else:
        maximum_number = _finite(maximum, "component maximum_area_um2")
        _require(maximum_number >= minimum, "component maximum area cannot be below minimum")
        _string_enum(
            component_filter["maximum_boundary"],
            frozenset({"inclusive", "exclusive"}),
            "component maximum_boundary",
        )
    _string_enum(
        component_filter["minimum_boundary"],
        frozenset({"inclusive", "exclusive"}),
        "component minimum_boundary",
    )
    _require(
        component_filter["processing_extent"] == processing_extent,
        "component processing_extent must match processing_frame.mask_construction_extent",
    )
    _string_enum(
        component_filter["edge_policy"],
        frozenset({"include", "exclude"}),
        "component edge_policy",
    )

    intersection = operations[4][1]
    _exact_fields(
        intersection,
        {"reference_space_id"},
        "endpoint.numerator_pipeline[4].parameters",
    )
    _require(
        intersection["reference_space_id"] == reference_space_id,
        "pipeline intersection uses the wrong reference_space_id",
    )
    return input_id, threshold_parameter_id


def _definition_bindings(definition: Mapping[str, Any]) -> tuple[str, str, str]:
    input_semantics = _object(definition["input_semantics"], "input_semantics")
    semantic_inputs = {
        item["input_id"]: item["marker_id"]
        for item in _array(input_semantics["semantic_inputs"], "semantic_inputs")
    }
    processing = _object(definition["processing_frame"], "processing_frame")
    reference = _object(definition["reference_space"], "reference_space")
    endpoint = _object(definition["endpoint"], "endpoint")
    input_id, parameter_id = _validate_numerator_pipeline(
        endpoint["numerator_pipeline"],
        semantic_inputs=semantic_inputs,
        reference_space_id=reference["reference_space_id"],
        processing_extent=processing["mask_construction_extent"],
    )
    return input_id, semantic_inputs[input_id], parameter_id


def validate_scientific_measurement_definition(definition: Mapping[str, Any]) -> None:
    """Fail closed unless *definition* is a pure v1 numerical area kernel."""

    root = _object(definition, "definition")
    _exact_fields(root, DEFINITION_ROOT_FIELDS, "definition")
    _reject_definition_envelope_fields(root)
    _require(root["$schema"] == DEFINITION_SCHEMA_URI, "definition.$schema is unsupported")
    _require(root["schema_version"] == SCHEMA_VERSION, "definition.schema_version is unsupported")
    _require(root["definition_type"] == DEFINITION_TYPE, "definition.definition_type is unsupported")
    _nonempty(root["definition_id"], "definition.definition_id")
    _semver(root["definition_version"], "definition.definition_version")
    _require(root["measurement_kind"] == "area_fraction", "measurement_kind must be 'area_fraction'")

    input_semantics = _object(root["input_semantics"], "input_semantics")
    _exact_fields(
        input_semantics,
        {
            "dimensionality",
            "plane_policy",
            "intensity_coordinate",
            "intensity_representation",
            "intensity_transform",
            "pixel_calibration",
            "pixel_area_unit",
            "semantic_inputs",
        },
        "input_semantics",
    )
    _require(input_semantics["dimensionality"] == "2d", "input dimensionality must be '2d'")
    _require(input_semantics["plane_policy"] == "already_2d", "plane_policy must be 'already_2d'")
    _require(
        input_semantics["intensity_coordinate"] == "native_sample_value",
        "intensity_coordinate must be 'native_sample_value'",
    )
    _require(
        input_semantics["intensity_representation"] == "unsigned_integer",
        "intensity_representation must be 'unsigned_integer'",
    )
    _require(input_semantics["intensity_transform"] == "none", "intensity_transform must be 'none'")
    _require(
        input_semantics["pixel_calibration"] == "required_axis_specific_source_metadata",
        "pixel_calibration must require axis-specific source metadata",
    )
    _require(input_semantics["pixel_area_unit"] == "um2", "pixel_area_unit must be 'um2'")
    semantic_inputs = _validate_semantic_inputs(input_semantics["semantic_inputs"])

    processing = _object(root["processing_frame"], "processing_frame")
    _exact_fields(
        processing,
        {"mask_construction_extent", "reference_intersection"},
        "processing_frame",
    )
    _require(
        processing["mask_construction_extent"]
        == "complete_supplied_plane_including_halo_if_present",
        "mask_construction_extent is unsupported",
    )
    _require(
        processing["reference_intersection"] == "after_component_filtering",
        "reference_intersection must be 'after_component_filtering'",
    )

    reference = _object(root["reference_space"], "reference_space")
    _exact_fields(
        reference,
        {
            "reference_space_id",
            "source",
            "artifact_role",
            "background_value",
            "foreground_value",
            "foreground_semantics",
        },
        "reference_space",
    )
    reference_id = _nonempty(reference["reference_space_id"], "reference_space.reference_space_id")
    _require(reference["source"] == "canonical_binary_mask", "reference source must be canonical_binary_mask")
    _require(reference["artifact_role"] == "reference_space_mask", "reference artifact_role is unsupported")
    background = _integer(reference["background_value"], "reference_space.background_value")
    foreground = _integer(reference["foreground_value"], "reference_space.foreground_value")
    _require(
        background <= 255 and foreground <= 255,
        "reference mask values must fit unsigned 8-bit encoding",
    )
    _require(background != foreground, "reference foreground and background values must differ")
    _require(reference["foreground_semantics"] == "included_measurement_area", "reference foreground_semantics is unsupported")

    endpoint = _object(root["endpoint"], "endpoint")
    _exact_fields(
        endpoint,
        {
            "endpoint_id",
            "calculation",
            "numerator_unit",
            "denominator_unit",
            "result_unit",
            "denominator_source",
            "zero_numerator_policy",
            "zero_denominator_policy",
            "numerator_pipeline",
            "aggregation",
        },
        "endpoint",
    )
    _nonempty(endpoint["endpoint_id"], "endpoint.endpoint_id")
    _require(endpoint["calculation"] == "ratio", "endpoint.calculation must be 'ratio'")
    _require(endpoint["numerator_unit"] == "um2", "endpoint.numerator_unit must be 'um2'")
    _require(endpoint["denominator_unit"] == "um2", "endpoint.denominator_unit must be 'um2'")
    _require(endpoint["result_unit"] == "fraction", "endpoint.result_unit must be 'fraction'")
    _require(endpoint["denominator_source"] == "reference_space", "denominator_source must be reference_space")
    _require(endpoint["zero_numerator_policy"] == "observed_zero", "zero numerator must be observed_zero")
    _require(endpoint["zero_denominator_policy"] == "not_evaluable", "zero denominator must be not_evaluable")
    _validate_numerator_pipeline(
        endpoint["numerator_pipeline"],
        semantic_inputs=semantic_inputs,
        reference_space_id=reference_id,
        processing_extent=processing["mask_construction_extent"],
    )
    aggregation = _object(endpoint["aggregation"], "endpoint.aggregation")
    _exact_fields(aggregation, {"method"}, "endpoint.aggregation")
    _require(aggregation["method"] == "ratio_of_sums", "aggregation.method must be ratio_of_sums")


def validate_frozen_threshold_set(threshold_set: Mapping[str, Any]) -> None:
    """Fail closed unless *threshold_set* is a narrow, explicitly scoped cutoff set."""

    root = _object(threshold_set, "threshold_set")
    _exact_fields(root, THRESHOLD_SET_ROOT_FIELDS, "threshold_set")
    _require(root["$schema"] == THRESHOLD_SET_SCHEMA_URI, "threshold_set.$schema is unsupported")
    _require(root["schema_version"] == SCHEMA_VERSION, "threshold_set.schema_version is unsupported")
    _require(root["threshold_set_type"] == THRESHOLD_SET_TYPE, "threshold_set_type is unsupported")
    _nonempty(root["threshold_set_id"], "threshold_set.threshold_set_id")
    _semver(root["threshold_set_version"], "threshold_set.threshold_set_version")

    definition = _object(root["scientific_definition"], "threshold_set.scientific_definition")
    _exact_fields(
        definition,
        {"definition_id", "definition_version", "canonical_sha256"},
        "threshold_set.scientific_definition",
    )
    _nonempty(definition["definition_id"], "threshold_set.scientific_definition.definition_id")
    _semver(definition["definition_version"], "threshold_set.scientific_definition.definition_version")
    _sha256(definition["canonical_sha256"], "threshold_set.scientific_definition.canonical_sha256")

    applicability = _object(root["applicability"], "threshold_set.applicability")
    _exact_fields(
        applicability,
        {
            "scope_id",
            "scope_binding",
            "scope_profile_sha256",
            "intensity_representation",
            "bit_depth",
            "intensity_transform",
            "transfer_policy",
        },
        "threshold_set.applicability",
    )
    _nonempty(applicability["scope_id"], "threshold_set.applicability.scope_id")
    scope_binding = _string_enum(
        applicability["scope_binding"],
        frozenset({"identifier_only_unattested", "content_addressed_profile"}),
        "threshold applicability scope_binding",
    )
    if scope_binding == "identifier_only_unattested":
        _require(
            applicability["scope_profile_sha256"] is None,
            "identifier-only applicability cannot claim a scope profile hash",
        )
    else:
        _sha256(
            applicability["scope_profile_sha256"],
            "threshold_set.applicability.scope_profile_sha256",
        )
    _require(
        applicability["intensity_representation"] == "unsigned_integer",
        "threshold applicability intensity_representation must be unsigned_integer",
    )
    bit_depth = _integer(applicability["bit_depth"], "threshold_set.applicability.bit_depth", minimum=1)
    _require(bit_depth <= 32, "threshold applicability bit_depth cannot exceed 32 in v1")
    _require(applicability["intensity_transform"] == "none", "threshold applicability intensity_transform must be none")
    _require(
        applicability["transfer_policy"] == "declared_scope_only",
        "threshold transfer_policy must be declared_scope_only",
    )

    thresholds = _array(root["thresholds"], "threshold_set.thresholds")
    _require(bool(thresholds), "threshold_set.thresholds must not be empty")
    ids: list[str] = []
    maximum_value = (1 << bit_depth) - 1
    for index, item in enumerate(thresholds):
        location = f"threshold_set.thresholds[{index}]"
        threshold = _object(item, location)
        _exact_fields(
            threshold,
            {"threshold_parameter_id", "marker_id", "value", "unit"},
            location,
        )
        parameter_id = _nonempty(threshold["threshold_parameter_id"], f"{location}.threshold_parameter_id")
        _nonempty(threshold["marker_id"], f"{location}.marker_id")
        value = _integer(threshold["value"], f"{location}.value")
        _require(value <= maximum_value, f"{location}.value exceeds the declared bit depth")
        _require(threshold["unit"] == "native_sample_value", f"{location}.unit must be native_sample_value")
        ids.append(parameter_id)
    _require(len(ids) == len(set(ids)), "threshold parameter IDs must be unique")
    _require(ids == sorted(ids), "threshold_set.thresholds must be sorted by threshold_parameter_id")


def _normalize_canonical(value: Any, location: str = "$") -> Any:
    if isinstance(value, Mapping):
        _require(all(isinstance(key, str) for key in value), f"{location} keys must be strings")
        return {
            key: _normalize_canonical(child, f"{location}.{key}")
            for key, child in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _normalize_canonical(child, f"{location}[{index}]")
            for index, child in enumerate(value)
        ]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        _require(abs(value) <= SAFE_JSON_INTEGER, f"{location} exceeds safe JSON integer range")
        return value
    if isinstance(value, float):
        numeric = _finite(value, location)
        _require(abs(numeric) <= SAFE_JSON_INTEGER, f"{location} exceeds safe JSON numeric range")
        if numeric.is_integer():
            return int(numeric)
        return numeric
    raise ScientificMeasurementDefinitionError(
        f"{location} contains unsupported canonical JSON type {type(value).__name__}"
    )


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    normalized = _normalize_canonical(value)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ScientificMeasurementDefinitionError(
            f"contract cannot be canonicalized: {exc}"
        ) from exc


def canonical_definition_bytes(definition: Mapping[str, Any]) -> bytes:
    """Return deterministic semantic bytes after definition validation."""

    validate_scientific_measurement_definition(definition)
    return _canonical_bytes(definition)


def scientific_definition_sha256(definition: Mapping[str, Any]) -> str:
    """Return the pure numerical-definition hash."""

    return hashlib.sha256(canonical_definition_bytes(definition)).hexdigest()


def canonical_threshold_set_bytes(threshold_set: Mapping[str, Any]) -> bytes:
    """Return deterministic bytes after frozen-threshold-set validation."""

    validate_frozen_threshold_set(threshold_set)
    return _canonical_bytes(threshold_set)


def frozen_threshold_set_sha256(threshold_set: Mapping[str, Any]) -> str:
    """Return the applicability-bearing threshold-set hash."""

    return hashlib.sha256(canonical_threshold_set_bytes(threshold_set)).hexdigest()


def resolve_measurement_method(
    definition: Mapping[str, Any], threshold_set: Mapping[str, Any]
) -> ResolvedMeasurementMethod:
    """Cross-validate and bind a definition to exactly one threshold set."""

    validate_scientific_measurement_definition(definition)
    validate_frozen_threshold_set(threshold_set)
    definition_hash = scientific_definition_sha256(definition)
    threshold_hash = frozen_threshold_set_sha256(threshold_set)

    definition_ref = _object(
        threshold_set["scientific_definition"], "threshold_set.scientific_definition"
    )
    _require(
        definition_ref["definition_id"] == definition["definition_id"],
        "threshold set targets the wrong definition_id",
    )
    _require(
        definition_ref["definition_version"] == definition["definition_version"],
        "threshold set targets the wrong definition_version",
    )
    _require(
        definition_ref["canonical_sha256"] == definition_hash,
        "threshold set scientific-definition hash does not match recomputed bytes",
    )

    input_id, marker_id, parameter_id = _definition_bindings(definition)
    del input_id
    supplied: dict[str, tuple[str, int | float]] = {}
    for item in _array(threshold_set["thresholds"], "threshold_set.thresholds"):
        supplied[item["threshold_parameter_id"]] = (item["marker_id"], item["value"])
    _require(
        set(supplied) == {parameter_id},
        "threshold set must supply exactly the definition's threshold parameter IDs",
    )
    supplied_marker, supplied_value = supplied[parameter_id]
    _require(
        supplied_marker == marker_id,
        "threshold marker_id disagrees with the selected semantic input",
    )

    input_semantics = _object(definition["input_semantics"], "input_semantics")
    applicability = _object(threshold_set["applicability"], "threshold_set.applicability")
    _require(
        applicability["intensity_representation"]
        == input_semantics["intensity_representation"],
        "threshold applicability intensity representation disagrees with definition",
    )
    _require(
        applicability["intensity_transform"] == input_semantics["intensity_transform"],
        "threshold applicability intensity transform disagrees with definition",
    )

    descriptor = {
        "contract": METHOD_INSTANCE_CONTRACT,
        "scientific_definition_sha256": definition_hash,
        "threshold_set_sha256": threshold_hash,
    }
    method_hash = hashlib.sha256(_canonical_bytes(descriptor)).hexdigest()
    return ResolvedMeasurementMethod(
        scientific_definition_sha256=definition_hash,
        threshold_set_sha256=threshold_hash,
        method_instance_sha256=method_hash,
        threshold_bindings=((parameter_id, supplied_value),),
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze(child) for child in value)
    return value


def _load_contract(
    path: str | Path,
    *,
    validator: Callable[[Mapping[str, Any]], None],
    canonicalizer: Callable[[Mapping[str, Any]], bytes],
) -> tuple[Mapping[str, Any], str, str, int]:
    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ScientificMeasurementDefinitionError(
            f"contract is unavailable: {source}: {exc}"
        ) from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ScientificMeasurementDefinitionError(
                    f"contract contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    def parse_integer(text: str) -> int:
        if text == "-0":
            raise ScientificMeasurementDefinitionError(
                "contract contains lexical negative zero"
            )
        digits = text.removeprefix("-")
        if len(digits) > len(str(SAFE_JSON_INTEGER)):
            raise ScientificMeasurementDefinitionError(
                "contract integer token exceeds the safe JSON integer range"
            )
        try:
            return int(text)
        except (ValueError, OverflowError) as exc:
            raise ScientificMeasurementDefinitionError(
                "contract contains an unreadable integer token"
            ) from exc

    def parse_float(text: str) -> float:
        try:
            value = float(text)
        except (ValueError, OverflowError) as exc:
            raise ScientificMeasurementDefinitionError(
                "contract contains an unreadable floating-point token"
            ) from exc
        mantissa = text.lower().split("e", 1)[0].lstrip("+-")
        if value == 0 and any(character in "123456789" for character in mantissa):
            raise ScientificMeasurementDefinitionError(
                "contract floating-point token underflows binary64"
            )
        if value == 0 and text.startswith("-"):
            raise ScientificMeasurementDefinitionError(
                "contract contains lexical negative zero"
            )
        return value

    def reject_nonfinite_constant(text: str) -> None:
        raise ScientificMeasurementDefinitionError(
            f"contract contains non-JSON numeric constant {text!r}"
        )

    try:
        document = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
            parse_int=parse_integer,
            parse_float=parse_float,
            parse_constant=reject_nonfinite_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ScientificMeasurementDefinitionError(
            f"contract is unreadable: {exc}"
        ) from exc
    _require(isinstance(document, dict), "contract root must be an object")
    validator(document)
    canonical = canonicalizer(document)
    return (
        _freeze(document),
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(canonical).hexdigest(),
        len(payload),
    )


def load_scientific_measurement_definition(
    path: str | Path,
) -> LoadedScientificMeasurementDefinition:
    """Read and freeze one validated scientific-definition snapshot."""

    document, file_hash, canonical_hash, size = _load_contract(
        path,
        validator=validate_scientific_measurement_definition,
        canonicalizer=canonical_definition_bytes,
    )
    return LoadedScientificMeasurementDefinition(
        document=document,
        file_sha256=file_hash,
        canonical_sha256=canonical_hash,
        size_bytes=size,
    )


def load_frozen_threshold_set(path: str | Path) -> LoadedFrozenThresholdSet:
    """Read and freeze one validated frozen-threshold-set snapshot."""

    document, file_hash, canonical_hash, size = _load_contract(
        path,
        validator=validate_frozen_threshold_set,
        canonicalizer=canonical_threshold_set_bytes,
    )
    return LoadedFrozenThresholdSet(
        document=document,
        file_sha256=file_hash,
        canonical_sha256=canonical_hash,
        size_bytes=size,
    )
