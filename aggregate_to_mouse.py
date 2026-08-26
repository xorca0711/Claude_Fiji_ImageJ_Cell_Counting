#!/usr/bin/env python3
"""
aggregate_to_mouse.py
=====================================================================
Roll the per-region `run_summary.csv` produced by IF_Quant_Pipeline.groovy
up to the BIOLOGICAL replicate (mouse) level, then summarise per group.

Why this exists
---------------
The image pipeline emits one row per tissue/lesion region. Several sections
(and several regions per section) come from the SAME animal. Statistics for
this study must use n = MICE, not n = sections/regions. Averaging region rows
directly would pseudo-replicate and inflate significance.

Pooling is area-weighted (the statistically correct pooling for a section):
  * KRT5 pod area fraction   = sum(pod_area) / sum(tissue_area)      per mouse
  * marker density (/mm2)    = sum(morphology-authoritative pos_count)
                               / sum(tissue_area_mm2) per mouse
  * mean pod size            = sum(pod_area) / sum(n_pods)           per mouse
Counts and areas are summed; thresholds are averaged (QC only).

Outputs (written next to the input, or to --outdir):
  mouse_level_summary.csv  -- one row per (mouse_id, genotype, condition, panel)
  group_level_summary.csv  -- mean / sd / sem / n_mice per (genotype, condition,
                              panel, metric); n_mice is the real n for stats.
  measurement_records.jsonl -- optional schema-v2 source-observation records
                               when an explicit mapping spec is supplied.

Usage
-----
  python3 aggregate_to_mouse.py /path/to/analysis_output/run_summary.csv
  python3 aggregate_to_mouse.py run_summary.csv --outdir ./stats
  python3 aggregate_to_mouse.py run_summary.csv --endpoint-csv endpoint_areas.csv \
      --endpoint-spec config/endpoints/dysplastic_over_damaged.json
  python3 aggregate_to_mouse.py run_summary.csv --sampling-unit field \
      --measurement-record-spec reviewed-record-map.json

No third-party dependencies (standard library only).
=====================================================================
"""
import argparse
import ast
import csv
import datetime
import hashlib
import io
import json
import math
import os
import platform
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

try:
    from ifquant.adapters import write_measurement_records_jsonl
    from ifquant.route_records import (
        RouteMeasurementSpecError,
        build_preaggregation_measurement_records,
        load_measurement_record_spec,
    )
except ImportError:  # pragma: no cover
    sys.exit("ERROR: the ifquant package must be importable beside this script")

# Columns that identify a row rather than measure something.
KEY_COLS = ["mouse_id", "genotype", "condition", "panel"]
ROW_ID_COLS = ["image", "region", "section_id"]
WSI_UNIFORM_PROVENANCE = [
    "aggregation_contract_version",
    "stage2_source_mode",
    "stage2_index_schema_version",
    "stage1_profile_sha256",
    "stage1_script_sha256",
    "stage2_script_sha256",
    "resolved_config_sha256",
    "configuration_artifact_set_sha256",
    "runtime_profile_sha256",
    "measurement_profile_sha256",
    "declared_channel_map_sha256",
    "ordered_channel_signature",
    "channel_signature_authority",
]
WSI_PER_SLIDE_PROVENANCE = [
    "stage2_index_sha256",
    "stage1_manifest_sha256",
    "stage1_source_metadata_sha256",
    "source_package_sha256",
    "tile_candidate_manifest_sha256",
    "tile_manifest_sha256",
    "parameter_set_sha256",
]
WSI_INDICATOR_COLUMNS = {
    "slide",
    "n_tiles_expected",
    "n_tiles_analyzed",
    "n_tiles_missing",
    "tile_coverage_fraction",
    "stage1_core_tissue_area_um2",
    "stage2_region_area_um2",
    "stage2_source_mode",
    "stage2_index_sha256",
}

AGGREGATION_AUDIT_SCHEMA_VERSION = "1.2.0"
STAGE3_AUDIT_FILENAME = "slide_level_summary.audit.json"
STAGE4_AUDIT_FILENAME = "mouse_group_aggregation.audit.json"
STAGE4_MEASUREMENT_RECORD_FILENAME = "measurement_records.jsonl"
DIRECT_RUN_MANIFEST_SCHEMA_VERSION = "2.0.0"
DIRECT_OUTPUT_ARTIFACT_DIGEST_CONTRACT = (
    "sha256_utf8_lf_role_tab_relative_path_tab_size_bytes_tab_sha256_v1"
)
MEASUREMENT_RECORD_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "measurement-record.schema.json"
)

REPOSITORY_PYTHON_CODE_ROLES = {
    "ifquant/__init__.py": "ifquant_package_init",
    "ifquant/stage2_index.py": "stage2_index_contract",
    "ifquant/contracts.py": "measurement_record_contract",
    "ifquant/adapters.py": "measurement_record_builder",
    "ifquant/route_records.py": "measurement_record_route_adapter",
}
CODE_ROLE_PATHS_BY_STAGE = {
    "stage3_slide_aggregation": {
        "stage3_aggregator": "aggregate_tiles_to_slide.py",
        "aggregation_library": "aggregate_to_mouse.py",
        **{role: path for path, role in REPOSITORY_PYTHON_CODE_ROLES.items()},
    },
    "stage4_mouse_aggregation": {
        "stage4_aggregator": "aggregate_to_mouse.py",
        **{role: path for path, role in REPOSITORY_PYTHON_CODE_ROLES.items()},
    },
}
REQUIRED_CODE_ROLES_BY_STAGE = {
    stage: frozenset(role_paths)
    for stage, role_paths in CODE_ROLE_PATHS_BY_STAGE.items()
}


class AggregationAuditError(ValueError):
    """Raised when an aggregation audit is malformed or its bytes drift."""


class DirectRunManifestError(AggregationAuditError):
    """Raised when direct-confocal output lacks sealed production authority."""


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value):
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_reference(path, audit_path):
    """Return a portable reference that remains resolvable by the audit."""
    absolute = os.path.abspath(os.fspath(path))
    audit_dir = os.path.dirname(os.path.abspath(os.fspath(audit_path)))
    try:
        relative = os.path.relpath(absolute, audit_dir)
    except ValueError:
        # Windows cannot express a relative path across volumes. Retain the
        # absolute reference in that uncommon case so validation can still
        # re-hash the exact declared artifact instead of silently skipping it.
        return absolute.replace("\\", "/"), "absolute"
    return relative.replace("\\", "/"), "audit_relative"


def artifact_descriptor(path, role, audit_path, *, digest=None, size_bytes=None):
    """Describe one exact file snapshot for an aggregation audit."""
    absolute = os.path.abspath(os.fspath(path))
    if digest is None or size_bytes is None:
        payload = Path(absolute).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        size_bytes = len(payload)
    reference, path_kind = _artifact_reference(absolute, audit_path)
    return {
        "role": str(role),
        "path": reference,
        "path_kind": path_kind,
        "size_bytes": int(size_bytes),
        "sha256": str(digest),
    }


def _repository_python_path(path, repository_root):
    root = Path(repository_root).resolve(strict=True)
    try:
        resolved = Path(path).resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AggregationAuditError(
            f"repository Python code escapes or is missing from {root}: {path}"
        ) from exc
    if resolved.suffix.lower() != ".py" or not resolved.is_file():
        raise AggregationAuditError(
            f"repository code artifact is not a Python source file: {resolved}"
        )
    return resolved, relative.as_posix()


def _local_module_paths(module_name, repository_root):
    """Resolve only source modules owned by this repository.

    Standard-library and environment modules have no matching source under the
    repository root and are deliberately ignored. Importing a package submodule
    also executes each repository-owned package ``__init__.py`` on its path, so
    those files are part of the closure.
    """

    if not module_name:
        return set()
    parts = module_name.split(".")
    if any(not part or not part.isidentifier() for part in parts):
        return set()
    root = Path(repository_root)
    resolved = set()
    for length in range(1, len(parts) + 1):
        package_init = root.joinpath(*parts[:length], "__init__.py")
        if package_init.is_file():
            resolved.add(package_init.resolve(strict=True))
    module_file = root.joinpath(*parts).with_suffix(".py")
    if module_file.is_file():
        resolved.add(module_file.resolve(strict=True))
    package_init = root.joinpath(*parts, "__init__.py")
    if package_init.is_file():
        resolved.add(package_init.resolve(strict=True))
    return resolved


def _repository_import_snapshot(source_path, repository_root):
    source_path, relative = _repository_python_path(source_path, repository_root)
    try:
        payload = source_path.read_bytes()
        tree = ast.parse(payload, filename=relative)
    except (OSError, SyntaxError, ValueError) as exc:
        raise AggregationAuditError(
            f"cannot derive repository import closure from {relative}: {exc}"
        ) from exc

    relative_path = Path(relative)
    package_parts = list(relative_path.parent.parts)
    if relative_path.name == "__init__.py":
        package_parts = list(relative_path.parent.parts)
    imports = set()
    for node in ast.walk(tree):
        module_names = []
        alias_suffixes = []
        if isinstance(node, ast.Import):
            module_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                trim = node.level - 1
                if trim > len(package_parts):
                    raise AggregationAuditError(
                        f"relative import escapes repository package in {relative}"
                    )
                base = package_parts[: len(package_parts) - trim]
                module = base + (node.module.split(".") if node.module else [])
                module_names.append(".".join(module))
                alias_suffixes.extend(alias.name for alias in node.names)
            elif node.module:
                module_names.append(node.module)
                alias_suffixes.extend(alias.name for alias in node.names)
        for module_name in module_names:
            imports.update(_local_module_paths(module_name, repository_root))
            # ``from package import submodule`` may name a source module in an
            # alias rather than in ImportFrom.module. Non-module attributes
            # simply do not resolve under the repository root.
            for suffix in alias_suffixes:
                if suffix != "*":
                    imports.update(
                        _local_module_paths(
                            f"{module_name}.{suffix}", repository_root
                        )
                    )
    return imports, hashlib.sha256(payload).hexdigest(), len(payload)


def repository_python_code_closure(entry_scripts, audit_path):
    """Describe the deterministic repository-owned Python import closure.

    ``entry_scripts`` is an iterable of ``(role, path)`` pairs. The closure is
    derived recursively from Python import statements, restricted to ``.py``
    sources below the repository root. This avoids binding standard-library,
    site-package, bytecode-cache, and ambient test-runner modules while ensuring
    package initializers and transitive IFQuant helpers cannot be omitted.
    """

    repository_root = Path(__file__).resolve().parent
    roles_by_path = {}
    for role, path in entry_scripts:
        resolved, relative = _repository_python_path(path, repository_root)
        if resolved in roles_by_path and roles_by_path[resolved] != str(role):
            raise AggregationAuditError(
                f"repository code source {relative} has conflicting roles"
            )
        roles_by_path[resolved] = str(role)
    if not roles_by_path:
        raise AggregationAuditError("repository Python closure needs an entry script")

    discovered = set()
    pending = set(roles_by_path)
    byte_snapshots = {}
    while pending:
        source_path = min(
            pending,
            key=lambda path: path.relative_to(repository_root).as_posix(),
        )
        pending.remove(source_path)
        if source_path in discovered:
            continue
        discovered.add(source_path)
        dependencies, digest, size_bytes = _repository_import_snapshot(
            source_path, repository_root
        )
        byte_snapshots[source_path] = (digest, size_bytes)
        pending.update(
            dependency
            for dependency in dependencies
            if dependency not in discovered
        )

    role_owners = {}
    for source_path in discovered:
        relative = source_path.relative_to(repository_root).as_posix()
        role = roles_by_path.get(
            source_path,
            REPOSITORY_PYTHON_CODE_ROLES.get(
                relative, f"repository_python:{relative}"
            ),
        )
        if role in role_owners and role_owners[role] != source_path:
            raise AggregationAuditError(
                f"repository Python closure role is duplicated: {role!r}"
            )
        role_owners[role] = source_path

    artifacts = []
    tracked_snapshots = []
    for role, source_path in sorted(role_owners.items()):
        digest, size_bytes = byte_snapshots[source_path]
        descriptor = artifact_descriptor(
            source_path,
            role,
            audit_path,
            digest=digest,
            size_bytes=size_bytes,
        )
        descriptor["repository_path"] = source_path.relative_to(
            repository_root
        ).as_posix()
        artifacts.append(descriptor)
        tracked_snapshots.append((descriptor, os.fspath(source_path)))
    # Catch any code drift after parsing but before handing the closed snapshot
    # to the caller. Publication performs the same check again after analysis.
    for descriptor, source_path in tracked_snapshots:
        verify_artifact_descriptor(descriptor, source_path)
    return artifacts, tracked_snapshots


