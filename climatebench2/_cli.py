"""Command-line interface for ClimateBench2.

Installed as the ``climatebench2`` console script (see ``[project.scripts]``
in ``pyproject.toml``); also runnable as ``python -m climatebench2._cli``.

Subcommands
-----------
``climatebench2 score MODEL``
    Run the ClimateBench2 evaluation suites on a model's CMOR output via
    ClimateEval and write one DuckDB results database per suite (default:
    the CB2 tier suites, which grow per the delineation plan §6), then run
    the Tier II scoring pass over them (``--no-score`` skips it).
``climatebench2 freeze-comparison DB [DB ...] -o DIR``
    Copy the CMIP6 comparison ensemble's rows out of finished result
    databases into a frozen bundle (:mod:`climatebench2.frozen_comparison`),
    so that ``score --comparison frozen --frozen-comparison DIR`` can score a
    new submission without the comparison ensemble on disk;
    ``merge-comparison DIR DB ...`` merges a bundle into existing databases.
``climatebench2 leaderboard DB [DB ...]``
    Build the ClimateBench2 leaderboard from result databases;
    ``--rescore`` re-runs the Tier II scoring pass over them first, and
    ``--regate`` re-evaluates every Tier I/III gate's pass/fail against the
    current ``thresholds.yml`` first (:mod:`climatebench2.regate`) — cheap,
    unlike re-running the suite that computed the raw statistic.

The Tier II scores are a **post-processing pass**
(:mod:`climatebench2.scoring_pass`), not a diagnostic: ensemble members are
ingested as separate data sources, so a model's fair CRPS can only be formed
once every member has run. The pass is idempotent — it replaces the rows it
wrote before — so it is safe to re-run at any time. The gate re-evaluation
pass (:mod:`climatebench2.regate`) is likewise idempotent but does not touch
Tier II/III scorer rows or declared-not-applicable gate rows.

ClimateBench2 deliberately has no data-loading or report machinery of its
own — ``score`` delegates to ClimateEval (``load_cmor_dir`` + ``Suite``), and
per-model interactive reports remain available via ``climateeval report``.

Each suite is fed the data *shape* and time window the protocol asks for
(:data:`SUITE_REGISTRY`), not one global slice of the submission:

- the Tier I / Tier II-events / Tier III suites take an **experiment dict**,
  whose ``historical`` entry is the model's own output loaded in **full** —
  the aerosol-era and Pinatubo diagnostics live in 1950–1993 and the
  clear-sky, ITCZ–EFE and hemispheric checks want the whole record. Tier I
  and Tier III run **once per model**; ``ClimateBench2_TierII_events`` runs
  **once per ensemble member** (``SuiteSpec.per_member``), each member
  supplying its own ``historical`` record, because its Tier II scalars —
  realized warming level, Pinatubo anomaly, hemispheric asymmetry — are
  scored across the ensemble by the fair-CRPS pass;
- ``ClimateBench2_TierI_variability`` (ENSO amplitude/spectrum) takes the
  **piControl** experiment in full — the paper asks for ≥ 100 yr of control;
- the Tier II suites take the model's cubes cut to the **post-2015 test
  window** (``tier2.test_window_start`` → ``<start>0101/<last complete
  year>1231``), which ``--timerange`` overrides explicitly. The monthly
  Tier II suite is then windowed **per suite entry**
  (:func:`materialise_windowed_suite`): its regime-(a) time-series entries
  reach back to the **baseline window's first year** (1985), because they
  are scored on anomalies about each source's own 1985–2014 climatology,
  while the EOF basis, the maps, the annual cycles and the zonal lines keep
  the test window. The submission's cubes are therefore loaded from 1985
  too; every diagnostic re-clips them to its own variable's ``timerange``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from climatebench2 import windows

# Heavy climateeval subsystems (ESMValCore/iris/dask, ~5 s to import) are
# imported lazily inside the command functions, mirroring climateeval's CLI.

DEFAULT_SUITES = [
    "ClimateBench2_TierI",
    "ClimateBench2_TierI_variability",
    "ClimateBench2_TierII_events",
    "ClimateBench2_TierII",
]
#: In-sample historical window: the default for cube suites that are not the
#: post-2015 test window. It is also the window upstream's single-member
#: `climateeval.data.CMIP6HistoricalR1I1P1F1` carries; the Tier II comparison
#: ensemble is `climatebench2.data.StagedCMIP6HistoricalSSP245` and runs to the
#: end of each member's SSP2-4.5 record instead.
DEFAULT_TIMERANGE = "19790101/20141231"


@dataclass(frozen=True)
class SuiteSpec:
    """How a CB2 suite is fed by ``climatebench2 score``.

    ``shape``
        ``"experiments"`` — an experiment dict (complex diagnostics), run once
        per model; ``"cubes"`` — a CubeList (simple diagnostics).
    ``source``
        For cube suites: ``"model"`` (the submission, run once per ensemble
        member) or ``"picontrol"`` (the piControl experiment, run once).
    ``window``
        For cube suites: ``"tier2"`` (the post-2015 test window),
        ``"historical"`` (:data:`DEFAULT_TIMERANGE`) or ``"full"`` (no cut).
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
        record** as its ``historical`` key. False for Tier I and Tier III: a
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
    """

    shape: str
    source: str = "model"
    window: str = "historical"
    per_member: bool = False
    reference_overlap: bool = False
    note: str = ""


#: Data shape and default window of every CB2 suite. Suites not listed (a
#: ClimateEval suite, a user's YAML) are classified by probing their
#: diagnostics and get the historical window.
SUITE_REGISTRY: dict[str, SuiteSpec] = {
    "ClimateBench2_TierI": SuiteSpec(
        shape="experiments",
        note="Tier I gates: one experiment dict, historical in full",
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


def default_tier2_timerange(today: dt.date | None = None) -> str:
    """Default Tier II window: ``tier2.test_window_start`` → last complete year.

    The protocol scores whole years from the start of the reserved test
    window to the last year that has finished, so the window grows by one
    year every January without a code change. Resolved by
    :mod:`climatebench2.windows`, which the reference-window diagnostics and
    the scoring pass share, so no two of them can disagree about the window.
    """
    return windows.test_window_timerange(today)


def suite_timerange(
    spec: SuiteSpec,
    explicit: str | None = None,
    tier2_window: str | None = None,
) -> str | None:
    """Resolve the ISO timerange for one suite (``None`` = no cut).

    ``explicit`` (the user's ``--timerange``) overrides the cube suites'
    default windows, but never the piControl-sourced ``full`` window: a
    control run has its own calendar, so an instrumental-era range would
    only throw its data away. ``tier2_window`` replaces today's protocol
    default for the Tier II window only — a frozen comparison bundle's
    window, which must not move with the date.
    """
    if spec.shape != "cubes" or spec.window == "full":
        return None
    if explicit is not None:
        return explicit
    if spec.window == "tier2":
        return tier2_window or default_tier2_timerange()
    return DEFAULT_TIMERANGE


#: CMIP6 DRS ensemble-member directory, e.g. ``r1i1p1f1``.
_VARIANT_RE = re.compile(r"^r\d+i\d+p\d+f\d+$")


def discover_members(root: Path, *, max_depth: int = 3) -> dict[str, Path]:
    """Find sibling ``r*i*p*f*`` variant directories in a DRS tree.

    Returns ``{}`` unless at least two variant directories share a parent, so
    a single-member tree (or a flat directory) keeps today's behaviour. Only
    the parent with the most variants is used — variant names repeat across
    experiments in a DRS tree, and merging those would mix experiments.
    """
    if not root.is_dir():
        return {}
    by_parent: dict[Path, dict[str, Path]] = {}
    pattern = "*"
    for _depth in range(max_depth):
        for path in sorted(root.glob(pattern)):
            if path.is_dir() and _VARIANT_RE.match(path.name):
                by_parent.setdefault(path.parent, {})[path.name] = path
        pattern += "/*"
    if not by_parent:
        return {}
    _parent, members = max(
        by_parent.items(),
        key=lambda item: (len(item[1]), str(item[0])),
    )
    return dict(sorted(members.items())) if len(members) > 1 else {}


def _resolve_suite(name: str) -> str:
    """Resolve a suite name to a CB2-packaged YAML if one exists.

    CB2 suites shadow ClimateEval ones of the same name; anything else
    (shipped ClimateEval suite names, explicit paths) passes through for
    ClimateEval's own resolution.
    """
    if Path(name).suffix in {".yml", ".yaml"}:
        return name
    cb2_suite = resources.files("climatebench2.suites").joinpath(f"{name}.yml")
    if cb2_suite.is_file():
        return str(cb2_suite)
    return name


def substitute_reference(node: Any, reference: str, others: list[str]) -> Any:  # noqa: ANN401
    """Replace every ``reference_data``/``other_data`` in a suite definition.

    The perfect-model test (metrics_reference.md §III.2) runs the **ordinary**
    Tier II diagnostics with a held-out ESM run in place of the observations,
    so nothing about the diagnostics changes — only which DataSource class
    their variables name. Walking the parsed YAML and swapping those two keys
    is the whole substitution; the result is written to a temporary suite
    file with the *same stem*, so the suite keeps its name and its database
    filename.

    ``other_data`` becomes the truth model's remaining ensemble members (or
    is dropped): the CMIP6 comparison models belong to the observational
    scorecard, and leaving them in would make ``E_ref`` a comparison against
    the real world inside a perfect-model experiment.
    """
    if isinstance(node, list):
        return [substitute_reference(item, reference, others) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "reference_data":
            out[key] = reference
        elif key == "other_data":
            if others:
                out[key] = list(others)
        else:
            out[key] = substitute_reference(value, reference, others)
    if "reference_data" in node and others and "other_data" not in out:
        out["other_data"] = list(others)
    return out


def materialise_windowed_suite(
    resolved: str,
    out_dir: Path,
    nominal: str | None,
    data_root: Path | None,
) -> tuple[str, dict[str, tuple[str, str]]]:
    """Write a copy of a suite YAML with a per-entry, per-variable window.

    ``nominal`` is the suite's protocol window, or ``None`` for a suite that
    has none (``SuiteSpec.window == "full"``, i.e. the daily suite). With
    ``None`` only a variable with a *staged* reference gets a window at all —
    that reference's own pre-2015 record — and everything else keeps the
    model's whole record.

    A single global ``variable_kwargs['timerange']`` cannot express what the
    Tier II suite needs, for two reasons:

    * The protocol's test window runs to the last complete year, but no
      observational product is that current, and ClimateEval *drops* a
      reference whose record stops short of the requested range rather than
      clipping it (:mod:`climatebench2.reference_windows`) — which silently
      deletes the variable's entire row from the scorecard.
    * Regime (a) is scored on **anomalies about each source's own 1985–2014
      climatology**, so the scored time-series entries must load from 1985
      while the EOF basis, the maps, the annual cycles and the zonal lines
      must keep the test window — their statistic *is* the test-window field.

    Writing the resolved window into each variable stanza, and building the
    suite with no global ``variable_kwargs['timerange']``, is what expresses
    both. Returns the written path and
    ``"<entry>/<variable>" -> (window used, window asked for)``.
    """
    import yaml

    from climatebench2 import reference_windows

    definition = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    windows_used: dict[str, tuple[str, str]] = {}
    windowed = reference_windows.apply_reference_windows(
        definition,
        nominal,
        data_root,
        resolved=windows_used,
    )
    target = out_dir / f"{Path(resolved).stem}.yml"
    target.write_text(yaml.safe_dump(windowed, sort_keys=False), encoding="utf-8")
    return str(target), windows_used


def materialise_truth_suite(
    resolved: str,
    out_dir: Path,
    reference: str,
    others: list[str],
) -> str:
    """Write a copy of a suite YAML whose references are the truth run."""
    import yaml

    definition = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    swapped = substitute_reference(definition, reference, others)
    target = out_dir / f"{Path(resolved).stem}.yml"
    target.write_text(yaml.safe_dump(swapped, sort_keys=False), encoding="utf-8")
    return str(target)


def materialise_stripped_suite(
    resolved: str,
    out_dir: Path,
) -> tuple[str, dict[str, list[str]]]:
    """Write a copy of a suite YAML without its comparison-ensemble generators.

    ``score --comparison frozen|none``: every ``other_data`` entry that is a
    generator (the CMIP6 comparison ensemble) is dropped, observational
    products and ``reference_data`` are kept
    (:func:`climatebench2.frozen_comparison.strip_comparison_ensemble`). Same
    stem, so the suite keeps its name and its database filename. Returns the
    path and ``entry -> [variable id]`` of what was stripped.
    """
    import yaml

    from climatebench2.frozen_comparison import strip_comparison_ensemble

    from climateeval.suites._base import resolve_suite_definition_file

    source = resolve_suite_definition_file(resolved)
    definition = yaml.safe_load(source.read_text(encoding="utf-8"))
    stripped_definition, stripped = strip_comparison_ensemble(definition)
    target = out_dir / f"{source.stem}.yml"
    target.write_text(
        yaml.safe_dump(stripped_definition, sort_keys=False),
        encoding="utf-8",
    )
    return str(target), stripped


def _parse_notes(specs: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--note KEY=VALUE`` options."""
    notes: dict[str, str] = {}
    for spec in specs or []:
        key, sep, value = spec.partition("=")
        if not sep or not key:
            msg = f"--note expects KEY=VALUE, got '{spec}'"
            raise SystemExit(msg)
        notes[key.strip()] = value
    return notes


