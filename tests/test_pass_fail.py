"""Tests for the Tier I gate machinery (climatebench2.diags.pass_fail)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from climatebench2._thresholds import get_threshold, load_thresholds
from climatebench2.diags.pass_fail import (
    REQUIREMENT_TAGS,
    GateCheck,
    GateMixin,
    band_power_ratio,
    gate_metrics,
    gate_requirement,
    not_applicable_metrics,
)


def test_thresholds_load_and_get() -> None:
    thresholds = load_thresholds()
    assert "tier1" in thresholds
    assert get_threshold("tier1.ecs.range") == [1.0, 7.0]
    with pytest.raises(KeyError, match="not found"):
        get_threshold("tier1.no_such_check.bound")


def test_gate_metrics_scalar_check() -> None:
    """One scalar per data source (the ECS shape)."""
    raw = pd.DataFrame(
        {
            "data_id": ["ModelA", "ModelB", "ModelC"],
            "data_type": ["to_benchmark", "other", "other"],
            "ecs": [3.2, 0.5, 8.1],
        },
    )
    checks = (GateCheck(check_id="ecs_gate", column="ecs", lower=1.0, upper=7.0),)
    metrics = gate_metrics(raw, checks)
    result = metrics.set_index("data_id")["passes"]
    assert result["ModelA"] == 1.0  # in range
    assert result["ModelB"] == 0.0  # below
    assert result["ModelC"] == 0.0  # above
    assert (metrics["var_id"] == "ecs_gate").all()
    assert metrics.set_index("data_id").loc["ModelA", "value"] == pytest.approx(3.2)


def test_gate_metrics_emits_requirement_and_applicable_columns() -> None:
    """Every gate row carries its protocol standing and applicability."""
    raw = pd.DataFrame(
        {
            "data_id": ["ModelA"],
            "data_type": ["to_benchmark"],
            "ecs": [3.2],
        },
    )
    checks = (
        GateCheck(
            check_id="ecs_gate",
            column="ecs",
            lower=1.0,
            upper=7.0,
            requirement="required",
            tier="I",
        ),
    )
    metrics = gate_metrics(raw, checks)
    assert metrics.loc[0, "requirement"] == "required"
    assert metrics.loc[0, "tier"] == "I"
    assert metrics.loc[0, "applicable"] == 1.0
    assert metrics.loc[0, "passes"] == 1.0
    expected = {
        "data_id",
        "data_type",
        "var_id",
        "value",
        "bound_lower",
        "bound_upper",
        "passes",
        "requirement",
        "tier",
        "applicable",
    }
    assert set(metrics.columns) == expected


def test_gate_requirement_reads_thresholds_and_validates() -> None:
    """The tag is protocol metadata in thresholds.yml, never hard-coded."""
    assert gate_requirement("tier1.ecs") == "required"
    assert gate_requirement("tier1.gfmip_patch") == "extended"  # not CMIP6 data
    assert gate_requirement("tier1.mjo") == "extended"
    assert gate_requirement("tier1.bjerknes") == "extra"
    assert gate_requirement("tier1.cc_scaling") == "extra"
    assert gate_requirement("tier2.pinatubo") == "diagnostic"
    assert gate_requirement("tier2.hemispheric_asymmetry") == "diagnostic"
    with pytest.raises(KeyError, match="not found"):
        gate_requirement("tier1.no_such_block")


def test_every_tier1_threshold_block_is_tagged() -> None:
    """A new Tier I gate cannot silently escape the entry-ticket grouping."""
    for block, settings in load_thresholds()["tier1"].items():
        assert "requirement" in settings, f"tier1.{block} has no requirement tag"
        assert settings["requirement"] in REQUIREMENT_TAGS


def test_not_applicable_metrics_rows() -> None:
    """Declared N/A: neither pass nor fail, and no value computed."""
    checks = (
        GateCheck(
            check_id="geostrophic_balance",
            column="geostrophic_corr",
            lower=0.9,
            requirement="required",
        ),
    )
    rows = not_applicable_metrics(checks, data_id="model_Emulator")
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["var_id"] == "geostrophic_balance"
    assert row["applicable"] == 0.0
    assert np.isnan(row["passes"])
    assert np.isnan(row["value"])
    assert row["requirement"] == "required"
    assert row["bound_lower"] == 0.9


def test_gate_metrics_series_statistic_orders_by_time() -> None:
    rng = np.random.default_rng(0)
    n = 240
    series = rng.normal(0.0, 0.9, n)
    raw = pd.DataFrame(
        {
            "data_id": "ModelA",
            "data_type": "to_benchmark",
            "time": pd.date_range("2000-01-01", periods=n, freq="MS")[::-1],  # shuffled
            "nino": series,
        },
    )
    checks = (
        GateCheck(
            check_id="amp",
            column="nino",
            statistic=lambda s: float(s.std(ddof=1)),
            lower=0.5,
            upper=1.4,
        ),
    )
    metrics = gate_metrics(raw, checks)
    assert len(metrics) == 1
    assert metrics.loc[0, "value"] == pytest.approx(np.std(series, ddof=1))
    assert metrics.loc[0, "passes"] == 1.0


def test_gate_metrics_skips_missing_column_and_empty_series() -> None:
    raw = pd.DataFrame(
        {
            "data_id": ["A", "B"],
            "data_type": ["to_benchmark", "other"],
            "x": [1.0, np.nan],
        },
    )
    checks = (
        GateCheck(check_id="x_gate", column="x", lower=0.0),
        GateCheck(check_id="missing", column="not_there", lower=0.0),
    )
    metrics = gate_metrics(raw, checks)
    # B's only value is NaN -> dropped; 'missing' column skipped entirely
    assert list(metrics["data_id"]) == ["A"]
    assert list(metrics["var_id"]) == ["x_gate"]


def test_band_power_ratio_enso_like_vs_white_noise() -> None:
    """Integrated in-band power (paper I.5b), not the mean PSD."""
    rng = np.random.default_rng(42)
    n = 600  # 50 years, monthly
    t = np.arange(n)
    # ENSO-like: dominant 4-yr oscillation + weak noise
    enso = np.sin(2 * np.pi * t / 48.0) + 0.2 * rng.normal(size=n)
    ratio_enso = band_power_ratio(pd.Series(enso))
    assert ratio_enso > 1.5

    # White noise: flat spectrum, so the ratio is the ratio of the bands'
    # frequency widths, (1/2 - 1/7) / (1 - 1/2) = 0.357 / 0.5 ~= 0.71
    noise = rng.normal(size=n)
    ratio_noise = band_power_ratio(pd.Series(noise))
    assert ratio_noise < ratio_enso
    assert ratio_noise < 1.5  # a white-noise "ENSO" fails the gate
    assert ratio_noise == pytest.approx(0.714, abs=0.35)


def test_gate_check_one_sided_bounds() -> None:
    raw = pd.DataFrame(
        {"data_id": ["A"], "data_type": ["to_benchmark"], "ratio": [2.0]},
    )
    metrics = gate_metrics(
        raw,
        (GateCheck(check_id="r", column="ratio", lower=1.5),),
    )
    assert metrics.loc[0, "passes"] == 1.0
    assert np.isnan(metrics.loc[0, "bound_upper"])


def test_enso_gate_index_is_unsmoothed_monthly_anomalies() -> None:
    """I.5a/b: ENSOGate drops Nino34's 3-month running mean (ONI smoothing)."""
    pytest.importorskip("climateeval")

    from cf_units import Unit
    from iris.coords import DimCoord
    from iris.cube import Cube

    from climateeval import Variable
    from climateeval.diags.simple import Nino34, SimpleDiagnosticInputData

    from climatebench2.diags.pass_fail import ENSOGate

    assert ENSOGate._rolling_window_length == 1
    assert Nino34._rolling_window_length == 3  # upstream default untouched

    n_years = 40
    n_time = n_years * 12
    rng = np.random.default_rng(3)
    month = np.arange(n_time) % 12
    values = (
        1.0 * np.sin(2 * np.pi * np.arange(n_time) / 48.0)  # 4-yr ENSO
        + 2.0 * np.sin(2 * np.pi * month / 12.0)  # seasonal cycle
        + 0.8 * rng.normal(size=n_time)  # month-to-month noise
    )
    time = DimCoord(
        np.arange(n_time, dtype=float) * 30.0 + 15.0,
        standard_name="time",
        units=Unit("days since 1850-01-01", calendar="360_day"),
    )
    time.guess_bounds()
    lat = DimCoord(
        np.linspace(-85.0, 85.0, 36),
        standard_name="latitude",
        units="degrees",
    )
    lon = DimCoord(
        np.linspace(2.5, 357.5, 72),
        standard_name="longitude",
        units="degrees",
    )
    lat.guess_bounds()
    lon.guess_bounds()
    cube = Cube(
        np.broadcast_to(values[:, None, None], (n_time, 36, 72))
        .astype(np.float32)
        .copy(),
        var_name="tos",
        units="degC",
        dim_coords_and_dims=[(time, 0), (lat, 1), (lon, 2)],
    )

    variable = Variable("tos_nino34", "tos", "mon")
    inputs = {variable: SimpleDiagnosticInputData(reference=None)}
    unsmoothed = ENSOGate("enso", inputs)._preprocess(cube.copy(), variable)
    smoothed = Nino34("nino34", inputs)._preprocess(cube.copy(), variable)

    # No points lost to the running window, and more variance retained
    assert unsmoothed.shape[0] == n_time
    assert smoothed.shape[0] == n_time - 2
    assert np.std(unsmoothed.data, ddof=1) > np.std(smoothed.data, ddof=1)


