# IFQuant-Platform successor handoff

> **Status: REFERENCE — successor-repository decision recorded 2026-08-29.**
> Clean-slate architecture work is now a separate project named
> `IFQuant-Platform`. Repository scaffolding and implementation are assigned to
> a new development session. Until a repository URL is recorded here, this file
> is the authoritative handoff decision, not evidence that successor code has
> already been published.

## Decision

Future QuPath-first, cell-centric, and machine-learning trials will be developed
in **IFQuant-Platform**, not on a long-lived branch of IFQuant-Lung.

IFQuant-Lung remains the reproducible record of the completed G-SURF study. Its
settled releases, authority records, historical algorithms, and negative results
must not be reinterpreted by successor development. The prospective Contract A
work in this repository remains a tested architectural bridge and source
reference; it does not turn new experiments into G-SURF results.

## Why a separate repository

The successor has a different purpose and lifecycle:

- G-SURF is complete, whereas the successor begins exploratory method and model
  development from zero.
- The current primary fluorescence endpoint is an area-mask measurement. The
  successor is expected to introduce cell/nucleus objects, morphology,
  compartment intensity, neighbourhood features, reviewed annotations, and
  versioned model predictions.
- QuPath becomes the primary image, annotation, detection, and review
  environment. Narrow Groovy scripts execute deterministic QuPath operations;
  they do not own training, validation, authority, or aggregation.
- Python governs contracts, provenance, canonical exports, dataset versions,
  model training, independent validation, statistics, and aggregation.
- Fiji remains a frozen compatibility and regression reference for historical
  G-SURF measurements, rather than a required successor runtime.
- Dataset and model lifecycles require identities, splits, scope declarations,
  review states, and promotion gates that do not belong in the settled-study
  authority model.

A permanent successor branch would mix historical reproducibility with ongoing
experimentation and make it easier to confuse new outputs with settled results.
A separate repository gives the new platform its own versioning, tests,
releases, and scientific claim boundary.

## Why the name IFQuant-Platform

`IFQuant-Platform` describes a durable, reusable system rather than a temporary
rewrite. It deliberately avoids:

- `Next`, which eventually becomes an outdated version label;
- `QuPath`, because QuPath is an important adapter and user environment, not the
  owner of scientific meaning or the only possible backend;
- `Lung` or `G-SURF`, because the successor should support explicitly scoped
  tissues and studies without implying that one model is universally valid; and
- `CNN`, because models must remain replaceable and classical baselines remain
  part of validation.

The intended design principle is **QuPath-first, Python-governed, and
model-replaceable**.

Recommended GitHub repository description:

> QuPath-first, Python-governed platform for reproducible immunofluorescence
> cell analysis, morphology, dataset curation, and replaceable machine-learning
> models.

## Initial responsibility boundary

| Component | Responsibility |
|---|---|
| QuPath | Image viewing, annotation, cell/nucleus segmentation, morphology and intensity measurements, prediction review |
| Groovy | Small deterministic QuPath executors driven by validated configuration |
| Python | Scientific and data contracts, provenance, training, validation, statistics, aggregation, and CLI orchestration |
| Model backends | Replaceable, scope-declared segmentation or classification implementations |
| Fiji compatibility | Reproduce and regression-test historical G-SURF measurements only |

The first implementation should define a canonical cell-object package before
choosing a custom CNN. Native QuPath detection, StarDist, and InstanSeg can then
be compared behind the same interface. Human-corrected QuPath annotations should
feed versioned datasets, with validation split by mouse, slide, acquisition
batch, and scanner rather than randomly by tile.

## What may move to the successor

Reusable concepts may be deliberately ported with new tests:

- closed scientific-definition and threshold/model contracts;
- content-addressed provenance and fail-closed validation;
- canonical additive measurement and aggregation semantics;
- synthetic and historical regression fixtures; and
- explicit observed-zero, not-evaluable, QC, and review states.

Do not bulk-copy the historical pipeline, settled outputs, authority records,
workstation paths, or G-SURF claim language. Compatibility must reference a
specific IFQuant-Lung commit or release and remain visibly separate from native
successor records.

## First successor milestone

1. Initialize the repository and Python package.
2. Define contracts for images, annotations, cell objects, detector/model
   identity, QC, and review state.
3. Implement a minimal configuration-driven QuPath Groovy executor that detects
   cells, calculates morphology/intensity features, and exports canonical
   intermediate data.
4. Add independent Python validation and deterministic test fixtures.
5. Establish classical and pretrained segmentation baselines before deciding
   whether a custom CNN is justified.

No model or backend is scientifically promoted merely by moving to the new
repository. Biological validity, domain transfer, and backend replacement still
require independent, predeclared validation.
