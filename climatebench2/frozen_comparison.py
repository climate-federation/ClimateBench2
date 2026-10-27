"""A frozen copy of the CMIP6 comparison ensemble's rows, for scoring on demand.

Why
---
``E_ref`` is the median per-model fair CRPS over the CMIP6 comparison models
(:mod:`climatebench2.scoring_pass`, paper §5.6). A live ``climatebench2
score`` builds it by running every Tier II diagnostic on every comparison
member — ``climatebench2.data.StagedCMIP6HistoricalSSP245``, ~160 members of
42 models — which needs the staged pool on disk and is most of the run time.
None of those rows depends on the submission: ``SimpleDiagnostic.get_output``
computes the comparison stage from the comparison source and the reference
alone (the argument :mod:`climatebench2.shared_sources` already relies on to
compute it once for all members), and the scoring pass re-derives every
comparison model's fair CRPS from ``raw_output`` each time it runs. So a new
submission can be scored against the rows of an *earlier* run:

* ``climatebench2 freeze-comparison DB... -o DIR`` copies the comparison rows
  out of finished result databases into a **bundle** (:func:`freeze`);
* ``climatebench2 score --comparison frozen --frozen-comparison DIR`` runs the
  cube suites with the comparison generators removed from ``other_data``
  (:func:`strip_comparison_ensemble`), merges the bundle into each database
  (:func:`merge`) and only then runs the scoring pass, which forms ``E_ref``
  from the frozen rows exactly as it would from live ones.

What counts as a comparison row
-------------------------------
A ``data_type = 'other'`` row whose ``data_sources.category`` is neither
observational (:data:`~climatebench2.scoring_pass.OBSERVATIONAL_CATEGORIES`)
nor a perfect-model truth — the same predicate by which the scoring pass lets
a group into ``E_ref``. Observational products that ride in ``other_data``
(``IAP`` next to the OHC reference, ``HadISST``/``NOAAERSSTv5`` in the ENSO
gate) are **not** frozen and stay live: they are one source each, and their
spread enters σ_obs, which every score — the submission's included — uses.
That is also why stripping removes only the ``other_data`` entries that are
*generators* (classes with ``generate``, which is how ClimateEval itself tells
an ensemble from a single source), never a plain DataSource.

Bundle layout
-------------
::

    DIR/manifest.json           provenance (below) and per-entry counts
    DIR/<suite stem>.ddb        e.g. ClimateBench2_TierII.ddb

Each ``.ddb`` mirrors ClimateEval's result layout — one schema per diagnostic,
holding ``raw_output`` / ``metrics`` / ``data_sources`` / ``variables`` —
restricted to comparison rows, deduplicated (a per-member run re-inserts them
once per member), plus one extra column, ``cb2_variable``: the suite
variable each row belongs to. ClimateEval's own metrics rows for the
comparison sources (``weighted_rmse`` & co.) are kept; the pass's ``scorer``
rows are not, since the pass rewrites them.

How the merge keys
------------------
On **(suite stem, diagnostic schema, variable id)**. The paper ran the Tier II
suite as split suites — one variable group per job — so one schema
(``annual_mean_timeseries``) arrives from many source databases, one variable
each, while a full-suite run writes it as one wide table with a column per
variable and one row per ``(data_id, time)``. The bundle therefore stores a
wide table *exploded* — one row per variable, the other variable columns NULL
— and the merge re-assembles exactly the rows the run's own variables need,
grouping on the non-variable columns and keeping the non-NULL value per
variable, which is what ClimateEval's ``_remove_superfluous_rows`` does to a
live table. A long table (the EOF coefficients' ``var_id``) and a table whose
columns are derived from one suite variable (``pr_extremes_series``'s eight
Rx1day/Rx5day columns from ``pr``) need no re-assembly.

Provenance: what must match
---------------------------
A frozen ensemble scored against a run it does not match would make ``E_ref``
silently meaningless, so these are checked and a mismatch is an error:

* the bundle format version and ``climatebench2.__version__``;
* the **protocol fingerprint** — a hash of the ``thresholds.yml`` entries that
  shape what a comparison source's rows *are* (:data:`PROTOCOL_THRESHOLDS`:
  windows, baseline, EOF basis, extremes/PDF/diurnal settings). The scoring
  pass's own settings (bootstrap, σ_obs, consistency) are deliberately out of
  it: the pass recomputes everything from ``raw_output``;
* the **nominal Tier II window** the bundle was produced with, which the
  protocol otherwise derives from today's date
  (:func:`climatebench2.windows.test_window_timerange`) — a frozen run with no
  ``--timerange`` takes the bundle's, so the window stops moving every
  January;
* per (schema, variable), the run's ``variables`` row against the source's —
  ``timerange`` (the *resolved* per-variable window, which also depends on how
  far the staged reference reaches), units, masks, regions, preprocessors.
  This one is read from the source database itself, so it is the source run's
  own record, not the freezer's claim.

The CB2 and ClimateEval commits of the freezer are recorded, as are the
source databases' paths, sizes and hashes and free-form ``notes`` (e.g. the
commits the source runs used, from their job logs); a ClimateEval version
difference is reported, not fatal.

Coverage
--------
In a frozen run every (schema, variable) whose comparison generator was
stripped **and** whose rows the scoring pass scores (a time series, an EOF
table or aggregated scalars, with a reference) must have frozen rows; a gap
is an error unless explicitly allowed, because the alternative is a row with
no ``E_ref``. Stripped entries the pass never scores (maps, annual cycles,
zonal lines; ``ReferenceBaselineRecord``, which writes no comparison rows)
only get a note.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from climatebench2._thresholds import get_threshold

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: ``format`` of :data:`MANIFEST`; ``format_version`` bumps on a layout change.
FORMAT = "climatebench2-frozen-comparison"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"

#: Bundle-only column naming the suite variable a row belongs to.
VARIABLE_COLUMN = "cb2_variable"

#: ``thresholds.yml`` entries that change what a comparison source's rows ARE
#: (its load window, its baseline, the EOF basis it is projected on, the
#: extremes/PDF/diurnal definitions). Their hash is the bundle's protocol
#: fingerprint. Anything only the scoring pass reads is left out on purpose.
PROTOCOL_THRESHOLDS = (
    "tier2.test_window_start",
    "tier2.climatology_baseline_period",
    "tier2.climatology_baseline_min_years",
    "tier2.anomaly_baseline",
    "tier2.eof",
    "tier2.extremes",
    "tier2.perkins",
    "tier2.diurnal",
)

#: ``variables`` columns that define a variable's rows (window, units, masks,
#: regions, preprocessing); compared per (schema, variable) at merge time.
VARIABLE_PROVENANCE_COLUMNS = (
    "var_name",
    "frequency",
    "vertical_level",
    "min_lat",
    "max_lat",
    "min_lon",
    "max_lon",
    "landsea_mask",
    "ar6_regions_mask",
    "timerange",
    "units",
    "diagnostic_kwargs",
    "additional_preprocessors",
)

_TABLES = ("raw_output", "metrics", "data_sources", "variables")


class FrozenComparisonError(RuntimeError):
    """A bundle that does not fit the run — never to be scored around."""


# ---------------------------------------------------------------------------
# Suite side: which other_data entries are the comparison ensemble
# ---------------------------------------------------------------------------


def is_comparison_generator(class_path: str) -> bool:
    """Whether an ``other_data`` entry is an ensemble generator.

    ClimateEval's ``SimpleDiagnostic.__init__`` expands an ``other_data``
    class with a ``generate`` method into many sources and instantiates any
    other class as one; the same test separates the comparison ensemble
    (``StagedCMIP6HistoricalSSP245``, ``CMIP6HistoricalR1I1P1F1``) from a
    single observational product.
    """
    from climateeval._utils import str_to_object  # noqa: PLC0415

    return hasattr(str_to_object(class_path), "generate")


def strip_comparison_ensemble(
    definition: list[Any],
) -> tuple[list[Any], dict[str, list[str]]]:
    """Drop the generator entries of every ``other_data`` in a suite definition.

    Returns the new definition (the input, including YAML-anchor-shared
    lists, is not modified) and ``entry name -> [variable id]`` of every
    variable that lost one — the (schema, variable) pairs whose comparison
    rows a frozen run has to bring from the bundle.
    """
    stripped: dict[str, list[str]] = {}
    out: list[Any] = []
    for entry in definition:
        variables = entry.get("variables") if isinstance(entry, dict) else None
        if not isinstance(variables, list):
            out.append(entry)
            continue
        new_variables: list[Any] = []
        for variable in variables:
            if not isinstance(variable, dict) or "other_data" not in variable:
                new_variables.append(variable)
                continue
            others = list(variable["other_data"] or [])
            kept = [o for o in others if not is_comparison_generator(str(o))]
            new = {k: v for k, v in variable.items() if k != "other_data"}
            if kept:
                new["other_data"] = kept
            if len(kept) < len(others):
                var_id = str(variable.get("id", variable.get("var_name")))
                stripped.setdefault(str(entry.get("name")), []).append(var_id)
            new_variables.append(new)
        out.append({**entry, "variables": new_variables})
    return out, stripped


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def protocol_fingerprint() -> tuple[dict[str, Any], str]:
    """``(values, sha256)`` of :data:`PROTOCOL_THRESHOLDS` in this package."""
    values = {key: get_threshold(key) for key in PROTOCOL_THRESHOLDS}
    text = json.dumps(values, sort_keys=True, default=str)
    return values, hashlib.sha256(text.encode()).hexdigest()


def _git_commit(start: Path) -> str | None:
    """HEAD commit of the git checkout containing ``start``, without git.

    Follows a worktree's ``.git`` file and ``commondir``; ``None`` if
    ``start`` is not inside a checkout (an installed wheel).
    """
    for directory in (start, *start.parents):
        dot_git = directory / ".git"
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git_dir = (directory / text.removeprefix("gitdir:").strip()).resolve()
        elif dot_git.is_dir():
            git_dir = dot_git
        else:
            continue
        common = git_dir
        if (git_dir / "commondir").is_file():
            relative = (git_dir / "commondir").read_text(encoding="utf-8").strip()
            common = (git_dir / relative).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head.removeprefix("ref:").strip()
        for base in (git_dir, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(f" {ref}"):
                    return line.split()[0]
        return None
    return None


def software_provenance() -> dict[str, Any]:
    """Versions and checkout commits of ClimateBench2 and ClimateEval."""
    import climateeval  # noqa: PLC0415

    import climatebench2  # noqa: PLC0415

    return {
        "climatebench2_version": climatebench2.__version__,
        "climatebench2_commit": _git_commit(Path(climatebench2.__file__).parent),
        "climateeval_version": getattr(climateeval, "__version__", None),
        "climateeval_commit": _git_commit(Path(climateeval.__file__).parent),
    }


def load_manifest(bundle: Path) -> dict[str, Any]:
    """Read and sanity-check ``bundle/manifest.json``."""
    path = Path(bundle) / MANIFEST
    if not path.is_file():
        msg = f"No frozen comparison bundle at {bundle} (missing {MANIFEST})"
        raise FrozenComparisonError(msg)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        msg = f"{path} is not a {FORMAT} manifest"
        raise FrozenComparisonError(msg)
    return manifest


def check_provenance(manifest: dict[str, Any], *, timerange: str | None) -> list[str]:
    """Fail unless the bundle was made under this run's protocol.

    ``timerange`` is the run's nominal Tier II window (``None``: not known
    yet, not checked). Returns non-fatal notes (a ClimateEval difference).
    """
    import climatebench2  # noqa: PLC0415

    errors: list[str] = []
    if manifest.get("format_version") != FORMAT_VERSION:
        errors.append(
            f"bundle format version {manifest.get('format_version')} != "
            f"{FORMAT_VERSION}",
        )
    software = manifest.get("software", {})
    if software.get("climatebench2_version") != climatebench2.__version__:
        errors.append(
            f"made with ClimateBench2 {software.get('climatebench2_version')}, "
            f"this is {climatebench2.__version__}",
        )
    _values, fingerprint = protocol_fingerprint()
    if manifest.get("protocol_fingerprint") != fingerprint:
        changed = sorted(
            key
            for key, value in _values.items()
            if json.dumps(value, sort_keys=True, default=str)
            != json.dumps(
                manifest.get("protocol_thresholds", {}).get(key),
                sort_keys=True,
                default=str,
            )
        )
        errors.append(
            "protocol fingerprint differs from this package's thresholds.yml "
            f"(changed: {', '.join(changed) or 'unknown'})",
        )
    nominal = manifest.get("nominal_timerange")
    if timerange is not None and nominal is not None and timerange != nominal:
        errors.append(
            f"bundle was produced over the Tier II window {nominal}, this run "
            f"asks for {timerange}",
        )
    if errors:
        msg = "Frozen comparison bundle does not match this run: " + "; ".join(errors)
        raise FrozenComparisonError(msg)
    notes = []
    current = software_provenance()
    for key in ("climateeval_version", "climateeval_commit"):
        if software.get(key) and current.get(key) and software[key] != current[key]:
            notes.append(f"bundle {key} {software[key]} != running {current[key]}")
    return notes


# ---------------------------------------------------------------------------
# DuckDB helpers
# ---------------------------------------------------------------------------


def _q(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _columns(con: Any, catalog: str, schema: str, table: str) -> dict[str, str]:  # noqa: ANN401
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
        "ORDER BY ordinal_position",
        [catalog, schema, table],
    ).fetchall()
    return {str(name): str(dtype) for name, dtype in rows}


def _tables(con: Any, catalog: str) -> dict[str, set[str]]:  # noqa: ANN401
    """``schema -> {table}`` of one attached catalog."""
    from climatebench2.scoring_pass import _SKIP_SCHEMAS  # noqa: PLC0415

    found: dict[str, set[str]] = {}
    for schema, table in con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_catalog = ?",
        [catalog],
    ).fetchall():
        if str(schema).lower() not in _SKIP_SCHEMAS:
            found.setdefault(str(schema), set()).add(str(table))
    return found


def _ref(catalog: str, schema: str, table: str) -> str:
    return f"{_q(catalog)}.{_q(schema)}.{_q(table)}"


def _excluded_categories() -> list[str]:
    from climatebench2.scoring_pass import (  # noqa: PLC0415
        OBSERVATIONAL_CATEGORIES,
        TRUTH_CATEGORY,
    )

    return sorted(OBSERVATIONAL_CATEGORIES | {TRUTH_CATEGORY})


def _comparison_ids(con: Any, catalog: str, schema: str, tables: set[str]) -> list[str]:  # noqa: ANN401
    """``data_id``s of a schema's comparison rows (see module docstring)."""
    raw = _ref(catalog, schema, "raw_output")
    excluded = _excluded_categories()
    marks = ", ".join("?" for _ in excluded)
    if "data_sources" in tables:
        sql = (
            f"SELECT DISTINCT r.data_id FROM {raw} r LEFT JOIN "  # noqa: S608
            f"(SELECT DISTINCT id, category FROM "
            f"{_ref(catalog, schema, 'data_sources')}) d ON r.data_id = d.id "
            f"WHERE r.data_type = 'other' "
            f"AND coalesce(d.category, '') NOT IN ({marks}) ORDER BY 1"
        )
        rows = con.execute(sql, excluded).fetchall()
    else:
        rows = con.execute(
            f"SELECT DISTINCT data_id FROM {raw} WHERE data_type = 'other' "  # noqa: S608
            f"ORDER BY 1",
        ).fetchall()
    return [str(r[0]) for r in rows]


