"""The Tier II scoring pass over a finished results database.

Why a pass and not a diagnostic
-------------------------------
Ensemble members are ingested as **separate data sources** (``--member`` /
DRS auto-discovery run every cube suite once per member, appending rows whose
``data_id``s differ only by ``variant``). A model's fair CRPS therefore can
only be formed once *all* of its members have run — which is after the suite,
not inside a diagnostic. This module is that post-processing step:
``climatebench2 score`` runs it on the databases it just wrote, and
``climatebench2 leaderboard --rescore`` re-runs it on existing ones. It is
also where the σ_int of the Tier I database meets the Tier II members, which
is why the regime-(c) consistency test lives here too.

What it computes (metrics_reference.md Tier II preamble, §II.0)
---------------------------------------------------------------
**Regime (a) — time series.** For every ``raw_output`` with a ``time``
column and a ``reference`` data source, and every variable in it:

0. where the protocol says the entry is scored on **anomalies**
   (``tier2.anomaly_baseline``, keyed by suite entry name), reduce every
   ``data_id`` — each ensemble member, the reference and every comparison
   member — to an anomaly about **its own** climatology over
   ``tier2.climatology_baseline_period`` (per calendar month for a monthly
   series), then cut the table to ``tier2.test_window_start`` onwards. The
   paper defines regime (a) over anomaly series; scoring the absolute
   series instead makes the fair CRPS a measure of each field's mean-state
   bias. A source that does not reach far enough into the baseline window
   is **not scored at all** — it gets a ``reason`` row, never a silent
   fall back to absolute values (:func:`anomalise_raw_output`);
1. group the non-reference rows **by model name** — the ``data_sources``
   table maps each ``data_id`` to ``(name, category)``, so the members of one
   model come back together, the CMIP6 comparison models stay apart, and an
   **observational product** (category ``observation``/``reanalysis`` sitting
   in ``other_data``, e.g. HadISST next to the ESACCI-SST reference) is never
   scored as if it were a comparison model;
2. stack a model's members on the times they share with the reference and
   score them with the **fair CRPS** per step (``M >= 2``; a single-member
   model gets a row with ``crps = NaN`` and ``reason = "single member"``,
   since fair CRPS is undefined for a deterministic forecast);
3. observational uncertainty enters as **common draws** around the observed
   series: σ_obs is the protocol floor for the variable
   (``tier2.obs_sigma``) combined in quadrature with the per-time-step
   **spread across observational products**, and every model and baseline
   sees the same draws (``tier2.obs_uncertainty.seed``);
4. summarise: time-mean score, ESS-corrected standard error, and a
   moving-block-bootstrap confidence interval (``tier2.bootstrap``);
5. add the **Climatology** baseline — the distribution of the reference's
   1985-2014 values for each calendar month, shifted into the same anomaly
   space as everything else, so it is the protocol's "no change since
   1985-2014" forecast. Those values come from the ``reference_baseline``
   rows that :class:`climatebench2.diags.ReferenceBaselineRecord` writes,
   or from the reference's own pre-test rows where no such entry exists
   (OHC, the sea-ice extents) — and, for a
   GMST-type annual series, the **PatternScaling** baseline: a two-layer EBM
   driven by the packaged ERF table with one parameter calibrated on the
   observations through 2014, displaced into pseudo-members by the detrended
   residuals of the baseline window;
6. set ``E_ref`` = **median of the per-model fair CRPS over the CMIP6
   comparison models** with ``M >= 2``, excluding any comparison model whose
   name equals the scored model's (leave-one-out, paper §5.6), and write the
   skill ``S = 1 - E_model / E_ref`` — bounded above, unbounded below.

**Aggregated scalars.** For a ``raw_output`` with **no time axis** — one
number per data source and ``var_id``, the shape a CB2 complex diagnostic
writes (``_scalar_outputs``) — plus ``reference`` rows carrying the observed
value of the same scalar, the same machinery runs with a single-point scoring
axis: fair CRPS of the members' values against the observation, ``E_ref`` and
the skill as elsewhere, and the regime-(c) consistency test with σ_int looked
up per scalar from ``tier2.scalar_consistency``. This is how the realized
warming level, the two GMST trends, the Pinatubo tas anomaly and the
hemispheric-asymmetry trend of §II.1 are scored.

**Regime (b) — spatial fields.** For a ``raw_output`` of EOF coefficients
(:class:`climatebench2.diags.ReferenceEOFProjection`: one row per source,
variable and mode on the reference's fixed pre-2015 basis, each coefficient
standardised by its pre-2015 σ), the same machinery runs with **mode** in
place of time: fair CRPS per coefficient, equal-weight mean over the
retained modes as the variable-level score, the same ``E_ref``/skill.

**Regime (c) — consistency.** For every annual series, one row per model:
the observed OLS trend against the distribution of its members' trends, with
σ_total² = var(members) + σ_int² + σ_obs² — σ_int from the piControl chunks
of :class:`climatebench2.diags.InternalVariability` (Tier I database, passed
alongside), σ_obs from the same observational-uncertainty terms as (a).

Every row also carries a ``window`` label — ``held-out`` or ``in-sample`` —
resolved from ``tier2.window_labels`` by (diagnostic, ``var_id``), which is
the paper's §5.6 scorecard rule: the leaderboard states, per entry, whether
the observations it was scored against predate the reserved test period.
Every Tier II row also carries ``variable_status`` — ``core`` or ``extended``
(paper Table 2), from ``tier2.variable_status`` — which the leaderboard uses
to show the Extended entries apart.

Rows are appended to the diagnostic's own ``metrics`` table (never a new
schema): missing columns are added with ``ALTER TABLE ADD COLUMN``, and every
row carries ``scorer = "climatebench2"`` so a re-run deletes its predecessors
first and is idempotent.

The pooled ``CMIP6-MME`` row of earlier revisions is gone: a pooled
multi-model mixture is overdispersed (its spread is structural disagreement,
not internal variability), which inflates ``E_ref`` — the doc's ⚠ rules it
out in favour of the median of per-model scores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from climatebench2 import baselines, scoring, windows
from climatebench2._thresholds import get_threshold

if TYPE_CHECKING:
    from pathlib import Path

#: Value of the ``scorer`` column on every row this pass writes. Rows with
#: this tag are deleted before a re-run, which makes the pass idempotent.
SCORER = "climatebench2"

#: ``scorer`` tag of the score rows the **Tier III paleo diagnostics** write
#: for themselves (:mod:`climatebench2.diags.tier3_paleo`). Their pseudo-
#: ensemble is built inside the diagnostic from blocks of one run, so there
#: is nothing for the pass to stack afterwards — but the rows use the same
#: column vocabulary (:data:`_SCORE_COLUMNS`) so the leaderboard reads one
#: schema. The distinct tag matters: :func:`_write_rows` deletes rows with
#: ``scorer = SCORER`` before re-inserting, and must not delete these.
TIER3_SCORER = "climatebench2.tier3"

#: ``data_id`` of the climatology-baseline row.
CLIMATOLOGY_DATA_ID = "Climatology"

#: ``data_id`` of the pattern-scaling baseline row (work package 6b). For a
#: **GMST-type annual series** it is a real score: the two-layer EBM driven by
#: the packaged ERF table, one parameter calibrated on the observations
#: through 2014, given pseudo-members so fair CRPS is defined
#: (:func:`_pattern_scaling_row`). For a **spatial field** it is still a
#: ``reason`` row: the normalized CMIP6 multi-model-mean warming pattern needs
#: the comparison models' **baseline-window maps**, which nothing writes --
#: ``ReferenceEOFProjection`` projects only each source's test-window anomaly
#: (:func:`_eof_pattern_scaling_row`). The ``reason`` text names upstream
#: ClimateEval PR #44, which was the blocker until the staged comparison
#: ensemble landed (2026-09-20); it is left alone because it is written into
#: result databases, but the remaining work is CB2's, not upstream's.
PATTERN_SCALING_DATA_ID = "PatternScaling"

#: ``data_id`` of the III.2 large-ensemble spread rows. Not a model: the test
#: is a statement about the *relationship* between two ensembles.
LE_SPREAD_DATA_ID = "LargeEnsembleSpread"

#: ``data_type`` of the reference's pre-test baseline-window rows, written by
#: ``climatebench2.diags.ReferenceBaselineRecord`` (which imports these).
#: They are a *sample*, never a target: nothing is ever scored against them.
BASELINE_MONTHLY_DATA_TYPE = "reference_baseline"
BASELINE_ANNUAL_DATA_TYPE = "reference_baseline_annual"

#: ``data_sources.category`` values that mark an observational product rather
#: than a model. Everything else in ``other_data`` (CMIP6, …) is a comparison
#: model and enters ``E_ref``.
OBSERVATIONAL_CATEGORIES = frozenset({"observation", "reanalysis"})

#: DuckDB schemas that are not diagnostics.
_SKIP_SCHEMAS = {"main", "memory", "information_schema", "temp", "system", "pg_catalog"}

def _coordinate_column_names() -> frozenset[str]:
    """Every column name a ClimateEval coordinate can appear under.

    A diagnostic's ``raw_output`` carries its final coordinates as columns
    beside the variables — ``month_number`` for an annual cycle, ``latitude``
    and ``longitude`` for a map, ``latitude`` for a zonal line. Those are the
    table's **axes**, never quantities to score, so they are excluded from the
    variable columns and they disqualify a table from the aggregated-scalar
    regime. Read from ClimateEval's coordinate registry rather than hard-coded
    so a new coordinate cannot silently start being scored.
    """
    names = {"data_id", "data_type", "time"}
    try:
        from climateeval._variable import COORDINATES  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - fall back to the built-in list
        return frozenset(names | {"month_number", "latitude", "longitude"})
    for key, info in COORDINATES.items():
        names.add(str(key))
        for attribute in ("standard_name", "long_name"):
            value = info.get(attribute) if isinstance(info, dict) else None
            if value:
                names.add(str(value))
    return frozenset(names)


#: raw_output columns that are not variables: the identity columns plus every
#: coordinate axis (:func:`_coordinate_column_names`).
_META_COLUMNS = _coordinate_column_names()

#: Just the coordinate axes, without the identity columns.
_AXIS_COLUMNS = _META_COLUMNS - {"data_id", "data_type"}

#: ``Diagnostic._get_raw_output_table`` builds its frame with
#: ``as_data_frame(cube).reset_index()``, so a *scalar* cube (no coordinates)
#: contributes an anonymous index column — ``level_0`` — carrying 0.0 for
#: every source. It is not a variable and must never be scored.
_INDEX_COLUMN_RE = re.compile(r"^(index|level_\d+)$")

#: Columns that identify an EOF-coefficient raw_output table (regime b).
_EOF_COLUMNS = {"mode", "coefficient", "var_id"}

#: Suffix of the companion column an aggregated-scalar diagnostic may emit
#: next to a scalar to declare that scalar's own observational sigma (e.g.
#: ``gmst_warming_level_sigma_obs``, the GSAT blending term). It is combined
#: in quadrature with the ``tier2.obs_sigma`` floor and is never itself
#: scored.
SCALAR_SIGMA_OBS_SUFFIX = "_sigma_obs"

#: Suffix marking a regime-(c) row of an aggregated scalar (the time-series
#: trends use :data:`TREND_CONSISTENCY_SUFFIX`).
SCALAR_CONSISTENCY_SUFFIX = "_consistency"

#: The two labels of the paper's scorecard rule (§5.6): whether the
#: observations a row is scored against were available before the reserved
#: test period (``in-sample``) or not (``held-out``).
WINDOW_HELD_OUT = "held-out"
WINDOW_IN_SAMPLE = "in-sample"

#: Label of a row scored against a **held-out ESM run** rather than against
#: observations (metrics_reference.md §III.2). It is neither of the other
#: two: the truth is known exactly (σ_obs = 0) and the target is a model, so
#: the number is not evidence about the real world at all.
WINDOW_PERFECT_MODEL = "perfect-model"

#: ``data_sources.category`` of a perfect-model truth run
#: (:mod:`climatebench2.diags.truth_reference`). A truth source is never a
#: comparison model: it must not enter ``E_ref``.
TRUTH_CATEGORY = "truth"

#: Minimum overlapping time steps for a score to be meaningful.
_MIN_OVERLAP = 3

#: Minimum members for a fair CRPS.
_MIN_MEMBERS = 2

#: Median monthly-step length used to tell a monthly series from an annual one.
_MONTHLY_MAX_DAYS = 200

#: Column names of ``InternalVariability``: ``<var>_sigma_int_<stat>_<window>``.
_SIGMA_INT_RE = re.compile(
    r"^(?P<var>.+)_sigma_int_(?P<statistic>mean|trend)_(?P<window>[a-z]+)$",
)

#: Suffix marking a regime-(c) row in ``var_id``.
TREND_CONSISTENCY_SUFFIX = "_trend_consistency"

#: ``reason`` written for a source that cannot be reduced to an anomaly
#: because its record does not reach far enough into the baseline window.
#: The protocol never falls back to absolute values: a mean-state bias would
#: dominate the fair CRPS and the row would silently mean something else.
NO_BASELINE_REASON = "no baseline window"

#: The scoring columns, with their DuckDB types. Text columns stay text;
#: everything else is DOUBLE so the table remains numeric-friendly.
_SCORE_COLUMNS: dict[str, str] = {
    "data_id": "VARCHAR",
    "data_type": "VARCHAR",
    "var_id": "VARCHAR",
    "scorer": "VARCHAR",
    "reason": "VARCHAR",
    # Held-out / in-sample (paper §5.6), from `tier2.window_labels`.
    "window": "VARCHAR",
    # Core / extended (paper Table 2), from `tier2.variable_status`.
    "variable_status": "VARCHAR",
    "crps": "DOUBLE",
    "crps_se": "DOUBLE",
    "crps_ci_lo": "DOUBLE",
    "crps_ci_hi": "DOUBLE",
    "t_eff": "DOUBLE",
    "r1": "DOUBLE",
    "n_members": "DOUBLE",
    "n_time": "DOUBLE",
    "block_length": "DOUBLE",
    "e_ref": "DOUBLE",
    "n_ref_models": "DOUBLE",
    "skill": "DOUBLE",
    # Regime (c) — the consistency test, same table, same `scorer` tag.
    "value": "DOUBLE",
    "z": "DOUBLE",
    "p_value": "DOUBLE",
    "passes": "DOUBLE",
    "ensemble_mean": "DOUBLE",
    "total_sigma": "DOUBLE",
    "sigma_internal": "DOUBLE",
    "sigma_obs": "DOUBLE",
    # Tier III (metrics_reference.md §III.1/§III.2). The paleo diagnostics
    # write their own score rows in this vocabulary (`TIER3_SCORER`); the
    # scoring axis there is the set of proxy SITES, so `n_sites` accompanies
    # `n_time`, and `dataset_type` carries the App. D distinction between a
    # raw proxy compilation (scored) and a data-assimilation product
    # (computed and reported, excluded from the protocol score).
    "n_sites": "DOUBLE",
    "dataset_type": "VARCHAR",
    # Gate-shaped columns, so the pass can emit a pass/fail row of its own
    # (the III.2 large-ensemble spread test) into the same table and the
    # leaderboard can place it by tier without a second schema.
    "requirement": "VARCHAR",
    "tier": "VARCHAR",
    "applicable": "DOUBLE",
    "bound_lower": "DOUBLE",
    "bound_upper": "DOUBLE",
}

#: Columns of :data:`_SCORE_COLUMNS` that must always hold a string: a NaN in
#: a VARCHAR column fails the DuckDB insert.
_TEXT_COLUMNS = frozenset(
    key for key, dtype in _SCORE_COLUMNS.items() if dtype == "VARCHAR"
)


@dataclass
class PassReport:
    """What the pass did to one database."""

    database: str
    diagnostics: list[str] = field(default_factory=list)
    rows: int = 0
    scored_models: int = 0
    single_member: int = 0
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One-line human-readable summary."""
        if not self.diagnostics:
            return f"{self.database}: no scorable time-series diagnostics"
        return (
            f"{self.database}: {self.rows} score rows across "
            f"{len(self.diagnostics)} diagnostic(s); {self.scored_models} model(s) "
            f"with M >= {_MIN_MEMBERS}, {self.single_member} single-member"
        )


