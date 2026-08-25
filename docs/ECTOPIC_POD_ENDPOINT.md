# Ectopic KRT5+ pod endpoint — the calibration record

> # ⚠ SUPERSEDED AS A SPECIFICATION — RETAINED AS THE CALIBRATION RECORD
>
> **The endpoint defined in this document has the wrong sign and the wrong
> denominator. Do not implement anything from sections 1 or 4d.**
>
> This document says the numerator is `KRT5+ AND PDPN−`. Lin et al. 2024
> (J Clin Invest 134(19):e176828) Fig 2A–B say **`KRT5+PDPN+`**, over a
> hand-traced **union** `(PDPN− OR KRT5+)`. Section 4d below quotes the paper
> incorrectly, and the error propagated into the implementation, into
> `config/endpoints/ectopic_pod_over_damaged.json`, and into the only endpoint
> numbers that exist. It was caught by reading the primary source rather than
> the citation of it.
>
> **Current specification:** `config/endpoints/dysplastic_over_damaged.json`.
> **Current state of everything:** [`PROJECT_STATE.md`](PROJECT_STATE.md) §2.
>
> The file is kept, unrewritten, because it is the only record of a substantial
> amount of work that was done correctly — a control-only operating-point lock, a
> measured retraction, and an executed set of guards — and because a measurement
> that is sound while pointed at the wrong quantity is worth being able to read.
>
> ### Section-by-section verdict
>
> | § | subject | verdict |
> |---|---|---|
> | 1 | endpoint definition `KRT5+ / damaged alveolar area` | **WRONG SIGN, WRONG DENOMINATOR.** Superseded. The *reasoning* for normalising to damage rather than total tissue still stands. |
> | 2 | "do not use AGER/PDPN to find airways" | **STILL TRUE and still binding.** The most damaged alveolus is the most AGER-poor, and that is where pods form. |
> | 3 | Stage 1 two-ROI anatomical partition | **STILL VALID as a mechanism**, still unimplemented for airways (needs hand-drawn annotations). |
> | 4 | why fixed thresholds are load-bearing | **STILL TRUE.** The `m6` Otsu = 54.6 → 4.95% KRT5 on an uninfected control is the reason nothing adaptive is trusted. |
> | 4b | first damage-detector parameters (AGER 200, σ 30, cutoff 0.10) | **RETIRED.** Self-labelled: tuned on the outcome. |
> | 4c | LOCKED damage detector (AGER 150, σ 40 µm, cutoff 0.14) | **DERIVATION SOUND, DETECTOR RETIRED.** The selection rule was declared before any number was read and the controls-only discipline is the part worth keeping. But the reference's denominator is a hand-traced union, not a density detector — so this solves a problem the reference does not have. It is **not** the endpoint denominator. |
> | 4d | KRT5 numerator, co-negativity, PDPN ceiling t = 200 | **SUPERSEDED.** Contains the mis-quote. The AGER retraction inside it is correct and still stands; see [`NEGATIVE_RESULTS.md`](NEGATIVE_RESULTS.md) §1. |
> | 5 | Stage 1 ROI naming | **STILL TRUE.** |
> | 6 | mandatory companions | item 1 is **DONE** (`IFQ_KRT5_THRESHOLD = 300`); items 2–4 still stand. |
> | 7 | open questions | **STILL OPEN**, except the two-ROI RoiSet round trip, which the WSI pilot exercised. |
> | morphometry | damaged/intact cross-check | **DIRECTIONAL ONLY.** Its MLI is inter-nuclear spacing, not the classical quantity — the document says so. |
> | 9 | partition QC columns dropped at mouse level | **FIXED 2026-08-09.** Explicit additive classification and pooled-fraction regression tests; panel shape unchanged. |
> | 8 | what this pilot can and cannot claim | **CONCLUSION STILL TRUE; RATIONALE CORRECTED.** n = 1 per crossed design cell leaves no replication-based residual error. The factors are not structurally confounded. |
> | 10 | whole-field region source + the 260808 endpoint run | **MECHANICALLY VALIDATED, WRONG ENDPOINT.** Read its own banner. |
>
> Everything below this line is preserved as written on 2026-08-07/08, except for
> inline correction notes marked **`[CORRECTION 2026-08-08]`**.

---

## 1. The endpoint

> **[CORRECTION 2026-08-08]** Superseded. The current definition is
> `KRT5+ AND PDPN+ area / (PDPN− OR KRT5+) area`, in
> `config/endpoints/dysplastic_over_damaged.json`. The denominator below —
> a per-pixel AGER-density damage mask — is **not** what the reference measured;
> theirs is a hand-traced union of regions. The argument for normalising to
> damage rather than to total tissue (immediately below) is the part that
> survives.

```
ectopic pod fraction  =   KRT5+ area  /  damaged alveolar area
```

both measured **within alveolar parenchyma only**, with conducting airways
excluded from numerator and denominator alike.

* **damaged alveolar area** = alveolar tissue lacking AT1 coverage.
  Primary marker **AGER⁻**. PDPN⁻ is computed alongside as a cross-check.
* **numerator** = KRT5+ area inside that same alveolar region.

### Why not KRT5+ area / total tissue area

Because it silently measures infection severity. Pod area scales with how much
lung was damaged, and that varies mouse to mouse. A smaller pod area in a KO
animal could simply mean that animal had a milder infection. Normalising to the
damaged area is what makes the genotype comparison honest.

