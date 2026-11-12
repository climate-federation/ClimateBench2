"""Tests for the Tier III scoring functions (proxy + perfect-model)."""

from __future__ import annotations

import numpy as np
import pytest

from climatebench2 import scoring


def test_proxy_site_consistency_perfect_and_poor_model() -> None:
    rng = np.random.default_rng(0)
    n_sites = 200
    truth = rng.normal(0, 2, n_sites)
    errors = np.full(n_sites, 1.0)
    proxies = truth + rng.normal(0, 1.0, n_sites)  # proxies = truth + error

    frac_good, z = scoring.proxy_site_consistency(truth, proxies, errors)
    assert frac_good > 0.9
    assert z.shape == (n_sites,)

    # A model off by 5 sigma everywhere fails nearly every site
    frac_bad, _ = scoring.proxy_site_consistency(truth + 5.0, proxies, errors)
    assert frac_bad < 0.1


def test_proxy_site_consistency_with_ensemble_and_nans() -> None:
    rng = np.random.default_rng(1)
    n_sites = 50
    truth = rng.normal(0, 1, n_sites)
    ensemble = truth[None, :] + rng.normal(0, 0.5, (8, n_sites))
    proxies = truth + rng.normal(0, 0.8, n_sites)
    errors = np.full(n_sites, 0.8)
    proxies[0] = np.nan  # excluded, not fatal
    frac, z = scoring.proxy_site_consistency(
        np.zeros(n_sites),
        proxies,
        errors,
        ensemble_values=ensemble,
    )
    assert 0.8 < frac <= 1.0
    assert np.isnan(z[0])

    with pytest.raises(ValueError, match="no valid proxy sites"):
        scoring.proxy_site_consistency(
            np.array([np.nan]),
            np.array([1.0]),
            np.array([1.0]),
        )


def test_sample_at_sites_nearest_and_wraparound() -> None:
    lats = np.array([-30.0, 0.0, 30.0])
    lons = np.array([0.0, 120.0, 240.0])
    field = np.arange(9, dtype=float).reshape(3, 3)
    # site at (2, 118): nearest (0, 120) -> row 1, col 1 -> 4
    # site at (-28, -20): lon -20 == 340 -> nearest col 0 (0) or 240? |340-0|=20 wraps, |340-240|=100 -> col 0
    sampled = scoring.sample_at_sites(
        field,
        lats,
        lons,
        np.array([2.0, -28.0]),
        np.array([118.0, -20.0]),
    )
    np.testing.assert_allclose(sampled, [4.0, 0.0])


def test_le_variance_ratio_and_pattern_correlation() -> None:
    rng = np.random.default_rng(2)
    n_members, n_space = 30, 500
    pattern = np.abs(np.sin(np.linspace(0, 3 * np.pi, n_space))) + 0.1
    truth = rng.normal(0, 1, (n_members, n_space)) * np.sqrt(pattern)

    # A perfect emulator of the spread
    pred_good = rng.normal(0, 1, (n_members, n_space)) * np.sqrt(pattern)
    ratio = scoring.le_variance_ratio(pred_good, truth)
    assert ratio == pytest.approx(1.0, abs=0.15)
    # 30-member variance estimates carry ~sqrt(2/29) sampling noise per
    # gridpoint, capping the recoverable pattern correlation well below 1
    corr = scoring.le_spread_pattern_correlation(pred_good, truth)
    assert corr > 0.5

    # A collapsed emulator: half the variance, flat pattern
    pred_flat = rng.normal(0, 0.7, (n_members, n_space))
    assert scoring.le_variance_ratio(pred_flat, truth) == pytest.approx(
        0.49 / pattern.mean(),
        rel=0.3,
    )
    assert scoring.le_spread_pattern_correlation(pred_flat, truth) < 0.3

    with pytest.raises(ValueError, match="zero variance"):
        scoring.le_variance_ratio(pred_good, np.zeros((3, 5)))


