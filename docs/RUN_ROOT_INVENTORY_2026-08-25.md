# Local run-root inventory, 2026-08-25

> **Status: READ-ONLY STRUCTURAL AUDIT.** This is a sanitized inventory of the
> local run root. It records no workstation-absolute paths, raw images,
> unblinding keys, or confidential table contents. It does not supersede the
> machine-readable project authority and does not promote an exploratory or
> engineering run.

## Inspection coverage

The local run root contained 24,139 files totaling 70,244,925,171 bytes
(65.421 GiB) across confocal releases and reruns, visual-panel work, H&E review
packages, confocal-to-H&E mapping work, exploratory outputs, and legacy/WSI
pilots. The scan was read-only. Small manifests, CSVs, logs, and declared hashes
were inspected; large image payloads were inventoried but not all re-hashed.
Nothing was copied into the repository.

## Authority findings

### Confocal

- The current settled release is present in two byte-identical eight-file
  copies. It contains 80 expected and 80 quantified unique fields, including
  the documented partial field and whole-field denominator override.
- The relocated 80-row generating run and isolated single-field repair are
  present. Their small-artifact hashes reconcile to the settled release, so the
  earlier "source run unavailable" limitation was a relocation issue.
- The confirmed visual-panel directory has 80 canonical panels, while a sibling
  pre-repair visual manifest and analysis directory each describe only 79.
  `canonical_field_manifest.csv.reviewed_visual_panel` is therefore the display
  authority; recursive discovery is not.
- A newer 80-key run is explicitly exploratory: it uses whole-field regions,
  average projection, adaptive Otsu, and records a partial-failure status. Its
  timestamp cannot outrank the settled authority.

### H&E

- The approved R1 package is present at its relocated location. All 87 declared
  entries exist with matching sizes; all 17 small metadata hashes checked in
  this audit match. Its approval, package, and locked-profile hashes match the
  repository configuration.
- The current H4 package contains 96 unique, exactly reconciled review
  candidates (64 core plus 32 supplements). Ten airway fields have been
  touched, but no review row is complete. The live review ledger has changed
  since package generation, so immutable package provenance and mutable review
  state must remain separate.
- The H5/H7 development package is structurally present, but all eight H7
  review rows remain blank. H5, H6, and H8 remain blocked.
- The later 22-row corresponding-region mapping passes its engineering checks;
  it remains corresponding-region context, not same-section or same-cell
  registration. The older full-80 mapping attempt is mostly ambiguous and
  superseded.

### Whole-slide fluorescence

- Five Stage 1/tile-manifest layouts exist. Every one records
  `coverage_complete=false`; there is no complete analytical WSI run in the
  inventory.
- Only two have Stage 2 summaries/manifests. One has six tiles and seven valid
  region rows because a tile is partitioned into damaged and intact regions.
  The other retains an orphan partial retry beside the completed retry.
- No Stage 2 run index, checkpoint, or content-addressed resume record existed.
  Older manifests also retain pre-relocation absolute paths and lack source,
  engine, configuration, or output hashes.

## Structural decisions derived from the scan

1. Analytical consumers use explicit allowlists and hashes; directory recency
   and recursive discovery carry no authority.
2. Relocation resolution preserves both declared and resolved locations,
   accepts a candidate only when the expected hash identifies it uniquely, and
   never rewrites immutable historical manifests.
3. Generated review packages are immutable. Reviewer progress belongs in an
   append-only or versioned ledger outside the package manifest.
4. Amended releases preserve the base run and isolated repair as separate
   provenance inputs.
5. WSI shard coverage is a disjoint union of tile IDs. Measurement uniqueness
   is `(section_id, region, panel)`, allowing legitimate multi-region tiles but
   rejecting stale retries even if `output_key` changes.
6. WSI inventory outputs remain engineering fixtures until an exhaustive,
   uncapped run passes the scientific mask, threshold, airway, sampling, and
   biological-design gates.

The first code implementation of these decisions is the explicit hashed Stage
2 index described in [`WSI_TILING_WORKFLOW.md`](WSI_TILING_WORKFLOW.md).
