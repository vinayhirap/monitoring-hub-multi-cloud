# tests/test_audit_b20.py
"""Audit b20: synthetic SSRF guard, CSPM reconcile, SLO math, status page scoping."""
import socket
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

from tests.conftest import load_module, install_stub, FakeConn


def _synthetic():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    return load_module("app/collector/synthetic.py")


@pytest.mark.parametrize("ip,blocked", [
    ("169.254.169.254", True), ("127.0.0.1", True), ("10.1.2.3", True),
    ("172.16.0.1", True), ("192.168.1.1", True), ("100.100.100.200", True),
    ("0.0.0.0", True), ("::1", True), ("fd00:ec2::254", True),
    ("::ffff:169.254.169.254", True), ("64:ff9b::a9fe:a9fe", True),
    ("8.8.8.8", False), ("2606:4700:4700::1111", False),
])
def test_blocked_ip_ranges(ip, blocked):
    assert _synthetic()._is_blocked_ip(ip) is blocked


def test_allowlist_opens_private_but_never_metadata():
    import ipaddress
    mod = _synthetic()
    mod._ALLOWED_PRIVATE_NETS = [ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("169.254.0.0/16")]
    assert mod._is_blocked_ip("10.1.2.3") is False
    assert mod._is_blocked_ip("169.254.169.254") is True


@pytest.mark.parametrize("ctype,target", [
    ("http", "http://169.254.169.254/latest/meta-data/"),
    ("http", "http://127.0.0.1:8000/api/health"),
    ("http", "http://[::1]/"),
    ("http", "ftp://example.com/"),
    ("tcp", "127.0.0.1:3306"),
    ("tcp", "10.0.0.5:6379"),
    ("tcp", "example.com:99999"),
])
def test_validate_target_rejects(ctype, target):
    with pytest.raises(ValueError):
        _synthetic().validate_target(ctype, target)


def test_validate_target_accepts_public_literal():
    mod = _synthetic()
    mod.validate_target("http", "https://8.8.8.8/")
    mod.validate_target("tcp", "8.8.8.8:53")
    mod.validate_target("dns", "example.com")


def test_http_probe_blocks_dns_rebinding_at_connect(monkeypatch):
    mod = _synthetic()
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "rebind.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]
        return real(host, *a, **k)

    monkeypatch.setattr(mod.socket, "getaddrinfo", fake)
    ok, _, status, err = mod._probe_http("http://rebind.test/latest/meta-data/", 2, None, "ami-id")
    assert ok is False and status is None and err.startswith("blocked")


def test_tcp_probe_blocks_loopback():
    ok, _, _, err = _synthetic()._probe_tcp("127.0.0.1:3306", 2)
    assert ok is False and err.startswith("blocked")


def test_timeout_clamped():
    mod = _synthetic()
    assert mod._clamp_timeout(3600) == mod.MAX_TIMEOUT_SECONDS
    assert mod._clamp_timeout(0) == mod.MIN_TIMEOUT_SECONDS


def test_resource_row_tags_are_valid_json():
    import json
    mod = _synthetic()
    calls = []

    class Cur:
        def execute(self, sql, params=None):
            calls.append(params)
    mod._ensure_resource_row(Cur(), {"id": 1, "aws_account_id": 2, "name": "x", "environment": 'pr"od'})
    assert json.loads(calls[0][3]) == {"environment": 'pr"od'}


def test_synthetic_alert_insert_sets_account():
    mod = _synthetic()
    calls = []

    class Cur:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return None
    mod._write_or_update_alert(Cur(), "synthetic-7", {
        "aws_account_id": 42, "environment": "prod", "consecutive_failures": 2,
        "consecutive_failure_threshold": 2, "name": "n", "id": 7}, "down")
    insert = [c for c in calls if c[0].startswith("INSERT INTO alerts")][0]
    assert "aws_account_id" in insert[0] and insert[1][0] == 42


# ── CSPM ─────────────────────────────────────────────────────────────

def _cspm():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.aws.sts", get_boto3_session=lambda a: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    return load_module("app/collector/cspm.py")


def test_merge_keeps_highest_severity():
    mod = _cspm()
    merged = mod._merge_findings([
        {"check_id": "sg_open_to_world", "resource_id": "sg-1", "severity": "HIGH", "title": "t", "description": "22"},
        {"check_id": "sg_open_to_world", "resource_id": "sg-1", "severity": "LOW", "title": "t", "description": "443"},
    ])
    assert len(merged) == 1 and merged[0]["severity"] == "HIGH"
    assert "22" in merged[0]["description"] and "443" in merged[0]["description"]