def _variable_ids(con: Any, catalog: str, schema: str, tables: set[str]) -> list[str]:  # noqa: ANN401
    if "variables" not in tables:
        return []
    return [
        str(r[0])
        for r in con.execute(
            f"SELECT DISTINCT id FROM {_ref(catalog, schema, 'variables')} "  # noqa: S608
            f"ORDER BY 1",
        ).fetchall()
    ]


def _variables_rows(
    con: Any,  # noqa: ANN401
    catalog: str,
    schema: str,
    var_id: str,
) -> list[dict[str, Any]]:
    """Distinct ``variables`` rows of one variable, provenance columns only."""
    present = _columns(con, catalog, schema, "variables")
    columns = [c for c in VARIABLE_PROVENANCE_COLUMNS if c in present]
    if not columns:
        return []
    rows = con.execute(
        f"SELECT DISTINCT {', '.join(_q(c) for c in columns)} "  # noqa: S608
        f"FROM {_ref(catalog, schema, 'variables')} WHERE id = ?",
        [var_id],
    ).fetchall()
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _layout(raw_columns: Iterable[str], variable_ids: Iterable[str]) -> str:
    """``long`` (``var_id`` column), ``wide`` (a column per variable) or ``single``."""
    columns = set(raw_columns)
    if "var_id" in columns:
        return "long"
    if columns & set(variable_ids):
        return "wide"
    return "single"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------


