"""Tests for the staged CMIP6 comparison ensemble.

`climatebench2.data.StagedCMIP6HistoricalSSP245` is what makes Tier II's
``E_ref`` exist: the median fair CRPS over the comparison models. The contract
that matters is that its data source **ids are identical to upstream's**
(`climateeval.data.CMIP6HistoricalSSP245`), because the scoring pass groups
members into one model by the ``name`` behind that id.
"""

from __future__ import annotations

import pytest

from climatebench2._thresholds import get_threshold
from climatebench2.data import (
    STAGED_ROOT_ENV,
    StagedCMIP6HistoricalSSP245,
    staged_root,
)
from climatebench2.data.cmip6_staged import clear_discovery_cache, select_members

pytest.importorskip("climateeval")

MODELS = {
    "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r1i1p1f1": {
        "tas": ["tas_Amon_MPI-ESM1-2-LR_historical_r1i1p1f1_gn_185001-186912.nc"],
        "rsdt": ["rsdt_Amon_MPI-ESM1-2-LR_ssp245_r1i1p1f1_gn_201501-203412.nc"],
        "rsut": ["rsut_Amon_MPI-ESM1-2-LR_ssp245_r1i1p1f1_gn_201501-203412.nc"],
        "rlut": ["rlut_Amon_MPI-ESM1-2-LR_ssp245_r1i1p1f1_gn_201501-203412.nc"],
    },
    "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r2i1p1f1": {
        "tas": ["tas_Amon_MPI-ESM1-2-LR_historical_r2i1p1f1_gn_185001-186912.nc"],
    },
    # A non-f1 forcing index: upstream's `ensemble: r*i1p1f1` facet would miss
    # it, which is half the reason the staged generator exists.
    "CMIP6_UKESM1-0-LL_historical-ssp245_r1i1p1f2": {
        "tas": ["tas_Amon_UKESM1-0-LL_historical_r1i1p1f2_gn_185001-194912.nc"],
    },
    # Not part of this ensemble: a single-experiment directory.
    "CMIP6_MPI-ESM1-2-LR_historical_r1i1p1f1": {"tas": ["tas.nc"]},
    "observation_HadCRUT5": {"tas": ["obs.nc"]},
}


