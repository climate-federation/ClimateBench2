"""Tests for the Tier I gate diagnostics (cube-level plumbing).

Builds small synthetic CMOR-like cubes and runs selected gates end-to-end
through ClimateEval's ComplexDiagnostic machinery (needs climateeval).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

climateeval = pytest.importorskip("climateeval")

import iris  # noqa: E402
from cf_units import Unit  # noqa: E402
from iris.coords import DimCoord  # noqa: E402
from iris.cube import Cube, CubeList  # noqa: E402

from climateeval.data import DataSourceInformation  # noqa: E402

from climatebench2._thresholds import get_threshold  # noqa: E402
from climatebench2.diags.tier1_physics import (  # noqa: E402
    CB2ComplexDiagnostic,
    ClearSkyFeedbackGate,
    ClosureGate,
    EnergyBalanceGate,
)

N_YEARS = 30


def _monthly_cube(
    var_name: str,
    value: float,
    units: str,
    *,
    n_years: int = N_YEARS,
    trend_per_year: float = 0.0,
    series: np.ndarray | None = None,
    start_year: int = 1850,
    field: np.ndarray | None = None,
) -> Cube:
    """Global monthly cube with a constant (optionally drifting) field.

    ``series`` adds a per-timestep offset (length ``n_years * 12``), so a
    cube can carry an arbitrary global-mean time series; ``field`` (lat, lon)
    multiplies that offset, so a cube can carry an arbitrary *pattern* times
    a time series. ``start_year`` moves the record along the 360-day calendar
    (the calendar-window logic of I.7 needs runs at different epochs).
    """
    n_time = n_years * 12
    time = DimCoord(
        np.arange(n_time, dtype=float) * 30.0 + 15.0 + (start_year - 1850) * 360.0,
        standard_name="time",
        units=Unit("days since 1850-01-01", calendar="360_day"),
    )
    lat = DimCoord(
        np.linspace(-85.0, 85.0, 18),
        standard_name="latitude",
        units="degrees",
    )
    lon = DimCoord(
        np.linspace(5.0, 355.0, 36),
        standard_name="longitude",
        units="degrees",
        circular=True,
    )
    lat.guess_bounds()
    lon.guess_bounds()
    time.guess_bounds()
    years = np.arange(n_time, dtype=float) / 12.0
    values = value + trend_per_year * years
    if series is not None:
        values = values + np.asarray(series, dtype=float)
    if field is None:
        data = np.broadcast_to(values[:, None, None], (n_time, 18, 36))
    else:
        data = value + (values - value)[:, None, None] * np.asarray(field)[None, :, :]
    data = np.asarray(data, dtype=np.float32)
    return Cube(
        data.copy(),
        var_name=var_name,
        standard_name=None,
        units=units,
        dim_coords_and_dims=[(time, 0), (lat, 1), (lon, 2)],
    )


def _picontrol_cubes(
    *,
    toa_imbalance: float = 0.0,
    drift: float = 0.0,
) -> CubeList:
    """piControl fluxes: balanced TOA + balanced surface/water budget."""
    pr = 3.0 / 86400.0  # kg m-2 s-1  (3 mm/day)
    hfls = pr * 2.5008e6  # W m-2, closes the water budget (~87 = L_v P)
    hfss = 20.0
    rsdt, rsut = 340.0, 100.0
    rlut = rsdt - rsut - toa_imbalance  # TOA net = toa_imbalance
    # Surface net radiation closing the atmospheric energy budget exactly
    # (paper I.2b): Q_rad = SFCrad - TOA = L_v P + SHF
    sfc_net_rad = toa_imbalance + hfls + hfss
    rsds, rsus = 185.0, 25.0  # net SW = 160
    rlds = sfc_net_rad - (rsds - rsus) + 340.0  # choose rlus = 340
    rlus = 340.0
    cubes = CubeList(
        [
            _monthly_cube("pr", pr, "kg m-2 s-1"),
            _monthly_cube("hfls", hfls, "W m-2"),
            _monthly_cube("hfss", hfss, "W m-2"),
            _monthly_cube("rsdt", rsdt, "W m-2"),
            _monthly_cube("rsut", rsut, "W m-2"),
            _monthly_cube("rlut", rlut, "W m-2", trend_per_year=-drift / 10.0),
            _monthly_cube("rsds", rsds, "W m-2"),
            _monthly_cube("rsus", rsus, "W m-2"),
            _monthly_cube("rlds", rlds, "W m-2"),
            _monthly_cube("rlus", rlus, "W m-2"),
        ],
    )
    return cubes


def _info() -> DataSourceInformation:
    return DataSourceInformation(
        name="SynthModel",
        category="model",
        institute="Synth",
        exp="piControl",
        variant="r1i1p1f1",
    )


@pytest.fixture(scope="module")
def balanced_output():  # noqa: ANN201
    diag = EnergyBalanceGate("energy_balance", fail_on_missing_data=True)
    data = {"picontrol": _picontrol_cubes(toa_imbalance=0.05)}
    return diag.get_output(data, _info())


def test_energy_balance_gate_passes_balanced_control(balanced_output) -> None:  # noqa: ANN001
    metrics = balanced_output.metrics.to_pandas().set_index("var_id")
    assert metrics.loc["energy_balance_mean", "value"] == pytest.approx(0.05, abs=0.02)
    assert metrics.loc["energy_balance_mean", "passes"] == 1.0
    assert metrics.loc["energy_balance_drift", "passes"] == 1.0


def test_energy_balance_gate_raw_output_columns(balanced_output) -> None:  # noqa: ANN001
    raw = balanced_output.raw_output.to_pandas()
    assert {"toa_net_mean_abs", "toa_net_drift_abs", "n_years"} <= set(raw.columns)
    assert raw.iloc[0]["n_years"] == N_YEARS


def test_energy_balance_gate_fails_drifting_control() -> None:
    diag = EnergyBalanceGate("energy_balance", fail_on_missing_data=True)
    data = {"picontrol": _picontrol_cubes(toa_imbalance=0.5, drift=0.5)}
    output = diag.get_output(data, _info())
    metrics = output.metrics.to_pandas().set_index("var_id")
    assert metrics.loc["energy_balance_mean", "passes"] == 0.0  # |0.5| > 0.1
    assert metrics.loc["energy_balance_drift", "passes"] == 0.0  # 0.5 > 0.02


def test_closure_gate_water_budget_passes() -> None:
    diag = ClosureGate("closure", fail_on_missing_data=True)
    data = {"picontrol": _picontrol_cubes()}
    output = diag.get_output(data, _info())
    metrics = output.metrics.to_pandas().set_index("var_id")
    # P and E constructed to balance exactly
    assert metrics.loc["water_budget", "value"] == pytest.approx(0.0, abs=1e-3)
    assert metrics.loc["water_budget", "passes"] == 1.0
    # ... and so do Q_rad and L_v P + SHF (paper I.2b arrangement)
    assert metrics.loc["atm_energy_budget", "value"] == pytest.approx(0.0, abs=0.05)
    assert metrics.loc["atm_energy_budget", "passes"] == 1.0


def test_closure_gate_atmospheric_energy_budget_fails_when_open() -> None:
    """A 5 W/m² surface-radiation error breaks the 2 W/m² bound."""
    diag = ClosureGate("closure", fail_on_missing_data=True)
    cubes = _picontrol_cubes()
    cubes.extract_cube(iris.NameConstraint(var_name="rlds")).data += np.float32(5.0)
    output = diag.get_output({"picontrol": cubes}, _info())
    metrics = output.metrics.to_pandas().set_index("var_id")
    assert metrics.loc["atm_energy_budget", "value"] == pytest.approx(5.0, abs=0.05)
    assert metrics.loc["atm_energy_budget", "passes"] == 0.0


def test_energy_balance_gate_uses_only_the_last_evaluation_years(monkeypatch) -> None:  # noqa: ANN001
    """I.1 is evaluated over the last `evaluation_years` of the control."""
    n_eval = int(get_threshold("tier1.energy_balance.evaluation_years"))
    # 50 badly imbalanced years followed by `n_eval` balanced ones: the whole
    # record would average 50*5/(50+n_eval) = 1.67 W/m2 and fail.
    toa_net = np.concatenate([np.full(50, 5.0), np.zeros(n_eval)])
    monkeypatch.setattr(
        EnergyBalanceGate,
        "_toa_net_annual_global",
        lambda self, data: toa_net,  # noqa: ARG005
    )
    diag = EnergyBalanceGate("energy_balance", fail_on_missing_data=True)
    raw = diag._calculate_raw_output(SimpleNamespace(data={"picontrol": None}))
    values = {variable.var_name: float(cube.data) for variable, cube in raw.items()}
    assert values["n_years"] == n_eval
    assert values["toa_net_mean_abs"] == pytest.approx(0.0, abs=1e-9)
    assert values["toa_net_drift_abs"] == pytest.approx(0.0, abs=1e-9)


def test_energy_balance_gate_short_control_uses_whole_record(monkeypatch) -> None:  # noqa: ANN001
    """A control shorter than the window is used whole (with a warning)."""
    toa_net = np.full(30, 0.05)
    monkeypatch.setattr(
        EnergyBalanceGate,
        "_toa_net_annual_global",
        lambda self, data: toa_net,  # noqa: ARG005
    )
    diag = EnergyBalanceGate("energy_balance", fail_on_missing_data=True)
    raw = diag._calculate_raw_output(SimpleNamespace(data={"picontrol": None}))
    values = {variable.var_name: float(cube.data) for variable, cube in raw.items()}
    assert values["n_years"] == 30
    assert values["toa_net_mean_abs"] == pytest.approx(0.05)


def _clear_sky_experiments(
    *,
    slope: float,
    n_years: int = 150,
    seed: int = 3,
) -> dict:
    """PiControl (steady) + abrupt-4xCO2 ``rlutcs``/``ts`` with a known forced slope.

    piControl carries no trend, so its long-term global mean is exactly
    ``ts0``/``rlutcs0`` — the baseline the gate subtracts (paper I.3a's
    baseline convention, shared with ``climateeval.diags.complex.ECS``). The
    abrupt-4xCO2 branch approaches a warmer equilibrium (an asymptotic ΔTs
    trajectory, like a real Gregory run) with small noise, and Δrlutcs
    tracks it at exactly ``slope`` plus small noise, so the annual
    global-mean OLS slope recovers ``slope`` to within a small tolerance.
    """
    rng = np.random.default_rng(seed)
    ts0, rlutcs0 = 288.0, 240.0
    picontrol = CubeList(
        [
            _monthly_cube("ts", ts0, "K", n_years=30),
            _monthly_cube("rlutcs", rlutcs0, "W m-2", n_years=30),
        ],
    )
    delta_ts = 8.0 * (1.0 - np.exp(-np.arange(n_years) / 40.0))
    delta_ts = delta_ts + rng.normal(0.0, 0.05, n_years)
    delta_rlutcs = slope * delta_ts + rng.normal(0.0, 0.02, n_years)
    a4x = CubeList(
        [
            _monthly_cube(
                "ts",
                ts0,
                "K",
                n_years=n_years,
                series=np.repeat(delta_ts, 12),
            ),
            _monthly_cube(
                "rlutcs",
                rlutcs0,
                "W m-2",
                n_years=n_years,
                series=np.repeat(delta_rlutcs, 12),
            ),
        ],
    )
    return {"picontrol": picontrol, "4xco2": a4x}


def test_clear_sky_feedback_gate_recovers_the_forced_slope() -> None:
    """I.3a: OLS slope of annual global-mean Δrlutcs on Δts, abrupt-4xCO2.

    Zhang et al. (2020)'s GCM-consensus forced feedback is 1.9 W/m²/K; a
    model built with exactly that slope (plus small noise) over 150 years
    must recover it to within ±0.05 and pass the ±25% gate.
    """
    data = _clear_sky_experiments(slope=1.9)
    diag = ClearSkyFeedbackGate("clear_sky_feedback", fail_on_missing_data=True)
    output = diag.get_output(data, _info())
    metrics = output.metrics.to_pandas().set_index("var_id")
    slope = metrics.loc["clear_sky_lw_feedback", "value"]
    assert slope == pytest.approx(1.9, abs=0.05)
    assert metrics.loc["clear_sky_lw_feedback", "passes"] == 1.0

    raw = output.raw_output.to_pandas()
    assert {
        "clear_sky_lw_feedback",
        "clear_sky_lw_feedback_intercept",
        "clear_sky_lw_feedback_r2",
        "clear_sky_lw_feedback_n_years",
    } <= set(raw.columns)
    # Each scalar output is its own row (NaN elsewhere), like EnergyBalanceGate's
    # raw_output — so pick out the row that actually carries n_years rather than
    # assuming a row position.
    n_years_col = raw["clear_sky_lw_feedback_n_years"].dropna()
    assert n_years_col.iloc[0] == 150


def test_clear_sky_feedback_gate_fails_a_weaker_forced_slope() -> None:
    """A forced slope of 1.0 W/m²/K sits outside ±25% of 1.9 → fails."""
    data = _clear_sky_experiments(slope=1.0, seed=4)
    diag = ClearSkyFeedbackGate("clear_sky_feedback", fail_on_missing_data=True)
    output = diag.get_output(data, _info())
    metrics = output.metrics.to_pandas().set_index("var_id")
    slope = metrics.loc["clear_sky_lw_feedback", "value"]
    assert slope == pytest.approx(1.0, abs=0.05)
    assert metrics.loc["clear_sky_lw_feedback", "passes"] == 0.0


def test_superset_keys_accepted() -> None:
    """CB2 complex diagnostics tolerate extra experiment keys (one suite dict)."""
    diag = EnergyBalanceGate("energy_balance", fail_on_missing_data=True)
    data = {
        "picontrol": _picontrol_cubes(),
        "4xco2": CubeList([]),  # extra key, unused
        "histaer": CubeList([]),
    }
    output = diag.get_output(data, _info())
    assert output.metrics is not None

    with pytest.raises(ValueError, match="Missing keys"):
        diag.get_output({"4xco2": CubeList([])}, _info())


# ---------------------------------------------------------------------------
# I.7 aerosol forcing: the decadal window and the parallel piControl segment
# ---------------------------------------------------------------------------

#: piControl 1850-1909 with a warming drift; hist-aer 1870-1899; the child
#: branched from piControl 1860, i.e. 10 yr before its own first year.
PI_DRIFT_PER_YEAR = 0.02
PI_START, PI_YEARS = 1850, 60
AER_START, AER_YEARS = 1870, 30
BRANCH_YEAR = 1860


def _flux_cubes(
    toa_net: float,
    tas: float,
    *,
    start_year: int,
    n_years: int,
    tas_trend: float = 0.0,
    toa_trend: float = 0.0,
) -> CubeList:
    """tas + the three TOA fluxes of a run, with ``N = toa_net``."""
    common = {"start_year": start_year, "n_years": n_years}
    return CubeList(
        [
            _monthly_cube("tas", tas, "K", trend_per_year=tas_trend, **common),
            _monthly_cube("rsdt", 340.0, "W m-2", **common),
            _monthly_cube("rsut", 100.0, "W m-2", **common),
            # N = rsdt - rsut - rlut, so a rising rlut lowers N
            _monthly_cube(
                "rlut",
                240.0 - toa_net,
                "W m-2",
                trend_per_year=-toa_trend,
                **common,
            ),
        ],
    )


def _aerosol_experiments(*, with_branch_metadata: bool) -> dict:
    picontrol = _flux_cubes(
        0.0,
        288.0,
        start_year=PI_START,
        n_years=PI_YEARS,
        tas_trend=PI_DRIFT_PER_YEAR,
    )
    # abrupt-4xCO2 with a Gregory slope of exactly -1 W/m2/K
    a4x = _flux_cubes(
        8.0,
        289.0,
        start_year=PI_START,
        n_years=40,
        tas_trend=0.02,
        toa_trend=-0.02,
    )
    histaer = _flux_cubes(
        -0.2,
        287.5,
        start_year=AER_START,
        n_years=AER_YEARS,
    )
    if with_branch_metadata:
        for cube in histaer:
            cube.attributes["branch_time_in_parent"] = float(
                (BRANCH_YEAR - PI_START) * 360,
            )
            cube.attributes["parent_time_units"] = "days since 1850-01-01"
    return {"picontrol": picontrol, "4xco2": a4x, "histaer": histaer}


def _pi_annual_tas(first_year: int, last_year: int) -> float:
    """Mean of the synthetic control's annual tas over a calendar window."""
    y = np.arange(first_year - PI_START, last_year - PI_START + 1, dtype=float)
    # annual mean of `288 + drift * (y + m/12)` over the 12 months of year y
    return float(288.0 + PI_DRIFT_PER_YEAR * (y + 5.5 / 12.0).mean())


