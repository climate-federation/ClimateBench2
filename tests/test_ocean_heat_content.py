"""Ocean heat content from ``thetao``: the layer derivation and its scoring wiring.

Tier II scores OHC (full column, 0-2000 m, 0-100 m) from the ``thetao`` a
submission uploads; `climatebench2.data.layer_heat_content` turns it into the
layer's heat content on load (ESMValCore main's ``phcint`` physics, the layer
cut exactly), and `climatebench2.diags.ScoredOceanHeatContent` is ClimateEval's
OHC diagnostic with the protocol baseline as its anomaly period. All synthetic.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

pytest.importorskip("climateeval")

import dask.array as da  # noqa: E402
import iris.coords  # noqa: E402
import iris.cube  # noqa: E402
from iris.cube import CubeList  # noqa: E402

from climatebench2.data.cmip6_staged import (  # noqa: E402
    STAGED_ROOT_ENV,
    StagedCMIP6HistoricalSSP245,
    clear_discovery_cache,
    normalise_vertical_names,
)
from climatebench2.data.ocean_heat_content import (  # noqa: E402
    RHO_CP,
    depth_bounds_m,
    layer_heat_content,
    layer_thickness,
)

LEVELS = np.array([5.0, 55.0, 1550.0])
BOUNDS = np.array([[0.0, 10.0], [10.0, 100.0], [100.0, 3000.0]])


def _z(points=LEVELS, bounds=BOUNDS, units="m", **kwargs):  # noqa: ANN001, ANN003, ANN202
    kwargs.setdefault("var_name", "lev")
    kwargs.setdefault("standard_name", "depth")
    return iris.coords.DimCoord(
        np.asarray(points, float),
        units=units,
        bounds=None if bounds is None else np.asarray(bounds, float),
        **kwargs,
    )


def _thetao(  # noqa: PLR0913
    *,
    years: range = range(1985, 1987),
    z: iris.coords.DimCoord | None = None,
    nlat: int = 6,
    nlon: int = 12,
    temperature: float = 10.0,
    trend_per_year: float = 0.0,
    lazy: bool = True,
) -> iris.cube.Cube:
    """thetao (time, z, lat, lon) in degC; first latitude row is land."""
    z = _z() if z is None else z
    nt = 12 * len(years)
    step = 365.0 / 12.0
    edges = np.arange(nt + 1) * step
    time = iris.coords.DimCoord(
        edges[:-1] + step / 2,
        standard_name="time",
        var_name="time",
        units=f"days since {years[0]}-01-01",
        bounds=np.stack([edges[:-1], edges[1:]], axis=1),
    )
    lat = iris.coords.DimCoord(
        np.linspace(-75.0, 75.0, nlat),
        standard_name="latitude",
        units="degrees",
    )
    lat.guess_bounds()
    lon = iris.coords.DimCoord(
        np.linspace(15.0, 345.0, nlon),
        standard_name="longitude",
        units="degrees",
        circular=True,
    )
    lon.guess_bounds()
    values = temperature + trend_per_year * np.arange(nt) / 12.0
    data = np.ma.masked_array(
        np.broadcast_to(values[:, None, None, None], (nt, z.shape[0], nlat, nlon))
        .astype("f4")
        .copy(),
    )
    data[:, :, 0, :] = np.ma.masked  # a land row
    data[:, -1, 1, :] = np.ma.masked  # a shallow row: no deepest cell
    payload = da.from_array(data, chunks=(12, 2, nlat, nlon)) if lazy else data
    return iris.cube.Cube(
        payload,
        var_name="thetao",
        standard_name="sea_water_potential_temperature",
        units="degC",
        dim_coords_and_dims=[(time, 0), (z, 1), (lat, 2), (lon, 3)],
    )


# ---------------------------------------------------------------------------
# Vertical coordinate
# ---------------------------------------------------------------------------


def test_depth_bounds_in_metres_pass_through() -> None:
    np.testing.assert_allclose(depth_bounds_m(_z()), BOUNDS)


def test_centimetre_points_with_metre_bounds_are_reconciled() -> None:
    """CESM2: ``lev`` in cm, ``lev_bnds`` in m, which iris labels cm too."""
    z = _z(points=LEVELS * 100.0, bounds=BOUNDS, units="centimeters")
    np.testing.assert_allclose(depth_bounds_m(z), BOUNDS)


def test_centimetre_bounds_are_converted() -> None:
    z = _z(points=LEVELS * 100.0, bounds=BOUNDS * 100.0, units="cm")
    np.testing.assert_allclose(depth_bounds_m(z), BOUNDS)


def test_a_unitless_depth_axis_is_read_as_metres() -> None:
    """MIROC's ``ocean_sigma_z``: nominal depths, no unit."""
    z = _z(units="unknown", standard_name=None, long_name="ocean sigma over z coordinate")
    np.testing.assert_allclose(depth_bounds_m(z), BOUNDS)


