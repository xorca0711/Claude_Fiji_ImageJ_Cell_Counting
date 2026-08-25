# Whole-Slide (WSI) Tiling Workflow

> **Status: VALIDATED (plumbing) / PROPOSED (thresholds).**
>
> * The Stage 1 → 2 → 3 chain is validated end to end on real data: exported
>   tiles bit-identical to the source region, ROI areas exact, summed region
>   areas reconciling to 2.1e-16. The evidence table is section 10.
> * **No threshold in this document is calibrated for slide-scanner data.**
>   `IFQ_KRT5_THRESHOLD = 300` was derived from *confocal* controls and **does
>   not transfer** — see section 10.
> * **This route has not been run on the current study data.** The confocal
>   route was used instead. The 6-tile pilot is what validated the plumbing.
>
> Last checked against the relocated local run inventory: 2026-08-25.

This document describes the slide-scanner route: quantifying an Olympus VS200
`.vsi` whole-slide scan with the **unchanged** validated Fiji engine.

QuPath is a **front end only**. It reads the slide, detects tissue, and cuts
calibrated tiles. It never measures anything. Every number in the final result
comes from [`IF_Quant_Pipeline.groovy`](../IF_Quant_Pipeline.groovy), which is
not modified by this route.

## 1. Why not just point Fiji at the `.vsi`?

Two independent reasons.

1. A 57165 x 42154 x 4-channel 16-bit scan is 19 GB uncompressed. Fiji cannot
   hold it.
2. Fiji's bundled Bio-Formats **cannot decode these files at all**. The `.ets`
   pyramid tiles inside the hidden `_<name>_` sidecar folder are JPEG-2000, and
   Fiji ships no JAI codec:
   `ClassNotFoundException: com.sun.media.imageio.plugins.jpeg2000.J2KImageReadParam`.
   QuPath works because it bundles `ome-jai`. (Adding
   `<QuPath>/app/ome-jai-0.1.5.jar` to Fiji's classpath does fix Fiji, but the
   memory problem remains.)

The alternative — writing a second measurement engine in QuPath — was rejected
deliberately. It would drift from the validated morphology-primary decision
model. So QuPath tiles, and Fiji measures.

## 2. The three stages

```
Stage 1   qupath_wsi_tile_export.groovy   .vsi  -> tiles/*.ome.tif
          (QuPath 0.7 headless)                 + tiles/*.ome_RoiSet.zip
                                                + tiles/samplesheet.csv
                                                + tile_manifest.csv
                                                + stage1_manifest.json

Stage 2   IF_Quant_Pipeline.groovy        tiles -> run_summary.csv
          (Fiji headless)                           (one or more region rows per tile)
          build_stage2_run_index.py              + stage2_run_index.json

Stage 3   aggregate_tiles_to_slide.py     tiles -> slide_level_summary.csv
Stage 4   aggregate_to_mouse.py           slides -> mouse_level / group_level
```

## 3. The seam problem, and how it is solved for free

Tiles overlap by a halo so that an object sitting on a core boundary is fully
imaged. Overlap would double-count at seams.

`resolveTissueRois()` in the Fiji engine already looks for a
`<stem>_RoiSet.zip` beside each image and uses it as **the** measurement region.
So Stage 1 writes, per tile, one ROI equal to

> (tile CORE rectangle) INTERSECT (global tissue mask)

expressed in tile-local full-resolution pixels. The validated engine then:

* measures pod area as `positiveAreaInRoi(mask, region)` — clipped to the core;
* reports `region_area_um2` for the core only;
* keeps only nuclei whose centroid falls inside the region.

Summing tiles is therefore exact, **with no changes to the engine**.

### Naming trap

The engine strips only the **final** extension. For `tile.ome.tif` the companion
must be `tile.ome_RoiSet.zip`, **not** `tile_RoiSet.zip`. A misnamed file does
not error — it silently falls through to automatic tissue detection, or to the
whole 2304 x 2304 frame *including the halo*, which double-counts at every seam.

Never leave a bare `RoiSet.zip` in the tile folder: it is the last fallback
candidate and would be applied to every tile.

### Areas are exact; counts are not

`ParticleAnalyzer` **clips** nuclei at the ROI edge rather than excluding them,
so one nucleus straddling a core boundary can appear as a fragment in each
neighbour. Area endpoints are unaffected; cell counts run slightly high.

Measured on the pilot (6 adjacent tiles, 6215 cells): **22 duplicate pairs,
0.35%**. Stage 3 computes this from the per-cell centroids and records it as
`seam_duplicate_fraction`. It does **not** silently alter counts — the
correction is smaller than the threshold uncertainty, and a silent adjustment
would be harder to audit than a reported number.