### Why AGER rather than PDPN as the AT1 marker

PDPN also marks lymphatics and is induced on injured fibroblasts and some
inflammatory cells; AGER (RAGE) is tightly AT1-restricted. Both are on the same
section in panel LEFT (KRT5-488 / AGER-555 / T1α-647), so both denominators are
computable from one image and can be reported side by side. **AGER⁻ is primary.**

### Reference method

Lin X. et al., *Viral infection induces inflammatory signals that coordinate YAP
regulation of dysplastic cells in lung alveoli*, J Clin Invest 2024;134(19):e176828.
DOI: https://doi.org/10.1172/JCI176828 (PMID 39352385). Retrieved via PubMed.

Their metric is "percentages of KRT5+ dysplastic cell areas in damaged alveolar
areas (PDPN− and KRT5+)". Their methods state the areas "were measured using
outline spline in the measure menu of Axiovision 4.8" — i.e. **drawn by hand**.

Two consequences follow, and both matter:

1. There is no published automated threshold to inherit. Any automated
   implementation must be validated against manual outlines on a subset before
   its numbers mean anything.
2. Manual outlining is *why* airway exclusion never appears as a rule in the
   literature. A human drawing "damaged alveolar area" excludes conducting
   airways by eye without writing anything down. An automated version has to
   make that implicit step explicit.

---

## 2. Hazard: do NOT use AGER/PDPN to find airways

Airway epithelium is AGER/PDPN-negative. So is severely injured alveolar
parenchyma — because PR8 destroys the AT1 sheet. **The most damaged alveolar
regions are the most AGER-poor, and that is precisely where pods form.**

An "exclude AGER-poor regions as airway" rule would therefore preferentially
delete the pods it is meant to measure, biasing the result toward the null —
in the same direction as the hypothesis being tested. That is the worst kind of
analysis bug: one that produces the expected answer.

**AGER⁻ as the damaged-area denominator is correct. AGER⁻ as an airway detector
is not.** These are different uses of the same channel and must not be conflated.

Nothing in the 4-channel panel (DAPI / KRT5 / AGER / PDPN) separates conducting
airway from injured alveolus on marker identity alone.

---

## 3. Implementation: anatomical partition at Stage 1

No change to the frozen `IF_Quant_Pipeline.groovy`. The engine already accepts
multiple named, non-overlapping ROIs per image and derives an anatomical tag
from each ROI name. Stage 1 writes **two** ROIs per tile instead of one:

| ROI name | geometry | tag |
|---|---|---|
| `alveolar_core_<tileid>` | core ∩ tissue − airway | alveolar |
| `airway_core_<tileid>` | core ∩ tissue ∩ airway | airway |

(the airway ROI is omitted when empty). The engine emits one summary row per
region; Stage 3 reports the alveolar row as the ectopic endpoint.

This is numerically exact: the KRT5 area mask is built once per tile on the whole
field *before* any region clipping, so both regions are measured against an
identical mask at an identical threshold, and alveolar + airway area = whole-tile
area. The existing Stage 3 area reconciliation therefore still passes unchanged
and keeps guarding against silent tile loss.

**It also keeps the airway signal as a built-in positive control.** Airway KRT5
per mm² of airway tissue should be present in every group including uninfected
controls, and should not differ by genotype. If it does, that is a staining or
threshold problem, discovered inside the same run rather than after the fact.

### The manual step, stated honestly

Airway polygons are drawn by a human in QuPath at slide level, exported as
GeoJSON in full-resolution slide coordinates, and subtracted per tile by Stage 1.

A geometry-only proposer (enclosed lumen + continuous DAPI-dense epithelial ring,
computed on the DAPI channel Stage 1 already thresholds) may propose candidates
for human confirmation. It must not touch the KRT5 channel, and it fails on
longitudinal and tangential airway cuts, which have no closed lumen. Semi-automatic
proposal, human confirmation, blinded, KRT5 channel off.

Annotation burden is an **estimate, not a measurement**: tens of airway profiles
per section, plausibly 15–30 min per slide, four slides, once. Count them on one
slide before committing.

---

## 4. Calibration: fixed thresholds are load-bearing

Measured on the four pilot slides, whole-slide, DAPI-Otsu tissue mask,
per-image Otsu on the KRT5 channel:

| slide | KRT5 Otsu threshold | KRT5⁺ % of tissue |
|---|---|---|
| het m4-1 **infected** | 218.0 | 6.68 |
| hom m2 **infected** | 290.5 | 4.41 |
| het m4-2 *uninfected* | 224.8 | **0.09** |
| hom m6 *uninfected* | **54.6** | **4.95** |

Uninfected `m6` reads 4.95% KRT5⁺ — indistinguishable from an infected animal —
because per-image Otsu chose a threshold of 54.6, inside that channel's noise
floor (in-tissue p50 ≈ 36, p90 ≈ 70). Uninfected `m4-2`, thresholded at 224.8,
reads 0.09%.

Otsu assumes a bimodal distribution. On a slide with almost no KRT5 signal there
is no second mode, so it splits noise. **Per-image adaptive thresholds would
manufacture pods in uninfected controls.** AGER thresholds drift too (527 → 742,
~40%), so AGER needs a fixed value as well.

