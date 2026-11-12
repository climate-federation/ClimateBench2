"""The protocol's time windows, derived from ``thresholds.yml``.

One place resolves every window the protocol names, so the CLI (which cuts
the submission's cubes), the reference-window diagnostics (which reach *back*
past that cut for the pre-2015 record) and the scoring pass (which chunks a
control into observation-length segments) cannot drift apart:

- **test window** — ``tier2.test_window_start`` → the last complete year; the
  reserved post-2015 period every Tier II score is taken over. It grows by a
  year every January without a code change.
- **baseline window** — ``tier2.climatology_baseline_period`` = 1985–2014;
  the pre-test record. It is the Climatology baseline's sample, the Pinatubo
  reference climatology, the reference's PRE-2015 record for the regime-(b)
  EOF basis and its standardisation, and — since the regime-(a) scores are
  **anomalies** — the climatology every scored time series is taken relative
  to, which is why the scored suite entries load from its first year
  (:func:`extend_to_baseline`).
- **long trend window** — ``tier2.long_trend_start`` → the last complete
  year; the "1950-present" window of §II.1, the second length the
  internal-variability diagnostic reports σ_int for.

Timeranges are ISO-like ``YYYYMMDD/YYYYMMDD`` strings, the form ClimateEval
puts on ``Variable.timerange`` and hands to ``DataSource.get_cube``.
"""

from __future__ import annotations

import datetime as dt

from climatebench2._thresholds import get_threshold


def last_complete_year(today: dt.date | None = None) -> int:
    """The last year that has finished (the protocol scores whole years)."""
    return (today or dt.date.today()).year - 1


def timerange(first_year: int, last_year: int) -> str:
    """``YYYY0101/YYYY1231`` for a whole-year window."""
    return f"{int(first_year)}0101/{int(last_year)}1231"


def _year_pair(value: object, key: str) -> tuple[int, int]:
    """``[first, last]`` of a threshold as a validated pair of years."""
    try:
        first, last = (int(year) for year in value)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        msg = f"{key} must be [first_year, last_year], got {value!r}"
        raise ValueError(msg) from None
    if first > last:
        msg = f"{key} must be [first_year, last_year], got {value!r}"
        raise ValueError(msg)
    return (first, last)


def test_window_years(today: dt.date | None = None) -> tuple[int, int]:
    """``(first, last)`` year of the scored window (the reserved test window).

    ``tier2.test_window_start`` → the last complete year, unless the optional
    ``tier2.scored_window = [first, last]`` names another (an AMIP protocol that
    scores 2015-2024). It is the window the CLI loads the scored suites over.
    A per-entry override (``tier2.scored_windows``) is :func:`scored_window`.
    """
    custom = get_threshold("tier2.scored_window", None)
    if custom is not None:
        return _year_pair(custom, "tier2.scored_window")
    start = int(get_threshold("tier2.test_window_start"))
    return (start, max(last_complete_year(today), start))


def scored_window(entry_name: str | None = None) -> tuple[int, int | None]:
    """``(first, last)`` year one suite entry is **scored** on.

    ``last`` is ``None`` for the default, open-ended window (every step from
    ``tier2.test_window_start`` on, as ClimateBench2 has always scored). The
    order of precedence: ``tier2.scored_windows[entry_name]`` (a hindcast entry
    with its own window), then ``tier2.scored_window``, then the default.
    """
    per_entry = get_threshold("tier2.scored_windows", None) or {}
    if entry_name is not None and entry_name in per_entry:
        key = f"tier2.scored_windows.{entry_name}"
        return _year_pair(per_entry[entry_name], key)
    custom = get_threshold("tier2.scored_window", None)
    if custom is not None:
        return _year_pair(custom, "tier2.scored_window")
    return (int(get_threshold("tier2.test_window_start")), None)


def in_scored_window(years: object, window: tuple[int, int | None]) -> object:
    """Boolean mask of the ``years`` (array) inside a :func:`scored_window`."""
    import numpy as np  # noqa: PLC0415

    years = np.asarray(years)
    mask = years >= window[0]
    if window[1] is not None:
        mask = mask & (years <= window[1])
    return mask


def has_custom_scored_window(entry_name: str | None = None) -> bool:
    """Whether ``entry_name`` is scored on anything but the default window."""
    return scored_window(entry_name)[1] is not None or scored_window(entry_name)[
        0
    ] != int(get_threshold("tier2.test_window_start"))