## 4. Series selection: refusing to quantify the wrong image

A VS200 `.vsi` holds four series:

| # | Name | Size | µm/px | C | What it is |
|---|------|------|-------|---|------------|
| 0 | `label` | 7072 x 9399 | 2.723 | RGB | slide label |
| 1 | `overview` | 31401 x 14841 | 1.725 | 1 | **real DAPI fluorescence**, 4x |
| 2 | `20x_DAPI, FITC, Cy3, Cy5(Gray)_01` | 57165 x 42154 | **0.345** | **4** | **the scan** |
| 3 | `macro image` | 991 x 375 | none | RGB | macro thumbnail |

Three traps, all verified:

* **QuPath opens series 0 by default** — the label image.
* A "reject anything coarser than ~2 µm/px" rule is **not enough**. Series 1 is
  a genuine, properly calibrated DAPI fluorescence image at 1.725 µm/px. It
  would pass, and would produce a plausible single-channel result.
* `BioFormatsServerBuilder.checkImageSupport()` returns only **3** builders — it
  silently drops series 3 as a thumbnail. Builder position is not a series
  index, and builder count is not a series count. Use
  `loci.formats.ImageReader.getSeriesCount()`.

Stage 1 therefore requires **all** of: pixel size <= `IFQ_WSI_MAX_PIXEL_UM`
(0.5), exactly `IFQ_WSI_EXPECT_CHANNELS` (4) channels, `SizeZ == 1`, not a
thumbnail series, and channel names matching the expected pattern. It requires
**exactly one** match and hard-fails otherwise, logging every rejection reason.

## 5. The tissue denominator is a protocol decision

Whatever the study endpoint is, this route reports areas against `region_area_um2`
— the analysed tissue region — so what counts as "tissue" is a first-order
scientific choice, not cleanup.

> **Note.** The study endpoint is **not** "KRT5⁺ area / tissue area". It is
> `KRT5+ AND PDPN+ / (PDPN− OR KRT5+)`, defined in
> `config/endpoints/dysplastic_over_damaged.json`. The tissue denominator here is
> the *engine's* measurement region, which the endpoint module clips to; it is not
> the endpoint's denominator. See [`PROJECT_STATE.md`](PROJECT_STATE.md) §2.

Two settings change it materially, both measured on the pilot slide:

| Setting | Effect |
|---------|--------|
| `IFQ_WSI_FILL_INTERIOR_RINGS=true` | 75.06 -> **84.47 mm2** (+12.5%). This fills alveolar airspace. **Default `false`.** |
| `IFQ_WSI_TISSUE_DOWNSAMPLE` 16 vs 32 | 75.06 vs **91.15 mm2** (+21%). Pin it and record it. |

Both are written into `stage1_manifest.json` for every run. Do not compare
results produced with different values.

One further detail matters for the reconciliation check: this scanner reports
`pixelWidth != pixelHeight` (0.3449973537 vs 0.3449984138 µm). ImageJ computes
area as width x height, so Stage 1 does too. Squaring a single "pixel size"
scalar instead shifts every area by ~3e-6 — harmless in itself, but it puts a
permanent floor under the Stage 3 reconciliation and would mask a real problem.
Both values are recorded as `pixel_size_um` and `pixel_size_um_y`.

The global mask is computed **once per slide**, not per tile. Per-tile Otsu
would use a different threshold in every tile — a tile that is 95% airspace and
one that is 95% tissue would get wildly different cutoffs.

