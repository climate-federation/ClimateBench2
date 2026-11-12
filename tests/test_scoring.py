"""Tests for the ClimateBench v2 scoring engine (climatebench2.scoring)."""

from __future__ import annotations

import numpy as np
import pytest

from climatebench2 import scoring

# ---------------------------------------------------------------------------
# Fair (Ferro) CRPS
# ---------------------------------------------------------------------------


def test_crps_fair_two_member_analytic() -> None:
    # For members {a, b} and obs y: fair CRPS = mean(|a-y|, |b-y|) - |a-b|/2
    # (the spread term is normalised by 2M(M-1) = 4, over the 2 ordered pairs)
    members = np.array([[0.0], [2.0]])
    obs = np.array([3.0])
    expected = (3.0 + 1.0) / 2 - 2.0 / 2
    np.testing.assert_allclose(scoring.crps_fair(members, obs), [expected])


def test_crps_fair_spread_term_is_larger_than_the_empirical_one() -> None:
    """The fair estimator subtracts M/(M-1) times the empirical spread."""
    rng = np.random.default_rng(11)
    members = rng.normal(size=(4, 7))
    obs = rng.normal(size=7)
    mae = np.abs(members - obs).mean(axis=0)
    empirical_spread = 0.5 * np.abs(
        members[:, None, :] - members[None, :, :],
    ).mean(axis=(0, 1))
    fair = scoring.crps_fair(members, obs)
    np.testing.assert_allclose(fair, mae - empirical_spread * 4 / 3)


def test_crps_fair_single_member_raises() -> None:
    """M = 1 is undefined — never |x - y| (paper: deterministic baselines)."""
    with pytest.raises(ValueError, match="undefined"):
        scoring.crps_fair(np.array([[1.0, 2.0]]), np.array([1.5, 2.0]))
    with pytest.raises(ValueError, match="undefined"):
        scoring.crps_fair(np.array([1.0, 2.0, 3.0]), 2.0)  # 1-D -> one member


def test_crps_fair_is_independent_of_ensemble_size() -> None:
    """Fairness: a calibrated ensemble scores the same for M = 2 and M = 20.

    Members and observations are i.i.d. N(0, 1), so the expected fair CRPS is
    E|X-Y| - 0.5 E|X-X'| = 0.5 * 2/sqrt(pi) for any M >= 2, while the
    empirical estimator is biased high for small M.
    """
    rng = np.random.default_rng(12)
    n_time = 40000
    obs = rng.normal(size=n_time)
    analytic = 0.5 * 2.0 / np.sqrt(np.pi)
    scores = {}
    for n_members in (2, 20):
        members = rng.normal(size=(n_members, n_time))
        scores[n_members] = scoring.crps_fair(members, obs).mean()
    assert scores[2] == pytest.approx(analytic, abs=0.02)
    assert scores[20] == pytest.approx(analytic, abs=0.02)
    assert scores[2] == pytest.approx(scores[20], abs=0.03)


def test_crps_fair_rewards_calibrated_spread() -> None:
    """A calibrated ensemble beats both a collapsed and an over-dispersed one."""
    rng = np.random.default_rng(1)
    n_time, n_members = 2000, 50
    truth = rng.normal(0.0, 1.0, n_time)
    calibrated = rng.normal(0.0, 1.0, (n_members, n_time))
    collapsed = np.zeros((n_members, n_time))
    overdispersed = rng.normal(0.0, 4.0, (n_members, n_time))
    s_cal = scoring.crps_fair(calibrated, truth).mean()
    s_col = scoring.crps_fair(collapsed, truth).mean()
    s_over = scoring.crps_fair(overdispersed, truth).mean()
    assert s_cal < s_col
    assert s_cal < s_over


def test_crps_fair_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="time steps"):
        scoring.crps_fair(np.zeros((3, 5)), np.zeros(4))


# ---------------------------------------------------------------------------
# Observational-uncertainty draws
# ---------------------------------------------------------------------------


def test_obs_draws_with_zero_sigma_reproduce_crps_fair_exactly() -> None:
    rng = np.random.default_rng(13)
    members = rng.normal(size=(5, 30))
    obs = rng.normal(size=30)
    np.testing.assert_array_equal(
        scoring.crps_fair_with_obs_draws(members, obs, obs_sigma=0.0),
        scoring.crps_fair(members, obs),
    )
    # n_draws < 1 also short-circuits
    np.testing.assert_array_equal(
        scoring.crps_fair_with_obs_draws(members, obs, obs_sigma=0.5, n_draws=0),
        scoring.crps_fair(members, obs),
    )


