# tests/test_synthetic_cert_thresholds.py
"""The Synthetic Checks UI follows the cert-expiry thresholds the backend alert really uses, and the
SYNTHETIC_* operator settings are documented.

Before: SyntheticChecks.jsx and CheckDetail.jsx hard-coded 30 / 7, so setting SYNTHETIC_CERT_WARN_DAYS changed
the alert but not the colours or the chart's dashed lines. Now GET /synthetic sends cert_warn_days /
cert_crit_days on every https row and both components read them through synthetic/certThresholds.js.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from dotenv import dotenv_values

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import app  # noqa: F401,E402
import app.auth  # noqa: F401,E402
from tests.conftest import load_module, install_stub, FakeConn  # noqa: E402


# ── API ──────────────────────────────────────────────────────────────

def _api(accessible=None):
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    return load_module("app/api/synthetic.py")


def _rows_conn(rows):
    class Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [dict(r) for r in rows]

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def close(self):
            pass
    return Conn()


_ROWS = [
    {"id": 1, "aws_account_id": 4, "check_type": "https", "name": "a"},
    {"id": 2, "aws_account_id": 4, "check_type": "http", "name": "b"},
    {"id": 3, "aws_account_id": 4, "check_type": "tcp", "name": "c"},
    {"id": 4, "aws_account_id": 9, "check_type": "https", "name": "other account"},
]


def test_list_sends_thresholds_on_https_rows_only(monkeypatch):
    mod = _api()
    monkeypatch.setattr(mod, "get_connection", lambda: _rows_conn(_ROWS))
    out = {r["id"]: r for r in mod.list_checks(current_user={"id": 1})}
    assert (out[1]["cert_warn_days"], out[1]["cert_crit_days"]) == (30, 7)      # the defaults, nothing configured
    assert (out[4]["cert_warn_days"], out[4]["cert_crit_days"]) == (30, 7)
    for plain in (2, 3):
        assert "cert_warn_days" not in out[plain] and "cert_crit_days" not in out[plain]


def test_list_reflects_the_values_the_alert_is_using(monkeypatch):
    mod = _api()
    monkeypatch.setattr(mod, "get_connection", lambda: _rows_conn(_ROWS))
    monkeypatch.setattr(mod, "CERT_WARN_DAYS", 45)
    monkeypatch.setattr(mod, "CERT_CRIT_DAYS", 10)
    out = {r["id"]: r for r in mod.list_checks(current_user={"id": 1})}
    assert (out[1]["cert_warn_days"], out[1]["cert_crit_days"]) == (45, 10)


def test_api_and_collector_share_one_source_of_truth():
    mod = _api()
    coll = load_module("app/collector/synthetic.py")
    assert (mod.CERT_WARN_DAYS, mod.CERT_CRIT_DAYS) == (coll.CERT_WARN_DAYS, coll.CERT_CRIT_DAYS)


def test_list_still_scopes_rows_to_accessible_accounts(monkeypatch):
    mod = _api(accessible={4})                                                 # RBAC filter is unchanged
    monkeypatch.setattr(mod, "get_connection", lambda: _rows_conn(_ROWS))
    ids = sorted(r["id"] for r in mod.list_checks(current_user={"id": 1}))
    assert ids == [1, 2, 3]


# ── frontend helper (real JS, via node) ──────────────────────────────

HELPER = os.path.join(ROOT, "frontend", "src", "pages", "synthetic", "certThresholds.js")


def _node(expr):
    script = ("import * as m from 'file://" + HELPER + "';\n"
              "console.log(JSON.stringify(" + expr + "));\n")
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
@pytest.mark.parametrize("check,expected", [
    ({"cert_warn_days": 45, "cert_crit_days": 10}, {"warn": 45, "crit": 10}),
    ({"cert_warn_days": 400, "cert_crit_days": 7}, {"warn": 400, "crit": 7}),
    ({"cert_warn_days": "45", "cert_crit_days": "10"}, {"warn": 45, "crit": 10}),      # JSON numbers/strings both fine
    ({}, {"warn": 30, "crit": 7}),                                                     # API that does not send them yet
    (None, {"warn": 30, "crit": 7}),
    ({"cert_warn_days": 7, "cert_crit_days": 7}, {"warn": 30, "crit": 7}),             # crit must be < warn
    ({"cert_warn_days": 5, "cert_crit_days": 9}, {"warn": 30, "crit": 7}),
    ({"cert_warn_days": 30.5, "cert_crit_days": 7}, {"warn": 30, "crit": 7}),
    ({"cert_warn_days": 30, "cert_crit_days": 0}, {"warn": 30, "crit": 7}),
    ({"cert_warn_days": None, "cert_crit_days": None}, {"warn": 30, "crit": 7}),
])
def test_cert_thresholds_helper(check, expected):
    assert _node(f"m.certThresholds({json.dumps(check)})") == expected


@node
def test_cert_tone_follows_the_thresholds_in_use():
    cases = [(None, ""), (400, "syn-cert-warn"), (401, "syn-cert-ok"), (80, "syn-cert-warn"),
             (8, "syn-cert-warn"), (7, "syn-cert-crit"), (0, "syn-cert-crit"), (-2, "syn-cert-crit")]
    got = _node("[" + ",".join(f"m.certTone({json.dumps(d)}, {{warn: 400, crit: 7}})" for d, _ in cases) + "]")
    assert got == [t for _, t in cases]
    # and with the defaults the old behaviour is unchanged
    d = [None, 31, 30, 8, 7, 0]
    assert _node("[" + ",".join(f"m.certTone({json.dumps(x)}, m.certThresholds({{}}))" for x in d) + "]") == \
        ["", "syn-cert-ok", "syn-cert-warn", "syn-cert-warn", "syn-cert-crit", "syn-cert-crit"]


def test_no_hard_coded_thresholds_left_in_the_components():
    for rel in ("SyntheticChecks.jsx", "synthetic/CheckDetail.jsx"):
        src = open(os.path.join(ROOT, "frontend", "src", "pages", rel), encoding="utf-8").read()
        assert "CERT_WARN_DAYS" not in src and "CERT_CRIT_DAYS" not in src, rel
        assert "certThresholds" in src, rel


# ── operator settings are documented ─────────────────────────────────

def _env_names_read_by_code():
    src = open(os.path.join(ROOT, "app", "collector", "synthetic.py"), encoding="utf-8").read()
    return set(re.findall(r"SYNTHETIC_[A-Z0-9_]+", src))


@pytest.mark.parametrize("fname", [".env.example", ".env.production.example"])
def test_every_synthetic_setting_the_code_reads_is_documented(fname):
    text = open(os.path.join(ROOT, fname), encoding="utf-8").read()
    missing = sorted(n for n in _env_names_read_by_code() if n not in text)
    assert not missing, f"{fname} does not document: {missing}"


@pytest.mark.parametrize("fname", [".env.example", ".env.production.example"])
def test_documented_synthetic_settings_are_inert_comments(fname):
    # copying the example to .env must not switch anything on
    active = [k for k in dotenv_values(os.path.join(ROOT, fname)) if k.startswith("SYNTHETIC_")]
    assert active == []


def test_the_code_reads_exactly_the_known_settings():
    # a new SYNTHETIC_* setting must be added here AND to both example files
    assert _env_names_read_by_code() == {
        "SYNTHETIC_ALLOWED_PRIVATE_CIDRS", "SYNTHETIC_CA_BUNDLE", "SYNTHETIC_CERT_CRIT_DAYS",
        "SYNTHETIC_CERT_WARN_DAYS", "SYNTHETIC_HTTPS_REDIRECT_MODE",
    }
