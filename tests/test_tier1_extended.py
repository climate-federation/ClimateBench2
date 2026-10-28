"""Tests for the extended Tier I gates (I.3b, I.3c and I.5c).

I.3c and I.5c are run end-to-end on synthetic cubes with the observational
fetch monkeypatched — no network, no CDS credentials, no real climate data
(see tests/README.md). I.3b needs no observations, so its synthetic daily
fields go straight through the gate.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest

climateeval = pytest.importorskip("climateeval")

from cf_units import Unit  # noqa: E402
from iris.coords import DimCoord  # noqa: E402
from iris.cube import Cube, CubeList  # noqa: E402

from climatebench2 import physics  # noqa: E402
from climatebench2._thresholds import get_threshold  # noqa: E402
from climatebench2.diags import ENSOTeleconnectionsGate, PrecipBuoyancyGate  # noqa: E402
from climatebench2.diags.tier1_extended import restrict_to_years  # noqa: E402

from test_tier1_physics import _info, _monthly_cube  # noqa: E402

if TYPE_CHECKING:
    import pandas as pd

N_YEARS = 20
LATS = np.linspace(-85.0, 85.0, 18)
LONS = np.linspace(5.0, 355.0, 36)


def _enso_pattern() -> np.ndarray:
    """A smooth (lat, lon) teleconnection pattern, positive over Niño-3.4."""
    return (
        np.cos(np.deg2rad(LATS))[:, None]
        * np.cos(np.deg2rad(LONS - 215.0))[None, :]
        + 0.3
    )


# ---------------------------------------------------------------------------
# I.5c ENSO teleconnection patterns
# ---------------------------------------------------------------------------


def _enso_cubes(
    seed: int,
    *,
    ts_sign: float = 1.0,
    pr_sign: float = 1.0,
    start_year: int = 1850,
) -> dict[str, Cube]:
    """tos (uniform ENSO index) + ts/pr carrying the pattern times that index."""
    rng = np.random.default_rng(seed)
    index = rng.normal(0.0, 1.0, N_YEARS * 12)
    pattern = _enso_pattern()
    common = {"n_years": N_YEARS, "series": index, "start_year": start_year}
    return {
        "tos": _monthly_cube("tos", 27.0, "degC", **common),
        "ts": _monthly_cube("ts", 288.0, "K", field=ts_sign * pattern, **common),
        "pr": _monthly_cube(
            "pr",
            3.0e-5,
            "kg m-2 s-1",
            field=pr_sign * pattern * 1.0e-6,
            **common,
        ),
    }


def _observed_cubes(*, pr_sign: float = 1.0) -> dict[str, Cube]:
    """HadISST-like ``tos`` (SST *and* index) and GPCP-like ``pr``."""
    obs = _enso_cubes(99, pr_sign=pr_sign, start_year=1979)
    # the observed temperature reference is SST, not ts: give tos the pattern
    pattern = _enso_pattern()
    rng = np.random.default_rng(99)
    index = rng.normal(0.0, 1.0, N_YEARS * 12)
    obs["tos"] = _monthly_cube(
        "tos",
        27.0,
        "degC",
        n_years=N_YEARS,
        series=index,
        field=pattern,
        start_year=1979,
    )
    return obs


def _run_teleconnections(
    monkeypatch: pytest.MonkeyPatch,
    model: dict[str, Cube],
    observed: dict[str, Cube] | None,
    *,
    fail_on_missing_data: bool = False,
):  # noqa: ANN201
    def fake_observation_cube(_self, _source, var_name: str) -> Cube:
        if observed is None:
            msg = "no network in the test environment"
            raise RuntimeError(msg)
        return observed[var_name]

    monkeypatch.setattr(
        ENSOTeleconnectionsGate,
        "_observation_cube",
        fake_observation_cube,
    )
    diag = ENSOTeleconnectionsGate(
        "enso_teleconnections",
        fail_on_missing_data=fail_on_missing_data,
    )
    return diag.get_output({"picontrol": CubeList(model.values())}, _info())


def test_teleconnection_patterns_matching_observations_score_one(monkeypatch) -> None:  # noqa: ANN001
    """A model whose ts/pr patterns equal the observed ones scores r ≈ 1."""
    output = _run_teleconnections(monkeypatch, _enso_cubes(3), _observed_cubes())
    metrics = output.metrics.to_pandas().set_index("var_id")
    for check in ("enso_teleconnection_ts", "enso_teleconnection_pr"):
        assert metrics.loc[check, "value"] == pytest.approx(1.0, abs=0.02)
        assert metrics.loc[check, "passes"] == 1.0
        assert metrics.loc[check, "bound_lower"] == 0.7
        assert metrics.loc[check, "requirement"] == "required"
    raw = output.raw_output.to_pandas().bfill().iloc[0]
    assert raw["teleconnection_obs_first_year"] == 1979.0
    assert raw["teleconnection_obs_last_year"] == 2014.0
    assert raw["ts_pattern_rms"] > 0.0


def test_sign_flipped_teleconnection_patterns_score_minus_one(monkeypatch) -> None:  # noqa: ANN001
    """Flip the model's ts and the observed pr: both correlations go to −1."""
    output = _run_teleconnections(
        monkeypatch,
        _enso_cubes(3, ts_sign=-1.0),
        _observed_cubes(pr_sign=-1.0),
    )
    metrics = output.metrics.to_pandas().set_index("var_id")
    for check in ("enso_teleconnection_ts", "enso_teleconnection_pr"):
        assert metrics.loc[check, "value"] == pytest.approx(-1.0, abs=0.02)
        assert metrics.loc[check, "passes"] == 0.0


