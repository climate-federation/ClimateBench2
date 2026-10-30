# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

ClimateBench v2 defines the **protocol for scoring and testing climate models and
emulators** (Tier I pass/fail gates, Tier II probabilistic scores, Tier III
out-of-sample tests, the leaderboard). It is a **thin layer on top of
[ClimateEval](https://github.com/climate-federation/ClimateEval)**, which owns data
loading, preprocessing, regridding and generic physical diagnostics.

[docs/metrics_reference.md](docs/metrics_reference.md) is the authoritative spec
(inputs, preprocessing, formula, threshold) of every check. **The paper is the truth**:
fix a disagreement in the document, never by diverging in code; add open questions to
its "Decisions needed" section rather than inventing a value.
[docs/climateeval_delineation_plan.md](docs/climateeval_delineation_plan.md) covers the
architecture and ownership boundary; check its §9 "Definition of done" before calling
anything finished.

**Ownership test:** code that loads or regrids data, or is a generic physical
diagnostic, belongs in ClimateEval (as an upstream PR). Code that encodes a threshold,
probabilistic score, baseline, tier structure or the leaderboard belongs here. The
only data-touching code here is declared: `data/`, `diags/truth_reference.py` (an
upstream candidate) and `reference_windows.py` (NetCDF headers only). `analysis/` holds
standalone paper studies, not protocol code; the package never imports it.

## The ClimateEval dependency

ClimateEval is third-party, pre-1.0 and not on PyPI; it is **pinned by commit** in
`pyproject.toml` (`d2559b2`, which has every upstream PR the suites need; keep
`deploy/Dockerfile`'s `CLIMATEEVAL_REF` equal to it). **Never vendor or patch it**:
contribute upstream or keep the code here. Development uses ClimateEval's pixi
environment: `pip install -e ../ClimateEval && pip install -e . --no-deps`.

## The package: `climatebench2/`

```
climatebench2/
├── diags/               # Diagnostic subclasses: pass_fail (GateMixin), tier1_*, tier2_*, tier3_paleo
├── suites/              # suite YAMLs; may reference climatebench2.diags.* AND climateeval.diags.*
├── thresholds.yml       # EVERY bound/constant/window + each gate's `requirement:` and `tier:`
├── _thresholds.py       # get_threshold("tier1.ecs.range"): the only reader of thresholds.yml
├── scoring.py           # pure fair-CRPS / bootstrap / consistency / EOF engine (numpy only)
├── scoring_pass.py      # post-suite Tier II pass: stack members -> fair CRPS -> skill
├── regate.py            # re-evaluate stored Tier I/III gate statistics against thresholds.yml
├── windows.py           # the protocol's time windows (test window, 1985-2014 baseline, ...)
├── reference_windows.py # protocol window clipped to the staged reference's complete years
├── physics.py           # pure Tier I physics + ETCCDI indices (numpy only)
├── baselines.py         # climatology pseudo-members, calibrated two-layer EBM
├── shared_sources.py    # reference + comparison ensemble loaded once for all members
├── frozen_comparison.py # freeze the CMIP6 comparison rows into a bundle; merge into a run
├── data/                # cmip6_staged.py (StagedCMIP6HistoricalSSP245: the ensemble behind
│                        #   E_ref), imerg.py (daily pr), modis.py (clouds), ERF table (EBM),
│                        #   ocean_heat_content.py (thetao -> layer OHC on load; upstream candidate)
├── leaderboard/         # .ddb results -> scores table -> static HTML page
└── _cli.py              # `climatebench2 score` / `climatebench2 leaderboard`; SUITE_REGISTRY
```

## Key integration facts

- ClimateEval's `Suite` loads any importable `diagnostic:` class path (`str_to_object`);
  a CB2 diagnostic need only subclass `climateeval.diags._base.Diagnostic`.
- Where ClimateEval already computes the quantity (ECS, land-ocean warming ratio,
  Arctic amplification, meridional heat transport), the CB2 gate is a **thin wrapper
  subclassing the upstream diagnostic** (`GateMixin`) that feeds `thresholds.yml`
  constants into its kwargs and gates its output columns. Never copy the physics.
- Multi-experiment inputs follow the `ECS` `ComplexDiagnostic` pattern
  (`_required_data_keys` + `ComplexDataSource`). CB2 complex diagnostics accept a
  *superset* of their required keys so one experiment dict feeds a whole suite.
  `Suite.get_database` passes the same data object to every diagnostic, so suites are
  split by data shape (cubes vs experiment dict).
- `_cli.SuiteSpec` (in `SUITE_REGISTRY`) says, per suite, which data shape and window
  it gets and whether it runs **once per ensemble member** (`per_member`). The cube
  Tier II suites and `ClimateBench2_TierII_events` are per member; Tier I, the
  variability suite and Tier III are not. A per-member experiment suite gets each
  member's own record as `historical`. Default suites: `TierI`, `TierI_variability`,
  `TierII_events`, `TierII`; `TierII_daily` and `TierIII` need `--suite`.
- Output uses ClimateEval's DuckDB schema (`raw_output`, `metrics`, `variables`,
  `data_sources` per diagnostic). CB2 **adds columns to `metrics`, never a schema**.
- `scoring_pass` recognises three `raw_output` shapes: a **time series** (regime a),
  an **EOF-coefficient** table (regime b) and **aggregated scalars** with no time axis
  (§II.1). A simple diagnostic writes the third shape through `ScalarTableDiagnostic`
  (`diags/tier2_daily.py`). A skill score that is not an error (the Perkins PDF
  overlap) is written as a metric and never enters `E_ref`.
- Every scored row carries a `window` label (`held-out` / `in-sample` /
  `perfect-model`) from `tier2.window_labels` and a `variable_status` (`core` /
  `extended`, paper Table 2) from `tier2.variable_status` — presentation only, so
  it stays out of `PROTOCOL_THRESHOLDS`; `perfect-model` whenever the reference's
  `data_sources.category` is `truth`. The Tier II pass deletes and rewrites only its own
  `scorer = climatebench2` rows; Tier III paleo rows (`scorer = climatebench2.tier3`)
  are written by the diagnostic and survive `--rescore`.
- Every **gate** row carries its protocol `tier` (I/II/III) and `requirement`
  (required/extended/extra/diagnostic) plus `applicable`; the leaderboard groups by
  (tier, requirement) and computes the entry ticket over the Required group.
- A frozen comparison run (`--comparison frozen`) must give the same `E_ref` as
  the live run its bundle came from: never let a merge go through on a
  mismatched window, variables row or protocol fingerprint
  (`frozen_comparison.PROTOCOL_THRESHOLDS` lists the `thresholds.yml` entries
  that shape comparison rows; extend it when a diagnostic reads a new one).
- **Every bound lives in `thresholds.yml`**, read via `_thresholds.get_threshold`;
  never hard-code one in a diagnostic.

## Commands

```bash
climatebench2 score /path/to/model/Amon --name MyModel --data-root /path/to/staged
#   one .ddb per suite in MyModel_climatebench2/ (-o/--out DIR). --data-root holds the
#   references (<source id>/<frequency>/<var>/*.nc) and the CMIP6 comparison ensemble
#   (CMIP6_<model>_historical-ssp245_<member>) unless $CLIMATEBENCH2_STAGED_CMIP6_ROOT
#   points the ensemble elsewhere (e.g. a subset for a smoke run).
climatebench2 score MODEL --name MyModel --member r1i1p1f1=DIR --member r2i1p1f1=DIR \
    --experiment picontrol=DIR --experiment 4xco2=DIR --experiment histaer=DIR
#   members are auto-discovered from a DRS tree if not given. Tier I keys: picontrol,
#   4xco2, histaer, day, amip, amip4xco2, patch_ep, patch_wp; gates missing a key skip.
climatebench2 score MODEL --name MyEmu --not-applicable geostrophic_balance
#   declare a Tier I gate inapplicable: recorded as n/a, not a fail
climatebench2 score MODEL --name MyModel --suite ClimateBench2_TierIII \
    --experiment picontrol=DIR --experiment lgm=DIR \
    --paleo-data-root paleo_scripts/paleo_data_cache/processed/observations
climatebench2 score MODEL --name MyModel --truth DIR --truth-name CESM2 \
    --truth-member r2i1p1f1=DIR          # Tier III.2 perfect model (window = perfect-model)
climatebench2 score MODEL --no-score     # suites only; run the Tier II pass later
climatebench2 score MODEL --no-cache     # re-load reference + ensemble for every member
climatebench2 freeze-comparison run/*.ddb -o BUNDLE --timerange 20150101/20251231
#   copy the CMIP6 comparison rows (data_type 'other', non-observational) of live
#   runs into a bundle (.ddb per suite + manifest.json); --extend adds a variable
#   group, --replace re-freezes one; tier2.frozen_comparison.exclude_variables (ts).
#   A bundle records climatebench2.__version__ and the CLI refuses another version:
#   bumping the version means re-freezing the bundle.
climatebench2 score MODEL --comparison frozen --frozen-comparison BUNDLE
#   strip the ensemble generators from other_data, Tier I wrappers skip theirs,
#   merge BUNDLE before the pass (keys: suite stem, schema, variable); refuses a
#   protocol/window/variables-row mismatch. --comparison none: no E_ref at all.
#   --isolate-crashes (default on unless live): CB2_GATE_CRASH, suite continues
climatebench2 merge-comparison BUNDLE DB...  # merge into existing DBs (--replace)
#   other score flags: --timerange, --download, --institute/--exp/--variant (provenance)
climatebench2 leaderboard MyModel_climatebench2/*.ddb -o board.html   # or --csv FILE
climateeval report MyModel_climatebench2/*.ddb   # ClimateEval's per-model report
climatebench2 leaderboard --rescore *.ddb  # re-run the Tier II pass (idempotent)
climatebench2 leaderboard --regate *.ddb   # re-gate Tier I/III from stored raw_output
#   --rescore/--regate carry a thresholds.yml edit into existing databases without
#   re-running suites; --regate never touches scorer rows or n/a rows

# Tests: SERIALLY (no pytest -n), in ClimateEval's pixi env with this repo on PYTHONPATH
cd ../ClimateEval && PYTHONPATH=/path/to/ClimateBench2 \
    pixi run --frozen python -m pytest /path/to/ClimateBench2/tests -q
```

Every test fixture is synthetic (no network, no real model output), so a green suite
does not prove a real run works. `tests/test_suites.py` builds every diagnostic of
every suite in `SUITE_REGISTRY`; `tests/README.md` covers linting.

## Paleoclimate pipeline (`paleo_scripts/`)

Data preparation, not protocol: it downloads and processes the proxy compilations of
paper Appendix D. `diags/tier3_paleo.py::PaleoProxyScore` scores the model against its
output, `paleo_data_cache/processed/observations/<period>/<dataset>.nc`.

```bash
cd paleo_scripts
python download_paleo_observations.py [--dataset lgmda lig127k] [--list]
cd download_model_data && bash lgm_tas.sh && cd ..   # ESGF wget scripts, {period}_{var}.sh
python process_paleo_observations.py                 # all observation sources
python process_paleo_models.py --model all --period all
python paleo_benchmark.py --model MIROC-ES2L --period lgm --picontrol-dir DIR
```

PI reference for anomalies: the lgmDA Holocene field, or the model's own piControl via
`--picontrol-dir` (required for the Bartlein MAP and Scussolini LIG pr benchmarks).
