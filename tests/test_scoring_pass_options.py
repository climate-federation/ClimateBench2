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


# ---------------------------------------------------------------------------
# CB-8: the configured scored window
# ---------------------------------------------------------------------------

from climatebench2 import windows  # noqa: E402
from test_scoring_pass import _offset_tables  # noqa: E402

HINDCAST = "hindcast_entry"
HINDCAST_YEARS = list(range(1900, 1991))
HINDCAST_SWITCHES = {
    "tier2": {
        "scored_windows": {HINDCAST: [1940, 1978]},
        "anomaly_baseline": {
            "diagnostics": {HINDCAST: True},
            "periods": {HINDCAST: [1900, 1930]},
        },
        "window_labels": {"diagnostics": {HINDCAST: "in-sample"}},
    },
}


def test_the_default_scored_window_is_the_test_window() -> None:
    assert windows.scored_window() == (2015, None)
    assert windows.scored_window("anything") == (2015, None)
    assert not windows.has_custom_scored_window(DIAGNOSTIC)
    assert windows.anomaly_baseline_years(DIAGNOSTIC) == (1985, 2014)
    first, last = windows.test_window_years()
    assert first == 2015
    assert last >= 2015


def test_scored_window_key_sets_the_test_window_and_the_pass() -> None:
    with use_thresholds({"tier2": {"scored_window": [2015, 2024]}}):
        assert windows.test_window_years() == (2015, 2024)
        assert windows.test_window_timerange() == "20150101/20241231"
        assert windows.scored_window(DIAGNOSTIC) == (2015, 2024)
        assert windows.window_lengths()["test"] == 10
        raw, sources = _tables(np.random.default_rng(5))
        rows = pd.DataFrame(
            score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
        )
    assert _row(rows, "MyModel", "to_benchmark")["n_time"] == 10
    assert _row(rows, CLIMATOLOGY_DATA_ID, "baseline")["n_time"] == 10
    with (
        use_thresholds({"tier2": {"scored_window": [2024, 2015]}}),
        pytest.raises(ValueError, match="scored_window"),
    ):
        windows.test_window_years()


def test_one_entry_has_its_own_window_and_baseline() -> None:
    raw, sources = _offset_tables(0.5, 3, years=HINDCAST_YEARS)
    with use_thresholds(HINDCAST_SWITCHES):
        assert windows.scored_window(HINDCAST) == (1940, 1978)
        assert windows.scored_window(DIAGNOSTIC) == (2015, None)
        assert windows.anomaly_baseline_years(HINDCAST) == (1900, 1930)
        assert windows.anomaly_baseline_years(DIAGNOSTIC) == (1985, 2014)
        context = scoring_pass.anomalise_raw_output(
            raw,
            None,
            ["tas"],
            diagnostic=HINDCAST,
        )
        rows = pd.DataFrame(
            score_raw_output(raw, sources, settings=FAST, diagnostic=HINDCAST),
        )
    assert context.period == (1900, 1930)
    assert context.window == (1940, 1978)
    years = pd.to_datetime(context.raw["time"]).dt.year
    assert (years.min(), years.max()) == (1940, 1978)
    # anomalies about the entry's OWN period: the reference's 1900-1930 mean
    reference = raw[raw["data_type"] == "reference"].set_index(
        pd.to_datetime(raw[raw["data_type"] == "reference"]["time"]).dt.year,
    )["tas"]
    got = context.raw[context.raw["data_type"] == "reference"].set_index(
        pd.to_datetime(context.raw[context.raw["data_type"] == "reference"]["time"]).dt.year,
    )["tas"]
    expected = reference.loc[1940:1978] - reference.loc[1900:1930].mean()
    np.testing.assert_allclose(got.to_numpy(), expected.to_numpy(), atol=1e-12)

    mine = _row(rows, "MyModel", "to_benchmark")
    assert mine["n_time"] == 39
    assert mine["window"] == "in-sample"  # tier2.window_labels, as today
    assert np.isfinite(mine["crps"])
    assert mine["crps"] < 0.2  # the 0.5 offset was removed
    clim = _row(rows, CLIMATOLOGY_DATA_ID, "baseline")
    assert clim["n_time"] == 39
    assert clim["n_members"] == 31  # sampled from 1900-1930, not 1985-2014


