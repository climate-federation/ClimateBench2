"""End-to-end leaderboard test on a synthetic results database."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

climateeval = pytest.importorskip("climateeval")

import ibis  # noqa: E402

from climatebench2.leaderboard import Scores, build_scores, render_html  # noqa: E402


@pytest.fixture
def synthetic_db(tmp_path):  # noqa: ANN201
    """A .ddb mirroring Suite.get_database's layout (database-per-diagnostic)."""
    db_path = tmp_path / "ClimateBench2_TierI.ddb"
    conn = ibis.connect(f"duckdb://{db_path}")

    gates = pd.DataFrame(
        {
            "data_id": ["GoodModel", "GoodModel", "BadModel", "BadModel"],
            "data_type": ["to_benchmark"] * 4,
            "var_id": ["ecs_gate", "energy_balance_mean"] * 2,
            "value": [3.1, 0.05, 9.4, 0.4],
            "bound_lower": [1.0, np.nan, 1.0, np.nan],
            "bound_upper": [7.0, 0.1, 7.0, 0.1],
            "passes": [1.0, 1.0, 0.0, 0.0],
        },
    )
    crps = pd.DataFrame(
        {
            "data_id": ["GoodModel", "SingleMember", "CMIP6_A", "Climatology"],
            "data_type": ["to_benchmark", "to_benchmark", "other", "baseline"],
            "var_id": ["tas"] * 4,
            "scorer": ["climatebench2"] * 4,
            "reason": ["", "single member", "", ""],
            "crps": [0.08, np.nan, 0.12, 0.20],
            "crps_se": [0.01, np.nan, 0.02, 0.03],
            "crps_ci_lo": [0.06, np.nan, 0.10, 0.17],
            "crps_ci_hi": [0.10, np.nan, 0.14, 0.23],
            "t_eff": [25.0, np.nan, 25.0, 25.0],
            "n_members": [5.0, 1.0, 3.0, 30.0],
            "n_time": [30.0, 30.0, 30.0, 30.0],
            "e_ref": [0.12, np.nan, np.nan, 0.12],
            "n_ref_models": [4.0, np.nan, 3.0, 4.0],
            # 1 - 0.08/0.12 = +0.33 vs the CMIP6 median
            "skill": [1.0 - 0.08 / 0.12, np.nan, np.nan, 1.0 - 0.20 / 0.12],
        },
    )
    tier3 = pd.DataFrame(
        {
            "data_id": ["GoodModel"],
            "data_type": ["to_benchmark"],
            "midholocene_site_consistency": [0.85],
            "midholocene_n_sites": [120.0],
        },
    )

    conn.create_database("gates_diag")
    conn.create_table("metrics", ibis.memtable(gates), database="gates_diag")
    conn.create_database("scored_ts")
    conn.create_table("metrics", ibis.memtable(crps), database="scored_ts")
    conn.create_database("paleo")
    conn.create_table("raw_output", ibis.memtable(tier3), database="paleo")
    conn.disconnect()
    return db_path


def test_build_scores_collects_all_sections(synthetic_db) -> None:  # noqa: ANN001
    scores = build_scores([synthetic_db])
    assert set(scores.gates["data_id"]) == {"GoodModel", "BadModel"}
    # the NaN-CRPS "single member" row is kept: it is a result to show
    assert set(scores.crps["data_id"]) == {
        "GoodModel",
        "SingleMember",
        "CMIP6_A",
        "Climatology",
    }
    assert not scores.tier3.empty
    assert scores.consistency.empty


