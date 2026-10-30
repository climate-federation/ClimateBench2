"""Packaged protocol data tables, and the staged CMIP6 comparison ensemble.

Anything a CB2 *score* needs that is neither a threshold nor a computation:
the annual effective-radiative-forcing series that drives the two-layer EBM
behind the pattern-scaling baseline (``erf_ar6_ssp245.csv``, read by
:func:`climatebench2.baselines.load_erf_series`).

Model output and observations never live here — they come from ClimateEval
DataSources (docs/climateeval_delineation_plan.md, the ownership test). Four
things that do:

* :class:`~climatebench2.data.cmip6_staged.StagedCMIP6HistoricalSSP245` — a
  *discovery policy*, not data: it finds the CMIP6 historical+SSP2-4.5
  comparison ensemble in a pre-staged root instead of globbing the pool, and
  applies the protocol's member policy and member cap itself
  (:func:`~climatebench2.data.cmip6_staged.select_members`). It
  yields ClimateEval data sources with ids identical to
  :class:`climateeval.data.CMIP6HistoricalSSP245`, which stays the documented
  alternative.
* :class:`~climatebench2.data.imerg.IMERG` — the daily-precipitation
  observational reference the paper's Table 2 names, which ClimateEval has no
  DataSource for. It is a plain :class:`climateeval.data.DataSource` over
  staged files and **belongs upstream**; it is here only because ClimateEval
  is a pinned dependency CB2 does not patch, and it is written so that moving
  it is a file copy.
* :class:`~climatebench2.data.modis.MODIS` — the monthly cloud reference
  (``clt``, ``clwvi``, ``clivi``, ``lwp``) from MODIS Aqua MYD08_M3, staged
  because ESMValTool's MODIS CMORizer output is in the unreadable Tier-3
  pool. Same shape as ``IMERG``, same "belongs upstream" caveat.
* :func:`~climatebench2.data.ocean_heat_content.layer_heat_content` — the
  ``thetao`` -> layer ocean heat content derivation (ESMValCore ``main``'s
  ``phcint``, which the pinned ESMValCore lacks), run as a suite variable's
  ``additional_preprocessors`` step so the submission, the references and the
  comparison ensemble all score OHC from ``thetao``. Belongs upstream too.
"""

from climatebench2.data.cmip6_staged import (
    STAGED_ROOT_ENV,
    StagedCMIP6HistoricalSSP245,
    staged_root,
)
from climatebench2.data.imerg import IMERG
from climatebench2.data.ocean_heat_content import layer_heat_content
from climatebench2.data.modis import MODIS

__all__ = [
    "IMERG",
    "MODIS",
    "STAGED_ROOT_ENV",
    "StagedCMIP6HistoricalSSP245",
    "layer_heat_content",
    "staged_root",
]