`IFQ_KRT5_THRESHOLD`, `IFQ_AGER_THRESHOLD` and `IFQ_T1A_THRESHOLD` must be frozen
from blinded control review **before** any batch run. Until then every call is
`adaptive_otsu_exploratory` and must be reported as exploratory.

> **[CORRECTION 2026-08-08] Partly resolved — but only on confocal data.**
> `IFQ_KRT5_THRESHOLD = 300` is now calibrated from the uninfected confocal
> controls (in-tissue p99.99 = 283 and 255) and both 260808 runs record
> `KRT5_threshold_source = fixed_predeclared`. It rests on **one** sound control:
> M6 LEFT is an established section-level staining failure.
> **AGER and T1A remain uncalibrated on purpose** — they are constitutively
> expressed, so "the control should be negative" gives no calibration handle, and
> both still run `adaptive_otsu_exploratory`. The slide-scanner numbers in this
> section are unaffected by that: they are why the confocal acquisition happened.

There is no secondary-only control section for this dataset. The available
biological negative is uninfected alveolar parenchyma, which controls for ectopic
alveolar KRT5 but **not** for airway KRT5. State this as a limitation.

Note also that the KRT5/488 channel was acquired at ~949 ms exposure versus
~0.5–2 ms for the other channels, so autofluorescence in that band is a live
concern and argues for a conservative threshold.

---

## 4b. How the damaged area is computed, and its measured parameters

Healthy alveolus is mostly airspace with thin AT1 membranes, so "pixels below
the AGER threshold" is not the damaged area. Binary closing of the AGER mask was
tried first and **failed**: it reported 61–87% damaged even in uninfected lung,
because at 5.5 µm/px the membranes are largely sub-resolution.

What works is a **local area fraction**: AT1-intact territory is where AGER⁺
pixels occupy at least `cutoff` of an alveolus-sized neighbourhood. Smoothing a
0/1 mask with a Gaussian *is* that local fraction.

Measured on all four pilot slides (detection at 2.76 µm/px):

| slide | AGER in-tissue p50 | AGER⁺% | **damaged%** |
|---|---|---|---|
| het m4-1 **infected** | 304 | 71.6 | **12.4** |
| hom m2 **infected** | 285 | 69.9 | **9.9** |
| het m4-2 *uninfected* | 314 | 80.5 | **2.1** |
| hom m6 *uninfected* | 369 | 95.6 | **1.0** |

at `IFQ_WSI_AGER_THRESHOLD=200`, `IFQ_WSI_DAMAGE_SIGMA_UM=30`,
`IFQ_WSI_DAMAGE_CUTOFF=0.10`.

The **het pair is the internal control**: `m4-1` and `m4-2` have matched AGER
staining intensity (in-tissue p50 304 vs 314) yet separate 12.4% vs 2.1%, so the
separation is not a staining-intensity artifact.

**It is fragile.** At a fixed threshold of 400 the separation vanishes; at 500+
it inverts. With *per-slide adaptive* Otsu it inverts at every one of 15
parameter combinations tested — uninfected read **more** damaged than infected —
because Otsu picked higher AGER thresholds on the uninfected slides (690/864 vs
616/617). Stage 1 therefore refuses to partition without an explicit
`IFQ_WSI_AGER_THRESHOLD`.

**Those values were tuned on the outcome** — they were chosen by looking at how
well infected and uninfected separated, which is exactly the thing the endpoint
is supposed to measure. They have been superseded. See 4c.

## 4c. Operating point locked from controls only — method kept, detector RETIRED

> **[CORRECTION 2026-08-08] The detector is RETIRED; the method is not.**
> Everything in this section was executed as described — the rule was declared
> before any number was read, the calibration script opened only the two control
> slides, and worst-of-both was load-bearing. **But the detector answers a
> question the reference never asked.** Lin et al.'s "damaged alveolar area" is a
> contiguous region a human traced with an outline spline at low magnification;
> it is not a per-pixel AGER-density mask. So this operating point is no longer
> the endpoint denominator and should not be described as central to it. Read
> this section as a worked example of locking a parameter from controls only —
> which is what it is good for.

Selection rule, **declared before any number was read**:

* Uninfected lung has an intact AT1 sheet, so its damaged fraction *is* the
  detector's **false-positive rate**.
* **Constraint:** worst-of-both control slides ≤ α.
  Worst-of-both, not the mean — one clean slide must not buy tolerance for a
  dirty one.
* **Objective:** subject to that, the **largest** cutoff. Damaged% is monotone
  increasing in cutoff, so the largest admissible cutoff is the most sensitive
  operating point that still meets the specificity floor. Ties break toward the
  flattest local slope.
* The calibration script opens **only** `m4-2` and `m6`. The infected slides are
  never read, so the operating point cannot be tuned on the outcome.

Result (worst-of-both control damaged%, i.e. false-positive rate):

| α | AGER thr | σ (µm) | cutoff | het m4-2 | hom m6 | slope |
|---|---|---|---|---|---|---|
| 0.5% | 150 | 80 | 0.10 | 0.27 | 0.09 | 5.96 |
| **1%** | **150** | **40** | **0.14** | **0.93** | **0.18** | **11.57** |
| 2% | 150 | 20 | 0.18 | 1.71 | 0.22 | 13.66 |
| 5% | 150 | 10 | 0.35 | 4.95 | 0.64 | 22.56 |

