"""The ClimateBench v2 probabilistic scoring engine (pure functions).

Implements the Tier II scoring machinery of the protocol
(docs/metrics_reference.md, Tier II preamble):

(a) **Time-resolved quantities** — **fair** (Ferro) CRPS of an ensemble
    against an observed series, time-averaged, with an effective-sample-size
    (lag-1 autocorrelation) correction on the *uncertainty* of that average
    and a moving-block bootstrap confidence interval.

(b) **Aggregated diagnostics and spatial fields** — fair CRPS in a projected
    basis: the EOFs of the *reference*, fixed over its pre-2015 record and
    truncated by a pre-registered variance-explained criterion
    (``eof_basis``), each coefficient standardised by its pre-2015 σ
    (``EOFBasis.pc_std``) and scored with the same fair CRPS
    (``crps_independent_summary`` for the equal-weight mean over
    coefficients).

(c) **Ensemble-consistency test** (complementary diagnostic) — is the
    observed value consistent with the model-ensemble distribution, whose
    spread combines ensemble spread, internal variability (piControl chunks)
    and observational uncertainty in quadrature; two-sided test at p < 0.05.
    Spatial fields are first projected onto a small number of EOFs and each
    PC tested (Bonferroni-corrected).

Everything here is numpy/scipy only — no ClimateEval imports, no protocol
constants (those live in ``thresholds.yml`` and are passed in by the caller)
— so the engine is unit-testable anywhere. The pass that applies it to a
results database is ``climatebench2.scoring_pass``.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy import stats

_MIN_FAIR_MEMBERS = 2

# ---------------------------------------------------------------------------
# Regime (a): fair CRPS with effective-sample-size correction
# ---------------------------------------------------------------------------


def crps_fair(members: np.ndarray, obs: np.ndarray) -> np.ndarray:
    """Fair (Ferro) CRPS of an ensemble forecast, per time step.

    The protocol's primary probabilistic score throughout Tiers II and III
    (metrics_reference.md, Tier II preamble)::

        CRPS_fair(x_1..M, y) = (1/M) Σ_i |x_i − y|
                             − (1/(2M(M−1))) Σ_i Σ_j |x_i − x_j|

    Note the ``M(M−1)`` normalisation of the spread term: the double sum runs
    over all ordered pairs, so pairs with ``i = j`` (which contribute zero)
    are excluded from the average. This makes the *expected* score of a
    calibrated ensemble independent of the ensemble size, so submissions with
    3 and with 50 members are directly comparable; the empirical estimator
    (``M²`` normalisation) penalises small ensembles.

    Parameters
    ----------
    members:
        Ensemble values, shape ``(n_members, n_time)`` (or ``(n_members,)``
        for a single time).
    obs:
        Observed values, shape ``(n_time,)`` (or scalar).

    Returns
    -------
    :
        Fair CRPS per time step, shape ``(n_time,)``.

    Raises
    ------
    ValueError
        If fewer than two members are given: **fair CRPS is undefined for a
        deterministic forecast** and must never fall through to |x − y|
        (paper §5.3). Deterministic references are handled explicitly by the
        caller (``climatebench2.scoring_pass``).
    """
    members = np.atleast_2d(np.asarray(members, dtype=float))  # (m, t)
    obs = np.atleast_1d(np.asarray(obs, dtype=float))  # (t,)
    n_members = members.shape[0]
    if n_members < _MIN_FAIR_MEMBERS:
        msg = (
            f"fair CRPS is undefined for {n_members} member(s): it needs at "
            f"least {_MIN_FAIR_MEMBERS} (a deterministic forecast has no "
            f"spread term). Handle M = 1 explicitly rather than falling "
            f"through to the absolute error."
        )
        raise ValueError(msg)
    if members.shape[1] != obs.shape[0]:
        msg = (
            f"members has {members.shape[1]} time steps but obs has "
            f"{obs.shape[0]}"
        )
        raise ValueError(msg)
    mae_term = np.abs(members - obs[None, :]).mean(axis=0)
    # Σ_i Σ_j |x_i − x_j| over all ordered pairs, per time step; the fair
    # normalisation 1/(2M(M−1)) is the average over the i ≠ j pairs only.
    pairwise = np.abs(members[:, None, :] - members[None, :, :]).sum(axis=(0, 1))
    spread_term = pairwise / (2.0 * n_members * (n_members - 1))
    return mae_term - spread_term


def crps_fair_with_obs_draws(
    members: np.ndarray,
    obs: np.ndarray,
    *,
    obs_sigma: float | np.ndarray = 0.0,
    n_draws: int = 100,
    seed: int = 0,
) -> np.ndarray:
    """Fair CRPS averaged over draws of the observational uncertainty.

    The protocol's treatment of observational error (metrics_reference.md
    Tier II, "observational variance term"): instead of widening the
    forecast, the score is averaged over pseudo-observations
    ``y_k ~ N(obs, σ_obs)``::

        CRPS_obs = (1/K) Σ_k CRPS_fair(x_1..M, y_k)

    The draws are generated from ``seed`` alone, so a submission and its
    baselines — scored with the same seed, the same ``n_draws`` and the same
    aligned time axis — see the **identical** pseudo-observations and remain
    directly comparable.

    ``obs_sigma`` may be a scalar or a per-time-step array. With
    ``obs_sigma = 0`` (or ``n_draws < 1``) this reproduces :func:`crps_fair`
    exactly, with no random numbers drawn at all.
    """
    obs = np.atleast_1d(np.asarray(obs, dtype=float))
    sigma = np.broadcast_to(np.asarray(obs_sigma, dtype=float), obs.shape)
    if n_draws < 1 or not np.any(sigma > 0.0):
        return crps_fair(members, obs)
    rng = np.random.default_rng(seed)
    draws = rng.standard_normal((int(n_draws), obs.size))
    total = np.zeros(obs.size)
    for draw in draws:
        total += crps_fair(members, obs + sigma * draw)
    return total / float(n_draws)


def lag1_autocorrelation(x: np.ndarray) -> float:
    """Lag-1 autocorrelation of a series (NaNs dropped, mean removed)."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 3:  # noqa: PLR2004 - need at least 3 points for a meaningful r1
        return 0.0
    x = x - x.mean()
    denom = float(np.sum(x * x))
    if denom == 0.0:
        return 0.0
    return float(np.sum(x[:-1] * x[1:]) / denom)


