"""Ocean heat content of a depth layer, computed from ``thetao`` on load.

Why this exists
---------------
Tier II scores the global ocean heat content (OHC) of the full column and of
the 0-2000 m and 0-100 m layers (paper Table 2). ClimateEval's
:class:`climateeval.diags.simple.OceanHeatContentTimeSeries` sums the derived
per-layer heat content ``phcint`` (J m-2) over depth and area, but the pinned
ESMValCore (2.14) has **no** ``phcint`` derivation — it exists only on
ESMValCore ``main`` (``esmvalcore/preprocessor/_derive/phcint.py``) — so
ClimateEval can only read a ``phcint`` that was pre-computed and staged. That
would mean every submission uploading a derived 3-D field it does not
normally write, and ~1.2 TB of ``phcint`` materialised for the CMIP6
comparison ensemble.

:func:`layer_heat_content` instead turns the ``thetao`` that every source
has — the submission, the EN4/IAP references and every comparison member —
into the layer's heat content **inside the load**, as an
``additional_preprocessors`` step of the suite variable (ClimateEval runs any
importable function there, after its own time/region cut). Nothing is
written to disk, and all sources go through the same code.

The physics is ESMValCore ``main``'s ``phcint``: ``thetao`` in K times
``rho_0 c_p = 4.09169e6 J m-3 K-1`` (Kuhlbrodt et al. 2015) times the layer
thickness from the vertical coordinate's bounds. Two things differ, both
deliberate:

* **The layer is cut exactly.** A level straddling ``z_max`` contributes the
  part of its cell above ``z_max`` (``extract_volume`` would keep or drop the
  whole cell by the depth of its centre, so "0-2000 m" would end anywhere
  between ~1870 m and ~2130 m depending on the grid — EN4's last level centred
  above 2000 m ends at 2000.0 m but a model's may not).
* **The layer is summed over depth here**, on the source's own grid, and
  returned as a single level spanning the layer. The diagnostic's horizontal
  regridding then sees a 2-D field per time step instead of 35-119 levels,
  which is what makes the comparison ensemble affordable, and the column is
  integrated over its own wet cells before any interpolation touches it.

Vertical coordinates in the CMIP6 pool are not uniformly CMOR-clean, and the
staged files are read without ESMValCore's CMOR fixes, so
:func:`depth_bounds_m` normalises them: units converted to metres (CESM2's
``lev`` is in centimetres while its ``lev_bnds`` are in metres — the bounds
are rescaled when the points do not sit inside them); a unitless coordinate
(MIROC's ``ocean_sigma_z`` nominal depths) is read as metres; missing bounds
(AWI-CM's FESOM levels) are guessed from the midpoints with the top at the
surface. A coordinate that is not a depth (NorESM2's isopycnal ``rho``) is an
error, never a guess.

Ownership
---------
This is data derivation, which belongs upstream (CLAUDE.md, the ownership
test): either a ClimateEval release on an ESMValCore that ships ``phcint``, or
a ClimateEval preprocessor equivalent to this function. It is here, declared,
because ClimateEval is pinned (d2559b2) and CB2 does not patch it; it is
written so that moving it is a file copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

if TYPE_CHECKING:
    from iris.coords import Coord
    from iris.cube import Cube

#: ``rho_0 * c_p`` [J m-3 K-1] — the constant of ESMValCore ``main``'s
#: ``phcint`` derivation (Kuhlbrodt et al. 2015, Clim. Dyn.), kept identical so
#: a future upstream derivation reproduces these numbers. A physical constant
#: of the derivation, like those in :mod:`climatebench2.physics`, not a
#: protocol threshold; the layer bounds, which are protocol, are the suite
#: variable's ``additional_preprocessors`` kwargs (recorded in every result
#: database's ``variables`` row, which a frozen comparison merge checks).
RHO_CP = 4.09169e6

#: Output metadata: ESMValCore's ``phcint`` (ClimateEval ``variables.yml``).
PHCINT_VAR_NAME = "phcint"
PHCINT_STANDARD_NAME = (
    "integral_wrt_depth_of_sea_water_potential_temperature_expressed_as_heat_content"
)
PHCINT_LONG_NAME = "Integrated Ocean Heat Content from Potential Temperature"
PHCINT_UNITS = "J m-2"

#: Relative tolerance when checking that each level's point lies inside its
#: bounds (float32 bounds in the CMIP6 files are not exact).
_BOUNDS_RTOL = 1e-3


def vertical_coord(cube: Cube) -> Coord:
    """The cube's vertical dimension coordinate.

    After ClimateEval's ``check_coordinates`` the ``olevel`` axis of an ocean
    variable is always the dimension coordinate with ``var_name = 'lev'``;
    otherwise the single dimension coordinate iris guesses as the Z axis.
    """
    named = cube.coords(var_name="lev", dim_coords=True)
    if len(named) == 1:
        return named[0]
    guessed = cube.coords(axis="Z", dim_coords=True)
    if len(guessed) == 1:
        return guessed[0]
    msg = (
        "Cannot identify the vertical coordinate of "
        f"{cube.summary(shorten=True)} (no dimension coordinate 'lev' and "
        f"{len(guessed)} Z-axis dimension coordinates)"
    )
    raise ValueError(msg)


def _guess_depth_bounds(points: np.ndarray) -> np.ndarray:
    """Midpoint bounds, the first level's top at the surface."""
    if points.size == 1:
        return np.array([[0.0, 2.0 * points[0]]])
    mid = 0.5 * (points[1:] + points[:-1])
    top = np.concatenate([[0.0], mid])
    bottom = np.concatenate([mid, [points[-1] + 0.5 * (points[-1] - points[-2])]])
    return np.stack([top, bottom], axis=1)