Every α level independently selects **AGER threshold 150**, not the 200 used in
4b.

### Selected (α = 1%): `AGER_THRESHOLD=150`, `DAMAGE_SIGMA_UM=40`, `DAMAGE_CUTOFF=0.14` — *no longer the endpoint denominator*

Rationale for α = 1%: a clean specificity spec for "healthy lung reads as
undamaged", and σ = 40 µm is approximately one alveolar diameter — the
physiologically natural neighbourhood for local AT1 coverage. The α = 0.5% point
is more stable (slope 5.96) but needs σ = 80 µm, which averages across 2–3
alveoli and blurs the damaged/intact boundary.

These are now the Stage 1 defaults for σ and cutoff. The AGER threshold remains
**required** rather than defaulted, so that supplying it stays a deliberate act.

### The binding control

`het m4-2` carries roughly 5× the false-positive rate of `hom m6` at every α
(0.93 vs 0.18 at α = 1%). That traces to staining intensity: `m6` has brighter
AGER (in-tissue p50 369 vs 314), so at a fixed threshold it reads as more
intact. Using worst-of-both rather than the mean was therefore load-bearing —
the mean would have let `m6` mask `m4-2`.

### Still outstanding

The infected slides have **not** been measured at the locked parameters. When
they are, that is a genuine held-out readout, not part of the selection. It must
be reported as such.

Validation against hand-drawn outlines on a subset is still required — the
reference method is manual, so manual outlines are the only available ground
truth.

## 4d. The KRT5 numerator: co-negativity, not a bare threshold

> # ⚠ [CORRECTION 2026-08-08] THIS SECTION CONTAINS THE ERROR
>
> The sentence below beginning "Fig 2A–B quantifies" **mis-quotes the paper**,
> and every conclusion in this section that depends on co-*negativity* follows
> from that mis-quote. Verbatim, Lin et al. 2024 (J Clin Invest 134(19):e176828):
>
> * Fig 2A–B: *"quantification of percentages of **KRT5⁺PDPN⁺** areas in PDPN⁻
>   and KRT5⁺ areas"*
> * Fig 6E–G: *"percentages of dysplastic cell (**KRT5⁺ PDPN⁺**) areas in damaged
>   alveolar areas (PDPN⁻ and KRT5⁺)"*
> * Fig 2E–F: *"Immunofluorescence images of dysplastic cells (**KRT5⁺ PDPN⁺**)
>   and AT1 (PDPN⁺) cells"*
> * Methods: *"To quantify KRT5⁺ **or** PDPN⁻ area … measured using **outline
>   spline** in … Axiovision 4.8"*
>
> PDPN is expressed **by** basal/dysplastic cells as well as by AT1, so it does
> not discriminate them by absence. Requiring PDPN-negativity excluded the very
> population being measured. The stated rationale below — that co-negativity
> rejects autofluorescence — was **our inference, not the paper's method**, and
> it was plausible enough to survive several rounds of measurement.
>
> **What still stands in this section:** the demonstration that a bare KRT5
> threshold cannot reach a 1e-4 false-positive area on slide-scanner data (the
> false positives are large, bright, connected objects — an autofluorescence
> signature, not noise); and the RETRACTION of AGER as a co-negativity marker,
> which was correct for a reason independent of the sign error.
>
> **What does not:** the PDPN ceiling t = 200, whose two-sided derivation is a
> co-*negativity* argument with no transfer to co-*positivity*; and the framing
> of co-negativity as "what makes the measurement possible".
>
> Current spec: `config/endpoints/dysplastic_over_damaged.json`.

### What the reference actually measures

Lin et al. Fig 1G quantifies "percentages of KRT5+ lung areas in total damaged
alveolar areas (PDPN− and KRT5+)". Fig 2A–B quantifies "percentages of
**KRT5+PDPN−** areas in PDPN− and KRT5+ areas" (n = 15 mice/group, two-tailed
Mann-Whitney).  ← **mis-quote; see the correction box above**

So the numerator is **KRT5+ AND PDPN−**, not bare KRT5+ area. That co-negativity
constraint is not cosmetic — it is what makes the measurement possible on this
dataset.  ← **superseded**

### Why a bare KRT5 threshold fails here

Measured inside intact parenchyma of the uninfected controls (a compartment with
no pods and no airway basal cells, so any KRT5 signal there is background):

| KRT5 thr | bare KRT5+ false-positive area fraction |
|---|---|
| 200 | 1.20e-3 |
| 400 | 6.0e-4 |
| 500 | 4.5e-4 |

No threshold reaches 1e-4. The background maxes at 3194–3894 (near the 12-bit
ceiling of 4095), and the engine's 50 µm² component filter barely touches it
(4.51e-4 → 4.40e-4), which rules out scattered noise — the false positives are
large, bright, connected objects. That is the signature of autofluorescence,
consistent with the KRT5/488 channel being exposed ~949 ms against ~0.5–2 ms for
the other channels.

Worse, above threshold ~200 the damaged compartment of control slides shows the
*same or less* KRT5 than intact, and by 400 it reads zero: genuine airway basal
cell signal dies before the background does.

### Why co-negativity fixes it

Autofluorescent structures are bright in **every** channel. Genuine dysplastic
pods are KRT5-bright but PDPN-negative. Requiring PDPN− therefore rejects
autofluorescence while retaining real signal.