# ---------------------------------------------------------------------------
# Grouping helpers (pure pandas — unit-testable without a database)
# ---------------------------------------------------------------------------


def _source_column(
    data_sources: pd.DataFrame | None,
    column: str,
) -> dict[str, str]:
    """``data_id -> <column>`` from a ``data_sources`` table."""
    if data_sources is None or data_sources.empty:
        return {}
    if not {"id", column} <= set(data_sources.columns):
        return {}
    return {
        str(row_id): str(value)
        for row_id, value in zip(
            data_sources["id"],
            data_sources[column],
            strict=True,
        )
    }


def source_names(data_sources: pd.DataFrame | None) -> dict[str, str]:
    """``data_id -> model name`` from a ``data_sources`` table.

    The id is ``"_".join(category, name, exp, variant)`` (empty parts
    dropped), so two ensemble members of one model differ only in their
    ``variant`` and must be mapped back to the same name before scoring.
    """
    return _source_column(data_sources, "name")


def source_categories(data_sources: pd.DataFrame | None) -> dict[str, str]:
    """``data_id -> category`` (``observation`` / ``CMIP6`` / ``model`` / …).

    The category is what separates an observational product sitting in a
    variable's ``other_data`` (a second estimate of the truth, and a term in
    σ_obs) from a comparison model (a forecast, and a term in ``E_ref``).
    """
    return _source_column(data_sources, "category")


def group_members(
    raw_df: pd.DataFrame,
    names: dict[str, str],
) -> dict[tuple[str, str], list[pd.DataFrame]]:
    """``(data_type, model name) -> [member frames]`` for the scorable rows.

    Reference rows are excluded (they are the target, not a forecast), as are
    the reference's pre-test baseline-window rows (a sample, not a forecast).
    A ``data_id`` absent from ``data_sources`` keeps its id as its name, so a
    hand-built or older database still groups sensibly (one member each).
    """
    groups: dict[tuple[str, str], list[pd.DataFrame]] = {}
    for (data_id, data_type), frame in raw_df.groupby(
        ["data_id", "data_type"],
        sort=True,
    ):
        if str(data_type) in (
            "reference",
            BASELINE_MONTHLY_DATA_TYPE,
            BASELINE_ANNUAL_DATA_TYPE,
        ):
            continue
        name = names.get(str(data_id), str(data_id))
        groups.setdefault((str(data_type), name), []).append(
            frame.sort_values("time"),
        )
    return groups


def group_ids(
    raw_df: pd.DataFrame,
    names: dict[str, str],
) -> dict[tuple[str, str], list[str]]:
    """``(data_type, model name) -> [data_id]`` — the ids behind each group."""
    ids: dict[tuple[str, str], list[str]] = {}
    for data_id, data_type in {
        (str(i), str(t))
        for i, t in zip(raw_df["data_id"], raw_df["data_type"], strict=True)
    }:
        name = names.get(data_id, data_id)
        ids.setdefault((data_type, name), []).append(data_id)
    return ids


def is_observational(
    data_type: str,
    data_ids: list[str],
    categories: dict[str, str],
) -> bool:
    """Whether a ``other`` group is an observational product, not a model."""
    if data_type != "other":
        return False
    return any(categories.get(i, "") in OBSERVATIONAL_CATEGORIES for i in data_ids)


def is_truth(
    data_type: str,
    data_ids: list[str],
    categories: dict[str, str],
) -> bool:
    """Whether a group is a perfect-model **truth** run (§III.2).

    Truth members ride in ``other_data`` so the large-ensemble spread test
    can reach them, but they are the target, not a competitor: scoring them
    would put the truth into its own ``E_ref``.
    """
    if data_type != "other":
        return False
    return any(categories.get(i, "") == TRUTH_CATEGORY for i in data_ids)


def stack_members(
    members: list[pd.DataFrame],
    reference: pd.DataFrame,
    column: str,
) -> tuple[np.ndarray, np.ndarray, pd.Series]:
    """Stack member series on the times every member shares with the obs.

    Returns ``(members (M, T), obs (T,), times (T,))``.
    """
    merged = reference[["time", column]].rename(columns={column: "obs"}).dropna()
    for i, member in enumerate(members):
        merged = pd.merge(
            merged,
            member[["time", column]].rename(columns={column: f"m{i}"}),
            on="time",
            how="inner",
        )
    merged = merged.dropna().sort_values("time")
    matrix = merged[[f"m{i}" for i in range(len(members))]].to_numpy(float).T
    return matrix, merged["obs"].to_numpy(float), merged["time"]


def is_monthly(times: pd.Series) -> bool:
    """Whether a time axis steps by roughly a month rather than a year.

    The times are sorted first: a ``raw_output`` table holds several sources
    one after another, so an unsorted slice can otherwise show negative
    steps and read as "monthly".
    """
    stamps = pd.to_datetime(pd.Series(times)).sort_values()
    if stamps.size < 2:  # noqa: PLR2004 - a single step tells us nothing
        return False
    return bool(stamps.diff().dropna().dt.days.median() < _MONTHLY_MAX_DAYS)


# ---------------------------------------------------------------------------
# Regime (a): anomalies about each source's own baseline climatology
# ---------------------------------------------------------------------------


@dataclass
class AnomalyContext:
    """What :func:`anomalise_raw_output` made of one ``raw_output`` table.

    ``raw``
        The table with every scorable value replaced by its anomaly about
        that ``data_id``'s **own** baseline-window climatology, and cut to
        the steps the protocol actually scores (``tier2.test_window_start``
        onwards) — so ``n_time`` counts post-2015 steps and nothing else.
    ``baseline``
        The ``reference_baseline`` frames, shifted by the *same* offsets, so
        the Climatology baseline's pseudo-members live in the same anomaly
        space and that baseline means what the protocol says it means: "no
        change since 1985–2014".
    ``missing``
        ``(data_id, variable)`` pairs whose record does not reach far enough
        into the baseline window. Their values are blanked, never left
        absolute, and the caller writes a ``reason`` row for them.
    ``reference_missing``
        Variables whose *reference* could not be anomalised: nothing in that
        variable can be scored, because there is no target.
    """

    raw: pd.DataFrame
    baseline: dict[tuple[str, str], pd.DataFrame] | None
    missing: set[tuple[str, str]] = field(default_factory=set)
    reference_missing: set[str] = field(default_factory=set)


def baseline_climatology(
    times: pd.Series,
    values: np.ndarray,
    *,
    monthly: bool,
    min_years: int | None = None,
) -> dict[int, float] | None:
    """One source's climatology over ``tier2.climatology_baseline_period``.

    Returns ``{calendar month: mean}`` for a monthly series and ``{0: mean}``
    for an annual one, or ``None`` when the record covers fewer than
    ``tier2.anomaly_baseline.min_years`` distinct years of the window — in
    which case the protocol does **not** score the source at all rather than
    anomalise it about a climatology it does not have.
    """
    if min_years is None:
        min_years = windows.anomaly_min_baseline_years()
    first, last = windows.baseline_window_years()
    stamps = pd.to_datetime(pd.Series(times)).reset_index(drop=True)
    years = stamps.dt.year.to_numpy()
    in_window = (years >= first) & (years <= last) & np.isfinite(values)
    if int(np.unique(years[in_window]).size) < int(min_years):
        return None
    if not monthly:
        return {0: float(np.mean(values[in_window]))}
    months = stamps.dt.month.to_numpy()
    return {
        int(month): float(np.mean(values[in_window & (months == month)]))
        for month in np.unique(months[in_window])
    }


