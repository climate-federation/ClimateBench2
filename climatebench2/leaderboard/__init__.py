"""The ClimateBench v2 leaderboard.

Turns ClimateEval result databases (``.ddb``, one per suite — accumulate
several models with ``Suite.get_database(..., append=True)``) into the
protocol's presentation:

- **Tier I gate matrix** — every pass/fail check per model, grouped by the
  ``requirement`` tag the gate carries (paper §7.1): *Required* (the entry
  ticket), *Extended* (reported alongside, additional credit, does not gate
  entry) and *extra* (code-only sanity checks). The **Entry ticket** column
  is ✓ only when every Required check either passed or was declared N/A by
  the submission, ⚠ when a Required check produced no row at all, ✗ when an
  applicable Required check failed.
- **Tier II scores** — the headline skill ``S = 1 − E/E_ref`` per model and
  variable, where E is the model's **fair CRPS** over its stacked ensemble
  members and E_ref the **median** fair CRPS across the CMIP6 reference
  models (leave-one-out); the CRPS, its bootstrap interval and the ensemble
  size are in the cell tooltip, the Climatology-baseline skill alongside.
  The scores come from :mod:`climatebench2.scoring_pass`, which
  ``climatebench2 score`` runs after the suites (and ``leaderboard
  --rescore`` re-runs), so a database that has not been through the pass
  shows no Tier II numbers. Columns are ordered **held-out first** and
  badged with the ``window`` label the pass writes (paper §5.6): held-out
  entries are scored against observations from the reserved post-2015 test
  period, in-sample ones against the historical record.
  Paper Table 2's **Extended** entries (OHC, surface fluxes, cloud
  properties, ``prw``) get a table of their own below the **Core** one, by
  the ``variable_status`` tag the pass writes (``tier2.variable_status``).
- **Tier II distribution skill** — the **Perkins score** of the daily PDFs
  (work package 6b), in its own table because it is a skill in [0, 1]
  rather than an error: it never enters ``E_ref`` or the headline
  ``S = 1 − E/E_ref``, and it is always in-sample.
- **Tier III** — paleo proxy-site consistency fractions and the
  mid-Holocene monsoon gate.

``build_scores`` returns plain DataFrames; ``render_html`` produces a
self-contained static page; ``build_scores_table`` keeps the simple
deterministic summary used since Phase 0.
"""

from __future__ import annotations

