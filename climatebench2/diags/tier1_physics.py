"""Tier I physical-consistency gates as ClimateEval complex diagnostics.

Each diagnostic loads its experiments from the data dict handed to the suite
(``{"picontrol": ..., "4xco2": ..., "histaer": ..., "historical": ...}``),
computes the paper-spec quantity (pure math in ``climatebench2.physics``),
emits it as scalar raw output, and gates it against ``thresholds.yml`` via
the ``pass_fail`` machinery (metrics rows with a ``passes`` column).

Where ClimateEval already owns the physics (I.6a/b/c, I.8a), the gate is a
thin ``_UpstreamGate`` wrapper that only feeds the protocol's constants into
the upstream diagnostic and gates its output columns — CB2 keeps no copy of
the computation.

Unlike ClimateEval's own complex diagnostics, CB2 gates accept a *superset*
of their required data keys (``SupersetExperimentMixin``), so one Tier I
suite can run every gate from a single experiment dict, and a gate whose
experiments are missing is skipped with a warning. Specs:
docs/metrics_reference.md (section numbers on each class).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
from cf_units import Unit
from esmvalcore.preprocessor import (
    annual_statistics,
    area_statistics,
    climate_statistics,
    regrid,
    zonal_statistics,
)
from iris.cube import Cube
from loguru import logger

from climateeval import Variable
from climateeval._config import setup_esmvaltool_config_and_logging
from climateeval._utils import get_prepared_cube
from climateeval.diags._utils import DEFAULT_GRID
from climateeval.diags.complex import (
    ECS,
    ArcticAmplification,
    LandOceanWarmingRatio,
    MeridionalHeatTransport,
)
from climateeval.diags.complex._base import ComplexDiagnostic

from climatebench2 import physics
from climatebench2._thresholds import get_threshold
from climatebench2.diags.pass_fail import (
    GateCheck,
    gate_requirement,
    gate_tier,
    GateMixin,
    SupersetExperimentMixin,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from iris.cube import CubeList
    from xarray import Dataset

    from climateeval.data import ComplexDataSource


@dataclass(frozen=True)
class ScalarVariable(Variable):
    """A scalar diagnostic-output variable outside ClimateEval's registry.

    ``Variable.__post_init__`` hard-fails for names missing from
    ``variables.yml`` (ClimateEval added ``ecs``/``lambda``/… there for its
    own ECS diagnostic). CB2 gates emit many protocol-specific scalars, so
    this subclass fills the minimal metadata directly instead of requiring a
    registry entry. (Upstream-PR candidate: ad-hoc scalar outputs.)
    """

    def __post_init__(self) -> None:
        object.__setattr__(self, "long_name", self.long_name or self.var_name)
        object.__setattr__(self, "ndim", len(self.coordinates))


def _fx(name: str) -> Variable:
    """Scalar output variable."""
    return ScalarVariable(name, name, "fx")


def _filled(data: Any) -> np.ndarray:
    """Return a float array with a masked array's mask turned into NaN.

    ``np.asarray`` on a masked array silently exposes the values *under* the
    mask, which for an SST product is land — so every array that may carry a
    mask (an observational reference, a sub-surface pressure level) goes
    through here.
    """
    if np.ma.isMaskedArray(data):
        return np.ma.filled(data.astype(float), np.nan)
    return np.asarray(data, dtype=float)


def _cube_years_months(cube: Cube) -> tuple[np.ndarray, np.ndarray]:
    """Calendar ``(years, months)`` of a cube's time coordinate."""
    coord = cube.coord("time")
    dates = coord.units.num2date(coord.points)
    return (
        np.array([d.year for d in dates], dtype=int),
        np.array([d.month for d in dates], dtype=int),
    )


def _mon(name: str) -> Variable:
    """Monthly input variable from ClimateEval's registry.

    ``rsds``/``rsus``/``rlds``/``rlus`` used to need a CB2-side definition;
    they landed in ClimateEval's ``variables.yml`` with the pinned `b0e941c`,
    so every Tier I input is now a plain registry lookup.
    """
    return Variable(name, name, "mon")