def test_render_html_gate_matrix_and_skill(synthetic_db) -> None:  # noqa: ANN001
    scores = build_scores([synthetic_db])
    html = render_html(scores, source_names=[synthetic_db.name])

    assert "<!doctype html>" in html
    assert "GoodModel" in html
    assert "BadModel" in html
    # Gate matrix: GoodModel all-pass, BadModel all-fail
    assert "✓" in html
    assert "✗" in html
    # Headline skill vs the CMIP6 median: 1 - 0.08/0.12 = +0.33
    assert "+0.33" in html
    # ... with the climatology skill alongside: 1 - 0.08/0.20 = +0.60
    assert "clim +0.60" in html
    # CRPS and its bootstrap interval are in the cell tooltip
    assert "fair CRPS 0.08" in html
    assert "95% CI [0.06, 0.1]" in html
    # A single-member submission cannot be scored under fair CRPS
    assert "single member" in html
    # The size of the reference ensemble behind E_ref is shown
    assert "CMIP6 models behind E_ref" in html
    # Tier III fraction
    assert "85%" in html
    # Baselines present and styled
    assert "Climatology" in html
    # Self-contained: no external resources
    assert "http" not in html.split("</style>")[1]


def test_skill_table_badges_and_orders_by_window(synthetic_db) -> None:  # noqa: ANN001
    """Paper §5.6: held-out entries first, every column labelled."""
    from climatebench2.leaderboard import variable_windows

    scores = build_scores([synthetic_db])
    # A second variable, scored in-sample (a historical-period climatology)
    in_sample = scores.crps[scores.crps["data_id"] == "GoodModel"].copy()
    in_sample["var_id"] = "a_seasonal_cycle"  # sorts before "tas" alphabetically
    in_sample["window"] = "in-sample"
    scores.crps["window"] = "held-out"
    scores.crps = pd.concat([scores.crps, in_sample], ignore_index=True)

    assert variable_windows(scores.crps) == {
        "tas": "held-out",
        "a_seasonal_cycle": "in-sample",
    }
    html = render_html(scores)
    assert "badge held'>held-out" in html
    assert "badge insample'>in-sample" in html
    # Held-out first, despite the alphabetical order of the names
    assert html.index(">tas<") < html.index(">a_seasonal_cycle<")


def test_skill_table_without_a_window_column_defaults_to_held_out(  # noqa: ANN001
    synthetic_db,
) -> None:
    """A database written before the pass wrote the label still renders."""
    from climatebench2.leaderboard import variable_windows

    scores = build_scores([synthetic_db])
    assert variable_windows(scores.crps) == {}
    assert "held-out" in render_html(scores)


def test_skill_table_shows_extended_entries_apart(synthetic_db) -> None:  # noqa: ANN001
    """Paper Table 2: Extended entries (here `prw`) get their own table."""
    from climatebench2.leaderboard import variable_statuses

    scores = build_scores([synthetic_db])
    extended = scores.crps.copy()
    extended["var_id"] = "prw"
    extended["variable_status"] = "extended"
    scores.crps["variable_status"] = "core"
    scores.crps = pd.concat([scores.crps, extended], ignore_index=True)

    assert variable_statuses(scores.crps) == {"tas": "core", "prw": "extended"}
    html = render_html(scores)
    core_at, extended_at = html.index("Core entries"), html.index("Extended entries")
    assert core_at < html.index(">tas<") < extended_at < html.index(">prw<")


def test_skill_table_resolves_the_status_of_an_older_database(  # noqa: ANN001
    synthetic_db,
) -> None:
    """No `variable_status` column: resolved from thresholds.yml instead."""
    from climatebench2.leaderboard import variable_statuses

    scores = build_scores([synthetic_db])
    assert "variable_status" not in scores.crps.columns
    clouds = scores.crps.copy()
    clouds["var_id"] = "clt"
    ohc = scores.crps.copy()
    ohc["var_id"] = "phcint_total"
    ohc["diagnostic"] = "ohc"
    scores.crps = pd.concat([scores.crps, clouds, ohc], ignore_index=True)
    assert variable_statuses(scores.crps) == {
        "tas": "core",
        "clt": "extended",
        "phcint_total": "extended",
    }
    html = render_html(scores)
    assert html.index(">tas<") < html.index("Extended entries") < html.index(">clt<")


