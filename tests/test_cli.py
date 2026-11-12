"""Tests for the `climatebench2 score` data paths (no data required).

The CLI decides, per suite, *what shape* of data a suite gets and *which time
window* it is cut to (metrics_reference.md §II.0, gap item 4). These tests
cover that plumbing — the registry, the derived Tier II test window, member
discovery and argument parsing — without loading a single cube.
"""

from __future__ import annotations

import datetime as dt

import pytest

from climatebench2 import windows as cb2_windows
from climatebench2._cli import (
    DEFAULT_SUITES,
    DEFAULT_TIMERANGE,
    SUITE_REGISTRY,
    SuiteSpec,
    _parse_members,
    _resolve_suite,
    build_parser,
    default_tier2_timerange,
    discover_members,
    suite_timerange,
)
from climatebench2._thresholds import get_threshold


# ---------------------------------------------------------------------------
# Suite registry and windows
# ---------------------------------------------------------------------------


def test_registry_covers_every_default_suite() -> None:
    for suite_name in DEFAULT_SUITES:
        assert suite_name in SUITE_REGISTRY, suite_name


def test_registry_shapes_match_the_protocol() -> None:
    """Tier I/events/paleo take experiment dicts; Tier II takes model cubes."""
    assert SUITE_REGISTRY["ClimateBench2_TierI"].shape == "experiments"
    assert SUITE_REGISTRY["ClimateBench2_TierII_events"].shape == "experiments"
    assert SUITE_REGISTRY["ClimateBench2_TierIII"].shape == "experiments"

    # ENSO amplitude/spectrum: the piControl experiment, in full (paper I.5)
    variability = SUITE_REGISTRY["ClimateBench2_TierI_variability"]
    assert (variability.shape, variability.source, variability.window) == (
        "cubes",
        "picontrol",
        "full",
    )
    assert not variability.per_member

    monthly = SUITE_REGISTRY["ClimateBench2_TierII"]
    assert (monthly.shape, monthly.source, monthly.window) == (
        "cubes",
        "model",
        "tier2",
    )
    # The daily suite is the exception: its extremes, PDFs and diurnal
    # climatologies are defined over the FULL historical record (work package
    # 6b), so it is in-sample and takes no window cut.
    daily = SUITE_REGISTRY["ClimateBench2_TierII_daily"]
    assert (daily.shape, daily.source, daily.window) == ("cubes", "model", "full")
    for spec in (monthly, daily):
        assert spec.per_member  # cube suites run once per ensemble member


def test_only_the_tier2_suites_run_per_member() -> None:
    """Tier II scalars are scored across the ensemble; Tier I/III are not."""
    assert SUITE_REGISTRY["ClimateBench2_TierII_events"].per_member
    assert not SUITE_REGISTRY["ClimateBench2_TierI"].per_member
    assert not SUITE_REGISTRY["ClimateBench2_TierIII"].per_member


def test_default_tier2_window_is_derived_from_thresholds() -> None:
    """<test_window_start>0101 / <last complete year>1231."""
    start = int(get_threshold("tier2.test_window_start"))
    assert start == 2015
    assert default_tier2_timerange(dt.date(2026, 9, 14)) == "20150101/20251231"
    assert default_tier2_timerange(dt.date(2031, 1, 1)) == "20150101/20301231"
    # Never inverted, even before the window has completed a year
    assert default_tier2_timerange(dt.date(2015, 6, 1)) == "20150101/20151231"


