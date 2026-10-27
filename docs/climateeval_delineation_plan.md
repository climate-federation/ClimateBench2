# ClimateBench2 ⟷ ClimateEval — Delineation Plan

> **Status (2026-09-20):** Phases 0–7 implemented — Phases 0–6 one commit per
> phase (`git log --oneline --grep "Phase"`), **Phase 7 (protocol
> re-alignment, §6 below)** in the ~20 commits of the metrics-reference gap
> list (`git log --oneline 0998a87..d21be52`) plus the consistency pass that
> followed it — and then the sixteen commits of the **first real-data runs**
> (`git log --oneline main..paper-figures-2026-09`). **333 tests pass** in
> ClimateEval's pixi env. The §7 upstream
> track has paid off:
> ClimateEval `main` (`b0e941c`) merged CB2's PR #35
> (`LandOceanWarmingRatio`, `ArcticAmplification`, `MeridionalHeatTransport`
> plus the `rsds`/`rsus`/`rlds`/`rlus`/`tasmax`/`tasmin` variables), **the pin
> in `pyproject.toml` is now `b0e941c`, and the three CB2 copies have been
> replaced by thin gate wrappers** (the `ECSGate` pattern: a
> `SupersetExperimentMixin`/`GateMixin` subclass of the upstream class that
> only feeds `thresholds.yml` constants into its kwargs and gates its output
> columns) — the first "retire as parity is reached" deletion. The full
> re-audit of the implementation against the 2026-09 paper draft — including
> what still blocks "run every suite against CMIP6" (fair CRPS, multi-member
> ingestion, a post-2015 multi-member CMIP6 reference generator in ClimateEval,
> entry-ticket tagging, the Tier III data contract) — is in
> [`metrics_reference.md`](metrics_reference.md), §"Prioritized gap list".
> **The `constants.py`/`utils.py`/`benchmark_scrips/` island is gone** (work
> package 7, 2026-09-14): `paleo_scripts/paleo_benchmark.py` takes a local
> `--picontrol-dir DIR` instead of `benchmark_utils.DataFinder`, the three
> helpers it still needed moved to `paleo_scripts/paleo_utils.py`, and
> `constants.py`, `utils.py`, `benchmark_scrips/` and `env.yml` were deleted —
> the last bullet of §9 below. Tier III is wired to the paleo pipeline's own
> NetCDFs and scores the paper's fair CRPS; the perfect-model half of Tier III
> exists as a code path (`score --truth DIR`) with no data staged.
> **The twelve ClimateEval PRs (#44–#55) are all merged LOCALLY** on the
> ClimateEval branch `cb2-integration` (`f8c925b` = `main` `b0e941c` + the
> twelve) **and all still open upstream** (§7), so **the pin in
> `pyproject.toml` stays at `b0e941c`** — an installed CB2 gets `main`, the
> real runs get the integration branch.
> **CB2 has now been run against real data:** Tier I end to end on CNRM-CM6-1
> (every Required gate produces a row) and Tier II on MPI-ESM1-2-LR against a
> staged 42-model / 160-member CMIP6 comparison ensemble, with the 64-model
> Tier I ensemble and the full Tier II comparison run in flight. Eight of the
> sixteen commits on this branch are "Real-data fix"; see "What has run on real
> data" in [`metrics_reference.md`](metrics_reference.md) for the numbers, the
> caveats and the four protocol questions the runs raised.

**Purpose.** Turn ClimateBench2 (CB2) into a *thin protocol layer* that runs on top of
[ClimateEval](https://github.com/climate-federation/ClimateEval), so CB2 owns only the
scientific protocol (which tests, which thresholds, how to score) and a leaderboard —
not the data-loading / preprocessing / diagnostic-computation machinery. The goal is to
**minimise the code CB2 must maintain into the future**.

This supersedes the ICONEval integration plan (`iconeval_integration_plan.md`, removed
2026-07-12 — see git history): the old plan was to port ICONEval *recipes* into a
hand-rolled ESMValTool runner inside CB2.
ClimateEval (built by the same DLR/Eyring group, derived from ICONEval) is now a
nearly-complete, pip-installable framework that already does that job, so CB2 depends on
it rather than re-implementing it.

**Authoritative protocol reference.** [`metrics_reference.md`](metrics_reference.md) —
the exact spec (inputs, preprocessing, formula, threshold/score) for every Tier I/II/III
diagnostic. That doc defines *what* CB2 must produce; this doc defines *where each piece
lives* and *how CB2 calls ClimateEval* to produce it.

---

## 1. Decisions taken (2026-07-12)

| Decision | Choice | Consequence |
|---|---|---|
| Where the scoring layer lives (pass/fail thresholds, fair CRPS, ensemble-consistency, baselines) | **CB2 custom diagnostics (plug-in) + a CB2 post-suite pass** | CB2 ships `Diagnostic` subclasses that plug into ClimateEval's suite/DataSource/report framework by class-path reference. The Tier II *probabilistic* score cannot be one of them — ensemble members arrive as separate data sources, so it is a pass over the finished DuckDBs (`scoring_pass.py`) that appends rows to each diagnostic's own `metrics` table. CB2 owns the protocol; ClimateEval does all I/O + preprocessing. |
| Leaderboard presentation | **New thin CB2 leaderboard** | A minimal standalone page generated from the scores table — not ClimateEval's report, not the legacy `ClimateBench_app`. |
| Fate of bespoke `benchmark_scrips/` | **Retire as ClimateEval reaches parity** — *completed 2026-09-14* | Each bespoke script was deleted only once a CB2-diagnostic-on-ClimateEval covered it. The last of them went with work package 7; there is no coverage gap and no legacy directory left. |
| Relationship to ClimateEval | **Third-party dependency** (authors: M. Schlund, A. Paçal; `climate-federation` org — not us) | CB2 consumes it via pixi/pip and contributes via PRs. CB2 must **not** assume it can refactor ClimateEval internals; anything CB2-specific stays in CB2 unless upstream accepts it. |

---

## 2. Ownership boundary

**ClimateEval owns (CB2 never re-implements):**
- Data sources & auto-download — `ERA5*`, `GPCP`, `CERESEBAF`, `HadCRUT5`, `HadISST`,
  `ESACCISST`, `NOAAERSSTv5`, `ORAS5`, `EN4`, `WOA`, `IAP`, `OSI450NH/SH`, `NSIDC…`,
  `CMIP6Historical/Abrupt4xCO2/PiControl…`, plus generic ESMValTool intake/CMORizer.
- Preprocessing — unit conversion, regridding, longitude normalisation, time-range
  extraction, level extraction, land/sea + AR6-region masking (ESMValCore).
- The `Suite` runner, DuckDB/Ibis output schema, and the base `Diagnostic` framework
  (`SimpleDiagnostic`, `ComplexDiagnostic`, `ComplexDataSource`, `DiagnosticOutput`).
- Deterministic metrics (`weighted_rmse`, `weighted_pearsonr`, `weighted_emd`) and the
  library of physical diagnostics (`Map`, `AnnualCycle`, `Nino34`, `ECS`,
  `OceanHeatContentTimeSeries`, `SeaIceArea*`, `AMOCTimeSeries`, `Hovmoller`, `QBO`,
  `DiurnalCycle`, `Histogram`, `TEMDiagnostic`, …).

**CB2 owns (the protocol):**
- **The protocol definition** — which diagnostics constitute Tier I/II/III, in what
  configuration, against which references, with which thresholds. Encoded as CB2 suite
  YAMLs + a thresholds/config file.
- **The scoring semantics ClimateEval lacks**, implemented as ClimateEval-compatible
  `Diagnostic` subclasses (§4):
  - Tier I **pass/fail** wrappers (turn a computed scalar into a binary against a paper
    threshold).
  - Tier II **fair (Ferro) CRPS with effective-sample-size (autocorrelation)
    correction** (regime a), and the same score on the reference's fixed pre-2015 EOF
    basis for spatial fields (regime b).
  - Tier II **ensemble-consistency test** (regime **c** — renumbered 2026-09, this
    bullet used to say (b)): piControl-chunk internal variance + obs-error quadrature +
    two-sided *p* < 0.05.
  - The three **baselines** (**1985–2014** climatology persistence — *not* 1990–2020,
    which overlapped the reserved test window; 2-layer-EBM × CMIP6-MMM pattern scaling;
    CMIP6 MME) as pseudo-"model" submissions run through the same suites. (As built:
    the pooled `CMIP6-MME` CRPS row was replaced by the **median of the per-model**
    fair CRPS with leave-one-out — see `metrics_reference.md` discrepancy #10.)
  - Physics ClimateEval has no diagnostic for but which are computable from its
    DataSources (land–ocean warming ratio, Arctic amplification, aerosol hist-aer ERF,
    meridional heat transport partitioning, ITCZ–EFE, geostrophic balance, MJO WK ratio,
    amip-4xCO2 ERF, GFMIP Δλ).
  - Tier III paleo proxy-aware scoring + mid-Holocene monsoon check, and the
    perfect-model / large-ensemble-spread suite.
- **The leaderboard** and the CB2 web presentation.
- **The paper / protocol governance.**

**One-line test for "does this belong in CB2?":** if it loads or regrids data, or is a
generic physical diagnostic → ClimateEval. If it encodes a threshold, a probabilistic
score, a baseline, a tier structure, or the leaderboard → CB2.

---

## 3. Integration mechanism (verified)

CB2 becomes an installable package `climatebench2` that **depends on** `climateeval` in
the same environment. The wiring, all confirmed against ClimateEval `main`:

1. **Custom diagnostics load by class path.** `Suite._get_diagnostics`
   (`suites/_base.py`) resolves the YAML `diagnostic:` string via
   `str_to_object` → `importlib.import_module` (`_utils.py:152`). It accepts **any**
   importable dotted path, requiring only `issubclass(cls, Diagnostic)`. So a CB2 suite
   can say `diagnostic: climatebench2.diags.EnsembleConsistency` and ClimateEval runs it.
2. **Auxiliary experiments / ensembles are a supported pattern.** The `ECS`
   `ComplexDiagnostic` declares `_required_data_keys = ("4xco2", "picontrol")` and pulls
   `CMIP6Abrupt4xCO2R1I1P1F1` + `CMIP6PiControlR1I1P1F1` via `ComplexDataSource`, emitting
   scalar `fx` outputs (`ecs`, `lambda`, `p_value`, …). This is exactly the shape CB2's
   pass/fail and ensemble-consistency diagnostics need (piControl chunks, multiple
   members, a4x/hist-aer). CB2 subclasses the same bases — **no bespoke data loading.**
3. **Output is uniform.** Every CB2 diagnostic writes to the standard DuckDB schema
   (`raw_output`, `metrics`, `variables`, `data_sources`). CB2's leaderboard reads that
   one schema regardless of which diagnostic produced a score.
4. **Run path.** CB2 exposes `climatebench2 score <model_dir>` (thin CLI) →
   `Suite("ClimateBench2_TierI", …).get_database(...)` for each tier → one `.ddb` →
   CB2 leaderboard renderer. No ClimateEval source is modified. A small registry in
   `_cli.py` (`SUITE_REGISTRY`) decides what each suite is handed — experiment dict vs
   cubes, which experiment, which window — so the protocol's windows (piControl in full
   for the variability gates, the post-2015 test window for Tier II) live in CB2 and not
   in a global `--timerange`; `Suite.get_database(append=True)` accumulates one run per
   ensemble member into the same database. Protocol standing is
   carried in the data, not the code path: each gate's `requirement` tag
   (`thresholds.yml`) and its `applicable` flag (`score --not-applicable NAME`) travel
   as `metrics` columns, so the leaderboard can compute the entry ticket over the
   Required group alone.

---

## 4. CB2 repository layout (as built, 2026-09-20)

`tree climatebench2 -I __pycache__`, annotated:

```
climatebench2/                 # the installable package
├── diags/                     # CB2 Diagnostic subclasses (plug into ClimateEval)
│   ├── pass_fail.py           #   the gate machinery: GateCheck, requirement tags, ENSO gates
│   ├── tier1_physics.py       #   CB2ComplexDiagnostic + energy/closure/covariance/forcing gates
│   ├── tier1_extended.py      #   geostrophic balance, MJO, ITCZ-EFE, Bjerknes, precip-buoyancy, teleconnections
│   ├── tier2_scores.py        #   the Scored* series subclasses + InternalVariability (σ_int)
│   ├── tier2_reference.py     #   ReferenceBaselineRecord / ReferenceEOFProjection (reach past the test cut)
│   ├── tier2_diagnostics.py   #   aggregated scalars: warming level, Pinatubo, asymmetry, seasonal cycle
│   ├── tier2_daily.py         #   ETCCDI extremes, Perkins skill, diurnal harmonic
│   ├── tier3_paleo.py         #   PaleoProxyScore + mid-Holocene monsoon gate
│   └── truth_reference.py     #   LocalCMORReference — the perfect-model reference (upstream candidate, PR #55)
├── suites/                    # CB2-owned suite YAMLs (climatebench2.diags.* + climateeval.diags.*)
│   ├── ClimateBench2_TierI.yml             ClimateBench2_TierI_variability.yml
│   ├── ClimateBench2_TierII.yml            ClimateBench2_TierII_daily.yml
│   ├── ClimateBench2_TierII_events.yml     ClimateBench2_TierIII.yml
│   └── README.md              #   what each suite holds, its data shape and its window
├── thresholds.yml             # single source of truth for every bound, requirement and tier tag
├── _thresholds.py             # get_threshold("tier1.ecs.range") — the only reader of the above
├── scoring.py                 # the engine (numpy only): fair CRPS, ESS, block bootstrap, EOF, proxy CRPS, Perkins
├── scoring_pass.py            # post-suite pass: stack members by name -> fair CRPS -> skill vs the CMIP6 median
├── windows.py                 # every window the protocol names, resolved in one place
├── reference_windows.py       # the window each variable is REALLY scored over: the protocol
│                              #   window clipped to the staged reference's complete years
├── physics.py                 # pure Tier I physics (numpy only)
├── baselines.py               # climatology pseudo-members; calibrated two-layer EBM + its pseudo-members
├── data/                      # packaged protocol tables (erf_ar6_ssp245.csv — the EBM's forcing)
│   └── cmip6_staged.py        #   + StagedCMIP6HistoricalSSP245 — the CMIP6 comparison
│                              #     ensemble behind E_ref, enumerated in a staged root
├── leaderboard/               # thin renderer: .ddb -> scores table -> static HTML page
└── _cli.py                    # `climatebench2 score` / `climatebench2 leaderboard`; SUITE_REGISTRY
pyproject.toml                 # pins climateeval by commit; entry point climatebench2 = climatebench2._cli:main
docs/                          # this plan, metrics_reference.md
paleo_scripts/                 # Tier III data pipeline (download/process/benchmark) — data prep, not protocol
analysis/                      # standalone exploratory studies behind paper figures (not part of the protocol)
tests/                         # engine + diagnostics + suite-integration tests
```

Nothing else remains: `benchmark_scrips/`, `download_scripts/`, `constants.py`,
`utils.py`, `esmvaltool/`, `env.yml` and `archive/` were all deleted by the end of
work package 7 (§9). Two deliberate differences from the original target layout:
`scoring.py` is a flat pure-numpy module rather than `diags/_scoring.py` (nothing in
it touches iris), and `baselines.py` sits beside it rather than under `diags/`, because
the baselines are consumed by the post-suite pass and are not `Diagnostic`s.

---

## 5. Protocol → provider map

From [`metrics_reference.md`](metrics_reference.md). "CE" = ClimateEval already provides
the physical diagnostic (CB2 adds only scoring config/threshold); "CB2-diag" = new CB2
`Diagnostic` subclass built on ClimateEval DataSources; "CB2-engine" = needs the §4
scoring primitives.

> **This table is the 2026-07 plan, kept for the provider split it records — not a
> status table.** Two things have moved since: the regimes were renumbered in 2026-09
> ((a) series fair CRPS, (b) spatial fields on the reference's fixed pre-2015 EOF basis,
> (c) the ensemble-consistency test — so "regime b" below means today's (c) wherever it
> labels a consistency test), and every item has since been built. The status of record
> is the Tier I/II/III summary tables in
> [`metrics_reference.md`](metrics_reference.md).

### Tier I (pass/fail entry ticket)
| Check | ClimateEval has | CB2 builds |
|---|---|---|
| I.1 Energy balance (piControl) | `Tier1_sanity_checks` (flux ranges) | pass/fail wrapper: \|μ(N)\|<0.1, drift of 10-yr mean <0.02 |
| I.2 Closure (water / atm energy) | `Tier1_consistency_checks` | absolute-bound pass/fail wrappers |
| I.3a Clear-sky LW β | — (has CERES + tas) | CB2-diag: gridpoint ∂rlutcs/∂Ts, ±25% of 2.2 |
| I.3b Geostrophic balance | — (needs daily ua/zg) | CB2-diag (daily data path) |
| I.3c Precip–buoyancy | — | CB2-diag: P′ vs ⟨MSE⟩′, ±30% of obs |
| I.4a GFMIP patch Δλ | — (non-CMIP exp) | CB2-diag + submission data path |
| I.4b amip-4xCO2 ERF | — | CB2-diag |
| I.5a–c ENSO | `Nino34` diagnostic | CB2-diag: σ, band-power ratio, teleconnection regressions |
| I.5d MJO WK ratio | `Tier3_dynamics` (Hovmöller/MJO, daily pr) | CB2-diag: east/west power ratio |
| I.6a Land–ocean ratio | — (a4x data path via ECS exists) | CB2-diag |
| I.6b Arctic amplification | — (a4x) | CB2-diag |
| I.6c ECS ∈ [1,7] | **`Tier3_ecs` / `ECS`** (emits ecs, p_value) | pass/fail wrapper only (near-free) |
| I.7 Aerosol hist-aer ERF | — (hist-aer) | CB2-diag |
| I.8a MHT partitioning | — | CB2-diag |
| I.8b ITCZ–EFE | — | CB2-diag |

### Tier II (probabilistic scoring vs post-2015 obs)
| Family | ClimateEval has | CB2 builds |
|---|---|---|
| Mean-state tas/pr/TOA/clouds | `Tier2_atmosphere_monthly` (Map/AnnualCycle/ZonalLine + ERA5/GPCP/CERES) | **CB2-engine** scoring on top (regime a CRPS-ESS / regime b consistency) |
| SST/salinity/OHC/AMOC/Niño | `Tier2_ocean_monthly`, `OceanHeatContentTimeSeries` | CB2-engine scoring |
| Sea ice extent/min/trend | `Tier2_sea_ice_monthly` | CB2-engine (regime b) |
| Diurnal cycle, pr intensity PDF | `Tier3_atmosphere_subdaily` (`DiurnalCycle`, `Histogram`) | CB2-engine scoring |
| tas daily extremes | — (needs daily tasmax/tasmin) | CB2-diag + engine |
| GMST trend, Pinatubo, hemispheric asymmetry, seasonal-cycle triplet | partial (timeseries) | CB2-diag + engine (regime b) |
| Baselines (climatology / EBM×MMM / MME) | — | CB2 baselines (run through the same suites) |
| CRPS-ESS + ensemble-consistency + EOF | — (deterministic only) | **CB2-engine — the blocking core build** |

### Tier III (paleo + perfect-model)
| Item | ClimateEval has | CB2 builds |
|---|---|---|
| lig127k / lgm / midHolocene vs proxies | — | CB2-diag (proxy-aware, reuses CB2-engine regime b) |
| mid-Holocene N-Africa JJAS monsoon | — | small CB2-diag |
| Perfect-model (CESM2 train→SSP245) | — | CB2 suite reusing Tier II engine |
| Large-ensemble spread vs CESM-LE | — | CB2-diag (variance ratio + pattern corr) |

**Blocking dependency:** the CB2-engine (CRPS-ESS + ensemble-consistency) gates every
Tier II/III aggregated score. Build it first (§6 Phase 2).

---

## 6. Migration — phased, retire-as-parity

Each phase ends with an explicit **delete** so the legacy surface only shrinks.

**Phase 0 — Scaffold (no science).** Create the `climatebench2` package + `pyproject.toml`
depending on `climateeval`; stand up an empty `diags/`, `suites/`, `thresholds.yml`,
`_cli.py`. Get `climatebench2 score` to run a *stock* ClimateEval `Tier2_atmosphere_monthly`
end-to-end on one model and emit a `.ddb`. *Deletes: nothing yet.*

**Phase 1 — Wrap the overlap (cheap wins).** CB2 suites reference existing ClimateEval
diagnostics for everything already covered: ECS (+[1,7] pass wrapper), Niño-3.4, mean-state
Tier II families, sea ice, diurnal/histogram. Populate `thresholds.yml` from
`metrics_reference.md`. *Deletes: `ecs_benchmark.py`, `enso_benchmark.py`, and the
`model_benchmark.py` deterministic path once the Tier II suite reproduces them.*

**Phase 2 — Build the scoring engine (blocking core).** Implement `diags/_scoring.py`:
CRPS-with-ESS and the ensemble-consistency test (piControl-chunk σ + obs-error quadrature +
EOF projection, two-sided p<0.05) as `ComplexDiagnostic`s. Wire them over the Phase-1
mean-state suites. This is the single most valuable new build; nothing in CB2 or ClimateEval
provides it. *Deleted: the raw-CRPS paths in `benchmark_utils.py`* (and the file
itself, with work package 7).

**Phase 3 — Port bespoke Tier I physics to CB2-diags.** Reimplement land–ocean, Arctic,
aerosol ERF, MHT, ITCZ–EFE (fixing the hard-coded `np.ones((1980,1))` bug), Bjerknes as
`ComplexDiagnostic`s on ClimateEval's a4x/piControl/hist-aer DataSources — align thresholds
to the paper as you port. *Deletes each `*_benchmark.py` as its CB2-diag lands and matches.*

**Phase 4 — New Tier I/II diagnostics (daily + causal).** Geostrophic balance, MJO WK,
amip-4xCO2 ERF, GFMIP Δλ, tas extremes, pr PDF, GMST trends, Pinatubo, hemispheric
asymmetry, seasonal-cycle triplet, and the three baselines. Requires daily-data variables
(ClimateEval's subdaily suite shows the path). *Deleted: `download_scripts/` here;
`constants.py`/`utils.py` survived until the last paleo import went with work
package 7 (Phase 7 below).*

**Phase 5 — Tier III.** Proxy-aware paleo scoring + mid-Holocene monsoon check
(coordinate with the open `paleo_data` branch — don't duplicate its tas-only work); then
the perfect-model + large-ensemble-spread suite. *Deleted: `app_data_prep/` +
`esmvaltool/recipe_pr_rmse.yml` + `_to_delete_git_litter/`.* `paleo_scripts/` is
**kept**: it is the data-preparation pipeline Tier III reads its proxy NetCDFs from,
and it retires only when a `PMIP4Proxies` CMORizer DataSource exists upstream.
*Done 2026-09-14 (work package 7)* except the data: the proxy scoring reads the
pipeline's NetCDFs, the perfect-model path swaps the Tier II reference for a held-out
ESM run, and the legacy island is deleted — but no PMIP4-proxy DataSource exists
upstream (PR #45 is the model half) and no CESM2/MPI-ESM/GISS-E2/CESM-LE output is
staged.

**Phase 6 — Leaderboard + docs.** `leaderboard/` renderer (`.ddb` → tiered scores table →
static HTML: Tier I gate pass/fail, Tier II/III scores vs the three baselines). Rewrite
`README.md`/`CLAUDE.md` to the thin-wrapper reality. *Deletes: anything left in the repo
root that predates the package.*

**Phase 7 — protocol re-alignment (2026-09-14).** Phases 0–6 built the *structure*;
Phase 7 re-audited it line by line against the 2026-09 JAMES draft and closed the
resulting "prioritized gap list" (`metrics_reference.md`) in ~20 commits,
`0998a87..d21be52`. It deletes nothing from the legacy surface except the last of it,
and adds no machinery ClimateEval could own. One paragraph per work package:

- **Gap item 0 — sync with ClimateEval `main`** (`b7e8593`). Pin bumped
  `4de03ed → b0e941c`, which had merged CB2's upstream PR #35. The three duplicated
  physics diagnostics became thin `SupersetExperimentMixin`/`GateMixin` wrappers over
  `climateeval.diags.complex.{LandOceanWarmingRatio, ArcticAmplification,
  MeridionalHeatTransport}` on the `ECSGate` pattern — the first "retire as parity is
  reached" deletion of §7 — and the CB2-side registry stopgap
  (`_MISSING_FROM_REGISTRY`/`RegistryFreeVariable`) went with them.
- **Gap item 1 — correct the gates that were wrong as written** (`a030496`). Six
  arithmetic/window defects: the I.2b energy identity, I.1's last-100-year evaluation
  slice, I.3a's monthly rather than annual anomalies, I.5a's unsmoothed index, I.5b's
  *integrated* band power, and the Tier II climatology baseline moved off the
  test-overlapping 1990–2020 window to 1985–2014.
- **Gap item 2 — entry-ticket semantics** (`5d600d3`). Every gate block in
  `thresholds.yml` carries a `requirement:` tag (required / extended / extra /
  diagnostic) read through one helper; `requirement` and `applicable` became `metrics`
  columns; `score --not-applicable NAME` lets a submission *declare* a check
  inapplicable, which is distinct from one that never ran; and the leaderboard computes
  the entry ticket (✓ / ✗ / ⚠) over the Required group alone.
- **Gap item 3 — the scoring engine to spec** (`a4c5948`, `732e860`, docs `f4bc072`;
  second half `519ef72`, `a3ff255`, docs `ba3b7ac`). Ferro's **fair** CRPS replaced the
  empirical estimator and raises for M < 2; observational-uncertainty draws and a
  moving-block bootstrap landed; and the scoring layer moved out of the diagnostics
  into **`scoring_pass.py`**, a pass over the finished DuckDBs — the only place that
  has a model's members grouped. The second half added regime (b) as fair CRPS on the
  reference's fixed pre-2015 EOF basis, σ_int from piControl chunks, a real σ_obs
  (protocol constants plus the measured inter-product spread, with observational
  products no longer ranked as models), and one label per model across the tiers.
- **Gap item 4 — per-suite data paths** (`a4cca38`). `_cli.SUITE_REGISTRY` gives every
  suite the data *shape* and *window* the protocol asks for instead of one global
  `--timerange`, and `--member LABEL=PATH` (with DRS auto-discovery) ingests several
  ensemble members into one database per suite.
- **Gap item 5 — the re-specced Tier I physics** (`bbf7d43`, `8f8a36a`, docs
  `383d0f3`). I.5c became the paper's pattern-correlation test against HadISST/GPCP,
  I.3c (precipitation–buoyancy) exists at last, and I.7 uses the decadal window centred
  on 2015 with parallel-piControl-segment drift removal — so Tier I now has a
  diagnostic for **every** row of the paper's Table 1, and two of them fetch
  observations through ClimateEval DataSources.
- **Gap item 6 — the missing Tier II diagnostics** (6a: `5185c72`, `9d4b7a9`, docs
  `69a1777`; 6b: `ce509c3`, `d8df85b`, `74a4841`, docs `440e4c8`). The §II.1 scalars
  (realized warming level, both GMST trends, scored Pinatubo and hemispheric-asymmetry
  magnitudes) with a generic **aggregated-scalar** regime and held-out/in-sample
  labelling; then the eight ETCCDI **extremes**, the **Perkins** PDF skill, the three
  **seasonal-cycle** metrics, the **diurnal** first harmonic, and the ERF-driven
  **pattern-scaling** baseline.
- **Gap item 7 — Tier III** (`f1fbdfe`, `f39a9f2`, `8cfa36f`, `d21be52`; raw proxy
  targets `393b6ef`). `PaleoProxyScore` reads the paleo pipeline's own per-dataset
  NetCDFs and scores the paper's primary statistic — the fair CRPS of a block
  pseudo-ensemble with σ_proxy as the observational variance term — keeping the
  site-consistency fraction beside it; Appendix D's scored/reported split is enforced
  by `dataset_type`; every gate gained a protocol `tier` tag; the perfect-model path
  (`score --truth DIR`) and the large-ensemble spread test exist but have no data; and
  the `constants.py`/`utils.py`/`benchmark_scrips/`/`env.yml` island was **deleted**.

*Deletes: the last legacy files in the repository root.* What Phase 7 did **not** do is
run any of it on real data — see "What is left to run against CMIP6" at the end of
`metrics_reference.md`.

---

## 7. Upstream-contribution track (optional, further shrinks CB2)

Offer to ClimateEval as PRs (they built ICONEval and welcome ESMValTool-idiomatic
diagnostics). If accepted, CB2 drops the code and just references the class path:
- Generic physical diagnostics with no CB2-specific threshold — MHT partitioning,
  ITCZ–EFE, geostrophic balance, MJO WK ratio, land–ocean & Arctic ratios. These are
  reusable evaluation diagnostics, not protocol.
- **Keep in CB2 regardless** (protocol/opinion, unlikely to be upstream-appropriate):
  the pass/fail thresholds, the CRPS-ESS + ensemble-consistency *scoring* semantics, the
  baselines, the tier structure, the leaderboard. `metrics_reference.md` §"ICONEval is
  deterministic only" already flags that probabilistic scoring stays bespoke.

Decouple this track from the migration: CB2 works whether or not any PR merges.

**Status 2026-09-20 — twelve PRs, all merged on `cb2-integration`, all still open
upstream** (`https://github.com/climate-federation/ClimateEval/pull/N`). The integration
branch is `f8c925b` = `main` `b0e941c` + the twelve, plus two follow-up fixes
(coordinate metadata reconciled before concatenating time-split files; a suppressed
warning for two timeless fields sharing a name). That is what the real runs use. **The
pin in `pyproject.toml` stays at `b0e941c`**, because a pin may only name a commit that
exists upstream — so an installed CB2 gets `main` and CB2 must keep working against it.
Merge order mattered in one place and was honoured locally: **#44 before #45** (they add
generators to the same module — an additive conflict), and **#49 before #53**. GitHub
Actions did not trigger on any of them, so each was validated only by local `pixi` tests
plus `ruff`/pre-commit — worth saying explicitly when chasing review, though all twelve
are now also exercised by the real runs.

**Merged is not staged.** Several of these add a DataSource whose data is not in the
staged root, so the CB2-side gap they were opened to close is still open: ERA5 `zg`/`ts`
(#48), the other GMST products and CRU TS (#49), HadEX3 and ESACCI sea ice (#53),
`ERA5Daily` (#54) and the PMIP4 generators (#45) all have nothing to read yet.

| PR | What it adds upstream | CB2 category | Taken up? |
|---|---|---|---|
| #44 | `CMIP6HistoricalSSP245` multi-member generator (+ `CMIP6HistoricalAllMembers`) | data source | **No, deliberately** — CB2 uses `climatebench2.data.StagedCMIP6HistoricalSSP245`, which enumerates a staged root instead of globbing the pool per (diagnostic, variable), keeps f2/f3 models, caps members and never reaches the network, and emits identical data-source ids |
| #45 | hist-aer / amip / amip-4xCO2 / lgm / midHolocene / lig127k generators | data source | Merged, **no pool staged** |
| #46 | `Nino34` accepting `window_length = 1` | diagnostic fix | **Yes** — `ENSOGate._preprocess` deleted (`224a133`) |
| #47 | `SeaIceExtent*` diagnostics (15 % threshold) | generic diagnostic | **Yes** — the sea-ice stanzas score extent (`b9f4952`) |
| #48 | ERA5 `zg` / `ts` | data source | Merged, **not staged** (CDS download); Tier II no longer scores `ts` (dropped 2026-10-27), I.3c keeps its hydrostatic fallback |
| #49 | GISTEMP / BerkeleyEarth / NOAAGlobalTemp / CRU sources (+ the `tasa` variable) | data source | Merged, **not staged** |
| #50 | Tier I energy-budget/covariance diagnostics — `EnergyBalance`, `BudgetClosure`, `ClearSkyLongwaveFeedback`, `PrecipitationBuoyancy`, `ClausiusClapeyronScaling` | generic physics **out of CB2** | Merged; CB2 copies **not retired** (needs the pin, needs the upstream merge) |
| #51 | dynamics/variability — `GeostrophicBalance`, `MJOEastWestPowerRatio`, `ITCZEnergyFluxEquator`, `BjerknesCompensation`, `ENSOTeleconnectionPatterns` | generic physics **out of CB2** | Same. ⚠ CB2's I.3b/I.3c were fixed on 2026-09-20 *after* this PR was opened; carry those fixes upstream first |
| #52 | forcing — `Amip4xCO2ERF`, `GFMIPPatchFeedback`, `AerosolERF` + a branch-time helper | generic physics **out of CB2** | Same |
| #53 | HadEX3 + ESACCISeaIceNH/SH (depends on #49) | data source | Merged, **not staged** — the ETCCDI extremes stay unscored |
| #54 | `ERA5Daily` | data source | Merged, **not staged** |
| #55 | `LocalCMORDataSource` | data source | Merged; `diags/truth_reference.py` **not retired** (same pin argument) |

#50–#52 are the substance of this section: they are exactly the "generic physical
diagnostics with no CB2-specific threshold" listed above, and when they merge
**upstream** CB2 deletes its copies in `diags/tier1_physics.py` and
`diags/tier1_extended.py` and keeps only `GateMixin` wrappers, as it already does for
ECS. Until then the copies stay, because the deletion needs a pin bump and the pin can
only name an upstream commit. `metrics_reference.md` §"Implementation synchronisation"
carries the per-PR table of what each one unblocks and what CB2 did with it.

---

## 8. Risks & open items

1. **Ensemble access in ClimateEval's data model.** *Resolved differently than
   expected.* The consistency test (today's regime (c)) needs *many* members +
   piControl chunks in one diagnostic — and the answer turned out to be that **it
   cannot**: `ComplexDataSource` + `_required_data_keys` delivers auxiliary
   *experiments* (the ECS precedent) but not an N-member ensemble, because each member
   arrives as its own data source. Hence `scoring_pass.py`, a pass over the finished
   DuckDBs, which is the only place a model's members are grouped (decision row 1 of
   §1).
2. **DuckDB schema for probabilistic scores.** The current schema is metric-per-row
   (rmse/pearson/emd). CB2 scores add *score + p-value + pass-flag + baseline-relative +
   provenance*. Extend via extra columns in CB2 diagnostics' `metrics` table (no
   ClimateEval change) and teach the CB2 leaderboard to read them.
3. **Daily / non-CMIP data paths.** *Half resolved, 2026-09-20.* Geostrophic balance
   (I.3b) has now run on a real `day`-table DRS tree — CNRM-CM6-1, ρ = 0.985 — and cost
   two fixes on the way: daily variables are **not** published over the same period per
   model (`ua` 1990–2014 vs `zg` 2000–2014, so cubes must be paired on calendar dates,
   never by position) and a masked pressure level read with `np.asarray` exposes the
   file's 1e20 fill value. Still unvalidated: the MJO gate (needs daily `pr`), the
   extremes and pr PDF (no daily observational product staged), and the memory footprint
   of many decades of daily 3-D fields. GFMIP and perfect-model still need
   submission-provided non-archive output, and none is staged.
4. **Version pinning.** Pin `climateeval` (it's `0.0.1`, pre-1.0, API may move). Track a
   known-good commit in `pyproject.toml`; CI runs CB2 suites against the pinned version.
   *Live tension, 2026-09-20:* the pin is `b0e941c` (`main`) while the real runs use
   `cb2-integration` (`f8c925b`), because the twelve PRs are merged locally and open
   upstream. Everything CB2 does must therefore keep working against `main`: the Tier II
   comparison ensemble is CB2's own `StagedCMIP6HistoricalSSP245` rather than upstream's
   #44 generator, and the #50–#52 physics copies cannot be deleted yet.
5. **`metrics_reference.md` unresolved specs** are paper decisions, not code — resolve in
   the protocol, then encode in `thresholds.yml`. The 2026-09-20 runs added four
   (metrics_reference "Decisions needed from Duncan", group E): I.3a's interannual
   gridpoint slope cannot reach the paper's ±25 % window, I.1's 0.1 W m⁻² bound will fail
   most CMIP6 models and the 2° regrid biases the global mean by +0.05, the AMET
   peak-latitude window is about one grid cell wide, and I.3c's ±30 % is a tight bound on
   a statistic with r ≈ 0.1 whose two sides are computed over different decades. Bjerknes
   and C–C are still absent from the paper's Tier I list (tagged `extra` in the meantime).

---

## 9. Definition of done

Each bullet marked against the tree at `52d2e50` (2026-09-20).

- ✅ **Met.** `pip install climatebench2` (with `climateeval`) →
  `climatebench2 score <model>` runs all three tiers and writes one `.ddb`;
  `climatebench2 leaderboard *.ddb` renders the page. *The CLI, the packaging and all
  six suites are verified (every suite YAML, `thresholds.yml` and `data/*.csv` resolve
  through `importlib.resources`; `tests/test_suites.py` builds every diagnostic of
  every suite in `SUITE_REGISTRY`), and since 2026-09-20 the end-to-end run has been
  exercised on **real** CMIP6 and observational data: Tier I on CNRM-CM6-1 and Tier II
  on MPI-ESM1-2-LR against a staged 42-model / 160-member comparison ensemble. The
  test fixtures are still synthetic, so the suite alone still proves nothing about a
  real run — but eight of them are now regressions from bugs the real runs found.*
- 🟡 **Partly met.** No data-loading, regridding, or generic-diagnostic code remains in
  CB2 — only `diags/` (thresholds + scoring), `suites/`, `thresholds.yml`,
  `leaderboard/`, docs. *No CB2 code loads or regrids **model** data: everything enters
  through `climateeval._loader.load_cmor_dir` and ClimateEval DataSources. Three
  exceptions, each declared: `diags/truth_reference.py` (upstream candidate, PR #55 —
  merged upstream-locally, not yet retired here because that needs the pin);
  `data/cmip6_staged.py`, which enumerates a staged directory tree to build the Tier II
  comparison ensemble and is deliberately CB2's rather than upstream's (§7, #44); and
  `reference_windows.py`, which reads NetCDF **headers** — not data — to find out what
  window a reference can actually support. Generic **physics** does still live here —
  the diagnostics of upstream PRs #50–#52 — and leaves when those merge upstream (§7).
  `paleo_scripts/` reads NetCDFs directly, but it is data preparation, not protocol.*
- ✅ **Met.** `benchmark_scrips/`, `download_scripts/`, `constants.py`, `utils.py`,
  `esmvaltool/`, `_to_delete_git_litter/` are gone — the last four with **work
  package 7** (2026-09-14), together with `env.yml`, the legacy conda
  environment that existed only for them. What remains outside `climatebench2/`
  is the **paleo pipeline** (`paleo_scripts/`), which is data preparation, not
  protocol: it downloads and processes the proxy compilations that
  `climatebench2/diags/tier3_paleo.py` then scores against. Its three
  general-purpose helpers live in `paleo_scripts/paleo_utils.py` and depend on
  nothing outside xarray/pandas.
- ✅ **Met.** Every Tier I/II/III row in `metrics_reference.md` is either a CB2
  diagnostic or an explicitly-tracked gap — nothing silently dropped. Tier I now has a
  diagnostic for all 16 Table-1 rows (18 sub-checks); every Tier II and Tier III row
  carries a status, and the unscoreable ones write a machine-readable `reason` rather
  than going quiet. The audit trail is the discrepancy list (#1–#23) and the gap list
  (items 0–8), each entry carrying DONE + hash, PARTIAL, or OPEN + what blocks it.
- 🟡 **Partly met, and the honest headline:** the protocol has now met real data, but
  only in part. **Run:** Tier I end to end on CNRM-CM6-1 (every Required gate produces
  a row) and Tier II on MPI-ESM1-2-LR with a real `E_ref` over 42 staged models and 160
  members; the 64-model Tier I ensemble and the full Tier II comparison run are in
  flight. **Not run:** Tier III's perfect-model half (§III.2), which still has no
  CESM2 / MPI-ESM / GISS-E2 / CESM-LE staged; Tier III's paleo stanzas against a real
  PMIP4 experiment; the fixed-SST gates I.4a/b. **Scored against stand-ins:** `tos` vs
  EN4 rather than ESACCI-SST, `siconc` vs HadISST — each because the
  protocol's own product is not staged. **Still provisional or `null`:** every
  `tier2.obs_sigma` value, the GSAT blending factor, the packaged ERF table,
  `tier3.block_years`/`spinup_years`, the LE variance-ratio bound. And the runs raised
  four *protocol* questions (group E) that no code change can settle. All of it is
  tracked rather than hidden — see "What has run on real data" and "Decisions needed
  from Duncan" in `metrics_reference.md`.
