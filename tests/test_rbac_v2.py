"""
tests/test_rbac_v2.py

Covers the PURE decision logic of app/auth/rbac.py -- scope
containment, the deny/allow precedence, delegation containment, and
the generated SQL filter. No database: these are the parts where a
subtle bug silently grants access, so they are tested in isolation
rather than only through an endpoint.

The DB-backed half (resolve/_load, the legacy compatibility shim) is
covered by the integration tests, which need a real schema.
"""
import sys
import types

import pytest

# app.db builds a MySQL connection pool at import time, which these
# tests neither have nor need. Stubbed before importing the module
# under test; every function exercised here is pure.
if "app.db" not in sys.modules:
    _stub = types.ModuleType("app.db")
    _stub.get_connection = lambda: (_ for _ in ()).throw(
        AssertionError("pure-logic test must not touch the database")
    )
    sys.modules["app.db"] = _stub

from app.auth.rbac import (  # noqa: E402
    AccessFilter, Binding, Denial, ResolvedAccess, Scope, Target,
    _scope_contains, accessible_filter, can, explain,
)

GLOBAL = Scope(label="Organization")
PROD_MUMBAI_COMPUTE = Scope(
    id=7, label="Prod AWS / Mumbai / compute",
    cloud="aws", account_ref_id=2,
    regions=["ap-south-1"], services=["ec2", "rds"],
)


# ── Scope.covers ────────────────────────────────────────────────────

def test_global_scope_covers_everything():
    assert GLOBAL.covers(Target(cloud="gcp", account_id=99,
                                region="us-central1", service="bigquery"))


@pytest.mark.parametrize("target,expected", [
    (Target(cloud="aws", account_id=2, region="ap-south-1", service="ec2"), True),
    (Target(cloud="aws", account_id=2, region="ap-south-1", service="rds"), True),
    (Target(cloud="aws", account_id=2, region="us-east-1",  service="ec2"), False),
    (Target(cloud="aws", account_id=2, region="ap-south-1", service="lambda"), False),
    (Target(cloud="aws", account_id=3, region="ap-south-1", service="ec2"), False),
    (Target(cloud="gcp", account_id=2, region="ap-south-1", service="ec2"), False),
])
def test_each_dimension_discriminates(target, expected):
    assert PROD_MUMBAI_COMPUTE.covers(target) is expected


@pytest.mark.parametrize("target", [
    Target(cloud="aws", account_id=2, service="ec2"),            # region unknown
    Target(cloud="aws", account_id=2, region="ap-south-1"),      # service unknown
    Target(cloud="aws", account_id=2),                           # both unknown
])
def test_restricted_dimension_denies_unknown_target_value(target):
    """
    The safety property that v1 got wrong. If a scope pins a
    dimension and the caller cannot say what the object's value is on
    that dimension, the answer must be NO. Allowing it is how a scope
    that names a region ends up never actually restricting anything.
    """
    assert PROD_MUMBAI_COMPUTE.covers(target) is False


def test_tag_selector_is_and_across_keys_or_within():
    scope = Scope(cloud="aws", tag_selector={
        "Environment": ["prod", "staging"], "Team": ["payments"],
    })
    assert scope.covers(Target(cloud="aws",
                               tags={"Environment": "prod", "Team": "payments"}))
    assert scope.covers(Target(cloud="aws",
                               tags={"Environment": "staging", "Team": "payments"}))
    # Missing one required key -> deny.
    assert not scope.covers(Target(cloud="aws", tags={"Environment": "prod"}))
    assert not scope.covers(Target(cloud="aws", tags={}))
    assert not scope.covers(Target(cloud="aws"))


# ── Delegation containment (privilege escalation gate) ──────────────

def test_global_contains_any_scope():
    assert _scope_contains(GLOBAL, PROD_MUMBAI_COMPUTE)


def test_narrow_scope_does_not_contain_global():
    assert not _scope_contains(PROD_MUMBAI_COMPUTE, GLOBAL)


def test_contains_strict_subset():
    narrower = Scope(cloud="aws", account_ref_id=2,
                     regions=["ap-south-1"], services=["ec2"])
    assert _scope_contains(PROD_MUMBAI_COMPUTE, narrower)
    assert not _scope_contains(narrower, PROD_MUMBAI_COMPUTE)


@pytest.mark.parametrize("attempt", [
    # Actor is pinned to ec2+rds; asking for "all services" is the escalation.
    Scope(cloud="aws", account_ref_id=2, regions=["ap-south-1"], services=None),
    # Pinned to one account; asking for the whole cloud.
    Scope(cloud="aws", account_ref_id=None, regions=["ap-south-1"], services=["ec2"]),
    # Pinned to one region; asking for all regions.
    Scope(cloud="aws", account_ref_id=2, regions=None, services=["ec2"]),
])
def test_cannot_widen_a_pinned_dimension(attempt):
    assert not _scope_contains(PROD_MUMBAI_COMPUTE, attempt)