def test_tier3_diagnostics_importable_and_wired() -> None:
    climateeval = pytest.importorskip("climateeval")  # noqa: F841

    from climatebench2.diags import MidHoloceneMonsoonGate, PaleoProxyScore
    from climatebench2.diags.pass_fail import resolved_checks
    from climatebench2.diags.tier1_physics import CB2ComplexDiagnostic

    assert issubclass(MidHoloceneMonsoonGate, CB2ComplexDiagnostic)
    assert issubclass(PaleoProxyScore, CB2ComplexDiagnostic)
    (check,) = resolved_checks(MidHoloceneMonsoonGate)
    assert check.lower == 0.5  # mm/day, tier3.midholocene_monsoon
    # The monsoon gate is a TIER III Extended check, not a Tier I one: that
    # tag is what keeps it out of the Tier I table on the scorecard.
    assert (check.tier, check.requirement) == ("III", "extended")


# ---------------------------------------------------------------------------
# Block pseudo-members and the Tier III fair CRPS (WP7)
# ---------------------------------------------------------------------------


def test_nonoverlapping_blocks_drops_spinup_and_remainder() -> None:
    # 250 yr, 100 yr spin-up, 30 yr blocks -> 5 blocks (150 yr), 0 left over
    blocks = scoring.nonoverlapping_blocks(250, 30, spinup=100)
    assert [(b.start, b.stop) for b in blocks] == [
        (100, 130),
        (130, 160),
        (160, 190),
        (190, 220),
        (220, 250),
    ]
    # a 20-yr remainder is dropped, never scored as a short block
    assert len(scoring.nonoverlapping_blocks(270, 30, spinup=100)) == 5
    # too short for even one block
    assert scoring.nonoverlapping_blocks(120, 30, spinup=100) == []
    assert scoring.nonoverlapping_blocks(10, 30) == []
    with pytest.raises(ValueError, match="block_length"):
        scoring.nonoverlapping_blocks(100, 0)


def test_block_climatologies_are_per_block_means_over_the_leading_axis() -> None:
    # A field whose value is its year index: block means are the block centres
    years = np.arange(10, dtype=float)
    field = years[:, None, None] * np.ones((1, 2, 3))
    members = scoring.block_climatologies(field, 4, spinup=2)
    assert members.shape == (2, 2, 3)  # years 2-5 and 6-9
    np.testing.assert_allclose(members[:, 0, 0], [3.5, 7.5])

    # NaNs inside a block are ignored, not propagated
    field[3, 0, 0] = np.nan
    members = scoring.block_climatologies(field, 4, spinup=2)
    assert members[0, 0, 0] == pytest.approx((2 + 4 + 5) / 3)
    # no blocks at all -> an empty (0, ...) array, not an error
    assert scoring.block_climatologies(field, 40).shape == (0, 2, 3)


def test_proxy_crps_rewards_the_right_ensemble_and_drops_bad_sites() -> None:
    rng = np.random.default_rng(3)
    n_sites = 60
    truth = rng.normal(0, 3, n_sites)
    sigma = np.full(n_sites, 1.0)
    proxy = truth + rng.normal(0, 1.0, n_sites)
    good = truth[None, :] + rng.normal(0, 1.0, (8, n_sites))
    bad = good + 6.0

    score_good, per_site = scoring.proxy_crps(good, proxy, sigma, seed=1)
    score_bad, _ = scoring.proxy_crps(bad, proxy, sigma, seed=1)
    assert score_good.score < score_bad.score
    assert per_site.shape == (n_sites,)
    # sites are an unordered axis: r1 is NaN and T_eff is the site count
    assert np.isnan(score_good.r1)
    assert score_good.t_eff == n_sites
    assert score_good.n_members == 8

    # a site with no proxy value (or no sigma) is dropped from both
    proxy[0] = np.nan
    sigma[1] = np.nan
    score, per_site = scoring.proxy_crps(good, proxy, sigma, seed=1)
    assert np.isnan(per_site[0]) and np.isnan(per_site[1])
    assert score.n_time == n_sites - 2

    with pytest.raises(ValueError, match="no valid proxy sites"):
        scoring.proxy_crps(good, np.full(n_sites, np.nan), sigma)


def test_proxy_crps_uses_the_proxy_sigma_as_observational_uncertainty() -> None:
    """A larger proxy error must not make a good model look better."""
    members = np.array([[0.0, 0.1], [0.2, -0.1], [-0.1, 0.05]])
    proxy = np.zeros(2)
    tight, _ = scoring.proxy_crps(members, proxy, np.full(2, 0.01), seed=7)
    loose, _ = scoring.proxy_crps(members, proxy, np.full(2, 5.0), seed=7)
    assert loose.score > tight.score
