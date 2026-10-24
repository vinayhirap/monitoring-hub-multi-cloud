# tests/test_host_agents_foundation.py
"""
Host agent phase B: migration 120, token/key primitives, authenticate_agent, enroll, enrollment keys, host list /
revoke / config, the HOST_AGENT_ENABLED gate, RBAC scoping, and "secrets are never returned or logged after creation".
No database needed (MiniDb); the single-use race against a real database is tests/test_host_agents_real_db.py.
"""
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.hostagent_support import Env, utcnow

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "db/migrations/120_host_agent_server_foundation.sql"
BASE = "/api/host-agents"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    monkeypatch.delenv("HOST_AGENT_STALE_SECONDS", raising=False)
    return Env()


def _enrolled(env, account=1, hostname="web-1"):
    k = env.make_key(account=account, max_uses=5)
    r = env.enroll(k["key"], hostname)
    assert r.status_code == 200, r.text
    return k, r.json()


# ── migration ────────────────────────────────────────────────────────────────

def test_migration_has_the_contract_tables_and_columns():
    sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text())
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 3
    for frag in ("key_hash       CHAR(64) NOT NULL", "max_uses       INT NOT NULL DEFAULT 1", "used_count     INT NOT NULL DEFAULT 0",
                 "expires_at     DATETIME NULL", "revoked        TINYINT(1) NOT NULL DEFAULT 0",
                 "status         ENUM('active','revoked') NOT NULL DEFAULT 'active'", "last_seq       BIGINT NOT NULL DEFAULT 0",
                 "config_version INT NOT NULL DEFAULT 1", "config_json    JSON NULL",
                 "UNIQUE KEY uq_host_agent (aws_account_id, hostname)", "token_hash     CHAR(64) NOT NULL",
                 "REFERENCES host_agents (id) ON DELETE CASCADE"):
        assert frag in sql, frag
    assert "REFERENCES users" not in sql and "REFERENCES aws_accounts" not in sql      # audit trail must survive deletes


def test_migration_permissions_in_both_role_tables_and_idempotent():
    sql = MIGRATION.read_text()
    legacy = dict((r, set(re.findall(r"'([a-z.]+)'", c))) for r, c in re.findall(
        r"INSERT IGNORE INTO role_permissions \(role, permission_id\)\s+SELECT '(\w+)', id FROM permissions WHERE code IN \(([^)]*)\)", sql))
    v2 = dict((r, set(re.findall(r"'([a-z.]+)'", c))) for r, c in re.findall(
        r"WHERE r\.role_key = '(\w+)' AND p\.code IN \(([^)]*)\)", sql))
    expected = {"viewer": {"hostagents.view"}, "editor": {"hostagents.view"}, "admin": {"hostagents.view", "hostagents.manage"}}
    assert legacy == expected and v2 == expected
    stmts = [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]
    assert all(s.startswith(("CREATE TABLE IF NOT EXISTS", "INSERT IGNORE INTO")) for s in stmts)
    assert "hostagents.manage" not in legacy["viewer"] | legacy["editor"]


# ── primitives ───────────────────────────────────────────────────────────────

def test_token_and_key_formats_and_hash(env):
    a = env.auth
    t, k = a.generate_token(), a.generate_enrollment_key()
    assert re.fullmatch(r"cah_[A-Za-z0-9_-]{40}", t) and re.fullmatch(r"cak_[A-Za-z0-9_-]{32}", k)
    assert a.generate_token() != a.generate_token()
    h = a.hash_secret(t)
    assert re.fullmatch(r"[0-9a-f]{64}", h) and h != t and t not in h
    assert a.looks_like_token(t) and not a.looks_like_token(k) and not a.looks_like_token("cah_short")
    assert a.looks_like_enrollment_key(k) and not a.looks_like_enrollment_key(t) and not a.looks_like_enrollment_key(None)


# ── authenticate_agent ───────────────────────────────────────────────────────

def test_authenticate_agent_returns_exactly_the_contract_and_writes_nothing(env):
    _, enrolled = _enrolled(env)
    before = len(env.db.sql_log)
    ctx = env.auth.authenticate_agent(enrolled["agent_token"])
    assert ctx == {"host_agent_id": enrolled["host_agent_id"], "aws_account_id": 1, "hostname": "web-1",
                   "resource_id": None, "config_version": 1}
    issued = [q for q, _ in env.db.sql_log[before:]]
    assert issued and all(q.startswith("SELECT") for q in issued)