def effective_sample_size(x: np.ndarray) -> float:
    """T_eff = T (1 − r₁) / (1 + r₁), clipped to [1, T]."""
    x = np.asarray(x, dtype=float)
    n = int(np.isfinite(x).sum())
    if n == 0:
        return 0.0
    r1 = np.clip(lag1_autocorrelation(x), -0.999, 0.999)
    return float(np.clip(n * (1.0 - r1) / (1.0 + r1), 1.0, n))


@dataclass(frozen=True)
class CRPSScore:
    """Time-averaged CRPS with ESS-corrected uncertainty."""

    score: float
    standard_error: float
    t_eff: float
    r1: float
    n_members: int
    n_time: int


def crps_ess_from_series(crps_t: np.ndarray, n_members: int) -> CRPSScore:
    """Regime-(a) summary of an already-computed per-time-step CRPS series.

    ``score = (1/T) Σ_t CRPS_t``; ``SE = std(CRPS_t) / sqrt(T_eff)`` where
    T_eff uses the lag-1 autocorrelation of the CRPS series (the ESS
    correction applies to the *uncertainty*, never to the point score).
    """
    valid = np.asarray(crps_t, dtype=float)
    valid = valid[np.isfinite(valid)]
    if valid.size == 0:
        msg = "CRPS series has no finite values"
        raise ValueError(msg)
    t_eff = effective_sample_size(valid)
    se = float(valid.std(ddof=1) / np.sqrt(t_eff)) if valid.size > 1 else np.nan
    return CRPSScore(
        score=float(valid.mean()),
        standard_error=se,
        t_eff=t_eff,
        r1=lag1_autocorrelation(valid),
        n_members=int(n_members),
        n_time=int(valid.size),
    )


def crps_ess_score(members: np.ndarray, obs: np.ndarray) -> CRPSScore:
    """Regime-(a) score of an ensemble: mean **fair** CRPS with its ESS SE.

    Raises ``ValueError`` for a single member (see :func:`crps_fair`).
    """
    members = np.atleast_2d(np.asarray(members, dtype=float))
    return crps_ess_from_series(crps_fair(members, obs), members.shape[0])


def crps_independent_summary(crps_k: np.ndarray, n_members: int) -> CRPSScore:
    """Summary of a CRPS sample whose entries are already independent.

    Regime (b): the score axis is the set of **orthogonal** EOF coefficients,
    not time, so there is no serial correlation to discount — the effective
    sample size *is* the number of retained modes and the standard error is
    the plain ``std / sqrt(K)``. (Using :func:`crps_ess_from_series` here
    would compute a lag-1 autocorrelation along an axis — mode index — that
    has no ordering.) ``r1`` is reported as NaN to say so.
    """
    valid = np.asarray(crps_k, dtype=float)
    valid = valid[np.isfinite(valid)]
    if valid.size == 0:
        msg = "CRPS sample has no finite values"
        raise ValueError(msg)
    se = float(valid.std(ddof=1) / np.sqrt(valid.size)) if valid.size > 1 else np.nan
    return CRPSScore(
        score=float(valid.mean()),
        standard_error=se,
        t_eff=float(valid.size),
        r1=float("nan"),
        n_members=int(n_members),
        n_time=int(valid.size),
    )


# ---------------------------------------------------------------------------
# Regime (a): moving-block bootstrap confidence interval
# ---------------------------------------------------------------------------


