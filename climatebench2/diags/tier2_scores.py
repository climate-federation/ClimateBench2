"""Tier II scoring diagnostics.

- :class:`ScoredAnnualMeanTimeSeries` / :class:`ScoredMonthlyMeanTimeSeries` /
  :class:`ScoredAnnualMaxTimeSeries` — the time-series diagnostics the Tier II
  suites name. They are **thin subclasses of the ClimateEval diagnostics**
  (``ScoredAnnualMaxTimeSeries`` additionally takes a per-gridpoint annual
  maximum before the area mean): they emit the raw series and ClimateEval's
  deterministic metrics, and nothing else.

  The probabilistic score is *not* computed here. Since ensemble members
  arrive as separate data sources (``--member`` / DRS discovery, one run per
  member), the fair CRPS across a model's members can only be formed once
  every member has run — so it is a post-processing pass over the finished
  database, :mod:`climatebench2.scoring_pass`, which ``climatebench2 score``
  runs after the suites and ``climatebench2 leaderboard --rescore`` can
  re-run. Scoring each source separately, as this module used to, made every
  model an M = 1 "ensemble" whose fair CRPS is undefined.

- :class:`InternalVariability` — the σ_int input of the regime-(c)
  consistency test: the piControl global-mean annual series of each core
  variable, chunked into observation-length segments
  (``scoring.chunked_statistic_std``) for the window **mean** and the OLS
  **trend**, at both window lengths the protocol scores (the post-2015 test
  window and the 1950-present window). It emits numbers, never a pass/fail —
  ``tier2.internal_variability.requirement`` is ``diagnostic``.

- :class:`TrendConsistency` — **now a thin alias** of the annual-mean series.
  The regime-(c) computation moved into
  :mod:`climatebench2.scoring_pass`, which is where the ensemble members
  (grouped by model name), the reference and — when the Tier I database is
  scored alongside — the σ_int rows of :class:`InternalVariability` all meet.
  Computed inside the diagnostic it could only pool the submission with the
  CMIP6 comparison *models*, whose spread is structural disagreement rather
  than the model's own ensemble spread, and σ_int was hard-wired to 0.

Metrics rows use the numeric-column convention of ``pass_fail``:
``value``/``passes`` plus score-specific columns.
"""

from __future__ import annotations

from typing import Any, ClassVar

from esmvalcore.preprocessor import (
    annual_statistics,
    area_statistics,
    regrid,
    regrid_time,
)
from loguru import logger

from climateeval import Variable
from climateeval._config import setup_esmvaltool_config_and_logging
from climateeval._variable import COORDINATES
from climateeval.diags._utils import DEFAULT_GRID
from climateeval.diags.simple import (
    AnnualMeanTimeSeries,
    MeanTimeSeries,
    OceanHeatContentTimeSeries,
)

from climatebench2 import scoring, windows
from climatebench2._thresholds import get_threshold
from climatebench2.diags.pass_fail import gate_requirement
from climatebench2.diags.tier1_physics import CB2ComplexDiagnostic

logger = logger.opt(colors=True)


class ScoredAnnualMeanTimeSeries(AnnualMeanTimeSeries):
    """Annual-mean series scored by :mod:`climatebench2.scoring_pass`.

    A thin alias of ClimateEval's diagnostic so the Tier II suite YAMLs keep
    naming a CB2 class (and so the protocol's scored variables stay visible
    in the suite); the fair CRPS, its ESS standard error, the block-bootstrap
    interval and the skill against the CMIP6 median are appended to this
    diagnostic's ``metrics`` table by the post-processing pass, once every
    ensemble member has been ingested.
    """


class ScoredOceanHeatContent(OceanHeatContentTimeSeries):
    """Global ocean heat content series (ZJ) scored by the Tier II pass.

    ClimateEval's diagnostic unchanged — regrid, sum over depth, sum over
    area, annual mean, anomaly, ZJ — except for **which years the anomaly is
    taken about**: upstream fixes 1979-1988, this takes the protocol's
    climatology baseline (``tier2.climatology_baseline_period``, 1985-2014).

    For the score itself that is immaterial: the pass re-anomalises every
    source about its own 1985-2014 mean (``tier2.anomaly_baseline``), and a
    constant offset per source cancels exactly. It matters for which sources
    can be scored at all: the entry is loaded from 1985, so the upstream
    reference period leaves four years (1985-1988) in the cube, and a source
    starting after 1988 — an emulator run from 2000, say, which the pass
    would accept with its 15 baseline years — fails the upstream ``anomalies``
    step ("outside cube time bounds") and loses its row. With the protocol
    baseline the raw series is the protocol's anomaly already.

    The input is ``thetao`` turned into the layer's heat content on load by
    :func:`climatebench2.data.layer_heat_content` (the suite variable's
    ``additional_preprocessors``), a single level per layer, so the depth sum
    here is a no-op.
    """

    @property
    def _anomaly_reference(self) -> dict[str, int]:  # type: ignore[override]
        start, end = (int(y) for y in get_threshold("tier2.climatology_baseline_period"))
        return {
            "start_year": start,
            "start_month": 1,
            "start_day": 1,
            "end_year": end,
            "end_month": 12,
            "end_day": 31,
        }