import html as html_module
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from climatebench2.scoring_pass import (
    CLIMATOLOGY_DATA_ID,
    SCORER,
    STATUS_EXTENDED,
    TIER3_SCORER,
    WINDOW_HELD_OUT,
    WINDOW_IN_SAMPLE,
    WINDOW_PERFECT_MODEL,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd

#: Prefix of the metrics columns the Perkins diagnostic writes
#: (``perkins_djf``, …, ``perkins_all``).
PERKINS_PREFIX = "perkins_"

#: Protocol tiers, in scorecard order. A gate row carries its own tier
#: (``thresholds.yml``), so a check appears under the tier the protocol gives
#: it rather than under the suite it happened to run in — which is what keeps
#: the mid-Holocene monsoon gate (Tier III, Extended) out of the Tier I table.
TIER_I, TIER_II, TIER_III = "I", "II", "III"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _read_all(db_paths: list[Path]) -> list[tuple[str, str, str, pd.DataFrame]]:
    """Yield (suite, diagnostic, table, df) for every table in the dbs."""
    # Private-module reuse is deliberate: climateeval is pinned by commit.
    from climateeval.report._db import read_database

    out = []
    for db_path in db_paths:
        suite = db_path.stem
        for diag_name, tables in read_database(db_path).items():
            for table_name, df in tables.items():
                out.append((suite, diag_name, table_name, df))
    return out


@dataclass
class Scores:
    """Leaderboard-ready frames (any may be empty)."""

    gates: pd.DataFrame = field(default_factory=lambda: _empty())
    crps: pd.DataFrame = field(default_factory=lambda: _empty())
    consistency: pd.DataFrame = field(default_factory=lambda: _empty())
    #: Distribution-shape SKILL scores (the Perkins score) — a skill, not an
    #: error, so it is kept apart from `crps` and never enters E_ref.
    distribution: pd.DataFrame = field(default_factory=lambda: _empty())
    deterministic: pd.DataFrame = field(default_factory=lambda: _empty())
    #: Tier III ``raw_output`` rows carrying the ``*_site_consistency``
    #: fractions (the complementary diagnostic).
    tier3: pd.DataFrame = field(default_factory=lambda: _empty())
    #: Tier III ``metrics`` rows the paleo diagnostics write themselves
    #: (``scorer = TIER3_SCORER``): the fair CRPS per (period, dataset,
    #: variable), its `dataset_type` and the reasons for the unscored ones.
    tier3_scores: pd.DataFrame = field(default_factory=lambda: _empty())


def _empty() -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame()


def _normalise_gates(gates: pd.DataFrame) -> pd.DataFrame:
    """Fill the requirement/tier/applicable columns (databases predating them).

    Older result databases carry only ``passes``; their rows are all
    applicable, and their requirement and tier tags are recovered from the
    gate classes (``climatebench2.diags``), defaulting to ``required`` and
    Tier I.
    """
    if gates.empty:
        return gates
    if "applicable" not in gates.columns:
        gates["applicable"] = 1.0
    gates["applicable"] = gates["applicable"].astype(float).fillna(1.0)
    requirements: dict[str, str] = {}
    tiers: dict[str, str] = {}
    try:
        from climatebench2.diags.pass_fail import gate_requirements, gate_tiers

        requirements = gate_requirements()
        tiers = gate_tiers()
    except Exception:  # noqa: BLE001 - climateeval may be unavailable
        requirements, tiers = {}, {}
    for column, registry, default in (
        ("requirement", requirements, "required"),
        ("tier", tiers, TIER_I),
    ):
        if column not in gates.columns:
            gates[column] = ""
        gates[column] = [
            tag if isinstance(tag, str) and tag else registry.get(var_id, default)
            for tag, var_id in zip(gates[column], gates["var_id"], strict=True)
        ]
    return gates


def _label_by_model(frame: pd.DataFrame, names: dict[str, str]) -> pd.DataFrame:
    """Relabel ``data_id`` by model name, so one model has one row everywhere.

    Tier I gate rows carry the full ``DataSourceInformation.id``
    (``model_MyModel_historical_r1i1p1f1``) because they are written per data
    source, while the scoring pass groups members and writes the **model
    name**. Mapping the ids through the ``data_sources`` tables makes the two
    halves of the scorecard agree — and collapses a model's members into one
    scorecard row, which is what a Tier I gate means (every member must pass).
    """
    if frame.empty or "data_id" not in frame.columns or not names:
        return frame
    frame = frame.copy()
    frame["data_id"] = [names.get(str(i), str(i)) for i in frame["data_id"]]
    return frame


def build_scores(db_paths: list[Path]) -> Scores:
    """Collect gate/CRPS/consistency/deterministic/Tier-III frames."""
    import pandas as pd

    gates, crps, consistency, deterministic, tier3 = [], [], [], [], []
    distribution: list[pd.DataFrame] = []
    tier3_scores: list[pd.DataFrame] = []
    names: dict[str, str] = {}
    for suite, diag, table, df in _read_all(db_paths):
        if df.empty:
            continue
        tagged = df.assign(suite=suite, diagnostic=diag)
        if table == "data_sources":
            if {"id", "name"} <= set(df.columns):
                names.update(
                    {str(i): str(n) for i, n in zip(df["id"], df["name"], strict=True)},
                )
        elif table == "metrics":
            # A row is a gate iff it has a verdict and no p-value: the
            # scoring pass now writes consistency rows (which carry both
            # `passes` and `p_value`) into the *same* metrics tables, so the
            # test must be per row, never per column.
            if "passes" in df.columns:
                is_gate = tagged["passes"].notna()
                if "p_value" in tagged.columns:
                    is_gate = is_gate & tagged["p_value"].isna()
                # Declared-N/A rows carry no `passes` but must be kept: they
                # are how a submission says a Required test does not apply.
                if "applicable" in tagged.columns:
                    is_gate = is_gate | (tagged["applicable"] == 0.0)
                gates.append(tagged[is_gate])
            if "crps" in df.columns:
                # Rows the scoring pass wrote are kept even with a NaN score:
                # "n/a (single member)" is a result the scorecard must show.
                keep = tagged["crps"].notna()
                is_tier3 = (
                    tagged["scorer"] == TIER3_SCORER
                    if "scorer" in tagged.columns
                    else tagged["crps"].isna() & False
                )
                if "scorer" in tagged.columns:
                    keep = keep | (tagged["scorer"] == SCORER)
                if "p_value" in tagged.columns:
                    keep = keep & tagged["p_value"].isna()
                # Tier III rows share the vocabulary but not the table: their
                # ensemble is a block pseudo-ensemble of one run, so they have
                # no E_ref and belong under Tier III, not in the skill table.
                crps.append(tagged[keep & ~is_tier3])
                if is_tier3.any():
                    tier3_scores.append(tagged[is_tier3])
            if "p_value" in df.columns:
                consistency.append(tagged[tagged["p_value"].notna()])
            # The Perkins skill score (climatebench2.diags.PerkinsSkillScore)
            # is written by the diagnostic, not the scoring pass, and is a
            # skill in [0, 1] rather than an error: its own table.
            perkins_cols = [c for c in df.columns if c.startswith(PERKINS_PREFIX)]
            if perkins_cols:
                distribution.append(tagged.dropna(subset=perkins_cols, how="all"))
            metric_cols = [
                c
                for c in ("weighted_rmse", "weighted_pearsonr", "weighted_emd")
                if c in df.columns
            ]
            if metric_cols:
                deterministic.append(
                    tagged.dropna(subset=metric_cols, how="all"),
                )
        elif table == "raw_output":
            site_cols = [c for c in df.columns if c.endswith("_site_consistency")]
            if site_cols:
                tier3.append(tagged)

    def cat(frames: list[pd.DataFrame]) -> pd.DataFrame:
        # `drop_duplicates`: the per-member Tier II suites are run once per
        # ensemble member and appended into one database, so a diagnostic's
        # own `reference` and `other` metrics rows (its weighted_rmse and
        # friends for the observations and the comparison ensemble) arrive
        # once per member. The scoring pass strips the same duplication from
        # `raw_output` (`scoring_pass.deduplicate_raw_output`); this is the
        # display side of it. A fully identical row can only be a
        # re-ingestion, so nothing else is affected.
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True, sort=False).drop_duplicates(
            ignore_index=True,
        )

    return Scores(
        gates=_label_by_model(_normalise_gates(cat(gates)), names),
        crps=_label_by_model(cat(crps), names),
        consistency=_label_by_model(cat(consistency), names),
        distribution=_label_by_model(cat(distribution), names),
        deterministic=_label_by_model(cat(deterministic), names),
        tier3=_label_by_model(cat(tier3), names),
        tier3_scores=_label_by_model(cat(tier3_scores), names),
    )


