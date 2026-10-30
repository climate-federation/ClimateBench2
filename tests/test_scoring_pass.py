"""End-to-end tests of the Tier II scoring pass on a synthetic database.

The database is built table-by-table to match what
``climateeval.suites.Suite.get_database`` writes (one DuckDB *schema* per
diagnostic, holding ``raw_output`` / ``metrics`` / ``variables`` /
``data_sources``) and what ``climateeval.report._db.read_database`` reads
back, so no climate data or ClimateEval run is needed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

duckdb = pytest.importorskip("duckdb")

from climatebench2 import scoring_pass  # noqa: E402
from climatebench2._thresholds import get_threshold  # noqa: E402
from climatebench2.scoring_pass import (  # noqa: E402
    CLIMATOLOGY_DATA_ID,
    SCORER,
    group_members,
    score_database,
    score_raw_output,
    source_names,
    stack_members,
)

DIAGNOSTIC = "annual_mean_timeseries"
BASELINE_YEARS = range(1985, 2015)
TEST_YEARS = range(2015, 2045)
ALL_YEARS = list(BASELINE_YEARS) + list(TEST_YEARS)

#: Fast bootstrap settings — the protocol values live in thresholds.yml.
FAST = {
    "block_length_monthly": 12,
    "block_length_annual": 3,
    "n_boot": 60,
    "alpha": 0.05,
    "resample_members": True,
    "seed": 1,
}


def _times(years: list[int]) -> pd.DatetimeIndex:
    return pd.to_datetime([f"{y}-01-01" for y in years])


def _truth(years: list[int], rng: np.random.Generator) -> np.ndarray:
    """A warming signal plus interannual noise."""
    x = np.array(years, dtype=float) - 1985.0
    return 0.02 * x + rng.normal(0.0, 0.1, len(years))


#: (name, category, variant(s), data_type, spread) of every source.
SOURCES = [
    ("OBS", "observation", [""], "reference", 0.0),
    ("MyModel", "model", ["r1i1p1f1", "r2i1p1f1", "r3i1p1f1"], "to_benchmark", 0.10),
    ("CMIP6_A", "CMIP6", ["r1i1p1f1", "r2i1p1f1"], "other", 0.25),
    ("CMIP6_B", "CMIP6", ["r1i1p1f1", "r2i1p1f1"], "other", 0.40),
    ("CMIP6_C", "CMIP6", ["r1i1p1f1"], "other", 0.30),  # single member -> NaN
    ("MyModel", "CMIP6", ["r1i1p1f1", "r2i1p1f1"], "other", 0.01),  # leave-one-out
]


def _source_id(category: str, name: str, exp: str, variant: str) -> str:
    """Mirror ``DataSourceInformation.id``."""
    return "_".join(p for p in (category, name, exp, variant) if p)


def _tables(rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(raw_output, data_sources)`` for one annual-mean tas diagnostic."""
    truth_all = _truth(ALL_YEARS, rng)
    raw_rows, sources = [], []
    for name, category, variants, data_type, spread in SOURCES:
        # Every source carries the baseline window as well as the test
        # window: regime (a) scores anomalies about each source's own
        # 1985-2014 climatology, so that is what the suite now loads
        # (`windows.extend_to_baseline`).
        years = ALL_YEARS
        exp = "" if data_type == "reference" else "historical"
        base = truth_all[-len(years) :] if years != ALL_YEARS else truth_all
        for variant in variants:
            data_id = _source_id(category, name, exp, variant)
            values = base + rng.normal(0.0, spread, len(years))
            raw_rows.append(
                pd.DataFrame(
                    {
                        "data_id": data_id,
                        "data_type": data_type,
                        "time": _times(years),
                        "tas": truth_all if data_type == "reference" else values,
                    },
                ),
            )
            sources.append(
                {
                    "id": data_id,
                    "name": name,
                    "category": category,
                    "institute": "",
                    "exp": exp,
                    "variant": variant,
                    "references": "",
                },
            )
    return pd.concat(raw_rows, ignore_index=True), pd.DataFrame(sources)


@pytest.fixture
def synthetic_db(tmp_path):  # noqa: ANN201
    """A .ddb laid out like ``Suite.get_database``'s output."""
    rng = np.random.default_rng(0)
    raw_output, data_sources = _tables(rng)
    metrics = pd.DataFrame(
        {
            "data_id": [_source_id("model", "MyModel", "historical", "r1i1p1f1")],
            "reference_data_id": ["observation_OBS"],
            "var_id": ["tas"],
            "weighted_rmse": [0.12],
            "weighted_pearsonr": [0.98],
            "weighted_emd": [0.05],
        },
    )
    db_path = tmp_path / "ClimateBench2_TierII.ddb"
    con = duckdb.connect(str(db_path))
    con.execute(f'CREATE SCHEMA "{DIAGNOSTIC}"')
    for table, frame in (
        ("raw_output", raw_output),
        ("metrics", metrics),
        ("data_sources", data_sources),
    ):
        con.register("frame", frame)
        con.execute(f'CREATE TABLE "{DIAGNOSTIC}"."{table}" AS SELECT * FROM frame')
        con.unregister("frame")
    con.close()
    return db_path


def _metrics(db_path) -> pd.DataFrame:  # noqa: ANN001
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(f'SELECT * FROM "{DIAGNOSTIC}"."metrics"').df()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_source_names_maps_ids_to_model_names() -> None:
    _raw, sources = _tables(np.random.default_rng(1))
    names = source_names(sources)
    assert names["model_MyModel_historical_r1i1p1f1"] == "MyModel"
    assert names["model_MyModel_historical_r3i1p1f1"] == "MyModel"
    assert names["CMIP6_CMIP6_A_historical_r1i1p1f1"] == "CMIP6_A"


def test_group_members_stacks_variants_of_one_model() -> None:
    raw, sources = _tables(np.random.default_rng(2))
    groups = group_members(raw, source_names(sources))
    assert len(groups[("to_benchmark", "MyModel")]) == 3
    assert len(groups[("other", "CMIP6_A")]) == 2
    assert len(groups[("other", "CMIP6_C")]) == 1
    # the reference is the target, never a forecast
    assert not any(key[0] == "reference" for key in groups)


def test_group_members_without_a_data_sources_table() -> None:
    """An older database still groups (one member per data id)."""
    raw, _sources = _tables(np.random.default_rng(3))
    groups = group_members(raw, {})
    assert all(len(frames) == 1 for frames in groups.values())