def test_failed_check_does_not_auto_resolve():
    mod = _cspm()
    updates = []

    class Cur:
        def execute(self, sql, params=None):
            n = " ".join(sql.split())
            self._rows = []
            if n.startswith("SELECT id, check_id, resource_id, region FROM security_findings"):
                self._rows = [
                    {"id": 1, "check_id": "iam_user_no_mfa", "resource_id": "bob", "region": None},
                    {"id": 2, "check_id": "ebs_unencrypted", "resource_id": "vol-1", "region": "us-east-1"},
                ]
            elif n.startswith("UPDATE security_findings"):
                updates.append(params)

        def fetchall(self):
            return self._rows
    mod._upsert_findings(Cur(), 5, [], failed_checks={"iam_user_no_mfa"})
    assert updates == [(2,)]


def test_check_run_records_failure():
    mod = _cspm()
    run = mod._CheckRun()

    def boom():
        raise RuntimeError("AccessDenied")
    run.run("x", boom)
    run.run("y", lambda: [{"check_id": "y"}])
    assert run.failed_checks == {"x"} and len(run.findings) == 1


# ── SLO ──────────────────────────────────────────────────────────────

def _slo():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return load_module("app/api/slo.py")


class _OneRowCur:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


def test_slo_100pct_target_with_downtime_is_breached():
    mod = _slo()
    cur = _OneRowCur({"total": 100, "successful": 99})
    out = mod._compute_status(cur, {"window_days": 30, "target_pct": 100, "synthetic_check_id": 3,
                                    "aws_account_id": 9, "resource_id": None, "metric_name": None})
    assert out["status"] == "breached"
    assert "c.aws_account_id = %s" in cur.calls[0][0] and cur.calls[0][1][0] == 9


def test_slo_100pct_target_clean_is_ok():
    mod = _slo()
    out = mod._compute_status(_OneRowCur({"total": 10, "successful": 10}),
                              {"window_days": 30, "target_pct": 100, "synthetic_check_id": 3,
                               "aws_account_id": 9, "resource_id": None, "metric_name": None})
    assert out["status"] == "ok" and out["budget_remaining_pct"] == 100


def test_slo_resource_mode_scoped_to_account():
    mod = _slo()
    cur = _OneRowCur({"bad_seconds": 0})
    mod._compute_status(cur, {"window_days": 7, "target_pct": 99.9, "synthetic_check_id": None,
                              "aws_account_id": 9, "resource_id": "i-1", "metric_name": None})
    assert "a.aws_account_id = %s" in cur.calls[0][0] and 9 in cur.calls[0][1]


@pytest.mark.parametrize("bad", [0, 101, "x", -5])
def test_slo_target_validation(bad):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _slo()._validate_target_pct(bad)


# ── Status page ─────────────────────────────────────────────────────

def _status_page(perms=()):
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.deps", get_current_user=lambda: None, require_role=lambda *a: (lambda: None))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None),
                 has_permission=lambda user, code: code in perms)
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return load_module("app/api/status_page.py")


def test_component_rejects_foreign_resource_ids():
    from fastapi import HTTPException
    mod = _status_page()

    class Cur:
        def execute(self, sql, params=None):
            self.params = params

        def fetchall(self):
            return [{"resource_id": "i-mine"}]
    cur = Cur()
    with pytest.raises(HTTPException):
        mod._validate_resource_ids(cur, 1, ["i-mine", "i-other-account"])
    assert cur.params[0] == 1
    assert mod._validate_resource_ids(cur, 1, ["i-mine", "i-mine"]) == ["i-mine"]


def test_public_status_page_is_cached():
    mod = _status_page()
    calls = []

    def build():
        calls.append(1)
        return {"overall_status": "operational", "components": [], "recent_events": [], "generated_at": "x"}
    mod._build_public_status_page = build
    a = mod.public_status_page()
    b = mod.public_status_page()
    assert a == b and len(calls) == 1
    mod._invalidate_public_cache()
    mod.public_status_page()
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [
    {"timeout_seconds": 3600}, {"interval_seconds": 10}, {"environment": "x" * 60},
    {"expected_status_code": "abc"}, {"consecutive_failure_threshold": 0},
])
def test_synthetic_api_bounds(payload):
    from fastapi import HTTPException
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    mod = load_module("app/api/synthetic.py")
    with pytest.raises(HTTPException) as exc:
        mod._validate_common(payload)
    assert exc.value.status_code == 400


# ── Follow-up: F17/F23/F24/F25/F26/F27 ───────────────────────────────

def test_region_failure_only_blocks_that_region():
    mod = _cspm()
    updates = []
    rows = [
        {"id": 1, "check_id": "sg_open_to_world", "resource_id": "sg-a", "region": "eu-west-1"},
        {"id": 2, "check_id": "sg_open_to_world", "resource_id": "sg-b", "region": "us-east-1"},
        {"id": 3, "check_id": "sg_open_to_world", "resource_id": "sg-legacy", "region": None},
    ]

    class Cur:
        def execute(self, sql, params=None):
            n = " ".join(sql.split())
            self._rows = rows if n.startswith("SELECT id, check_id") else []
            if n.startswith("UPDATE security_findings"):
                updates.append(params)

        def fetchall(self):
            return self._rows
    mod._upsert_findings(Cur(), 5, [], failed_checks={("sg_open_to_world", "eu-west-1")})
    assert updates == [(2,)]  # eu-west-1 kept, legacy (no region) kept