class CB2ComplexDiagnostic(SupersetExperimentMixin, GateMixin, ComplexDiagnostic):
    """Base for the Tier I gates: superset data keys + esmvalcore helpers."""

    # -- preprocessing helpers (all regrid to the common 2x2 grid first) ----

    def _regridded(self, cube: Cube) -> Cube:
        """Any cube on ClimateEval's common 2°×2° grid (linear)."""
        with setup_esmvaltool_config_and_logging():
            cube = regrid(cube, DEFAULT_GRID, "linear", cache_weights=True)
        return cube  # noqa: RET504

    def _cube(
        self,
        data: CubeList | Dataset,
        variable: Variable,
    ) -> Cube:
        return self._regridded(get_prepared_cube(data, variable))

    def _annual_global_series(
        self,
        data: CubeList | Dataset,
        variable: Variable,
    ) -> np.ndarray:
        return self._annual_global_series_years(data, variable)[0]

    def _annual_global_series_years(
        self,
        data: CubeList | Dataset,
        variable: Variable,
    ) -> tuple[np.ndarray, np.ndarray]:
        """(global annual-mean series, its calendar years).

        The years matter wherever the protocol names a *calendar* window
        rather than a position in the record (I.7's decadal mean centred on
        2015, and the parallel piControl segment it is drift-corrected with).
        """
        cube = self._cube(data, variable)
        with setup_esmvaltool_config_and_logging():
            cube = area_statistics(cube, "mean")
            cube = annual_statistics(cube, "mean")
        years, _months = _cube_years_months(cube)
        return _filled(cube.data), years

    def _monthly_global_series(
        self,
        data: CubeList | Dataset,
        variable: Variable,
    ) -> np.ndarray:
        cube = self._cube(data, variable)
        with setup_esmvaltool_config_and_logging():
            cube = area_statistics(cube, "mean")
        return np.asarray(cube.data, dtype=float)

    def _toa_net_annual_global(self, data: CubeList | Dataset) -> np.ndarray:
        return self._toa_net_annual_global_years(data)[0]

    def _toa_net_annual_global_years(
        self,
        data: CubeList | Dataset,
    ) -> tuple[np.ndarray, np.ndarray]:
        """(annual global-mean ``N = rsdt − rsut − rlut``, its years)."""
        series = {
            name: self._annual_global_series_years(data, _mon(name))
            for name in ("rsdt", "rsut", "rlut")
        }
        years = series["rsdt"][1]
        for _values, other in series.values():
            years = np.intersect1d(years, other)

        def aligned(name: str) -> np.ndarray:
            values, own_years = series[name]
            return values[np.isin(own_years, years)]

        return aligned("rsdt") - aligned("rsut") - aligned("rlut"), years

    def _timemean_zonal(
        self,
        data: CubeList | Dataset,
        variable: Variable,
    ) -> tuple[np.ndarray, np.ndarray]:
        """(zonal-mean time-mean profile, latitudes)."""
        cube = self._cube(data, variable)
        with setup_esmvaltool_config_and_logging():
            cube = climate_statistics(cube, "mean", "full")
            cube = zonal_statistics(cube, "mean")
        return (
            np.asarray(cube.data, dtype=float),
            cube.coord("latitude").points.astype(float),
        )

    def _scalar_outputs(self, values: dict[str, float]) -> dict[Variable, Cube]:
        return {
            _fx(name): Cube(np.float64(value), var_name=name)
            for name, value in values.items()
        }

    # -- flux profiles shared by Bjerknes / ITCZ-EFE ------------------------

    _SFC_FLUXES: ClassVar[tuple[tuple[str, float], ...]] = (
        # (variable, sign in F_sfc = net downward surface flux)
        ("rsds", +1.0),
        ("rsus", -1.0),
        ("rlds", +1.0),
        ("rlus", -1.0),
        ("hfss", -1.0),
        ("hfls", -1.0),
    )


# ---------------------------------------------------------------------------
# Gate wrappers over upstream ClimateEval complex diagnostics
#
# ClimateEval `main` owns the generic physics of I.6a/b/c and I.8a; CB2 keeps
# only the protocol layer — the thresholds and the pass/fail rows. Each
# wrapper feeds its protocol constants from thresholds.yml into the upstream
# kwargs (so the suite YAML keeps `additional_diagnostic_kwargs: {}`), gates
# the upstream output columns, and inherits the superset-key / graceful-skip
# behaviour of every CB2 complex diagnostic.
# ---------------------------------------------------------------------------


class _UpstreamGate(SupersetExperimentMixin, GateMixin):
    """Common base of the CB2 wrappers over ClimateEval complex diagnostics.

    ``_threshold_kwargs`` maps an upstream keyword argument to its
    ``thresholds.yml`` key; values are applied with ``setdefault`` semantics,
    so an explicit suite kwarg still wins.
    """

    _threshold_kwargs: ClassVar[dict[str, str]] = {}

    def __init__(self, name: str, **kwargs: Any) -> None:
        """Initialize class instance with the protocol's constants."""
        for kwarg, threshold_key in self._threshold_kwargs.items():
            kwargs.setdefault(kwarg, get_threshold(threshold_key))
        super().__init__(name, **kwargs)

    def _get_other_data(self) -> Generator[ComplexDataSource]:
        """Upstream's CMIP6 comparison ensemble, unless it is switched off.

        ``GateMixin.comparison_ensemble`` (``score --comparison frozen|none``)
        falls back to ``ComplexDiagnostic``'s empty generator, so upstream's
        pool glob never runs; the submission's gate rows are unchanged.
        """
        if not GateMixin.comparison_ensemble:
            return ComplexDiagnostic._get_other_data(self)  # noqa: SLF001
        return super()._get_other_data()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# I.6c — ECS gate (wrapper of ClimateEval's ECS)
