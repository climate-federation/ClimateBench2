"""Integration tests: CB2 suites parse and their diagnostics instantiate.

These need ClimateEval importable (run inside its pixi env, see tests/README.md);
they build every diagnostic from the CB2 suite YAMLs without loading any data.
"""

from __future__ import annotations

import pytest

climateeval = pytest.importorskip("climateeval")

from climateeval.suites import Suite  # noqa: E402

from climatebench2._cli import (  # noqa: E402
    DEFAULT_SUITES,
    SUITE_REGISTRY,
    _resolve_suite,
)


@pytest.mark.parametrize("suite_name", sorted(SUITE_REGISTRY))
def test_cb2_suites_build_all_diagnostics(suite_name: str) -> None:
    """Every suite CB2 ships — not only the CLI defaults — must be installable.

    Parametrised over the whole registry so the two non-default suites
    (``ClimateBench2_TierII_daily``, ``ClimateBench2_TierIII``) are covered
    too: they are packaged the same way and a missing ``suites/*.yml`` entry
    in ``pyproject.toml``'s package data would only ever show up here.
    """
    resolved = _resolve_suite(suite_name)
    assert resolved != suite_name, f"CB2 suite YAML not packaged for {suite_name}"
    suite = Suite(resolved)
    diagnostics = suite._get_diagnostics()
    assert diagnostics, f"suite {suite_name} built no diagnostics"
    for diag in diagnostics.values():
        # Every diagnostic must be a real ClimateEval Diagnostic
        from climateeval.diags._base import Diagnostic

        assert isinstance(diag, Diagnostic)


def test_every_default_suite_is_in_the_registry() -> None:
    """The CLI's defaults are a subset of the suites whose data shape is known."""
    assert set(DEFAULT_SUITES) <= set(SUITE_REGISTRY)
    # ... and the two that are not defaults are the ones that need extra data
    assert set(SUITE_REGISTRY) - set(DEFAULT_SUITES) == {
        "ClimateBench2_TierII_daily",  # needs day/ and sub-daily output
        "ClimateBench2_TierIII",  # needs the paleo experiments + proxy NetCDFs
    }


def test_packaged_protocol_data_is_importable() -> None:
    """`pyproject.toml`'s package data must cover everything read at run time."""
    from importlib.resources import files

    root = files("climatebench2")
    assert (root / "thresholds.yml").is_file()
    suites = {p.name for p in (root / "suites").iterdir() if p.name.endswith(".yml")}
    assert suites == {f"{name}.yml" for name in SUITE_REGISTRY}
    # data/*.csv — the packaged ERF table behind the pattern-scaling baseline
    assert (root / "data" / "erf_ar6_ssp245.csv").is_file()


def test_tier1_gates_have_thresholds_wired() -> None:
    from climatebench2.diags import ECSGate, ENSOGate
    from climatebench2.diags.pass_fail import resolved_checks

    (ecs_check,) = resolved_checks(ECSGate)
    assert (ecs_check.lower, ecs_check.upper) == (1.0, 7.0)

    amp, ratio = resolved_checks(ENSOGate)
    assert (amp.lower, amp.upper) == (0.5, 1.4)
    assert ratio.lower == 1.5
    assert ratio.upper is None


#: Entries of the Tier I suites that are inputs to Tier II rather than gates:
#: they emit numbers only and can never touch the entry ticket.
TIER1_NON_GATE_DIAGNOSTICS = {"internal_variability"}


def test_every_tier1_gate_carries_a_requirement_tag() -> None:
    """No gate may reach the scorecard without a Required/Extended/extra tag."""
    from climatebench2.diags.pass_fail import REQUIREMENT_TAGS, resolved_checks

    for suite_name in ("ClimateBench2_TierI", "ClimateBench2_TierI_variability"):
        suite = Suite(_resolve_suite(suite_name))
        for diag_name, diag in suite._get_diagnostics().items():
            checks = resolved_checks(diag)
            if diag_name in TIER1_NON_GATE_DIAGNOSTICS:
                assert not checks, f"{diag_name} must emit no pass/fail row"
                continue
            assert checks, f"{diag_name} defines no gate checks"
            for check in checks:
                assert check.requirement in REQUIREMENT_TAGS, (
                    f"{diag_name}/{check.check_id} has requirement "
                    f"'{check.requirement}'"
                )
                # Tier II event diagnostics do not belong in the Tier I suites
                assert check.requirement != "diagnostic", diag_name


