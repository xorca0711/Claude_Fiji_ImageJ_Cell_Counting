"""Conservative adapters from tabular measurements to shared records.

These helpers are deliberately route-neutral.  They do not discover endpoints
from column suffixes and they do not infer that a blank is either zero or not
evaluable.  A route must provide an explicit endpoint mapping and complete,
truthful identity/provenance context before it can emit a record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import (
    EVALUABILITY,
    SCHEMA_VERSION,
    MeasurementContractError,
    measurement_identity,
    validate_measurement_record,
)


MISSING_TOKENS = frozenset({"", "NA", "N/A"})


class MeasurementAdapterError(MeasurementContractError):
    """Raised when a source row cannot be mapped without inventing meaning."""


@dataclass(frozen=True)
class RatioColumnMapping:
    """Explicit source columns and semantics for one ratio endpoint."""

    endpoint_id: str
    reference_space_id: str
    numerator_column: str
    numerator_unit: str
    denominator_column: str
    denominator_unit: str
    result_unit: str
    value_column: str | None = None


@dataclass(frozen=True)
class CategoricalStateColumnMapping:
    """Explicit source column and vocabulary for one categorical endpoint."""

    endpoint_id: str
    reference_space_id: str
    state_column: str
    state_vocabulary_id: str
    allowed_states: Sequence[str]


@dataclass(frozen=True)
class OrdinalColumnMapping:
    """Explicit source column and closed integer rank scale for one endpoint."""

    endpoint_id: str
    reference_space_id: str
    rank_column: str
    scale_id: str
    minimum_rank: int
    maximum_rank: int


@dataclass(frozen=True)
class MeasurementRecordContext:
    """Route-supplied fields that cannot be inferred safely from a CSV row."""

    track: str
    record_level: str
    identifiers: Mapping[str, str | None]
    measurement_profile_id: str
    channel_signature: Sequence[Mapping[str, Any]]
    segmentation_model: Mapping[str, Any] | None
    sampling: Mapping[str, Any]
    compartment: Mapping[str, Any]
    provenance: Mapping[str, Any]
    qc: Mapping[str, Any]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MeasurementAdapterError(message)


def _nonempty(value: Any, location: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()),
        f"{location} must be a non-empty string",
    )
    return value


def _source_number(row: Mapping[str, Any], column: str, location: str) -> float:
    _require(column in row, f"{location} column {column!r} is absent from the source row")
    raw = row[column]
    if isinstance(raw, str):
        token = raw.strip()
        _require(
            token.upper() not in MISSING_TOKENS,
            f"{location} column {column!r} is blank; missingness cannot be coerced",
        )
        try:
            numeric = float(token)
        except ValueError as exc:
            raise MeasurementAdapterError(
                f"{location} column {column!r} must be numeric"
            ) from exc
    else:
        _require(
            isinstance(raw, (int, float)) and not isinstance(raw, bool),
            f"{location} column {column!r} must be numeric",
        )
        numeric = float(raw)
    _require(math.isfinite(numeric), f"{location} column {column!r} must be finite")
    return numeric


def _source_text(row: Mapping[str, Any], column: str, location: str) -> str:
    _require(column in row, f"{location} column {column!r} is absent from the source row")
    raw = row[column]
    _require(isinstance(raw, str), f"{location} column {column!r} must be text")
    token = raw.strip()
    _require(
        token.upper() not in MISSING_TOKENS,
        f"{location} column {column!r} is blank; missingness cannot be coerced",
    )
    return token


def _source_integer(row: Mapping[str, Any], column: str, location: str) -> int:
    numeric = _source_number(row, column, location)
    _require(numeric.is_integer(), f"{location} column {column!r} must be an integer")
    return int(numeric)


def _validate_mapping(mapping: RatioColumnMapping) -> None:
    for name in (
        "endpoint_id",
        "reference_space_id",
        "numerator_column",
        "numerator_unit",
        "denominator_column",
        "denominator_unit",
        "result_unit",
    ):
        _nonempty(getattr(mapping, name), f"mapping.{name}")
    if mapping.value_column is not None:
        _nonempty(mapping.value_column, "mapping.value_column")
    _require(
        mapping.numerator_column != mapping.denominator_column,
        "numerator and denominator columns must be distinct",
    )


def _validate_categorical_mapping(mapping: CategoricalStateColumnMapping) -> tuple[str, ...]:
    for name in (
        "endpoint_id",
        "reference_space_id",
        "state_column",
        "state_vocabulary_id",
    ):
        _nonempty(getattr(mapping, name), f"mapping.{name}")
    _require(
        isinstance(mapping.allowed_states, Sequence)
        and not isinstance(mapping.allowed_states, (str, bytes)),
        "mapping.allowed_states must be a sequence of state labels",
    )
    allowed = tuple(mapping.allowed_states)
    _require(bool(allowed), "mapping.allowed_states must not be empty")
    for index, state in enumerate(allowed):
        _nonempty(state, f"mapping.allowed_states[{index}]")
    _require(len(allowed) == len(set(allowed)), "mapping.allowed_states must be unique")
    return allowed


def _validate_ordinal_mapping(mapping: OrdinalColumnMapping) -> None:
    for name in ("endpoint_id", "reference_space_id", "rank_column", "scale_id"):
        _nonempty(getattr(mapping, name), f"mapping.{name}")
    _require(
        isinstance(mapping.minimum_rank, int)
        and not isinstance(mapping.minimum_rank, bool),
        "mapping.minimum_rank must be an integer",
    )
    _require(
        isinstance(mapping.maximum_rank, int)
        and not isinstance(mapping.maximum_rank, bool),
        "mapping.maximum_rank must be an integer",
    )
    _require(
        mapping.minimum_rank <= mapping.maximum_rank,
        "mapping.minimum_rank cannot exceed mapping.maximum_rank",
    )


def _ratio_unit(mapping: RatioColumnMapping) -> str:
    if mapping.numerator_unit == mapping.denominator_unit:
        _require(
            mapping.result_unit in {"fraction", "ratio"},
            "equal-unit quantities require result_unit='fraction' or 'ratio'",
        )
        return mapping.result_unit
    expected = f"{mapping.numerator_unit}_per_{mapping.denominator_unit}"
    _require(
        mapping.result_unit == expected,
        f"unequal-unit quantities require result_unit={expected!r}",
    )
    return mapping.result_unit


def _deterministic_record_id(record: dict[str, Any]) -> str:
    # The provisional ID lets the shared validator establish that all fields
    # used by measurement_identity() are structurally valid before hashing.
    record["record_id"] = "provisional"
    identity = measurement_identity(record)
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "ifqmr-" + hashlib.sha256(payload).hexdigest()


def _build_record(
    context: MeasurementRecordContext,
    endpoint: Mapping[str, Any],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_id": "provisional",
        "track": context.track,
        "record_level": context.record_level,
        "identifiers": copy.deepcopy(dict(context.identifiers)),
        "measurement_profile_id": context.measurement_profile_id,
        "channel_signature": [
            copy.deepcopy(dict(channel)) for channel in context.channel_signature
        ],
        "segmentation_model": (
            None
            if context.segmentation_model is None
            else copy.deepcopy(dict(context.segmentation_model))
        ),
        "endpoint": copy.deepcopy(dict(endpoint)),
        "sampling": copy.deepcopy(dict(context.sampling)),
        "compartment": copy.deepcopy(dict(context.compartment)),
        "provenance": copy.deepcopy(dict(context.provenance)),
        "qc": copy.deepcopy(dict(context.qc)),
    }
    record["record_id"] = _deterministic_record_id(record)
    validate_measurement_record(record)
    return record


def build_ratio_measurement_record(
    row: Mapping[str, Any],
    mapping: RatioColumnMapping,
    context: MeasurementRecordContext,
    *,
    evaluability: str = "measured",
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Build and validate one measurement record from an explicit ratio map.

    Measured values are recomputed from source numerator and denominator
    columns.  When ``value_column`` is declared, its source value is checked but
    never trusted as the authoritative result.  Non-measured states must be
    explicit and carry a reason; their numeric values remain null even if the
    legacy row contains numbers.
    """

    _require(isinstance(row, Mapping), "source row must be an object")
    _validate_mapping(mapping)
    _require(
        evaluability in EVALUABILITY,
        f"evaluability must be one of {sorted(EVALUABILITY)}",
    )

    measured = evaluability == "measured"
    if measured:
        _require(reason_code is None, "a measured endpoint cannot carry a reason_code")
        numerator = _source_number(row, mapping.numerator_column, "numerator")
        denominator = _source_number(row, mapping.denominator_column, "denominator")
        _require(numerator >= 0, "ratio numerator cannot be negative")
        _require(denominator > 0, "ratio denominator must be greater than zero")
        value = numerator / denominator
        if mapping.value_column is not None:
            source_value = _source_number(row, mapping.value_column, "derived value")
            _require(
                math.isclose(source_value, value, rel_tol=1e-12, abs_tol=1e-15),
                f"derived value column {mapping.value_column!r} does not equal "
                "numerator / denominator",
            )
        zero_is_observed = value == 0
    else:
        _nonempty(reason_code, "reason_code")
        numerator = None
        denominator = None
        value = None
        zero_is_observed = False

    return _build_record(
        context,
        {
            "endpoint_id": mapping.endpoint_id,
            "calculation": "ratio",
            "reference_space_id": mapping.reference_space_id,
            "evaluability": evaluability,
            "numerator": {"value": numerator, "unit": mapping.numerator_unit},
            "denominator": {"value": denominator, "unit": mapping.denominator_unit},
            "value": value,
            "unit": _ratio_unit(mapping),
            "zero_is_observed": zero_is_observed,
            "reason_code": reason_code,
        },
    )


