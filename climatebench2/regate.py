"""Re-evaluate Tier I/III gate pass/fail rows against the CURRENT thresholds.yml.

Why a separate pass from ``--rescore``
---------------------------------------
``climatebench2.scoring_pass`` (``leaderboard --rescore``) re-runs the Tier II
probabilistic scoring: it re-derives fair CRPS etc. from ``raw_output`` every
time, so it is naturally idempotent against a ``thresholds.yml`` edit. The
Tier I/III **gates** (:mod:`climatebench2.diags.pass_fail`) are different: a
gate's raw statistic (e.g. ``itcz_efe.raw_output.itcz_efe_r_abs``) is cheap,
but *computing* it means re-running a whole suite against CMOR output — hours
per model. Today, changing a bound in ``thresholds.yml`` after that run has
already happened means the ``passes``/``bound_lower``/``bound_upper`` written
into ``<schema>.metrics`` are frozen at the old bound, and the only way to
update them is to re-run the suite.

This module is the missing update path: for every gate-bearing schema of a
result database, it re-reads the already-computed raw statistic from
``<schema>.raw_output`` and re-evaluates every :class:`GateCheck
<climatebench2.diags.pass_fail.GateCheck>` of the diagnostic class that owns
that schema — using :func:`climatebench2.diags.pass_fail.gate_metrics`, the
*exact* function ``GateMixin.get_output`` calls when the suite first runs, so
"what would this gate say today" is computed identically whether the suite
just ran or ran months ago. Only ``passes``, ``bound_lower``, ``bound_upper``,
``requirement`` and ``tier`` are rewritten, in place; ``value`` (the stored
raw statistic reduced by the check, not the bound) is never touched, and
neither is anything else in ``metrics`` — a Tier II ``scorer`` row
(:data:`climatebench2.scoring_pass.SCORER` /
:data:`climatebench2.scoring_pass.TIER3_SCORER`) or a declared-not-applicable
gate row (``applicable = 0.0``, written by
:func:`climatebench2.diags.pass_fail.not_applicable_metrics`, which has no
backing row in ``raw_output`` at all and so is never matched by a fresh
:func:`~climatebench2.diags.pass_fail.gate_metrics` computation).

Which diagnostic class owns a schema
-------------------------------------
A DuckDB schema written by ``Suite.get_database``
(``ClimateEval src/climateeval/suites/_base.py``) is named after the suite
YAML's ``name:`` entry; the database itself never records the ``diagnostic:``
class path that produced it (no table carries it — see ClimateEval's
``docs/output-schema.md``). So the *only* way to recover "which diagnostic
class owns schema X" is the same YAML ``Suite._get_diagnostics`` reads at run
time: this module rebuilds the ``name -> diagnostic`` map from every suite
shipped in ``climatebench2/suites/*.yml`` (:func:`schema_diagnostic_paths`).
A schema whose name is not a CB2 suite entry — a hand-written suite, or an
upstream ClimateEval suite entry that carries no ``_gate_checks`` at all
(e.g. ``climateeval.diags.simple.Map``) — is skipped, not an error: it is
simply not a gate-bearing schema.

Idempotence and safety
-----------------------
Re-running ``--regate`` with no threshold change re-evaluates the same
checks against the same raw statistics and writes back the same bounds and
the same ``passes`` — a no-op. A gate row is identified as "belongs to
:mod:`pass_fail`'s gate machinery" by two conditions together: its ``var_id``
is one of the owning diagnostic's ``_gate_checks[].check_id`` values, *and*
its ``scorer`` column is NULL/empty — ``GateMixin``/``apply_gate`` never sets
``scorer`` (only :mod:`climatebench2.scoring_pass` and the Tier III paleo
diagnostics' own score rows do), so this can never match a Tier II CRPS row,
a regime-(c) consistency row or a Tier III paleo score row, even though some
of those carry gate-shaped columns (``requirement``/``tier``/``bound_lower``)
of their own (the III.2 large-ensemble spread test, scored by the pass).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger

from climatebench2.diags.pass_fail import GateCheck, gate_metrics, resolved_checks

if TYPE_CHECKING:
    from pathlib import Path

#: DuckDB schemas that are not diagnostics (mirrors scoring_pass._SKIP_SCHEMAS).
_SKIP_SCHEMAS = {"main", "memory", "information_schema", "temp", "system", "pg_catalog"}

#: Gate-row identity columns rewritten by a regate (never ``value``).
_REGATED_COLUMNS = ("passes", "bound_lower", "bound_upper", "requirement", "tier")


@dataclass
class RegateReport:
    """What one database's regate pass did."""

    database: str
    schemas: list[str] = field(default_factory=list)
    rows_evaluated: int = 0
    rows_changed: int = 0
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One-line human-readable summary."""
        if not self.schemas:
            return f"{self.database}: no gate-bearing schemas found"
        return (
            f"{self.database}: {self.rows_evaluated} gate row(s) re-evaluated "
            f"across {len(self.schemas)} schema(s), {self.rows_changed} pass/fail "
            f"flip(s)"
        )


def schema_diagnostic_paths() -> dict[str, str]:
    """``suite entry name -> dotted diagnostic class path``.

    Read from every ``*.yml`` of the active protocol's suites packages
    (``climatebench2.suites`` for ClimateBench2) — the same source
    ``Suite._get_diagnostics`` builds its ``diag_name -> diag_cls`` map from
    (ClimateEval ``src/climateeval/suites/_base.py``). A ``name:`` reused
    across suites (e.g. ``reference_baseline``/``sst_baseline`` both naming
    :class:`~climatebench2.diags.ReferenceBaselineRecord`) is harmless: it is
    the same class either way. A ``name:`` that means two *different* classes
    across suites would be a suite-authoring bug; the later file wins and is
    not specially detected, exactly as running both suites into one database
    would silently overwrite the first's schema.
    """
    import yaml

    from climatebench2.protocol import get_protocol

    mapping: dict[str, str] = {}
    # Every package of the active protocol's `suites_packages`; walked last to
    # first so that, as in `_cli._resolve_suite`, an earlier package wins a name
    # two of them define.
    for package in reversed(get_protocol().suites_packages):
        for entry in resources.files(package).iterdir():
            if not entry.name.endswith((".yml", ".yaml")):
                continue
            definition = yaml.safe_load(entry.read_text(encoding="utf-8"))
            for stanza in definition or ():
                name = stanza.get("name")
                diagnostic = stanza.get("diagnostic")
                if name and diagnostic:
                    mapping[str(name)] = str(diagnostic)
    return mapping


def gate_checks_for_schema(
    schema: str,
    schema_map: dict[str, str] | None = None,
) -> tuple[GateCheck, ...] | None:
    """Resolved ``_gate_checks`` of the diagnostic class that owns ``schema``.

    ``None`` means the schema is not a CB2 suite entry at all (unknown
    diagnostic); an empty tuple means it is a known diagnostic with no gates
    (e.g. ``ScoredAnnualMeanTimeSeries``, ``PaleoProxyScore`` without its own
    hard requirement, or an upstream ``climateeval.diags.simple`` class).
    Both are "not gate-bearing", but the caller reports them differently
    (unknown vs. known-non-gate) so a suite-registry gap is visible.
    """
    from climateeval._utils import str_to_object

    if schema_map is None:
        schema_map = schema_diagnostic_paths()
    class_path = schema_map.get(schema)
    if class_path is None:
        return None
    cls = str_to_object(class_path)
    return resolved_checks(cls)


def _table_columns(con: Any, schema: str, table: str) -> set[str]:  # noqa: ANN401
    return {
        str(row[0])
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchall()
    }


def _diagnostic_schemas(con: Any) -> list[str]:  # noqa: ANN401
    return [
        str(row[0])
        for row in con.execute(
            "SELECT schema_name FROM information_schema.schemata",
        ).fetchall()
        if str(row[0]).lower() not in _SKIP_SCHEMAS
    ]


def _schema_tables(con: Any, schema: str) -> set[str]:  # noqa: ANN401
    return {
        str(row[0])
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            [schema],
        ).fetchall()
    }


#: The gate row's identity columns, in the order ``gate_metrics`` writes them
#: (:func:`climatebench2.diags.pass_fail._gate_row`); a regate matches an
#: existing ``metrics`` row to a freshly computed one by these, content-keyed
#: rather than by DuckDB row order — see :func:`_regate_schema`.
_KEY_COLUMNS = ("data_id", "data_type", "var_id")


def regated_rows(
    raw_df: pd.DataFrame,
    metrics_df: pd.DataFrame,
    checks: tuple[GateCheck, ...],
) -> tuple[pd.DataFrame, list[str]]:
    """Pure-pandas core of one schema's regate: what changes, and why not.

    Returns ``(updates, messages)``: ``updates`` has one row per existing
    gate row whose ``passes``/``bound_lower``/``bound_upper``/``requirement``/
    ``tier`` should be rewritten, keyed by :data:`_KEY_COLUMNS` plus a
    ``changed`` bool column (whether ``passes`` actually flipped).
    ``messages`` names any check whose raw column is entirely absent from
    ``raw_df`` — the audit warning.
    """
    messages: list[str] = []
    empty = pd.DataFrame(columns=[*_KEY_COLUMNS, *_REGATED_COLUMNS, "changed"])
    if metrics_df.empty or not checks or "var_id" not in metrics_df.columns:
        return empty, messages
    check_by_id = {check.check_id: check for check in checks}
    check_ids = set(check_by_id)

    scorer = (
        metrics_df["scorer"]
        if "scorer" in metrics_df.columns
        else pd.Series([""] * len(metrics_df), index=metrics_df.index)
    )
    is_gate_row = metrics_df["var_id"].isin(check_ids) & (
        scorer.isna() | (scorer.astype(str) == "")
    )
    gate_rows = metrics_df[is_gate_row]
    if gate_rows.empty:
        return empty, messages

    for check_id in sorted(check_ids & set(gate_rows["var_id"])):
        column = check_by_id[check_id].column
        if column not in raw_df.columns:
            n = int((gate_rows["var_id"] == check_id).sum())
            messages.append(
                f"check '{check_id}': raw column '{column}' is absent from "
                f"raw_output — {n} gate row(s) left untouched",
            )

    fresh = gate_metrics(raw_df, checks)
    if fresh.empty:
        return empty, messages
    key_cols = [c for c in _KEY_COLUMNS if c in fresh.columns]
    fresh = fresh.set_index(key_cols)

    updates: list[dict[str, Any]] = []
    for _index, row in gate_rows.iterrows():
        key = tuple(row.get(c, "") for c in key_cols)
        lookup_key = key[0] if len(key_cols) == 1 else key
        if lookup_key not in fresh.index:
            # Either this check's column is missing (already warned above) or
            # this particular source has no finite value for it (e.g. an
            # n/a-declared row, which never had a matching raw_output row to
            # begin with) — either way, nothing to rewrite.
            continue
        new = fresh.loc[lookup_key]
        old_passes = row.get("passes")
        changed = bool(pd.isna(old_passes)) != bool(pd.isna(new["passes"])) or (
            not pd.isna(old_passes)
            and not pd.isna(new["passes"])
            and bool(old_passes) != bool(new["passes"])
        )
        updates.append(
            {
                **dict(zip(key_cols, key, strict=True)),
                "passes": new["passes"],
                "bound_lower": new["bound_lower"],
                "bound_upper": new["bound_upper"],
                "requirement": new["requirement"],
                "tier": new["tier"],
                "changed": changed,
            },
        )
    if not updates:
        return empty, messages
    return pd.DataFrame(updates), messages


def _regate_schema(
    con: Any,  # noqa: ANN401
    schema: str,
    checks: tuple[GateCheck, ...],
    report: RegateReport,
) -> None:
    raw_df = con.execute(f'SELECT * FROM "{schema}"."raw_output"').df()
    metrics_df = con.execute(f'SELECT * FROM "{schema}"."metrics"').df()
    updates, messages = regated_rows(raw_df, metrics_df, checks)
    for message in messages:
        logger.warning(f"Diagnostic schema '{schema}': {message}")
        report.messages.append(f"{schema}: {message}")
    if updates.empty:
        return

    key_cols = [c for c in _KEY_COLUMNS if c in updates.columns]
    has_scorer = "scorer" in _table_columns(con, schema, "metrics")
    join = " AND ".join(f'm."{c}" = u."{c}"' for c in key_cols)
    scorer_guard = ' AND (m."scorer" IS NULL OR m."scorer" = \'\')' if has_scorer else ""

    con.register("cb2_regate_updates", updates)
    try:
        con.execute(
            f'UPDATE "{schema}"."metrics" AS m '  # noqa: S608
            f'SET "passes" = u."passes", "bound_lower" = u."bound_lower", '
            f'"bound_upper" = u."bound_upper", "requirement" = u."requirement", '
            f'"tier" = u."tier" '
            f"FROM cb2_regate_updates AS u WHERE {join}{scorer_guard}",
        )
    finally:
        con.unregister("cb2_regate_updates")

    report.schemas.append(schema)
    report.rows_evaluated += len(updates)
    report.rows_changed += int(updates["changed"].sum())


def regate_database(db_path: Path | str) -> RegateReport:
    """Re-evaluate every gate row of one results database, in place.

    Idempotent: re-running with no ``thresholds.yml`` change recomputes the
    same bounds and the same ``passes`` from the same raw statistics, so
    nothing changes on the second pass.
    """
    import duckdb

    path = str(db_path)
    report = RegateReport(database=path)
    schema_map = schema_diagnostic_paths()
    con = duckdb.connect(path)
    try:
        for schema in _diagnostic_schemas(con):
            tables = _schema_tables(con, schema)
            if "raw_output" not in tables or "metrics" not in tables:
                continue
            checks = gate_checks_for_schema(schema, schema_map)
            if not checks:
                continue
            _regate_schema(con, schema, checks, report)
    finally:
        con.close()
    return report


def regate_databases(db_paths: list[Path] | list[str]) -> list[RegateReport]:
    """Run :func:`regate_database` over several databases."""
    return [regate_database(path) for path in db_paths]