Enrichment ratio R = P(co-negative | KRT5 bright) / P(co-negative). R ≪ 1 means
KRT5-bright pixels are preferentially co-bright, i.e. the constraint is doing
real work; R ≈ 1 means it is only removing area indiscriminately.

| ceiling t | R_PDPN m4-2 | R_PDPN m6 | R_AGER m4-2 | R_AGER m6 |
|---|---|---|---|---|
| 100 | 0.232 | 0.139 | 0.620 | 0.000 |
| 150 | 0.405 | 0.319 | 0.806 | 2.160 |
| 200 | 0.620 | 0.548 | 0.987 | 0.386 |
| 300 | 0.833 | 0.895 | 1.049 | 0.463 |

### RETRACTED: AGER must NOT be used for co-negativity

An earlier analysis in this session reported that AGER− co-negativity performed
*better* than PDPN−. **That was an artifact and is withdrawn.** "Intact" is
*defined* as AGER-dense territory, so requiring AGER < t removes area by
construction. The enrichment ratio proves it: R_AGER ≈ 1.0 (0.987, 1.049) on
m4-2 and erratic on m6, versus R_PDPN = 0.14–0.62 consistently below 1.

AGER co-negativity also destroys real signal — it discards **100%** of genuine
KRT5+ airway pixels in m6 at every ceiling tested.

Use **PDPN** for co-negativity. AGER stays as the *denominator* marker only.

### The ceiling cannot be set by minimising false positives

That objective is monotone: lower t always looks better, driving t → 0, which
destroys real signal along with background. The controls do supply a positive
control — conducting airway in the damaged compartment is genuinely KRT5+PDPN−:

| ceiling t | airway KRT5 preserved (PDPN) |
|---|---|
| 100 | 0.150 / 0.286 |
| 150 | 0.450 / 1.000 |
| **200** | **0.900 / 1.000** |
| 300 | 1.000 / 1.000 |

At t = 100 — the best-looking value on specificity alone — PDPN− discards
72–85% of genuine KRT5.

**Two-sided rule, still control-only:** maximise specificity subject to
preserving ≥ 90% of genuine airway KRT5.

### PDPN co-negativity ceiling t = 200 — proposed then, RETIRED now

R = 0.55–0.62 (real enrichment) while preserving 90–100% of genuine airway KRT5.

**Not locked, because two checks are outstanding:**

1. Everything above is measured at 2.76 µm/px. Partial-volume mixing at
   pod/AT1 boundaries is worst at that scale, so the constraint may reject real
   pods more than these numbers suggest. It must be re-measured at full tile
   resolution (0.345 µm/px) before freezing.
2. m6's airway positive control is only ~11.8k px, and its KRT5-bright subset is
   a handful of pixels (0.286 ≈ 2/7). It is statistically unusable. The
   recommendation rests on m4-2 alone.

### Parameter count versus control count

Five parameters are now derived from two control animals: AGER threshold, damage
sigma, damage cutoff, KRT5 threshold, and the PDPN ceiling. That is a real
overfitting risk and belongs in any methods section as a stated limitation, not
buried. Additional control animals would do more for confidence here than any
further tuning.

## 5. Stage 1 ROI naming

Fixed. Stage 1 previously named every tile ROI `alveolar_core`, which asserts the
tile contains no conducting airway — mislabelling pure-airway tiles. The default
is now the neutral `parenchyma_core` (matching no compartment keyword), and the
alveolar claim is opt-in via `IFQ_WSI_ROI_COMPARTMENT=alveolar`, to be set only
once airways are actually excluded.

Consequence of the neutral default: AGER and T1A declare
`expectedCompartment:"alveolar"`, so their calls degrade to
`context_unresolved` / `indeterminate`. The KRT5 pod endpoint is unaffected.

---

## 6. Mandatory companions

1. ~~Freeze `IFQ_KRT5_THRESHOLD` from blinded controls first. Nothing about
   ectopic pods is interpretable before this.~~ **DONE 2026-08-08** on confocal
   data: `IFQ_KRT5_THRESHOLD = 300`, from the two uninfected controls, recorded
   as `fixed_predeclared`. Caveat: one of the two controls (M6 LEFT) is a
   staining failure, so this rests on M4-2 alone and must be re-derived when a
   second sound control exists.
2. `IFQ_MIN_INCLUDED_NUCLEI=0` — a small airway region with zero accepted nuclei
   throws inside the region loop and kills the whole tile, alveolar row included.
3. Fix the `alveolar_core` mis-tag above.
4. Pre-register where the airway annotation boundary falls relative to a
   peribronchiolar pod. It materially changes the endpoint. A dilation
   sensitivity analysis (0 / +10 / +25 µm) bounds it honestly, but the primary
   rule must be fixed before unblinding.

---

## 7. Open questions

* Whether AGER and PDPN survive in the injured/consolidated regions of *this*
  dataset. This decides whether any marker-based automation is safe at all.
  Check positive-area fraction in visibly injured versus spared parenchyma on one
  slide.
* No repo-derived size/shape distribution exists for KRT5 components in mouse
  lung sections, so no size/shape pod filter can be tuned yet.
