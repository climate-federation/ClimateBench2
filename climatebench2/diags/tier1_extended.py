"""Extended Tier I gates: causal-response, daily-data and pattern checks.

Phase 4, plus I.3c and the I.5c rewrite of work package 5.

Previously entirely missing from the protocol implementation
(metrics_reference.md status ❌): geostrophic balance (I.3b),
precipitation–buoyancy (I.3c), GFMIP patch Δλ (I.4a), amip-4xCO2 ERF (I.4b),
ENSO teleconnections (I.5c), MJO Wheeler–Kiladis (I.5d).

Data keys (superset semantics as in ``tier1_physics``):

- ``day`` — daily-frequency output (ua/zg for I.3b, pr for I.5d)
- ``historical`` — the coupled historical run for I.3c
- ``amip`` / ``amip4xco2`` — fixed-SST runs for I.4b
- ``amip`` / ``patch_ep`` / ``patch_wp`` — GFMIP patch runs for I.4a
- ``picontrol`` — coupled control for I.5c

I.3c and I.5c are the only Tier I gates that need **observations**; they take
them through ClimateEval DataSources (GPCP, HadISST, ERA5) over a
satellite-era window from ``thresholds.yml``, and degrade to reporting the
model's own statistic — no gate row, a logged reason — when those data cannot
be fetched and ``fail_on_missing_data`` is False.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import numpy as np
from esmvalcore.preprocessor import (
    anomalies,
    area_statistics,
    extract_levels,
    extract_region,
)
from loguru import logger

from climatebench2 import physics, windows
from climatebench2._thresholds import get_threshold
from climatebench2.diags.pass_fail import GateCheck, gate_requirement, gate_tier
from climatebench2.diags.tier1_physics import (
    CB2ComplexDiagnostic,
    _cube_years_months,
    _filled,
    _mon,
)
from climateeval import Variable
from climateeval._config import setup_esmvaltool_config_and_logging
from climateeval._utils import get_prepared_cube
from climateeval.data import GPCP, ERA5Monthly, HadISST

if TYPE_CHECKING:
    from collections.abc import Callable

    from iris.cube import Cube, CubeList
    from xarray import Dataset

    from climateeval.data import ComplexDataSource, DataSource


def _day(name: str) -> Variable:
    """Daily input variable."""
    return Variable(name, name, "day")


def _time_keys(cube: Cube) -> np.ndarray:
    """One hashable key per time step, comparable across calendars."""
    coord = cube.coord("time")
    dates = coord.units.num2date(coord.points)
    return np.array(
        [d.year * 10000 + d.month * 100 + d.day for d in dates],
        dtype=np.int64,
    )


def _month_keys(cube: Cube) -> np.ndarray:
    """One key per time step identifying its calendar month."""
    coord = cube.coord("time")
    dates = coord.units.num2date(coord.points)
    return np.array([d.year * 100 + d.month for d in dates], dtype=np.int64)


def _align_on_common_times(
    cubes: tuple[Cube, ...],
    key_of: Callable[[Cube], np.ndarray],
    what: str,
) -> list[Cube]:
    """Cut every cube to the time steps all of them have."""
    keys = [key_of(cube) for cube in cubes]
    common = keys[0]
    for other in keys[1:]:
        common = np.intersect1d(common, other)
    if common.size == 0:
        msg = f"The inputs share no {what}"
        raise ValueError(msg)
    aligned: list[Cube] = []
    for cube, key in zip(cubes, keys, strict=True):
        if key.size == common.size and np.array_equal(key, common):
            aligned.append(cube)
            continue
        index = np.flatnonzero(np.isin(key, common))
        time_axis = cube.coord_dims("time")[0]
        slicer: list[slice | np.ndarray] = [slice(None)] * cube.ndim
        slicer[time_axis] = index
        aligned.append(cube[tuple(slicer)])
    return aligned


def align_on_common_days(*cubes: Cube) -> list[Cube]:
    """Cut every cube to the calendar days all of them have.

    CMIP6 does not publish a model's daily variables over the same period:
    CNRM-CM6-1 has ``day ua`` for 1990-2014 but ``day zg`` only for 2000-2014,
    which made the geostrophic-balance gate try to correlate a 25-year field
    with a 15-year one. Aligning on the leading axis by length would silently
    pair 1990 winds with 2000 heights, so the alignment is by **date**.
    """
    return _align_on_common_times(cubes, _time_keys, "calendar day")


def align_on_common_months(*cubes: Cube) -> list[Cube]:
    """Cut every cube to the calendar months all of them have.

    The monthly counterpart of :func:`align_on_common_days`, and needed for
    the same reason. I.3c pairs a *precipitation* product with a *reanalysis*
    thermodynamic profile, and the two need not cover the same years: GPCP
    starts in 1983, ERA5 in 1979, so while the protocol window still began in
    1979 ``reference_windows.clip_to_source`` handed the gate a 1983-2014
    ``pr`` and a 1979-2014 ``ta``/``hus``. Truncating both to the shorter
    length regressed GPCP 1983-2014 on ERA5 1979-2010 — a four-year offset
    that turned the observed precipitation-buoyancy slope negative. I.3c also
    pairs the model's fields with the reference's this way, so both sides are
    regressed over one month set.
    """
    return _align_on_common_times(cubes, _month_keys, "calendar month")


def restrict_to_years(cube: Cube, first: int, last: int) -> Cube:
    """Cut a cube to the time steps in calendar years ``first``–``last``.

    I.3c's protocol window (``tier1.precip_buoyancy.obs_window``) applied to
    a model record the CLI hands over in full — 1850–2014, or 1850–2100 when
    the historical run is spliced onto a scenario. By calendar date, like
    :func:`align_on_common_months`, so any calendar works.
    """
    keys = _month_keys(cube)
    inside = (keys >= first * 100 + 1) & (keys <= last * 100 + 12)
    if not inside.any():
        msg = f"The record has no month in {first}-{last}"
        raise ValueError(msg)
    if inside.all():
        return cube
    time_axis = cube.coord_dims("time")[0]
    slicer: list[slice | np.ndarray] = [slice(None)] * cube.ndim
    slicer[time_axis] = np.flatnonzero(inside)
    return cube[tuple(slicer)]


# ---------------------------------------------------------------------------
# I.3b — Midlatitude geostrophic balance (daily 850 hPa)
# ---------------------------------------------------------------------------


class GeostrophicBalanceGate(CB2ComplexDiagnostic):
    """I.3b: spatial ρ(u, u_g) at 850 hPa over 30–60° (daily) > 0.9.

    Sub-orography 850 hPa points are masked where surface pressure is
    available (``ps`` in the daily data, < 870 hPa); otherwise the check
    proceeds unmasked with a logged caveat (paper implementation note keeps
    the level at 850 hPa and handles masking in code).
    """

    _required_data_keys = ("day",)
    _pressure_level_pa: ClassVar[float] = 85000.0
    _ps_mask_pa: ClassVar[float] = 87000.0

    _gate_checks = (
        GateCheck(
            check_id="geostrophic_balance",
            column="geostrophic_corr",
            lower=get_threshold("tier1.geostrophic_balance.spatial_corr_min"),
            requirement=gate_requirement("tier1.geostrophic_balance"),
            tier=gate_tier("tier1.geostrophic_balance"),
        ),
    )

    def _level_cube(self, data: CubeList | Dataset, name: str) -> Cube:
        cube = self._cube(data, _day(name))
        with setup_esmvaltool_config_and_logging():
            cube = extract_levels(cube, [self._pressure_level_pa], "linear")
        return cube

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        daily = complex_data_source.data["day"]
        ua = self._level_cube(daily, "ua")
        zg = self._level_cube(daily, "zg")
        # The two are rarely published over the same period, so pair them by
        # date rather than by position (see `align_on_common_days`).
        ua, zg = align_on_common_days(ua, zg)
        lats = ua.coord("latitude").points.astype(float)

        # `_filled`, never `np.asarray`: CMIP6 publishes `ua`/`zg` on a
        # pressure level that intersects orography as a **masked** array, and
        # `np.asarray` exposes the 1e20 fill value under the mask instead of
        # dropping the point. On CNRM-CM6-1 (6% of the 850 hPa field is
        # sub-surface) that alone drove ρ from 0.99 to 0.09.
        ua_data = np.squeeze(_filled(ua.data))
        zg_data = np.squeeze(_filled(zg.data))

        try:  # optional orography masking via daily surface pressure
            ps = self._cube(daily, _day("ps"))
            ua_ps, zg_ps, ps = align_on_common_days(ua, zg, ps)
            ps_data = np.squeeze(_filled(ps.data))
            ua_data = np.squeeze(_filled(ua_ps.data)).copy()
            zg_data = np.squeeze(_filled(zg_ps.data)).copy()
            mask = ps_data < self._ps_mask_pa
            ua_data[mask] = np.nan
            zg_data[mask] = np.nan
        except Exception as exc:  # noqa: BLE001 - ps genuinely optional
            logger.warning(
                f"Diagnostic '{self.name}': no daily 'ps' to mask sub-surface "
                f"850 hPa points with ({type(exc).__name__}: {exc}); the "
                f"correlation uses whatever the model published there",
            )

        ug = physics.geostrophic_wind_u(zg_data, lats)
        rho = physics.midlatitude_pattern_correlation(ua_data, ug, lats)
        return self._scalar_outputs({"geostrophic_corr": rho})


# ---------------------------------------------------------------------------
# I.4a — GFMIP SST patch experiments
# ---------------------------------------------------------------------------


class GFMIPPatchGate(CB2ComplexDiagnostic):
    """I.4a: Δλ = ΔR_EP/ΔTs_EP − ΔR_WP/ΔTs_WP > 0.5 W/m²/K.

    Requires submission-provided ``amip``, ``patch_ep`` and ``patch_wp``
    fixed-SST experiments (GFMIP protocol; not in the CMIP6 archive).
    """

    _required_data_keys = ("amip", "patch_ep", "patch_wp")
    _gate_checks = (
        GateCheck(
            check_id="gfmip_patch",
            column="delta_lambda",
            lower=get_threshold("tier1.gfmip_patch.delta_lambda_min"),
            requirement=gate_requirement("tier1.gfmip_patch"),
            tier=gate_tier("tier1.gfmip_patch"),
        ),
    )

    def _lambda_vs_amip(
        self,
        patch: CubeList | Dataset,
        amip: CubeList | Dataset,
    ) -> float:
        def gm(data: CubeList | Dataset, name: str) -> float:
            return float(self._annual_global_series(data, _mon(name)).mean())

        def toa_net(data: CubeList | Dataset) -> float:
            return gm(data, "rsdt") - gm(data, "rsut") - gm(data, "rlut")

        d_r = toa_net(patch) - toa_net(amip)
        d_ts = gm(patch, "ts") - gm(amip, "ts")
        return d_r / d_ts

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        data = complex_data_source.data
        lam_ep = self._lambda_vs_amip(data["patch_ep"], data["amip"])
        lam_wp = self._lambda_vs_amip(data["patch_wp"], data["amip"])
        return self._scalar_outputs(
            {
                "delta_lambda": lam_ep - lam_wp,
                "lambda_ep": lam_ep,
                "lambda_wp": lam_wp,
            },
        )


# ---------------------------------------------------------------------------
# I.4b — amip-4xCO2 effective radiative forcing
# ---------------------------------------------------------------------------


class Amip4xCO2ERFGate(CB2ComplexDiagnostic):
    """I.4b: fixed-SST ERF (amip-4xCO2 − amip TOA net) ∈ [6.5, 9.0] W/m²."""

    _required_data_keys = ("amip", "amip4xco2")
    _gate_checks = (
        GateCheck(
            check_id="amip_4xco2_erf",
            column="erf_wm2",
            lower=get_threshold("tier1.amip_4xco2_erf.range")[0],
            upper=get_threshold("tier1.amip_4xco2_erf.range")[1],
            requirement=gate_requirement("tier1.amip_4xco2_erf"),
            tier=gate_tier("tier1.amip_4xco2_erf"),
        ),
    )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        data = complex_data_source.data

        def toa_net_mean(exp: CubeList | Dataset) -> float:
            n = self._toa_net_annual_global(exp)
            return float(n.mean())

        erf = toa_net_mean(data["amip4xco2"]) - toa_net_mean(data["amip"])
        return self._scalar_outputs({"erf_wm2": erf})


# ---------------------------------------------------------------------------
# I.5c — ENSO teleconnections (piControl)
# ---------------------------------------------------------------------------


class ENSOTeleconnectionsGate(CB2ComplexDiagnostic):
    """I.5c: modelled vs observed ENSO teleconnection *patterns*, 30S–30N.

    The paper's 2026-09 criterion (which superseded the scalar ta500 /
    Maritime-Continent sign checks this class used to implement): regress
    monthly anomalies of surface temperature and of precipitation on the
    **standardized** Niño-3.4 index at every grid point over 30S–30N, do the
    same for the observations, and gate the **centred, cos-weighted spatial
    correlation** of the two regression patterns at
    ``tier1.enso.teleconnection_pattern_corr_min`` (0.7, i.e. spatial
    R² > 0.5) — one gate row per field.

    Model patterns come from the piControl run (the ≥ 100 yr control of I.5,
    with the same *unsmoothed* index definition as I.5a/b); the observed ones
    from ClimateEval DataSources over
    ``tier1.enso.teleconnection_obs_window`` (1979–2014: GPCP begins in 1979,
    and the protocol never touches the reserved post-2015 window).

    Reference products: **HadISST** ``tos`` for temperature and **GPCP**
    ``pr`` for precipitation, with the observed index taken from HadISST so
    both observed patterns are regressed on one index. HadISST being SST-only,
    its pattern is missing over land; the pattern correlation drops non-finite
    pairs, which masks the model's ``ts`` to the same ocean points. (ERA5 is
    the paper's other temperature option but ClimateEval's
    ``ERA5.VARIABLE_MAPPING`` has no ``ts``/skin temperature — an upstream
    gap, see docs/metrics_reference.md I.5c.)

    When the observations cannot be fetched (no network or CDS credentials)
    and ``fail_on_missing_data`` is False, the model's own pattern summary is
    emitted, the reason is logged and **no gate row is written** — the same
    "not run" state the scorecard already renders as a hole.
    """

    _required_data_keys = ("picontrol",)

    #: 30S–30N (paper: extratropical teleconnections are explicitly not gated).
    _band: ClassVar[tuple[float, float]] = (-30.0, 30.0)
    #: Niño-3.4 box (lon0, lon1, lat0, lat1), as in ClimateEval's ``Nino34``.
    _nino34_region: ClassVar[tuple[float, float, float, float]] = (
        190.0,
        240.0,
        -5.0,
        5.0,
    )
    #: Fewest common months a regression pattern is computed from.
    _min_months: ClassVar[int] = 24

    #: Observational references, as classes so a test can substitute them.
    _ts_reference: ClassVar[type[DataSource]] = HadISST
    _pr_reference: ClassVar[type[DataSource]] = GPCP

    _corr_min = get_threshold("tier1.enso.teleconnection_pattern_corr_min")
    _gate_checks = (
        GateCheck(
            check_id="enso_teleconnection_ts",
            column="teleconnection_corr_ts",
            lower=_corr_min,
            requirement=gate_requirement("tier1.enso"),
            tier=gate_tier("tier1.enso"),
        ),
        GateCheck(
            check_id="enso_teleconnection_pr",
            column="teleconnection_corr_pr",
            lower=_corr_min,
            requirement=gate_requirement("tier1.enso"),
            tier=gate_tier("tier1.enso"),
        ),
    )

    # -- the pieces of one regression pattern -------------------------------

    @staticmethod
    def _time_keys(cube: Cube) -> np.ndarray:
        """``year·12 + month`` of every step, for aligning two products."""
        years, months = _cube_years_months(cube)
        return years * 12 + months

    def _nino34_index(self, sst: Cube) -> tuple[np.ndarray, np.ndarray]:
        """(standardized unsmoothed Niño-3.4 index, its time keys).

        The I.5a definition without the operational 3-month running mean
        (``ENSOGate._rolling_window_length = 1``), then standardised so the
        regression slopes are "per σ of Niño-3.4" and comparable between a
        model and the observations.
        """
        lon0, lon1, lat0, lat1 = self._nino34_region
        with setup_esmvaltool_config_and_logging():
            cube = extract_region(
                sst,
                start_longitude=lon0,
                end_longitude=lon1,
                start_latitude=lat0,
                end_latitude=lat1,
            )
            cube = anomalies(cube, period="month")
            cube = area_statistics(cube, "mean")
        return physics.standardised(_filled(cube.data)), self._time_keys(cube)

    def _tropical_anomalies(
        self,
        cube: Cube,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(monthly anomalies over the band, latitudes, time keys)."""
        with setup_esmvaltool_config_and_logging():
            cube = extract_region(
                cube,
                start_longitude=0.0,
                end_longitude=360.0,
                start_latitude=self._band[0],
                end_latitude=self._band[1],
            )
            cube = anomalies(cube, period="month")
        return (
            _filled(cube.data),
            cube.coord("latitude").points.astype(float),
            self._time_keys(cube),
        )

    def _pattern(
        self,
        field: np.ndarray,
        field_keys: np.ndarray,
        index: np.ndarray,
        index_keys: np.ndarray,
    ) -> np.ndarray | None:
        """Per-gridpoint slope of ``field`` on the index, on common months."""
        common = np.intersect1d(field_keys, index_keys)
        if common.size < self._min_months:
            logger.warning(
                f"Diagnostic '{self.name}': only {common.size} months shared "
                f"between the field and the Niño-3.4 index (need "
                f"{self._min_months}); no regression pattern",
            )
            return None
        y = field[np.isin(field_keys, common)]
        x = index[np.isin(index_keys, common)]
        return physics.gridpoint_regression_slope(y, x[:, None, None])

    # -- model and observations --------------------------------------------

    def _model_patterns(
        self,
        data: CubeList | Dataset,
    ) -> tuple[dict[str, np.ndarray | None], np.ndarray, float]:
        """({field: pattern}, latitudes, σ(Niño-3.4)) from the piControl run."""
        sst = self._cube(data, _mon("tos"))
        index, index_keys = self._nino34_index(sst)
        patterns: dict[str, np.ndarray | None] = {}
        lats = np.array([])
        for name in ("ts", "pr"):
            field, lats, keys = self._tropical_anomalies(self._cube(data, _mon(name)))
            patterns[name] = self._pattern(field, keys, index, index_keys)
        # σ of the raw index: the standardisation above divides it out
        raw = self._nino34_raw_sigma(sst)
        return patterns, lats, raw

    def _nino34_raw_sigma(self, sst: Cube) -> float:
        """σ(Niño-3.4) in K — the I.5a statistic, reported for context."""
        lon0, lon1, lat0, lat1 = self._nino34_region
        with setup_esmvaltool_config_and_logging():
            cube = extract_region(
                sst,
                start_longitude=lon0,
                end_longitude=lon1,
                start_latitude=lat0,
                end_latitude=lat1,
            )
            cube = anomalies(cube, period="month")
            cube = area_statistics(cube, "mean")
        return float(np.nanstd(_filled(cube.data), ddof=1))

    def _observation_cube(self, source: type[DataSource], var_name: str) -> Cube:
        """One observational field over the protocol's satellite-era window.

        The window is clipped to what the staged product actually holds:
        the protocol asks for 1979-2014, but GPCP starts in 1983 and
        ClimateEval refuses a range its record does not cover
        (``MissingDataError``), which on the first real run silently removed
        both I.5c gate rows for every model. The two products need not share
        a span — :meth:`_pattern` aligns them on their common months.
        """
        from climatebench2 import reference_windows

        first, last = get_threshold("tier1.enso.teleconnection_obs_window")
        product = source()
        timerange = reference_windows.clip_to_source(
            windows.timerange(int(first), int(last)),
            self.data_root_dir,
            product.id,
            "mon",
            var_name,
        )
        variable = Variable(var_name, var_name, "mon", timerange=timerange)
        return product.get_cube(
            self.data_root_dir,
            variable,
            download_missing_data=self._download_missing_data,
        )

    def _observed_patterns(self) -> dict[str, np.ndarray | None]:
        """{field: observed regression pattern} on the common 2° grid."""
        sst = self._regridded(self._observation_cube(self._ts_reference, "tos"))
        index, index_keys = self._nino34_index(sst)
        ts_field, _lats, ts_keys = self._tropical_anomalies(sst)
        precip = self._regridded(self._observation_cube(self._pr_reference, "pr"))
        pr_field, _pr_lats, pr_keys = self._tropical_anomalies(precip)
        return {
            "ts": self._pattern(ts_field, ts_keys, index, index_keys),
            "pr": self._pattern(pr_field, pr_keys, index, index_keys),
        }

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        picontrol = complex_data_source.data["picontrol"]
        model, lats, nino34_sigma = self._model_patterns(picontrol)

        values: dict[str, float] = {"nino34_std": nino34_sigma}
        for name, pattern in model.items():
            if pattern is not None:
                values[f"{name}_pattern_rms"] = float(
                    np.sqrt(physics.area_weighted_mean(pattern**2, lats)),
                )

        observed = self._observed_patterns_or_none()
        if observed is None:
            return self._scalar_outputs(values)

        first, last = get_threshold("tier1.enso.teleconnection_obs_window")
        values["teleconnection_obs_first_year"] = float(first)
        values["teleconnection_obs_last_year"] = float(last)
        for name in ("ts", "pr"):
            if model[name] is None or observed[name] is None:
                continue
            values[f"teleconnection_corr_{name}"] = physics.banded_pattern_correlation(
                model[name],
                observed[name],
                lats,
                band=self._band,
            )
        return self._scalar_outputs(values)

    def _observed_patterns_or_none(self) -> dict[str, np.ndarray | None] | None:
        """Return the observed patterns, or ``None``, logging the reason."""
        try:
            return self._observed_patterns()
        except Exception as exc:
            if self._fail_on_missing_data:
                raise
            logger.warning(
                f"Diagnostic '{self.name}': no observed ENSO teleconnection "
                f"patterns ({type(exc).__name__}: {exc}); emitting the model's "
                f"own regression-pattern summary without the gate rows",
            )
            return None


