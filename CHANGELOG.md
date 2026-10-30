# Changelog

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
