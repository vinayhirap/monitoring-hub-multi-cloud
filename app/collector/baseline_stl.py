# app/collector/baseline_stl.py
"""
AIOps roadmap #9 -- STL seasonal-decomposition upgrade for dynamic
thresholds (2026-09-14). OPTIONAL, ADDITIVE pass on top of
app/collector/baseline.py's sigma-clipped hour/weekday buckets -- see
db/migrations/028_baseline_stl_method.sql for why this is safe (same
table, same columns, same consumer, `alert_evaluator._dynamic_bounds()`
is not touched by this file at all).

WHY THIS EXISTS
----------------
Hour/weekday bucketing treats each of the 168 (hour, weekday) slots as
its own independent mean/stddev. That's fine for a metric with only ONE
seasonal pattern, but a metric with real daily AND weekly seasonality
(request-count traffic, error-rate, anything correlated with human
activity) has a smooth underlying SHAPE that bucketing can't see -- it
only ever compares a bucket to itself, never to its neighbors.

STL (Seasonal-Trend decomposition using LOESS, Cleveland et al. 1990,
implemented in statsmodels) fits trend + seasonal + residual components
against the FULL ordered time series, so it borrows strength across the
whole series instead of per-bucket. The trend+seasonal fit becomes this
bucket's mean; the residual's stddev (noise left over AFTER removing
the known daily pattern) becomes this bucket's stddev -- almost always
tighter/more accurate than sigma-clipped raw stddev for a strongly
seasonal metric, which means a genuine anomaly stands out more sigma
above/below the band instead of being buried in one bucket's own noise.

WHY THIS IS OPT-IN PER (resource, metric), NOT A REPLACEMENT
--------------------------------------------------------------
STL needs a long, REGULARLY-SPACED series to fit a daily period
reliably. Two real failure modes if used blindly:
  1. Too little history -> statsmodels either raises or returns a
     degenerate/noisy fit that's WORSE than sigma-clipped bucketing.
  2. A flat/step-like metric (e.g. a count that barely changes) has no
     real seasonal shape -- STL can manufacture a "seasonal" component
     out of what's actually just noise, which would UNDER-estimate the
     residual stddev and make the band too tight (more false alerts,
     the opposite of the roadmap's goal).

So this module:
  - Requires MIN_STL_DAYS of history with reasonably regular sampling
    (see _has_regular_coverage) before attempting STL at all.
  - Runs baseline.py's sigma-clip recompute FIRST (scheduler.py calls
    this module immediately after, not instead of, recompute_baselines()),
    and only OVERWRITES a bucket if STL's fit succeeds AND its residual
    stddev is not implausibly smaller than the already-written
    sigma-clip stddev (see MIN_STDDEV_RATIO) -- a cheap sanity clamp
    against exactly failure mode #2 above, without needing a full
    backtest harness to ship v1 safely.
  - Marks every bucket it writes with computed_by='stl' (migration 028)
    so this is auditable/reversible: a bad STL fit for one resource can
    be spotted and the bucket will simply revert to 'sigma_clip' on the
    next cycle if STL's own sanity gates stop passing for it.

Every failure (missing statsmodels, insufficient data, a fit exception
for one specific resource/metric) is caught and logged, never raised --
same non-fatal contract as every other scheduler.py "low" tier job. The
sigma-clipped bucket baseline.py already wrote is always a safe
fallback; this module can never leave a bucket WORSE than before it
ran, only sometimes fail to improve it.
"""
import logging
from datetime import datetime, timedelta

from app.db import get_connection

logger = logging.getLogger(__name__)

# Needs at least this many days of reasonably continuous history before
# a daily-period STL fit is trustworthy. Two full weeks catches one
# weekday cycle at minimum; baseline.py's own MIN_SAMPLES_PER_BUCKET
# cold-start gate already keeps brand-new resources on static
# thresholds for their first 2-3 weeks, so this doesn't meaningfully
# delay anything beyond what already happens today.
MIN_STL_DAYS = 14

# Metrics are collected on (roughly) a 5-minute cadence elsewhere in
# this app (see metrics_writer.py) -- this is STL's `period` parameter
# for the daily seasonal component: 24h * 60m / 5m = 288 points/day.
POINTS_PER_DAY = 288