def test_obs_draws_are_common_to_every_forecast() -> None:
    """The same seed must give the same pseudo-observations for every model."""
    rng = np.random.default_rng(14)
    obs = rng.normal(size=25)
    members_a = rng.normal(size=(4, 25))
    members_b = rng.normal(size=(6, 25)) + 0.5
    sigma, n_draws, seed = 0.3, 16, 4242

    draws = np.random.default_rng(seed).standard_normal((n_draws, obs.size))
    for members in (members_a, members_b):
        expected = np.mean(
            [scoring.crps_fair(members, obs + sigma * d) for d in draws],
            axis=0,
        )
        np.testing.assert_allclose(
            scoring.crps_fair_with_obs_draws(
                members,
                obs,
                obs_sigma=sigma,
                n_draws=n_draws,
                seed=seed,
            ),
            expected,
        )


def test_obs_draws_of_a_perfect_forecast_approach_the_obs_error() -> None:
    """With the ensemble on the truth, the score is E|sigma Z| = sigma sqrt(2/pi)."""
    obs = np.zeros(200)
    members = np.zeros((5, 200))
    sigma = 2.0
    score = scoring.crps_fair_with_obs_draws(
        members,
        obs,
        obs_sigma=sigma,
        n_draws=400,
        seed=7,
    ).mean()
    assert score == pytest.approx(sigma * np.sqrt(2.0 / np.pi), rel=0.05)


def test_obs_draws_accept_a_per_timestep_sigma() -> None:
    obs = np.zeros(4)
    members = np.zeros((3, 4))
    sigma = np.array([0.0, 1.0, 2.0, 3.0])
    score = scoring.crps_fair_with_obs_draws(
        members,
        obs,
        obs_sigma=sigma,
        n_draws=500,
        seed=2,
    )
    assert score[0] == 0.0
    assert score[3] > score[2] > score[1] > 0.0


# ---------------------------------------------------------------------------
# Moving-block bootstrap
# ---------------------------------------------------------------------------


def _ar1(rng: np.random.Generator, n: int, phi: float, sigma: float = 1.0) -> np.ndarray:
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.normal(0.0, sigma)
    return x


def test_block_bootstrap_brackets_the_mean_and_widens_with_the_block() -> None:
    rng = np.random.default_rng(21)
    series = 1.0 + _ar1(rng, 480, 0.8)
    lo1, hi1 = scoring.moving_block_bootstrap_ci(series, block_length=1, seed=1)
    lo12, hi12 = scoring.moving_block_bootstrap_ci(series, block_length=12, seed=1)
    assert lo1 < series.mean() < hi1
    assert lo12 < series.mean() < hi12
    # Blocks preserve the serial correlation, so the interval must be wider
    assert (hi12 - lo12) > 1.5 * (hi1 - lo1)


def test_block_bootstrap_coverage_on_ar1() -> None:
    """Blocks restore most of the coverage an iid resample throws away.

    The moving block bootstrap is known to undercover somewhat at these
    sample sizes; what matters for the protocol is that it is far closer to
    nominal than resampling single steps, which ignores the serial
    correlation entirely.
    """
    rng = np.random.default_rng(22)
    phi, n_trials = 0.6, 200
    covered = {1: 0, 12: 0}
    for _ in range(n_trials):
        series = _ar1(rng, 240, phi)
        seed = int(rng.integers(1e6))
        for length in covered:
            lo, hi = scoring.moving_block_bootstrap_ci(
                series,
                block_length=length,
                n_boot=300,
                seed=seed,
            )
            covered[length] += int(lo <= 0.0 <= hi)
    assert 0.85 <= covered[12] / n_trials <= 0.99  # near nominal 0.95
    assert covered[12] > covered[1] + 0.1 * n_trials  # blocks matter


def test_block_bootstrap_with_member_resampling() -> None:
    rng = np.random.default_rng(23)
    obs = rng.normal(size=120)
    members = rng.normal(size=(8, 120))
    crps_t = scoring.crps_fair(members, obs)
    plain = scoring.moving_block_bootstrap_ci(
        crps_t,
        block_length=12,
        n_boot=200,
        seed=3,
    )
    with_members = scoring.moving_block_bootstrap_ci(
        crps_t,
        block_length=12,
        n_boot=200,
        seed=3,
        members=members,
        obs=obs,
    )
    assert plain[0] < crps_t.mean() < plain[1]
    assert with_members[0] < crps_t.mean() < with_members[1]
    # Member sampling is an extra source of uncertainty
    assert (with_members[1] - with_members[0]) > (plain[1] - plain[0])