def _append(con: Any, target: str, schema: str, table: str, select_sql: str, params: list[Any]) -> int:  # noqa: ANN401, PLR0913
    """Create ``target.schema.table`` from a SELECT, or append to it by name."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(target)}.{_q(schema)}")
    con.execute(f"CREATE OR REPLACE TEMP TABLE cb2_freeze_rows AS {select_sql}", params)
    count = int(con.execute("SELECT count(*) FROM cb2_freeze_rows").fetchone()[0])
    existing = _columns(con, target, schema, table)
    if not existing:
        con.execute(
            f"CREATE TABLE {_ref(target, schema, table)} AS "  # noqa: S608
            f"SELECT * FROM cb2_freeze_rows",
        )
        return count
    for column, dtype in _columns(con, "temp", "main", "cb2_freeze_rows").items():
        if column not in existing:
            con.execute(
                f"ALTER TABLE {_ref(target, schema, table)} "
                f"ADD COLUMN {_q(column)} {dtype}",
            )
    con.execute(
        f"INSERT INTO {_ref(target, schema, table)} BY NAME "  # noqa: S608
        f"SELECT * FROM cb2_freeze_rows",
    )
    return count


def _freeze_schema(  # noqa: C901, PLR0912, PLR0913, PLR0915
    con: Any,  # noqa: ANN401
    schema: str,
    tables: set[str],
    *,
    source_index: int,
    entries: dict[str, Any],
) -> None:
    """Copy one source schema's comparison rows into the open bundle."""
    ids = _comparison_ids(con, "src", schema, tables)
    if not ids:
        return
    con.execute("CREATE OR REPLACE TEMP TABLE cb2_ids (data_id VARCHAR)")
    con.executemany("INSERT INTO cb2_ids VALUES (?)", [[i] for i in ids])
    variable_ids = _variable_ids(con, "src", schema, tables)
    raw_columns = list(_columns(con, "src", schema, "raw_output"))
    layout = _layout(raw_columns, variable_ids)
    raw = _ref("src", schema, "raw_output")
    where = "data_type = 'other' AND data_id IN (SELECT data_id FROM cb2_ids)"

    if layout == "long":
        variables = [
            str(r[0])
            for r in con.execute(
                f"SELECT DISTINCT var_id FROM {raw} WHERE {where} ORDER BY 1",  # noqa: S608
            ).fetchall()
        ]
        selects = [(f"SELECT DISTINCT *, var_id AS {VARIABLE_COLUMN} FROM {raw} WHERE {where}", [])]  # noqa: S608
    elif layout == "wide":
        variables = [v for v in variable_ids if v in raw_columns]
        keys = [c for c in raw_columns if c not in variable_ids]
        selects = [
            (
                f"SELECT DISTINCT {', '.join(_q(c) for c in keys)}, {_q(v)}, "  # noqa: S608
                f"? AS {VARIABLE_COLUMN} FROM {raw} WHERE {where} "
                f"AND {_q(v)} IS NOT NULL",
                [v],
            )
            for v in variables
        ]
    elif len(variable_ids) == 1:
        variables = list(variable_ids)
        selects = [(f"SELECT DISTINCT *, ? AS {VARIABLE_COLUMN} FROM {raw} WHERE {where}", variables)]  # noqa: S608
    else:
        msg = (
            f"cannot tell which of the variables {variable_ids} the comparison "
            f"rows of schema '{schema}' belong to (no var_id column and no "
            f"column per variable)"
        )
        raise FrozenComparisonError(msg)

    clash = [v for v in variables if v in entries.get(schema, {})]
    if clash:
        msg = (
            f"schema '{schema}' variable(s) {clash} arrive from more than one "
            f"source database; pass each (diagnostic, variable) once"
        )
        raise FrozenComparisonError(msg)

    for sql, params in selects:
        _append(con, "bundle", schema, "raw_output", sql, params)

    if "metrics" in tables:
        metric_columns = _columns(con, "src", schema, "metrics")
        if "var_id" in metric_columns or len(variable_ids) == 1:
            variable_expr = "var_id" if "var_id" in metric_columns else "?"
            unscored = "AND scorer IS NULL" if "scorer" in metric_columns else ""
            _append(
                con,
                "bundle",
                schema,
                "metrics",
                f"SELECT DISTINCT *, {variable_expr} AS {VARIABLE_COLUMN} "  # noqa: S608
                f"FROM {_ref('src', schema, 'metrics')} "
                f"WHERE data_id IN (SELECT data_id FROM cb2_ids) {unscored}",
                [] if variable_expr == "var_id" else variable_ids,
            )

    if "data_sources" in tables:
        # The comparison ids, plus any member of the same categories that
        # produced no rows (a variable it lacks): data_sources is per schema.
        sources = _ref("src", schema, "data_sources")
        known = ""
        if _columns(con, "bundle", schema, "data_sources"):
            known = (
                f"AND id NOT IN (SELECT id FROM "
                f"{_ref('bundle', schema, 'data_sources')})"
            )
        _append(
            con,
            "bundle",
            schema,
            "data_sources",
            f"SELECT DISTINCT * FROM {sources} WHERE (id IN (SELECT data_id FROM "  # noqa: S608
            f"cb2_ids) OR category IN (SELECT DISTINCT category FROM {sources} "
            f"WHERE id IN (SELECT data_id FROM cb2_ids))) AND id NOT IN (SELECT "
            f"data_id FROM {raw} WHERE data_type <> 'other') {known}",
            [],
        )

    if "variables" in tables:
        known = ""
        if _columns(con, "bundle", schema, "variables"):
            known = (
                f"AND id NOT IN (SELECT id FROM {_ref('bundle', schema, 'variables')})"
            )
        marks = ", ".join("?" for _ in variables)
        _append(
            con,
            "bundle",
            schema,
            "variables",
            f"SELECT DISTINCT * FROM {_ref('src', schema, 'variables')} "  # noqa: S608
            f"WHERE id IN ({marks}) {known}",
            list(variables),
        )

    for var_id in variables:
        entries.setdefault(schema, {})[var_id] = _entry_summary(
            con,
            schema,
            var_id,
            source_index=source_index,
            layout=layout,
        )


