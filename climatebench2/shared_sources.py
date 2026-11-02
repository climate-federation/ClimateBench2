"""Load a suite's reference and comparison ensemble ONCE for all members.

A per-member cube suite (``_cli.SuiteSpec.per_member``) runs once per
submission member, and every one of those runs used to re-load the suite's
``reference_data`` and its whole ``other_data`` comparison ensemble
(:class:`climatebench2.data.StagedCMIP6HistoricalSSP245`, 109-160 members)
from disk: ClimateEval's ``Suite.get_database`` rebuilds every diagnostic per
call, and ``SimpleDiagnostic.get_output`` calls ``DataSource.get_cube`` for
the reference and for each comparison source inside the run, with no cache.
Ten submission members meant ten reads of the ensemble — 1,190 daily ``pr``
loads for the held-out extremes entry, which timed out at 12 h twice
(2026-09-26/28).

Nothing about those two stages depends on the member. ``get_output`` runs

1. ``_get_reference_cubes()`` — load + preprocess every reference;
2. ``_get_output_of_reference_data(reference_cubes)`` — its raw-output rows;
3. ``_get_output_of_to_benchmark_data(data, info, reference_cubes)`` — the
   **member**;
4. ``_get_output_of_other_data(reference_cubes)`` — load + preprocess every
   comparison source, its raw-output rows and its metrics against the
   reference,

and only step 3 sees the member. So :func:`run_members` runs a per-member
suite **one diagnostic at a time**: it builds that diagnostic once, runs it
for every member, and memoises steps 1, 2 and 4 on the instance for the
duration. What is held is therefore

- the **preprocessed** reference cubes (step 1 realises them anyway), and
- the comparison sources' **raw-output / metrics tables** (step 4's output:
  one row of scalars, a ~25-year annual series or a histogram per source —
  kilobytes), never their cubes,

and it is dropped as soon as that diagnostic has seen its last member, so
the peak is the same reference cube a single ``get_output`` call already
held, plus the comparison tables of one diagnostic.

Running diagnostic-major rather than member-major writes the **same rows**:
each diagnostic writes its own DuckDB schema, and within a schema the
members are still inserted in order. Only the log order changes.

What is shared, and what is not
-------------------------------
A diagnostic is shared only when that cannot change a value:

- a ClimateEval ``SimpleDiagnostic`` that keeps upstream's ``get_output``
  (the four stages above) and has a reference or comparison source — every
  upstream simple diagnostic and every CB2 one built on them
  (``ScoredAnnualMeanTimeSeries``, ``ETCCDIExtremes``,
  ``AnnualExtremeIndexSeries``, ``PerkinsSkillScore``, ...);
- a CB2 diagnostic that opts in with a ``share_across_members()`` method and
  memoises its own member-invariant part (``ReferenceBaselineRecord``,
  ``ReferenceEOFProjection``, which override ``get_output``).

Anything else — a complex diagnostic, a simple one with its own
``get_output`` — is rebuilt for every member exactly as ``Suite.get_database``
did before, and ``--no-cache`` restores the old member-major loop for the
whole suite.

The upstream seam
-----------------
This lives here, not in ClimateEval, because ClimateEval is pinned and not
patched. It leans on two private details of ``climateeval.suites.Suite``
(``_suite_definition``, the parsed YAML list, and ``_get_diagnostics``) and
on the four ``SimpleDiagnostic`` stage methods above. The upstream change
that would make it unnecessary is small: let ``Suite.get_database`` accept
several ``(data, information)`` pairs (or keep its diagnostics across calls)
and memoise the reference/other stages per diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from climateeval.diags.simple._base import SimpleDiagnostic
from climateeval.suites import Suite
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from climateeval.data import DataSourceInformation
    from climateeval.diags._base import Diagnostic

#: Instance attribute a shared ``SimpleDiagnostic`` keeps its memo under.
_MEMO_ATTRIBUTE = "_cb2_member_invariant_memo"

#: The ``SimpleDiagnostic`` stages that do not see the member.
_STAGES = (
    "_get_reference_cubes",
    "_get_output_of_reference_data",
    "_get_output_of_other_data",
)


class _StageMemo:
    """Memoised member-invariant stages of one ``SimpleDiagnostic`` instance.

    Installed as instance attributes shadowing the class's stage methods, so
    upstream's ``get_output`` calls them unchanged. The first member computes
    each stage; every later one gets the stored result. The comparison stage
    is only reused for the *same* reference-cube dict it was computed from
    (which is what the memoised step 1 hands back), so a caller passing some
    other dict falls through to the real computation.
    """

    def __init__(self, diag: SimpleDiagnostic) -> None:
        self._originals = {name: getattr(diag, name) for name in _STAGES}
        self._reference_cubes: dict[Any, Any] | None = None
        self._reference_output: Any = None
        self._other_output: Any = None
        diag._get_reference_cubes = self.reference_cubes  # type: ignore[method-assign]  # noqa: SLF001
        diag._get_output_of_reference_data = self.reference_output  # type: ignore[method-assign]  # noqa: SLF001
        diag._get_output_of_other_data = self.other_output  # type: ignore[method-assign]  # noqa: SLF001

    def reference_cubes(self) -> dict[Any, Any]:
        """Step 1: the preprocessed reference cubes, loaded once."""
        if self._reference_cubes is None:
            self._reference_cubes = self._originals["_get_reference_cubes"]()
        return self._reference_cubes

    def reference_output(self, reference_cubes: dict[Any, Any]) -> Any:  # noqa: ANN401
        """Step 2: the reference's raw-output rows."""
        if reference_cubes is not self._reference_cubes:
            return self._originals["_get_output_of_reference_data"](reference_cubes)
        if self._reference_output is None:
            self._reference_output = self._originals["_get_output_of_reference_data"](
                reference_cubes,
            )
        return self._reference_output

    def other_output(self, reference_cubes: dict[Any, Any]) -> Any:  # noqa: ANN401
        """Step 4: every comparison source's raw-output rows and metrics."""
        if reference_cubes is not self._reference_cubes:
            return self._originals["_get_output_of_other_data"](reference_cubes)
        if self._other_output is None:
            self._other_output = self._originals["_get_output_of_other_data"](
                reference_cubes,
            )
        return self._other_output

    def clear(self) -> None:
        """Drop everything held (the reference cubes can be gigabytes)."""
        self._reference_cubes = None
        self._reference_output = None
        self._other_output = None