def test_block_bootstrap_short_series_returns_nan() -> None:
    lo, hi = scoring.moving_block_bootstrap_ci(np.array([1.0]), block_length=12)
    assert np.isnan(lo)
    assert np.isnan(hi)


# ---------------------------------------------------------------------------
# ESS correction
# ---------------------------------------------------------------------------


def test_lag1_autocorrelation_of_ar1() -> None:
    rng = np.random.default_rng(2)
    phi = 0.7
    x = np.zeros(20000)
    for i in range(1, x.size):
        x[i] = phi * x[i - 1] + rng.normal()
    assert scoring.lag1_autocorrelation(x) == pytest.approx(phi, abs=0.03)


def test_effective_sample_size_white_noise_and_ar1() -> None:
    rng = np.random.default_rng(3)
    white = rng.normal(size=1000)
    assert scoring.effective_sample_size(white) == pytest.approx(1000, rel=0.15)

    phi = 0.6
    ar1 = np.zeros(1000)
    for i in range(1, ar1.size):
        ar1[i] = phi * ar1[i - 1] + rng.normal()
    expected = 1000 * (1 - phi) / (1 + phi)
    assert scoring.effective_sample_size(ar1) == pytest.approx(expected, rel=0.25)


def test_crps_ess_score_fields() -> None:
    rng = np.random.default_rng(4)
    members = rng.normal(0, 1, (10, 300))
    obs = rng.normal(0, 1, 300)
    result = scoring.crps_ess_score(members, obs)
    assert result.n_members == 10
    assert result.n_time == 300
    assert 1.0 <= result.t_eff <= 300.0
    assert result.score > 0
    assert result.standard_error > 0
    # ... and it is the fair estimator, summarised from the same series
    from_series = scoring.crps_ess_from_series(scoring.crps_fair(members, obs), 10)
    assert from_series == result


def test_crps_ess_score_single_member_raises() -> None:
    with pytest.raises(ValueError, match="undefined"):
        scoring.crps_ess_score(np.zeros((1, 10)), np.zeros(10))


# ---------------------------------------------------------------------------
# piControl chunking
# ---------------------------------------------------------------------------


def test_chunked_statistic_std_mean_and_trend() -> None:
    rng = np.random.default_rng(5)
    series = rng.normal(0.0, 2.0, 1000)
    sigma_mean = scoring.chunked_statistic_std(series, 50, "mean")
    # std of the mean of 50 iid N(0,2) values ~ 2/sqrt(50)
    assert sigma_mean == pytest.approx(2.0 / np.sqrt(50), rel=0.4)
    sigma_trend = scoring.chunked_statistic_std(series, 50, "trend")
    assert sigma_trend > 0

    with pytest.raises(ValueError, match="at least 2"):
        scoring.chunked_statistic_std(series, 600)
    with pytest.raises(ValueError, match="Unknown statistic"):
        scoring.chunked_statistic_std(series, 50, "median")


def test_ensemble_spread_sigma_mean_is_the_between_member_std() -> None:
    members = np.array([[1.0, 3.0], [2.0, 4.0], [5.0, 7.0], [0.0, 2.0]])  # means 2,3,6,1
    expected = np.std([2.0, 3.0, 6.0, 1.0], ddof=1)
    assert scoring.ensemble_spread_sigma(members, "mean") == pytest.approx(expected)


def test_ensemble_spread_sigma_trend_is_the_std_of_member_slopes() -> None:
    t = np.arange(10.0)
    slopes = np.array([0.1, 0.3, 0.2, 0.6])
    members = slopes[:, None] * t[None, :] + np.array([5.0, -1.0, 2.0, 0.0])[:, None]
    assert scoring.ensemble_spread_sigma(members, "trend") == pytest.approx(
        np.std(slopes, ddof=1),
    )


def test_ensemble_spread_sigma_needs_three_members() -> None:
    two = np.random.default_rng(0).normal(size=(2, 20))
    assert np.isnan(scoring.ensemble_spread_sigma(two, "mean"))
    assert np.isnan(scoring.ensemble_spread_sigma(two, "trend"))
    three = np.random.default_rng(0).normal(size=(3, 20))
    assert np.isfinite(scoring.ensemble_spread_sigma(three, "mean"))