def test_suite_timerange_per_shape_and_override() -> None:
    tier2 = SUITE_REGISTRY["ClimateBench2_TierII"]
    variability = SUITE_REGISTRY["ClimateBench2_TierI_variability"]
    tier1 = SUITE_REGISTRY["ClimateBench2_TierI"]
    other = SuiteSpec(shape="cubes")  # unregistered cube suite

    assert suite_timerange(tier2) == default_tier2_timerange()
    assert suite_timerange(other) == DEFAULT_TIMERANGE
    # Experiment-based suites and the piControl suite are never cut
    assert suite_timerange(tier1) is None
    assert suite_timerange(variability) is None

    # --timerange overrides the cube windows, but not a control's own calendar
    assert suite_timerange(tier2, "19790101/20141231") == "19790101/20141231"
    assert suite_timerange(other, "19790101/20141231") == "19790101/20141231"
    assert suite_timerange(variability, "19790101/20141231") is None
    assert suite_timerange(tier1, "19790101/20141231") is None


def test_tier2_window_starts_after_the_climatology_baseline() -> None:
    """The test window must not overlap the pre-test baseline (1985–2014)."""
    _y0, y1 = get_threshold("tier2.climatology_baseline_period")
    assert int(get_threshold("tier2.test_window_start")) == int(y1) + 1


# ---------------------------------------------------------------------------
# Ensemble members
# ---------------------------------------------------------------------------


def test_parse_members_and_errors() -> None:
    parsed = _parse_members(["r1i1p1f1=/data/r1", "r2i1p1f1=/data/r2"])
    assert list(parsed) == ["r1i1p1f1", "r2i1p1f1"]
    assert str(parsed["r2i1p1f1"]) == "/data/r2"
    with pytest.raises(SystemExit, match="LABEL=PATH"):
        _parse_members(["r1i1p1f1"])


def _drs_tree(root, experiment: str, variants: list[str]) -> None:  # noqa: ANN001
    """A CMIP6-like DRS tree: <exp>/<variant>/Amon/tas/gn/v1/*.nc."""
    for variant in variants:
        leaf = root / experiment / variant / "Amon" / "tas" / "gn" / "v1"
        leaf.mkdir(parents=True, exist_ok=True)
        (leaf / f"tas_Amon_M_{experiment}_{variant}_gn_185001-201412.nc").touch()