def _aerosol_values(*, with_branch_metadata: bool) -> dict[str, float]:
    from climatebench2.diags import AerosolForcingGate

    diag = AerosolForcingGate("aerosol_forcing", fail_on_missing_data=True)
    output = diag.get_output(
        _aerosol_experiments(with_branch_metadata=with_branch_metadata),
        _info(),
    )
    # one row per emitted scalar (nothing joins them), so collapse the column
    raw = output.raw_output.to_pandas().bfill().iloc[0]
    metrics = output.metrics.to_pandas().set_index("var_id")
    return {"raw": raw, "metrics": metrics}


def test_aerosol_gate_uses_the_decadal_window_clipped_to_the_record() -> None:
    """I.7: the 2010-2019 window slides back to the end of a short hist-aer."""
    out = _aerosol_values(with_branch_metadata=True)
    raw = out["raw"]
    # hist-aer stops in 1899, so the last decade available is 1890-1899
    assert raw["window_first_year"] == AER_START + AER_YEARS - 10
    assert raw["window_last_year"] == AER_START + AER_YEARS - 1


def test_aerosol_gate_removes_drift_with_the_parallel_control_segment() -> None:
    """Anomalies are against the concurrent control years, not the whole run."""
    out = _aerosol_values(with_branch_metadata=True)
    raw = out["raw"]
    assert raw["parallel_segment"] == 1.0
    # window 1890-1899 branched at piControl 1860 from a run starting 1870
    # -> the parallel control segment is 1880-1889
    expected = 287.5 - _pi_annual_tas(1880, 1889)
    assert raw["delta_t_end"] == pytest.approx(expected, abs=1e-3)
    # ... which differs from the long-term-mean answer this used to give
    assert raw["delta_t_end"] != pytest.approx(
        287.5 - _pi_annual_tas(PI_START, PI_START + PI_YEARS - 1),
        abs=1e-3,
    )
    assert raw["lambda_4x"] == pytest.approx(-1.0, abs=0.02)
    assert raw["aerosol_erf_wm2"] == pytest.approx(
        raw["delta_n_end"] - raw["lambda_4x"] * raw["delta_t_end"],
        abs=1e-6,
    )
    metrics = out["metrics"]
    assert metrics.loc["aerosol_cooling", "passes"] == 1.0
    assert metrics.loc["aerosol_erf", "passes"] == 1.0


