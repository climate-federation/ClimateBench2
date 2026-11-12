"""Tests for the protocol plug-in (CB-3).

A toy protocol is defined in a temporary package (one suite YAML, one gate
class, its own thresholds overlay) and put on ``sys.path``. Everything that is
hard-wired to ClimateBench2 must follow the *active* protocol inside
``use_protocol(...)`` and be ClimateBench2's again outside it.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

pytest.importorskip("climateeval")

from climatebench2 import _cli  # noqa: E402
from climatebench2._thresholds import BASE_LAYER, get_threshold  # noqa: E402
from climatebench2.protocol import (  # noqa: E402
    CB2_PROTOCOL,
    Protocol,
    SuiteSpec,
    get_protocol,
    set_protocol,
    use_protocol,
)

PKG = "cb2_toybench"


@pytest.fixture(scope="module")
def toy(tmp_path_factory) -> Protocol:  # noqa: ANN001
    """A toy protocol whose package lives in a temp dir on ``sys.path``."""
    root = tmp_path_factory.mktemp("toy")
    pkg = root / PKG
    (pkg / "suites").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "suites" / "__init__.py").write_text("")
    (pkg / "suites" / "ToySuite.yml").write_text(
        textwrap.dedent(
            f"""\
            - name: toy_gate
              diagnostic: {PKG}.diags.ToyGate
              additional_diagnostic_kwargs: {{}}
            """,
        ),
    )
    # A suite that shadows a ClimateBench2 one by name.
    (pkg / "suites" / "ClimateBench2_TierI.yml").write_text(
        "- name: shadow\n  diagnostic: x.y\n",
    )
    (pkg / "thresholds.yml").write_text(
        "toy:\n  gate:\n    requirement: required\n    tier: I\n"
        "    upper: 2.0\ntier2:\n  test_window_start: 2016\n",
    )
    (pkg / "diags.py").write_text(
        textwrap.dedent(
            """\
            from climatebench2.diags.pass_fail import GateCheck
            from climatebench2.diags.tier1_physics import CB2ComplexDiagnostic

            __all__ = ["ToyGate"]


            class ToyGate(CB2ComplexDiagnostic):
                _required_data_keys = ("amip",)
                _gate_checks = (
                    GateCheck(
                        check_id="toy_check",
                        column="toy_x",
                        upper=2.0,
                        requirement="required",
                        tier="I",
                    ),
                )

                def _calculate_raw_output(self, complex_data_source):
                    return self._scalar_outputs({"toy_x": 1.0})
            """,
        ),
    )
    sys.path.insert(0, str(root))
    try:
        yield Protocol(
            name="toybench",
            threshold_layers=(BASE_LAYER, (f"{PKG}", "thresholds.yml")),
            suites_packages=(f"{PKG}.suites", "climatebench2.suites"),
            diags_modules=(f"{PKG}.diags",),
            suite_registry={
                "ToySuite": SuiteSpec(
                    shape="experiments",
                    per_member=True,
                    historical_pre_test=True,
                ),
            },
            default_suites=("ToySuite",),
            submission_key="amip",
            pre_test_keys=("amip",),
            experiment_frequency={"amip_day": "day"},
            scorer="toybench",
            staged_root_env="TOYBENCH_STAGED_ROOT",
            display_name="Toy Bench",
        )
    finally:
        sys.path.remove(str(root))
        for name in [m for m in sys.modules if m == PKG or m.startswith(f"{PKG}.")]:
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _restore_protocol():  # noqa: ANN202
    saved = get_protocol()
    yield
    set_protocol(saved)


def test_cb2_protocol_is_the_default_and_matches_the_old_constants() -> None:
    protocol = get_protocol()
    assert protocol is CB2_PROTOCOL
    assert protocol.name == "climatebench2"
    assert protocol.submission_key == "historical"
    assert protocol.pre_test_keys == ("historical",)
    assert protocol.experiment_frequency == {"day": "day"}
    assert protocol.scorer == "climatebench2"
    assert protocol.tier3_scorer == "climatebench2.tier3"
    assert _cli.SUITE_REGISTRY is protocol.suite_registry
    assert list(protocol.default_suites) == _cli.DEFAULT_SUITES
    assert protocol.diags_modules == ("climatebench2.diags",)


def test_use_protocol_swaps_and_restores_protocol_and_thresholds(toy) -> None:  # noqa: ANN001
    before = get_threshold("tier2.test_window_start")
    with use_protocol(toy):
        assert get_protocol() is toy
        assert get_threshold("toy.gate.upper") == 2.0
        assert get_threshold("tier2.test_window_start") == 2016
    assert get_protocol() is CB2_PROTOCOL
    assert get_threshold("tier2.test_window_start") == before
    assert get_threshold("toy.gate.upper", None) is None


def test_resolve_suite_searches_packages_in_order(toy) -> None:  # noqa: ANN001
    with use_protocol(toy):
        toy_suite = _cli._resolve_suite("ToySuite")
        assert toy_suite.endswith("ToySuite.yml")
        assert PKG in toy_suite
        # earlier package shadows a ClimateBench2 suite of the same name
        assert PKG in _cli._resolve_suite("ClimateBench2_TierI")
        # a CB2 suite the toy package lacks is still found (second package)
        assert "climatebench2" in _cli._resolve_suite("ClimateBench2_TierII")
    assert _cli._resolve_suite("ToySuite") == "ToySuite"  # unknown: passes through
    assert PKG not in _cli._resolve_suite("ClimateBench2_TierI")


def test_schema_diagnostic_paths_read_every_suites_package(toy) -> None:  # noqa: ANN001
    from climatebench2.regate import gate_checks_for_schema, schema_diagnostic_paths

    with use_protocol(toy):
        paths = schema_diagnostic_paths()
        assert paths["toy_gate"] == f"{PKG}.diags.ToyGate"
        assert "ecs_gate" in paths  # climatebench2.suites is the second package
        # the earlier package wins a shared stanza name; not a CB2 shadow here
        checks = gate_checks_for_schema("toy_gate", paths)
        assert [c.check_id for c in checks] == ["toy_check"]
    outside = schema_diagnostic_paths()
    assert "toy_gate" not in outside
    assert "ecs_gate" in outside


def test_gate_requirements_and_leaderboard_required_set_follow_the_protocol(
    toy,  # noqa: ANN001
) -> None:
    import pandas as pd

    from climatebench2.diags.pass_fail import gate_requirements, gate_tiers
    from climatebench2.leaderboard import _check_requirements

    empty = pd.DataFrame()
    with use_protocol(toy):
        assert gate_requirements() == {"toy_check": "required"}
        assert gate_tiers() == {"toy_check": "I"}
        required = {c for c, t in _check_requirements(empty).items() if t == "required"}
        assert required == {"toy_check"}
    outside = gate_requirements()
    assert "toy_check" not in outside
    assert "water_budget" in outside
    assert "toy_check" not in _check_requirements(empty)


def test_leaderboard_scorer_and_title_follow_the_protocol(toy) -> None:  # noqa: ANN001
    import pandas as pd

    from climatebench2.leaderboard import Scores, render_html

    scores = Scores(
        gates=pd.DataFrame(),
        crps=pd.DataFrame(),
        consistency=pd.DataFrame(),
        deterministic=pd.DataFrame(),
        distribution=pd.DataFrame(),
        tier3=pd.DataFrame(),
        tier3_scores=pd.DataFrame(),
    )
    with use_protocol(toy):
        page = render_html(scores)
    assert "<h1>Toy Bench Leaderboard</h1>" in page
    assert "<code>toybench leaderboard</code>" in page
    assert "<h1>ClimateBench v2 Leaderboard</h1>" in render_html(scores)


def test_leaderboard_reads_the_protocols_scorer(toy, tmp_path) -> None:  # noqa: ANN001
    """Only rows of the active protocol's scorer count as scoring-pass rows."""
    import duckdb

    from climatebench2.leaderboard import build_scores

    db = tmp_path / "x.ddb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA d")
    con.execute(
        "CREATE TABLE d.metrics AS SELECT 'm' AS data_id, 'tas' AS var_id, "
        "CAST(NULL AS DOUBLE) AS crps, 'toybench' AS scorer",
    )
    con.close()
    with use_protocol(toy):
        assert len(build_scores([db]).crps) == 1
    assert build_scores([db]).crps.empty


