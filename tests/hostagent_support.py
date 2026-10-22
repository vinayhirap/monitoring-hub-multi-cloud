# tests/hostagent_support.py
"""
Shared helpers for the host agent tests: loads the REAL app/hostagents/{settings,auth}.py and app/api/host_agents.py by
path (tests/conftest.py convention) with everything they import from the rest of the app stubbed, and provides MiniDb,
an in-memory stand-in that understands exactly the SQL those modules issue (an unexpected statement fails loudly).
MiniDb cannot prove atomicity -- tests/test_host_agents_real_db.py does that against a real database.
"""
import copy
import sys
import threading
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.conftest import load_module, install_stub


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MiniDb:
    def __init__(self):
        self.accounts = {1: "Acct One", 2: "Acct Two", 3: "Acct Three"}
        self.keys, self.hosts, self.tokens = {}, {}, {}
        self._seq = {"k": 0, "h": 0, "t": 0}
        self.sql_log = []
        self.lock = threading.Lock()

    def _state(self):
        return (self.keys, self.hosts, self.tokens, dict(self._seq))

    def _restore(self, snap):
        self.keys, self.hosts, self.tokens, self._seq = snap

    def next(self, kind):
        self._seq[kind] += 1
        return self._seq[kind]

    def connect(self):
        return MiniConn(self)


class MiniConn:
    def __init__(self, db):
        self.db = db
        self._snap = copy.deepcopy(db._state())

    def cursor(self, dictionary=True):
        return MiniCursor(self.db)

    def commit(self):
        self._snap = copy.deepcopy(self.db._state())

    def rollback(self):
        self.db._restore(copy.deepcopy(self._snap))

    def close(self):
        pass


class MiniCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self.lastrowid = None
        self._rows = []

    def close(self):
        pass

    def fetchone(self):
        return copy.deepcopy(self._rows[0]) if self._rows else None

    def fetchall(self):
        return copy.deepcopy(self._rows)

    def _key_by_hash(self, h):
        return next((k for k in self.db.keys.values() if k["key_hash"] == h), None)

    def execute(self, sql, params=None):
        q, p, db = " ".join(sql.split()), tuple(params or ()), self.db
        db.sql_log.append((q, p))
        self.rowcount, self._rows, self.lastrowid = 0, [], None
        now = utcnow()

        if q.startswith("UPDATE host_agent_enrollment_keys SET used_count = used_count + 1"):
            k = self._key_by_hash(p[0])
            if k and not k["revoked"] and k["used_count"] < k["max_uses"] and (k["expires_at"] is None or k["expires_at"] > now):
                k["used_count"] += 1
                self.rowcount = 1
        elif q.startswith("SELECT id, aws_account_id FROM host_agent_enrollment_keys WHERE key_hash"):
            k = self._key_by_hash(p[0])
            self._rows = [{"id": k["id"], "aws_account_id": k["aws_account_id"]}] if k else []
        elif q == "SELECT id FROM aws_accounts WHERE id = %s":
            self._rows = [{"id": p[0]}] if p[0] in db.accounts else []
        elif q.startswith("INSERT INTO host_agents"):
            acct, host, inst, os_, arch, ver = p
            h = next((x for x in db.hosts.values() if x["aws_account_id"] == acct and x["hostname"] == host), None)
            if h:
                h.update(instance_id=inst, os=os_, arch=arch, agent_version=ver, status="active", last_seq=0)
                self.rowcount, self.lastrowid = 2, h["id"]
            else:
                hid = db.next("h")
                db.hosts[hid] = dict(id=hid, aws_account_id=acct, hostname=host, instance_id=inst, os=os_, arch=arch,
                                     agent_version=ver, status="active", last_seen_at=None, last_seq=0,
                                     config_version=1, config_json=None, resource_id=None)
                self.rowcount, self.lastrowid = 1, hid
        elif q.startswith("SELECT COUNT(*) AS n FROM host_agent_tokens WHERE host_agent_id"):
            self._rows = [{"n": sum(1 for t in db.tokens.values() if t["host_agent_id"] == p[0])}]
        elif q.startswith("UPDATE host_agent_tokens SET revoked_at"):
            for t in db.tokens.values():
                if t["host_agent_id"] == p[0] and t["revoked_at"] is None:
                    t["revoked_at"] = now
                    self.rowcount += 1
        elif q.startswith("INSERT INTO host_agent_tokens"):
            tid = db.next("t")
            db.tokens[tid] = dict(id=tid, host_agent_id=p[0], token_hash=p[1], revoked_at=None)
            self.lastrowid = tid
        elif q.startswith("SELECT config_json FROM host_agents WHERE id"):
            self._rows = [{"config_json": db.hosts[p[0]]["config_json"]}]
        elif q.startswith("SELECT h.id, h.aws_account_id, a.account_name"):
            rows = [h for h in db.hosts.values() if "1 = 0" not in q and ("IN (" not in q or h["aws_account_id"] in p)]
            self._rows = [dict(h, account_name=db.accounts.get(h["aws_account_id"])) for h in
                          sorted(rows, key=lambda x: (x["hostname"], x["id"]))]
        elif q.startswith("INSERT INTO host_agent_enrollment_keys"):
            acct, key_hash, label, max_uses, hours, by = p
            kid = db.next("k")
            db.keys[kid] = dict(id=kid, aws_account_id=acct, key_hash=key_hash, label=label, max_uses=max_uses,
                                used_count=0, expires_at=now + timedelta(hours=hours), revoked=0, created_by=by,
                                created_at=now)
            self.lastrowid = kid
        elif q.startswith("SELECT k.id, k.aws_account_id, a.account_name"):
            rows = [k for k in db.keys.values() if "1 = 0" not in q and ("IN (" not in q or k["aws_account_id"] in p)]
            self._rows = [{c: k[c] for c in ("id", "aws_account_id", "label", "max_uses", "used_count", "expires_at",
                                              "revoked", "created_by", "created_at")}
                          | {"account_name": db.accounts.get(k["aws_account_id"])}
                          for k in sorted(rows, key=lambda x: -x["id"])]
        elif q.startswith("SELECT id, aws_account_id, revoked FROM host_agent_enrollment_keys WHERE id"):
            k = db.keys.get(p[0])
            self._rows = [{"id": k["id"], "aws_account_id": k["aws_account_id"], "revoked": k["revoked"]}] if k else []
        elif q.startswith("UPDATE host_agent_enrollment_keys SET revoked = 1"):
            db.keys[p[0]]["revoked"] = 1
            self.rowcount = 1
        elif q.startswith("SELECT id, aws_account_id, hostname, status FROM host_agents WHERE id"):
            h = db.hosts.get(p[0])
            self._rows = [{c: h[c] for c in ("id", "aws_account_id", "hostname", "status")}] if h else []
        elif q.startswith("UPDATE host_agents SET status = 'revoked'"):
            db.hosts[p[0]]["status"] = "revoked"
            self.rowcount = 1
        elif q.startswith("SELECT id, aws_account_id, hostname, status, config_version, config_json FROM host_agents WHERE id"):
            h = db.hosts.get(p[0])
            self._rows = [{c: h[c] for c in ("id", "aws_account_id", "hostname", "status", "config_version", "config_json")}] if h else []
        elif q.startswith("UPDATE host_agents SET config_json"):
            h = db.hosts[p[1]]
            h["config_json"], h["config_version"] = p[0], h["config_version"] + 1
            self.rowcount = 1
        elif q.startswith("SELECT t.token_hash, t.revoked_at, h.id AS host_agent_id"):
            t = next((x for x in db.tokens.values() if x["token_hash"] == p[0]), None)
            if t:
                h = db.hosts[t["host_agent_id"]]
                self._rows = [dict(token_hash=t["token_hash"], revoked_at=t["revoked_at"], host_agent_id=h["id"],
                                   aws_account_id=h["aws_account_id"], hostname=h["hostname"],
                                   resource_id=h["resource_id"], config_version=h["config_version"], status=h["status"])]
        else:
            raise AssertionError(f"MiniDb: unexpected SQL: {q!r} params={p!r}")


