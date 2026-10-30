"""Tier I pass/fail gate machinery and the Phase-1 gate diagnostics.

A *gate* turns a quantity computed by an existing ClimateEval diagnostic into
a binary pass/fail against a bound from ``thresholds.yml``, emitted as extra
rows in the diagnostic's ``metrics`` table with the columns::

    data_id | data_type | var_id | value | bound_lower | bound_upper |
    passes | requirement | applicable

``passes`` is 1.0/0.0 (float, so the DuckDB schema stays numeric). Gates are
applied to *every* data source the diagnostic processed (the benchmarked
model, references, CMIP6 comparison models), so observations act as a sanity
check on the thresholds themselves.

``requirement`` carries the protocol standing of the check — ``required``
(the entry ticket), ``extended`` (reported, additional credit, does not gate
entry), ``extra`` (code-only sanity check) or ``diagnostic`` (a Tier II
aggregated diagnostic emitted through the gate machinery) — read from the
gate's ``thresholds.yml`` block by :func:`gate_requirement`, never hard-coded.

``applicable`` is 1.0 for a check that was evaluated and 0.0 for one a
submission *declared* not applicable (``score --not-applicable NAME``; paper
§7.1 — e.g. I.3b geostrophic balance for a model with no dynamical
representation). A declared-N/A check emits one row per check with
``value``/``passes`` NaN, which is how the scorecard distinguishes "does not
apply" from "was not run" (no row at all).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import ibis
import numpy as np
import pandas as pd
from iris.exceptions import ConstraintMismatchError
from loguru import logger
from scipy import signal

from climateeval.diags._base import DiagnosticOutput
from climateeval.diags.simple import Nino34
from climateeval.exceptions import MissingDataError

from climatebench2._thresholds import get_threshold

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

MONTHS_PER_YEAR = 12


# ---------------------------------------------------------------------------
# Gate machinery
# ---------------------------------------------------------------------------


#: Protocol standing of a gate (paper §7.1); see the module docstring.
REQUIREMENT_TAGS = ("required", "extended", "extra", "diagnostic")

#: Tag assumed for a check whose block predates the tags (backward compat).
DEFAULT_REQUIREMENT = "required"

#: Protocol tier a check belongs to. Independent of the *suite* it runs in:
#: the mid-Holocene monsoon gate is emitted through the same gate machinery
#: as the Tier I checks but is a Tier III Extended test, and the Pinatubo /
#: hemispheric-asymmetry sign flags are Tier II.
TIER_TAGS = ("I", "II", "III")

#: Tier assumed for a check whose ``thresholds.yml`` block has no ``tier:``.
DEFAULT_TIER = "I"


def gate_requirement(threshold_block: str) -> str:
    """Return the ``requirement`` tag of a ``thresholds.yml`` gate block.

    ``threshold_block`` is the dotted path of the block holding the gate's
    bounds, e.g. ``"tier1.ecs"``. The tag is protocol metadata, so it lives
    in ``thresholds.yml`` next to the bounds and is never hard-coded in a
    diagnostic.

    Raises
    ------
    KeyError
        If the block has no ``requirement`` key (a protocol bug).
    ValueError
        If the tag is not one of :data:`REQUIREMENT_TAGS`.
    """
    tag = get_threshold(f"{threshold_block}.requirement")
    if tag not in REQUIREMENT_TAGS:
        msg = (
            f"Unknown requirement '{tag}' in thresholds.yml block "
            f"'{threshold_block}' (expected one of {REQUIREMENT_TAGS})"
        )
        raise ValueError(msg)
    return str(tag)


def gate_tier(threshold_block: str) -> str:
    """Return the ``tier`` tag (I / II / III) of a ``thresholds.yml`` block.

    Which tier a check belongs to is protocol metadata, so — like
    ``requirement`` — it lives beside the bounds rather than being inferred
    from the suite the gate happens to run in. A block with no ``tier:`` key
    falls back to :data:`DEFAULT_TIER`, which keeps result databases written
    before the tag readable.

    Raises
    ------
    ValueError
        If the tag is not one of :data:`TIER_TAGS`.
    """
    try:
        tag = get_threshold(f"{threshold_block}.tier")
    except KeyError:
        return DEFAULT_TIER
    if tag not in TIER_TAGS:
        msg = (
            f"Unknown tier '{tag}' in thresholds.yml block "
            f"'{threshold_block}' (expected one of {TIER_TAGS})"
        )
        raise ValueError(msg)
    return str(tag)


@dataclass(frozen=True)
class GateCheck:
    """One pass/fail check on a column of a diagnostic's raw output.

    ``statistic`` reduces the (time-ordered) series of ``column`` values for
    one data source to a scalar; ``None`` means the column already holds a
    scalar (one row per data source). Bounds are inclusive; ``None`` means
    unbounded on that side. ``requirement`` is the protocol standing of the
    check (:data:`REQUIREMENT_TAGS`) and comes from
    :func:`gate_requirement`; ``tier`` (:data:`TIER_TAGS`) is the protocol
    tier it belongs to and comes from :func:`gate_tier`. Both are carried on
    every gate row, and the leaderboard groups the scorecard by the pair.

    ``annotate`` adds descriptive columns to the gate row: it receives the
    data source's raw-output rows and returns ``{column: str}`` — e.g. I.3c's
    ``reference_source``, which says whether the bound was taken against the
    run-time observations or the stored fallback. Annotation columns are
    strings, never part of the verdict.
    """

    check_id: str
    column: str
    statistic: Callable[[pd.Series], float] | None = None
    lower: float | None = None
    upper: float | None = None
    requirement: str = DEFAULT_REQUIREMENT
    tier: str = DEFAULT_TIER
    annotate: Callable[[pd.DataFrame], dict[str, str]] | None = None


def _gate_row(check: GateCheck, **fields: Any) -> dict[str, Any]:
    """Common columns of a gate metrics row."""
    return {
        "var_id": check.check_id,
        "bound_lower": np.nan if check.lower is None else check.lower,
        "bound_upper": np.nan if check.upper is None else check.upper,
        "requirement": check.requirement,
        "tier": check.tier,
        **fields,
    }


def gate_metrics(
    raw_df: pd.DataFrame,
    checks: tuple[GateCheck, ...],
    *,
    time_column: str = "time",
) -> pd.DataFrame:
    """Evaluate ``checks`` per data source of a raw-output DataFrame."""
    rows: list[dict[str, Any]] = []
    group_cols = [c for c in ("data_id", "data_type") if c in raw_df.columns]
    for keys, group in raw_df.groupby(group_cols, sort=False):
        key_dict = dict(zip(group_cols, keys if isinstance(keys, tuple) else (keys,)))
        if time_column in group.columns:
            group = group.sort_values(time_column)
        for check in checks:
            if check.column not in group.columns:
                continue
            series = group[check.column].dropna()
            if series.empty:
                continue
            value = (
                float(series.iloc[0])
                if check.statistic is None
                else float(check.statistic(series))
            )
            passes = (check.lower is None or value >= check.lower) and (
                check.upper is None or value <= check.upper
            )
            rows.append(
                {
                    **key_dict,
                    **_gate_row(
                        check,
                        value=value,
                        passes=float(passes),
                        applicable=1.0,
                    ),
                    **(check.annotate(group) if check.annotate else {}),
                },
            )
    return pd.DataFrame(rows)


def not_applicable_metrics(
    checks: tuple[GateCheck, ...],
    *,
    data_id: str,
    data_type: str = "to_benchmark",
) -> pd.DataFrame:
    """Gate rows for checks a submission declared not applicable.

    One row per check with ``value``/``passes`` NaN and ``applicable = 0.0``
    — neither a pass nor a fail (paper §7.1). Nothing is computed, so this
    needs no data at all.
    """
    return pd.DataFrame(
        [
            {
                "data_id": data_id,
                "data_type": data_type,
                **_gate_row(
                    check,
                    value=np.nan,
                    passes=np.nan,
                    applicable=0.0,
                ),
            }
            for check in checks
        ],
    )


def _with_gate_rows(
    output: DiagnosticOutput,
    gates_df: pd.DataFrame,
    *,
    raw_output: Any = None,
) -> DiagnosticOutput:
    """Merge gate rows into a diagnostic output's ``metrics`` table."""
    # The gate rows' string columns: `requirement`, `tier` and any
    # `GateCheck.annotate` column (identity columns aside).
    string_columns = [
        c
        for c in gates_df.columns
        if not pd.api.types.is_numeric_dtype(gates_df[c])
        and c not in ("data_id", "data_type", "var_id")
    ]
    if output.metrics is not None:
        existing = output.metrics.to_pandas()
        gates_df = pd.concat([existing, gates_df], ignore_index=True, sort=False)
    # Keep them string columns: rows from non-gate metrics (or from a check
    # without that annotation) have no value, and a NaN there would make ibis
    # infer a numeric column.
    for column in string_columns:
        gates_df[column] = gates_df[column].fillna("")
    return DiagnosticOutput(
        raw_output=output.raw_output if raw_output is None else raw_output,
        metrics=ibis.memtable(gates_df),
        variables=output.variables,
        data_sources=output.data_sources,
    )