def apply_climatology(
    times: pd.Series,
    values: np.ndarray,
    climatology: dict[int, float],
    *,
    monthly: bool,
) -> np.ndarray:
    """``values`` minus their climatology (per calendar month, if monthly).

    A step whose calendar month is absent from the climatology becomes NaN:
    it has no baseline, so it is dropped rather than scored against a
    neighbouring month's mean.
    """
    if not monthly:
        return values - climatology[0]
    months = pd.to_datetime(pd.Series(times)).reset_index(drop=True).dt.month
    offsets = np.array(
        [climatology.get(int(month), np.nan) for month in months],
        dtype=float,
    )
    return values - offsets


def anomalise_raw_output(
    raw_df: pd.DataFrame,
    baseline: dict[tuple[str, str], pd.DataFrame] | None,
    var_columns: list[str],
) -> AnomalyContext:
    """Reduce a scored time-series table to anomalies, then cut to the window.

    The paper defines regime (a) over **anomaly** series (§scoring), and the
    anomaly is taken per ``data_id`` — that is, per ensemble member, about
    *that member's* own climatology, not about the model's or the
    reference's. Scoring the absolute series instead makes the fair CRPS a
    statement about each field's mean-state bias: on the first full Tier II
    run CNRM-CM6-1's 1.1 K cold GMST bias alone gave CRPS ≈ 1.0 K and skill
    −2.85 with a perfectly reasonable post-2015 trajectory.

    The submission, the reference and every comparison member are treated
    identically. The reference may take its climatology from the
    ``reference_baseline`` rows when its own rows do not reach back (which
    is what an older, test-window-only database has), and those rows are
    shifted by the same offsets so the Climatology baseline stays in the
    same space.

    Only then is the table cut to ``tier2.test_window_start`` onwards: the
    baseline years define the anomaly, they are never scored.
    """
    frame = raw_df.reset_index(drop=True).copy()
    min_years = windows.anomaly_min_baseline_years()
    first, last = windows.baseline_window_years()
    context = AnomalyContext(raw=frame, baseline=dict(baseline or {}))
    reference_ids = {
        str(i) for i in frame.loc[frame["data_type"] == "reference", "data_id"]
    }
    positions = {
        str(data_id): index
        for data_id, index in frame.groupby(frame["data_id"].astype(str)).groups.items()
    }

    for column in var_columns:
        if column not in frame.columns:
            continue
        # ClimateEval writes its raw_output columns as float32, and pandas
        # refuses (TypeError, not a silent downcast) to put a float64
        # anomaly back into one. Widen once, per column, before anything is
        # written — a real Tier II database is float32 throughout.
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
        reference_offsets: dict[int, float] | None = None
        reference_monthly = False
        reference_index: pd.Index | None = None
        for data_id, index in positions.items():
            values = frame.loc[index, column].to_numpy(float)
            if not np.isfinite(values).any():
                continue  # this source does not carry this variable
            times = frame.loc[index, "time"]
            monthly = is_monthly(times)
            offsets = baseline_climatology(
                times,
                values,
                monthly=monthly,
                min_years=min_years,
            )
            own_window = offsets is not None
            if offsets is None and data_id in reference_ids:
                offsets = _recorded_climatology(
                    baseline,
                    column,
                    monthly=monthly,
                    min_years=min_years,
                )
            if offsets is None:
                context.missing.add((data_id, column))
                if data_id in reference_ids:
                    context.reference_missing.add(column)
                # Never leave an absolute value next to anomalies: it would
                # corrupt sigma_obs and any group it shares a model with.
                frame.loc[index, column] = np.nan
                continue
            frame.loc[index, column] = apply_climatology(
                times,
                values,
                offsets,
                monthly=monthly,
            )
            if data_id in reference_ids:
                reference_offsets, reference_monthly = offsets, monthly
                reference_index = index if own_window else None

        # Order matters: the recorded `reference_baseline` rows are still
        # absolute and must be shifted, whereas the reference's own rows
        # have already been anomalised in place and must not be shifted
        # twice.
        if reference_offsets is not None:
            _shift_recorded_baseline(
                context,
                column,
                reference_offsets,
                monthly=reference_monthly,
            )
        if reference_index is not None:
            _record_own_baseline(
                context,
                frame.loc[reference_index],
                column,
                monthly=reference_monthly,
                years=(first, last),
            )

    start = int(get_threshold("tier2.test_window_start"))
    scored = pd.to_datetime(frame["time"]).dt.year >= start
    context.raw = frame[scored].reset_index(drop=True)
    return context


def _recorded_climatology(
    baseline: dict[tuple[str, str], pd.DataFrame] | None,
    column: str,
    *,
    monthly: bool,
    min_years: int,
) -> dict[int, float] | None:
    """The reference's climatology from its ``reference_baseline`` rows."""
    recorded = _baseline_window_series(baseline, column, monthly=monthly)
    if recorded is None:
        return None
    return baseline_climatology(
        recorded["time"],
        recorded[column].to_numpy(float),
        monthly=monthly,
        min_years=min_years,
    )


def _record_own_baseline(
    context: AnomalyContext,
    reference_rows: pd.DataFrame,
    column: str,
    *,
    monthly: bool,
    years: tuple[int, int],
) -> None:
    """Use the reference's own pre-test rows as the Climatology sample.

    ``ReferenceBaselineRecord`` only runs for the suite entries that name it
    (``reference_baseline`` / ``sst_baseline``), so OHC and the sea-ice
    extents never had a Climatology row. Now that the scored entries load
    from the baseline window anyway, the reference carries its own sample
    and that baseline exists for them too.
    """
    key = (column, "monthly" if monthly else "annual")
    if key in (context.baseline or {}):
        return
    stamps = pd.to_datetime(reference_rows["time"])
    window = reference_rows[(stamps.dt.year >= years[0]) & (stamps.dt.year <= years[1])]
    series = window[["time", column]].dropna().sort_values("time")
    if not series.empty and context.baseline is not None:
        context.baseline[key] = series


def _shift_recorded_baseline(
    context: AnomalyContext,
    column: str,
    climatology: dict[int, float],
    *,
    monthly: bool,
) -> None:
    """Put the Climatology baseline's sample in the same anomaly space.

    The pseudo-members are the baseline window's own values, so subtracting
    the same climatology turns that baseline into exactly what the protocol
    intends it to be: "no change since 1985–2014".
    """
    key = (column, "monthly" if monthly else "annual")
    recorded = (context.baseline or {}).get(key)
    if recorded is None or recorded.empty or context.baseline is None:
        return
    shifted = recorded.copy()
    shifted[column] = apply_climatology(
        recorded["time"],
        recorded[column].to_numpy(float),
        climatology,
        monthly=monthly,
    )
    context.baseline[key] = shifted


# ---------------------------------------------------------------------------
# Held-out vs in-sample labelling (paper §5.6)
# ---------------------------------------------------------------------------


def window_label(diagnostic: str, var_id: str) -> str:
    """``held-out`` / ``in-sample`` for one scored row.

    The protocol's scorecard states, per entry, whether the observations it
    is scored against were available before the reserved post-2015 test
    period. The mapping is protocol metadata, so it lives in
    ``thresholds.yml`` (``tier2.window_labels``) rather than in a diagnostic:
    a ``var_ids`` entry wins over a ``diagnostics`` entry (keyed by the suite
    entry name, which is the DuckDB schema the rows land in), and anything
    unlisted takes ``default``.

    A consistency row (``<var>_trend_consistency`` / ``<var>_consistency``)
    inherits the label of the variable it tests.
    """
    table = get_threshold("tier2.window_labels")
    by_var = table.get("var_ids") or {}
    candidates = [str(var_id)]
    for suffix in (TREND_CONSISTENCY_SUFFIX, SCALAR_CONSISTENCY_SUFFIX):
        if str(var_id).endswith(suffix):
            candidates.append(str(var_id)[: -len(suffix)])
    for candidate in candidates:
        if candidate in by_var:
            return str(by_var[candidate])
    by_diagnostic = table.get("diagnostics") or {}
    if diagnostic in by_diagnostic:
        return str(by_diagnostic[diagnostic])
    return str(table["default"])


#: ``tier2.variable_status`` tags (paper Table 2).
STATUS_CORE = "core"
STATUS_EXTENDED = "extended"
VARIABLE_STATUS_TAGS = (STATUS_CORE, STATUS_EXTENDED)


def variable_status(diagnostic: str, var_id: str) -> str:
    """``core`` / ``extended`` for one Tier II entry (paper Table 2).

    Resolved from ``tier2.variable_status`` exactly as :func:`window_label`
    resolves ``tier2.window_labels``: a ``var_ids`` entry wins over a
    ``diagnostics`` entry (the suite entry name), anything unlisted takes
    ``default``, and a consistency row inherits the tag of the variable it
    tests. Presentation metadata only — no score depends on it, which is why
    it is outside the frozen-comparison fingerprint.

    Raises
    ------
    ValueError
        If the tag is not one of :data:`VARIABLE_STATUS_TAGS`.
    """
    table = get_threshold("tier2.variable_status")
    by_var = table.get("var_ids") or {}
    candidates = [str(var_id)]
    for suffix in (TREND_CONSISTENCY_SUFFIX, SCALAR_CONSISTENCY_SUFFIX):
        if str(var_id).endswith(suffix):
            candidates.append(str(var_id)[: -len(suffix)])
    tag = None
    for candidate in candidates:
        if candidate in by_var:
            tag = by_var[candidate]
            break
    if tag is None:
        tag = (table.get("diagnostics") or {}).get(diagnostic, table["default"])
    if tag not in VARIABLE_STATUS_TAGS:
        msg = (
            f"Unknown Tier II status '{tag}' for ({diagnostic}, {var_id}) in "
            f"tier2.variable_status (expected one of {VARIABLE_STATUS_TAGS})"
        )
        raise ValueError(msg)
    return str(tag)


def is_perfect_model(
    raw_df: pd.DataFrame,
    categories: dict[str, str],
) -> bool:
    """Whether this diagnostic's reference is a held-out ESM run (§III.2).

    Self-describing, so it survives ``leaderboard --rescore``: the truth
    DataSource records itself in ``data_sources`` with
    ``category = "truth"``, and any row scored against it is labelled
    ``perfect-model`` rather than held-out/in-sample.
    """
    if "data_type" not in raw_df.columns:
        return False
    reference_ids = raw_df.loc[raw_df["data_type"] == "reference", "data_id"]
    return any(categories.get(str(i), "") == TRUTH_CATEGORY for i in reference_ids)


def _apply_window_labels(
    rows: list[dict[str, Any]],
    diagnostic: str,
    *,
    perfect_model: bool = False,
) -> None:
    """Stamp the window label and the Core/Extended status, in place.

    ``window``: held-out / in-sample / perfect-model (``tier2.window_labels``).
    ``variable_status``: core / extended (``tier2.variable_status``), on the
    Tier II rows only — a gate-shaped row of another tier (the III.2
    large-ensemble spread test) is left blank.
    """
    for row in rows:
        var_id = str(row.get("var_id", ""))
        row["window"] = (
            WINDOW_PERFECT_MODEL
            if perfect_model
            else window_label(diagnostic, var_id)
        )
        tier = row.get("tier")
        if isinstance(tier, str) and tier and tier != "II":
            row["variable_status"] = ""
        else:
            row["variable_status"] = variable_status(diagnostic, var_id)


# ---------------------------------------------------------------------------
# Observational uncertainty
# ---------------------------------------------------------------------------


