# H&E brightfield feature roadmap

> **Status: R1 approved / H7-H8 engineering ready / biological review
> required.** Image QC and the usable-tissue denominator are approved for the
> current cohort through H3. H4 context and the locked H7 whole-section rubric
> are development inputs. Strict ordinal review parsing, schema-v2 section
> records, technical-section agreement and non-composite mouse summaries are
> implemented, but publish nothing until all eight biological review rows are
> accepted. H5-H6 quantitative measurement remains unavailable and Route 3
> remains disabled.

The machine-readable source of truth is
`config/brightfield/he_feature_roadmap.json`. The existing decision hierarchy
defines scientific order; this roadmap defines software work, dependencies,
outputs and acceptance evidence.

## Evidence-driven decisions

1. **Keep H&E separate from fluorescence.** Brightfield structures are darker
   than their background and require stain separation. QuPath uses color
   deconvolution for H&E, but warns that separated intensities generally should
   not be treated as direct quantitative stain measurements.
2. **Freeze one reviewed batch stain profile.** Set background and H/E vectors
   before measurement and apply the same representative profile across the
   similarly stained batch. Never estimate by condition during confirmation.
3. **Use classifiers for candidates, anatomy and exclusions—not autonomous
   pathology.** Train with small, diverse, approximately balanced annotations
   from multiple images and a limited feature set. Compartment and topology
   must authorize local color/texture candidates.
4. **Treat reference space and sampling as data.** ATS/ERS standards require
   objective sampling and the correct reference space, and warn against deriving
   3D quantities from ordinary 2D profiles. Airspace area stays a 2D profile
   fraction; alveolar number and volume remain deferred.
5. **Model influenza-relevant lesions.** Mouse influenza literature uses
   blinded, compartment-specific assessment of epithelial/interstitial
   necrosis, peribronchial, perivascular and interstitial inflammation, edema,
   bronchiolitis, alveolitis and interstitial pneumonitis. These become explicit
   masks or ordinal rubrics, without immune-lineage claims.
6. **Keep mouse as the experimental unit.** BF_01 and BF_02 are technical
   sections. The current four-mouse cohort supports development and descriptive
   paired-section QC only, not genotype-by-infection inference.

## Release ladder

| release | capability | current state | maximum permitted interpretation |
|---|---|---|---|
| R0 | engineering preflight | available; superseded by R1 | identity and coarse H3 candidates |
| R1 | production image QC | **approved/current for this cohort** | reviewed usable-tissue denominator; no lesion endpoint |
| R2 | reviewer-gated section analysis | development; review required | section quantitative + ordinal results |
| R3 | confirmatory mouse reporting | unavailable | prespecified mouse endpoints in an adequately powered cohort |
| R4 | multimodal G-SURF association | unavailable | mouse-level H&E-to-IF association |

The launcher must display the release level and its allowed claim. Enabling a
route is not equivalent to authorizing every downstream result.

## Ordered implementation backlog

### P0 — denominator, review and reporting integrity

| feature | H stage | build | release evidence |
|---|---|---|---|
| HE-F01 deterministic QuPath project/inventory | H0-H1 | persistent project, exact declared-series mapping, inventory and preflight failures | exactly 4 mice/8 sections; reject missing, extra or renamed series |
| HE-F02 frozen batch stain profile | H2 | reviewed background + H/E vectors, separation contact sheet, named hashed JSON | reviewer sign-off across all sections; hash consumed by runner |
| HE-F03 artifact masks | H2-H3 | distinct pen, dust, saturation, fold, tear, bubble and blur classes | per-class overlay and denominator reconciliation; locked audit |
| HE-F04 usable-tissue mask | H3 | saved QuPath thresholder or compact classifier with Tissue/Glass/Ignore* | mouse/slide-separated audit; fail on denominator or profile drift |
| HE-F09 blinded review + rubric | H7 | **locked whole-section rubric, blinded package, strict parser and audit-last publication implemented**; geometry-edit workflow remains for mask endpoints | complete accepted review, repeat agreement, immutable edit provenance |
| HE-F11 mouse aggregation | H8 | **ordinal completeness gate and non-composite paired-section mouse summary implemented**; quantitative raw-component pooling remains future work | fail closed on missing, duplicate, ineligible or vocabulary-drifted sections |
| HE-F14 validation/provenance | all | exact raw/R1/H4 hashes plus **H7/H8 input/output audit implemented**; classifier partitions remain future work | no mouse leakage between development and locked validation |

### P1 — pathology candidates and operator workflow

