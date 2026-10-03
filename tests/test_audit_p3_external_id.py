# tests/test_audit_p3_external_id.py
"""Audit E7: IAM-role onboarding requires an External ID (confused-deputy guard)."""
import sys
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "app/api/admin/accounts.py").read_text()


def _fns():
    # Pull just the two helpers out of the module source: importing the router drags in the
    # whole auth/DB stack, and this rule is pure logic.
    start = SRC.index("def _external_id_required()")
    end = SRC.index("def _add_aws_account")
    ns = {"os": __import__("os")}

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail
    ns["HTTPException"] = HTTPException
    exec(SRC[start:end], ns)
    return ns, HTTPException


def test_role_without_external_id_is_rejected(monkeypatch):
    monkeypatch.delenv("REQUIRE_EXTERNAL_ID", raising=False)
    ns, HTTPException = _fns()
    try:
        ns["_check_external_id"]("arn:aws:iam::123456789012:role/CloudOps", "")
        assert False, "should raise"
    except HTTPException as e:
        assert e.status_code == 400 and "External ID" in e.detail


def test_role_with_external_id_passes(monkeypatch):
    monkeypatch.delenv("REQUIRE_EXTERNAL_ID", raising=False)
    ns, _ = _fns()
    ns["_check_external_id"]("arn:aws:iam::123456789012:role/CloudOps", "abc-123")


def test_no_role_arn_means_no_requirement(monkeypatch):
    monkeypatch.delenv("REQUIRE_EXTERNAL_ID", raising=False)
    ns, _ = _fns()
    ns["_check_external_id"]("", "")          # ambient-credentials / static-key accounts


def test_opt_out_env(monkeypatch):
    monkeypatch.setenv("REQUIRE_EXTERNAL_ID", "false")
    ns, _ = _fns()
    ns["_check_external_id"]("arn:aws:iam::123456789012:role/CloudOps", "")


def test_enforced_on_add_and_test_role_but_only_for_assume_role():
    assert 'if auth_mode == "assume_role":\n        _check_external_id(role_arn, external_id)' in SRC
    assert "_check_external_id(role_arn, ext_id)" in SRC
    jsx = (ROOT / "frontend/src/pages/AccountOnboarding.jsx").read_text()
    assert "External ID is required for IAM role access" in jsx
