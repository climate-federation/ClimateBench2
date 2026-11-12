"""Tier III paleoclimate diagnostics (metrics_reference.md §III.1).

Works with the ``paleo_scripts/`` pipeline: that pipeline downloads and
processes the PMIP4 model output and the proxy compilations of paper
Appendix D; these diagnostics apply the *protocol* — the fair CRPS of a
block pseudo-ensemble against the proxy values, the complementary
site-consistency fraction, and the mid-Holocene Green-Sahara monsoon gate —
through the same ClimateEval machinery as Tiers I/II.

Data keys: ``midholocene`` / ``lgm`` / ``lig127k`` (PMIP4 experiment cubes)
and ``picontrol``.

The proxy target
----------------
:class:`PaleoProxyScore` reads **one processed dataset NetCDF** written by
``paleo_scripts/process_paleo_observations.py``::

    <paleo_data_root>/<period>/<dataset>.nc

either on a ``site`` dimension (``lat``, ``lon``, ``<var>``, ``<var>_std``)
or on a grid (1-D ``lat``/``lon``, or 2-D curvilinear ones); both are
flattened to a list of sites, which for a gridded product is exactly
nearest-neighbour regridding of the model onto the proxy grid — the right
treatment for a sparse pollen or SST compilation, where interpolating would
invent data between cells.

Reading that file is the one place Tier III touches data directly: there is
no ClimateEval DataSource for the paleo proxy compilations, and writing one
(a ``PMIP4Proxies`` CMORizer, alongside the ``lgm``/``midHolocene``/
``lig127k`` model generators of **upstream PR #45**, which is merged on the
``cb2-integration`` branch and still open upstream) is the obvious upstream
home for it. Until then this stays a thin, documented adapter — it loads a
file and reads two arrays, it does not regrid or derive anything.

What is scored and what is only reported
----------------------------------------
Paper Appendix D (2026-09) scores the **raw proxy compilations**, not the
assimilated global products, whose spatial covariances come from the models
used in the assimilation. Every dataset's ``dataset_type`` attribute is
carried onto its rows; a product whose type is outside
``tier3.scored_dataset_types`` is computed and reported in full but marked
``reason = "data_assimilation product: reported, excluded from the protocol
score (paper App. D)"``. Three datasets are not scoreable targets at all
(:data:`NOT_SCOREABLE`) and stop with a logged reason.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import ibis
import numpy as np
import pandas as pd
from esmvalcore.preprocessor import (
    climate_statistics,
    extract_region,
)
from loguru import logger

from climateeval._config import setup_esmvaltool_config_and_logging
from climateeval.diags._base import DiagnosticOutput

from climatebench2 import scoring
from climatebench2._thresholds import get_threshold
from climatebench2.diags.pass_fail import GateCheck, Threshold
from climatebench2.diags.tier1_physics import (
    CB2ComplexDiagnostic,
    _cube_years_months,
    _filled,
    _mon,
)
from climatebench2.scoring_pass import (
    TIER3_SCORER,
    WINDOW_HELD_OUT,
    empty_score_row,
)

if TYPE_CHECKING:
    from iris.cube import Cube, CubeList
    from xarray import Dataset

    from climateeval import Variable
    from climateeval.data import ComplexDataSource, DataSourceInformation

JJAS_MONTHS = (6, 7, 8, 9)
ALL_MONTHS = tuple(range(1, 13))
SECONDS_PER_DAY = 86400.0
DAYS_PER_YEAR = 365.25

#: Data key -> the directory ``process_paleo_observations.py`` writes under
#: (the pipeline uses the CMIP6 experiment spelling, the suites the lower-case
#: data key).
PERIOD_DIRECTORIES = {
    "lgm": "lgm",
    "midholocene": "midHolocene",
    "lig127k": "lig127k",
}

#: Processed datasets that exist but are **not scoreable targets**, with the
#: reason the protocol cannot use them. A suite stanza naming one of these
#: writes the reason instead of a number (the stanzas themselves are left
#: commented out in ``ClimateBench2_TierIII.yml``).
NOT_SCOREABLE: dict[str, str] = {
    "Osman2021Proxies_proxy": (
        "uncalibrated proxy measurements (UK'37, TEX86, Mg/Ca, planktic d18O "
        "in native units), not SST: calibration needs the Bayesian forward "
        "models the pipeline deliberately does not choose — use "
        "Tierney2020_tos for calibrated LGM SSTs"
    ),
    "SISALv3_d18O": (
        "speleothem d18O can only be scored against isotope-enabled model "
        "output (d18O of precipitation or drip water), which CMIP6 Amon does "
        "not carry, and no calcite-precipitation fractionation is applied"
    ),
    "Hoffman2017_tos": (
        "LIG SST compilation not validated as the paper's target: App. D "
        "names Osman et al. 2026, which supersedes it and has no public "
        "archive as of 2026-09"
    ),
    "Temp12k_tas": (
        "latitude-band ensemble reconstruction (method x latband x age x "
        "ensemble), not a site or gridded field: it needs its own zonal-mean "
        "comparison rather than site sampling"
    ),
}

#: ``reason`` written on the rows of a product the protocol reports but does
#: not score.
DA_REASON = (
    "data_assimilation product: reported, excluded from the protocol score "
    "(paper App. D scores the raw compilations)"
)

#: No post-2015 analogue exists for a paleo time slice, so there is no
#: comparison ensemble to form E_ref from.
#:
#: ⚠ The text names upstream ClimateEval PR #45, which since 2026-09-20 is
#: merged on the ``cb2-integration`` branch — so the generator now exists and
#: the real reason is that **no PMIP4 model pool is staged** for it to find.
#: The string is written into result databases and asserted on by
#: ``tests/test_tier3_proxies.py``, so it is left alone; read it as a staging
#: statement until a PMIP4 pool is staged and the rows stop being emitted.
NO_REFERENCE_ENSEMBLE = "no PMIP4 comparison ensemble (ClimateEval PR #45)"


def _units_factor(model_var: str, units_attr: str | None) -> float | None:
    """Model-anomaly units -> the proxy file's units, or ``None`` if unknown.

    Model cubes arrive in ClimateEval's registry units (``tas`` K, ``tos``
    degC, ``pr`` kg m-2 s-1). Temperature *anomalies* are the same number in
    K and in degC, so every temperature spelling maps to 1; precipitation
    does not, and the pollen compilations publish mm/yr.

    Returning ``None`` (rather than guessing 1) is deliberate: a silently
    wrong precipitation factor is a factor of 3 x 10^7.
    """
    text = str(units_attr or "").lower()
    if model_var in {"tas", "tos", "ts"}:
        if any(token in text for token in ("kelvin", "celsius")) or text.startswith(
            ("k", "c", "degc", "deg c", "degree"),
        ):
            return 1.0
        return None
    if model_var == "pr":
        if "mm/yr" in text or "mm yr" in text or "mm/year" in text:
            return SECONDS_PER_DAY * DAYS_PER_YEAR
        if "mm/day" in text or "mm day" in text or "mm/d" in text:
            return SECONDS_PER_DAY
        if "kg m-2 s-1" in text or "kg/m2/s" in text:
            return 1.0
        # Bartlein's raw `mm` is an annual total (mm/yr); the pipeline writes
        # the axis units through unchanged, so accept it with the same factor.
        if text.startswith("mm"):
            return SECONDS_PER_DAY * DAYS_PER_YEAR
        return None
    return None


def _flatten_sites(
    dataset: Dataset,
    var_name: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(values, sigma, lats, lons)`` of one proxy variable, flattened.

    Handles all three layouts the pipeline writes — a ``site`` dimension, a
    regular ``lat``/``lon`` grid and 2-D curvilinear coordinates — by
    broadcasting ``lat``/``lon`` against the data and flattening: a gridded
    product becomes a list of cell centres, so the same nearest-gridpoint
    sampling serves both.

    Sites with no finite value are dropped. ``sigma`` is all-NaN when the
    file carries no ``<var>_std``.
    """
    values = np.asarray(dataset[var_name].to_numpy(), dtype=float)
    sigma_name = next(
        (n for n in (f"{var_name}_std", f"{var_name}_sigma") if n in dataset.variables),
        None,
    )
    sigma = (
        np.asarray(dataset[sigma_name].to_numpy(), dtype=float)
        if sigma_name
        else np.full(values.shape, np.nan)
    )
    lat = np.asarray(dataset["lat"].to_numpy(), dtype=float)
    lon = np.asarray(dataset["lon"].to_numpy(), dtype=float)
    if lat.shape != values.shape or lon.shape != values.shape:
        dims = dataset[var_name].dims
        lat_dims = dataset["lat"].dims
        lon_dims = dataset["lon"].dims
        lat = _broadcast_coordinate(lat, lat_dims, dims, values.shape)
        lon = _broadcast_coordinate(lon, lon_dims, dims, values.shape)
    flat = (values.ravel(), sigma.ravel(), lat.ravel(), lon.ravel())
    keep = np.isfinite(flat[0]) & np.isfinite(flat[2]) & np.isfinite(flat[3])
    return tuple(array[keep] for array in flat)  # type: ignore[return-value]