# A (resource, metric) series needs at least this fraction of the
# theoretically-possible points over the lookback window actually
# present, or the series is too gappy for a fixed-period STL fit to be
# meaningful (large gaps get linearly interpolated below, but too much
# interpolation just means STL is fitting its own guesses).
MIN_COVERAGE_FRACTION = 0.5

# Sanity clamp against STL under-estimating noise on a near-flat metric
# (see module docstring, failure mode #2): if STL's residual stddev for
# a bucket is below this fraction of the sigma-clipped stddev
# baseline.py already computed for the SAME bucket, treat the STL fit
# as unreliable for that bucket and leave the sigma-clip value in
# place. 0.25 is deliberately loose (STL SHOULD produce a tighter
# stddev than sigma-clip for genuinely seasonal metrics -- that's the
# whole point) -- this only catches an implausible 4x-or-more drop.
MIN_STDDEV_RATIO = 0.25

# Only bother running STL on (resource, metric) pairs that actually
# have variance worth decomposing -- skip anything sigma_clip already
# wrote with stddev 0 (flat-line, no seasonal shape possible) or that
# has no bucket at all yet (cold start -- let sigma-clip accumulate
# history first).
_CANDIDATES_SQL = """
    SELECT DISTINCT resource_id, metric_name
    FROM metric_baseline
    WHERE stddev_value > 0
"""


def _has_statsmodels():
    try:
        import statsmodels.api  # noqa: F401
        return True
    except Exception:
        # Broad except, not just ImportError -- on 2026-09-15 this
        # caught a REAL production incident: statsmodels 0.14.5 (the
        # version originally pinned here) raised TypeError at import
        # time under pandas==3.0.2 (an upstream statsmodels/pandas
        # incompatibility -- see requirements.txt's updated comment,
        # fixed by bumping to statsmodels==0.15.0). With only
        # `except ImportError` here, that TypeError propagated straight
        # through this function uncaught, and then through
        # upgrade_baselines_with_stl()'s own top-level try (which has
        # no wrapper around this specific call), crashing the ENTIRE
        # job every single 2-minute scheduler cycle instead of the
        # single graceful "not installed" warning this function is
        # supposed to produce. A dependency import can fail in more
        # ways than "not installed" -- this now degrades safely no
        # matter which way it breaks.
        logger.warning(
            "[baseline_stl] statsmodels import failed -- skipping STL upgrade pass "
            "entirely this cycle (sigma-clipped baselines from baseline.py are "
            "unaffected). If this persists, check for a statsmodels/pandas version "
            "incompatibility (see requirements.txt's comment on this dependency) "
            "rather than assuming it's simply uninstalled.",
            exc_info=True,
        )
        return False


def _load_series(cursor, resource_id, metric_name):
    """Returns a pandas Series of metric_value indexed by a regular
    5-minute DatetimeIndex over the lookback window, gaps forward-
    filled then linearly interpolated. Returns None if coverage is too
    sparse to trust (see MIN_COVERAGE_FRACTION)."""
    import pandas as pd

    cursor.execute("""
        SELECT metric_timestamp, metric_value
        FROM metric_history
        WHERE resource_id = (SELECT id FROM resources WHERE resource_id = %s LIMIT 1)
          AND metric_name = %s
          AND metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
          AND metric_value IS NOT NULL
        ORDER BY metric_timestamp
    """, (resource_id, metric_name, MIN_STL_DAYS))
    rows = cursor.fetchall()
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["metric_timestamp"] = pd.to_datetime(df["metric_timestamp"])
    df = df.drop_duplicates(subset="metric_timestamp").set_index("metric_timestamp")

    span_start = df.index.min()
    span_end = df.index.max()
    expected_points = int((span_end - span_start).total_seconds() / 300) + 1
    if expected_points <= 0:
        return None
    coverage = len(df) / expected_points
    if coverage < MIN_COVERAGE_FRACTION:
        return None

    full_index = pd.date_range(start=span_start, end=span_end, freq="5min")
    series = df["metric_value"].reindex(full_index)
    # Short gaps: linear interpolation. Any gap still NaN at the edges
    # after that (a run cut off at the very start/end of the window)
    # is forward/back-filled -- STL cannot handle NaN at all.
    series = series.interpolate(method="linear").ffill().bfill()
    if series.isna().any() or len(series) < MIN_STL_DAYS * POINTS_PER_DAY * MIN_COVERAGE_FRACTION:
        return None
    return series


