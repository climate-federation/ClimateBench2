# Changelog

## Unreleased

The protocol machinery is generalised so that a second protocol can run on it
without forking, for example an AMIP-style comparison of atmosphere-only
models. ClimateBench2's own behaviour is unchanged. No key is added to
`thresholds.yml`, the protocol fingerprint of the
frozen comparison bundle is the same, and the version stays 2.0.1, so existing
bundles remain valid. Every new switch is an optional thresholds key whose
default is the previous behaviour.

### Protocol plug-in

- **Layered thresholds.** `thresholds.yml` is the base of a stack of YAML layers,
  deep-merged; a protocol overlays its own file (`_thresholds.set_threshold_layers`,
  `use_thresholds`; `get_threshold(path, default)` for optional switches).
- **Bounds are bound at run time.** Nothing reads a threshold at import or
  class-definition time: `GateCheck` bounds are `Threshold` / `Band` references
  resolved when the gate is evaluated (`pass_fail.resolved_checks`); a gate can be
  repointed at another thresholds block (`_threshold_block`).
- **Experiment keys.** Every complex gate reads its experiments through a class-level
  `keys` mapping (role to data key), so a subclass changes experiments without
  copying the computation.
- **`climatebench2.protocol.Protocol`** bundles the suite registry, the suites and
  gate packages, the submission key, further keys that read each member's own record
  (`member_keys`), the experiment-to-frequency map and the scorer tag. The CLI
  (`main(argv, protocol=...)`), `regate`, the gate set, the scoring pass and the
  leaderboard read the active protocol.
- **Automatic not-applicable.** A thresholds block may list `na_if_missing`
  variables: a gate whose experiments lack one writes rows with `applicable = 0` and
  a `na_reason`, not no row. `--not-applicable` also reaches gates in cube suites.
- **Deploy runner.** `deploy/cb2_run.py` takes its CLI, scorer tag, comparison mode,
  member-record keys and model card from the profile; the `v2` profile is unchanged.

### Tier II scoring pass (all optional, default off)

- **Pluggable baselines** (`register_baseline`, `tier2.baselines`).
- **`E_ref` window and mode** (`tier2.e_ref.window`, `tier2.e_ref.mode`): the
  reference error over the years the comparison ensemble covers, or the Climatology
  baseline as an interim reference.
- **Scored windows per entry** (`tier2.scored_window(s)`,
  `tier2.anomaly_baseline.periods`).
- **Per-step scores** (`tier2.persist_steps`): a `score_steps` table with
  leave-one-member-out variants, and `scoring.paired_block_indices`, a seedable
  block-bootstrap draw that does not depend on member count.
- **Internal variability from the ensemble** (`tier2.internal_variability.source:
  ensemble_spread`), for protocols without a control run.
- **Single-member mean absolute error** (`tier2.single_member_mae`) in its own
  column; a deterministic run still gets no CRPS.

### Fixes

- **III.1b mid-Holocene monsoon gate.** Its North-Africa box crosses the Greenwich
  meridian and was passed to `extract_region` as 340..30, which iris refuses
  ("minimum greater than maximum"): the gate could not run. It is now -20..30, and a
  test computes the gate (none did).
- **A monthly series labelled `monC` is monthly.** GFDL-CM4's Amon files carry the
  CMIP6 label of a monthly climatology; `select_frequency` dropped them wherever the
  variable also existed daily, and the gates reading monthly `pr` or `rlut` reported
  the variable missing.
- **An open side of a range** (`range: [1.0, null]`) resolves to unbounded.

## 2.0.1 (2026-11-02)

Fixes found by the first end-to-end run of the cloud runner (MPI-ESM1-2-LR, ten
members, the full default suites against the v2.0.0 frozen comparison bundle), plus
one ratified protocol decision. The frozen comparison bundle is re-frozen under 2.0.1
(the CLI refuses a bundle of another version); its protocol fingerprint is unchanged.

### Tier I

- **`historical` ends at 2014** (ratified 2026-11-02, decision E.7). A submission's
  `historical` member is historical + SSP2-4.5 spliced to 2100; Tier I now receives it
  cut at `windows.pre_test_last_year()` (`tier2.test_window_start` − 1), in the CLI so
  every entry point agrees. I.3c was already windowed (1983–2014); I.8b's 12-month
  ITCZ–EFE climatology read the whole record and so averaged in the scenario and the
  reserved test window. The paper's Tier I run used a historical-only farm, so the
  published I.8b values stand. The per-member events suite keeps each member's full
  record.

### Tier II

- **A variable's ensemble is the sources that carry it.** One `raw_output` can hold
  several variables: the full default `ClimateBench2_TierII` suite writes all of
  `annual_mean_timeseries` into one table, and the published `cre_score` split run
  held `swcre` and `lwcre` together. A comparison member staged for one of them but
  not the other sat in the table with NULLs, emptied its model's inner join in
  `stack_members`, and silently dropped that whole model from the other variable's
  `E_ref` (rlut: 15 models instead of 18 in the full-suite run). The scoring pass now
  groups, per variable, only the sources with a value for it
  (`scoring_pass.variable_carriers`), so a full-suite run gives the split-suite rows.
  **Published `lwcre`/`swcre` scores change:** GISS-E2-1-G (6 `lwcre`, 5 `swcre`
  members) re-enters both ensembles; for MPI-ESM1-2-LR `E_ref` goes 0.2551 → 0.2473
  (`lwcre`) and 0.2926 → 0.2781 (`swcre`), skill −0.345 → −0.387 and −0.647 → −0.733.
  Every other published Tier II row is unchanged.