def test_teleconnections_without_observations_emit_no_gate_rows(monkeypatch) -> None:  # noqa: ANN001
    """No network: the model's pattern summary, a logged reason, no gate."""
    output = _run_teleconnections(monkeypatch, _enso_cubes(3), None)
    if output.metrics is not None:
        assert output.metrics.to_pandas().empty
    raw = output.raw_output.to_pandas().bfill().iloc[0]
    assert raw["nino34_std"] == pytest.approx(1.0, abs=0.1)
    assert raw["ts_pattern_rms"] > 0.0
    assert "teleconnection_corr_ts" not in raw.index


def test_teleconnections_reraise_when_failing_on_missing_data(monkeypatch) -> None:  # noqa: ANN001
    with pytest.raises(RuntimeError, match="no network"):
        _run_teleconnections(
            monkeypatch,
            _enso_cubes(3),
            None,
            fail_on_missing_data=True,
        )


# ---------------------------------------------------------------------------
# I.3c tropical precipitation–buoyancy
# ---------------------------------------------------------------------------

PLEV = np.array([100000.0, 85000.0, 50000.0, 25000.0])
PR_PER_UNIT_TA = 4.0e-5  # kg m-2 s-1 per K of the (uniform) ta anomaly
#: The protocol window, tier1.precip_buoyancy.obs_window (paper App. B.3).
WINDOW = (1983, 2014)


def _level_cube(
    var_name: str,
    value: float,
    units: str,
    *,
    n_years: int,
    series: np.ndarray | None = None,
    start_year: int = 1850,
    profile: np.ndarray | None = None,
) -> Cube:
    """A (time, plev, lat, lon) cube, constant in space.

    ``series`` is constant in height unless ``profile`` (one weight per level
    of ``PLEV``, which runs top-down from 1000 hPa as CMIP6 files do) shapes it.
    """
    n_time = n_years * 12
    time = DimCoord(
        np.arange(n_time, dtype=float) * 30.0 + 15.0 + (start_year - 1850) * 360.0,
        standard_name="time",
        units=Unit("days since 1850-01-01", calendar="360_day"),
    )
    plev = DimCoord(PLEV, standard_name="air_pressure", units="Pa")
    lat = DimCoord(LATS, standard_name="latitude", units="degrees")
    lon = DimCoord(LONS, standard_name="longitude", units="degrees", circular=True)
    for coord in (time, lat, lon):
        coord.guess_bounds()
    values = np.full(n_time, value, dtype=float)
    if series is not None:
        values = values + np.asarray(series, dtype=float)
    weights = np.ones(PLEV.size) if profile is None else np.asarray(profile, float)
    offsets = values - value
    data = np.broadcast_to(
        (value + offsets[:, None] * weights[None, :])[:, :, None, None],
        (n_time, PLEV.size, LATS.size, LONS.size),
    ).astype(np.float32)
    return Cube(
        data.copy(),
        var_name=var_name,
        units=units,
        dim_coords_and_dims=[(time, 0), (plev, 1), (lat, 2), (lon, 3)],
    )