def obs_sigma_floor(var_id: str) -> float:
    """Protocol σ_obs for a variable (``tier2.obs_sigma``), 0 when unknown.

    Keys are matched against the full suite variable id first (``tas``,
    ``phcint_2000m``), then the token before its first underscore (so
    ``tos_nino34`` inherits ``tos``), then ``default``. A ``null`` entry means
    "no published value" and scores at 0 — the draws are then a no-op, which
    is the honest treatment of an unknown error, not a claim of zero error.
    """
    table = get_threshold("tier2.obs_sigma")
    for key in (var_id, var_id.split("_", 1)[0], "default"):
        if key in table:
            value = table[key]
            return 0.0 if value is None else float(value)
    return 0.0


def observational_sigma(
    reference: pd.DataFrame,
    product_frames: list[pd.DataFrame],
    column: str,
    floor: float,
) -> pd.Series | None:
    """Per-time-step σ_obs: the protocol floor plus the inter-product spread.

    The paper takes observational uncertainty partly "from the spread across
    products", so where a variable carries several observational datasets
    (the reference plus any ``other_data`` whose category is
    observational) their per-time-step standard deviation is added in
    quadrature to the fixed floor. With a single product the floor is
    returned as a flat series.

    Returns a series indexed by the reference's times, or ``None`` when there
    is nothing to say (no floor and no second product).
    """
    ref = reference[["time", column]].dropna().sort_values("time")
    if ref.empty:
        return None
    index = pd.Index(pd.to_datetime(ref["time"]), name="time")
    columns: list[np.ndarray] = [ref[column].to_numpy(float)]
    for frame in product_frames:
        aligned = (
            frame[["time", column]]
            .dropna()
            .assign(time=lambda f: pd.to_datetime(f["time"]))
            .set_index("time")[column]
            .reindex(index)
        )
        # A product that barely overlaps the reference tells us nothing.
        if aligned.notna().sum() >= _MIN_OVERLAP:
            columns.append(aligned.to_numpy(float))
    if len(columns) < _MIN_MEMBERS:
        return None if floor <= 0.0 else pd.Series(floor, index=index)

    # Per-time-step std across the products that have a value there. Written
    # out rather than np.nanstd(ddof=1) so a step with a single product is 0
    # instead of a NaN-and-a-warning.
    stacked = np.vstack(columns)
    finite = np.isfinite(stacked)
    counts = finite.sum(axis=0)
    filled = np.where(finite, stacked, 0.0)
    mean = np.divide(filled.sum(axis=0), np.maximum(counts, 1))
    squares = np.where(finite, (stacked - mean) ** 2, 0.0).sum(axis=0)
    spread = np.where(
        counts >= _MIN_MEMBERS,
        np.sqrt(squares / np.maximum(counts - 1, 1)),
        0.0,
    )
    return pd.Series(np.sqrt(floor**2 + spread**2), index=index)


def _sigma_at(
    sigma: pd.Series | None,
    times: pd.Series,
    floor: float,
) -> float | np.ndarray:
    """σ_obs aligned to a model's own time axis (``floor`` where unknown)."""
    if sigma is None:
        return floor
    values = sigma.reindex(pd.Index(pd.to_datetime(times))).to_numpy(float)
    return np.where(np.isfinite(values), values, floor)


# ---------------------------------------------------------------------------
# Internal variability (σ_int) collected from the Tier I database
# ---------------------------------------------------------------------------

#: ``(var_id, statistic) -> [(window length in years, sigma)]``.
SigmaInternal = dict[tuple[str, str], list[tuple[int, float]]]


def internal_variability_from_raw(raw_df: pd.DataFrame) -> SigmaInternal:
    """Read ``InternalVariability``'s scalar columns out of one raw_output."""
    table: SigmaInternal = {}
    windows = {
        name.removeprefix("window_years_"): _first_finite(raw_df[name])
        for name in raw_df.columns
        if name.startswith("window_years_")
    }
    for name in raw_df.columns:
        match = _SIGMA_INT_RE.match(str(name))
        if match is None:
            continue
        length = windows.get(match["window"])
        sigma = _first_finite(raw_df[name])
        if length is None or not np.isfinite(length) or not np.isfinite(sigma):
            continue
        table.setdefault((match["var"], match["statistic"]), []).append(
            (int(length), float(sigma)),
        )
    return table


def _first_finite(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.iloc[0]) if not values.empty else float("nan")


def sigma_internal_for(
    table: SigmaInternal | None,
    var_id: str,
    statistic: str,
    n_years: int,
) -> float:
    """σ_int for a statistic over an ``n_years`` window (nearest available).

    The diagnostic reports σ_int at the two window lengths the protocol
    scores (the test window and 1950-present); the record actually being
    scored may be a year or two off either, so the closest length is used.
    Returns 0.0 when no σ_int is available — the consistency test then
    degrades to the spread-plus-σ_obs form it had before this was wired.
    """
    if not table:
        return 0.0
    candidates = table.get((var_id, statistic)) or table.get(
        (var_id.split("_", 1)[0], statistic),
    )
    if not candidates:
        return 0.0
    length, sigma = min(candidates, key=lambda item: abs(item[0] - int(n_years)))
    del length
    return float(sigma)


# ---------------------------------------------------------------------------
# Scoring one (diagnostic, variable)
# ---------------------------------------------------------------------------


def _bootstrap_settings() -> dict[str, Any]:
    return {
        "block_length_monthly": int(get_threshold("tier2.bootstrap.block_length_monthly")),
        "block_length_annual": int(get_threshold("tier2.bootstrap.block_length_annual")),
        "n_boot": int(get_threshold("tier2.bootstrap.n_boot")),
        "alpha": float(get_threshold("tier2.bootstrap.alpha")),
        "resample_members": bool(get_threshold("tier2.bootstrap.resample_members")),
        "seed": int(get_threshold("tier2.bootstrap.seed")),
    }


def empty_score_row(
    data_id: str,
    data_type: str,
    var_id: str,
    reason: str = "",
    *,
    scorer: str = SCORER,
) -> dict[str, Any]:
    """A score row with every numeric column NaN and every text column empty.

    The shared constructor for :data:`_SCORE_COLUMNS`-shaped rows: the pass
    builds every row it writes from it, and so do the Tier III paleo
    diagnostics, which pass ``scorer=TIER3_SCORER`` (their rows are written
    by the diagnostic, not by the pass, and must survive a re-run of it).

    With a ``reason`` and nothing else it is the protocol's "why there is no
    score here" row, which keeps a model visible on the scorecard instead of
    letting it silently vanish.
    """
    row: dict[str, Any] = {
        key: ("" if key in _TEXT_COLUMNS else np.nan) for key in _SCORE_COLUMNS
    }
    row.update(
        data_id=data_id,
        data_type=data_type,
        var_id=var_id,
        scorer=scorer,
        reason=reason,
    )
    return row


def _empty_row(data_id: str, data_type: str, var_id: str, reason: str) -> dict[str, Any]:
    """A row that records *why* there is no score, keeping the model visible."""
    return empty_score_row(data_id, data_type, var_id, reason)


def _score_row(
    *,
    data_id: str,
    data_type: str,
    var_id: str,
    members: np.ndarray,
    obs: np.ndarray,
    monthly: bool,
    settings: dict[str, Any],
    obs_sigma: float | np.ndarray = 0.0,
) -> dict[str, Any]:
    """Fair CRPS + ESS SE + block-bootstrap CI for one stacked ensemble."""
    n_draws = int(get_threshold("tier2.obs_uncertainty.n_draws"))
    crps_t = scoring.crps_fair_with_obs_draws(
        members,
        obs,
        obs_sigma=obs_sigma,
        n_draws=n_draws,
        seed=int(get_threshold("tier2.obs_uncertainty.seed")),
    )
    summary = scoring.crps_ess_from_series(crps_t, members.shape[0])
    block_length = (
        settings["block_length_monthly"] if monthly else settings["block_length_annual"]
    )
    lo, hi = scoring.moving_block_bootstrap_ci(
        crps_t,
        block_length=block_length,
        n_boot=settings["n_boot"],
        alpha=settings["alpha"],
        seed=settings["seed"],
        members=members if settings["resample_members"] else None,
        obs=obs if settings["resample_members"] else None,
    )
    row = _empty_row(data_id, data_type, var_id, "")
    row.update(
        crps=summary.score,
        crps_se=summary.standard_error,
        crps_ci_lo=lo,
        crps_ci_hi=hi,
        t_eff=summary.t_eff,
        r1=summary.r1,
        n_members=float(summary.n_members),
        n_time=float(summary.n_time),
        block_length=float(block_length),
        sigma_obs=float(np.mean(np.broadcast_to(obs_sigma, obs.shape))),
    )
    return row


def _baseline_window_series(
    baseline: dict[tuple[str, str], pd.DataFrame] | None,
    column: str,
    *,
    monthly: bool,
) -> pd.DataFrame | None:
    """The reference's pre-test baseline series for one variable, if recorded."""
    if not baseline:
        return None
    frame = baseline.get((column, "monthly" if monthly else "annual"))
    if frame is None or frame.empty:
        return None
    return frame


def _climatology_row(
    reference: pd.DataFrame,
    column: str,
    settings: dict[str, Any],
    *,
    baseline: dict[tuple[str, str], pd.DataFrame] | None = None,
    sigma: pd.Series | None = None,
    floor: float = 0.0,
) -> dict[str, Any]:
    """Baseline (i) as a distribution (see :mod:`climatebench2.baselines`)."""
    ref = reference[["time", column]].dropna().sort_values("time")
    if ref.empty:
        return _empty_row(CLIMATOLOGY_DATA_ID, "baseline", column, "no reference")
    times = pd.to_datetime(ref["time"])
    year_0, year_1 = get_threshold("tier2.climatology_baseline_period")
    years = times.dt.year.to_numpy()
    monthly = is_monthly(ref["time"])

    # The baseline window is normally outside the reserved test window the
    # suite is cut to, so it arrives through `ReferenceBaselineRecord`'s rows
    # rather than through the reference series being scored. Fall back to the
    # reference's own rows when a database happens to span both windows (a
    # hand-built or full-record run).
    recorded = _baseline_window_series(baseline, column, monthly=monthly)
    if recorded is not None:
        window_times = pd.to_datetime(recorded["time"])
        window_values = recorded[column].to_numpy(float)
        window_months = window_times.dt.month.to_numpy()
    else:
        in_window = (years >= int(year_0)) & (years <= int(year_1))
        if in_window.sum() < _MIN_OVERLAP:
            return _empty_row(
                CLIMATOLOGY_DATA_ID,
                "baseline",
                column,
                f"reference has no {int(year_0)}-{int(year_1)} baseline window",
            )
        window_values = ref[column].to_numpy(float)[in_window]
        window_months = times.dt.month.to_numpy()[in_window]

    # Score the baseline on the reserved test window — the same target steps
    # the models are scored on, never on the window it was fitted to.
    is_target = years >= int(get_threshold("tier2.test_window_start"))
    if is_target.sum() < _MIN_OVERLAP:
        is_target = (years < int(year_0)) | (years > int(year_1))
    if is_target.sum() < _MIN_OVERLAP:
        return _empty_row(
            CLIMATOLOGY_DATA_ID,
            "baseline",
            column,
            "reference has no test-window steps to score the baseline on",
        )
    obs = ref[column].to_numpy(float)[is_target]
    target_times = times[is_target]
    try:
        if monthly:
            members = baselines.climatology_pseudo_members(
                window_values,
                window_months=window_months,
                target_months=target_times.dt.month.to_numpy(),
            )
        else:
            members = baselines.climatology_pseudo_members(
                window_values,
                n_time=int(obs.size),
            )
    except ValueError as exc:
        return _empty_row(CLIMATOLOGY_DATA_ID, "baseline", column, str(exc))
    if members.shape[0] < _MIN_MEMBERS:
        return _empty_row(
            CLIMATOLOGY_DATA_ID,
            "baseline",
            column,
            "baseline window too short for a distribution",
        )
    return _score_row(
        data_id=CLIMATOLOGY_DATA_ID,
        data_type="baseline",
        var_id=column,
        members=members,
        obs=obs,
        monthly=monthly,
        settings=settings,
        obs_sigma=_sigma_at(sigma, target_times, floor),
    )


