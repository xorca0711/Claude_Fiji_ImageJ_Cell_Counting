# AI handoff — machine-oriented project context

> **Purpose.** A dense, factual *technical* context transfer for an AI agent picking up this
> project with no conversation history. Written to be read start-to-finish.
> State reconciled 2026-08-12 against baseline `main` commit `e60b7e6`, the
> new four-mouse/eight-section H&E cohort, and the
> G-SURF research scheme.
> The human-facing entry points are [`../README.md`](../README.md) and
> [`../WORKFLOW.md`](../WORKFLOW.md); [`PROJECT_STATE.md`](PROJECT_STATE.md) is
> the living handoff, and [`../DEVELOPMENT.md`](../DEVELOPMENT.md) is the
> authorship and scientific-control statement.
> This file adds what those omit: environment traps,
> failure modes, and process lessons that are expensive to rediscover.

---

## 1. Identity

| | |
|---|---|
| **Repo** | `<REPOSITORY_ROOT>` · GitHub `xorca0711/IFQuant-Lung` |
| **Domain** | Mouse-lung immunofluorescence quantification |
| **Study** | IFN-γ *ligand* KO + PR8 influenza; does KO change dysplastic KRT5⁺ repair? |
| **Reference** | Lin X. et al., *J Clin Invest* 2024;134(19):e176828 (DOI 10.1172/JCI176828) |
| **Endpoint** | KRT5⁺PDPN⁺ area ÷ damaged alveolar area (PDPN⁻ ∪ KRT5⁺) |
| **Baseline HEAD** | `e60b7e6` · `main` == `origin/main` before the 2026-08-12 maintenance/H&E work · tags `v1.8.0`, `v1.9.0`, `v1.9.1`, `v2.0.0` |
| **Research scheme** | internal G-SURF research scheme (not stored in this repository) · M4-1 is **het**, matching raw filenames and `samplesheet.csv` |

---

## 2. Architecture ("one engine measures")

```
.vsi whole slide ──► QuPath 0.7 (reads, tiles, MEASURES NOTHING)
                          │ tiles/*.ome.tif + *_RoiSet.zip
.oir/.czi/.nd2 ───────────┴──► Fiji · IF_Quant_Pipeline.groovy
                                 THE ONLY MEASUREMENT ENGINE
                                          │
                                    Python (sums only)
                                    tile → slide → MOUSE
```

**Invariants — violating any of these is a design regression:**

