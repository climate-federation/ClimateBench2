# ClimateBench2 suites

CB2-owned suite YAMLs in [ClimateEval's suite format](https://github.com/climate-federation/ClimateEval/blob/main/docs/suites.md).
They mix stock ClimateEval diagnostics (`climateeval.diags.*`) with CB2
protocol diagnostics (`climatebench2.diags.*`), which ClimateEval loads by
dotted class path. A suite here shadows a ClimateEval suite of the same name
(`_resolve_suite` in `climatebench2/_cli.py`).

`Suite.get_database` passes **one data object to every diagnostic**, so
suites are homogeneous by data shape: *complex* suites take an experiment
dict (`--experiment KEY=PATH`), *simple* suites take cubes. Which of the two a
suite gets — and over which time window — is declared in the CLI's suite
registry (`_cli.SUITE_REGISTRY`): experiments always load in full, the
variability suite takes `picontrol`, and the monthly Tier II suite takes the
model's cubes over the post-2015 test window (`tier2.test_window_start`),
once per ensemble member (`--member LABEL=PATH`, or DRS auto-discovery).
`ClimateBench2_TierII_daily` is the one cube suite that takes **no window
cut**: the paper computes its extremes, PDF and diurnal climatologies "over
the full historical record", so every entry in it is in-sample.
`SuiteSpec.per_member` is what decides that, and it is set for
`ClimateBench2_TierII_events` too: its aggregated scalars are scored *across*
the ensemble, so the suite runs once per member with **that member's own
record** under the `historical` key (an explicit `--experiment historical=DIR`
pins one record for every member instead, collapsing the runs to one). Tier I
and Tier III stay once-per-model — a gate is a property of the model.
CB2 complex diagnostics accept a superset of their required keys and skip
(with a warning) when their experiments are absent
(`climatebench2.diags.SupersetExperimentMixin`) — including the thin gate
wrappers over ClimateEval's own complex diagnostics (ECS, land–ocean warming
ratio, Arctic amplification, meridional heat transport), which take their
protocol constants from `thresholds.yml`, so their suite entries keep
`additional_diagnostic_kwargs: {}`.