# ── Decision precedence ─────────────────────────────────────────────

def _access(bindings, denials=()):
    return ResolvedAccess(user_id=1, bindings=list(bindings), denials=list(denials))


def _patched(monkeypatch, access):
    monkeypatch.setattr("app.auth.rbac.resolve", lambda user: access)
    return {"id": 1, "role": "editor"}


def test_allow_when_binding_covers_target(monkeypatch):
    access = _access([Binding("editor", 20, frozenset({"alerts.resolve"}),
                              PROD_MUMBAI_COMPUTE)])
    user = _patched(monkeypatch, access)
    assert can(user, "alerts.resolve",
               Target(cloud="aws", account_id=2, region="ap-south-1", service="ec2"))


def test_permission_held_but_target_out_of_scope(monkeypatch):
    access = _access([Binding("editor", 20, frozenset({"alerts.resolve"}),
                              PROD_MUMBAI_COMPUTE)])
    user = _patched(monkeypatch, access)
    assert not can(user, "alerts.resolve",
                   Target(cloud="aws", account_id=2, region="us-east-1", service="ec2"))
    assert explain(user, "alerts.resolve",
                   Target(cloud="aws", account_id=2, region="us-east-1",
                          service="ec2"))["reason"] == "out_of_scope"


def test_deny_beats_a_global_admin_binding(monkeypatch):
    """Explicit deny outranks every allow, including admin-at-global."""
    access = _access(
        bindings=[Binding("admin", 30, frozenset({"operations.execute"}), GLOBAL)],
        denials=[Denial("operations.execute", PROD_MUMBAI_COMPUTE)],
    )
    user = _patched(monkeypatch, access)
    in_denied = Target(cloud="aws", account_id=2, region="ap-south-1", service="ec2")
    assert not can(user, "operations.execute", in_denied)
    assert explain(user, "operations.execute", in_denied)["reason"] == "explicit_deny"
    # ...but only where the deny's scope reaches.
    assert can(user, "operations.execute",
               Target(cloud="aws", account_id=5, region="us-east-1", service="ec2"))


def test_deny_by_default(monkeypatch):
    user = _patched(monkeypatch, _access([]))
    assert not can(user, "alerts.view", Target(cloud="aws", account_id=1))
    assert explain(user, "alerts.view")["reason"] == "no_permission"


def test_permission_specific_scopes_coexist(monkeypatch):
    """Editor on prod, Viewer on dev -- the case v1 could not express."""
    dev = Scope(id=8, cloud="aws", account_ref_id=3)
    access = _access([
        Binding("editor", 20, frozenset({"alerts.view", "alerts.resolve"}),
                PROD_MUMBAI_COMPUTE),
        Binding("viewer", 10, frozenset({"alerts.view"}), dev),
    ])
    user = _patched(monkeypatch, access)
    prod = Target(cloud="aws", account_id=2, region="ap-south-1", service="ec2")
    dev_t = Target(cloud="aws", account_id=3, region="ap-south-1", service="ec2")

    assert can(user, "alerts.view", prod)
    assert can(user, "alerts.view", dev_t)
    assert can(user, "alerts.resolve", prod)
    assert not can(user, "alerts.resolve", dev_t)      # viewer on dev


# ── Query filter generation ─────────────────────────────────────────

def test_unrestricted_filter_is_a_noop():
    assert AccessFilter(unrestricted=True).sql() == ("1=1", [])


def test_no_access_filter_matches_nothing():
    where, params = AccessFilter(account_ids=set()).sql()
    assert where == "1=0" and params == []


def test_filter_applies_all_three_dimensions():
    f = AccessFilter(
        account_ids={2, 3}, services={"ec2", "rds"},
        regions_by_account={2: {"ap-south-1"}, 3: None},
    )
    where, params = f.sql("r")
    assert "r.aws_account_id IN (%s,%s)" in where
    assert "r.resource_type IN (%s,%s)" in where
    assert "r.region IN (%s)" in where
    # Account 3 is unrestricted on region -> no region clause for it.
    assert where.count("r.region IN") == 1
    assert params == [2, 3, "ec2", "rds", 2, "ap-south-1", 3]


def test_empty_service_set_matches_nothing():
    """
    services=set() means 'no services permitted', which must produce
    1=0 -- NOT an omitted clause. Conflating empty-set with None is
    the classic RBAC filter bug and turns a total denial into a total
    grant.
    """
    where, _ = AccessFilter(account_ids={2}, services=set()).sql()
    assert where == "1=0"


# ── accessible_filter() honours deny overrides (audit b02, CRIT-1) ──
# Before this fix, accessible_filter() only ever looked at
# access.bindings -- a Denial resolved onto the same ResolvedAccess
# was silently ignored by every list endpoint that filters through
# this function, even though can()/assert_can() (tested above) already
# enforce it correctly for single-object checks. These tests pin the
# fixed behaviour so a future refactor can't reopen the gap.

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self._rows
    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
    def cursor(self, dictionary=True):
        return _FakeCursor(self._rows)
    def close(self):
        pass