1. **Exactly one measurement implementation.** A second engine inside QuPath was
   built on two branches and rejected (PRs #9, #10): two engines drift.
2. **QuPath reads and cuts. Python sums. Neither decides.**
3. **File-based handoff**, because QuPath and Fiji ship incompatible Java
   versions (Chiaruttini et al. 2022, *Front Comput Sci* 3:780026).
4. **Statistical unit is the mouse.** Nuclei/fields/sections are not replicates.
5. **Morphology before intensity.** Marker calls are three-state
   (positive / negative / **indeterminate**); "indeterminate" is a real answer.
6. **Display never touches measurement.** Display copies are duplicates.

---

## 3. Environment traps

These caused real failures. Check before assuming.

| Trap | Detail |
|---|---|
| **PowerShell 5.1** | No `&&` / `\|\|` / ternary. Heredocs with apostrophes break `git commit -m`; use `git commit -F <file>`. |
| **BOM kills Groovy** | `Set-Content -Encoding UTF8` writes a BOM; Groovy will not compile. Use `[System.IO.File]::WriteAllText($p,$t,(New-Object System.Text.UTF8Encoding($false)))`. |
| **Fiji launcher broken on ARM64** | Invoke the JVM directly: `<fiji>\java\**\java.exe --add-opens=java.base/java.lang=ALL-UNNAMED -javaagent:<fiji>\jars\ij1-patcher-*.jar=init -Djava.awt.headless=true -Dplugins.dir=<fiji> -Xmx1g -cp "<fiji>\jars\*;<fiji>\plugins\*" net.imagej.Main --headless --run <script>` |
| **Memory: 15.6 GB total** | `-Xmx4g` caused paging (19.1 GB committed, 15,831 hard faults). Cap at 1–2 g; **one JVM at a time**. Full-batch renders were OOM-killed (exit 137) repeatedly. |
| **PowerShell drops `""` args** | Empty-string arguments to native exes vanish, shifting every later positional. Cost an hour on the equivalence harness. |
| **Safety guard on drive-root paths** | `Remove-Item <DRIVE_ROOT>\<dir>` is blocked. The user must delete those. |
| **git writes to stderr** | PowerShell renders normal git progress as red errors. Check exit codes, not colour. |
| **`csc.exe`** | `<WINDOWS_FRAMEWORK_CSC>`. `/out` and `/target` must precede sources. Use `/main:` when linking the launcher (two entry points). |
| **QuPath has no groovy-json** | Use `qupath.lib.io.GsonTools`. Fiji *does* ship gson 2.14. |

---

## 4. Data locations

| Path | Contents | Disposable |
|---|---|---|
| `<LOCAL_CONFOCAL_DATA>` | **RAW — 391 `.oir`, 82 analysis fields.** Never write here | **NO** |
| `<LOCAL_WSI_DATA>` | 4 `.vsi` + `.ets` (5.3 GB pixel data) | **NO** |
| `<LOCAL_RUN_ROOT>\confocal_260808` | pre-fix run — **counts void**, areas valid | yes |
| `<LOCAL_RUN_ROOT>\confocal_260808_fixed` | **post-fix run — use this one** | regenerable |
| `<LOCAL_RUN_ROOT>\confocal_260809_rerun` | byte-identical independent reproduction; corrected endpoint outputs are **exploratory only** | regenerable |
| `<repo>\.cache\slide_channels` | deleted 2026-08-09; rebuild ~10 min via `scripts/cache_slide_channels.groovy` | yes |
| `<LOCAL_HE_DATA>` | 4 H&E VSI slides; 2 analytical 20x BF series per mouse | **NO** |
| `<LOCAL_RUN_ROOT>\he_20260812\02_pilot_r2_od018` | current H0-H3 H&E engineering pilot; 8 previews + 8 overlays + section QC | regenerable |

**Batch design:** 4 mice × 2 panels × ~10 fields. LEFT = DAPI/KRT5-488/AGER-555/T1α-647; RIGHT = DAPI/ProSPC-488/AGER-555/KRT8-647. 2048², single Z, 0.3107 µm/px, 12-bit.

---

## 5. Established results

| Result | Value |
|---|---|
| `IFQ_KRT5_THRESHOLD` | **300**, control-derived (p99.99 = 283, 255) |
| KRT5⁺ area, mouse level | M2 14.11 %, M4-1 11.98 % (PR8) vs M4-2 0.000 %, M6 0.003 % |
| Nucleus density, post-fix | **15,393.3 /mm²** pooled (was 152.5 → ~101× undercount) |
| Tile→slide reconciliation | **2.1e-16** |
| Launcher legacy equivalence | **84 checks, 0 failures**, runnable from a clone |

**Two claims executable from a clean clone with no data:**
```
powershell -ExecutionPolicy Bypass -File ./launcher/run_legacy_equivalence.ps1
powershell -ExecutionPolicy Bypass -File ./validation/run_demo.ps1
```

---

## 6. Negative results — do not re-derive

Recorded in [`NEGATIVE_RESULTS.md`](NEGATIVE_RESULTS.md). Test: lock a cut from
controls only (worst-of-both), apply to held-out infected, compute
R = infected fraction ÷ control fraction beyond the cut. **R ≈ 1 ⇒ rejected.**

| Marker / claim | R | Verdict |
|---|---|---|
| AGER as co-negativity marker | 0.99–1.05 | **RETRACTED** — circular; "intact" was *defined* by AGER density |
| KRT8 as discriminator | 0.80–1.25 at every cut | **REJECTED** — infected animals *bracket* the controls, so between-section staining variance > biological signal |
| Endpoint as KRT5⁺PDPN**⁻** | — | **WRONG SIGN.** Reference says KRT5⁺PDPN**⁺**; PDPN is expressed *by* dysplastic cells |

---

## 7. Failure modes seen in this project

All of these produced **plausible, wrong output with no error**. Assume more exist.

1. **`blackBackground` global pref flip.** A missing `black` token in
   `IJ.run(mask,"Options...", …)` wrote `Prefs.blackBackground=false` globally,
   inverting `Fill Holes` and erasing every nucleus not touching the frame.
   ~101× undercount, nothing crashed. Diagnosed by replay to **IoU = 1.0000**.
2. **Filename collision.** Olympus repeats field names across `_Cycle` folders;
   naming outputs by filename silently overwrote 8 of 80 panels while the log
   said 80. **Use `run_manifest.json` (`relative_path` → `output_key`).**
3. **Two mask formats.** `*_pod_mask` / `*_membrane_positive_mask` are uint8
   0/255; every `tissue__*_nuclei_mask` is a **uint16 label image**. A `>127`
   test renders nothing when a field has <128 objects. **Use `>0`.**
4. **Silent column drop — fixed for partition QC.** Damaged/intact areas and
   KRT5-in-intact tripwires now have explicit additive classifications and are
   recomputed at mouse level. New measurement columns still require an explicit
   pooling rule; never infer semantics from a generic numeric type.
5. **Declared-but-uncomputed denominator — executor implemented.** The endpoint
   evaluator now executes declarative AND/OR/NOT expressions for numerator and
   denominator and emits both areas plus their fraction. It still refuses the
   real corrected endpoint by default because T1A/PDPN is uncalibrated.
6. **The tissue exporter duplicated the `blackBackground` bug.** Its fresh-JVM
   reconstruction differed by 0.456% until the same missing `black` token was
   restored. A regression test now requires the fixed call; the real rerun then
   reconciled all 39 LEFT regions (worst rel. diff 3.285e-07).
7. **Caption lied about its own parameters.** Panels printed config values, not
   resolved ones — every caption read `[0-0]`.
8. **Otsu is *permissive* for broad markers.** It assumes two comparable-mass
   modes; a continuum splits near background.
9. **Disabled guard hid a defect and remains an explicit tradeoff.**
   `IFQ_MIN_INCLUDED_NUCLEI=0` preserves area measurements in sparse regions,
   but it also allowed the nucleus-segmentation collapse to exit successfully.
   It is still zero in confocal/WSI area workflows; candidate-acceptance QC and
   plausibility review are therefore mandatory. Do not describe this guard as
   restored.

---

## 8. Open items, ranked

1. **Corrected endpoint has never been computed defensibly.** The evaluator can
   now build the PDPN⁻ ∪ KRT5⁺ union denominator, but T1A/PDPN has no locked
   threshold and the reference used hand-traced regions. Calibration and manual
   outline validation, not boolean plumbing, are now the top blockers.
2. **DAPI is saturated at acquisition** (in-tissue p90 = 4095, 5–19 % clipped).
   Fixable only at the microscope — lower 405 gain. Permanently lossy if skipped.
3. **Terminal imaging n = 1 day-28 survivor per genotype × condition cell.**
   The factors are crossed, but there is no replication-based residual error;
   genotype, condition, and interaction inference are unavailable. Two further
   heterozygous infected mice died on days 8–9, so this is not enrollment n.
   Reference used n = 15/group. **No software change fixes this.**
4. **KRT5 = 300 rests on one clean control** (M6 has a LEFT-panel AGER staining
   failure confirmed by the PI — `frac>500` 0.0097 LEFT vs 0.289 RIGHT).
5. **Routes 1 and 2 never driven end-to-end** through the launcher UI.
6. **RIGHT panel unusable** — registry gives area mode only to KRT5/AGER/T1α.
7. Unlanded drafts on `claude/module-drafts` (**never merge**; copy pieces):
   `spatial/` (complete + smoke test), `morphometry/`, `hierarchy-contract/`,
   `injury_model_profiles/`.

---

## 9. Hard rules

- **`IF_Quant_Pipeline.groovy` is the frozen engine.** It was unfrozen exactly
  once, with explicit operator authorisation, for a one-token fix. Do not modify
  it otherwise; prefer env vars, wrappers, or separate modules.
- **Never commit image data** (`.vsi/.ets/.nd2/.tif`/tiles/outputs).
- **Built `.exe` belongs in GitHub Releases**, not git.
- **Never write to `<LOCAL_MICROSCOPY_ROOT>`.**
- **Do not rewrite git history** — `BRANCHING.md` and `launcher/README.md` quote
  SHAs as runnable instructions. `.git` is 9 MB; a rewrite saves a few MB.
- **`legacy/launchers/IFQuantLauncher-v1.7.2.exe` must not move.** Its path is
  hardcoded at `IFQuantLauncher.Routing.cs` `V172ExeArchivePath` and printed to
  users at runtime with its sha256 `bd8e7176…4a1c4`.
- **Refuse silent false success.** Fail loudly over producing a plausible number.

---

## 10. Process lessons

Costly, and not visible in the code.

- **Verify before asserting.** Several claims here were wrong on first statement
  and corrected only when measured: the undercount (89× single-field quoted as
  batch; pooled truth ~101×), "area unaffected max 0.0154 pp" (KRT5 only; worst
  across markers 0.0209 pp), and the endpoint sign itself. **Read the primary
  source before calibrating against it, not after.**
- **Name the estimator.** Pooled, mean-of-per-field and single-field gave 101×,
  109× and 89× for the same bug. All internally consistent; shipping several is
  what looks like sloppiness.
- **Give parallel agents disjoint file ownership.** Two workflows editing
  `launcher/*.cs` concurrently produced ambiguous authorship and a broken tree.
- **Probe on 2–4 items before a full batch.** Three 82-field renders were burned
  testing one parameter.
- **Object-level questions need object-level tools.** Seven display versions
  failed trying to express "true-positive **cell**" with pixel thresholds.
- **A per-image adaptive display must record what it chose**, or it is
  unauditable. Uniform *rule* ≠ uniform *effect*: a filter chosen because the
  content you dislike disappears is selective manipulation
  (Rossner & Yamada 2004, *J Cell Biol* 166:11), regardless of its name.

---

## 11. Division of labour — see DEVELOPMENT.md

The operator is a wet-lab scientist who owns the biology. Several of this
project''s most consequential corrections came from them rather than from code
or from an automated agent — cell-type identity, section-level staining
judgement, the control-locked threshold rule, the rejection of two markers, and
the refusal of an analysis that would have been selective manipulation.

That record now lives in **[`../DEVELOPMENT.md`](../DEVELOPMENT.md)**, which is
the authorship and scientific-control statement for this repository. Read it
before proposing anything; it is not a courtesy document, it defines which
decisions are not yours to make.

**Escalate, do not decide alone:** cell-type identity · whether a population is
specific staining or artefact · acquisition settings · what a figure must show ·
which animals or sections are usable · anything outward-facing (push, release,
deleting tracked history) · unfreezing the engine · trading measurement validity
for appearance.

## 12. Key files

| Path | Role |
|---|---|
| `IF_Quant_Pipeline.groovy` | measurement engine (frozen) — bug fix at ~line 1784 |
| `qupath_wsi_tile_export.groovy` | Stage 1 tiling |
| `aggregate_tiles_to_slide.py` / `aggregate_to_mouse.py` | Stage 3 / mouse roll-up |
| `endpoints/evaluate_endpoints.groovy` | relational endpoints by mask algebra |
| `config/endpoints/dysplastic_over_damaged.json` | **corrected** spec |
| `config/endpoints/ectopic_pod_over_damaged.json` | retracted; kept as the record |
| `panels/MergePanels.java` | merge panels (photograph) |
| `panels/qc/RenderPanels.java` | QC overlays (analysis result) |
| `launcher/` | v1.9.3 GUI build with calibrated internal visual-merge scale bars, 4 routes; H&E remains disabled; `run_legacy_equivalence.ps1` |
| `config/brightfield/` | proposed H&E decision hierarchy and endpoint tiers |
| `config/studies/g_surf_he_20260812.json` | verified 4-mouse/8-section H&E identity contract |
| `docs/HE_BRIGHTFIELD_DECISION_HIERARCHY.md` | H&E scope, QC gates, endpoints, outputs and validation ladder |
| `brightfield/qupath_he_exploratory_pilot.groovy` | standalone review-gated H0-H3 H&E engineering pilot; not a biological endpoint engine |
| `scripts/Invoke-HePilot.ps1` / `scripts/Test-HePilotOutput.ps1` | timestamped QuPath pilot runner and fail-closed output validator |
| `validation/` | synthetic fixture demonstrating the bug from a clone |
| `scripts/` | calibration and probe scripts |