def _require_stage_code_closure(stage, code_artifacts):
    expected_paths = CODE_ROLE_PATHS_BY_STAGE.get(stage)
    if expected_paths is None:
        return
    observed = {
        artifact.get("role")
        for artifact in code_artifacts
        if isinstance(artifact, dict)
    }
    required = set(expected_paths)
    missing = sorted(required - observed)
    unexpected = sorted(observed - required)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing=" + ", ".join(missing))
        if unexpected:
            details.append("unexpected=" + ", ".join(unexpected))
        raise AggregationAuditError(
            f"{stage} audit does not carry the exact repository Python code "
            "closure roles: " + "; ".join(details)
        )
    by_role = {artifact["role"]: artifact for artifact in code_artifacts}
    for role, relative_path in expected_paths.items():
        actual = by_role[role].get("repository_path")
        if actual != relative_path:
            raise AggregationAuditError(
                f"{stage} code role {role!r} declares repository_path "
                f"{actual!r}, expected {relative_path!r}"
            )


def verify_artifact_descriptor(descriptor, path):
    """Fail closed if a file no longer matches its captured descriptor."""
    absolute = os.path.abspath(os.fspath(path))
    try:
        size = os.path.getsize(absolute)
    except OSError as exc:
        raise AggregationAuditError(
            f"artifact {descriptor.get('role')!r} is unavailable: {absolute}: {exc}"
        ) from exc
    if size != descriptor["size_bytes"]:
        raise AggregationAuditError(
            f"artifact {descriptor['role']!r} size drift: expected "
            f"{descriptor['size_bytes']}, found {size}: {absolute}"
        )
    digest = _sha256_file(absolute)
    if digest != descriptor["sha256"]:
        raise AggregationAuditError(
            f"artifact {descriptor['role']!r} SHA-256 drift: expected "
            f"{descriptor['sha256']}, found {digest}: {absolute}"
        )


def _runtime_record():
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_cache_tag": str(getattr(sys.implementation, "cache_tag", "")),
        "python_compiler": platform.python_compiler(),
        "platform": platform.platform(),
    }


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _sorted_artifacts(artifacts):
    return sorted(
        (dict(artifact) for artifact in artifacts),
        key=lambda artifact: (artifact["role"], artifact["path"]),
    )


def _repository_code_set_sha256(code_artifacts):
    """Return a location-independent digest for repository Python sources.

    The existing code-set seal intentionally includes audit-relative paths.
    This companion seal remains stable when a complete run folder/repository
    is moved because it hashes only role, repository path, and source bytes.
    Non-production test audits may carry ad-hoc code without a repository path;
    those audits explicitly receive ``None`` instead of pretending portability.
    """
    portable = []
    for artifact in code_artifacts:
        repository_path = artifact.get("repository_path")
        if repository_path is None:
            return None
        if (
            not isinstance(repository_path, str)
            or not repository_path
            or repository_path.startswith("/")
            or ".." in Path(repository_path).parts
            or "\\" in repository_path
        ):
            raise AggregationAuditError(
                "code artifact repository_path is not a safe portable path: "
                + repr(repository_path)
            )
        portable.append(
            {
                "role": artifact["role"],
                "repository_path": repository_path,
                "sha256": artifact["sha256"],
            }
        )
    return _canonical_sha256(
        sorted(portable, key=lambda item: (item["role"], item["repository_path"]))
    )


def build_aggregation_audit(
        *, stage, arguments, code_artifacts, input_artifacts, output_artifacts,
        generated_utc=None, runtime=None):
    """Build and content-seal one complete aggregation publication record."""
    sorted_code_artifacts = _sorted_artifacts(code_artifacts)
    _require_stage_code_closure(stage, sorted_code_artifacts)
    document = {
        "schema_version": AGGREGATION_AUDIT_SCHEMA_VERSION,
        "stage": stage,
        "status": "complete",
        "generated_utc": generated_utc or _utc_now(),
        "runtime": runtime or _runtime_record(),
        "arguments": dict(arguments),
        "code_artifacts": sorted_code_artifacts,
        "input_artifacts": _sorted_artifacts(input_artifacts),
        "output_artifacts": _sorted_artifacts(output_artifacts),
    }
    document["repository_code_set_sha256"] = _repository_code_set_sha256(
        document["code_artifacts"]
    )
    all_roles = [
        artifact["role"]
        for section in ("code_artifacts", "input_artifacts", "output_artifacts")
        for artifact in document[section]
    ]
    if len(all_roles) != len(set(all_roles)):
        raise AggregationAuditError("aggregation audit artifact roles must be unique")
    for section in ("code_artifacts", "input_artifacts", "output_artifacts"):
        document[section.replace("artifacts", "set_sha256")] = _canonical_sha256(
            document[section]
        )
    document["audit_payload_sha256"] = _canonical_sha256(document)
    return document


def write_json_atomic(path, document):
    """Write deterministic JSON through an unpredictable same-directory file."""
    path = os.path.abspath(os.fspath(path))
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=os.path.dirname(path),
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def _resolved_artifact_path(descriptor, audit_path):
    reference = descriptor["path"]
    if descriptor["path_kind"] == "audit_relative":
        native = reference.replace("/", os.sep)
        return os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(audit_path)), native)
        )
    if descriptor["path_kind"] == "absolute":
        if not os.path.isabs(reference):
            raise AggregationAuditError(
                f"artifact {descriptor['role']!r} declares a non-absolute path"
            )
        return os.path.abspath(reference)
    raise AggregationAuditError(
        f"artifact {descriptor['role']!r} has unsupported path_kind "
        f"{descriptor['path_kind']!r}"
    )