def test_internal_variability_rides_in_tier1_without_gating() -> None:
    """σ_int comes from the piControl experiment, so it lives in Tier I.

    It is tagged ``diagnostic`` in thresholds.yml, emits no ``_gate_checks``
    and therefore contributes nothing to the entry ticket — the scoring pass
    reads its rows to fill σ_int in the regime-(c) consistency test.
    """
    from climatebench2.diags import InternalVariability

    diagnostics = Suite(_resolve_suite("ClimateBench2_TierI"))._get_diagnostics()
    diag = diagnostics["internal_variability"]
    assert isinstance(diag, InternalVariability)
    assert diag._required_data_keys == ("picontrol",)
    assert InternalVariability.requirement == "diagnostic"
    # ... and it is invisible to the leaderboard's Required set
    from climatebench2.diags.pass_fail import gate_requirements

    assert not [c for c, t in gate_requirements().items() if t == "diagnostic" and
                c.startswith("sigma_int")]


def test_tier2_event_gates_moved_out_of_the_tier1_suite() -> None:
    """Pinatubo / hemispheric asymmetry are Tier II diagnostics, not gates."""
    tier1 = Suite(_resolve_suite("ClimateBench2_TierI"))._get_diagnostics()
    assert "pinatubo" not in tier1
    assert "hemispheric_asymmetry" not in tier1

    from climatebench2.diags.pass_fail import resolved_checks

    events = Suite(_resolve_suite("ClimateBench2_TierII_events"))._get_diagnostics()
    assert set(events) == {
        "realized_warming_level",
        "pinatubo",
        "hemispheric_asymmetry",
        # the seasonal-cycle metrics of §II.1 (work package 6b)
        "land_temperature_range",
        "sst_low_cloud_covariance",
        "seasonal_cloud_feedback",
    }
    for diag in events.values():
        assert diag._required_data_keys == ("historical",)
        for check in resolved_checks(diag):
            assert check.requirement == "diagnostic"
    assert "ClimateBench2_TierII_events" in DEFAULT_SUITES


def test_realized_warming_level_emits_no_gate_row() -> None:
    """§II.1's primary scalar is scored, not gated: no pass/fail anywhere."""
    from climatebench2.diags import RealizedWarmingLevel
    from climatebench2.diags.pass_fail import gate_requirements

    assert RealizedWarmingLevel._gate_checks == ()
    assert RealizedWarmingLevel.requirement == "diagnostic"
    assert "gmst_warming_level" not in gate_requirements()


def test_tier2_suite_covers_the_protocol_variables() -> None:
    """Clear-sky TOA, CREs, condensed-water paths and both OHC layers are wired."""
    suite = Suite(_resolve_suite("ClimateBench2_TierII"))._get_diagnostics()
    core = {v.id for v in suite["annual_mean_timeseries"]._variables}
    assert {"rsutcs", "rlutcs", "swcre", "lwcre", "clt", "clwvi", "clivi"} <= core
    # ... and every one of them carries a reference, or the pass cannot score
    referenced = {v.id for v in suite["annual_mean_timeseries"]._reference_data}
    assert core <= referenced
    ohc = {v.id for v in suite["ohc"]._variables}
    assert {"phcint_total", "phcint_2000m", "phcint_100m"} <= ohc


#: The entries of the Tier II suite that carry the shared core-variable list
#: (the `*core_variables` anchor): every core variable is in all six.
CORE_ENTRIES = (
    "annual_mean_timeseries",
    "reference_baseline",
    "annual_cycle",
    "map",
    "eof_projection",
    "zonal_line",
)