def _annual_observed_record(
    reference: pd.DataFrame,
    column: str,
    baseline: dict[tuple[str, str], pd.DataFrame] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """``(years, values)`` of the reference, reaching back past the test cut.

    The Tier II suites are cut to the post-2015 test window, so the
    reference's own rows start in 2015; the pre-test record comes from
    :class:`climatebench2.diags.ReferenceBaselineRecord`. Both are needed
    here: the baseline window defines the anomaly and the pre-2015 years are
    the only ones the pattern-scaling emulator may be calibrated on.
    """
    frames = [reference[["time", column]].dropna()]
    recorded = _baseline_window_series(baseline, column, monthly=False)
    if recorded is not None:
        frames.append(recorded[["time", column]].dropna())
    joined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset="time")
        .sort_values("time")
    )
    years = pd.to_datetime(joined["time"]).dt.year.to_numpy()
    return years, joined[column].to_numpy(float)


def _pattern_scaling_row(
    reference: pd.DataFrame,
    column: str,
    settings: dict[str, Any],
    *,
    baseline: dict[tuple[str, str], pd.DataFrame] | None = None,
    sigma: pd.Series | None = None,
    floor: float = 0.0,
) -> dict[str, Any] | None:
    """Baseline (iii): the calibrated two-layer EBM, as an ensemble.

    The protocol's "simplest defensible emulator": a global-mean temperature
    trajectory from a two-layer EBM driven by the packaged annual ERF series
    and calibrated — **one** parameter, on observations **through 2014** —
    then reported alongside the headline skill.

    Returns ``None`` for a variable the emulator says nothing about (only
    ``tier2.pattern_scaling.variables`` qualify) or for a monthly series; a
    ``reason`` row whenever it qualifies but the data do not allow the fit,
    so the absence is visible on the scorecard rather than silent.

    ⚠ Two CB2 interpretations, both recorded in docs/metrics_reference.md:
    the deterministic trajectory is given **pseudo-members** (the detrended
    observed residuals of the baseline window displace it, exactly as the
    climatology baseline uses that window's values), and the ERF table it is
    driven by is currently a provisional interpolation of AR6 anchors.
    """
    qualifies = get_threshold("tier2.pattern_scaling.variables")
    if column not in qualifies and column.split("_", 1)[0] not in qualifies:
        return None
    ref = reference[["time", column]].dropna().sort_values("time")
    if ref.empty or is_monthly(ref["time"]):
        # The EBM steps annually; a monthly target would need an
        # interpolation the protocol does not specify.
        return None

    def _reason(text: str) -> dict[str, Any]:
        return _empty_row(PATTERN_SCALING_DATA_ID, "baseline", column, text)

    first, last = (int(y) for y in get_threshold("tier2.climatology_baseline_period"))
    end = int(get_threshold("tier2.pattern_scaling.calibration_end_year"))
    minimum = int(get_threshold("tier2.pattern_scaling.min_calibration_years"))
    years, values = _annual_observed_record(reference, column, baseline)
    if (years <= end).sum() < minimum:
        return _reason(
            f"pattern scaling needs >= {minimum} observed years through {end} "
            f"to calibrate; the database has {(years <= end).sum()}",
        )
    in_baseline = (years >= first) & (years <= last)
    if in_baseline.sum() < _MIN_MEMBERS:
        return _reason(f"reference has no {first}-{last} window to anchor the EBM")

    ebm_kwargs = {
        key: float(value)
        for key, value in get_threshold("tier2.ebm").items()
        if key != get_threshold("tier2.pattern_scaling.calibrated_parameter")
    }
    try:
        erf_years, erf = baselines.load_erf_series(
            str(get_threshold("tier2.pattern_scaling.erf_file")),
        )
        calibration = baselines.calibrate_two_layer_ebm(
            erf,
            erf_years,
            values,
            years,
            baseline=(first, last),
            calibration_end=end,
            parameter=str(get_threshold("tier2.pattern_scaling.calibrated_parameter")),
            bracket=tuple(get_threshold("tier2.pattern_scaling.lambda_bounds")),
            **ebm_kwargs,
        )
        residuals = baselines.detrended_residuals(values[in_baseline])
    except (ValueError, OSError) as exc:
        return _reason(f"pattern scaling could not be calibrated: {exc}")

    # Score it on the reserved test window — the steps the models are scored
    # on — never on the years it was calibrated to.
    target_years = pd.to_datetime(ref["time"]).dt.year.to_numpy()
    covered = np.isin(target_years, calibration.years) & (
        target_years >= int(get_threshold("tier2.test_window_start"))
    )
    if covered.sum() < _MIN_OVERLAP:
        return _reason(
            "no scored year is covered by both the test window and the ERF "
            "series",
        )
    positions = np.searchsorted(calibration.years, target_years[covered])
    forecast = calibration.trajectory[positions]
    anomaly = ref[column].to_numpy(float)[covered] - float(
        np.nanmean(values[in_baseline]),
    )
    try:
        members = baselines.ebm_pseudo_members(forecast, residuals)
    except ValueError as exc:
        return _reason(str(exc))
    row = _score_row(
        data_id=PATTERN_SCALING_DATA_ID,
        data_type="baseline",
        var_id=column,
        members=members,
        obs=anomaly,
        monthly=False,
        settings=settings,
        obs_sigma=_sigma_at(sigma, ref["time"][covered], floor),
    )
    row["value"] = calibration.value  # the one calibrated parameter
    return row


def _apply_reference_skill(scored: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Fill ``e_ref`` / ``n_ref_models`` / ``skill`` in place.

    ``E_ref`` is the median of the per-model fair CRPS over the comparison
    models with ``M >= 2``, leaving out any model of the scored model's own
    name (paper §5.6). Comparison models are scored the same way, so each of
    them is also left out of its own reference. Observational products never
    enter: they carry no ``crps``.
    """
    comparison = {
        name: row["crps"]
        for (data_type, name), row in scored.items()
        if data_type == "other" and np.isfinite(row["crps"])
    }
    for row in scored.values():
        others = [e for n, e in comparison.items() if n != row["data_id"]]
        row["n_ref_models"] = float(len(others))
        if others:
            e_ref = float(np.median(others))
            row["e_ref"] = e_ref
            if np.isfinite(row["crps"]) and e_ref > 0:
                row["skill"] = 1.0 - row["crps"] / e_ref


def _trend_consistency_row(
    *,
    data_id: str,
    data_type: str,
    column: str,
    members: np.ndarray,
    obs: np.ndarray,
    obs_sigma: float | np.ndarray,
    sigma_internal: SigmaInternal | None,
) -> dict[str, Any] | None:
    """Regime (c): the observed trend against one model's ensemble of trends.

    σ_total² = var(member trends) + σ_int² + σ_obs(trend)². The σ_obs of a
    *trend* is derived from the per-step observational error by the OLS slope
    variance of independent errors,
    ``σ_trend = σ_step · sqrt(12 / (T(T²−1)))`` — an interpretation the
    protocol does not spell out (metrics_reference.md §II.0).
    """
    n_time = obs.size
    if n_time < _MIN_OVERLAP:
        return None
    obs_trend = scoring.ols_trend(obs)
    member_trends = np.array([scoring.ols_trend(row) for row in members], dtype=float)
    sigma_int = sigma_internal_for(sigma_internal, column, "trend", n_time)
    sigma_step = float(np.sqrt(np.mean(np.broadcast_to(obs_sigma, obs.shape) ** 2)))
    sigma_obs_trend = scoring.ols_trend_sigma(sigma_step, n_time)
    if members.shape[0] < _MIN_MEMBERS and sigma_int <= 0.0 and sigma_obs_trend <= 0.0:
        return None  # no spread at all: the test would divide by zero
    result = scoring.ensemble_consistency(
        member_trends,
        obs_trend,
        sigma_internal=sigma_int,
        sigma_obs=sigma_obs_trend,
        p_threshold=float(get_threshold("tier2.consistency_p_value")),
    )
    row = _empty_row(data_id, data_type, f"{column}{TREND_CONSISTENCY_SUFFIX}", "")
    row.update(
        value=obs_trend,
        z=result.z,
        p_value=result.p_value,
        passes=float(result.passes),
        ensemble_mean=result.ensemble_mean,
        total_sigma=result.total_sigma,
        sigma_internal=sigma_int,
        sigma_obs=sigma_obs_trend,
        n_members=float(members.shape[0]),
        n_time=float(n_time),
    )
    return row


def reference_for_variable(
    references: pd.DataFrame,
    column: str,
) -> pd.DataFrame | None:
    """The reference source that actually carries ``column``.

    One diagnostic's ``raw_output`` holds **several** references: the Tier II
    ``annual_mean_timeseries`` entry scores ``tas`` against HadCRUT5, ``pr``
    against GPCP and the TOA fluxes against CERES-EBAF, and ``sea_ice_minimum``
    scores the NH series against OSI-450-NH and the SH one against OSI-450-SH.
    Each reference's rows carry only its own variable (the others are NULL
    after the union), so the reference must be chosen **per variable** —
    taking the first reference source in the table, as earlier revisions did,
    silently dropped every variable but one.
    """
    for _data_id, frame in references.groupby("data_id", sort=True):
        if frame[["time", column]].dropna().shape[0] >= _MIN_OVERLAP:
            return frame
    return None


def deduplicate_raw_output(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows a per-member run re-ingested verbatim.

    The Tier II suites are ``per_member`` (``_cli.SuiteSpec``): the CLI runs
    them once per ensemble member and **appends** into one database. A
    diagnostic writes its ``reference`` rows and its ``other`` rows (the
    observational products and the whole CMIP6 comparison ensemble) on *every*
    one of those runs, because it re-reads them each time — so with M members
    each of those rows appears M times, while the ``to_benchmark`` rows are
    genuinely one per member.

    That is not a cosmetic duplication:

    * :func:`observational_sigma` indexes sigma by the reference's times, and a
      duplicated index makes the later ``reindex`` raise
      ``cannot reindex on an axis with duplicate labels`` — which is how this
      surfaced, as a hard failure of the whole scoring pass on the first
      3-member real-data run;
    * :func:`stack_members` inner-joins each member on ``time``, so M copies of
      one comparison member would multiply into M**2 rows and silently corrupt
      its fair CRPS.

    A fully identical row can only be a re-ingestion: two data sources differ
    in ``data_id`` and two time steps differ in ``time``. So exact-duplicate
    rows are dropped and nothing else is touched.
    """
    if raw_df.empty:
        return raw_df
    return raw_df.drop_duplicates(ignore_index=True)