def test_declared_not_applicable_gate_emits_na_rows_without_data() -> None:
    """`score --not-applicable NAME`: N/A rows, no computation, no data."""
    pytest.importorskip("climateeval")

    from climateeval.data import DataSourceInformation

    from climatebench2.diags import GeostrophicBalanceGate

    info = DataSourceInformation(name="Emulator", category="model")
    gate = GeostrophicBalanceGate(
        "geostrophic_balance",
        not_applicable=["geostrophic_balance"],
    )
    assert gate.declared_not_applicable

    # No `day` experiment supplied, and none needed: nothing is computed.
    output = gate.get_output({}, info)
    assert output.raw_output is None
    metrics = output.metrics.to_pandas()
    assert list(metrics["var_id"]) == ["geostrophic_balance"]
    assert metrics.loc[0, "applicable"] == 0.0
    assert np.isnan(metrics.loc[0, "passes"])
    assert metrics.loc[0, "requirement"] == "required"

    # A gate that was *not* declared N/A but has no data writes nothing at
    # all — "not run" must stay distinguishable from "does not apply".
    other = GeostrophicBalanceGate(
        "geostrophic_balance",
        not_applicable=["mjo"],
    )
    assert not other.declared_not_applicable
    assert other.get_output({}, info).metrics is None