def _parse_experiments(specs: list[str] | None) -> dict[str, Path]:
    """Parse repeated ``--experiment KEY=PATH`` options."""
    experiments: dict[str, Path] = {}
    for spec in specs or []:
        key, sep, path = spec.partition("=")
        if not sep or not key or not path:
            msg = f"--experiment expects KEY=PATH, got '{spec}'"
            raise SystemExit(msg)
        experiments[key.strip().lower()] = Path(path)
    return experiments


def _parse_members(specs: list[str] | None) -> dict[str, Path]:
    """Parse repeated ``--member LABEL=PATH`` options."""
    members: dict[str, Path] = {}
    for spec in specs or []:
        label, sep, path = spec.partition("=")
        if not sep or not label or not path:
            msg = f"--member expects LABEL=PATH, got '{spec}'"
            raise SystemExit(msg)
        members[label.strip()] = Path(path)
    return members


def _resolve_members(args: argparse.Namespace) -> dict[str, Path]:
    """Ensemble members of the submission: explicit, discovered or single."""
    members = _parse_members(args.member)
    for label, path in members.items():
        if not path.exists():
            msg = f"Member path not found: {label}={path}"
            raise SystemExit(msg)
    if members:
        return members
    discovered = discover_members(args.model)
    if discovered:
        print(
            f"Discovered {len(discovered)} ensemble members under {args.model}: "
            f"{', '.join(discovered)}",
            file=sys.stderr,
        )
        return discovered
    return {args.variant: args.model}