def test_stack_members_aligns_on_common_times() -> None:
    raw, sources = _tables(np.random.default_rng(4))
    reference = raw[raw.data_type == "reference"]
    groups = group_members(raw, source_names(sources))
    frames = groups[("to_benchmark", "MyModel")]
    frames = [frames[0].iloc[2:], *frames[1:]]  # first member misses 2 years
    members, obs, times = stack_members(frames, reference, "tas")
    assert members.shape == (3, len(ALL_YEARS) - 2)
    assert obs.shape == (len(ALL_YEARS) - 2,)
    assert times.size == len(ALL_YEARS) - 2


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_score_raw_output_stacks_members_and_scores_baselines() -> None:
    raw, sources = _tables(np.random.default_rng(5))
    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    by_id = rows.set_index("data_id")

    assert set(rows["data_id"]) == {
        "MyModel",
        "CMIP6_A",
        "CMIP6_B",
        "CMIP6_C",
        CLIMATOLOGY_DATA_ID,
        # the calibrated-EBM baseline, reported alongside (work package 6b)
        scoring_pass.PATTERN_SCALING_DATA_ID,
    }
    submission = by_id.loc[
        (by_id.index == "MyModel") & (by_id["data_type"] == "to_benchmark")
    ].iloc[0]
    assert submission["n_members"] == 3  # the three variants, one ensemble
    # Scored absolutely here (`diagnostic=""` is not an anomaly entry), so
    # every loaded year is a scored step; the anomaly path is exercised below.
    assert submission["n_time"] == len(ALL_YEARS)
    assert submission["crps"] > 0
    assert submission["crps_se"] > 0
    assert submission["crps_ci_lo"] < submission["crps"] < submission["crps_ci_hi"]
    assert submission["reason"] == ""
    assert rows["scorer"].eq(SCORER).all()

    # The tighter ensemble scores better than the looser one
    assert (
        by_id.loc[by_id.index == "CMIP6_A", "crps"].iloc[0]
        < by_id.loc[by_id.index == "CMIP6_B", "crps"].iloc[0]
    )
    # ... and the climatology baseline, which ignores the warming, worst of all
    assert by_id.loc[CLIMATOLOGY_DATA_ID, "crps"] > submission["crps"]
    assert by_id.loc[CLIMATOLOGY_DATA_ID, "n_members"] == len(BASELINE_YEARS)


def test_single_member_model_gets_a_nan_row_with_a_reason() -> None:
    raw, sources = _tables(np.random.default_rng(6))
    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    single = rows[rows["data_id"] == "CMIP6_C"].iloc[0]
    assert np.isnan(single["crps"])
    assert single["reason"] == "single member"
    assert single["n_members"] == 1
    assert np.isnan(single["skill"])


def test_e_ref_is_the_leave_one_out_median_of_per_model_crps() -> None:
    raw, sources = _tables(np.random.default_rng(7))
    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))

    comparison = rows[(rows["data_type"] == "other") & rows["crps"].notna()]
    per_model = dict(zip(comparison["data_id"], comparison["crps"], strict=True))
    assert set(per_model) == {"CMIP6_A", "CMIP6_B", "MyModel"}

    submission = rows[
        (rows["data_id"] == "MyModel") & (rows["data_type"] == "to_benchmark")
    ].iloc[0]
    # Leave-one-out: the comparison model called MyModel is excluded
    expected = float(np.median([per_model["CMIP6_A"], per_model["CMIP6_B"]]))
    assert submission["e_ref"] == pytest.approx(expected)
    assert submission["n_ref_models"] == 2
    assert submission["skill"] == pytest.approx(1.0 - submission["crps"] / expected)
    # A comparison model is also left out of its own reference
    cmip6_a = rows[
        (rows["data_id"] == "CMIP6_A") & (rows["data_type"] == "other")
    ].iloc[0]
    assert cmip6_a["n_ref_models"] == 2
    assert cmip6_a["e_ref"] == pytest.approx(
        float(np.median([per_model["CMIP6_B"], per_model["MyModel"]])),
    )
    # The submission tracks the truth more closely than the median CMIP6 model
    assert submission["skill"] > 0


def test_no_pooled_cmip6_mme_row() -> None:
    """The pooled mixture is ruled out (overdispersed) — median-of-models only."""
    raw, sources = _tables(np.random.default_rng(8))
    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    assert "CMIP6-MME" not in set(rows["data_id"])


def test_climatology_row_says_why_when_the_baseline_window_is_missing() -> None:
    """A real Tier II database is cut to the test window (no 1985-2014 obs)."""
    raw, sources = _tables(np.random.default_rng(9))
    years = pd.to_datetime(raw["time"]).dt.year
    cut = raw[years >= min(TEST_YEARS)]
    rows = pd.DataFrame(score_raw_output(cut, sources, settings=FAST))
    clim = rows[rows["data_id"] == CLIMATOLOGY_DATA_ID].iloc[0]
    assert np.isnan(clim["crps"])
    assert "1985-2014" in clim["reason"]


def test_monthly_climatology_uses_calendar_month_pseudo_members() -> None:
    rng = np.random.default_rng(10)
    times = pd.date_range("2010-01-01", "2019-12-01", freq="MS")
    season = 5.0 * np.sin(2 * np.pi * (times.month - 1) / 12)
    obs = season + rng.normal(0, 0.5, times.size)
    frames = [
        pd.DataFrame(
            {"data_id": "obs_O", "data_type": "reference", "time": times, "tas": obs},
        ),
    ]
    for variant in ("r1i1p1f1", "r2i1p1f1"):
        frames.append(
            pd.DataFrame(
                {
                    "data_id": f"model_M_{variant}",
                    "data_type": "to_benchmark",
                    "time": times,
                    "tas": season + rng.normal(0, 0.5, times.size),
                },
            ),
        )
    sources = pd.DataFrame(
        [
            {"id": "obs_O", "name": "O"},
            {"id": "model_M_r1i1p1f1", "name": "M"},
            {"id": "model_M_r2i1p1f1", "name": "M"},
        ],
    )
    # 2010-2014 is inside the protocol baseline window, 2015+ is the test window
    rows = pd.DataFrame(score_raw_output(pd.concat(frames), sources, settings=FAST))
    clim = rows[rows["data_id"] == CLIMATOLOGY_DATA_ID].iloc[0]
    assert clim["n_members"] == 5  # 2010..2014, one pseudo-member per year
    assert clim["n_time"] == 60  # 2015..2019 monthly
    assert clim["block_length"] == FAST["block_length_monthly"]
    # A seasonal-cycle climatology is a decent forecast: CRPS well under the
    # 5 K amplitude it captures
    assert 0.0 < clim["crps"] < 1.5


# ---------------------------------------------------------------------------
# Database round trip
# ---------------------------------------------------------------------------


def test_score_database_appends_to_the_metrics_table(synthetic_db) -> None:  # noqa: ANN001
    report = score_database(synthetic_db, settings=FAST)
    assert report.diagnostics == [DIAGNOSTIC]
    assert report.single_member == 1

    metrics = _metrics(synthetic_db)
    # ClimateEval's deterministic row survives, untouched
    deterministic = metrics[metrics["scorer"].isna()]
    assert len(deterministic) == 1
    assert deterministic.iloc[0]["weighted_rmse"] == pytest.approx(0.12)
    # ... alongside the new score rows, in the same table
    scored = metrics[metrics["scorer"] == SCORER]
    assert len(scored) == report.rows
    assert set(scored["data_id"]) == {
        "MyModel",
        "CMIP6_A",
        "CMIP6_B",
        "CMIP6_C",
        CLIMATOLOGY_DATA_ID,
        scoring_pass.PATTERN_SCALING_DATA_ID,
    }
    for column in ("crps_ci_lo", "crps_ci_hi", "e_ref", "n_ref_models", "skill"):
        assert column in metrics.columns


def test_score_database_is_idempotent(synthetic_db) -> None:  # noqa: ANN001
    first = score_database(synthetic_db, settings=FAST)
    before = _metrics(synthetic_db)
    second = score_database(synthetic_db, settings=FAST)
    after = _metrics(synthetic_db)
    assert first.rows == second.rows
    assert len(before) == len(after)  # replaced, not duplicated
    np.testing.assert_allclose(
        before[before["scorer"] == SCORER].sort_values("data_id")["crps"].to_numpy(),
        after[after["scorer"] == SCORER].sort_values("data_id")["crps"].to_numpy(),
        equal_nan=True,
    )


def test_score_database_reads_back_through_climateeval(synthetic_db) -> None:  # noqa: ANN001
    """The rows must be visible to the leaderboard's reader."""
    pytest.importorskip("climateeval")
    from climateeval.report._db import read_database

    score_database(synthetic_db, settings=FAST)
    tables = read_database(synthetic_db)
    metrics = tables[DIAGNOSTIC]["metrics"]
    assert (metrics["scorer"] == SCORER).sum() > 0


