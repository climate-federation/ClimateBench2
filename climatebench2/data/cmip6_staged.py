"""The CMIP6 historical+SSP2-4.5 comparison ensemble, read from a staged root.

Why this exists
---------------
Tier II's ``E_ref`` is the median fair CRPS over the **CMIP6 comparison
models** (metrics_reference.md §II.0, paper §5.6), so every core variable's
``other_data`` has to resolve to a multi-member historical+SSP2-4.5 ensemble
over the reserved post-2015 test window. Upstream ClimateEval has exactly that
generator — :class:`climateeval.data.CMIP6HistoricalSSP245` (PR #44, merged on
the ``cb2-integration`` branch, still open upstream, so it is *not* in the
``b0e941c`` that ``pyproject.toml`` pins) — and it remains the **documented
alternative**: the data source ids this module produces are byte-identical to
its, so a database written either way scores the same and the two can be mixed.

What differs is only *discovery*:

* upstream globs the CMIP6 pool through ESMValCore
  (``Dataset(...).from_files()``), which takes on the order of half a minute
  **per variable per diagnostic instance** on a DKRZ-sized replica — a Tier II
  suite instantiates a dozen diagnostics over ten variables, so the discovery
  alone would dominate the run;
* upstream's ``ensemble: r*i1p1f1`` facet silently drops every model whose
  members carry a non-``f1`` forcing index (UKESM1-0-LL, CNRM-*, HadGEM3-* are
  all ``f2``/``f3``), and it has no member cap, so one large ensemble can
  outvote the whole multi-model spread in the ``E_ref`` median;
* upstream falls back to intake-esgf when a variable is missing locally, which
  on an HPC login/compute node means a network path that bypasses the local
  pool entirely.

This generator instead **enumerates directories** in a staged root and applies
the protocol's **member policy** itself (:func:`select_members`, paper §5.6 and
the Fig. 4 caption): members matching ``tier2.comparison_ensemble.
member_pattern`` (``r*i1p1f*`` — initialisation and physics index 1, **any**
forcing index ``f``), capped at the first
``tier2.comparison_ensemble.max_members_per_model`` (10) by realization number
per model. The policy used to live only in the out-of-repo staging script that
built the Levante root, so a root built any other way (a cloud bucket, a
mirror holding every member) silently gave a different ``E_ref``; it is now
protocol code with its bounds in ``thresholds.yml``. On a root that staging
script built it is a no-op — every model there already holds at most ten
``i1p1`` members — so the databases and the frozen comparison bundle made from
that root are unchanged.

The cap is applied **per model, before** looking at any variable, exactly as
the staging script did: a model's ensemble is its first ten qualifying members,
fixed once, and a variable that some of them lack is scored on fewer members
rather than on the next members in line that happen to carry it.

Where the staged root is
------------------------
``CLIMATEBENCH2_STAGED_CMIP6_ROOT``, if set; otherwise the ``--data-root`` that
``climatebench2 score`` passes to the diagnostics (``climatebench2._cli``
exports it into the environment for exactly this reason, because ClimateEval
instantiates a generator with no arguments and calls ``generate(variable)``
before the diagnostic's data root is known). Pointing the variable at a subset
of the root is how a smoke run limits the comparison ensemble without
restaging. A member is read from the root it was discovered in (falling back
to ``--data-root``), so the ensemble root and the reference root may be
separate directories, as they are for the v2 reference bundle.

Layout it expects — ClimateEval's own
:meth:`climateeval.data.DataSource._get_data_dir` layout::

    <root>/CMIP6_<model>_historical-ssp245_<member>/<frequency>/<var_name>/*.nc

A model/member/variable that has no such directory is simply not yielded, so a
variable staged for only some members gives a smaller ensemble for that
variable rather than an error. A member *directory* counts towards its model's
cap whatever it holds.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from loguru import logger

from climatebench2._thresholds import get_threshold
from climateeval.data import DataSourceGenerator, DataSourceInformation
from climateeval.data._esmvaltool_intake import ESMValToolIntakeDataSource

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from climateeval import Variable

#: Environment variable naming the staged comparison-ensemble root. Unset
#: means "the data root ``climatebench2 score`` was given".
STAGED_ROOT_ENV = "CLIMATEBENCH2_STAGED_CMIP6_ROOT"

#: ``exp`` of the concatenated historical+SSP2-4.5 record, as
#: :func:`climateeval.data._utils.facet_to_str` renders ``["historical",
#: "ssp245"]``. This string is half of the data source id, so it must not
#: drift from upstream's.
STAGED_EXP = "historical-ssp245"

#: The ``exp`` facet list itself (what upstream puts in
#: ``esmvaltool_dataset_facets``).
STAGED_EXP_FACET: tuple[str, ...] = ("historical", "ssp245")

#: Directory name of one staged member:
#: ``CMIP6_<model>_historical-ssp245_<member>``.
_SOURCE_DIR_RE = re.compile(
    rf"^CMIP6_(?P<model>.+)_{STAGED_EXP}_(?P<member>r\d+i\d+p\d+f\d+)$",
)

#: A variant label, ``r<realization>i<initialization>p<physics>f<forcing>``.
_VARIANT_RE = re.compile(r"^r(\d+)i(\d+)p(\d+)f(\d+)$")

#: A member pattern: the same four indices, each a number or ``*`` (any).
_MEMBER_PATTERN_RE = re.compile(r"^r(\d+|\*)i(\d+|\*)p(\d+|\*)f(\d+|\*)$")

#: A CMOR filename, ``<var>_<table>_<model>_<exp>_<member>_<grid>[_<time>].nc``.
_CMOR_FILE_RE = re.compile(
    r"^(?P<short_name>[^_]+)_(?P<mip>[^_]+)_(?P<dataset>[^_]+)_(?P<exp>[^_]+)_"
    r"(?P<ensemble>r\d+i\d+p\d+f\d+)_(?P<grid>[^_]+)(?:_(?P<time>[^_]+))?\.nc$",
)


def _parse_member_pattern(pattern: str) -> tuple[int | None, ...]:
    """``"r*i1p1f*"`` -> ``(None, 1, 1, None)``; ``None`` matches any index."""
    match = _MEMBER_PATTERN_RE.match(pattern)
    if match is None:
        msg = (
            f"member pattern {pattern!r} is not of the form r<R>i<I>p<P>f<F> "
            f"with each index a number or '*'"
        )
        raise ValueError(msg)
    return tuple(None if g == "*" else int(g) for g in match.groups())


def select_members(
    members: Iterable[str],
    *,
    pattern: str | None = None,
    cap: int | None = None,
) -> list[str]:
    """The members of ONE model that form its comparison ensemble, in order.

    The protocol's member policy (paper §5.6, Fig. 4 caption: "its
    ``r*i1p1f*`` members, capped at the first 10"): keep the variant labels
    that match ``pattern`` (default ``tier2.comparison_ensemble.
    member_pattern``), order them by **realization number** — numerically, so
    ``r2`` precedes ``r10`` — and keep the first ``cap`` (default
    ``tier2.comparison_ensemble.max_members_per_model``). Labels that are not
    variants at all are ignored.

    The semantics are those of ``select_members`` in the staging script that
    built the Levante root (``runs/staging_scripts/build_comparison_root.py``)
    exactly, ties included: two labels with the same realization number (e.g.
    ``r1i1p1f1`` and ``r1i1p1f2``) are separate members, ordered by name. The
    function is pure; it knows nothing of variables, so a caller that wants the
    cap applied before per-variable availability (as the generator does) gets
    it by construction.
    """
    if pattern is None:
        pattern = get_threshold("tier2.comparison_ensemble.member_pattern")
    if cap is None:
        cap = int(get_threshold("tier2.comparison_ensemble.max_members_per_model"))
    wanted = _parse_member_pattern(pattern)
    selected: list[tuple[int, str]] = []
    for member in set(members):
        match = _VARIANT_RE.match(member)
        if match is None:
            continue
        indices = tuple(int(g) for g in match.groups())
        if all(w is None or w == i for w, i in zip(wanted, indices, strict=True)):
            selected.append((indices[0], member))
    return [member for _realization, member in sorted(selected)[:cap]]


def staged_root(data_root_dir: Path | str | None = None) -> Path | None:
    """The staged comparison-ensemble root, or ``None`` if there is none.

    ``CLIMATEBENCH2_STAGED_CMIP6_ROOT`` wins over ``data_root_dir`` so a run
    can point discovery at a subset of a larger root.
    """
    from_env = os.environ.get(STAGED_ROOT_ENV, "").strip()
    root = Path(from_env) if from_env else None
    if root is None and data_root_dir is not None:
        root = Path(data_root_dir)
    if root is None or not root.is_dir():
        return None
    return root


#: ``(root, frequency, var_name) -> [(model, member, files)]``. A Tier II suite
#: instantiates ~7 diagnostics over ~10 variables and ClimateEval calls
#: ``generate`` once per (diagnostic, variable), so without this the same
#: directory walk — and the same few hundred ``Path.resolve()`` calls on
#: symlinks into the pool — would run ~70 times per member set.
_DISCOVERY_CACHE: dict[tuple[str, str, str], list[tuple[str, str, list[Path]]]] = {}

#: ``root -> [(model, member, source_dir)]`` that pass the member policy.
_MEMBER_CACHE: dict[str, list[tuple[str, str, Path]]] = {}

#: ``(root, model) -> institute``.
_INSTITUTE_CACHE: dict[tuple[str, str], str] = {}


def _institute(files: list[Path], model: str) -> str:
    """Institute of a model: from the staged file's DRS path, else CMOR tables.

    The staged files are symlinks into a CMIP6 DRS replica
    (``.../CMIP6/<activity>/<institute>/<model>/...``), which is the
    authoritative spelling for that pool. The institute is provenance only —
    it is not part of the data source id — so an empty string is harmless.
    """
    for path in files[:1]:
        try:
            parts = path.resolve().parts
        except OSError:  # pragma: no cover - broken symlink
            continue
        for index in reversed([i for i, p in enumerate(parts) if p == "CMIP6"]):
            if len(parts) > index + 3 and parts[index + 3] == model:
                return parts[index + 2]
    try:
        from esmvalcore.cmor.table import CMOR_TABLES  # noqa: PLC0415

        institutes = CMOR_TABLES["CMIP6"].institutes.get(model, [])
    except Exception:  # noqa: BLE001
        return ""
    return str(institutes[0]) if institutes else ""


def _facets_from_filename(files: list[Path]) -> dict[str, str]:
    """``mip``/``grid`` of a staged leaf, read off its CMOR filenames."""
    for path in files:
        match = _CMOR_FILE_RE.match(path.name)
        if match is not None:
            return {"mip": match["mip"], "grid": match["grid"]}
    return {}


def _required_var_names(variable: Variable) -> list[str]:
    """Variable names a member must carry for ``variable`` to be loadable.

    A **derived** variable (``rtnt`` = ``rsdt − rsut − rlut``) is never staged
    itself: :meth:`climateeval.data.DataSource.get_cube` derives it from the
    variables ESMValCore says it requires, and returns nothing unless *all* of
    them are present. Discovering on the variable's own directory would
    therefore yield an empty comparison ensemble for every derived variable —
    which is how ``rtnt`` silently lost its ``E_ref``.
    """
    if not variable.derived:
        return [variable.var_name]
    try:
        return [v.var_name for v in variable.get_required_variables()]
    except Exception:  # noqa: BLE001 - CMOR tables unavailable
        return [variable.var_name]


def _ensemble_members(root: Path) -> list[tuple[str, str, Path]]:
    """``[(model, member, source_dir)]`` of the comparison ensemble in ``root``.

    Every ``CMIP6_<model>_historical-ssp245_<member>`` directory is grouped by
    model and passed through :func:`select_members`, **before** any variable
    is looked at, so a model's members are the same for every variable.
    Sorted by directory name, the order the walk has always yielded.
    """
    key = str(root)
    if key in _MEMBER_CACHE:
        return _MEMBER_CACHE[key]
    by_model: dict[str, dict[str, Path]] = {}
    for source_dir in root.iterdir():
        match = _SOURCE_DIR_RE.match(source_dir.name)
        if match is None or not source_dir.is_dir():
            continue
        by_model.setdefault(match["model"], {})[match["member"]] = source_dir
    members: list[tuple[str, str, Path]] = []
    for model, dirs in by_model.items():
        selected = select_members(dirs)
        if len(selected) < len(dirs):
            logger.info(
                f"Comparison ensemble: {model} keeps {len(selected)} of the "
                f"{len(dirs)} members staged in {root} (member policy "
                f"tier2.comparison_ensemble)",
            )
        members.extend((model, member, dirs[member]) for member in selected)
    members.sort(key=lambda item: item[2].name)
    _MEMBER_CACHE[key] = members
    return members


def _discover(root: Path, variable: Variable) -> list[tuple[str, str, list[Path]]]:
    """``[(model, member, files)]`` staged for one variable, cached per root."""
    key = (str(root), variable.frequency, variable.var_name)
    if key in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE[key]
    var_names = _required_var_names(variable)
    found: list[tuple[str, str, list[Path]]] = []
    for model, member, source_dir in _ensemble_members(root):
        files: list[Path] = []
        for var_name in var_names:
            var_dir = source_dir / variable.frequency / var_name
            var_files = sorted(var_dir.glob("*.nc")) if var_dir.is_dir() else []
            if not var_files:
                files = []
                break
            files.extend(var_files)
        if files:
            found.append((model, member, files))
    _DISCOVERY_CACHE[key] = found
    return found


def is_ocean_3d(variable: Variable) -> bool:
    """Whether ``variable`` lives on ocean model levels (``thetao``, ``so``)."""
    return any(c.var_name == "olevel" for c in variable.coordinates)


#: ``var_name`` of a vertical axis in CMIP6 ocean files that iris cannot
#: identify as one (no ``positive`` attribute, no unit — MIROC's
#: ``ocean_sigma_z``).
_VERTICAL_VAR_NAMES = ("lev", "olevel", "depth", "deptht")


def normalise_vertical_names(cubes: Any, var_name: str) -> None:  # noqa: ANN401
    """Make an ocean field's vertical axis findable by ClimateEval.

    ClimateEval's ``check_coordinates`` finds the ``olevel`` axis by the name
    ``depth`` (the standard name of its ``lev`` coordinate) — iris matches
    ``coord.name()``, i.e. the standard name, else the long name, else the
    variable name. CMIP6 files read without ESMValCore's CMOR fixes often fail
    that: IPSL-CM6A-LR and the CNRM models call the axis "Vertical T levels",
    CESM2 gives it an invalid standard name that iris drops, MIROC an
    ``ocean_sigma_z`` long name; and MIROC also carries the sea-floor depth as
    a second coordinate named ``depth``, which makes the lookup ambiguous. The
    member would be dropped as missing data.

    So: the vertical dimension coordinate (iris' Z-axis guess, else a known
    vertical ``var_name``) gets ``standard_name = 'depth'``, and any other
    coordinate that would answer to ``depth`` loses that standard name. Only
    names change — never values or units (``layer_heat_content`` handles the
    units). Anything ambiguous is left for ClimateEval to reject.
    """
    for cube in cubes:
        if cube.var_name != var_name:
            continue
        vertical = cube.coords(axis="Z", dim_coords=True) or [
            c for c in cube.dim_coords if c.var_name in _VERTICAL_VAR_NAMES
        ]
        if len(vertical) != 1:
            continue
        axis = vertical[0]
        for other in cube.coords("depth"):
            if other is not axis:
                other.standard_name = None
                other.long_name = f"{other.var_name or 'depth'} (not the vertical axis)"
        if axis.name() != "depth":
            axis.standard_name = "depth"


class StagedMemberDataSource(ESMValToolIntakeDataSource):
    """One staged comparison member (a subclass per member is generated).

    Two differences from the upstream base, both about *where* and *how* the
    staged files are read, never what is computed from them:

    * the files are read from the staged root the member was **discovered**
      in (``_staged_root``), falling back to the diagnostic's data root. Since
      the v2 reference bundle lives apart from the staged ensemble
      (``--data-root`` = references, ``$CLIMATEBENCH2_STAGED_CMIP6_ROOT`` =
      ensemble), loading from the data root alone found no member at all and
      every comparison source was reported as "would need to be downloaded";
    * an ocean-level variable gets its vertical axis named the way ClimateEval
      expects (:func:`normalise_vertical_names`) before ClimateEval checks it.
    """

    #: The staged root the member was found in (set per generated subclass).
    _staged_root: ClassVar[Path | None] = None

    def _get_data_dir(self, data_root_dir: Path, variable: Variable) -> Path:
        """``<staged root>/<id>/<freq>/<var>`` if staged there, else the data root's."""
        if self._staged_root is not None:
            staged = Path(self._staged_root) / self.id / variable.frequency / variable.var_name
            if staged.is_dir():
                return staged
        return super()._get_data_dir(data_root_dir, variable)

    def get_cube(
        self,
        data_root_dir: Path | str,
        variable: Variable,
        *,
        download_missing_data: bool = True,
    ) -> Any:  # noqa: ANN401
        """Load as upstream does; ocean-level fields get their axis renamed first."""
        if variable.derived or not is_ocean_3d(variable):
            return super().get_cube(
                data_root_dir,
                variable,
                download_missing_data=download_missing_data,
            )
        from climateeval._utils import get_prepared_cube, load_cubes  # noqa: PLC0415
        from climateeval.data._base import _paths_within_timerange  # noqa: PLC0415

        paths = _paths_within_timerange(
            sorted(self._get_data_dir(Path(data_root_dir), variable).glob("*.nc")),
            variable.timerange,
        )
        if not paths:
            return super().get_cube(
                data_root_dir,
                variable,
                download_missing_data=download_missing_data,
            )
        cubes = load_cubes(paths)
        normalise_vertical_names(cubes, variable.var_name)
        return get_prepared_cube(cubes, variable)