def _suite_spec(
    stem: str,
    diagnostics: dict[str, Any],
    complex_type: type,
) -> SuiteSpec:
    """Registry entry for a suite, or one inferred from its diagnostics."""
    if stem in SUITE_REGISTRY:
        return SUITE_REGISTRY[stem]
    shape = (
        "experiments"
        if any(isinstance(diag, complex_type) for diag in diagnostics.values())
        else "cubes"
    )
    # An unregistered cube suite keeps the old behaviour (one run per member);
    # an unregistered experiment suite is assumed to be a gate suite.
    return SuiteSpec(
        shape=shape,
        per_member=shape == "cubes",
        note="not in the CB2 suite registry",
    )


#: ``score --comparison`` modes.
COMPARISON_MODES = ("live", "frozen", "none")


@dataclass
class ComparisonSetup:
    """Where the CMIP6 comparison ensemble of a ``score`` run comes from.

    ``live`` runs it (the staged pool, or upstream's pool glob for the Tier I
    wrappers); ``frozen`` strips it from the suites and merges a
    :mod:`climatebench2.frozen_comparison` bundle into the databases before
    the scoring pass; ``none`` strips it and merges nothing, so ``E_ref`` and
    the skill stay empty.
    """

    mode: str = "live"
    bundle: Path | None = None
    manifest: dict[str, Any] | None = None
    #: The bundle's nominal Tier II window, used when --timerange is absent.
    tier2_window: str | None = None
    isolate_crashes: bool = False


def comparison_setup(args: argparse.Namespace) -> ComparisonSetup:
    """Validate ``--comparison``/``--frozen-comparison`` before anything runs.

    A frozen bundle's provenance is checked here, so a mismatch fails in
    seconds rather than after the suites (the per-variable windows are
    checked per suite before it runs, the coverage after it, see
    :func:`_run_score`).
    """
    mode = getattr(args, "comparison", "live") or "live"
    bundle = getattr(args, "frozen_comparison", None)
    isolate = getattr(args, "isolate_crashes", None)
    setup = ComparisonSetup(
        mode=mode,
        isolate_crashes=(mode != "live") if isolate is None else bool(isolate),
    )
    if mode != "frozen":
        if bundle is not None:
            msg = "--frozen-comparison needs --comparison frozen"
            raise SystemExit(msg)
        return setup
    if bundle is None:
        msg = "--comparison frozen needs --frozen-comparison DIR"
        raise SystemExit(msg)
    if getattr(args, "truth", None) is not None:
        msg = (
            "--comparison frozen cannot be combined with --truth: a "
            "perfect-model run has no CMIP6 comparison ensemble (use "
            "--comparison none)"
        )
        raise SystemExit(msg)
    from climatebench2.frozen_comparison import (
        FrozenComparisonError,
        check_provenance,
        load_manifest,
    )

    try:
        manifest = load_manifest(bundle)
        window = args.timerange or manifest.get("nominal_timerange")
        notes = check_provenance(manifest, timerange=window)
    except FrozenComparisonError as exc:
        raise SystemExit(str(exc)) from exc
    for note in notes:
        print(f"Note: frozen comparison bundle: {note}", file=sys.stderr)
    setup.bundle = Path(bundle)
    setup.manifest = manifest
    setup.tier2_window = manifest.get("nominal_timerange")
    return setup


def _check_frozen_windows(
    stem: str,
    stripped: dict[str, list[str]],
    windows_used: dict[str, tuple[str, str]],
    manifest: dict[str, Any],
) -> None:
    """Compare each stripped variable's resolved window with the bundle's.

    Runs before the suite does: the resolved window depends on the date and
    on how far each staged reference reaches, and comparison rows over a
    different window would make ``E_ref`` meaningless.
    """
    entries = manifest.get("databases", {}).get(stem, {}).get("entries", {})
    mismatches: list[str] = []
    absent: list[str] = []
    for entry, variables in stripped.items():
        for var_id in variables:
            frozen = entries.get(entry, {}).get(var_id)
            if frozen is None:
                absent.append(f"{entry}/{var_id}")
                continue
            used = windows_used.get(f"{entry}/{var_id}")
            if used is not None and frozen["timerange"] and (
                used[0] not in frozen["timerange"]
            ):
                mismatches.append(
                    f"{entry}/{var_id}: run {used[0]}, bundle "
                    f"{', '.join(frozen['timerange'])}",
                )
    if mismatches:
        msg = (
            f"Suite '{stem}': the frozen comparison rows were produced over "
            f"different windows than this run resolves (a different "
            f"--timerange, or a staged reference that now reaches further): "
            + "; ".join(mismatches)
        )
        raise SystemExit(msg)
    if absent:
        print(
            f"Note: suite '{stem}': the frozen bundle has no comparison rows "
            f"for {', '.join(absent)}; that is fatal after the run for any of "
            f"them the scoring pass scores (unless --allow-missing-comparison), "
            f"and harmless for a deterministic entry (map, annual cycle, "
            f"baseline record)",
            file=sys.stderr,
        )


def merge_frozen_comparison(
    db_paths: list[Path],
    setup: ComparisonSetup,
    stripped_by_stem: dict[str, dict[str, list[str]]],
    *,
    allow_missing: bool,
) -> None:
    """Merge the bundle into each database before the scoring pass."""
    from climatebench2.frozen_comparison import FrozenComparisonError, merge

    assert setup.bundle is not None  # noqa: S101 - comparison_setup guarantees it
    print(
        f"\nMerging the frozen comparison ensemble from {setup.bundle}…",
        file=sys.stderr,
    )
    for path in db_paths:
        try:
            report = merge(
                path,
                setup.bundle,
                stripped=stripped_by_stem.get(path.stem, {}),
                allow_missing=allow_missing,
            )
        except FrozenComparisonError as exc:
            msg = (
                f"{exc}\nThe suite databases are written; after fixing the "
                f"bundle, `climatebench2 merge-comparison BUNDLE DB` then "
                f"`climatebench2 leaderboard --rescore DB` finish the run."
            )
            raise SystemExit(msg) from exc
        print(f"  {report.summary()}", file=sys.stderr)
        for message in report.messages:
            print(f"    {message}", file=sys.stderr)


def _cmd_score(args: argparse.Namespace) -> None:
    from climatebench2.diags.pass_fail import gate_switches

    setup = comparison_setup(args)
    with gate_switches(
        comparison_ensemble=setup.mode == "live",
        isolate_crashes=setup.isolate_crashes,
    ):
        _run_score(args, setup)