| feature | H stage | build | release evidence |
|---|---|---|---|
| HE-F05 anatomy | H4 | airway, vessel, alveolar, pleural and Unresolved objects; reviewed boundary lengths | compartment-stratified audit and calibrated units |
| HE-F06 nuclei/cellularity | H5 | hematoxylin or OD-sum detection inside usable tissue | blinded point-count bias/precision by compartment |
| HE-F07 PR8 injury candidates | H5 | multi-scale interstitial/alveolar inflammation, consolidation, cuff and epithelial-injury candidates | locked per-class holdout; no group label as a feature |
| HE-F08 topology authorization | H6 | boundary-contact and containment rules; Indeterminate path | decision log plus synthetic geometry and WSI audit |
| HE-F10 technical repeatability | H8 | **ordinal paired values, range and exact agreement implemented**; quantitative BF_01/BF_02 differences remain future work | larger development-cohort repeatability limits |
| HE-F13 launcher Route 3 | H0-H8 | H&E screen, stain-profile and tier selectors, review/resume | fail-closed self-tests for every dependency |

### P2 — multimodal integration

HE-F12 joins H&E with KRT5-pod and AT1/AT2 IF endpoints only at mouse level.
Region/cell transfer is separate future work requiring validated serial-section
registration and an error tolerance tied to the intended biological claim.

## Endpoint-to-feature map

| endpoint | required features | authorization |
|---|---|---|
| usable tissue fraction | F02-F04 | artifact review |
| consolidated/dense-lesion fraction | F04, F05, F07-F09 | reviewed lesion mask |
| hematoxylin-positive nuclear density | F02, F04-F06, F09 | point-count-audited detection; cellularity only |
| inflammatory-hotspot fraction | F04-F09 | reviewed compartment-aware hotspot mask |
| airspace profile-area fraction | F04, F05, F09 | alveolar reference area; 2D claim only |
| peribronchial cuff burden | F05, F07-F09 | reviewed airway boundary and cuff |
| perivascular cuff burden | F05, F07-F09 | reviewed vascular boundary and cuff |
| blinded ordinal scores | F05, F09 | locked rubric and blinded reviewer |
| mouse-level endpoint | F10-F11 | complete reviewed section set |
| H&E-to-IF association | F11-F12 | adequately powered mouse cohort |

## Validation and launcher gates

- Split by **mouse/slide**, never by patch, so one mouse cannot enter both
  development and locked validation.
- Audit normal alveoli, airway, vessel, consolidated tissue, edge, fragment and
  every artifact type.
- Report segmentation by class/section, nuclear count bias and precision by
  compartment, lesion performance by class, reviewer agreement, technical
  repeatability and all failures.
- Lock numerical pass thresholds from a larger development cohort. Do not
  invent universal Dice, ICC or disagreement cutoffs from the current n=4.
- Treat QuPath pseudo-probabilities as ranking aids, not calibrated probability.

```text
R0 current pilot
  -> F01 + F02 + F03 + F04 + F14 pass => R1 engineering route
  -> F05 + F06 + F07 + F08 + F09 + F10 pass => R2 reviewed section route
  -> F11 + adequate cohort + prespecified analysis pass => R3 mouse reporting
  -> F12 pass => R4 multimodal reporting
```

`BrightfieldRouteEnabled=true` is never sufficient by itself. The launcher must
read validation evidence and expose only the highest satisfied release. Route 3
must never invoke the fluorescence Fiji engine.

## Intentionally unavailable claims

- Immune subtype or lineage from H&E alone.
- Quantitative stain concentration from color-deconvolution intensity.
- Alveolar number or volume from ordinary 2D profiles.
- Genotype, infection or interaction statistics from the current n=1-per-cell
  study.
- Fully automated pathology grade without labelled development data, blinded
  review and independent validation.

## Primary and authoritative sources

- [ATS/ERS standards for quantitative lung structure](https://pmc.ncbi.nlm.nih.gov/articles/PMC5455840/)
- [QuPath 0.7: separating stains](https://qupath.readthedocs.io/en/stable/docs/tutorials/separating_stains.html)
- [QuPath 0.7: pixel classification](https://qupath.readthedocs.io/en/stable/docs/tutorials/pixel_classification.html)
- [QuPath 0.7: detecting tissue](https://qupath.readthedocs.io/en/stable/docs/tutorials/thresholding.html)
- [QuPath 0.7: cell detection](https://qupath.readthedocs.io/en/stable/docs/tutorials/cell_detection.html)
- [QuPath 0.7: exporting measurements](https://qupath.readthedocs.io/en/stable/docs/tutorials/exporting_measurements.html)
- [Influenza pathology with impaired IFN-gamma signaling](https://pmc.ncbi.nlm.nih.gov/articles/PMC6286381/)
- [PR8 lung injury and AT1 depletion](https://pmc.ncbi.nlm.nih.gov/articles/PMC3627938/)
- [Comparative pathology of influenza animal models](https://pmc.ncbi.nlm.nih.gov/articles/PMC10820042/)
- [ARRIVE 2.0 guidelines](https://arriveguidelines.org/arrive-guidelines)
