"""The protocol plug-in for the CLI, the regate pass and the leaderboard.

A :class:`Protocol` holds everything that makes them *ClimateBench2's* rather
than another benchmark's.

ClimateBench2's tooling is protocol-agnostic apart from a handful of facts: the
suite names and how each is fed (:data:`SUITE_REGISTRY`), where the suite YAMLs
and the gate classes live, which experiment key the submission's own members
fill, the thresholds layer stack, and a few tags. A :class:`Protocol` bundles
them. :data:`CB2_PROTOCOL` is today's behaviour and the default, so nothing
changes unless a caller activates another one (for example an
AMIP-style comparison) with :func:`set_protocol` / :func:`use_protocol`, or by
``climatebench2._cli.main(argv, protocol=...)``.

Like the threshold stack, the active protocol is process-wide state: **nothing
may read it at import or class-definition time**.

This module is import-light on purpose (no ClimateEval, no iris).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from climatebench2._thresholds import BASE_LAYER, set_threshold_layers, threshold_layers


DEFAULT_SUITES: list[str] = [
    "ClimateBench2_TierI",
    "ClimateBench2_TierI_variability",
    "ClimateBench2_TierII_events",
    "ClimateBench2_TierII",
]
@dataclass(frozen=True)
class SuiteSpec:
    """How a CB2 suite is fed by ``climatebench2 score``.

    ``shape``
        ``"experiments"`` — an experiment dict (complex diagnostics), run once
        per model; ``"cubes"`` — a CubeList (simple diagnostics).
    ``source``
        For cube suites: ``"model"`` (the submission, run once per ensemble
        member) or the key of an experiment (``"picontrol"``, ``"amip"``, …;
        the cubes of ``--experiment KEY=DIR``, run once). Any experiment key
        is allowed; ``"picontrol"`` is ClimateBench2's only one.
    ``window``
        For cube suites: ``"tier2"`` (the post-2015 test window),
        ``"historical"`` (``climatebench2._cli.DEFAULT_TIMERANGE``) or
        ``"full"`` (no cut).
        ``--timerange`` overrides the first two but never ``"full"``, which
        is used where a window would destroy the statistic: a control run has
        its own calendar (``TierI_variability``), and the daily suite's
        extremes, PDFs and diurnal climatologies are defined by the paper
        over the **whole historical record** and labelled in-sample.
    ``per_member``
        Whether the suite runs **once per submitted ensemble member**. True
        for the cube suites (each member's series is its own data source, and
        the scoring pass stacks them into one fair-CRPS ensemble) and for
        ``ClimateBench2_TierII_events``, whose aggregated scalars — the
        realized warming level, the Pinatubo anomaly, the hemispheric
        asymmetry — are scored across the members exactly the same way. An
        experiment-based suite that runs per member gets **that member's own
        record** as its submission key (``Protocol.submission_key``,
        ``historical`` for ClimateBench2). False for Tier I and Tier III: a
        gate is a property of the model, not of one member.
    ``reference_overlap``
        Only for ``window == "full"``: whether a variable of this suite that
        *has* a staged observational reference should be cut to that
        reference's own record rather than left on the model's whole record
        (:func:`climatebench2.reference_windows.resolve_full_record_timerange`).
        True for the daily suite, whose in-sample statistics are climatologies
        — an Rx1day climatology over a model's 1850–2100 and one over IMERG's
        2001–2025 are different statistics, and an in-sample one must not
        reach into the reserved window, so those entries are cut to the
        reference's own pre-2015 record. The one entry scored as anomalies
        (``pr_extremes_series``, ``tier2.anomaly_baseline``) is cut instead to
        the baseline start through the reference's own last complete year —
        2001–2024 for IMERG — because it needs the post-2015 steps too, but
        it is still nowhere near the model's whole 1850–2100 record: reading
        that full record for every comparison-ensemble member is what timed
        out the 12 h daily job (2026-09-26). False for
        ``ClimateBench2_TierI_variability``, whose ``full`` window means "this
        is a piControl with its own calendar", where any clip is wrong.
    ``historical_pre_test``
        Only for a once-per-model experiment suite: its ``historical`` input
        ends at :func:`climatebench2.windows.pre_test_last_year` (2014), the
        year before the reserved test window. True for Tier I (ratified by
        Duncan, 2026-11-02): a submission's ``historical`` is historical +
        SSP2-4.5 spliced to 2100, and the I.8b ITCZ–EFE climatology, which
        reads the whole record, would otherwise average in the scenario and
        the held-out years. The per-member events suite keeps each member's
        full record (the realized warming level *is* a test-window statistic).
    """

    shape: str
    source: str = "model"
    window: str = "historical"
    per_member: bool = False
    reference_overlap: bool = False
    historical_pre_test: bool = False
    note: str = ""


#: Data shape and default window of every CB2 suite. Suites not listed (a
#: ClimateEval suite, a user's YAML) are classified by probing their
#: diagnostics and get the historical window.
SUITE_REGISTRY: dict[str, SuiteSpec] = {
    "ClimateBench2_TierI": SuiteSpec(
        shape="experiments",
        historical_pre_test=True,
        note="Tier I gates: one experiment dict, historical up to 2014",
    ),
    "ClimateBench2_TierI_variability": SuiteSpec(
        shape="cubes",
        source="picontrol",
        window="full",
        note="ENSO amplitude/spectrum: >= 100 yr of piControl (paper I.5)",
    ),
    "ClimateBench2_TierII": SuiteSpec(
        shape="cubes",
        window="tier2",
        per_member=True,
        note="Tier II scoring over the reserved post-2015 test window",
    ),
    "ClimateBench2_TierII_daily": SuiteSpec(
        shape="cubes",
        window="full",
        per_member=True,
        reference_overlap=True,
        note=(
            "Tier II daily/hourly statistics over the FULL historical record "
            "(the paper computes the extremes and the PDF/diurnal "
            "climatologies over it, not over the test window) — every entry "
            "is in-sample except `pr_extremes_series`, whose annual Rx1day/"
            "Rx5day series is scored held-out over the test window. A variable "
            "with a STAGED reference is cut to that reference's own record "
            "instead: pre-2015 for the in-sample entries (IMERG: 2001–2014), "
            "or the baseline start through the reference's own last complete "
            "year for `pr_extremes_series` (IMERG: 2001–2024, since it also "
            "scores the post-2015 steps) — so the model and the observations "
            "give the same statistic, without reading the model's whole "
            "1850–2100 record for every comparison-ensemble member"
        ),
    ),
    "ClimateBench2_TierII_events": SuiteSpec(
        shape="experiments",
        per_member=True,
        note=(
            "Warming level / Pinatubo / hemispheric asymmetry: each member's "
            "own historical record, in full"
        ),
    ),
    "ClimateBench2_TierIII": SuiteSpec(
        shape="experiments",
        note="Paleo time slices vs piControl",
    ),
}


def _default_experiment_frequency() -> dict[str, str]:
    return {"day": "day"}


@dataclass(frozen=True)
class Protocol:
    """Everything protocol-specific the shared tooling needs to know.

    ``name``
        CLI ``prog``, the suffix of the default output directory
        (``<model name>_<protocol name>/``) and the default leaderboard file name.
    ``threshold_layers``
        The :mod:`climatebench2._thresholds` layer stack the protocol runs with
        (``(BASE_LAYER,)`` for ClimateBench2; a sibling's is
        ``(BASE_LAYER, ("my_protocol", "thresholds.yml"))``).
    ``suites_packages``
        Packages holding the suite YAMLs, searched **in order** (first hit wins)
        by suite name; a protocol that extends ClimateBench2 lists its own
        package first.
    ``diags_modules``
        Modules whose ``__all__`` names the gate classes: the union of their
        ``_gate_checks`` is the protocol's gate set (the leaderboard's Required
        set, ``regate``).
    ``suite_registry`` / ``default_suites``
        How each suite is fed (:class:`SuiteSpec`) and which suites run by
        default.
    ``submission_key``
        The experiment key the submission's own members fill: the first member
        becomes ``experiments[submission_key]``, and a per-member experiment
        suite hands each member its own record under that key.
    ``member_keys``
        Further experiment keys that read **each member's own record** — the
        same output at another frequency (e.g. a daily ``amip_day``). A
        per-member experiment suite substitutes them per member, as it does
        ``submission_key``, unless ``--experiment KEY=DIR`` pinned one.
    ``pre_test_keys``
        Experiment keys cut to end the year before the reserved test window for
        a suite with ``SuiteSpec.historical_pre_test``.
    ``experiment_frequency``
        ``experiment key -> frequency`` the experiment suites read it at
        (default ``"mon"``); see :mod:`climatebench2.data.frequency`.
    ``scorer``
        The ``scorer`` tag of the Tier II rows the scoring pass writes (Tier III
        paleo rows carry ``<scorer>.tier3``).
    ``staged_root_env``
        Environment variable naming the staged comparison-ensemble root.

    Fields added beyond the CB-3 sketch (all optional): ``display_name`` (the
    leaderboard's title), ``description`` and ``epilog`` (CLI help; ``None`` is
    ClimateBench2's text).
    """

    name: str
    threshold_layers: tuple[Any, ...]
    suites_packages: tuple[str, ...]
    diags_modules: tuple[str, ...]
    suite_registry: Mapping[str, SuiteSpec]
    default_suites: tuple[str, ...]
    submission_key: str = "historical"
    member_keys: tuple[str, ...] = ()
    pre_test_keys: tuple[str, ...] = ("historical",)
    experiment_frequency: Mapping[str, str] = field(
        default_factory=_default_experiment_frequency,
    )
    scorer: str = "climatebench2"
    staged_root_env: str = "CLIMATEBENCH2_STAGED_CMIP6_ROOT"
    display_name: str = "ClimateBench v2"
    description: str | None = None
    epilog: str | None = None

    @property
    def tier3_scorer(self) -> str:
        """``scorer`` of the Tier III paleo rows (``climatebench2.tier3``)."""
        return f"{self.scorer}.tier3"


#: ClimateBench2 itself: the default, and the behaviour before the plug-in existed.
CB2_PROTOCOL = Protocol(
    name="climatebench2",
    threshold_layers=(BASE_LAYER,),
    suites_packages=("climatebench2.suites",),
    diags_modules=("climatebench2.diags",),
    suite_registry=SUITE_REGISTRY,
    default_suites=tuple(DEFAULT_SUITES),
)

_active: Protocol = CB2_PROTOCOL


def get_protocol() -> Protocol:
    """The active protocol (:data:`CB2_PROTOCOL` unless one was set)."""
    return _active


def set_protocol(protocol: Protocol) -> None:
    """Make ``protocol`` active, and its threshold layers with it."""
    global _active  # noqa: PLW0603 - process-wide protocol state
    set_threshold_layers(protocol.threshold_layers)
    _active = protocol


@contextmanager
def use_protocol(protocol: Protocol) -> Iterator[Protocol]:
    """Scope a protocol (and its threshold layers); restore both on exit."""
    global _active  # noqa: PLW0603 - process-wide protocol state
    saved_protocol = _active
    saved_layers = threshold_layers()
    set_protocol(protocol)
    try:
        yield protocol
    finally:
        set_threshold_layers(saved_layers)
        _active = saved_protocol
