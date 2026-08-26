# IF Quant Windows launcher

`IFQuantLauncher-v1.9.7.exe` is a Windows Forms front end for the analysis
pipeline. It embeds the exact Groovy engine, marker registry, QuPath tiling
script, sharded Stage 2 orchestrator, Python modules and schemas present at
build time. **It does not reimplement image analysis.** Pixel/object marker
measurements come from the frozen `IF_Quant_Pipeline.groovy`; Route 2 tissue
geometry comes from the packaged QuPath script, and its derived slide/mouse/
group rollups come from the packaged Python aggregators.

## What the four routes replaced (introduced v1.8.0; current source v1.9.7)

v1.7.2 assumed one kind of input: a folder of confocal/field images measured by
Fiji. v1.8.0 makes the *kind of image* an explicit first choice, because the
correct chain of tools differs per kind and choosing wrongly produces numbers
that look fine and are not.

The old behaviour did not go away — it is route 4, and it is verified equal to
v1.7.2 by execution rather than by assertion (see **Legacy equivalence** below).

## The four routes

Route is the first thing selected, before any folder. Each route declares its
own stage list, its own required tools, and its own hazard policy; the launcher
refuses to start a run it cannot describe.

| # | Route | Tools | Produces |
|---|---|---|---|
| 1 | **IF — confocal / field images** | Fiji only | `run_summary.csv` (+ `.xlsx`, `run_manifest.json`), one row per (image, region) |
| 2 | **IF — slide scanner (`.vsi` whole slide)** | QuPath → Fiji → Python | tiles → per-tile measurements → slide, mouse, and group CSV summaries |
| 3 | **H&E / brightfield** | packaged Python review tools only | **biological execution disabled; separate engineering/review screen** |
| 4 | **Fiji-only legacy mode** | Fiji only | byte-for-byte the v1.7.2 environment and command line |

**Route 2 is the important architectural point.** QuPath reads and tiles the
slide; the *same* frozen Fiji engine measures the tiles; stages 3 and 4
reconcile tiles to slides and then to mouse/group summaries. QuPath defines
the tissue/tiling geometry and tissue-area denominator; it does not measure
marker positivity. The handoff is
file-based because the two applications ship incompatible Java versions
(Chiaruttini et al. 2022, *Front Comput Sci* 3:780026).

Route 2 also **hard-blocks** an omitted threshold. Routes 1 and 4 only flag it.
The difference is deliberate: a field run with adaptive thresholds is a
defensible exploratory measurement, whereas a slide run silently re-derives a
threshold on each of ~370 tiles, which is not one measurement at all.

Version 1.9.6 requires a fresh Stage 1 output, keeps resume disabled,
checks the ordered acquisition-channel labels, and exposes the engine's
probability, NMS, and tile-count StarDist settings. These are integrity and
configuration controls; by themselves they do not validate a StarDist model
or establish biological channel identity.

Version 1.9.7 completes that route end to end. The launcher leaves the chosen
Stage 1 root absent (or verifies that it is empty), lets QuPath create it, then
reads only the manifest-declared `slide_stem` values. Each value must be a
unique direct child name and its slide folder must contain `tiles/`,
`tiles/samplesheet.csv`, `tile_manifest.csv`, and
`tile_candidate_manifest.csv`. For every declared slide, the launcher invokes
the packaged `scripts/Invoke-Stage2Sharded.ps1` with the sealed Stage 2
environment and requires `stage2_run_index.json`. Only after every slide has an
index does it invoke Stage 3 once, explicitly binding the Stage 1 manifest and
the packaged Stage 2 engine. It then invokes the packaged Stage 4 aggregator.
Terminal success requires `stats/slide_level_summary.csv`,
`stats/mouse_level_summary.csv`, `stats/group_level_summary.csv`, and one
published index per declared slide.