## 6. Stage 1 — settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `IFQ_WSI_INPUT` | *(required)* | a `.vsi` file, or a folder of them. `.ets` is refused. |
| `IFQ_WSI_OUTPUT` | *(required)* | output root |
| `IFQ_WSI_SLIDE_METADATA` | *(none)* | CSV: `vsi_filename,mouse_id,genotype,condition`. Overrides filename parsing. |
| `IFQ_WSI_CORE_PX` | `2048` | core tile size (706 µm at 0.345 µm/px) |
| `IFQ_WSI_HALO_PX` | `128` | overlap per side (44 µm, >2 cell diameters) |
| `IFQ_WSI_MIN_TILE_TISSUE_UM2` | `2000` | skip tiles with less core tissue than this |
| `IFQ_WSI_TISSUE_DOWNSAMPLE` | `16` | tissue detection resolution — **pin this** |
| `IFQ_WSI_TISSUE_BLUR_SIGMA` | `2.0` | in downsampled px |
| `IFQ_WSI_TISSUE_CLOSE_RADIUS` | `4.0` | morphological closing |
| `IFQ_WSI_TISSUE_OPEN_RADIUS` | `2.0` | morphological opening |
| `IFQ_WSI_MIN_FRAGMENT_MM2` | `0.05` | drop debris specks |
| `IFQ_WSI_FILL_INTERIOR_RINGS` | `false` | **fills airspace — see section 5** |
| `IFQ_WSI_MAX_PIXEL_UM` | `0.5` | reject coarser series |
| `IFQ_WSI_EXPECT_CHANNELS` | `4` | required channel count |
| `IFQ_WSI_COMPRESSION` | `ZLIB` | lossless. `J2K_LOSSY`/`JPEG` are refused. |
| `IFQ_WSI_PANEL` | `LEFT` | written into `samplesheet.csv` |
| `IFQ_WSI_ROI_NAME` | `alveolar_core` | ROI/region name — **see section 7** |
| `IFQ_WSI_RESUME` | `true` | skip tiles already written |
| `IFQ_WSI_MAX_TILES_PER_SLIDE` | `0` | smoke-test cap; records `coverage_complete=false` |

```powershell
$env:IFQ_WSI_INPUT  = "D:\Confocal_Images\20260806_CW\20260806_CW"
$env:IFQ_WSI_OUTPUT = "D:\IFQ_Runs\<run_name>"
& "X:\QuPath\QuPath-0.7.0 (console).exe" script .\qupath_wsi_tile_export.groovy
```

## 7. Stage 2 — running the unchanged engine

Three settings are **mandatory**. Each has a silent failure mode.

| Must set | Why |
|----------|-----|
| `IFQ_PANEL=LEFT` | The default is `T`, a placeholder pilot panel whose nuclear channel is index 4 and whose KRT5/AGER assignments are explicitly meaningless. Forgetting it produces garbage, not an error. |
| `IFQ_MIN_INCLUDED_NUCLEI=0` | Default is 1. A tile yielding zero nuclei becomes a per-image failure, and its tissue area **and** its KRT5 pod area vanish from `run_summary.csv`, biasing the slide fraction. |
| `IFQ_KRT5_THRESHOLD`, `IFQ_AGER_THRESHOLD`, `IFQ_T1A_THRESHOLD` | Without fixed thresholds the engine runs adaptive Otsu **per tile**. On a mostly-background tile Otsu splits pure noise and reports `KRT5_pod_area_frac` ~0.89. Summing 370 such tiles destroys the endpoint. |

The threshold token comes from the panel's marker name, uppercased with
non-alphanumerics stripped. For panel LEFT that is `DAPI`, `KRT5`, `AGER`,
`T1A`. There is **no** `IFQ_PDPN_THRESHOLD` or `IFQ_T1ALPHA_THRESHOLD` — the
registry-alias fallback does not reach built-in panels.

`IFQ_WSI_ROI_NAME` must contain an `alveol` token. AGER and T1A declare
`expectedCompartment:"alveolar"`; without a matching token every AGER/T1A call
degrades to `context_unresolved` or `indeterminate`. KRT5 has no compartment
requirement, so the primary pod-area endpoint is unaffected either way.

```powershell
$env:IFQ_INPUT_DIR  = "D:\IFQ_Runs\<run_name>\<slide>\tiles"
$env:IFQ_OUTPUT_DIR = "D:\IFQ_Runs\<run_name>\<slide>\analysis"
$env:IFQ_PANEL = "LEFT"; $env:IFQ_SEGMENTER = "classic"
$env:IFQ_MIN_INCLUDED_NUCLEI = "0"
$env:IFQ_KRT5_THRESHOLD = "<calibrated>"
$env:IFQ_AGER_THRESHOLD = "<calibrated>"
$env:IFQ_T1A_THRESHOLD  = "<calibrated>"
$fj = "X:\Fiji"
# Resolve the bundled JVM rather than hard-coding its version-stamped folder.
# The Fiji launcher .exe is broken on win-arm64, so the JVM is invoked directly.
$jre = (Get-ChildItem "$fj\java" -Recurse -Filter java.exe | Select-Object -First 1).FullName
& $jre `
  '--add-opens=java.base/java.lang=ALL-UNNAMED' `
  "-javaagent:$fj\jars\ij1-patcher-2.0.0.jar=init" `
  '-Djava.awt.headless=true' "-Dplugins.dir=$fj" '-Xmx6g' `
  -cp "$fj\jars\*;$fj\plugins\*" net.imagej.Main --headless `
  --run .\IF_Quant_Pipeline.groovy