def test_tier2_clouds_are_referenced_to_modis_and_cre_to_ceres() -> None:
    """2026-09-24: the clouds and the CREs join the paper's figure set.

    ESACCI-CLOUD ends in 2016, so the clouds had a two-year scored window;
    MODIS (staged) covers the whole test window. `swcre`/`lwcre` are the
    derived CERES-EBAF cloud-radiative effects. Every one of them sits in
    every core entry, with the CMIP6 comparison ensemble alongside.
    """
    from climatebench2.data import MODIS

    suite = Suite(_resolve_suite("ClimateBench2_TierII"))._get_diagnostics()
    expected = {
        "clt": MODIS,
        "clwvi": MODIS,
        "clivi": MODIS,
        "swcre": climateeval.data.CERESEBAF,
        "lwcre": climateeval.data.CERESEBAF,
    }
    for entry in CORE_ENTRIES:
        references = {v.id: s for v, s in suite[entry]._reference_data.items()}
        for var_id, source in expected.items():
            assert type(references[var_id]) is source, (entry, var_id)
        assert "ESACCI-CLOUD" not in {s.name for s in references.values()}

    # ... and every one carries the comparison ensemble (read off the YAML:
    # the generator yields nothing without a staged root)
    yaml = pytest.importorskip("yaml")
    with open(_resolve_suite("ClimateBench2_TierII"), encoding="utf-8") as stream:
        definition = yaml.safe_load(stream)
    for entry in definition:
        if entry["name"] not in CORE_ENTRIES:
            continue
        by_id = {v["id"]: v for v in entry["variables"]}
        for var_id in expected:
            assert by_id[var_id]["other_data"] == [
                "climatebench2.data.StagedCMIP6HistoricalSSP245",
            ], (entry["name"], var_id)


def test_the_cre_variables_derive_from_all_and_clear_sky_fluxes() -> None:
    """The CRE ids CB2 uses exist in ClimateEval's registry as derived variables.

    ESMValCore's derivations: swcre = rsutcs - rsut, lwcre = rlutcs - rlut.
    Those inputs are what both the CERES reference and a comparison member
    must have staged.
    """
    from climateeval import Variable

    required = {
        name: sorted(v.var_name for v in Variable(name, name, "mon").get_required_variables())
        for name in ("swcre", "lwcre")
    }
    assert required == {"swcre": ["rsut", "rsutcs"], "lwcre": ["rlut", "rlutcs"]}
    assert Variable("swcre", "swcre", "mon").derived
    assert Variable("lwcre", "lwcre", "mon").derived


def test_complex_gates_accept_the_not_applicable_kwarg() -> None:
    """Every experiment-based gate can be declared N/A by a submission."""
    for suite_name in ("ClimateBench2_TierI", "ClimateBench2_TierII_events"):
        suite = Suite(
            _resolve_suite(suite_name),
            diagnostic_kwargs={"not_applicable": ["geostrophic_balance"]},
        )
        diagnostics = suite._get_diagnostics()
        assert diagnostics
        for name, diag in diagnostics.items():
            assert diag.declared_not_applicable == (name == "geostrophic_balance")


def test_suite_registry_matches_the_actual_diagnostics() -> None:
    """The CLI's data-shape registry must agree with what each suite holds."""
    from climateeval.diags.complex._base import ComplexDiagnostic

    from climatebench2._cli import SUITE_REGISTRY

    for suite_name, spec in SUITE_REGISTRY.items():
        diagnostics = Suite(_resolve_suite(suite_name))._get_diagnostics()
        is_complex = [
            isinstance(diag, ComplexDiagnostic) for diag in diagnostics.values()
        ]
        assert any(is_complex) == (spec.shape == "experiments"), suite_name
        # Suites are homogeneous by data shape (Suite.get_database passes one
        # data object to every diagnostic).
        assert all(is_complex) == any(is_complex), suite_name


def test_unregistered_suites_are_classified_by_probing() -> None:
    from climateeval.diags.complex._base import ComplexDiagnostic

    from climatebench2._cli import _suite_spec

    tier1 = Suite(_resolve_suite("ClimateBench2_TierI"))._get_diagnostics()
    spec = _suite_spec("SomeOtherSuite", tier1, ComplexDiagnostic)
    assert spec.shape == "experiments"
    assert "not in the CB2 suite registry" in spec.note

    variability = Suite(
        _resolve_suite("ClimateBench2_TierI_variability"),
    )._get_diagnostics()
    spec = _suite_spec("SomeOtherSuite", variability, ComplexDiagnostic)
    assert (spec.shape, spec.window) == ("cubes", "historical")


#: The one held-out entry of the daily suite: the annual Rx1day/Rx5day
#: series scored over the reserved test window against IMERG. Everything
#: else there is a climatology of the historical record.
DAILY_HELD_OUT_ENTRIES = {"pr_extremes_series"}


