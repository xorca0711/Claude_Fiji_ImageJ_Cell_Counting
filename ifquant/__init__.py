"""Shared contracts for IFQuant pipeline routes."""

from .adapters import (
    CategoricalStateColumnMapping,
    MeasurementAdapterError,
    MeasurementRecordContext,
    OrdinalColumnMapping,
    RatioColumnMapping,
    build_categorical_state_measurement_record,
    build_ordinal_measurement_record,
    build_ratio_measurement_record,
    write_measurement_records_jsonl,
)
from .contracts import (
    MeasurementContractError,
    measurement_identity,
    require_aggregation_batch_eligible,
    require_aggregation_eligible,
    validate_measurement_record,
)
from .route_records import (
    RouteMeasurementSpecError,
    build_preaggregation_measurement_records,
    load_measurement_record_spec,
    parse_measurement_record_spec,
)
from .stage2_index import (
    RunDeclaration,
    Stage2IndexError,
    ValidatedStage2Index,
    additive_column_marker_ids,
    build_stage2_index,
    canonical_marker_id,
    declared_marker_ids,
    validate_stage2_index,
)

__all__ = [
    "CategoricalStateColumnMapping",
    "MeasurementAdapterError",
    "MeasurementRecordContext",
    "OrdinalColumnMapping",
    "RatioColumnMapping",
    "build_categorical_state_measurement_record",
    "build_ordinal_measurement_record",
    "build_ratio_measurement_record",
    "write_measurement_records_jsonl",
    "MeasurementContractError",
    "measurement_identity",
    "require_aggregation_batch_eligible",
    "require_aggregation_eligible",
    "validate_measurement_record",
    "RouteMeasurementSpecError",
    "build_preaggregation_measurement_records",
    "load_measurement_record_spec",
    "parse_measurement_record_spec",
    "RunDeclaration",
    "Stage2IndexError",
    "ValidatedStage2Index",
    "additive_column_marker_ids",
    "build_stage2_index",
    "canonical_marker_id",
    "declared_marker_ids",
    "validate_stage2_index",
]