def _buoyancy_cubes(
    seed: int = 5,
    *,
    start_year: int = WINDOW[0],
    n_years: int = 5,
    outside_window_factor: float = 1.0,
    zg: str | None = "flat",
) -> CubeList:
    """pr responding to a uniform ta anomaly — but only inside 25S–25N.

    ``outside_window_factor`` scales the precipitation response in the years
    outside the protocol window, so a gate that regressed the whole record
    would recover a different slope. ``zg`` is ``"flat"`` (a constant
    height), ``"wrong"`` (large noise correlated with the ta anomaly — what a
    gate that still read the model's ``zg`` would see) or ``None`` (absent).
    """
    rng = np.random.default_rng(seed)
    anomaly = rng.normal(0.0, 1.0, n_years * 12)
    years = start_year + np.arange(n_years * 12) // 12
    inside = (years >= WINDOW[0]) & (years <= WINDOW[1])
    response = PR_PER_UNIT_TA * np.where(inside, 1.0, outside_window_factor)
    # 1 in the tropics, 5 outside: a global regression would see a steeper
    # slope than the 20S-20N one the protocol asks for
    weight = np.where(np.abs(LATS) <= 25.0, 1.0, 5.0)[:, None] * np.ones(LONS.size)
    common = {"n_years": n_years, "start_year": start_year}
    cubes = [
        _monthly_cube(
            "pr",
            3.0e-5,
            "kg m-2 s-1",
            series=response * anomaly,
            field=weight,
            **common,
        ),
        _level_cube("ta", 250.0, "K", series=anomaly, **common),
        _level_cube("hus", 0.005, "1", **common),
    ]
    if zg == "flat":
        cubes.append(_level_cube("zg", 5000.0, "m", **common))
    elif zg == "wrong":
        noise = 500.0 * anomaly + 300.0 * rng.normal(0.0, 1.0, anomaly.size)
        cubes.append(_level_cube("zg", 5000.0, "m", series=noise, **common))
    return CubeList(cubes)


def _expected_slope(profile: np.ndarray | None = None) -> float:
    """mm/day per MJ/m² for the synthetic column.

    Only ``ta`` varies — uniformly in height, or shaped by ``profile`` — and
    the geopotential is the hydrostatic one of that ``ta``, so ⟨h⟩ moves by
    ``c_p·Δp/g`` plus the hypsometric ``R_d·T_v`` term per K.
    """
    weights = np.ones(PLEV.size) if profile is None else np.asarray(profile, float)

    def column(temperature: float) -> float:
        ta = 250.0 + (temperature - 250.0) * weights
        hus = np.full(PLEV.size, 0.005)
        z = physics.hydrostatic_height(ta, hus, PLEV)
        mse = physics.moist_static_energy(ta, z, hus)
        return float(physics.mass_weighted_column_integral(mse, PLEV))

    d_column = (column(251.0) - column(250.0)) / 1.0e6  # MJ/m2 per K
    return PR_PER_UNIT_TA * physics.SECONDS_PER_DAY / d_column


def _run_precip_buoyancy(
    monkeypatch: pytest.MonkeyPatch,
    reference: float | None,
    cubes: CubeList | None = None,
):  # noqa: ANN201
    def fake_observed_slope(_self, model, _levels=None):  # noqa: ANN001, ANN202
        if reference is None:
            msg = "no network in the test environment"
            raise RuntimeError(msg)
        return reference, model

    monkeypatch.setattr(PrecipBuoyancyGate, "_observed_slope", fake_observed_slope)
    diag = PrecipBuoyancyGate("precip_buoyancy", fail_on_missing_data=False)
    data = _buoyancy_cubes() if cubes is None else cubes
    return diag.get_output({"historical": data}, _info())