* Airway profile count per section on these slides — not measured.
* The two-ROI RoiSet round trip through `RoiEncoder`/`readRoiFile`, and the
  engine's non-overlap check, are code readings and must be smoke-tested on one
  tile before any batch.
* Stage 2 runtime under a two-region partition: `segmentNuclei` re-thresholds and
  re-watersheds the full tile per region, so tiles containing airway roughly
  double in cost. Measure on the 6-tile pilot.

---

## Morphometry cross-check — partial support, and one actionable finding

Run 2026-08-08 entirely from the channel cache while D: was offline.
`scripts/morphometry_crosscheck.groovy`. Ratios are damaged / intact.

| slide | condition | damaged mm2 | solid | MLI | boundary |
|---|---|---|---|---|---|
| het m4-1 | PR8 | 3.92 | **0.851** | 1.027 | **0.907** |
| hom m2 | PR8 | 3.28 | **0.576** | 0.963 | **0.701** |
| het m4-2 | uninfected | 0.51 | 1.132 | 0.506 | 1.090 |
| hom m6 | uninfected | 0.09 | 0.895 | 0.317 | 1.185 |

### Supports the denominator, partially

In **infected** slides the damaged compartment has materially less solid tissue
(0.85, 0.58) and lower boundary density (0.91, 0.70) than the intact
compartment. That is consistent with real tissue destruction, and it is measured
from DAPI only — AGER defines the compartments but never measures them, so the
comparison is not circular.

### The controls point somewhere useful

In **uninfected** slides the pattern inverts: the tiny "damaged" areas have MORE
solid tissue (1.13, 0.90), HIGHER boundary density (1.09, 1.19) and much SMALLER
MLI (0.51, 0.32). Dense, nucleus-rich structures with small lumens.

That is what a conducting airway or a vessel looks like — not injured
parenchyma. So the residual 0.93% / 0.18% control false-positive rate is
probably **not** threshold noise; it is anatomy the detector cannot exclude.
This is independent evidence that airway exclusion is the missing piece, and it
suggests the false-positive rate would largely disappear once airways are
annotated out rather than needing a stricter threshold.

### What this run does NOT establish

- **MLI as implemented is not the classical quantity.** Intact MLI is ~17 um on
  every slide, whereas mouse alveolar chord lengths are typically 40-80 um. DAPI
  marks nuclei, not septal walls, so chords terminate at every nucleus and this
  measures inter-nuclear spacing. Directionally usable, absolutely not
  publishable. A tissue-marking channel independent of AGER would fix it; none
  exists in this panel.
- **MLI shows no damaged/intact difference in infected slides** (1.03, 0.96).
  Given the above, that is more likely a limitation of the proxy than evidence
  of no architectural change.
- n = 1 per group. These are directional observations, not a result.

---

## 9. KNOWN BUG: partition QC columns do not reach mouse level

Found 2026-08-07 by a design audit, then confirmed empirically.

`aggregate_to_mouse.classify_columns()` builds `sum_cols` as a **closed whitelist
of name suffixes** (`aggregate_to_mouse.py:184-186`) and `aggregate_mice()`
writes `rec[]` only from that set. **Every column outside the whitelist is
silently discarded** — no error, no warning, it simply is not in the output
header.

Verified by running `aggregate_to_mouse.py` on the real partitioned slide
summary:

| column | in slide summary | at mouse level |
|---|---|---|
| `damaged_area_um2` | present | **DROPPED** |
| `intact_area_um2` | present | **DROPPED** |
| `damaged_fraction_of_parenchyma` | present | **DROPPED** |
| `KRT5_pod_area_um2_in_intact` | present | **DROPPED** |
| `KRT5_pod_area_frac_of_intact` | present | **DROPPED** |
| `KRT5_pod_area_um2` | present | survives |

### The primary endpoint is NOT affected

`region_area_um2` carries the damaged area when partitioned, and
`<M>_pod_area_um2` matches the whitelist, so pod-area-over-damaged-area survives
and its fraction is recomputed from pooled numerators correctly.

### What is lost

Per-mouse QC. "% of lung damaged" never becomes a mouse-level endpoint, and the
KRT5-in-intact tripwire — the check that would catch a bad damage mask or a bad
KRT5 threshold — stops at slide level and never reaches the group table. That is
exactly the readout most worth having per animal.

### Why the obvious fix does not work

Renaming the columns to match the whitelist is not enough, because every
recomputed area fraction in `aggregate_to_mouse` divides by
`sum(region_area_um2)`. There is only ONE denominator. And `KEY_COLS`
(`aggregate_to_mouse.py:43`) is `[mouse_id, genotype, condition, panel]` — it
does **not** include `region`, so writing damaged and intact as separate `region`
values does not separate them either; they are summed into one mouse row.

### Proposed fix, not yet applied

Emit one row per denominator **scope** rather than one row per slide, using the
only free grouping key: `panel = "<PANEL>@<scope>"`, e.g. `LEFT@damaged` and
`LEFT@parenchyma`. Each row then carries its own `region_area_um2`, and every
fraction is recomputed against the correct denominator, with
`aggregate_to_mouse.py` completely unchanged.

This changes the shape of `slide_level_summary.csv`, so it is a deliberate
decision rather than a patch, and is left for review.

## 8. What this pilot can and cannot claim

n = 1 mouse per group cell (het/hom × infected/uninfected). With n = mice as the
statistical unit, this dataset supports pipeline validation and threshold
freezing. It does not support a group comparison.