def test_ensemble_spread_sigma_ignores_nans_and_drops_empty_members() -> None:
    members = np.array([[1.0, np.nan, 3.0], [2.0, 4.0, np.nan], [np.nan] * 3, [5.0, 7.0, 6.0]])
    # member means 2, 3, (dropped), 6
    assert scoring.ensemble_spread_sigma(members, "mean") == pytest.approx(
        np.std([2.0, 3.0, 6.0], ddof=1),
    )
    # only two members left with a usable value -> not an estimate
    assert np.isnan(scoring.ensemble_spread_sigma(members[:3], "mean"))


def test_ensemble_spread_sigma_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="Unknown statistic"):
        scoring.ensemble_spread_sigma(np.zeros((4, 5)), "median")
    with pytest.raises(ValueError, match="2-D"):
        scoring.ensemble_spread_sigma(np.zeros(5), "mean")


# ---------------------------------------------------------------------------
# Ensemble consistency
# ---------------------------------------------------------------------------


def test_ensemble_consistency_obvious_cases() -> None:
    ensemble = np.array([1.0, 1.1, 0.9, 1.05, 0.95])
    consistent = scoring.ensemble_consistency(ensemble, 1.0)
    assert consistent.passes
    assert abs(consistent.z) < 1.0

    inconsistent = scoring.ensemble_consistency(ensemble, 5.0)
    assert not inconsistent.passes
    assert inconsistent.p_value < 0.05


def test_ensemble_consistency_sigma_terms_widen_the_test() -> None:
    ensemble = np.array([1.0, 1.1, 0.9])
    obs = 2.0
    narrow = scoring.ensemble_consistency(ensemble, obs)
    wide = scoring.ensemble_consistency(ensemble, obs, sigma_internal=0.8)
    assert not narrow.passes
    assert wide.passes
    assert abs(wide.z) < abs(narrow.z)


def test_ensemble_consistency_calibration() -> None:
    """Under the null, the test should reject ~5% of the time."""
    rng = np.random.default_rng(6)
    rejections = 0
    n_trials = 2000
    for _ in range(n_trials):
        ensemble = rng.normal(0, 1, 20)
        obs = rng.normal(0, 1)
        if not scoring.ensemble_consistency(ensemble, obs).passes:
            rejections += 1
    rate = rejections / n_trials
    assert rate == pytest.approx(0.05, abs=0.025)


def test_ensemble_consistency_errors() -> None:
    with pytest.raises(ValueError, match="empty"):
        scoring.ensemble_consistency(np.array([np.nan]), 0.0)
    with pytest.raises(ValueError, match="zero"):
        scoring.ensemble_consistency(np.array([1.0]), 0.0)  # 1 member, no sigmas


# ---------------------------------------------------------------------------
# EOF projection
# ---------------------------------------------------------------------------


def _synthetic_fields(
    rng: np.random.Generator,
    n_samples: int,
    amp1: float = 3.0,
    amp2: float = 1.0,
) -> np.ndarray:
    """Fields = two orthogonal sinusoidal modes with random amplitudes."""
    x = np.linspace(0, 2 * np.pi, 60)
    mode1, mode2 = np.sin(x), np.cos(2 * x)
    a = rng.normal(0, amp1, n_samples)
    b = rng.normal(0, amp2, n_samples)
    return a[:, None] * mode1[None, :] + b[:, None] * mode2[None, :]


def test_eof_basis_recovers_leading_mode() -> None:
    rng = np.random.default_rng(7)
    fields = _synthetic_fields(rng, 200)
    basis = scoring.eof_basis(fields, n_modes=2)
    assert basis.eofs.shape == (2, 60)
    # Leading EOF should be the sin mode (up to sign/normalization)
    x = np.linspace(0, 2 * np.pi, 60)
    corr = np.corrcoef(basis.eofs[0], np.sin(x))[0, 1]
    assert abs(corr) > 0.99
    assert basis.explained_variance[0] > basis.explained_variance[1]