def _raw(output) -> pd.Series:  # noqa: ANN001
    return output.raw_output.to_pandas().bfill().iloc[0]


def test_precip_buoyancy_window_is_the_papers() -> None:
    """Paper Table 1 / App. B.3: the slope is taken over 1983–2014."""
    assert tuple(get_threshold("tier1.precip_buoyancy.obs_window")) == WINDOW


def test_precip_buoyancy_slope_is_the_tropical_pooled_regression(monkeypatch) -> None:  # noqa: ANN001
    """The recovered slope is the 20S–20N one, not the global one."""
    expected = _expected_slope()
    output = _run_precip_buoyancy(monkeypatch, expected / 1.1)
    raw = _raw(output)
    assert raw["precip_buoyancy_slope"] == pytest.approx(expected, rel=0.02)
    assert raw["precip_buoyancy_reference_from_obs"] == 1.0
    metrics = output.metrics.to_pandas().set_index("var_id")
    # |slope/ref - 1| = 0.1 <= 0.30
    assert metrics.loc["precip_buoyancy", "value"] == pytest.approx(0.1, abs=0.02)
    assert metrics.loc["precip_buoyancy", "passes"] == 1.0
    assert metrics.loc["precip_buoyancy", "bound_upper"] == 0.3


def test_precip_buoyancy_fails_a_slope_outside_the_tolerance(monkeypatch) -> None:  # noqa: ANN001
    output = _run_precip_buoyancy(monkeypatch, _expected_slope() / 2.0)
    metrics = output.metrics.to_pandas().set_index("var_id")
    assert metrics.loc["precip_buoyancy", "value"] == pytest.approx(1.0, abs=0.05)
    assert metrics.loc["precip_buoyancy", "passes"] == 0.0


def test_precip_buoyancy_integrates_the_reference_over_the_model_levels() -> None:
    """The reference column must use the *submission's* pressure levels.

    Regression test: the observed slope was integrated over ERA5's 37
    levels while the model's used CMIP6 ``plev19``, and the pooled slope
    appeared to depend strongly on that discretisation (0.146 mm/day per
    MJ/m2 on 37 levels, 0.034 on ``plev19`` — an artefact of the
    level-ordering bug tested below); the paper integrates the reference
    over the submission's own levels regardless.
    """
    recorded: list[list[float] | None] = []
    gate = PrecipBuoyancyGate("precip_buoyancy", fail_on_missing_data=False)
    original = PrecipBuoyancyGate._slope  # noqa: SLF001

    def record(self, pr, ta, hus, levels=None):  # noqa: ANN001, ANN202
        recorded.append(levels)
        return original(self, pr, ta, hus, levels)

    reference_levels: list[float] = []

    def fake_observed_slope(_self, model, levels=None):  # noqa: ANN001, ANN202
        reference_levels.extend(levels or [])
        return 1.0, model

    PrecipBuoyancyGate._slope = record  # noqa: SLF001
    try:
        gate._observed_slope = fake_observed_slope.__get__(gate)  # noqa: SLF001
        gate.get_output({"historical": _buoyancy_cubes()}, _info())
    finally:
        PrecipBuoyancyGate._slope = original  # noqa: SLF001

    # The model's own levels reached both the model slope and the reference
    assert recorded == [sorted(PLEV)]
    assert reference_levels == sorted(PLEV)


def test_precip_buoyancy_pairs_pr_and_the_column_by_calendar_month() -> None:
    """GPCP starts four years after ERA5; the slope must still be recovered.

    Regression test: ``_slope`` used to truncate both sides to the shorter
    record's *length*, which regressed 1983-2014 precipitation on 1979-2010
    column MSE and turned the observed reference slope of I.3c negative
    (model/obs relative error 6.01 against a bound of 0.30).
    """
    n_column_years, lag_years = 12, 4
    rng = np.random.default_rng(11)
    anomaly = rng.normal(0.0, 1.0, n_column_years * 12)
    common = {"n_years": n_column_years, "start_year": 1979}
    ta = _level_cube("ta", 250.0, "K", series=anomaly, **common)
    hus = _level_cube("hus", 0.005, "1", **common)
    # ...and a precipitation record that starts four years later, responding
    # to the *same* months of the column anomaly.
    pr = _monthly_cube(
        "pr",
        3.0e-5,
        "kg m-2 s-1",
        n_years=n_column_years - lag_years,
        start_year=1979 + lag_years,
        series=PR_PER_UNIT_TA * anomaly[lag_years * 12 :],
    )
    gate = PrecipBuoyancyGate("precip_buoyancy", fail_on_missing_data=False)
    slope = gate._slope(pr, ta, hus)  # noqa: SLF001
    assert slope == pytest.approx(_expected_slope(), rel=0.02)