| Suite | Data shape | Window | Per member? | Contents |
|---|---|---|---|---|
| `ClimateBench2_TierI` | experiment dict (`picontrol`, `4xco2`, `histaer`, `historical`, `day`, `amip`, `amip4xco2`, `patch_ep`, `patch_wp`) | full | no | 17 physical-consistency gates: ECS, energy balance, closures, clear-sky β, precip–buoyancy, land–ocean, Arctic, aerosol ERF, MHT, ITCZ–EFE, ENSO teleconnections, geostrophic balance, MJO, amip-4xCO2 ERF, GFMIP Δλ + the Bjerknes and C–C extras — **plus** `internal_variability`, which is not a gate: it reports the piControl σ_int the Tier II consistency test needs, and rides here because this is where the control is loaded in full |
| `ClimateBench2_TierI_variability` | cubes (monthly `tos`), from `picontrol` | full (≥ 100 yr) | no | ENSO amplitude + spectral-shape gates |
| `ClimateBench2_TierII` | cubes (monthly) | post-2015 test window, **clipped per variable to its reference's record** | **yes** | Core variables vs HadCRUT5 (`tas`), GPCP (`pr`), CERES-EBAF (all-sky **and clear-sky** TOA: `rsut`/`rlut`/`rtnt`/`rsutcs`/`rlutcs`, and the cloud radiative effects `swcre`/`lwcre`), ERA5 (`prw`), MODIS Aqua MYD08_M3 (`clt`/`clwvi`/`clivi`), EN4 (`tos`, OHC) and HadISST (`siconc`, as **extent**) — plus OHC (total, 0–2000 m **and 0–100 m**), `reference_baseline` / `sst_baseline` (the reference's pre-test 1985–2014 record) and `eof_projection` / `sst_eof_projection` (the regime-(b) coefficients on the reference's fixed pre-2015 EOF basis). Every core variable carries the **staged CMIP6 comparison ensemble** in `other_data` |
| `ClimateBench2_TierII_daily` | cubes (daily/hourly) | **full record** | **yes** | `extremes` — the eight ETCCDI indices (TXx, TNn, TX90p, WSDI; Rx1day, Rx5day, R95pTOT, CDD) as a climatological mean and a decadal trend per land band, on the ~1° conservative grid; `perkins` — the PDF-overlap skill of daily `tas` anomalies and wet-day `pr` intensity; `diurnal_harmonic` — first-harmonic amplitude and (cos, sin) phase of the sub-daily `pr` climatology in local solar time; plus the older deterministic displays (TXx block maxima, pr intensity histogram, `DiurnalCycle`) |
| `ClimateBench2_TierII_events` | experiment dict (`historical`) | full | **yes** | The Tier II aggregated scalars of §II.1 — realized warming level (the protocol's primary test-window statistic) and the two GMST trends, the Pinatubo response, the aerosol-era hemispheric asymmetry, and the three **seasonal-cycle metrics** (land annual temperature range; SST–low-cloud covariance and the seasonal cloud-radiative feedback over the five stratocumulus decks). Reported and *scored*, never part of the Tier I entry ticket |
| `ClimateBench2_TierIII` | experiment dict (`picontrol` + `midholocene`/`lgm`/`lig127k`) | full | no | The mid-Holocene Green-Sahara monsoon gate (with the Harrison 2015 North-Africa magnitude reported beside it) plus **one `PaleoProxyScore` stanza per (period, dataset, variable)** — LGM Tierney 2020 `tos`, Bartlein 2011 `tas`/`pr` and Cleator 2020 `tas`/`pr` (data assimilation: reported, not scored); mid-Holocene Bartlein 2011 `tas`/`pr` and Temp12k; LIG Otto-Bliesner 2021 `tas` and Scussolini 2019 `pr`. Each reads one processed NetCDF from `paleo_scripts/process_paleo_observations.py` (`--paleo-data-root DIR`, default `tier3.paleo_data_root`), scores the model's **block pseudo-ensemble** against it with fair CRPS and reports the site-consistency fraction |

Four of the six are CLI defaults (`DEFAULT_SUITES`): Tier I, the variability suite,
the Tier II events suite and the monthly Tier II suite. `ClimateBench2_TierII_daily`
and `ClimateBench2_TierIII` are **not** — they need `day`-table output and the paleo
experiments respectively — so ask for them explicitly with `--suite`. Every suite in
`SUITE_REGISTRY`, defaults included, is built and type-checked by
`tests/test_suites.py`.

The `Scored*` diagnostics named by the Tier II suites (and `TrendConsistency`,
kept only so older YAMLs stay valid) are **thin subclasses of the ClimateEval
time-series diagnostics**: they emit the raw series and ClimateEval's
deterministic metrics only. The probabilistic score is added
afterwards by `climatebench2.scoring_pass`, which `climatebench2 score` runs
over the finished databases (and `climatebench2 leaderboard --rescore`
re-runs) — members arrive as separate data sources, so a model's fair CRPS
can only be formed once every member has run, and the regime-(c) consistency
test additionally needs the σ_int rows from the Tier I database.

Two Tier I gates fetch **observations** through ClimateEval DataSources rather
than working from the submission alone: `precip_buoyancy` (I.3c — the GPCP/ERA5
reference slope over `tier1.precip_buoyancy.obs_window`) and
`enso_teleconnections` (I.5c — the HadISST/GPCP regression patterns over
`tier1.enso.teleconnection_obs_window`). Both windows end in 2014, so neither
touches the reserved post-2015 test period. When the products cannot be fetched
(no network, no CDS key) and `fail_on_missing_data` is False, each emits the
model's own statistic, logs the reason and writes **no gate row** — the
scorecard's "not run" hole rather than a spurious pass or fail. Both windows are
clipped to the staged record per product, which is why I.3c pairs its model and
reference fields on **calendar months** (`align_on_common_months`): GPCP starts
in 1983 and ERA5 in 1979, and cutting both to the shorter length from the start
regressed 1983–2014 precipitation on 1979–2010 buoyancy — a four-year offset
that produced a *negative* observed precipitation–buoyancy slope. I.3c's window
(1983–2014, paper App. B.3) also bounds the **model**: its fields are cut to it
and then to the months the six model and reference fields share, so both slopes
are regressed over one month set.

Two Tier II entries deliberately reach **back past the suite's test-window cut**,
by loading their reference through a copy of the `Variable` carrying a different
`timerange` (`diags/tier2_reference.py`): `reference_baseline` records the
1985–2014 series the Climatology baseline is built from, and `eof_projection`
builds the regime-(b) basis from the reference's pre-2015 monthly anomalies. Both
skip a reference that does not cover the window, with a logged reason.

The **Tier II events** suite also fetches observations, and unlike the Tier I
gates it emits them as `reference` rows so the scoring pass can score against
them (`diags/tier2_diagnostics.py::ObservedScalarMixin`): HadCRUT5 `tas`,
corrected from its blended land-air/SST basis to a surface-air-temperature one
with `tier2.gsat_blending_factor` and carrying
`tier2.gsat_blending_relative_uncertainty` of the change as the scalar's
`_sigma_obs`. Two of its scalars have **no** observational counterpart and stay
model-only sign flags, with the reason recorded: the Pinatubo `rsds` dimming
(no BSRN or CERES-SYN DataSource) and the ITCZ shift (GPCP starts in 1979,
after the 1950–1985 aerosol era). GISTEMP, Berkeley Earth and NOAAGlobalTemp —
the paper's other three GMST products — have DataSources upstream (PR #49,
merged on `cb2-integration`) but **none of them is staged**, so `tas` still has
no inter-product observational spread; the pass picks them up automatically once
the data are there, with no code change.

Each of these scalars also fetches its **own** observational record, over a
window that runs to last year, and HadCRUT5 stops in 2023-09. Until `9cb9cc8`
that overrun was caught as missing data and the diagnostic fell back to emitting
the model's own scalars, which the scoring pass cannot score — so the realized
warming level, both GMST trends, the Pinatubo cooling and the NH−SH trend
difference were all silently unscorable on real data. `ObservedScalarMixin`
clips its request through `reference_windows.clip_to_source` now; a record that
is genuinely too short to carry the statistic is still refused.

The suite's three **seasonal-cycle metrics** use the same mixin with *several*
products at once — ERA5 `tas` for the land annual temperature range,
ESACCI-CLOUD `clt` + ESACCI-SST `tos` for the low-cloud covariance, CERES-EBAF
`swcre` (a derived variable) + ESACCI-SST for the seasonal cloud-radiative
feedback. The `reference` row is written under the primary product and every
product read is registered in `data_sources`. All three are properties of the
**1985–2014** climatology, so they are labelled in-sample; the window used is
emitted as `seasonal_window_first/last_year` on the model side only, which
keeps it provenance rather than a scored statistic.

## The daily suite's references, and the one that does not exist

`ClimateBench2_TierII_daily` is where the protocol's daily statistics live,
and where the upstream observational gap bites hardest:

- **the ETCCDI extremes have no reference at all.**
  `ERA5.VARIABLE_MAPPING` has `tas` and `pr` but **no `tasmax`/`tasmin`**, and
  `ERA5Hourly`'s CDS request is hard-wired to one year, so no *staged*
  DataSource can supply daily temperature extremes however the `Variable` is
  spelled. The `extremes` entry therefore carries **no `reference_data:`** and
  its scalars are written, reported and left **unscored** — the scoring pass
  skips a scalar whose reference has no value, exactly as it does for the
  Pinatubo `rsds` dimming. **HadEX3** is the natural reference and its
  DataSource now exists (upstream PR #53, merged on `cb2-integration`) — but
  **no HadEX3 data is staged**, so nothing changes yet; `ERA5Daily` (#54) is in
  the same position. Berkeley daily, HadGHCND, IMERG and MSWEP have no
  DataSource at all. The `pr` indices *could* be scored against `ERA5Hourly`
  through `daily_statistics`, and the suite carries that stanza commented out
  rather than scoring a model against a single ERA5 year;
- **the diurnal harmonic and the Perkins score are scored**, against
  `ERA5Hourly` `pr`/`tas` — with the caveat that a `frequency: day` variable
  looks in `<data_root>/ERA5/day/<var>`, which a staged daily ERA5 must fill.

The extremes are *shaped* like a scored entry regardless: `ETCCDIExtremes` and
`DiurnalHarmonic` are `ScalarTableDiagnostic`s, whose raw output is one row per
data source with one column per named scalar — the aggregated-scalar table
`scoring_pass.score_scalar_output` reads. The day a daily observational
DataSource lands, the suite needs one `reference_data:` line and nothing else.

The **Perkins score** is the exception to that shape: it is a *skill* in [0, 1],
not an error, so it is written as a **metric** (`perkins_<season>`,
`perkins_all`) and shown in the leaderboard's own distribution-skill table. It
must never enter `E_ref` or the headline `S = 1 − E/E_ref`.

## The comparison ensemble, and the window each variable is really scored over

Two things every Tier II number depends on, and neither is visible in a suite
stanza on its own.

**`other_data` is `climatebench2.data.StagedCMIP6HistoricalSSP245`** — the CMIP6
historical+SSP2-4.5 comparison ensemble whose per-model fair CRPS `E_ref` is the
median of. It enumerates `CMIP6_<model>_historical-ssp245_<member>/<freq>/<var>/`
directories under `$CLIMATEBENCH2_STAGED_CMIP6_ROOT` (defaulting to
`--data-root`) and yields data sources with **ids identical to upstream's**
`climateeval.data.CMIP6HistoricalSSP245`, so the scoring pass groups members the
same way either one is used; see that module's docstring for why CB2 enumerates
a staged root instead of globbing the pool. Two consequences for a suite author:
a **derived** variable (`rtnt`, `phcint`) yields a member only when *every*
required input is staged for it, and a stanza that lists only observational
products in `other_data` gets **no comparison models at all**, so nothing reaches
`E_ref` — which is exactly what had happened to `tos` and `siconc`.

**The window is the reference's, not the protocol's.** The nominal post-2015 test
window runs to last year, and most staged observational records stop earlier.
ClimateEval raises `MissingDataError` when a request overruns a record, and
`climatebench2 score` runs with `fail_on_missing_data=False`, so an overrun used
to mean a logged warning, a vanished reference and a variable that scored
**nothing at all, silently**. `climatebench2/reference_windows.py` reads each
staged reference's first and last time out of the NetCDF headers and resolves the
window per variable, in **whole calendar years** (a partial year at either end is
dropped — an annual mean over Jan–Sep is a seasonal-cycle artefact); the CLI
materialises a copy of the suite carrying those windows, drops the global
`variable_kwargs["timerange"]`, and **prints every window it had to clip**. A
derived reference takes the intersection of its inputs' coverage. The window is a
property of the reference, so it is the same for every model scored on that
variable. The same clipping is applied on the paths that do not go through a
stanza: the Tier II scalars' own HadCRUT5 fetch, the baseline and EOF-basis
diagnostics, and I.3c's GPCP/ERA5 reference.

## Sea ice is extent

Sea ice is scored as **extent** (Σ A over cells whose concentration reaches 15 %),
which is what the protocol asks for and what ClimateEval's `SeaIceExtent*`
diagnostics compute — area (Σ siconc·A) is a systematically different, much less
bias-sensitive number. There is **no `extent_threshold` diagnostic kwarg**: 15 %
is the class default `SeaIceExtentTimeSeries._concentration_threshold`
(`climateeval.diags.simple._sea_ice.SEA_ICE_EXTENT_THRESHOLD`), and a stanza that
passed `extent_threshold:` would raise on an unexpected kwarg.

The reference is **HadISST**, which is what is staged in ClimateEval form
(`reanalysis_HadISST/mon/siconc`, ending 2021-12, so the scored window is
2015–2021). The protocol names OSI-450 / NSIDC-G02202; neither is staged, and a
reference that does not resolve leaves every sea-ice row unscored, so the swap
back is a one-line change once they are. The same reasoning put **EN4** behind
`tos` in place of ESACCI-SST, commented in the suite with the protocol's own
choice. (Skin temperature `ts`, once scored against MERRA-2 as an ERA5
stand-in, is no longer a Tier II variable: Duncan dropped it on 2026-10-27.)

## The perfect-model variant (Tier III.2)

`climatebench2 score --truth DIR` does not add a suite: it **materialises a copy** of
each cube suite with every `reference_data:` swapped for
`climatebench2.diags.truth_reference.LocalCMORReference`, pointed at a held-out ESM
run. The CMIP6 comparison models are dropped (an `E_ref` against the real world is
meaningless in a perfect-model experiment), σ_obs is zeroed and every scored row is
labelled `window = perfect-model`. `--truth-member LABEL=PATH` adds members of the
truth model — not extra references, but the inter-member spread the large-ensemble
test (`tier3.le_spread`, Extended) compares the submission's own against.
`LocalCMORReference` is a declared **upstream candidate**: ClimateEval had no
"local directory" DataSource, and PR #55 (`LocalCMORDataSource`) is where this belongs.
#55 is merged on the `cb2-integration` branch and still open upstream, so this class
cannot be retired yet — the deletion needs a pin bump and the pin can only name an
upstream commit. No truth data is staged, so this path has still only ever run on
synthetic databases.

Every threshold referenced by a diagnostic comes from
[`../thresholds.yml`](../thresholds.yml) — never hard-coded, and that includes each
gate's `requirement:` tag (`required` / `extended` / `extra` / `diagnostic`), which
decides whether the check is part of the leaderboard's entry ticket, is reported for
additional credit, or is a non-protocol sanity check. A submission may declare an
individual gate inapplicable with `climatebench2 score --not-applicable NAME` (NAME is
the suite entry name, e.g. `geostrophic_balance`); the gate then computes nothing and
its rows are marked `applicable = 0` — neither a pass nor a fail — which the scorecard
distinguishes from a gate that never ran.
