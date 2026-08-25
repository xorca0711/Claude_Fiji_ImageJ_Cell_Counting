# Project authority

`authority/project_state.json` is the repository's machine-readable source of
truth for current release status, scientific claim boundaries, modality scope,
and promotion gates. It intentionally contains no workstation paths, subject
identifiers beyond the study's public mouse labels, or copied source artifacts.

`docs/generated/AUTHORITY_STATUS.md` is a deterministic view of that JSON. Do
not edit the generated Markdown by hand.

## Update workflow

1. Edit `authority/project_state.json` and keep its contract version explicit.
2. Run `python scripts/render_authority_status.py` to regenerate the Markdown.
3. Run `python scripts/render_authority_status.py --check` to prove the checked-in
   Markdown is current.
4. Run `python -m unittest tests.test_authority_contract -v`.

The renderer uses only the Python standard library. It performs project-specific
contract checks in addition to the declarative JSON Schema. In particular, it
rejects absolute local paths, home-directory references, UNC paths, and local
file URIs anywhere in the authority payload.

## Interpretation rule

An engineering state does not promote a scientific claim. The settled confocal
CSV release records 80 quantified fields, but one included field has a
whole-field tissue-denominator override that is not scientifically comparable
with the usual auto-DAPI denominators. The release therefore remains a
descriptive selected-field artifact with an explicit comparability exception.
H&E H0-H3 and the six-tile WSI pilot remain engineering or image-QC work only.

If prose elsewhere conflicts with the JSON, resolve the discrepancy explicitly;
do not silently weaken a limitation or infer a scientific promotion from an
implementation milestone.