#: A ta anomaly that decays with height on the top-down ``PLEV``.
BOTTOM_HEAVY = np.array([1.0, 0.6, 0.2, 0.0])


def test_precip_buoyancy_pairs_each_level_with_its_own_pressure() -> None:
    """The column integral must weight each level by its OWN pressure.

    Regression test (2026-10-27): ``_column_mse`` passed the ascending sorted
    level list to ``mass_weighted_column_integral`` as the pressure of each
    level, but ``extract_levels`` keeps a file's own order when the levels
    are a subset of its own, and CMIP6 and ERA5 files run top-down from
    1000 hPa — so the 1000 hPa field was integrated as if it sat at the top
    of the column. A height-uniform anomaly (every other test here) cannot
    see that; one that decays with height gives a 25 % different column.
    """
    rng = np.random.default_rng(17)
    anomaly = rng.normal(0.0, 1.0, 5 * 12)
    common = {"n_years": 5, "start_year": WINDOW[0]}
    ta = _level_cube("ta", 250.0, "K", series=anomaly, profile=BOTTOM_HEAVY, **common)
    hus = _level_cube("hus", 0.005, "1", **common)
    pr = _monthly_cube(
        "pr",
        3.0e-5,
        "kg m-2 s-1",
        series=PR_PER_UNIT_TA * anomaly,
        **common,
    )
    gate = PrecipBuoyancyGate("precip_buoyancy", fail_on_missing_data=False)
    slope = gate._slope(pr, ta, hus)  # noqa: SLF001
    assert slope == pytest.approx(_expected_slope(BOTTOM_HEAVY), rel=0.02)
    assert slope != pytest.approx(_expected_slope(), rel=0.05)


def test_precip_buoyancy_without_a_reference_emits_the_model_slope(monkeypatch) -> None:  # noqa: ANN001
    """No observations and a null stored slope: report, do not gate."""
    output = _run_precip_buoyancy(monkeypatch, None)
    raw = _raw(output)
    assert raw["precip_buoyancy_slope"] == pytest.approx(_expected_slope(), rel=0.02)
    assert "precip_buoyancy_slope_rel_error" not in raw.index
    if output.metrics is not None:
        assert output.metrics.to_pandas().empty


# -- one window, one month set, one column statistic ------------------------


def _observations(
    *,
    era5_last_year: int = WINDOW[1],
    seed: int = 21,
) -> dict[str, Cube]:
    """GPCP-like ``pr`` from 1983 and ERA5-like ``ta``/``hus`` from 1979."""
    era5_years = era5_last_year - 1979 + 1
    rng = np.random.default_rng(seed)
    anomaly = rng.normal(0.0, 1.0, (WINDOW[1] - 1979 + 1) * 12)
    gpcp_start = (WINDOW[0] - 1979) * 12
    return {
        "pr": _monthly_cube(
            "pr",
            3.0e-5,
            "kg m-2 s-1",
            n_years=WINDOW[1] - WINDOW[0] + 1,
            start_year=WINDOW[0],
            series=PR_PER_UNIT_TA * anomaly[gpcp_start:],
        ),
        "ta": _level_cube(
            "ta",
            250.0,
            "K",
            n_years=era5_years,
            start_year=1979,
            series=anomaly[: era5_years * 12],
        ),
        "hus": _level_cube("hus", 0.005, "1", n_years=era5_years, start_year=1979),
    }


#: The unpatched slope, so nested recorders never wrap one another.
_PRECIP_BUOYANCY_SLOPE = PrecipBuoyancyGate._slope  # noqa: SLF001


