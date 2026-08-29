#!/usr/bin/env python3
"""Validate and identify one backend-neutral numerical method contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from ifquant.measurement_definition import (  # noqa: E402
    ScientificMeasurementDefinitionError,
    load_frozen_threshold_set,
    load_scientific_measurement_definition,
    resolve_measurement_method,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a pure IFQuant scientific definition and, optionally, "
            "bind it to a separate frozen threshold set."
        )
    )
    parser.add_argument("definition", type=Path)
    parser.add_argument("--threshold-set", type=Path)
    parser.add_argument("--expect-definition-sha256")
    parser.add_argument("--expect-threshold-set-sha256")
    parser.add_argument("--expect-method-instance-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        loaded = load_scientific_measurement_definition(args.definition)
        if (
            args.expect_definition_sha256 is not None
            and args.expect_definition_sha256 != loaded.canonical_sha256
        ):
            raise ScientificMeasurementDefinitionError(
                "scientific-definition hash does not match --expect-definition-sha256"
            )

        threshold_set = None
        resolved = None
        if args.threshold_set is not None:
            threshold_set = load_frozen_threshold_set(args.threshold_set)
            resolved = resolve_measurement_method(
                loaded.document, threshold_set.document
            )
            if (
                args.expect_threshold_set_sha256 is not None
                and args.expect_threshold_set_sha256
                != threshold_set.canonical_sha256
            ):
                raise ScientificMeasurementDefinitionError(
                    "threshold-set hash does not match --expect-threshold-set-sha256"
                )
            if (
                args.expect_method_instance_sha256 is not None
                and args.expect_method_instance_sha256
                != resolved.method_instance_sha256
            ):
                raise ScientificMeasurementDefinitionError(
                    "method-instance hash does not match --expect-method-instance-sha256"
                )
        elif (
            args.expect_threshold_set_sha256 is not None
            or args.expect_method_instance_sha256 is not None
        ):
            raise ScientificMeasurementDefinitionError(
                "threshold or method expectations require --threshold-set"
            )
    except ScientificMeasurementDefinitionError as exc:
        print(f"MEASUREMENT_DEFINITION_ERROR: {exc}", file=sys.stderr)
        return 2

    document = loaded.document
    summary = {
        "authorization": "none",
        "canonical_sha256": loaded.canonical_sha256,
        "definition_id": document["definition_id"],
        "definition_version": document["definition_version"],
        "endpoint_id": document["endpoint"]["endpoint_id"],
        "file_sha256": loaded.file_sha256,
        "schema_version": document["schema_version"],
        "size_bytes": loaded.size_bytes,
        "validation_scope": (
            "contract identity only; not backend equivalence, threshold authority, "
            "scientific validation, or promotion"
        ),
    }
    if threshold_set is not None and resolved is not None:
        summary.update(
            {
                "method_instance_sha256": resolved.method_instance_sha256,
                "threshold_set_file_sha256": threshold_set.file_sha256,
                "threshold_set_id": threshold_set.document["threshold_set_id"],
                "threshold_set_sha256": threshold_set.canonical_sha256,
                "threshold_set_version": threshold_set.document[
                    "threshold_set_version"
                ],
            }
        )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