def test_an_entry_override_leaves_the_other_entries_alone() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    plain = pd.DataFrame(
        score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
    )
    with use_thresholds(HINDCAST_SWITCHES):
        other = pd.DataFrame(
            score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
        )
    pd.testing.assert_frame_equal(plain, other)


def test_a_window_on_an_absolute_entry_cuts_the_table() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    with use_thresholds({"tier2": {"scored_windows": {"plain": [2020, 2029]}}}):
        rows = pd.DataFrame(
            score_raw_output(raw, sources, settings=FAST, diagnostic="plain"),
        )
    assert _row(rows, "MyModel", "to_benchmark")["n_time"] == 10
    assert _row(rows, CLIMATOLOGY_DATA_ID, "baseline")["n_time"] == 10


# ---------------------------------------------------------------------------
# CB-9: paired block indices and the per-step table
# ---------------------------------------------------------------------------


def _old_bootstrap(crps_t, block_length, n_boot, alpha, seed, members=None, obs=None):  # noqa: ANN001, ANN202
    """``moving_block_bootstrap_ci`` as it was before ``paired_block_indices``."""
    series = np.asarray(crps_t, dtype=float)
    series = series[np.isfinite(series)]
    n_time = series.size
    length = int(np.clip(block_length, 1, n_time))
    n_blocks = int(np.ceil(n_time / length))
    rng = np.random.default_rng(seed)
    offsets = np.arange(length)

    def block_index(size: int) -> np.ndarray:
        starts = rng.integers(0, n_time - length + 1, size=(size, n_blocks))
        idx = starts[:, :, None] + offsets[None, None, :]
        return idx.reshape(size, n_blocks * length)[:, :n_time]

    if members is None:
        means = series[block_index(int(n_boot))].mean(axis=1)
    else:
        ensemble = np.atleast_2d(np.asarray(members, dtype=float))
        means = np.empty(int(n_boot))
        for i in range(int(n_boot)):
            picked = rng.integers(0, ensemble.shape[0], size=ensemble.shape[0])
            replicate = scoring.crps_fair(ensemble[picked], obs)
            means[i] = replicate[block_index(1)[0]].mean()
    lo, hi = np.percentile(means, [100 * alpha / 2.0, 100 * (1.0 - alpha / 2.0)])
    return (float(lo), float(hi))


def test_paired_block_indices_are_moving_blocks_from_a_seed_alone() -> None:
    idx = scoring.paired_block_indices(30, 4, 50, seed=7)
    assert idx.shape == (50, 30)
    assert idx.min() >= 0
    assert idx.max() <= 29
    # blocks of 4 consecutive steps (the last one is cut to 30 = 7 * 4 + 2)
    assert (np.diff(idx[:, :4], axis=1) == 1).all()
    assert (np.diff(idx[:, 4:8], axis=1) == 1).all()
    np.testing.assert_array_equal(idx, scoring.paired_block_indices(30, 4, 50, seed=7))
    assert not np.array_equal(idx, scoring.paired_block_indices(30, 4, 50, seed=8))
    # a block longer than the series degenerates to the series itself
    np.testing.assert_array_equal(
        scoring.paired_block_indices(5, 99, 3, seed=0),
        np.tile(np.arange(5), (3, 1)),
    )
    with pytest.raises(ValueError, match="n_steps"):
        scoring.paired_block_indices(0, 1, 1, seed=0)


def test_one_draw_applies_to_every_entry_on_the_same_axis() -> None:
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=40), rng.normal(size=40)
    idx = scoring.paired_block_indices(40, 3, 200, seed=1)
    # paired: the difference of the resampled means is the resampled difference
    np.testing.assert_allclose(
        a[idx].mean(axis=1) - b[idx].mean(axis=1),
        (a - b)[idx].mean(axis=1),
    )


def test_moving_block_bootstrap_ci_is_bit_identical_for_existing_callers() -> None:
    rng = np.random.default_rng(3)
    members = rng.normal(size=(4, 36))
    obs = rng.normal(size=36)
    crps_t = scoring.crps_fair(members, obs)
    for block in (1, 3, 12, 50):
        assert scoring.moving_block_bootstrap_ci(
            crps_t,
            block_length=block,
            n_boot=80,
            seed=5,
        ) == _old_bootstrap(crps_t, block, 80, 0.05, 5)
        assert scoring.moving_block_bootstrap_ci(
            crps_t,
            block_length=block,
            n_boot=20,
            seed=5,
            members=members,
            obs=obs,
        ) == _old_bootstrap(crps_t, block, 20, 0.05, 5, members, obs)