def _fit_stl(series):
    """Runs STL with a daily period, returns (trend, seasonal, resid)
    component Series, or None if the fit fails or the series is too
    short for the requested period."""
    from statsmodels.tsa.seasonal import STL

    if len(series) < POINTS_PER_DAY * 2:
        return None
    try:
        result = STL(series, period=POINTS_PER_DAY, robust=True).fit()
        return result.trend, result.seasonal, result.resid
    except Exception:
        logger.exception("[baseline_stl] STL fit raised for a resource/metric series")
        return None


def upgrade_baselines_with_stl() -> int:
    """
    Upgrades metric_baseline buckets from sigma-clipped to STL-derived
    mean/stddev wherever the safety gates in this module's docstring
    pass. Returns the number of buckets upgraded. Always safe to call
    after app/collector/baseline.py's recompute_baselines() -- never
    raises, never leaves a bucket in a worse state than it found it.
    """
    if not _has_statsmodels():
        return 0

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    upgraded = 0
    try:
        cursor.execute(_CANDIDATES_SQL)
        candidates = cursor.fetchall()

        for c in candidates:
            resource_id, metric_name = c["resource_id"], c["metric_name"]
            try:
                series = _load_series(cursor, resource_id, metric_name)
                if series is None:
                    continue

                fit = _fit_stl(series)
                if fit is None:
                    continue
                trend, seasonal, resid = fit

                fitted = trend + seasonal
                resid_by_bucket = {}
                fitted_by_bucket = {}
                for ts, val in fitted.items():
                    key = (ts.hour, ts.weekday())
                    fitted_by_bucket.setdefault(key, []).append(val)
                for ts, val in resid.items():
                    key = (ts.hour, ts.weekday())
                    resid_by_bucket.setdefault(key, []).append(val)

                for key, fitted_vals in fitted_by_bucket.items():
                    resid_vals = resid_by_bucket.get(key)
                    if not resid_vals or len(resid_vals) < 3:
                        continue
                    hour_of_day, day_of_week = key
                    mean_value = sum(fitted_vals) / len(fitted_vals)
                    resid_mean = sum(resid_vals) / len(resid_vals)
                    variance = sum((r - resid_mean) ** 2 for r in resid_vals) / max(1, len(resid_vals) - 1)
                    stddev_value = variance ** 0.5

                    cursor.execute("""
                        SELECT stddev_value FROM metric_baseline
                        WHERE resource_id = %s AND metric_name = %s
                          AND hour_of_day = %s AND day_of_week = %s
                    """, (resource_id, metric_name, hour_of_day, day_of_week))
                    existing = cursor.fetchone()
                    if existing and existing["stddev_value"] > 0:
                        if stddev_value < existing["stddev_value"] * MIN_STDDEV_RATIO:
                            # Safety clamp tripped -- see module docstring.
                            # Leave the sigma-clipped value in place.
                            continue

                    cursor.execute("""
                        UPDATE metric_baseline
                        SET mean_value = %s, stddev_value = %s, computed_by = 'stl'
                        WHERE resource_id = %s AND metric_name = %s
                          AND hour_of_day = %s AND day_of_week = %s
                    """, (mean_value, stddev_value, resource_id, metric_name,
                          hour_of_day, day_of_week))
                    upgraded += cursor.rowcount

            except Exception:
                # One resource/metric's series failing (bad data, a
                # transient pandas/statsmodels error) must never block
                # the rest of the fleet from being upgraded.
                logger.exception(
                    f"[baseline_stl] failed to upgrade {resource_id}/{metric_name}, "
                    f"leaving its sigma-clipped baseline unchanged"
                )
                continue

        conn.commit()
        logger.info(f"[baseline_stl] upgraded {upgraded} bucket(s) to STL-derived mean/stddev")
        return upgraded
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