def test_authenticate_agent_rejects_unknown_malformed_revoked_and_disabled(env, monkeypatch):
    _, enrolled = _enrolled(env)
    tok = enrolled["agent_token"]
    assert env.auth.authenticate_agent(env.auth.generate_token()) is None
    assert env.auth.authenticate_agent("garbage") is None and env.auth.authenticate_agent(None) is None
    # revoked token
    for t in env.db.tokens.values():
        t["revoked_at"] = utcnow()
    assert env.auth.authenticate_agent(tok) is None
    # revoked host with a live token row
    for t in env.db.tokens.values():
        t["revoked_at"] = None
    env.db.hosts[enrolled["host_agent_id"]]["status"] = "revoked"
    assert env.auth.authenticate_agent(tok) is None
    env.db.hosts[enrolled["host_agent_id"]]["status"] = "active"
    assert env.auth.authenticate_agent(tok) is not None
    monkeypatch.setenv("HOST_AGENT_ENABLED", "false")
    assert env.auth.authenticate_agent(tok) is None


def test_authenticate_agent_uses_constant_time_compare(env, monkeypatch):
    _, enrolled = _enrolled(env)
    calls = []
    real = env.auth.hmac.compare_digest
    monkeypatch.setattr(env.auth.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert env.auth.authenticate_agent(enrolled["agent_token"]) is not None
    assert calls


# ── feature flag ─────────────────────────────────────────────────────────────

def test_every_route_is_404_when_disabled_even_unauthenticated_and_touches_nothing(env, monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "false")
    env.user = None
    c, n = env.client, len(env.db.sql_log)
    responses = [c.post(f"{BASE}/enroll", json={}), c.post(f"{BASE}/ingest", json={}), c.get(BASE),
                 c.post(f"{BASE}/enrollment-keys", json={}), c.get(f"{BASE}/enrollment-keys"),
                 c.delete(f"{BASE}/enrollment-keys/1"), c.post(f"{BASE}/1/revoke"), c.patch(f"{BASE}/1/config", json={})]
    assert [r.status_code for r in responses] == [404] * 8
    assert len(env.db.sql_log) == n and not env.rate_calls


def test_flag_defaults_to_false(monkeypatch, env):
    monkeypatch.delenv("HOST_AGENT_ENABLED", raising=False)
    assert env.settings.enabled() is False
    assert env.client.get(BASE).status_code == 404

# ── enrollment keys: lifecycle ───────────────────────────────────────────────

def test_key_lifecycle_create_list_revoke_then_enroll_fails(env, caplog):
    caplog.set_level(logging.DEBUG)
    created = env.make_key(account=1, max_uses=2, label="web fleet")
    assert re.fullmatch(r"cak_[A-Za-z0-9_-]{32}", created["key"]) and set(created) == {"key", "id"}
    stored = env.db.keys[created["id"]]
    assert stored["key_hash"] == env.auth.hash_secret(created["key"]) and created["key"] not in json.dumps(stored, default=str)

    listed = env.client.get(f"{BASE}/enrollment-keys")
    assert listed.status_code == 200
    row = listed.json()[0]
    assert row["status"] == "active" and row["max_uses"] == 2 and row["used_count"] == 0 and row["account"] == "Acct One"
    assert "key" not in row and "key_hash" not in row and created["key"] not in listed.text and stored["key_hash"] not in listed.text

    assert env.client.delete(f"{BASE}/enrollment-keys/{created['id']}").json() == {"id": created["id"], "revoked": True}
    assert env.client.get(f"{BASE}/enrollment-keys").json()[0]["status"] == "revoked"
    assert env.enroll(created["key"]).status_code == 401
    n_audit = len(env.audit)
    assert env.client.delete(f"{BASE}/enrollment-keys/{created['id']}").status_code == 200    # idempotent
    assert len(env.audit) == n_audit                                                           # no second audit row
    actions = [a[1] for a in env.audit]
    assert actions == ["Host agent enrollment key created", "Host agent enrollment key revoked"]
    assert created["key"] not in caplog.text and created["key"] not in json.dumps(env.audit)


@pytest.mark.parametrize("mutate", ["expired", "exhausted", "revoked", "unknown", "malformed"])
def test_enroll_rejects_bad_keys_with_one_indistinguishable_error(env, mutate):
    k = env.make_key(max_uses=1)
    row = env.db.keys[k["id"]]
    key = k["key"]
    if mutate == "expired":
        row["expires_at"] = utcnow() - timedelta(seconds=1)
    elif mutate == "exhausted":
        row["used_count"] = 1
    elif mutate == "revoked":
        row["revoked"] = 1
    elif mutate == "unknown":
        key = env.auth.generate_enrollment_key()
    else:
        key = "cak_tooshort"
    r = env.enroll(key)
    assert r.status_code == 401 and r.json()["detail"] == "Invalid or expired enrollment key"
    assert not env.db.hosts and not env.db.tokens
    assert row["used_count"] == (1 if mutate == "exhausted" else 0)


def test_single_use_key_works_once(env):
    k = env.make_key(max_uses=1)
    assert env.enroll(k["key"], "a").status_code == 200
    assert env.enroll(k["key"], "b").status_code == 401
    assert env.db.keys[k["id"]]["used_count"] == 1 and len(env.db.hosts) == 1


def test_enroll_validity_is_one_conditional_update_not_read_then_write(env):
    k = env.make_key()
    env.db.sql_log.clear()
    env.enroll(k["key"])
    first = env.db.sql_log[0][0]
    for cond in ("used_count = used_count + 1", "revoked = 0", "used_count < max_uses", "expires_at > UTC_TIMESTAMP()"):
        assert cond in first, cond
    assert not any(q.startswith("SELECT") and "used_count" in q for q, _ in env.db.sql_log)


def test_account_with_no_row_cannot_enroll_and_key_use_rolls_back(env):
    k = env.make_key(account=1)
    del env.db.accounts[1]
    assert env.enroll(k["key"]).status_code == 401
    assert env.db.keys[k["id"]]["used_count"] == 0 and not env.db.hosts


# ── enroll ───────────────────────────────────────────────────────────────────

def test_enroll_returns_token_once_and_stores_only_hashes(env, caplog):
    caplog.set_level(logging.DEBUG)
    k = env.make_key(account=2)
    r = env.enroll(k["key"], "db-1", instance_id="i-0abc123def", os="linux", arch="arm64")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"host_agent_id", "agent_token", "config"}
    assert re.fullmatch(r"cah_[A-Za-z0-9_-]{40}", body["agent_token"])
    assert body["config"] == {"interval_seconds": 60, "watch_services": [],
                              "disk_exclude_fstypes": ["tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs"],
                              "max_batch_metrics": 500}
    host = env.db.hosts[body["host_agent_id"]]
    assert (host["aws_account_id"], host["hostname"], host["instance_id"], host["arch"], host["status"]) == (2, "db-1", "i-0abc123def", "arm64", "active")
    (tok,) = env.db.tokens.values()
    assert tok["token_hash"] == env.auth.hash_secret(body["agent_token"]) and body["agent_token"] not in json.dumps(tok, default=str)
    # not in later reads, logs or audit
    assert body["agent_token"] not in env.client.get(BASE).text
    assert body["agent_token"] not in caplog.text and k["key"] not in caplog.text
    assert body["agent_token"] not in json.dumps(env.audit) and k["key"] not in json.dumps(env.audit)
    assert env.audit[-1][1] == "Host agent enrolled" and env.audit[-1][0] == "host-agent"
    assert env.rate_calls == [("host-agent-enroll:203.0.113.9", 20, 3600)]