# ---------------------------------------------------------------------------
# I.3c — Tropical precipitation–buoyancy relationship (historical)
# ---------------------------------------------------------------------------


class PrecipBuoyancyGate(CB2ComplexDiagnostic):
    """I.3c: tropical P′-on-column-MSE′ slope within ±30% of GPCP/ERA5.

    Column moist static energy ``h = c_p·ta + g·z + L_v·hus`` is integrated
    over the pressure levels the monthly Amon data actually carry
    (``∫ h dp/g``, :func:`physics.mass_weighted_column_integral`); the
    monthly anomalies of precipitation and of ⟨h⟩ over 20S–20N are then
    regressed against each other with time and space **pooled**
    (:func:`physics.pooled_regression_slope`). Slope units are
    mm day⁻¹ per MJ m⁻², so the number is O(0.1–1) rather than O(1e-7).

    **One window, one month set** (paper App. B.3: "over 1983–2014, the
    period of the GPCP/ERA5 reference"). The model is regressed over
    ``tier1.precip_buoyancy.obs_window`` and nothing else — never its whole
    historical record, which the CLI hands over in full and which a spliced
    historical+SSP run extends to 2100 — and, when the reference is computed,
    over exactly the calendar months the GPCP/ERA5 fields share inside that
    window (:func:`align_on_common_months` across all six fields). With the
    stored ``reference_slope`` fallback the model still uses the protocol
    window alone. ``precip_buoyancy_n_months`` and the first/last year are
    emitted so a row says which months it was regressed over.

    **The same column statistic on both sides.** ``g·z`` is the hydrostatic
    geopotential of each side's **own** ``ta``/``hus`` on the common levels
    (:func:`physics.hydrostatic_height`), for the model as for ERA5; the
    model's ``zg`` is not read. ClimateEval's ERA5 mapping has no ``zg``, so
    the reference had to be hydrostatic, and mixing a hydrostatic reference
    with the model's published ``zg`` is a difference of construction, not
    of physics (on CNRM-CM6-1, 1983–2014: 0.1164 with ``zg``, 0.1147
    hydrostatic; +27 % while the level-ordering bug below was present). The
    hydrostatic height is taken relative to the lowest common level (1000 hPa
    on ``plev19``), so it omits that level's own height ``z_b``. Because
    ``z_b`` is the same at every level of a column, it adds only
    ``z_b·(p_bottom − p_top)`` to ⟨h⟩: its climatology is removed exactly by
    the monthly anomalies, and what is left — the month-to-month variation of
    the 1000 hPa height, a few metres in the deep tropics, i.e. tens of J/kg
    against the ~10³ J/kg of ``c_p·T′ + L_v·q′`` — is now omitted identically
    from the model and the reference rather than present on one side only.

    The reference slope is the same statistic from GPCP ``pr`` and ERA5
    ``ta``/``hus``, computed at run time when those data are obtainable and
    otherwise read from ``tier1.precip_buoyancy.reference_slope`` (null by
    design). With neither available the model slope is emitted alone and no
    gate row is written. The reference column is integrated over the
    **submission's own** pressure levels, not ERA5's (paper App. B.3), and
    ``precip_buoyancy_n_levels`` is reported alongside the slope.

    **Each level is weighted by its own pressure** (fixed 2026-10-27).
    ``extract_levels`` keeps a file's level order when the requested levels
    are a subset of its own, and CMIP6 and ERA5 files run top-down from
    1000 hPa, but the column integral used to be handed the *ascending*
    level list — so the 1000 hPa field was integrated as if it sat at the
    top of the column, on both sides. That bug, not the vertical
    discretisation, is what made the slope look level-set dependent: on the
    staged ERA5 (20S–20N, 1983–2014) it gave 0.034 on ``plev19`` and 0.146 on
    ERA5's 37 levels, where the correct column gives 0.1166 and 0.1162
    mm/day per MJ/m² (r ≈ 0.48).
    """

    _required_data_keys = ("historical",)

    #: 20S–20N (paper I.3c).
    _band: ClassVar[tuple[float, float]] = (-20.0, 20.0)
    #: J/m² → MJ/m², so the reported slope is O(0.1–1).
    _mse_scale: ClassVar[float] = 1.0e6
    #: Relative tolerance for "the same pressure level" across products: a
    #: numerical-representation tolerance, not a protocol bound (ACCESS-CM2's
    #: plev19 is 100000.00000001 Pa where ERA5 has 100000).
    _level_rtol: ClassVar[float] = 1.0e-6

    _gate_checks = (
        GateCheck(
            check_id="precip_buoyancy",
            column="precip_buoyancy_slope_rel_error",
            upper=get_threshold("tier1.precip_buoyancy.rel_tolerance_vs_obs"),
            requirement=gate_requirement("tier1.precip_buoyancy"),
            tier=gate_tier("tier1.precip_buoyancy"),
        ),
    )

    # -- the window and the column integral ---------------------------------

    @staticmethod
    def _protocol_years() -> tuple[int, int]:
        """``tier1.precip_buoyancy.obs_window`` as (first, last) year."""
        first, last = get_threshold("tier1.precip_buoyancy.obs_window")
        return int(first), int(last)

    def _model_cube(self, data: CubeList | Dataset, name: str) -> Cube:
        """One model field cut to the protocol window, then regridded.

        Cut first: a 1850–2100 ``ta`` on ``plev19`` is ten times the data the
        regression uses.
        """
        cube = get_prepared_cube(data, _mon(name))
        return self._regridded(restrict_to_years(cube, *self._protocol_years()))

    @classmethod
    def _match_level(cls, level: float, candidates: np.ndarray) -> int | None:
        """Index of the candidate within ``_level_rtol`` of ``level``, if any."""
        close = np.flatnonzero(
            np.isclose(candidates, level, rtol=cls._level_rtol, atol=0.0),
        )
        return int(close[0]) if close.size else None

    @classmethod
    def _snap_levels(cls, cube: Cube, levels: list[float]) -> None:
        """Give ``cube``'s pressure points within tolerance of ``levels`` those values.

        In place, on a cube the gate created itself (a regrid output): the
        submission's level values become the common vocabulary, so the
        ``extract_levels`` that follows takes them by exact match instead of
        interpolating to a level 1e-8 Pa away — which ESMValCore treats as
        outside the source range and fills with NaN.
        """
        coord = cube.coord("air_pressure")
        points = coord.points.astype(float).copy()
        targets = np.asarray(levels, dtype=float)
        for index, point in enumerate(points):
            match = cls._match_level(point, targets)
            if match is not None:
                points[index] = targets[match]
        if not np.array_equal(points, coord.points):
            coord.points = points.astype(coord.dtype)

    @classmethod
    def _common_levels(cls, ta: Cube, hus: Cube) -> list[float]:
        """The pressure levels both profiles carry (Pa, ascending, ``ta``'s values).

        Matched within ``_level_rtol``, and ``hus`` is snapped onto ``ta``'s
        values, so a representation offset between the two files cannot
        empty the set.
        """
        ta_points = ta.coord("air_pressure").points.astype(float)
        hus_points = hus.coord("air_pressure").points.astype(float)
        levels = sorted(
            float(level)
            for level in ta_points
            if cls._match_level(level, hus_points) is not None
        )
        cls._snap_levels(hus, levels)
        if len(levels) < 2:  # noqa: PLR2004
            msg = (
                f"need at least two common pressure levels for the column "
                f"integral of I.3c, got {levels}"
            )
            raise ValueError(msg)
        return levels

    def _levels_with_data(
        self,
        ta: Cube,
        hus: Cube,
        levels: list[float],
        side: str,
    ) -> tuple[list[float], dict[float, str]]:
        """(the ``levels`` on which ``ta`` AND ``hus`` hold data, {dropped: why}).

        A level is dropped when either field is entirely missing on it —
        all-NaN or masked over 20S–20N and every month passed in — after
        being put on ``levels`` exactly as the column integral will put it.
        One such level makes the trapezoid NaN in every column (CAMS-CSM1-0,
        GFDL-CM4 and INM-CM4-8 publish nothing at 1 or 5 hPa), so it is left
        out rather than allowed to void the statistic. A level that is only
        partly missing (sub-surface 1000 hPa points) is kept: those columns
        drop out of the pooled regression as before.
        """
        present: dict[float, bool] = dict.fromkeys(levels, True)
        for cube in (ta, hus):
            with setup_esmvaltool_config_and_logging():
                cube = extract_levels(self._tropical_band(cube), levels, "linear")
            axis = cube.coord_dims("air_pressure")[0]
            finite = np.isfinite(_filled(cube.data))
            others = tuple(i for i in range(finite.ndim) if i != axis)
            has_data = finite.any(axis=others)
            for level, ok in zip(
                cube.coord("air_pressure").points.astype(float),
                has_data,
                strict=True,
            ):
                index = self._match_level(level, np.asarray(levels, dtype=float))
                if index is not None and not ok:
                    present[levels[index]] = False
        kept = [level for level in levels if present[level]]
        dropped = {
            level: f"no {side} ta/hus data over 20S-20N"
            for level in levels
            if not present[level]
        }
        return kept, dropped

    def _reference_levels(
        self,
        ta: Cube,
        hus: Cube,
        levels: list[float],
    ) -> tuple[list[float], dict[float, str]]:
        """The submission's ``levels`` the reference can be put on, and the rest.

        The reference's own points are snapped onto the submission's values
        (within ``_level_rtol``); a level outside the reference's pressure
        range is dropped (ICON-ESM-LR's 3, 5 and 7 Pa lie above ERA5's 1 hPa
        top, and ``extract_levels`` returns NaN there), and so is one the
        reference has no data on. Levels inside the range that ERA5 does not
        carry are interpolated, as before.
        """
        for cube in (ta, hus):
            self._snap_levels(cube, levels)
        points = np.concatenate(
            [c.coord("air_pressure").points.astype(float) for c in (ta, hus)],
        )
        low = max(c.coord("air_pressure").points.min() for c in (ta, hus))
        high = min(c.coord("air_pressure").points.max() for c in (ta, hus))
        inside = [
            level
            for level in levels
            if (low <= level <= high) or self._match_level(level, points) is not None
        ]
        dropped = {
            level: "outside the reference's pressure range"
            for level in levels
            if level not in inside
        }
        if len(inside) < 2:  # noqa: PLR2004
            return inside, dropped
        kept, missing = self._levels_with_data(ta, hus, inside, "reference")
        return kept, {**dropped, **missing}

    def _column_mse(
        self,
        ta: Cube,
        hus: Cube,
        levels: list[float] | None = None,
    ) -> tuple[np.ndarray, Cube]:
        """(⟨h⟩ in J/m² with shape (time, lat, lon), the cube it came from).

        ``g·z`` is the hydrostatic geopotential of these ``ta``/``hus`` on
        ``levels``, built **after** both are put on those levels, so the
        model and the reference construct it identically (see the class
        docstring for why its missing surface term does not matter).
        """
        if levels is None:
            levels = self._common_levels(ta, hus)
        with setup_esmvaltool_config_and_logging():
            ta, hus = (extract_levels(cube, levels, "linear") for cube in (ta, hus))
        axis = ta.coord_dims("air_pressure")[0]
        # The cube's OWN level order, never `levels`: `extract_levels` keeps a
        # file's order when the levels are a subset of its own, and CMIP6/ERA5
        # files run top-down from 1000 hPa, so pairing the data with the sorted
        # list integrated the 1000 hPa field as if it were the column top.
        plev = ta.coord("air_pressure").points.astype(float)
        t = _filled(ta.data)
        q = _filled(hus.data)
        z = physics.hydrostatic_height(t, q, plev, axis=axis)
        mse = physics.moist_static_energy(t, z, q)
        return physics.mass_weighted_column_integral(mse, plev, axis=axis), ta

    def _tropical_band(self, cube: Cube) -> Cube:
        with setup_esmvaltool_config_and_logging():
            return extract_region(
                cube,
                start_longitude=0.0,
                end_longitude=360.0,
                start_latitude=self._band[0],
                end_latitude=self._band[1],
            )

    def _slope(
        self,
        pr: Cube,
        ta: Cube,
        hus: Cube,
        levels: list[float] | None = None,
    ) -> float:
        """Pooled slope of P′ (mm/day) on ⟨h⟩′ (MJ/m²) over 20S–20N.

        The three inputs are paired **by calendar month** first. On the model
        side they come from one experiment and normally agree, but the
        observational reference pairs GPCP with ERA5 over records that can
        start years apart (see :func:`align_on_common_months`), and both the
        anomaly climatology and the pooled regression need the same months
        on all three.

        ``levels`` is the pressure-level set the column is integrated over.
        The caller passes the **model's** levels for the reference too
        (paper App. B.3; see ``_reference_slope``).
        """
        pr, ta, hus = align_on_common_months(pr, ta, hus)
        pr, ta, hus = (self._tropical_band(c) for c in (pr, ta, hus))
        column, level_cube = self._column_mse(ta, hus, levels)
        _years, months = _cube_years_months(level_cube)
        h_anom = physics.deseasonalised_anomalies(column, months) / self._mse_scale
        with setup_esmvaltool_config_and_logging():
            pr = anomalies(pr, period="month")
        p_anom = _filled(pr.data) * physics.SECONDS_PER_DAY  # mm/day
        return physics.pooled_regression_slope(p_anom, h_anom)

    # -- the observational reference ----------------------------------------

    def _observation_cube(self, source: type[DataSource], var_name: str) -> Cube:
        """One observational field over the protocol window, clipped to its record.

        ClimateEval refuses a range a staged record does not cover
        (``MissingDataError``), which would send the whole reference slope
        down the fallback path, so the window is clipped per product; the
        products are then paired on their common months with the model.
        """
        from climatebench2 import reference_windows

        first, last = self._protocol_years()
        product = source()
        timerange = reference_windows.clip_to_source(
            windows.timerange(first, last),
            self.data_root_dir,
            product.id,
            "mon",
            var_name,
        )
        variable = Variable(var_name, var_name, "mon", timerange=timerange)
        return self._regridded(
            product.get_cube(
                self.data_root_dir,
                variable,
                download_missing_data=self._download_missing_data,
            ),
        )

    def _observed_slope(
        self,
        model: tuple[Cube, Cube, Cube],
        levels: list[float],
    ) -> tuple[float, tuple[Cube, Cube, Cube], list[float], dict[float, str]]:
        """(GPCP/ERA5 slope, model fields on its months, levels used, dropped).

        ``model`` is the submission's ``(pr, ta, hus)`` already cut to the
        protocol window. All six fields are cut to the calendar months every
        one of them has, so the reference and the model are regressed over
        the identical month set — a reference product that ends early, or a
        model record with a gap, shortens both sides alike. ``levels`` (the
        submission's usable levels) are narrowed to those the reference can
        also be put on (:meth:`_reference_levels`); the caller integrates
        the model over the same narrowed set.
        """
        observed = (
            self._observation_cube(GPCP, "pr"),
            self._observation_cube(ERA5Monthly, "ta"),
            self._observation_cube(ERA5Monthly, "hus"),
        )
        aligned = align_on_common_months(*model, *observed)
        pr, ta, hus = aligned[3:]
        levels, dropped = self._reference_levels(ta, hus, levels)
        if len(levels) < 2:  # noqa: PLR2004
            msg = f"the reference shares fewer than two levels with the model: {levels}"
            raise ValueError(msg)
        return (
            self._slope(pr, ta, hus, levels),
            (aligned[0], aligned[1], aligned[2]),
            levels,
            dropped,
        )

    def _reference_slope(
        self,
        model: tuple[Cube, Cube, Cube],
        levels: list[float],
    ) -> tuple[
        float | None,
        bool,
        tuple[Cube, Cube, Cube],
        list[float],
        dict[float, str],
    ]:
        """(reference slope, from observations?, model fields, levels, dropped).

        The model fields come back cut to the months the reference used, and
        the levels narrowed to those the reference also carries; with the
        stored fallback both come back unchanged (the protocol window, the
        submission's usable levels).

        ``levels`` are the **model's** pressure levels: the column integral
        is a trapezoid over whatever levels the data carry, so the paper
        computes the reference on the submission's own level set and the
        two columns span the same partial column. (The strong level-set
        dependence once quoted here — 0.034 on ``plev19`` against 0.146 on
        ERA5's 37 levels — was the level-ordering bug of ``_column_mse``;
        with it fixed the two give 0.1166 and 0.1162.)
        """
        try:
            reference, model, used, dropped = self._observed_slope(model, levels)
        except Exception as exc:
            if self._fail_on_missing_data:
                raise
            logger.warning(
                f"Diagnostic '{self.name}': no GPCP/ERA5 reference slope "
                f"({type(exc).__name__}: {exc}); falling back to "
                f"tier1.precip_buoyancy.reference_slope",
            )
        else:
            return reference, True, model, used, dropped
        stored = get_threshold("tier1.precip_buoyancy.reference_slope")
        if stored is None:
            logger.warning(
                f"Diagnostic '{self.name}': tier1.precip_buoyancy."
                f"reference_slope is null, so I.3c reports the model slope "
                f"without a gate row",
            )
            return None, False, model, levels, {}
        return float(stored), False, model, levels, {}

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        historical = complex_data_source.data["historical"]
        pr, ta, hus = (
            self._model_cube(historical, name) for name in ("pr", "ta", "hus")
        )
        model = align_on_common_months(pr, ta, hus)
        # One level set for both sides: the levels the submission carries
        # (matched within tolerance), minus any it has no data on, minus any
        # the reference cannot be put on or has no data on.
        levels = self._common_levels(model[1], model[2])
        levels, dropped = self._levels_with_data(model[1], model[2], levels, "model")
        if len(levels) < 2:  # noqa: PLR2004
            msg = f"the model has data on fewer than two levels: {levels}"
            raise ValueError(msg)
        reference, from_observations, model, levels, more = self._reference_slope(
            (model[0], model[1], model[2]),
            levels,
        )
        dropped.update(more)
        if dropped:
            listed = ", ".join(
                f"{level:g} Pa ({why})" for level, why in sorted(dropped.items())
            )
            logger.warning(
                f"Diagnostic '{self.name}': I.3c integrates both columns over "
                f"{len(levels)} levels; dropped {listed}",
            )
        slope = self._slope(*model, levels)
        years, _months = _cube_years_months(model[0])
        values = {
            "precip_buoyancy_slope": slope,
            "precip_buoyancy_n_levels": float(len(levels)),
            "precip_buoyancy_levels_dropped": float(len(dropped)),
            "precip_buoyancy_top_level_pa": float(min(levels)),
            "precip_buoyancy_bottom_level_pa": float(max(levels)),
            "precip_buoyancy_n_months": float(years.size),
            "precip_buoyancy_first_year": float(years.min()),
            "precip_buoyancy_last_year": float(years.max()),
        }
        if reference is not None and np.isfinite(reference) and reference != 0.0:
            values["precip_buoyancy_slope_ref"] = reference
            values["precip_buoyancy_reference_from_obs"] = float(from_observations)
            values["precip_buoyancy_slope_rel_error"] = abs(slope / reference - 1.0)
        return self._scalar_outputs(values)