The **Dry** tier is a Stage 1-only smoke test: after QuPath publishes a manifest
whose every declared slide has `dry_run=true`, `coverage_complete=false`, and
`n_written=0`, the launcher stops before Stages 2-4 and does not offer any
summary for aggregation. It still performs the packaged
Python/Fiji/PowerShell preflight and therefore requires the complete Route 2
toolchain; this catches a broken workstation setup before a later quantitative
run rather than treating Dry as a QuPath-only installation check. Slide metadata
is also required because Stage 1 itself validates it in Dry mode.

The embedded runtime is published to a content-addressed directory through a
unique staging tree and one atomic rename. Existing bundles are never repaired
or overwritten: every expected byte and path is validated, reparse points and
hard links are rejected, and each expected file is held against write/delete
for the launcher lifetime. The launcher revalidates exact directory membership
after acquiring those file leases; it does not rely on that enumeration as a
filesystem ACL. Closing or cancelling during Route 2 latches cancellation between
stages and terminates the current Windows Job Object; the launcher closes only
after the kernel reports that the job contains zero active processes.

### Optional Route 2 external reference masks

`Reference-mask profile JSON` is optional and is propagated only to QuPath
Stage 1 as `IFQ_WSI_REFERENCE_MASK_PROFILE`. Blank deliberately selects the
automatic DAPI/Otsu engineering mask. In that mode airway exclusion is not
available, and neither the launcher nor the Stage 1 manifest claims an
independently validated anatomical reference.

When a profile is selected, the launcher requires an absolute, readable,
regular non-reparse `.json` file whose root is an object. Stage 1 remains the
authority for the closed profile contract, slide/package/series/grid identity,
and the exact profile, tissue-mask and airway-mask bytes. An external profile
or an `expert_reviewed` declaration is provenance; it does not by itself prove
biological validity. The packaged runtime includes
`schemas/wsi-reference-mask-profile.schema.json` for inspection.

### Explicit StarDist model and runtime authority

Modern Route 1 or Route 2 runs with `Nucleus detection = stardist` require both
an exported `.zip` model and `stardist-runtime-manifest.schema.json`-conformant
JSON manifest. The launcher refuses blank, relative, missing, reparse-point or
wrong-extension paths; it caps the manifest at 1 MiB and checks its closed
top-level envelope before launch. It emits `IFQ_STARDIST_MODEL_PATH` and
`IFQ_STARDIST_RUNTIME_MANIFEST` only for StarDist. Classic segmentation emits
neither key, even when saved path values remain in the hidden fields.

The Fiji engine is the content authority: it verifies the model, manifest and
every declared StarDist/CSBDeep/TensorFlow artifact by size and SHA-256, checks
that required runtime classes were loaded from the declared files, and verifies
the content again after analysis. The Route 2 sharder copies the complete
launcher-sealed `IFQ_*` environment into every shard, so both exact paths reach
each engine process. It also accepts the same paths explicitly for direct use:

```powershell
.\scripts\Invoke-Stage2Sharded.ps1 `
  -TilesDir <tiles> -OutputRoot <slide-root> -Segmenter stardist `
  -StarDistModelPath <exported-model.zip> `
  -StarDistRuntimeManifest <stardist-runtime.json>