def test_discover_members_finds_sibling_variants(tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "MyModel"
    _drs_tree(root, "historical", ["r1i1p1f1", "r2i1p1f1", "r10i1p1f1"])
    found = discover_members(root)
    assert list(found) == ["r10i1p1f1", "r1i1p1f1", "r2i1p1f1"]  # sorted by name
    assert found["r2i1p1f1"] == root / "historical" / "r2i1p1f1"


def test_discover_members_ignores_single_member_and_flat_trees(
    tmp_path,  # noqa: ANN001
) -> None:
    single = tmp_path / "Single"
    _drs_tree(single, "historical", ["r1i1p1f1"])
    assert discover_members(single) == {}

    flat = tmp_path / "Flat"
    flat.mkdir()
    (flat / "tas_Amon_M_historical_r1i1p1f1_gn_185001-201412.nc").touch()
    assert discover_members(flat) == {}

    assert discover_members(tmp_path / "missing") == {}


def test_discover_members_does_not_mix_experiments(tmp_path) -> None:  # noqa: ANN001
    """Variant names repeat across experiments; only one parent is used."""
    root = tmp_path / "MyModel"
    _drs_tree(root, "historical", ["r1i1p1f1", "r2i1p1f1", "r3i1p1f1"])
    _drs_tree(root, "piControl", ["r1i1p1f1"])
    found = discover_members(root)
    assert list(found) == ["r1i1p1f1", "r2i1p1f1", "r3i1p1f1"]
    assert all(path.parent.name == "historical" for path in found.values())


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_score_arguments_parse() -> None:
    args = build_parser().parse_args(
        [
            "score",
            "/path/model",
            "--name",
            "MyModel",
            "--member",
            "r1i1p1f1=/path/r1",
            "--member",
            "r2i1p1f1=/path/r2",
            "--not-applicable",
            "geostrophic_balance",
            "--experiment",
            "picontrol=/path/pi",
        ],
    )
    assert args.member == ["r1i1p1f1=/path/r1", "r2i1p1f1=/path/r2"]
    assert args.not_applicable == ["geostrophic_balance"]
    assert args.experiment == ["picontrol=/path/pi"]
    # No global window any more: each suite gets its protocol default
    assert args.timerange is None
    assert args.suite is None


def test_timerange_is_an_explicit_override() -> None:
    args = build_parser().parse_args(
        ["score", "/path/model", "--timerange", "20150101/20241231"],
    )
    assert args.timerange == "20150101/20241231"


def test_tier2_events_suite_is_packaged_and_default() -> None:
    resolved = _resolve_suite("ClimateBench2_TierII_events")
    assert resolved.endswith("ClimateBench2_TierII_events.yml")
    assert "ClimateBench2_TierII_events" in DEFAULT_SUITES


# ---------------------------------------------------------------------------
# End-to-end plumbing: which data, which window, which member, per suite
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_score(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """Run `climatebench2 score` with the data layer stubbed out."""
    pytest.importorskip("climateeval")

    import climateeval._loader as loader
    from climateeval.suites import Suite

    from climatebench2._cli import main

    loads: list[tuple[str, str | None]] = []
    runs: list[dict] = []

    def fake_load(path, *, timerange=None, **_kwargs):  # noqa: ANN001, ANN202
        loads.append((str(path), timerange))
        return [f"cubes:{path}:{timerange}"]

    seen: set[tuple[str, str]] = set()

    def fake_get_database(self, data, information, **kwargs):  # noqa: ANN001, ANN202
        # A per-member cube suite runs one diagnostic at a time
        # (`climatebench2.shared_sources.run_members`), so each (suite,
        # member) arrives once per suite entry; record it once, which is
        # what the old one-call-per-member loop produced.
        key = (self.name, information.variant)
        if key in seen:
            return None
        seen.add(key)
        runs.append(
            {
                "suite": self.name,
                "suite_class": type(self).__name__,
                "data": data,
                "variant": information.variant,
                "append": kwargs.get("append", False),
                "timerange": next(
                    (
                        variable.timerange
                        for diag in self._get_diagnostics().values()
                        for variable in getattr(diag, "_variables", ())
                    ),
                    None,
                ),
            },
        )
        return None

    monkeypatch.setattr(loader, "load_cmor_dir", fake_load)
    monkeypatch.setattr(Suite, "get_database", fake_get_database)

    def run(argv: list[str]) -> tuple[list, list]:
        seen.clear()
        main(["score", *argv, "--out", str(tmp_path / "out")])
        return loads, runs

    return run


def test_score_feeds_each_suite_its_protocol_window(  # noqa: PLR0915
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
) -> None:
    model = tmp_path / "MyModel"
    _drs_tree(model, "historical", ["r1i1p1f1", "r2i1p1f1"])
    picontrol = tmp_path / "piControl"
    picontrol.mkdir()

    loads, runs = recorded_score(
        [
            str(model),
            "--name",
            "MyModel",
            "--experiment",
            f"picontrol={picontrol}",
            "--suite",
            "ClimateBench2_TierI",
            "--suite",
            "ClimateBench2_TierI_variability",
            "--suite",
            "ClimateBench2_TierII",
        ],
    )
    by_suite = {run["suite"]: run for run in runs}

    # Tier I: the experiment dict, historical loaded in FULL (no timerange),
    # and run once for the model (not once per member).
    tier1 = by_suite["ClimateBench2_TierI"]
    assert set(tier1["data"]) == {"historical", "picontrol"}
    assert tier1["data"]["historical"] == [
        f"cubes:{model / 'historical' / 'r1i1p1f1'}:None",
    ]
    assert (str(model / "historical" / "r1i1p1f1"), None) in loads

    # Variability: the piControl experiment, in full — not the model cubes
    variability = by_suite["ClimateBench2_TierI_variability"]
    assert variability["data"] == [f"cubes:{picontrol}:None"]
    assert variability["timerange"] == "*"  # no cut

    # Tier II: once per member, appending into the one database. The cubes
    # are loaded from the BASELINE window's first year, because regime (a)
    # scores anomalies about each source's own 1985-2014 climatology; the
    # suite's own entries then take either that extended window (the scored
    # time series, which is what `run["timerange"]` reads back) or the
    # nominal test window (the maps, the EOF basis, the annual cycles).
    tier2_runs = [run for run in runs if run["suite"] == "ClimateBench2_TierII"]
    window = default_tier2_timerange()
    extended = cb2_windows.extend_to_baseline(window)
    assert extended.startswith("1985") and extended.endswith(window.split("/")[1])
    assert len(tier2_runs) == 2
    assert [run["variant"] for run in tier2_runs] == ["r1i1p1f1", "r2i1p1f1"]
    assert [run["append"] for run in tier2_runs] == [False, True]
    assert {run["timerange"] for run in tier2_runs} == {extended}
    assert (str(model / "historical" / "r2i1p1f1"), extended) in loads


def test_score_timerange_overrides_only_the_cube_suites(
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
) -> None:
    model = tmp_path / "Model"
    model.mkdir()
    picontrol = tmp_path / "piControl"
    picontrol.mkdir()

    _loads, runs = recorded_score(
        [
            str(model),
            "--experiment",
            f"picontrol={picontrol}",
            "--timerange",
            "19790101/20141231",
            "--suite",
            "ClimateBench2_TierII",
            "--suite",
            "ClimateBench2_TierI_variability",
        ],
    )
    by_suite = {run["suite"]: run for run in runs}
    assert by_suite["ClimateBench2_TierII"]["timerange"] == "19790101/20141231"
    assert by_suite["ClimateBench2_TierI_variability"]["timerange"] == "*"


def test_score_falls_back_to_model_cubes_without_picontrol(
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
    capsys,  # noqa: ANN001
) -> None:
    model = tmp_path / "Model"
    model.mkdir()

    _loads, runs = recorded_score(
        [str(model), "--suite", "ClimateBench2_TierI_variability"],
    )
    assert runs[0]["data"] == [f"cubes:{model}:None"]
    assert "wants the piControl experiment" in capsys.readouterr().err


def test_score_skips_a_complex_suite_with_no_runnable_gate(
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
    capsys,  # noqa: ANN001
) -> None:
    """Tier III needs paleo experiments; Tier II events only needs historical."""
    model = tmp_path / "Model"
    model.mkdir()

    _loads, runs = recorded_score(
        [
            str(model),
            "--suite",
            "ClimateBench2_TierIII",
            "--suite",
            "ClimateBench2_TierII_events",
        ],
    )
    assert [run["suite"] for run in runs] == ["ClimateBench2_TierII_events"]
    assert "Skipping suite 'ClimateBench2_TierIII'" in capsys.readouterr().err
    assert set(runs[0]["data"]) == {"historical"}


def test_explicit_members_win_over_discovery(
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
) -> None:
    model = tmp_path / "MyModel"
    _drs_tree(model, "historical", ["r1i1p1f1", "r2i1p1f1"])
    other = tmp_path / "extra_member"
    other.mkdir()

    _loads, runs = recorded_score(
        [str(model), "--member", f"r9i1p1f1={other}", "--suite", "ClimateBench2_TierII"],
    )
    assert [run["variant"] for run in runs] == ["r9i1p1f1"]
    loaded = cb2_windows.extend_to_baseline(default_tier2_timerange())
    assert runs[0]["data"] == [f"cubes:{other}:{loaded}"]


def test_tier2_events_runs_once_per_member_with_its_own_historical(
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
) -> None:
    """Aggregated Tier II scalars need every member, not just the first."""
    model = tmp_path / "MyModel"
    _drs_tree(model, "historical", ["r1i1p1f1", "r2i1p1f1"])

    _loads, runs = recorded_score(
        [
            str(model),
            "--name",
            "MyModel",
            "--suite",
            "ClimateBench2_TierII_events",
            "--suite",
            "ClimateBench2_TierIII",
            "--suite",
            "ClimateBench2_TierI",
        ],
    )
    events = [r for r in runs if r["suite"] == "ClimateBench2_TierII_events"]
    assert [r["variant"] for r in events] == ["r1i1p1f1", "r2i1p1f1"]
    assert [r["append"] for r in events] == [False, True]
    # Each run sees THAT member's own record under the `historical` key
    for run, variant in zip(events, ("r1i1p1f1", "r2i1p1f1"), strict=True):
        assert run["data"]["historical"] == [
            f"cubes:{model / 'historical' / variant}:None",
        ]

    # Tier I stays a once-per-model suite (a gate is a model property)
    tier1 = [r for r in runs if r["suite"] == "ClimateBench2_TierI"]
    assert len(tier1) == 1
    assert tier1[0]["variant"] == "r1i1p1f1"


def test_per_member_cube_suites_share_sources_unless_no_cache(
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
) -> None:
    """The cube suites run entry by entry (sources loaded once per entry).

    ``climatebench2.shared_sources.run_members`` hands ClimateEval one
    single-entry ``EntrySuite`` per diagnostic; ``--no-cache`` restores the
    member-by-member ``Suite.get_database`` loop, and the experiment suites
    never leave it.
    """
    model = tmp_path / "MyModel"
    _drs_tree(model, "historical", ["r1i1p1f1", "r2i1p1f1"])
    suites = ["--suite", "ClimateBench2_TierII", "--suite", "ClimateBench2_TierII_events"]

    _loads, runs = recorded_score([str(model), "--name", "MyModel", *suites])
    classes = {(r["suite"], r["variant"]): r["suite_class"] for r in runs}
    assert classes["ClimateBench2_TierII", "r1i1p1f1"] == "EntrySuite"
    assert classes["ClimateBench2_TierII", "r2i1p1f1"] == "EntrySuite"
    assert classes["ClimateBench2_TierII_events", "r2i1p1f1"] == "Suite"
    tier2 = [r for r in runs if r["suite"] == "ClimateBench2_TierII"]
    assert [r["append"] for r in tier2] == [False, True]

    runs.clear()
    recorded_score([str(model), "--name", "MyModel", *suites, "--no-cache"])
    assert {r["suite_class"] for r in runs} == {"Suite"}


def test_no_cache_flag_parses() -> None:
    args = build_parser().parse_args(["score", "MODEL"])
    assert args.no_cache is False
    args = build_parser().parse_args(["score", "MODEL", "--no-cache"])
    assert args.no_cache is True


def test_explicit_historical_experiment_collapses_the_per_member_runs(
    tmp_path,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
    capsys,  # noqa: ANN001
) -> None:
    """One pinned record for every member would only duplicate rows."""
    model = tmp_path / "MyModel"
    _drs_tree(model, "historical", ["r1i1p1f1", "r2i1p1f1"])
    pinned = tmp_path / "pinned"
    pinned.mkdir()

    _loads, runs = recorded_score(
        [
            str(model),
            "--experiment",
            f"historical={pinned}",
            "--suite",
            "ClimateBench2_TierII_events",
        ],
    )
    assert len(runs) == 1
    assert runs[0]["data"]["historical"] == [f"cubes:{pinned}:None"]
    assert "runs per member" in capsys.readouterr().err


def test_missing_member_path_is_an_error() -> None:
    from climatebench2._cli import main

    with pytest.raises(SystemExit, match="Member path not found"):
        main(["score", ".", "--member", "r1i1p1f1=/definitely/not/here"])


# ---------------------------------------------------------------------------
# The Tier II scoring pass (gap item 3)
# ---------------------------------------------------------------------------


def test_score_runs_the_scoring_pass_unless_disabled(
    tmp_path,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
    recorded_score,  # noqa: ANN001
) -> None:
    """`score` finishes by stacking the ingested members into fair CRPS."""
    scored: list[list] = []
    monkeypatch.setattr(
        "climatebench2._cli.run_scoring_pass",
        lambda paths: scored.append(list(paths)),
    )
    model = tmp_path / "Model"
    model.mkdir()
    recorded_score([str(model), "--suite", "ClimateBench2_TierII"])
    assert len(scored) == 1
    assert [p.name for p in scored[0]] == ["ClimateBench2_TierII.ddb"]

    scored.clear()
    recorded_score([str(model), "--suite", "ClimateBench2_TierII", "--no-score"])
    assert scored == []


def test_leaderboard_rescore_flag_reruns_the_pass(
    tmp_path,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    from climatebench2._cli import main

    db_path = tmp_path / "ClimateBench2_TierII.ddb"
    db_path.write_bytes(b"")
    scored: list[list] = []
    monkeypatch.setattr(
        "climatebench2._cli.run_scoring_pass",
        lambda paths: scored.append(list(paths)),
    )
    monkeypatch.setattr(
        "climatebench2.leaderboard.build_scores_table",
        lambda paths: __import__("pandas").DataFrame({"model": ["M"]}),
    )
    main(["leaderboard", str(db_path), "--rescore", "--csv", str(tmp_path / "s.csv")])
    assert [p.name for p in scored[0]] == ["ClimateBench2_TierII.ddb"]

    scored.clear()
    main(["leaderboard", str(db_path), "--csv", str(tmp_path / "s.csv")])
    assert scored == []


def test_leaderboard_regate_flag_reruns_the_gate_pass(
    tmp_path,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    from climatebench2._cli import main

    db_path = tmp_path / "ClimateBench2_TierI.ddb"
    db_path.write_bytes(b"")
    regated: list[list] = []
    monkeypatch.setattr(
        "climatebench2._cli.run_regate_pass",
        lambda paths: regated.append(list(paths)),
    )
    monkeypatch.setattr(
        "climatebench2.leaderboard.build_scores_table",
        lambda paths: __import__("pandas").DataFrame({"model": ["M"]}),
    )
    main(["leaderboard", str(db_path), "--regate", "--csv", str(tmp_path / "s.csv")])
    assert [p.name for p in regated[0]] == ["ClimateBench2_TierI.ddb"]

    regated.clear()
    main(["leaderboard", str(db_path), "--csv", str(tmp_path / "s.csv")])
    assert regated == []


def test_member_keys_follow_each_member_in_a_per_member_experiment_suite(
    recorded_score,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    """`Protocol.member_keys`: the member's own output under a second key."""
    from dataclasses import replace

    from climatebench2.protocol import CB2_PROTOCOL, use_protocol

    model = tmp_path / "Model"
    members = {}
    for label in ("r1i1p1f1", "r2i1p1f1"):
        (model / label).mkdir(parents=True)
        (model / label / "x.nc").touch()
        members[label] = model / label
    argv = [
        str(model),
        "--suite",
        "ClimateBench2_TierII_events",
        "--no-score",
        *[part for label, path in members.items() for part in ("--member", f"{label}={path}")],
    ]

    with use_protocol(replace(CB2_PROTOCOL, member_keys=("day",))):
        _loads, runs = recorded_score(argv)
    events = [run for run in runs if run["suite"] == "ClimateBench2_TierII_events"]
    assert [run["variant"] for run in events] == list(members)
    for run, path in zip(events, members.values()):
        # Both keys hold THIS member's record, not the first member's.
        assert str(path) in str(run["data"]["historical"])
        assert str(path) in str(run["data"]["day"])

    # Without member_keys (ClimateBench2 itself) there is no such key.
    runs.clear()
    _loads, runs = recorded_score(argv)
    assert all("day" not in run["data"] for run in runs)