```

Note the `ij1-patcher` javaagent and `--add-opens`. Without them the run dies
with `No _hooks field found in ij.IJ`.

### Heap sizing — do not over-allocate

`-Xmx` must leave room for the OS and for any other JVM running concurrently.
The development machine has 15.6 GB total, and asking for `-Xmx12g` on it makes
the JVM believe it has memory the OS then has to page to disk; the run gets
*slower*, not faster, and the symptom looks like slow I/O rather than swapping.

Rule of thumb: **one heavy JVM at a time**, `-Xmx` no more than about 40% of
physical RAM, and lower still when sharding Stage 2 — each shard is its own JVM
and they all draw on the same physical memory. Five shards at `-Xmx6g` on a
16 GB machine will thrash; size shard heaps to `RAM * 0.4 / n_shards`.

QuPath sizes its tile cache as a percentage of max heap, so an inflated `-Xmx`
also inflates a cache that cannot be backed by real memory.

### Runtime, and sharding

Measured on the pilot: **~2–3 minutes per tile**. At ~370 tiles that is roughly
**12–15 hours per slide** single-threaded. Stage 2 is embarrassingly parallel —
the engine loops over files with a per-file try/catch and one bad tile never
aborts the batch. Use `scripts/Invoke-Stage2Sharded.ps1` to split the tile
folder into N hard-linked shards (hard links cost no disk) and run N Fiji
processes. After all processes finish, the launcher writes a content-addressed
`stage2_run_index.json`. It binds every shard's samplesheet, exit status,
manifest, summary, tile/ROI hashes, run-manifest configuration, engine hash,
and declared ordered channel signature. Stage 3 reads only those declared
summaries. A sibling retry, partial output, or stale summary is never guessed.
Before launch, the orchestrator writes a content-addressed engine snapshot and
executes every shard from it. Read handles deny mutation of that snapshot,
Stage 1 manifests, samplesheets, tiles, and ROIs until the validated index is
published, so the recorded hashes refer to the bytes Fiji could actually read.

The index counts coverage by unique tile/section IDs, not CSV rows. A
partitioned tile may legitimately emit both damaged and intact region rows.
Duplicate protection instead uses `(section_id, region, panel)`, so a retry
cannot evade detection merely by changing `output_key`.

For an unsharded run, create the same index explicitly:

```powershell
python .\scripts\build_stage2_run_index.py `
  --slide-dir '<run>\<slide>' `
  --stage1-manifest '<run>\stage1_manifest.json' `
  --stage2-script '.\IF_Quant_Pipeline.groovy' `
  --run 'analysis' 'tiles\samplesheet.csv' 0