# ---------------------------------------------------------------------------
# I.5d — MJO Wheeler–Kiladis east/west power ratio (daily pr)
# ---------------------------------------------------------------------------


class MJOGate(CB2ComplexDiagnostic):
    """I.5d: eastward/westward power (k = 1–3, 30–90 d) of near-equatorial
    daily precipitation > 1.5.
    """

    _required_data_keys = ("day",)
    _gate_checks = (
        GateCheck(
            check_id="mjo_east_west",
            column="mjo_east_west_ratio",
            lower=get_threshold("tier1.mjo.east_west_power_ratio_min"),
            requirement=gate_requirement("tier1.mjo"),
            tier=gate_tier("tier1.mjo"),
        ),
    )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        daily = complex_data_source.data["day"]
        cube = self._cube(daily, _day("pr"))
        with setup_esmvaltool_config_and_logging():
            cube = extract_region(
                cube,
                start_longitude=0.0,
                end_longitude=360.0,
                start_latitude=-15.0,
                end_latitude=15.0,
            )
        pr = np.asarray(cube.data, dtype=float)
        lat_axis = cube.coord_dims("latitude")[0]
        pr_eq = np.nanmean(pr, axis=lat_axis)  # (time, lon)
        ratio = physics.mjo_east_west_ratio(pr_eq)
        return self._scalar_outputs({"mjo_east_west_ratio": ratio})