# ---------------------------------------------------------------------------


class ECSGate(_UpstreamGate, ECS):
    """I.6c: ECS (Gregory, 150 yr) ∈ tier1.ecs.range = [1, 7] K."""

    _gate_checks = (
        GateCheck(
            check_id="ecs_gate",
            column="ecs",
            lower=get_threshold("tier1.ecs.range")[0],
            upper=get_threshold("tier1.ecs.range")[1],
            requirement=gate_requirement("tier1.ecs"),
            tier=gate_tier("tier1.ecs"),
        ),
    )


# ---------------------------------------------------------------------------
# I.1 — Energy balance closure (piControl)
# ---------------------------------------------------------------------------


class EnergyBalanceGate(CB2ComplexDiagnostic):
    """I.1: |μ(N)| < 0.1 W/m² and 10-yr-running-mean drift < 0.02 W/m²/dec.

    Both criteria are evaluated over the **last**
    ``tier1.energy_balance.evaluation_years`` (100) annual values of the
    supplied piControl — the segment contemporaneous with the historical
    branch point, so a model still equilibrating early in its control is not
    penalised. A shorter control is used whole (with a warning); the number
    of years actually used is emitted as ``n_years``.
    """

    _required_data_keys = ("picontrol",)
    _gate_checks = (
        GateCheck(
            check_id="energy_balance_mean",
            column="toa_net_mean_abs",
            upper=get_threshold("tier1.energy_balance.mean_toa_net_abs_max"),
            requirement=gate_requirement("tier1.energy_balance"),
            tier=gate_tier("tier1.energy_balance"),
        ),
        GateCheck(
            check_id="energy_balance_drift",
            column="toa_net_drift_abs",
            upper=get_threshold("tier1.energy_balance.drift_10yr_running_abs_max"),
            requirement=gate_requirement("tier1.energy_balance"),
            tier=gate_tier("tier1.energy_balance"),
        ),
    )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        picontrol = complex_data_source.data["picontrol"]
        n = self._toa_net_annual_global(picontrol)
        n_eval = int(get_threshold("tier1.energy_balance.evaluation_years"))
        if n.size < n_eval:
            logger.warning(
                f"Diagnostic '{self.name}': piControl has {n.size} yr, fewer than "
                f"the {n_eval} yr evaluation window of I.1; using the whole record "
                f"(reduced power to detect slow drifts)",
            )
        else:
            n = n[-n_eval:]
        return self._scalar_outputs(
            {
                "toa_net_mean_abs": abs(float(n.mean())),
                "toa_net_drift_abs": abs(physics.running_mean_drift(n)),
                "n_years": float(n.size),
            },
        )


# ---------------------------------------------------------------------------
# I.2 — Closure constraints (piControl)
# ---------------------------------------------------------------------------


class ClosureGate(CB2ComplexDiagnostic):
    """I.2a/b: water budget < 0.05 mm/day; atm energy budget < 2 W/m²."""

    _required_data_keys = ("picontrol",)
    _gate_checks = (
        GateCheck(
            check_id="water_budget",
            column="water_budget_residual_mmday",
            upper=get_threshold("tier1.water_budget.p_minus_e_abs_max"),
            requirement=gate_requirement("tier1.water_budget"),
            tier=gate_tier("tier1.water_budget"),
        ),
        GateCheck(
            check_id="atm_energy_budget",
            column="atm_energy_residual_wm2",
            upper=get_threshold("tier1.atm_energy_budget.residual_abs_max"),
            requirement=gate_requirement("tier1.atm_energy_budget"),
            tier=gate_tier("tier1.atm_energy_budget"),
        ),
    )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        picontrol = complex_data_source.data["picontrol"]
        means = {
            name: float(self._annual_global_series(picontrol, _mon(name)).mean())
            for name in (
                "pr",
                "hfls",
                "hfss",
                "rsdt",
                "rsut",
                "rlut",
                "rsds",
                "rsus",
                "rlds",
                "rlus",
            )
        }
        toa_net = means["rsdt"] - means["rsut"] - means["rlut"]
        sfc_net_rad = (
            means["rsds"] - means["rsus"] + means["rlds"] - means["rlus"]
        )
        return self._scalar_outputs(
            {
                "water_budget_residual_mmday": physics.water_budget_residual(
                    means["pr"],
                    means["hfls"],
                ),
                "atm_energy_residual_wm2": physics.atmospheric_energy_residual(
                    means["pr"],
                    toa_net,
                    sfc_net_rad,
                    means["hfss"],
                ),
            },
        )


