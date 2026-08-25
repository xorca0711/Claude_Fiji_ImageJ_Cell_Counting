# IF Quant Windows launcher

`IFQuantLauncher-v1.9.5.exe` is a Windows Forms front end for the analysis
pipeline. It embeds the exact Groovy engine, marker registry, QuPath tiling
script and Python reconciliation script present at build time. **It does not
reimplement image analysis.** Every number it produces comes from
`IF_Quant_Pipeline.groovy`, which is frozen.

## What the four routes replaced (introduced v1.8.0; current source v1.9.5)

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
| 2 | **IF — slide scanner (`.vsi` whole slide)** | QuPath → Fiji → Python | tiles → per-tile measurements → `stats/slide_level_summary.csv` |
| 3 | **H&E / brightfield** | — | **not available in this build** |
| 4 | **Fiji-only legacy mode** | Fiji only | byte-for-byte the v1.7.2 environment and command line |

**Route 2 is the important architectural point.** QuPath reads and tiles the
slide; the *same* frozen Fiji engine measures the tiles; stage 3 reconciles
tiles back to one slide. QuPath never measures anything. The handoff is
file-based because the two applications ship incompatible Java versions
(Chiaruttini et al. 2022, *Front Comput Sci* 3:780026).

Route 2 also **hard-blocks** an omitted threshold. Routes 1 and 4 only flag it.
The difference is deliberate: a field run with adaptive thresholds is a
defensible exploratory measurement, whereas a slide run silently re-derives a
threshold on each of ~370 tiles, which is not one measurement at all.

### Route 3 is visible and deliberately unselectable

It appears in the list, greyed, with a written reason. That is a design choice,
not an oversight. Hiding it would invite someone to point route 1 at an H&E
slide, and **that would not fail** — the fluorescence engine assumes signal is
bright on a dark background, which is inverted for H&E. It would produce a
complete, plausible, wrong `run_summary.csv`.

Re-enabling is one line:

```csharp
// launcher/IFQuantLauncher.Routing.cs
public static readonly bool BrightfieldRouteEnabled = false;   // this build
```

It is `static readonly`, not `const`, so the branches are *not* folded away at
compile time and the disabled paths stay reachable and testable. Flipping it
makes the route selectable but does not conjure an engine: `BuildStage2` still
refuses with a named cause, so a half-finished re-enable fails at the Run
button instead of producing an empty run.

An unknown route id fails closed on every axis — no tools assumed present, an
omitted threshold treated as a hard stop, nothing written.

## Legacy equivalence (route 4)

Route 4 exists so that analyses run before v1.8.0 stay reproducible. It is
checked by a harness that *executes* both versions rather than asserting about
them — `launcher/legacy_equivalence_report.txt`, **84 checks, 0 failures**:

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

Three source files are compiled into one self-contained `AnyCPU` executable
with no external dependencies; the same file supports Windows ARM64 and x64 and
needs no .NET SDK on the analysis system. The build **runs** `--self-test` and
a UI smoke test, and discards the binary on failure. (v1.7.2 shipped a
self-test and never ran it.)

Artifacts are written to the repository root and are **not committed** —
`.exe` and its `.sha256.txt` sidecar belong in GitHub Releases:

- `IFQuantLauncher-v1.9.5.exe`
- `IFQuantLauncher-v1.9.5.sha256.txt`

The build prints the SHA-256 of the exe and of each embedded artefact, so a
shipped binary can be traced to the exact engine it carries.

Visual merge panels generated by v1.9.3 contain a calibrated internal 100 micrometre scale bar.
The engine converts that length from the OIR micrometre-per-pixel calibration and draws a
6-pixel white bar with a one-pixel keyline. Missing or non-micrometre calibration fails
closed instead of producing a plausible but uncalibrated bar.

## Runtime requirements

- Windows ARM64 or x64 with .NET Framework 4.x
- Fiji with Bio-Formats and the plugins for the selected segmentation mode
- QuPath 0.7+ for route 2 only
- Python 3 for route 2 stage 3 only
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

Every route produces per-image or per-tile rows. Those are **not** the
statistical unit. Run `aggregate_to_mouse.py` before any test; n = mice.

## Released binary vs a build from HEAD

The published **v1.9.0** release corresponds to commit `22afada`. Version 1.9.3
retains the post-release GUI repair and adds calibrated internal scale bars; the
complete Step 1 and Analysis settings groups are height-stabilized, and the
validated 20x/2k lung-field preset removes the need to recover a hidden expert
regular expression. Building from a later `HEAD` produces a different SHA-256
because both the launcher source and its embedded pipeline have advanced.

To reproduce the v1.9.0 release binary exactly, build from `22afada`.