@pytest.fixture
def staged(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A staged comparison root with a handful of members."""
    for source_id, variables in MODELS.items():
        for var_name, files in variables.items():
            var_dir = tmp_path / source_id / "mon" / var_name
            var_dir.mkdir(parents=True)
            for name in files:
                (var_dir / name).touch()
    monkeypatch.setenv(STAGED_ROOT_ENV, str(tmp_path))
    clear_discovery_cache()
    yield tmp_path
    clear_discovery_cache()


def _variable(var_name: str):  # noqa: ANN202
    from climateeval import Variable

    return Variable(var_name, var_name, "mon")


def test_ids_match_the_upstream_generator(staged) -> None:  # noqa: ANN001, ARG001
    sources = list(StagedCMIP6HistoricalSSP245().generate(_variable("tas")))
    assert [s.id for s in sources] == [
        "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r1i1p1f1",
        "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r2i1p1f1",
        "CMIP6_UKESM1-0-LL_historical-ssp245_r1i1p1f2",
    ]
    # The scoring pass groups members of one model by `name`.
    assert [s.name for s in sources] == [
        "MPI-ESM1-2-LR",
        "MPI-ESM1-2-LR",
        "UKESM1-0-LL",
    ]
    assert {s.information.exp for s in sources} == {"historical-ssp245"}
    assert {s.information.category for s in sources} == {"CMIP6"}


def test_facets_are_filled_in_from_the_cmor_filenames(staged) -> None:  # noqa: ANN001, ARG001
    source = next(iter(StagedCMIP6HistoricalSSP245().generate(_variable("tas"))))
    facets = source.esmvaltool_dataset_facets
    assert facets["project"] == "CMIP6"
    assert facets["exp"] == ["historical", "ssp245"]
    assert facets["dataset"] == "MPI-ESM1-2-LR"
    assert facets["ensemble"] == "r1i1p1f1"
    assert facets["short_name"] == "tas"
    assert facets["frequency"] == "mon"
    assert (facets["mip"], facets["grid"]) == ("Amon", "gn")


def test_a_variable_a_member_does_not_carry_is_skipped(staged) -> None:  # noqa: ANN001, ARG001
    sources = list(StagedCMIP6HistoricalSSP245().generate(_variable("tos")))
    assert sources == []


def test_a_derived_variable_needs_every_required_input(staged) -> None:  # noqa: ANN001, ARG001
    """`rtnt` is never staged: it is derived from rsdt/rsut/rlut."""
    sources = list(StagedCMIP6HistoricalSSP245().generate(_variable("rtnt")))
    assert [s.id for s in sources] == [
        "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r1i1p1f1",
    ]


def test_no_staged_root_yields_nothing(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv(STAGED_ROOT_ENV, str(tmp_path / "does-not-exist"))
    clear_discovery_cache()
    assert staged_root() is None
    assert list(StagedCMIP6HistoricalSSP245().generate(_variable("tas"))) == []


def test_the_environment_wins_over_an_explicit_root(staged, tmp_path) -> None:  # noqa: ANN001
    """A smoke run points discovery at a subset while loading from --data-root."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert staged_root(other) == staged


def test_the_generator_enumerates_daily_directories_too(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """`frequency: day` needs no special case: the walk uses the variable's own.

    The daily suite's held-out Rx1day/Rx5day series takes its `E_ref` from
    the same comparison ensemble as every monthly entry, so the generator
    has to find `<member>/day/pr/` — a member staged only at `mon` must not
    be yielded for a `day` variable, and vice versa.
    """
    from climateeval import Variable

    layout = {
        "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r1i1p1f1": {
            "day": ["pr_day_MPI-ESM1-2-LR_historical_r1i1p1f1_gn_19790101-19791231.nc"],
            "mon": ["pr_Amon_MPI-ESM1-2-LR_historical_r1i1p1f1_gn_185001-186912.nc"],
        },
        # Monthly only: present for the `mon` ensemble, absent from the daily one
        "CMIP6_UKESM1-0-LL_historical-ssp245_r1i1p1f2": {
            "mon": ["pr_Amon_UKESM1-0-LL_historical_r1i1p1f2_gn_185001-194912.nc"],
        },
    }
    for source_id, by_frequency in layout.items():
        for frequency, files in by_frequency.items():
            var_dir = tmp_path / source_id / frequency / "pr"
            var_dir.mkdir(parents=True)
            for name in files:
                (var_dir / name).touch()
    monkeypatch.setenv(STAGED_ROOT_ENV, str(tmp_path))
    clear_discovery_cache()

    daily = list(StagedCMIP6HistoricalSSP245().generate(Variable("pr", "pr", "day")))
    assert [s.id for s in daily] == [
        "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r1i1p1f1",
    ]
    # ... and the facets carry the daily table, not Amon
    assert daily[0].esmvaltool_dataset_facets["frequency"] == "day"
    assert daily[0].esmvaltool_dataset_facets["mip"] == "day"

    monthly = list(StagedCMIP6HistoricalSSP245().generate(Variable("pr", "pr", "mon")))
    assert len(monthly) == 2  # noqa: PLR2004
    clear_discovery_cache()


#: CMOR standard names of the TOA fluxes the CRE derivations extract by name.
_FLUX_STANDARD_NAMES = {
    "rsut": "toa_outgoing_shortwave_flux",
    "rsutcs": "toa_outgoing_shortwave_flux_assuming_clear_sky",
    "rlut": "toa_outgoing_longwave_flux",
    "rlutcs": "toa_outgoing_longwave_flux_assuming_clear_sky",
}


def _write_flux(path, var_name: str, value: float) -> None:  # noqa: ANN001
    """A tiny CMOR-like monthly TOA-flux file (2015-01 .. 2016-12)."""
    import numpy as np

    netCDF4 = pytest.importorskip("netCDF4")  # noqa: N806

    path.parent.mkdir(parents=True, exist_ok=True)
    lats = np.array([-45.0, 45.0])
    lons = np.array([90.0, 270.0])
    days = [(year - 2015) * 365.0 + month * 30.4 + 15.0
            for year in (2015, 2016) for month in range(12)]
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("time", len(days))
        ds.createDimension("lat", lats.size)
        ds.createDimension("lon", lons.size)
        time = ds.createVariable("time", "f8", ("time",))
        time.units = "days since 2015-01-01"
        time.calendar = "standard"
        time.standard_name = "time"
        time[:] = days
        lat = ds.createVariable("lat", "f8", ("lat",))
        lat.units = "degrees_north"
        lat.standard_name = "latitude"
        lat[:] = lats
        lon = ds.createVariable("lon", "f8", ("lon",))
        lon.units = "degrees_east"
        lon.standard_name = "longitude"
        lon[:] = lons
        var = ds.createVariable(var_name, "f4", ("time", "lat", "lon"))
        var.units = "W m-2"
        var.standard_name = _FLUX_STANDARD_NAMES[var_name]
        var.positive = "up"
        var[:] = np.full((len(days), lats.size, lons.size), value, dtype="f4")


def test_the_cre_variables_need_both_all_sky_and_clear_sky(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """swcre = rsutcs - rsut, lwcre = rlutcs - rlut: a member needs both fluxes.

    Same rule as `rtnt` (dc1efc3): the CREs are never staged under their own
    names, so the generator walks the ESMValCore derivation inputs, and a
    member missing the clear-sky flux is NOT yielded for that CRE. The
    yielded member then really derives the CRE from its staged fluxes.
    """
    from climateeval import Variable

    full = "CMIP6_MPI-ESM1-2-LR_historical-ssp245_r1i1p1f1"
    no_lw_clear = "CMIP6_UKESM1-0-LL_historical-ssp245_r1i1p1f2"
    values = {"rsut": 100.0, "rsutcs": 55.0, "rlut": 240.0, "rlutcs": 268.0}
    for source_id, var_names in (
        (full, ("rsut", "rsutcs", "rlut", "rlutcs")),
        (no_lw_clear, ("rsut", "rsutcs", "rlut")),
    ):
        model, member = source_id.split("_")[1], source_id.split("_")[-1]
        for var_name in var_names:
            _write_flux(
                tmp_path / source_id / "mon" / var_name
                / f"{var_name}_Amon_{model}_ssp245_{member}_gn_201501-201612.nc",
                var_name,
                values[var_name],
            )
    monkeypatch.setenv(STAGED_ROOT_ENV, str(tmp_path))
    clear_discovery_cache()
    try:
        swcre = list(
            StagedCMIP6HistoricalSSP245().generate(Variable("swcre", "swcre", "mon")),
        )
        lwcre = list(
            StagedCMIP6HistoricalSSP245().generate(Variable("lwcre", "lwcre", "mon")),
        )
        assert [s.id for s in swcre] == [full, no_lw_clear]
        assert [s.id for s in lwcre] == [full]

        # ... and the member's own fluxes really give the CRE.
        import numpy as np

        cube = swcre[0].get_cube(
            tmp_path,
            Variable("swcre", "swcre", "mon"),
            download_missing_data=False,
        )
        assert float(np.asarray(cube.data).mean()) == pytest.approx(55.0 - 100.0)
        cube = lwcre[0].get_cube(
            tmp_path,
            Variable("lwcre", "lwcre", "mon"),
            download_missing_data=False,
        )
        assert float(np.asarray(cube.data).mean()) == pytest.approx(268.0 - 240.0)
    finally:
        clear_discovery_cache()


def test_the_ceres_reference_derives_the_cre_from_its_staged_fluxes(tmp_path) -> None:  # noqa: ANN001
    """The reference side of the same derivation: nothing named swcre is staged."""
    import numpy as np
    from climateeval import Variable
    from climateeval.data import CERESEBAF

    values = {"rsut": 99.0, "rsutcs": 52.0, "rlut": 239.0, "rlutcs": 265.0}
    for var_name, value in values.items():
        _write_flux(
            tmp_path / "observation_CERES-EBAF" / "mon" / var_name / f"{var_name}.nc",
            var_name,
            value,
        )
    for var_name, expected in (("swcre", 52.0 - 99.0), ("lwcre", 265.0 - 239.0)):
        cube = CERESEBAF().get_cube(
            tmp_path,
            Variable(var_name, var_name, "mon"),
            download_missing_data=False,
        )
        assert float(np.asarray(cube.data).mean()) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# The member policy: r*i1p1f*, first 10 by realization number, per model
# ---------------------------------------------------------------------------


def test_the_member_policy_lives_in_thresholds() -> None:
    """Paper Fig. 4 caption: "its r*i1p1f* members, capped at the first 10"."""
    assert get_threshold("tier2.comparison_ensemble.member_pattern") == "r*i1p1f*"
    assert get_threshold("tier2.comparison_ensemble.max_members_per_model") == 10


def test_select_members_orders_by_realization_number_not_by_name() -> None:
    """``r2`` precedes ``r10``; a string sort would put ``r10`` second."""
    members = ["r10i1p1f1", "r2i1p1f1", "r1i1p1f1", "r3i1p1f1"]
    assert select_members(members) == ["r1i1p1f1", "r2i1p1f1", "r3i1p1f1", "r10i1p1f1"]


def test_select_members_takes_any_forcing_index_but_only_i1p1() -> None:
    members = [
        "r1i1p1f2",  # UKESM1-0-LL / CNRM-*: f2 is in the ensemble
        "r2i1p1f3",
        "r1i2p1f1",  # another initialisation method: out
        "r1i1p2f1",  # another physics version (CanESM5 p2): out
        "r3i1p1f1",
        "not-a-variant",
    ]
    assert select_members(members) == ["r1i1p1f2", "r2i1p1f3", "r3i1p1f1"]


def test_select_members_caps_at_ten_after_ordering() -> None:
    """The cap keeps the FIRST ten realizations, not ten arbitrary ones."""
    members = [f"r{r}i1p1f1" for r in range(25, 0, -1)] + [
        f"r{r}i1p2f1" for r in range(1, 26)
    ]
    assert select_members(members) == [f"r{r}i1p1f1" for r in range(1, 11)]


def test_select_members_mirrors_the_staging_script_on_ties() -> None:
    """Same realization, two forcing indices: two members, ordered by name."""
    assert select_members(["r1i1p1f2", "r1i1p1f1", "r2i1p1f1"], cap=2) == [
        "r1i1p1f1",
        "r1i1p1f2",
    ]


def test_select_members_takes_an_explicit_pattern() -> None:
    members = ["r1i1p1f1", "r1i1p1f2", "r2i1p1f1"]
    assert select_members(members, pattern="r*i1p1f1") == ["r1i1p1f1", "r2i1p1f1"]
    with pytest.raises(ValueError, match="member pattern"):
        select_members(members, pattern="r?i1p1f1")


def _stage(root, source_id: str, var_names: list[str]) -> None:  # noqa: ANN001
    for var_name in var_names:
        var_dir = root / source_id / "mon" / var_name
        var_dir.mkdir(parents=True)
        (var_dir / f"{var_name}.nc").touch()


def test_the_generator_applies_the_policy_before_variable_availability(
    tmp_path,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    """A model's first ten members are fixed; a variable may have fewer.

    Twelve qualifying members of one model are staged, plus an ``i2`` and a
    ``p2`` member. ``tas`` is on all of them; ``siconc`` is missing from
    ``r3`` and present on ``r11`` and ``r12``. The ensemble is r1..r10 for
    both variables, so ``siconc`` gets nine members — not r11 in r3's place,
    which a per-variable cap would give (the staging script that built the
    Levante root behaves the same way).
    """
    model = "CanESM5"
    for r in range(1, 13):
        variables = ["tas"] if r == 3 else ["tas", "siconc"]
        _stage(tmp_path, f"CMIP6_{model}_historical-ssp245_r{r}i1p1f1", variables)
    _stage(tmp_path, f"CMIP6_{model}_historical-ssp245_r1i2p1f1", ["tas", "siconc"])
    _stage(tmp_path, f"CMIP6_{model}_historical-ssp245_r1i1p2f1", ["tas", "siconc"])
    monkeypatch.setenv(STAGED_ROOT_ENV, str(tmp_path))
    clear_discovery_cache()
    try:
        def members(var_name: str) -> set[str]:
            sources = StagedCMIP6HistoricalSSP245().generate(_variable(var_name))
            return {s.information.variant for s in sources}

        first_ten = {f"r{r}i1p1f1" for r in range(1, 11)}
        assert members("tas") == first_ten
        assert members("siconc") == first_ten - {"r3i1p1f1"}
    finally:
        clear_discovery_cache()