def _entry_summary(
    con: Any,  # noqa: ANN401
    schema: str,
    var_id: str,
    *,
    source_index: int,
    layout: str,
) -> dict[str, Any]:
    """Manifest record of one frozen (schema, variable)."""
    raw = _ref("bundle", schema, "raw_output")
    rows = int(
        con.execute(
            f"SELECT count(*) FROM {raw} WHERE {VARIABLE_COLUMN} = ?",  # noqa: S608
            [var_id],
        ).fetchone()[0],
    )
    members: dict[str, int] = {}
    if _columns(con, "bundle", schema, "data_sources"):
        members = {
            str(name): int(n)
            for name, n in con.execute(
                f"SELECT coalesce(d.name, r.data_id), count(DISTINCT r.data_id) "  # noqa: S608
                f"FROM {raw} r LEFT JOIN (SELECT DISTINCT id, name FROM "
                f"{_ref('bundle', schema, 'data_sources')}) d ON r.data_id = d.id "
                f"WHERE r.{VARIABLE_COLUMN} = ? GROUP BY 1 ORDER BY 1",
                [var_id],
            ).fetchall()
        }
    metrics = 0
    if _columns(con, "bundle", schema, "metrics"):
        metrics = int(
            con.execute(
                f"SELECT count(*) FROM {_ref('bundle', schema, 'metrics')} "  # noqa: S608
                f"WHERE {VARIABLE_COLUMN} = ?",
                [var_id],
            ).fetchone()[0],
        )
    span = None
    if "time" in _columns(con, "bundle", schema, "raw_output"):
        first, last = con.execute(
            f"SELECT min(time), max(time) FROM {raw} WHERE {VARIABLE_COLUMN} = ?",  # noqa: S608
            [var_id],
        ).fetchone()
        span = [str(first), str(last)]
    variables_rows = (
        _variables_rows(con, "src", schema, var_id)
        if _columns(con, "src", schema, "variables")
        else []
    )
    return {
        "source": source_index,
        "layout": layout,
        "variables": variables_rows,
        "timerange": sorted({str(r.get("timerange")) for r in variables_rows}),
        "raw_rows": rows,
        "metrics_rows": metrics,
        "n_models": len(members),
        "n_members": sum(members.values()),
        "members_per_model": members,
        "time_span": span,
    }


