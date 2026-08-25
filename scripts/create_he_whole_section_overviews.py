#!/usr/bin/env python3
"""Create calibrated H&E whole-section overview panels.

The renderer changes no histology pixels other than an optional whole-image
horizontal mirror and a small, automatically placed scale-bar plaque.  Scale
is recovered from the approved section-QC table and checked against the
overview dimensions before any output is written.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat, PngImagePlugin


@dataclass(frozen=True)
class SectionSpec:
    blind_id: str
    section_id: str
    mouse_id: str
    series_label: str
    source: Path
    source_width_px: int
    source_height_px: int
    source_pixel_width_um: float
    source_pixel_height_um: float
    overview_downsample: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-config", required=True, type=Path)
    parser.add_argument("--calibration-csv", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--section-id",
        action="append",
        dest="section_ids",
        help="Section ID to render; repeat as needed. The default is every mapped section.",
    )
    parser.add_argument("--scale-bar-um", type=float, default=5000.0)
    parser.add_argument(         "--scale-bar-placement",         choices=("auto", "bottom_left", "bottom_right", "top_left", "top_right"),         default="auto",         help="Scale-bar corner. The default selects the clearest corner automatically.",     )
    parser.add_argument(
        "--mirror-x",
        action="store_true",
        help="Mirror the complete overview left-right before adding the scale bar.",
    )
    parser.add_argument(
        "--contact-sheet",
        default="HE_whole_section_overviews__5mm_scalebar.png",
        help="Contact-sheet filename, or an empty string to omit it.",
    )
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        Path(r"C:\Windows\Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
        Path(r"C:\Windows\Fonts") / ("segoeuib.ttf" if bold else "segoeui.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def load_specs(args: argparse.Namespace) -> list[SectionSpec]:
    study = json.loads(args.study_config.read_text(encoding="utf-8-sig"))
    blind_by_section = {
        item["section_id"]: item["blind_section_id"]
        for item in study["blind_section_map"]
    }
    calibration_rows = {
        row["section_id"]: row
        for row in csv.DictReader(
            args.calibration_csv.open(encoding="utf-8-sig", newline="")
        )
    }
    sample_by_section: dict[str, tuple[str, str]] = {}
    for sample in study["samples"]:
        for section_id in sample["section_ids"]:
            series_label = section_id.rsplit("_", 2)[-2] + section_id.rsplit("_", 2)[-1]
            sample_by_section[section_id] = (sample["mouse_id"], series_label)

    requested = args.section_ids or list(blind_by_section)
    specs: list[SectionSpec] = []
    for section_id in requested:
        if section_id not in blind_by_section:
            raise SystemExit(f"Section is absent from the study blind map: {section_id}")
        if section_id not in calibration_rows:
            raise SystemExit(f"Section is absent from the calibration CSV: {section_id}")
        blind_id = blind_by_section[section_id]
        source = args.source_dir / f"{blind_id}__whole_section_reference.png"
        if not source.is_file():
            raise SystemExit(f"Missing whole-section reference: {source}")
        row = calibration_rows[section_id]
        mouse_id, series_label = sample_by_section[section_id]
        specs.append(
            SectionSpec(
                blind_id=blind_id,
                section_id=section_id,
                mouse_id=mouse_id,
                series_label=series_label,
                source=source,
                source_width_px=int(row["source_width_px"]),
                source_height_px=int(row["source_height_px"]),
                source_pixel_width_um=float(row["pixel_width_um"]),
                source_pixel_height_um=float(row["pixel_height_um"]),
                overview_downsample=float(row["analysis_downsample"]),
            )
        )
    return specs


def validate_overview_dimensions(image: Image.Image, spec: SectionSpec) -> None:
    expected = (
        math.ceil(spec.source_width_px / spec.overview_downsample),
        math.ceil(spec.source_height_px / spec.overview_downsample),
    )
    # QuPath thumbnails can round either edge down by one pixel at the pyramid
    # boundary; larger disagreement means the calibration row is not the source.
    if any(abs(actual - target) > 1 for actual, target in zip(image.size, expected)):
        raise SystemExit(
            f"Overview/calibration mismatch for {spec.section_id}: "
            f"image={image.size}, expected={expected} at "
            f"downsample={spec.overview_downsample:g}"
        )


def corner_score(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    gray = image.crop(box).convert("L")
    mean = ImageStat.Stat(gray).mean[0]
    histogram = gray.histogram()
    pixels = max(1, gray.width * gray.height)
    dark_fraction = sum(histogram[:220]) / pixels
    return mean - 100.0 * dark_fraction


def add_scale_bar(
    image: Image.Image,
    scale_bar_um: float,
    micrometres_per_pixel_x: float,
    requested_placement: str,
) -> tuple[Image.Image, dict[str, object]]:
    bar_px = round(scale_bar_um / micrometres_per_pixel_x)
    if bar_px < 20 or bar_px > image.width * 0.45:
        raise SystemExit(
            f"Implausible scale bar: {bar_px}px for an image {image.width}px wide"
        )

    label = f"{scale_bar_um / 1000:g} mm" if scale_bar_um >= 1000 else f"{scale_bar_um:g} µm"
    font = load_font(max(20, round(image.width * 0.022)), bold=True)
    probe = ImageDraw.Draw(image)
    label_box = probe.textbbox((0, 0), label, font=font)
    label_width = label_box[2] - label_box[0]
    label_height = label_box[3] - label_box[1]
    padding_x = max(15, round(image.width * 0.014))
    padding_y = max(10, round(image.height * 0.013))
    bar_thickness = max(7, round(image.width * 0.007))
    gap = max(6, round(image.height * 0.008))
    plaque_width = max(bar_px, label_width) + 2 * padding_x
    plaque_height = padding_y + bar_thickness + gap + label_height + padding_y
    margin = max(14, round(min(image.size) * 0.02))

    candidates = {
        "bottom_right": (
            image.width - margin - plaque_width,
            image.height - margin - plaque_height,
            image.width - margin,
            image.height - margin,
        ),
        "bottom_left": (
            margin,
            image.height - margin - plaque_height,
            margin + plaque_width,
            image.height - margin,
        ),
        "top_right": (
            image.width - margin - plaque_width,
            margin,
            image.width - margin,
            margin + plaque_height,
        ),
        "top_left": (margin, margin, margin + plaque_width, margin + plaque_height),
    }
    scores = {name: corner_score(image, box) for name, box in candidates.items()}
    placement = (
        max(scores, key=scores.get)
        if requested_placement == "auto"
        else requested_placement
    )
    left, top, right, bottom = candidates[placement]

    rgba = image.convert("RGBA")
    overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    radius = max(5, round(image.width * 0.005))
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        fill=(255, 255, 255, 232),
        outline=(30, 30, 30, 100),
        width=1,
    )
    bar_left = left + (plaque_width - bar_px) // 2
    bar_top = top + padding_y
    draw.rectangle(
        (bar_left, bar_top, bar_left + bar_px - 1, bar_top + bar_thickness - 1),
        fill=(0, 0, 0, 255),
    )
    text_left = left + (plaque_width - label_width) // 2
    text_top = bar_top + bar_thickness + gap - label_box[1]
    draw.text((text_left, text_top), label, font=font, fill=(0, 0, 0, 255))
    result = Image.alpha_composite(rgba, overlay).convert("RGB")
    return result, {
        "label": label,
        "requested_length_um": scale_bar_um,
        "bar_length_px": bar_px,
        "realized_length_um": bar_px * micrometres_per_pixel_x,
        "placement": placement,
        "corner_scores": scores,
    }


def save_png(
    image: Image.Image,
    destination: Path,
    spec: SectionSpec,
    orientation: str,
    micrometres_per_pixel_x: float,
    micrometres_per_pixel_y: float,
    scale_bar: dict[str, object],
) -> None:
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Description", f"H&E whole-section overview: {spec.section_id}")
    metadata.add_text("Orientation", orientation)
    metadata.add_text("SourceBlindSection", spec.blind_id)
    metadata.add_text("MicrometresPerPixelX", f"{micrometres_per_pixel_x:.12g}")
    metadata.add_text("MicrometresPerPixelY", f"{micrometres_per_pixel_y:.12g}")
    metadata.add_text("ScaleBar", json.dumps(scale_bar, sort_keys=True))
    image.save(destination, pnginfo=metadata, optimize=True)


def create_contact_sheet(
    rendered: list[tuple[SectionSpec, Path, Image.Image]], destination: Path
) -> None:
    columns = 2
    rows = math.ceil(len(rendered) / columns)
    tile_width = max(image.width for _, _, image in rendered) + 64
    tile_height = max(image.height for _, _, image in rendered) + 104
    title_height = 80
    canvas = Image.new(
        "RGB", (columns * tile_width, title_height + rows * tile_height), "white"
    )
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(32, bold=True)
    label_font = load_font(25, bold=True)
    draw.text((32, 20), "H&E whole-section overviews", font=title_font, fill=(20, 20, 20))
    for index, (spec, _, image) in enumerate(rendered):
        column = index % columns
        row = index // columns
        x0 = column * tile_width
        y0 = title_height + row * tile_height
        label = f"{spec.mouse_id}  |  H&E {spec.series_label}"
        draw.text((x0 + 32, y0 + 14), label, font=label_font, fill=(20, 20, 20))
        x = x0 + (tile_width - image.width) // 2
        y = y0 + 58
        canvas.paste(image, (x, y))
        draw.rectangle((x - 1, y - 1, x + image.width, y + image.height), outline=(190, 190, 190))
    canvas.save(destination, optimize=True)


def main() -> None:
    args = parse_args()
    if args.scale_bar_um <= 0:
        raise SystemExit("--scale-bar-um must be positive")
    specs = load_specs(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    orientation = "global horizontal mirror" if args.mirror_x else "native"
    rendered: list[tuple[SectionSpec, Path, Image.Image]] = []
    manifest_entries: list[dict[str, object]] = []

    for spec in specs:
        with Image.open(spec.source) as opened:
            image = opened.convert("RGB")
        validate_overview_dimensions(image, spec)
        if args.mirror_x:
            image = ImageOps.mirror(image)
        mpp_x = spec.source_pixel_width_um * spec.overview_downsample
        mpp_y = spec.source_pixel_height_um * spec.overview_downsample
        image, scale_bar = add_scale_bar(
            image, args.scale_bar_um, mpp_x, args.scale_bar_placement
        )
        suffix = "GLOBAL_LEFT_RIGHT_FLIP__" if args.mirror_x else ""
        destination = args.output_dir / (
            f"{spec.mouse_id}__HE_{spec.series_label}__{suffix}"
            f"whole_section_overview__{scale_bar['label'].replace(' ', '')}_scalebar.png"
        )
        save_png(image, destination, spec, orientation, mpp_x, mpp_y, scale_bar)
        rendered.append((spec, destination, image))
        manifest_entries.append(
            {
                "blind_section_id": spec.blind_id,
                "section_id": spec.section_id,
                "mouse_id": spec.mouse_id,
                "series_label": spec.series_label,
                "source": str(spec.source.resolve()),
                "source_sha256": sha256(spec.source),
                "source_size_px": list(image.size),
                "source_pixel_width_um": spec.source_pixel_width_um,
                "source_pixel_height_um": spec.source_pixel_height_um,
                "overview_downsample": spec.overview_downsample,
                "overview_micrometres_per_pixel_x": mpp_x,
                "overview_micrometres_per_pixel_y": mpp_y,
                "orientation": orientation,
                "scale_bar": scale_bar,
                "output": str(destination.resolve()),
                "output_sha256": sha256(destination),
            }
        )

    contact_path: Path | None = None
    if args.contact_sheet and rendered:
        contact_path = args.output_dir / args.contact_sheet
        create_contact_sheet(rendered, contact_path)

    manifest = {
        "schema_version": "1.0.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "renderer": str(Path(__file__).resolve()),
        "scale_source": str(args.calibration_csv.resolve()),
        "study_config": str(args.study_config.resolve()),
        "contact_sheet": str(contact_path.resolve()) if contact_path else None,
        "contact_sheet_sha256": sha256(contact_path) if contact_path else None,
        "sections": manifest_entries,
    }
    manifest_path = args.output_dir / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Rendered {len(rendered)} calibrated whole-section overview(s)")
    print(f"Output: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