def _run_score(args: argparse.Namespace, setup: ComparisonSetup) -> None:  # noqa: C901, PLR0912, PLR0915
    if not args.model.exists():
        msg = f"Model path not found: {args.model}"
        raise SystemExit(msg)
    experiment_paths = _parse_experiments(args.experiment)
    for key, path in experiment_paths.items():
        if not path.exists():
            msg = f"Experiment path not found: {key}={path}"
            raise SystemExit(msg)
    members = _resolve_members(args)

    print("Loading ClimateEval (this can take a few seconds)…", file=sys.stderr)
    from climatebench2.data import STAGED_ROOT_ENV, staged_root
    from climateeval._loader import load_cmor_dir
    from climateeval.data import DataSourceInformation
    from climateeval.diags.complex._base import ComplexDiagnostic
    from climateeval.suites import Suite

    cache: dict[tuple[str, str | None], Any] = {}

    def load(path: Path, timerange: str | None) -> Any:  # noqa: ANN401
        """Load a CMOR directory once per (path, window)."""
        key = (str(path), timerange)
        if key not in cache:
            cubes = load_cmor_dir(path, timerange=timerange)
            if not cubes:
                window = f" within {timerange}" if timerange else ""
                msg = f"No NetCDF data found in {path}{window}"
                raise SystemExit(msg)
            cache[key] = cubes
        return cache[key]

    # Experiment dict for the complex suites. Every experiment is loaded in
    # FULL (no timerange cut): piControl/abrupt-4xCO2 need their length, and
    # the historical gates run outside the Tier II test window (the aerosol
    # era, Pinatubo, the clear-sky and ITCZ-EFE records). The first member's
    # own output serves as "historical" unless --experiment historical=DIR
    # overrides it — Tier I is evaluated once per model, not per member.
    first_label, first_path = next(iter(members.items()))
    experiments: dict[str, Any] = {"historical": load(first_path, None)}
    explicit_historical = "historical" in experiment_paths
    for key, path in experiment_paths.items():
        experiments[key] = load(path, None)
        print(f"Loaded experiment '{key}' from {path} (full record)", file=sys.stderr)

    def info_for(label: str) -> DataSourceInformation:
        """Provenance for one member (the variant separates their data ids)."""
        return DataSourceInformation(
            name=args.name,
            category="model",
            institute=args.institute,
            exp=args.exp,
            variant=label or args.variant,
        )

    diagnostic_kwargs: dict[str, Any] = {
        "fail_on_missing_data": False,
        "fail_on_metric_error": False,
        "download_missing_data": args.download,
    }
    if args.data_root is not None:
        diagnostic_kwargs["data_root_dir"] = args.data_root

    # ClimateEval builds an `other_data` generator with no arguments and calls
    # `generate(variable)` inside `SimpleDiagnostic.__init__`, before the
    # diagnostic's data root is set, so the staged comparison ensemble can only
    # be told where to look through the environment. Default it to --data-root;
    # an explicit $CLIMATEBENCH2_STAGED_CMIP6_ROOT (e.g. a small subset for a
    # smoke run) always wins.
    if args.data_root is not None and not os.environ.get(STAGED_ROOT_ENV):
        os.environ[STAGED_ROOT_ENV] = str(args.data_root)

    out_dir = args.out or Path(f"{args.name}_climatebench2")
    out_dir.mkdir(parents=True, exist_ok=True)

    not_applicable = [name.strip() for name in (args.not_applicable or [])]

    # Perfect model (III.2): the cube suites score against a held-out ESM run
    # instead of observations. Registering the directories here, before any
    # Suite is built, is what lets a suite YAML name the DataSource class.
    truth_reference: str | None = None
    truth_others: list[str] = []
    truth_suite_dir: Path | None = None
    if args.truth is not None:
        from climatebench2.diags.truth_reference import configure_truth

        if not args.truth.exists():
            msg = f"Truth path not found: {args.truth}"
            raise SystemExit(msg)
        truth_members = _parse_members(args.truth_member)
        for label, path in truth_members.items():
            if not path.exists():
                msg = f"Truth member path not found: {label}={path}"
                raise SystemExit(msg)
        truth_reference, truth_others = configure_truth(
            args.truth,
            truth_members,
            name=args.truth_name,
        )
        truth_suite_dir = out_dir / "_perfect_model_suites"
        truth_suite_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"Perfect-model run: scoring against the held-out truth "
            f"'{args.truth_name}' at {args.truth}"
            + (
                f" with {len(truth_members)} further truth member(s) "
                f"({', '.join(truth_members)}) for the large-ensemble spread "
                f"test"
                if truth_members
                else " (no --truth-member given: no spread test)"
            )
            + ". Every scored row is labelled window = perfect-model, and the "
            "CMIP6 comparison models are dropped — E_ref against the real "
            "world is meaningless here.",
            file=sys.stderr,
        )

    def build_suite(
        resolved: str,
        extra: dict[str, Any],
        timerange: str | None,
    ) -> Any:  # noqa: ANN401
        """A Suite with the CLI's kwargs and this suite's own time window."""
        return Suite(
            resolved,
            diagnostic_kwargs={**diagnostic_kwargs, **extra},
            variable_kwargs={} if timerange is None else {"timerange": timerange},
        )

    if setup.mode != "live":
        print(
            f"Comparison ensemble: {setup.mode} — the CMIP6 comparison "
            f"generators are stripped from every suite's other_data "
            f"(observational products kept) and the Tier I gate wrappers skip "
            f"upstream's ensemble"
            + (
                f"; the frozen rows of {setup.bundle} are merged in before the "
                f"scoring pass"
                if setup.mode == "frozen"
                else "; E_ref and the skill stay EMPTY"
            )
            + (
                f". Tier II window pinned to the bundle's {setup.tier2_window}"
                if setup.mode == "frozen" and args.timerange is None
                else ""
            )
            + f". Gate crash isolation: {'on' if setup.isolate_crashes else 'off'}.",
            file=sys.stderr,
        )

    suite_names = args.suite or DEFAULT_SUITES
    db_paths: list[Path] = []
    declared_seen: set[str] = set()
    stripped_by_stem: dict[str, dict[str, list[str]]] = {}
    for suite_name in suite_names:
        resolved = _resolve_suite(suite_name)
        stem = Path(resolved).stem

        # Before any diagnostic is built: building one instantiates its
        # other_data generators, which is what a frozen/none run avoids.
        stripped: dict[str, list[str]] = {}
        if setup.mode != "live":
            stripped_dir = out_dir / "_comparison_stripped_suites"
            stripped_dir.mkdir(parents=True, exist_ok=True)
            resolved, stripped = materialise_stripped_suite(resolved, stripped_dir)
            if stripped:
                stripped_by_stem[stem] = stripped

        diagnostics = build_suite(resolved, {}, None)._get_diagnostics()
        spec = _suite_spec(stem, diagnostics, ComplexDiagnostic)
        timerange = suite_timerange(spec, args.timerange, setup.tier2_window)

        # The truth run replaces the observations only where there ARE
        # observations to replace: the cube suites fed by the submission. The
        # piControl-sourced variability suite and the experiment suites (Tier
        # I gates, Tier III paleo) are unaffected by a perfect-model run.
        if (
            truth_reference is not None
            and truth_suite_dir is not None
            and spec.shape == "cubes"
            and spec.source == "model"
        ):
            resolved = materialise_truth_suite(
                resolved,
                truth_suite_dir,
                truth_reference,
                truth_others,
            )

        # `not_applicable` is a complex-diagnostic kwarg (declared N/A gates);
        # ClimateEval's simple diagnostics take no **kwargs, so it is only
        # handed to the experiment-based suites.
        extra_kwargs: dict[str, Any] = {}
        if spec.shape == "experiments" and not_applicable:
            declared_seen |= set(diagnostics) & set(not_applicable)
            extra_kwargs["not_applicable"] = not_applicable
        if spec.shape == "experiments" and args.paleo_data_root is not None:
            # Only the Tier III diagnostics read it; every other complex
            # diagnostic swallows unknown kwargs (`ComplexDiagnostic._kwargs`).
            extra_kwargs["paleo_data_root"] = str(args.paleo_data_root)

        # Per-ENTRY, per-variable scored window, carried in the suite itself
        # rather than as one global `variable_kwargs['timerange']`:
        #   * a reference that stops short of the requested range is DROPPED
        #     by ClimateEval, not clipped, so a window running to the last
        #     complete year would delete `tas` (HadCRUT5 ends 2023-09),
        #     `siconc` (HadISST 2021-12) and the TOA fluxes (CERES-EBAF
        #     2025-09) from the scorecard entirely;
        #   * the regime-(a) entries are scored on ANOMALIES about each
        #     source's own 1985-2014 climatology, so they load from 1985
        #     while the EOF basis, the maps and the annual cycles keep the
        #     test window (`windows.scores_anomalies`).
        #   * a full-record suite (the daily one) has no nominal window at
        #     all, but a variable with a STAGED reference still has to be cut
        #     to that reference's own record: an in-sample climatology has to
        #     be the same statistic for the model and the observations, and
        #     must not reach into the reserved window, so it is cut to the
        #     reference's own pre-2015 years (`SuiteSpec.reference_overlap`).
        #     The one entry scored as anomalies (`pr_extremes_series`) is cut
        #     instead to the baseline start through the reference's own last
        #     complete year, because it also scores the post-2015 steps —
        #     but that is still only ~25 years, not the model's whole
        #     1850-2100 record, which is what a 12 h daily job timed out
        #     reading for every comparison-ensemble member (2026-09-26).
        suite_timerange_kwarg: str | None = timerange
        windows_used: dict[str, tuple[str, str]] = {}
        if timerange is not None or spec.reference_overlap:
            windowed_dir = out_dir / "_windowed_suites"
            windowed_dir.mkdir(parents=True, exist_ok=True)
            resolved, windows_used = materialise_windowed_suite(
                resolved,
                windowed_dir,
                timerange,
                args.data_root,
            )
            suite_timerange_kwarg = None
            from climatebench2 import reference_windows

            moved = list(reference_windows.summarise(windows_used))
            if moved:
                print(
                    f"Suite '{stem}': {len(moved)} variable window(s) differ "
                    f"from the nominal "
                    f"{timerange or reference_windows.FULL_RECORD} — extended "
                    f"back to the baseline window where the score is an "
                    f"anomaly, clipped forward to what the reference record "
                    f"covers —",
                    file=sys.stderr,
                )
                for line in moved:
                    print(line, file=sys.stderr)

        if setup.mode == "frozen" and stripped and setup.manifest is not None:
            _check_frozen_windows(stem, stripped, windows_used, setup.manifest)

        suite = build_suite(resolved, extra_kwargs, suite_timerange_kwarg)

        # (data, information) pairs to run, in order; the first writes the
        # database and the rest append into it.
        runs: list[tuple[Any, DataSourceInformation]] = []
        if spec.shape == "experiments":
            available = set(experiments)
            if not any(
                set(getattr(diag, "_required_data_keys", ())) <= available
                for diag in diagnostics.values()
            ):
                print(
                    f"Skipping suite '{stem}': none of its diagnostics can run "
                    f"from the experiments given ({', '.join(sorted(available))}); "
                    f"add --experiment KEY=PATH (e.g. picontrol=DIR, 4xco2=DIR)",
                    file=sys.stderr,
                )
                continue
            if spec.per_member and not explicit_historical:
                # Tier II aggregated scalars (§II.1) are scored across the
                # submission's ensemble, so each member contributes its OWN
                # historical record under the `historical` key, with its own
                # variant in the data id; every other experiment is shared.
                runs = [
                    ({**experiments, "historical": load(path, None)}, info_for(label))
                    for label, path in members.items()
                ]
            else:
                if spec.per_member and len(members) > 1:
                    print(
                        f"Note: suite '{stem}' runs per member, but "
                        f"--experiment historical=DIR pins one record for "
                        f"every member; running it once instead",
                        file=sys.stderr,
                    )
                runs = [(experiments, info_for(first_label))]
        elif spec.source == "picontrol":
            if "picontrol" in experiments:
                runs = [(experiments["picontrol"], info_for(first_label))]
            else:
                print(
                    f"Warning: suite '{stem}' wants the piControl experiment "
                    f"(the paper asks for >= 100 yr of control) but none was "
                    f"given; falling back to the submission's own output — pass "
                    f"--experiment picontrol=DIR for a protocol-conforming run",
                    file=sys.stderr,
                )
                runs = [(load(first_path, None), info_for(first_label))]
        else:
            # `per_member` is authoritative for every shape, so a cube suite
            # that is *not* per-member scores the first member only.
            chosen = (
                list(members.items())
                if spec.per_member
                else [(first_label, first_path)]
            )
            # The submission is loaded over the WIDEST window any entry of
            # this suite asks for — back to the baseline window, because the
            # regime-(a) entries score anomalies about each member's own
            # 1985-2014 climatology. That costs only file reads: every
            # diagnostic re-clips the CubeList to its own variable's
            # `timerange` (`climateeval._utils.get_prepared_cube` ->
            # `_extract_time_and_region`), so the entries that keep the test
            # window are unaffected.
            load_timerange = (
                windows.extend_to_baseline(timerange)
                if timerange is not None
                else None
            )
            runs = [
                (load(path, load_timerange), info_for(label)) for label, path in chosen
            ]

        if spec.window == "tier2" and setup.mode == "live":
            root = staged_root(args.data_root)
            print(
                "Note: the CMIP6 comparison ensemble of the Tier II suites is "
                "climatebench2.data.StagedCMIP6HistoricalSSP245 — the "
                "historical+SSP2-4.5 members staged under "
                + (str(root) if root else "(no staged root found)")
                + " (override with $"
                + STAGED_ROOT_ENV
                + "). It covers the post-2015 test window and carries "
                "every member staged by the protocol's policy, so E_ref is "
                "formed from several members per comparison model. Upstream's "
                "climateeval.data.CMIP6HistoricalSSP245 is the equivalent "
                "pool-globbing generator (identical data ids, minutes per call). "
                "Model variables that do not cover the window are skipped, not "
                "fatal.",
                file=sys.stderr,
            )

        db_path = out_dir / f"{stem}.ddb"
        db_path.unlink(missing_ok=True)
        Path(str(db_path) + ".wal").unlink(missing_ok=True)
        window = timerange or "full record"
        print(
            f"Running suite '{stem}' ({spec.shape}, {window})"
            + (f" for {len(runs)} members" if len(runs) > 1 else ""),
            file=sys.stderr,
        )
        # A per-member cube suite loads its reference and comparison
        # ensemble ONCE for all members (climatebench2.shared_sources): it
        # runs diagnostic by diagnostic, each shareable diagnostic built once
        # and its reference/other stages memoised across the members. Same
        # rows, same schema; before this, ten members meant ten reads of a
        # 109-160-member ensemble (the held-out daily extremes timed out at
        # 12 h on it). The experiment suites keep the member-by-member loop:
        # their complex diagnostics have no such stages to share.
        from climatebench2.shared_sources import run_members

        report = run_members(
            suite,
            runs,
            database_resource=f"duckdb://{db_path}",
            share=spec.shape == "cubes" and not args.no_cache,
        )
        if len(runs) > 1:
            print(f"Suite '{stem}': {report.summary()}", file=sys.stderr)
        print(f"Wrote database {db_path}", file=sys.stderr)
        db_paths.append(db_path)

    for name in sorted(set(not_applicable) - declared_seen):
        print(
            f"Warning: --not-applicable '{name}' matched no gate diagnostic in "
            f"the suites that ran; the scorecard will show it as 'not run', "
            f"not 'n/a'",
            file=sys.stderr,
        )

    if len(members) > 1:
        print(
            f"\nIngested {len(members)} members ({', '.join(members)}) into the "
            f"per-member suites (the cube suites and the Tier II events), each "
            f"with its own data id; Tier I and Tier III ran once on "
            f"'{first_label or args.variant or args.name}'. The scoring pass "
            f"below stacks them into one fair-CRPS ensemble per model.",
            file=sys.stderr,
        )

    if db_paths and setup.mode == "frozen":
        merge_frozen_comparison(
            db_paths,
            setup,
            stripped_by_stem,
            allow_missing=args.allow_missing_comparison,
        )

    if db_paths and not args.no_score:
        run_scoring_pass(db_paths)

    print(
        f"\nDone. Score with:  climatebench2 leaderboard "
        f"{' '.join(str(p) for p in db_paths)}",
        file=sys.stderr,
    )