def test_the_daily_suite_runs_over_the_full_record_per_member() -> None:
    """The extremes/PDF/diurnal statistics are in-sample, not test-window.

    The paper computes them "over the full historical record", so the daily
    suite is the one cube suite the CLI does not cut to a protocol window —
    and every entry in it is labelled in-sample in `tier2.window_labels`,
    except the annual index series, which is scored post-2015.
    """
    from climatebench2._cli import SUITE_REGISTRY, suite_timerange
    from climatebench2._thresholds import get_threshold

    spec = SUITE_REGISTRY["ClimateBench2_TierII_daily"]
    assert (spec.shape, spec.window, spec.per_member) == ("cubes", "full", True)
    # ... and not even an explicit --timerange re-cuts it
    assert suite_timerange(spec) is None
    assert suite_timerange(spec, "19790101/20141231") is None
    # A staged reference still narrows a variable of it, though: an
    # in-sample climatology has to be the same statistic for the model and
    # the observations (IMERG -> 2001-2014).
    assert spec.reference_overlap

    labels = get_threshold("tier2.window_labels")["diagnostics"]
    suite = Suite(_resolve_suite("ClimateBench2_TierII_daily"))._get_diagnostics()
    assert DAILY_HELD_OUT_ENTRIES <= set(suite)
    for name in suite:
        expected = (
            "held-out" if name in DAILY_HELD_OUT_ENTRIES else "in-sample"
        )
        assert labels.get(name, "held-out") == expected, name


def test_the_daily_extremes_score_pr_against_imerg_and_tas_not_at_all() -> None:
    """Precipitation has a daily observational product now; temperature does not.

    IMERG (paper Table 2) references the four precipitation indices. No
    ClimateEval DataSource serves daily `tasmax`/`tasmin`, so those stay
    model-only — in the scored shape, so the day HadEX3 is staged the suite
    needs one `reference_data:` line.
    """
    from climatebench2.data import IMERG
    from climatebench2.diags import ETCCDIExtremes, ScalarTableDiagnostic

    suite = Suite(_resolve_suite("ClimateBench2_TierII_daily"))._get_diagnostics()
    extremes = suite["extremes"]
    assert isinstance(extremes, ETCCDIExtremes)
    assert isinstance(extremes, ScalarTableDiagnostic)
    assert {v.var_name for v in extremes._variables} == {"tasmax", "tasmin", "pr"}
    referenced = {
        v.var_name: type(source)
        for v, source in extremes._reference_data.items()
    }
    assert referenced == {"pr": IMERG}
    # ETCCDI indices are land indices: the mask is ClimateEval's, per variable
    assert all(v.landsea_mask == "land_only" for v in extremes._variables)

    # ... and the daily intensity PDF is IMERG's too (paper Table 2), while
    # the Perkins temperature entries stay on ERA5.
    perkins = {
        v.id: type(source).__name__
        for v, source in suite["perkins"]._reference_data.items()
    }
    assert perkins["pr_intensity_land"] == "IMERG"
    assert perkins["pr_intensity_tropics"] == "IMERG"
    assert perkins["tas_anomaly_land"] == "ERA5Hourly"

    # ... whereas the diurnal harmonic keeps its hourly ERA5 reference
    assert suite["diurnal_harmonic"]._reference_data


def test_the_seasonal_metrics_read_their_boxes_from_thresholds() -> None:
    from climatebench2._thresholds import get_threshold
    from climatebench2.diags import (
        LandAnnualTemperatureRange,
        SeasonalCloudRadiativeFeedback,
        SSTLowCloudCovariance,
    )

    decks = get_threshold("tier2.seasonal.stratocumulus_regions")
    assert len(decks) == 5  # the five conventional stratocumulus decks
    for box in decks.values():
        assert len(box["lat"]) == 2 and len(box["lon"]) == 2

    suite = Suite(_resolve_suite("ClimateBench2_TierII_events"))._get_diagnostics()
    assert isinstance(suite["land_temperature_range"], LandAnnualTemperatureRange)
    assert isinstance(suite["sst_low_cloud_covariance"], SSTLowCloudCovariance)
    assert isinstance(
        suite["seasonal_cloud_feedback"],
        SeasonalCloudRadiativeFeedback,
    )
    # reported numbers, never gates
    for name in (
        "land_temperature_range",
        "sst_low_cloud_covariance",
        "seasonal_cloud_feedback",
    ):
        assert suite[name]._gate_checks == ()
        assert suite[name].requirement == "diagnostic"