def test_project_onto_eofs_roundtrip_scale() -> None:
    rng = np.random.default_rng(8)
    fields = _synthetic_fields(rng, 100)
    basis = scoring.eof_basis(fields, n_modes=2)
    pcs = scoring.project_onto_eofs(basis, fields)
    assert pcs.shape == (100, 2)
    # PC variance should match the basis explained variance
    np.testing.assert_allclose(
        pcs.var(axis=0, ddof=1),
        basis.explained_variance,
        rtol=1e-8,
    )


def test_field_consistency_pass_and_fail() -> None:
    rng = np.random.default_rng(9)
    variability = _synthetic_fields(rng, 300)
    ensemble = _synthetic_fields(rng, 25)
    obs_ok = _synthetic_fields(rng, 1)[0]
    passes, results = scoring.field_consistency(
        ensemble,
        obs_ok,
        variability,
        n_modes=2,
    )
    assert passes
    assert len(results) == 2

    # An observation far outside the variability envelope must fail
    obs_bad = obs_ok + 100.0 * np.sin(np.linspace(0, 2 * np.pi, 60))
    fails, _ = scoring.field_consistency(ensemble, obs_bad, variability, n_modes=2)
    assert not fails


def test_eof_basis_validates_ndim() -> None:
    with pytest.raises(ValueError, match="2-D"):
        scoring.eof_basis(np.zeros(10), 2)


# ---------------------------------------------------------------------------
# Regime (b): variance-explained truncation, standardisation, projection
# ---------------------------------------------------------------------------


def test_eof_basis_truncates_by_variance_explained() -> None:
    """The protocol pre-registers a variance fraction, not a mode count."""
    rng = np.random.default_rng(11)
    fields = _synthetic_fields(rng, 400)  # two modes, amplitudes 3 and 1
    # 80% is reached by the leading mode alone (9 / 10 of the variance)
    lead = scoring.eof_basis(fields, variance_explained=0.8)
    assert lead.eofs.shape[0] == 1
    assert lead.explained_variance_ratio[0] > 0.8

    # 99% needs both
    both = scoring.eof_basis(fields, variance_explained=0.99)
    assert both.eofs.shape[0] == 2
    assert float(both.explained_variance_ratio.sum()) > 0.99

    # ... and the cap always wins
    capped = scoring.eof_basis(fields, variance_explained=0.99, max_modes=1)
    assert capped.eofs.shape[0] == 1


def test_eof_basis_records_the_sample_pc_sigma() -> None:
    """``pc_std`` is the protocol's pre-2015 observational sigma."""
    rng = np.random.default_rng(12)
    fields = _synthetic_fields(rng, 500)
    basis = scoring.eof_basis(fields, n_modes=2)
    np.testing.assert_allclose(
        basis.pc_std**2,
        basis.explained_variance,
        rtol=1e-8,
    )
    # The sample's amplitudes are 3 and 1; the absolute PC scale depends on
    # the (weighted, unit-norm) basis, but their ratio does not.
    assert basis.pc_std[0] / basis.pc_std[1] == pytest.approx(3.0, rel=0.15)


def test_standardised_coefficients_are_in_units_of_pre2015_sigma() -> None:
    """A field equal to one sigma of a mode gives that mode a coefficient 1."""
    rng = np.random.default_rng(13)
    fields = _synthetic_fields(rng, 500)
    basis = scoring.eof_basis(fields, n_modes=2)

    # Reconstruct one sigma of the leading mode in field space. The basis is
    # orthonormal in *weighted* space, so undo the weighting.
    one_sigma = basis.mean + basis.pc_std[0] * basis.eofs[0] / basis.weights
    coefficients = scoring.standardised_coefficients(basis, one_sigma)
    assert coefficients[0] == pytest.approx(1.0, rel=1e-6)
    assert abs(coefficients[1]) < 1e-6

    # The whole sample standardises to unit variance per mode
    sample = scoring.standardised_coefficients(basis, fields)
    np.testing.assert_allclose(sample.std(axis=0, ddof=1), np.ones(2), rtol=1e-8)


def test_standardised_coefficients_survive_a_degenerate_mode() -> None:
    """A zero-sigma mode is left unscaled rather than dividing by zero."""
    fields = np.tile(np.linspace(0.0, 1.0, 8), (4, 1))  # rank 0 after centring
    basis = scoring.eof_basis(fields, n_modes=1)
    coefficients = scoring.standardised_coefficients(basis, fields[0])
    assert np.isfinite(coefficients).all()