# ---------------------------------------------------------------------------
# Run-wide switches: the upstream comparison ensemble, crash isolation
# ---------------------------------------------------------------------------

#: The CB2 gates that wrap an upstream complex diagnostic with its own
#: comparison-ensemble generator (``_UpstreamGate`` in tier1_physics).
UPSTREAM_GATES = (
    "ECSGate",
    "LandOceanWarmingGate",
    "ArcticAmplificationGate",
    "MeridionalHeatTransportGate",
)


@pytest.mark.parametrize("gate_name", UPSTREAM_GATES)
def test_upstream_gates_skip_the_comparison_ensemble_when_switched_off(
    gate_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``score --comparison frozen|none``: upstream's pool glob never runs."""
    pytest.importorskip("climateeval")
    from climateeval.diags.complex._base import ComplexDiagnostic

    from climatebench2 import diags
    from climatebench2.diags.pass_fail import gate_switches

    cls = getattr(diags, gate_name)
    upstream = next(
        k
        for k in cls.__mro__
        if "_get_other_data" in vars(k) and k.__module__.startswith("climateeval")
    )
    assert upstream is not ComplexDiagnostic  # it really has its own ensemble
    calls: list[str] = []

    def counting_generator(self):  # noqa: ANN001, ANN202
        calls.append(type(self).__name__)
        yield "a comparison model"

    monkeypatch.setattr(upstream, "_get_other_data", counting_generator)
    gate = object.__new__(cls)  # the generator needs no state but a name
    gate._name = gate_name

    assert list(gate._get_other_data()) == ["a comparison model"]  # live
    assert calls == [gate_name]
    with gate_switches(comparison_ensemble=False):
        assert list(gate._get_other_data()) == []
    assert calls == [gate_name]  # never called while switched off
    assert list(gate._get_other_data()) == ["a comparison model"]  # restored


class _CrashingBase:
    """Stands in for the wrapped diagnostic."""

    name = "crashy"
    error: type[Exception] = RuntimeError

    def get_output(self, data, data_information):  # noqa: ANN001, ANN201, ARG002
        raise self.error("boom")


class _CrashingGate(GateMixin, _CrashingBase):
    _gate_checks = (GateCheck(check_id="crashy", column="x", lower=0.0),)


def test_gate_crash_isolation_logs_and_returns_no_rows() -> None:
    from loguru import logger

    from climatebench2.diags.pass_fail import gate_switches

    gate = _CrashingGate()
    with pytest.raises(RuntimeError, match="boom"):
        gate.get_output({}, "MyModel")  # off by default: stock behaviour

    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        with gate_switches(isolate_crashes=True):
            output = gate.get_output({}, "MyModel")
    finally:
        logger.remove(sink)
    assert output.raw_output is None
    assert output.metrics is None
    assert any("CB2_GATE_CRASH gate='crashy'" in m for m in messages)
    assert any("RuntimeError: boom" in m for m in messages)  # traceback kept

    # Missing data keeps its own handling (SupersetExperimentMixin's skip,
    # or fail_on_missing_data): it is not a crash.
    from climateeval.exceptions import MissingDataError

    missing = _CrashingGate()
    missing.error = MissingDataError
    with gate_switches(isolate_crashes=True), pytest.raises(MissingDataError):
        missing.get_output({}, "MyModel")
