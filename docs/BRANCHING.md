# Branch roles

> **Status: CURRENT.** Branch topology verified against `git branch -a` and
> `git tag` on 2026-08-26. The "Completed Z-stack merge gate" section at the
> bottom is a **historical record** of a gate that was passed, not a live
> checklist.

The repository was renamed to **`IFQuant-Lung`** on 2026-08-07 (previously
`Fiji_ImageJ_Cell_Counting`). GitHub redirects the old URL, so existing clones
keep working, but new remotes should use the new name.

## Active branches

- `main`: **the default and integration line.** Carries the morphology-first
  Fiji engine, the QuPath whole-slide front end, the damaged-area partition,
  the relational endpoints module, the control-derived calibration, the
  four-route launcher, and (since PR #22 through PR #24, merged 2026-08-25
  and 2026-08-26) the audit-driven provenance framework. Work reaches `main`
  by pull request.
- `codex/audit-authority-provenance`: the current working branch for the
  audit-authority and provenance work and the v1.9.7 release preparation.
  New work lands here first.

Fully merged history labels with zero unique commits:
`codex/fix-run-failures-v1.9.5` (tip `d692fdd`) and the local-only
`work/local-analysis-in-progress-20260821` (tip `45ecea6`).

## Tags

| tag | commit | what it marks |
|---|---|---|
| `v2.0.0` | `dfa3cfa` | "Preserve the superseded QuPath engines and consolidate branch roles" |
| `v1.8.0` | `f16e8b4` | the four-route launcher |
| `v1.9.0` | `22afada` | published launcher release (2026-08-08) |
| `v1.9.1` | `febcf94` | latest published launcher release, the input-scope reachability repair (2026-08-09) |

Two tag series coexist because `v2.0.0` versions the **repository** and `v1.8.0`
versions the **launcher**. That is confusing, reads as a rollback in `git tag`
output, and puts the *lower* number on the *later* commit.

The launcher was tagged and published as **v1.9.0** at `22afada` on 2026-08-08.
**v1.9.1** (`febcf94`, the input-scope reachability repair) followed on
2026-08-09 as the latest published launcher release, with its own binary and
hash sidecar; its commit also archives the v1.9.0 binary under
`legacy/launchers`. The launcher source has since advanced to v1.9.7, which is
not yet tagged or released.
The remaining problem is naming, not missing release state: repository tag
`v2.0.0` predates the launcher tags `v1.8.0` through `v1.9.1`. Future tags
should either use one repository-wide version or explicit namespaces such as
`repo-v*` and `launcher-v*`. Existing tags are historical records and must not
be rewritten.

Everything else was consolidated onto `main` on 2026-08-07 (PR #11).

## Review-only branches

- `claude/module-drafts`: unreviewed drafts of the morphometry, spatial,
  hierarchy-contract and injury-model-profile modules, under `_drafts/`.
  **Never merge this branch.** It exists so the drafts can be read as a diff.
  Integrate by copying reviewed pieces onto `main`.

## Retired

Branches removed from the remote on 2026-08-07. Nothing was lost.

**Fully merged, zero unique commits** — pure history labels, deleted:

- `codex/universal-lung-marker-profiles` (tip `680e19a`)
- `codex/z-stack-analysis` (tip `3a2319e`, promoted to `main`)

**Superseded, unique content preserved before removal:**

- `claude/qupath-influenza-pipeline-d4jt28` (PR #9, tip `ac9250a`)
- `claude/qupath-slidescanner-launcher-d4jt28` (PR #10, tip `3160753`)

Both implemented a **second, independent QuPath-side measurement engine**. That
was evaluated and rejected: two measurement engines drift, and the
morphology-first decision model has been validated exactly once. The adopted
architecture keeps QuPath as a reader/tiler only and routes everything through
the unchanged Fiji engine — see
[`QUPATH_FIJI_INTEGRATION.md`](QUPATH_FIJI_INTEGRATION.md).

By the time they were retired both branches were *behind* `main`, so merging
either would have deleted the current work (14,309 and 3,721 deletions
respectively).

Their unique files are preserved at
[`../legacy/qupath-superseded/`](../legacy/qupath-superseded/README.md), with
the commit SHAs needed to restore the full branches if ever required.

## Already merged historical labels

These are ancestors of `main` and contain no unmerged work. Some no longer exist
on the remote; verify with `git ls-remote --heads origin` before relying on any
of them.

- `claude/influenza-injury-fiji-pipeline-d4jt28`
- `codex/incorporate-morphology-hierarchy`
- `codex/legacy-pre-reorganization` (historical pre-reorganization snapshot)

## Completed Z-stack merge gate — historical

The following checks were completed before promoting
`codex/z-stack-analysis` to `main`. They are a record of a gate that was passed
during the ALI organoid pilot; they are **not** a live checklist, and the
launcher version history below stops at v1.7.2 because that is where the gate
stopped. Current launcher state is in
[`DEVELOPMENT_CHECKPOINT_2026-08-26.md`](DEVELOPMENT_CHECKPOINT_2026-08-26.md).

1. Representative 20× stacks for `ALI1`, `ALI2`, and `ALI3` completed.
2. Each validation manifest completed without image failures.
3. Zero-cell false-success protection was enabled and DAPI QC reviewed.
4. Per-marker Z profiles and selected slabs were recorded.
5. The display-enhancement regression reproduced the pre-enhancement ALI3
   counts and area fractions exactly.
6. The versioned launcher was rebuilt and its embedded-runtime self-test
   returned exit code 0. Launcher v1.6.1 additionally passed real-folder AUTO
   detection for ALI1, ALI2, and ALI3 and rejected their mixed parent folder.
   Launcher v1.6.2 added the independently validated five-image,
   enhanced-PNG-only preview route without changing the full-analysis path.
   Launcher v1.7.0 adds strict per-image routing for mixed built-in panels and
   validated three-channel ALI mapping subsets.
   Launcher v1.7.1 additionally exports a companion visual merge panel for
   every image in a full analysis and removes the five-image cap from the
   separate visual-merge-only operation.
   Launcher v1.7.2 declares ALI channel 4 as the primary endpoint, embeds the
   cilia-specific AcTub correction, and retains morphology as final authority
   after the more permissive ALI tdTomato candidate threshold.

Automatic Z ranges remain pilot settings. A confirmatory study must still
freeze explicit ranges, intensity thresholds, and morphology gates after
blinded control review.