def test_missing_bounds_are_guessed_from_the_surface() -> None:
    """AWI-CM's FESOM levels carry no bounds."""
    bounds = depth_bounds_m(_z(points=[0.0, 10.0, 30.0], bounds=None))
    np.testing.assert_allclose(bounds, [[0.0, 5.0], [5.0, 20.0], [20.0, 40.0]])


def test_a_density_axis_is_refused() -> None:
    """NorESM2's ``gn`` thetao is on isopycnal levels: no thickness to take."""
    z = _z(
        points=[1027.0, 1030.0],
        bounds=[[1026.0, 1028.0], [1028.0, 1032.0]],
        units="kg m-3",
        standard_name="sea_water_potential_density",
    )
    with pytest.raises(ValueError, match="not a depth"):
        depth_bounds_m(z)


def test_the_layer_is_cut_exactly_at_its_bounds() -> None:
    np.testing.assert_allclose(layer_thickness(BOUNDS), [10.0, 90.0, 2900.0])
    np.testing.assert_allclose(layer_thickness(BOUNDS, 0.0, 2000.0), [10.0, 90.0, 1900.0])
    np.testing.assert_allclose(layer_thickness(BOUNDS, 0.0, 100.0), [10.0, 90.0, 0.0])
    np.testing.assert_allclose(layer_thickness(BOUNDS, 0.0, 50.0), [10.0, 40.0, 0.0])


# ---------------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("z_max", "depth"), [(None, 3000.0), (2000.0, 2000.0), (100.0, 100.0)])
def test_layer_heat_content_is_esmvalcore_phcint_summed_over_the_layer(z_max, depth) -> None:  # noqa: ANN001
    cube = _thetao()
    result = layer_heat_content(cube, 0.0, z_max)
    assert result.has_lazy_data()
    assert result.shape == (cube.shape[0], 1, *cube.shape[2:])
    assert result.var_name == "phcint"
    assert str(result.units) == "J m-2"
    np.testing.assert_allclose(result.coord("depth").bounds, [[0.0, depth]])
    data = result.data
    # open ocean: thetao[K] * rho0 cp * layer depth, as ESMValCore's phcint
    np.testing.assert_allclose(data[:, 0, 2:, :], (10.0 + 273.15) * RHO_CP * depth, rtol=1e-6)
    # land stays masked; a shallow column counts its wet cells only
    assert np.ma.getmaskarray(data[:, 0, 0, :]).all()
    shallow = (10.0 + 273.15) * RHO_CP * min(depth, 100.0)
    np.testing.assert_allclose(data[:, 0, 1, :], shallow, rtol=1e-6)


def test_layer_heat_content_works_on_realised_data_too() -> None:
    eager = layer_heat_content(_thetao(lazy=False), 0.0, 2000.0).data
    lazy = layer_heat_content(_thetao(), 0.0, 2000.0).data
    np.testing.assert_array_equal(np.ma.getmaskarray(eager), np.ma.getmaskarray(lazy))
    np.testing.assert_allclose(eager, lazy)


def test_a_layer_no_level_reaches_is_an_error() -> None:
    with pytest.raises(ValueError, match="overlaps the layer"):
        layer_heat_content(_thetao(), 4000.0, 5000.0)


