# Measurement-definition contracts

> **Status: engineering reconstruction only.** These files establish a clean
> numerical contract boundary. They do not change the settled release, attest
> an external run, authorize a threshold, prove backend equivalence, or enable
> WSI measurement.

## The split

[`krt5_positive_area_kernel_v1.json`](krt5_positive_area_kernel_v1.json) holds
only numerical semantics: one semantic KRT5 input; calibrated 2-D input;
complete-plane processing including supplied halo; Gaussian blur; a named but
unvalued fixed-threshold parameter; 8-connected component filtering at 50 µm²;
reference-mask intersection last; explicit zero behavior; and ratio-of-sums
aggregation.

This v1 contract begins after a canonical 0/255 reference mask has been
supplied. It does not define tissue/anatomy-mask construction or review, the
sampling design, or the biological estimand. Different confocal and WSI
reference spaces need separate upstream attestations and must not be treated as
comparable merely because this kernel accepts the same mask primitive.

[`g_surf_confocal_260808_krt5_threshold_candidate_v1.json`](g_surf_confocal_260808_krt5_threshold_candidate_v1.json)
binds that parameter to 300 and declares a uint16, identity-transform confocal
reconstruction scope. The state `identifier_only_unattested` means that no
acquisition bytes or content-addressed scope profile are bound. Changing the
value or applicability declaration changes the threshold-set and method-instance
hashes, but not the scientific-definition hash.

| identity | SHA-256 |
|---|---|
| scientific definition | `086d69cf98379a9d685685a3b4785b58c1df163bdd571884f3ebde99978b9e16` |
| threshold set | `5301ce71ff693f5975eceeaea6dfac7da66469d002984a229c26f1b21ea513aa` |
| resolved method instance | `94ec5add390ece6d3a318a56d4b03fc40535183e151f0f11f32a00e086bf7628` |

The method-instance identity is a domain-separated hash over the two canonical
hashes. Raw-file SHA-256 values remain distinct byte-provenance identities.

## Validate

```powershell
python scripts/validate_measurement_definition.py `
  config/measurement_definitions/krt5_positive_area_kernel_v1.json `
  --threshold-set config/measurement_definitions/g_surf_confocal_260808_krt5_threshold_candidate_v1.json `
  --expect-definition-sha256 086d69cf98379a9d685685a3b4785b58c1df163bdd571884f3ebde99978b9e16 `
  --expect-threshold-set-sha256 5301ce71ff693f5975eceeaea6dfac7da66469d002984a229c26f1b21ea513aa `
  --expect-method-instance-sha256 94ec5add390ece6d3a318a56d4b03fc40535183e151f0f11f32a00e086bf7628
```

Success reports `authorization: none`. Contract validity is not project
authority.

## Endpoint-name crosswalk

The definition uses the canonical authority endpoint ID
`thresholded_total_KRT5_area_fraction`. `KRT5_pod_area_frac` is a legacy engine
CSV column, and `KRT5_pod_area` is a settled-release report label. Those aliases
belong in explicit adapters or a future governance bundle, never in the pure
scientific identity.

## Boundary of the checked-in candidate

Threshold 300 is also declared by
[`scripts/run_confocal_260808.ps1`](../../scripts/run_confocal_260808.ps1).
That coincident repository value is not a machine-checked relationship and may
drift independently. It does not prove that every settled external artifact
used the same runner bytes, nor does the settled release currently expose
canonical binary reference masks for exact replay. A future governance manifest
and external verifier must bind those facts before this reconstruction can
become an attested historical bundle.

Still missing are acquisition-to-semantic channel bindings, Fiji and QuPath
execution attestations, canonical primitive packages, numerical golden
fixtures, a record-level method compatibility gate, and any WSI-specific
threshold/reference-space contract. Until those exist, these artifacts must
not enter analytical aggregation.