> **[CORRECTION 2026-08-25]** The earlier correction used “confounded”
> incorrectly. Genotype and condition form a complete crossed 2 × 2, but each
> cell contains one mouse, leaving no replication-based residual error. Each
> apparent factor contrast is also an individual-animal contrast. Genotype,
> condition, and interaction inference are unavailable; any four-mouse table is
> descriptive.

Separately: `het` is likely the control rather than a second genotype arm — a
heterozygous *Ifng*⁺/⁻ often retains enough cytokine to signal normally. Confirm
the line before interpreting any pod difference.

A global IFN-γ ligand knockout can also alter viral clearance, so a pod
difference may be an infection-severity difference. NP staining or NP qPCR is
needed to exclude this; no image analysis can substitute for it.


## 10. Whole-field runs: where the region comes from

> **[CORRECTION 2026-08-08] Mechanically validated; wrong endpoint; wrong
> denominator.** Everything in this section about *plumbing* was executed and
> holds: the region-source modes, the reconciliation to a TIFF resolution-tag
> rounding constant, the containment check, the three executed failure guards,
> and the aggregation survival check. Read it as validation of the mask-algebra
> module.
>
> What it is **not** is a measurement of the endpoint. Three things to hold in
> mind while reading the numbers below:
>
> 1. **Wrong sign.** `endpoint.log` records
>    `endpoint : ectopic_pod_over_damaged`, numerator
>    `KRT5_pod_mask AND NOT T1A_membrane_positive_mask` — the superseded spec.
> 2. **Wrong denominator in this historical run.** The evaluator used at the
>    time divided by `region_area_um2`, i.e. **total analysed tissue**, not damaged
>    area. The 2026-08-09 evaluator now reads the declarative union denominator,
>    but that does not retroactively change these files. The "% " figures in the
>    table below are therefore *fraction of tissue*, not the endpoint.
> 3. **Uncalibrated ceiling.** The section says this itself: T1A ran adaptive
>    Otsu, so the ~50% that co-negativity removes is the size of an uncalibrated
>    parameter's effect, not a measurement.
>
> Also note the source run: `D:\IFQ_Runs\confocal_260808`, i.e. **before** the
> `blackBackground` fix. That does not invalidate these numbers — they are
> area-only, and the largest area-fraction change across all 79 fields between
> the buggy and fixed runs was 0.021 pp — but any count from that run is wrong.

`endpoints/evaluate_endpoints.groovy` must clip the relational numerator to the
same region the engine measured in. For a tiled whole-slide run that region is
the per-tile `<stem>_RoiSet.zip`. Whole-field confocal acquisitions have no
RoiSet, and the guard that made a missing RoiSet fatal is deliberate: an earlier
bug fell back silently to the unclipped field and corrupted the result.

The region source is therefore now chosen **by name**, never by fallback:

| `IFQ_ENDPOINT_REGION_MODE` | region | missing input |
|---|---|---|
| `roiset` (default) | `IFQ_TILES_DIR/<stem>_RoiSet.zip` | FATAL |
| `tissue_mask` | `IFQ_TISSUE_MASK_DIR/<output_key>/*__<region>__tissue_region_mask.tif` | FATAL |
| `whole_field` | none - no clipping at all | n/a, but see below |

`roiset` is unchanged, so existing whole-slide behaviour is untouched.

### The engine does not export a tissue mask

This was checked against the 260808 confocal output rather than assumed. Each
per-field folder contains whole-field marker masks
(`<sig>__KRT5_pod_mask.tif`, `<sig>__T1A_membrane_positive_mask.tif`, ...) and
per-region **nuclei** masks (`<sig>__tissue__nuclei_mask.tif`,
`<sig>__tissue__DAPI_candidate_mask.tif`, ...). Nothing in that folder has the
tissue *region* as its foreground. In `IF_Quant_Pipeline.groovy` the auto-DAPI
region exists only as an in-memory `ShapeRoi` and reaches disk solely as the
scalar `region_area_um2` in `run_summary.csv`.

So `tissue_mask` mode is fed by a companion script,
`endpoints/export_tissue_region_masks.groovy`, which re-derives the region from
the same source pixels with the same constants
(`TISSUE_BLUR_SIGMA_PX=4.0`, `Triangle`, `TISSUE_MIN_AREA_UM2=2000`, binary
close `iterations=2 count=1`, particles merged with `ShapeRoi.or`) and then
**proves** it by reconciling the mask's calibrated area against the engine's own
`region_area_um2`, field by field. It refuses to bless the export if any field
misses the tolerance, and it refuses outright for any run whose
`tissue_roi_source` is not `auto_dapi`. It writes outside the analysis
directory and never touches the source image tree.

### Why clipping is not optional here

On this batch the unclipped whole-field T1alpha area is up to about 4x the
region-clipped area the engine reported (e.g. field `M4-1_PR8_LEFT_04_G001_0001`:
48592 um2 whole-field vs 8843 um2 in tissue). Running the endpoint in
`whole_field` mode on a run whose regions were auto-DAPI would not be a
conservative approximation - it would be a different measurement over a
different denominator. `whole_field` mode therefore verifies its own premise:
the region area it uses must reconcile with `region_area_um2`, which can only
happen if the engine really did analyse the whole field.