def test_unrecognised_vertical_names_are_normalised_for_climateeval() -> None:
    """IPSL ("Vertical T levels"/``olevel``) and MIROC (a 2nd ``depth`` coord)."""
    from climateeval import Variable
    from climateeval._utils import get_prepared_cube

    ipsl = _thetao(
        z=_z(
            var_name="olevel",
            standard_name=None,
            long_name="Vertical T levels",
            attributes={"positive": "down"},
        ),
    )
    miroc = _thetao(
        z=_z(standard_name=None, units="unknown", long_name="ocean sigma over z coordinate"),
    )
    floor = iris.coords.AuxCoord(
        np.full(miroc.shape[2:], 4000.0),
        standard_name="depth",
        var_name="depth",
        units="m",
    )
    miroc.add_aux_coord(floor, (2, 3))
    variable = Variable(
        "phcint_2000m",
        "thetao",
        "mon",
        units="J m-2",
        additional_preprocessors={
            "climatebench2.data.layer_heat_content": {"z_min": 0.0, "z_max": 2000.0},
        },
    )
    for cube in (ipsl, miroc):
        cubes = CubeList([cube])
        normalise_vertical_names(cubes, "thetao")
        prepared = get_prepared_cube(cubes, variable)
        assert prepared.var_name == "phcint"
        assert len(prepared.coords("depth")) == 1


# ---------------------------------------------------------------------------
# The diagnostic
# ---------------------------------------------------------------------------


def _prepared(years: range, z_max: float | None, trend: float = 0.01):  # noqa: ANN202
    from climateeval import Variable
    from climateeval._utils import get_prepared_cube

    variable = Variable(
        "phcint_x",
        "thetao",
        "mon",
        units="J m-2",
        timerange=f"{years[0]}0101/{years[-1]}1231",
        additional_preprocessors={
            "climatebench2.data.layer_heat_content": {"z_min": 0.0, "z_max": z_max},
        },
    )
    cube = _thetao(years=years, trend_per_year=trend)
    return get_prepared_cube(CubeList([cube]), variable), variable


@pytest.mark.parametrize("first_year", [1985, 2000])
def test_the_ohc_series_is_an_anomaly_about_the_protocol_baseline(first_year) -> None:  # noqa: ANN001
    """Anomaly about 1985-2014 (not upstream's 1979-1988), so a source that
    starts after 1988 — which the pass accepts with >= 10 baseline years —
    still gets a series instead of failing the anomaly step."""
    from climatebench2.diags import ScoredOceanHeatContent

    years = range(first_year, 2017)
    cube, variable = _prepared(years, 2000.0)
    diag = ScoredOceanHeatContent.__new__(ScoredOceanHeatContent)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = diag._preprocess(cube, variable)  # noqa: SLF001
    assert str(out.units) == "ZJ"
    time = out.coord("time")
    out_years = np.array([d.year for d in time.units.num2date(time.points)])
    np.testing.assert_array_equal(out_years, np.arange(first_year, 2017))
    values = np.asarray(out.data, dtype=float)
    assert abs(values[out_years <= 2014].mean()) < 1e-6
    assert np.polyfit(out_years, values, 1)[0] > 0.0


def test_the_full_column_and_layers_scale_with_their_thickness() -> None:
    from climatebench2.diags import ScoredOceanHeatContent

    diag = ScoredOceanHeatContent.__new__(ScoredOceanHeatContent)
    trends = {}
    for z_max in (None, 2000.0, 100.0):
        cube, variable = _prepared(range(1985, 2017), z_max)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = diag._preprocess(cube, variable)  # noqa: SLF001
        trends[z_max] = np.polyfit(np.arange(out.shape[0]), np.asarray(out.data, float), 1)[0]
    # uniform warming: uptake proportional to each column's wet depth in the
    # layer — 3000 m in the open ocean, 100 m in the shallow row — so the
    # ratios sit between the open-ocean ratio and 1
    assert trends[100.0] < trends[2000.0] < trends[None]
    assert 2000.0 / 3000.0 < trends[2000.0] / trends[None] < 1.0
    assert 100.0 / 3000.0 < trends[100.0] / trends[None] < 100.0 / 2000.0