def build_scores_table(db_paths: list[Path]) -> pd.DataFrame:
    """Simple per-model summary from the deterministic metrics (Phase 0)."""
    from climateeval.report._db import read_database
    from climateeval.report._leaderboard import build_leaderboard_data

    all_metrics = {}
    for db_path in db_paths:
        diags = read_database(db_path)
        all_metrics[db_path.stem] = {
            name: tables["metrics"]
            for name, tables in diags.items()
            if "metrics" in tables
        }
    summary, _detail = build_leaderboard_data(all_metrics)
    return summary


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;margin:2rem auto;
     max-width:1100px;padding:0 1rem;color:#1a1a1a;background:#fafafa}
h1{border-bottom:3px solid #205493;padding-bottom:.3rem}
h2{margin-top:2.2rem;color:#205493}
h3{margin-top:1.6rem;color:#205493;font-size:1.02rem}
table{border-collapse:collapse;width:100%;margin:.8rem 0;background:#fff;
      box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:.9rem}
th,td{border:1px solid #e2e2e2;padding:.4rem .6rem;text-align:left}
th{background:#f0f4f8;position:sticky;top:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pass{background:#e6f4ea;color:#1e7e34;font-weight:600;text-align:center}
.fail{background:#fdecea;color:#c0392b;font-weight:600;text-align:center}
.na{color:#999;text-align:center}
.gate-all{font-weight:700}
.baseline{font-style:italic;color:#555}
.beats{color:#1e7e34;font-weight:600}
.loses{color:#c0392b}
.badge{display:inline-block;padding:0 .35rem;border-radius:.7rem;font-weight:600;
       font-size:.72rem;letter-spacing:.02em}
.badge.held{background:#e8eef7;color:#205493;border:1px solid #c3d3e8}
.badge.insample{background:#f4f0e6;color:#8a6d1f;border:1px solid #e3d9bd}
footer{margin-top:3rem;color:#777;font-size:.8rem;border-top:1px solid #ddd;
       padding-top:.6rem}
"""


def _esc(value: object) -> str:
    return html_module.escape(str(value))


# -- Tier I gate matrix -----------------------------------------------------
#
# Gate rows carry a `requirement` tag (required / extended / extra /
# diagnostic — thresholds.yml) and an `applicable` flag. The scorecard shows
# the three Tier I groups separately, because only the Required group is the
# paper's entry ticket, and it must distinguish "does not apply to this
# submission" (declared N/A) from "was not run".

#: Per-cell markup: state -> (css class, glyph).
_STATE_CELLS = {
    "pass": ("pass", "✓"),
    "fail": ("fail", "✗"),
    "na": ("na", "n/a"),
    "missing": ("na", "—"),
}

#: Entry-ticket verdicts.
_TICKET_CELLS = {
    "pass": ("pass", "✓"),
    "fail": ("fail", "✗"),
    "incomplete": ("na", "⚠"),
}


def _gate_states(gates: pd.DataFrame) -> dict[str, dict[str, str]]:
    """``data_id -> check_id -> "pass" | "fail" | "na"``.

    A check with rows from several diagnostics/suites must pass everywhere;
    a check whose rows are all declared N/A (``applicable = 0``) is ``na``.
    """
    states: dict[str, dict[str, str]] = {}
    for keys, group in gates.groupby(["data_id", "var_id"], sort=False):
        data_id, var_id = keys
        applicable = float(group["applicable"].max())
        passes = group["passes"].dropna()
        if applicable < 1.0 or passes.empty:
            state = "na"
        else:
            state = "pass" if float(passes.min()) >= 1.0 else "fail"
        states.setdefault(str(data_id), {})[str(var_id)] = state
    return states


def _check_tags(gates: pd.DataFrame, column: str) -> dict[str, str]:
    """``check_id -> requirement`` (or ``tier``) for every known gate, run or not.

    The gate classes are the authority on the *full* Required set (a gate
    that never ran writes no row, and its absence is what makes an entry
    ticket incomplete); tags found in the databases win where they differ.
    """
    tags: dict[str, str] = {}
    try:
        from climatebench2.diags.pass_fail import gate_requirements, gate_tiers

        tags.update(gate_requirements() if column == "requirement" else gate_tiers())
    except Exception:  # noqa: BLE001 - climateeval may be unavailable
        pass
    if not gates.empty and column in gates.columns:
        for var_id, tag in zip(gates["var_id"], gates[column], strict=True):
            if isinstance(tag, str) and tag:
                tags[str(var_id)] = tag
    return tags


def _check_requirements(gates: pd.DataFrame) -> dict[str, str]:
    """``check_id -> requirement`` for every known gate, run or not."""
    return _check_tags(gates, "requirement")


def entry_ticket(
    states: dict[str, str],
    required_checks: list[str],
) -> str:
    """Entry-ticket verdict for one model (paper §7.1).

    ``pass`` when every Required check either passed or was declared N/A;
    ``fail`` when any applicable Required check failed (a definitive
    outcome, so it outranks missing results); ``incomplete`` when a Required
    check produced no row at all.
    """
    outcomes = [states.get(check, "missing") for check in required_checks]
    if "fail" in outcomes:
        return "fail"
    if "missing" in outcomes or not outcomes:
        return "incomplete"
    return "pass"


def _gate_group_table(
    states: dict[str, dict[str, str]],
    checks: list[str],
    models: list[str],
    *,
    tickets: dict[str, str] | None = None,
) -> str:
    """One gate table; with ``tickets`` an Entry-ticket column is prepended."""
    header = "".join(f"<th>{_esc(c)}</th>" for c in checks)
    ticket_header = "<th>Entry ticket</th>" if tickets is not None else ""
    rows = []
    for model in models:
        cells = []
        if tickets is not None:
            cls, glyph = _TICKET_CELLS[tickets[model]]
            cells.append(f"<td class='gate-all {cls}'>{glyph}</td>")
        for check in checks:
            cls, glyph = _STATE_CELLS[states.get(model, {}).get(check, "missing")]
            cells.append(f"<td class='{cls}'>{glyph}</td>")
        rows.append(
            f"<tr><td><strong>{_esc(model)}</strong></td>{''.join(cells)}</tr>",
        )
    return (
        f"<table><thead><tr><th>Model</th>{ticket_header}{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _grouped_checks(
    gates: pd.DataFrame,
    tier: str,
) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """``(states, {requirement: [check_id]})`` for one protocol tier.

    A check is placed by its own ``tier`` tag, so a gate emitted through the
    Tier I machinery but belonging to Tier III (the mid-Holocene monsoon
    check) lands in the Tier III table. Required checks are listed even when
    they produced no row — that hole is what makes an entry ticket ⚠.
    """
    requirements = _check_requirements(gates)
    tiers = _check_tags(gates, "tier")
    states = _gate_states(gates)
    seen = set(gates["var_id"].astype(str)) if not gates.empty else set()

    def group(tag: str, *, include_unrun: bool) -> list[str]:
        ids = {
            c
            for c, t in requirements.items()
            if t == tag and tiers.get(c, TIER_I) == tier
        }
        return sorted(ids if include_unrun else ids & seen)

    return states, {
        "required": group("required", include_unrun=True),
        "extended": group("extended", include_unrun=False),
        "extra": group("extra", include_unrun=False),
        "diagnostic": group("diagnostic", include_unrun=False),
    }


def _gate_matrix_html(gates: pd.DataFrame) -> str:
    """The Tier I scorecard: Required (+ entry ticket), Extended, extra."""
    if gates.empty:
        return "<p class='na'>No Tier I gate results in the given databases.</p>"

    states, groups = _grouped_checks(gates, TIER_I)
    required = groups["required"]
    tickets = {m: entry_ticket(s, required) for m, s in states.items()}
    order = {"pass": 0, "incomplete": 1, "fail": 2}
    models = sorted(states, key=lambda m: (order[tickets[m]], m))

    html = [
        "<p>Required checks are the paper's entry ticket: a model is scored "
        "only if every applicable Required check passes. "
        "<strong>✓</strong> passed · <strong>✗</strong> failed · "
        "<strong>n/a</strong> declared not applicable by the submission "
        "(<code>score --not-applicable NAME</code>) · <strong>—</strong> not "
        "run. The entry ticket is <strong>⚠</strong> while a Required check "
        "has no result at all.</p>",
        _gate_group_table(states, required, models, tickets=tickets),
    ]
    if groups["extended"]:
        html += [
            "<h3>Extended checks</h3>",
            "<p>Reported alongside the entry ticket and contributing "
            "additional credit; these do <em>not</em> gate entry (their "
            "inputs are outside the CMIP6 protocol or need daily fields).</p>",
            _gate_group_table(states, groups["extended"], models),
        ]
    if groups["extra"]:
        html += [
            "<h3>Extra checks (not part of the protocol)</h3>",
            "<p>Code-only sanity checks kept from the legacy pipeline; "
            "excluded from the entry ticket pending paper reconciliation.</p>",
            _gate_group_table(states, groups["extra"], models),
        ]
    return "".join(html)


def _event_flags_html(gates: pd.DataFrame) -> str:
    """Tier II aggregated event diagnostics (Pinatubo, hemispheric asymmetry)."""
    if gates.empty:
        return ""
    states, groups = _grouped_checks(gates, TIER_II)
    checks = groups["diagnostic"]
    if not checks:
        return ""
    models = sorted(m for m, s in states.items() if s.keys() & set(checks))
    return (
        "<h2>Tier II — aggregated event diagnostics</h2>"
        "<p>Sign flags of the Pinatubo response and the aerosol-era "
        "hemispheric asymmetry (<code>ClimateBench2_TierII_events</code>). "
        "These are Tier II diagnostics, not Tier I gates: they are reported, "
        "never part of the entry ticket.</p>"
        + _gate_group_table(states, checks, models)
    )


def _tier3_gates_html(gates: pd.DataFrame) -> str:
    """Tier III pass/fail checks (the mid-Holocene monsoon, the LE spread)."""
    if gates.empty:
        return ""
    states, groups = _grouped_checks(gates, TIER_III)
    checks = groups["required"] + groups["extended"] + groups["extra"]
    if not checks:
        return ""
    models = sorted(m for m, s in states.items() if s.keys() & set(checks))
    return (
        "<h3>Tier III checks</h3>"
        "<p>Pass/fail checks belonging to Tier III — the mid-Holocene "
        "Green-Sahara monsoon requirement (JJAS precipitation anomaly over "
        "North Africa) and, for a perfect-model submission, the "
        "large-ensemble spread test. mid-Holocene is an <em>Extended</em> "
        "period, so these are reported for credit and never part of the "
        "Tier I entry ticket.</p>" + _gate_group_table(states, checks, models)
    )


#: The paper clips the displayed skill score at S = -1 (it is bounded above
#: by 1, unbounded below, so a hopeless model would otherwise squash the
#: whole column).
SKILL_DISPLAY_FLOOR = -1.0

#: Order of the scorecard's columns: the paper's §5.6 rule is that the
#: **held-out** entries — those scored against observations from the reserved
#: post-2015 test period — come first, with the in-sample ones (climatologies
#: and event diagnostics over the historical record the models were developed
#: against) reported after them and clearly marked.
_WINDOW_ORDER = {WINDOW_HELD_OUT: 0, WINDOW_PERFECT_MODEL: 1, WINDOW_IN_SAMPLE: 2}


def variable_windows(crps: pd.DataFrame) -> dict[str, str]:
    """``var_id -> held-out | in-sample`` from the pass's ``window`` column.

    A variable scored by several diagnostics keeps the first label seen; a
    database written before the pass wrote the column falls back to
    ``held-out``, which is the protocol's default (``tier2.window_labels``).
    """
    labels: dict[str, str] = {}
    if crps.empty or "window" not in crps.columns:
        return labels
    for var_id, window in zip(crps["var_id"], crps["window"], strict=True):
        if isinstance(window, str) and window and str(var_id) not in labels:
            labels[str(var_id)] = window
    return labels


#: CSS class per window label. `perfect-model` shares the held-out styling:
#: it is out-of-sample evidence too, just against a model rather than the
#: real world.
_WINDOW_CLASS = {
    WINDOW_HELD_OUT: "held",
    WINDOW_PERFECT_MODEL: "held",
    WINDOW_IN_SAMPLE: "insample",
}


def _window_class(label: str) -> str:
    return _WINDOW_CLASS.get(label, "insample")


def _window_badge(label: str) -> str:
    return f"<br><small class='badge {_window_class(label)}'>{_esc(label)}</small>"


def _number(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


def _skill_cell(row: pd.Series, clim_crps: float) -> str:
    """One Tier II cell: headline skill, climatology skill, CRPS tooltip."""
    import numpy as np

    crps = _number(row.get("crps"))
    reason = row.get("reason")
    if not np.isfinite(crps):
        note = str(reason) if isinstance(reason, str) and reason else "not scored"
        return f"<td class='na' title='{_esc(note)}'>n/a<br><small>{_esc(note)}</small></td>"

    skill = _number(row.get("skill"))
    e_ref = _number(row.get("e_ref"))
    n_ref = _number(row.get("n_ref_models"))
    lo, hi = _number(row.get("crps_ci_lo")), _number(row.get("crps_ci_hi"))

    tip = [f"fair CRPS {crps:.4g}"]
    if np.isfinite(lo) and np.isfinite(hi):
        tip.append(f"95% CI [{lo:.4g}, {hi:.4g}]")
    se = _number(row.get("crps_se"))
    if np.isfinite(se):
        tip.append(f"SE {se:.3g}")
    members = _number(row.get("n_members"))
    if np.isfinite(members):
        tip.append(f"M = {members:.0f}")
    if np.isfinite(e_ref):
        tip.append(f"E_ref {e_ref:.4g} (median of {n_ref:.0f} CMIP6 models)")

    if np.isfinite(skill):
        shown = max(skill, SKILL_DISPLAY_FLOOR)
        text = f"{shown:+.2f}" + ("" if shown == skill else "▼")
        cls = "num " + ("beats" if skill > 0 else "loses")
    else:
        # No CMIP6 comparison ensemble in this database (the post-2015
        # generator is an upstream gap) — show the raw score instead.
        text = f"{crps:.4g}"
        cls = "num"
        tip.append("no CMIP6 reference ensemble: showing the raw CRPS")

    small = ""
    if np.isfinite(clim_crps) and clim_crps > 0 and row.get("data_type") != "baseline":
        small = f"<br><small>clim {1.0 - crps / clim_crps:+.2f}</small>"
    return f"<td class='{cls}' title='{_esc('; '.join(tip))}'>{text}{small}</td>"


def _skill_table_html(crps: pd.DataFrame) -> str:
    """One Tier II skill table (models x variables) for the given rows."""
    import numpy as np

    # One record per (model, variable); a variable scored by several
    # diagnostics keeps the first row rather than averaging incomparable
    # scores together. A submission that is *also* in the CMIP6 comparison
    # ensemble (the leave-one-out case) is shown as the submission.
    #: Which row wins a (model, variable) collision, and the row order of the
    #: table: the submission first, then the comparison models, baselines last.
    rank = {"to_benchmark": 0, "other": 1, "baseline": 2}
    records: dict[tuple[str, str], pd.Series] = {}
    for _, row in crps.iterrows():
        key = (str(row["data_id"]), str(row["var_id"]))
        kept = records.get(key)
        if kept is None or rank.get(str(row.get("data_type")), 3) < rank.get(
            str(kept.get("data_type")),
            3,
        ):
            records[key] = row
    # Held-out entries first (paper §5.6), each column badged with its label.
    windows = variable_windows(crps)
    variables = sorted(
        {var for (_, var) in records},
        key=lambda v: (_WINDOW_ORDER.get(windows.get(v, WINDOW_HELD_OUT), 0), v),
    )

    def data_type(model: str) -> str:
        return min(
            (str(r.get("data_type")) for (m, _), r in records.items() if m == model),
            key=lambda t: rank.get(t, 3),
        )

    models = sorted(
        {model for (model, _) in records},
        key=lambda m: (rank.get(data_type(m), 3), m),
    )
    clim = {
        var: _number(records[CLIMATOLOGY_DATA_ID, var].get("crps"))
        for var in variables
        if (CLIMATOLOGY_DATA_ID, var) in records
    }

    header = "".join(
        f"<th>{_esc(v)}{_window_badge(windows.get(v, WINDOW_HELD_OUT))}</th>"
        for v in variables
    )
    body = []
    for model in models:
        cells = []
        for var in variables:
            row = records.get((model, var))
            if row is None:
                cells.append("<td class='na'>—</td>")
                continue
            cells.append(_skill_cell(row, clim.get(var, float("nan"))))
        tr_cls = " class='baseline'" if data_type(model) == "baseline" else ""
        body.append(
            f"<tr{tr_cls}><td><strong>{_esc(model)}</strong></td>"
            f"{''.join(cells)}</tr>",
        )

    # How many CMIP6 models stand behind each E_ref (leave-one-out, so the
    # largest value over the column is the size of the reference ensemble).
    counts = []
    for var in variables:
        values = [
            _number(r.get("n_ref_models"))
            for (_, v), r in records.items()
            if v == var
        ]
        finite = [v for v in values if np.isfinite(v)]
        counts.append(
            f"<td class='num'>{max(finite):.0f}</td>" if finite else "<td class='na'>0</td>",
        )
    body.append(
        "<tr><td><em>CMIP6 models behind E_ref</em></td>" + "".join(counts) + "</tr>",
    )

    return (
        f"<table><thead><tr><th>Model</th>{header}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def variable_statuses(crps: pd.DataFrame) -> dict[str, str]:
    """``var_id -> core | extended`` (paper Table 2).

    From the pass's ``variable_status`` column; a row without one (a database
    scored before the column existed) is resolved from
    ``tier2.variable_status`` by its (diagnostic, ``var_id``). A variable
    keeps the first tag seen.
    """
    from climatebench2.scoring_pass import variable_status

    labels: dict[str, str] = {}
    if crps.empty:
        return labels
    stored = (
        crps["variable_status"]
        if "variable_status" in crps.columns
        else [None] * len(crps)
    )
    diagnostics = (
        crps["diagnostic"] if "diagnostic" in crps.columns else [""] * len(crps)
    )
    for var_id, tag, diagnostic in zip(crps["var_id"], stored, diagnostics, strict=True):
        if str(var_id) in labels:
            continue
        labels[str(var_id)] = (
            tag
            if isinstance(tag, str) and tag
            else variable_status(str(diagnostic), str(var_id))
        )
    return labels


def _crps_table_html(crps: pd.DataFrame) -> str:
    if crps.empty:
        return "<p class='na'>No Tier II CRPS scores in the given databases.</p>"

    statuses = variable_statuses(crps)
    is_extended = crps["var_id"].astype(str).map(statuses) == STATUS_EXTENDED
    tables = []
    if (~is_extended).any():
        tables.append(
            "<h3>Core entries</h3>" + _skill_table_html(crps[~is_extended]),
        )
    if is_extended.any():
        tables.append(
            "<h3>Extended entries</h3>"
            "<p>Paper Table 2's Extended variables: reported and scored the "
            "same way, but outside the Core set.</p>"
            + _skill_table_html(crps[is_extended]),
        )
    return (
        "<p>Headline skill <strong>S = 1 − E/E_ref</strong> per variable, "
        "where E is the <strong>fair CRPS</strong> of the model's ensemble and "
        "E_ref the <strong>median</strong> fair CRPS across the CMIP6 "
        "reference models (leave-one-out). S = 0 is median-CMIP6 performance, "
        "S = 1 a perfect match; the score is bounded above but not below and "
        f"the display is clipped at {SKILL_DISPLAY_FLOOR:+.0f} (▼). The small "
        "figure is the same skill against the <em>Climatology</em> baseline. "
        "Hover a cell for the CRPS, its bootstrap interval and the ensemble "
        "size. <strong>n/a</strong> marks a model the protocol cannot score — "
        "most often a single-member submission, for which fair CRPS is "
        "undefined.</p>"
        "<p>Columns are ordered <strong>held-out first</strong> and each is "
        "badged: <span class='badge held'>held-out</span> is scored against "
        "observations from the reserved post-2015 test period, "
        "<span class='badge insample'>in-sample</span> against the historical "
        "record the models were developed with — a climatology, a seasonal or "
        "diurnal cycle, an extremes index or a historical event. Only the "
        "held-out entries are out-of-sample evidence.</p>"
        "<p>Entries are split as in paper Table 2: <strong>Core</strong> "
        "(2 m temperature and its daily extremes, sea surface temperature, "
        "precipitation mean and extremes, TOA fluxes and CRE, sea ice) and, "
        "in their own table, <strong>Extended</strong> (ocean heat content, "
        "surface fluxes, cloud properties, water vapour path) — "
        "<code>tier2.variable_status</code>.</p>"
        + "".join(tables)
    )




def _distribution_table_html(distribution: pd.DataFrame) -> str:
    """Tier II distribution skill — the Perkins score, in its own table.

    Deliberately separate from the skill table: ``S = 1 − E/E_ref`` compares
    an *error* against the CMIP6 median, whereas the Perkins score is an
    overlap in [0, 1] with no reference ensemble behind it. Mixing the two
    into one column would make "1" mean two different things.
    """
    import pandas as pd

    if distribution.empty:
        return ""
    score_cols = sorted(c for c in distribution.columns if c.startswith(PERKINS_PREFIX))
    if not score_cols:
        return ""
    cols = ["data_id", "var_id", *score_cols]
    sub = distribution[[c for c in cols if c in distribution.columns]].sort_values(
        ["var_id", "data_id"],
        kind="stable",
    )
    header = "".join(
        f"<th>{_esc(c.removeprefix(PERKINS_PREFIX).upper() if c in score_cols else c)}</th>"
        for c in sub.columns
    )
    rows = []
    for _, row in sub.iterrows():
        cells = []
        for col in sub.columns:
            value = row[col]
            if isinstance(value, float) and not pd.isna(value):
                cells.append(f"<td class='num'>{value:.3f}</td>")
            else:
                cells.append(f"<td>{_esc(value)}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    badge = f"<span class='badge insample'>{_esc(WINDOW_IN_SAMPLE)}</span>"
    return (
        "<h2>Tier II — distribution skill (Perkins)</h2>"
        f"<p>{badge} Overlap of the modelled and observed daily PDFs, "
        "<code>S = Σ min(f_model, f_obs)</code> over the pre-registered bins "
        "of <code>tier2.perkins.bins</code> (daily <code>tas</code> anomalies "
        "against the fixed base-period monthly climatology; wet-day "
        "<code>pr</code> intensity, ≥ 1 mm/day). 1 is a perfect overlap. "
        "These are computed over the full historical record — in-sample — "
        "and are a <em>skill</em>, not an error: they never enter "
        "<code>E_ref</code> or the headline <code>S = 1 − E/E_ref</code>.</p>"
        f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _consistency_table_html(consistency: pd.DataFrame) -> str:
    import pandas as pd

    if consistency.empty:
        return ""
    cols = [
        "data_id",
        "var_id",
        "window",
        "variable_status",
        "value",
        "z",
        "p_value",
        "sigma_internal",
        "sigma_obs",
        "n_members",
        "passes",
    ]
    cols = [c for c in cols if c in consistency.columns]
    # Held-out consistency statements first, as in the skill table.
    sub = consistency[cols]
    if "window" in cols:
        sub = sub.assign(
            _order=[
                _WINDOW_ORDER.get(str(w), 0) for w in sub["window"]
            ],
        ).sort_values(["_order", "var_id", "data_id"], kind="stable")[cols]
    header = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    rows = []
    for _, row in sub.iterrows():
        cells = []
        for c in cols:
            val = row[c]
            if c == "passes":
                cls = "pass" if val >= 1.0 else "fail"
                cells.append(f"<td class='{cls}'>{'✓' if val >= 1.0 else '✗'}</td>")
            elif c == "window" and isinstance(val, str) and val:
                badge = _window_class(val)
                cells.append(f"<td><span class='badge {badge}'>{_esc(val)}</span></td>")
            elif isinstance(val, float) and not pd.isna(val):
                cells.append(f"<td class='num'>{val:.4g}</td>")
            else:
                cells.append(f"<td>{_esc(val)}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        "<h2>Tier II — ensemble-consistency tests</h2>"
        "<p>Regime (c), a reported diagnostic and falsification check — never "
        "part of the entry ticket or of the headline skill — and the primary "
        "form the protocol asks for the realized warming level in (§II.1). "
        "Held-out statements come first. The observed "
        "statistic is tested against the model's own ensemble, with "
        "σ_total² = var(members) + σ_int² + σ_obs²: σ_int from the piControl "
        "chunks of the <code>internal_variability</code> diagnostic (0 when "
        "no piControl was supplied), σ_obs from <code>tier2.obs_sigma</code> "
        "and the spread across observational products.</p>"
        f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _tier3_scores_html(tier3_scores: pd.DataFrame) -> str:
    """Tier III fair CRPS per (period, dataset, variable).

    The primary Tier III statistic (§III.1): the fair CRPS of the model's
    **block pseudo-ensemble** against one proxy compilation, with the proxy
    σ as the observational uncertainty and an equal-weight mean over sites.
    There is no ``E_ref`` behind it — that needs a PMIP4 comparison ensemble
    — so the table shows the raw score, never a skill.

    ⚠ The rendered note below says the ensemble "does not exist yet (upstream
    ClimateEval PR #45)". Since 2026-09-20 #45 is merged on the
    ``cb2-integration`` branch, so the generator exists and the real reason is
    that **no PMIP4 model pool is staged** for it to find. The wording is page
    text a reader sees, so changing it changes output; update it when a pool
    is staged and the note stops applying.
    """
    import numpy as np

    if tier3_scores.empty:
        return ""
    # The `<key>_skill` companions only carry the "no comparison ensemble"
    # reason; the note below says it once instead of once per row.
    rows = []
    for _, row in tier3_scores.sort_values("var_id", kind="stable").iterrows():
        var_id = str(row.get("var_id", ""))
        if var_id.endswith("_skill"):
            continue
        crps = _number(row.get("crps"))
        dataset_type = str(row.get("dataset_type") or "")
        reason = str(row.get("reason") or "")
        if np.isfinite(crps):
            scored = not reason
            cls = "num" if scored else "num na"
            value = f"{crps:.4g}" + ("" if scored else " †")
        else:
            cls = "na"
            value = "n/a"
        n_sites = _number(row.get("n_sites"))
        n_members = _number(row.get("n_members"))
        rows.append(
            f"<tr><td><strong>{_esc(row.get('data_id', '?'))}</strong></td>"
            f"<td><code>{_esc(var_id)}</code></td>"
            f"<td>{_esc(dataset_type)}</td>"
            f"<td class='{cls}' title='{_esc(reason)}'>{value}</td>"
            f"<td class='num'>{'' if not np.isfinite(n_sites) else f'{n_sites:.0f}'}</td>"
            f"<td class='num'>{'' if not np.isfinite(n_members) else f'{n_members:.0f}'}</td>"
            f"<td>{_esc(reason)}</td></tr>",
        )
    if not rows:
        return ""
    return (
        "<h3>Paleo fair CRPS vs the proxy compilations</h3>"
        "<p>The protocol's primary Tier III score: <strong>fair CRPS</strong> "
        "of the model's block pseudo-ensemble — the climatological anomalies "
        "of non-overlapping blocks of the equilibrated paleo run, minus the "
        "full piControl climatology — against the proxy values, with the "
        "proxy uncertainty as the observational variance term and an "
        "equal-weight mean over sites. Lower is better; there is no skill "
        "score because a PMIP4 comparison ensemble does not exist yet "
        "(upstream ClimateEval PR #45). <strong>†</strong> marks a value that "
        "is reported but <em>not</em> part of the protocol score — a "
        "data-assimilation product, which paper App. D excludes because its "
        "spatial covariances come from the assimilating model.</p>"
        "<table><thead><tr><th>Model</th><th>Period / dataset / variable</th>"
        "<th>Type</th><th>fair CRPS</th><th>Sites</th><th>M</th>"
        f"<th>Note</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _tier3_html(tier3: pd.DataFrame) -> str:
    if tier3.empty:
        return ""
    site_cols = sorted(
        {c for c in tier3.columns if c.endswith("_site_consistency")},
    )
    rows = []
    for _, row in tier3.iterrows():
        for col in site_cols:
            val = row.get(col)
            if val is None or (isinstance(val, float) and val != val):
                continue
            rows.append(
                f"<tr><td><strong>{_esc(row.get('data_id', '?'))}</strong></td>"
                f"<td>{_esc(col.replace('_site_consistency', ''))}</td>"
                f"<td class='num'>{float(val):.0%}</td></tr>",
            )
    if not rows:
        return ""
    return (
        "<h3>Paleo proxy-site consistency</h3>"
        "<p>The complementary diagnostic: the fraction of proxy sites where "
        "the model anomaly is consistent with the proxy within the combined "
        "pseudo-member spread and proxy uncertainty (p ≥ 0.05).</p>"
        "<table><thead><tr><th>Model</th><th>Period / dataset / variable</th>"
        f"<th>Consistent sites</th></tr></thead><tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _tier3_section_html(scores: Scores) -> str:
    """The whole Tier III block: gates, the fair CRPS and the consistency."""
    parts = [
        _tier3_gates_html(scores.gates),
        _tier3_scores_html(scores.tier3_scores),
        _tier3_html(scores.tier3),
    ]
    body = "".join(p for p in parts if p)
    if not body:
        return ""
    return (
        "<h2>Tier III — paleoclimate and perfect-model tests</h2>"
        "<p>Out-of-sample evidence: PMIP4 time slices against the proxy "
        "compilations of paper Appendix D (LGM <strong>Required</strong>; "
        "mid-Holocene and LIG <strong>Extended</strong>), and — for a "
        "submission scored against a held-out ESM — the perfect-model "
        "spread test.</p>" + body
    )


def render_html(scores: Scores, *, source_names: list[str] | None = None) -> str:
    """Render the leaderboard as a self-contained static HTML page."""
    sources = ", ".join(_esc(s) for s in (source_names or []))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClimateBench v2 Leaderboard</title><style>{_CSS}</style></head><body>
<h1>ClimateBench v2 Leaderboard</h1>
<p>Protocol: tiered physical-consistency gates (Tier I), probabilistic
scores against observations (Tier II), out-of-sample paleo tests (Tier III).
Spec: <code>docs/metrics_reference.md</code>.</p>
<h2>Tier I — physical-consistency gates (entry ticket)</h2>
{_gate_matrix_html(scores.gates)}
<h2>Tier II — probabilistic scores vs baselines</h2>
{_crps_table_html(scores.crps)}
{_event_flags_html(scores.gates)}
{_distribution_table_html(scores.distribution)}
{_consistency_table_html(scores.consistency)}
{_tier3_section_html(scores)}
<footer>Generated by <code>climatebench2 leaderboard</code> from: {sources}
</footer></body></html>"""