ACCOUNTS = [
    {"id": 2, "provider": "aws"},
    {"id": 3, "provider": "aws"},
]


def _patched_filter(monkeypatch, access):
    monkeypatch.setattr("app.auth.rbac.resolve", lambda user: access)
    monkeypatch.setattr("app.auth.rbac.get_connection", lambda: _FakeConn(ACCOUNTS))
    return {"id": 1, "role": "editor"}


def test_unconditional_deny_empties_the_filter(monkeypatch):
    """A scope=None deny revokes the permission everywhere -- the
    filter must match nothing, not fall back to the allow bindings."""
    access = _access(
        bindings=[Binding("admin", 30, frozenset({"alerts.view"}), GLOBAL)],
        denials=[Denial("alerts.view", scope=None)],
    )
    user = _patched_filter(monkeypatch, access)
    f = accessible_filter(user, "alerts.view")
    assert f.sql() == ("1=0", [])


def test_scoped_deny_removes_just_that_account(monkeypatch):
    dev = Scope(id=9, cloud="aws", account_ref_id=3)
    access = _access(
        bindings=[Binding("editor", 20, frozenset({"alerts.view"}), GLOBAL)],
        denials=[Denial("alerts.view", scope=dev)],
    )
    user = _patched_filter(monkeypatch, access)
    f = accessible_filter(user, "alerts.view")
    assert f.account_ids == {2}


def test_scoped_deny_by_region_narrows_without_dropping_the_account(monkeypatch):
    prod = Scope(id=7, cloud="aws", account_ref_id=2, regions=["ap-south-1", "us-east-1"])
    deny_region = Scope(id=10, cloud="aws", account_ref_id=2, regions=["us-east-1"])
    access = _access(
        bindings=[Binding("editor", 20, frozenset({"alerts.view"}), prod)],
        denials=[Denial("alerts.view", scope=deny_region)],
    )
    user = _patched_filter(monkeypatch, access)
    f = accessible_filter(user, "alerts.view")
    assert f.account_ids == {2}
    assert f.regions_by_account[2] == {"ap-south-1"}


def test_deny_scoped_by_service_fails_closed_to_whole_account(monkeypatch):
    """
    A deny narrowed by service can't be represented per-account by
    this filter's flat `services` set (it applies across every
    account, not one). Rather than silently ignore that part of the
    deny -- which is what the pre-fix code effectively did for every
    deny -- the whole matched account is dropped: fail closed, not
    fail open.
    """
    prod = Scope(id=7, cloud="aws", account_ref_id=2)
    deny_service = Scope(id=11, cloud="aws", account_ref_id=2, services=["rds"])
    access = _access(
        bindings=[Binding("editor", 20, frozenset({"alerts.view"}), prod)],
        denials=[Denial("alerts.view", scope=deny_service)],
    )
    user = _patched_filter(monkeypatch, access)
    f = accessible_filter(user, "alerts.view")
    assert 2 not in f.account_ids


def test_global_allow_with_scoped_deny_expands_and_subtracts(monkeypatch):
    """A global (org-wide) allow combined with one scoped deny must no
    longer collapse to unconditional 'unrestricted=True' -- it has to
    expand to concrete accounts so the deny has something to remove."""
    dev = Scope(id=9, cloud="aws", account_ref_id=3)
    access = _access(
        bindings=[Binding("admin", 30, frozenset({"alerts.view"}), GLOBAL)],
        denials=[Denial("alerts.view", scope=dev)],
    )
    user = _patched_filter(monkeypatch, access)
    f = accessible_filter(user, "alerts.view")
    assert f.unrestricted is False
    assert f.account_ids == {2}


def test_global_allow_with_no_denials_is_still_unrestricted(monkeypatch):
    """No applicable deny -> unchanged fast path, still a plain 1=1."""
    access = _access(
        bindings=[Binding("admin", 30, frozenset({"alerts.view"}), GLOBAL)],
    )
    user = _patched_filter(monkeypatch, access)
    f = accessible_filter(user, "alerts.view")
    assert f.unrestricted is True
    assert f.sql() == ("1=1", [])


def test_deny_for_a_different_permission_is_not_applied(monkeypatch):
    dev = Scope(id=9, cloud="aws", account_ref_id=3)
    prod_and_dev = Scope(cloud="aws")
    access = _access(
        bindings=[Binding("editor", 20, frozenset({"alerts.view"}), prod_and_dev)],
        denials=[Denial("alerts.resolve", scope=dev)],
    )
    user = _patched_filter(monkeypatch, access)
    f = accessible_filter(user, "alerts.view")
    assert f.account_ids == {2, 3}