def test_build_parser_prog_and_added_subcommand(toy) -> None:  # noqa: ANN001
    parser = _cli.build_parser(toy)
    assert parser.prog == "toybench"
    sub = parser.cb2_subparsers.add_parser("model-card")
    sub.add_argument("path")
    sub.set_defaults(func=lambda args: None)
    args = parser.parse_args(["model-card", "card.yml"])
    assert args.command == "model-card"
    assert args.path == "card.yml"
    assert "toybench leaderboard" in parser.format_help()
    # the default parser is untouched
    assert _cli.build_parser().prog == "climatebench2"
    assert "model-card" not in _cli.build_parser().format_help()


def test_main_runs_a_protocol_subcommand_and_restores_it(toy, capsys) -> None:  # noqa: ANN001
    calls: list[str] = []

    def fake_cmd(args) -> None:  # noqa: ANN001
        calls.append(get_protocol().name)

    original = _cli.build_parser

    def build(protocol=None):  # noqa: ANN001, ANN202
        parser = original(protocol)
        parser.cb2_subparsers.add_parser("ping").set_defaults(func=fake_cmd)
        return parser

    _cli.build_parser = build
    try:
        _cli.main(["ping"], protocol=toy)
    finally:
        _cli.build_parser = original
    assert calls == ["toybench"]
    assert get_protocol() is CB2_PROTOCOL