def _broadcast_coordinate(
    coordinate: np.ndarray,
    coordinate_dims: tuple[str, ...],
    value_dims: tuple[str, ...],
    shape: tuple[int, ...],
) -> np.ndarray:
    """Broadcast a coordinate onto the value array's shape."""
    axes = [value_dims.index(dim) for dim in coordinate_dims]
    expanded = np.ones(len(shape), dtype=int)
    for axis, size in zip(axes, coordinate.shape, strict=True):
        expanded[axis] = size
    return np.broadcast_to(coordinate.reshape(expanded), shape)


class _ScoreRowMixin:
    """Emit :mod:`climatebench2.scoring_pass`-shaped rows from a diagnostic.

    Tier III cannot use the post-suite pass: its pseudo-ensemble is built
    from blocks of a **single** run inside the diagnostic, so there is
    nothing for the pass to stack afterwards. The rows therefore come from
    here, in the pass's own column vocabulary and tagged
    ``scorer = TIER3_SCORER`` so a later ``leaderboard --rescore`` — which
    deletes the rows tagged ``climatebench2`` — leaves them alone.
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        """Initialize class instance."""
        self._pending_rows: list[dict[str, Any]] = []
        super().__init__(name, **kwargs)  # type: ignore[call-arg]

    def _row(self, var_id: str, reason: str = "", **fields: Any) -> dict[str, Any]:
        """Queue one score row (``data_id`` filled in by :meth:`get_output`)."""
        row = empty_score_row("", "to_benchmark", var_id, reason, scorer=TIER3_SCORER)
        row.update(window=WINDOW_HELD_OUT, **fields)
        self._pending_rows.append(row)
        return row

    def get_output(
        self,
        data: Any,  # noqa: ANN401
        data_information: DataSourceInformation,
    ) -> DiagnosticOutput:
        """Run the diagnostic, then merge the queued score rows into metrics."""
        self._pending_rows = []
        output = super().get_output(data, data_information)  # type: ignore[misc]
        if not self._pending_rows:
            return output
        frame = pd.DataFrame(self._pending_rows)
        frame["data_id"] = data_information.id
        if output.metrics is not None:
            existing = output.metrics.to_pandas()
            frame = pd.concat([existing, frame], ignore_index=True, sort=False)
            for column in ("requirement", "tier", "reason", "window", "dataset_type"):
                if column in frame.columns:
                    frame[column] = frame[column].fillna("")
        return DiagnosticOutput(
            raw_output=output.raw_output,
            metrics=ibis.memtable(frame),
            variables=output.variables,
            data_sources=output.data_sources,
        )


class MidHoloceneMonsoonGate(CB2ComplexDiagnostic):
    """III.1 hard requirement: Green-Sahara monsoon amplification.

    mid-Holocene JJAS precipitation anomaly vs piControl, area-mean over
    North Africa (10–30N, 20W–30E; region wraps 0° longitude), must be
    ≥ +0.5 mm/day (``tier3.midholocene_monsoon``).

    With ``harrison_dataset`` (the default, when the pipeline has processed
    it) the **observed** North-Africa precipitation magnitude of Harrison &
    Prentice's mid-Holocene moisture benchmark is reported beside the
    modelled anomaly — as ``northafrica_pr_anom_obs_mmday``, next to the
    model's own **annual**-mean anomaly ``annual_pr_anom_mmday``, because
    the Harrison compilation is an annual precipitation anomaly (mm/yr) and
    is *not* the JJAS quantity the gate bounds. It is reported, never gated.
    """

    _required_data_keys = ("midholocene", "picontrol")

    _gate_checks = (
        GateCheck(
            check_id="midholocene_monsoon",
            column="jjas_pr_anom_mmday",
            lower=Threshold(".jjas_pr_anom_min"),
            block="tier3.midholocene_monsoon",
        ),
    )

    def __init__(
        self,
        name: str,
        *,
        paleo_data_root: str | Path | None = None,
        harrison_dataset: str | None = "Harrison2015_pr",
        **kwargs: Any,
    ) -> None:
        """Initialize class instance."""
        super().__init__(name, **kwargs)
        self._paleo_data_root = Path(
            paleo_data_root or get_threshold("tier3.paleo_data_root"),
        )
        self._harrison_dataset = harrison_dataset

    def _region_mean(
        self,
        data: CubeList | Dataset,
        months: tuple[int, ...],
    ) -> float:
        """Precipitation climatology over North Africa (kg m-2 s-1)."""
        cube = self._cube(data, _mon("pr"))
        with setup_esmvaltool_config_and_logging():
            # extract_region handles the 20W..30E wrap (start > end)
            cube = extract_region(
                cube,
                start_longitude=float(self._region["lon"][0]) % 360.0,
                end_longitude=float(self._region["lon"][1]),
                start_latitude=float(self._region["lat"][0]),
                end_latitude=float(self._region["lat"][1]),
            )
            clim = climate_statistics(cube, "mean", "month")
        month_numbers = clim.coord("month_number").points.astype(int)
        selected = np.isin(month_numbers, months)
        data_arr = _filled(clim.data)[selected]
        lats = clim.coord("latitude").points.astype(float)
        w = np.cos(np.deg2rad(lats))[None, :, None]
        w = np.broadcast_to(w, data_arr.shape)
        valid = np.isfinite(data_arr)
        return float((data_arr * w)[valid].sum() / w[valid].sum())

    def _harrison_magnitude(self) -> float:
        """Observed North-Africa precipitation anomaly (mm/day), or NaN.

        Harrison & Prentice publish a latitudinal profile (mm/yr); the
        reported number is the cos-weighted mean over the gate's own
        latitude band, converted to mm/day so the two magnitudes are in the
        same units. Best-effort: a missing file is a logged note, never a
        failure.
        """
        if not self._harrison_dataset:
            return float("nan")
        path = (
            self._paleo_data_root
            / PERIOD_DIRECTORIES["midholocene"]
            / f"{self._harrison_dataset}.nc"
        )
        if not path.is_file():
            logger.info(
                f"Diagnostic '{self.name}': no Harrison 2015 benchmark at "
                f"{path}; reporting the modelled anomaly alone",
            )
            return float("nan")
        import xarray as xr

        with xr.open_dataset(path) as dataset:
            values = np.asarray(dataset["pr"].to_numpy(), dtype=float)
            lats = np.asarray(dataset["lat"].to_numpy(), dtype=float)
            factor = _units_factor("pr", dataset.attrs.get("units"))
        lat_lo, lat_hi = (float(v) for v in self._region["lat"])
        band = (lats >= lat_lo) & (lats <= lat_hi) & np.isfinite(values)
        if not band.any() or factor is None:
            return float("nan")
        weights = np.cos(np.deg2rad(lats[band]))
        # `factor` takes kg m-2 s-1 to the file's units; the observed value
        # goes the other way, then out in mm/day.
        mean = float(np.average(values[band], weights=weights))
        return mean / factor * SECONDS_PER_DAY

    def _calculate_raw_output(
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        data = complex_data_source.data
        jjas = self._region_mean(data["midholocene"], JJAS_MONTHS) - self._region_mean(
            data["picontrol"],
            JJAS_MONTHS,
        )
        annual = self._region_mean(data["midholocene"], ALL_MONTHS) - self._region_mean(
            data["picontrol"],
            ALL_MONTHS,
        )
        return self._scalar_outputs(
            {
                "jjas_pr_anom_mmday": jjas * SECONDS_PER_DAY,
                "annual_pr_anom_mmday": annual * SECONDS_PER_DAY,
                "northafrica_pr_anom_obs_mmday": self._harrison_magnitude(),
            },
        )


class PaleoProxyScore(_ScoreRowMixin, CB2ComplexDiagnostic):
    """III.1: fair CRPS of a paleo time slice against one proxy dataset.

    The protocol's primary Tier III score (metrics_reference.md §III.1).
    For each suite stanza — one (period, dataset, variable) triple — the
    diagnostic:

    1. builds the **pseudo-ensemble**: the equilibrated portion of the paleo
       run (drop ``tier3.spinup_years``) is cut into non-overlapping blocks
       of ``tier3.block_years``, and each block's climatology minus the full
       piControl climatology is one pseudo-member (paper §5.1). Fewer than
       ``tier3.min_pseudo_members`` blocks means fair CRPS is undefined, and
       the diagnostic writes a ``reason`` row instead of a number;
    2. samples every member at the proxy sites (nearest gridpoint,
       :func:`climatebench2.scoring.sample_at_sites`; a gridded product's
       cell centres are its sites);
    3. scores them with :func:`climatebench2.scoring.proxy_crps` — fair CRPS
       per site with the proxy σ as the observational uncertainty, averaged
       with equal weight over sites (``tier3.site_weighting``);
    4. reports the complementary **site-consistency fraction**, now with the
       pseudo-member spread in the denominator alongside σ_proxy.

    ``skill`` and ``e_ref`` stay NaN: scoring them needs a PMIP4 comparison
    ensemble. Upstream ClimateEval PR #45 adds the generator and is merged on
    the ``cb2-integration`` branch, but no PMIP4 model pool is staged for it
    to find — see :data:`NO_REFERENCE_ENSEMBLE`.

    Diagnostic kwargs (per suite entry):

    - ``period_key`` — which data key holds the time slice (``lgm``,
      ``lig127k``, ``midholocene``);
    - ``dataset`` — the processed file's stem, e.g. ``Tierney2020_tos``;
    - ``var_name`` — the proxy variable in that file (``tas``/``tos``/``pr``);
    - ``model_var`` — the model variable to compare it with (defaults to
      ``var_name``: ``tos`` proxies score against model ``tos``, land ``tas``
      against ``tas``);
    - ``landsea_mask`` — ClimateEval's own mask for the model variable
      (``land_only`` for the pollen compilations, ``sea_only`` for SSTs);
    - ``season_months`` — the months the scored climatology averages
      (``null`` = the annual mean, ``tier3.seasonality``);
    - ``paleo_data_root`` — where the pipeline's NetCDFs live
      (``tier3.paleo_data_root``; ``score --paleo-data-root DIR`` overrides).
    """

    _required_data_keys: ClassVar[tuple[str, ...]] = ("picontrol",)  # + period_key
    _gate_checks = ()

    def __init__(
        self,
        name: str,
        *,
        period_key: str = "lgm",
        dataset: str = "",
        var_name: str = "tas",
        model_var: str | None = None,
        landsea_mask: str | None = None,
        season_months: list[int] | tuple[int, ...] | None = None,
        paleo_data_root: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize class instance."""
        super().__init__(name, **kwargs)
        self._period_key = period_key
        self._dataset = dataset
        self._var_name = var_name
        self._model_var = model_var or var_name
        self._landsea_mask = landsea_mask
        self._season_months = (
            tuple(int(m) for m in season_months) if season_months else ALL_MONTHS
        )
        self._paleo_data_root = Path(
            paleo_data_root or get_threshold("tier3.paleo_data_root"),
        )

    # -- inputs -------------------------------------------------------------

    @property
    def _key(self) -> str:
        """``var_id`` prefix identifying this (period, dataset, variable)."""
        return f"{self._period_key}_{self._dataset}_{self._var_name}"

    @property
    def proxy_path(self) -> Path:
        """Processed NetCDF this stanza scores against."""
        directory = PERIOD_DIRECTORIES.get(self._period_key, self._period_key)
        return self._paleo_data_root / directory / f"{self._dataset}.nc"

    def _check_required_dict_keys(self, dict_: dict[str, Any], dict_name: str) -> None:
        missing = {self._period_key, "picontrol"} - set(dict_)
        if missing:
            msg = (
                f"Missing keys {sorted(missing)} for {dict_name} dictionary of "
                f"diagnostic '{self.name}'"
            )
            raise ValueError(msg)

    def _model_variable(self) -> Variable:
        """The model variable, with ClimateEval's own land/sea mask applied.

        Masking is ClimateEval's job, not CB2's: a pollen compilation scores
        against ``tas`` with ``land_only``, an SST compilation against ``tos``
        with ``sea_only``, and a site whose nearest gridpoint is masked comes
        back NaN and is dropped from the score rather than compared to the
        wrong surface.
        """
        variable = _mon(self._model_var)
        if self._landsea_mask:
            variable = replace(variable, landsea_mask=self._landsea_mask)
        return variable

    def _annual_field(self, data: CubeList | Dataset) -> np.ndarray:
        """``(n_years, lat, lon)`` climatological means of the scored season."""
        cube = self._cube(data, self._model_variable())
        years, months = _cube_years_months(cube)
        values = _filled(cube.data)
        selected = np.isin(months, self._season_months)
        unique_years = np.unique(years[selected])
        with warnings.catch_warnings():
            # A permanently masked gridpoint (land under `sea_only`) is NaN by
            # design; sampling drops it, so there is nothing to warn about.
            warnings.filterwarnings("ignore", message="Mean of empty slice")
            return np.stack(
                [
                    np.nanmean(values[selected & (years == year)], axis=0)
                    for year in unique_years
                ],
            )

    def _control_climatology(self, data: CubeList | Dataset) -> np.ndarray:
        """Full-record piControl climatology of the scored season."""
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Mean of empty slice")
            return np.nanmean(self._annual_field(data), axis=0)

    def _grid(self, data: CubeList | Dataset) -> tuple[np.ndarray, np.ndarray]:
        cube = self._cube(data, self._model_variable())
        return (
            cube.coord("latitude").points.astype(float),
            cube.coord("longitude").points.astype(float),
        )

    # -- the score ----------------------------------------------------------

    def _skip(self, reason: str) -> dict[Variable, Cube]:
        """Write the reason and emit no numbers."""
        logger.warning(f"Diagnostic '{self.name}' not scored: {reason}")
        self._row(self._key, reason)
        return self._scalar_outputs({f"{self._key}_n_sites": float("nan")})

    def _calculate_raw_output(  # noqa: C901, PLR0911
        self,
        complex_data_source: ComplexDataSource,
    ) -> dict[Variable, Cube]:
        if self._dataset in NOT_SCOREABLE:
            return self._skip(NOT_SCOREABLE[self._dataset])
        if not self.proxy_path.is_file():
            return self._skip(
                f"no processed proxy dataset at {self.proxy_path} — run "
                f"paleo_scripts/process_paleo_observations.py, or point "
                f"--paleo-data-root at the cache",
            )

        import xarray as xr

        with xr.open_dataset(self.proxy_path) as proxies:
            if self._var_name not in proxies.variables:
                return self._skip(
                    f"{self.proxy_path.name} carries no variable "
                    f"'{self._var_name}' (has {sorted(proxies.data_vars)})",
                )
            dataset_type = str(proxies.attrs.get("dataset_type", "unknown"))
            units_attr = proxies.attrs.get("units")
            factor = _units_factor(self._model_var, units_attr)
            values, sigma, site_lats, site_lons = _flatten_sites(
                proxies,
                self._var_name,
            )

        if factor is None:
            return self._skip(
                f"cannot convert model '{self._model_var}' to the dataset's "
                f"units '{units_attr}'",
            )
        if not np.isfinite(sigma).any():
            return self._skip(
                f"{self.proxy_path.name} carries no proxy uncertainty "
                f"('{self._var_name}_std'), which the fair CRPS needs as its "
                f"observational variance term",
            )

        data = complex_data_source.data
        annual = self._annual_field(data[self._period_key])
        control = self._control_climatology(data["picontrol"])
        block_years = int(get_threshold("tier3.block_years"))
        spinup = int(get_threshold("tier3.spinup_years"))
        blocks = scoring.block_climatologies(annual, block_years, spinup=spinup)
        min_members = int(get_threshold("tier3.min_pseudo_members"))
        if blocks.shape[0] < min_members:
            return self._skip(
                f"{annual.shape[0]} yr of {self._period_key} output gives "
                f"{blocks.shape[0]} pseudo-member(s) after dropping "
                f"{spinup} yr of spin-up at {block_years} yr per block; fair "
                f"CRPS needs at least {min_members}",
            )

        members = (blocks - control[None, ...]) * factor
        lats, lons = self._grid(data[self._period_key])
        at_sites = np.stack(
            [
                scoring.sample_at_sites(m, lats, lons, site_lats, site_lons)
                for m in members
            ],
        )

        try:
            summary, per_site = scoring.proxy_crps(
                at_sites,
                values,
                sigma,
                n_draws=int(get_threshold("tier2.obs_uncertainty.n_draws")),
                seed=int(get_threshold("tier2.obs_uncertainty.seed")),
            )
            fraction, z = scoring.proxy_site_consistency(
                at_sites.mean(axis=0),
                values,
                sigma,
                ensemble_values=at_sites,
                p_threshold=float(get_threshold("tier2.consistency_p_value")),
            )
        except ValueError as exc:
            return self._skip(
                f"no site where the model, the proxy and its uncertainty are "
                f"all finite ({exc})",
            )

        scored = dataset_type in set(get_threshold("tier3.scored_dataset_types"))
        n_sites = float(np.isfinite(per_site).sum())
        self._row(
            self._key,
            "" if scored else DA_REASON,
            crps=summary.score,
            crps_se=summary.standard_error,
            t_eff=summary.t_eff,
            r1=summary.r1,
            n_members=float(summary.n_members),
            n_time=float(summary.n_time),
            n_sites=n_sites,
            sigma_obs=float(np.nanmean(sigma)),
            dataset_type=dataset_type,
        )
        # `skill`/`e_ref` need a comparison ensemble; say so rather than
        # leaving an unexplained empty cell on the scorecard.
        self._row(
            f"{self._key}_skill",
            NO_REFERENCE_ENSEMBLE,
            n_members=float(summary.n_members),
            n_sites=n_sites,
            dataset_type=dataset_type,
        )
        return self._scalar_outputs(
            {
                f"{self._key}_crps": summary.score,
                f"{self._key}_site_consistency": fraction,
                f"{self._key}_n_sites": n_sites,
                f"{self._key}_n_members": float(summary.n_members),
                f"{self._key}_mean_abs_z": float(np.nanmean(np.abs(z))),
            },
        )
