"""ClimateBench2 diagnostics — ClimateEval-compatible ``Diagnostic`` subclasses.

These plug into ClimateEval suites by dotted class path, e.g.::

    - name: ecs_gate
      diagnostic: climatebench2.diags.ECSGate

Modules (docs/climateeval_delineation_plan.md §4):

- ``pass_fail``      — Tier I gate machinery + gates over ClimateEval diagnostics
- ``_scoring``       — CRPS-with-ESS + ensemble-consistency primitives (Phase 2)
- ``tier1_physics``  — land-ocean, Arctic, aerosol ERF, MHT, ITCZ, ... (Phase 3)
- ``tier2_scores``   — probabilistic scoring diagnostics, regimes (a)/(b) (Phase 2)
- ``tier2_daily``    — the daily/sub-daily statistics: the ETCCDI extremes (as
                       in-sample scalars and as a held-out annual series), the
                       Perkins PDF skill and the diurnal first harmonic (WP6b)
- ``tier2_reference``— reference records outside the test window: the 1985-2014
                       baseline series and the fixed pre-2015 EOF basis (regime b)
- ``baselines``      — climatology persistence / EBM pattern scaling / MME (Phase 4)
- ``tier3_paleo``    — the Tier III fair CRPS against the pipeline's proxy
                       NetCDFs + the mid-Holocene monsoon check (WP7)
- ``truth_reference``— the perfect-model (III.2) truth DataSource: a held-out
                       ESM run standing in for the observations (WP7)

Every diagnostic subclasses ``climateeval.diags._base.Diagnostic`` (usually via
``SimpleDiagnostic`` or ``ComplexDiagnostic``) and takes its thresholds from
``climatebench2/thresholds.yml`` — never hard-code a bound.
"""

from __future__ import annotations

from climatebench2.diags.pass_fail import (
    ENSOGate,
    GateCheck,
    GateMixin,
    SupersetExperimentMixin,
    gate_requirement,
    gate_requirements,
    gate_tier,
    gate_tiers,
)
from climatebench2.diags.tier1_physics import (
    AerosolForcingGate,
    ArcticAmplificationGate,
    BjerknesGate,
    CB2ComplexDiagnostic,
    CCScalingGate,
    ClearSkyFeedbackGate,
    ClosureGate,
    ECSGate,
    EnergyBalanceGate,
    ITCZEFEGate,
    LandOceanWarmingGate,
    MeridionalHeatTransportGate,
)
from climatebench2.diags.tier1_extended import (
    Amip4xCO2ERFGate,
    ENSOTeleconnectionsGate,
    GeostrophicBalanceGate,
    GFMIPPatchGate,
    MJOGate,
    PrecipBuoyancyGate,
)
from climatebench2.diags.tier2_daily import (
    AnnualExtremeIndexSeries,
    DiurnalHarmonic,
    ETCCDIExtremes,
    PerkinsSkillScore,
    ScalarTableDiagnostic,
)
from climatebench2.diags.tier2_diagnostics import (
    HemisphericAsymmetryGate,
    LandAnnualTemperatureRange,
    ObservedScalarMixin,
    PinatuboResponseGate,
    RealizedWarmingLevel,
    SeasonalCloudRadiativeFeedback,
    SSTLowCloudCovariance,
)
from climatebench2.diags.tier3_paleo import (
    MidHoloceneMonsoonGate,
    PaleoProxyScore,
)
from climatebench2.diags.truth_reference import LocalCMORReference
from climatebench2.diags.tier2_reference import (
    ReferenceBaselineRecord,
    ReferenceEOFProjection,
)
from climatebench2.diags.tier2_scores import (
    InternalVariability,
    ScoredAnnualMaxTimeSeries,
    ScoredAnnualMeanTimeSeries,
    ScoredMonthlyMeanTimeSeries,
    ScoredOceanHeatContent,
    TrendConsistency,
)

__all__ = [
    "AerosolForcingGate",
    "Amip4xCO2ERFGate",
    "AnnualExtremeIndexSeries",
    "ArcticAmplificationGate",
    "BjerknesGate",
    "CB2ComplexDiagnostic",
    "CCScalingGate",
    "ClearSkyFeedbackGate",
    "ClosureGate",
    "DiurnalHarmonic",
    "ECSGate",
    "ENSOGate",
    "ENSOTeleconnectionsGate",
    "ETCCDIExtremes",
    "EnergyBalanceGate",
    "GFMIPPatchGate",
    "GateCheck",
    "GateMixin",
    "GeostrophicBalanceGate",
    "HemisphericAsymmetryGate",
    "ITCZEFEGate",
    "LandAnnualTemperatureRange",
    "InternalVariability",
    "LandOceanWarmingGate",
    "LocalCMORReference",
    "MJOGate",
    "MeridionalHeatTransportGate",
    "MidHoloceneMonsoonGate",
    "ObservedScalarMixin",
    "PaleoProxyScore",
    "PerkinsSkillScore",
    "PinatuboResponseGate",
    "PrecipBuoyancyGate",
    "RealizedWarmingLevel",
    "ReferenceBaselineRecord",
    "ReferenceEOFProjection",
    "SSTLowCloudCovariance",
    "ScalarTableDiagnostic",
    "ScoredAnnualMaxTimeSeries",
    "ScoredAnnualMeanTimeSeries",
    "ScoredMonthlyMeanTimeSeries",
    "ScoredOceanHeatContent",
    "SeasonalCloudRadiativeFeedback",
    "SupersetExperimentMixin",
    "TrendConsistency",
    "gate_requirement",
    "gate_requirements",
    "gate_tier",
    "gate_tiers",
]
