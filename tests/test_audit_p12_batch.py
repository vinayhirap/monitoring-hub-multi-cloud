# tests/test_audit_p12_batch.py
"""Audit G1/G5/G10/F5/B5/B15/E9 grouped batch - wiring and the one backend helper."""
import re
import sys
from pathlib import Path

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend/src"


def _t(rel):
    return (FE / rel).read_text()


def _cooldown():
    src = (ROOT / "app/api/settings.py").read_text()
    body = src[src.index("CHECK_COOLDOWN_SECONDS = 30"):src.index('@router.get("/check")')]
    ns = {}
    exec(body, ns)
    return ns["check_cooldown_remaining"]


def test_check_now_cooldown_blocks_repeats_per_account_and_expires():
    f, store = _cooldown(), {}
    assert f(10, now=100.0, store=store) == 0                      # first click goes through
    assert f(10, now=110.0, store=store) == 20                     # 20 s still to wait
    assert f(7, now=110.0, store=store) == 0                       # another account is independent
    assert f(10, now=129.5, store=store) == 1                      # never reports 0 while still blocked
    assert f(10, now=131.0, store=store) == 0                      # window over
    assert f(1, now=10_000.0, store=store) == 0 and set(store) == {1}      # stale entries are dropped


def test_check_endpoint_enforces_cooldown_after_the_permission_and_scope_checks():
    src = (ROOT / "app/api/settings.py").read_text()
    fn = src.split("def check_thresholds")[1].split("\n\n\n")[0]
    assert (fn.index("_require_account_access(account_id, current_user)") < fn.index("check_cooldown_remaining(account_id)")
            < fn.index("from app.aws.collector_direct import check_and_write_alerts"))      # cooldown after auth, before AWS
    assert "status_code=429" in fn and "Retry-After" in fn


def test_pages_are_code_split_but_the_shell_login_overview_and_404_are_not():
    app_jsx = _t("App.jsx")
    for eager in ("Layout", "Login", "Overview", "NotFound"):
        assert re.search(rf'^import {eager}\s+from', app_jsx, re.M), eager
    for page in ("Alerts", "Settings", "Topology", "Reports", "SecurityFindings", "StatusPagePublic"):
        assert f'const {page} = lazyWithRetry(() => import("./pages/{page}"));' in app_jsx, page
    assert "<Suspense" in _t("components/Layout.jsx") and "<Suspense" in app_jsx           # sidebar stays up while a page loads


def test_login_video_is_gated_and_forecasts_use_the_honest_formatter():
    login = _t("pages/Login.jsx")
    assert "shouldLoadBrandVideo(readEnvironment())" in login and 'preload="metadata"' in login
    assert "formatDaysLeft(" in _t("components/ResourceEvidence.jsx")
    assert "formatDaysLeftShort(c.days_to_exhaustion)" in _t("pages/overview/panels.jsx")


def test_labels_and_examples():
    assert "serviceLabelFor(a.service, a.resource)" in _t("pages/Alerts.jsx")
    assert "payment-api" not in _t("pages/Search.jsx") and "rds" not in _t("pages/Search.jsx").split("Plain English")[1].split("</p>")[0]
    assert "Open in cloud console" in _t("pages/ServiceDetail.jsx") and "Open in cloud console" in _t("pages/GenericServiceDetail.jsx")


def test_forgot_password_stays_enumeration_safe_and_never_returns_the_token():
    src = (ROOT / "app/api/auth.py").read_text()
    fn = src.split("def forgot_password")[1].split("\n@router")[0]
    assert "enforce_forgot_password_rate_limit(request)" in fn and "_token_hash(token)" in fn
    assert "Same response either way so usernames can't be enumerated." in fn
    assert not re.search(r'return\s*\{[^}]*token', fn)               # the token is never in the response body