def apply_gate(
    output: DiagnosticOutput,
    checks: tuple[GateCheck, ...],
) -> DiagnosticOutput:
    """Append gate metrics to a :class:`DiagnosticOutput`."""
    if output.raw_output is None or not checks:
        return output
    raw_df = output.raw_output.to_pandas()
    gates_df = gate_metrics(raw_df, checks)
    if gates_df.empty:
        return output
    return _with_gate_rows(output, gates_df)


def _gate_attribute(attribute: str) -> dict[str, str]:
    """Map every CB2 gate ``check_id`` to one attribute of its check.

    Collected from the ``_gate_checks`` of every diagnostic exported by
    :mod:`climatebench2.diags`, so the leaderboard knows the *full* set of
    checks — including those whose gate did not run and therefore wrote no
    row, which is what makes an entry ticket read "incomplete" rather than
    silently passing.
    """
    from climatebench2 import diags  # local: climatebench2.diags imports this module

    values: dict[str, str] = {}
    for name in diags.__all__:
        for check in getattr(getattr(diags, name), "_gate_checks", ()):
            if isinstance(check, GateCheck):
                values[check.check_id] = getattr(check, attribute)
    return values


def gate_requirements() -> dict[str, str]:
    """Map every CB2 gate ``check_id`` to its requirement tag."""
    return _gate_attribute("requirement")