def run_regate_pass(db_paths: list[Path]) -> None:
    """Re-evaluate every Tier I/III gate against the current thresholds.yml.

    Unlike :func:`run_scoring_pass`, this never re-runs a suite: it re-reads
    each gate's already-computed raw statistic from ``<schema>.raw_output``
    and re-applies the current ``thresholds.yml`` bound
    (``climatebench2.regate``). It is how a ``thresholds.yml`` edit reaches
    an existing database's ``passes``/``bound_lower``/``bound_upper`` without
    the hours-per-model cost of re-running the suite; idempotent.
    """
    from climatebench2.regate import regate_databases

    print(
        "\nRe-gating Tier I/III checks against the current thresholds.yml…",
        file=sys.stderr,
    )
    for report in regate_databases(db_paths):
        print(f"  {report.summary()}", file=sys.stderr)
        for message in report.messages:
            print(f"  {message}", file=sys.stderr)


def run_scoring_pass(db_paths: list[Path]) -> None:
    """Run the Tier II scoring pass over the given databases, reporting to stderr.

    Members are ingested as separate data sources, so the fair CRPS of a
    model's ensemble can only be formed once every member has run — this is
    that step (``climatebench2.scoring_pass``). It is idempotent: re-running
    it replaces the scores it wrote before.
    """
    from climatebench2.scoring_pass import score_databases

    print("\nScoring Tier II (fair CRPS over stacked members)…", file=sys.stderr)
    for report in score_databases(db_paths):
        print(f"  {report.summary()}", file=sys.stderr)
        for message in report.messages:
            print(f"  {message}", file=sys.stderr)