@pytest.fixture
def recorded(monkeypatch, tmp_path):  # noqa: ANN001, ANN201
    import climateeval._loader as loader
    from climateeval.suites import Suite

    runs: list[dict] = []
    cuts: list[tuple[str, int]] = []
    seen: set[tuple[str, str]] = set()

    def fake_load(path, *, timerange=None, **_kw):  # noqa: ANN001, ANN202
        return [f"cubes:{path}"]

    def fake_get_database(self, data, information, **_kw):  # noqa: ANN001, ANN202
        key = (self.name, information.variant)
        if key not in seen:
            seen.add(key)
            runs.append({"suite": self.name, "data": data, "variant": information.variant})

    def fake_end(cubes, last_year):  # noqa: ANN001, ANN202
        cuts.append((cubes[0], last_year))
        return [*cubes, f"cut<={last_year}"]

    monkeypatch.setattr(loader, "load_cmor_dir", fake_load)
    monkeypatch.setattr(Suite, "get_database", fake_get_database)
    monkeypatch.setattr(_cli, "end_at_year", fake_end)

    def run(argv, protocol=None):  # noqa: ANN001, ANN202
        seen.clear()
        _cli.main(["score", *argv, "--out", str(tmp_path / "out")], protocol=protocol)
        return runs, cuts

    return run


def _members(tmp_path, names):  # noqa: ANN001, ANN202
    for name in names:
        (tmp_path / "model" / "amip" / name).mkdir(parents=True)
    return tmp_path / "model"


def test_submission_key_and_pre_test_cut_follow_the_protocol(
    toy,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
    recorded,  # noqa: ANN001
) -> None:
    model = _members(tmp_path, ["r1i1p1f1", "r2i1p1f1"])
    # the toy gate's suite is the protocol's DEFAULT suite; per_member + a
    # discovered DRS tree gives each member's record under the key `amip`.
    runs, _ = recorded([str(model), "--name", "M"], protocol=toy)
    assert [r["suite"] for r in runs] == ["ToySuite", "ToySuite"]
    assert [r["variant"] for r in runs] == ["r1i1p1f1", "r2i1p1f1"]
    for run, member in zip(runs, ("r1i1p1f1", "r2i1p1f1"), strict=True):
        assert set(run["data"]) == {"amip"}
        assert run["data"]["amip"] == [f"cubes:{model / 'amip' / member}"]


def test_once_per_model_suite_cuts_only_the_protocols_pre_test_keys(
    toy,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
    recorded,  # noqa: ANN001
) -> None:
    once = Protocol(
        **{
            **toy.__dict__,
            "suite_registry": {
                "ToySuite": SuiteSpec(shape="experiments", historical_pre_test=True),
            },
        },
    )
    model = _members(tmp_path, ["r1i1p1f1", "r2i1p1f1"])
    other = tmp_path / "other"
    other.mkdir()
    runs, cuts = recorded(
        [str(model), "--name", "M", "--experiment", f"amip4k={other}"],
        protocol=once,
    )
    assert len(runs) == 1
    data = runs[0]["data"]
    assert data["amip"][-1].startswith("cut<=")  # the pre-test key was cut
    assert data["amip4k"] == [f"cubes:{other}"]  # a non-listed key was not
    assert len(cuts) == 1


def test_cb2_submission_key_is_unchanged(tmp_path, recorded) -> None:  # noqa: ANN001
    model = tmp_path / "model"
    (model / "historical" / "r1i1p1f1").mkdir(parents=True)
    (model / "historical" / "r2i1p1f1").mkdir(parents=True)
    runs, _ = recorded(
        [str(model), "--name", "M", "--suite", "ClimateBench2_TierII_events"],
    )
    assert [set(r["data"]) for r in runs] == [{"historical"}, {"historical"}]


def test_experiment_sourced_cube_suite_accepts_any_experiment_key() -> None:
    spec = SuiteSpec(shape="cubes", source="amip", window="full")
    assert spec.source == "amip"
    assert _cli.suite_timerange(spec) is None