def paired_block_indices(
    n_steps: int,
    block_length: int,
    n_boot: int,
    seed: int,
) -> np.ndarray:
    """Moving-block bootstrap index draws, from a seed alone.

    Returns ``(n_boot, n_steps)`` integer indices into a series of ``n_steps``
    steps: ``ceil(n_steps / L)`` blocks of ``L`` consecutive steps
    (``L = clip(block_length, 1, n_steps)``) with start positions drawn
    uniformly from ``[0, n_steps - L]`` (**non-circular**, as in
    :func:`moving_block_bootstrap_ci`), concatenated and cut to ``n_steps``.

    The draw depends on ``(n_steps, block_length, n_boot, seed)`` and nothing
    else — in particular not on how many ensemble members an entry has — so
    one array can be applied to the per-step scores of **every** entry on the
    same step axis, which is what a paired comparison (a rank bootstrap)
    needs: ``scores[indices].mean(axis=1)`` is that entry's bootstrap
    distribution, resampled identically to every other entry's.
    """
    n_steps = int(n_steps)
    if n_steps < 1:
        msg = f"n_steps must be at least 1, got {n_steps}"
        raise ValueError(msg)
    length = int(np.clip(block_length, 1, n_steps))
    n_blocks = int(np.ceil(n_steps / length))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n_steps - length + 1, size=(int(n_boot), n_blocks))
    idx = starts[:, :, None] + np.arange(length)[None, None, :]
    return idx.reshape(int(n_boot), n_blocks * length)[:, :n_steps]


def moving_block_bootstrap_ci(
    crps_t: np.ndarray,
    *,
    block_length: int,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
    members: np.ndarray | None = None,
    obs: np.ndarray | None = None,
) -> tuple[float, float]:
    """Percentile CI of the time-mean CRPS from a moving-block bootstrap.

    The per-time-step CRPS series is serially correlated, so the interval is
    built from **moving blocks** of ``block_length`` consecutive steps
    (protocol defaults in ``thresholds.yml`` ``tier2.bootstrap``: 12 steps —
    one year — for a monthly series, 3 for an annual one). ``ceil(T/L)``
    blocks are drawn with replacement and concatenated to length ``T``, and
    the mean of each resample forms the bootstrap distribution.

    When ``members`` and ``obs`` are given, each replicate **also resamples
    the ensemble members** with replacement and recomputes the fair CRPS
    from the resampled ensemble, so the interval covers ensemble-sampling as
    well as time-sampling uncertainty. Member resampling costs
    ``O(n_boot · M² · T)``; pass only ``crps_t`` to skip it.

    Returns
    -------
    :
        ``(lo, hi)``, the ``alpha/2`` and ``1 − alpha/2`` percentiles of the
        resampled means. ``(nan, nan)`` for a series shorter than two steps.
    """
    series = np.asarray(crps_t, dtype=float)
    series = series[np.isfinite(series)]
    n_time = series.size
    if n_time < 2:  # noqa: PLR2004 - a CI needs at least two points
        return (float("nan"), float("nan"))
    length = int(np.clip(block_length, 1, n_time))
    n_blocks = int(np.ceil(n_time / length))
    rng = np.random.default_rng(seed)
    offsets = np.arange(length)

    def block_index(size: int) -> np.ndarray:
        starts = rng.integers(0, n_time - length + 1, size=(size, n_blocks))
        idx = starts[:, :, None] + offsets[None, None, :]
        return idx.reshape(size, n_blocks * length)[:, :n_time]

    resample_members = (
        members is not None
        and obs is not None
        # Member resampling recomputes the series, so it needs the raw series
        # to be gap-free (otherwise the block indices no longer line up).
        and np.asarray(crps_t, dtype=float).size == n_time
    )
    if not resample_members:
        # The seed alone fixes the draw (same first rng call as `block_index`).
        means = series[paired_block_indices(n_time, block_length, n_boot, seed)].mean(
            axis=1,
        )
    else:
        ensemble = np.atleast_2d(np.asarray(members, dtype=float))
        n_members = ensemble.shape[0]
        means = np.empty(int(n_boot))
        for i in range(int(n_boot)):
            picked = rng.integers(0, n_members, size=n_members)
            replicate = crps_fair(ensemble[picked], obs)
            means[i] = replicate[block_index(1)[0]].mean()
    lo, hi = np.percentile(means, [100 * alpha / 2.0, 100 * (1.0 - alpha / 2.0)])
    return (float(lo), float(hi))


# ---------------------------------------------------------------------------
# Regime (b): ensemble-consistency test
# ---------------------------------------------------------------------------