def _run_against_observations(
    monkeypatch: pytest.MonkeyPatch,
    cubes: CubeList,
    observations: dict[str, Cube],
) -> tuple[pd.Series, list[tuple[str, int]]]:
    """Run the gate with the GPCP/ERA5 fetch replaced; record every slope's months."""
    monkeypatch.setattr(
        PrecipBuoyancyGate,
        "_observation_cube",
        lambda _self, _source, var_name: observations[var_name].copy(),
    )
    calls: list[tuple[str, int]] = []

    def record(self, pr, ta, hus, levels=None):  # noqa: ANN001, ANN202
        # the model is on the 2° grid; the patched observations stay on 10°
        side = "reference" if pr.coord("latitude").points.size == LATS.size else "model"
        calls.append((side, ta.coord("time").points.size))
        return _PRECIP_BUOYANCY_SLOPE(self, pr, ta, hus, levels)

    monkeypatch.setattr(PrecipBuoyancyGate, "_slope", record)
    diag = PrecipBuoyancyGate("precip_buoyancy", fail_on_missing_data=False)
    return _raw(diag.get_output({"historical": cubes}, _info())), calls


def test_precip_buoyancy_regresses_the_model_over_the_reference_window(monkeypatch) -> None:  # noqa: ANN001
    """A 1850–2100 record gives the slope of the same record cut to 1983–2014.

    Regression test: the CLI hands Tier I the first member's FULL historical
    record (sometimes spliced to 2100) and the gate regressed all of it,
    against a reference taken over 1983–2014 only — on CNRM-CM6-1 that alone
    moved the relative error from ≈ 0.17 to 0.32. Outside the window this
    synthetic model's precipitation responds three times as strongly, so a
    full-record regression would miss the in-window slope by a factor ~2.5.
    """
    full = _buoyancy_cubes(
        start_year=1850,
        n_years=2100 - 1850 + 1,
        outside_window_factor=3.0,
    )
    pre_cut = CubeList(
        [restrict_to_years(cube, *WINDOW) for cube in full],
    )
    raw_full, calls = _run_against_observations(monkeypatch, full, _observations())
    raw_cut, _ = _run_against_observations(monkeypatch, pre_cut, _observations())

    assert raw_full["precip_buoyancy_slope"] == pytest.approx(
        raw_cut["precip_buoyancy_slope"],
        rel=1e-9,
    )
    assert raw_full["precip_buoyancy_slope"] == pytest.approx(
        _expected_slope(),
        rel=0.02,
    )
    assert raw_full["precip_buoyancy_slope_ref"] == pytest.approx(
        _expected_slope(),
        rel=0.02,
    )
    assert raw_full["precip_buoyancy_slope_rel_error"] < 0.05  # noqa: PLR2004
    n_months = (WINDOW[1] - WINDOW[0] + 1) * 12
    assert raw_full["precip_buoyancy_n_months"] == n_months
    first_last = ("precip_buoyancy_first_year", "precip_buoyancy_last_year")
    assert tuple(raw_full[list(first_last)]) == WINDOW
    # both sides regressed over the same 384 months (ERA5's 1979-1982 dropped)
    assert sorted(calls) == [("model", n_months), ("reference", n_months)]


def test_precip_buoyancy_uses_the_identical_month_set_on_both_sides(monkeypatch) -> None:  # noqa: ANN001
    """A reference that ends early shortens the model's regression too."""
    full = _buoyancy_cubes(start_year=1850, n_years=2100 - 1850 + 1)
    raw, calls = _run_against_observations(
        monkeypatch,
        full,
        _observations(era5_last_year=2010),
    )
    n_months = (2010 - WINDOW[0] + 1) * 12
    assert raw["precip_buoyancy_n_months"] == n_months
    assert raw["precip_buoyancy_last_year"] == 2010.0  # noqa: PLR2004
    assert sorted(calls) == [("model", n_months), ("reference", n_months)]


