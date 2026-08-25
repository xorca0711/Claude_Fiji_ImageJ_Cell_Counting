#!/usr/bin/env python3
"""Build or verify the explicit run index consumed by WSI Stage 3."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from ifquant.stage2_index import (  # noqa: E402
    RunDeclaration,
    Stage2IndexError,
    build_stage2_index,
    sha256_file,
    validate_stage2_index,
    write_stage2_index_atomic,
)


def _run_path(value: str, slide_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else slide_dir / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a content-addressed WSI Stage 2 index from explicitly "
            "declared full-run or shard outputs. No recursive discovery occurs."
        )
    )
    parser.add_argument("--slide-dir", required=True, type=Path)
    parser.add_argument("--stage1-manifest", required=True, type=Path)
    parser.add_argument("--stage2-script", required=True, type=Path)
    parser.add_argument(
        "--run",
        action="append",
        nargs=3,
        metavar=("ANALYSIS_DIR", "SAMPLESHEET", "EXIT_CODE"),
        help=(
            "Declare one Stage 2 process. Paths may be absolute or relative to "
            "--slide-dir. Repeat for every shard."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output index (default: <slide-dir>/stage2_run_index.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify an existing --output instead of rewriting it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    slide_dir = args.slide_dir.resolve()
    output = (args.output or (slide_dir / "stage2_run_index.json")).resolve()
    published = False
    try:
        if output.parent != slide_dir:
            raise Stage2IndexError("--output must be directly inside --slide-dir")
        if args.check:
            validated = validate_stage2_index(
                output,
                slide_dir=slide_dir,
                stage1_manifest=args.stage1_manifest,
                stage2_script=args.stage2_script,
            )
            print(
                "Stage 2 index verified: "
                f"{len(validated.summary_paths)} declared run(s), "
                f"index={validated.document['index_sha256']}"
            )
            return 0
        if not args.run:
            raise Stage2IndexError("at least one --run declaration is required")
        declarations: list[RunDeclaration] = []
        for analysis_value, samplesheet_value, exit_value in args.run:
            try:
                exit_code = int(exit_value)
            except ValueError as exc:
                raise Stage2IndexError(
                    f"process exit code must be an integer: {exit_value!r}"
                ) from exc
            declarations.append(
                RunDeclaration(
                    analysis_dir=_run_path(analysis_value, slide_dir),
                    samplesheet=_run_path(samplesheet_value, slide_dir),
                    process_exit_code=exit_code,
                )
            )
        document = build_stage2_index(
            slide_dir=slide_dir,
            stage1_manifest=args.stage1_manifest,
            stage2_script=args.stage2_script,
            runs=declarations,
        )
        write_stage2_index_atomic(document, output)
        published = True
        validate_stage2_index(
            output,
            slide_dir=slide_dir,
            stage1_manifest=args.stage1_manifest,
            stage2_script=args.stage2_script,
        )
        print(
            f"Wrote complete Stage 2 index for {document['coverage']['expected_tile_count']} "
            f"tile(s) and {document['coverage']['summary_row_count']} region row(s): {output}"
        )
        return 0
    except (OSError, Stage2IndexError) as exc:
        if published and output.is_file():
            try:
                digest = sha256_file(output)[:12]
                quarantined = output.with_name(
                    f"{output.stem}.INVALID.{digest}{output.suffix}"
                )
                output.replace(quarantined)
                print(
                    f"Quarantined invalid newly published index: {quarantined}",
                    file=sys.stderr,
                )
            except OSError as quarantine_error:
                print(
                    f"ERROR: failed to quarantine invalid index {output}: "
                    f"{quarantine_error}",
                    file=sys.stderr,
                )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
