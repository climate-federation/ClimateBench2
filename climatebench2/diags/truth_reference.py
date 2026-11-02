"""Perfect-model (III.2): a held-out ESM run standing in for observations.

metrics_reference.md §III.2. The protocol's perfect-model test trains a
submission on one ESM's historical output, asks it to predict that ESM's
SSP2-4.5 future, and scores the prediction with the **Tier II machinery**
against the held-out truth run — isolating model skill from observational
uncertainty, which is the whole point (the truth is known exactly, so
σ_obs = 0).

The design that costs the least code (delineation plan §5) is to change
*nothing* about the Tier II diagnostics and swap only their reference: a
suite's ``reference_data:`` names a ``DataSource`` class, so pointing it at
the truth run makes every scored diagnostic run unchanged.
:class:`LocalCMORReference` is that DataSource — a thin adapter over
``climateeval._loader.load_cmor_dir``, which is what
``climatebench2 score`` already uses for the submission itself.

**Upstream candidate.** ClimateEval has no DataSource for "a local CMOR
directory": every one of its sources downloads and CMORizes a published
product. A generic ``LocalCMORDataSource(path, information)`` belongs
upstream next to ``ESMValToolCMORizerDataSource``, and this class is the
prototype for it — it adds no preprocessing of its own, it only resolves a
path and hands the cubes to ``get_prepared_cube`` exactly as the base class
does.

Configuring it
--------------
A suite YAML names a *class*, which ClimateEval instantiates with no
arguments, so the directories are registered on the module before the suite
is built::

    paths = configure_truth(Path("/data/CESM2/ssp245"), {"r2i1p1f1": ...})
    # -> ["climatebench2.diags.truth_reference.LocalCMORReference",
    #     "climatebench2.diags.truth_reference.TruthMember_r2i1p1f1"]

The first is the reference; the rest are the truth model's **other ensemble
members**, which enter ``other_data`` so the scoring pass can run the
large-ensemble spread test against the submission's own spread. Every one of
them is tagged ``category = "truth"``, which is how the pass tells a truth
run from a CMIP6 comparison model (it must never enter ``E_ref``) and how it
knows to label the rows ``window = perfect-model``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

from loguru import logger

from climateeval._utils import get_prepared_cube
from climateeval.data import DataSource, DataSourceInformation

if TYPE_CHECKING:
    from pathlib import Path

    from iris.cube import Cube, CubeList

    from climateeval import Variable

#: ``data_sources.category`` of every truth run. Distinct from ``model`` (the
#: submission) and from the CMIP6 comparison category, so the scoring pass
#: can exclude it from ``E_ref`` and label its rows ``perfect-model``.
TRUTH_CATEGORY = "truth"

#: ``data_sources.name`` of the truth run — the held-out ESM. Overridden per
#: run by :func:`configure_truth` (``--truth-name CESM2``).
DEFAULT_TRUTH_NAME = "PerfectModelTruth"

#: Prefix of the generated per-member classes.
MEMBER_CLASS_PREFIX = "TruthMember_"

#: Label of the primary truth run (the one used as ``reference_data``).
PRIMARY_LABEL = ""

#: ``label -> directory`` of every registered truth run.
_PATHS: dict[str, Path] = {}

#: Name the truth runs are registered under.
_NAME: str = DEFAULT_TRUTH_NAME

#: ``(label, timerange) -> CubeList``: a truth directory is read once.
_CUBES: dict[tuple[str, str | None], Any] = {}

_LABEL_SAFE = re.compile(r"[^0-9A-Za-z_]")


class TruthNotConfiguredError(RuntimeError):
    """Raised when a truth DataSource runs without a registered directory."""


class LocalCMORReference(DataSource):
    """The held-out truth run, as a ClimateEval reference DataSource.

    Delegates to ``load_cmor_dir`` — the same loader
    ``climatebench2 score`` uses for the submission — and then to the base
    class's own ``get_prepared_cube``, so the truth passes through the
    identical unit conversion, masking and time extraction as any published
    reference product. Nothing is downloaded: :meth:`download` says so
    rather than reaching for the network.
    """

    #: Which registered truth run this class serves (``""`` = the primary
    #: one, used as the reference; a member label otherwise).
    _label: ClassVar[str] = PRIMARY_LABEL

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Build ``_information`` lazily, after :func:`configure_truth`."""
        if name == "_information":
            return DataSourceInformation(
                name=_NAME,
                category=TRUTH_CATEGORY,
                exp="held-out",
                variant=type(self)._label,  # noqa: SLF001
            )
        return super().__getattr__(name)

    @property
    def path(self) -> Path:
        """Directory this truth run reads from."""
        label = type(self)._label  # noqa: SLF001
        if label not in _PATHS:
            msg = (
                f"No truth directory registered for label '{label}'. The "
                f"perfect-model reference is configured at run time — pass "
                f"`climatebench2 score --truth DIR` (and --truth-member "
                f"LABEL=PATH), which calls configure_truth() before the "
                f"suite is built."
            )
            raise TruthNotConfiguredError(msg)
        return _PATHS[label]

    def download(self, variable: Variable, data_root_dir: Path | str) -> list[Path]:
        """Never: a truth run is supplied by the user, not downloaded."""
        msg = (
            f"{type(self).__name__} reads a local CMOR directory "
            f"({self.path}); there is nothing to download for {variable}"
        )
        raise TruthNotConfiguredError(msg)

    def _cubes(self, variable: Variable) -> CubeList:
        """Load (and cache) the truth directory for one time window."""
        from climateeval._loader import load_cmor_dir

        timerange = variable.timerange if variable.timerange != "*" else None
        key = (type(self)._label, timerange)  # noqa: SLF001
        if key not in _CUBES:
            logger.info(
                f"Loading perfect-model truth '{self.id}' from {self.path}"
                + (f" over {timerange}" if timerange else ""),
            )
            _CUBES[key] = load_cmor_dir(self.path, timerange=timerange)
        return _CUBES[key]

    def get_cube(
        self,
        data_root_dir: Path | str,  # noqa: ARG002 - the truth is not in the cache
        variable: Variable,
        *,
        download_missing_data: bool = True,  # noqa: ARG002
    ) -> Cube:
        """One variable of the truth run, preprocessed like any reference.

        A truth directory holding the variable at several frequencies
        (``Amon/pr`` + ``day/pr``) is narrowed to ``variable.frequency``
        first (:mod:`climatebench2.data.frequency`).
        """
        from climatebench2.data.frequency import select_frequency

        cubes = select_frequency(self._cubes(variable), variable.frequency)
        return get_prepared_cube(cubes, variable)