def build_categorical_state_measurement_record(
    row: Mapping[str, Any],
    mapping: CategoricalStateColumnMapping,
    context: MeasurementRecordContext,
    *,
    evaluability: str = "measured",
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Build a vocabulary-bound categorical-state record.

    State labels are case-sensitive and must be named explicitly in the mapping;
    a blank is never converted to an ``indeterminate`` state.  Non-measured
    records carry a reason and a null state.
    """

    _require(isinstance(row, Mapping), "source row must be an object")
    allowed_states = _validate_categorical_mapping(mapping)
    _require(
        evaluability in EVALUABILITY,
        f"evaluability must be one of {sorted(EVALUABILITY)}",
    )
    if evaluability == "measured":
        _require(reason_code is None, "a measured endpoint cannot carry a reason_code")
        state = _source_text(row, mapping.state_column, "categorical state")
        _require(
            state in allowed_states,
            f"categorical state {state!r} is not in mapping.allowed_states",
        )
    else:
        _nonempty(reason_code, "reason_code")
        state = None

    return _build_record(
        context,
        {
            "endpoint_id": mapping.endpoint_id,
            "calculation": "categorical_state",
            "reference_space_id": mapping.reference_space_id,
            "evaluability": evaluability,
            "state": state,
            "state_vocabulary_id": mapping.state_vocabulary_id,
            "reason_code": reason_code,
        },
    )


def build_ordinal_measurement_record(
    row: Mapping[str, Any],
    mapping: OrdinalColumnMapping,
    context: MeasurementRecordContext,
    *,
    evaluability: str = "measured",
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Build a rank record without treating ordinal distance as quantitative."""

    _require(isinstance(row, Mapping), "source row must be an object")
    _validate_ordinal_mapping(mapping)
    _require(
        evaluability in EVALUABILITY,
        f"evaluability must be one of {sorted(EVALUABILITY)}",
    )
    if evaluability == "measured":
        _require(reason_code is None, "a measured endpoint cannot carry a reason_code")
        rank = _source_integer(row, mapping.rank_column, "ordinal rank")
        _require(
            mapping.minimum_rank <= rank <= mapping.maximum_rank,
            "ordinal rank is outside the declared mapping scale",
        )
    else:
        _nonempty(reason_code, "reason_code")
        rank = None

    return _build_record(
        context,
        {
            "endpoint_id": mapping.endpoint_id,
            "calculation": "ordinal",
            "reference_space_id": mapping.reference_space_id,
            "evaluability": evaluability,
            "rank": rank,
            "minimum_rank": mapping.minimum_rank,
            "maximum_rank": mapping.maximum_rank,
            "scale_id": mapping.scale_id,
            "reason_code": reason_code,
        },
    )


def write_measurement_records_jsonl(
    path: str | os.PathLike[str],
    records: Iterable[Mapping[str, Any]],
) -> None:
    """Validate and atomically write a portable JSON Lines record set.

    This writer validates the entire set and serializes it before creating a
    temporary file, so an invalid later record cannot replace a prior canonical
    output or leave a partial new one behind.  It permits explicit unevaluable
    records; analytical pooling must separately call
    ``require_aggregation_batch_eligible``.
    """

    batch = list(records)
    _require(bool(batch), "measurement record output must contain at least one record")
    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, ...]] = set()
    serializable: list[Mapping[str, Any]] = []
    for index, record in enumerate(batch):
        try:
            validate_measurement_record(record)
            identity = measurement_identity(record)
        except MeasurementContractError as exc:
            raise MeasurementAdapterError(f"record {index} is invalid: {exc}") from exc
        record_id = record["record_id"]
        _require(record_id not in seen_ids, f"duplicate record_id in output: {record_id}")
        _require(
            identity not in seen_identities,
            f"duplicate measurement identity in output at record {index}",
        )
        seen_ids.add(record_id)
        seen_identities.add(identity)
        serializable.append(record)

    try:
        payload = "".join(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
            for record in serializable
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MeasurementAdapterError(
            "measurement records are not portable JSON values"
        ) from exc

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