# ---------------------------------------------------------------------------
# I.3a — Clear-sky longwave feedback (forced response, abrupt-4xCO2)
# ---------------------------------------------------------------------------


class ClearSkyFeedbackGate(CB2ComplexDiagnostic):
    """I.3a: forced clear-sky LW feedback within ±25% of 1.9 W/m²/K.

    Redefined 2026-09-21 (Duncan, ratified): the paper's own gridpoint
    regression of monthly `historical` anomalies (β ≈ 0.71 on CNRM-CM6-1)
    probes interannual variability, a different physical regime from
    Koll & Cronin's (2018) 2.2 W/m²/K fixed-RH column value that the old
    reference quoted. Zhang, Jeevanjee & Fueglistaler (2020, GRL,
    doi:10.1029/2020GL089235) show GCMs instead give a robust *global-mean*
    clear-sky LW feedback of ≈1.9 W/m²/K in the forced abrupt-4xCO2 response
    (locally negative over tropical oceans, compensated globally) — smaller
    than the fixed-RH column value, which GCMs undershoot.

    The statistic is now the OLS slope of the annual global-mean anomaly of
    `rlutcs` on the annual global-mean anomaly of `ts`, over the same
    ``picontrol``/``4xco2`` inputs as the I.6c ECS gate, both anomalies
    relative to the piControl long-term mean (the same baseline convention
    as ``climateeval.diags.complex.ECS``). Evaluated over the first
    ``tier1.clear_sky_lw_feedback.n_years`` (150) of the abrupt-4xCO2 record,
    or the whole record if shorter (with a warning); the number of years
    actually used is emitted as ``clear_sky_lw_feedback_n_years``.
    """

    _required_data_keys = ("picontrol", "4xco2")

    _reference = float(get_threshold("tier1.clear_sky_lw_feedback.reference"))
    _tolerance = float(get_threshold("tier1.clear_sky_lw_feedback.rel_tolerance"))
    _gate_checks = (
        GateCheck(
            check_id="clear_sky_lw_feedback",
            column="clear_sky_lw_feedback",
            lower=_reference * (1.0 - _tolerance),
            upper=_reference * (1.0 + _tolerance),
            requirement=gate_requirement("tier1.clear_sky_lw_feedback"),
            tier=gate_tier("tier1.clear_sky_lw_feedback"),
        ),
    )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        data = complex_data_source.data
        n_years = int(get_threshold("tier1.clear_sky_lw_feedback.n_years"))

        pi_rlutcs, _pi_rlutcs_years = self._annual_global_series_years(
            data["picontrol"],
            _mon("rlutcs"),
        )
        pi_ts, _pi_ts_years = self._annual_global_series_years(
            data["picontrol"],
            _mon("ts"),
        )

        a4x_rlutcs = self._annual_global_series(data["4xco2"], _mon("rlutcs"))
        a4x_ts = self._annual_global_series(data["4xco2"], _mon("ts"))
        n_available = min(a4x_rlutcs.size, a4x_ts.size)
        n = min(n_available, n_years)
        if n_available < n_years:
            logger.warning(
                f"Diagnostic '{self.name}': abrupt-4xCO2 has {n_available} yr, "
                f"fewer than the {n_years} yr window of I.3a; using the whole "
                f"record",
            )

        delta_ts = a4x_ts[:n] - pi_ts.mean()
        delta_rlutcs = a4x_rlutcs[:n] - pi_rlutcs.mean()
        slope, intercept, r2 = physics.gregory_regression(delta_ts, delta_rlutcs)

        return self._scalar_outputs(
            {
                "clear_sky_lw_feedback": slope,
                "clear_sky_lw_feedback_intercept": intercept,
                "clear_sky_lw_feedback_r2": r2,
                "clear_sky_lw_feedback_n_years": float(n),
            },
        )


# ---------------------------------------------------------------------------
# I.6a/b — Land–ocean warming ratio & Arctic amplification (a4x vs piControl)
# ---------------------------------------------------------------------------


class LandOceanWarmingGate(_UpstreamGate, LandOceanWarmingRatio):
    """I.6a: ΔT_land/ΔT_ocean ∈ tier1.land_ocean_warming.range = [1.2, 1.6].

    Thin gate over ``climateeval.diags.complex.LandOceanWarmingRatio`` (same
    land/sea masks, same last-``equilibrium_years`` abrupt-4xCO2 window, plus
    the CMIP6 r1i1p1f1 comparison ensemble). One strict-range check — the
    earlier "> 1 required / [1.2, 1.6] expected" pair was superseded by the
    2026-09 paper draft.
    """

    _threshold_kwargs: ClassVar[dict[str, str]] = {
        "equilibrium_years": "tier1.land_ocean_warming.equilibrium_years",
    }
    _gate_checks = (
        GateCheck(
            check_id="land_ocean_warming",
            column="land_ocean_warming_ratio",
            lower=get_threshold("tier1.land_ocean_warming.range")[0],
            upper=get_threshold("tier1.land_ocean_warming.range")[1],
            requirement=gate_requirement("tier1.land_ocean_warming"),
            tier=gate_tier("tier1.land_ocean_warming"),
        ),
    )