STEPS_ON = {"tier2": {"persist_steps": True}}


def _steps(db_path) -> pd.DataFrame:  # noqa: ANN001
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(f'SELECT * FROM "{DIAGNOSTIC}"."score_steps"').df()
    finally:
        con.close()


def _tables_of(db_path) -> set[str]:  # noqa: ANN001
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    finally:
        con.close()


def test_nothing_is_written_unless_persist_steps_is_on(synthetic_db) -> None:  # noqa: ANN001, F811
    scoring_pass.score_database(synthetic_db, settings=FAST)
    assert "score_steps" not in _tables_of(synthetic_db)


def test_score_steps_reproduce_the_stored_crps_and_leave_one_out(synthetic_db) -> None:  # noqa: ANN001, F811
    with use_thresholds(STEPS_ON):
        scoring_pass.score_database(synthetic_db, settings=FAST)
    steps = _steps(synthetic_db)
    assert list(steps.columns) == [
        "data_id",
        "data_type",
        "var_id",
        "step_kind",
        "step",
        "left_out",
        "crps",
        "scorer",
    ]
    assert set(steps["scorer"]) == {scoring_pass.scorer_tag()}
    assert set(steps["step_kind"]) == {"time"}
    assert steps["step"].iloc[0] == "1985-01-01" or "-" in steps["step"].iloc[0]

    con = duckdb.connect(str(synthetic_db), read_only=True)
    try:
        metrics = con.execute(
            f'SELECT * FROM "{DIAGNOSTIC}"."metrics" WHERE scorer = ? AND crps IS NOT NULL',
            [scoring_pass.scorer_tag()],
        ).df()
    finally:
        con.close()
    whole = steps[steps["left_out"] == ""]
    for _, row in metrics.iterrows():
        mine = whole[
            (whole["data_id"] == row["data_id"])
            & (whole["data_type"] == row["data_type"])
            & (whole["var_id"] == row["var_id"])
        ]
        assert len(mine) == row["n_time"]
        # no ESS reweighting touches the point score: the mean IS the crps
        assert mine["crps"].mean() == pytest.approx(row["crps"], rel=1e-12)

    # the 3-member model has leave-one-out rows; 2-member models and baselines do not
    loo = steps[steps["left_out"] != ""]
    assert set(loo["data_id"]) == {"MyModel"}
    assert set(loo["data_type"]) == {"to_benchmark"}
    assert set(loo["left_out"]) == {"r1i1p1f1", "r2i1p1f1", "r3i1p1f1"}
    assert len(loo) == 3 * len(
        whole[(whole["data_id"] == "MyModel") & (whole["data_type"] == "to_benchmark")],
    )


def test_leave_one_out_rows_are_the_ensemble_without_that_member() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    with use_thresholds(NO_DRAWS):
        steps: list[dict] = []
        score_raw_output(raw, sources, settings=FAST, steps_out=steps)
    frame = pd.DataFrame(steps)
    members, obs, _years = _model_steps(raw, sources, "MyModel", "model")
    for i, label in enumerate(["r1i1p1f1", "r2i1p1f1", "r3i1p1f1"]):
        got = frame[
            (frame["data_id"] == "MyModel")
            & (frame["data_type"] == "to_benchmark")
            & (frame["left_out"] == label)
        ]
        expected = scoring.crps_fair(np.delete(members, i, axis=0), obs)
        np.testing.assert_allclose(got["crps"].to_numpy(), expected)


def test_the_pass_rewrites_its_own_step_rows(synthetic_db) -> None:  # noqa: ANN001, F811
    with use_thresholds(STEPS_ON):
        scoring_pass.score_database(synthetic_db, settings=FAST)
        first = _steps(synthetic_db)
        scoring_pass.score_database(synthetic_db, settings=FAST)
        second = _steps(synthetic_db)
    assert len(first) == len(second)
    # a foreign row survives; switching the key off clears only the pass's own
    con = duckdb.connect(str(synthetic_db))
    con.execute(
        f'INSERT INTO "{DIAGNOSTIC}"."score_steps" VALUES '
        "('x', 'other', 'tas', 'time', '2000-01-01', '', 1.0, 'someone-else')",
    )
    con.close()
    scoring_pass.score_database(synthetic_db, settings=FAST)
    left = _steps(synthetic_db)
    assert list(left["scorer"]) == ["someone-else"]