def gate_tiers() -> dict[str, str]:
    """Map every CB2 gate ``check_id`` to its protocol tier (I / II / III)."""
    return _gate_attribute("tier")


class GateMixin:
    """Mixin appending ``_gate_checks`` results to any diagnostic's output.

    Two class-level switches, set for a whole run by
    ``climatebench2 score`` (:func:`gate_switches`), never per instance:

    ``comparison_ensemble``
        Whether a gate that wraps an upstream ClimateEval complex diagnostic
        runs that diagnostic's CMIP6 comparison ensemble too
        (``_UpstreamGate._get_other_data`` in ``tier1_physics``). Upstream's
        ECS, forced-response and heat-transport diagnostics glob the CMIP6
        pool through ESMValCore and compute every model found — 90 % of a
        Tier I run — and the values reach no gate decision of the
        submission's (they only add gate rows for the comparison models
        themselves). ``score --comparison frozen|none`` turns it off.
    ``isolate_crashes``
        Whether an unexpected exception inside a gate is logged (marker
        ``CB2_GATE_CRASH``, with its traceback) and turned into an empty
        output, so the suite carries on with the next diagnostic — ClimateEval's
        ``Suite.get_database`` has no ``try`` around its loop, so otherwise
        one crashing gate aborts the rest of the suite. The gate then simply
        has no row, which the scorecard shows as "not run".
    """

    _gate_checks: ClassVar[tuple[GateCheck, ...]] = ()
    comparison_ensemble: ClassVar[bool] = True
    isolate_crashes: ClassVar[bool] = False

    def get_output(self, data: Any, data_information: Any) -> DiagnosticOutput:
        if not GateMixin.isolate_crashes:
            return self._gated_output(data, data_information)
        try:
            return self._gated_output(data, data_information)
        except (ConstraintMismatchError, MissingDataError):
            raise  # missing data: SupersetExperimentMixin's own skip handles it
        except Exception:  # noqa: BLE001 - isolation is the point
            logger.opt(exception=True).error(
                f"CB2_GATE_CRASH gate='{getattr(self, 'name', type(self).__name__)}' "
                f"data='{getattr(data_information, 'id', data_information)}': "
                "emitting no rows and continuing with the next diagnostic",
            )
            return DiagnosticOutput(
                raw_output=None,
                metrics=None,
                variables=None,  # type: ignore[arg-type] - Suite skips None tables
                data_sources=None,  # type: ignore[arg-type]
            )

    def _gated_output(self, data: Any, data_information: Any) -> DiagnosticOutput:
        output = super().get_output(data, data_information)  # type: ignore[misc]
        return apply_gate(output, self._gate_checks)