def test_score_database_without_a_metrics_table(tmp_path) -> None:  # noqa: ANN001
    raw, sources = _tables(np.random.default_rng(11))
    db_path = tmp_path / "no_metrics.ddb"
    con = duckdb.connect(str(db_path))
    con.execute('CREATE SCHEMA "ts"')
    for table, frame in (("raw_output", raw), ("data_sources", sources)):
        con.register("frame", frame)
        con.execute(f'CREATE TABLE "ts"."{table}" AS SELECT * FROM frame')
        con.unregister("frame")
    con.close()

    report = score_database(db_path, settings=FAST)
    assert report.rows > 0
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        created = con.execute('SELECT * FROM "ts"."metrics"').df()
    finally:
        con.close()
    assert set(created["scorer"]) == {SCORER}


def test_score_database_ignores_non_timeseries_diagnostics(tmp_path) -> None:  # noqa: ANN001
    db_path = tmp_path / "spatial.ddb"
    con = duckdb.connect(str(db_path))
    con.execute('CREATE SCHEMA "map_diag"')
    frame = pd.DataFrame(
        {
            "data_id": ["m"],
            "data_type": ["to_benchmark"],
            "latitude": [0.0],
            "longitude": [0.0],
            "tas": [1.0],
        },
    )
    con.register("frame", frame)
    con.execute('CREATE TABLE "map_diag"."raw_output" AS SELECT * FROM frame')
    con.close()
    report = score_database(db_path, settings=FAST)
    assert report.rows == 0
    assert "no scorable" in report.summary()


def test_each_variable_takes_its_own_reference() -> None:
    """One diagnostic holds several references (HadCRUT5, GPCP, CERES...).

    Their rows carry only their own variable, so picking "the first reference
    data_id" scored one variable and silently dropped the rest.
    """
    rng = np.random.default_rng(40)
    raw, sources = _tables(rng)
    times = _times(list(TEST_YEARS))
    # A second variable with a DIFFERENT reference product, as `pr` vs `tas`
    # sit in the Tier II annual-mean entry.
    extra = [
        pd.DataFrame(
            {
                "data_id": "observation_GPCP",
                "data_type": "reference",
                "time": times,
                "pr": np.full(len(times), 2.5),
            },
        ),
    ]
    for variant in ("r1i1p1f1", "r2i1p1f1", "r3i1p1f1"):
        extra.append(
            pd.DataFrame(
                {
                    "data_id": _source_id("model", "MyModel", "historical", variant),
                    "data_type": "to_benchmark",
                    "time": times,
                    "pr": 2.5 + rng.normal(0.0, 0.1, len(times)),
                },
            ),
        )
    sources = pd.concat(
        [
            sources,
            pd.DataFrame(
                [{"id": "observation_GPCP", "name": "GPCP", "category": "observation"}],
            ),
        ],
        ignore_index=True,
    )
    rows = pd.DataFrame(
        score_raw_output(
            pd.concat([raw, *extra], ignore_index=True),
            sources,
            settings=FAST,
        ),
    )
    assert {"tas", "pr"} <= set(rows["var_id"])
    pr_rows = rows[(rows["var_id"] == "pr") & (rows["data_id"] == "MyModel")]
    assert np.isfinite(pr_rows["crps"]).any()


def test_every_scored_row_carries_a_window_label() -> None:
    raw, sources = _tables(np.random.default_rng(41))
    rows = pd.DataFrame(
        score_raw_output(
            raw,
            sources,
            settings=FAST,
            diagnostic="annual_mean_timeseries",
        ),
    )
    assert set(rows["window"]) == {scoring_pass.WINDOW_HELD_OUT}

    # An in-sample diagnostic (a historical-period climatology)
    rows = pd.DataFrame(
        score_raw_output(raw, sources, settings=FAST, diagnostic="annual_cycle"),
    )
    assert set(rows["window"]) == {scoring_pass.WINDOW_IN_SAMPLE}


def test_window_label_resolution_order() -> None:
    label = scoring_pass.window_label
    # A var_id entry wins over the diagnostic's own label ...
    assert label("realized_warming_level", "gmst_trend_1950") == "in-sample"
    assert label("realized_warming_level", "gmst_warming_level") == "held-out"
    # ... a consistency row inherits the label of the variable it tests ...
    assert label("realized_warming_level", "gmst_trend_1950_consistency") == "in-sample"
    assert label("annual_mean_timeseries", "tas_trend_consistency") == "held-out"
    # ... an unlisted variable falls back to the diagnostic ...
    assert label("pinatubo", "anything") == "in-sample"
    # ... and an unlisted diagnostic to the default.
    assert label("some_new_diagnostic", "anything") == "held-out"


def test_variable_status_follows_paper_table_2() -> None:
    """Core vs Extended (paper Table 2; OHC Extended, ratified 2026-10-30)."""
    status = scoring_pass.variable_status
    for var_id in (
        "tas", "tos", "pr", "rsut", "rlut", "rtnt", "rsutcs", "rlutcs",
        "swcre", "lwcre", "siconc_nh_sep_extent", "siconc_sh_feb_extent",
    ):
        assert status("annual_mean_timeseries", var_id) == "core", var_id
    for var_id in ("clt", "clwvi", "clivi", "prw"):
        assert status("annual_mean_timeseries", var_id) == "extended", var_id
        assert status("eof_projection", var_id) == "extended", var_id
    for var_id in ("phcint_total", "phcint_2000m", "phcint_100m"):
        assert status("ohc", var_id) == "extended", var_id
    # The whole OHC entry is Extended, whatever its layer is called ...
    assert status("ohc", "some_new_layer") == "extended"
    # ... a consistency row inherits its variable's tag ...
    assert status("annual_mean_timeseries", "prw_trend_consistency") == "extended"
    assert status("annual_mean_timeseries", "tas_trend_consistency") == "core"
    # ... and unlisted Core entries (daily extremes, events) take the default.
    assert status("pr_extremes_series", "rx1day_global_land") == "core"
    assert status("extremes", "txx_global_land") == "core"
    assert status("realized_warming_level", "gmst_warming_level") == "core"


def test_variable_status_rejects_an_unknown_tag(monkeypatch) -> None:  # noqa: ANN001
    real = scoring_pass.get_threshold

    def thresholds(path: str):  # noqa: ANN202
        if path == "tier2.variable_status":
            return {"default": "core", "var_ids": {"tas": "optional"}}
        return real(path)

    monkeypatch.setattr(scoring_pass, "get_threshold", thresholds)
    with pytest.raises(ValueError, match="optional"):
        scoring_pass.variable_status("annual_mean_timeseries", "tas")


def test_variable_status_is_not_part_of_the_bundle_fingerprint() -> None:
    """Presentation, not data: re-tagging must never invalidate a bundle."""
    from climatebench2 import frozen_comparison

    assert "tier2.variable_status" not in frozen_comparison.PROTOCOL_THRESHOLDS
    assert not any(
        key.startswith("tier2.variable_status")
        for key in frozen_comparison.PROTOCOL_THRESHOLDS
    )


