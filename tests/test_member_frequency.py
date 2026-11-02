"""A member directory that holds one variable at several frequencies.

``deploy/README.md`` documents the CMIP-style submission layout
``<member>/<table>/<var>/<grid>/<version>/*.nc``, so a member that wrote both
``Amon/pr`` and ``day/pr`` is normal. ClimateEval's ``load_cmor_dir`` returns
one cube per table and ``get_prepared_cube`` then refuses ``pr`` outright
("Got 2 cubes ... expecting 1"): the first full-suite cloud run lost the
submission's ``pr`` from every Tier II entry, the daily suite and Tier I
I.3c/I.8b that way (2026-10-30). ``climatebench2.data.frequency`` narrows each
ambiguous variable to the frequency its consumer names.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

iris = pytest.importorskip("iris")
pytest.importorskip("climateeval")

from climatebench2.data import frequency  # noqa: E402
from iris.coords import DimCoord  # noqa: E402
from iris.cube import Cube, CubeList  # noqa: E402

from climateeval import Variable  # noqa: E402
from climateeval._loader import load_cmor_dir  # noqa: E402
from climateeval._utils import get_prepared_cube  # noqa: E402

N_MONTHS = 24
N_DAYS = 60


def _cube(
    var_name: str,
    times: np.ndarray,
    table: str | None,
    *,
    units: str = "kg m-2 s-1",
) -> Cube:
    lat = DimCoord(
        np.array([-45.0, 45.0]),
        standard_name="latitude",
        units="degrees",
        var_name="lat",
    )
    lon = DimCoord(
        np.array([90.0, 270.0]),
        standard_name="longitude",
        units="degrees",
        var_name="lon",
    )
    lat.guess_bounds()
    lon.guess_bounds()
    time = DimCoord(
        times.astype(float),
        standard_name="time",
        units="days since 2000-01-01",
        var_name="time",
    )
    data = np.full((times.size, 2, 2), 1e-5, dtype=np.float32)
    cube = Cube(
        data,
        var_name=var_name,
        standard_name="precipitation_flux" if var_name == "pr" else None,
        units=units,
        dim_coords_and_dims=[(time, 0), (lat, 1), (lon, 2)],
    )
    if table is not None:
        cube.attributes["frequency"] = {"Amon": "mon", "day": "day"}[table]
        cube.attributes["table_id"] = table
    return cube


def _monthly(var_name: str = "pr", table: str | None = "Amon") -> Cube:
    return _cube(var_name, 15.0 + 30.4375 * np.arange(N_MONTHS), table)


def _daily(var_name: str = "pr", table: str | None = "day") -> Cube:
    return _cube(var_name, 0.5 + np.arange(N_DAYS), table)


def _member_dir(root: Path) -> Path:
    """``<member>/{Amon,day}/pr`` plus a monthly-only ``tas``, CMIP DRS style."""
    member = root / "historical-ssp245" / "r1i1p1f1"
    for table, cube, stamp in (
        ("Amon", _monthly(), "200001-200112"),
        ("day", _daily(), "20000101-20000229"),
    ):
        leaf = member / table / "pr" / "gn" / "v20190710"
        leaf.mkdir(parents=True)
        iris.save(cube, leaf / f"pr_{table}_M_historical_r1i1p1f1_gn_{stamp}.nc")
    leaf = member / "Amon" / "tas" / "gn" / "v20190710"
    leaf.mkdir(parents=True)
    tas = _monthly("tas")
    tas.units = "K"
    iris.save(tas, leaf / "tas_Amon_M_historical_r1i1p1f1_gn_200001-200112.nc")
    return member


def _entry(var_name: str, freq: str) -> dict:
    variable = {"id": var_name, "var_name": var_name, "frequency": freq}
    return {"name": "e", "variables": [variable]}


def test_a_cmip_member_dir_loads_pr_at_two_frequencies(tmp_path) -> None:  # noqa: ANN001
    """The defect: ClimateEval alone cannot pick one."""
    cubes = load_cmor_dir(_member_dir(tmp_path))
    assert len(cubes.extract(iris.NameConstraint(var_name="pr"))) == 2  # noqa: PLR2004
    assert frequency.ambiguous_variables(cubes) == {"pr": {"mon", "day"}}
    with pytest.raises(Exception, match="2 cubes"):
        get_prepared_cube(cubes, Variable("pr", "pr", "mon"))


@pytest.mark.parametrize(("freq", "n_time"), [("mon", N_MONTHS), ("day", N_DAYS)])
def test_each_entry_reads_the_frequency_its_variables_name(
    tmp_path,  # noqa: ANN001
    freq: str,
    n_time: int,
) -> None:
    cubes = load_cmor_dir(_member_dir(tmp_path))
    selected = frequency.select_for_entry(cubes, _entry("pr", freq))
    cube = get_prepared_cube(selected, Variable("pr", "pr", freq))
    assert cube.coord("time").shape == (n_time,)
    # an unambiguous variable is untouched, whatever the entry asks for
    assert selected.extract_cube(iris.NameConstraint(var_name="tas")) is not None


def test_experiments_read_monthly_except_the_daily_key(tmp_path) -> None:  # noqa: ANN001
    """Tier I `historical` (I.3c, I.8b) is monthly; the `day` experiment daily."""
    cubes = load_cmor_dir(_member_dir(tmp_path))
    assert frequency.experiment_frequency("historical") == "mon"
    assert frequency.experiment_frequency("day") == "day"
    historical = frequency.select_frequency(
        cubes,
        frequency.experiment_frequency("historical"),
    )
    pr = historical.extract_cube(iris.NameConstraint(var_name="pr"))
    assert pr.coord("time").shape == (N_MONTHS,)


def test_frequency_is_read_off_the_time_step_without_an_attribute() -> None:
    cubes = CubeList([_monthly(table=None), _daily(table=None)])
    assert frequency.ambiguous_variables(cubes) == {"pr": {"mon", "day"}}
    (daily,) = frequency.select_frequency(cubes, "day")
    assert daily.coord("time").shape == (N_DAYS,)


def test_nothing_changes_without_an_ambiguity() -> None:
    """A paper-style farm (one table per variable) loads exactly as before."""
    cubes = CubeList([_monthly(), _monthly("tas")])
    assert frequency.select_frequency(cubes, "day") is cubes
    assert frequency.select_for_entry(cubes, _entry("pr", "day")) is cubes
    # Not a CubeList (an experiment dict, a test stub): passed through
    data = {"historical": cubes}
    assert frequency.select_for_entry(data, _entry("pr", "mon")) is data


def test_no_cube_at_the_wanted_frequency_means_missing_not_the_wrong_one() -> None:
    """A 1hr entry must not silently read daily (or monthly) pr."""
    cubes = CubeList([_monthly(), _daily(), _monthly("tas")])
    selected = frequency.select_for_entry(cubes, _entry("pr", "1hr"))
    assert not selected.extract(iris.NameConstraint(var_name="pr"))
    assert selected.extract(iris.NameConstraint(var_name="tas"))


def test_point_and_mean_sampling_count_as_one_frequency() -> None:
    assert frequency.normalise_frequency("3hrPt") == "3hr"
    assert frequency.normalise_frequency("monC") == "monC"


def test_run_members_gives_each_entry_its_own_frequency(monkeypatch) -> None:  # noqa: ANN001
    """The CLI's hook: entry by entry, each sees only its frequency of `pr`."""
    from climatebench2.shared_sources import EntrySuite, run_members

    from climateeval.data import DataSourceInformation
    from climateeval.suites import Suite

    seen: list[tuple[str, int]] = []

    def fake_get_database(self, data, information, **_kwargs) -> None:  # noqa: ANN001, ARG001
        (entry,) = self._suite_definition
        pr = data.extract_cube(iris.NameConstraint(var_name="pr"))
        seen.append((entry["name"], pr.coord("time").shape[0]))

    monkeypatch.setattr(Suite, "get_database", fake_get_database)
    suite = Suite.__new__(Suite)
    suite._suite_definition = [  # noqa: SLF001
        {**_entry("pr", "mon"), "name": "monthly"},
        {**_entry("pr", "day"), "name": "daily"},
    ]
    monkeypatch.setattr(
        EntrySuite,
        "__init__",
        lambda self, parent, entry, *, share=True: (  # noqa: ARG005
            setattr(self, "_suite_definition", [entry])
            or setattr(self, "_shared", None)
            or setattr(self, "shared", False)
        ),
    )
    cubes = CubeList([_monthly(), _daily()])
    info = DataSourceInformation(name="M", category="model", variant="r1i1p1f1")
    run_members(
        suite,
        [(cubes, info)],
        database_resource="duckdb:///unused.ddb",
        share=False,
        select_data=frequency.select_for_entry,
    )
    assert seen == [("monthly", N_MONTHS), ("daily", N_DAYS)]