def test_the_anomaly_period_is_the_protocol_baseline() -> None:
    from climatebench2._thresholds import get_threshold
    from climatebench2.diags import ScoredOceanHeatContent

    start, end = get_threshold("tier2.climatology_baseline_period")
    reference = ScoredOceanHeatContent.__new__(ScoredOceanHeatContent)._anomaly_reference  # noqa: SLF001
    assert (reference["start_year"], reference["end_year"]) == (start, end)


# ---------------------------------------------------------------------------
# The comparison ensemble
# ---------------------------------------------------------------------------


def test_a_staged_member_is_read_from_its_own_root_not_the_data_root(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The v2 references live apart from the staged ensemble: a member must
    load from the root it was discovered in. With an IPSL-style ``olevel``
    axis, which ClimateEval alone would reject."""
    from climateeval import Variable

    staged = tmp_path / "staged"
    refs = tmp_path / "refs"
    refs.mkdir()
    source = "CMIP6_IPSL-CM6A-LR_historical-ssp245_r1i1p1f1"
    leaf = staged / source / "mon" / "thetao"
    leaf.mkdir(parents=True)
    cube = _thetao(
        lazy=False,
        z=_z(
            var_name="olevel",
            standard_name=None,
            long_name="Vertical T levels",
            attributes={"positive": "down"},
        ),
    )
    iris.save(cube, leaf / "thetao_Omon_IPSL-CM6A-LR_historical_r1i1p1f1_gn_198501-198612.nc")
    monkeypatch.setenv(STAGED_ROOT_ENV, str(staged))
    clear_discovery_cache()
    try:
        variable = Variable(
            "phcint_100m",
            "thetao",
            "mon",
            units="J m-2",
            additional_preprocessors={
                "climatebench2.data.layer_heat_content": {"z_min": 0.0, "z_max": 100.0},
            },
        )
        (member,) = StagedCMIP6HistoricalSSP245().generate(variable)
        assert member.id == source
        result = member.get_cube(refs, variable, download_missing_data=False)
        np.testing.assert_allclose(
            np.asarray(result.data)[:, 0, 2:, :],
            (10.0 + 273.15) * RHO_CP * 100.0,
            rtol=1e-6,
        )
    finally:
        clear_discovery_cache()


def test_the_ohc_entry_scores_thetao_with_the_comparison_ensemble() -> None:
    """E_ref needs the CMIP6 ensemble on the entry; IAP is the reference
    (paper-named, ends 2024), EN4 the second product behind sigma_obs."""
    from climateeval.suites import Suite

    from climatebench2._cli import _resolve_suite
    from climatebench2.diags import ScoredOceanHeatContent

    ohc = Suite(_resolve_suite("ClimateBench2_TierII"))._get_diagnostics()["ohc"]  # noqa: SLF001
    assert isinstance(ohc, ScoredOceanHeatContent)
    layers = {}
    for variable in ohc._variables:  # noqa: SLF001
        assert variable.var_name == "thetao"
        assert variable.units == "J m-2"
        (kwargs,) = variable.additional_preprocessors.values()
        layers[variable.id] = (kwargs["z_min"], kwargs["z_max"])
        assert ohc._reference_data[variable].id == "reanalysis_IAP"  # noqa: SLF001
    assert layers == {
        "phcint_total": (0.0, None),
        "phcint_2000m": (0.0, 2000.0),
        "phcint_100m": (0.0, 100.0),
    }
    import yaml

    with open(_resolve_suite("ClimateBench2_TierII"), encoding="utf-8") as stream:
        (entry,) = [e for e in yaml.safe_load(stream) if e["name"] == "ohc"]
    for variable in entry["variables"]:
        assert variable["other_data"] == [
            "climateeval.data.EN4",
            "climatebench2.data.StagedCMIP6HistoricalSSP245",
        ]