### Output column survives mouse-level aggregation

`spec.output.area_column` is `KRT5ectopic_pod_area_um2`. It ends in
`_pod_area_um2` and not `_mean_pod_area_um2`, so
`aggregate_to_mouse.classify_columns()` puts it in `pod_area` and hence in
`sum_cols` (`aggregate_to_mouse.py:168-186`), and `aggregate_mice()` emits
`KRT5ectopic_pod_area_um2_total` and `KRT5ectopic_pod_area_frac`
(`aggregate_to_mouse.py:325-332`). It is not dropped by the section 9 bug.

The QC columns the endpoint script now also writes are prefixed `qc_` precisely
so that they fall outside every whitelist suffix and cannot be mistaken for
endpoints if the CSV is joined into a slide summary.

### Measured on the 260808 confocal batch

Run: `scripts/run_endpoint_confocal_260808.ps1`, both stages exit 0.
Output: `D:\IFQ_Runs\confocal_260808\endpoint_areas.csv`, 39 LEFT-panel fields
(40 RIGHT-panel rows skipped by `spec.panel`).

Reconstruction fidelity. All 39 tissue masks reproduce the engine's
`region_area_um2` with relative difference **exactly 0** as measured by
ImageStatistics inside the exporter. Downstream, `evaluate_endpoints.groovy`
computes the region area from pixel counts times the calibration stored in the
mask TIFF, and there the worst relative discrepancy is **3.285148e-07**. That
number is not a geometry difference: it is the ratio between the TIFF resolution
tag (`XResolution = 3218121/1000000`, i.e. 0.31074033574250315 um) and the true
double from the `.oir` (0.310740284701119 um). The ratio is the same constant for
all 39 region areas and for all 18 non-zero KRT5 areas, so the reconstructed
pixel sets are identical to the engine's, not merely close.

Containment. `KRT5+ AND NOT T1A+` area <= bare `KRT5+` area held for 39/39
fields against both this script's own in-region KRT5 area and, independently,
against the engine's `KRT5_pod_area_um2`. A separate numpy re-implementation of
the same boolean algebra agreed with the Groovy output to 3.285e-07, i.e. to the
same calibration constant.

What co-negativity removes, at mouse level (pooled sums, LEFT panel):

| mouse | condition | tissue um2 | KRT5+ | KRT5+T1A- | removed |
|---|---|---|---|---|---|
| M2   | PR8        | 1 324 706 | 14.11388 % | **7.20667 %** | 6.907 pp (48.9 % rel) |
| M4-1 | PR8        | 1 465 188 | 11.97849 % | **6.09052 %** | 5.888 pp (49.2 % rel) |
| M4-2 | uninfected |   249 836 |  0.00000 % | **0.00000 %** | - |
| M6   | uninfected |   665 754 |  0.00347 % | **0.00322 %** | 0.00025 pp (7.1 % rel) |

The T1alpha co-negativity requirement removes roughly **half** the KRT5+ area in
both infected animals (per field: min 7.1 %, median 49.1 %, max 78.2 % over the
18 fields with any KRT5+ signal; it removed nothing in 0 fields). The
infected-vs-uninfected separation survives: about 2200-fold and 1900-fold
against M6, and against M4-2 the control numerator is still exactly zero.

Interpretation limits are unchanged and still binding. `IFQ_T1A_THRESHOLD` was
never locked - this run used the engine's ADAPTIVE per-region Otsu for T1alpha
(`T1A_area_threshold_source` is exploratory), so the ~50 % that co-negativity
removes is a number produced by an uncalibrated ceiling. It is the size of the
effect the ceiling has, not a validated measurement of ectopic pod area. Locking
T1alpha from controls at full resolution remains outstanding (section 4d).

Aggregation, verified rather than reasoned. Merging
`KRT5ectopic_pod_area_um2` into the 39 LEFT rows and running
`aggregate_to_mouse.py` produced `KRT5ectopic_pod_area_um2_total` and
`KRT5ectopic_pod_area_frac` in `mouse_level_summary.csv`, matching the table
above. The column survives.

One cosmetic side effect: the aggregator infers a marker name from the suffix, so
it also emits `KRT5ectopic_n_pods_total` and `KRT5ectopic_mean_pod_area_um2`,
both 0, because there is no `KRT5ectopic_n_pods` input. They are meaningless
placeholders, not measurements - a component count for a relational mask would
have to be defined before it could be reported.

### Guards, verified by executing them

Each failure path was run, not just written:

| scenario | result |
|---|---|
| `roiset` with no RoiSet present | FATAL, exit non-zero, no CSV - original guard intact |
| `tissue_mask` with no mask exported | FATAL, no CSV, message names the exporter to run |
| `whole_field` on this auto-DAPI run | FATAL, no CSV, region-area discrepancy 7.426259e-01 |

The `whole_field` result is the point: on a run whose regions were auto-DAPI, the
whole field is 74 % larger than the region the engine measured, and the mode
refuses rather than reporting areas over the wrong denominator.

One gap deliberately left alone. In `roiset` mode, if the RoiSet exists but
contains no ROI whose name matches the `region` value, that (image, region) is
still measured unclipped - it now logs a WARNING but does not fail. That is
pre-existing behaviour on the validated whole-slide path and changing it needs a
look at how Stage 1 names partition ROIs, so it was not changed here.