def test_window_timerange(today: dt.date | None = None) -> str:
    """ISO timerange of the reserved test window."""
    return timerange(*test_window_years(today))


def pre_test_last_year() -> int:
    """The last year *before* the reserved test window (2014).

    An **in-sample** statistic — the ETCCDI climatologies, the PDF overlaps,
    the diurnal and seasonal climatologies — is one the protocol allows to be
    computed from data a modeller could have seen, and the line between
    "could have seen" and "reserved" is ``tier2.test_window_start``. So an
    in-sample window may end here and no later: an extremes climatology that
    ran to 2025 would be scored partly on the held-out period, which is the
    one thing the split exists to prevent.
    """
    return int(get_threshold("tier2.test_window_start")) - 1


def baseline_window_years() -> tuple[int, int]:
    """``(first, last)`` year of the pre-test 1985–2014 baseline window."""
    first, last = get_threshold("tier2.climatology_baseline_period")
    return (int(first), int(last))


def baseline_timerange() -> str:
    """ISO timerange of the pre-test baseline window."""
    return timerange(*baseline_window_years())


def extend_to_baseline(nominal: str) -> str:
    """``nominal`` with its start pulled back to the baseline window's first year.

    Regime (a) scores **anomalies about each source's own 1985–2014
    climatology** (docs/metrics_reference.md, Tier II preamble), so the suite
    entries whose ``raw_output`` is a scored time series must load the
    submission, the reference and every comparison member from the baseline
    start, not from ``tier2.test_window_start``. The end is untouched: it is
    still the last scored year, and :mod:`climatebench2.reference_windows`
    still clips it to what each reference product actually covers.

    A ``nominal`` that already reaches back past the baseline (a user's
    ``--timerange 19790101/...``) is returned unchanged.
    """
    first = int(nominal.split("/")[0][:4])
    last = int(nominal.split("/")[1][:4])
    baseline_first, _ = baseline_window_years()
    return timerange(min(first, baseline_first), last)


def scores_anomalies(entry_name: str) -> bool:
    """Whether one suite entry's time series are scored as **anomalies**.

    Regime (a) scores anomalies about each source's own baseline-window
    climatology, but only for the entries the protocol says so of
    (``tier2.anomaly_baseline``, keyed by suite entry name — which is also
    the DuckDB schema the rows land in, so ``leaderboard --rescore`` resolves
    it the same way the suite run did). Everything unlisted takes ``default``,
    which is false: Tier I, the daily suite's in-sample annual block maxima
    and any user suite keep absolute values.

    This one predicate drives both halves of the mechanism — the load window
    (:func:`climatebench2.reference_windows.apply_reference_windows`) and the
    scoring (:mod:`climatebench2.scoring_pass`) — so the two cannot disagree
    about which series are anomalies.
    """
    table = get_threshold("tier2.anomaly_baseline")
    by_entry = table.get("diagnostics") or {}
    if entry_name in by_entry:
        return bool(by_entry[entry_name])
    return bool(table.get("default", False))


def anomaly_baseline_years(entry_name: str | None = None) -> tuple[int, int]:
    """``(first, last)`` year of the climatology one entry's anomalies are about.

    ``tier2.anomaly_baseline.periods[entry_name]`` if given (a hindcast entry
    anomalised about its own years), else the protocol baseline window.
    """
    periods = get_threshold("tier2.anomaly_baseline.periods", None) or {}
    if entry_name is not None and entry_name in periods:
        key = f"tier2.anomaly_baseline.periods.{entry_name}"
        return _year_pair(periods[entry_name], key)
    return baseline_window_years()


def anomaly_min_baseline_years() -> int:
    """Distinct baseline years a source needs before it may be anomalised."""
    return int(get_threshold("tier2.anomaly_baseline.min_years"))


def long_trend_window_years(today: dt.date | None = None) -> tuple[int, int]:
    """``(first, last)`` year of the "1950-present" trend window (§II.1)."""
    start = int(get_threshold("tier2.long_trend_start"))
    return (start, max(last_complete_year(today), start))


def window_lengths(today: dt.date | None = None) -> dict[str, int]:
    """Length in whole years of every window σ_int is reported for.

    ``{"test": …, "long": …}`` — the keys the internal-variability
    diagnostic tags its output columns with.
    """
    test_first, test_last = test_window_years(today)
    long_first, long_last = long_trend_window_years(today)
    return {
        "test": test_last - test_first + 1,
        "long": long_last - long_first + 1,
    }