def chunked_statistic_std(
    series: np.ndarray,
    chunk_length: int,
    statistic: str = "mean",
) -> float:
    """Internal-variability σ of a statistic from non-overlapping chunks.

    Chops a (piControl) series into ``chunk_length`` segments, evaluates the
    statistic on each, and returns the inter-chunk standard deviation — the
    protocol's estimate of internal variability for an observation-length
    diagnostic.

    ``statistic``: ``"mean"`` or ``"trend"`` (OLS slope per step).
    """
    x = np.asarray(series, dtype=float)
    n_chunks = x.size // chunk_length
    if n_chunks < 2:  # noqa: PLR2004 - need >= 2 chunks for a std
        msg = (
            f"series of length {x.size} gives {n_chunks} chunk(s) of "
            f"{chunk_length}; need at least 2"
        )
        raise ValueError(msg)
    values = []
    t = np.arange(chunk_length, dtype=float)
    for i in range(n_chunks):
        chunk = x[i * chunk_length : (i + 1) * chunk_length]
        if statistic == "mean":
            values.append(np.nanmean(chunk))
        elif statistic == "trend":
            mask = np.isfinite(chunk)
            values.append(stats.linregress(t[mask], chunk[mask]).slope)
        else:
            msg = f"Unknown statistic '{statistic}'"
            raise ValueError(msg)
    return float(np.std(values, ddof=1))


def ensemble_spread_sigma(members: np.ndarray, statistic: str) -> float:
    """Internal-variability σ of a statistic from the ensemble's own spread.

    ``members`` is a ``members x time`` array (one global series per
    ensemble member over the scored window). The statistic — ``"mean"`` (the
    window mean) or ``"trend"`` (OLS slope per step, as in
    :func:`chunked_statistic_std`) — is evaluated per member and the result
    is the **between-member** standard deviation (``ddof=1``). Non-finite
    values are ignored within a member; a member with no finite value (or,
    for the trend, fewer than two) is dropped. Fewer than 3 usable members
    give ``nan``: a spread from two members is not an estimate.

    This is the ``ensemble_spread`` source of
    ``tier2.internal_variability.source`` (an AMIP ensemble has no piControl
    to chop into chunks).
    """
    if statistic not in ("mean", "trend"):
        msg = f"Unknown statistic '{statistic}'"
        raise ValueError(msg)
    x = np.asarray(members, dtype=float)
    if x.ndim != 2:  # noqa: PLR2004 - members x time
        msg = f"members must be a 2-D (members x time) array, got shape {x.shape}"
        raise ValueError(msg)
    t = np.arange(x.shape[1], dtype=float)
    values: list[float] = []
    for series in x:
        mask = np.isfinite(series)
        if statistic == "mean":
            if mask.any():
                values.append(float(series[mask].mean()))
        elif mask.sum() >= 2:  # noqa: PLR2004 - a slope needs two points
            values.append(float(stats.linregress(t[mask], series[mask]).slope))
    if len(values) < 3:  # noqa: PLR2004 - minimum ensemble for a spread
        return float("nan")
    return float(np.std(values, ddof=1))


def perkins_skill_score(pdf_model: np.ndarray, pdf_obs: np.ndarray) -> float:
    """Perkins skill score: the overlap of two binned distributions.

    ``S = Σ_b min(f_model,b, f_obs,b)`` over a **common** set of bins
    (Perkins et al. 2007), the protocol's PDF-shape statistic for daily
    temperature anomalies and wet-day precipitation intensity
    (metrics_reference.md §II.1 "Daily tas extremes / pr intensity PDF").
    ``S = 1`` is a perfect overlap and ``S = 0`` disjoint distributions.

    Both inputs are renormalised to sum to one first, so bin *frequencies*
    (Σf = 1) and bin *densities* (∫f dx = 1, what ESMValCore's ``histogram``
    with ``normalization="integral"`` returns) give the same answer as long as
    the bins are the pre-registered common ones (``tier2.perkins.bins``). A
    bin that is NaN in either distribution is dropped from both.

    Unlike fair CRPS this is a **skill score, not an error**: it is reported
    in its own column and never enters ``E_ref`` or ``S = 1 − E/E_ref``.
    """
    model = np.asarray(pdf_model, dtype=float)
    obs = np.asarray(pdf_obs, dtype=float)
    if model.shape != obs.shape:
        msg = f"Perkins score needs a common set of bins, got {model.shape} and {obs.shape}"
        raise ValueError(msg)
    valid = np.isfinite(model) & np.isfinite(obs)
    if not valid.any():
        return float("nan")
    model = model[valid]
    obs = obs[valid]
    model_sum = model.sum()
    obs_sum = obs.sum()
    if model_sum <= 0.0 or obs_sum <= 0.0:
        return float("nan")
    return float(np.minimum(model / model_sum, obs / obs_sum).sum())


def ols_trend(y: np.ndarray) -> float:
    """OLS slope of a series against its own index (units per step).

    The protocol's warming-rate statistic for an annual series: slope per
    year. NaNs are dropped; fewer than three finite points give NaN.
    """
    values = np.asarray(y, dtype=float)
    index = np.arange(values.size, dtype=float)
    mask = np.isfinite(values)
    if mask.sum() < 3:  # noqa: PLR2004 - a trend needs at least three points
        return float("nan")
    return float(stats.linregress(index[mask], values[mask]).slope)