def score_raw_output(  # noqa: C901, PLR0912, PLR0915
    raw_df: pd.DataFrame,
    data_sources: pd.DataFrame | None,
    *,
    settings: dict[str, Any] | None = None,
    baseline: dict[tuple[str, str], pd.DataFrame] | None = None,
    sigma_internal: SigmaInternal | None = None,
    diagnostic: str = "",
) -> list[dict[str, Any]]:
    """Score one diagnostic's ``raw_output`` table; returns metrics rows.

    Where the protocol says so (``tier2.anomaly_baseline``, resolved by suite
    entry name — which is this ``diagnostic``), the table is first reduced to
    **anomalies about each source's own baseline-window climatology** and cut
    to the scored window (:func:`anomalise_raw_output`), so everything below
    works on the quantity the paper defines regime (a) over.

    Pure pandas/numpy — the database plumbing is in :func:`score_database`.
    """
    if "time" not in raw_df.columns or "data_id" not in raw_df.columns:
        return []
    settings = settings or _bootstrap_settings()
    names = source_names(data_sources)
    categories = source_categories(data_sources)
    var_columns = [c for c in raw_df.columns if c not in _META_COLUMNS]
    if raw_df.empty or not var_columns:
        return []

    anomalies = windows.scores_anomalies(diagnostic)
    no_baseline: set[tuple[str, str]] = set()
    unscorable: list[str] = []
    if anomalies:
        context = anomalise_raw_output(raw_df, baseline, var_columns)
        raw_df, baseline = context.raw, context.baseline
        no_baseline = context.missing
        unscorable = [c for c in var_columns if c in context.reference_missing]
        var_columns = [c for c in var_columns if c not in context.reference_missing]

    references = raw_df[raw_df["data_type"] == "reference"]
    if references.empty and not unscorable:
        return []
    groups = group_members(raw_df, names)
    ids = group_ids(raw_df, names)
    observational = {
        key: is_observational(key[0], ids.get(key, []), categories) for key in groups
    }
    truth = {key: is_truth(key[0], ids.get(key, []), categories) for key in groups}
    perfect_model = is_perfect_model(raw_df, categories)

    rows: list[dict[str, Any]] = []
    # A variable whose *reference* has no baseline window has no target at
    # all, so nothing in it can be scored — but it stays on the scorecard,
    # with the reason, instead of vanishing.
    for column in unscorable:
        rows.extend(
            _empty_row(name, data_type, column, f"reference has {NO_BASELINE_REASON}")
            for data_type, name in groups
        )
        rows.append(
            _empty_row(
                CLIMATOLOGY_DATA_ID,
                "baseline",
                column,
                f"reference has {NO_BASELINE_REASON}",
            ),
        )
    for column in var_columns:
        reference = reference_for_variable(references, column)
        if reference is None:
            continue
        # A perfect-model truth run is known exactly: there is no
        # observational error to draw over, and carrying the instrumental
        # floor here would flatter every submission equally.
        floor = 0.0 if perfect_model else obs_sigma_floor(column)
        sigma = observational_sigma(
            reference,
            [f for key, frames in groups.items() if observational[key] for f in frames],
            column,
            floor,
        )

        scored: dict[tuple[str, str], dict[str, Any]] = {}
        consistency: list[dict[str, Any]] = []
        for key, member_frames in groups.items():
            data_type, name = key
            if observational[key]:
                scored[key] = _empty_row(
                    name,
                    data_type,
                    column,
                    "observational product (a term in sigma_obs, not scored)",
                )
                continue
            if truth[key]:
                scored[key] = _empty_row(
                    name,
                    data_type,
                    column,
                    "perfect-model truth member (the spread-test target, "
                    "not a comparison model)",
                )
                continue
            if no_baseline:
                # Members whose record does not reach into the baseline
                # window are dropped from the ensemble, never scored on
                # absolute values next to their anomalised siblings; a group
                # that loses all of them keeps a `reason` row.
                usable = [
                    f
                    for f in member_frames
                    if (str(f["data_id"].iloc[0]), column) not in no_baseline
                ]
                if not usable:
                    scored[key] = _empty_row(name, data_type, column, NO_BASELINE_REASON)
                    continue
                member_frames = usable  # noqa: PLW2901
            members, obs, times = stack_members(member_frames, reference, column)
            if obs.size < _MIN_OVERLAP:
                continue
            monthly = is_monthly(times)
            obs_sigma = _sigma_at(sigma, times, floor)
            if members.shape[0] < _MIN_MEMBERS:
                scored[key] = _empty_row(name, data_type, column, "single member")
                scored[key]["n_members"] = float(members.shape[0])
                scored[key]["n_time"] = float(obs.size)
            else:
                scored[key] = _score_row(
                    data_id=name,
                    data_type=data_type,
                    var_id=column,
                    members=members,
                    obs=obs,
                    monthly=monthly,
                    settings=settings,
                    obs_sigma=obs_sigma,
                )
            # Regime (c) is defined on the *annual* warming rate; a monthly
            # series would need a σ_int chunked the same way, which the
            # piControl diagnostic reports annually.
            if not monthly:
                row = _trend_consistency_row(
                    data_id=name,
                    data_type=data_type,
                    column=column,
                    members=members,
                    obs=obs,
                    obs_sigma=obs_sigma,
                    sigma_internal=sigma_internal,
                )
                if row is not None:
                    consistency.append(row)

        clim = _climatology_row(
            reference,
            column,
            settings,
            baseline=baseline,
            sigma=sigma,
            floor=floor,
        )
        scored[clim["data_type"], clim["data_id"]] = clim

        pattern = _pattern_scaling_row(
            reference,
            column,
            settings,
            baseline=baseline,
            sigma=sigma,
            floor=floor,
        )
        if pattern is not None:
            scored[pattern["data_type"], pattern["data_id"]] = pattern

        _apply_reference_skill(scored)
        rows.extend(scored.values())
        rows.extend(consistency)
    rows.extend(le_spread_rows(groups, truth, var_columns))
    _apply_window_labels(rows, diagnostic, perfect_model=perfect_model)
    return rows


# ---------------------------------------------------------------------------
# III.2: the large-ensemble spread test
# ---------------------------------------------------------------------------


def _stacked(frames: list[pd.DataFrame], column: str) -> pd.DataFrame | None:
    """Inner-join member series on their shared times: one column per member."""
    merged: pd.DataFrame | None = None
    for i, frame in enumerate(frames):
        part = frame[["time", column]].rename(columns={column: f"m{i}"}).dropna()
        merged = (
            part if merged is None else pd.merge(merged, part, on="time", how="inner")
        )
    if merged is None or merged.empty:
        return None
    return merged.sort_values("time")


def _matrix(stacked: pd.DataFrame, times: pd.Series) -> np.ndarray:
    """``(n_members, n_time)`` of a stacked frame, cut to ``times``."""
    frame = stacked[stacked["time"].isin(set(times))].sort_values("time")
    columns = [c for c in frame.columns if c != "time"]
    return frame[columns].to_numpy(float).T


def le_spread_rows(
    groups: dict[tuple[str, str], list[pd.DataFrame]],
    truth: dict[tuple[str, str], bool],
    var_columns: list[str],
) -> list[dict[str, Any]]:
    """The perfect-model large-ensemble spread test (metrics_reference §III.2).

    When a database carries **truth members** — the truth model's own
    ensemble, ingested through ``other_data`` by ``score --truth-member`` —
    the submission's inter-member variability is compared with the truth
    ensemble's, for each variable of ``tier3.le_spread.variables``:

    (i) the **variance ratio** (predicted / true inter-member variance),
        gated at ``tier3.le_spread.variance_ratio_range`` — an Extended Tier
        III check, reported for credit and never part of the entry ticket;
    (ii) the **pattern correlation** of the two inter-member variance
         fields, reported without a bound (the paper sets none).

    Both come from :mod:`climatebench2.scoring`; this is only the plumbing
    that finds the two ensembles and writes the rows. On a *time-series*
    raw_output the remaining axis is time, so "pattern" is the shape of the
    spread through the record; the same two functions apply unchanged to a
    Map raw_output whose axis is space — only the stacking would differ, and
    the pass scores no Map today.

    ⚠ Never exercised on real data: CESM2 / MPI-ESM / GISS-E2 and CESM-LE
    are not staged (the paper promises them on publication), so the tests
    are synthetic databases and the bound is still a TODO.
    """
    truth_frames = [f for key, frames in groups.items() if truth[key] for f in frames]
    predicted = [
        frames
        for key, frames in groups.items()
        if key[0] == "to_benchmark" and not truth[key]
    ]
    if not truth_frames or not predicted:
        return []
    lower, upper = get_threshold("tier3.le_spread.variance_ratio_range")
    requirement = str(get_threshold("tier3.le_spread.requirement"))
    tier = str(get_threshold("tier3.le_spread.tier"))
    wanted = set(get_threshold("tier3.le_spread.variables"))

    def reason_row(column: str, reason: str) -> dict[str, Any]:
        return empty_score_row(
            LE_SPREAD_DATA_ID,
            "to_benchmark",
            f"{column}_le_variance_ratio",
            reason,
        )

    rows: list[dict[str, Any]] = []
    for column in var_columns:
        if column not in wanted:
            continue
        true = _stacked(truth_frames, column)
        if true is None:
            continue
        for member_frames in predicted:
            pred = _stacked(member_frames, column)
            if pred is None:
                continue
            common = pred.merge(true[["time"]], on="time", how="inner")["time"]
            if common.size < _MIN_OVERLAP:
                continue
            pred_matrix = _matrix(pred, common)
            true_matrix = _matrix(true, common)
            if min(pred_matrix.shape[0], true_matrix.shape[0]) < _MIN_MEMBERS:
                rows.append(
                    reason_row(
                        column,
                        f"needs >= {_MIN_MEMBERS} members on each side, got "
                        f"{pred_matrix.shape[0]} predicted and "
                        f"{true_matrix.shape[0]} truth",
                    ),
                )
                continue
            try:
                ratio = scoring.le_variance_ratio(pred_matrix, true_matrix)
                correlation = scoring.le_spread_pattern_correlation(
                    pred_matrix,
                    true_matrix,
                )
            except ValueError as exc:
                rows.append(reason_row(column, str(exc)))
                continue
            ratio_row = empty_score_row(
                LE_SPREAD_DATA_ID,
                "to_benchmark",
                f"{column}_le_variance_ratio",
            )
            ratio_row.update(
                value=ratio,
                passes=float(lower <= ratio <= upper),
                bound_lower=float(lower),
                bound_upper=float(upper),
                requirement=requirement,
                tier=tier,
                applicable=1.0,
                n_members=float(pred_matrix.shape[0]),
                n_time=float(common.size),
            )
            correlation_row = empty_score_row(
                LE_SPREAD_DATA_ID,
                "to_benchmark",
                f"{column}_le_spread_pattern_corr",
                "reported without a bound (the paper sets none)",
            )
            correlation_row.update(
                value=correlation,
                n_members=float(pred_matrix.shape[0]),
                n_time=float(common.size),
            )
            rows.extend([ratio_row, correlation_row])
    return rows


# ---------------------------------------------------------------------------
# Aggregated scalars: one number per data source (no time axis)
# ---------------------------------------------------------------------------


def is_scalar_output(raw_df: pd.DataFrame) -> bool:
    """Whether a ``raw_output`` holds aggregated scalars with a reference.

    A CB2 complex diagnostic writes one row per (data source, scalar) with
    every other scalar column NULL and **no** ``time`` column
    (``CB2ComplexDiagnostic._scalar_outputs``). Such a table is scorable as
    soon as it also carries ``reference`` rows — the observed value of the
    same scalar, emitted by the diagnostic through a ClimateEval DataSource.
    """
    if not {"data_id", "data_type"} <= set(raw_df.columns):
        return False
    # Any coordinate axis means the table is a series, a climatology or a
    # field, not a set of scalars. Without this an annual cycle was scored on
    # its `month_number` axis and a map on `latitude`/`longitude`, each as if
    # the axis were a variable, and the map's variables were collapsed to
    # whatever happened to be in the first grid cell.
    if _AXIS_COLUMNS & set(raw_df.columns) or is_eof_output(raw_df):
        return False
    return bool((raw_df["data_type"] == "reference").any())