def test_skill_table_without_extended_entries_has_no_extended_table(  # noqa: ANN001
    synthetic_db,
) -> None:
    html = render_html(build_scores([synthetic_db]))
    assert "Core entries" in html
    assert "Extended entries" not in html


def test_render_html_clips_the_displayed_skill(synthetic_db) -> None:  # noqa: ANN001
    """S is unbounded below; the paper clips the display at -1."""
    scores = build_scores([synthetic_db])
    scores.crps.loc[scores.crps["data_id"] == "GoodModel", "skill"] = -4.2
    html = render_html(scores)
    assert "-4.2" not in html
    assert "-1.00▼" in html


def test_render_html_empty_sections() -> None:
    html = render_html(Scores(), source_names=[])
    assert "No Tier I gate results" in html
    assert "No Tier II CRPS scores" in html


# ---------------------------------------------------------------------------
# Entry-ticket semantics (paper §7.1)
# ---------------------------------------------------------------------------


def _gate_rows(model: str, states: dict[str, str]) -> pd.DataFrame:
    """Gate metrics rows for one model: check_id -> pass|fail|na."""
    from climatebench2.diags.pass_fail import gate_requirements

    requirements = gate_requirements()
    rows = []
    for check, state in states.items():
        rows.append(
            {
                "data_id": model,
                "data_type": "to_benchmark",
                "var_id": check,
                "value": np.nan if state == "na" else 1.0,
                "bound_lower": np.nan,
                "bound_upper": np.nan,
                "passes": {"pass": 1.0, "fail": 0.0, "na": np.nan}[state],
                "requirement": requirements.get(check, "required"),
                "applicable": 0.0 if state == "na" else 1.0,
                "suite": "ClimateBench2_TierI",
                "diagnostic": check,
            },
        )
    return pd.DataFrame(rows)


def test_entry_ticket_four_outcomes() -> None:
    from climatebench2.leaderboard import entry_ticket

    required = ["a", "b", "c"]
    assert entry_ticket({"a": "pass", "b": "pass", "c": "pass"}, required) == "pass"
    # a declared N/A Required check does not block the ticket
    assert entry_ticket({"a": "pass", "b": "na", "c": "pass"}, required) == "pass"
    # a missing Required check makes the scorecard incomplete
    assert entry_ticket({"a": "pass", "b": "pass"}, required) == "incomplete"
    # an applicable Required failure is definitive, even with results missing
    assert entry_ticket({"a": "fail"}, required) == "fail"
    assert entry_ticket({"a": "pass", "b": "fail", "c": "na"}, required) == "fail"


def test_gate_matrix_groups_and_entry_ticket_column() -> None:
    from climatebench2.diags.pass_fail import gate_requirements
    from climatebench2.leaderboard import Scores, render_html

    requirements = gate_requirements()
    required = sorted(c for c, t in requirements.items() if t == "required")

    complete = dict.fromkeys(required, "pass")
    passing = {**complete, "geostrophic_balance": "na"}  # declared N/A
    failing = {**complete, "ecs_gate": "fail"}
    partial = {c: "pass" for c in required if c != "ecs_gate"}

    gates = pd.concat(
        [
            _gate_rows("Emulator", {**passing, "mjo_east_west": "pass"}),
            _gate_rows("BadModel", failing),
            _gate_rows("PartialModel", partial),
            _gate_rows("ExtraModel", {**complete, "bjerknes_compensation": "fail"}),
        ],
        ignore_index=True,
    )
    html = render_html(Scores(gates=gates))

    def ticket(model: str) -> str:
        row = html.split(f"<strong>{model}</strong></td>", 1)[1]
        cell = row.split("</td>", 1)[0]
        assert "gate-all" in cell, cell
        return cell.split(">")[-1]

    assert ticket("Emulator") == "✓"  # N/A does not block entry
    assert ticket("BadModel") == "✗"
    assert ticket("PartialModel") == "⚠"
    assert ticket("ExtraModel") == "✓"  # an extra check never gates entry

    # Declared N/A renders as n/a, not as a pass or a fail
    assert "n/a" in html
    # The three groups are rendered separately
    assert "Extended checks" in html
    assert "Extra checks" in html