def _validate_aggregation_audit_payload(
        payload, audit_path, *, expected_stage=None, expected_artifacts=None,
        verify_files=True):
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AggregationAuditError(f"aggregation audit is unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise AggregationAuditError("aggregation audit root must be a JSON object")
    required = {
        "schema_version", "stage", "status", "generated_utc", "runtime",
        "arguments", "code_artifacts", "input_artifacts", "output_artifacts",
        "code_set_sha256", "input_set_sha256", "output_set_sha256",
        "repository_code_set_sha256",
        "audit_payload_sha256",
    }
    missing = sorted(required - set(document))
    if missing:
        raise AggregationAuditError(
            "aggregation audit is missing required fields: " + ", ".join(missing)
        )
    if document["schema_version"] != AGGREGATION_AUDIT_SCHEMA_VERSION:
        raise AggregationAuditError(
            "unsupported aggregation audit schema_version: "
            + repr(document["schema_version"])
        )
    if document["status"] != "complete":
        raise AggregationAuditError("aggregation audit status is not complete")
    if expected_stage is not None and document["stage"] != expected_stage:
        raise AggregationAuditError(
            f"aggregation audit stage mismatch: expected {expected_stage!r}, "
            f"found {document['stage']!r}"
        )
    if not isinstance(document["runtime"], dict) or not document["runtime"]:
        raise AggregationAuditError("aggregation audit runtime must be a non-empty object")
    if not isinstance(document["arguments"], dict):
        raise AggregationAuditError("aggregation audit arguments must be an object")
    try:
        parsed_utc = datetime.datetime.fromisoformat(
            str(document["generated_utc"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise AggregationAuditError("aggregation audit generated_utc is invalid") from exc
    if parsed_utc.tzinfo is None:
        raise AggregationAuditError("aggregation audit generated_utc must include UTC")

    expected_payload_sha256 = document["audit_payload_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", str(expected_payload_sha256)) is None:
        raise AggregationAuditError("aggregation audit payload SHA-256 is malformed")
    unsealed = dict(document)
    del unsealed["audit_payload_sha256"]
    actual_payload_sha256 = _canonical_sha256(unsealed)
    if actual_payload_sha256 != expected_payload_sha256:
        raise AggregationAuditError(
            "aggregation audit payload SHA-256 drift: expected "
            f"{expected_payload_sha256}, found {actual_payload_sha256}"
        )

    artifacts_by_role = {}
    expected_artifacts = {
        str(role): os.path.abspath(os.fspath(path))
        for role, path in (expected_artifacts or {}).items()
    }
    for section in ("code_artifacts", "input_artifacts", "output_artifacts"):
        artifacts = document[section]
        if not isinstance(artifacts, list) or not artifacts:
            raise AggregationAuditError(f"aggregation audit {section} must be non-empty")
        expected_set_hash = document[section.replace("artifacts", "set_sha256")]
        if re.fullmatch(r"[0-9a-f]{64}", str(expected_set_hash)) is None:
            raise AggregationAuditError(
                f"aggregation audit {section} set SHA-256 is malformed"
            )
        actual_set_hash = _canonical_sha256(artifacts)
        if actual_set_hash != expected_set_hash:
            raise AggregationAuditError(
                f"aggregation audit {section} set SHA-256 drift: expected "
                f"{expected_set_hash}, found {actual_set_hash}"
            )
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise AggregationAuditError(f"aggregation audit {section} entry is not an object")
            artifact_required = {"role", "path", "path_kind", "size_bytes", "sha256"}
            artifact_missing = sorted(artifact_required - set(artifact))
            if artifact_missing:
                raise AggregationAuditError(
                    f"aggregation audit {section} entry is missing: "
                    + ", ".join(artifact_missing)
                )
            role = artifact["role"]
            if not isinstance(role, str) or not role:
                raise AggregationAuditError("aggregation audit artifact role must be non-empty")
            if role in artifacts_by_role:
                raise AggregationAuditError(
                    f"aggregation audit artifact role is duplicated: {role!r}"
                )
            if (not isinstance(artifact["size_bytes"], int)
                    or isinstance(artifact["size_bytes"], bool)
                    or artifact["size_bytes"] < 0):
                raise AggregationAuditError(
                    f"aggregation audit artifact {role!r} has invalid size_bytes"
                )
            if re.fullmatch(r"[0-9a-f]{64}", str(artifact["sha256"])) is None:
                raise AggregationAuditError(
                    f"aggregation audit artifact {role!r} has malformed SHA-256"
                )
            if not isinstance(artifact["path"], str) or not artifact["path"]:
                raise AggregationAuditError(
                    f"aggregation audit artifact {role!r} has an invalid path"
                )
            artifacts_by_role[role] = artifact
            if verify_files:
                actual_path = expected_artifacts.get(
                    role, _resolved_artifact_path(artifact, audit_path)
                )
                verify_artifact_descriptor(artifact, actual_path)

    _require_stage_code_closure(document["stage"], document["code_artifacts"])
    expected_repository_code_set = _repository_code_set_sha256(
        document["code_artifacts"]
    )
    if document["repository_code_set_sha256"] != expected_repository_code_set:
        raise AggregationAuditError(
            "aggregation audit repository code-set SHA-256 drift"
        )

    absent_expected = sorted(set(expected_artifacts) - set(artifacts_by_role))
    if absent_expected:
        raise AggregationAuditError(
            "aggregation audit lacks expected artifact roles: "
            + ", ".join(absent_expected)
        )
    return document


def read_aggregation_audit_snapshot(
        audit_path, *, expected_stage=None, expected_artifacts=None,
        verify_files=True):
    """Validate an audit and return its document plus the exact parsed bytes."""
    try:
        payload = Path(audit_path).read_bytes()
    except OSError as exc:
        raise AggregationAuditError(
            f"aggregation audit is unavailable: {audit_path}: {exc}"
        ) from exc
    document = _validate_aggregation_audit_payload(
        payload,
        os.fspath(audit_path),
        expected_stage=expected_stage,
        expected_artifacts=expected_artifacts,
        verify_files=verify_files,
    )
    return document, payload, hashlib.sha256(payload).hexdigest()


def validate_aggregation_audit(
        audit_path, *, expected_stage=None, expected_artifacts=None,
        verify_files=True):
    """Validate structure, content seals, and every declared artifact byte."""
    document, _, _ = read_aggregation_audit_snapshot(
        audit_path,
        expected_stage=expected_stage,
        expected_artifacts=expected_artifacts,
        verify_files=verify_files,
    )
    return document


def _num(v):
    """Parse a cell to float; blanks / non-numeric -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.upper() == "NA":
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_rows_snapshot(path):
    """Parse and hash the same immutable byte snapshot of a CSV input."""
    try:
        payload = Path(path).read_bytes()
        text = payload.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        sys.exit(f"ERROR: cannot read {path}: {exc}")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        sys.exit(f"ERROR: {path} is empty or has no header.")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        sys.exit(f"ERROR: {path} contains duplicate CSV header columns.")
    rows = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                sys.exit(
                    f"ERROR: {path} row {row_number} has more fields than its header."
                )
            if not all(value is None or isinstance(value, str) for value in row.values()):
                sys.exit(f"ERROR: {path} row {row_number} contains a non-text CSV value.")
            if any((value or "").strip() for value in row.values()):
                rows.append(row)
    except csv.Error as exc:
        sys.exit(f"ERROR: {path} contains malformed CSV: {exc}")
    return (
        reader.fieldnames,
        rows,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )


def read_rows(path):
    header, rows, _, _ = read_rows_snapshot(path)
    return header, rows


def _strict_json_object(payload, label):
    def reject_constant(token):
        raise ValueError(f"non-finite JSON constant {token}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DirectRunManifestError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DirectRunManifestError(f"{label} root must be an object")
    return document


def _stable_file_snapshot(path, label):
    """Hash one regular non-symlink file and reject concurrent byte drift."""
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise DirectRunManifestError(
                f"{label} is not a regular non-symlink file: {candidate}"
            )
        before = candidate.stat()
        digest = _sha256_file(candidate)
        after = candidate.stat()
    except DirectRunManifestError:
        raise
    except OSError as exc:
        raise DirectRunManifestError(f"{label} is unreadable: {candidate}: {exc}") from exc
    identity_before = (
        before.st_size,
        before.st_mtime_ns,
        getattr(before, "st_ino", 0),
        getattr(before, "st_dev", 0),
    )
    identity_after = (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_ino", 0),
        getattr(after, "st_dev", 0),
    )
    if identity_before != identity_after:
        raise DirectRunManifestError(f"{label} changed while it was hashed")
    return digest, before.st_size


def _safe_relative_path(root, relative_path, label):
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise DirectRunManifestError(f"{label} must be a non-empty relative path")
    normalized = relative_path.replace("\\", "/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise DirectRunManifestError(f"{label} is unsafe: {relative_path!r}")
    try:
        root_path = Path(root).resolve(strict=True)
        candidate = root_path
        for part in normalized.split("/"):
            candidate = candidate / part
            if candidate.is_symlink():
                raise DirectRunManifestError(
                    f"{label} traverses a symbolic link: {relative_path!r}"
                )
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_path)
    except DirectRunManifestError:
        raise
    except (OSError, ValueError) as exc:
        raise DirectRunManifestError(
            f"{label} escapes or is unavailable below {root}: {relative_path!r}"
        ) from exc
    return resolved, normalized


def _content_identity(value, label):
    if not isinstance(value, dict):
        raise DirectRunManifestError(f"{label} must be an object")
    required = {"name", "size_bytes", "sha256"}
    if set(value) != required:
        raise DirectRunManifestError(
            f"{label} must contain exactly name, size_bytes, and sha256"
        )
    if not isinstance(value["name"], str) or not value["name"]:
        raise DirectRunManifestError(f"{label}.name must be non-empty")
    if (
        not isinstance(value["size_bytes"], int)
        or isinstance(value["size_bytes"], bool)
        or value["size_bytes"] < 0
    ):
        raise DirectRunManifestError(f"{label}.size_bytes must be non-negative")
    if re.fullmatch(r"[0-9a-f]{64}", str(value["sha256"])) is None:
        raise DirectRunManifestError(f"{label}.sha256 must be lowercase SHA-256")
    return {
        "name": value["name"],
        "size_bytes": value["size_bytes"],
        "sha256": value["sha256"],
    }


def _require_current_content(path, declared, label):
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
        raise DirectRunManifestError(f"{label} path must be non-empty")
    identity = _content_identity(declared, label)
    digest, size_bytes = _stable_file_snapshot(path, label)
    if Path(path).name != identity["name"]:
        raise DirectRunManifestError(
            f"{label} filename disagrees with the sealed identity"
        )
    if digest != identity["sha256"] or size_bytes != identity["size_bytes"]:
        raise DirectRunManifestError(
            f"{label} current bytes disagree with the sealed size/SHA-256"
        )
    return digest, size_bytes


def _direct_output_artifact_set_sha256(artifacts):
    lines = []
    for artifact in sorted(
        artifacts, key=lambda item: (item["role"], item["relative_path"])
    ):
        lines.append(
            f"{artifact['role']}\t{artifact['relative_path']}\t"
            f"{artifact['size_bytes']}\t{artifact['sha256']}\n"
        )
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _validate_direct_output_artifacts(manifest, output_root):
    artifacts = manifest.get("output_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise DirectRunManifestError(
            "direct run manifest output_artifacts must be non-empty"
        )
    normalized = []
    seen = set()
    for index, artifact in enumerate(artifacts):
        label = f"direct run manifest output_artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise DirectRunManifestError(f"{label} must be an object")
        expected = {"role", "relative_path", "size_bytes", "sha256"}
        role = artifact.get("role")
        if role == "image_params":
            expected.add("output_key")
        if set(artifact) != expected:
            raise DirectRunManifestError(
                f"{label} has unexpected or missing fields"
            )
        if role not in {"run_summary", "summary_workbook", "image_params"}:
            raise DirectRunManifestError(f"{label}.role is unsupported: {role!r}")
        path, relative = _safe_relative_path(
            output_root, artifact.get("relative_path"), f"{label}.relative_path"
        )
        key = (role, relative)
        if key in seen:
            raise DirectRunManifestError(f"{label} duplicates role/path {key!r}")
        seen.add(key)
        size_bytes = artifact.get("size_bytes")
        sha256 = artifact.get("sha256")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 1
            or re.fullmatch(r"[0-9a-f]{64}", str(sha256)) is None
        ):
            raise DirectRunManifestError(f"{label} has invalid size/SHA-256")
        digest, current_size = _stable_file_snapshot(path, label)
        if digest != sha256 or current_size != size_bytes:
            raise DirectRunManifestError(
                f"{label} current bytes disagree with its sealed size/SHA-256"
            )
        record = dict(artifact)
        record["relative_path"] = relative
        record["path"] = path
        normalized.append(record)
    declared_order = [
        (item["role"], item["relative_path"]) for item in normalized
    ]
    if declared_order != sorted(declared_order):
        raise DirectRunManifestError(
            "direct run manifest output_artifacts are not canonically ordered"
        )
    if (
        manifest.get("output_artifact_digest_contract")
        != DIRECT_OUTPUT_ARTIFACT_DIGEST_CONTRACT
    ):
        raise DirectRunManifestError(
            "direct run manifest output artifact digest contract is unsupported"
        )
    expected_digest = manifest.get("output_artifact_set_sha256")
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(expected_digest)) is None
        or expected_digest != _direct_output_artifact_set_sha256(normalized)
    ):
        raise DirectRunManifestError(
            "direct run manifest output artifact-set SHA-256 is invalid"
        )
    return normalized


def _artifact_for_direct_input(path, role, audit_path, digest, size_bytes):
    return artifact_descriptor(
        path, role, audit_path, digest=digest, size_bytes=size_bytes
    )


def validate_direct_run_manifest(
    manifest_path,
    summary_path,
    summary_header,
    summary_rows,
    summary_digest,
    summary_size,
    audit_path,
):
    """Validate a sealed modern engine run for schema-v2 direct records.

    This is intentionally stricter than legacy descriptive aggregation. It
    checks every successful params record and re-hashes current raw, engine,
    model, and runtime bytes before any measurement record may be pooled.
    """
    manifest_path = Path(manifest_path).absolute()
    if manifest_path.is_symlink():
        raise DirectRunManifestError(
            "direct run manifest must not be a symbolic link"
        )
    manifest_path = manifest_path.resolve(strict=True)
    manifest_digest, manifest_size = _stable_file_snapshot(
        manifest_path, "direct run manifest"
    )
    manifest_payload = manifest_path.read_bytes()
    if (
        hashlib.sha256(manifest_payload).hexdigest() != manifest_digest
        or len(manifest_payload) != manifest_size
    ):
        raise DirectRunManifestError(
            "direct run manifest changed while it was being parsed"
        )
    manifest = _strict_json_object(manifest_payload, "direct run manifest")
    if manifest.get("run_manifest_schema_version") != DIRECT_RUN_MANIFEST_SCHEMA_VERSION:
        raise DirectRunManifestError(
            "schema-v2 direct publication requires run_manifest_schema_version "
            + DIRECT_RUN_MANIFEST_SCHEMA_VERSION
        )
    if manifest.get("publication_contract") != "sealed_outputs_manifest_published_last":
        raise DirectRunManifestError(
            "direct run manifest lacks the audit-last sealed-output contract"
        )
    if manifest.get("status") != "complete" or manifest.get("publication_status") != "sealed_complete":
        raise DirectRunManifestError(
            "direct run manifest is incomplete, capped, or failed"
        )
    for field in (
        "analytical_coverage_complete",
        "input_content_verified_at_manifest_publication",
        "engine_script_verified_before_and_after",
        "engine_script_verified_at_manifest_publication",
    ):
        if manifest.get(field) is not True:
            raise DirectRunManifestError(f"direct run manifest {field} is not true")
    for field in ("failure_count", "output_failure_count", "max_images_excluded_count"):
        value = manifest.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value != 0:
            raise DirectRunManifestError(
                f"direct run manifest {field} must be zero for production records"
            )
    counts = [
        manifest.get("success_count"),
        manifest.get("analytical_input_count"),
        manifest.get("intended_analytical_input_count"),
    ]
    if (
        any(not isinstance(value, int) or isinstance(value, bool) for value in counts)
        or counts[0] < 1
        or len(set(counts)) != 1
    ):
        raise DirectRunManifestError(
            "direct run manifest success/analytical/intended counts must match and be positive"
        )
    if manifest.get("summary_workbook_status") != "complete":
        raise DirectRunManifestError("direct run summary workbook is not complete")

    output_root = manifest_path.parent.resolve(strict=True)
    artifacts = _validate_direct_output_artifacts(manifest, output_root)
    by_role = defaultdict(list)
    for artifact in artifacts:
        by_role[artifact["role"]].append(artifact)
    if len(by_role["run_summary"]) != 1 or len(by_role["summary_workbook"]) != 1:
        raise DirectRunManifestError(
            "complete direct run must seal exactly one summary and one workbook"
        )
    if set(by_role) != {"run_summary", "summary_workbook", "image_params"}:
        raise DirectRunManifestError("direct run output artifact roles are incomplete")
    summary_artifact = by_role["run_summary"][0]
    if summary_artifact["relative_path"] != "run_summary.csv":
        raise DirectRunManifestError(
            "direct run summary artifact must be canonical run_summary.csv"
        )
    try:
        same_summary = summary_artifact["path"].samefile(summary_path)
    except OSError as exc:
        raise DirectRunManifestError(f"cannot resolve sealed run summary: {exc}") from exc
    if (
        not same_summary
        or summary_artifact["sha256"] != summary_digest
        or summary_artifact["size_bytes"] != summary_size
    ):
        raise DirectRunManifestError(
            "run_summary.csv does not match the exact artifact sealed by the direct manifest"
        )
    summary_content = _content_identity(
        manifest.get("run_summary_content"), "direct run run_summary_content"
    )
    if summary_content != {
        "name": summary_artifact["path"].name,
        "size_bytes": summary_size,
        "sha256": summary_digest,
    }:
        raise DirectRunManifestError("run_summary_content disagrees with output_artifacts")
    workbook = by_role["summary_workbook"][0]
    if (
        workbook["relative_path"] != "run_summary.xlsx"
        or manifest.get("summary_workbook") != "run_summary.xlsx"
    ):
        raise DirectRunManifestError(
            "direct run workbook artifact must be canonical run_summary.xlsx"
        )
    workbook_content = _content_identity(
        manifest.get("summary_workbook_content"),
        "direct run summary_workbook_content",
    )
    if workbook_content != {
        "name": workbook["path"].name,
        "size_bytes": workbook["size_bytes"],
        "sha256": workbook["sha256"],
    }:
        raise DirectRunManifestError(
            "summary_workbook_content disagrees with output_artifacts"
        )

    images = manifest.get("images")
    if not isinstance(images, list):
        raise DirectRunManifestError("direct run manifest images must be an array")
    successes = [item for item in images if isinstance(item, dict) and item.get("status") == "success"]
    failures = [item for item in images if not isinstance(item, dict) or item.get("status") == "failed"]
    skips = [item for item in images if isinstance(item, dict) and item.get("status") == "skipped"]
    unknown_status = [
        item for item in images
        if isinstance(item, dict) and item.get("status") not in {"success", "failed", "skipped"}
    ]
    if failures or unknown_status or len(successes) != counts[0]:
        raise DirectRunManifestError(
            "direct run image status/count ledger is inconsistent or contains failures"
        )
    skipped_count = manifest.get("skipped_count")
    if (
        not isinstance(skipped_count, int)
        or isinstance(skipped_count, bool)
        or skipped_count != len(skips)
    ):
        raise DirectRunManifestError("direct run skipped_count disagrees with images")
    allowed_skip_reasons = {
        "non_analytical_map_acquisition",
        "not_in_canonical_manifest",
    }
    if any(item.get("skip_reason") not in allowed_skip_reasons for item in skips):
        raise DirectRunManifestError(
            "direct run contains a capped or unsupported skipped analytical input"
        )

    if "output_key" not in summary_header:
        raise DirectRunManifestError(
            "sealed modern direct run_summary.csv is missing output_key"
        )
    summary_by_key = defaultdict(list)
    for row_number, row in enumerate(summary_rows, start=2):
        output_key = (row.get("output_key") or "").strip()
        if not output_key:
            raise DirectRunManifestError(
                f"run_summary.csv row {row_number} has a blank output_key"
            )
        summary_by_key[output_key].append(row)

    params_artifacts = {}
    for artifact in by_role["image_params"]:
        output_key = artifact.get("output_key")
        if not isinstance(output_key, str) or not output_key or output_key in params_artifacts:
            raise DirectRunManifestError(
                "image_params output_key values must be unique non-empty strings"
            )
        params_artifacts[output_key] = artifact
    success_by_key = {}
    for image in successes:
        output_key = image.get("output_key")
        if (
            not isinstance(output_key, str)
            or re.fullmatch(r"[A-Za-z0-9._-]+", output_key) is None
            or output_key in success_by_key
        ):
            raise DirectRunManifestError(
                "successful image output_key values must be unique and non-empty"
            )
        success_by_key[output_key] = image
    if set(success_by_key) != set(params_artifacts) or set(success_by_key) != set(summary_by_key):
        raise DirectRunManifestError(
            "summary, successful-image, and params output_key sets disagree"
        )

    input_dir = manifest.get("input_dir")
    if not isinstance(input_dir, str) or not input_dir.strip():
        raise DirectRunManifestError("direct run manifest input_dir is missing")
    if not Path(input_dir).is_absolute() or Path(input_dir).is_symlink():
        raise DirectRunManifestError(
            "direct run manifest input_dir must be an absolute non-symlink directory"
        )
    try:
        input_root = Path(input_dir).resolve(strict=True)
    except OSError as exc:
        raise DirectRunManifestError(
            f"direct run raw input directory is unavailable: {exc}"
        ) from exc
    if not input_root.is_dir():
        raise DirectRunManifestError("direct run input_dir is not a directory")

    config = manifest.get("config")
    if not isinstance(config, dict):
        raise DirectRunManifestError("direct run manifest config must be an object")
    config_sha256 = config.get("resolvedConfigSha256")
    if re.fullmatch(r"[0-9a-f]{64}", str(config_sha256)) is None:
        raise DirectRunManifestError(
            "direct run manifest lacks a resolved configuration SHA-256"
        )
    engine_content = _content_identity(
        manifest.get("engine_script"), "direct run engine_script"
    )
    engine_path = manifest.get("engine_script_path")
    if (
        not isinstance(engine_path, str)
        or not engine_path.strip()
        or not Path(engine_path).is_absolute()
    ):
        raise DirectRunManifestError(
            "direct run engine_script_path must be absolute"
        )
    engine_digest, engine_size = _require_current_content(
        engine_path, engine_content, "direct run engine script"
    )
    if config.get("engineScript") != engine_content:
        raise DirectRunManifestError(
            "direct run config engineScript disagrees with the manifest engine identity"
        )

    input_artifacts = [
        _artifact_for_direct_input(
            manifest_path,
            "direct_run_manifest",
            audit_path,
            manifest_digest,
            manifest_size,
        ),
        _artifact_for_direct_input(
            workbook["path"],
            "direct_summary_workbook",
            audit_path,
            workbook["sha256"],
            workbook["size_bytes"],
        ),
        _artifact_for_direct_input(
            engine_path,
            "direct_engine_script",
            audit_path,
            engine_digest,
            engine_size,
        ),
    ]
    tracked = [
        (input_artifacts[0], os.fspath(manifest_path)),
        (input_artifacts[1], os.fspath(workbook["path"])),
        (input_artifacts[2], os.path.abspath(engine_path)),
    ]

    panel_signatures = defaultdict(set)
    segmenter = config.get("segmenter")
    if segmenter not in {"classic", "stardist"}:
        raise DirectRunManifestError("direct run segmenter is unsupported")
    authority = config.get("stardistAuthority")
    if not isinstance(authority, dict):
        raise DirectRunManifestError("direct run stardistAuthority must be an object")
    common_authority_fields = {
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
    }
    expected_authority_fields = set(common_authority_fields)
    if segmenter == "stardist":
        expected_authority_fields.update({"model_path", "runtime_manifest_path"})
    if set(authority) != expected_authority_fields:
        raise DirectRunManifestError(
            "direct run StarDist authority fields do not match the engine contract"
        )
    runtime_profile_id = authority.get("runtime_profile_id")
    model_sha256 = None

    for index, output_key in enumerate(sorted(success_by_key), start=1):
        image = success_by_key[output_key]
        params_artifact = params_artifacts[output_key]
        channel_signature = image.get("channel_signature")
        image_file = image.get("file")
        if (
            not isinstance(channel_signature, str)
            or re.fullmatch(r"[A-Za-z0-9._-]+", channel_signature) is None
            or not isinstance(image_file, str)
            or not image_file
            or Path(image_file).name != image_file
        ):
            raise DirectRunManifestError(
                f"successful image {output_key!r} has unsafe file/channel identity"
            )
        expected_params_relative = f"{output_key}/{channel_signature}__params.json"
        if (
            image.get("params_relative_path", "").replace("\\", "/")
            != params_artifact["relative_path"]
            or params_artifact["relative_path"] != expected_params_relative
        ):
            raise DirectRunManifestError(
                f"successful image {output_key!r} params path disagrees with output_artifacts"
            )
        params_content = _content_identity(
            image.get("params_content"), f"successful image {output_key!r} params_content"
        )
        if params_content != {
            "name": params_artifact["path"].name,
            "size_bytes": params_artifact["size_bytes"],
            "sha256": params_artifact["sha256"],
        }:
            raise DirectRunManifestError(
                f"successful image {output_key!r} params_content disagrees with the artifact ledger"
            )
        params_payload = params_artifact["path"].read_bytes()
        if (
            len(params_payload) != params_artifact["size_bytes"]
            or hashlib.sha256(params_payload).hexdigest() != params_artifact["sha256"]
        ):
            raise DirectRunManifestError(
                f"successful image {output_key!r} params changed while parsed"
            )
        params = _strict_json_object(
            params_payload, f"successful image {output_key!r} params"
        )
        required_identity = {
            "image": image.get("file"),
            "output_key": output_key,
            "panel": image.get("panel"),
            "channel_signature": image.get("channel_signature"),
            "source_content": image.get("source_content"),
            "engine_script": engine_content,
            "resolved_config_sha256": config_sha256,
            "segmenter": segmenter,
        }
        for field, expected in required_identity.items():
            if params.get(field) != expected:
                raise DirectRunManifestError(
                    f"successful image {output_key!r} params {field} disagrees with the run manifest"
                )
        if image.get("source_content_verified_before_and_after") is not True:
            raise DirectRunManifestError(
                f"successful image {output_key!r} lacks before/after raw verification"
            )
        raw_path, _ = _safe_relative_path(
            input_root,
            image.get("relative_path"),
            f"successful image {output_key!r} raw relative_path",
        )
        raw_identity = _content_identity(
            image.get("source_content"),
            f"successful image {output_key!r} source_content",
        )
        if raw_identity["name"] != image.get("file"):
            raise DirectRunManifestError(
                f"successful image {output_key!r} raw filename disagrees with image identity"
            )
        raw_digest, raw_size = _require_current_content(
            raw_path, raw_identity, f"successful image {output_key!r} current raw source"
        )
        summary_image = os.path.splitext(str(image.get("file")))[0]
        for row in summary_by_key[output_key]:
            if (row.get("panel") or "").strip() != image.get("panel"):
                raise DirectRunManifestError(
                    f"summary panel disagrees for successful image {output_key!r}"
                )
            if (row.get("image") or "").strip() != summary_image:
                raise DirectRunManifestError(
                    f"summary image identity disagrees for successful image {output_key!r}"
                )
        panel_signatures[image.get("panel")].add(image.get("channel_signature"))
        params_descriptor = _artifact_for_direct_input(
            params_artifact["path"],
            f"direct_image_params_{index:04d}",
            audit_path,
            params_artifact["sha256"],
            params_artifact["size_bytes"],
        )
        raw_descriptor = _artifact_for_direct_input(
            raw_path,
            f"direct_raw_source_{index:04d}",
            audit_path,
            raw_digest,
            raw_size,
        )
        input_artifacts.extend((params_descriptor, raw_descriptor))
        tracked.extend(
            (
                (params_descriptor, os.fspath(params_artifact["path"])),
                (raw_descriptor, os.fspath(raw_path)),
            )
        )

        params_runtime = params.get("stardist_runtime")
        if not isinstance(params_runtime, dict):
            raise DirectRunManifestError(
                f"successful image {output_key!r} stardist_runtime must be an object"
            )
        if set(params_runtime) != common_authority_fields | {"label_outputs"}:
            raise DirectRunManifestError(
                f"successful image {output_key!r} stardist_runtime fields are not exact"
            )
        for field in (
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
        ):
            if params_runtime.get(field) != authority.get(field):
                raise DirectRunManifestError(
                    f"successful image {output_key!r} StarDist runtime {field} disagrees with run authority"
                )
        if params.get("stardist_model_sha256") != config.get("stardistModelSha256"):
            raise DirectRunManifestError(
                f"successful image {output_key!r} model SHA-256 disagrees with run config"
            )
        if params.get("stardist_model_authority") != config.get("stardistModelAuthority"):
            raise DirectRunManifestError(
                f"successful image {output_key!r} model authority disagrees with run config"
            )
        if params.get("stardist_model_choice") != config.get("stardistModelChoice"):
            raise DirectRunManifestError(
                f"successful image {output_key!r} model choice disagrees with run config"
            )

    if any(len(signatures) != 1 for signatures in panel_signatures.values()):
        raise DirectRunManifestError(
            "successful direct images reuse a panel with different channel signatures"
        )

    if segmenter == "classic":
        if (
            authority.get("active") is not False
            or authority.get("authority") != "not_applicable_classic"
            or authority.get("api_command") is not None
            or authority.get("model_choice") is not None
            or authority.get("model_content") is not None
            or authority.get("model_archive") is not None
            or authority.get("runtime_manifest_content") is not None
            or authority.get("runtime_profile_id") is not None
            or authority.get("runtime_artifacts") != []
            or authority.get("class_bindings") != []
            or config.get("stardistModelSha256") is not None
            or config.get("stardistModelChoice") is not None
            or config.get("stardistModelAuthority") != "not_applicable_classic"
        ):
            raise DirectRunManifestError(
                "classic run carries inconsistent StarDist model/runtime authority"
            )
    else:
        if authority.get("active") is not True:
            raise DirectRunManifestError("StarDist run authority is not active")
        if (
            config.get("stardistModelChoice") != authority.get("model_choice")
            or config.get("stardistModelAuthority") != authority.get("authority")
        ):
            raise DirectRunManifestError(
                "StarDist model choice/authority disagrees with run config"
            )
        if manifest.get("stardist_authority_verified_before_and_after") is not True or manifest.get("stardist_authority_verified_at_manifest_publication") is not True:
            raise DirectRunManifestError(
                "StarDist model/runtime was not verified through manifest publication"
            )
        model_content = _content_identity(
            authority.get("model_content"), "direct StarDist model_content"
        )
        model_path = authority.get("model_path")
        if not isinstance(model_path, str) or not Path(model_path).is_absolute():
            raise DirectRunManifestError(
                "direct StarDist model path must be absolute"
            )
        model_digest, model_size = _require_current_content(
            model_path, model_content, "direct StarDist model"
        )
        model_sha256 = model_digest
        if config.get("stardistModelSha256") != model_digest:
            raise DirectRunManifestError(
                "direct StarDist config model SHA-256 disagrees with current model"
            )
        model_descriptor = _artifact_for_direct_input(
            model_path,
            "direct_stardist_model",
            audit_path,
            model_digest,
            model_size,
        )
        input_artifacts.append(model_descriptor)
        tracked.append((model_descriptor, os.path.abspath(model_path)))

        runtime_manifest_content = _content_identity(
            authority.get("runtime_manifest_content"),
            "direct StarDist runtime_manifest_content",
        )
        runtime_manifest_path = authority.get("runtime_manifest_path")
        if (
            not isinstance(runtime_manifest_path, str)
            or not Path(runtime_manifest_path).is_absolute()
        ):
            raise DirectRunManifestError(
                "direct StarDist runtime manifest path must be absolute"
            )
        runtime_manifest_digest, runtime_manifest_size = _require_current_content(
            runtime_manifest_path,
            runtime_manifest_content,
            "direct StarDist runtime manifest",
        )
        runtime_manifest_descriptor = _artifact_for_direct_input(
            runtime_manifest_path,
            "direct_stardist_runtime_manifest",
            audit_path,
            runtime_manifest_digest,
            runtime_manifest_size,
        )
        input_artifacts.append(runtime_manifest_descriptor)
        tracked.append(
            (runtime_manifest_descriptor, os.path.abspath(runtime_manifest_path))
        )
        runtime_artifacts = authority.get("runtime_artifacts")
        if not isinstance(runtime_artifacts, list) or len(runtime_artifacts) < 4:
            raise DirectRunManifestError(
                "direct StarDist runtime artifact ledger is incomplete"
            )
        seen_runtime_roles = set()
        for index, artifact in enumerate(runtime_artifacts, start=1):
            if (
                not isinstance(artifact, dict)
                or set(artifact)
                != {"role", "path", "expected_classes", "content"}
            ):
                raise DirectRunManifestError(
                    "direct StarDist runtime artifact fields are not exact"
                )
            role = artifact.get("role")
            if (
                not isinstance(role, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role) is None
                or role in seen_runtime_roles
            ):
                raise DirectRunManifestError(
                    "direct StarDist runtime artifact role is invalid or duplicated"
                )
            seen_runtime_roles.add(role)
            content = _content_identity(
                artifact.get("content"), f"direct StarDist runtime artifact {role}"
            )
            artifact_path = artifact.get("path")
            if (
                not isinstance(artifact_path, str)
                or not Path(artifact_path).is_absolute()
            ):
                raise DirectRunManifestError(
                    f"direct StarDist runtime artifact {role} path must be absolute"
                )
            artifact_digest, artifact_size = _require_current_content(
                artifact_path, content, f"direct StarDist runtime artifact {role}"
            )
            descriptor = _artifact_for_direct_input(
                artifact_path,
                f"direct_stardist_runtime_{index:02d}_{role}",
                audit_path,
                artifact_digest,
                artifact_size,
            )
            input_artifacts.append(descriptor)
            tracked.append((descriptor, os.path.abspath(artifact_path)))
        required_runtime_roles = {
            "stardist_plugin",
            "csbdeep_plugin",
            "tensorflow_java",
            "tensorflow_native",
        }
        if not required_runtime_roles.issubset(seen_runtime_roles):
            raise DirectRunManifestError(
                "direct StarDist runtime artifact ledger lacks required roles"
            )

    direct_authority = {
        "engine_sha256": engine_content["sha256"],
        "config_sha256": config_sha256,
        "segmenter": segmenter,
        "model_sha256": model_sha256,
        "runtime_profile_id": runtime_profile_id,
        "panel_signatures": {
            panel: next(iter(signatures))
            for panel, signatures in sorted(panel_signatures.items())
        },
    }
    return input_artifacts, tracked, direct_authority


def validate_direct_measurement_spec_authority(spec, authority):
    """Reconcile explicit record semantics with the sealed engine identities."""
    profiles = spec.get("profiles", [])
    declared_panels = {profile.get("panel") for profile in profiles}
    if declared_panels != set(authority["panel_signatures"]):
        raise DirectRunManifestError(
            "measurement profiles do not exactly cover sealed direct-run panels"
        )
    for profile in profiles:
        panel = profile["panel"]
        provenance = profile["provenance"]
        if provenance["code_revision"] != authority["engine_sha256"]:
            raise DirectRunManifestError(
                f"direct profile {panel!r} code_revision must equal the sealed engine SHA-256"
            )
        if provenance["config_sha256"] != authority["config_sha256"]:
            raise DirectRunManifestError(
                f"direct profile {panel!r} config_sha256 disagrees with the sealed engine config"
            )
        signature = profile["channel_signature"]
        if not isinstance(signature, list) or not signature:
            raise DirectRunManifestError(
                f"direct profile {panel!r} channel_signature must be non-empty"
            )
        tokens = []
        indices = set()
        for channel in signature:
            if not isinstance(channel, dict) or set(channel) != {"index", "label", "role"}:
                raise DirectRunManifestError(
                    f"direct profile {panel!r} has an invalid channel signature entry"
                )
            index = channel["index"]
            label = channel["label"]
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 1
                or index in indices
                or not isinstance(label, str)
                or not label
            ):
                raise DirectRunManifestError(
                    f"direct profile {panel!r} channel signature is invalid"
                )
            indices.add(index)
            tokens.append((index, f"C{index}-{label}"))
        engine_signature = "_".join(token for _, token in sorted(tokens))
        if engine_signature != authority["panel_signatures"][panel]:
            raise DirectRunManifestError(
                f"direct profile {panel!r} channel signature disagrees with successful params"
            )
        model = profile.get("segmentation_model")
        if authority["segmenter"] == "classic":
            if model is not None:
                raise DirectRunManifestError(
                    f"classic direct profile {panel!r} must not declare a segmentation model"
                )
        else:
            if not isinstance(model, dict):
                raise DirectRunManifestError(
                    f"StarDist direct profile {panel!r} must declare its segmentation model"
                )
            if model.get("model_sha256") != authority["model_sha256"]:
                raise DirectRunManifestError(
                    f"direct profile {panel!r} model SHA-256 disagrees with sealed StarDist"
                )
            if model.get("profile_id") != authority["runtime_profile_id"]:
                raise DirectRunManifestError(
                    f"direct profile {panel!r} model profile_id disagrees with sealed runtime"
                )


def is_quarantined_summary(path):
    """Recognize Stage 3 diagnostic and stale-output naming conventions."""
    name = os.path.basename(path).lower()
    return ".rejected" in name or ".stale." in name


def validate_rows(header, rows):
    """Fail before aggregation when biological identity or row identity is unsafe."""
    missing_columns = [
        c for c in KEY_COLS + ROW_ID_COLS + ["region_area_um2", "n_nuclei"]
        if c not in header
    ]
    if missing_columns:
        sys.exit("ERROR: run_summary.csv is missing required columns: " +
                 ", ".join(missing_columns))

    invalid_mouse_rows = [
        r for r in rows
        if (r.get("mouse_id") or "").strip().upper() in {"", "NA", "N/A", "UNKNOWN"}
    ]
    if invalid_mouse_rows:
        examples = ", ".join((r.get("image") or "<unknown>") for r in invalid_mouse_rows[:5])
        sys.exit(
            f"ERROR: {len(invalid_mouse_rows)} row(s) lack a valid mouse_id ({examples}). "
            "Do not aggregate unknown animals into one pseudo-mouse; fix samplesheet.csv first."
        )

    identities = defaultdict(set)
    for r in rows:
        identities[(r.get("mouse_id") or "").strip()].add(
            ((r.get("genotype") or "NA").strip(), (r.get("condition") or "NA").strip())
        )
    conflicts = {mouse: values for mouse, values in identities.items() if len(values) > 1}
    if conflicts:
        details = "; ".join(f"{mouse}: {sorted(values)}" for mouse, values in list(conflicts.items())[:5])
        sys.exit("ERROR: a mouse_id maps to multiple genotype/condition identities: " + details)

    seen = set()
    duplicates = []
    for r in rows:
        # The biological/measurement identity must not depend on output_key:
        # a rerun can legitimately generate a different output name for the
        # same section and would otherwise evade duplicate detection.
        # section_id is reused across animals in the established confocal
        # route (for example G001). Include the specimen identity while keeping
        # output_key out of the key so a renamed retry still collides.
        key_columns = ["mouse_id", "section_id", "region", "panel"]
        key = tuple((r.get(c) or "").strip() for c in key_columns)
        if any(not value for value in key):
            sys.exit(
                "ERROR: blank mouse_id/section_id/region/panel in aggregation identity: "
                + repr(key)
            )
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        sys.exit(
            f"ERROR: {len(duplicates)} duplicate specimen-section-region-panel row(s) detected. "
            "Combine only one declared run per analyzed region; output_key changes do not "
            "make a retry a new measurement."
        )

    if "qc_status" in header:
        failed_qc = [
            (row.get("image") or row.get("section_id") or "<unknown>")
            for row in rows
            if (row.get("qc_status") or "").strip().lower() != "ok"
        ]
        if failed_qc:
            sys.exit(
                "ERROR: refusing to aggregate non-passing slide/region QC rows: "
                + ", ".join(failed_qc[:5])
            )

    numeric_problems = []
    rows_by_panel = defaultdict(list)
    for row in rows:
        rows_by_panel[(row.get("panel") or "").strip()].append(row)
    for panel, panel_rows in sorted(rows_by_panel.items()):
        for column in classify_columns(header)["sum_cols"]:
            missing, invalid, out_of_range = 0, [], 0
            available = 0
            for index, row in enumerate(panel_rows, start=1):
                raw = row.get(column)
                token = "" if raw is None else str(raw).strip()
                if token == "" or token.upper() in {"NA", "N/A"}:
                    missing += 1
                    continue
                available += 1
                value = _num(token)
                if value is None:
                    invalid.append((index, token))
                elif ((column == "region_area_um2" and value <= 0)
                      or (column != "region_area_um2" and value < 0)):
                    out_of_range += 1

            # The established confocal schema is a union of LEFT and RIGHT
            # measurements.  A marker that is absent from a panel is blank for
            # every row in that panel and is unevaluable, not numerically zero.
            # Once a column is present for a panel, however, it must be present
            # and finite for every row in that panel.
            if available == 0:
                if column in {"region_area_um2", "n_nuclei"}:
                    numeric_problems.append(
                        f"{column}: unavailable for every row in panel {panel!r}"
                    )
                continue
            if invalid:
                numeric_problems.append(
                    f"{column}: {len(invalid)} invalid/non-finite value(s) "
                    f"in panel {panel!r}"
                )
            if missing:
                numeric_problems.append(
                    f"{column}: missing in {missing}/{len(panel_rows)} row(s) "
                    f"in panel {panel!r}"
                )
            if out_of_range:
                range_description = (
                    "non-positive region area" if column == "region_area_um2"
                    else "negative additive"
                )
                numeric_problems.append(
                    f"{column}: {out_of_range} {range_description} value(s) "
                    f"in panel {panel!r}"
                )
    if numeric_problems:
        sys.exit(
            "ERROR: additive measurements are incomplete or invalid; missing/non-finite "
            "values cannot be coerced to zero: " + " | ".join(numeric_problems[:8])
        )

    # Stage 3 v2 rows are authoritative only when every contributing slide was
    # produced from an explicit hashed index and passed QC. Direct confocal
    # run_summary inputs predate these columns and remain on their established
    # validation route.
    is_wsi_summary = (
        "aggregation_contract_version" in header
        or bool(WSI_INDICATOR_COLUMNS.intersection(header))
    )
    if is_wsi_summary:
        required_provenance = (
            WSI_UNIFORM_PROVENANCE
            + WSI_PER_SLIDE_PROVENANCE
            + ["qc_status", "dataset_qc_status"]
        )
        missing_provenance = [c for c in required_provenance if c not in header]
        if missing_provenance:
            sys.exit(
                "ERROR: WSI slide summary is missing provenance columns: "
                + ", ".join(missing_provenance)
            )
        for row in rows:
            slide = (row.get("image") or row.get("section_id") or "<unknown>").strip()
            if (row.get("aggregation_contract_version") or "").strip() != "2.0.0":
                sys.exit(
                    f"ERROR: WSI slide row has unsupported aggregation contract: {slide}"
                )
            if (row.get("stage2_source_mode") or "").strip() != "explicit_hashed_index":
                sys.exit(
                    f"ERROR: WSI slide row is not backed by an explicit Stage 2 index: {slide}"
                )
            if (row.get("dataset_qc_status") or "").strip().lower() != "ok":
                sys.exit(
                    f"ERROR: WSI slide row belongs to a rejected dataset: {slide}"
                )
            blanks = [
                column for column in WSI_UNIFORM_PROVENANCE + WSI_PER_SLIDE_PROVENANCE
                if not (row.get(column) or "").strip()
            ]
            if blanks:
                sys.exit(
                    f"ERROR: WSI slide row {slide} has blank provenance: {', '.join(blanks)}"
                )
            malformed_hashes = [
                column
                for column in WSI_UNIFORM_PROVENANCE + WSI_PER_SLIDE_PROVENANCE
                if column.endswith("_sha256")
                and re.fullmatch(r"[0-9a-f]{64}", (row.get(column) or "").strip()) is None
            ]
            if malformed_hashes:
                sys.exit(
                    f"ERROR: WSI slide row {slide} has malformed SHA-256 provenance: "
                    + ", ".join(malformed_hashes)
                )

        # A measurement profile must be comparable across the biological
        # groups that will appear together in the cohort output.  Checking
        # only within a mouse would allow genotype/condition groups to be
        # measured with different scripts or configs while still looking
        # like one valid comparison.  Panel is the appropriate boundary:
        # LEFT and RIGHT can legitimately have different channel mappings,
        # but every mouse within a panel must share one profile.
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row.get("panel") or "NA").strip()].append(row)
        for panel, group in grouped.items():
            for column in WSI_UNIFORM_PROVENANCE:
                values = {(row.get(column) or "").strip() for row in group}
                if len(values) != 1:
                    sys.exit(
                        "ERROR: panel rows mix incompatible WSI provenance "
                        f"for {column}: panel={panel!r}, values={sorted(values)}"
                    )
    return is_wsi_summary


def _merge_endpoint_rows_snapshot(header, rows, endpoint_header, endpoint_rows):
    """Join endpoint measurements to exactly matching run rows.

    Endpoint evaluation is commonly panel-filtered. Return only matched rows so
    an endpoint computed on LEFT cannot become a plausible zero-valued endpoint
    on RIGHT during mouse aggregation.
    """
    if "output_key" not in header:
        sys.exit("ERROR: --endpoint-csv requires output_key in run_summary.csv")
    required = [c for c in ("output_key", "region") if c not in endpoint_header]
    if required:
        sys.exit("ERROR: endpoint CSV is missing required columns: " + ", ".join(required))
    if not endpoint_rows:
        sys.exit("ERROR: endpoint CSV has no data rows")

    main_by_key = {}
    for row in rows:
        key = ((row.get("output_key") or "").strip(), (row.get("region") or "").strip())
        if key in main_by_key:
            sys.exit(f"ERROR: duplicate run_summary endpoint join key: {key}")
        main_by_key[key] = row

    endpoint_by_key = {}
    for row in endpoint_rows:
        key = ((row.get("output_key") or "").strip(), (row.get("region") or "").strip())
        if key in endpoint_by_key:
            sys.exit(f"ERROR: duplicate endpoint CSV join key: {key}")
        endpoint_by_key[key] = row

    unmatched = [key for key in endpoint_by_key if key not in main_by_key]
    if unmatched:
        sys.exit(
            f"ERROR: {len(unmatched)} endpoint row(s) do not match run_summary "
            f"by (output_key, region); examples: {unmatched[:5]}"
        )

    identity = {"output_key", "image", "region", "region_mode"}
    measurement_cols = [c for c in endpoint_header if c not in identity]
    conflicts = [c for c in measurement_cols if c in header]
    if conflicts:
        sys.exit("ERROR: endpoint CSV would overwrite run_summary columns: " + ", ".join(conflicts))

    merged = []
    for key, endpoint_row in endpoint_by_key.items():
        row = dict(main_by_key[key])
        for column in measurement_cols:
            row[column] = endpoint_row.get(column, "")
        merged.append(row)
    return list(header) + measurement_cols, merged


def merge_endpoint_rows(header, rows, endpoint_path):
    endpoint_header, endpoint_rows = read_rows(endpoint_path)
    return _merge_endpoint_rows_snapshot(
        header, rows, endpoint_header, endpoint_rows
    )

def classify_columns(header):
    """Group measurement columns by how they must be pooled."""
    # The plain <marker>_pos_count field is morphology-authoritative. Keep the
    # explicitly named raw-mean and state-audit fields in separate categories so
    # they cannot silently become the statistical endpoint.
    pos_count = [
        c for c in header
        if c.endswith("_pos_count")
        and not c.endswith("_morphology_pos_count")
        and not c.endswith("_raw_mean_pos_count")
        and not c.endswith("_true_pos_count")
        and not c.endswith("_marker_evidence_pos_count")
    ]
    raw_mean_pos_count = [c for c in header if c.endswith("_raw_mean_pos_count")]
    morphology_pos_count = [c for c in header if c.endswith("_morphology_pos_count")]
    morphology_negative_count = [c for c in header if c.endswith("_morphology_negative_count")]
    marker_indeterminate_count = [
        c for c in header if c.endswith("_indeterminate_count")
        and not c.startswith("class_")
    ]
    morphology_evaluable_count = [c for c in header if c.endswith("_morphology_evaluable_count")]
    final_cell_state_count = [
        c for c in header
        if c.endswith((
            "_final_positive_cell_count",
            "_final_negative_cell_count",
            "_final_indeterminate_cell_count",
            "_context_resolved_positive_count",
            "_context_resolved_evaluable_count",
            "_context_unresolved_positive_count",
            "_marker_evidence_pos_count",
        ))
    ]
    marker_audit_count = [
        c for c in header
        if c.endswith((
            "_raw_positive_final_negative_count",
            "_raw_negative_final_positive_count",
            "_intensity_morphology_discordant_count",
            "_review_burden_proxy_count",
        ))
    ]
    nucleus_qc_count = [
        c for c in header
        if c in {
            "n_rejected_nucleus_candidates", "n_rejected_below_min_area",
            "n_rejected_at_image_edge", "n_rejected_by_particle_filter",
            "n_nucleus_candidates_total",
        }
    ]
    positive_area = [c for c in header if c.endswith("_positive_area_um2")]
    n_components = [c for c in header if c.endswith("_n_components")]
    # Slide-level partition QC has its own denominator semantics. These columns
    # are raw additive areas, not region-level fractions, so they are safe to
    # carry to mouse level by summing and recomputing the fractions below.
    # Keeping the names explicit prevents a future arbitrary ``*_area_um2``
    # column from silently acquiring sum semantics.
    partition_area = [
        c for c in header if c in {"damaged_area_um2", "intact_area_um2"}
    ]
    intact_pod_area = [c for c in header if c.endswith("_pod_area_um2_in_intact")]
    endpoint_denominator_area = [
        c for c in header if c.endswith("_denominator_area_um2")
    ]
    # total pod area only -- exclude the derived per-region MEAN pod size, which
    # also ends in "_pod_area_um2" and would otherwise be summed as an area.
    pod_area = [c for c in header
                if c.endswith("_pod_area_um2") and not c.endswith("_mean_pod_area_um2")]
    n_pods = [c for c in header if c.endswith("_n_pods")]
    class_count = [
        c for c in header if c.startswith("class_") and c.endswith("_count")
        and not c.endswith("_evaluable_count")
        and not c.endswith("_indeterminate_count")
    ]
    class_evaluable_count = [c for c in header if c.startswith("class_") and c.endswith("_evaluable_count")]
    class_indeterminate_count = [c for c in header if c.startswith("class_") and c.endswith("_indeterminate_count")]
    # everything else numeric-ish that we simply sum or average
    state_count_set = (set(raw_mean_pos_count) | set(morphology_pos_count) |
                       set(morphology_negative_count) | set(marker_indeterminate_count) |
                       set(morphology_evaluable_count) | set(class_evaluable_count) |
                       set(class_indeterminate_count) | set(marker_audit_count) |
                       set(final_cell_state_count))
    sum_col_set = (set(["region_area_um2", "n_nuclei"]) | set(pos_count) |
                   set(pod_area) | set(n_pods) | set(class_count) | state_count_set |
                   set(nucleus_qc_count) | set(positive_area) | set(n_components) |
                   set(partition_area) | set(intact_pod_area) |
                   set(endpoint_denominator_area))
    # Preserve the source schema order. Iterating a set made output column order
    # depend on Python hash randomization, which undermines byte-level rerun
    # comparisons even when every numerical value is identical.
    state_counts = [column for column in header if column in state_count_set]
    sum_cols = [column for column in header if column in sum_col_set]
    # derived columns we recompute (do NOT sum): fractions, densities, mean pod size, thresholds
    return {
        "pos_count": pos_count,
        "raw_mean_pos_count": raw_mean_pos_count,
        "state_counts": state_counts,
        "final_cell_state_count": final_cell_state_count,
        "marker_audit_count": marker_audit_count,
        "nucleus_qc_count": nucleus_qc_count,
        "positive_area": positive_area,
        "n_components": n_components,
        "partition_area": partition_area,
        "intact_pod_area": intact_pod_area,
        "endpoint_denominator_area": endpoint_denominator_area,
        "pod_area": pod_area,
        "n_pods": n_pods,
        "class_count": class_count,
        "sum_cols": sum_cols,
    }


def marker_of(col, suffix):
    return col[: -len(suffix)]


def aggregate_mice(header, rows, endpoint_relation=None, sampling_unit="section"):
    if sampling_unit not in {"field", "section"}:
        raise ValueError(
            "sampling_unit must be 'field' for microscope fields or "
            "'section' for histological/whole-slide sections"
        )
    cats = classify_columns(header)
    groups = defaultdict(list)
    for r in rows:
        key = tuple(r.get(k, "NA") for k in KEY_COLS)
        groups[key].append(r)

    out_rows = []
    for key, grp in sorted(groups.items()):
        mouse_id, genotype, condition, panel = key
        rec = {"mouse_id": mouse_id, "genotype": genotype,
               "condition": condition, "panel": panel}
        if "aggregation_contract_version" in header:
            for column in WSI_UNIFORM_PROVENANCE:
                rec[column] = (grp[0].get(column) or "").strip()
            for column in WSI_PER_SLIDE_PROVENANCE:
                values = sorted({(row.get(column) or "").strip() for row in grp})
                rec[column + "s"] = ";".join(values)
        rec["n_regions"] = len(grp)
        rec[f"n_{sampling_unit}s"] = len(
            {(g.get("section_id") or "NA") for g in grp})
        rec["sampling_unit"] = sampling_unit

        # --- sums ---
        # The input schema is a union across panels.  Do not turn a marker that
        # is wholly unavailable in this panel into a plausible zero-valued
        # measurement; omit it from this mouse row so CSV output leaves it
        # blank and group statistics do not count it as evaluated.
        applicable_sum_cols = [
            c for c in cats["sum_cols"]
            if any(
                str(g.get(c) or "").strip().upper() not in {"", "NA", "N/A"}
                for g in grp
            )
        ]
        sums = {}
        for c in applicable_sum_cols:
            vals = [_num(g.get(c)) for g in grp]
            vals = [v for v in vals if v is not None]
            if vals:
                sums[c] = math.fsum(vals)

        total_area_um2 = sums.get("region_area_um2", 0.0)
        total_area_mm2 = total_area_um2 / 1e6
        rec["total_tissue_area_um2"] = total_area_um2
        rec["total_nuclei"] = sums.get("n_nuclei", 0.0)

        # --- WSI damaged/intact partition QC ------------------------------
        # These values used to stop at slide level because classify_columns()
        # did not recognize them. Pool the additive areas first, then recompute
        # every fraction from the mouse-level denominator.
        if any(c in sums for c in cats["partition_area"]):
            damaged_area = sums.get("damaged_area_um2", 0.0)
            intact_area = sums.get("intact_area_um2", 0.0)
            parenchyma_area = damaged_area + intact_area
            rec["damaged_area_um2"] = damaged_area
            rec["intact_area_um2"] = intact_area
            rec["damaged_fraction_of_parenchyma"] = (
                damaged_area / parenchyma_area if parenchyma_area > 0 else 0.0
            )
        for c in (column for column in cats["intact_pod_area"] if column in sums):
            marker = marker_of(c, "_pod_area_um2_in_intact")
            area = sums[c]
            intact_area = sums.get("intact_area_um2", 0.0)
            rec[c] = area
            rec[f"{marker}_pod_area_frac_of_intact"] = (
                area / intact_area if intact_area > 0 else 0.0
            )

        # --- relational endpoint denominators ----------------------------
        # Endpoint CSVs can be joined to run_summary before aggregation. A
        # denominator is additive; its fraction must be rebuilt from pooled
        # numerator/denominator areas, never averaged across unequal regions.
        for c in (column for column in cats["endpoint_denominator_area"] if column in sums):
            endpoint = marker_of(c, "_denominator_area_um2")
            numerator_col = f"{endpoint}_pod_area_um2"
            denominator_area = sums[c]
            numerator_area = sums.get(numerator_col, 0.0)
            rec[c] = denominator_area
            rec[f"{endpoint}_fraction"] = (
                numerator_area / denominator_area if denominator_area > 0 else 0.0
            )
        if endpoint_relation:
            numerator_col = endpoint_relation["area_column"]
            bare_col = endpoint_relation["bare_area_column"]
            companion_fraction_col = endpoint_relation["numerator_fraction_of_bare_column"]
            numerator_area = sums.get(numerator_col, 0.0)
            bare_area = sums.get(bare_col, 0.0)
            rec[companion_fraction_col] = (
                numerator_area / bare_area if bare_area > 0 else 0.0
            )

        # --- nucleus-candidate QC totals and pooled fractions ---
        for c in (column for column in cats["nucleus_qc_count"] if column in sums):
            rec[f"{c}_total"] = sums[c]
        candidate_total = sums.get(
            "n_nucleus_candidates_total",
            sums.get("n_nuclei", 0.0) + sums.get("n_rejected_nucleus_candidates", 0.0),
        )
        rejected_total = sums.get("n_rejected_nucleus_candidates", 0.0)
        rec["nucleus_candidate_acceptance_fraction"] = (
            sums.get("n_nuclei", 0.0) / candidate_total if candidate_total > 0 else 0.0
        )
        rec["nucleus_candidate_rejection_fraction"] = (
            rejected_total / candidate_total if candidate_total > 0 else 0.0
        )
        for source, target in (
            ("n_rejected_below_min_area", "rejected_below_min_fraction_of_rejected"),
            ("n_rejected_at_image_edge", "rejected_edge_fraction_of_rejected"),
            ("n_rejected_by_particle_filter", "rejected_particle_filter_fraction_of_rejected"),
        ):
            rec[target] = sums.get(source, 0.0) / rejected_total if rejected_total > 0 else 0.0

        # --- marker positive counts + pooled density ---
        for c in (column for column in cats["pos_count"] if column in sums):
            m = marker_of(c, "_pos_count")
            rec[f"{m}_pos_count_total"] = sums[c]
            rec[f"{m}_density_per_mm2"] = (sums[c] / total_area_mm2) if total_area_mm2 > 0 else 0.0

        # --- explicit audit/state totals; never substitute for the endpoint ---
        for c in (column for column in cats["raw_mean_pos_count"] if column in sums):
            m = marker_of(c, "_raw_mean_pos_count")
            rec[f"{m}_raw_mean_pos_count_total"] = sums[c]
            rec[f"{m}_raw_mean_density_per_mm2"] = (
                sums[c] / total_area_mm2 if total_area_mm2 > 0 else 0.0
            )
        for c in (column for column in cats["state_counts"] if column in sums):
            if c in cats["raw_mean_pos_count"]:
                continue
            rec[f"{c}_total"] = sums[c]

        # Recompute morphology/QC fractions from pooled counts. Region-level
        # percentages must never be averaged because region sizes differ.
        for c in [
            x for x in header
            if x.endswith("_morphology_evaluable_count") and x in sums
        ]:
            marker = marker_of(c, "_morphology_evaluable_count")
            evaluable = sums.get(c, 0.0)
            included = sums.get("n_nuclei", 0.0)
            positive = sums.get(f"{marker}_morphology_pos_count", 0.0)
            negative = sums.get(f"{marker}_morphology_negative_count", 0.0)
            indeterminate = sums.get(f"{marker}_indeterminate_count", 0.0)
            discordant = sums.get(f"{marker}_intensity_morphology_discordant_count", 0.0)
            review = sums.get(f"{marker}_review_burden_proxy_count", indeterminate + discordant)
            rec[f"{marker}_morphology_positive_fraction_of_evaluable"] = positive / evaluable if evaluable > 0 else 0.0
            rec[f"{marker}_morphology_negative_fraction_of_evaluable"] = negative / evaluable if evaluable > 0 else 0.0
            rec[f"{marker}_indeterminate_fraction_of_included"] = indeterminate / included if included > 0 else 0.0
            rec[f"{marker}_intensity_morphology_discordant_fraction_of_evaluable"] = discordant / evaluable if evaluable > 0 else 0.0
            rec[f"{marker}_review_burden_proxy_fraction_of_included"] = review / included if included > 0 else 0.0

        # --- explicit final cell counts and fractions among all included cells ---
        # These are the human-facing "eventual quantification" fields used by
        # the Excel workbook. Pool counts first, then divide by the pooled
        # nucleus denominator; never average region-level fractions.
        for c in [
            x for x in header
            if x.endswith("_final_positive_cell_count") and x in sums
        ]:
            marker = marker_of(c, "_final_positive_cell_count")
            included = sums.get("n_nuclei", 0.0)
            for state in ("positive", "negative", "indeterminate"):
                source = f"{marker}_final_{state}_cell_count"
                count = sums.get(source, 0.0)
                rec[f"{source}_total"] = count
                rec[f"{marker}_final_{state}_fraction_of_total_cells"] = (
                    count / included if included > 0 else 0.0
                )
            context_positive = sums.get(f"{marker}_context_resolved_positive_count", 0.0)
            context_evaluable = sums.get(f"{marker}_context_resolved_evaluable_count", 0.0)
            rec[f"{marker}_context_resolved_positive_fraction_of_total_cells"] = (
                context_positive / included if included > 0 else 0.0
            )
            rec[f"{marker}_context_resolved_positive_fraction"] = (
                context_positive / context_evaluable if context_evaluable > 0 else 0.0
            )

        # --- generic regional area endpoints (AcTub, membranes, reporter, ECM) ---
        for c in (column for column in cats["positive_area"] if column in sums):
            marker = marker_of(c, "_positive_area_um2")
            area = sums[c]
            components = sums.get(f"{marker}_n_components", 0.0)
            rec[f"{marker}_positive_area_um2_total"] = area
            rec[f"{marker}_positive_area_fraction"] = area / total_area_um2 if total_area_um2 > 0 else 0.0
            rec[f"{marker}_n_components_total"] = components
            rec[f"{marker}_mean_component_area_um2"] = area / components if components > 0 else 0.0

        # --- pod area, fraction, count, mean size (per area-marker, e.g. KRT5) ---
        for c in (column for column in cats["pod_area"] if column in sums):
            m = marker_of(c, "_pod_area_um2")
            pod_area = sums[c]
            npods = sums.get(f"{m}_n_pods", 0.0)
            rec[f"{m}_pod_area_um2_total"] = pod_area
            rec[f"{m}_pod_area_frac"] = (pod_area / total_area_um2) if total_area_um2 > 0 else 0.0
            rec[f"{m}_n_pods_total"] = npods
            rec[f"{m}_mean_pod_area_um2"] = (pod_area / npods) if npods > 0 else 0.0

        # --- classification counts + pooled density ---
        for c in (column for column in cats["class_count"] if column in sums):
            base = c[: -len("_count")]  # e.g. class_KRT5+_AGER-
            rec[f"{base}_count_total"] = sums[c]
            rec[f"{base}_density_per_mm2"] = (sums[c] / total_area_mm2) if total_area_mm2 > 0 else 0.0

        out_rows.append(rec)
    return out_rows


def _stats(values):
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return 0, None, None, None
    mean = sum(vals) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        sd = math.sqrt(var)
        sem = sd / math.sqrt(n)
    else:
        sd = None
        sem = None
    return n, mean, sd, sem


def group_stats(mouse_rows):
    """Group descriptives; variability is not estimable for a one-mouse group."""
    metric_cols = []
    seen = set()
    skip = set(KEY_COLS) | {
        "n_regions", "n_fields", "n_sections", "sampling_unit",
    }
    for r in mouse_rows:
        for c in r:
            if c in skip or c in seen:
                continue
            if isinstance(r[c], (int, float)):
                metric_cols.append(c)
                seen.add(c)

    groups = defaultdict(list)
    for r in mouse_rows:
        groups[(r["genotype"], r["condition"], r["panel"])].append(r)

    out = []
    for (geno, cond, panel), grp in sorted(groups.items()):
        provenance = {}
        if any("aggregation_contract_version" in row for row in grp):
            for column in WSI_UNIFORM_PROVENANCE:
                values = {(row.get(column) or "").strip() for row in grp}
                if len(values) == 1:
                    provenance[column] = next(iter(values))
            for column in WSI_PER_SLIDE_PROVENANCE:
                list_column = column + "s"
                values = sorted({
                    value
                    for row in grp
                    for value in (row.get(list_column) or "").split(";")
                    if value
                })
                provenance[list_column] = ";".join(values)
        for metric in metric_cols:
            vals = [r[metric] for r in grp if isinstance(r.get(metric), (int, float))]
            if not vals:
                continue
            n, mean, sd, sem = _stats(vals)
            out.append({
                "genotype": geno,
                "condition": cond,
                "panel": panel,
                "metric": metric,
                "n_mice": n,
                "mean": mean,
                "sd": sd,
                "sem": sem,
                "reportability": (
                    "DESCRIPTIVE_ONLY" if n < 2 else "VARIABILITY_ESTIMABLE"
                ),
                **provenance,
            })
    return out


def write_csv(path, rows):
    path = os.path.abspath(path)
    descriptor, temporary = tempfile.mkstemp(
        dir=os.path.dirname(path),
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as fh:
            descriptor = -1
            if rows:
                cols = []
                for r in rows:
                    for c in r:
                        if c not in cols:
                            cols.append(c)
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow(r)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def quarantine_if_exists(path):
    """Move a previous canonical Stage 4 output aside before a new attempt."""
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stem, extension = os.path.splitext(path)
    stale = f"{stem}.STALE.{digest.hexdigest()[:12]}{extension}"
    os.replace(path, stale)
    return stale


def main():
    ap = argparse.ArgumentParser(description="Aggregate per-region run_summary.csv to mouse and group level.")
    ap.add_argument("run_summary", help="path to run_summary.csv from the Fiji pipeline")
    ap.add_argument("--outdir", default=None, help="output folder (default: alongside input)")
    ap.add_argument(
        "--sampling-unit",
        choices=("field", "section"),
        default="section",
        help=(
            "identity represented by section_id: use field for direct confocal "
            "images and section for slide/WSI inputs (default: section)"
        ),
    )
    ap.add_argument(
        "--endpoint-csv", default=None,
        help="optional endpoint_areas.csv; joins exact output_key/region matches and "
             "aggregates only evaluated rows",
    )
    ap.add_argument(
        "--endpoint-spec", default=None,
        help="endpoint JSON used to recompute its numerator/bare companion fraction; "
             "requires --endpoint-csv",
    )
    ap.add_argument(
        "--measurement-record-spec",
        default=None,
        help=(
            "optional explicit panel/endpoint mapping for schema-v2 JSONL emission; "
            "records must pass aggregation eligibility before mouse pooling"
        ),
    )
    ap.add_argument(
        "--direct-run-manifest",
        default=None,
        help=(
            "required sealed modern IF_Quant_Pipeline.groovy run_manifest.json "
            "when emitting direct-confocal schema-v2 records"
        ),
    )
    ap.add_argument(
        "--measurement-record-input",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help=(
            "additional content-bound provenance input for measurement records; "
            "repeatable and valid only with --measurement-record-spec"
        ),
    )
    args = ap.parse_args()

    if args.measurement_record_input and not args.measurement_record_spec:
        ap.error(
            "--measurement-record-input requires --measurement-record-spec"
        )
    if args.direct_run_manifest and not args.measurement_record_spec:
        ap.error(
            "--direct-run-manifest is valid only with --measurement-record-spec; "
            "legacy descriptive direct aggregation does not require it"
        )

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.run_summary))
    os.makedirs(outdir, exist_ok=True)

    prefix = "endpoint_" if args.endpoint_csv else ""
    mouse_path = os.path.join(outdir, prefix + "mouse_level_summary.csv")
    group_path = os.path.join(outdir, prefix + "group_level_summary.csv")
    audit_path = os.path.join(outdir, prefix + STAGE4_AUDIT_FILENAME)
    measurement_record_path = os.path.join(
        outdir, prefix + STAGE4_MEASUREMENT_RECORD_FILENAME
    )
    for prior in (mouse_path, group_path, measurement_record_path, audit_path):
        stale = quarantine_if_exists(prior)
        if stale:
            print(f"Quarantined prior Stage 4 publication artifact -> {stale}")

    # Seal the repository-owned source closure before parsing analytical input.
    # Every member is rechecked immediately before publication below.
    script_path = os.path.abspath(__file__)
    try:
        code_artifacts, code_snapshots = repository_python_code_closure(
            [("stage4_aggregator", script_path)], audit_path
        )
    except (OSError, AggregationAuditError, ValueError) as exc:
        sys.exit(f"ERROR: Stage 4 code-provenance closure failed: {exc}")

    if not os.path.isfile(args.run_summary):
        sys.exit(f"ERROR: not found: {args.run_summary}")
    if is_quarantined_summary(args.run_summary):
        sys.exit(
            "ERROR: refusing to aggregate a REJECTED or STALE diagnostic table "
            "to mouse/group level"
        )

    header, rows, run_summary_digest, run_summary_size = read_rows_snapshot(
        args.run_summary
    )
    if not rows:
        sys.exit("ERROR: no data rows in run_summary.csv")
    is_wsi_summary = validate_rows(header, rows)
    source_header = list(header)
    source_rows = list(rows)

    input_artifacts = [
        artifact_descriptor(
            args.run_summary,
            "aggregation_input_summary",
            audit_path,
            digest=run_summary_digest,
            size_bytes=run_summary_size,
        )
    ]
    tracked_snapshots = [
        *code_snapshots,
        (input_artifacts[0], os.path.abspath(args.run_summary)),
    ]
    direct_authority = None
    direct_manifest_signature = None

    if args.measurement_record_spec:
        try:
            schema_digest, schema_size = _stable_file_snapshot(
                MEASUREMENT_RECORD_SCHEMA_PATH,
                "measurement-record schema",
            )
        except (OSError, DirectRunManifestError) as exc:
            sys.exit(f"ERROR: cannot bind measurement-record schema bytes: {exc}")
        schema_descriptor = artifact_descriptor(
            MEASUREMENT_RECORD_SCHEMA_PATH,
            "measurement_record_schema",
            audit_path,
            digest=schema_digest,
            size_bytes=schema_size,
        )
        input_artifacts.append(schema_descriptor)
        tracked_snapshots.append(
            (schema_descriptor, os.fspath(MEASUREMENT_RECORD_SCHEMA_PATH))
        )

        if is_wsi_summary:
            if args.direct_run_manifest:
                sys.exit(
                    "ERROR: --direct-run-manifest is only valid for direct-confocal input"
                )
        else:
            if not args.direct_run_manifest:
                sys.exit(
                    "ERROR: schema-v2 direct-confocal publication requires "
                    "--direct-run-manifest from a sealed complete modern engine run"
                )
            try:
                direct_artifacts, direct_snapshots, direct_authority = (
                    validate_direct_run_manifest(
                        args.direct_run_manifest,
                        args.run_summary,
                        source_header,
                        source_rows,
                        run_summary_digest,
                        run_summary_size,
                        audit_path,
                    )
                )
            except (OSError, DirectRunManifestError, ValueError) as exc:
                sys.exit(f"ERROR: direct run manifest is not authoritative: {exc}")
            input_artifacts.extend(direct_artifacts)
            tracked_snapshots.extend(direct_snapshots)
            direct_manifest_signature = [
                (
                    artifact["role"],
                    artifact["path"],
                    artifact["path_kind"],
                    artifact["size_bytes"],
                    artifact["sha256"],
                )
                for artifact in direct_artifacts
            ]

    upstream_audit_path = None
    upstream_expected_artifacts = None
    if is_wsi_summary:
        upstream_audit_path = os.path.join(
            os.path.dirname(os.path.abspath(args.run_summary)),
            STAGE3_AUDIT_FILENAME,
        )
        stage3_script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "aggregate_tiles_to_slide.py",
        )
        aggregation_library_path = os.path.abspath(__file__)
        try:
            _, stage3_code_snapshots = repository_python_code_closure(
                [
                    ("stage3_aggregator", stage3_script_path),
                    ("aggregation_library", aggregation_library_path),
                ],
                upstream_audit_path,
            )
            upstream_expected_artifacts = {
                "stage3_slide_summary": args.run_summary,
                **{
                    descriptor["role"]: source_path
                    for descriptor, source_path in stage3_code_snapshots
                },
            }
            _, upstream_audit_payload, upstream_audit_digest = (
                read_aggregation_audit_snapshot(
                    upstream_audit_path,
                    expected_stage="stage3_slide_aggregation",
                    expected_artifacts=upstream_expected_artifacts,
                )
            )
        except AggregationAuditError as exc:
            sys.exit(
                "ERROR: WSI slide summary lacks a valid content-addressed Stage 3 "
                f"audit: {exc}"
            )
        upstream_descriptor = artifact_descriptor(
            upstream_audit_path,
            "upstream_stage3_audit",
            audit_path,
            digest=upstream_audit_digest,
            size_bytes=len(upstream_audit_payload),
        )
        input_artifacts.append(upstream_descriptor)
        tracked_snapshots.append((upstream_descriptor, upstream_audit_path))

    if args.endpoint_csv:
        if not os.path.isfile(args.endpoint_csv):
            sys.exit(f"ERROR: not found: {args.endpoint_csv}")
        endpoint_header, endpoint_rows, endpoint_digest, endpoint_size = (
            read_rows_snapshot(args.endpoint_csv)
        )
        endpoint_descriptor = artifact_descriptor(
            args.endpoint_csv,
            "endpoint_measurements",
            audit_path,
            digest=endpoint_digest,
            size_bytes=endpoint_size,
        )
        input_artifacts.append(endpoint_descriptor)
        tracked_snapshots.append(
            (endpoint_descriptor, os.path.abspath(args.endpoint_csv))
        )
        header, rows = _merge_endpoint_rows_snapshot(
            header, rows, endpoint_header, endpoint_rows
        )
        merged_is_wsi = validate_rows(header, rows)
        if merged_is_wsi != is_wsi_summary:
            sys.exit("ERROR: endpoint join changed the detected aggregation input mode")

    endpoint_relation = None
    if args.endpoint_spec:
        if not args.endpoint_csv:
            sys.exit("ERROR: --endpoint-spec requires --endpoint-csv")
        if not os.path.isfile(args.endpoint_spec):
            sys.exit(f"ERROR: not found: {args.endpoint_spec}")
        try:
            endpoint_spec_payload = Path(args.endpoint_spec).read_bytes()
            spec = json.loads(endpoint_spec_payload.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            sys.exit(f"ERROR: endpoint spec is unreadable: {exc}")
        if not isinstance(spec, dict):
            sys.exit("ERROR: endpoint spec root must be a JSON object")
        endpoint_spec_descriptor = artifact_descriptor(
            args.endpoint_spec,
            "endpoint_specification",
            audit_path,
            digest=hashlib.sha256(endpoint_spec_payload).hexdigest(),
            size_bytes=len(endpoint_spec_payload),
        )
        input_artifacts.append(endpoint_spec_descriptor)
        tracked_snapshots.append(
            (endpoint_spec_descriptor, os.path.abspath(args.endpoint_spec))
        )
        output = spec.get("output") or {}
        required_outputs = [
            "area_column", "bare_area_column", "numerator_fraction_of_bare_column"
        ]
        missing_outputs = [name for name in required_outputs if not output.get(name)]
        if missing_outputs:
            sys.exit("ERROR: endpoint spec output is missing: " + ", ".join(missing_outputs))
        endpoint_relation = {name: output[name] for name in required_outputs}

    existing_input_roles = {item["role"] for item in input_artifacts}
    for declaration in args.measurement_record_input:
        if "=" not in declaration:
            sys.exit(
                "ERROR: --measurement-record-input must use ROLE=PATH syntax"
            )
        role, input_path = declaration.split("=", 1)
        role = role.strip()
        input_path = input_path.strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", role) is None:
            sys.exit(
                "ERROR: measurement record input ROLE must contain only "
                "letters, digits, dot, underscore, or hyphen"
            )
        if role in existing_input_roles:
            sys.exit(
                f"ERROR: duplicate measurement record input role: {role}"
            )
        if not os.path.isfile(input_path):
            sys.exit(
                f"ERROR: measurement record provenance input not found: {input_path}"
            )
        try:
            input_size = os.path.getsize(input_path)
            input_digest = _sha256_file(input_path)
        except OSError as exc:
            sys.exit(
                f"ERROR: measurement record provenance input is unreadable: {exc}"
            )
        descriptor = artifact_descriptor(
            input_path,
            role,
            audit_path,
            digest=input_digest,
            size_bytes=input_size,
        )
        input_artifacts.append(descriptor)
        tracked_snapshots.append((descriptor, os.path.abspath(input_path)))
        existing_input_roles.add(role)

    measurement_records = None
    measurement_record_track = None
    if args.measurement_record_spec:
        if not os.path.isfile(args.measurement_record_spec):
            sys.exit(
                "ERROR: --measurement-record-spec not found: "
                + args.measurement_record_spec
            )
        try:
            measurement_spec_payload = Path(
                args.measurement_record_spec
            ).read_bytes()
        except OSError as exc:
            sys.exit(f"ERROR: measurement record spec is unreadable: {exc}")
        measurement_spec_descriptor = artifact_descriptor(
            args.measurement_record_spec,
            "measurement_record_specification",
            audit_path,
            digest=hashlib.sha256(measurement_spec_payload).hexdigest(),
            size_bytes=len(measurement_spec_payload),
        )
        input_artifacts.append(measurement_spec_descriptor)
        tracked_snapshots.append(
            (
                measurement_spec_descriptor,
                os.path.abspath(args.measurement_record_spec),
            )
        )
        expected_track = "area_wsi" if is_wsi_summary else "cell_confocal"
        expected_sampling_unit = "section" if is_wsi_summary else "field"
        if args.sampling_unit != expected_sampling_unit:
            sys.exit(
                "ERROR: measurement record integration requires "
                f"--sampling-unit {expected_sampling_unit} for {expected_track}"
            )
        try:
            measurement_spec = load_measurement_record_spec(
                measurement_spec_payload,
                expected_track=expected_track,
            )
            if direct_authority is not None:
                validate_direct_measurement_spec_authority(
                    measurement_spec, direct_authority
                )
            legacy_additive_columns = set(classify_columns(header)["sum_cols"])
            mapped_additive_columns = {
                endpoint[column]
                for profile in measurement_spec["profiles"]
                for endpoint in profile["endpoints"]
                for column in ("numerator_column", "denominator_column")
            }
            unsupported_additive_columns = sorted(
                mapped_additive_columns - legacy_additive_columns
            )
            if unsupported_additive_columns:
                raise RouteMeasurementSpecError(
                    "explicit endpoint mapping names column(s) that the production "
                    "aggregator does not pool additively: "
                    + ", ".join(unsupported_additive_columns)
                )
            measurement_records, _ = build_preaggregation_measurement_records(
                rows,
                measurement_spec,
                source_inputs=[
                    {"role": item["role"], "sha256": item["sha256"]}
                    for item in input_artifacts
                ],
                pool_columns=KEY_COLS,
            )
        except (RouteMeasurementSpecError, DirectRunManifestError) as exc:
            sys.exit(
                "ERROR: measurement records are not safe to aggregate: " + str(exc)
            )
        measurement_record_track = measurement_spec["track"]

    mouse_rows = aggregate_mice(
        header,
        rows,
        endpoint_relation=endpoint_relation,
        sampling_unit=args.sampling_unit,
    )
    grp_rows = group_stats(mouse_rows)

    try:
        write_csv(mouse_path, mouse_rows)
        write_csv(group_path, grp_rows)
        if measurement_records is not None:
            write_measurement_records_jsonl(
                measurement_record_path, measurement_records
            )

        # Re-check the exact byte snapshots after computation. For WSI, also
        # re-validate the complete upstream Stage 3 chain immediately before
        # sealing Stage 4 so no referenced artifact can drift mid-run.
        if upstream_audit_path is not None:
            read_aggregation_audit_snapshot(
                upstream_audit_path,
                expected_stage="stage3_slide_aggregation",
                expected_artifacts=upstream_expected_artifacts,
            )
        if direct_authority is not None:
            repeated_artifacts, _, repeated_authority = validate_direct_run_manifest(
                args.direct_run_manifest,
                args.run_summary,
                source_header,
                source_rows,
                run_summary_digest,
                run_summary_size,
                audit_path,
            )
            repeated_signature = [
                (
                    artifact["role"],
                    artifact["path"],
                    artifact["path_kind"],
                    artifact["size_bytes"],
                    artifact["sha256"],
                )
                for artifact in repeated_artifacts
            ]
            if (
                repeated_signature != direct_manifest_signature
                or repeated_authority != direct_authority
            ):
                raise AggregationAuditError(
                    "direct run authority changed before Stage 4 publication"
                )
        for descriptor, source_path in tracked_snapshots:
            verify_artifact_descriptor(descriptor, source_path)

        output_artifacts = [
            artifact_descriptor(mouse_path, "stage4_mouse_summary", audit_path),
            artifact_descriptor(group_path, "stage4_group_summary", audit_path),
        ]
        if measurement_records is not None:
            output_artifacts.append(
                artifact_descriptor(
                    measurement_record_path,
                    "stage4_measurement_records",
                    audit_path,
                )
            )
        audit = build_aggregation_audit(
            stage="stage4_mouse_aggregation",
            arguments={
                "sampling_unit": args.sampling_unit,
                "input_mode": (
                    "wsi_stage3" if is_wsi_summary else "direct_confocal"
                ),
                "endpoint_csv_supplied": bool(args.endpoint_csv),
                "endpoint_spec_supplied": bool(args.endpoint_spec),
                "measurement_record_spec_supplied": bool(
                    args.measurement_record_spec
                ),
                "direct_run_manifest_supplied": bool(args.direct_run_manifest),
                "measurement_record_schema_sha256": (
                    None
                    if not args.measurement_record_spec
                    else next(
                        item["sha256"]
                        for item in input_artifacts
                        if item["role"] == "measurement_record_schema"
                    )
                ),
                "measurement_record_track": measurement_record_track,
                "measurement_record_count": (
                    0 if measurement_records is None else len(measurement_records)
                ),
                "measurement_record_input_roles": sorted(
                    declaration.split("=", 1)[0].strip()
                    for declaration in args.measurement_record_input
                ),
                "output_prefix": prefix,
            },
            code_artifacts=code_artifacts,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
        )
        # The audit is deliberately the final member published in the set.
        write_json_atomic(audit_path, audit)
    except (OSError, AggregationAuditError, ValueError) as exc:
        # The two CSVs and audit are one publication set. Do not leave a
        # partial set looking like a complete current run.
        for partial in (
            mouse_path,
            group_path,
            measurement_record_path,
            audit_path,
        ):
            quarantine_if_exists(partial)
        sys.exit(f"ERROR: Stage 4 publication failed closed: {exc}")

    n_mice = len({(r["mouse_id"], r["genotype"], r["condition"]) for r in mouse_rows})
    print(f"Read {len(rows)} region rows.")
    print(f"Wrote {len(mouse_rows)} mouse x panel rows -> {mouse_path}")
    print(f"Wrote {len(grp_rows)} group x metric rows -> {group_path}")
    if measurement_records is not None:
        print(
            f"Wrote {len(measurement_records)} schema-v2 measurement records "
            f"-> {measurement_record_path}"
        )
    print(f"Wrote content-addressed aggregation audit last -> {audit_path}")
    print(f"Distinct animals: {n_mice}  (this is your statistical n, split by group)")
    print("Reminder: compare groups on the mouse-level metrics; n = mice.")
    print(f"Sampling units are reported as n_{args.sampling_unit}s.")


if __name__ == "__main__":
    main()