def test_aerosol_gate_falls_back_to_the_long_term_control_mean() -> None:
    """No branch metadata -> the long-term mean, flagged and warned about."""
    out = _aerosol_values(with_branch_metadata=False)
    raw = out["raw"]
    assert raw["parallel_segment"] == 0.0
    assert raw["delta_t_end"] == pytest.approx(
        287.5 - _pi_annual_tas(PI_START, PI_START + PI_YEARS - 1),
        abs=1e-3,
    )


def test_all_tier1_gates_are_cb2_complex() -> None:
    """The CB2-owned gates (no upstream provider) share the CB2 base class."""
    from climatebench2 import diags

    for name in (
        "EnergyBalanceGate",
        "ClosureGate",
        "ClearSkyFeedbackGate",
        "AerosolForcingGate",
        "ITCZEFEGate",
        "BjerknesGate",
        "CCScalingGate",
    ):
        assert issubclass(getattr(diags, name), CB2ComplexDiagnostic)
        assert getattr(diags, name)._gate_checks  # every gate has checks wired


def test_upstream_gate_wrappers_subclass_climateeval_diagnostics() -> None:
    """I.6a/b/c and I.8a are thin gates over ClimateEval's own diagnostics."""
    from climateeval.diags.complex import (
        ECS,
        ArcticAmplification,
        LandOceanWarmingRatio,
        MeridionalHeatTransport,
    )

    from climatebench2 import diags
    from climatebench2.diags.pass_fail import GateMixin, SupersetExperimentMixin

    for gate, upstream in (
        (diags.ECSGate, ECS),
        (diags.LandOceanWarmingGate, LandOceanWarmingRatio),
        (diags.ArcticAmplificationGate, ArcticAmplification),
        (diags.MeridionalHeatTransportGate, MeridionalHeatTransport),
    ):
        assert issubclass(gate, upstream)
        assert issubclass(gate, GateMixin)
        assert issubclass(gate, SupersetExperimentMixin)
        # No CB2 re-implementation of the upstream computation
        assert "_calculate_raw_output" not in vars(gate)