def test_gate_matrix_distinguishes_not_run_from_not_applicable() -> None:
    from climatebench2.leaderboard import Scores, render_html

    gates = _gate_rows("M", {"ecs_gate": "na", "water_budget": "pass"})
    html = render_html(Scores(gates=gates))
    assert "n/a" in html  # declared N/A
    assert "—" in html  # every other Required check: no row at all


def test_tier2_event_flags_are_not_in_the_entry_ticket() -> None:
    from climatebench2.diags.pass_fail import gate_requirements
    from climatebench2.leaderboard import Scores, entry_ticket, render_html

    requirements = gate_requirements()
    required = sorted(c for c, t in requirements.items() if t == "required")
    states = {**dict.fromkeys(required, "pass"), "pinatubo_dimming": "fail"}
    gates = _gate_rows("M", states)

    assert entry_ticket(states, required) == "pass"
    html = render_html(Scores(gates=gates))
    assert "Tier II — aggregated event diagnostics" in html
    assert "pinatubo_dimming" in html


def test_build_scores_keeps_declared_na_rows_and_fills_old_columns(
    tmp_path,  # noqa: ANN001
) -> None:
    """Databases written before the tags still render (backward compatible)."""
    from climatebench2.leaderboard import build_scores

    db_path = tmp_path / "ClimateBench2_TierI.ddb"
    conn = ibis.connect(f"duckdb://{db_path}")
    gates = pd.DataFrame(
        {
            "data_id": ["M", "M"],
            "data_type": ["to_benchmark"] * 2,
            "var_id": ["ecs_gate", "geostrophic_balance"],
            "value": [3.1, np.nan],
            "bound_lower": [1.0, 0.9],
            "bound_upper": [7.0, np.nan],
            "passes": [1.0, np.nan],
            "applicable": [1.0, 0.0],
        },
    )
    conn.create_database("gates_diag")
    conn.create_table("metrics", ibis.memtable(gates), database="gates_diag")
    conn.disconnect()

    scores = build_scores([db_path])
    assert len(scores.gates) == 2  # the declared-N/A row survives the filter
    by_check = scores.gates.set_index("var_id")
    assert by_check.loc["geostrophic_balance", "applicable"] == 0.0
    # requirement recovered from the gate classes
    assert by_check.loc["ecs_gate", "requirement"] == "required"
    assert by_check.loc["geostrophic_balance", "requirement"] == "required"


def test_gate_rows_are_relabelled_by_model_name(tmp_path) -> None:  # noqa: ANN001
    """One model, one row — Tier I ids and Tier II names must agree.

    Tier I gate rows are written per data source, so they carry the full
    ``DataSourceInformation.id`` (``model_MyModel_historical_r1i1p1f1``),
    while the scoring pass groups the members and writes the model **name**.
    The leaderboard maps ids through ``data_sources`` so the same model is
    one row in the gate matrix and one row in the skill table.
    """
    from climatebench2.leaderboard import build_scores, render_html

    db_path = tmp_path / "ClimateBench2_TierI.ddb"
    conn = ibis.connect(f"duckdb://{db_path}")
    gates = pd.DataFrame(
        {
            "data_id": [
                "model_MyModel_historical_r1i1p1f1",
                "model_MyModel_historical_r2i1p1f1",
                "observation_HadCRUT5",
            ],
            "data_type": ["to_benchmark", "to_benchmark", "reference"],
            "var_id": ["ecs_gate"] * 3,
            "value": [3.1, 3.2, 3.0],
            "bound_lower": [1.0] * 3,
            "bound_upper": [7.0] * 3,
            "passes": [1.0, 1.0, 1.0],
        },
    )
    sources = pd.DataFrame(
        {
            "id": [
                "model_MyModel_historical_r1i1p1f1",
                "model_MyModel_historical_r2i1p1f1",
                "observation_HadCRUT5",
            ],
            "name": ["MyModel", "MyModel", "HadCRUT5"],
            "category": ["model", "model", "observation"],
        },
    )
    crps = pd.DataFrame(
        {
            "data_id": ["MyModel"],
            "data_type": ["to_benchmark"],
            "var_id": ["tas"],
            "scorer": ["climatebench2"],
            "reason": [""],
            "crps": [0.08],
            "skill": [0.3],
            "e_ref": [0.12],
            "n_ref_models": [4.0],
        },
    )
    conn.create_database("gates_diag")
    conn.create_table("metrics", ibis.memtable(gates), database="gates_diag")
    conn.create_table("data_sources", ibis.memtable(sources), database="gates_diag")
    conn.create_database("scored_ts")
    conn.create_table("metrics", ibis.memtable(crps), database="scored_ts")
    conn.disconnect()

    scores = build_scores([db_path])
    assert set(scores.gates["data_id"]) == {"MyModel", "HadCRUT5"}
    html = render_html(scores)
    assert "r1i1p1f1" not in html  # the raw ids never reach the scorecard
    # One "MyModel" row in the gate matrix, one in the skill table
    assert html.count("<strong>MyModel</strong>") == 2