def test_precip_buoyancy_keeps_the_window_with_the_stored_reference(monkeypatch) -> None:  # noqa: ANN001
    """No observations, a stored slope: the model is still cut to 1983–2014."""
    from climatebench2.diags import tier1_extended

    stored = _expected_slope() / 1.1
    real = tier1_extended.get_threshold

    def thresholds(path: str):  # noqa: ANN202
        if path == "tier1.precip_buoyancy.reference_slope":
            return stored
        return real(path)

    monkeypatch.setattr(tier1_extended, "get_threshold", thresholds)
    full = _buoyancy_cubes(
        start_year=1850,
        n_years=2100 - 1850 + 1,
        outside_window_factor=3.0,
    )
    raw = _raw(_run_precip_buoyancy(monkeypatch, None, full))
    assert raw["precip_buoyancy_reference_from_obs"] == 0.0
    assert raw["precip_buoyancy_slope_ref"] == pytest.approx(stored)
    assert raw["precip_buoyancy_slope"] == pytest.approx(_expected_slope(), rel=0.02)
    assert raw["precip_buoyancy_n_months"] == (WINDOW[1] - WINDOW[0] + 1) * 12


def test_precip_buoyancy_builds_the_geopotential_hydrostatically_on_both_sides(
    monkeypatch,  # noqa: ANN001
) -> None:
    """A model's own ``zg`` never enters the column: wrong or absent, same slope.

    Regression test: the reference column used a hydrostatic geopotential
    (ClimateEval's ERA5 mapping has no ``zg``) while the model used its
    published ``zg`` — on CNRM-CM6-1 a +27 % move of the model slope while
    the level-ordering bug was present. Both sides now build ``g·z`` from
    their own ``ta``/``hus``.
    """
    reference = _expected_slope()
    slopes = {
        zg: _raw(
            _run_precip_buoyancy(monkeypatch, reference, _buoyancy_cubes(zg=zg)),
        )["precip_buoyancy_slope"]
        for zg in ("flat", "wrong", None)
    }
    assert slopes["wrong"] == pytest.approx(slopes["flat"], rel=1e-9)
    assert slopes[None] == pytest.approx(slopes["flat"], rel=1e-9)
    assert slopes[None] == pytest.approx(reference, rel=0.02)


def test_restrict_to_years_refuses_a_record_outside_the_window() -> None:
    cube = _monthly_cube("pr", 3.0e-5, "kg m-2 s-1", n_years=5, start_year=1850)
    with pytest.raises(ValueError, match="no month in 1983-2014"):
        restrict_to_years(cube, *WINDOW)
    inside = restrict_to_years(cube, 1851, 1852)
    assert inside.coord("time").points.size == 24  # noqa: PLR2004


# ---------------------------------------------------------------------------
# I.3b geostrophic balance — through the gate, on a masked pressure level
# ---------------------------------------------------------------------------

#: The common 2°×2° grid every CB2 gate regrids to (``DEFAULT_GRID``), so the
#: gate's own regrid is the identity and the test measures only its physics.
_GEO_LATS = np.arange(-89.0, 90.0, 2.0)
_GEO_LONS = np.arange(1.0, 360.0, 2.0)
_GEO_PLEV = np.array([100000.0, 85000.0, 70000.0])
_GEO_N_DAYS = 8


def _balanced_zg_and_ua() -> tuple[np.ndarray, np.ndarray]:
    """A geostrophically balanced ``(zg, ua)`` pair, analytically exact.

    ``Z = ½(A + a₁s₁) cos²φ + B s₂ cos λ cos²φ`` has, through
    ``u_g = −(g/f) ∂Z/∂y`` with ``f = 2Ω sin φ``,

        ``u_g = g(A + a₁s₁) cos φ / (2Ω a) + g B s₂ cos λ cos φ / (Ω a)``

    — no finite differences on either side, so a gate that recovers the
    balance must return ρ ≈ 1.
    """
    phi = np.deg2rad(_GEO_LATS)[None, :, None]
    lam = np.deg2rad(_GEO_LONS)[None, None, :]
    day = np.arange(_GEO_N_DAYS, dtype=float)[:, None, None]
    s_1 = np.sin(2.0 * np.pi * day / _GEO_N_DAYS)
    s_2 = np.cos(2.0 * np.pi * day / _GEO_N_DAYS)
    amp, amp_1, amp_wave = 500.0, 150.0, 80.0
    c = amp + amp_1 * s_1
    d = amp_wave * s_2
    zg = 0.5 * c * np.cos(phi) ** 2 + d * np.cos(lam) * np.cos(phi) ** 2
    scale = physics.GRAVITY / (2.0 * physics.OMEGA_EARTH * physics.EARTH_RADIUS_M)
    ua = scale * c * np.cos(phi) + 2.0 * scale * d * np.cos(lam) * np.cos(phi)
    return (
        np.broadcast_to(zg, (_GEO_N_DAYS,) + zg.shape[1:]).copy(),
        np.broadcast_to(ua, (_GEO_N_DAYS,) + ua.shape[1:]).copy(),
    )


