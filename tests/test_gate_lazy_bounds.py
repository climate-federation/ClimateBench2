"""CB-2: gate bounds, ``requirement`` and ``tier`` resolve at evaluation time.

Nothing may read ``thresholds.yml`` at import or class-definition time,
because the layer stack (``set_threshold_layers`` / ``use_thresholds``) can
change after ``climatebench2.diags`` is imported.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("climateeval")

from climatebench2._thresholds import use_thresholds  # noqa: E402
from climatebench2.diags import (  # noqa: E402
    ClearSkyFeedbackGate,
    ClosureGate,
    ECSGate,
    InternalVariability,
    MeridionalHeatTransportGate,
)
from climatebench2.diags.pass_fail import (  # noqa: E402
    Band,
    GateCheck,
    Threshold,
    gate_metrics,
    gate_requirements,
    resolved_checks,
)


def test_ecs_bounds_follow_the_active_layer_stack() -> None:
    (packaged,) = resolved_checks(ECSGate)
    assert (packaged.lower, packaged.upper) == (1.0, 7.0)

    with use_thresholds({"tier1": {"ecs": {"range": [2, 3]}}}):
        (inside,) = resolved_checks(ECSGate)
        assert (inside.lower, inside.upper) == (2.0, 3.0)
        # an instance sees the same layers
        (via_instance,) = resolved_checks(ECSGate("ecs"))
        assert (via_instance.lower, via_instance.upper) == (2.0, 3.0)

    (after,) = resolved_checks(ECSGate)
    assert (after.lower, after.upper) == (1.0, 7.0)


def test_gate_class_attribute_holds_lazy_references_not_numbers() -> None:
    (raw,) = ECSGate._gate_checks
    assert raw.lower == Threshold(".range", 0)
    assert raw.upper == Threshold(".range", 1)
    assert raw.block == "tier1.ecs"
    assert raw.requirement is None  # resolved from the block at evaluation time


def test_requirement_and_tier_resolve_from_the_block_at_evaluation_time() -> None:
    overlay = {"tier1": {"ecs": {"requirement": "extended", "tier": "II"}}}
    with use_thresholds(overlay):
        (check,) = resolved_checks(ECSGate)
        assert (check.requirement, check.tier) == ("extended", "II")
        assert gate_requirements()["ecs_gate"] == "extended"
    (check,) = resolved_checks(ECSGate)
    assert (check.requirement, check.tier) == ("required", "I")


def test_gate_metrics_evaluates_against_the_active_layers() -> None:
    raw = pd.DataFrame({"data_id": ["m"], "data_type": ["to_benchmark"], "ecs": [2.5]})
    row = gate_metrics(raw, ECSGate._gate_checks).iloc[0]  # unresolved checks
    assert (row["bound_lower"], row["bound_upper"], row["passes"]) == (1.0, 7.0, 1.0)
    with use_thresholds({"tier1": {"ecs": {"range": [3, 4]}}}):
        row = gate_metrics(raw, resolved_checks(ECSGate)).iloc[0]
        assert (row["bound_lower"], row["bound_upper"], row["passes"]) == (3.0, 4.0, 0.0)


def test_class_attribute_requirement_is_lazy() -> None:
    assert InternalVariability.requirement == "diagnostic"
    overlay = {"tier2": {"internal_variability": {"requirement": "extra"}}}
    with use_thresholds(overlay):
        assert InternalVariability.requirement == "extra"
    assert InternalVariability.requirement == "diagnostic"


def test_band_edges_match_reference_plus_minus_tolerance() -> None:
    lower, upper = resolved_checks(ClearSkyFeedbackGate)[0].lower, resolved_checks(
        ClearSkyFeedbackGate,
    )[0].upper
    assert (lower, upper) == (pytest.approx(1.425), pytest.approx(2.375))
    bounds = {c.check_id: (c.lower, c.upper) for c in resolved_checks(MeridionalHeatTransportGate)}
    assert bounds["amet_peak_lat"] == (38.5, 43.5)
    absolute = Band(Threshold("a.ref"), Threshold("a.tol"), "upper", relative=False)
    with use_thresholds({"a": {"ref": 10, "tol": 2}}):
        assert absolute.resolve() == 12.0


def test_relative_threshold_needs_a_block() -> None:
    with pytest.raises(ValueError, match="needs a GateCheck.block"):
        GateCheck(check_id="x", column="x", lower=Threshold(".min")).resolve()


# -- _threshold_block ---------------------------------------------------------

AMIP_LAYER = {
    "amip": {
        "ecs": {"range": [0.5, 9.0], "requirement": "extended", "tier": "II"},
        "water_budget": {"p_minus_e_abs_max": 0.5, "requirement": "required", "tier": "I"},
        "atm_energy_budget": {"residual_abs_max": 7.0, "requirement": "extra", "tier": "I"},
    },
}


def test_threshold_block_string_repoints_a_single_block_gate() -> None:
    class AmipECSGate(ECSGate):
        _threshold_block = "amip.ecs"

    with use_thresholds(AMIP_LAYER):
        (check,) = resolved_checks(AmipECSGate)
        assert (check.lower, check.upper) == (0.5, 9.0)
        assert (check.requirement, check.tier, check.block) == ("extended", "II", "amip.ecs")
        # the parent is untouched
        (parent,) = resolved_checks(ECSGate)
        assert (parent.lower, parent.upper) == (1.0, 7.0)
        # the instance reads its own block for run-time parameters too
        assert AmipECSGate("ecs")._threshold("tier1.ecs.range") == [0.5, 9.0]


def test_threshold_block_mapping_repoints_each_block_of_a_multi_block_gate() -> None:
    class AmipClosureGate(ClosureGate):
        _threshold_block = {
            "tier1.water_budget": "amip.water_budget",
            "tier1.atm_energy_budget": "amip.atm_energy_budget",
        }

    with use_thresholds(AMIP_LAYER):
        checks = {c.check_id: c for c in resolved_checks(AmipClosureGate)}
    assert checks["water_budget"].upper == 0.5
    assert checks["atm_energy_budget"].upper == 7.0
    assert checks["atm_energy_budget"].requirement == "extra"
    assert {c.check_id: c.upper for c in resolved_checks(ClosureGate)} == {
        "water_budget": 0.05,
        "atm_energy_budget": 2.0,
    }


def test_a_repointed_block_missing_a_bound_is_an_error_not_a_silent_fallback() -> None:
    class Broken(ECSGate):
        _threshold_block = "amip.nowhere"

    with pytest.raises(KeyError):
        resolved_checks(Broken)


# -- nothing is read at import time -------------------------------------------


def test_importing_the_package_reads_no_threshold() -> None:
    """A fresh interpreter whose threshold loader raises still imports everything."""
    code = textwrap.dedent(
        """
        import importlib, pkgutil
        import climatebench2._thresholds as t

        def boom(*args, **kwargs):
            raise RuntimeError("threshold read at import time")

        t.load_thresholds = boom
        import climatebench2
        for info in pkgutil.walk_packages(climatebench2.__path__, "climatebench2."):
            if info.name.startswith("climatebench2.analysis"):
                continue
            importlib.import_module(info.name)
        import climatebench2.diags
        print("IMPORT-OK")
        """,
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "IMPORT-OK" in result.stdout, result.stderr[-3000:]


def test_resolving_a_numpy_free_check_is_idempotent() -> None:
    (check,) = resolved_checks(ECSGate)
    assert check.resolve() == check
    assert not np.isnan(check.lower)