def test_re_enroll_same_account_and_hostname_rotates_token_and_keeps_admin_config(env):
    k = env.make_key(max_uses=3)
    first = env.enroll(k["key"], "web-1").json()
    env.as_user("admin")
    assert env.client.patch(f"{BASE}/{first['host_agent_id']}/config", json={"interval_seconds": 120, "watch_services": ["nginx"]}).status_code == 200
    second = env.enroll(k["key"], "web-1", agent_version="0.2.0").json()
    assert second["host_agent_id"] == first["host_agent_id"] and len(env.db.hosts) == 1
    assert second["agent_token"] != first["agent_token"]
    assert env.auth.authenticate_agent(first["agent_token"]) is None
    assert env.auth.authenticate_agent(second["agent_token"])["host_agent_id"] == first["host_agent_id"]
    assert second["config"]["interval_seconds"] == 120 and second["config"]["watch_services"] == ["nginx"]
    assert sum(1 for t in env.db.tokens.values() if t["revoked_at"] is None) == 1
    assert env.db.hosts[first["host_agent_id"]]["agent_version"] == "0.2.0"
    assert "re-enrolled" in env.audit[-1][2]


def test_same_hostname_in_two_accounts_is_two_hosts(env):
    a, b = env.make_key(account=1), env.make_key(account=2)
    assert env.enroll(a["key"], "web-1").json()["host_agent_id"] != env.enroll(b["key"], "web-1").json()["host_agent_id"]