def clear_discovery_cache() -> None:
    """Forget the staged-root directory walk (used by the tests)."""
    _DISCOVERY_CACHE.clear()
    _MEMBER_CACHE.clear()
    _INSTITUTE_CACHE.clear()


class StagedCMIP6HistoricalSSP245(DataSourceGenerator):
    """CMIP6 historical+SSP2-4.5 comparison ensemble from a staged data root.

    Yields one :class:`StagedMemberDataSource` (an
    :class:`~climateeval.data._esmvaltool_intake.ESMValToolIntakeDataSource`)
    subclass per staged ``(model, member)`` that passes the member policy
    (:func:`select_members`, applied per model before any variable is looked
    at) and actually carries the variable, with

    * ``name`` = the model, ``category`` = ``CMIP6``, ``exp`` =
      ``historical-ssp245``, ``variant`` = the member — i.e. the data source id
      ``CMIP6_<model>_historical-ssp245_<member>``, the same id upstream's
      :class:`climateeval.data.CMIP6HistoricalSSP245` produces, so
      :func:`climatebench2.scoring_pass.group_members` stacks the members of
      one model into one comparison ensemble;
    * ``esmvaltool_dataset_facets`` filled in, so a source that *is* missing
      locally can still be downloaded when ``--download`` is given.
    """

    #: Facets of the ensemble this generator stands for. Not used for
    #: discovery (that is the directory walk); they are what the yielded data
    #: sources carry, and what a ``--download`` fallback would use.
    esmvaltool_dataset_facets: ClassVar[dict[str, Any]] = {
        "project": "CMIP6",
        "exp": list(STAGED_EXP_FACET),
        "dataset": "*",
        "institute": "*",
        "ensemble": get_threshold("tier2.comparison_ensemble.member_pattern"),
        "grid": "*",
        "timerange": "19790101/*",
    }

    def __init__(self, data_root_dir: Path | str | None = None) -> None:
        """Remember an explicit root (ClimateEval instantiates with none)."""
        self._data_root_dir = data_root_dir

    def generate(
        self,
        variable: Variable,
    ) -> Generator[ESMValToolIntakeDataSource]:
        """Yield one data source per staged member carrying ``variable``."""
        root = staged_root(self._data_root_dir)
        if root is None:
            return

        for model, member, files in _discover(root, variable):
            institute_key = (str(root), model)
            if institute_key not in _INSTITUTE_CACHE:
                _INSTITUTE_CACHE[institute_key] = _institute(files, model)
            institute = _INSTITUTE_CACHE[institute_key]
            facets: dict[str, Any] = {
                **self.esmvaltool_dataset_facets,
                "dataset": model,
                "institute": institute or "*",
                "ensemble": member,
                "short_name": variable.var_name,
                "frequency": variable.frequency,
                "mip": variable.cmip6_table_id,
            }
            facets.update(_facets_from_filename(files))

            information = DataSourceInformation(
                name=model,
                category="CMIP6",
                institute=institute,
                exp=STAGED_EXP,
                variant=member,
                references=(
                    "https://www.wdc-climate.de/ui/cmip6?input=CMIP6.CMIP."
                    f"{institute}.{model}",
                    "https://www.wdc-climate.de/ui/cmip6?input=CMIP6.ScenarioMIP."
                    f"{institute}.{model}",
                ),
            )
            yield type(
                f"{self.__class__.__name__}DataSource",
                (StagedMemberDataSource,),
                {
                    "_information": information,
                    "esmvaltool_dataset_facets": facets,
                    "_staged_root": root,
                },
            )()