def _daily_level_cube(var_name: str, units: str, field: np.ndarray) -> Cube:
    """``(time, plev, lat, lon)`` daily cube carrying ``field`` at every level.

    ``field`` is a ``(time, lat, lon)`` masked array; the mask is kept, which
    is the point of the fixture — CMIP6 publishes 850 hPa ``ua``/``zg`` as a
    masked array wherever the level is below ground.
    """
    time = DimCoord(
        np.arange(field.shape[0], dtype=float) + 0.5,
        standard_name="time",
        units=Unit("days since 2000-01-01", calendar="360_day"),
    )
    plev = DimCoord(
        _GEO_PLEV,
        standard_name="air_pressure",
        units="Pa",
        attributes={"positive": "down"},
    )
    lat = DimCoord(_GEO_LATS, standard_name="latitude", units="degrees")
    lon = DimCoord(_GEO_LONS, standard_name="longitude", units="degrees", circular=True)
    for coord in (lat, lon, time):
        coord.guess_bounds()
    data = np.ma.stack([field] * _GEO_PLEV.size, axis=1)
    return Cube(
        data,
        var_name=var_name,
        units=units,
        dim_coords_and_dims=[(time, 0), (plev, 1), (lat, 2), (lon, 3)],
    )


def _sub_surface_mask() -> np.ndarray:
    """A plateau: 30–60°N, 90–150°E, where 850 hPa is below ground."""
    lat_hit = (_GEO_LATS >= 30.0) & (_GEO_LATS <= 60.0)
    lon_hit = (_GEO_LONS >= 90.0) & (_GEO_LONS <= 150.0)
    return (lat_hit[:, None] & lon_hit[None, :])[None, :, :] & np.ones(
        (_GEO_N_DAYS, 1, 1),
        dtype=bool,
    )


def _geostrophic_correlation(*, masked: bool) -> float:
    from climatebench2.diags import GeostrophicBalanceGate

    zg, ua = _balanced_zg_and_ua()
    if masked:
        # ...exactly as iris hands a CMIP6 file over: values *under* the mask
        # are the file's 1e20 fill value, not anything physical.
        mask = _sub_surface_mask()
        zg = np.ma.masked_array(np.where(mask, 1.0e20, zg), mask=mask)
        ua = np.ma.masked_array(np.where(mask, 1.0e20, ua), mask=mask)
    data = CubeList(
        [
            _daily_level_cube("zg", "m", zg),
            _daily_level_cube("ua", "m s-1", ua),
        ],
    )
    gate = GeostrophicBalanceGate("geostrophic_balance")
    output = gate._calculate_raw_output(SimpleNamespace(data={"day": data}))  # noqa: SLF001
    (cube,) = (c for v, c in output.items() if v.var_name == "geostrophic_corr")
    return float(cube.data)


def test_geostrophic_gate_recovers_a_balanced_wind() -> None:
    """An exactly balanced zg/ua pair must correlate at ρ > 0.99."""
    assert _geostrophic_correlation(masked=False) > 0.99


def test_geostrophic_gate_ignores_sub_surface_fill_values() -> None:
    """A masked 850 hPa plateau must not enter the correlation.

    Regression test: reading the cube with ``np.asarray`` instead of
    ``_filled`` exposed the 1e20 under the mask and drove ρ on real
    CNRM-CM6-1 daily data from 0.99 to 0.09.
    """
    assert _geostrophic_correlation(masked=True) > 0.99