def is_scorable_layout(columns: set[str] | frozenset[str]) -> bool:
    """Whether a ``raw_output`` with these columns is one of the pass's shapes.

    A time series (``time``), an EOF-coefficient table or aggregated scalars
    (no coordinate axis at all) — given ``reference`` rows, which the caller
    checks. A map, annual cycle or zonal line is never scored. Used by
    :mod:`climatebench2.frozen_comparison` to tell which comparison rows a
    frozen run cannot do without.
    """
    columns = set(columns)
    if not {"data_id", "data_type"} <= columns:
        return False
    return "time" in columns or _EOF_COLUMNS <= columns or not columns & _AXIS_COLUMNS


def _first_finite_value(frame: pd.DataFrame, column: str) -> float:
    """First finite value of ``column``, or NaN.

    The scalar layout is one row per (source, scalar) with NULLs elsewhere,
    and a per-member run of a complex suite re-emits the *same* reference
    rows into the same database, so duplicates are expected and collapse
    here rather than being scored twice.
    """
    if column not in frame.columns:
        return float("nan")
    return _first_finite(frame[column])


def group_scalar_members(
    raw_df: pd.DataFrame,
    names: dict[str, str],
    column: str,
) -> tuple[dict[tuple[str, str], list[float]], dict[tuple[str, str], list[str]]]:
    """``(data_type, model name) -> [one value per member]`` plus its ids."""
    values: dict[tuple[str, str], list[float]] = {}
    ids: dict[tuple[str, str], list[str]] = {}
    for (data_id, data_type), frame in raw_df.groupby(
        ["data_id", "data_type"],
        sort=True,
    ):
        if str(data_type) == "reference":
            continue
        value = _first_finite_value(frame, column)
        if not np.isfinite(value):
            continue
        key = (str(data_type), names.get(str(data_id), str(data_id)))
        values.setdefault(key, []).append(float(value))
        ids.setdefault(key, []).append(str(data_id))
    return values, ids


def scalar_sigma_obs(references: pd.DataFrame, column: str) -> float:
    """σ_obs of one scalar: the protocol floor plus the emitted companion.

    ``tier2.obs_sigma`` is a per-time-step floor for the *series* variables
    and says nothing about an aggregated statistic, so a diagnostic that
    knows its own observational error emits it as a
    ``<scalar>_sigma_obs`` column on the reference row (for the GMST
    scalars, the GSAT blending term of
    ``tier2.gsat_blending_relative_uncertainty``). The two are combined in
    quadrature; either may be absent, in which case σ_obs is just the other.
    """
    floor = obs_sigma_floor(column)
    emitted = _first_finite_value(references, f"{column}{SCALAR_SIGMA_OBS_SUFFIX}")
    if not np.isfinite(emitted):
        emitted = 0.0
    return float(np.sqrt(floor**2 + emitted**2))


def _scalar_sigma_internal(
    table: SigmaInternal | None,
    var_id: str,
) -> float:
    """σ_int for an aggregated scalar from ``tier2.scalar_consistency``.

    The registry names, per scalar, the ``(variable, statistic, window)``
    triple whose piControl-chunk σ the consistency test should use; the
    window is resolved to a length in years by
    :func:`climatebench2.windows.window_lengths`, and the nearest reported
    length wins (:func:`sigma_internal_for`). An unregistered scalar gets
    0.0 — the test then rests on the ensemble spread and σ_obs alone.
    """
    from climatebench2 import windows

    registry = get_threshold("tier2.scalar_consistency")
    entry = registry.get(var_id)
    if entry is None:
        return 0.0
    lengths = windows.window_lengths()
    n_years = int(lengths.get(str(entry["window"]), 0))
    return sigma_internal_for(
        table,
        str(entry["variable"]),
        str(entry["statistic"]),
        n_years,
    )


def _scalar_consistency_row(
    *,
    data_id: str,
    data_type: str,
    var_id: str,
    values: np.ndarray,
    obs: float,
    sigma_obs: float,
    sigma_internal: SigmaInternal | None,
) -> dict[str, Any] | None:
    """Regime (c) for an aggregated scalar (metrics_reference.md §II.1)."""
    sigma_int = _scalar_sigma_internal(sigma_internal, var_id)
    try:
        result = scoring.ensemble_consistency(
            values,
            obs,
            sigma_internal=sigma_int,
            sigma_obs=sigma_obs,
            p_threshold=float(get_threshold("tier2.consistency_p_value")),
        )
    except ValueError:
        # No ensemble spread and no σ terms: the test would divide by zero,
        # so there is nothing to say rather than something false.
        return None
    row = _empty_row(data_id, data_type, f"{var_id}{SCALAR_CONSISTENCY_SUFFIX}", "")
    row.update(
        value=float(obs),
        z=result.z,
        p_value=result.p_value,
        passes=float(result.passes),
        ensemble_mean=result.ensemble_mean,
        total_sigma=result.total_sigma,
        sigma_internal=sigma_int,
        sigma_obs=sigma_obs,
        n_members=float(values.size),
        n_time=1.0,
    )
    return row


def score_scalar_output(  # noqa: C901
    raw_df: pd.DataFrame,
    data_sources: pd.DataFrame | None,
    *,
    sigma_internal: SigmaInternal | None = None,
    diagnostic: str = "",
) -> list[dict[str, Any]]:
    """Score an aggregated-scalar ``raw_output`` (metrics_reference.md §II.1).

    One scalar per data source and ``var_id``, so the scoring axis has a
    single point: the fair CRPS is the score of that one number (with the
    same observational draws as every other regime), its bootstrap interval
    is undefined (``moving_block_bootstrap_ci`` returns NaN below two points)
    and ``T_eff = 1``. ``E_ref`` and the skill are formed exactly as in
    regimes (a)/(b), and each scalar additionally gets the regime-(c)
    consistency row the protocol asks for — which for the realized warming
    level is the primary statement (§II.1). There is no bootstrap block
    length to choose, so this regime takes no ``tier2.bootstrap`` settings.
    """
    names = source_names(data_sources)
    categories = source_categories(data_sources)
    references = raw_df[raw_df["data_type"] == "reference"]
    var_columns = [
        c
        for c in raw_df.columns
        if c not in _META_COLUMNS
        and not c.endswith(SCALAR_SIGMA_OBS_SUFFIX)
        and not _INDEX_COLUMN_RE.match(str(c))
    ]
    n_draws = int(get_threshold("tier2.obs_uncertainty.n_draws"))
    seed = int(get_threshold("tier2.obs_uncertainty.seed"))

    rows: list[dict[str, Any]] = []
    for column in var_columns:
        obs = _first_finite_value(references, column)
        if not np.isfinite(obs):
            continue  # a model-only scalar (no observational product)
        sigma_obs = scalar_sigma_obs(references, column)
        groups, ids = group_scalar_members(raw_df, names, column)

        scored: dict[tuple[str, str], dict[str, Any]] = {}
        consistency: list[dict[str, Any]] = []
        for key, member_values in groups.items():
            data_type, name = key
            if is_observational(data_type, ids[key], categories):
                scored[key] = _empty_row(
                    name,
                    data_type,
                    column,
                    "observational product (a term in sigma_obs, not scored)",
                )
                continue
            if is_truth(data_type, ids[key], categories):
                scored[key] = _empty_row(
                    name,
                    data_type,
                    column,
                    "perfect-model truth member (the spread-test target, "
                    "not a comparison model)",
                )
                continue
            values = np.asarray(member_values, dtype=float)
            if values.size < _MIN_MEMBERS:
                scored[key] = _empty_row(name, data_type, column, "single member")
                scored[key]["n_members"] = float(values.size)
                scored[key]["n_time"] = 1.0
            else:
                crps_k = scoring.crps_fair_with_obs_draws(
                    values[:, None],
                    np.array([obs], dtype=float),
                    obs_sigma=sigma_obs,
                    n_draws=n_draws,
                    seed=seed,
                )
                summary = scoring.crps_independent_summary(crps_k, values.size)
                row = _empty_row(name, data_type, column, "")
                row.update(
                    crps=summary.score,
                    crps_se=summary.standard_error,
                    t_eff=summary.t_eff,
                    r1=summary.r1,
                    n_members=float(summary.n_members),
                    n_time=float(summary.n_time),
                    sigma_obs=sigma_obs,
                )
                scored[key] = row
            row = _scalar_consistency_row(
                data_id=name,
                data_type=data_type,
                var_id=column,
                values=values,
                obs=float(obs),
                sigma_obs=sigma_obs,
                sigma_internal=sigma_internal,
            )
            if row is not None:
                consistency.append(row)

        if not scored:
            continue
        _apply_reference_skill(scored)
        rows.extend(scored.values())
        rows.extend(consistency)
    _apply_window_labels(
        rows,
        diagnostic,
        perfect_model=is_perfect_model(raw_df, categories),
    )
    return rows


# ---------------------------------------------------------------------------
# Regime (b): EOF-coefficient tables
# ---------------------------------------------------------------------------


def is_eof_output(raw_df: pd.DataFrame) -> bool:
    """Whether a ``raw_output`` holds regime-(b) EOF coefficients."""
    return _EOF_COLUMNS <= set(raw_df.columns)


def _coefficient_vector(frame: pd.DataFrame) -> np.ndarray:
    """One source's coefficients, ordered by mode."""
    ordered = frame.sort_values("mode")
    return ordered["coefficient"].to_numpy(float)


def score_eof_output(
    raw_df: pd.DataFrame,
    data_sources: pd.DataFrame | None,
    *,
    settings: dict[str, Any] | None = None,
    diagnostic: str = "",
) -> list[dict[str, Any]]:
    """Score a regime-(b) coefficient table (metrics_reference.md Tier II (b)).

    Fair CRPS **per standardised coefficient** across a model's members, the
    equal-weight mean over the retained modes as the variable-level score,
    and the same ``E_ref``/skill as regime (a).

    *Uncertainty (CB2 interpretation).* The paper asks for a block bootstrap
    that "resamples spatial blocks and discounts them to an effective sample
    size". The scoring axis here is not space but the **orthogonal,
    standardised coefficients**, which is exactly what that discount is for:
    the retained modes *are* the effective sample. So the interval is an iid
    bootstrap over coefficients (``block_length = 1``) with
    ``T_eff = number of retained modes``, and no serial-correlation
    correction is applied — there is no ordering along a mode index to
    correlate.
    """
    settings = settings or _bootstrap_settings()
    names = source_names(data_sources)
    categories = source_categories(data_sources)
    rows: list[dict[str, Any]] = []

    for var_id, var_frame in raw_df.groupby("var_id", sort=True):
        references = var_frame[var_frame["data_type"] == "reference"]
        if references.empty:
            continue
        obs = _coefficient_vector(
            references[references["data_id"] == references["data_id"].iloc[0]],
        )
        if obs.size == 0:
            continue

        groups: dict[tuple[str, str], list[np.ndarray]] = {}
        ids: dict[tuple[str, str], list[str]] = {}
        for (data_id, data_type), frame in var_frame.groupby(
            ["data_id", "data_type"],
            sort=True,
        ):
            if str(data_type) == "reference":
                continue
            key = (str(data_type), names.get(str(data_id), str(data_id)))
            groups.setdefault(key, []).append(_coefficient_vector(frame))
            ids.setdefault(key, []).append(str(data_id))

        scored: dict[tuple[str, str], dict[str, Any]] = {}
        for key, vectors in groups.items():
            data_type, name = key
            if is_observational(data_type, ids[key], categories):
                scored[key] = _empty_row(
                    name,
                    data_type,
                    str(var_id),
                    "observational product (a term in sigma_obs, not scored)",
                )
                continue
            usable = [v for v in vectors if v.size == obs.size]
            if not usable:
                continue
            members = np.vstack(usable)
            if members.shape[0] < _MIN_MEMBERS:
                scored[key] = _empty_row(name, data_type, str(var_id), "single member")
                scored[key]["n_members"] = float(members.shape[0])
                scored[key]["n_time"] = float(obs.size)
                continue
            scored[key] = _eof_score_row(
                data_id=name,
                data_type=data_type,
                var_id=str(var_id),
                members=members,
                obs=obs,
                settings=settings,
            )

        if not scored:
            continue
        scored["baseline", PATTERN_SCALING_DATA_ID] = _eof_pattern_scaling_row(
            var_frame,
            str(var_id),
        )
        _apply_reference_skill(scored)
        rows.extend(scored.values())
    _apply_window_labels(
        rows,
        diagnostic,
        perfect_model=is_perfect_model(raw_df, source_categories(data_sources)),
    )
    return rows