def test_every_scored_row_carries_its_variable_status() -> None:
    raw, sources = _tables(np.random.default_rng(41))
    rows = pd.DataFrame(
        score_raw_output(
            raw,
            sources,
            settings=FAST,
            diagnostic="annual_mean_timeseries",
        ),
    )
    assert set(rows["variable_status"]) == {scoring_pass.STATUS_CORE}
    rows = pd.DataFrame(
        score_raw_output(raw, sources, settings=FAST, diagnostic="ohc"),
    )
    # `tas` is listed Core by var_id, which wins over the `ohc` entry's tag
    assert set(rows["variable_status"]) == {scoring_pass.STATUS_CORE}


# ---------------------------------------------------------------------------
# The pattern-scaling baseline (work package 6b)
# ---------------------------------------------------------------------------


def _pattern_row(rows: pd.DataFrame) -> pd.Series:
    return rows[rows["data_id"] == scoring_pass.PATTERN_SCALING_DATA_ID].iloc[0]


def test_pattern_scaling_baseline_is_scored_for_a_gmst_series() -> None:
    """The calibrated EBM is a real, fair-CRPS-able forecast of tas."""
    raw, sources = _tables(np.random.default_rng(12))
    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    row = _pattern_row(rows)
    assert row["reason"] == ""
    assert np.isfinite(row["crps"])
    # One pseudo-member per baseline year (the detrended observed residuals)
    assert row["n_members"] == len(BASELINE_YEARS)
    # ... scored only on the years the ERF table covers (it stops in 2030)
    assert 3 <= row["n_time"] <= len(TEST_YEARS)
    # `value` carries the one calibrated parameter, inside its search bounds
    low, high = get_threshold("tier2.pattern_scaling.lambda_bounds")
    assert low <= row["value"] <= high
    # It is a baseline, so it never enters E_ref
    assert row["data_type"] == "baseline"


def test_pattern_scaling_is_skipped_for_a_variable_it_cannot_forecast() -> None:
    """A two-layer EBM says nothing about precipitation: no row at all."""
    raw, sources = _tables(np.random.default_rng(13))
    rows = pd.DataFrame(
        score_raw_output(raw.rename(columns={"tas": "pr"}), sources, settings=FAST),
    )
    assert scoring_pass.PATTERN_SCALING_DATA_ID not in set(rows["data_id"])


def test_pattern_scaling_says_why_without_a_calibration_record() -> None:
    """Cut to the test window and with no baseline record, it cannot be fitted."""
    raw, sources = _tables(np.random.default_rng(14))
    cut = raw[pd.to_datetime(raw["time"]).dt.year >= min(TEST_YEARS)]
    rows = pd.DataFrame(score_raw_output(cut, sources, settings=FAST))
    row = _pattern_row(rows)
    assert np.isnan(row["crps"])
    assert "calibrate" in row["reason"]


def test_pattern_scaling_uses_the_recorded_baseline_window() -> None:
    """With the pre-2015 record back in the database it can be fitted again."""
    rng = np.random.default_rng(15)
    raw, sources = _tables(rng)
    cut = raw[pd.to_datetime(raw["time"]).dt.year >= min(TEST_YEARS)]
    baseline = scoring_pass.baseline_records(
        _baseline_rows(list(BASELINE_YEARS), rng),
    )
    rows = pd.DataFrame(
        score_raw_output(cut, sources, settings=FAST, baseline=baseline),
    )
    row = _pattern_row(rows)
    assert row["reason"] == ""
    assert np.isfinite(row["crps"])


# ---------------------------------------------------------------------------
# The reference's baseline-window record (ReferenceBaselineRecord's rows)
# ---------------------------------------------------------------------------


def _baseline_rows(years: list[int], rng: np.random.Generator) -> pd.DataFrame:
    """What ``ReferenceBaselineRecord`` writes for an annual series."""
    return pd.DataFrame(
        {
            "data_id": "observation_OBS",
            "data_type": scoring_pass.BASELINE_ANNUAL_DATA_TYPE,
            "time": _times(years),
            "tas": _truth(years, rng),
        },
    )


def test_climatology_uses_the_recorded_baseline_window() -> None:
    """The Tier II cut removes 1985-2014; the baseline record puts it back."""
    rng = np.random.default_rng(20)
    raw, sources = _tables(rng)
    cut = raw[pd.to_datetime(raw["time"]).dt.year >= min(TEST_YEARS)]
    baseline = scoring_pass.baseline_records(
        _baseline_rows(list(BASELINE_YEARS), rng),
    )
    assert set(baseline) == {("tas", "annual")}

    rows = pd.DataFrame(
        score_raw_output(cut, sources, settings=FAST, baseline=baseline),
    )
    clim = rows[rows["data_id"] == CLIMATOLOGY_DATA_ID].iloc[0]
    assert clim["reason"] == ""
    assert np.isfinite(clim["crps"])
    assert clim["n_members"] == len(BASELINE_YEARS)
    assert clim["n_time"] == len(TEST_YEARS)
    # The no-skill floor is worse than the submission, which tracks the truth
    submission = rows[
        (rows["data_id"] == "MyModel") & (rows["data_type"] == "to_benchmark")
    ].iloc[0]
    assert clim["crps"] > submission["crps"]


def test_baseline_rows_are_never_scored_as_a_forecast() -> None:
    """A pre-2015 sample is not a model: it must not appear as a scored row."""
    rng = np.random.default_rng(21)
    raw, sources = _tables(rng)
    combined = pd.concat(
        [raw, _baseline_rows(list(BASELINE_YEARS), rng)],
        ignore_index=True,
    )
    groups = group_members(combined, source_names(sources))
    assert not [
        key for key in groups if "baseline" in key[0]
    ]
    rows = pd.DataFrame(score_raw_output(combined, sources, settings=FAST))
    assert "observation_OBS" not in set(rows["data_id"])


# ---------------------------------------------------------------------------
# Observational uncertainty
# ---------------------------------------------------------------------------


def test_obs_sigma_floor_resolves_by_variable_then_prefix() -> None:
    assert scoring_pass.obs_sigma_floor("tas") == pytest.approx(0.05)
    # `tos_nino34` inherits the `tos` entry through its leading token
    assert scoring_pass.obs_sigma_floor("tos_nino34") == scoring_pass.obs_sigma_floor(
        "tos",
    )
    # An unknown sigma is 0 (the draws become a no-op), never invented
    assert scoring_pass.obs_sigma_floor("pr") == 0.0
    assert scoring_pass.obs_sigma_floor("no_such_variable") == 0.0


def test_observational_spread_adds_to_the_sigma_floor() -> None:
    times = _times(list(TEST_YEARS))
    reference = pd.DataFrame({"time": times, "tas": np.zeros(len(times))})
    product = pd.DataFrame({"time": times, "tas": np.full(len(times), 0.2)})
    sigma = scoring_pass.observational_sigma(reference, [product], "tas", 0.05)
    # std of {0, 0.2} with ddof=1 is 0.1414; in quadrature with the 0.05 floor
    expected = np.sqrt(0.05**2 + (0.2 / np.sqrt(2)) ** 2)
    assert sigma.iloc[0] == pytest.approx(expected)

    # With a single product only the floor is left
    flat = scoring_pass.observational_sigma(reference, [], "tas", 0.05)
    assert flat.unique().tolist() == [0.05]
    assert scoring_pass.observational_sigma(reference, [], "tas", 0.0) is None