def _inside(points: np.ndarray, bounds: np.ndarray) -> bool:
    """Whether every point lies within its own cell (with a float tolerance)."""
    lo, hi = bounds.min(axis=1), bounds.max(axis=1)
    tol = _BOUNDS_RTOL * np.maximum(hi - lo, 1e-6)
    return bool(np.all((points >= lo - tol) & (points <= hi + tol)))


def depth_bounds_m(coord: Coord) -> np.ndarray:
    """``(n, 2)`` cell bounds of a vertical coordinate, in metres, positive down.

    Raises ``ValueError`` for a coordinate that is not a depth (a density or
    pressure axis), or whose bounds cannot be reconciled with its points.
    """
    from cf_units import Unit  # noqa: PLC0415

    units = coord.units
    metres = Unit("m")
    points = np.abs(np.asarray(coord.points, dtype=np.float64))
    bounds = (
        np.abs(np.asarray(coord.bounds, dtype=np.float64))
        if coord.has_bounds()
        else None
    )
    if units.is_unknown() or units.is_no_unit() or units.is_dimensionless():
        # MIROC's ocean_sigma_z: nominal depths with no unit. Anything that
        # does not look like a depth axis in metres is refused.
        if points.size < 2 or points.max() < 10.0:  # noqa: PLR2004
            msg = f"Unitless vertical coordinate {coord.name()!r} does not look like depth in m"
            raise ValueError(msg)
        logger.debug(f"Reading unitless vertical coordinate {coord.name()!r} as metres")
    elif units.is_convertible(metres):
        points = units.convert(points, metres)
        if bounds is not None:
            bounds = units.convert(bounds, metres)
    else:
        msg = (
            f"Vertical coordinate {coord.name()!r} is in {units}, not a depth: "
            "ocean heat content needs thetao on depth levels"
        )
        raise ValueError(msg)

    if bounds is None:
        return _guess_depth_bounds(points)
    if _inside(points, bounds):
        return bounds
    # CESM2: `lev` in centimetres, `lev_bnds` in metres (iris gives bounds the
    # coordinate's units). Rescale the bounds if that puts every point inside.
    for factor in (100.0, 0.01):
        if _inside(points, bounds * factor):
            logger.debug(
                f"Rescaled the bounds of {coord.name()!r} by {factor}: its points "
                "were outside them (bounds and points in different units)",
            )
            return bounds * factor
    logger.warning(
        f"The bounds of vertical coordinate {coord.name()!r} do not contain its "
        "points; using midpoint bounds instead",
    )
    return _guess_depth_bounds(points)