class ArcticAmplificationGate(_UpstreamGate, ArcticAmplification):
    """I.6b: ΔT(>66.5N)/ΔT(global) ≥ 1.5.

    Thin gate over ``climateeval.diags.complex.ArcticAmplification``.
    """

    _threshold_kwargs: ClassVar[dict[str, str]] = {
        "equilibrium_years": "tier1.arctic_amplification.equilibrium_years",
        "arctic_latitude": "tier1.arctic_amplification.lat_min",
    }
    _gate_checks = (
        GateCheck(
            check_id="arctic_amplification",
            column="arctic_amplification",
            lower=get_threshold("tier1.arctic_amplification.ratio_min"),
            requirement=gate_requirement("tier1.arctic_amplification"),
            tier=gate_tier("tier1.arctic_amplification"),
        ),
    )


# ---------------------------------------------------------------------------
# I.7 — Aerosol forcing (DAMIP hist-aer)
# ---------------------------------------------------------------------------


class AerosolForcingGate(CB2ComplexDiagnostic):
    """I.7: aerosol ERF ∈ [−2.0, −0.5] W/m² and cooling in 2015.

    Two details of paper App. B.8 that the first implementation approximated
    (metrics_reference.md discrepancy #8):

    - "2015" is the **decadal mean centred on 2015**
      (``tier1.aerosol_forcing.window`` = 2010–2019), not the last 30 yr of
      whatever was supplied. DAMIP ``hist-aer`` runs that stop in 2014 get
      the window slid back to the last decade they have
      (:func:`physics.clip_window_to_record`), and the window actually used
      is emitted with the gate.
    - control drift is removed with the **parallel piControl segment** — the
      control years concurrent with that window, located through the CMIP6
      ``branch_time_in_parent`` / ``parent_time_units`` attributes — rather
      than with the control's long-term mean, which is only used as a
      fallback (with a warning) when the branch metadata is absent.

    ERF is still ``ΔN − λ·ΔT`` with λ from the model's own 150-yr Gregory
    regression (negative, hence the minus sign; see the doc's sign nit).
    """

    _required_data_keys = ("picontrol", "4xco2", "histaer")

    _gate_checks = (
        GateCheck(
            check_id="aerosol_erf",
            column="aerosol_erf_wm2",
            lower=get_threshold("tier1.aerosol_forcing.erf_range")[0],
            upper=get_threshold("tier1.aerosol_forcing.erf_range")[1],
            requirement=gate_requirement("tier1.aerosol_forcing"),
            tier=gate_tier("tier1.aerosol_forcing"),
        ),
        GateCheck(
            check_id="aerosol_cooling",
            column="delta_t_end",
            upper=0.0,
            requirement=gate_requirement("tier1.aerosol_forcing"),
            tier=gate_tier("tier1.aerosol_forcing"),
        ),
    )

    # -- control drift: the piControl segment parallel to the window --------

    @staticmethod
    def _global_attribute(data: CubeList | Dataset, key: str) -> Any:
        """Return a CMIP6 global attribute of the cubes, if it survived.

        ``load_cmor_dir`` runs ``equalise_attributes`` over the time-split
        files of one variable, which drops attributes that *differ* between
        them (``creation_date``, ``tracking_id``) but keeps the run-level
        ones — ``branch_time_in_parent`` and ``parent_time_units`` are
        identical across a run's files, and ``get_prepared_cube`` copies the
        cube rather than rebuilding it, so they reach here.
        """
        attrs = getattr(data, "attrs", None)  # an xarray Dataset
        if attrs is not None and key in attrs:
            return attrs[key]
        try:
            cubes = list(data)
        except TypeError:
            return None
        for cube in cubes:
            value = getattr(cube, "attributes", {}).get(key)
            if value is not None:
                return value
        return None

    @staticmethod
    def _calendar(data: CubeList | Dataset) -> str | None:
        """Calendar of the experiment's time axis (the parent shares it)."""
        try:
            cubes = list(data)
        except TypeError:
            return None
        for cube in cubes:
            try:
                return str(cube.coord("time").units.calendar)
            except Exception:  # noqa: BLE001, S112 - not every cube has time
                continue
        return None

    def _branch_year_in_parent(self, data: CubeList | Dataset) -> int | None:
        """Return the piControl calendar year the child run branched from."""
        branch = self._global_attribute(data, "branch_time_in_parent")
        units = self._global_attribute(data, "parent_time_units")
        if branch is None or units is None:
            logger.warning(
                f"Diagnostic '{self.name}': hist-aer carries no "
                f"branch_time_in_parent/parent_time_units",
            )
            return None
        calendar = self._calendar(data)
        try:
            unit = (
                Unit(str(units), calendar=calendar)
                if calendar
                else Unit(str(units))
            )
            return int(unit.num2date(float(branch)).year)
        except Exception as exc:  # noqa: BLE001 - malformed metadata is common
            logger.warning(
                f"Diagnostic '{self.name}': cannot decode "
                f"branch_time_in_parent={branch!r} in '{units}': {exc}",
            )
            return None

    def _control_baseline(
        self,
        histaer: CubeList | Dataset,
        window: tuple[int, int],
        child_first_year: int,
        pi_tas: tuple[np.ndarray, np.ndarray],
        pi_toa_net: tuple[np.ndarray, np.ndarray],
    ) -> tuple[float, float, bool]:
        """(tas, N) control means the hist-aer anomalies are taken against.

        The parallel segment when the branch metadata allows it (App. B.8),
        the long-term control mean with a warning when it does not; the third
        element records which, and is emitted as ``parallel_segment``.
        """
        tas_values, tas_years = pi_tas
        n_values, n_years = pi_toa_net
        branch = self._branch_year_in_parent(histaer)
        if branch is not None:
            first, last = physics.parallel_control_window(
                window,
                branch_year_in_parent=branch,
                child_first_year=child_first_year,
            )
            tas_mask = (tas_years >= first) & (tas_years <= last)
            n_mask = (n_years >= first) & (n_years <= last)
            if tas_mask.any() and n_mask.any():
                logger.info(
                    f"Diagnostic '{self.name}': removing control drift with the "
                    f"parallel piControl segment {first}-{last} "
                    f"({int(tas_mask.sum())} yr; branch year {branch})",
                )
                return (
                    float(tas_values[tas_mask].mean()),
                    float(n_values[n_mask].mean()),
                    True,
                )
            logger.warning(
                f"Diagnostic '{self.name}': the parallel piControl segment "
                f"{first}-{last} lies outside the control record "
                f"{tas_years[0]}-{tas_years[-1]}",
            )
        logger.warning(
            f"Diagnostic '{self.name}': falling back to the piControl long-term "
            f"mean for the hist-aer anomalies (no parallel-segment drift removal)",
        )
        return float(tas_values.mean()), float(n_values.mean()), False

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        data = complex_data_source.data
        n_years_ecs = int(get_threshold("tier1.ecs.n_years"))

        pi_tas, pi_tas_years = self._annual_global_series_years(
            data["picontrol"],
            _mon("tas"),
        )
        pi_n, pi_n_years = self._toa_net_annual_global_years(data["picontrol"])

        a4x_tas = self._annual_global_series(data["4xco2"], _mon("tas"))
        a4x_n = self._toa_net_annual_global(data["4xco2"])
        n = min(a4x_tas.size, a4x_n.size, n_years_ecs)
        lambda_4x, _f4x, _r2 = physics.gregory_regression(
            a4x_tas[:n] - pi_tas.mean(),
            a4x_n[:n] - pi_n.mean(),
        )

        aer_tas, aer_tas_years = self._annual_global_series_years(
            data["histaer"],
            _mon("tas"),
        )
        aer_n, aer_n_years = self._toa_net_annual_global_years(data["histaer"])
        years = np.intersect1d(aer_tas_years, aer_n_years)
        aer_tas = aer_tas[np.isin(aer_tas_years, years)]
        aer_n = aer_n[np.isin(aer_n_years, years)]

        window = tuple(get_threshold("tier1.aerosol_forcing.window"))
        first, last = physics.clip_window_to_record(
            window,  # type: ignore[arg-type]
            int(years[0]),
            int(years[-1]),
        )
        if (first, last) != window:
            logger.warning(
                f"Diagnostic '{self.name}': hist-aer covers "
                f"{years[0]}-{years[-1]}, so the protocol's decadal window "
                f"{window[0]}-{window[1]} is evaluated over {first}-{last}",
            )
        in_window = (years >= first) & (years <= last)

        baseline_tas, baseline_n, parallel = self._control_baseline(
            data["histaer"],
            (first, last),
            int(years[0]),
            (pi_tas, pi_tas_years),
            (pi_n, pi_n_years),
        )

        dt_end = float(aer_tas[in_window].mean() - baseline_tas)
        dn_end = float(aer_n[in_window].mean() - baseline_n)

        return self._scalar_outputs(
            {
                "aerosol_erf_wm2": physics.aerosol_erf(dn_end, dt_end, lambda_4x),
                "delta_t_end": dt_end,
                "delta_n_end": dn_end,
                "lambda_4x": lambda_4x,
                "window_first_year": float(first),
                "window_last_year": float(last),
                "parallel_segment": float(parallel),
            },
        )