def test_consistency_rows_are_not_mistaken_for_gates_or_scores(tmp_path) -> None:  # noqa: ANN001
    """The pass writes gates, CRPS and consistency into one metrics table."""
    from climatebench2.leaderboard import build_scores, render_html

    db_path = tmp_path / "ClimateBench2_TierII.ddb"
    conn = ibis.connect(f"duckdb://{db_path}")
    metrics = pd.DataFrame(
        {
            "data_id": ["MyModel", "MyModel"],
            "data_type": ["to_benchmark"] * 2,
            "var_id": ["tas", "tas_trend_consistency"],
            "scorer": ["climatebench2"] * 2,
            "reason": ["", ""],
            "crps": [0.08, np.nan],
            "skill": [0.3, np.nan],
            "value": [np.nan, 0.021],
            "z": [np.nan, 0.6],
            "p_value": [np.nan, 0.55],
            "passes": [np.nan, 1.0],
            "sigma_internal": [np.nan, 0.004],
            "sigma_obs": [0.05, 0.0005],
            "n_members": [3.0, 3.0],
        },
    )
    conn.create_database("scored_ts")
    conn.create_table("metrics", ibis.memtable(metrics), database="scored_ts")
    conn.disconnect()

    scores = build_scores([db_path])
    # The consistency row has a verdict but is NOT a Tier I gate
    assert scores.gates.empty
    assert list(scores.consistency["var_id"]) == ["tas_trend_consistency"]
    # ... and it is not a column of the Tier II skill table either
    assert list(scores.crps["var_id"]) == ["tas"]
    html = render_html(scores)
    assert "ensemble-consistency tests" in html
    assert "tas_trend_consistency" in html


def test_distribution_skill_table_is_separate_from_the_crps_table(tmp_path) -> None:  # noqa: ANN001
    """The Perkins score is a skill in [0, 1], not an error: its own table."""
    import ibis

    db_path = tmp_path / "ClimateBench2_TierII_daily.ddb"
    conn = ibis.connect(f"duckdb://{db_path}")
    perkins = pd.DataFrame(
        {
            "data_id": ["GoodModel", "GoodModel"],
            "reference_data_id": ["reanalysis_ERA5"] * 2,
            "var_id": ["pr_intensity_land", "tas_anomaly_land"],
            "perkins_djf": [0.91, 0.83],
            "perkins_jja": [0.88, 0.79],
            "perkins_all": [0.90, 0.81],
        },
    )
    conn.create_database("perkins")
    conn.create_table("metrics", ibis.memtable(perkins), database="perkins")
    conn.disconnect()

    scores = build_scores([db_path])
    assert set(scores.distribution["var_id"]) == {
        "pr_intensity_land",
        "tas_anomaly_land",
    }
    # ... and it is NOT mistaken for a CRPS score or a gate
    assert scores.crps.empty
    assert scores.gates.empty

    html = render_html(scores, source_names=[db_path.name])
    assert "distribution skill (Perkins)" in html
    assert "0.910" in html
    # always in-sample, and never part of the headline skill
    assert "in-sample" in html
    assert "E_ref" in html.split("distribution skill (Perkins)")[1]