def ols_trend_sigma(sigma_step: float, n_time: int) -> float:
    """σ of an OLS slope given independent per-step errors of σ_step.

    ``Var(slope) = σ² / Σ(t − t̄)²`` and, for the evenly spaced index
    ``0…T−1``, ``Σ(t − t̄)² = T(T²−1)/12`` — hence
    ``σ_slope = σ_step · sqrt(12 / (T(T²−1)))``.

    This is how CB2 turns a **per-time-step** observational uncertainty into
    the σ_obs of a **trend** for the regime-(c) consistency test; the
    protocol states the quadrature sum but not this conversion, so it is a
    documented CB2 interpretation (metrics_reference.md §II.0).
    """
    n = int(n_time)
    if n < 3 or sigma_step <= 0.0:  # noqa: PLR2004
        return 0.0
    return float(sigma_step * np.sqrt(12.0 / (n * (n**2 - 1))))


@dataclass(frozen=True)
class ConsistencyResult:
    """Result of the regime-(c) ensemble-consistency test."""

    z: float
    p_value: float
    passes: bool
    ensemble_mean: float
    total_sigma: float


def ensemble_consistency(
    ensemble_values: np.ndarray,
    observed_value: float,
    *,
    sigma_internal: float = 0.0,
    sigma_obs: float = 0.0,
    p_threshold: float = 0.05,
) -> ConsistencyResult:
    """Two-sided consistency of an observation with an ensemble distribution.

    σ_total² = var(ensemble) + σ_internal² + σ_obs²;
    z = (obs − mean(ensemble)) / σ_total; pass iff two-sided p ≥ p_threshold.

    A single-member "ensemble" is allowed when σ_internal/σ_obs carry the
    spread (var(ensemble) is then 0).
    """
    values = np.asarray(ensemble_values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        msg = "ensemble_values is empty"
        raise ValueError(msg)
    var_ens = float(values.var(ddof=1)) if values.size > 1 else 0.0
    total_var = var_ens + sigma_internal**2 + sigma_obs**2
    if total_var <= 0.0:
        msg = "total variance is zero: no ensemble spread and no sigma terms"
        raise ValueError(msg)
    mu = float(values.mean())
    z = (float(observed_value) - mu) / float(np.sqrt(total_var))
    p_value = float(2.0 * stats.norm.sf(abs(z)))
    return ConsistencyResult(
        z=z,
        p_value=p_value,
        passes=p_value >= p_threshold,
        ensemble_mean=mu,
        total_sigma=float(np.sqrt(total_var)),
    )


# ---------------------------------------------------------------------------
# EOF projection for spatial fields (regime b)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EOFBasis:
    """Leading EOFs of a variability sample.

    ``explained_variance`` is the absolute variance of each retained mode;
    ``explained_variance_ratio`` its share of the *total* variance of the
    sample (all modes, retained or not), which is what the protocol's
    truncation criterion is expressed in. ``pc_std`` is the standard
    deviation of the sample's own principal-component series — for a basis
    built on the reference's pre-2015 record this is the protocol's
    "pre-2015 observational standard deviation", the divisor that
    standardises every coefficient before it is scored.
    """

    eofs: np.ndarray  # (n_modes, n_space)
    explained_variance: np.ndarray  # (n_modes,)
    mean: np.ndarray  # (n_space,)
    weights: np.ndarray  # (n_space,)
    explained_variance_ratio: np.ndarray = None  # type: ignore[assignment]
    pc_std: np.ndarray = None  # type: ignore[assignment]


def eof_basis(
    variability_fields: np.ndarray,
    n_modes: int | None = None,
    *,
    weights: np.ndarray | None = None,
    variance_explained: float | None = None,
    max_modes: int | None = None,
    min_modes: int = 1,
) -> EOFBasis:
    """Leading EOFs (area-weighted SVD) of a sample of fields.

    ``variability_fields``: shape ``(n_samples, n_space)`` — e.g. the
    reference's pre-2015 monthly anomaly fields (regime b), piControl chunks
    or CMIP6-member anomalies, flattened over space with any masked points
    removed beforehand.

    Truncation is either explicit (``n_modes``) or by the protocol's
    **pre-registered variance-explained criterion** (``variance_explained``,
    ``tier2.eof.variance_explained``): the fewest leading modes whose
    cumulative share of the total variance reaches that fraction, capped at
    ``max_modes`` (``tier2.eof.max_modes``) and floored at ``min_modes``.
    Giving neither keeps every mode.
    """
    fields = np.asarray(variability_fields, dtype=float)
    if fields.ndim != 2:  # noqa: PLR2004
        msg = f"variability_fields must be 2-D (samples, space), got {fields.ndim}-D"
        raise ValueError(msg)
    w = np.ones(fields.shape[1]) if weights is None else np.asarray(weights, float)
    sqrt_w = np.sqrt(w / w.sum())
    mean = fields.mean(axis=0)
    anom = (fields - mean) * sqrt_w[None, :]
    _u, s, vt = np.linalg.svd(anom, full_matrices=False)
    var = s**2 / max(fields.shape[0] - 1, 1)
    total = float(var.sum())
    ratio = var / total if total > 0 else np.zeros_like(var)

    if n_modes is None:
        if variance_explained is None:
            n_modes = s.size
        else:
            cumulative = np.cumsum(ratio)
            reached = np.searchsorted(cumulative, float(variance_explained)) + 1
            n_modes = int(reached)
    n_modes = min(int(n_modes), s.size)
    if max_modes is not None:
        n_modes = min(n_modes, int(max_modes))
    n_modes = max(n_modes, min(int(min_modes), s.size))

    eofs = vt[:n_modes]  # weighted-space orthonormal
    pcs = anom @ eofs.T  # the sample's own principal components
    pc_std = (
        pcs.std(axis=0, ddof=1) if fields.shape[0] > 1 else np.ones(n_modes)
    )
    return EOFBasis(
        eofs=eofs,
        explained_variance=var[:n_modes],
        mean=mean,
        weights=sqrt_w,
        explained_variance_ratio=ratio[:n_modes],
        pc_std=pc_std,
    )


def standardised_coefficients(
    basis: EOFBasis,
    field: np.ndarray,
) -> np.ndarray:
    """Project a field onto ``basis`` and divide by the pre-2015 PC σ.

    The protocol's regime-(b) coefficient vector: each retained coefficient
    is expressed in units of the reference's own pre-2015 variability, so
    the equal-weight mean over modes is a mean of commensurate numbers. A
    mode with zero σ (a degenerate basis) is left unscaled rather than
    divided by zero.
    """
    pcs = project_onto_eofs(basis, field)
    sigma = np.asarray(basis.pc_std, dtype=float)
    safe = np.where(np.isfinite(sigma) & (sigma > 0.0), sigma, 1.0)
    return pcs / safe


def cos_latitude_weights(latitudes: np.ndarray, n_longitudes: int) -> np.ndarray:
    """Area weights of a regular lat/lon grid, flattened as ``(lat, lon)``.

    ``cos(lat)`` repeated over longitude — the standard area weighting of the
    regular 2° grid every CB2 field is regridded to
    (``climateeval.diags._utils.DEFAULT_GRID``), matching
    ``physics.area_weighted_mean``.
    """
    lats = np.asarray(latitudes, dtype=float)
    return np.repeat(np.cos(np.deg2rad(lats)), int(n_longitudes))


def project_onto_eofs(basis: EOFBasis, field: np.ndarray) -> np.ndarray:
    """Project a field (``(n_space,)`` or ``(n, n_space)``) onto the basis."""
    field = np.asarray(field, dtype=float)
    anom = (field - basis.mean) * basis.weights
    return anom @ basis.eofs.T


def field_consistency(
    ensemble_fields: np.ndarray,
    observed_field: np.ndarray,
    variability_fields: np.ndarray,
    *,
    n_modes: int = 5,
    weights: np.ndarray | None = None,
    sigma_obs_field: np.ndarray | None = None,
    p_threshold: float = 0.05,
) -> tuple[bool, list[ConsistencyResult]]:
    """Superseded regime-(b) test for a spatial field via EOF projection.

    **Not the protocol's regime (b) any more** and not wired into any suite:
    since 2026-09 regime (b) is the fair CRPS of standardised coefficients on
    the *reference's* fixed pre-2015 EOF basis (`eof_basis` +
    `standardised_coefficients`, scored by `scoring_pass.score_eof_output`).
    This model-variability z-test is kept only as the consistency-style
    complement, in the shape of regime (c).

    EOFs come from ``variability_fields`` (e.g. piControl chunks); the
    inter-sample PC spread is the internal-variability term. Ensemble fields
    and the observed field are projected onto each mode and tested with
    :func:`ensemble_consistency`, Bonferroni-corrected across modes
    (per-mode threshold = p_threshold / n_modes). Overall pass = all modes
    pass.

    ``sigma_obs_field``: optional per-gridpoint observational σ, projected in
    quadrature onto each mode.
    """
    basis = eof_basis(variability_fields, n_modes, weights=weights)
    n_modes = basis.eofs.shape[0]
    pcs_ens = np.atleast_2d(project_onto_eofs(basis, ensemble_fields))
    pcs_obs = project_onto_eofs(basis, observed_field)
    pcs_var = project_onto_eofs(basis, variability_fields)
    sigma_int = pcs_var.std(axis=0, ddof=1)

    if sigma_obs_field is not None:
        obs_var = np.asarray(sigma_obs_field, dtype=float) ** 2
        sigma_obs_pc = np.sqrt((obs_var * basis.weights**2) @ (basis.eofs.T**2))
    else:
        sigma_obs_pc = np.zeros(n_modes)

    per_mode_threshold = p_threshold / n_modes  # Bonferroni
    results = [
        ensemble_consistency(
            pcs_ens[:, k],
            float(pcs_obs[k]),
            sigma_internal=float(sigma_int[k]),
            sigma_obs=float(sigma_obs_pc[k]),
            p_threshold=per_mode_threshold,
        )
        for k in range(n_modes)
    ]
    return all(r.passes for r in results), results


# ---------------------------------------------------------------------------
# Tier III: proxy-aware site consistency & perfect-model spread tests
# ---------------------------------------------------------------------------


def proxy_site_consistency(
    model_values: np.ndarray,
    proxy_values: np.ndarray,
    proxy_errors: np.ndarray,
    *,
    ensemble_values: np.ndarray | None = None,
    p_threshold: float = 0.05,
) -> tuple[float, np.ndarray]:
    """Regime-(c)-style consistency at proxy sites (metrics_reference.md III.1).

    The *complementary* Tier III diagnostic: the protocol's primary paleo
    statistic is `proxy_crps`, the fair CRPS of the block pseudo-ensemble.

    Per site: z = (proxy − model) / sqrt(var_ens + σ_proxy²); the score is
    the fraction of sites with two-sided p ≥ ``p_threshold`` (proxy error
    dominates σ, per the paper).

    ``model_values``: model anomaly sampled at the sites — ``(n_sites,)``
    (single run) or via ``ensemble_values`` ``(n_members, n_sites)``.
    Returns (fraction consistent, per-site z). Sites with NaN model, proxy
    or error values are excluded from the fraction.
    """
    proxy = np.asarray(proxy_values, dtype=float)
    err = np.asarray(proxy_errors, dtype=float)
    if ensemble_values is not None:
        members = np.asarray(ensemble_values, dtype=float)
        mu = members.mean(axis=0)
        var_ens = members.var(axis=0, ddof=1) if members.shape[0] > 1 else 0.0
    else:
        mu = np.asarray(model_values, dtype=float)
        var_ens = 0.0
    total_sigma = np.sqrt(var_ens + err**2)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (proxy - mu) / total_sigma
    valid = np.isfinite(z)
    if not valid.any():
        msg = "no valid proxy sites"
        raise ValueError(msg)
    z_crit = stats.norm.isf(p_threshold / 2.0)
    fraction = float((np.abs(z[valid]) < z_crit).mean())
    return fraction, z


def nonoverlapping_blocks(
    n_steps: int,
    block_length: int,
    *,
    spinup: int = 0,
) -> list[slice]:
    """Non-overlapping blocks of the **equilibrated** portion of a run.

    The protocol's pseudo-ensemble rule for a single-member equilibrium
    experiment (metrics_reference.md §III.1, paper §5.1): drop the first
    ``spinup`` steps, then chop what remains into consecutive blocks of
    ``block_length`` steps. A trailing remainder shorter than one block is
    dropped rather than scored as a short block — every pseudo-member must
    average the same number of years, or their spread would mix sampling
    noise with block length.

    Returns the blocks **latest-first is not implied**: they are in record
    order, and the count is ``(n_steps − spinup) // block_length``, which the
    caller checks against the protocol's minimum of two (fair CRPS is
    undefined for one member).
    """
    usable = int(n_steps) - int(spinup)
    length = int(block_length)
    if length < 1:
        msg = f"block_length must be >= 1, got {block_length}"
        raise ValueError(msg)
    n_blocks = max(usable // length, 0)
    start = int(spinup)
    return [
        slice(start + i * length, start + (i + 1) * length) for i in range(n_blocks)
    ]


def block_climatologies(
    values: np.ndarray,
    block_length: int,
    *,
    spinup: int = 0,
) -> np.ndarray:
    """Per-block means along the leading (time) axis.

    ``values`` is ``(n_steps, …)`` — an annual-mean field, a site series, a
    scalar series; the result is ``(n_blocks, …)``, one climatology per
    :func:`nonoverlapping_blocks` block. These are the protocol's
    **pseudo-members** once the piControl climatology is subtracted, which
    the caller does (the control mean is common to every block, so
    subtracting it shifts them all equally and leaves the spread untouched).

    NaNs are ignored within a block (``np.nanmean``), so a field masked over
    land or ice keeps those points masked rather than poisoning the block.
    """
    array = np.asarray(values, dtype=float)
    blocks = nonoverlapping_blocks(array.shape[0], block_length, spinup=spinup)
    if not blocks:
        return np.empty((0, *array.shape[1:]), dtype=float)
    with warnings.catch_warnings():
        # A gridpoint masked for the whole block (land under an SST field)
        # is NaN by design, not a problem to warn about.
        warnings.filterwarnings("ignore", message="Mean of empty slice")
        return np.stack([np.nanmean(array[b], axis=0) for b in blocks])


def proxy_crps(
    members_at_sites: np.ndarray,
    proxy_values: np.ndarray,
    proxy_sigma: np.ndarray,
    *,
    n_draws: int = 100,
    seed: int = 0,
) -> tuple[CRPSScore, np.ndarray]:
    """Tier III primary score: fair CRPS against proxies, meaned over sites.

    Per site, the fair CRPS of the model's pseudo-ensemble against the proxy
    value, with the **proxy uncertainty as the observational variance term**
    — the same common-draw treatment as Tier II
    (:func:`crps_fair_with_obs_draws`), so the two tiers' CRPS numbers are
    formed the same way (metrics_reference.md §III.1).

    The variable-level score is the **equal-weight mean over sites**: a proxy
    network is a set of point measurements, not an area sample, so
    cos-latitude weighting would be meaningless (it weights grid cells, not
    cores). Sites are treated as an unordered axis, so the standard error is
    ``std / sqrt(n_sites)`` (:func:`crps_independent_summary`) — which
    **ignores spatial correlation between nearby sites** and is therefore
    optimistic; a documented CB2 reading, not a protocol statement.

    Parameters
    ----------
    members_at_sites:
        ``(n_members, n_sites)`` — the pseudo-ensemble sampled at the sites.
    proxy_values, proxy_sigma:
        ``(n_sites,)`` — the proxy anomaly and its 1σ uncertainty.
    n_draws, seed:
        Observational-uncertainty draws (``tier2.obs_uncertainty``).

    Returns
    -------
    :
        ``(summary, per-site CRPS)``. Sites with a non-finite proxy value,
        σ or model value are dropped from both.
    """
    members = np.atleast_2d(np.asarray(members_at_sites, dtype=float))
    proxy = np.asarray(proxy_values, dtype=float)
    sigma = np.asarray(proxy_sigma, dtype=float)
    valid = (
        np.isfinite(proxy)
        & np.isfinite(sigma)
        & np.isfinite(members).all(axis=0)
    )
    if not valid.any():
        msg = "no valid proxy sites"
        raise ValueError(msg)
    crps_sites = crps_fair_with_obs_draws(
        members[:, valid],
        proxy[valid],
        obs_sigma=sigma[valid],
        n_draws=n_draws,
        seed=seed,
    )
    full = np.full(proxy.shape, np.nan)
    full[valid] = crps_sites
    return crps_independent_summary(crps_sites, members.shape[0]), full


def sample_at_sites(
    field: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    site_lats: np.ndarray,
    site_lons: np.ndarray,
) -> np.ndarray:
    """Nearest-gridpoint sampling of a (lat, lon) field at proxy sites.

    Longitudes are compared modulo 360 so −20 and 340 match.
    """
    field = np.asarray(field, dtype=float)
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float) % 360.0
    out = np.empty(len(site_lats))
    for i, (slat, slon) in enumerate(zip(site_lats, site_lons)):
        j = int(np.argmin(np.abs(lats - slat)))
        k = int(np.argmin(np.abs((lons - float(slon) % 360.0 + 180.0) % 360.0 - 180.0)))
        out[i] = field[j, k]
    return out


def le_variance_ratio(
    predicted_members: np.ndarray,
    truth_members: np.ndarray,
) -> float:
    """Large-ensemble spread test (III.2): predicted/true inter-member
    variance, aggregated over all remaining dimensions."""
    pred = np.asarray(predicted_members, dtype=float)
    truth = np.asarray(truth_members, dtype=float)
    var_pred = np.nanmean(pred.var(axis=0, ddof=1))
    var_truth = np.nanmean(truth.var(axis=0, ddof=1))
    if var_truth == 0:
        msg = "truth ensemble has zero variance"
        raise ValueError(msg)
    return float(var_pred / var_truth)


def le_spread_pattern_correlation(
    predicted_members: np.ndarray,
    truth_members: np.ndarray,
    *,
    weights: np.ndarray | None = None,
) -> float:
    """Spatial correlation of the inter-member variability patterns (III.2).

    Members of shape ``(n_members, n_space)``; optional area weights.
    """
    pred_var = np.asarray(predicted_members, dtype=float).var(axis=0, ddof=1)
    truth_var = np.asarray(truth_members, dtype=float).var(axis=0, ddof=1)
    w = np.ones(pred_var.size) if weights is None else np.asarray(weights, float)
    valid = np.isfinite(pred_var) & np.isfinite(truth_var)
    x, y, w = pred_var[valid], truth_var[valid], w[valid]
    mx, my = np.average(x, weights=w), np.average(y, weights=w)
    cov = np.average((x - mx) * (y - my), weights=w)
    vx = np.average((x - mx) ** 2, weights=w)
    vy = np.average((y - my) ** 2, weights=w)
    return float(cov / np.sqrt(vx * vy))