class Env:
    """One isolated copy of the host agent modules wired to a MiniDb and stubbed app services."""

    def __init__(self, connect=None):
        """connect: optional zero-arg real-database connection factory (tests/test_host_agents_real_db.py)."""
        self.db = MiniDb()
        self.audit = []                      # (actor, action, detail, role)
        self.rate_calls = []
        self.rate_error = None
        self.user = None                     # None => 401
        self.accessible = None               # None => full access, else a set of account ids

        def get_connection():
            return connect() if connect else self.db.connect()

        def fake_require_permission(code):
            def _check():
                u = self.user
                if u is None:
                    raise HTTPException(status_code=401, detail="Not authenticated")
                if u["role"] != "admin" and code not in u["perms"]:
                    raise HTTPException(status_code=403, detail=f"Missing permission: {code}")
                return u
            return _check

        def check_rate_limit(key, max_attempts, window_seconds):
            self.rate_calls.append((key, max_attempts, window_seconds))
            if self.rate_error:
                raise self.rate_error

        install_stub("app.db", get_connection=get_connection)
        install_stub("app.account_names", account_label=lambda i: f"account {i}")
        install_stub("app.audit", write_audit=lambda actor, action, detail=None, **kw: self.audit.append(
            (actor, action, detail, kw.get("role"))))
        install_stub("app.auth")
        install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: self.accessible)
        install_stub("app.auth.permissions", require_permission=fake_require_permission)
        install_stub("app.auth.rate_limit", check_rate_limit=check_rate_limit, _client_ip=lambda req: "203.0.113.9")

        self.settings = load_module("app/hostagents/settings.py")
        install_stub("app.hostagents", settings=self.settings)
        sys.modules["app.hostagents.settings"] = self.settings
        self.auth = load_module("app/hostagents/auth.py")
        install_stub("app.hostagents", auth=self.auth)
        sys.modules["app.hostagents.auth"] = self.auth
        self.mod = load_module("app/api/host_agents.py")
        app = FastAPI()
        app.include_router(self.mod.router)
        self.client = TestClient(app, raise_server_exceptions=True)

    def as_user(self, role="admin", perms=(), accessible=None, username="alice"):
        self.user = {"username": username, "role": role, "perms": set(perms)}
        self.accessible = accessible

    def make_key(self, account=1, max_uses=1, hours=24, label="t"):
        self.as_user("admin")
        r = self.client.post("/api/host-agents/enrollment-keys",
                             json={"aws_account_id": account, "label": label, "max_uses": max_uses, "expires_in_hours": hours})
        assert r.status_code == 200, r.text
        return r.json()

    def enroll(self, key, hostname="web-1", **extra):
        body = {"enrollment_key": key, "hostname": extra.pop("hostname", hostname), "os": "linux", "arch": "amd64",
                "agent_version": "0.1.0"}
        body.update(extra)
        return self.client.post("/api/host-agents/enroll", json=body)
