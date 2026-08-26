"""Executable checks for the versioned IFQuant measurement record.

The JSON Schema is the declarative contract. These standard-library checks are
the runtime fail-closed subset available to route adapters. Legacy CSVs acquire
record semantics only through an explicit route mapping; the Stage 4 bridge
never infers them from column names. Passing the checks establishes record
structure, not biological validity.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any


SCHEMA_VERSION = "2.0.0"
TRACKS = frozenset({"area_wsi", "cell_confocal", "he_pathology"})
IDENTIFIER_FIELDS = (
    "mouse_id",
    "slide_id",
    "section_id",
    "field_id",
    "tile_id",
    "region_id",
    "cell_id",
)
OBSERVATION_IDENTIFIER_FIELDS = IDENTIFIER_FIELDS[1:]
EVALUABILITY = frozenset({"measured", "not_evaluable", "not_applicable", "excluded"})
SAMPLING_DESIGNS = frozenset({"exhaustive", "probability", "purposive", "unknown"})
ESTIMAND_SCOPES = frozenset({"observed_units", "whole_section", "whole_lung"})
COMPARTMENT_STATES = frozenset({"assigned", "unassigned", "not_applicable"})
QC_STATES = frozenset({"pass", "warning", "fail", "not_run"})
REVIEW_STATES = frozenset({"not_required", "pending", "accepted", "rejected"})
SHA256 = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^a-z0-9])(?:[a-z]:[\\/]|\\\\)")
POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[=\s\"'(:])/(?!/)")
HOME_SHORTHAND = re.compile(
    r"(?i)(?:^|[=\s\"'(:])(?:~[\\/]|(?:\$(?:\{?home\}?|env:userprofile)|%(?:userprofile|homepath)%)(?:[\\/]|$))"
)
LEVELS_BY_TRACK = {
    "area_wsi": frozenset({"region", "tile", "section", "slide", "mouse"}),
    "cell_confocal": frozenset({"cell", "region", "field", "mouse"}),
    "he_pathology": frozenset({"region", "section", "slide", "mouse"}),
}
CALCULATIONS_BY_TRACK = {
    "area_wsi": frozenset({"ratio"}),
    "cell_confocal": frozenset({"ratio", "categorical_state"}),
    "he_pathology": frozenset({"ratio", "categorical_state", "ordinal"}),
}
LEVEL_IDENTIFIER = {
    "mouse": "mouse_id",
    "slide": "slide_id",
    "section": "section_id",
    "field": "field_id",
    "tile": "tile_id",
    "region": "region_id",
    "cell": "cell_id",
}
DESCENDANT_IDENTIFIERS = {
    "mouse": OBSERVATION_IDENTIFIER_FIELDS,
    "slide": ("section_id", "field_id", "tile_id", "region_id", "cell_id"),
    "section": ("field_id", "tile_id", "region_id", "cell_id"),
    "field": ("tile_id", "region_id", "cell_id"),
    "tile": ("region_id", "cell_id"),
    "region": ("cell_id",),
    "cell": (),
}

ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "record_id",
        "track",
        "record_level",
        "identifiers",
        "measurement_profile_id",
        "channel_signature",
        "segmentation_model",
        "endpoint",
        "sampling",
        "compartment",
        "provenance",
        "qc",
    }
)


class MeasurementContractError(ValueError):
    """Raised when a measurement record violates the shared contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MeasurementContractError(message)


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{location} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str] | frozenset[str], location: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    _require(not missing, f"{location} is missing: {', '.join(missing)}")
    _require(not extra, f"{location} has unknown fields: {', '.join(extra)}")


def _fields(
    value: Mapping[str, Any],
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str],
    location: str,
) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    extra = sorted(actual - required - optional)
    _require(not missing, f"{location} is missing: {', '.join(missing)}")
    _require(not extra, f"{location} has unknown fields: {', '.join(extra)}")


