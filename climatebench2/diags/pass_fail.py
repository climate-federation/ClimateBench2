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

A protocol may also make N/A **automatic**: a thresholds block with
``na_if_missing: [var, …]`` makes a gate whose experiments are present but one
of those variables is not write the same N/A rows, with a string column
``na_reason`` (``"missing output: evspsbl"``), instead of no row
(:meth:`SupersetExperimentMixin._missing_output_variables`). Without the key
(ClimateBench2's file has none) nothing changes.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import ibis
import numpy as np
import pandas as pd
from iris.exceptions import ConstraintMismatchError
from loguru import logger
from scipy import signal

from climateeval.diags._base import DiagnosticOutput
from climateeval.diags.simple import Nino34
from climateeval.exceptions import MissingDataError

from climatebench2._thresholds import _MISSING, get_threshold

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


def effective_block(owner: Any, block: str | None) -> str | None:  # noqa: ANN401
    """The ``thresholds.yml`` block ``owner`` reads in place of ``block``.

    ``owner`` is a gate class or instance; its ``_threshold_block`` is either
    a mapping ``{default block: replacement block}`` (a gate with several
    blocks, e.g. ``ClosureGate``'s water- and atmospheric-energy budgets) or a
    string that replaces every block the gate reads (a single-block gate).
    Without ``_threshold_block``, ``block`` is returned unchanged.
    """
    spec = getattr(owner, "_threshold_block", None)
    if spec is None or block is None:
        return block
    if isinstance(spec, str):
        return spec
    return spec.get(block, block)


@dataclass(frozen=True)
class Threshold:
    """A lazy reference to a number in ``thresholds.yml``.

    Accepted by :attr:`GateCheck.lower` / ``upper``; resolved by
    :func:`resolved_checks` under the layer stack active *then*, never at
    import. ``path`` is dotted from the root (``"tier1.ecs.range"``) or, when
    it starts with ``"."``, **relative to the check's block**
    (``".range"`` -> ``"<block>.range"``), so a gate that is repointed at
    another block with ``_threshold_block`` reads the same keys there.
    ``index`` picks one element of a list-valued threshold (a range).
    """

    path: str
    index: int | None = None

    def resolve(self, block: str | None = None) -> float:
        """The concrete number, under the active threshold layers."""
        path = self.path
        if path.startswith("."):
            if block is None:
                msg = f"Relative threshold '{path}' needs a GateCheck.block"
                raise ValueError(msg)
            path = block + path
        value = get_threshold(path)
        if self.index is not None:
            value = value[self.index]
        return float(value)


@dataclass(frozen=True)
class Band:
    """One edge of a ``reference ± tolerance`` band, from two thresholds.

    ``relative=True``: ``reference * (1 ∓ tolerance)``; otherwise
    ``reference ∓ tolerance``. ``side`` is ``"lower"`` or ``"upper"``.
    """

    reference: Threshold
    tolerance: Threshold
    side: Literal["lower", "upper"]
    relative: bool = True

    def resolve(self, block: str | None = None) -> float:
        """The concrete edge, under the active threshold layers."""
        ref = self.reference.resolve(block)
        tol = self.tolerance.resolve(block)
        sign = -1.0 if self.side == "lower" else 1.0
        return ref * (1.0 + sign * tol) if self.relative else ref + sign * tol


def _resolve_bound(bound: Any, block: str | None) -> float | None:  # noqa: ANN401
    if isinstance(bound, (Threshold, Band)):
        return bound.resolve(block)
    return bound


@dataclass(frozen=True)
class GateCheck:
    """One pass/fail check on a column of a diagnostic's raw output.

    ``statistic`` reduces the (time-ordered) series of ``column`` values for
    one data source to a scalar; ``None`` means the column already holds a
    scalar (one row per data source). Bounds are inclusive; ``None`` means
    unbounded on that side.

    Nothing here is read from ``thresholds.yml`` until the gate is evaluated:
    ``lower`` / ``upper`` are a float, ``None``, a :class:`Threshold` or a
    :class:`Band`; ``block`` is the dotted path of the check's
    ``thresholds.yml`` block (``"tier1.ecs"``), from which ``requirement``
    (:data:`REQUIREMENT_TAGS`, via :func:`gate_requirement`) and ``tier``
    (:data:`TIER_TAGS`, via :func:`gate_tier`) are resolved unless given
    explicitly. :func:`resolved_checks` returns the concrete check; both tags
    are carried on every gate row, and the leaderboard groups the scorecard by
    the pair.

    ``annotate`` adds descriptive columns to the gate row: it receives the
    data source's raw-output rows and returns ``{column: str}`` — e.g. I.3c's
    ``reference_source``, which says whether the bound was taken against the
    run-time observations or the stored fallback. Annotation columns are
    strings, never part of the verdict.
    """

    check_id: str
    column: str
    statistic: Callable[[pd.Series], float] | None = None
    lower: float | Threshold | Band | None = None
    upper: float | Threshold | Band | None = None
    requirement: str | None = None
    tier: str | None = None
    annotate: Callable[[pd.DataFrame], dict[str, str]] | None = None
    block: str | None = None

    def resolve(self, owner: Any = None) -> GateCheck:  # noqa: ANN401
        """This check with concrete bounds, ``requirement`` and ``tier``.

        ``owner`` (a gate class or instance) supplies ``_threshold_block``.
        Resolving an already-resolved check returns an equal check.
        """
        block = effective_block(owner, self.block)
        requirement = self.requirement
        if requirement is None:
            requirement = (
                DEFAULT_REQUIREMENT if block is None else gate_requirement(block)
            )
        tier = self.tier
        if tier is None:
            tier = DEFAULT_TIER if block is None else gate_tier(block)
        return replace(
            self,
            lower=_resolve_bound(self.lower, block),
            upper=_resolve_bound(self.upper, block),
            requirement=requirement,
            tier=tier,
            block=block,
        )


def resolved_checks(owner: Any) -> tuple[GateCheck, ...]:  # noqa: ANN401
    """The ``_gate_checks`` of a gate class or instance, bounds made concrete.

    Evaluated under the active threshold layer stack and the owner's
    ``_threshold_block``; call it at evaluation time, never at import.
    """
    return tuple(
        check.resolve(owner) for check in getattr(owner, "_gate_checks", ())
    )


class BlockTag:
    """Class attribute resolving a block's ``requirement`` or ``tier`` lazily.

    ``requirement = BlockTag("tier2.internal_variability")`` reads as the
    string tag (``Cls.requirement == "diagnostic"``) but evaluates under the
    layer stack active at access time, and follows ``_threshold_block``.
    """

    def __init__(self, block: str, what: Literal["requirement", "tier"] = "requirement") -> None:
        """Remember the block and which tag to read."""
        self._block = block
        self._what = what

    def __get__(self, instance: Any, owner: type | None = None) -> str:  # noqa: ANN401
        """Resolve the tag now."""
        block = effective_block(owner if instance is None else instance, self._block)
        assert block is not None  # noqa: S101 - self._block is a str
        return gate_requirement(block) if self._what == "requirement" else gate_tier(block)


class ThresholdBlockMixin:
    """``_threshold_block`` and ``_threshold`` for a diagnostic that has gates.

    ``_threshold_block`` points the diagnostic at another ``thresholds.yml``
    block without redefining its checks (see :func:`effective_block`):
    ``{"tier1.water_budget": "amip.water_budget"}`` for several blocks, or a
    plain string for a gate with one.
    """

    _threshold_block: ClassVar[str | dict[str, str] | None] = None

    def _threshold(self, path: str, default: Any = _MISSING) -> Any:  # noqa: ANN401
        """``get_threshold`` of the gate's own block, honouring ``_threshold_block``.

        ``path`` is the CB2 path (``"tier1.ecs.n_years"``); its block (all
        but the last element) is mapped through :func:`effective_block`.
        """
        block, _, key = path.rpartition(".")
        return get_threshold(f"{effective_block(self, block)}.{key}", default)


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


class ExperimentKeysMixin:
    """Experiment-key indirection: a gate reads ``data[self.keys[role]]``.

    ``keys`` maps a **role** (``control``, ``historical``, ``abrupt``,
    ``aerosol``, ``day``, ``amip``, ...) to the key of that experiment in the
    suite's data dict; the values are ClimateBench2's. ``_required_data_keys``
    is derived from ``keys`` (in ``__init_subclass__``), so a subclass remaps
    its experiments by overriding ``keys`` alone::

        class AmipClosureGate(ClosureGate):
            keys = {"control": "amip"}

    ``optional_keys`` maps roles the gate can use but does not require to
    their default keys; they are never part of ``_required_data_keys``.
    A subclass must keep every role of its parent (it may add roles).
    """

    keys: ClassVar[dict[str, str]] = {}
    optional_keys: ClassVar[dict[str, str]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401
        """Derive ``_required_data_keys`` from an overridden ``keys``."""
        parent_roles = set().union(
            *(set(getattr(base, "keys", {}) or ()) for base in cls.__bases__),
        )
        super().__init_subclass__(**kwargs)
        if "keys" not in cls.__dict__:
            return
        missing = parent_roles - set(cls.keys)
        if missing:
            msg = f"{cls.__name__}.keys must keep the roles {sorted(missing)} of its base"
            raise TypeError(msg)
        cls._required_data_keys = tuple(dict.fromkeys(cls.keys.values()))  # type: ignore[attr-defined]


def gate_metrics(
    raw_df: pd.DataFrame,
    checks: tuple[GateCheck, ...],
    *,
    time_column: str = "time",
) -> pd.DataFrame:
    """Evaluate ``checks`` per data source of a raw-output DataFrame.

    Checks still holding lazy bounds are resolved here, under the active
    layer stack; pass :func:`resolved_checks` of a gate to honour its
    ``_threshold_block``.
    """
    checks = tuple(check.resolve() for check in checks)
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
    reason: str | None = None,
) -> pd.DataFrame:
    """Gate rows for checks a submission declared not applicable.

    One row per check with ``value``/``passes`` NaN and ``applicable = 0.0``
    — neither a pass nor a fail (paper §7.1). Nothing is computed, so this
    needs no data at all. ``reason`` (an automatic N/A, e.g. ``"missing
    output: evspsbl"``) adds the string column ``na_reason``; a declared N/A
    has none.
    """
    checks = tuple(check.resolve() for check in checks)
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
                **({} if reason is None else {"na_reason": reason}),
            }
            for check in checks
        ],
    )