# ---------------------------------------------------------------------------
# I.8a — Meridional heat transport partitioning (piControl)
# ---------------------------------------------------------------------------


class MeridionalHeatTransportGate(_UpstreamGate, MeridionalHeatTransport):
    """I.8a: OMET peak 1.5–2.0 PW near 15–20N; AMET peak 4–5 PW near 45N.

    Thin gate over ``climateeval.diags.complex.MeridionalHeatTransport`` (the
    residual method on piControl monthly fluxes, plus the CMIP6 piControl
    comparison ensemble); the NH peak-search bands come from
    ``thresholds.yml`` rather than the upstream defaults.
    """

    _threshold_kwargs: ClassVar[dict[str, str]] = {
        "amet_search_band": "tier1.meridional_heat_transport.amet_search_band",
        "omet_search_band": "tier1.meridional_heat_transport.omet_search_band",
    }

    _omet_lat = get_threshold("tier1.meridional_heat_transport.omet_peak_lat_range")
    _amet_lat_ref = float(get_threshold("tier1.meridional_heat_transport.amet_peak_lat"))
    _amet_lat_tol = float(
        get_threshold("tier1.meridional_heat_transport.amet_peak_lat_tolerance"),
    )
    _gate_checks = (
        GateCheck(
            check_id="omet_peak",
            column="omet_peak",
            lower=get_threshold("tier1.meridional_heat_transport.omet_peak_range")[0],
            upper=get_threshold("tier1.meridional_heat_transport.omet_peak_range")[1],
            requirement=gate_requirement("tier1.meridional_heat_transport"),
            tier=gate_tier("tier1.meridional_heat_transport"),
        ),
        GateCheck(
            check_id="omet_peak_lat",
            column="omet_peak_lat",
            lower=float(_omet_lat[0]),
            upper=float(_omet_lat[1]),
            requirement=gate_requirement("tier1.meridional_heat_transport"),
            tier=gate_tier("tier1.meridional_heat_transport"),
        ),
        GateCheck(
            check_id="amet_peak",
            column="amet_peak",
            lower=get_threshold("tier1.meridional_heat_transport.amet_peak_range")[0],
            upper=get_threshold("tier1.meridional_heat_transport.amet_peak_range")[1],
            requirement=gate_requirement("tier1.meridional_heat_transport"),
            tier=gate_tier("tier1.meridional_heat_transport"),
        ),
        GateCheck(
            check_id="amet_peak_lat",
            column="amet_peak_lat",
            lower=_amet_lat_ref - _amet_lat_tol,
            upper=_amet_lat_ref + _amet_lat_tol,
            requirement=gate_requirement("tier1.meridional_heat_transport"),
            tier=gate_tier("tier1.meridional_heat_transport"),
        ),
    )


