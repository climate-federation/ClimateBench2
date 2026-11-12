"""Pick one frequency of a variable a submission provides at several.

A CMIP-style member directory holds every table the model wrote:
``<member>/Amon/pr`` *and* ``<member>/day/pr`` (likewise ``Omon``/``Oday``,
``SImon``/``SIday``, ``3hr``/``1hr`` …). ``deploy/README.md`` documents that
layout for submissions. ClimateEval's ``load_cmor_dir`` returns one cube per
DRS leaf, so such a directory loads as **two** ``pr`` cubes, and
``climateeval._utils.get_prepared_cube`` — which extracts by ``var_name``
only — then fails every ``pr`` variable with ``Got 2 cubes for constraint
NameConstraint(var_name='pr'), expecting 1``. On the first full-suite cloud run
(MPI-ESM1-2-LR, 2026-10-30) that silently removed the submission's ``pr``
from every Tier II entry, the daily suite and Tier I I.3c/I.8b.

The variable already says which one it wants: every ``Variable`` carries a
``frequency`` (``mon`` / ``day`` / ``1hr`` in the suite YAMLs, ``mon`` from
the Tier I ``_mon`` helper, ``day`` from ``_day``). :func:`select_frequency`
keeps, for each ``var_name`` present at **more than one** frequency, only the
cubes at the wanted one. A ``var_name`` present at a single frequency is left
alone — a directory without the ambiguity loads exactly as before — and so is
anything without a time axis (``fx`` fields).

⚠ **Belongs upstream.** The right home is ``get_prepared_cube``: when the
``var_name`` constraint matches several cubes, narrow by
``variable.frequency`` (the cube's ``frequency`` attribute, else its time
step) before failing. CB2 does not patch the pinned ClimateEval, so the
selection runs here, at the two places CB2 hands cubes to it: the CLI's member
and experiment data (per suite entry, from the entry's variables) and the
perfect-model truth source (per variable).
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from iris.cube import Cube, CubeList

#: Median time step (days) below which a series is read as that frequency,
#: for a cube without a CMOR ``frequency`` attribute.
_STEP_DAYS: tuple[tuple[float, str], ...] = (
    (1.5 / 24.0, "1hr"),
    (4.5 / 24.0, "3hr"),
    (9.0 / 24.0, "6hr"),
    (1.5, "day"),
    (45.0, "mon"),
    (400.0, "yr"),
)

#: The frequency the experiment-shaped suites read each experiment at. Their
#: complex diagnostics are monthly (``tier1_physics._mon``) except the ones
#: reading the ``day`` experiment (``tier1_extended._day``). This is
#: ClimateBench2's map, the default of ``Protocol.experiment_frequency``; the
#: active map is :func:`experiment_frequency`'s.
EXPERIMENT_FREQUENCY: dict[str, str] = {"day": "day"}
DEFAULT_EXPERIMENT_FREQUENCY = "mon"


def normalise_frequency(value: str) -> str:
    """CMOR ``frequency`` without the point/climatology qualifiers' noise.

    ``3hrPt`` and ``3hr`` are the same sampling interval for this purpose;
    ``monC`` (a climatology) and ``1hrCM`` stay distinct, since they are not
    the time series a ``mon``/``1hr`` variable means.
    """
    text = str(value).strip()
    return text.removesuffix("Pt") or text


def _time_step_frequency(cube: Cube) -> str | None:
    try:
        time = cube.coord("time")
    except Exception:  # noqa: BLE001 - iris raises CoordinateNotFoundError
        return None
    if time.shape[0] < 2:  # noqa: PLR2004 - one step has no interval
        return None
    # Only the step's length matters: convert the interval unit ("hours" of
    # "hours since ..."), never the reference date, which would fail across
    # calendars (360_day vs standard).
    from cf_units import Unit  # noqa: PLC0415

    interval = str(time.units.origin).split(" since ")[0]
    step = float(np.median(np.diff(np.sort(np.asarray(time.points, dtype=float)))))
    try:
        step = float(Unit(interval).convert(step, "days"))
    except ValueError:
        return None
    for limit, name in _STEP_DAYS:
        if step < limit:
            return name
    return None


def cube_frequency(cube: Cube) -> str | None:
    """Return a cube's frequency: its CMOR attribute, else its time step."""
    attribute = cube.attributes.get("frequency")
    if attribute:
        frequency = normalise_frequency(str(attribute))
        # GFDL-CM4 labels its ordinary monthly-mean Amon files `monC`, the
        # CMIP6 frequency of a monthly CLIMATOLOGY. A cube whose time axis
        # steps by a month through the years is a monthly series whatever
        # the label says (a real climatology has twelve steps).
        if frequency == "monC" and _is_monthly_series(cube):
            return "mon"
        return frequency
    return _time_step_frequency(cube)