```

Both parameters are required for `stardist` and forbidden for `classic`. The
sharder reconciles explicit parameters with launcher-inherited values exactly,
holds both files against write/delete until index publication, and injects them
into every shard. Manifest-declared runtime artifacts remain verified by the
engine. Route 4 cannot represent the new keys without ceasing to be the exact
v1.7.2 environment, so Route 4 + StarDist is refused; use classic there or a
modern route for content-bound StarDist. These controls establish runtime
provenance, not model accuracy, segmentation performance or scientific suitability. See
`docs/STARDIST_RUNTIME.md` for the manifest contract and preparation workflow.

### Route 3 biological execution is disabled; review tools are isolated

Route 3 appears in the list, greyed, with a written reason. A separate **Open
H&E engineering/review tools** button is available beneath it. The button does
not select Route 3 and does not enter the fluorescence runner: its closed
command surface contains only the packaged `he_pipeline.py status`,
`build-review`, and `aggregate-review` commands. It never starts Fiji or
QuPath, and it cannot run the unvalidated H5/H6 nuclei, lesion, compartment, or
topology analysis.

The executable content-addresses and holds the exact H&E script, locked study,
review rubric, stain profile, measurement-record schema, and required
`ifquant` modules. Every operation first probes the selected executable through
the same contained `-I -B -S` process path, requires CPython 3.10 or newer,
reconciles `sys.executable` to the selected regular `python*.exe`, and records
its normalized path, version, and SHA-256 in the launcher receipt. The
aggregation audit and each schema-v2 record bind that same interpreter hash.

Before any review output is published, the launcher validates and read-locks
the declared source/R1/H4 trees, revalidates their membership after the child
exits, and validates every declared byte/hash. For an aggregate it additionally
parses the exact three CSV contracts and all 48 JSONL records, reconciles the
eight sections, four mice, six endpoints, every published locked review field
back to the selected blinded CSV, deterministic schema-v2 record IDs, the
measurement profile, provenance closure, and exact non-composite descriptors.
Publication uses a unique sibling
staging directory and a cancellation/publication state lock. The staging
validation lease denies write, delete, rename, and replacement of every
validated package file, and is disposed before the Windows directory rename; the launcher
then immediately reopens and fully revalidates the final path and held inputs
before marking it committed. Cancellation wins before the rename, or the
receipt explicitly reports either a committed result or an existing but
unauthorized final path if post-move validation fails. Existing output is never
resumed or overwritten.
Aggregation remains descriptive only: technical sections are not biological
replicates, no scalar ordinal composite is emitted, and group inference remains
unavailable.

Keeping biological execution greyed is intentional. Hiding it would invite
someone to point route 1 at an H&E slide, and **that would not fail** — the
fluorescence engine assumes bright signal on a dark background, which is
inverted for H&E. It would produce a complete, plausible, wrong
`run_summary.csv`.

Re-enabling is one line:

```csharp
// launcher/IFQuantLauncher.Routing.cs
public static readonly bool BrightfieldRouteEnabled = false;   // this build
```

It is `static readonly`, not `const`, so the branches are *not* folded away at
compile time and the disabled paths stay reachable and testable. Flipping it
makes the biological route selectable but does not conjure an engine:
`BuildStage2` still refuses with a named cause, so a half-finished re-enable
fails at the Run button instead of producing an empty run. It is not required
for, and does not widen, the isolated review-only screen.

An unknown route id fails closed on every axis — no tools assumed present, an
omitted threshold treated as a hard stop, nothing written.

## Legacy equivalence (route 4)

Route 4 exists so that analyses run before v1.8.0 stay reproducible. It is
checked by a harness that *executes* both versions rather than asserting about
them — `launcher/legacy_equivalence_report.txt`, **85 checks, 0 failures**:

- **Environment**: 7 fixture cases (defaults, all-non-default, each conditional
  key, both at once, an Advanced overlay that shadows a base key, and values
  containing spaces/quotes/non-ASCII) produce byte-identical variable sets.
- **Process level**: the *child process* environment is compared, not just the
  dictionary — inherited `IFQ_*` variables are stripped, and the child receives
  no `IFQ_MIN_INCLUDED_NUCLEI`, which v1.7.2 never wrote.
- **Windows key casing**: before materializing `ProcessStartInfo` the launcher
  normalizes duplicate case variants such as `Path`/`PATH`. .NET Framework's
  case-insensitive environment dictionary otherwise throws before the child
  process starts; values and all non-duplicate keys are preserved.
- **Drift guard**: the harness re-reads the real v1.7.2 source and confirms the
  key set, the assignment *order*, the hardcoded values, and that v1.7.2
  contains no QuPath/`.vsi`/brightfield reference. If someone edits the legacy
  profile to match a changed v1.7.2, this fails.
- **Advanced box**: 23 input lines, accepted/refused identically by both.

- **Artefact drift**: the harness verifies that drift is *detected and named*,
  not that it is absent. The embedded engine legitimately differs from v1.7.2's
  since the `blackBackground` fix, and asserting equality would force a choice
  between shipping a known-buggy engine and a red suite.

Run it from a clean clone, no arguments:

```powershell
powershell -ExecutionPolicy Bypass -File .\launcher\run_legacy_equivalence.ps1
```

The v1.7.2 reference source is committed at
`launcher/reference/IFQuantLauncher-v1.7.2.cs`, so this no longer requires
extracting it from git history.

## Build

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\launcher\build.ps1
```