def test_eof_and_scalar_layouts_write_mode_and_scalar_steps() -> None:
    raw, sources = _eof_tables(np.random.default_rng(31))
    steps: list[dict] = []
    rows = pd.DataFrame(score_eof_output(raw, sources, settings=FAST, steps_out=steps))
    frame = pd.DataFrame(steps)
    assert set(frame["step_kind"]) == {"mode"}
    mine = frame[
        (frame["data_id"] == "MyModel")
        & (frame["data_type"] == "to_benchmark")
        & (frame["left_out"] == "")
    ]
    assert list(mine["step"]) == [str(i) for i in range(1, len(mine) + 1)]
    crps = _row(rows, "MyModel", "to_benchmark")["crps"]
    assert mine["crps"].mean() == pytest.approx(crps)
    assert set(frame[frame["left_out"] != ""]["left_out"]) == {
        "r1i1p1f1",
        "r2i1p1f1",
        "r3i1p1f1",
    }

    raw, sources = _scalar_tables(np.random.default_rng(51))
    steps = []
    rows = pd.DataFrame(score_scalar_output(raw, sources, steps_out=steps))
    frame = pd.DataFrame(steps)
    assert set(frame["step_kind"]) == {"scalar"}
    assert set(frame["step"]) == {"0"}
    mine = frame[
        (frame["data_id"] == "MyModel")
        & (frame["data_type"] == "to_benchmark")
        & (frame["left_out"] == "")
    ]
    assert len(mine) == 1
    assert mine["crps"].iloc[0] == pytest.approx(
        _row(rows, "MyModel", "to_benchmark")["crps"],
    )
    loo = frame[
        (frame["data_id"] == "MyModel")
        & (frame["data_type"] == "to_benchmark")
        & (frame["left_out"] != "")
    ]
    assert len(loo) == 3


def test_a_baseline_registered_through_the_context_gets_step_rows(toy_baseline) -> None:  # noqa: ANN001
    raw, sources = _tables(np.random.default_rng(5))
    steps: list[dict] = []
    with use_thresholds({"tier2": {"baselines": ["toy"]}}):
        score_raw_output(raw, sources, settings=FAST, steps_out=steps)
    toy = pd.DataFrame(steps).query("data_id == 'ToyBaseline'")
    assert len(toy) == len(ALL_YEARS_)
    assert set(toy["left_out"]) == {""}  # pseudo-members: no leave-one-out


ALL_YEARS_ = list(range(1985, 2045))


def test_frozen_comparison_ignores_the_step_table(tmp_path) -> None:  # noqa: ANN001
    """Freeze and merge copy their own four tables and leave ``score_steps`` alone."""
    import shutil

    import test_frozen_comparison as tfc

    (tmp_path / "live").mkdir()
    live = tfc._write(tmp_path / "live" / "ClimateBench2_TierII.ddb", tfc._live_tables())  # noqa: SLF001
    with use_thresholds(STEPS_ON):
        scoring_pass.score_databases([live], settings=FAST)
    assert "score_steps" in tfc.duckdb.connect(str(live), read_only=True).execute(
        "SELECT table_name FROM information_schema.tables",
    ).df()["table_name"].tolist()
    tfc.fc.freeze([live], tmp_path / "bundle", nominal_timerange="20150101/20341231")
    run = tmp_path / "run" / live.name
    run.parent.mkdir()
    shutil.copy(live, run)
    assert tfc.fc.strip_comparison_rows(run) > 0
    tfc.fc.merge(
        run,
        tmp_path / "bundle",
        stripped={tfc.WIDE: ["tas", "pr"], tfc.EOF: ["tas", "pr"]},
    )
    with use_thresholds(STEPS_ON):
        scoring_pass.score_databases([run], settings=FAST)

    def steps_of(path) -> pd.DataFrame:  # noqa: ANN001
        con = duckdb.connect(str(path), read_only=True)
        try:
            return (
                con.execute(f'SELECT * FROM "{tfc.WIDE}"."score_steps"')
                .df()
                .sort_values(["data_id", "data_type", "var_id", "left_out", "step"])
                .reset_index(drop=True)
            )
        finally:
            con.close()

    pd.testing.assert_frame_equal(steps_of(live), steps_of(run))