### Data

- **One variable at several frequencies.** A CMIP-style member directory holds
  `Amon/pr` *and* `day/pr` (the layout `deploy/README.md` documents); ClimateEval
  loaded them as two `pr` cubes and refused both ("Got 2 cubes … expecting 1"), so the
  submission's `pr` silently vanished from every Tier II entry, the events
  hemispheric asymmetry, the whole daily suite and Tier I I.3c/I.8b.
  `climatebench2.data.frequency` now gives each suite entry the frequency its
  variables name (experiment suites: monthly, except the `day` experiment; the
  perfect-model truth: per variable). Data without the ambiguity loads exactly as
  before. Upstream candidate: the same narrowing in ClimateEval's
  `get_prepared_cube`.

### Cloud runner

- `run.json` records `climatebench2.__version__` (a stale editable install reported
  0.1.0 for a 2.0.0 run).
- `run.json`'s `unscored` lists only skill-bearing rows (submission and baselines with
  a CRPS) that ended without `E_ref`, not the consistency and `reason` rows that never
  carry one.

## 2.0.0 (2026-10-30)

The ClimateBench v2 protocol as published, with the fixes and additions made
since v1.0.0 (the paper's code release). Decisions are recorded in
[docs/metrics_reference.md](docs/metrics_reference.md) ("Decisions needed").

### Tier I

- **I.3c precipitation–buoyancy fixed.** Published I.3c results change: 20 of 51
  CMIP6 models passed at v1.0.0, 41 of 58 now.
  - The model is regressed over the paper's 1983–2014 window, on the same calendar
    months as the GPCP/ERA5 reference. v1.0.0 used its full historical record.
  - The geopotential is hydrostatic from each side's own `ta`/`hus`, for the model
    as for ERA5. v1.0.0 used the model's `zg` against a hydrostatic reference.
  - Two level-ordering bugs. The column integral paired top-down data with an
    ascending pressure list, so 1000 hPa was integrated as the column top. ESMValCore
    returned all-NaN when the requested levels ran opposite to the data's order.
  - The model and the reference are integrated over one symmetric level set: the
    submission's levels, matched within a 1e-6 tolerance, less any level either side
    has no data on or the reference cannot reach.
  - `tier1.precip_buoyancy.reference_slope: 0.1166` is a documented fallback, used
    only when the run-time GPCP/ERA5 reference is unavailable. Each I.3c gate row
    records which reference it used in `reference_source` (`observations` or
    `stored-fallback`). The ±30 % tolerance is unchanged.

### Tier II

- **Comparison-ensemble member policy in the package.** `r*i1p1f*`, the first ten
  realizations per model, capped per model before variable availability
  (`tier2.comparison_ensemble`, `data.cmip6_staged.select_members`). Any data root
  now gives the same `E_ref`. In v1.0.0 this policy lived only in an out-of-repo
  staging script.
- **`ts` dropped** from the Tier II scored variables (MERRA-2 skin temperature).
  Model `ts` stays a Tier I input.
- **Sea ice scored to 2025** against HadISST, refreshed to 2026-07. v1.0.0 scored
  it to 2021.
- **Two new scored entries:**
  - `prw` (water vapour path), with its CMIP6 comparison ensemble.
  - Ocean heat content: full column, 0–2000 m and 0–100 m. It is computed from the
    submission's `thetao`, scored against IAP with EN4 as the second product, and
    has a comparison ensemble of 34 models.
- **Core and Extended entries** follow paper Table 2 (`tier2.variable_status`).
  - Extended: OHC, surface fluxes, the cloud properties (`clt`/`clwvi`/`clivi`) and
    `prw`.
  - Every scored row carries `variable_status`, and the leaderboard shows the
    Extended entries in a table of their own.
  - The tag only affects presentation; it is not part of the frozen-bundle
    fingerprint.

### Running

- **Frozen comparison mode.** `climatebench2 freeze-comparison` copies the CMIP6
  comparison rows of live runs into a bundle. `climatebench2 score --comparison
  frozen` (or `merge-comparison`) then scores a submission with only the
  submission and the references on disk.
  - A merge is refused if the protocol fingerprint, the version, the Tier II window
    or a variable's resolved window does not match.
  - A frozen rescore reproduces the live scores exactly.
  - `--comparison none` scores with no `E_ref` at all.
- **Cloud deployment** (`deploy/`):
  - a container image (`ghcr.io/climate-federation/climatebench2`) and the
    `cb2_run.py` runner;
  - a Kubernetes Job and a pinned scoring profile (`deploy/profiles/v2.yml`);
  - submissions are read from a private GCS bucket (or S3) and results are written
    to `gs://climatebench/v2/results/`.
- **v2 reference bundle** at `gs://climatebench/v2/references/` (~34 GB, sha256
  manifest). The v2 frozen comparison bundle goes at
  `gs://climatebench/v2/comparison/`.
- **ClimateEval pinned to `d2559b2`**, ClimateEval main with every upstream pull
  request the suites need. v1.0.0 pinned `b0e941c` and needed a local checkout. The
  pixi lock is the same as for the paper's results, and no Tier I gate value or
  Tier II `raw_output` changes.
- `__version__` and the package version are now 2.0.0. v1.0.0 still reported 0.1.0.