class ScoredMonthlyMeanTimeSeries(MeanTimeSeries):
    """Monthly-mean series scored by :mod:`climatebench2.scoring_pass`."""


class ScoredAnnualMaxTimeSeries(AnnualMeanTimeSeries):
    """TXx-style annual block maxima, scored by the post-processing pass.

    Per-gridpoint annual maximum first, then the area mean — the global-mean
    TXx series of metrics_reference.md §II.1 "Daily tas extremes". Feed it
    daily ``tasmax`` (or ``tas``); on monthly input it degrades to the
    annual maximum of monthly means (documented, weaker).
    """

    def _preprocess(self, cube: Any, _variable: Any) -> Any:
        with setup_esmvaltool_config_and_logging():
            cube = regrid(cube, DEFAULT_GRID, "linear", cache_weights=True)
            cube = annual_statistics(cube, "max")  # per-gridpoint block max
            cube = area_statistics(cube, "mean")
            cube = regrid_time(
                cube,
                frequency="yr",
                calendar="standard",
                units=COORDINATES["time"]["units"],
            )
        return cube


class TrendConsistency(AnnualMeanTimeSeries):
    """Annual-mean series whose trend is tested by the scoring pass.

    A thin alias, kept so existing suite YAMLs (and any user's) stay valid.
    The regime-(c) trend-consistency test itself is now
    :func:`climatebench2.scoring_pass.trend_consistency_rows`, run over the
    finished database: only there are a model's ensemble members grouped
    together (they arrive as separate data sources), and only there can the
    σ_int rows of :class:`InternalVariability` — written into the Tier I
    database from the piControl experiment — reach the test.

    ``ClimateBench2_TierII.yml`` no longer names this class: the
    ``annual_mean_timeseries`` entry already carries the same series, and the
    pass writes one trend-consistency row per model and variable from it.
    """


class InternalVariability(CB2ComplexDiagnostic):
    """σ_int of the protocol's scored statistics, from piControl chunks.

    The regime-(c) consistency test combines, in quadrature, the ensemble
    spread, the **internal variability** of the statistic over an
    observation-length window, and the observational uncertainty
    (metrics_reference.md Tier II preamble (c)). This diagnostic supplies the
    middle term: for the global-mean **annual** series of each core variable
    it can find in the piControl experiment, it chops the control into
    non-overlapping segments of each scored window length and takes the
    inter-segment standard deviation of

    - the window **mean** (``..._sigma_int_mean_<window>``), and
    - the window **OLS trend** per year (``..._sigma_int_trend_<window>``),

    for both window lengths the protocol scores: ``test`` (the reserved
    post-2015 window, ``tier2.test_window_start`` → last complete year) and
    ``long`` (``tier2.long_trend_start`` = 1950 → last complete year). The
    lengths themselves are emitted as ``window_years_<window>`` so the
    scoring pass can match a σ_int to the record it is actually scoring.

    It lives in the Tier I suite because that is where the ``picontrol``
    experiment is loaded in full, and it is tagged
    ``tier2.internal_variability.requirement: diagnostic`` — it emits no
    pass/fail row and can never touch the entry ticket.
    """

    _required_data_keys = ("picontrol",)
    #: Protocol standing (thresholds.yml), read rather than hard-coded. This
    #: diagnostic defines no ``_gate_checks``: it reports numbers only.
    requirement: ClassVar[str] = gate_requirement("tier2.internal_variability")

    def _calculate_raw_output(self, complex_data_source: Any) -> dict[Any, Any]:
        picontrol = complex_data_source.data["picontrol"]
        lengths = windows.window_lengths()
        min_chunks = int(get_threshold("tier2.internal_variability.min_chunks"))
        outputs: dict[str, float] = {
            f"window_years_{window}": float(length)
            for window, length in lengths.items()
        }

        for var_name in get_threshold("tier2.internal_variability.variables"):
            try:
                series = self._annual_global_series(
                    picontrol,
                    Variable(var_name, var_name, "mon"),
                )
            except Exception as exc:  # noqa: BLE001 - a control may lack a variable
                logger.info(
                    f"Diagnostic '{self.name}': no '{var_name}' in the piControl "
                    f"experiment, no sigma_int for it ({exc})",
                )
                continue
            outputs[f"{var_name}_n_years_picontrol"] = float(series.size)
            for window, length in lengths.items():
                if series.size // length < min_chunks:
                    logger.warning(
                        f"Diagnostic '{self.name}': piControl has {series.size} yr, "
                        f"too few for {min_chunks} chunks of {length} yr "
                        f"({window} window) — no sigma_int for '{var_name}'",
                    )
                    continue
                for statistic in ("mean", "trend"):
                    outputs[f"{var_name}_sigma_int_{statistic}_{window}"] = (
                        scoring.chunked_statistic_std(series, length, statistic)
                    )
        return self._scalar_outputs(outputs)
