"""Tier II aggregated scalar diagnostics (metrics_reference.md §II.1).

Three historical-experiment diagnostics that ClimateEval has no provider for:

- :class:`RealizedWarmingLevel` — the **primary** scalar of the test window
  (paper, revised 2026-09): the mean global-mean ``tas`` anomaly over the
  complete years from ``tier2.test_window_start`` to the end of the record,
  relative to the fixed 1985-2014 baseline; plus the test-window OLS trend
  (secondary) and the ``tier2.long_trend_start`` (1950-present) trend.
- :class:`PinatuboResponseGate` — the 1991-93 dimming and cooling.
- :class:`HemisphericAsymmetryGate` — the aerosol-era NH-SH trend difference
  and the associated southward ITCZ shift.

All three are complex diagnostics on the ``historical`` key, and the CLI runs
``ClimateBench2_TierII_events`` **once per ensemble member**
(``_cli.SuiteSpec.per_member``) with each member's own record, because these
scalars are scored **across the ensemble**: one number per member, fair CRPS
against the observed value and the regime-(c) consistency test, both computed
by :mod:`climatebench2.scoring_pass` once every member has run.

Observations
------------
Where a ClimateEval DataSource exists, each diagnostic emits the observed
value of the same scalar as a ``reference`` row
(:class:`ObservedScalarMixin`) so the pass can score against it. Today that
means **HadCRUT5** ``tas`` and nothing else:

- GISTEMP, Berkeley Earth and NOAAGlobalTemp — the paper's other three GMST
  products, and the source of its inter-product observational spread — have
  **no ClimateEval DataSource** (upstream gap). The pass's multi-product
  machinery picks them up the day they land: they only have to appear in the
  suite as observational ``other_data``.
- ``rsds`` has no BSRN (or CERES-SYN) DataSource, so the Pinatubo dimming
  stays a **model-only sign flag**.
- The ITCZ shift is defined over the aerosol era (1950-1985) and GPCP starts
  in 1979, so it stays a **model-only sign flag** too.

The sign flags survive as gate rows tagged ``requirement: diagnostic``, which
never reach the Tier I entry ticket; the scalars are what get *scored*.

GSAT blending correction
------------------------
HadCRUT5 blends land 2 m air temperature with sea surface temperature,
whereas a model's ``tas`` is surface air temperature everywhere. The protocol
corrects the **observations** to a SAT basis rather than building blended,
coverage-masked model fields (which would need ``tos``/``siconc`` that not
every architecture produces). :func:`blended_to_sat` multiplies the observed
*anomaly* by ``tier2.gsat_blending_factor`` and returns the correction
uncertainty — ``tier2.gsat_blending_relative_uncertainty`` of the corrected
value — which is emitted as the scalar's ``_sigma_obs`` companion and enters
the fair CRPS draws and the consistency test's σ_obs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import ibis
import numpy as np
import pandas as pd
from esmvalcore.preprocessor import (
    annual_statistics,
    area_statistics,
    climate_statistics,
    extract_region,
    extract_time,
    mask_landsea,
    zonal_statistics,
)
from loguru import logger

from climateeval import Variable
from climateeval._config import setup_esmvaltool_config_and_logging
from climateeval.data import CERESEBAF, ERA5Monthly, ESACCICloud, ESACCISST, HadCRUT5
from climateeval.diags._base import DiagnosticOutput
from climateeval.diags._utils import union

from climatebench2 import physics, windows
from climatebench2._thresholds import get_threshold
from climatebench2.diags.pass_fail import BlockTag, GateCheck, Threshold
from climatebench2.diags.tier1_physics import CB2ComplexDiagnostic, _filled, _mon
from climatebench2.scoring_pass import SCALAR_SIGMA_OBS_SUFFIX

if TYPE_CHECKING:
    from iris.cube import Cube, CubeList
    from xarray import Dataset

    from climateeval.data import ComplexDataSource, DataSource

logger = logger.opt(colors=True)

#: Pinatubo response window (paper §II.1: "1991-93 anomalies"). The eruption
#: was 1991-06-15, so the response window opens the following month.
PINATUBO_WINDOW = ((1991, 7), (1993, 12))


def blending_factor() -> float:
    """``tier2.gsat_blending_factor`` — blended obs → SAT basis."""
    return float(get_threshold("tier2.gsat_blending_factor"))


def blended_to_sat(anomaly: float) -> tuple[float, float]:
    """Correct a blended observed anomaly to a SAT basis.

    Returns ``(corrected anomaly, its 1-σ correction uncertainty)``. The
    paper assesses the uncertainty at "at most 10% of the long-term change,
    with low confidence in its sign"; CB2 reads "the long-term change" as the
    magnitude of the corrected statistic itself
    (``tier2.gsat_blending_relative_uncertainty × |value|``) — an
    interpretation recorded in docs/metrics_reference.md §II.1.
    """
    corrected = float(anomaly) * blending_factor()
    relative = float(get_threshold("tier2.gsat_blending_relative_uncertainty"))
    return corrected, abs(corrected) * relative


class ObservedScalarMixin:
    """Emit observation-derived ``reference`` scalars beside a diagnostic's own.

    ``ComplexDiagnostic.get_output`` only ever writes ``to_benchmark`` and
    ``other`` rows, because a complex diagnostic's input is an experiment
    dict rather than a suite ``Variable`` with a ``reference_data:`` entry.
    The Tier II scalars of §II.1 *do* have an observed counterpart, so this
    mixin appends it: subclasses return ``{scalar: value}`` (plus any
    ``<scalar>_sigma_obs`` companions) from :meth:`_observed_scalars` and the
    values are unioned into ``raw_output`` under ``data_type = "reference"``,
    with the product's own ``DataSourceInformation`` added to
    ``data_sources`` so the scoring pass can tell an observation from a model.

    Fetching is best-effort: with ``fail_on_missing_data`` False (the CLI
    default) an unreachable product is logged and the diagnostic degrades to
    its model-only output, exactly as the observation-fed Tier I gates do.
    """

    #: Observational product the reference scalars come from. It is also the
    #: ``data_id`` the ``reference`` rows are written under, so a diagnostic
    #: drawing on several products names its **primary** one here and lists
    #: the rest in :attr:`_observation_products` (for provenance).
    _observation_source: ClassVar[type[DataSource]] = HadCRUT5

    #: Every product this diagnostic reads, registered in ``data_sources``.
    #: ``()`` means "just the primary one".
    _observation_products: ClassVar[tuple[type[DataSource], ...]] = ()

    def _observed_scalars(self) -> dict[str, float]:
        """``{scalar: observed value}``; empty when there is nothing to add."""
        return {}

    def _observation_cube(
        self,
        var_name: str,
        timerange: str,
        source: type[DataSource] | None = None,
    ) -> Cube:
        """One observational field over ``timerange`` (an ISO window).

        ``source`` defaults to :attr:`_observation_source`; the seasonal-cycle
        diagnostics pass a second product (an SST or a cloud product beside
        the primary) because their statistic is a regression *between* two
        observed fields.

        HadCRUT5 CMORizes both ``tas`` and the anomaly field ``tasa``, but
        only ``tas`` is in ClimateEval's variable registry (``tasa`` would
        fail ``Variable.__post_init__``) — which costs nothing here, since
        every statistic below is an anomaly against the protocol's own
        baseline window and the two differ by a constant.
        """
        from climatebench2 import reference_windows

        data_root = self.data_root_dir  # type: ignore[attr-defined]
        product = (source or self._observation_source)()
        # The observational products lag: HadCRUT5 stops in 2023-09, so a
        # window running to the last complete year raises MissingDataError and
        # the whole scalar falls back to "model only, unscorable". Ask for what
        # is actually staged instead (climatebench2.reference_windows).
        timerange = reference_windows.clip_to_source(
            timerange,
            data_root,
            product.id,
            "mon",
            var_name,
        )
        variable = Variable(var_name, var_name, "mon", timerange=timerange)
        return product.get_cube(
            data_root,
            variable,
            download_missing_data=self._download_missing_data,  # type: ignore[attr-defined]
        )

    def _observed_scalars_or_none(self) -> dict[str, float] | None:
        """The observed scalars, or ``None``, logging the reason."""
        try:
            return self._observed_scalars()
        except Exception as exc:
            if self._fail_on_missing_data:  # type: ignore[attr-defined]
                raise
            logger.warning(
                f"Diagnostic '{self.name}': no observational reference "  # type: ignore[attr-defined]
                f"({type(exc).__name__}: {exc}); emitting the model's own "
                f"scalars, which the scoring pass then cannot score",
            )
            return None

    def _postprocess_output(self, output: DiagnosticOutput) -> DiagnosticOutput:
        """Append the observed scalars as ``reference`` rows."""
        output = super()._postprocess_output(output)  # type: ignore[misc]
        if output.raw_output is None:
            return output
        values = self._observed_scalars_or_none()
        if not values:
            return output
        information = self._observation_source().information
        products = [
            source().information
            for source in (self._observation_products or (self._observation_source,))
        ]
        scalars = self._scalar_outputs(values)  # type: ignore[attr-defined]
        tables = list(
            self._get_raw_output_tables(  # type: ignore[attr-defined]
                scalars,
                data_id=information.id,
                data_type="reference",
            ),
        )
        sources = pd.concat(
            [
                output.data_sources.to_pandas(),
                self._get_data_sources_table(products).to_pandas(),  # type: ignore[attr-defined]
            ],
            ignore_index=True,
        ).drop_duplicates()
        variables = pd.concat(
            [
                output.variables.to_pandas(),
                self._get_variables_table(scalars).to_pandas(),  # type: ignore[attr-defined]
            ],
            ignore_index=True,
        ).drop_duplicates()
        return DiagnosticOutput(
            raw_output=union(output.raw_output, *tables),
            metrics=output.metrics,
            variables=ibis.memtable(variables),
            data_sources=ibis.memtable(sources),
        )


class _GlobalMeanTasMixin(ObservedScalarMixin):
    """Shared HadCRUT5 handling: one global/regional mean ``tas`` record."""

    def _observation_record(self) -> Cube:
        """HadCRUT5 ``tas`` from ``tier2.long_trend_start`` to last year.

        One fetch covers every window the Tier II scalars need — the
        1985-2014 baseline, the Pinatubo response, the aerosol era and the
        post-2015 test window all sit inside 1950-present.
        """
        first = int(get_threshold("tier2.long_trend_start"))
        return self._observation_cube(
            "tas",
            windows.timerange(first, windows.last_complete_year()),
        )

    def _regional_annual_series(
        self,
        cube: Cube,
        lat0: float = -90.0,
        lat1: float = 90.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """(annual area-mean series, its calendar years) over a latitude band."""
        with setup_esmvaltool_config_and_logging():
            if (lat0, lat1) != (-90.0, 90.0):
                cube = extract_region(
                    cube,
                    start_longitude=0.0,
                    end_longitude=360.0,
                    start_latitude=lat0,
                    end_latitude=lat1,
                )
            cube = area_statistics(cube, "mean")
            cube = annual_statistics(cube, "mean")
        coord = cube.coord("time")
        years = np.array(
            [coord.units.num2date(p).year for p in coord.points],
            dtype=int,
        )
        return _filled(cube.data), years


def _window_mean(values: np.ndarray, years: np.ndarray, first: int, last: int) -> float:
    """Mean of the complete years inside ``[first, last]`` (NaN if none)."""
    inside = (years >= int(first)) & (years <= int(last))
    if not inside.any():
        return float("nan")
    return float(np.nanmean(values[inside]))


def _window_trend(
    values: np.ndarray,
    years: np.ndarray,
    first: int,
    last: int,
    *,
    min_years: int = 5,
) -> float:
    """OLS trend per **decade** over ``[first, last]`` (NaN if too short)."""
    inside = (years >= int(first)) & (years <= int(last))
    selected = values[inside]
    if selected.size < min_years or not np.isfinite(selected).all():
        return float("nan")
    return physics.ols_trend(selected) * 10.0


class RealizedWarmingLevel(_GlobalMeanTasMixin, CB2ComplexDiagnostic):
    """§II.1: realized warming level (primary) and the two GMST trends.

    The paper's **primary scalar diagnostic for the test window is the
    realized warming level**, not the trend within it: over a single decade
    an OLS trend is dominated by internal variability, whereas the mean level
    is an integrated, variability-robust measure of the recent warming.

    Per ensemble member, on the ``historical`` key (the CLI hands each member
    its own record):

    - ``gmst_warming_level`` — mean global annual-mean ``tas`` over the
      complete years from ``tier2.test_window_start`` to the end of the
      record, minus the ``tier2.climatology_baseline_period`` (1985-2014)
      mean. **Primary**, labelled *held-out*;
    - ``gmst_trend_test_window`` — OLS trend (K/decade) over the same years.
      **Secondary**, labelled *held-out*;
    - ``gmst_trend_1950`` — OLS trend (K/decade) from
      ``tier2.long_trend_start`` to the end of the record, where the record
      is long enough for the trend uncertainty to be acceptable. Labelled
      *in-sample*.

    The observed counterparts come from HadCRUT5 through its ClimateEval
    DataSource, corrected to a surface-air-temperature basis
    (:func:`blended_to_sat`) with the correction uncertainty carried into
    each scalar's ``_sigma_obs``. The window actually used is emitted as
    ``warming_level_first_year`` / ``warming_level_last_year`` — model-only
    provenance columns, which the scoring pass never scores because the
    reference carries no value for them.

    A submission whose ``historical`` record stops before the test window (a
    plain CMIP6 *historical* run ends in 2014) gets the 1950 trend and a
    logged warning instead of the two test-window scalars.
    """

    _required_data_keys = ("historical",)
    #: Protocol standing (thresholds.yml): reported numbers, never a gate.
    requirement: ClassVar[str] = BlockTag("tier2.warming_level")  # type: ignore[assignment]

    def _series(self, data: CubeList | Dataset) -> tuple[np.ndarray, np.ndarray]:
        return self._annual_global_series_years(data, _mon("tas"))

    def _scalars(self, values: np.ndarray, years: np.ndarray) -> dict[str, float]:
        """The three statistics of §II.1 from one global annual-mean series."""
        base_first, base_last = windows.baseline_window_years()
        baseline = _window_mean(values, years, base_first, base_last)
        test_first = int(get_threshold("tier2.test_window_start"))
        long_first = int(get_threshold("tier2.long_trend_start"))
        last = int(years.max())

        out: dict[str, float] = {}
        if last < test_first:
            logger.warning(
                f"Diagnostic '{self.name}': the record ends in {last}, before "
                f"the reserved test window opens in {test_first}; no realized "
                f"warming level for this source",
            )
        elif not np.isfinite(baseline):
            logger.warning(
                f"Diagnostic '{self.name}': the record does not cover the "
                f"{base_first}-{base_last} baseline; no realized warming level",
            )
        else:
            level = _window_mean(values, years, test_first, last)
            out["gmst_warming_level"] = float(level - baseline)
            out["warming_level_first_year"] = float(test_first)
            out["warming_level_last_year"] = float(last)
            trend = _window_trend(values, years, test_first, last)
            if np.isfinite(trend):
                out["gmst_trend_test_window"] = trend
        long_trend = _window_trend(values, years, long_first, last)
        if np.isfinite(long_trend):
            out["gmst_trend_1950"] = long_trend
            # The window actually used: a record that starts after
            # `long_trend_start` gives a shorter trend, which the scorecard
            # must be able to see rather than take on the name's word.
            out["long_trend_first_year"] = float(max(long_first, int(years.min())))
            out["long_trend_last_year"] = float(last)
        return out

    def _observed_scalars(self) -> dict[str, float]:
        """The same three statistics from HadCRUT5, on a SAT basis."""
        values, years = self._regional_annual_series(
            self._regridded(self._observation_record()),
        )
        base_first, base_last = windows.baseline_window_years()
        baseline = _window_mean(values, years, base_first, base_last)
        if not np.isfinite(baseline):
            msg = (
                f"HadCRUT5 does not cover the {base_first}-{base_last} "
                f"baseline window"
            )
            raise ValueError(msg)
        # Anomalies first, then the blending correction: it is a correction
        # to the *change*, not to the absolute level.
        anomalies = values - baseline
        observed = self._scalars(anomalies, years)
        out: dict[str, float] = {}
        for name, value in observed.items():
            if not name.startswith("gmst_"):  # model-side provenance only
                continue
            corrected, sigma = blended_to_sat(value)
            out[name] = corrected
            out[f"{name}{SCALAR_SIGMA_OBS_SUFFIX}"] = sigma
        return out

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        values, years = self._series(complex_data_source.data["historical"])
        return self._scalar_outputs(self._scalars(values, years))


class PinatuboResponseGate(_GlobalMeanTasMixin, CB2ComplexDiagnostic):
    """Pinatubo (1991-93) response: surface dimming and cooling.

    Global-mean ``rsds`` and ``tas`` anomalies for Jul 1991 - Dec 1993
    relative to the ``tier2.climatology_baseline_period`` (1985-2014)
    climatology. Two sign flags survive as gate rows tagged
    ``requirement: diagnostic`` (Δrsds < 0, Δtas < 0), never part of the
    entry ticket; the **magnitudes** are now scored as aggregated scalars by
    :mod:`climatebench2.scoring_pass`.

    ``pinatubo_tas_anom`` has an observational counterpart — HadCRUT5 over
    the same window and the same baseline, corrected to a SAT basis — so the
    pass scores it and writes its consistency row. ``pinatubo_rsds_anom``
    does **not**: ClimateEval has no BSRN or CERES-SYN DataSource, so the
    dimming stays a model-only sign flag (the pass skips a scalar whose
    reference carries no value). Neither is the joint [Δrsds, Δtas]
    co-variation test of §II.1, nor is ENSO regressed out.
    """

    _required_data_keys = ("historical",)
    _gate_checks = (
        GateCheck(
            check_id="pinatubo_dimming",
            column="pinatubo_rsds_anom",
            upper=Threshold(".rsds_anomaly_max"),
            block="tier2.pinatubo",
        ),
        GateCheck(
            check_id="pinatubo_cooling",
            column="pinatubo_tas_anom",
            upper=Threshold(".tas_anomaly_max"),
            block="tier2.pinatubo",
        ),
    )

    @staticmethod
    def _event_anomaly(cube: Cube) -> float:
        """Event-window mean minus the baseline mean of an area-mean cube."""
        clim_first, clim_last = windows.baseline_window_years()
        (event_year0, event_month0), (event_year1, event_month1) = PINATUBO_WINDOW
        with setup_esmvaltool_config_and_logging():
            clim_cube = extract_time(cube, clim_first, 1, 1, clim_last, 12, 31)
            clim = float(
                np.asarray(climate_statistics(clim_cube, "mean", "full").data),
            )
            event = extract_time(
                cube,
                event_year0,
                event_month0,
                1,
                event_year1,
                event_month1,
                31,
            )
        return float(np.nanmean(_filled(event.data)) - clim)

    def _anomaly(self, data: CubeList | Dataset, variable: Variable) -> float:
        cube = self._cube(data, variable)
        with setup_esmvaltool_config_and_logging():
            cube = area_statistics(cube, "mean")
        return self._event_anomaly(cube)

    def _observed_scalars(self) -> dict[str, float]:
        """The observed ``tas`` anomaly (HadCRUT5, SAT basis)."""
        cube = self._regridded(self._observation_record())
        with setup_esmvaltool_config_and_logging():
            cube = area_statistics(cube, "mean")
        corrected, sigma = blended_to_sat(self._event_anomaly(cube))
        return {
            "pinatubo_tas_anom": corrected,
            f"pinatubo_tas_anom{SCALAR_SIGMA_OBS_SUFFIX}": sigma,
        }

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        historical = complex_data_source.data["historical"]
        return self._scalar_outputs(
            {
                "pinatubo_rsds_anom": self._anomaly(historical, _mon("rsds")),
                "pinatubo_tas_anom": self._anomaly(historical, _mon("tas")),
            },
        )


class HemisphericAsymmetryGate(_GlobalMeanTasMixin, CB2ComplexDiagnostic):
    """Aerosol-era hemispheric asymmetry.

    NH-SH ``tas`` trend difference (NH suppressed by aerosol forcing → < 0)
    and the associated southward ITCZ shift (trend of the zonal-mean-``pr``
    maximum latitude < 0), both over ``tier2.hemispheric_asymmetry.era``
    (1950-1985). The two sign flags stay gate rows tagged
    ``requirement: diagnostic``; the ``nh_minus_sh_trend`` **magnitude** is
    scored as an aggregated scalar against HadCRUT5 over the same era,
    corrected to a SAT basis (a multiplicative correction scales the
    hemispheric difference too).

    The ITCZ shift has no observed counterpart: GPCP begins in 1979, well
    after the aerosol era, so it stays a model-only sign flag.
    """

    _required_data_keys = ("historical",)
    _gate_checks = (
        GateCheck(
            check_id="hemispheric_trend_asymmetry",
            column="nh_minus_sh_trend",
            upper=Threshold(".nh_minus_sh_trend_max"),
            block="tier2.hemispheric_asymmetry",
        ),
        GateCheck(
            check_id="itcz_southward_shift",
            column="itcz_shift_deg_per_decade",
            upper=Threshold(".itcz_shift_max"),
            block="tier2.hemispheric_asymmetry",
        ),
    )

    @property
    def _era(self) -> tuple[int, int]:
        """Aerosol era (first, last year) from ``thresholds.yml``."""
        era = self._threshold("tier2.hemispheric_asymmetry.era")
        return (int(era[0]), int(era[1]))

    def _hemisphere_trend(
        self,
        data: CubeList | Dataset,
        lat0: float,
        lat1: float,
    ) -> float:
        cube = self._cube(data, _mon("tas"))
        with setup_esmvaltool_config_and_logging():
            cube = extract_time(cube, self._era[0], 1, 1, self._era[1], 12, 31)
            cube = extract_region(
                cube,
                start_longitude=0.0,
                end_longitude=360.0,
                start_latitude=lat0,
                end_latitude=lat1,
            )
            cube = area_statistics(cube, "mean")
            cube = annual_statistics(cube, "mean")
        return physics.ols_trend(_filled(cube.data))

    def _itcz_shift(self, data: CubeList | Dataset) -> float:
        """Trend (°/decade) of the annual zonal-mean-pr maximum latitude."""
        cube = self._cube(data, _mon("pr"))
        with setup_esmvaltool_config_and_logging():
            cube = extract_time(cube, self._era[0], 1, 1, self._era[1], 12, 31)
            cube = annual_statistics(cube, "mean")
            cube = zonal_statistics(cube, "mean")
        pr = np.asarray(cube.data, dtype=float)  # (year, lat)
        lats = cube.coord("latitude").points.astype(float)
        tropics = (lats >= -30.0) & (lats <= 30.0)
        itcz = lats[tropics][np.argmax(pr[:, tropics], axis=1)]
        return physics.ols_trend(itcz) * 10.0  # per decade

    def _observed_scalars(self) -> dict[str, float]:
        """The observed NH-SH trend difference (HadCRUT5, SAT basis)."""
        cube = self._regridded(self._observation_record())
        first, last = self._era
        difference = 0.0
        for lat0, lat1, sign in ((0.0, 90.0, 1.0), (-90.0, 0.0, -1.0)):
            values, years = self._regional_annual_series(cube, lat0, lat1)
            inside = (years >= first) & (years <= last)
            if inside.sum() < 2:  # noqa: PLR2004 - a trend needs two points
                msg = f"HadCRUT5 does not cover the {first}-{last} aerosol era"
                raise ValueError(msg)
            difference += sign * physics.ols_trend(values[inside])
        corrected, sigma = blended_to_sat(difference)
        return {
            "nh_minus_sh_trend": corrected,
            f"nh_minus_sh_trend{SCALAR_SIGMA_OBS_SUFFIX}": sigma,
        }

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        historical = complex_data_source.data["historical"]
        nh = self._hemisphere_trend(historical, 0.0, 90.0)
        sh = self._hemisphere_trend(historical, -90.0, 0.0)
        return self._scalar_outputs(
            {
                "nh_minus_sh_trend": nh - sh,
                "nh_trend": nh,
                "sh_trend": sh,
                "itcz_shift_deg_per_decade": self._itcz_shift(historical),
            },
        )


# ---------------------------------------------------------------------------
# Seasonal-cycle metrics (§II.1 "Seasonal-cycle metrics")
# ---------------------------------------------------------------------------


class _SeasonalCycleDiagnostic(ObservedScalarMixin, CB2ComplexDiagnostic):
    """Shared machinery of the three seasonal-cycle statistics of §II.1.

    Each one is a property of the **12-month climatology**, computed over the
    protocol's fixed ``tier2.climatology_baseline_period`` (1985-2014) so
    that every submission and every observational product see the same
    years, and labelled *in-sample*. The window is emitted as
    ``seasonal_window_first/last_year`` on the model side only, so the
    scoring pass — which scores a scalar only where the reference carries a
    value — treats it as provenance rather than as a statistic.

    A model whose record does not cover the window is not skipped: its own
    record is used, with a warning, because a seasonal cycle is far less
    window-sensitive than a trend.
    """

    _required_data_keys = ("historical",)
    #: Protocol standing (thresholds.yml): reported numbers, never a gate.
    requirement: ClassVar[str] = BlockTag("tier2.seasonal")  # type: ignore[assignment]

    def _window(self, cube: Cube) -> Cube:
        """The climatology window of the protocol, or the whole record."""
        first, last = windows.baseline_window_years()
        try:
            with setup_esmvaltool_config_and_logging():
                return extract_time(cube, first, 1, 1, last, 12, 31)
        except Exception as exc:  # noqa: BLE001 - a short record is not fatal
            logger.warning(
                f"Diagnostic '{self.name}': cannot cut to the {first}-{last} "
                f"seasonal-cycle window ({exc}); using the whole record",
            )
            return cube

    def _climatology(self, cube: Cube) -> Cube:
        """The 12-month climatology of a regridded monthly cube."""
        with setup_esmvaltool_config_and_logging():
            return climate_statistics(self._window(cube), "mean", "month")

    def _model_climatology(
        self,
        data: CubeList | Dataset,
        var_name: str,
    ) -> Cube:
        return self._climatology(self._cube(data, _mon(var_name)))

    def _observed_climatology(
        self,
        var_name: str,
        source: type[DataSource],
    ) -> Cube:
        """The same 12-month climatology from one observational product."""
        first, last = windows.baseline_window_years()
        cube = self._observation_cube(
            var_name,
            windows.timerange(first, last),
            source=source,
        )
        return self._climatology(self._regridded(cube))

    @staticmethod
    def _box_series(climatology: Cube, box: dict[str, list[float]]) -> np.ndarray:
        """Area-mean 12-month series over one lat/lon box.

        Longitudes may be negative (the Namibian deck straddles Greenwich):
        ``extract_region`` goes through ``iris.Cube.intersection``, which is
        modular, and ``area_statistics`` weights by true cell area.
        """
        lat0, lat1 = (float(v) for v in box["lat"])
        lon0, lon1 = (float(v) for v in box["lon"])
        with setup_esmvaltool_config_and_logging():
            cube = extract_region(
                climatology,
                start_longitude=lon0,
                end_longitude=lon1,
                start_latitude=lat0,
                end_latitude=lat1,
            )
            cube = area_statistics(cube, "mean")
        return _filled(cube.data)

    def _window_provenance(self) -> dict[str, float]:
        first, last = windows.baseline_window_years()
        return {
            "seasonal_window_first_year": float(first),
            "seasonal_window_last_year": float(last),
        }


class LandAnnualTemperatureRange(_SeasonalCycleDiagnostic):
    """§II.1 (i): the climatological annual temperature range over land.

    ``max − min`` of the 12-month ``tas`` climatology **at each land grid
    point**, then an area-weighted mean over land — the order matters: the
    range of the area-mean seasonal cycle would cancel the hemispheres
    against each other and be several times smaller.

    Emits ``land_annual_temperature_range`` (K). Reference: **ERA5Monthly**
    ``tas`` (``VARIABLE_MAPPING`` has ``2m_temperature``). ⚠ ``climateeval.data
    .CRU`` (upstream PR #49, merged) is the better land-only reference and
    should replace or join ERA5 here once CRU TS is staged.
    """

    _observation_source: ClassVar[type[DataSource]] = ERA5Monthly

    def _range(self, cube: Cube) -> float:
        """Area-mean over land of the gridpoint annual temperature range."""
        with setup_esmvaltool_config_and_logging():
            land = mask_landsea(cube, "sea")
        climatology = self._climatology(land)
        data = _filled(climatology.data)  # (12, lat, lon)
        span = np.nanmax(data, axis=0) - np.nanmin(data, axis=0)
        latitudes = climatology.coord("latitude").points.astype(float)
        return physics.area_weighted_mean(span, latitudes)

    def _observed_scalars(self) -> dict[str, float]:
        first, last = windows.baseline_window_years()
        cube = self._observation_cube(
            "tas",
            windows.timerange(first, last),
        )
        return {"land_annual_temperature_range": self._range(self._regridded(cube))}

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        cube = self._cube(complex_data_source.data["historical"], _mon("tas"))
        return self._scalar_outputs(
            {
                "land_annual_temperature_range": self._range(cube),
                **self._window_provenance(),
            },
        )


class _StratocumulusRegression(_SeasonalCycleDiagnostic):
    """Regression of a cloud/radiation field on SST over the seasonal cycle.

    Shared by §II.1 (ii) and (iii): in each stratocumulus deck of
    ``tier2.seasonal.stratocumulus_regions`` the 12-month climatologies of
    the predictand and of ``tos`` are area-averaged over the box and
    regressed against each other (12 points), giving one slope per deck plus
    their unweighted mean — the headline number, because a single deck is
    noisy and the five together are the emergent constraint the paper names.
    """

    #: Predictand and its unit label, set by the subclasses.
    _predictand: ClassVar[str] = ""
    _scalar_prefix: ClassVar[str] = ""
    #: Where the predictand's observations come from (SST is always ESACCI).
    _predictand_source: ClassVar[type[DataSource]] = ESACCISST

    def _slopes(self, predictand: Cube, sst: Cube) -> dict[str, float]:
        """One slope per deck plus their mean, from two 12-month climatologies."""
        decks = self._threshold("tier2.seasonal.stratocumulus_regions")
        out: dict[str, float] = {}
        values: list[float] = []
        for deck, box in decks.items():
            slope = physics.pooled_regression_slope(
                self._box_series(predictand, box),
                self._box_series(sst, box),
            )
            out[f"{self._scalar_prefix}_{deck}"] = slope
            if np.isfinite(slope):
                values.append(slope)
        out[f"{self._scalar_prefix}_mean"] = (
            float(np.mean(values)) if values else float("nan")
        )
        return out

    def _observed_scalars(self) -> dict[str, float]:
        return self._slopes(
            self._observed_climatology(self._predictand, self._predictand_source),
            self._observed_climatology("tos", ESACCISST),
        )

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        historical = complex_data_source.data["historical"]
        return self._scalar_outputs(
            {
                **self._slopes(
                    self._model_climatology(historical, self._predictand),
                    self._model_climatology(historical, "tos"),
                ),
                **self._window_provenance(),
            },
        )


class SSTLowCloudCovariance(_StratocumulusRegression):
    """§II.1 (ii): the SST-low-cloud seasonal covariance.

    Slope of the 12-month cloud-fraction climatology on the 12-month ``tos``
    climatology in each stratocumulus deck (% per K). Observations: **ESACCI-
    CLOUD** ``clt`` on **ESACCI-SST** ``tos``.

    ⚠ **CB2 reading: ``clt`` is the low-cloud proxy.** The protocol says
    "low-cloud fraction". A genuine low-cloud fraction would come from ``cl``
    with a level selection (or an ISCCP-style simulator diagnostic), which
    needs a model-level coordinate that not every submission provides and an
    observational product whose level definition matches. In the
    stratocumulus decks total cloud cover *is* essentially low cloud, which
    is precisely why the decks are the region the constraint is evaluated
    over — but over a deck with cirrus overhead the proxy is biased.
    Recorded in docs/metrics_reference.md §II.1.
    """

    _predictand = "clt"
    _scalar_prefix = "sst_low_cloud_slope"
    _observation_source: ClassVar[type[DataSource]] = ESACCICloud
    _observation_products: ClassVar[tuple[type[DataSource], ...]] = (
        ESACCICloud,
        ESACCISST,
    )
    _predictand_source: ClassVar[type[DataSource]] = ESACCICloud


class SeasonalCloudRadiativeFeedback(_StratocumulusRegression):
    """§II.1 (iii): the seasonal cloud-radiative feedback (§5.2's constraint).

    Slope of the 12-month ``swcre`` climatology on the 12-month ``tos``
    climatology in each stratocumulus deck (W m⁻² per K) — the seasonal-cycle
    analogue of the low-cloud feedback, and an emergent constraint on it.
    ``swcre`` is a **derived** variable in ClimateEval's registry
    (``rsut``-based), so both the model and CERES-EBAF supply it without CB2
    computing a flux difference itself.

    Observations: **CERES-EBAF** ``swcre`` on **ESACCI-SST** ``tos``.
    """

    _predictand = "swcre"
    _scalar_prefix = "seasonal_swcre_slope"
    _observation_source: ClassVar[type[DataSource]] = CERESEBAF
    _observation_products: ClassVar[tuple[type[DataSource], ...]] = (
        CERESEBAF,
        ESACCISST,
    )
    _predictand_source: ClassVar[type[DataSource]] = CERESEBAF
