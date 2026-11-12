"""Optional switches of the Tier II scoring pass.

Every switch tested here is read with ``get_threshold(path, default)`` and
defaults to ClimateBench2's original behaviour; the tests scope the switch
with ``use_thresholds`` and check both that it does what it says and that the
default is unchanged. Fixtures come from ``test_scoring_pass``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from climatebench2 import scoring_pass
from climatebench2._thresholds import use_thresholds
from climatebench2.scoring_pass import (
    BASELINES,
    CLIMATOLOGY_DATA_ID,
    PATTERN_SCALING_DATA_ID,
    BaselineContext,
    register_baseline,
    score_eof_output,
    score_raw_output,
    score_scalar_output,
)
from test_scoring_pass import (
    DIAGNOSTIC,
    FAST,
    _eof_tables,
    _scalar_tables,
    _tables,
)

# ---------------------------------------------------------------------------
# CB-6: the baseline registry
# ---------------------------------------------------------------------------


@pytest.fixture
def toy_baseline():  # noqa: ANN201
    """Register a toy baseline for one test, then take it out again."""
    calls: list[BaselineContext] = []

    def toy(context: BaselineContext) -> dict | None:
        calls.append(context)
        ref = context.reference.dropna()
        obs = ref["value"].to_numpy(float)
        rng = np.random.default_rng(0)
        members = obs[None, :] + rng.normal(0.0, 0.1, (4, obs.size))
        return context.score(members, obs, ref["time"])

    register_baseline("toy", toy, data_id="ToyBaseline")
    yield calls
    BASELINES.pop("toy", None)


def _rows(**kwargs) -> pd.DataFrame:  # noqa: ANN003
    raw, sources = _tables(np.random.default_rng(5))
    return pd.DataFrame(score_raw_output(raw, sources, settings=FAST, **kwargs))


def test_the_two_builtin_baselines_are_registered_in_todays_order() -> None:
    assert list(BASELINES)[:2] == ["climatology", "pattern_scaling"]
    assert BASELINES["climatology"].data_id == CLIMATOLOGY_DATA_ID
    assert BASELINES["pattern_scaling"].data_id == PATTERN_SCALING_DATA_ID
    assert [b.name for b in scoring_pass.configured_baselines("time")] == [
        "climatology",
        "pattern_scaling",
    ]
    assert [b.name for b in scoring_pass.configured_baselines("eof")] == [
        "pattern_scaling",
    ]
    assert scoring_pass.configured_baselines("scalar") == []


def test_a_registered_baseline_runs_only_when_it_is_listed(toy_baseline) -> None:  # noqa: ANN001
    assert "ToyBaseline" not in set(_rows()["data_id"])
    assert toy_baseline == []  # not even called

    with use_thresholds({"tier2": {"baselines": ["climatology", "toy"]}}):
        listed = _rows()
    toy = listed[listed["data_id"] == "ToyBaseline"].iloc[0]
    assert toy["data_type"] == "baseline"
    assert toy["var_id"] == "tas"
    assert toy["crps"] > 0
    assert toy["scorer"] == scoring_pass.scorer_tag()
    assert np.isfinite(toy["skill"])  # it gets E_ref and a skill like any row
    # ... in place of pattern scaling, which was not listed
    assert PATTERN_SCALING_DATA_ID not in set(listed["data_id"])
    assert CLIMATOLOGY_DATA_ID in set(listed["data_id"])


def test_a_baseline_context_carries_what_the_builtins_receive(toy_baseline) -> None:  # noqa: ANN001
    with use_thresholds({"tier2": {"baselines": ["toy"]}}):
        _rows(diagnostic=DIAGNOSTIC)
    context = toy_baseline[0]
    assert context.var_id == "tas"
    assert context.diagnostic == DIAGNOSTIC
    assert context.layout == "time"
    assert not context.monthly
    assert list(context.reference.columns) == ["time", "value"]
    assert len(context.reference) == 30  # the scored post-2015 steps
    assert len(context.reference_full) >= len(context.reference)
    assert context.baseline_period == (1985, 2014)
    assert context.window == (2015, None)
    assert context.settings["n_boot"] == FAST["n_boot"]
    assert context.sigma_floor == pytest.approx(0.05)  # tier2.obs_sigma tas
    with pytest.raises(AttributeError):
        context.var_id = "other"  # frozen


def test_a_baseline_returning_none_writes_no_row() -> None:
    register_baseline("silent", lambda _context: None, data_id="Silent")
    try:
        with use_thresholds({"tier2": {"baselines": ["silent"]}}):
            rows = _rows()
    finally:
        BASELINES.pop("silent")
    assert "Silent" not in set(rows["data_id"])
    # an empty list means no baseline at all
    with use_thresholds({"tier2": {"baselines": []}}):
        assert not (_rows()["data_type"] == "baseline").any()


def test_a_listed_but_unregistered_baseline_is_an_error() -> None:
    with (
        use_thresholds({"tier2": {"baselines": ["nonesuch"]}}),
        pytest.raises(ValueError, match="nonesuch"),
    ):
        _rows()


def test_registering_a_second_function_under_a_name_is_refused() -> None:
    def first(_context: BaselineContext) -> None:
        return None

    register_baseline("dup", first)
    try:
        register_baseline("dup", first)  # same function: idempotent
        with pytest.raises(ValueError, match="already registered"):
            register_baseline("dup", lambda _c: None)
        with pytest.raises(ValueError, match="layout"):
            register_baseline("bad", first, layouts=("space",))
    finally:
        BASELINES.pop("dup")
    assert "dup" not in BASELINES


def test_the_registry_row_is_normalised() -> None:
    """A bare dict is enough: the registry fills the rest and stamps the id."""

    def bare(context: BaselineContext) -> dict:
        return {"crps": 0.5, "data_id": "ignored", "var_id": context.var_id}

    register_baseline("bare", bare, data_id="Bare")
    try:
        with use_thresholds({"tier2": {"baselines": ["bare"]}}):
            rows = _rows()
    finally:
        BASELINES.pop("bare")
    row = rows[rows["data_id"] == "Bare"].iloc[0]
    assert row["data_type"] == "baseline"
    assert row["crps"] == 0.5
    assert row["scorer"] == scoring_pass.scorer_tag()
    assert np.isnan(row["crps_se"])


def test_baselines_run_on_the_eof_and_scalar_layouts() -> None:
    seen: list[str] = []

    def everywhere(context: BaselineContext) -> dict:
        seen.append(context.layout)
        obs = context.reference["value"].to_numpy(float)
        rng = np.random.default_rng(1)
        if context.layout == "scalar":
            return context.score(obs[0] + rng.normal(0.0, 0.1, 4), obs)
        return context.score(obs[None, :] + rng.normal(0.0, 0.1, (4, obs.size)), obs)

    register_baseline(
        "everywhere",
        everywhere,
        data_id="Everywhere",
        layouts=("eof", "scalar"),
    )
    try:
        with use_thresholds({"tier2": {"baselines": ["everywhere"]}}):
            raw, sources = _eof_tables(np.random.default_rng(31))
            eof = pd.DataFrame(score_eof_output(raw, sources, settings=FAST))
            raw, sources = _scalar_tables(np.random.default_rng(51))
            scalar = pd.DataFrame(
                score_scalar_output(raw, sources, diagnostic="realized_warming_level"),
            )
    finally:
        BASELINES.pop("everywhere")
    assert set(seen) == {"eof", "scalar"}
    for frame in (eof, scalar):
        row = frame[frame["data_id"] == "Everywhere"].iloc[0]
        assert row["data_type"] == "baseline"
        assert row["crps"] > 0
        assert np.isfinite(row["skill"])
    assert "Everywhere" not in set(_rows()["data_id"])  # time layout: not asked
    # the EOF layout's own default is untouched: pattern scaling's reason row
    # is gone only because the list replaced it
    assert PATTERN_SCALING_DATA_ID not in set(eof["data_id"])


def test_default_rows_are_unchanged_by_an_explicit_default_list() -> None:
    plain = _rows(diagnostic=DIAGNOSTIC)
    with use_thresholds(
        {"tier2": {"baselines": ["climatology", "pattern_scaling"]}},
    ):
        explicit = _rows(diagnostic=DIAGNOSTIC)
    pd.testing.assert_frame_equal(plain, explicit)
    assert "_steps" not in plain.columns


# ---------------------------------------------------------------------------
# CB-5: the E_ref window and the climatology mode
# ---------------------------------------------------------------------------

from climatebench2 import scoring  # noqa: E402
from test_scoring_pass import synthetic_db  # noqa: E402, F401

duckdb = pytest.importorskip("duckdb")

#: No observational draws: per-step CRPS is then independent of the step count,
#: so a windowed mean can be compared with an independently cut score.
NO_DRAWS = {"tier2": {"obs_sigma": {"tas": 0.0}}}
OPTIONAL = {"crps_ref_window", "e_ref_window", "e_ref_mode"}


def _row(rows: pd.DataFrame, name: str, data_type: str) -> pd.Series:
    return rows[(rows["data_id"] == name) & (rows["data_type"] == data_type)].iloc[0]


def _model_steps(raw: pd.DataFrame, sources: pd.DataFrame, name: str, category: str):
    """``(members, obs, years)`` of one model, as the pass stacks them."""
    ids = sorted(
        sources[(sources["name"] == name) & (sources["category"] == category)]["id"],
    )
    reference = raw[raw["data_type"] == "reference"].sort_values("time")
    members = np.vstack(
        [raw[raw["data_id"] == i].sort_values("time")["tas"].to_numpy() for i in ids],
    )
    return (
        members,
        reference["tas"].to_numpy(),
        pd.to_datetime(reference["time"]).dt.year.to_numpy(),
    )


def test_the_e_ref_window_cuts_e_ref_and_the_crps_in_skill() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    with use_thresholds(NO_DRAWS):
        full = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
        with use_thresholds({"tier2": {"e_ref": {"window": [2015, 2024]}}}):
            cut = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))

    def windowed(name: str, category: str) -> float:
        members, obs, years = _model_steps(raw, sources, name, category)
        crps_t = scoring.crps_fair(members, obs)
        return float(crps_t[(years >= 2015) & (years <= 2024)].mean())

    mine = _row(cut, "MyModel", "to_benchmark")
    # `crps` stays the full-window value; the windowed one is its own column
    assert mine["crps"] == _row(full, "MyModel", "to_benchmark")["crps"]
    assert mine["crps_ref_window"] == pytest.approx(windowed("MyModel", "model"))
    assert mine["e_ref_window"] == "2015-2024"
    expected = float(
        np.median([windowed("CMIP6_A", "CMIP6"), windowed("CMIP6_B", "CMIP6")]),
    )
    assert mine["e_ref"] == pytest.approx(expected)
    assert mine["skill"] == pytest.approx(1.0 - mine["crps_ref_window"] / expected)
    assert mine["e_ref"] != _row(full, "MyModel", "to_benchmark")["e_ref"]
    # every comparison model and baseline is windowed too
    other = _row(cut, "CMIP6_A", "other")
    assert other["crps_ref_window"] == pytest.approx(windowed("CMIP6_A", "CMIP6"))
    assert other["crps"] == _row(full, "CMIP6_A", "other")["crps"]
    clim = _row(cut, CLIMATOLOGY_DATA_ID, "baseline")
    assert clim["crps_ref_window"] != clim["crps"]
    assert (cut.loc[cut["crps"].notna(), "e_ref_window"] == "2015-2024").all()
    assert "e_ref_mode" not in cut.columns  # default mode: nothing written


def test_a_window_without_steps_gives_no_skill() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    with use_thresholds({"tier2": {"e_ref": {"window": [2100, 2110]}}}):
        rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    mine = _row(rows, "MyModel", "to_benchmark")
    assert np.isnan(mine["crps_ref_window"])
    assert np.isnan(mine["skill"])
    assert np.isfinite(mine["crps"])


def test_climatology_mode_normalises_by_the_climatology_baseline() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    with use_thresholds({"tier2": {"e_ref": {"mode": "climatology"}}}):
        rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    clim = _row(rows, CLIMATOLOGY_DATA_ID, "baseline")
    mine = _row(rows, "MyModel", "to_benchmark")
    assert mine["e_ref"] == pytest.approx(clim["crps"])
    assert mine["skill"] == pytest.approx(1.0 - mine["crps"] / clim["crps"])
    assert mine["skill"] > 0  # it tracks the warming the climatology ignores
    assert clim["skill"] == 0.0  # zero by construction
    assert (rows.loc[rows["crps"].notna(), "n_ref_models"] == 0).all()
    assert (rows.loc[rows["crps"].notna(), "e_ref_mode"] == "climatology").all()
    assert "crps_ref_window" not in rows.columns  # no window set

    # ... and no comparison model is needed
    alone = raw[raw["data_type"] != "other"]
    sources_alone = sources[sources["category"].isin(["observation", "model"])]
    with use_thresholds({"tier2": {"e_ref": {"mode": "climatology"}}}):
        rows = pd.DataFrame(score_raw_output(alone, sources_alone, settings=FAST))
    assert _row(rows, "MyModel", "to_benchmark")["skill"] == pytest.approx(
        mine["skill"],
    )
    with use_thresholds({"tier2": {"e_ref": {"mode": "bogus"}}}):
        with pytest.raises(ValueError, match="mode"):
            score_raw_output(raw, sources, settings=FAST)


def test_climatology_mode_and_a_window_combine() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    switches = {"tier2": {"e_ref": {"mode": "climatology", "window": [2015, 2021]}}}
    with use_thresholds(NO_DRAWS, switches):
        rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    clim = _row(rows, CLIMATOLOGY_DATA_ID, "baseline")
    mine = _row(rows, "MyModel", "to_benchmark")
    assert mine["e_ref"] == pytest.approx(clim["crps_ref_window"])
    assert mine["skill"] == pytest.approx(1.0 - mine["crps_ref_window"] / mine["e_ref"])
    assert mine["e_ref_window"] == "2015-2021"
    assert clim["skill"] == 0.0


def test_a_layout_without_a_time_axis_keeps_its_full_record_value() -> None:
    switches = {"tier2": {"e_ref": {"window": [2015, 2021]}}}
    raw, sources = _eof_tables(np.random.default_rng(31))
    plain = pd.DataFrame(score_eof_output(raw, sources, settings=FAST))
    with use_thresholds(switches):
        eof = pd.DataFrame(score_eof_output(raw, sources, settings=FAST))
    raw, sources = _scalar_tables(np.random.default_rng(51))
    plain_scalar = pd.DataFrame(score_scalar_output(raw, sources))
    with use_thresholds(switches):
        scalar = pd.DataFrame(score_scalar_output(raw, sources))
    for windowed, base in ((eof, plain), (scalar, plain_scalar)):
        scored = windowed[windowed["crps"].notna()]
        assert (scored["crps_ref_window"] == scored["crps"]).all()
        assert (scored["e_ref_window"] == "full").all()
        for column in ("e_ref", "skill", "crps"):
            pd.testing.assert_series_equal(windowed[column], base[column])


def test_the_default_writes_no_optional_column(synthetic_db) -> None:  # noqa: ANN001, F811
    scoring_pass.score_database(synthetic_db, settings=FAST)
    con = duckdb.connect(str(synthetic_db), read_only=True)
    try:
        columns = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'metrics'",
            ).fetchall()
        }
    finally:
        con.close()
    assert not columns & OPTIONAL
    assert set(scoring_pass._SCORE_COLUMNS) <= columns  # noqa: SLF001


def test_the_optional_columns_are_written_and_rescoring_is_idempotent(  # noqa: ANN201
    synthetic_db,  # noqa: ANN001, F811
):
    switches = {"tier2": {"e_ref": {"mode": "climatology", "window": [2015, 2021]}}}

    def read() -> pd.DataFrame:
        con = duckdb.connect(str(synthetic_db), read_only=True)
        try:
            return con.execute(
                f'SELECT * FROM "{DIAGNOSTIC}"."metrics" WHERE scorer = ?',
                [scoring_pass.scorer_tag()],
            ).df()
        finally:
            con.close()

    with use_thresholds(switches):
        scoring_pass.score_database(synthetic_db, settings=FAST)
        first = read()
        scoring_pass.score_database(synthetic_db, settings=FAST)
        second = read()
    assert OPTIONAL <= set(first.columns)
    assert len(first) == len(second)
    scored = first[first["crps"].notna()]
    assert set(scored["e_ref_mode"]) == {"climatology"}
    assert set(scored["e_ref_window"]) == {"2015-2021"}
    # a later default run rewrites the rows; the columns stay but are NULL
    scoring_pass.score_database(synthetic_db, settings=FAST)
    third = read()
    assert len(third) == len(first)
    assert third["e_ref_mode"].isna().all()
    assert third["crps_ref_window"].isna().all()