def _cmd_freeze_comparison(args: argparse.Namespace) -> None:
    from climatebench2.frozen_comparison import FrozenComparisonError, freeze

    try:
        manifest = freeze(
            args.db,
            args.out,
            nominal_timerange=args.timerange,
            notes=_parse_notes(args.note),
            exclude=args.exclude_variable or (),
            include=args.include_variable or (),
            extend=args.extend,
            replace=args.replace,
        )
    except FrozenComparisonError as exc:
        raise SystemExit(str(exc)) from exc
    for stem, record in manifest["databases"].items():
        entries = record["entries"]
        pairs = sum(len(v) for v in entries.values())
        print(
            f"{stem}: {pairs} (diagnostic, variable) pair(s) frozen"
            + (f" -> {args.out / record['file']} ({record['size']} bytes)"
               if record.get("file") else " (no comparison rows)"),
            file=sys.stderr,
        )
        for schema, variables in sorted(entries.items()):
            for var_id, entry in sorted(variables.items()):
                print(
                    f"  {schema}/{var_id}: {entry['n_models']} models, "
                    f"{entry['n_members']} members, {entry['raw_rows']} rows, "
                    f"window {', '.join(entry['timerange']) or '?'}",
                    file=sys.stderr,
                )
    if manifest.get("nominal_timerange") is None:
        print(
            "Note: no --timerange given, so the bundle pins no nominal Tier II "
            "window; a frozen run then uses today's protocol default and only "
            "the per-variable windows are checked",
            file=sys.stderr,
        )
    if manifest.get("excluded_variables"):
        print(
            f"Left out: {', '.join(manifest['excluded_variables'])} "
            f"(tier2.frozen_comparison.exclude_variables / --exclude-variable; "
            f"--include-variable keeps one)",
            file=sys.stderr,
        )
    print(f"Wrote {args.out / 'manifest.json'}", file=sys.stderr)


def _cmd_merge_comparison(args: argparse.Namespace) -> None:
    from climatebench2.frozen_comparison import (
        FrozenComparisonError,
        check_provenance,
        load_manifest,
        merge,
    )

    try:
        for note in check_provenance(load_manifest(args.bundle), timerange=None):
            print(f"Note: {note}", file=sys.stderr)
        for path in args.db:
            report = merge(path, args.bundle, replace=args.replace)
            print(report.summary(), file=sys.stderr)
            for message in report.messages:
                print(f"  {message}", file=sys.stderr)
    except FrozenComparisonError as exc:
        raise SystemExit(str(exc)) from exc