def freeze(
    db_paths: Sequence[Path | str],
    out_dir: Path | str,
    *,
    nominal_timerange: str | None = None,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a bundle from finished result databases; returns the manifest.

    ``nominal_timerange`` is the Tier II window the source runs were made
    over (their ``--timerange``, or the protocol default on the day they
    ran) — not recoverable from the databases, so it is the caller's
    statement, and a frozen run with no ``--timerange`` adopts it.
    """
    import duckdb  # noqa: PLC0415

    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        msg = f"{out} is not empty; a bundle is written into a fresh directory"
        raise FrozenComparisonError(msg)
    out.mkdir(parents=True, exist_ok=True)

    values, fingerprint = protocol_fingerprint()
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "software": software_provenance(),
        "protocol_fingerprint": fingerprint,
        "protocol_thresholds": values,
        "nominal_timerange": nominal_timerange,
        "notes": dict(notes or {}),
        "sources": [],
        "databases": {},
    }
    for index, db in enumerate(Path(p).resolve() for p in db_paths):
        if not db.is_file():
            msg = f"Database not found: {db}"
            raise FrozenComparisonError(msg)
        stem = db.stem
        manifest["sources"].append(
            {
                "path": str(db),
                "database": stem,
                "size": db.stat().st_size,
                "sha256": _sha256(db),
            },
        )
        record = manifest["databases"].setdefault(
            stem,
            {"file": f"{stem}.ddb", "entries": {}},
        )
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"ATTACH '{out / record['file']}' AS bundle")
            con.execute(f"ATTACH '{db}' AS src (READ_ONLY)")
            for schema, tables in sorted(_tables(con, "src").items()):
                if "raw_output" in tables:
                    _freeze_schema(
                        con,
                        schema,
                        tables,
                        source_index=index,
                        entries=record["entries"],
                    )
            con.execute("DETACH src")
            con.execute("CHECKPOINT bundle")
        finally:
            con.close()
    for stem, record in manifest["databases"].items():
        path = out / record["file"]
        record["size"] = path.stat().st_size
        record["sha256"] = _sha256(path)
        if not record["entries"]:
            path.unlink()
            Path(str(path) + ".wal").unlink(missing_ok=True)
            record["file"] = None
        del stem
    (out / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=False, default=str) + "\n",
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


@dataclass
class MergeReport:
    """What :func:`merge` did to one database."""

    database: str
    merged: list[tuple[str, str]] = field(default_factory=list)
    raw_rows: int = 0
    missing: list[tuple[str, str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One line for stderr."""
        if not self.merged:
            return f"{self.database}: no frozen comparison rows apply"
        line = (
            f"{self.database}: {self.raw_rows} frozen comparison rows merged "
            f"for {len(self.merged)} (diagnostic, variable) pair(s)"
        )
        if self.missing:
            line += f"; {len(self.missing)} scored pair(s) WITHOUT comparison rows"
        return line


def scored_variables(con: Any, schema: str, variable_ids: Iterable[str]) -> set[str]:  # noqa: ANN401
    """The variables of a run schema the scoring pass will score.

    Mirrors the pass's three shapes (:mod:`climatebench2.scoring_pass`): an
    EOF table, a time series, or aggregated scalars — each only with
    ``reference`` rows. A map, annual cycle or zonal line (an axis column
    and no ``time``) is never scored.
    """
    from climatebench2.scoring_pass import is_scorable_layout  # noqa: PLC0415

    columns = set(_columns(con, "run", schema, "raw_output"))
    raw = _ref("run", schema, "raw_output")
    candidates = set(variable_ids)
    if not candidates or not is_scorable_layout(columns):
        return set()
    if "var_id" in columns:
        return candidates & {
            str(r[0])
            for r in con.execute(
                f"SELECT DISTINCT var_id FROM {raw} WHERE data_type = 'reference'",  # noqa: S608
            ).fetchall()
        }
    wide = [v for v in sorted(candidates) if v in columns]
    if wide:
        return {
            v
            for v in wide
            if con.execute(
                f"SELECT 1 FROM {raw} WHERE data_type = 'reference' "  # noqa: S608
                f"AND {_q(v)} IS NOT NULL LIMIT 1",
            ).fetchone()
        }
    has_reference = con.execute(
        f"SELECT 1 FROM {raw} WHERE data_type = 'reference' LIMIT 1",  # noqa: S608
    ).fetchone()
    return candidates if has_reference else set()


def _check_variables_row(
    con: Any,  # noqa: ANN401
    schema: str,
    var_id: str,
    frozen: list[dict[str, Any]],
) -> str | None:
    """Why the run's ``variables`` row for (schema, var) differs, or ``None``."""
    run_rows = _variables_rows(con, "run", schema, var_id)
    if not run_rows or not frozen:
        return None
    for run_row in run_rows:
        for frozen_row in frozen:
            shared = set(run_row) & set(frozen_row)
            if all(run_row[c] == frozen_row[c] for c in shared):
                break
        else:
            differ = sorted(
                c
                for c in set(run_row) & set(frozen[0])
                if run_row[c] != frozen[0][c]
            )
            return (
                f"'{schema}'/{var_id}: "
                + ", ".join(
                    f"{c} run={run_row[c]!r} frozen={frozen[0][c]!r}" for c in differ
                )
            )
    return None


def _run_comparison_ids(con: Any, schema: str, tables: set[str]) -> list[str]:  # noqa: ANN401
    return _comparison_ids(con, "run", schema, tables)


def strip_comparison_rows(db_path: Path | str) -> int:
    """Delete every comparison row from a result database; returns rows deleted.

    ``raw_output`` rows of the comparison sources, ClimateEval's metrics rows
    for them and their ``data_sources`` rows — the inverse of :func:`merge`,
    used by ``merge --replace`` and to simulate a frozen run on a live
    database. The pass's own ``scorer`` rows are left for the pass to rewrite.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(str(db_path))
    try:
        catalog = str(con.execute("SELECT current_database()").fetchone()[0])
        return _strip(con, catalog, _tables(con, catalog))
    finally:
        con.close()


def _strip(con: Any, catalog: str, schemas: dict[str, set[str]]) -> int:  # noqa: ANN401
    deleted = 0
    for schema, tables in schemas.items():
        if "raw_output" not in tables:
            continue
        ids = _comparison_ids(con, catalog, schema, tables)
        if not ids:
            continue
        con.execute("CREATE OR REPLACE TEMP TABLE cb2_ids (data_id VARCHAR)")
        con.executemany("INSERT INTO cb2_ids VALUES (?)", [[i] for i in ids])
        in_ids = "IN (SELECT data_id FROM cb2_ids)"
        deleted += int(
            con.execute(
                f"DELETE FROM {_ref(catalog, schema, 'raw_output')} "  # noqa: S608
                f"WHERE data_type = 'other' AND data_id {in_ids}",
            ).fetchone()[0],
        )
        if "metrics" in tables:
            unscored = (
                "AND scorer IS NULL"
                if "scorer" in _columns(con, catalog, schema, "metrics")
                else ""
            )
            con.execute(
                f"DELETE FROM {_ref(catalog, schema, 'metrics')} "  # noqa: S608
                f"WHERE data_id {in_ids} {unscored}",
            )
        if "data_sources" in tables:
            # The same rows freeze() takes: the comparison ids and any other
            # source of their categories that produced no rows.
            sources = _ref(catalog, schema, "data_sources")
            con.execute(
                f"DELETE FROM {sources} WHERE id {in_ids} OR (category IN "  # noqa: S608
                f"(SELECT DISTINCT category FROM {sources} WHERE id {in_ids}) "
                f"AND id NOT IN (SELECT data_id FROM "
                f"{_ref(catalog, schema, 'raw_output')}))",
            )
    return deleted


@dataclass
class _Plan:
    """What to merge into one run schema."""

    schema: str
    variables: list[str]
    layout: str
    bundle_variables: list[str]


def _plan(  # noqa: C901, PLR0912, PLR0913
    con: Any,  # noqa: ANN401
    run_schemas: dict[str, set[str]],
    bundle_schemas: dict[str, set[str]],
    entries: dict[str, dict[str, Any]],
    stripped: dict[str, list[str]] | None,
    report: MergeReport,
    *,
    replace: bool,
) -> list[_Plan]:
    """Decide and validate everything before a single row is written."""
    plans: list[_Plan] = []
    mismatches: list[str] = []
    for schema, tables in sorted(run_schemas.items()):
        if "raw_output" not in tables:
            continue
        run_vars = _variable_ids(con, "run", schema, tables)
        frozen = entries.get(schema, {})
        if "raw_output" not in bundle_schemas.get(schema, set()):
            frozen = {}
        take = [v for v in run_vars if v in frozen]
        if stripped is not None:
            wanted = set(stripped.get(schema, ()))
            scored = scored_variables(con, schema, run_vars) & wanted
            report.missing.extend((schema, v) for v in sorted(scored - set(take)))
            for var_id in sorted(wanted - scored - set(take)):
                report.messages.append(
                    f"'{schema}'/{var_id}: not scored by the pass and not in the "
                    f"bundle — no comparison rows (deterministic entry)",
                )
        if not take:
            continue
        for var_id in take:
            why = _check_variables_row(con, schema, var_id, frozen[var_id]["variables"])
            if why:
                mismatches.append(why)
        if _run_comparison_ids(con, schema, tables) and not replace:
            msg = (
                f"schema '{schema}' already holds comparison rows (a live run, or "
                f"merged before); refusing to merge a second copy — use replace"
            )
            raise FrozenComparisonError(msg)
        bundle_columns = [
            c
            for c in _columns(con, "frozen", schema, "raw_output")
            if c != VARIABLE_COLUMN
        ]
        bundle_variables = sorted(frozen)
        layout = _layout(bundle_columns, bundle_variables)
        run_columns = set(_columns(con, "run", schema, "raw_output"))
        needed = (
            [c for c in bundle_columns if c not in bundle_variables]
            if layout == "wide"
            else bundle_columns
        )
        absent = [c for c in needed if c not in run_columns]
        if absent:
            msg = (
                f"schema '{schema}': the run's raw_output has no column(s) "
                f"{absent} that the frozen rows carry — a different diagnostic "
                f"layout, not a mergeable bundle"
            )
            raise FrozenComparisonError(msg)
        plans.append(_Plan(schema, take, layout, bundle_variables))
    if mismatches:
        msg = (
            "Frozen comparison rows were produced with different variable "
            "settings than this run (window, units, masks, ...); E_ref from "
            "them would be meaningless: " + "; ".join(mismatches)
        )
        raise FrozenComparisonError(msg)
    return plans


def _merge_schema(con: Any, plan: _Plan) -> tuple[int, list[str]]:  # noqa: ANN401
    """Insert one schema's frozen rows; returns ``(raw rows, data ids)``."""
    schema = plan.schema
    frozen_raw = _ref("frozen", schema, "raw_output")
    run_raw = _ref("run", schema, "raw_output")
    marks = ", ".join("?" for _ in plan.variables)
    pick = f"{VARIABLE_COLUMN} IN ({marks})"
    bundle_columns = [
        c for c in _columns(con, "frozen", schema, "raw_output") if c != VARIABLE_COLUMN
    ]
    if plan.layout == "wide":
        # Re-assemble one row per (data_id, data_type, coordinates) with every
        # variable of the run's that the source has — the shape ClimateEval's
        # `_remove_superfluous_rows` gives a live multi-variable table.
        keys = [c for c in bundle_columns if c not in plan.bundle_variables]
        run_columns = set(_columns(con, "run", schema, "raw_output"))
        values = [v for v in plan.variables if v in run_columns]
        if not values:
            return 0, []
        select = (
            f"SELECT {', '.join(_q(k) for k in keys)}, "  # noqa: S608
            + ", ".join(f"any_value({_q(v)}) AS {_q(v)}" for v in values)
            + f" FROM {frozen_raw} WHERE {pick} GROUP BY ALL"
        )
    else:
        select = (
            f"SELECT {', '.join(_q(c) for c in bundle_columns)} "  # noqa: S608
            f"FROM {frozen_raw} WHERE {pick}"
        )
    con.execute(f"CREATE OR REPLACE TEMP TABLE cb2_merge_rows AS {select}", plan.variables)
    rows = int(con.execute("SELECT count(*) FROM cb2_merge_rows").fetchone()[0])
    con.execute(f"INSERT INTO {run_raw} BY NAME SELECT * FROM cb2_merge_rows")  # noqa: S608
    ids = [
        str(r[0])
        for r in con.execute(
            "SELECT DISTINCT data_id FROM cb2_merge_rows ORDER BY 1",
        ).fetchall()
    ]

    run_tables = _tables(con, "run").get(schema, set())
    frozen_tables = _tables(con, "frozen").get(schema, set())
    if "metrics" in frozen_tables:
        metric_columns = [
            c for c in _columns(con, "frozen", schema, "metrics") if c != VARIABLE_COLUMN
        ]
        select = (
            f"SELECT {', '.join(_q(c) for c in metric_columns)} "  # noqa: S608
            f"FROM {_ref('frozen', schema, 'metrics')} WHERE {pick}"
        )
        if "metrics" not in run_tables:
            con.execute(
                f"CREATE TABLE {_ref('run', schema, 'metrics')} AS {select}",
                plan.variables,
            )
        else:
            existing = _columns(con, "run", schema, "metrics")
            for column, dtype in _columns(con, "frozen", schema, "metrics").items():
                if column != VARIABLE_COLUMN and column not in existing:
                    # CB2 adds columns to `metrics`, never a schema.
                    con.execute(
                        f"ALTER TABLE {_ref('run', schema, 'metrics')} "
                        f"ADD COLUMN {_q(column)} {dtype}",
                    )
            con.execute(
                f"INSERT INTO {_ref('run', schema, 'metrics')} BY NAME {select}",
                plan.variables,
            )
            ids.extend(
                str(r[0])
                for r in con.execute(
                    f"SELECT DISTINCT data_id FROM {_ref('frozen', schema, 'metrics')} "  # noqa: S608
                    f"WHERE {pick}",
                    plan.variables,
                ).fetchall()
            )

    for table, key, wanted in (
        ("data_sources", "id", sorted(set(ids))),
        ("variables", "id", plan.variables),
    ):
        if table not in frozen_tables or not wanted:
            continue
        columns = list(_columns(con, "frozen", schema, table))
        wanted_marks = ", ".join("?" for _ in wanted)
        select = (
            f"SELECT DISTINCT {', '.join(_q(c) for c in columns)} "  # noqa: S608
            f"FROM {_ref('frozen', schema, table)} WHERE {_q(key)} IN ({wanted_marks})"
        )
        if table not in run_tables:
            con.execute(f"CREATE TABLE {_ref('run', schema, table)} AS {select}", wanted)
            continue
        run_columns = set(_columns(con, "run", schema, table))
        shared = [c for c in columns if c in run_columns]
        con.execute(
            f"INSERT INTO {_ref('run', schema, table)} BY NAME "  # noqa: S608
            f"SELECT {', '.join(_q(c) for c in shared)} FROM ({select}) "
            f"WHERE {_q(key)} NOT IN (SELECT {_q(key)} FROM "
            f"{_ref('run', schema, table)})",
            wanted,
        )
    return rows, ids


def merge(
    db_path: Path | str,
    bundle: Path | str,
    *,
    stripped: dict[str, list[str]] | None = None,
    allow_missing: bool = False,
    replace: bool = False,
) -> MergeReport:
    """Merge a bundle's comparison rows into one result database, in place.

    The bundle database is the one named like ``db_path`` (its suite stem).
    ``stripped`` is ``entry -> [variable]`` from
    :func:`strip_comparison_ensemble`: every such pair the scoring pass will
    score must be covered, else :class:`FrozenComparisonError` — unless
    ``allow_missing``, when those rows simply get no ``E_ref``. ``None`` (a
    standalone merge) requires nothing. ``replace`` first deletes every
    comparison row the database already holds (:func:`strip_comparison_rows`)
    instead of refusing. All or nothing: validation runs before any write,
    and the writes are one transaction.
    """
    import duckdb  # noqa: PLC0415

    bundle = Path(bundle)
    manifest = load_manifest(bundle)
    path = Path(db_path)
    report = MergeReport(database=str(path))
    record = manifest.get("databases", {}).get(path.stem)
    bundle_file = (
        bundle / record["file"] if record and record.get("file") else None
    )
    entries: dict[str, dict[str, Any]] = record["entries"] if record else {}

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{path}' AS run")
        if bundle_file is not None:
            con.execute(f"ATTACH '{bundle_file}' AS frozen (READ_ONLY)")
        run_schemas = _tables(con, "run")
        bundle_schemas = _tables(con, "frozen") if bundle_file is not None else {}
        plans = _plan(
            con,
            run_schemas,
            bundle_schemas,
            entries,
            stripped,
            report,
            replace=replace,
        )
        if report.missing and not allow_missing:
            pairs = ", ".join(f"{s}/{v}" for s, v in report.missing)
            msg = (
                f"{path}: no frozen comparison rows for scored (diagnostic, "
                f"variable) pair(s) {pairs} — their E_ref and skill would be "
                f"empty. Rebuild the bundle with them, or accept that with "
                f"--allow-missing-comparison"
            )
            raise FrozenComparisonError(msg)
        for schema, var_id in report.missing:
            report.messages.append(
                f"'{schema}'/{var_id}: scored, but the bundle has no comparison "
                f"rows — no E_ref, no skill",
            )
        if not plans:
            return report
        con.execute("BEGIN TRANSACTION")
        try:
            if replace:
                _strip(
                    con,
                    "run",
                    {p.schema: run_schemas[p.schema] for p in plans},
                )
            for plan in plans:
                rows, _ids = _merge_schema(con, plan)
                report.raw_rows += rows
                report.merged.extend((plan.schema, v) for v in plan.variables)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return report