def _nonempty_string(value: Any, location: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{location} must be a non-empty string")
    return value


def _finite_number(value: Any, location: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{location} must be numeric")
    numeric = float(value)
    _require(math.isfinite(numeric), f"{location} must be finite")
    return numeric


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _validate_privacy(value: Any) -> None:
    for item in _walk_strings(value):
        _require(
            not WINDOWS_ABSOLUTE_PATH.search(item)
            and not POSIX_ABSOLUTE_PATH.search(item)
            and not HOME_SHORTHAND.search(item)
            and "file://" not in item.lower(),
            "measurement records cannot contain local or absolute paths",
        )


def _validate_identifiers(value: Any) -> None:
    identifiers = _mapping(value, "identifiers")
    _exact_fields(identifiers, set(IDENTIFIER_FIELDS), "identifiers")
    _nonempty_string(identifiers["mouse_id"], "identifiers.mouse_id")
    for name in OBSERVATION_IDENTIFIER_FIELDS:
        item = identifiers[name]
        _require(item is None or (isinstance(item, str) and bool(item.strip())), f"identifiers.{name} must be null or a non-empty string")


def _validate_record_level(identifiers: Mapping[str, Any], track: str, level: str) -> None:
    required_identifier = LEVEL_IDENTIFIER[level]
    _require(identifiers[required_identifier] is not None, f"record_level={level} requires {required_identifier}")
    for name in DESCENDANT_IDENTIFIERS[level]:
        _require(identifiers[name] is None, f"record_level={level} cannot carry descendant identifier {name}")

    if track == "area_wsi" and level != "mouse":
        _require(identifiers["slide_id"] is not None, "non-mouse area_wsi records require slide_id")
    elif track == "cell_confocal" and level != "mouse":
        _require(identifiers["field_id"] is not None, "non-mouse cell_confocal records require field_id")
    elif track == "he_pathology" and level != "mouse":
        _require(
            identifiers["slide_id"] is not None or identifiers["section_id"] is not None,
            "non-mouse he_pathology records require slide_id or section_id",
        )


def _validate_quantity(value: Any, location: str, *, measured: bool) -> None:
    quantity = _mapping(value, location)
    _exact_fields(quantity, {"value", "unit"}, location)
    _nonempty_string(quantity["unit"], f"{location}.unit")
    if measured:
        _finite_number(quantity["value"], f"{location}.value")
    else:
        _require(quantity["value"] is None, f"{location}.value must be null when not measured")


def _validate_ratio_endpoint(endpoint: Mapping[str, Any]) -> None:
    """Validate the original additive ratio/fraction endpoint shape."""

    expected = {
        "endpoint_id",
        "calculation",
        "reference_space_id",
        "evaluability",
        "numerator",
        "denominator",
        "value",
        "unit",
        "zero_is_observed",
        "reason_code",
    }
    _exact_fields(endpoint, expected, "endpoint")
    _require(endpoint["calculation"] == "ratio", "endpoint.calculation must be ratio")
    _nonempty_string(endpoint["unit"], "endpoint.unit")
    _require(isinstance(endpoint["zero_is_observed"], bool), "endpoint.zero_is_observed must be Boolean")

    evaluability = endpoint["evaluability"]
    measured = evaluability == "measured"
    _validate_quantity(endpoint["numerator"], "endpoint.numerator", measured=measured)
    _validate_quantity(endpoint["denominator"], "endpoint.denominator", measured=measured)

    if measured:
        result = _finite_number(endpoint["value"], "endpoint.value")
        numerator = _finite_number(endpoint["numerator"]["value"], "endpoint.numerator.value")
        denominator = _finite_number(endpoint["denominator"]["value"], "endpoint.denominator.value")
        _require(numerator >= 0, "endpoint.numerator.value cannot be negative")
        _require(denominator > 0, "endpoint.denominator.value must be greater than zero")
        expected_result = numerator / denominator
        _require(
            math.isclose(result, expected_result, rel_tol=1e-12, abs_tol=1e-15),
            "endpoint.value does not equal numerator / denominator",
        )
        numerator_unit = endpoint["numerator"]["unit"]
        denominator_unit = endpoint["denominator"]["unit"]
        if numerator_unit == denominator_unit:
            _require(
                endpoint["unit"] in {"fraction", "ratio"},
                "equal-unit endpoint.unit must be 'fraction' or 'ratio'",
            )
        else:
            expected_unit = f"{numerator_unit}_per_{denominator_unit}"
            _require(endpoint["unit"] == expected_unit, f"endpoint.unit must be {expected_unit!r} for the declared quantities")
        if endpoint["unit"] == "fraction":
            _require(numerator <= denominator, "fraction numerator cannot exceed its denominator")
            _require(0 <= result <= 1, "fraction endpoint.value must be in [0, 1]")
        _require(endpoint["reason_code"] is None, "measured endpoints cannot carry a reason_code")
        _require(
            endpoint["zero_is_observed"] is (result == 0),
            "endpoint.zero_is_observed must be true exactly when the measured result is zero",
        )
    else:
        _require(endpoint["value"] is None, "endpoint.value must be null when not measured")
        _require(endpoint["zero_is_observed"] is False, "an unevaluable endpoint cannot be an observed zero")
        _nonempty_string(endpoint["reason_code"], "endpoint.reason_code")


def _validate_categorical_endpoint(endpoint: Mapping[str, Any]) -> None:
    expected = {
        "endpoint_id",
        "calculation",
        "reference_space_id",
        "evaluability",
        "state",
        "state_vocabulary_id",
        "reason_code",
    }
    _exact_fields(endpoint, expected, "endpoint")
    _require(
        endpoint["calculation"] == "categorical_state",
        "endpoint.calculation must be categorical_state",
    )
    _nonempty_string(endpoint["state_vocabulary_id"], "endpoint.state_vocabulary_id")
    if endpoint["evaluability"] == "measured":
        _nonempty_string(endpoint["state"], "endpoint.state")
        _require(endpoint["reason_code"] is None, "measured endpoints cannot carry a reason_code")
    else:
        _require(endpoint["state"] is None, "endpoint.state must be null when not measured")
        _nonempty_string(endpoint["reason_code"], "endpoint.reason_code")


def _validate_ordinal_endpoint(endpoint: Mapping[str, Any]) -> None:
    expected = {
        "endpoint_id",
        "calculation",
        "reference_space_id",
        "evaluability",
        "rank",
        "minimum_rank",
        "maximum_rank",
        "scale_id",
        "reason_code",
    }
    _exact_fields(endpoint, expected, "endpoint")
    _require(endpoint["calculation"] == "ordinal", "endpoint.calculation must be ordinal")
    _nonempty_string(endpoint["scale_id"], "endpoint.scale_id")
    minimum = endpoint["minimum_rank"]
    maximum = endpoint["maximum_rank"]
    _require(
        isinstance(minimum, int) and not isinstance(minimum, bool),
        "endpoint.minimum_rank must be an integer",
    )
    _require(
        isinstance(maximum, int) and not isinstance(maximum, bool),
        "endpoint.maximum_rank must be an integer",
    )
    _require(minimum <= maximum, "endpoint.minimum_rank cannot exceed endpoint.maximum_rank")
    if endpoint["evaluability"] == "measured":
        rank = endpoint["rank"]
        _require(
            isinstance(rank, int) and not isinstance(rank, bool),
            "endpoint.rank must be an integer",
        )
        _require(minimum <= rank <= maximum, "endpoint.rank must be within the declared ordinal scale")
        _require(endpoint["reason_code"] is None, "measured endpoints cannot carry a reason_code")
    else:
        _require(endpoint["rank"] is None, "endpoint.rank must be null when not measured")
        _nonempty_string(endpoint["reason_code"], "endpoint.reason_code")


def _validate_endpoint(value: Any, *, track: str) -> None:
    endpoint = _mapping(value, "endpoint")
    common = {
        "endpoint_id",
        "calculation",
        "reference_space_id",
        "evaluability",
        "reason_code",
    }
    variant = {
        "numerator",
        "denominator",
        "value",
        "unit",
        "zero_is_observed",
        "state",
        "state_vocabulary_id",
        "rank",
        "minimum_rank",
        "maximum_rank",
        "scale_id",
    }
    _fields(endpoint, required=common, optional=variant, location="endpoint")
    _nonempty_string(endpoint["endpoint_id"], "endpoint.endpoint_id")
    _nonempty_string(endpoint["reference_space_id"], "endpoint.reference_space_id")
    evaluability = endpoint["evaluability"]
    _require(evaluability in EVALUABILITY, f"endpoint.evaluability must be one of {sorted(EVALUABILITY)}")
    calculation = endpoint.get("calculation")
    allowed = CALCULATIONS_BY_TRACK[track]
    _require(
        calculation in allowed,
        f"endpoint.calculation must be one of {sorted(allowed)} for track {track}",
    )
    if calculation == "ratio":
        _validate_ratio_endpoint(endpoint)
    elif calculation == "categorical_state":
        _validate_categorical_endpoint(endpoint)
    else:
        _validate_ordinal_endpoint(endpoint)


def _validate_sampling(value: Any) -> None:
    sampling = _mapping(value, "sampling")
    _fields(
        sampling,
        required={"design", "inclusion_probability", "selection_source"},
        optional={"estimand_scope", "estimator_profile_id"},
        location="sampling",
    )
    design = sampling["design"]
    _require(design in SAMPLING_DESIGNS, f"sampling.design must be one of {sorted(SAMPLING_DESIGNS)}")
    _nonempty_string(sampling["selection_source"], "sampling.selection_source")
    probability = sampling["inclusion_probability"]
    estimand_scope = sampling.get("estimand_scope", "observed_units")
    _require(
        estimand_scope in ESTIMAND_SCOPES,
        f"sampling.estimand_scope must be one of {sorted(ESTIMAND_SCOPES)}",
    )
    estimator_profile_id = sampling.get("estimator_profile_id")
    if design == "exhaustive":
        numeric = _finite_number(probability, "sampling.inclusion_probability")
        _require(numeric == 1, "exhaustive sampling requires inclusion_probability=1")
        _require(
            estimator_profile_id is None,
            "exhaustive sampling cannot declare a probability estimator",
        )
    elif design == "probability":
        numeric = _finite_number(probability, "sampling.inclusion_probability")
        _require(0 < numeric <= 1, "sampling.inclusion_probability must be in (0, 1]")
        _nonempty_string(
            estimator_profile_id,
            "sampling.estimator_profile_id",
        )
    else:
        _require(probability is None, f"{design} sampling cannot invent an inclusion probability")
        _require(
            estimand_scope == "observed_units",
            f"{design} sampling cannot declare a whole-section or whole-lung estimand",
        )
        _require(
            estimator_profile_id is None,
            f"{design} sampling cannot declare a probability estimator",
        )


def _validate_compartment(value: Any) -> None:
    compartment = _mapping(value, "compartment")
    _exact_fields(compartment, {"status", "labels", "assignment_profile_id"}, "compartment")
    status = compartment["status"]
    _require(status in COMPARTMENT_STATES, f"compartment.status must be one of {sorted(COMPARTMENT_STATES)}")
    labels = compartment["labels"]
    _require(isinstance(labels, list), "compartment.labels must be an array")
    _require(len(labels) == len(set(labels)), "compartment.labels must be unique")
    for index, label in enumerate(labels):
        _nonempty_string(label, f"compartment.labels[{index}]")
    profile = compartment["assignment_profile_id"]
    if status == "assigned":
        _require(bool(labels), "assigned compartments require at least one label")
        _nonempty_string(profile, "compartment.assignment_profile_id")
    else:
        _require(not labels, f"{status} compartments cannot carry labels")
        _require(profile is None, f"{status} compartments cannot carry an assignment profile")


def _validate_channel_signature(value: Any) -> None:
    _require(isinstance(value, list) and bool(value), "channel_signature must be a non-empty array")
    indices = []
    labels = []
    for index, item in enumerate(value):
        channel = _mapping(item, f"channel_signature[{index}]")
        _exact_fields(channel, {"index", "label", "role"}, f"channel_signature[{index}]")
        channel_index = channel["index"]
        _require(isinstance(channel_index, int) and not isinstance(channel_index, bool) and channel_index >= 1, f"channel_signature[{index}].index must be a positive integer")
        indices.append(channel_index)
        labels.append(_nonempty_string(channel["label"], f"channel_signature[{index}].label"))
        _nonempty_string(channel["role"], f"channel_signature[{index}].role")
    _require(
        indices == sorted(set(indices)),
        "channel_signature indices must be unique and strictly increasing",
    )
    _require(len(labels) == len(set(labels)), "channel_signature labels must be unique")


def _validate_segmentation_model(value: Any) -> None:
    if value is None:
        return
    model = _mapping(value, "segmentation_model")
    _exact_fields(model, {"provider", "model_id", "model_sha256", "profile_id"}, "segmentation_model")
    _nonempty_string(model["provider"], "segmentation_model.provider")
    _nonempty_string(model["model_id"], "segmentation_model.model_id")
    _nonempty_string(model["profile_id"], "segmentation_model.profile_id")
    _require(
        isinstance(model["model_sha256"], str)
        and bool(SHA256.fullmatch(model["model_sha256"])),
        "segmentation_model.model_sha256 must be lowercase SHA-256",
    )


def _validate_provenance(value: Any) -> None:
    provenance = _mapping(value, "provenance")
    _exact_fields(
        provenance,
        {"code_revision", "config_sha256", "measurement_profile_sha256", "inputs", "run_id"},
        "provenance",
    )
    code_revision = _nonempty_string(provenance["code_revision"], "provenance.code_revision")
    _require(len(code_revision) >= 7, "provenance.code_revision must contain at least seven characters")
    _nonempty_string(provenance["run_id"], "provenance.run_id")
    _require(
        isinstance(provenance["config_sha256"], str)
        and bool(SHA256.fullmatch(provenance["config_sha256"])),
        "provenance.config_sha256 must be lowercase SHA-256",
    )
    _require(
        isinstance(provenance["measurement_profile_sha256"], str)
        and bool(SHA256.fullmatch(provenance["measurement_profile_sha256"])),
        "provenance.measurement_profile_sha256 must be lowercase SHA-256",
    )
    inputs = provenance["inputs"]
    _require(isinstance(inputs, list) and bool(inputs), "provenance.inputs must be a non-empty array")
    roles = []
    for index, item in enumerate(inputs):
        source = _mapping(item, f"provenance.inputs[{index}]")
        _exact_fields(source, {"role", "sha256"}, f"provenance.inputs[{index}]")
        roles.append(_nonempty_string(source["role"], f"provenance.inputs[{index}].role"))
        _require(
            isinstance(source["sha256"], str)
            and bool(SHA256.fullmatch(source["sha256"])),
            f"provenance.inputs[{index}].sha256 must be lowercase SHA-256",
        )
    _require(len(roles) == len(set(roles)), "provenance input roles must be unique")


def _validate_qc(value: Any) -> None:
    qc = _mapping(value, "qc")
    _exact_fields(qc, {"status", "reason_codes", "review_status"}, "qc")
    _require(qc["status"] in QC_STATES, f"qc.status must be one of {sorted(QC_STATES)}")
    _require(qc["review_status"] in REVIEW_STATES, f"qc.review_status must be one of {sorted(REVIEW_STATES)}")
    reasons = qc["reason_codes"]
    _require(isinstance(reasons, list), "qc.reason_codes must be an array")
    _require(len(reasons) == len(set(reasons)), "qc.reason_codes must be unique")
    for index, reason in enumerate(reasons):
        _nonempty_string(reason, f"qc.reason_codes[{index}]")
    _require(qc["status"] == "pass" or bool(reasons), "non-passing QC requires at least one reason code")


def validate_measurement_record(record: Mapping[str, Any]) -> None:
    """Fail closed unless *record* satisfies the shared runtime contract."""

    root = _mapping(record, "record")
    _exact_fields(root, ROOT_FIELDS, "record")
    _require(root["schema_version"] == SCHEMA_VERSION, f"schema_version must be {SCHEMA_VERSION}")
    _nonempty_string(root["record_id"], "record.record_id")
    _require(root["track"] in TRACKS, f"track must be one of {sorted(TRACKS)}")
    _require(root["record_level"] in LEVELS_BY_TRACK[root["track"]], f"record_level is invalid for track {root['track']}")
    _nonempty_string(root["measurement_profile_id"], "record.measurement_profile_id")
    _validate_identifiers(root["identifiers"])
    _validate_record_level(root["identifiers"], root["track"], root["record_level"])
    _validate_channel_signature(root["channel_signature"])
    _validate_segmentation_model(root["segmentation_model"])
    _validate_endpoint(root["endpoint"], track=root["track"])
    _validate_sampling(root["sampling"])
    _validate_compartment(root["compartment"])
    _validate_provenance(root["provenance"])
    _validate_qc(root["qc"])
    _validate_privacy(root)


def _endpoint_aggregation_contract(endpoint: Mapping[str, Any]) -> tuple[Any, ...]:
    calculation = endpoint["calculation"]
    if calculation == "ratio":
        return (
            calculation,
            endpoint["numerator"]["unit"],
            endpoint["denominator"]["unit"],
            endpoint["unit"],
        )
    if calculation == "categorical_state":
        return (calculation, endpoint["state_vocabulary_id"])
    return (
        calculation,
        endpoint["scale_id"],
        endpoint["minimum_rank"],
        endpoint["maximum_rank"],
    )


def _supported_probability_estimators(value: Iterable[str]) -> frozenset[str]:
    _require(
        not isinstance(value, (str, bytes)),
        "supported_probability_estimators must be an iterable of profile IDs",
    )
    try:
        supported = frozenset(value)
    except TypeError as exc:
        raise MeasurementContractError(
            "supported_probability_estimators must be an iterable of profile IDs"
        ) from exc
    for profile_id in supported:
        _nonempty_string(profile_id, "supported_probability_estimators entry")
    return supported


def require_aggregation_eligible(
    record: Mapping[str, Any],
    *,
    target_estimand: str | None = None,
    supported_probability_estimators: Iterable[str] = (),
) -> None:
    """Reject records that cannot enter the declared analytical estimand.

    An omitted ``sampling.estimand_scope`` is interpreted as
    ``observed_units`` for compatibility with the original ratio contract.  A
    whole-section or whole-lung target must be declared on the record.  A
    probability design additionally needs a named estimator profile and the
    calling aggregation route must explicitly list that exact profile as one it
    implements; recording an inclusion probability alone never authorizes
    population aggregation.
    """

    validate_measurement_record(record)
    _require(record["endpoint"]["evaluability"] == "measured", "only measured endpoints can enter aggregation")
    _require(record["qc"]["status"] in {"pass", "warning"}, "failed or unrun QC cannot enter aggregation")
    _require(record["qc"]["review_status"] in {"not_required", "accepted"}, "pending or rejected review cannot enter aggregation")

    sampling = record["sampling"]
    declared_estimand = sampling.get("estimand_scope", "observed_units")
    if target_estimand is not None:
        _require(
            target_estimand in ESTIMAND_SCOPES,
            f"target_estimand must be one of {sorted(ESTIMAND_SCOPES)}",
        )
        _require(
            target_estimand == declared_estimand,
            "requested aggregation estimand does not match "
            f"sampling.estimand_scope={declared_estimand!r}",
        )

    if declared_estimand == "whole_section":
        identifiers = record["identifiers"]
        _require(
            record["record_level"] != "mouse"
            and (identifiers["section_id"] is not None or identifiers["slide_id"] is not None),
            "whole_section aggregation requires a non-mouse record with a section or slide identity",
        )

    design = sampling["design"]
    if declared_estimand in {"whole_section", "whole_lung"}:
        _require(
            design in {"exhaustive", "probability"},
            "whole-section and whole-lung aggregation require exhaustive or probability sampling",
        )
    if design == "probability":
        supported = _supported_probability_estimators(
            supported_probability_estimators
        )
        estimator_profile_id = sampling["estimator_profile_id"]
        _require(
            estimator_profile_id in supported,
            "probability sampling estimator profile "
            f"{estimator_profile_id!r} is not explicitly supported by this aggregation route",
        )


def require_aggregation_batch_eligible(
    records: Iterable[Mapping[str, Any]],
    *,
    target_estimand: str | None = None,
    supported_probability_estimators: Iterable[str] = (),
) -> None:
    """Reject a record batch that is unsafe to pool as one analytical input.

    Individual eligibility is necessary but not sufficient: two otherwise valid
    records can still represent duplicate observations or measurements produced
    with incompatible channel, model, configuration, or sampling contracts.
    Compatibility is enforced within each endpoint/reference-space/record-level
    scope. Run IDs and input artifact hashes may differ because distinct source
    observations are expected to come from distinct files and runs.
    """

    batch = list(records)
    _require(bool(batch), "aggregation batch must contain at least one record")
    supported_estimators = _supported_probability_estimators(
        supported_probability_estimators
    )

    seen_identities: dict[tuple[str, ...], int] = {}
    compatibility_by_scope: dict[tuple[str, ...], tuple[int, dict[str, Any]]] = {}

    for index, record in enumerate(batch):
        require_aggregation_eligible(
            record,
            target_estimand=target_estimand,
            supported_probability_estimators=supported_estimators,
        )
        identity = measurement_identity(record)
        previous_index = seen_identities.get(identity)
        _require(
            previous_index is None,
            "aggregation batch contains duplicate measurement identity at "
            f"records {previous_index} and {index}",
        )
        seen_identities[identity] = index

        endpoint = record["endpoint"]
        scope = (
            record["track"],
            record["record_level"],
            endpoint["calculation"],
            endpoint["endpoint_id"],
            endpoint["reference_space_id"],
        )
        model = record["segmentation_model"]
        compatibility = {
            "measurement_profile_id": record["measurement_profile_id"],
            "measurement_profile_sha256": record["provenance"]["measurement_profile_sha256"],
            "config_sha256": record["provenance"]["config_sha256"],
            "code_revision": record["provenance"]["code_revision"],
            "channel_signature": tuple(
                (channel["index"], channel["label"], channel["role"])
                for channel in record["channel_signature"]
            ),
            "segmentation_model": (
                None
                if model is None
                else (
                    model["provider"],
                    model["model_id"],
                    model["model_sha256"],
                    model["profile_id"],
                )
            ),
            "endpoint_contract": _endpoint_aggregation_contract(endpoint),
            "sampling_contract": (
                record["sampling"]["design"],
                record["sampling"]["inclusion_probability"],
                record["sampling"]["selection_source"],
                record["sampling"].get("estimand_scope", "observed_units"),
                record["sampling"].get("estimator_profile_id"),
            ),
            "compartment_contract": (
                record["compartment"]["status"],
                tuple(sorted(record["compartment"]["labels"])),
                record["compartment"]["assignment_profile_id"],
            ),
        }
        prior = compatibility_by_scope.get(scope)
        if prior is None:
            compatibility_by_scope[scope] = (index, compatibility)
            continue
        prior_index, baseline = prior
        drift = sorted(
            name for name, value in compatibility.items()
            if value != baseline[name]
        )
        _require(
            not drift,
            "aggregation batch mixes incompatible records in endpoint scope "
            f"{scope!r}; records {prior_index} and {index} differ in: "
            + ", ".join(drift),
        )


def measurement_identity(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the stable analytical key after validating the record."""

    validate_measurement_record(record)
    identifiers = record["identifiers"]
    return (
        record["track"],
        record["record_level"],
        *("" if identifiers[name] is None else identifiers[name] for name in IDENTIFIER_FIELDS),
        record["endpoint"]["endpoint_id"],
        record["endpoint"]["reference_space_id"],
        record["measurement_profile_id"],
    )