def _cmd_leaderboard(args: argparse.Namespace) -> None:
    db_paths = [p.resolve() for p in args.db]
    for path in db_paths:
        if not path.exists():
            msg = f"Database not found: {path}"
            raise SystemExit(msg)

    if args.regate:
        run_regate_pass(db_paths)

    if args.rescore:
        run_scoring_pass(db_paths)

    from climatebench2.leaderboard import (
        build_scores,
        build_scores_table,
        render_html,
    )

    if args.csv is not None:
        summary = build_scores_table(db_paths)
        if summary.empty:
            msg = "No metrics found in the given database(s)."
            raise SystemExit(msg)
        summary.to_csv(args.csv, index=False)
        print(f"Wrote {args.csv}", file=sys.stderr)
        return

    scores = build_scores(db_paths)
    if all(
        frame.empty
        for frame in (
            scores.gates,
            scores.crps,
            scores.consistency,
            scores.tier3,
            scores.tier3_scores,
        )
    ):
        msg = "No ClimateBench2 scores found in the given database(s)."
        raise SystemExit(msg)
    html = render_html(scores, source_names=[p.name for p in db_paths])
    output = args.output or Path("climatebench2_leaderboard.html")
    output.write_text(html, encoding="utf-8")
    print(f"Wrote {output}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``climatebench2`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="climatebench2",
        description=(
            "ClimateBench2: score and test climate models against the "
            "ClimateBench v2 protocol, using ClimateEval for all diagnostics."
        ),
        epilog=(
            "examples:\n"
            "  climatebench2 score /path/to/model/cmor/Amon --name MyModel\n"
            "      run the ClimateBench2 suites and write result databases\n"
            "  climatebench2 score MODEL --name Emulator "
            "--not-applicable geostrophic_balance\n"
            "      declare a Tier I gate inapplicable (recorded as n/a, not a fail)\n"
            "  climatebench2 score MODEL --name MyModel "
            "--member r1i1p1f1=DIR --member r2i1p1f1=DIR\n"
            "      score several ensemble members into the same databases\n"
            "  climatebench2 leaderboard MyModel_climatebench2/*.ddb\n"
            "      build the scorecard from the results\n"
            "  climatebench2 leaderboard --rescore MyModel_climatebench2/*.ddb\n"
            "      re-run the Tier II scoring pass first (idempotent)\n"
            "\n"
            "each suite gets the data shape and window the protocol asks for:\n"
            "  Tier I / TierII_events / TierIII  experiment dict, every "
            "experiment in full\n"
            "  TierI_variability                 the piControl experiment, in "
            "full (>= 100 yr)\n"
            f"  TierII                            model cubes over "
            f"{default_tier2_timerange()} (--timerange overrides)\n"
            "  TierII_daily                      model cubes over the full "
            "record (extremes/PDFs/diurnal are in-sample)\n"
            "\n"
            "protocol spec: docs/metrics_reference.md\n"
            "architecture:  docs/climateeval_delineation_plan.md"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    score = subparsers.add_parser(
        "score",
        help="Run the ClimateBench2 suites on a model via ClimateEval.",
    )
    score.add_argument(
        "model",
        type=Path,
        help=(
            "Model output: a NetCDF file, a flat directory of NetCDF files, "
            "or a CMOR DRS tree (e.g. an Amon table directory)."
        ),
    )
    score.add_argument("--name", default="model", help="Model name (provenance).")
    score.add_argument(
        "--experiment",
        action="append",
        metavar="KEY=PATH",
        help=(
            "CMOR output of an auxiliary experiment for the Tier I gates; "
            "repeatable. Keys: picontrol, 4xco2, histaer, day, amip, "
            "amip4xco2, patch_ep, patch_wp (historical defaults to the MODEL "
            "data). Gates whose keys are absent are skipped; suites needing "
            "experiments are skipped if none are given."
        ),
    )
    score.add_argument(
        "--not-applicable",
        action="append",
        metavar="NAME",
        help=(
            "Declare a Tier I gate inapplicable to this submission "
            "(repeatable; NAME is the gate's suite entry name, e.g. "
            "geostrophic_balance for a model with no dynamical "
            "representation). The gate computes nothing and is recorded as "
            "n/a — neither a pass nor a fail — on the scorecard, which is "
            "different from a gate that simply did not run."
        ),
    )
    score.add_argument(
        "--suite",
        action="append",
        metavar="SUITE",
        help=(
            f"Suite name or YAML path; repeat for several suites. CB2 suites "
            f"shadow ClimateEval ones of the same name "
            f"(default: {', '.join(DEFAULT_SUITES)})."
        ),
    )
    score.add_argument(
        "--member",
        action="append",
        metavar="LABEL=PATH",
        help=(
            "An ensemble member of the submission (repeatable); LABEL becomes "
            "the variant of its data id, e.g. r1i1p1f1=/path/r1i1p1f1. If not "
            "given and MODEL is a DRS tree with several r*i*p*f* directories, "
            "they are discovered automatically. The per-member suites — the "
            "cube suites and ClimateBench2_TierII_events, whose Tier II "
            "aggregated scalars are scored across the ensemble — run once per "
            "member into one database per suite (an event run takes that "
            "member's own record as its `historical` experiment); the Tier I "
            "and paleo suites run once per model, on the first member."
        ),
    )
    score.add_argument(
        "--truth",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Perfect-model test (Tier III.2): CMOR output of a HELD-OUT ESM "
            "run to score against instead of observations. The cube-based "
            "Tier II suites then use it as their reference for every "
            "variable, the CMIP6 comparison models are dropped (E_ref against "
            "the real world is meaningless in a perfect-model experiment) and "
            "every scored row is labelled window = perfect-model."
        ),
    )
    score.add_argument(
        "--truth-member",
        action="append",
        metavar="LABEL=PATH",
        help=(
            "A further ensemble member of the truth model (repeatable). These "
            "are not extra references: they are what the large-ensemble "
            "spread test compares the submission's own inter-member spread "
            "against (tier3.le_spread, Extended)."
        ),
    )
    score.add_argument(
        "--truth-name",
        default="PerfectModelTruth",
        help="Name the truth run is recorded under (e.g. CESM2).",
    )
    score.add_argument(
        "--timerange",
        default=None,
        help=(
            "ISO time range for the cube-based suites, overriding their "
            "protocol defaults: the Tier II suites score the reserved test "
            f"window (currently {default_tier2_timerange()}, from "
            f"tier2.test_window_start) and other cube suites use "
            f"{DEFAULT_TIMERANGE}. The experiment-based suites (Tier I, "
            "events, paleo) always load their experiments in full and ignore "
            "this, as does the piControl-fed variability suite."
        ),
    )
    score.add_argument(
        "--data-root",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Directory with staged reference datasets, in ClimateEval's "
            "<data-source-id>/<frequency>/<var>/*.nc layout. It also supplies "
            "the Tier II CMIP6 comparison ensemble "
            "(CMIP6_<model>_historical-ssp245_<member>/... , see "
            "climatebench2.data.StagedCMIP6HistoricalSSP245); set "
            "$CLIMATEBENCH2_STAGED_CMIP6_ROOT to discover that ensemble in a "
            "different directory (e.g. a small subset for a smoke run) while "
            "still loading everything from --data-root."
        ),
    )
    score.add_argument(
        "--paleo-data-root",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Where the processed Tier III proxy NetCDFs live — the "
            "<period>/<dataset>.nc tree written by "
            "paleo_scripts/process_paleo_observations.py. Defaults to "
            "tier3.paleo_data_root in thresholds.yml, resolved against the "
            "working directory."
        ),
    )
    score.add_argument(
        "--download",
        action="store_true",
        help="Download missing references (ERA5 needs ~/.cdsapirc).",
    )
    score.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        metavar="DIR",
        help="Output directory (default: <name>_climatebench2/).",
    )
    score.add_argument(
        "--no-score",
        action="store_true",
        help=(
            "Write the suite databases but skip the Tier II scoring pass "
            "(fair CRPS over the stacked ensemble members, its bootstrap "
            "interval and the skill against the CMIP6 median). Run it later "
            "with `climatebench2 leaderboard --rescore DB ...`."
        ),
    )
    score.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Do not share the reference and CMIP6 comparison ensemble across "
            "the members of a per-member cube suite. By default each such "
            "diagnostic loads them once and reuses the result for every "
            "--member (climatebench2.shared_sources), which writes the same "
            "rows; with this flag every member re-loads them "
            "(N members = N reads of the whole ensemble)."
        ),
    )
    score.add_argument(
        "--comparison",
        choices=COMPARISON_MODES,
        default="live",
        help=(
            "Where the CMIP6 comparison ensemble behind E_ref comes from. "
            "live (default): run it, as always — the staged pool must be on "
            "disk. frozen: strip the comparison generators from every suite's "
            "other_data (observational products and the reference stay), "
            "skip the Tier I gate wrappers' upstream ensemble, and merge the "
            "rows of --frozen-comparison DIR into the databases before the "
            "scoring pass, so E_ref is that frozen ensemble's. none: strip as "
            "for frozen and merge nothing — E_ref and the skill stay empty."
        ),
    )
    score.add_argument(
        "--frozen-comparison",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Bundle written by `climatebench2 freeze-comparison` (with "
            "--comparison frozen). Its protocol fingerprint, ClimateBench2 "
            "version and Tier II window must match this run's, and each "
            "variable's resolved window must match the bundle's, or the run "
            "stops; without --timerange the Tier II window is the bundle's."
        ),
    )
    score.add_argument(
        "--allow-missing-comparison",
        action="store_true",
        help=(
            "With --comparison frozen: let a scored (diagnostic, variable) "
            "that the bundle has no rows for go through with an empty E_ref "
            "and skill, instead of stopping the run."
        ),
    )
    score.add_argument(
        "--isolate-crashes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Log an unexpected exception inside a gate (marker "
            "CB2_GATE_CRASH) and carry on with the next diagnostic instead of "
            "aborting the suite; the gate then has no row. Default: on with "
            "--comparison frozen/none (unattended scoring of a submission), "
            "off with live (today's behaviour)."
        ),
    )
    score.add_argument("--institute", default="", help="Institute (provenance).")
    score.add_argument("--exp", default="", help="Experiment (provenance).")
    score.add_argument("--variant", default="", help="Variant label (provenance).")
    score.set_defaults(func=_cmd_score)

    freeze_parser = subparsers.add_parser(
        "freeze-comparison",
        help=(
            "Copy the CMIP6 comparison ensemble's rows out of result databases "
            "into a frozen bundle for `score --comparison frozen`."
        ),
    )
    freeze_parser.add_argument(
        "db",
        type=Path,
        nargs="+",
        help=(
            "Result .ddb file(s) of a LIVE run; split-suite databases (one "
            "variable group each) are fine, each (diagnostic, variable) may "
            "come from one database only. One bundle database per suite stem."
        ),
    )
    freeze_parser.add_argument(
        "-o",
        "--out",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Bundle directory to create (must be empty or absent), or the "
            "bundle to add to with --extend."
        ),
    )
    freeze_parser.add_argument(
        "--timerange",
        default=None,
        help=(
            "The nominal Tier II window the source runs used (their "
            "--timerange, or the protocol default the day they ran, e.g. "
            "20150101/20251231). Recorded and enforced: a frozen run without "
            "--timerange adopts it."
        ),
    )
    freeze_parser.add_argument(
        "--note",
        action="append",
        metavar="KEY=VALUE",
        help=(
            "Free-form provenance for the manifest (repeatable), e.g. the "
            "CB2/ClimateEval commits the source runs used."
        ),
    )
    freeze_parser.add_argument(
        "--exclude-variable",
        action="append",
        metavar="VAR",
        help=(
            "Leave a suite variable out of the bundle (repeatable), on top of "
            "tier2.frozen_comparison.exclude_variables in thresholds.yml "
            "(currently ts: dropped from the v2 cloud protocol)."
        ),
    )
    freeze_parser.add_argument(
        "--include-variable",
        action="append",
        metavar="VAR",
        help="Keep a variable the default exclusion would leave out (repeatable).",
    )
    freeze_parser.add_argument(
        "--extend",
        action="store_true",
        help=(
            "Add the databases' (diagnostic, variable) pairs to the EXISTING "
            "bundle in --out — one variable group at a time, e.g. OHC once it "
            "is staged — after checking it was made under the same protocol "
            "and window. The rest of the bundle is kept as it is."
        ),
    )
    freeze_parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "With --extend: swap a (diagnostic, variable) already in the "
            "bundle for the new database's rows (re-freeze a group after a "
            "re-run) instead of refusing."
        ),
    )
    freeze_parser.set_defaults(func=_cmd_freeze_comparison)

    merge_parser = subparsers.add_parser(
        "merge-comparison",
        help="Merge a frozen comparison bundle into existing result databases.",
    )
    merge_parser.add_argument("bundle", type=Path, help="Bundle directory.")
    merge_parser.add_argument("db", type=Path, nargs="+", help="DuckDB .ddb file(s).")
    merge_parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "First delete every comparison row of each diagnostic the bundle "
            "covers (all its variables), instead of refusing to merge into a "
            "database that already has some."
        ),
    )
    merge_parser.set_defaults(func=_cmd_merge_comparison)

    leaderboard = subparsers.add_parser(
        "leaderboard",
        help="Build the ClimateBench2 leaderboard from result databases.",
    )
    leaderboard.add_argument("db", type=Path, nargs="+", help="DuckDB .ddb file(s).")
    leaderboard.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="HTML output path (default: climatebench2_leaderboard.html).",
    )
    leaderboard.add_argument(
        "--csv",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write the simple deterministic summary as CSV instead of HTML.",
    )
    leaderboard.add_argument(
        "--rescore",
        action="store_true",
        help=(
            "Re-run the Tier II scoring pass over the databases first, "
            "replacing the scores in them. `climatebench2 score` already runs "
            "the pass; use this on databases written with --no-score, on ones "
            "written before the pass existed, or after changing "
            "thresholds.yml. The pass is idempotent."
        ),
    )
    leaderboard.add_argument(
        "--regate",
        action="store_true",
        help=(
            "Re-evaluate every Tier I/III gate's pass/fail against the "
            "CURRENT thresholds.yml, without re-running the suite: rewrites "
            "`passes`/`bound_lower`/`bound_upper`/`requirement`/`tier` from "
            "each gate's already-computed raw statistic (`raw_output`). Use "
            "this after a thresholds.yml edit instead of re-running `score` "
            "(hours per model); never touches Tier II/III scorer rows or "
            "n/a rows. Idempotent."
        ),
    )
    leaderboard.set_defaults(func=_cmd_leaderboard)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the climatebench2 command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    args.func(args)


if __name__ == "__main__":
    main()