def test_regional_finding_region_is_stored():
    mod = _cspm()
    inserts = []

    class Cur:
        def execute(self, sql, params=None):
            if "INSERT INTO security_findings" in sql:
                inserts.append(params)

        def fetchall(self):
            return []
    mod._upsert_findings(Cur(), 5, [{"check_id": "ebs_unencrypted", "resource_id": "vol-1", "region": "ap-south-1",
                                     "severity": "MEDIUM", "title": "t", "description": "d"}])
    assert inserts[0][3] == "ap-south-1"


def test_aws_regions_uses_enabled_regions_default_first(monkeypatch):
    mod = _cspm()

    class EC2:
        def describe_regions(self, AllRegions=False):
            return {"Regions": [{"RegionName": "us-west-2"}, {"RegionName": "ap-south-1"}, {"RegionName": "bogus"}]}

    class Session:
        def client(self, *a, **k):
            return EC2()
    monkeypatch.delenv("CSPM_AWS_REGIONS", raising=False)
    regions = mod._aws_regions(Session(), {"id": 1, "default_region": "ap-south-1"})
    assert regions[0] == "ap-south-1" and set(regions) == {"ap-south-1", "us-west-2"}
    monkeypatch.setenv("CSPM_AWS_REGIONS", "ap-south-1")
    assert mod._aws_regions(Session(), {"id": 1, "default_region": "ap-south-1"}) == ["ap-south-1"]


def test_aws_regions_falls_back_when_describe_denied():
    mod = _cspm()
    mod._discovered_regions = lambda account_id: {"eu-central-1"}

    class Session:
        def client(self, *a, **k):
            raise RuntimeError("AccessDenied")
    regions = mod._aws_regions(Session(), {"id": 1, "default_region": "us-east-1"})
    assert regions == ["us-east-1", "eu-central-1"]


def test_slo_new_check_not_extrapolated_over_window():
    mod = _slo()
    # 288 probes (1 day @ 5 min), 3 failed -> 15 bad minutes. 30-day
    # 99.9% budget = 43.2 min -> ~34.7% consumed, NOT 'breached'.
    out = mod._compute_status(_OneRowCur({"total": 288, "successful": 285, "interval_seconds": 300}),
                              {"window_days": 30, "target_pct": 99.9, "synthetic_check_id": 3,
                               "aws_account_id": 9, "resource_id": None, "metric_name": None})
    assert out["status"] == "ok" and 34 <= out["budget_consumed_pct"] <= 35


def test_status_page_list_allows_view_permission():
    from fastapi import HTTPException
    mod = _status_page(perms=("status_page.view",))
    assert mod._require_view_or_manage({"id": 1}) == {"id": 1}
    mod = _status_page(perms=())
    with pytest.raises(HTTPException):
        mod._require_view_or_manage({"id": 1})


def test_delete_check_removes_resource_row():
    sqls = []

    class Cur:
        def execute(self, sql, params=None):
            sqls.append((" ".join(sql.split()), params))
            self._row = {"aws_account_id": 4}

        def fetchone(self):
            return self._row

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass
    install_stub("app.db", get_connection=lambda: Conn())
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    mod = load_module("app/api/synthetic.py")
    mod.delete_check(7, current_user={"id": 1, "username": "u"})
    res = [p for s, p in sqls if s.startswith("DELETE FROM resources")]
    assert res == [(4, "synthetic-7")]


def _security(rows_by_prefix):
    calls = []

    class Cur:
        def execute(self, sql, params=None):
            n = " ".join(sql.split())
            calls.append((n, params))
            self._rows = next((v for k, v in rows_by_prefix.items() if n.startswith(k)), [])

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=True):
            return Cur()

        def close(self):
            pass
    install_stub("app.db", get_connection=lambda: Conn())
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return load_module("app/api/security.py"), calls


def test_findings_list_bounded_active_only_with_total_header():
    from fastapi import Response
    mod, calls = _security({"SELECT COUNT(*)": [{"n": 1234}], "SELECT f.*": [{"id": 1}]})
    resp = Response()
    out = mod.list_findings(status="open", severity=None, account_id=None, limit=50, offset=100,
                            response=resp, current_user={"id": 1})
    assert out == [{"id": 1}] and resp.headers["X-Total-Count"] == "1234"
    sel = [c for c in calls if c[0].startswith("SELECT f.*")][0]
    assert "acc.status = 'active'" in sel[0] and "LIMIT %s OFFSET %s" in sel[0] and sel[1][-2:] == (50, 100)


def test_findings_summary_active_accounts_only():
    mod, calls = _security({})
    mod.findings_summary(current_user={"id": 1})
    assert "acc.status = 'active'" in calls[0][0]
