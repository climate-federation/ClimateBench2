# ClimateBench2

ClimateBench v2 is a protocol for scoring and testing climate models and
emulators. A submission is evaluated in three tiers:

- **Tier I: physical consistency (pass/fail).** Gates on budget closure,
  diagnostic covariances, forced responses, coupled variability and coupled
  diagnostics. Each is tagged **Required** (passing every applicable one is
  the entry ticket to being scored), **Extended** (reported for additional
  credit) or **extra** (a sanity check outside the protocol).
- **Tier II: probabilistic scores against observations.** Each scorecard
  entry is the fair (Ferro) CRPS of the submission's ensemble against an
  observational product, reported as a skill `S = 1 − E/E_ref`, where
  `E_ref` is the median fair CRPS across the CMIP6 comparison models
  (leaving out any model with the submission's own name). Entries scored over
  the reserved post-2015 test window are labelled *held-out*; those scored
  over the historical record are labelled *in-sample*. Climatology and
  pattern-scaling baselines are reported alongside. Fair CRPS needs at least
  two members, so a single-member submission is reported as `n/a`.
- **Tier III: out-of-sample tests.** Paleoclimate time slices (LGM,
  mid-Holocene, Last Interglacial) scored with fair CRPS against proxy
  compilations, and perfect-model tests that score the submission against a
  held-out ESM run instead of observations.

## Relationship to ClimateEval

ClimateBench2 is a thin protocol layer on top of
[ClimateEval](https://github.com/climate-federation/ClimateEval). ClimateEval
owns data loading, preprocessing, regridding and the generic physical
diagnostics. ClimateBench2 owns the tier structure, thresholds, probabilistic
scoring, baselines and leaderboard. Its diagnostics subclass ClimateEval's,
its suites are ClimateEval suite YAMLs, and its results use ClimateEval's
DuckDB schema, so `climateeval report` works on them too.

## Install

Python ≥ 3.12. ClimateEval is pre-1.0 and not on PyPI. The recommended setup
is ClimateEval's pixi environment with this package installed into it:

```bash
git clone https://github.com/climate-federation/ClimateEval
cd ClimateEval && pixi install
pixi run pip install -e /path/to/ClimateBench2 --no-deps
```

A plain `pip install .` instead pulls the ClimateEval commit pinned in
`pyproject.toml`. Some suite entries use ClimateEval diagnostics from pull
requests not yet merged upstream, which that commit does not provide.

## Usage

```bash
# Score CMOR NetCDF output (file, flat directory or DRS tree); writes one DuckDB
# database per suite to MyModel_climatebench2/ (-o/--out to change)
climatebench2 score /path/to/model/Amon --name MyModel --data-root /path/to/staged

# Tier I needs auxiliary experiments (keys: picontrol, 4xco2, histaer, day,
# amip, amip4xco2, patch_ep, patch_wp); ensemble members are auto-discovered
# from a DRS tree or named with --member
climatebench2 score model/Amon --name MyModel \
    --experiment picontrol=DIR --experiment 4xco2=DIR --experiment histaer=DIR \
    --member r1i1p1f1=DIR --member r2i1p1f1=DIR

# Declare a gate the submission cannot meet by construction: recorded as n/a
climatebench2 score emulator_output --name MyEmu --not-applicable geostrophic_balance

# Tier III paleo slices against the processed proxy NetCDFs
climatebench2 score model/Amon --name MyModel --suite ClimateBench2_TierIII \
    --experiment picontrol=DIR --experiment lgm=DIR \
    --paleo-data-root paleo_scripts/paleo_data_cache/processed/observations

# Tier III perfect-model test: score against a held-out ESM run
climatebench2 score model/Amon --name MyModel --truth DIR --truth-name CESM2

# Build the leaderboard (static HTML; --csv for a flat summary)
climatebench2 leaderboard MyModel_climatebench2/*.ddb -o leaderboard.html
```

Other `score` options (`--help` lists all): `--suite` picks suites by name or
YAML path; `--timerange` overrides the cube suites' window; `--no-score`
skips the Tier II scoring pass; `--no-cache` re-loads the reference and
comparison ensemble for every member rather than once per diagnostic (same
results, slower); `--download` fetches missing references.

Tier II scoring is a pass over the finished databases
(`climatebench2.scoring_pass`), since fair CRPS needs every member; `score`
runs it at the end. A change to
`thresholds.yml` reaches existing databases without re-running the suites
through `leaderboard --rescore` (re-runs the Tier II pass) and
`leaderboard --regate` (re-evaluates every Tier I/III gate from its stored
raw statistic). Both are idempotent.

### Suites

Each suite gets the data shape and window the protocol asks for. The first
four run by default; ask for the other two with `--suite`.

| Suite | Input | Window | Per member |
|---|---|---|---|
| `ClimateBench2_TierI` | experiment dict (`historical` = the submission) | full record | no |
| `ClimateBench2_TierI_variability` | `picontrol` cubes (≥ 100 yr) | full record | no |
| `ClimateBench2_TierII_events` | experiment dict, each member's own `historical` | full record | yes |
| `ClimateBench2_TierII` | monthly cubes | post-2015 test window; time series loaded from 1985 for the 1985–2014 anomaly baseline | yes |
| `ClimateBench2_TierII_daily` | daily/hourly cubes (needs `day` output) | full record, cut to the staged reference's record where there is one | yes |
| `ClimateBench2_TierIII` | experiment dict (`picontrol` + paleo slices) | full record | no |

[climatebench2/suites/README.md](climatebench2/suites/README.md) lists what
each suite contains.

### Staged data

References and the CMIP6 comparison ensemble are read from `--data-root` in
ClimateEval's layout, `<data_root>/<data source id>/<frequency>/<variable>/*.nc`.
The comparison ensemble behind `E_ref` is every directory named
`CMIP6_<model>_historical-ssp245_<member>` under that root (concatenated
historical + SSP2-4.5 records; the protocol uses `i1p1` members of any
forcing index, at most ten per model). Set `CLIMATEBENCH2_STAGED_CMIP6_ROOT`
to discover the ensemble elsewhere (e.g. a small subset for a smoke run)
while still loading everything else from `--data-root`. Unstaged members or
variables are left out of the ensemble. The Tier III proxy targets are made by
[paleo_scripts/](paleo_scripts/README.md).

### Scoring without the comparison ensemble (frozen bundle)

Running the ensemble is most of a Tier II run and needs the staged pool.
None of its rows depends on the submission, so they can be copied out of a
finished live run once and reused
([climatebench2/frozen_comparison.py](climatebench2/frozen_comparison.py)):

```bash
# Once, from live result databases (split-suite databases are fine):
climatebench2 freeze-comparison run/*.ddb -o frozen_v2.0 --timerange 20150101/20251231
climatebench2 freeze-comparison ohc/*.ddb -o frozen_v2.0 --extend   # add a variable group later
# Then per submission, with only the submission and the references on disk:
climatebench2 score model/Amon --name MyModel --data-root refs \
    --comparison frozen --frozen-comparison frozen_v2.0
```

`--comparison frozen` drops the ensemble generators from every suite's
`other_data` (the reference and observational products stay), skips the
Tier I wrappers' upstream ensemble, and merges the bundle's rows into each
database before the scoring pass, so `E_ref` and the skill are the frozen
ensemble's. The run stops if the bundle was made under another protocol
(`thresholds.yml` fingerprint, version, Tier II window, which a frozen run
adopts when `--timerange` is not given) or a variable's resolved window
differs, and after the suites if a scored diagnostic has no frozen rows
(`--allow-missing-comparison` accepts an empty `E_ref` instead).
`--comparison none` scores without any `E_ref`. `--isolate-crashes` (on by
default with `frozen`/`none`) logs a crashing gate as `CB2_GATE_CRASH` and
carries on. Variables in `tier2.frozen_comparison.exclude_variables` (`ts`)
are left out of a bundle unless `--include-variable` keeps them.
`merge-comparison DIR DB...` merges a bundle into existing databases.

## Thresholds and specification

Every pass/fail bound, scoring constant, protocol window and gate tag
(`requirement:`, `tier:`) lives in
[climatebench2/thresholds.yml](climatebench2/thresholds.yml), read through
`climatebench2._thresholds.get_threshold`; no diagnostic hard-codes a bound.
[docs/metrics_reference.md](docs/metrics_reference.md) is the authoritative
specification of every check (inputs, preprocessing, formula, threshold);
where it and the paper disagree, the paper is correct.
[docs/climateeval_delineation_plan.md](docs/climateeval_delineation_plan.md)
describes the architecture and the ownership boundary with ClimateEval.

## Repository layout

```
climatebench2/           the installable package
├── diags/               protocol diagnostics (ClimateEval Diagnostic subclasses)
├── suites/              suite YAMLs for Tiers I, II and III
├── thresholds.yml       every bound, constant, requirement and tier tag
├── scoring.py           fair CRPS, bootstrap, consistency test, EOF projection
├── scoring_pass.py      post-suite Tier II pass: stack members, score, skill
├── regate.py            re-evaluate stored gate statistics against thresholds.yml
├── windows.py           the protocol's time windows
├── reference_windows.py the window each reference can actually support
├── physics.py           Tier I physics and ETCCDI indices (numpy only)
├── baselines.py         climatology pseudo-members, two-layer EBM
├── shared_sources.py    loads reference + comparison ensemble once per diagnostic
├── data/                staged CMIP6 ensemble, IMERG and MODIS sources, ERF table
├── leaderboard/         .ddb results to a static HTML scorecard
└── _cli.py              the climatebench2 command
docs/                    specification and architecture
paleo_scripts/           Tier III data preparation (download and process proxies)
analysis/                standalone studies behind paper figures; not protocol code
tests/                   test suite (see tests/README.md)
```

`analysis/` makes its own choices (e.g. a 1990–2020 anomaly reference) that
are not the protocol's; the package imports nothing from it.

## Tests

Run the tests serially (no `pytest -n`) in ClimateEval's pixi environment:

```bash
cd /path/to/ClimateEval
PYTHONPATH=/path/to/ClimateBench2 pixi run --frozen python -m pytest /path/to/ClimateBench2/tests -q
```

Every fixture is synthetic; no test reads real model output or uses the
network. See [tests/README.md](tests/README.md).

## What has run on real data

Tier I has been run on 64 CMIP6 models. Tier II has been run on 24
multi-member CMIP6 models against HadCRUT5, GPCP, CERES-EBAF, HadISST, MODIS
and IMERG. Several protocol constants are still provisional; they are listed
under "Decisions needed" in [docs/metrics_reference.md](docs/metrics_reference.md).

## Citing

Please cite the paper describing the protocol: Watson-Parris, D., et al.,
*Journal of Advances in Modeling Earth Systems* (in review). Full citation to follow.

## Licence

Apache License 2.0, see [LICENSE](LICENSE).