def layer_thickness(
    bounds_m: np.ndarray,
    z_min: float = 0.0,
    z_max: float | None = None,
) -> np.ndarray:
    """Thickness [m] of each cell inside ``[z_min, z_max]`` (0 outside)."""
    top = bounds_m.min(axis=1)
    bottom = bounds_m.max(axis=1)
    upper = np.inf if z_max is None else float(z_max)
    return np.clip(np.minimum(bottom, upper) - np.maximum(top, float(z_min)), 0.0, None)


def _drop_auxiliary_coords(cube: Cube, z_dim: int) -> None:
    """Remove coordinates that would confuse the diagnostic downstream.

    Aux coords on the vertical dimension (MIROC's ``sigma``/``zlev`` formula
    terms) are meaningless once the column is summed; a second Z-like coord
    (MIROC's sea-floor ``depth`` on (y, x)) makes ``axis_statistics(axis='Z')``
    ambiguous; and the SSH formula term ``eta`` on (time, y, x) has no place
    in a regridded heat-content field. Only latitude/longitude and the
    scalar coordinates survive among the aux coords.
    """
    for coord in list(cube.aux_coords):
        dims = cube.coord_dims(coord)
        if not dims:
            continue
        if coord.name() in ("latitude", "longitude") and z_dim not in dims:
            continue
        cube.remove_coord(coord)
    for factory in list(cube.aux_factories):
        cube.remove_aux_factory(factory)


def layer_heat_content(
    cube: Cube,
    z_min: float = 0.0,
    z_max: float | None = None,
) -> Cube:
    """Heat content per unit area of the layer ``[z_min, z_max]`` from ``thetao``.

    ``z_max = None`` is the full column. Returns a cube with the layer as a
    **single** vertical level (bounds ``[z_min, z_max]``, clipped to the
    deepest cell) in J m-2, named like ESMValCore's ``phcint``, so
    :class:`~climateeval.diags.simple.OceanHeatContentTimeSeries` sums it
    over depth (a no-op) and area exactly as it would sum ``phcint``.

    A column is masked where every cell of the layer is masked (land); cells
    below the sea floor contribute nothing. Lazy data stay lazy.
    """
    import dask.array as da  # noqa: PLC0415

    z_coord = vertical_coord(cube)
    (z_dim,) = cube.coord_dims(z_coord)
    bounds = depth_bounds_m(z_coord)
    thickness = layer_thickness(bounds, z_min, z_max)
    wet = np.flatnonzero(thickness > 0.0)
    if wet.size == 0:
        msg = (
            f"No level of {cube.summary(shorten=True)} overlaps the layer "
            f"[{z_min}, {z_max}] m"
        )
        raise ValueError(msg)
    # Levels are monotonic in depth, so the overlapping ones are contiguous.
    keep = slice(int(wet.min()), int(wet.max()) + 1)
    index: list[slice] = [slice(None)] * cube.ndim
    index[z_dim] = keep
    cube = cube[tuple(index)]
    thickness = thickness[keep]
    layer_top = max(float(z_min), float(bounds[keep].min()))
    layer_bottom = float(np.max(bounds[keep])) if z_max is None else min(
        float(z_max),
        float(np.max(bounds[keep])),
    )

    cube = cube.copy()
    cube.convert_units("K")
    data = da.asarray(cube.core_data())
    shape = [1] * cube.ndim
    shape[z_dim] = thickness.size
    weighted = data * (RHO_CP * thickness).reshape(shape)
    total = da.sum(da.ma.filled(weighted, 0.0), axis=z_dim, keepdims=True)
    all_masked = da.all(da.ma.getmaskarray(weighted), axis=z_dim, keepdims=True)
    layer = da.ma.masked_array(total, mask=all_masked)

    index[z_dim] = slice(0, 1)
    result = cube[tuple(index)].copy(data=layer)
    _drop_auxiliary_coords(result, z_dim)
    level = result.coord(vertical_coord(result))
    level.units = "m"
    level.points = np.array([0.5 * (layer_top + layer_bottom)])
    level.bounds = np.array([[layer_top, layer_bottom]])
    # what makes iris (and so the diagnostic's axis_statistics(axis="Z"))
    # recognise it as the vertical axis; MIROC's files carry no `positive`
    level.attributes["positive"] = "down"
    result.var_name = PHCINT_VAR_NAME
    result.standard_name = PHCINT_STANDARD_NAME
    result.long_name = PHCINT_LONG_NAME
    result.units = PHCINT_UNITS
    return result
