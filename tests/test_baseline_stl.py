# tests/test_baseline_stl.py
"""
Coverage for app/collector/baseline_stl.py, focused on the
account-scoping regression fixed in this audit (see
app/collector/multivariate_anomaly.py's sibling test file for the same
bug shape elsewhere): metric_baseline's identity is (aws_account_id,
resource_id, metric_name, hour_of_day, day_of_week) since migration
046, because two different accounts CAN legitimately share a
resource_id string (the stock "System" CloudWatch Logs group is this
repo's own recurring example -- see migration 048's 2026-09-16 AuroGov
Mumbai/U4RAD precedent). Before this fix, every query in this module
matched on resource_id/metric_name alone, so two accounts sharing a
resource_id would have their metric_history pooled into one STL fit,
and the UPDATE would overwrite BOTH accounts' buckets with it.

Only the DB layer (get_connection) is faked, via the same
FakeCursor/FakeConn/install_stub convention as every other test in
this suite. A real (small) STL fit runs end-to-end, same rationale as
test_multivariate_anomaly.py: the numerical behavior is the thing worth
testing, not a mocked model.
"""
import sys
from datetime import datetime, timedelta

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn

POINTS_PER_DAY = 288  # must match baseline_stl.py's own constant


def _seasonal_series_rows(days=14, base=50.0, amplitude=10.0, start=None):
    """A clean daily-seasonal series (sine wave + mild noise) -- dense
    enough to clear _load_series()'s coverage gate and long enough to
    clear _fit_stl()'s >= 2 * POINTS_PER_DAY minimum."""
    import math
    import random
    random.seed(7)
    start = start or datetime(2026, 9, 1, 0, 0, 0)
    rows = []
    n = days * POINTS_PER_DAY
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        phase = 2 * math.pi * (i % POINTS_PER_DAY) / POINTS_PER_DAY
        value = base + amplitude * math.sin(phase) + random.uniform(-0.5, 0.5)
        rows.append({"metric_timestamp": ts, "metric_value": value})
    return rows


def _install_db_stub(candidates, series_rows_by_account, existing_stddev_by_account=None):
    """candidates: [{"aws_account_id", "resource_id", "metric_name"}, ...]
    series_rows_by_account: {account_id: rows} -- what _load_series's
    query returns for that account (lets a test give two accounts
    sharing a resource_id DIFFERENT underlying data, so a leak would be
    observable).
    existing_stddev_by_account: {account_id: stddev_value} for the
    pre-existing sigma-clipped row each account already has."""
    existing_stddev_by_account = existing_stddev_by_account or {}
    load_series_calls = []   # (account_id, resource_id, metric_name) per call
    updates_by_account = {}  # account_id -> list of UPDATE params

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT DISTINCT aws_account_id, resource_id, metric_name"):
                self._pending = candidates
            elif normalized.startswith("SELECT metric_timestamp, metric_value"):
                # params: (resource_id, aws_account_id, metric_name, MIN_STL_DAYS)
                resource_id, account_id, metric_name, _days = params
                load_series_calls.append((account_id, resource_id, metric_name))
                self._pending = series_rows_by_account.get(account_id, [])
            elif normalized.startswith("SELECT stddev_value FROM metric_baseline"):
                # params: (account_id, resource_id, metric_name, hour, weekday)
                account_id = params[0]
                stddev = existing_stddev_by_account.get(account_id)
                self._pending = [{"stddev_value": stddev}] if stddev is not None else []
            elif normalized.startswith("UPDATE metric_baseline SET mean_value"):
                account_id = params[2]  # (mean, stddev, account_id, resource_id, metric_name, hour, weekday)
                updates_by_account.setdefault(account_id, []).append(params)
                self.rowcount = 1
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    return load_series_calls, updates_by_account


def test_load_series_query_is_scoped_to_the_candidates_own_account():
    """Regression test: _load_series's resources subquery used to match
    resource_id alone (`SELECT id FROM resources WHERE resource_id = %s
    LIMIT 1`, no account filter) -- with two accounts sharing a
    resource_id, this LIMIT-1-no-account-filter pattern could silently
    pick the WRONG account's resource row. Asserts the account id is
    now actually bound as a query parameter."""
    candidates = [{"aws_account_id": 10, "resource_id": "System", "metric_name": "IncomingLogEvents"}]
    rows = _seasonal_series_rows(days=14)
    load_series_calls, _ = _install_db_stub(candidates, {10: rows})

    mod = load_module("app/collector/baseline_stl.py")
    mod.upgrade_baselines_with_stl()

    assert load_series_calls == [(10, "System", "IncomingLogEvents")]


def test_cross_account_shared_resource_id_does_not_leak_or_overwrite():
    """The core regression: accounts 7 and 10 both have a resource
    named "System" with the SAME metric name, each with genuinely
    different underlying data. Before the fix, account 10's candidate
    would have its _load_series() call (unscoped) potentially pull
    account 7's metric_history instead, and the final UPDATE (also
    unscoped) would overwrite BOTH accounts' metric_baseline rows with
    whichever series was loaded. After the fix, each account's fit
    uses only its own data and updates only its own row."""
    candidates = [
        {"aws_account_id": 7,  "resource_id": "System", "metric_name": "IncomingLogEvents"},
        {"aws_account_id": 10, "resource_id": "System", "metric_name": "IncomingLogEvents"},
    ]
    rows_7  = _seasonal_series_rows(days=14, base=50.0, amplitude=10.0)
    rows_10 = _seasonal_series_rows(days=14, base=500.0, amplitude=100.0)
    load_series_calls, updates_by_account = _install_db_stub(
        candidates, {7: rows_7, 10: rows_10},
        existing_stddev_by_account={7: 1.0, 10: 1.0},  # low enough that MIN_STDDEV_RATIO never blocks the write
    )

    mod = load_module("app/collector/baseline_stl.py")
    upgraded = mod.upgrade_baselines_with_stl()

    # Each account's series was loaded with its OWN account id -- never
    # the other account's, even though both share resource_id "System".
    assert (7, "System", "IncomingLogEvents") in load_series_calls
    assert (10, "System", "IncomingLogEvents") in load_series_calls

    # Both accounts got their OWN buckets upgraded -- not a shared,
    # cross-contaminated write, and the two accounts' fitted means are
    # in the right ballpark for their own (very different) input data,
    # not swapped or averaged together.
    assert upgraded > 0
    assert 7 in updates_by_account and 10 in updates_by_account
    mean_7  = updates_by_account[7][0][0]
    mean_10 = updates_by_account[10][0][0]
    assert 30 < mean_7 < 70,     f"account 7's fitted mean should reflect ITS OWN ~50-centered series, got {mean_7}"
    assert 350 < mean_10 < 650,  f"account 10's fitted mean should reflect ITS OWN ~500-centered series, got {mean_10}"
