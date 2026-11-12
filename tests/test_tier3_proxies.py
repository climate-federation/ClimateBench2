"""Tier III: the proxy-NetCDF contract and the block pseudo-ensemble score.

Runs :class:`climatebench2.diags.PaleoProxyScore` end to end on synthetic
cubes against a **synthetic** proxy NetCDF written into ``tmp_path`` — the
tests never touch ``paleo_scripts/paleo_data_cache``, which is gitignored and
may not exist. The synthetic files copy the pipeline's layout exactly (site
or gridded, ``<var>`` + ``<var>_std``, the ``dataset_type``/``units`` global
attributes), so a change to that contract fails here.
"""

from __future__ import annotations

import numpy as np
import pytest

climateeval = pytest.importorskip("climateeval")

import xarray as xr  # noqa: E402
from cf_units import Unit  # noqa: E402
from iris.coords import DimCoord  # noqa: E402
from iris.cube import Cube, CubeList  # noqa: E402

from climateeval.data import DataSourceInformation  # noqa: E402

from climatebench2.diags.tier3_paleo import (  # noqa: E402
    DA_REASON,
    NOT_SCOREABLE,
    PaleoProxyScore,
    _units_factor,
)
from climatebench2.scoring_pass import TIER3_SCORER  # noqa: E402

N_LAT, N_LON = 18, 36


def _cube(
    var_name: str,
    units: str,
    *,
    n_years: int,
    field: np.ndarray,
    per_year: np.ndarray | None = None,
) -> Cube:
    """Monthly cube whose annual mean is ``field`` (+ a per-year offset)."""
    n_time = n_years * 12
    time = DimCoord(
        np.arange(n_time, dtype=float) * 30.0 + 15.0,
        standard_name="time",
        units=Unit("days since 1850-01-01", calendar="360_day"),
    )
    lat = DimCoord(
        np.linspace(-85.0, 85.0, N_LAT),
        standard_name="latitude",
        units="degrees",
    )
    lon = DimCoord(
        np.linspace(5.0, 355.0, N_LON),
        standard_name="longitude",
        units="degrees",
        circular=True,
    )
    for coord in (time, lat, lon):
        coord.guess_bounds()
    offsets = np.zeros(n_years) if per_year is None else np.asarray(per_year, float)
    data = np.repeat(offsets, 12)[:, None, None] + field[None, :, :]
    return Cube(
        np.asarray(data, dtype=np.float32),
        var_name=var_name,
        units=units,
        dim_coords_and_dims=[(time, 0), (lat, 1), (lon, 2)],
    )


def _pattern() -> np.ndarray:
    lat = np.linspace(-85.0, 85.0, N_LAT)
    return np.broadcast_to(
        (280.0 + 0.05 * lat)[:, None],
        (N_LAT, N_LON),
    ).astype(float)


def _experiments(
    *,
    n_years: int = 250,
    anomaly: float = -5.0,
    per_year: np.ndarray | None = None,
    var_name: str = "tas",
    units: str = "K",
) -> dict[str, CubeList]:
    """A paleo slice `anomaly` K colder than a 200-yr control."""
    base = _pattern()
    return {
        "lgm": CubeList(
            [
                _cube(
                    var_name,
                    units,
                    n_years=n_years,
                    field=base + anomaly,
                    per_year=per_year,
                ),
            ],
        ),
        "picontrol": CubeList(
            [_cube(var_name, units, n_years=200, field=base)],
        ),
    }


