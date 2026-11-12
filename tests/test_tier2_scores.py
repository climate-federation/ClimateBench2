"""Tests for the Tier II scoring diagnostics' post-processing.

Exercises the mixin logic on synthetic DiagnosticOutput tables — no data
loading, but the real ibis/DiagnosticOutput plumbing (needs climateeval).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

climateeval = pytest.importorskip("climateeval")

import ibis  # noqa: E402

from climateeval.diags._base import DiagnosticOutput  # noqa: E402

from climatebench2.diags.tier2_scores import (  # noqa: E402
    ScoredAnnualMaxTimeSeries,
    ScoredAnnualMeanTimeSeries,
    ScoredMonthlyMeanTimeSeries,
    TrendConsistency,
)

N_YEARS = 20
TIMES = pd.date_range("2000-01-01", periods=N_YEARS, freq="YS")


def _raw_output_df() -> pd.DataFrame:
    """Model + reference + two CMIP6 'other' sources, one variable."""
    rng = np.random.default_rng(0)
    truth = np.linspace(0.0, 1.0, N_YEARS) + rng.normal(0, 0.05, N_YEARS)
    frames = []
    sources = [
        ("MyModel", "to_benchmark", truth + rng.normal(0, 0.1, N_YEARS)),
        ("OBS", "reference", truth),
        ("CMIP6_A", "other", truth + rng.normal(0, 0.2, N_YEARS)),
        ("CMIP6_B", "other", truth + rng.normal(0, 0.2, N_YEARS)),
    ]
    for data_id, data_type, values in sources:
        frames.append(
            pd.DataFrame(
                {
                    "data_id": data_id,
                    "data_type": data_type,
                    "time": TIMES,
                    "tas": values,
                },
            ),
        )
    return pd.concat(frames, ignore_index=True)


def _output_from(raw_df: pd.DataFrame) -> DiagnosticOutput:
    placeholder = ibis.memtable(pd.DataFrame({"x": [0]}))
    return DiagnosticOutput(
        raw_output=ibis.memtable(raw_df),
        metrics=None,
        variables=placeholder,
        data_sources=placeholder,
    )


def test_scored_time_series_emit_no_crps_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scored classes are thin aliases now: no M = 1 fair-CRPS rows.

    Members arrive as separate data sources, so the fair CRPS is formed by
    the post-processing pass (``climatebench2.scoring_pass``) once every
    member has run — never per data source inside the diagnostic, where the
    ensemble would always be M = 1 and the fair score undefined.
    """
    base_output = _output_from(_raw_output_df())
    monkeypatch.setattr(
        "climateeval.diags.simple.AnnualMeanTimeSeries.get_output",
        lambda self, data, info: base_output,
    )
    diag = ScoredAnnualMeanTimeSeries.__new__(ScoredAnnualMeanTimeSeries)
    output = diag.get_output(None, None)
    assert output.metrics is None
    assert output is base_output


def test_scored_classes_are_climateeval_diagnostics() -> None:
    from climateeval.diags.simple import AnnualMeanTimeSeries, MeanTimeSeries

    assert issubclass(ScoredAnnualMeanTimeSeries, AnnualMeanTimeSeries)
    assert issubclass(ScoredMonthlyMeanTimeSeries, MeanTimeSeries)
    assert issubclass(ScoredAnnualMaxTimeSeries, AnnualMeanTimeSeries)
    # the TXx variant keeps its own preprocessing
    assert "_preprocess" in vars(ScoredAnnualMaxTimeSeries)


def test_trend_consistency_is_now_a_thin_alias() -> None:
    """Regime (c) moved to the scoring pass; the class stays for old YAMLs.

    Inside a diagnostic the "ensemble" could only be the submission pooled
    with the CMIP6 comparison *models* — structural disagreement, not the
    model's own ensemble spread — and sigma_int was hard-wired to 0. The
    pass has the members grouped by model name and the piControl sigma_int.
    """
    from climateeval.diags.simple import AnnualMeanTimeSeries

    assert issubclass(TrendConsistency, AnnualMeanTimeSeries)
    # No post-processing of its own left
    assert "get_output" not in vars(TrendConsistency)
    assert "_preprocess" not in vars(TrendConsistency)


def test_trend_consistency_is_not_in_the_tier2_suite() -> None:
    """The annual-mean entry already carries the series the pass needs."""
    from climateeval.suites import Suite

    from climatebench2._cli import _resolve_suite

    diagnostics = Suite(_resolve_suite("ClimateBench2_TierII"))._get_diagnostics()
    assert "trend_consistency" not in diagnostics
    assert "annual_mean_timeseries" in diagnostics


def test_internal_variability_chunks_the_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sigma_int for the mean and the trend, at both protocol window lengths."""
    import datetime as dt

    from climatebench2 import windows
    from climatebench2.diags.tier2_scores import InternalVariability

    rng = np.random.default_rng(0)
    control = rng.normal(0.0, 1.0, 600)  # 600 yr of white-noise "piControl"

    diag = InternalVariability.__new__(InternalVariability)
    diag._name = "internal_variability"
    monkeypatch.setattr(
        InternalVariability,
        "_annual_global_series",
        lambda self, data, variable: (
            control if variable.var_name == "tas" else _raise_missing()
        ),
    )
    monkeypatch.setattr(
        windows,
        "window_lengths",
        lambda today=None: {"test": 11, "long": 76},
    )
    scalars: dict[str, float] = {}
    monkeypatch.setattr(
        InternalVariability,
        "_scalar_outputs",
        lambda self, values: scalars.update(values) or values,
    )

    source = type("Src", (), {"data": {"picontrol": None}})()
    diag._calculate_raw_output(source)

    assert scalars["window_years_test"] == 11.0
    assert scalars["window_years_long"] == 76.0
    assert scalars["tas_n_years_picontrol"] == 600.0
    # White noise: sigma of an N-yr mean is 1/sqrt(N)
    assert scalars["tas_sigma_int_mean_test"] == pytest.approx(
        1.0 / np.sqrt(11),
        rel=0.4,
    )
    # A longer window averages harder and trends less
    assert scalars["tas_sigma_int_mean_long"] < scalars["tas_sigma_int_mean_test"]
    assert scalars["tas_sigma_int_trend_long"] < scalars["tas_sigma_int_trend_test"]
    # A variable the control does not carry is simply absent
    assert not [k for k in scalars if k.startswith("pr_")]
    del dt


def _raise_missing() -> None:
    msg = "not in this control"
    raise KeyError(msg)


def test_internal_variability_skips_cleanly_for_the_ensemble_spread_source(caplog) -> None:  # noqa: ANN001
    """CB-7: no piControl rows (and no missing-key warning) when the source is the ensemble."""
    from climateeval.data import DataSourceInformation
    from loguru import logger

    from climatebench2._thresholds import use_thresholds
    from climatebench2.diags.tier2_scores import InternalVariability

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        info = DataSourceInformation(
            name="m", category="model", institute="i", exp="amip", variant="r1",
        )
        diag = InternalVariability("internal_variability")
        overlay = {"tier2": {"internal_variability": {"source": "ensemble_spread"}}}
        with use_thresholds(overlay):
            output = diag.get_output({"amip": None}, info)  # no picontrol at all
    finally:
        logger.remove(sink)
    assert output.metrics is None
    assert output.raw_output is None
    assert any("ensemble_spread" in m for m in messages)

    with use_thresholds({"tier2": {"internal_variability": {"source": "bogus"}}}), pytest.raises(
        ValueError, match="must be 'picontrol' or 'ensemble_spread'",
    ):
        diag.get_output({}, info)