#: ``data_type`` a future EOF table would carry the CMIP6 multi-model-mean
#: baseline-window projection under. Nothing writes it yet.
MMM_PATTERN_DATA_TYPE = "baseline_pattern"


def _eof_pattern_scaling_row(var_frame: pd.DataFrame, var_id: str) -> dict[str, Any]:
    """Regime (b)'s pattern-scaling baseline — a ``reason`` row for now.

    The spatial half of baseline (iii) is ``ΔT_global(t)`` (which the
    time-series regime now has, from the calibrated EBM) times a
    **normalized CMIP6 multi-model-mean warming pattern**:
    ``baselines.pattern_scaling_forecast`` is that arithmetic. What is
    missing is the pattern itself — the mean over comparison models of
    ``(test-window map − baseline-window map) / ΔGMST`` — because the EOF
    diagnostic projects only the *test-window* anomaly of each source, so
    nothing in the database carries a comparison model's **baseline-window**
    map.

    ⚠ The ``reason`` string below names **upstream ClimateEval PR #44**, and
    that is now out of date: #44 was the blocker while the comparison
    ensemble had no post-2015 member at all, and since 2026-09-20
    ``climatebench2.data.StagedCMIP6HistoricalSSP245`` supplies those. The
    remaining gap is CB2's — a diagnostic that reaches back past the
    test-window cut for the *comparison* sources, the way
    ``ReferenceBaselineRecord`` does for the reference. The text is left
    unchanged because it is written into result databases; fix it when the
    pattern itself is implemented and the row stops being emitted.

    So the row records why there is no number, exactly as a single-member
    model does, rather than the baseline silently disappearing from the
    scorecard.
    """
    if (var_frame["data_type"] == MMM_PATTERN_DATA_TYPE).any():  # pragma: no cover
        # Reserved for the day the projections are in the table; the caller
        # would then build the forecast with pattern_scaling_forecast.
        return _empty_row(
            PATTERN_SCALING_DATA_ID,
            "baseline",
            var_id,
            "pattern-scaling projections present but not yet scored",
        )
    return _empty_row(
        PATTERN_SCALING_DATA_ID,
        "baseline",
        var_id,
        "pattern scaling needs the CMIP6 multi-model-mean baseline-window "
        "maps (upstream ClimateEval PR #44); the GMST trajectory alone is "
        "scored in the time-series regime",
    )


def _eof_score_row(
    *,
    data_id: str,
    data_type: str,
    var_id: str,
    members: np.ndarray,
    obs: np.ndarray,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Fair CRPS per coefficient, its equal-weight mean and mode bootstrap."""
    crps_k = scoring.crps_fair(members, obs)
    summary = scoring.crps_independent_summary(crps_k, members.shape[0])
    lo, hi = scoring.moving_block_bootstrap_ci(
        crps_k,
        block_length=1,  # coefficients are orthogonal: no blocks to preserve
        n_boot=settings["n_boot"],
        alpha=settings["alpha"],
        seed=settings["seed"],
        members=members if settings["resample_members"] else None,
        obs=obs if settings["resample_members"] else None,
    )
    row = _empty_row(data_id, data_type, var_id, "")
    row.update(
        crps=summary.score,
        crps_se=summary.standard_error,
        crps_ci_lo=lo,
        crps_ci_hi=hi,
        t_eff=summary.t_eff,
        r1=summary.r1,
        n_members=float(summary.n_members),
        n_time=float(summary.n_time),
        block_length=1.0,
    )
    return row


# ---------------------------------------------------------------------------
# Database plumbing
# ---------------------------------------------------------------------------


def _table_columns(con: Any, schema: str, table: str) -> dict[str, str]:  # noqa: ANN401
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchall()
    return {str(name): str(dtype) for name, dtype in rows}


def baseline_records(raw_df: pd.DataFrame) -> dict[tuple[str, str], pd.DataFrame]:
    """``(variable, "monthly"|"annual") -> series`` from a raw_output table.

    The rows :class:`climatebench2.diags.ReferenceBaselineRecord` writes: the
    reference's pre-test 1985-2014 area-mean record, which the Tier II cut
    removed from every other table in the database.
    """
    found: dict[tuple[str, str], pd.DataFrame] = {}
    if "data_type" not in raw_df.columns or "time" not in raw_df.columns:
        return found
    for data_type, frequency in (
        (BASELINE_MONTHLY_DATA_TYPE, "monthly"),
        (BASELINE_ANNUAL_DATA_TYPE, "annual"),
    ):
        frame = raw_df[raw_df["data_type"] == data_type]
        if frame.empty:
            continue
        for column in frame.columns:
            if column in _META_COLUMNS:
                continue
            series = frame[["time", column]].dropna().sort_values("time")
            if not series.empty:
                found[column, frequency] = series
    return found


def _database_context(
    con: Any,  # noqa: ANN401
    schemas: dict[str, set[str]],
) -> dict[tuple[str, str], pd.DataFrame]:
    """Baseline records found anywhere in one database."""
    context: dict[tuple[str, str], pd.DataFrame] = {}
    for schema, tables in schemas.items():
        if "raw_output" not in tables:
            continue
        raw_df = deduplicate_raw_output(
            con.execute(f'SELECT * FROM "{schema}"."raw_output"').df(),
        )
        context.update(baseline_records(raw_df))
    return context


def collect_internal_variability(db_paths: list[Path] | list[str]) -> SigmaInternal:
    """σ_int from every ``InternalVariability`` raw_output in the databases.

    ``climatebench2 score`` writes one database per suite and hands the pass
    all of them, so the Tier I database's piControl σ_int reaches the Tier II
    consistency test without either suite knowing about the other.
    """
    import duckdb

    table: SigmaInternal = {}
    for path in db_paths:
        try:
            con = duckdb.connect(str(path), read_only=True)
        except Exception:  # noqa: BLE001 - a locked or missing db is not fatal
            continue
        try:
            for schema in _diagnostic_schemas(con):
                tables = _schema_tables(con, schema)
                if "raw_output" not in tables:
                    continue
                raw_df = deduplicate_raw_output(
                con.execute(f'SELECT * FROM "{schema}"."raw_output"').df(),
            )
                for key, entries in internal_variability_from_raw(raw_df).items():
                    table.setdefault(key, []).extend(entries)
        finally:
            con.close()
    return table


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


def score_database(
    db_path: Path | str,
    *,
    settings: dict[str, Any] | None = None,
    sigma_internal: SigmaInternal | None = None,
) -> PassReport:
    """Run the Tier II scoring pass over one results database, in place.

    Idempotent: rows tagged ``scorer = 'climatebench2'`` are deleted before
    the new ones are inserted, so re-running (``leaderboard --rescore``)
    replaces earlier scores rather than duplicating them.

    ``settings`` overrides the ``tier2.bootstrap`` block (tests and quick
    re-scores; the protocol values come from ``thresholds.yml``);
    ``sigma_internal`` is the piControl internal variability collected from
    the other databases of the same run (:func:`collect_internal_variability`).
    """
    import duckdb

    path = str(db_path)
    report = PassReport(database=path)
    settings = settings or _bootstrap_settings()
    con = duckdb.connect(path)
    try:
        schemas = {
            schema: _schema_tables(con, schema) for schema in _diagnostic_schemas(con)
        }
        baseline = _database_context(con, schemas)
        for schema, tables in schemas.items():
            if "raw_output" not in tables:
                continue
            raw_df = deduplicate_raw_output(
                con.execute(f'SELECT * FROM "{schema}"."raw_output"').df(),
            )
            sources = (
                con.execute(f'SELECT * FROM "{schema}"."data_sources"').df()
                if "data_sources" in tables
                else None
            )
            if is_eof_output(raw_df):
                rows = score_eof_output(
                    raw_df,
                    sources,
                    settings=settings,
                    diagnostic=schema,
                )
            elif is_scalar_output(raw_df):
                rows = score_scalar_output(
                    raw_df,
                    sources,
                    sigma_internal=sigma_internal,
                    diagnostic=schema,
                )
            else:
                rows = score_raw_output(
                    raw_df,
                    sources,
                    settings=settings,
                    baseline=baseline,
                    sigma_internal=sigma_internal,
                    diagnostic=schema,
                )
            if not rows:
                # Still clear this diagnostic's OWN earlier rows: a schema
                # that has stopped being scorable (a map, once the axis
                # columns were excluded from the scalar regime) would
                # otherwise keep the scores of a previous `--rescore`
                # for ever, and the pass would not be idempotent.
                _clear_rows(con, schema, has_metrics="metrics" in tables)
                continue
            frame = pd.DataFrame(rows)[list(_SCORE_COLUMNS)]
            _write_rows(con, schema, frame, has_metrics="metrics" in tables)
            report.diagnostics.append(schema)
            report.rows += len(frame)
            report.scored_models += int(frame["crps"].notna().sum())
            for reason, count in frame.loc[
                frame["reason"] != "",
                "reason",
            ].value_counts().items():
                if str(reason) == "single member":
                    report.single_member += int(count)
                report.messages.append(
                    f"  {schema}: {int(count)} row(s) unscored — {reason}",
                )
    finally:
        con.close()
    return report


def _clear_rows(con: Any, schema: str, *, has_metrics: bool) -> None:  # noqa: ANN401
    """Delete this pass's own rows from ``schema.metrics``, if any."""
    if not has_metrics:
        return
    if "scorer" not in _table_columns(con, schema, "metrics"):
        return
    con.execute(
        f'DELETE FROM "{schema}"."metrics" WHERE scorer = ?',  # noqa: S608
        [SCORER],
    )


def _write_rows(
    con: Any,  # noqa: ANN401
    schema: str,
    frame: pd.DataFrame,
    *,
    has_metrics: bool,
) -> None:
    """Append score rows to ``schema.metrics``, adding columns as needed."""
    con.register("cb2_score_rows", frame)
    try:
        if not has_metrics:
            con.execute(
                f'CREATE TABLE "{schema}"."metrics" AS '
                f"SELECT * FROM cb2_score_rows",
            )
            return
        existing = _table_columns(con, schema, "metrics")
        for column, dtype in _SCORE_COLUMNS.items():
            if column not in existing:
                con.execute(
                    f'ALTER TABLE "{schema}"."metrics" ADD COLUMN "{column}" {dtype}',
                )
        if "scorer" in existing:
            con.execute(
                f'DELETE FROM "{schema}"."metrics" WHERE scorer = ?',  # noqa: S608
                [SCORER],
            )
        columns = ", ".join(f'"{c}"' for c in _SCORE_COLUMNS)
        con.execute(
            f'INSERT INTO "{schema}"."metrics" ({columns}) '  # noqa: S608
            f"SELECT {columns} FROM cb2_score_rows",
        )
    finally:
        con.unregister("cb2_score_rows")


def score_databases(
    db_paths: list[Path] | list[str],
    *,
    settings: dict[str, Any] | None = None,
) -> list[PassReport]:
    """Run :func:`score_database` over several databases.

    σ_int is collected from **all** of them first, so the Tier I database's
    piControl internal variability reaches the Tier II consistency test.
    """
    sigma_internal = collect_internal_variability(db_paths)
    return [
        score_database(path, settings=settings, sigma_internal=sigma_internal)
        for path in db_paths
    ]