def test_enroll_is_rate_limited_before_the_key_is_checked(env):
    k = env.make_key()
    env.rate_error = HTTPException(status_code=429, detail="Too many attempts. Try again in 60 seconds.")
    assert env.enroll(k["key"]).status_code == 429
    assert env.db.keys[k["id"]]["used_count"] == 0 and not env.db.hosts


@pytest.mark.parametrize("bad", [{"hostname": "bad host"}, {"hostname": ""}, {"hostname": "x" * 256}, {"os": ""},
                                 {"arch": "a" * 21}, {"instance_id": "i-1; DROP"}, {"agent_version": "1.0 beta"}])
def test_enroll_validates_agent_supplied_fields(env, bad):
    k = env.make_key()
    assert env.enroll(k["key"], **bad).status_code == 422
    assert env.db.keys[k["id"]]["used_count"] == 0 and not env.db.hosts


# ── list + derived state ─────────────────────────────────────────────────────

def test_state_derivation_boundaries(env):
    d, now = env.mod._derive_state, datetime(2026, 10, 23, 12, 0, 0)
    ago = lambda s: now - timedelta(seconds=s)
    assert d("active", ago(0), now, 180) == "online" and d("active", ago(180), now, 180) == "online"
    assert d("active", ago(181), now, 180) == "stale" and d("active", ago(900), now, 180) == "stale"
    assert d("active", ago(901), now, 180) == "offline"
    assert d("active", None, now, 180) == "offline"
    assert d("revoked", ago(1), now, 180) == "revoked"


def test_list_returns_state_using_the_stale_env_and_never_secrets(env, monkeypatch):
    _, e1 = _enrolled(env, hostname="a")
    _, e2 = _enrolled(env, hostname="b")
    _, e3 = _enrolled(env, hostname="c")
    now = utcnow()
    env.db.hosts[e1["host_agent_id"]]["last_seen_at"] = now - timedelta(seconds=30)
    env.db.hosts[e2["host_agent_id"]]["last_seen_at"] = now - timedelta(seconds=400)
    env.db.hosts[e3["host_agent_id"]]["last_seen_at"] = now - timedelta(seconds=30)
    env.as_user("viewer", {"hostagents.view"})
    r = env.client.get(BASE)
    assert r.status_code == 200
    assert {h["hostname"]: h["state"] for h in r.json()} == {"a": "online", "b": "stale", "c": "online"}
    assert set(r.json()[0]) == {"id", "aws_account_id", "account", "hostname", "instance_id", "os", "arch",
                                "agent_version", "status", "last_seen_at", "state"}
    assert r.json()[0]["last_seen_at"].endswith("Z")
    for e in (e1, e2, e3):
        assert e["agent_token"] not in r.text
    monkeypatch.setenv("HOST_AGENT_STALE_SECONDS", "20")
    assert {h["hostname"]: h["state"] for h in env.client.get(BASE).json()} == {"a": "stale", "b": "offline", "c": "stale"}
    env.as_user("admin")
    env.client.post(f"{BASE}/{e1['host_agent_id']}/revoke")
    env.as_user("viewer", {"hostagents.view"})
    assert {h["hostname"]: h["state"] for h in env.client.get(BASE).json()}["a"] == "revoked"