#: A monthly climatology has twelve time steps; more is a series.
_CLIMATOLOGY_STEPS = 12


def _is_monthly_series(cube: Cube) -> bool:
    try:
        n_time = cube.coord("time").shape[0]
    except Exception:  # noqa: BLE001 - iris raises CoordinateNotFoundError
        return False
    return n_time > _CLIMATOLOGY_STEPS and _time_step_frequency(cube) == "mon"


def _has_time(cube: Cube) -> bool:
    return hasattr(cube, "coords") and bool(cube.coords("time"))


def frequencies_by_variable(cubes: Iterable[Cube]) -> dict[str, set[str]]:
    """``var_name -> {frequency}`` over the time-varying cubes."""
    found: dict[str, set[str]] = defaultdict(set)
    for cube in cubes:
        if not _has_time(cube):  # also anything that is not a cube
            continue
        frequency = cube_frequency(cube)
        found[str(cube.var_name or cube.name())].add(frequency or "?")
    return dict(found)


def ambiguous_variables(cubes: Iterable[Cube]) -> dict[str, set[str]]:
    """Return the ``var_name``s present at more than one frequency."""
    return {
        name: found
        for name, found in frequencies_by_variable(cubes).items()
        if len(found) > 1
    }


def select_frequency(
    cubes: CubeList,
    wanted: str | Mapping[str, Iterable[str]] | Iterable[str],
) -> CubeList:
    """``cubes`` with each ambiguous ``var_name`` narrowed to ``wanted``.

    ``wanted`` is one frequency, a set of them, or ``{var_name: frequencies}``
    (a ``var_name`` it does not list falls back to the union of its values —
    the inputs of a derived variable share their output's frequency). A
    ``var_name`` with no cube at a wanted frequency loses all its cubes, so
    the diagnostic reports it missing rather than reading the wrong sampling.
    Returns ``cubes`` itself when nothing is ambiguous.
    """
    from iris.cube import CubeList  # noqa: PLC0415

    ambiguous = ambiguous_variables(cubes)
    if not ambiguous:
        return cubes
    if isinstance(wanted, str):
        default = {normalise_frequency(wanted)}
        per_name: dict[str, set[str]] = {}
    elif isinstance(wanted, dict):
        per_name = {
            str(name): {normalise_frequency(f) for f in values}
            for name, values in wanted.items()
        }
        default = set().union(*per_name.values()) if per_name else set()
    else:
        default = {normalise_frequency(f) for f in wanted}
        per_name = {}

    kept = CubeList()
    for cube in cubes:
        name = str(cube.var_name or cube.name())
        if not _has_time(cube) or name not in ambiguous:
            kept.append(cube)
            continue
        if (cube_frequency(cube) or "?") in per_name.get(name, default):
            kept.append(cube)
    return kept


def entry_frequencies(entry: Mapping[str, Any]) -> dict[str, set[str]]:
    """``var_name -> {frequency}`` of one suite entry's variables."""
    found: dict[str, set[str]] = defaultdict(set)
    for variable in entry.get("variables") or []:
        name, frequency = variable.get("var_name"), variable.get("frequency")
        if name and frequency:
            found[str(name)].add(normalise_frequency(str(frequency)))
    return dict(found)


def select_for_entry(data: Any, entry: Mapping[str, Any]) -> Any:
    """Narrow ``data`` to what one suite entry reads (non-CubeLists pass through)."""
    from iris.cube import CubeList  # noqa: PLC0415

    if not isinstance(data, CubeList):
        return data
    wanted = entry_frequencies(entry)
    if not wanted:
        return data
    return select_frequency(data, wanted)


def experiment_frequency(key: str) -> str:
    """Return the frequency an experiment-shaped suite reads ``key`` at.

    From the active protocol's ``experiment_frequency`` (so a protocol can
    declare that ``amip_day`` is daily); keys it does not list are monthly.
    """
    from climatebench2.protocol import get_protocol  # noqa: PLC0415

    return get_protocol().experiment_frequency.get(key, DEFAULT_EXPERIMENT_FREQUENCY)
