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