# ── permissions and account scoping ──────────────────────────────────────────

def test_permissions(env):
    k = env.make_key()
    env.user = None
    assert env.client.get(BASE).status_code == 401
    env.as_user("viewer", {"help.view"})
    assert env.client.get(BASE).status_code == 403
    env.as_user("viewer", {"hostagents.view"})
    assert env.client.get(BASE).status_code == 200
    assert env.client.get(f"{BASE}/enrollment-keys").status_code == 403
    assert env.client.post(f"{BASE}/enrollment-keys", json={"aws_account_id": 1}).status_code == 403
    assert env.client.delete(f"{BASE}/enrollment-keys/{k['id']}").status_code == 403
    assert env.client.post(f"{BASE}/1/revoke").status_code == 403
    assert env.client.patch(f"{BASE}/1/config", json={"interval_seconds": 30}).status_code == 403


def test_scope_leakage_user_of_account_1_cannot_see_or_touch_account_2(env):
    k1, e1 = _enrolled(env, account=1, hostname="one")
    k2, e2 = _enrolled(env, account=2, hostname="two")
    h2 = env.db.hosts[e2["host_agent_id"]]
    env.as_user("admin", accessible={1})            # a manager whose scope is account 1 only

    listed = env.client.get(BASE).json()
    assert [h["hostname"] for h in listed] == ["one"]
    keys = env.client.get(f"{BASE}/enrollment-keys").json()
    assert [k["id"] for k in keys] == [k1["id"]]

    assert env.client.post(f"{BASE}/{e2['host_agent_id']}/revoke").status_code == 404
    assert env.client.patch(f"{BASE}/{e2['host_agent_id']}/config", json={"interval_seconds": 30}).status_code == 404
    assert env.client.delete(f"{BASE}/enrollment-keys/{k2['id']}").status_code == 404
    assert env.client.post(f"{BASE}/enrollment-keys", json={"aws_account_id": 2}).status_code == 404
    assert h2["status"] == "active" and h2["config_version"] == 1 and not env.db.keys[k2["id"]]["revoked"]
    assert env.auth.authenticate_agent(e2["agent_token"]) is not None
    assert len(env.db.keys) == 2                                    # no key was created for account 2
    # same answer for a host that does not exist: no existence oracle
    assert env.client.post(f"{BASE}/9999/revoke").status_code == 404

    assert env.client.post(f"{BASE}/{e1['host_agent_id']}/revoke").status_code == 200


def test_empty_scope_sees_nothing(env):
    _enrolled(env)
    env.as_user("viewer", {"hostagents.view"}, accessible=set())
    assert env.client.get(BASE).json() == []
    env.as_user("admin", accessible=set())
    assert env.client.get(f"{BASE}/enrollment-keys").json() == []


def test_key_for_missing_account_is_404(env):
    env.as_user("admin")
    assert env.client.post(f"{BASE}/enrollment-keys", json={"aws_account_id": 99}).status_code == 404


# ── revoke host ──────────────────────────────────────────────────────────────