# ---------------------------------------------------------------------------
# I.8b — ITCZ–EFE relationship (historical, 12-month climatology)
# ---------------------------------------------------------------------------


class ITCZEFEGate(CB2ComplexDiagnostic):
    """I.8b: ITCZ-lat-vs-F_xeq slope within ±50% of 3°/PW and |r| > 0.9.

    Uses the 12-month climatology (paper spec; the retired script used every
    monthly timestep and carried a hard-coded array-length bug).
    """

    _required_data_keys = ("historical",)

    _slope_ref = float(get_threshold("tier1.itcz_efe.slope_reference"))
    _slope_tol = float(get_threshold("tier1.itcz_efe.slope_rel_tolerance"))
    _lag_months = int(get_threshold("tier1.itcz_efe.itcz_lag_months"))
    _gate_checks = (
        GateCheck(
            check_id="itcz_efe_slope",
            column="itcz_efe_slope_abs",
            lower=_slope_ref * (1.0 - _slope_tol),
            upper=_slope_ref * (1.0 + _slope_tol),
            requirement=gate_requirement("tier1.itcz_efe"),
            tier=gate_tier("tier1.itcz_efe"),
        ),
        GateCheck(
            check_id="itcz_efe_correlation",
            column="itcz_efe_r_abs",
            lower=get_threshold("tier1.itcz_efe.corr_min"),
            requirement=gate_requirement("tier1.itcz_efe"),
            tier=gate_tier("tier1.itcz_efe"),
        ),
    )

    def _monthly_clim_zonal(
        self,
        data: CubeList | Dataset,
        variable: Variable,
    ) -> tuple[np.ndarray, np.ndarray]:
        """(12, lat) climatological zonal means."""
        cube = self._cube(data, variable)
        with setup_esmvaltool_config_and_logging():
            cube = climate_statistics(cube, "mean", "month")
            cube = zonal_statistics(cube, "mean")
        return (
            np.asarray(cube.data, dtype=float),
            cube.coord("latitude").points.astype(float),
        )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        historical = complex_data_source.data["historical"]

        parts = {
            name: self._monthly_clim_zonal(historical, _mon(name))
            for name in ("rsdt", "rsut", "rlut")
        }
        lats = next(iter(parts.values()))[1]
        toa_net = parts["rsdt"][0] - parts["rsut"][0] - parts["rlut"][0]
        f_sfc = np.zeros_like(toa_net)
        for name, sign in self._SFC_FLUXES:
            f_sfc = f_sfc + sign * self._monthly_clim_zonal(historical, _mon(name))[0]
        div_a = toa_net - f_sfc  # (12, lat)

        pr_clim, pr_lats = self._monthly_clim_zonal(historical, _mon("pr"))
        tropics = (pr_lats >= -30.0) & (pr_lats <= 30.0)

        itcz_lats = np.empty(12)
        f_xeq_pw = np.empty(12)
        for month in range(12):
            amet = physics.meridional_transport(div_a[month], lats)
            f_xeq_pw[month] = np.interp(0.0, lats, amet) / 1e15
            pr_band = pr_clim[month][tropics]
            itcz_lats[month] = float(pr_lats[tropics][np.argmax(pr_band)])

        slope, r = physics.itcz_efe_regression(
            itcz_lats,
            f_xeq_pw,
            lag_months=self._lag_months,
        )
        return self._scalar_outputs(
            {
                "itcz_efe_slope_abs": abs(slope),
                "itcz_efe_r_abs": abs(r),
                "itcz_efe_slope": slope,
            },
        )