def test_upstream_gate_wrappers_carry_the_protocol_thresholds() -> None:
    from climatebench2 import diags
    from climatebench2.diags.pass_fail import resolved_checks

    (land_ocean,) = resolved_checks(diags.LandOceanWarmingGate)
    assert land_ocean.check_id == "land_ocean_warming"
    assert land_ocean.column == "land_ocean_warming_ratio"
    assert (land_ocean.lower, land_ocean.upper) == (1.2, 1.6)

    (arctic,) = resolved_checks(diags.ArcticAmplificationGate)
    assert arctic.column == "arctic_amplification"
    assert (arctic.lower, arctic.upper) == (1.5, None)

    bounds = {
        check.check_id: (check.column, check.lower, check.upper)
        for check in resolved_checks(diags.MeridionalHeatTransportGate)
    }
    assert bounds["omet_peak"] == ("omet_peak", 1.5, 2.0)
    assert bounds["omet_peak_lat"] == ("omet_peak_lat", 15.0, 20.0)
    assert bounds["amet_peak"] == ("amet_peak", 4.0, 5.0)
    # 41 +/- 2.5 degN since 00f4e49 (Duncan 2026-09-21); was 45 +/- 5
    assert bounds["amet_peak_lat"] == ("amet_peak_lat", 38.5, 43.5)


def test_upstream_gate_wrappers_feed_thresholds_into_upstream_kwargs() -> None:
    """Protocol constants reach the upstream diagnostic, not the suite YAML."""
    from climatebench2 import diags

    land_ocean = diags.LandOceanWarmingGate("land_ocean_warming")
    assert land_ocean._equilibrium_years == 50

    arctic = diags.ArcticAmplificationGate("arctic_amplification")
    assert arctic._equilibrium_years == 50
    assert arctic._arctic_latitude == pytest.approx(66.5)

    mht = diags.MeridionalHeatTransportGate("meridional_heat_transport")
    assert tuple(mht._omet_search_band) == (5, 30)
    assert tuple(mht._amet_search_band) == (25, 55)

    # An explicit suite kwarg still wins over the thresholds.yml default
    override = diags.ArcticAmplificationGate("arctic", arctic_latitude=70.0)
    assert override._arctic_latitude == pytest.approx(70.0)