def test_cos_latitude_weights_match_the_area_weighting() -> None:
    lats = np.array([-60.0, 0.0, 60.0])
    weights = scoring.cos_latitude_weights(lats, 4)
    assert weights.shape == (12,)
    np.testing.assert_allclose(weights[:4], 0.5, atol=1e-12)
    np.testing.assert_allclose(weights[4:8], 1.0, atol=1e-12)


def test_crps_independent_summary_uses_the_sample_size_as_t_eff() -> None:
    """Modes are orthogonal: no serial correlation to discount."""
    values = np.array([0.1, 0.3, 0.2, 0.4, 0.15])
    summary = scoring.crps_independent_summary(values, n_members=4)
    assert summary.score == pytest.approx(values.mean())
    assert summary.t_eff == 5.0
    assert summary.n_time == 5
    assert summary.n_members == 4
    assert summary.standard_error == pytest.approx(
        values.std(ddof=1) / np.sqrt(5),
    )
    assert np.isnan(summary.r1)
    with pytest.raises(ValueError, match="no finite values"):
        scoring.crps_independent_summary(np.array([np.nan]), 2)


# ---------------------------------------------------------------------------
# Trend statistics for the regime-(c) consistency test
# ---------------------------------------------------------------------------


def test_ols_trend_recovers_a_known_slope() -> None:
    y = 2.0 + 0.03 * np.arange(40, dtype=float)
    assert scoring.ols_trend(y) == pytest.approx(0.03)
    y[5] = np.nan
    assert scoring.ols_trend(y) == pytest.approx(0.03)
    assert np.isnan(scoring.ols_trend(np.array([1.0, 2.0])))


def test_ols_trend_sigma_matches_a_monte_carlo() -> None:
    """sigma_step -> sigma of the OLS slope, for independent errors."""
    rng = np.random.default_rng(21)
    n, sigma_step = 30, 0.05
    slopes = [
        scoring.ols_trend(rng.normal(0.0, sigma_step, n)) for _ in range(4000)
    ]
    assert scoring.ols_trend_sigma(sigma_step, n) == pytest.approx(
        float(np.std(slopes, ddof=1)),
        rel=0.05,
    )
    # Degenerate inputs are 0, never NaN or a division by zero
    assert scoring.ols_trend_sigma(0.0, 30) == 0.0
    assert scoring.ols_trend_sigma(0.05, 2) == 0.0


# ---------------------------------------------------------------------------
# Perkins skill score (work package 6b)
# ---------------------------------------------------------------------------


def test_perkins_skill_score_identical_distributions_is_one() -> None:
    pdf = np.array([0.1, 0.4, 0.3, 0.2])
    assert scoring.perkins_skill_score(pdf, pdf) == pytest.approx(1.0)


def test_perkins_skill_score_disjoint_distributions_is_zero() -> None:
    model = np.array([1.0, 1.0, 0.0, 0.0])
    obs = np.array([0.0, 0.0, 1.0, 1.0])
    assert scoring.perkins_skill_score(model, obs) == pytest.approx(0.0)


def test_perkins_skill_score_is_the_overlap() -> None:
    model = np.array([0.5, 0.3, 0.2])
    obs = np.array([0.2, 0.3, 0.5])
    assert scoring.perkins_skill_score(model, obs) == pytest.approx(0.2 + 0.3 + 0.2)


def test_perkins_skill_score_normalises_densities_and_frequencies_alike() -> None:
    """A density (integral 1) and a frequency (sum 1) must score the same."""
    frequencies_m = np.array([0.1, 0.4, 0.3, 0.2])
    frequencies_o = np.array([0.25, 0.25, 0.25, 0.25])
    width = 0.5
    assert scoring.perkins_skill_score(
        frequencies_m / width,
        frequencies_o / width,
    ) == pytest.approx(scoring.perkins_skill_score(frequencies_m, frequencies_o))


def test_perkins_skill_score_handles_empty_and_mismatched_input() -> None:
    with pytest.raises(ValueError, match="common set of bins"):
        scoring.perkins_skill_score(np.zeros(3), np.zeros(4))
    assert np.isnan(scoring.perkins_skill_score(np.zeros(3), np.ones(3)))
    assert np.isnan(scoring.perkins_skill_score(np.full(3, np.nan), np.ones(3)))
    # a bin missing from one side is dropped from both
    assert scoring.perkins_skill_score(
        np.array([0.5, 0.5, np.nan]),
        np.array([0.5, 0.5, 0.2]),
    ) == pytest.approx(1.0)