def _member_class(label: str) -> type[LocalCMORReference]:
    """Create (once) the DataSource class serving one truth member."""
    attribute = f"{MEMBER_CLASS_PREFIX}{_LABEL_SAFE.sub('_', label)}"
    existing = globals().get(attribute)
    if existing is not None:
        return existing
    created = type(
        attribute,
        (LocalCMORReference,),
        {
            "_label": label,
            "__doc__": f"Truth ensemble member '{label}' (perfect-model, III.2).",
            "__module__": __name__,
        },
    )
    globals()[attribute] = created
    return created


def class_path(source: type[LocalCMORReference]) -> str:
    """Dotted path a suite YAML can name, for one truth DataSource class."""
    return f"{source.__module__}.{source.__name__}"


def configure_truth(
    primary: Path,
    members: dict[str, Path] | None = None,
    *,
    name: str = DEFAULT_TRUTH_NAME,
) -> tuple[str, list[str]]:
    """Register the truth run(s); return their suite class paths.

    Parameters
    ----------
    primary:
        CMOR directory of the held-out truth run — the ``reference_data:``
        every scored Tier II variable is swapped to.
    members:
        ``label -> directory`` of the truth model's other ensemble members.
        They become ``other_data`` sources so the scoring pass can compare
        the submission's inter-member spread with the truth ensemble's
        (III.2's large-ensemble spread test).
    name:
        ``data_sources.name`` they are recorded under (e.g. ``CESM2``).

    Returns
    -------
    :
        ``(reference class path, [member class paths])``.
    """
    global _NAME  # noqa: PLW0603 - one module-level registry, set by the CLI
    _NAME = name
    _PATHS.clear()
    _CUBES.clear()
    _PATHS[PRIMARY_LABEL] = primary
    member_paths = []
    for label, path in (members or {}).items():
        _PATHS[label] = path
        member_paths.append(class_path(_member_class(label)))
    return class_path(LocalCMORReference), member_paths


def truth_is_configured() -> bool:
    """Whether a truth run has been registered in this process."""
    return PRIMARY_LABEL in _PATHS