# ---------------------------------------------------------------------------
# Tier labels (WP7): a gate is shown under its own tier, not its suite's
# ---------------------------------------------------------------------------


def test_tier3_gates_leave_the_tier_i_extended_table() -> None:
    """The mid-Holocene monsoon gate is Tier III Extended, not Tier I.

    It is emitted through the same gate machinery as the Tier I checks and
    used to appear in the Tier I Extended table; the `tier` tag it now carries
    (`thresholds.yml`) is what moves it.
    """
    from climatebench2.diags.pass_fail import gate_requirements, gate_tiers
    from climatebench2.leaderboard import Scores, render_html

    requirements = gate_requirements()
    tiers = gate_tiers()
    assert tiers["midholocene_monsoon"] == "III"
    assert requirements["midholocene_monsoon"] == "extended"

    gates = _gate_rows(
        "M",
        {
            "ecs_gate": "pass",
            "mjo_east_west": "pass",       # Tier I, Extended
            "midholocene_monsoon": "fail",  # Tier III, Extended
        },
    )
    html = render_html(Scores(gates=gates))

    tier1_extended = html.split("<h3>Extended checks</h3>", 1)[1].split("</table>", 1)[0]
    assert "mjo_east_west" in tier1_extended
    assert "midholocene_monsoon" not in tier1_extended

    tier3 = html.split("<h3>Tier III checks</h3>", 1)[1].split("</table>", 1)[0]
    assert "midholocene_monsoon" in tier3
    assert "Tier III — paleoclimate and perfect-model tests" in html


def test_tier3_scores_table_separates_scored_from_reported(tmp_path) -> None:  # noqa: ANN001
    """Tier III CRPS rows get their own table, never the Tier II skill one."""
    from climatebench2.leaderboard import build_scores, render_html
    from climatebench2.scoring_pass import SCORER, TIER3_SCORER

    rows = pd.DataFrame(
        {
            "data_id": ["M", "M", "M"],
            "data_type": ["to_benchmark"] * 3,
            "var_id": [
                "lgm_Tierney2020_tos_tos",
                "lgm_Cleator2020_tas_tas",
                "lgm_Scussolini2019_pr_pr",
            ],
            "scorer": [TIER3_SCORER] * 3,
            "reason": ["", "data_assimilation product: reported", "no dataset"],
            "window": ["held-out"] * 3,
            "crps": [0.42, 1.31, np.nan],
            "n_sites": [512.0, 1180.0, np.nan],
            "n_members": [5.0, 5.0, np.nan],
            "dataset_type": ["proxy_compilation", "data_assimilation", ""],
        },
    )
    db_path = tmp_path / "ClimateBench2_TierIII.ddb"
    conn = ibis.connect(f"duckdb://{db_path}")
    conn.create_database("lgm_tierney2020_tos")
    conn.create_table("metrics", ibis.memtable(rows), database="lgm_tierney2020_tos")
    conn.disconnect()

    scores = build_scores([db_path])
    assert scores.crps.empty, "Tier III rows must not enter the Tier II skill table"
    assert len(scores.tier3_scores) == 3
    assert set(scores.tier3_scores["scorer"]) == {TIER3_SCORER}
    assert SCORER not in set(scores.tier3_scores["scorer"])

    html = render_html(scores)
    assert "Paleo fair CRPS vs the proxy compilations" in html
    assert "0.42" in html
    assert "1.31 †" in html      # reported, excluded from the protocol score
    assert "no dataset" in html  # the unscored one still says why