def test_revoke_host_revokes_all_tokens_blocks_auth_and_is_idempotent(env):
    k, e = _enrolled(env)
    env.enroll(k["key"], "web-1")                   # second token generation, first already revoked
    env.as_user("admin")
    r = env.client.post(f"{BASE}/{e['host_agent_id']}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "revoked" and r.json()["tokens_revoked"] == 1
    assert all(t["revoked_at"] is not None for t in env.db.tokens.values())
    assert env.db.hosts[e["host_agent_id"]]["status"] == "revoked"
    assert env.auth.authenticate_agent(e["agent_token"]) is None
    n = len(env.audit)
    again = env.client.post(f"{BASE}/{e['host_agent_id']}/revoke")
    assert again.status_code == 200 and again.json()["tokens_revoked"] == 0 and len(env.audit) == n
    assert env.audit[-1][1] == "Host agent revoked"
    # a revoked host can be re-enrolled with a fresh key
    k2 = env.make_key()
    env.as_user("admin")
    r2 = env.enroll(k2["key"], "web-1")
    assert r2.status_code == 200 and env.db.hosts[e["host_agent_id"]]["status"] == "active"


# ── config ───────────────────────────────────────────────────────────────────

def test_patch_config_bumps_version_validates_and_audits(env):
    _, e = _enrolled(env)
    env.as_user("admin")
    url = f"{BASE}/{e['host_agent_id']}/config"
    r = env.client.patch(url, json={"watch_services": ["nginx", "sshd", "nginx"], "interval_seconds": 30})
    assert r.status_code == 200
    assert r.json() == {"id": e["host_agent_id"], "config_version": 2,
                        "config": {"interval_seconds": 30, "watch_services": ["nginx", "sshd"],
                                   "disk_exclude_fstypes": ["tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs"],
                                   "max_batch_metrics": 500}}
    assert env.client.patch(url, json={"interval_seconds": 15}).json()["config_version"] == 3
    assert env.client.patch(url, json={"watch_services": []}).json()["config"]["watch_services"] == []
    assert json.loads(env.db.hosts[e["host_agent_id"]]["config_json"]) == {"watch_services": [], "interval_seconds": 15}
    assert env.auth.authenticate_agent(e["agent_token"])["config_version"] == 4
    assert [a[1] for a in env.audit].count("Host agent config changed") == 3
    for bad in ({"interval_seconds": 14}, {"interval_seconds": 301}, {"interval_seconds": "x"}, {"watch_services": ["a b"]},
                {"watch_services": ["x" * 101]}, {"watch_services": [f"s{i}" for i in range(101)]}, {"bogus": 1},
                {"disk_exclude_fstypes": []}):
        assert env.client.patch(url, json=bad).status_code == 422, bad
    assert env.client.patch(url, json={}).status_code == 400
    assert env.db.hosts[e["host_agent_id"]]["config_version"] == 4          # rejected requests changed nothing


def test_patch_config_on_revoked_host_is_409(env):
    _, e = _enrolled(env)
    env.as_user("admin")
    env.client.post(f"{BASE}/{e['host_agent_id']}/revoke")
    assert env.client.patch(f"{BASE}/{e['host_agent_id']}/config", json={"interval_seconds": 30}).status_code == 409


def test_key_create_validation(env):
    env.as_user("admin")
    for bad in ({"aws_account_id": 1, "max_uses": 0}, {"aws_account_id": 1, "expires_in_hours": 0},
                {"aws_account_id": 1, "expires_in_hours": 8761}, {"aws_account_id": 0}, {"aws_account_id": 1, "label": "x" * 101},
                {"aws_account_id": 1, "extra": 1}):
        assert env.client.post(f"{BASE}/enrollment-keys", json=bad).status_code == 422, bad
    assert not env.db.keys


# ── RBAC matrix (generated from the real app, in a subprocess like test_audit_p13) ───────────────────────────

def test_rbac_matrix_lists_every_host_agent_route_with_the_right_guard():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/rbac_matrix.py"), "--json"], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    rows = {(m, r["path"]): r for r in json.loads(proc.stdout) for m in r["methods"] if r["path"].startswith(BASE)}
    expected = {
        ("POST", f"{BASE}/enroll"): ("public", []), ("POST", f"{BASE}/ingest"): ("public", []),
        ("GET", BASE): ("permission", ["hostagents.view"]),
        ("GET", f"{BASE}/{{host_id}}/metrics"): ("permission", ["hostagents.view"]),
        ("POST", f"{BASE}/enrollment-keys"): ("permission", ["hostagents.manage"]),
        ("GET", f"{BASE}/enrollment-keys"): ("permission", ["hostagents.manage"]),
        ("DELETE", f"{BASE}/enrollment-keys/{{key_id}}"): ("permission", ["hostagents.manage"]),
        ("POST", f"{BASE}/{{host_id}}/revoke"): ("permission", ["hostagents.manage"]),
        ("PATCH", f"{BASE}/{{host_id}}/config"): ("permission", ["hostagents.manage"]),
    }
    assert {k: (v["level"], v["permissions"]) for k, v in rows.items()} == expected


def test_nothing_outside_host_agent_files_is_touched_by_the_ai_agent_namespace():
    src = (ROOT / "app/api/host_agents.py").read_text() + (ROOT / "app/hostagents/auth.py").read_text()
    assert "app.agent" not in src and "/api/agent" not in src