def share_member_invariant_stages(diag: Diagnostic) -> bool:
    """Make ``diag`` compute its member-invariant part once; ``False`` if it can't.

    See the module docstring for which diagnostics qualify. Returns whether
    the diagnostic will share, so the caller knows whether it may reuse the
    instance across members.
    """
    opt_in = getattr(diag, "share_across_members", None)
    if callable(opt_in):
        return bool(opt_in())
    if not isinstance(diag, SimpleDiagnostic):
        return False
    if type(diag).get_output is not SimpleDiagnostic.get_output:
        return False
    has_sources = bool(diag._reference_data) or any(  # noqa: SLF001
        diag._other_data.values(),  # noqa: SLF001
    )
    if not has_sources:
        return False
    setattr(diag, _MEMO_ATTRIBUTE, _StageMemo(diag))
    return True


def release_member_invariant_stages(diag: Diagnostic) -> None:
    """Drop whatever ``diag`` memoised for the members."""
    memo = getattr(diag, _MEMO_ATTRIBUTE, None)
    if memo is not None:
        memo.clear()
    release = getattr(diag, "release_member_cache", None)
    if callable(release):
        release()


class EntrySuite(Suite):
    """One entry of a suite, whose diagnostic is built once when it can share.

    A ``Suite`` restricted to a single YAML entry, so ClimateEval's own
    ``get_database`` writes its tables exactly as for the whole suite. When
    the entry's diagnostic qualifies (:func:`share_member_invariant_stages`)
    the same instance serves every member; otherwise a fresh one is built
    per call, as before.
    """

    def __init__(
        self,
        parent: Suite,
        entry: dict[str, Any],
        *,
        share: bool = True,
    ) -> None:
        """Wrap entry ``entry`` of ``parent``'s definition."""
        super().__init__(
            parent._suite_definition_file,  # noqa: SLF001
            diagnostic_kwargs=parent._diagnostic_kwargs,  # noqa: SLF001
            variable_kwargs=parent._variable_kwargs,  # noqa: SLF001
        )
        self._suite_definition = [entry]
        self._share = share
        self._shared: dict[str, Diagnostic] | None = None
        #: Whether the entry's diagnostic is shared across members; ``None``
        #: until it has been built.
        self.shared: bool | None = None

    def _get_diagnostics(self) -> dict[str, Diagnostic]:
        if self._shared is not None:
            return self._shared
        diagnostics = super()._get_diagnostics()
        self.shared = self._share and all(
            share_member_invariant_stages(diag) for diag in diagnostics.values()
        )
        if self.shared:
            self._shared = diagnostics
        return diagnostics

    def release(self) -> None:
        """Drop the shared diagnostic and everything it memoised."""
        for diag in (self._shared or {}).values():
            release_member_invariant_stages(diag)
        self._shared = None


