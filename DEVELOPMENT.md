# Development and AI assistance

Claude Code was used extensively for implementation, debugging, testing, and
documentation. **Scientific questions, experimental interpretation, analysis
specifications, acceptance and rejection criteria, validation strategy, and final
interpretation were directed and reviewed by me.**

This is visible in the git history — many commits carry `Co-Authored-By: Claude`
— and it is not concealed. The AI could implement and calculate; it was not the
authority on biological identity, experimental validity, or whether a result
warranted belief.

| Responsibility | Primary authority |
|---|---|
| Biological question and experimental interpretation | **Me** |
| Statistical unit and which comparisons are valid | **Me** |
| Acceptance / rejection criteria | **Me** |
| Endpoint definition and interpretation | **Me** |
| Implementation, refactoring, diagnostic code | AI-assisted |
| Documentation and diagrams | AI-assisted |
| Verification strategy | Author-directed, computationally executed |
| Final retain / reject decision | **Me** |

---

## Five decisions that shaped the result

### 1 · The mouse is the statistical unit, not fields or cells

The pipeline reports ~20,000 cells from four animals. Treating those as the
sample size would inflate confidence by roughly √(cells per mouse) and
manufacture significance from a single animal.

I specified the animal as the unit. `aggregate_to_mouse.py` enforces the roll-up
and prints the distinct-animal count, and the constraint is restated wherever a
number is reported.

### 2 · Thresholds locked from uninfected controls, before test data was evaluated

A cutoff chosen to separate the groups being compared is not a measurement of
those groups. I required intensity cutoffs to be re-derived from the uninfected
controls only, then frozen before infected animals were measured.

`IFQ_KRT5_THRESHOLD = 300` comes from the two controls alone (in-tissue
p99.99 = 283 and 255), giving control false-positive area ≤ 1e-4 in each
independently. Infected tissue then measured 8.1 % of area above 500.

I also accepted the cost: AGER and T1α are expressed in healthy lung, so no such
anchor exists for them. Those cutoffs remain adaptive and everything derived from
them is labelled `adaptive_otsu_exploratory` rather than reported as a result.

### 3 · AGER and KRT8 rejected when the data failed the pre-specified criterion

Both were tested with a control-locked enrichment ratio (R = infected fraction ÷
control fraction beyond the same cut), and both failed:

| Marker | R | Outcome |
|---|---|---|
| AGER as a co-negativity marker | 0.99–1.05 | Retracted — the exclusion was definitional, not biological |
| KRT8 as a discriminator | 0.80–1.25 at every cut | Rejected — infected animals *bracket* the controls |

The AGER retraction reversed a marker I had chosen earlier. I kept both on the
record rather than deleting them, so neither gets re-derived.

→ [`docs/NEGATIVE_RESULTS.md`](docs/NEGATIVE_RESULTS.md)

### 4 · No genotype-level inference from a four-mouse design

Genotype and condition are crossed in the terminal imaging cohort, but one
day-28 imaged survivor per cell leaves no replication-based residual error.
This is not an enrollment count. The difference between the two infected
animals therefore cannot be separated from the difference between those two
individual animals, and no genotype, condition, or interaction inference is
supported.

I decided this is prohibitive rather than a caveat to soften. The measurement
behaves as expected and the infected/uninfected contrast is near-binary, but no
statistical comparison of groups is supportable from this batch.

### 5 · Endpoint corrected against the primary source

The implementation computed KRT5⁺PDPN⁻. The reference specifies

> "percentages of **KRT5⁺PDPN⁺** areas in PDPN⁻ and KRT5⁺ areas"
> — Lin et al. 2024, *J Clin Invest* 134(19):e176828, Fig 2A–B

PDPN is expressed by dysplastic cells as well as AT1, so requiring
PDPN-negativity had been excluding the population being measured. The denominator
is also a hand-traced region union, not a computed density map — which retired a
detector that had been solving a problem the reference does not pose.

I required the evaluator to **refuse** the corrected specification rather than
divide by a denominator it cannot construct and exit successfully.

---

## Further examples

**Cell-type identity.** The 488 channel showed a bright, continuous band lining
the airway. I identified it as basal/club lineages rather than AT2, and specified
that genuine AT2 have circular perinuclear-granular morphology rather than a ring.
That led to a measurement settling it: ProSPC intensity is p50 = 430 inside AT2
against 385 in the airway — nearly identical, so no intensity threshold separates
them and the difference is extent, not brightness.

**Bench-to-computation feedback overriding automated QC.** A staining failure in
one animal (M6) was identified at the bench by my PI. No in-image statistic had
flagged it; it was confirmed afterwards in the data (AGER `frac>500` = 0.0097 in
the LEFT section against 0.289 in the RIGHT, same antibody, same animal). The
consequence is carried forward: the KRT5 cutoff in practice rests on one clean
control, not two.

**Refusing an analysis that would have looked better.** A rolling-ball background
subtraction was proposed to remove the non-specific airway population. I rejected
it because the filter radius was being chosen for making the unwanted content
disappear — a repeat of an error already retired from the codebase. A merge panel
may not delete image content; suppression belongs to the QC overlay, where it is
visibly an analysis decision.

---

## AI-produced work was not accepted automatically

Consequential examples:

- **A single-field statistic quoted as a batch result** — 185 → 16,422 (89×)
  came from one field and was presented as the batch figure. The pooled value is
  152.5 → 15,393.3 (~101×). Corrected everywhere, with the estimator now named.
- **An image-manipulation proposal** — background subtraction tuned to remove a
  population (above), and before it a connected-component gate that deleted the
  airway from a panel presented as a micrograph. Both retired; a configuration
  still requesting the gate now fails.
- **Over-aggressive display processing** — marker channels erased twice by floors
  derived from pooled statistics and applied per image, where each section's
  ceiling is lower.
- **Implementation defects exposed by validation** — outputs named from bare
  filenames silently overwrote 8 of 80 panels while the log reported 80; a
  `>127` mask test rendered nothing for label images with fewer than 128 objects.

---

## How work was verified

| Method | Applied to |
|---|---|
| Execution-based equivalence | Launcher legacy mode — 84 checks against what a *real child process* receives |
| Mutation testing | 28 mutants of the launcher's decision logic; 26 killed, 2 intentional controls |
| Replay to bit-identity | The segmentation defect, reproduced at **IoU = 1.0000** |
| Synthetic fixture | Same defect, demonstrable from a clone with no data |
| Regression measurement | Area outputs after the engine fix — *measured* unchanged (worst 0.0209 pp), not assumed |
| Held-out / control-locked validation | Thresholds derived on controls, applied to infected animals not used in derivation |
| Independent re-running | Agent-produced results re-executed before being retained |

Two are executable by a reader from a clean clone:

```bash
powershell -ExecutionPolicy Bypass -File ./validation/run_demo.ps1
powershell -ExecutionPolicy Bypass -File ./launcher/run_legacy_equivalence.ps1
```

---

## Licence

[MIT](LICENSE), covering the software and documentation. Deliberately scoped:
it does not extend to microscopy image data (none is committed), to the
third-party tools this pipeline invokes, or to unpublished scientific findings —
reuse of the code is granted, reuse of the results follows normal academic
citation.

---

AI use in this repository is visible and documented. The question I would want a
reviewer to ask is not whether an AI coding agent was used, but whether
scientific control and accountability were retained while using one — and the
specifications, rejections, retractions, and preserved negative results above are
the evidence I would offer.