def test_observational_products_are_not_scored_as_comparison_models() -> None:
    """HadISST next to an SST reference is a second truth, not a forecast."""
    rng = np.random.default_rng(22)
    raw, sources = _tables(rng)
    # Re-label CMIP6_B as an observational product of the same variable
    sources = sources.copy()
    is_b = sources["name"] == "CMIP6_B"
    sources.loc[is_b, "category"] = "observation"

    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    product = rows[rows["data_id"] == "CMIP6_B"].iloc[0]
    assert np.isnan(product["crps"])
    assert "observational product" in product["reason"]

    # ... and it is out of E_ref, which now rests on CMIP6_A alone
    submission = rows[
        (rows["data_id"] == "MyModel") & (rows["data_type"] == "to_benchmark")
    ].iloc[0]
    assert submission["n_ref_models"] == 1
    cmip6_a = rows[
        (rows["data_id"] == "CMIP6_A") & (rows["data_type"] == "other")
    ].iloc[0]
    assert submission["e_ref"] == pytest.approx(cmip6_a["crps"])


def test_sigma_obs_is_recorded_on_every_scored_row() -> None:
    rng = np.random.default_rng(23)
    raw, sources = _tables(rng)
    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    scored = rows[rows["crps"].notna() & (rows["var_id"] == "tas")]
    assert (scored["sigma_obs"] > 0).all()  # tas has a protocol floor


# ---------------------------------------------------------------------------
# Regime (c): trend consistency in the pass
# ---------------------------------------------------------------------------


def _sigma_internal_rows(sigma_trend: float, sigma_mean: float = 0.1) -> pd.DataFrame:
    """What ``InternalVariability`` writes into the Tier I database."""
    return pd.DataFrame(
        {
            "data_id": ["model_MyModel_historical_r1i1p1f1"],
            "data_type": ["to_benchmark"],
            "window_years_test": [float(len(TEST_YEARS))],
            "window_years_long": [76.0],
            "tas_sigma_int_mean_test": [sigma_mean],
            "tas_sigma_int_trend_test": [sigma_trend],
            "tas_sigma_int_trend_long": [sigma_trend / 4.0],
        },
    )


def test_internal_variability_rows_are_read_back_by_window() -> None:
    table = scoring_pass.internal_variability_from_raw(_sigma_internal_rows(0.004))
    assert set(table) == {("tas", "mean"), ("tas", "trend")}
    assert dict(table["tas", "trend"]) == {len(TEST_YEARS): 0.004, 76: 0.001}
    # The nearest window length wins
    assert scoring_pass.sigma_internal_for(
        table,
        "tas",
        "trend",
        len(TEST_YEARS),
    ) == pytest.approx(0.004)
    assert scoring_pass.sigma_internal_for(table, "tas", "trend", 70) == pytest.approx(
        0.001,
    )
    # No table, no variable, or no statistic -> 0, never a crash
    assert scoring_pass.sigma_internal_for(None, "tas", "trend", 30) == 0.0
    assert scoring_pass.sigma_internal_for(table, "clt", "trend", 30) == 0.0


def test_trend_consistency_rows_carry_sigma_internal_and_sigma_obs() -> None:
    rng = np.random.default_rng(24)
    raw, sources = _tables(rng)
    sigma_internal = scoring_pass.internal_variability_from_raw(
        _sigma_internal_rows(0.004),
    )
    rows = pd.DataFrame(
        score_raw_output(
            raw,
            sources,
            settings=FAST,
            sigma_internal=sigma_internal,
            diagnostic=DIAGNOSTIC,  # the real entry: 30 scored post-2015 years
        ),
    )
    trend = rows[rows["var_id"] == "tas_trend_consistency"]
    assert not trend.empty
    submission = trend[trend["data_type"] == "to_benchmark"].iloc[0]
    assert submission["data_id"] == "MyModel"
    assert submission["sigma_internal"] == pytest.approx(0.004)
    assert submission["sigma_obs"] > 0  # from tier2.obs_sigma tas = 0.05 K
    assert submission["total_sigma"] > submission["sigma_internal"]
    assert submission["value"] == pytest.approx(0.02, abs=0.01)  # the 0.02 K/yr truth
    assert submission["passes"] == 1.0
    assert 0.0 <= submission["p_value"] <= 1.0
    # A consistency row is not a CRPS row
    assert np.isnan(submission["crps"])


def test_trend_consistency_fails_for_an_inconsistent_observed_trend() -> None:
    rng = np.random.default_rng(25)
    raw, sources = _tables(rng)
    raw = raw.copy()
    is_ref = raw["data_type"] == "reference"
    raw.loc[is_ref, "tas"] = np.linspace(0.0, 50.0, int(is_ref.sum()))
    rows = pd.DataFrame(score_raw_output(raw, sources, settings=FAST))
    submission = rows[
        (rows["var_id"] == "tas_trend_consistency")
        & (rows["data_type"] == "to_benchmark")
    ].iloc[0]
    assert submission["passes"] == 0.0


def test_widening_sigma_internal_makes_the_test_more_permissive() -> None:
    rng = np.random.default_rng(26)
    raw, sources = _tables(rng)
    raw = raw.copy()
    is_ref = raw["data_type"] == "reference"
    raw.loc[is_ref, "tas"] = raw.loc[is_ref, "tas"] + np.linspace(
        0.0,
        1.0,
        int(is_ref.sum()),
    )

    def z_for(sigma_trend: float) -> float:
        table = scoring_pass.internal_variability_from_raw(
            _sigma_internal_rows(sigma_trend),
        )
        rows = pd.DataFrame(
            score_raw_output(raw, sources, settings=FAST, sigma_internal=table),
        )
        return float(
            rows[
                (rows["var_id"] == "tas_trend_consistency")
                & (rows["data_type"] == "to_benchmark")
            ].iloc[0]["z"],
        )

    assert abs(z_for(1.0)) < abs(z_for(0.0001))


def test_no_trend_consistency_for_a_monthly_series() -> None:
    """sigma_int is chunked annually; a monthly trend has no counterpart."""
    rng = np.random.default_rng(27)
    times = pd.date_range("2015-01-01", periods=72, freq="MS")
    frames = [
        pd.DataFrame(
            {
                "data_id": "obs_O",
                "data_type": "reference",
                "time": times,
                "tas": rng.normal(0, 1, times.size),
            },
        ),
    ]
    for variant in ("r1i1p1f1", "r2i1p1f1"):
        frames.append(
            pd.DataFrame(
                {
                    "data_id": f"model_M_{variant}",
                    "data_type": "to_benchmark",
                    "time": times,
                    "tas": rng.normal(0, 1, times.size),
                },
            ),
        )
    sources = pd.DataFrame(
        [
            {"id": "obs_O", "name": "O", "category": "observation"},
            {"id": "model_M_r1i1p1f1", "name": "M", "category": "model"},
            {"id": "model_M_r2i1p1f1", "name": "M", "category": "model"},
        ],
    )
    rows = pd.DataFrame(score_raw_output(pd.concat(frames), sources, settings=FAST))
    assert not any(str(v).endswith("_trend_consistency") for v in rows["var_id"])


# ---------------------------------------------------------------------------
# Aggregated scalars (metrics_reference.md §II.1)
# ---------------------------------------------------------------------------

SCALAR = "gmst_warming_level"
OBSERVED_LEVEL = 0.62