# ---------------------------------------------------------------------------
# Extras (code-only, pending paper reconciliation)
# ---------------------------------------------------------------------------


class BjerknesGate(CB2ComplexDiagnostic):
    """Extra: decadal AMET–OMET anti-correlation at 40–70N (r < −0.3)."""

    _required_data_keys = ("picontrol",)
    _band: ClassVar[tuple[float, float]] = (40.0, 70.0)

    _gate_checks = (
        GateCheck(
            check_id="bjerknes_compensation",
            column="bjerknes_r",
            upper=get_threshold("tier1.bjerknes.corr_max"),
            requirement=gate_requirement("tier1.bjerknes"),
            tier=gate_tier("tier1.bjerknes"),
        ),
    )

    def _band_transport_series(
        self,
        data: CubeList | Dataset,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Monthly 40–70N band-mean AMET and OMET series."""
        monthly_zonal: dict[str, np.ndarray] = {}
        lats: np.ndarray | None = None
        for name in ("rsdt", "rsut", "rlut", *(n for n, _ in self._SFC_FLUXES)):
            cube = self._cube(data, _mon(name))
            with setup_esmvaltool_config_and_logging():
                cube = zonal_statistics(cube, "mean")
            monthly_zonal[name] = np.asarray(cube.data, dtype=float)  # (t, lat)
            lats = cube.coord("latitude").points.astype(float)
        assert lats is not None
        toa_net = (
            monthly_zonal["rsdt"] - monthly_zonal["rsut"] - monthly_zonal["rlut"]
        )
        f_sfc = np.zeros_like(toa_net)
        for name, sign in self._SFC_FLUXES:
            f_sfc = f_sfc + sign * monthly_zonal[name]
        band = (lats >= self._band[0]) & (lats <= self._band[1])
        n_time = toa_net.shape[0]
        amet = np.empty(n_time)
        omet = np.empty(n_time)
        for t in range(n_time):
            amet[t] = physics.meridional_transport(toa_net[t] - f_sfc[t], lats)[
                band
            ].mean()
            omet[t] = physics.meridional_transport(f_sfc[t], lats)[band].mean()
        return amet, omet

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        amet, omet = self._band_transport_series(complex_data_source.data["picontrol"])
        r = physics.bjerknes_correlation(amet, omet)
        return self._scalar_outputs({"bjerknes_r": r})


class CCScalingGate(CB2ComplexDiagnostic):
    """Extra: Clausius–Clapeyron scaling Δprw vs Δtas slope in 7 ± 2 %/K."""

    _required_data_keys = ("picontrol",)
    _gate_checks = (
        GateCheck(
            check_id="cc_scaling",
            column="cc_scaling_slope",
            lower=get_threshold("tier1.cc_scaling.slope_range")[0],
            upper=get_threshold("tier1.cc_scaling.slope_range")[1],
            requirement=gate_requirement("tier1.cc_scaling"),
            tier=gate_tier("tier1.cc_scaling"),
        ),
    )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        picontrol = complex_data_source.data["picontrol"]
        prw = self._annual_global_series(picontrol, _mon("prw"))
        tas = self._annual_global_series(picontrol, _mon("tas"))
        n = min(prw.size, tas.size)
        return self._scalar_outputs(
            {"cc_scaling_slope": physics.cc_scaling_slope(prw[:n], tas[:n])},
        )