def na_if_missing_variables(
    checks: tuple[GateCheck, ...],
    threshold_block: str | dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Variables whose absence makes a gate automatically N/A (``na_if_missing``).

    Read lazily from the gate's thresholds block(s): ``threshold_block`` (the
    gate class's ``_threshold_block``) if given, else the ``block`` of each of
    its checks. ``()`` — the feature is off — when no block is known or none
    carries the key, which is every ClimateBench2 gate.
    """
    own = [b for b in (getattr(c, "block", None) for c in checks) if b]
    if isinstance(threshold_block, str):
        # One string repoints every block of the gate (``effective_block``).
        blocks = [threshold_block]
    elif threshold_block:
        # A mapping ``{default block: replacement block}``.
        blocks = [threshold_block.get(b, b) for b in own]
    else:
        blocks = own
    names: list[str] = []
    for block in dict.fromkeys(blocks):
        listed = get_threshold(f"{block}.na_if_missing", None)
        if listed:
            names.extend([listed] if isinstance(listed, str) else map(str, listed))
    return tuple(dict.fromkeys(names))


def _has_variable(data: Any, var_name: str) -> bool | None:  # noqa: ANN401
    """Whether an experiment's data holds ``var_name``; ``None`` if unknowable.

    A CubeList (cubes with a ``var_name``) or an xarray ``Dataset``; anything
    else (a test stub) cannot be inspected, so the answer is "don't know".
    """
    if hasattr(data, "data_vars"):
        return var_name in data.data_vars
    try:
        items = list(data)
    except TypeError:
        return None
    if items and not all(hasattr(item, "var_name") for item in items):
        return None
    return any(item.var_name == var_name for item in items)


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


def _with_empty_na_reason(output: DiagnosticOutput) -> DiagnosticOutput:
    """Give an evaluated gate's rows the ``na_reason`` column (empty string).

    Only where the gate *can* write an automatic N/A (``na_if_missing``), so
    that its evaluated and its N/A rows share one ``metrics`` schema when they
    are appended into one table.
    """
    metrics = output.metrics.to_pandas()
    if "na_reason" in metrics.columns:
        return output
    metrics["na_reason"] = ""
    return DiagnosticOutput(
        raw_output=output.raw_output,
        metrics=ibis.memtable(metrics),
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
    """Map every gate ``check_id`` of the active protocol to one attribute.

    Collected from the ``_gate_checks`` of every diagnostic exported by each
    of the protocol's ``diags_modules`` (:mod:`climatebench2.diags` for
    ClimateBench2), so the leaderboard knows the *full* set of
    checks — including those whose gate did not run and therefore wrote no
    row, which is what makes an entry ticket read "incomplete" rather than
    silently passing.
    """
    # local imports: climatebench2.diags imports this module
    from importlib import import_module

    from climatebench2.protocol import get_protocol

    values: dict[str, str] = {}
    for module_name in get_protocol().diags_modules:
        module = import_module(module_name)
        for name in module.__all__:
            for check in resolved_checks(getattr(module, name)):
                if isinstance(check, GateCheck):
                    values[check.check_id] = getattr(check, attribute)
    return values


def gate_requirements() -> dict[str, str]:
    """Map every CB2 gate ``check_id`` to its requirement tag."""
    return _gate_attribute("requirement")


def gate_tiers() -> dict[str, str]:
    """Map every CB2 gate ``check_id`` to its protocol tier (I / II / III)."""
    return _gate_attribute("tier")


class GateMixin(ThresholdBlockMixin):
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
    _not_applicable: tuple[str, ...] = ()

    def __init__(
        self,
        *args: Any,
        not_applicable: list[str] | tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> None:
        """Take the ``not_applicable`` kwarg, which ClimateEval's simple diagnostics lack.

        ``score --not-applicable NAME`` reaches a gate-bearing diagnostic of a
        *cube* suite through its own stanza's ``diagnostic_kwargs`` (the CLI
        writes it there); this is what accepts it. The complex gates take it
        first, in :class:`SupersetExperimentMixin`.
        """
        if not_applicable is not None:
            self._not_applicable = tuple(not_applicable)
        super().__init__(*args, **kwargs)

    @property
    def declared_not_applicable(self) -> bool:
        """Whether this gate was declared not applicable by the submission."""
        return getattr(self, "name", None) in self._not_applicable

    def get_output(self, data: Any, data_information: Any) -> DiagnosticOutput:
        if self.declared_not_applicable:
            logger.info(
                f"Gate '{self.name}' declared not applicable for "  # type: ignore[attr-defined]
                f"'{data_information.id}': emitting N/A rows without computing",
            )
            rows = not_applicable_metrics(resolved_checks(self), data_id=data_information.id)
            return DiagnosticOutput(
                raw_output=None,
                metrics=None if rows.empty else ibis.memtable(rows),
                variables=None,  # type: ignore[arg-type] - Suite skips None tables
                data_sources=None,  # type: ignore[arg-type]
            )
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
        checks = resolved_checks(self)
        output = apply_gate(output, checks)
        if output.metrics is not None and na_if_missing_variables(
            checks,
            getattr(self, "_threshold_block", None),
        ):
            output = _with_empty_na_reason(output)
        return output


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

    **Automatic N/A** (``na_if_missing``). A gate's thresholds block (its
    class's ``_threshold_block``, else the ``block`` of its checks) may list
    variables: when the gate's required experiments are present but one of
    these variables has no cube in them — checked up front, and also when the
    gate raises a missing-data error that names one — the gate writes the N/A
    rows of a declared one plus ``na_reason = "missing output: <var>[, …]"``
    instead of no row, whatever ``fail_on_missing_data`` says: the protocol
    declared the output optional. A gate with no such key behaves as before.

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

    def _na_if_missing(self) -> tuple[str, ...]:
        """The ``na_if_missing`` variables of this gate's thresholds block."""
        return na_if_missing_variables(
            getattr(self, "_gate_checks", ()),
            getattr(self, "_threshold_block", None),
        )

    def _missing_output_variables(self, data: dict[str, Any]) -> list[str]:
        """``na_if_missing`` variables absent from the gate's experiments."""
        listed = self._na_if_missing()
        if not listed:
            return []
        experiments = [
            data[key]
            for key in getattr(self, "_required_data_keys", ())
            if key in data
        ]
        return [
            name
            for name in listed
            if any(_has_variable(exp, name) is False for exp in experiments)
        ]

    def _not_applicable_output(
        self,
        data_information: Any,  # noqa: ANN401
        reason: str | None = None,
    ) -> DiagnosticOutput:
        """Gate rows marking every check of this diagnostic as N/A."""
        checks = resolved_checks(self)
        rows = not_applicable_metrics(
            checks,
            data_id=data_information.id,
            reason=reason,
        )
        return DiagnosticOutput(
            raw_output=None,
            metrics=None if rows.empty else ibis.memtable(rows),
            variables=None,  # type: ignore[arg-type] - Suite skips None tables
            data_sources=None,  # type: ignore[arg-type]
        )

    def _automatic_not_applicable(
        self,
        missing: list[str],
        data_information: Any,  # noqa: ANN401
    ) -> DiagnosticOutput:
        """N/A rows for a gate whose optional output is absent (``na_if_missing``)."""
        reason = f"missing output: {', '.join(missing)}"
        logger.warning(
            f"Gate '{self.name}' is not applicable for "  # type: ignore[attr-defined]
            f"'{data_information.id}' ({reason}): emitting N/A rows",
        )
        return self._not_applicable_output(data_information, reason)

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
        missing = self._missing_output_variables(data)
        if missing:
            return self._automatic_not_applicable(missing, data_information)
        try:
            return super().get_output(data, data_information)  # type: ignore[misc]
        except (ConstraintMismatchError, MissingDataError) as exc:
            named = [
                name
                for name in self._na_if_missing()
                if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", str(exc))
            ]
            if named:
                return self._automatic_not_applicable(named, data_information)
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
    """The two ENSO checks on ``column``; bounds resolve at evaluation time."""
    return (
        GateCheck(
            check_id="enso_amplitude",
            column=column,
            statistic=lambda s: float(s.std(ddof=1)),
            lower=Threshold(".amplitude_range", 0),
            upper=Threshold(".amplitude_range", 1),
            block="tier1.enso",
        ),
        GateCheck(
            check_id="enso_spectral_ratio",
            column=column,
            statistic=band_power_ratio,
            lower=Threshold(".band_power_ratio_min"),
            block="tier1.enso",
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
