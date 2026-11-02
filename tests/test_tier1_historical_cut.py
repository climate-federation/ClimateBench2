"""Tier I's `historical` ends the year before the reserved test window.

Ratified by Duncan (2026-11-02). A submission's `historical` member is
historical + SSP2-4.5 spliced to 2100; the I.8b ITCZ-EFE climatology reads
the whole record, so without the cut it averaged in the scenario and the
held-out years (the paper's Tier I run used a historical-only farm).
"""

from __future__ import annotations

import cf_units
import numpy as np
import pytest

iris = pytest.importorskip("iris")
pytest.importorskip("climateeval")

from iris.coords import DimCoord  # noqa: E402
from iris.cube import Cube, CubeList  # noqa: E402

from climatebench2 import windows  # noqa: E402
from climatebench2._cli import SUITE_REGISTRY, end_at_year  # noqa: E402

FIRST, LAST = 2010, 2020


def _series(var_name: str = "tas") -> Cube:
    months = (LAST - FIRST + 1) * 12
    time = DimCoord(
        15.0 + 30.0 * np.arange(months),
        standard_name="time",
        units="days since 2010-01-01",
        var_name="time",
    )
    time.units = cf_units.Unit("days since 2010-01-01", calendar="360_day")
    return Cube(
        np.zeros(months, dtype=np.float32),
        var_name=var_name,
        units="K",
        dim_coords_and_dims=[(time, 0)],
    )


def _last_year(cube: Cube) -> int:
    time = cube.coord("time")
    return int(time.units.num2date(time.points[-1]).year)


def test_only_tier1_cuts_its_historical() -> None:
    flagged = {name for name, spec in SUITE_REGISTRY.items() if spec.historical_pre_test}
    assert flagged == {"ClimateBench2_TierI"}
    assert not SUITE_REGISTRY["ClimateBench2_TierII_events"].historical_pre_test


def test_end_at_year_cuts_series_and_keeps_fixed_fields() -> None:
    fx = Cube(np.ones(3, dtype=np.float32), var_name="sftlf", units="%")
    after = _series("pr")
    after.coord("time").points = after.coord("time").points + 360.0 * 20  # 2030-
    cut = end_at_year(CubeList([_series(), fx, after]), windows.pre_test_last_year())
    names = [c.var_name for c in cut]
    assert names == ["tas", "sftlf"]  # the all-future series is dropped
    assert _last_year(cut[0]) == windows.pre_test_last_year()
    # not a CubeList: passed through
    assert end_at_year(["stub"], 2014) == ["stub"]


def test_score_gives_tier1_a_pre_test_historical_and_events_the_full_one(
    tmp_path,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    import climateeval._loader as loader
    from climateeval.suites import Suite

    from climatebench2._cli import main

    monkeypatch.setattr(loader, "load_cmor_dir", lambda *_a, **_k: CubeList([_series()]))
    last: dict[str, int] = {}

    def fake_get_database(self, data, information, **_kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        cube = data["historical"].extract_cube(iris.NameConstraint(var_name="tas"))
        last[self.name] = _last_year(cube)

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
            *("--suite", "ClimateBench2_TierII_events"),
        ],
    )
    assert last["ClimateBench2_TierI"] == windows.pre_test_last_year()
    assert last["ClimateBench2_TierII_events"] == LAST