def _site_file(
    path,  # noqa: ANN001
    *,
    var_name: str = "tas",
    values: np.ndarray | None = None,
    sigma: float | np.ndarray = 1.0,
    dataset_type: str = "proxy_compilation",
    units: str = "K (anomaly relative to pre-industrial)",
    with_std: bool = True,
):  # noqa: ANN201
    """A site-dimension proxy file in the pipeline's layout."""
    lats = np.array([-60.0, -20.0, 0.0, 25.0, 55.0])
    lons = np.array([10.0, 120.0, 250.0, -30.0, 300.0])
    values = np.full(lats.size, -5.0) if values is None else values
    data = {
        var_name: ("site", values),
        "lat": ("site", lats),
        "lon": ("site", lons),
    }
    if with_std:
        data[f"{var_name}_std"] = ("site", np.broadcast_to(sigma, lats.shape).copy())
    dataset = xr.Dataset(data)
    dataset.attrs.update(
        source="synthetic",
        variable=var_name,
        units=units,
        period="lgm",
        dataset_type=dataset_type,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_netcdf(path)
    return path


def _info() -> DataSourceInformation:
    return DataSourceInformation(
        name="SynthModel",
        category="model",
        institute="Synth",
        exp="lgm",
        variant="r1i1p1f1",
    )


def _scalars(raw):  # noqa: ANN001, ANN201
    """``{scalar: value}`` from a scalar raw_output (one row per scalar)."""
    return {
        column: raw[column].dropna().iloc[0]
        for column in raw.columns
        if column not in {"data_id", "data_type"} and raw[column].notna().any()
    }


def _run(tmp_path, **kwargs):  # noqa: ANN001, ANN201
    experiments = kwargs.pop("experiments", None) or _experiments()
    diag = PaleoProxyScore(
        kwargs.pop("name", "lgm_synth_tas"),
        period_key="lgm",
        paleo_data_root=tmp_path,
        fail_on_missing_data=True,
        **kwargs,
    )
    output = diag.get_output(experiments, _info())
    return output.metrics.to_pandas(), output.raw_output.to_pandas()


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_units_factor_covers_the_pipeline_spellings() -> None:
    # temperature anomalies are the same number in K and degC
    for units in (
        "K",
        "C",
        "degC (anomaly)",
        "K (anomaly relative to pre-industrial)",
        "degrees Celsius",
    ):
        assert _units_factor("tas", units) == 1.0
        assert _units_factor("tos", units) == 1.0
    # precipitation is not: the pollen compilations publish mm/yr
    assert _units_factor("pr", "mm/yr (anomaly)") == pytest.approx(86400 * 365.25)
    assert _units_factor("pr", "mm") == pytest.approx(86400 * 365.25)
    assert _units_factor("pr", "mm/day") == pytest.approx(86400)
    assert _units_factor("pr", "kg m-2 s-1") == 1.0
    # an unrecognised unit is None, never a silent 1.0 (that would be a
    # factor of 3e7 wrong for precipitation)
    assert _units_factor("pr", "per mil VPDB") is None
    assert _units_factor("tas", "per mil VPDB") is None


# ---------------------------------------------------------------------------
# The score
# ---------------------------------------------------------------------------


def test_site_proxies_are_scored_with_a_block_pseudo_ensemble(tmp_path) -> None:  # noqa: ANN001
    _site_file(tmp_path / "lgm" / "Synth_tas.nc")
    metrics, raw = _run(tmp_path, dataset="Synth_tas", var_name="tas")

    scored = metrics.set_index("var_id").loc["lgm_Synth_tas_tas"]
    assert scored["scorer"] == TIER3_SCORER
    assert scored["reason"] == ""
    assert scored["window"] == "held-out"
    assert scored["dataset_type"] == "proxy_compilation"
    # 250 yr minus 100 yr of spin-up, 30 yr blocks -> 5 pseudo-members
    assert scored["n_members"] == 5
    assert scored["n_sites"] == 5
    # the model anomaly IS the proxy value, so the score is small (it is not
    # zero: the proxy sigma is drawn over)
    assert scored["crps"] < 1.0
    # skill/E_ref stay empty, with the reason named
    assert np.isnan(scored["crps"]) is np.False_
    skill = metrics.set_index("var_id").loc["lgm_Synth_tas_tas_skill"]
    assert "PR #45" in skill["reason"]
    assert np.isnan(skill["skill"])

    scalars = _scalars(raw)
    assert scalars["lgm_Synth_tas_tas_site_consistency"] == pytest.approx(1.0)
    assert scalars["lgm_Synth_tas_tas_n_sites"] == 5


def test_a_biased_model_scores_worse_than_a_good_one(tmp_path) -> None:  # noqa: ANN001
    _site_file(tmp_path / "lgm" / "Synth_tas.nc")
    good, _ = _run(tmp_path, dataset="Synth_tas", var_name="tas")
    bad, bad_raw = _run(
        tmp_path,
        dataset="Synth_tas",
        var_name="tas",
        experiments=_experiments(anomaly=+5.0),
    )
    key = "lgm_Synth_tas_tas"
    assert bad.set_index("var_id").loc[key, "crps"] > (
        good.set_index("var_id").loc[key, "crps"]
    )
    # ... and the complementary consistency fraction collapses
    assert _scalars(bad_raw)[f"{key}_site_consistency"] == 0.0


def test_gridded_proxies_are_sampled_at_their_own_cell_centres(tmp_path) -> None:  # noqa: ANN001
    """A gridded compilation is nearest-neighbour regridded onto its grid."""
    lats = np.array([-40.0, 0.0, 40.0])
    lons = np.array([20.0, 200.0])
    values = np.full((3, 2), -5.0)
    values[0, 0] = np.nan  # a cell with no proxy is dropped, not scored
    dataset = xr.Dataset(
        {
            "tas": (("lat", "lon"), values),
            "tas_std": (("lat", "lon"), np.full((3, 2), 1.0)),
        },
        coords={"lat": ("lat", lats), "lon": ("lon", lons)},
    )
    dataset.attrs.update(units="C", dataset_type="proxy_compilation", period="lgm")
    (tmp_path / "lgm").mkdir(parents=True, exist_ok=True)
    dataset.to_netcdf(tmp_path / "lgm" / "SynthGrid_tas.nc")

    metrics, _ = _run(tmp_path, dataset="SynthGrid_tas", var_name="tas")
    scored = metrics.set_index("var_id").loc["lgm_SynthGrid_tas_tas"]
    assert scored["n_sites"] == 5  # 6 cells minus the NaN one
    assert scored["crps"] < 1.0


def test_precipitation_is_converted_to_the_proxy_units(tmp_path) -> None:  # noqa: ANN001
    """A pollen mm/yr target scores a kg m-2 s-1 model anomaly correctly."""
    # model: 1 mm/day drier than the control -> -365.25 mm/yr
    control = 3.0 / 86400.0
    anomaly = -1.0 / 86400.0
    experiments = _experiments(
        anomaly=0.0,
        var_name="pr",
        units="kg m-2 s-1",
    )
    experiments["lgm"] = CubeList(
        [
            _cube(
                "pr",
                "kg m-2 s-1",
                n_years=250,
                field=np.full((N_LAT, N_LON), control + anomaly),
            ),
        ],
    )
    experiments["picontrol"] = CubeList(
        [_cube("pr", "kg m-2 s-1", n_years=200, field=np.full((N_LAT, N_LON), control))],
    )
    _site_file(
        tmp_path / "lgm" / "SynthPr_pr.nc",
        var_name="pr",
        values=np.full(5, -365.25),
        sigma=20.0,
        units="mm/yr (anomaly relative to pre-industrial)",
    )
    metrics, _ = _run(
        tmp_path,
        dataset="SynthPr_pr",
        var_name="pr",
        experiments=experiments,
    )
    scored = metrics.set_index("var_id").loc["lgm_SynthPr_pr_pr"]
    # the model matches the proxy exactly once converted; without the
    # conversion the anomaly would be -1.2e-5 against a -365 target
    assert scored["crps"] < 20.0


def test_data_assimilation_products_are_reported_but_not_scored(tmp_path) -> None:  # noqa: ANN001
    _site_file(
        tmp_path / "lgm" / "SynthDA_tas.nc",
        dataset_type="data_assimilation",
    )
    metrics, _ = _run(tmp_path, dataset="SynthDA_tas", var_name="tas")
    scored = metrics.set_index("var_id").loc["lgm_SynthDA_tas_tas"]
    assert scored["dataset_type"] == "data_assimilation"
    assert scored["reason"] == DA_REASON
    # the number is still there — reported, just excluded from the score
    assert np.isfinite(scored["crps"])


# ---------------------------------------------------------------------------
# The reason rows: everything that cannot be scored says why
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dataset", sorted(NOT_SCOREABLE))
def test_non_scoreable_datasets_write_their_reason(tmp_path, dataset: str) -> None:  # noqa: ANN001
    metrics, _ = _run(tmp_path, dataset=dataset, var_name="tas")
    (row,) = metrics.to_dict("records")
    assert row["reason"] == NOT_SCOREABLE[dataset]
    assert np.isnan(row["crps"])


def test_a_missing_dataset_file_is_a_reason_not_a_crash(tmp_path) -> None:  # noqa: ANN001
    metrics, _ = _run(tmp_path, dataset="Scussolini2019_pr", var_name="pr")
    (row,) = metrics.to_dict("records")
    assert "no processed proxy dataset" in row["reason"]


def test_a_dataset_without_an_uncertainty_column_is_not_scored(tmp_path) -> None:  # noqa: ANN001
    """Fair CRPS needs sigma_proxy; inventing one would be a fabrication."""
    _site_file(tmp_path / "lgm" / "NoSigma_tas.nc", with_std=False)
    metrics, _ = _run(tmp_path, dataset="NoSigma_tas", var_name="tas")
    (row,) = metrics.to_dict("records")
    assert "no proxy uncertainty" in row["reason"]


def test_a_short_run_cannot_form_two_pseudo_members(tmp_path) -> None:  # noqa: ANN001
    _site_file(tmp_path / "lgm" / "Synth_tas.nc")
    metrics, _ = _run(
        tmp_path,
        dataset="Synth_tas",
        var_name="tas",
        experiments=_experiments(n_years=150),  # 50 yr after spin-up -> M = 1
    )
    (row,) = metrics.to_dict("records")
    assert "pseudo-member" in row["reason"]
    assert np.isnan(row["crps"])


def test_an_unconvertible_unit_is_a_reason_not_a_wrong_number(tmp_path) -> None:  # noqa: ANN001
    _site_file(
        tmp_path / "lgm" / "Isotopes_tas.nc",
        units="per mil VPDB",
    )
    metrics, _ = _run(tmp_path, dataset="Isotopes_tas", var_name="tas")
    (row,) = metrics.to_dict("records")
    assert "cannot convert" in row["reason"]


def test_a_missing_variable_in_the_file_is_a_reason(tmp_path) -> None:  # noqa: ANN001
    _site_file(tmp_path / "lgm" / "Synth_tas.nc")
    metrics, _ = _run(tmp_path, dataset="Synth_tas", var_name="tos")
    (row,) = metrics.to_dict("records")
    assert "carries no variable 'tos'" in row["reason"]


# ---------------------------------------------------------------------------
# CB-10: spin-up (and block length) can be overridden by the gate's own block
# ---------------------------------------------------------------------------


def test_a_gates_own_block_overrides_the_global_spinup(tmp_path) -> None:  # noqa: ANN001
    from climatebench2._thresholds import use_thresholds

    _site_file(tmp_path / "lgm" / "Synth_tas.nc")

    class FixedSSTProxyScore(PaleoProxyScore):
        _threshold_block = "fixed_sst.tier3"

    def run(cls, years):  # noqa: ANN001, ANN202
        diag = cls(
            "lgm_synth_tas",
            period_key="lgm",
            dataset="Synth_tas",
            var_name="tas",
            paleo_data_root=tmp_path,
            fail_on_missing_data=True,
        )
        out = diag.get_output(_experiments(n_years=years), _info())
        return out.metrics.to_pandas().set_index("var_id").loc["lgm_Synth_tas_tas"]

    overlay = {"fixed_sst": {"tier3": {"spinup_years": 5}}}
    with use_thresholds(overlay):
        # 150 yr: the global 100 yr spin-up leaves one block; the override 5 yr leaves four
        assert run(FixedSSTProxyScore, 150)["n_members"] == 4
        assert "pseudo-member" in run(PaleoProxyScore, 150)["reason"]
    # without the layer the subclass falls back to the global value
    assert "pseudo-member" in run(FixedSSTProxyScore, 150)["reason"]

    # block_years is read the same way
    with use_thresholds({"fixed_sst": {"tier3": {"spinup_years": 0, "block_years": 50}}}):
        assert run(FixedSSTProxyScore, 150)["n_members"] == 3


# ---------------------------------------------------------------------------
# III.1b mid-Holocene monsoon gate, computed (not only wired)
# ---------------------------------------------------------------------------


def test_midholocene_monsoon_gate_computes_the_north_africa_anomaly() -> None:
    """The gate runs: its box crosses the Greenwich meridian (20W..30E)."""
    from types import SimpleNamespace

    from climatebench2.diags.tier3_paleo import MidHoloceneMonsoonGate

    lat = np.linspace(-85.0, 85.0, N_LAT)
    lon = np.linspace(5.0, 355.0, N_LON)
    # The box (10-30N, 20W-30E) and the cells bordering it are 0.8 mm/day
    # wetter (the gate interpolates this 10-degree field to its 2-degree grid
    # before taking the box, so the bordering cells bleed into it); everything
    # well away is much drier, so a box taken on the wrong side of the
    # meridian gives a negative number.
    near_box = ((lat >= 0.0) & (lat <= 40.0))[:, None] & ((lon >= 320.0) | (lon <= 50.0))[None, :]
    per_day = 1.0 / 86400.0  # kg m-2 s-1 per mm/day
    control = np.full((N_LAT, N_LON), 6.0 * per_day)
    wetter = control + np.where(near_box, 0.8 * per_day, -5.0 * per_day)
    data = {
        "midholocene": CubeList([_cube("pr", "kg m-2 s-1", n_years=4, field=wetter)]),
        "picontrol": CubeList([_cube("pr", "kg m-2 s-1", n_years=4, field=control)]),
    }
    gate = MidHoloceneMonsoonGate("midholocene_monsoon", harrison_dataset=None)
    output = gate._calculate_raw_output(SimpleNamespace(data=data))  # noqa: SLF001
    values = {variable.var_name: float(cube.data) for variable, cube in output.items()}
    assert values["jjas_pr_anom_mmday"] == pytest.approx(0.8, rel=1e-4)
    assert values["annual_pr_anom_mmday"] == pytest.approx(0.8, rel=1e-4)


def test_an_open_side_of_a_range_is_unbounded() -> None:
    """`range: [1.0, null]` resolves to a lower bound and no upper bound."""
    from climatebench2._thresholds import use_thresholds
    from climatebench2.diags.pass_fail import GateCheck, Threshold

    check = GateCheck(
        "toy",
        "x",
        lower=Threshold(".range", 0),
        upper=Threshold(".range", 1),
        block="toy.gate",
    )
    overlay = {"toy": {"gate": {"range": [1.0, None], "requirement": "required", "tier": "I"}}}
    with use_thresholds(overlay):
        resolved = check.resolve()
    assert resolved.lower == 1.0
    assert resolved.upper is None