```

The index's channel signature is the ordered mapping declared by the panel
configuration. It does not yet prove that acquisition metadata labels match
that mapping; exact source-metadata verification remains an open gate. The
current cross-slide profile also does not bind external panel/registry file
bytes, per-image `__params.json` snapshots, or a normalized
ImageJ/Bio-Formats/Java runtime signature. Built-in-panel mechanics are covered;
custom-panel and scientific cohort promotion require those additional hashes.
Built-in acquisition labels are normalized to their summary IDs (for example,
`Pro-SPC-488` to `PROSPC` and `T1alpha-647` to `T1A`). An arbitrary custom
`fileLabel` alias still needs structured marker metadata; until that exists, the
index also cannot safely classify every populated marker-like derived column as
declared or undeclared.

## 8. Stage 3 — reconciliation

```powershell
python .\aggregate_tiles_to_slide.py --slide-root D:\IFQ_Runs\<run_name>
python .\aggregate_to_mouse.py D:\IFQ_Runs\<run_name>\stats\slide_level_summary.csv
```

Stage 3 exists mainly to make silent loss impossible. It **refuses** to write
`slide_level_summary.csv` when:

* a slide lacks a valid `stage2_run_index.json`;
* any declared artifact, tile/ROI, script, configuration, samplesheet,
  manifest, or summary hash has drifted;
* shard assignments overlap or do not exactly cover the tile manifest;
* a duplicate `(section_id, region, panel)` identity exists;
* an additive measurement is non-finite or only partly missing within a panel;
* an emitted additive marker column is blank for a panel whose indexed channel
  signature declares that marker (a measured zero must be written as `0`);
* Stage 1 omitted a low-tissue candidate before `tile_manifest.csv`, or did not
  record that count;
* a tile in `tile_manifest.csv` has no `run_summary.csv` row;
* `sum(region_area_um2)` differs from the Stage 1 core tissue area by >1%
  (which means the `_RoiSet.zip` files were not picked up);
* Stage 1 recorded `coverage_complete=false` or `dry_run=true`.

A slide missing 20 tiles still produces a perfectly plausible pod fraction, so
this is fatal. Diagnostic rows are written only to
`slide_level_summary.REJECTED.csv`; `--allow-incomplete` is retained as a
deprecated compatibility spelling and can no longer publish rejected rows at
the analytical filename. `--legacy-recursive-discovery` is likewise diagnostic-
only. If a current attempt fails, any older canonical slide summary is moved to
a hash-suffixed `.STALE` filename rather than left looking authoritative.
Rejected dataset rows are themselves marked `PROBLEM`, and Stage 4 refuses both
`.REJECTED` and `.STALE` inputs. Panel-specific markers that are absent from the
indexed panel signature may remain blank through mouse/group output rather than
becoming false zeros; blankness alone is never used to infer that absence.

The 2026-08-25 run-root inventory found five WSI Stage 1 layouts and no complete
whole-slide analytical run: all five record `coverage_complete=false`. The two
Stage 2/3 layouts are engineering fixtures only. One contains six tiles and
seven legitimate region rows; another contains an orphan partial retry beside
the complete retry. Those layouts motivated the index rules but do not become
scientific evidence by being indexable.

Pooling reuses `aggregate_to_mouse.classify_columns()` so slide-level and
mouse-level aggregation cannot drift. Fractions are always recomputed from
pooled numerators, never averaged across tiles — tiles differ in tissue area.

## 9. Statistical unit

**n = MICE.** Not tiles, not slides, not regions. A slide yields ~370 tile rows;
those are 370 measurements of one animal. `aggregate_to_mouse.py` reports
`n_mice`, and that is the n for any test.

The 2026-08-06 dataset is four slides = four mice, one per genotype x infection
cell. That supports pipeline validation and threshold freezing. It does not
support a group comparison.

## 10. Validation status

The plumbing is verified end to end on real data (QuPath 0.7.0, Fiji/ImageJ
1.54p, Olympus VS200 `.vsi`, 2026-08-06 dataset, 6-tile pilot):

| Check | Result |
|-------|--------|
| exported tile vs source region, all 4 channels | **0 differing pixels** (bit-identical) |
| pixel calibration round trip | `0.3449973537372698` µm preserved |
| channel names round trip | `DAPI, FITC, Cy3, Cy5(Gray)` preserved |
| series/resolution count of exported tile | 1 / 1 (flat, as the engine requires) |
| per-tile ROI area vs `tile_manifest.csv` core area | rel diff **0.00e+00**, 6/6 tiles, all in bounds |
| Stage 2 `sum(region_area_um2)` vs Stage 1 core tissue area | 1106122.8907924264 vs …67 µm², rel diff **2.1e-16** (machine epsilon) |
| Stage 2 batch status | `complete`, 6/6 success, 0 failures |
| Z handling on single-plane tiles | `range=1:1 projection=max source=single_slice_input` (no ZProjector) |
| seam count inflation | 22 duplicate pairs / 6215 cells = **0.35%** |
| full chain to `aggregate_to_mouse.py` | 6 tile rows -> 1 slide row -> 1 mouse, `n_mice=1` |

That last row is the point: the tile count never becomes the n.

**Thresholds are NOT validated on this route.** The gate defaults and any
`IFQ_*_THRESHOLD` values in examples here are placeholders. Per the project
rule — tune on 2-3 images, freeze, then batch — thresholds must be set from
blinded control review before any confirmatory run. Until then every call is
`adaptive_otsu_exploratory` and must be reported as exploratory.

**`IFQ_KRT5_THRESHOLD = 300` does not transfer here.** That value was derived
from *confocal* uninfected controls at 0.3107 µm/px, where the in-tissue 488
p99.99 was 283 and 255. On this scanner the same channel has an autofluorescence
floor an order of magnitude worse — per-image Otsu on uninfected `m6` chose 54.6
and reported 4.95% KRT5⁺, indistinguishable from an infected animal. Confocal
removing that floor is the whole reason the calibration became possible. A
slide-scanner threshold must be derived from slide-scanner controls.

**No study data has been through this route.** The 6-tile pilot above validated
the machinery; the 2026-08-06 `.vsi` slides were superseded by the 2026-08-08
confocal acquisition before a full slide run happened.