def _scalar_tables(
    rng: np.random.Generator,
    *,
    sigma_obs: float | None = 0.06,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A scalar table shaped like ``CB2ComplexDiagnostic._scalar_outputs``.

    One row per (data source, scalar) with every other scalar column NULL,
    no ``time`` axis, and ``reference`` rows carrying the observed value.
    """
    rows: list[pd.DataFrame] = []
    sources: list[dict] = []
    reference = {
        "data_id": ["observation_HadCRUT5"],
        "data_type": ["reference"],
        SCALAR: [OBSERVED_LEVEL],
    }
    rows.append(pd.DataFrame(reference))
    if sigma_obs is not None:
        rows.append(
            pd.DataFrame(
                {
                    "data_id": ["observation_HadCRUT5"],
                    "data_type": ["reference"],
                    f"{SCALAR}{scoring_pass.SCALAR_SIGMA_OBS_SUFFIX}": [sigma_obs],
                },
            ),
        )
    sources.append(
        {"id": "observation_HadCRUT5", "name": "HadCRUT5", "category": "observation"},
    )
    for name, category, variants, data_type, spread in SOURCES:
        if data_type == "reference":
            continue
        exp = "historical"
        for variant in variants:
            data_id = _source_id(category, name, exp, variant)
            rows.append(
                pd.DataFrame(
                    {
                        "data_id": [data_id],
                        "data_type": [data_type],
                        SCALAR: [OBSERVED_LEVEL + rng.normal(0.0, spread)],
                        # a model-only provenance scalar: never scored
                        "warming_level_first_year": [2015.0],
                    },
                ),
            )
            sources.append(
                {"id": data_id, "name": name, "category": category, "variant": variant},
            )
    return (
        pd.concat(rows, ignore_index=True),
        pd.DataFrame(sources),
    )


def test_is_scalar_output_recognises_the_layout() -> None:
    scalars, _ = _scalar_tables(np.random.default_rng(50))
    assert scoring_pass.is_scalar_output(scalars)
    series, _ = _tables(np.random.default_rng(50))
    assert not scoring_pass.is_scalar_output(series)  # has a time axis
    eof, _ = _eof_tables(np.random.default_rng(50))
    assert not scoring_pass.is_scalar_output(eof)
    # A Tier I gate table has no reference rows and is left alone
    assert not scoring_pass.is_scalar_output(
        scalars[scalars["data_type"] != "reference"],
    )


def test_scalar_pass_scores_members_against_the_observed_value() -> None:
    raw, sources = _scalar_tables(np.random.default_rng(51))
    rows = pd.DataFrame(
        scoring_pass.score_scalar_output(
            raw,
            sources,
            diagnostic="realized_warming_level",
        ),
    )
    submission = rows[
        (rows["data_id"] == "MyModel")
        & (rows["data_type"] == "to_benchmark")
        & (rows["var_id"] == SCALAR)
    ].iloc[0]

    assert submission["n_members"] == 3
    assert submission["n_time"] == 1  # one number, one scoring point
    assert submission["t_eff"] == 1
    assert submission["crps"] > 0
    assert np.isnan(submission["crps_ci_lo"])  # no interval from a single point
    assert submission["window"] == "held-out"
    # sigma_obs = the emitted blending term (the tier2.obs_sigma floor is null
    # for this scalar), so the draws bite
    assert submission["sigma_obs"] == pytest.approx(0.06)

    # A model-only provenance column is never scored
    assert "warming_level_first_year" not in set(rows["var_id"])
    # The single-member comparison model is n/a here too
    single = rows[(rows["data_id"] == "CMIP6_C") & (rows["var_id"] == SCALAR)].iloc[0]
    assert np.isnan(single["crps"])
    assert single["reason"] == "single member"
    # ... and E_ref is the leave-one-out median over the M >= 2 comparisons
    assert submission["n_ref_models"] == 2
    assert np.isfinite(submission["skill"])


def test_scalar_pass_writes_a_consistency_row_with_sigma_int() -> None:
    raw, sources = _scalar_tables(np.random.default_rng(52))
    table = scoring_pass.internal_variability_from_raw(
        pd.DataFrame(
            {
                "data_id": ["model_MyModel_historical_r1i1p1f1"],
                "data_type": ["to_benchmark"],
                "window_years_test": [11.0],
                "window_years_long": [76.0],
                "tas_sigma_int_mean_test": [0.09],
                "tas_sigma_int_mean_long": [0.03],
            },
        ),
    )
    rows = pd.DataFrame(
        scoring_pass.score_scalar_output(
            raw,
            sources,
            sigma_internal=table,
            diagnostic="realized_warming_level",
        ),
    )
    consistency = rows[
        (rows["var_id"] == f"{SCALAR}{scoring_pass.SCALAR_CONSISTENCY_SUFFIX}")
        & (rows["data_type"] == "to_benchmark")
    ].iloc[0]
    # `gmst_warming_level` is registered against (tas, mean, test window)
    assert consistency["sigma_internal"] == pytest.approx(0.09)
    assert consistency["sigma_obs"] == pytest.approx(0.06)
    assert consistency["total_sigma"] > 0.09
    assert consistency["value"] == pytest.approx(OBSERVED_LEVEL)
    assert consistency["passes"] in (0.0, 1.0)
    assert np.isnan(consistency["crps"])  # a consistency row is not a score
    assert consistency["window"] == "held-out"


def test_scalar_pass_ignores_a_scalar_with_no_observed_value() -> None:
    """`rsds` has no BSRN DataSource: model-only, so nothing to score."""
    raw, sources = _scalar_tables(np.random.default_rng(53))
    raw = raw.copy()
    raw["pinatubo_rsds_anom"] = np.where(
        raw["data_type"] == "to_benchmark",
        -1.5,
        np.nan,
    )
    rows = pd.DataFrame(scoring_pass.score_scalar_output(raw, sources))
    assert "pinatubo_rsds_anom" not in set(rows["var_id"])


def test_scalar_pass_deduplicates_repeated_reference_rows() -> None:
    """A per-member run of a complex suite re-emits the same reference."""
    raw, sources = _scalar_tables(np.random.default_rng(54))
    doubled = pd.concat([raw, raw[raw["data_type"] == "reference"]], ignore_index=True)
    rows = pd.DataFrame(scoring_pass.score_scalar_output(doubled, sources))
    once = pd.DataFrame(scoring_pass.score_scalar_output(raw, sources))
    assert len(rows) == len(once)
    assert rows.sort_values("data_id")["crps"].to_numpy() == pytest.approx(
        once.sort_values("data_id")["crps"].to_numpy(),
        nan_ok=True,
    )


def test_score_database_scores_a_scalar_schema(tmp_path) -> None:  # noqa: ANN001
    raw, sources = _scalar_tables(np.random.default_rng(55))
    db_path = tmp_path / "ClimateBench2_TierII_events.ddb"
    con = duckdb.connect(str(db_path))
    con.execute('CREATE SCHEMA "realized_warming_level"')
    for table, frame in (("raw_output", raw), ("data_sources", sources)):
        con.register("frame", frame)
        con.execute(
            f'CREATE TABLE "realized_warming_level"."{table}" AS SELECT * FROM frame',
        )
        con.unregister("frame")
    con.close()

    report = score_database(db_path, settings=FAST)
    assert report.diagnostics == ["realized_warming_level"]
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        metrics = con.execute(
            'SELECT * FROM "realized_warming_level"."metrics"',
        ).df()
    finally:
        con.close()
    assert set(metrics["scorer"]) == {SCORER}
    assert SCALAR in set(metrics["var_id"])
    assert set(metrics["window"]) == {"held-out"}


# ---------------------------------------------------------------------------
# Regime (b): EOF coefficient tables
# ---------------------------------------------------------------------------

N_MODES = 6


def _eof_tables(rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A coefficient table shaped like ``ReferenceEOFProjection``'s output."""
    truth = rng.normal(0.0, 1.0, N_MODES)
    rows, sources = [], []
    for name, category, variants, data_type, spread in SOURCES:
        exp = "" if data_type == "reference" else "historical"
        for variant in variants:
            data_id = _source_id(category, name, exp, variant)
            values = (
                truth
                if data_type == "reference"
                else truth + rng.normal(0.0, spread, N_MODES)
            )
            rows.append(
                pd.DataFrame(
                    {
                        "data_id": data_id,
                        "data_type": data_type,
                        "var_id": "tas",
                        "mode": np.arange(1, N_MODES + 1, dtype=float),
                        "coefficient": values,
                        "explained_variance": np.full(N_MODES, 1.0 / N_MODES),
                        "sigma_pre2015": np.ones(N_MODES),
                    },
                ),
            )
            sources.append(
                {"id": data_id, "name": name, "category": category, "variant": variant},
            )
    return pd.concat(rows, ignore_index=True), pd.DataFrame(sources)


def test_is_eof_output_recognises_the_coefficient_table() -> None:
    raw, _ = _eof_tables(np.random.default_rng(30))
    assert scoring_pass.is_eof_output(raw)
    series, _ = _tables(np.random.default_rng(30))
    assert not scoring_pass.is_eof_output(series)


def test_eof_pass_scores_the_mean_over_coefficients() -> None:
    raw, sources = _eof_tables(np.random.default_rng(31))
    rows = pd.DataFrame(scoring_pass.score_eof_output(raw, sources, settings=FAST))
    submission = rows[
        (rows["data_id"] == "MyModel") & (rows["data_type"] == "to_benchmark")
    ].iloc[0]

    assert submission["n_members"] == 3
    assert submission["n_time"] == N_MODES  # the retained modes are the sample
    assert submission["t_eff"] == N_MODES  # ... and the effective sample size
    assert submission["block_length"] == 1.0  # orthogonal: no blocks
    assert np.isnan(submission["r1"])  # no ordering along the mode index
    assert submission["crps"] > 0
    assert submission["crps_ci_lo"] <= submission["crps"] <= submission["crps_ci_hi"]

    # The score really is the equal-weight mean of the per-coefficient CRPS
    from climatebench2 import scoring as engine

    members = np.vstack(
        [
            raw[(raw["data_id"] == i)].sort_values("mode")["coefficient"].to_numpy()
            for i in sorted(
                sources[
                    (sources["name"] == "MyModel") & (sources["category"] == "model")
                ]["id"],
            )
        ],
    )
    obs = (
        raw[raw["data_type"] == "reference"]
        .sort_values("mode")["coefficient"]
        .to_numpy()
    )
    assert submission["crps"] == pytest.approx(engine.crps_fair(members, obs).mean())


def test_eof_pass_reuses_the_leave_one_out_cmip6_median() -> None:
    raw, sources = _eof_tables(np.random.default_rng(32))
    rows = pd.DataFrame(scoring_pass.score_eof_output(raw, sources, settings=FAST))
    per_model = dict(
        zip(
            rows[(rows["data_type"] == "other") & rows["crps"].notna()]["data_id"],
            rows[(rows["data_type"] == "other") & rows["crps"].notna()]["crps"],
            strict=True,
        ),
    )
    submission = rows[
        (rows["data_id"] == "MyModel") & (rows["data_type"] == "to_benchmark")
    ].iloc[0]
    expected = float(np.median([per_model["CMIP6_A"], per_model["CMIP6_B"]]))
    assert submission["e_ref"] == pytest.approx(expected)
    assert submission["skill"] == pytest.approx(1.0 - submission["crps"] / expected)
    # The single-member comparison model is n/a here too
    single = rows[rows["data_id"] == "CMIP6_C"].iloc[0]
    assert np.isnan(single["crps"])
    assert single["reason"] == "single member"


def test_score_database_scores_an_eof_schema(tmp_path) -> None:  # noqa: ANN001
    raw, sources = _eof_tables(np.random.default_rng(33))
    db_path = tmp_path / "ClimateBench2_TierII.ddb"
    con = duckdb.connect(str(db_path))
    con.execute('CREATE SCHEMA "eof_projection"')
    for table, frame in (("raw_output", raw), ("data_sources", sources)):
        con.register("frame", frame)
        con.execute(f'CREATE TABLE "eof_projection"."{table}" AS SELECT * FROM frame')
        con.unregister("frame")
    con.close()

    report = score_database(db_path, settings=FAST)
    assert report.diagnostics == ["eof_projection"]
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        metrics = con.execute('SELECT * FROM "eof_projection"."metrics"').df()
    finally:
        con.close()
    assert set(metrics["scorer"]) == {SCORER}
    assert set(metrics["var_id"]) == {"tas"}


def test_eof_pattern_scaling_is_a_reason_row_until_pr_44() -> None:
    """Regime (b)'s pattern-scaling baseline needs CMIP6 baseline-window maps."""
    raw, sources = _eof_tables(np.random.default_rng(34))
    rows = pd.DataFrame(scoring_pass.score_eof_output(raw, sources, settings=FAST))
    row = rows[rows["data_id"] == scoring_pass.PATTERN_SCALING_DATA_ID].iloc[0]
    assert np.isnan(row["crps"])
    assert "#44" in row["reason"]
    assert row["data_type"] == "baseline"
    # ... and it does not disturb E_ref, which is still the CMIP6 median
    model = rows[(rows["data_id"] == "MyModel") & (rows["data_type"] == "to_benchmark")]
    assert np.isfinite(model["e_ref"].iloc[0])


# ---------------------------------------------------------------------------
# Regime (a) is scored on ANOMALIES about each source's own baseline window
# ---------------------------------------------------------------------------


def _offset_tables(
    offset: float,
    seed: int,
    *,
    years: list[int] | None = None,
    monthly: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A reference and a 3-member model that differ only by ``offset``.

    The model tracks the observed trajectory exactly and sits ``offset``
    above it — a pure mean-state bias, which is precisely what the paper
    says regime (a) must NOT be measuring.
    """
    rng = np.random.default_rng(seed)
    years = years or ALL_YEARS
    if monthly:
        times = pd.date_range(f"{years[0]}-01-01", f"{years[-1]}-12-01", freq="MS")
        signal = 0.02 * (times.year - 1985) + 5.0 * np.sin(
            2 * np.pi * (times.month - 1) / 12,
        )
    else:
        times = _times(years)
        signal = 0.02 * (np.array(years, dtype=float) - 1985.0)
    truth = signal + rng.normal(0.0, 0.1, times.size)
    frames = [
        pd.DataFrame(
            {
                "data_id": "observation_OBS",
                "data_type": "reference",
                "time": times,
                "tas": truth,
            },
        ),
    ]
    rows = [{"id": "observation_OBS", "name": "OBS", "category": "observation"}]
    for variant in ("r1i1p1f1", "r2i1p1f1", "r3i1p1f1"):
        data_id = f"model_MyModel_historical_{variant}"
        frames.append(
            pd.DataFrame(
                {
                    "data_id": data_id,
                    "data_type": "to_benchmark",
                    "time": times,
                    "tas": truth + offset + rng.normal(0.0, 0.05, times.size),
                },
            ),
        )
        rows.append({"id": data_id, "name": "MyModel", "category": "model"})
    return pd.concat(frames, ignore_index=True), pd.DataFrame(rows)


def _submission(rows: pd.DataFrame) -> pd.Series:
    return rows[
        (rows["data_id"] == "MyModel") & (rows["data_type"] == "to_benchmark")
    ].iloc[0]


def test_a_constant_offset_is_removed_before_scoring() -> None:
    """The defect this regime exists to avoid: a mean-state bias as the score."""
    raw, sources = _offset_tables(5.0, 100)

    absolute = _submission(
        pd.DataFrame(score_raw_output(raw, sources, settings=FAST)),
    )
    anomaly = _submission(
        pd.DataFrame(
            score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
        ),
    )
    # Scored absolutely, the 5 K offset IS the score ...
    assert absolute["crps"] == pytest.approx(5.0, abs=0.2)
    # ... and once both sides are anomalies about their own 1985-2014 mean,
    # a model that tracks the observed trajectory scores ~ 0.
    assert anomaly["crps"] < 0.1
    assert anomaly["reason"] == ""


def test_n_time_counts_only_the_post_2015_steps() -> None:
    """The baseline years define the anomaly; they are never scored."""
    raw, sources = _offset_tables(0.3, 101)
    rows = pd.DataFrame(
        score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
    )
    assert _submission(rows)["n_time"] == len(TEST_YEARS)
    # The climatology baseline is scored on exactly the same steps
    clim = rows[rows["data_id"] == CLIMATOLOGY_DATA_ID].iloc[0]
    assert clim["n_time"] == len(TEST_YEARS)
    assert clim["n_members"] == len(BASELINE_YEARS)


def test_monthly_anomalies_are_taken_per_calendar_month() -> None:
    raw, sources = _offset_tables(3.0, 102, years=ALL_YEARS, monthly=True)
    reference = raw[raw["data_type"] == "reference"]
    climatology = scoring_pass.baseline_climatology(
        reference["time"],
        reference["tas"].to_numpy(float),
        monthly=True,
    )
    assert set(climatology) == set(range(1, 13))
    # The 5 K seasonal cycle is in the climatology, not in the anomaly
    assert max(climatology.values()) - min(climatology.values()) > 9.0
    anomaly = scoring_pass.apply_climatology(
        reference["time"],
        reference["tas"].to_numpy(float),
        climatology,
        monthly=True,
    )
    assert np.nanstd(anomaly) < 1.0

    rows = pd.DataFrame(
        score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
    )
    submission = _submission(rows)
    assert submission["n_time"] == 12 * len(TEST_YEARS)
    assert submission["block_length"] == FAST["block_length_monthly"]
    # The seasonal cycle and the 3 K offset are both gone
    assert submission["crps"] < 0.1


def test_a_source_without_a_baseline_window_gets_a_reason_row() -> None:
    """Never a silent fall back to absolute values (the whole point)."""
    raw, sources = _offset_tables(5.0, 103)
    years = pd.to_datetime(raw["time"]).dt.year
    # The reference keeps its full record; the model starts in 2015, as an
    # SSP-only submission (or an older database) would.
    cut = raw[(raw["data_type"] == "reference") | (years >= min(TEST_YEARS))]
    rows = pd.DataFrame(
        score_raw_output(cut, sources, settings=FAST, diagnostic=DIAGNOSTIC),
    )
    submission = _submission(rows)
    assert np.isnan(submission["crps"])
    assert submission["reason"] == scoring_pass.NO_BASELINE_REASON


def test_a_reference_without_a_baseline_window_leaves_the_row_visible() -> None:
    """No target, no score — but the model does not vanish from the card."""
    raw, sources = _offset_tables(5.0, 104)
    years = pd.to_datetime(raw["time"]).dt.year
    cut = raw[years >= min(TEST_YEARS)]
    rows = pd.DataFrame(
        score_raw_output(cut, sources, settings=FAST, diagnostic=DIAGNOSTIC),
    )
    submission = _submission(rows)
    assert np.isnan(submission["crps"])
    assert scoring_pass.NO_BASELINE_REASON in submission["reason"]
    # The climatology baseline says the same thing rather than disappearing
    clim = rows[rows["data_id"] == CLIMATOLOGY_DATA_ID].iloc[0]
    assert scoring_pass.NO_BASELINE_REASON in clim["reason"]


def test_a_test_window_only_database_rescores_without_crashing(tmp_path) -> None:  # noqa: ANN001
    """An existing .ddb holds post-2015 rows only: unscored WITH A REASON."""
    raw, sources = _offset_tables(5.0, 105)
    years = pd.to_datetime(raw["time"]).dt.year
    cut = raw[years >= min(TEST_YEARS)].reset_index(drop=True)
    db_path = tmp_path / "ClimateBench2_TierII.ddb"
    con = duckdb.connect(str(db_path))
    con.execute(f'CREATE SCHEMA "{DIAGNOSTIC}"')
    for table, frame in (("raw_output", cut), ("data_sources", sources)):
        con.register("frame", frame)
        con.execute(f'CREATE TABLE "{DIAGNOSTIC}"."{table}" AS SELECT * FROM frame')
        con.unregister("frame")
    con.close()

    first = score_database(db_path, settings=FAST)
    second = score_database(db_path, settings=FAST)  # --rescore is idempotent
    assert first.rows == second.rows
    metrics = _metrics(db_path)
    assert metrics["crps"].isna().all()
    assert metrics["reason"].str.contains(scoring_pass.NO_BASELINE_REASON).all()
    assert set(metrics["window"]) == {scoring_pass.WINDOW_HELD_OUT}


def test_the_climatology_baseline_lives_in_the_same_anomaly_space() -> None:
    """"No change since 1985-2014" — so its forecast is ~ zero anomaly."""
    raw, sources = _offset_tables(0.0, 106)
    reference = raw[raw["data_type"] == "reference"]
    context = scoring_pass.anomalise_raw_output(
        raw,
        scoring_pass.baseline_records(
            reference.assign(data_type=scoring_pass.BASELINE_ANNUAL_DATA_TYPE),
        ),
        ["tas"],
    )
    recorded = context.baseline[("tas", "annual")]
    window = recorded[pd.to_datetime(recorded["time"]).dt.year <= max(BASELINE_YEARS)]
    assert float(window["tas"].mean()) == pytest.approx(0.0, abs=1e-9)
    # ... and the scored rows only cover the test window
    assert pd.to_datetime(context.raw["time"]).dt.year.min() == min(TEST_YEARS)


def test_pattern_scaling_is_not_double_differenced() -> None:
    """The EBM re-anchors to the baseline, so an anomaly input is fine."""
    raw, sources = _offset_tables(0.0, 107)
    rows = pd.DataFrame(
        score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
    )
    row = _pattern_row(rows)
    assert row["reason"] == ""
    assert np.isfinite(row["crps"])
    low, high = get_threshold("tier2.pattern_scaling.lambda_bounds")
    assert low <= row["value"] <= high


def test_only_the_listed_suite_entries_are_scored_as_anomalies() -> None:
    """Tier I and the daily suite's full-record series keep absolute values."""
    assert scoring_pass.windows.scores_anomalies("annual_mean_timeseries")
    assert scoring_pass.windows.scores_anomalies("sst")
    assert not scoring_pass.windows.scores_anomalies("tas_annual_max")
    assert not scoring_pass.windows.scores_anomalies("enso_gate")
    assert not scoring_pass.windows.scores_anomalies("")


def test_a_float32_raw_output_column_is_anomalised(tmp_path) -> None:  # noqa: ANN001, ARG001
    """ClimateEval writes float32; pandas refuses a float64 anomaly into one.

    Found on the real Tier II database (`tas` is float32 there), not by any
    synthetic fixture: the assignment raised
    ``TypeError: Invalid value '[...]' for dtype 'float32'`` and took the
    whole scoring pass down.
    """
    raw, sources = _offset_tables(5.0, 108)
    raw = raw.astype({"tas": "float32"})
    rows = pd.DataFrame(
        score_raw_output(raw, sources, settings=FAST, diagnostic=DIAGNOSTIC),
    )
    submission = _submission(rows)
    assert submission["reason"] == ""
    assert submission["crps"] < 0.1
    assert submission["n_time"] == len(TEST_YEARS)