@dataclass
class MemberRunReport:
    """What :func:`run_members` did, for the CLI to print."""

    members: int
    shared: list[str] = field(default_factory=list)
    unshared: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One line for stderr."""
        if not self.shared and not self.unshared:
            return f"{self.members} member run(s), member by member (no sharing)"
        line = (
            f"reference + comparison ensemble loaded once for "
            f"{len(self.shared)} of {len(self.shared) + len(self.unshared)} "
            f"diagnostic(s), shared across {self.members} members"
        )
        if self.unshared:
            line += f"; rebuilt per member: {', '.join(self.unshared)}"
        return line


def run_members(
    suite: Suite,
    runs: Sequence[tuple[Any, DataSourceInformation]],
    *,
    database_resource: str,
    share: bool = True,
    select_data: Callable[[Any, dict[str, Any]], Any] | None = None,
) -> MemberRunReport:
    """Write every ``(data, information)`` run of ``suite`` into one database.

    With ``share`` (and more than one run) the suite runs diagnostic-major
    and each shareable diagnostic loads its reference and comparison
    ensemble once (module docstring). Without it — ``--no-cache``, or a
    single run — this is exactly the old loop: one ``Suite.get_database``
    per run, appending after the first.

    ``select_data(data, entry)``, when given, narrows each run's data to
    what one suite entry needs (the submission's ``pr`` at the entry's
    ``frequency`` when its member directory holds ``Amon/pr`` *and*
    ``day/pr``: :func:`climatebench2.data.frequency.select_for_entry`); the
    suite then always runs entry by entry, sharing only as ``share`` says.
    """
    report = MemberRunReport(members=len(runs))
    sharing = share and len(runs) >= 2  # noqa: PLR2004
    if not sharing and select_data is None:
        for index, (data, information) in enumerate(runs):
            suite.get_database(
                data,
                information,
                database_resource=database_resource,
                append=index > 0,
            )
        return report

    first = True
    for entry in suite._suite_definition:  # noqa: SLF001
        entry_suite = EntrySuite(suite, entry, share=sharing)
        try:
            for data, information in runs:
                entry_suite.get_database(
                    data if select_data is None else select_data(data, entry),
                    information,
                    database_resource=database_resource,
                    append=not first,
                )
                first = False
        finally:
            entry_suite.release()
        name = str(entry.get("name", "?"))
        if not sharing:
            continue
        (report.shared if entry_suite.shared else report.unshared).append(name)
        if entry_suite.shared:
            logger.info(
                f"Diagnostic '{name}': reference and comparison sources loaded "
                f"once for {len(runs)} members",
            )
    return report
