"""ClimateBench v2 — the protocol layer on top of ClimateEval.

ClimateBench2 defines *how climate models are scored and tested* (tier
structure, pass/fail thresholds, probabilistic scoring, baselines, the
leaderboard). All data loading, preprocessing and generic physical
diagnostics are delegated to ClimateEval
(https://github.com/climate-federation/ClimateEval).

See ``docs/climateeval_delineation_plan.md`` for the ownership boundary and
``docs/metrics_reference.md`` for the exact spec of every protocol check.
"""

from __future__ import annotations

__version__ = "2.0.0"