Four source files are compiled into one self-contained `AnyCPU` executable
with no external dependencies; the same file supports Windows ARM64 and x64 and
needs no .NET SDK on the analysis system. The build **runs** `--self-test` and
a UI smoke test, and discards the binary on failure. (v1.7.2 shipped a
self-test and never ran it.)

Artifacts are written to the repository root and are **not committed** —
`.exe` and its `.sha256.txt` sidecar belong in GitHub Releases:

- `IFQuantLauncher-v1.9.7.exe`
- `IFQuantLauncher-v1.9.7.sha256.txt`

The build prints the SHA-256 of the exe and of each embedded artefact, so a
shipped binary can be traced to the exact engine it carries.

Visual merge panels generated by v1.9.3 contain a calibrated internal 100 micrometre scale bar.
The engine converts that length from the OIR micrometre-per-pixel calibration and draws a
6-pixel white bar with a one-pixel keyline. Missing or non-micrometre calibration fails
closed instead of producing a plausible but uncalibrated bar.

## Runtime requirements

- Windows ARM64 or x64 with .NET Framework 4.x
- Fiji with Bio-Formats and the plugins for the selected segmentation mode
- for StarDist: an exported `.zip` model plus a closed runtime manifest for the
  exact StarDist, CSBDeep and TensorFlow artifacts loaded by that Fiji
- QuPath 0.7+ console executable for route 2 only
- CPython 3.10+ for route 2 index validation and Stages 3-4, and for the
  isolated Route 3 engineering/review screen
- Windows PowerShell 5.1 for route 2's authoritative sharded orchestrator
- Fiji's bundled `java.exe` and `ij1-patcher-*.jar`; route 2 forces this path
- Route 2 `tiles` and slide output on the same hard-link-capable Windows volume
- a new absent or empty Stage 1 result root; multi-slide inputs use one panel
  and one sealed measurement profile for every manifest-declared slide
- images reachable through a local, mapped, or network folder

## Panel assignment

Leave the staining panel on **AUTO** when the complete marker combination is
present in image or folder names. AUTO assigns every matching analytical image
independently, so multiple recognized panels and validated marker subsets can
share one batch. An explicit `samplesheet.csv` panel is used first; otherwise
the marker names select a built-in preset and its fixed acquisition channel
order.

AUTO does **not** identify stains from fluorescence colours, intensity, or
image content. Any unknown image stops the run before Fiji starts, and a
nonstandard channel order requires a validated custom panel. The confirmation
dialog lists each allocated panel and its image count.

File scope is pinned above the scrolling configuration pane. For the validated
260808 lung cohort, **Use validated 20x 2k .oir fields** selects
`.*20x 2k.*\.oir`, excluding the 4x navigation acquisitions before AUTO panel
detection. The preset resolves 82 analytical inputs as 42 LEFT and 40 RIGHT
using the cohort's `samplesheet.csv`; it does not infer markers from image
appearance.

## Aggregation is not optional

Per-image or per-tile rows are **not** the statistical unit. Route 2 now runs
the packaged mouse/group aggregation as its required Stage 4. For the other
routes, run `aggregate_to_mouse.py` before any test; n = mice.

## Released binary vs a build from HEAD

The published **v1.9.0** release corresponds to commit `22afada`. Version 1.9.3
retains the post-release GUI repair and adds calibrated internal scale bars; the
complete Step 1 and Analysis settings groups are height-stabilized, and the
validated 20x/2k lung-field preset removes the need to recover a hidden expert
regular expression. Building from a later `HEAD` produces a different SHA-256
because both the launcher source and its embedded pipeline have advanced.

To reproduce the v1.9.0 release binary exactly, build from `22afada`.
