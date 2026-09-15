# app/alert_visibility.py
"""
Metric names that exist as real rows in `alerts` (so correlation/health
scoring/RCA keep working unchanged) but are deliberately hidden from
every end-user-facing alert list/count -- across BOTH app/api/alerts.py
(the Alerts page's own tabs/list) and app/api/live_data.py (the Overview
page's account tiles/banner, via _get_active_alert_counts_by_account()).

Living in its own module, not inside either of those two files, is
deliberate: app/api/alerts.py already imports invalidate_accounts_cache
from app/api/live_data.py, so live_data.py importing the hidden-metrics
filter back FROM alerts.py would be a circular import. Both modules
import this one instead.

2026-09-15: extracted after finding these two call sites had drifted
apart. alerts.py's own _fetch_counts_from_db() docstring claims its
"critical" count is "defined identically to live_data.py's
_get_active_alert_counts_by_account() ... so this number always matches
the Overview banner/tiles" -- true for critical (neither query filtered
anything there), but false for warning: alerts.py's queries all filtered
`a.metric_name NOT IN ({hidden})`, and live_data.py's never did. Net
effect confirmed live: an account with real, visible warnings on 3
resources plus 3 hidden multivariate_anomaly warning rows showed
"6 WARNING" on the Overview banner/account card while the Alerts page's
own Active/Warning tabs (correctly excluding the hidden rows) showed 3 --
same "two dashboards built from different queries disagree" bug class
already fixed once this session for the auto-resolve cache-invalidation
gap, just from a second, independent cause.
"""

# multivariate_anomaly (see app/collector/multivariate_anomaly.py) is an
# IsolationForest decision-score, not a real CloudWatch metric -- its
# "value"/"threshold" (e.g. -0.1 / 0) reads as confusing noise next to
# genuine metric alerts (Net In, Net Out, etc.) wherever alerts are
# counted or listed for a human. The detector keeps running and these
# rows still exist in `alerts` so correlation/health scoring/RCA
# (correlate.py, health_score.py, rca.py) keep working unchanged -- only
# user-facing list/count queries filter it out.
HIDDEN_FROM_ALERTS_UI_METRICS = ("multivariate_anomaly",)


def hidden_metrics_sql() -> str:
    """Comma-separated, quoted SQL literal list for use in a `NOT IN (...)`
    clause. Safe to inline (not parameterized) because this only ever
    renders the fixed HIDDEN_FROM_ALERTS_UI_METRICS constant above, never
    request input."""
    return ", ".join(f"'{m}'" for m in HIDDEN_FROM_ALERTS_UI_METRICS)