@contextmanager
def gate_switches(
    *,
    comparison_ensemble: bool = True,
    isolate_crashes: bool = False,
) -> Iterator[None]:
    """Set :class:`GateMixin`'s run-wide switches, restoring them on exit."""
    saved = (GateMixin.comparison_ensemble, GateMixin.isolate_crashes)
    GateMixin.comparison_ensemble = comparison_ensemble
    GateMixin.isolate_crashes = isolate_crashes
    try:
        yield
    finally:
        GateMixin.comparison_ensemble, GateMixin.isolate_crashes = saved


class SupersetExperimentMixin:
    """Mixin: superset experiment keys + graceful skip when they are absent.

    ClimateEval's ``ComplexDiagnostic`` requires the data dict to match
    ``_required_data_keys`` *exactly*. CB2 runs a whole Tier I suite from one
    experiment dict (``Suite.get_database`` hands the same object to every
    diagnostic), so CB2 complex diagnostics — and the CB2 gate wrappers around
    upstream ClimateEval ones — accept a *superset* of their required keys.
    With ``fail_on_missing_data=False`` (the CLI default) a gate whose
    experiments were not supplied at all is skipped with a warning instead of
    aborting the suite.

    ``not_applicable`` (diagnostic kwarg, from ``score --not-applicable
    NAME``) lists gate diagnostics a submission declares inapplicable. A
    diagnostic whose own name is listed computes nothing and emits one gate
    row per check with ``applicable = 0.0`` and NaN ``value``/``passes`` —
    *declared* N/A, which the scorecard must show as neither pass nor fail
    (paper §7.1), and which is distinct from a gate that simply did not run
    (no rows at all).

    Mix in *before* :class:`GateMixin` so the skip short-circuits gating too.
    """

    def __init__(
        self,
        name: str,
        *,
        not_applicable: list[str] | tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize class instance, recording any declared N/A gates."""
        self._not_applicable = tuple(not_applicable or ())
        super().__init__(name, **kwargs)  # type: ignore[call-arg]

    @property
    def declared_not_applicable(self) -> bool:
        """Whether this gate was declared not applicable by the submission."""
        return getattr(self, "name", None) in getattr(self, "_not_applicable", ())

    def _not_applicable_output(self, data_information: Any) -> DiagnosticOutput:
        """Gate rows marking every check of this diagnostic as N/A."""
        checks: tuple[GateCheck, ...] = getattr(self, "_gate_checks", ())
        rows = not_applicable_metrics(checks, data_id=data_information.id)
        return DiagnosticOutput(
            raw_output=None,
            metrics=None if rows.empty else ibis.memtable(rows),
            variables=None,  # type: ignore[arg-type] - Suite skips None tables
            data_sources=None,  # type: ignore[arg-type]
        )

    def _check_required_dict_keys(self, dict_: dict[str, Any], dict_name: str) -> None:
        """Require a *subset* match so one suite dict feeds every gate."""
        required: tuple[str, ...] = self._required_data_keys  # type: ignore[attr-defined]
        missing = set(required) - set(dict_)
        if missing:
            name = getattr(self, "name", type(self).__name__)
            msg = (
                f"Missing keys {sorted(missing)} for {dict_name} dictionary of "
                f"diagnostic '{name}' (required: {required})"
            )
            raise ValueError(msg)

    def _empty_output(self) -> DiagnosticOutput:
        """No rows at all — the Suite skips ``None`` tables."""
        return DiagnosticOutput(
            raw_output=None,
            metrics=None,
            variables=None,  # type: ignore[arg-type]
            data_sources=None,  # type: ignore[arg-type]
        )

    def get_output(self, data: Any, data_information: Any) -> DiagnosticOutput:
        """Run the diagnostic; degrade to an empty output on missing data."""
        if self.declared_not_applicable:
            logger.info(
                f"Gate '{self.name}' declared not applicable for "  # type: ignore[attr-defined]
                f"'{data_information.id}': emitting N/A rows without computing",
            )
            return self._not_applicable_output(data_information)
        try:
            self._check_required_dict_keys(data, "data")
        except ValueError as exc:
            if self._fail_on_missing_data:  # type: ignore[attr-defined]
                raise
            logger.warning(f"Skipping gate '{self.name}': {exc}")  # type: ignore[attr-defined]
            return self._empty_output()
        try:
            return super().get_output(data, data_information)  # type: ignore[misc]
        except (ConstraintMismatchError, MissingDataError) as exc:
            # A VARIABLE the diagnostic wants is absent from an experiment
            # that *was* supplied — e.g. the Tier II submission carries the
            # nine core variables but not `rsds` (the Pinatubo dimming flag)
            # or `clt` (the low-cloud covariance). Missing-key handling above
            # covers a missing experiment; this covers a missing variable
            # inside one, which otherwise aborts the whole suite mid-run.
            if self._fail_on_missing_data:  # type: ignore[attr-defined]
                raise
            logger.warning(
                f"Skipping gate '{self.name}': a required variable is absent "  # type: ignore[attr-defined]
                f"from the data given ({exc})",
            )
            return self._empty_output()


# ---------------------------------------------------------------------------
# Series statistics used by gates
# ---------------------------------------------------------------------------


def band_power_ratio(
    series: pd.Series,
    *,
    fs: float = float(MONTHS_PER_YEAR),
    numerator_period_years: tuple[float, float] = (2.0, 7.0),
    denominator_period_years: tuple[float, float] = (1.0, 2.0),
) -> float:
    """Ratio of *integrated* spectral power between two period bands.

    Welch PSD, then ``∫S df`` over each band (paper I.5b: integrated power,
    not the mean PSD — the 1–2 yr band spans 0.5 cycles/yr against the 2–7 yr
    band's 0.357, so a mean-PSD ratio runs ≈ 1.4× high).

    Defaults implement the ENSO spectral-shape check (metrics_reference I.5b):
    power in the 2–7 yr band over the 1–2 yr band of a monthly index.
    """
    x = np.asarray(series, dtype=float)
    x = x[np.isfinite(x)]
    nperseg = min(len(x), 20 * MONTHS_PER_YEAR)  # 20-yr segments resolve 7-yr power
    freqs, psd = signal.welch(x - x.mean(), fs=fs, nperseg=nperseg)

    def band_power(period_band: tuple[float, float]) -> float:
        lo_p, hi_p = period_band  # years
        mask = (freqs >= 1.0 / hi_p) & (freqs <= 1.0 / lo_p)
        if mask.sum() < 2:  # cannot integrate over fewer than two frequencies
            return float("nan")
        return float(np.trapezoid(psd[mask], freqs[mask]))

    return band_power(numerator_period_years) / band_power(denominator_period_years)


# ---------------------------------------------------------------------------
# Phase-1 gate diagnostics
# ---------------------------------------------------------------------------


def _enso_checks(column: str) -> tuple[GateCheck, ...]:
    amp_lo, amp_hi = get_threshold("tier1.enso.amplitude_range")
    ratio_min = get_threshold("tier1.enso.band_power_ratio_min")
    requirement = gate_requirement("tier1.enso")
    tier = gate_tier("tier1.enso")
    return (
        GateCheck(
            check_id="enso_amplitude",
            column=column,
            statistic=lambda s: float(s.std(ddof=1)),
            lower=amp_lo,
            upper=amp_hi,
            requirement=requirement,
            tier=tier,
        ),
        GateCheck(
            check_id="enso_spectral_ratio",
            column=column,
            statistic=band_power_ratio,
            lower=ratio_min,
            requirement=requirement,
            tier=tier,
        ),
    )


class ENSOGate(GateMixin, Nino34):
    """Tier I checks I.5a/b: ENSO amplitude and spectral shape.

    Runs ClimateEval's ``Nino34`` diagnostic (deseasonalised monthly Niño-3.4
    SST anomalies) with ``_rolling_window_length = 1``: the paper's σ and
    spectrum are of the *unsmoothed* monthly anomalies, whereas ClimateEval's
    default 3-month running mean (the operational ONI definition) lowers σ by
    roughly 5–10% and reddens the spectrum. Then gates:

    - amplitude: σ(Niño-3.4) within ``tier1.enso.amplitude_range``;
    - spectral shape: Welch band-power ratio (2–7 yr)/(1–2 yr) above
      ``tier1.enso.band_power_ratio_min``.

    Teleconnections (I.5c) need pr/ta fields regressed on the index and land
    in Phase 4 (see docs/climateeval_delineation_plan.md §5).
    """

    # No 3-month running mean: gate the raw monthly anomalies (paper I.5a/b).
    # ClimateEval PR #46 made `_rolling_window_length = 1` a supported no-op in
    # `Nino34._preprocess` (iris refuses a rolling window shorter than two
    # points, so before that the unsmoothed index could not be expressed
    # upstream and CB2 carried a copy of the chain). It is merged, so setting
    # the ClassVar is the whole override.
    _rolling_window_length: ClassVar[int] = 1

    # The Nino34 suite entry uses variable id `tos_nino34` (kept for
    # compatibility with ClimateEval's Tier2_ocean_monthly stanza).
    _gate_checks = _enso_checks("tos_nino34")