def test_score_feeds_each_suite_the_frequency_it_reads(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """End to end through `climatebench2 score`, with the suites stubbed.

    Every member/experiment directory loads as monthly AND daily `pr`. Tier
    I's `historical` must see the monthly cube, each Tier II entry its own
    YAML frequency (monthly for the main suite, daily for the extremes) —
    and an hourly entry no `pr` at all rather than the daily one.
    """
    from climatebench2._cli import main

    import climateeval._loader as loader
    from climateeval.suites import Suite

    monkeypatch.setattr(
        loader,
        "load_cmor_dir",
        lambda *_a, **_k: CubeList([_monthly(), _daily()]),
    )
    seen: dict[tuple[str, str], set[str]] = {}

    def fake_get_database(self, data, information, **_kwargs) -> None:  # noqa: ANN001, ARG001
        entries = [str(e.get("name")) for e in self._suite_definition]
        cubes = data["historical"] if isinstance(data, dict) else data
        found = frequency.frequencies_by_variable(
            cubes.extract(iris.NameConstraint(var_name="pr")),
        ).get("pr", set())
        for entry in entries:
            seen[self.name, entry] = found

    monkeypatch.setattr(Suite, "get_database", fake_get_database)
    model = tmp_path / "M"
    for variant in ("r1i1p1f1", "r2i1p1f1"):
        (model / "historical-ssp245" / variant).mkdir(parents=True)
    main(
        [
            "score",
            str(model),
            "--name",
            "M",
            "--no-score",
            "--out",
            str(tmp_path / "out"),
            *("--suite", "ClimateBench2_TierI"),
            *("--suite", "ClimateBench2_TierII"),
            *("--suite", "ClimateBench2_TierII_daily"),
        ],
    )
    assert seen["ClimateBench2_TierI", "itcz_efe"] == {"mon"}
    assert seen["ClimateBench2_TierI", "precip_buoyancy"] == {"mon"}
    assert seen["ClimateBench2_TierII", "annual_mean_timeseries"] == {"mon"}
    assert seen["ClimateBench2_TierII_daily", "pr_extremes_series"] == {"day"}
    assert seen["ClimateBench2_TierII_daily", "extremes"] == {"day"}
    assert seen["ClimateBench2_TierII_daily", "pr_diurnal_cycle"] == set()


def test_time_step_inference_works_on_any_calendar() -> None:
    """A 360-day model calendar must not break the step reading (it did)."""
    cube = _cube("pr", 0.5 + np.arange(N_DAYS), None)
    cube.coord("time").units = "hours since 1980-01-01"
    cube.coord("time").points = 12.0 + 24.0 * np.arange(N_DAYS)
    import cf_units

    cube.coord("time").units = cf_units.Unit("hours since 1980-01-01", calendar="360_day")
    assert frequency.cube_frequency(cube) == "day"
