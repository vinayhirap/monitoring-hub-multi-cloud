# tests/test_email_rule_anomaly_0008.py
"""
Email & Reports centre, patch 0008: an anomaly alert (multivariate_anomaly.py) that was just INSERTED is offered to the email
rules after the commit. Refreshes of an open alert, resolves, maintenance windows and a failing hook change nothing.
"""
import sys
import types

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import pytest  # noqa: E402

from tests.conftest import load_module  # noqa: E402
from tests.test_multivariate_anomaly import _history_with_joint_anomaly_at_end, _install_db_stub  # noqa: E402


class _Cur:
    """Dictionary cursor for _queue_rule_mail: answers the account-name lookup."""
    def __init__(self, names=None, fail_names=False):
        self.names, self.fail_names, self.sqls = names or {}, fail_names, []

    def execute(self, sql, params=None):
        self.sqls.append((" ".join(sql.split()), params))
        if self.fail_names:
            raise RuntimeError("db down")
        self._rows = [{"id": i, "account_name": self.names[i]} for i in (params or ()) if i in self.names]

    def fetchall(self):
        return self._rows


def _stub(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, mod)
    parent, _, leaf = name.rpartition(".")
    pkg = sys.modules.get(parent)
    if pkg is None:                                          # never import the real package: it would open the database config
        pkg = types.ModuleType(parent)
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, parent, pkg)
    monkeypatch.setattr(pkg, leaf, mod, raising=False)       # `from pkg import leaf` reads this attribute first
    return mod


@pytest.fixture
def mv(monkeypatch):
    # monkeypatch-based stubs (restored after each test): conftest.install_stub would overwrite attributes of a REAL
    # app.environment_classifier / app.env_tiers that an earlier test file already imported, and leak into later files
    _stub(monkeypatch, "app.db", get_connection=lambda: None)
    _stub(monkeypatch, "app.environment_classifier", flag_enabled=lambda: False)   # lazily imported by the insert
    _stub(monkeypatch, "app.env_tiers", to_tier=lambda v: str(v))
    return load_module("app/collector/multivariate_anomaly.py")


def _env(monkeypatch, enabled=True, covered=None, covered_raises=False):
    queued = []
    _stub(monkeypatch, "app.email.delivery_log", enabled=lambda: enabled)
    _stub(monkeypatch, "app.email.rule_dispatch", enqueue_alert_event=lambda e, a: queued.append((e, a)) or True)

    def silenced(cur):
        if covered_raises:
            raise RuntimeError("cannot read windows")
        return dict(covered or {})
    _stub(monkeypatch, "app.collector.maintenance", active_silenced_map=silenced)
    return queued


def test_a_new_anomaly_alert_is_queued_as_alert_opened_with_the_account_name(mv, monkeypatch):
    queued = _env(monkeypatch)
    mv._queue_rule_mail(_Cur({7: "Prod"}), [(501, 7, "i-1")])
    assert queued == [("alert_opened", {"id": 501, "severity": "WARNING", "metric_name": "multivariate_anomaly",
                                        "resource_id": "i-1", "aws_account_id": 7, "account_name": "Prod", "silenced": False})]


def test_one_name_lookup_for_all_new_alerts_and_a_failed_lookup_still_queues(mv, monkeypatch):
    queued = _env(monkeypatch)
    cur = _Cur({7: "Prod", 8: "Dev"})
    mv._queue_rule_mail(cur, [(1, 7, "i-1"), (2, 8, "i-2"), (3, 7, "i-3")])
    assert len(queued) == 3 and len(cur.sqls) == 1 and cur.sqls[0][1] == (7, 8)
    queued = _env(monkeypatch)
    mv._queue_rule_mail(_Cur(fail_names=True), [(1, 7, "i-1")])
    assert len(queued) == 1 and queued[0][1]["account_name"] is None


def test_a_resource_in_a_maintenance_window_is_not_queued(mv, monkeypatch):
    queued = _env(monkeypatch, covered={(7, "i-1"): "Maintenance window: x"})
    mv._queue_rule_mail(_Cur({7: "Prod"}), [(1, 7, "i-1"), (2, 7, "i-2"), (3, 8, "i-1")])
    assert [a["id"] for _, a in queued] == [2, 3]            # same resource id in another account is a different resource


def test_nothing_is_queued_when_the_windows_cannot_be_read(mv, monkeypatch):
    queued = _env(monkeypatch, covered_raises=True)
    mv._queue_rule_mail(_Cur(), [(1, 7, "i-1")])             # must not raise
    assert queued == []


def test_the_hook_is_inert_and_runs_no_query_when_the_feature_is_off(mv, monkeypatch):
    queued = _env(monkeypatch, enabled=False)
    cur = _Cur({7: "Prod"})
    mv._queue_rule_mail(cur, [(1, 7, "i-1")])
    assert queued == [] and cur.sqls == []
    mv._queue_rule_mail(cur, [])                             # nothing opened: nothing to do
    assert cur.sqls == []


def test_a_failing_queue_never_raises_into_the_detector(mv, monkeypatch):
    _env(monkeypatch)

    def boom(e, a):
        raise RuntimeError("queue exploded")
    _stub(monkeypatch, "app.email.rule_dispatch", enqueue_alert_event=boom)
    mv._queue_rule_mail(_Cur({7: "Prod"}), [(1, 7, "i-1")])


# ── what the detector hands to the hook ──────────────────────────────

class _InsertCur:
    def __init__(self, existing, lastrowid=77):
        self.existing, self.lastrowid, self.inserts, self.updates = existing, lastrowid, 0, 0

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        self._rows = []
        if n.startswith("SELECT id FROM alerts"):
            self._rows = [{"id": 9}] if self.existing else []
        elif n.startswith("SELECT resource_type, tags"):
            self._rows = [{"resource_type": "ec2_instance", "tags": "{}"}]
        elif n.startswith("INSERT INTO alerts"):
            self.inserts += 1
        elif n.startswith("UPDATE alerts SET current_value"):
            self.updates += 1

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_the_upsert_returns_the_new_id_only_for_an_inserted_row(mv):
    c = _InsertCur(existing=False)
    assert mv._upsert_anomaly_alert(c, "i-1", 7, -0.2, ["cpu"]) == 77 and c.inserts == 1
    c = _InsertCur(existing=True)
    assert mv._upsert_anomaly_alert(c, "i-1", 7, -0.2, ["cpu"]) is None and c.inserts == 0 and c.updates == 1
    c = _InsertCur(existing=False, lastrowid=0)
    assert mv._upsert_anomaly_alert(c, "i-1", 7, -0.2, ["cpu"]) is None


def test_the_detector_queues_after_the_commit_and_only_for_inserted_rows(monkeypatch):
    for existing, expected in ((None, 1), ({"id": 9}, 0)):
        _install_db_stub(_history_with_joint_anomaly_at_end(n=80, buckets=3), existing_alert=existing)
        _stub(monkeypatch, "app.environment_classifier", flag_enabled=lambda: False)
        _stub(monkeypatch, "app.env_tiers", to_tier=lambda v: str(v))
        mod = load_module("app/collector/multivariate_anomaly.py")
        seen = []
        orig = mod._upsert_anomaly_alert
        monkeypatch.setattr(mod, "_upsert_anomaly_alert", lambda cur, *a, **k: (orig(cur, *a, **k), 501 if existing is None else None)[1])
        monkeypatch.setattr(mod, "_queue_rule_mail", lambda cur, opened: seen.append(list(opened)))
        assert mod.detect_multivariate_anomalies() == 1
        assert len(seen) == 1 and len(seen[0]) == expected
        if expected:
            assert seen[0][0][0] == 501 and seen[0][0][1] == 7


def test_the_queue_call_sits_after_the_commit_and_inside_no_error_path():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app/collector/multivariate_anomaly.py").read_text()
    assert src.count("\n        _queue_rule_mail(cursor, opened)") == 1                       # one call site
    assert src.index("conn.commit()\n        _queue_rule_mail(cursor, opened)") > src.index("_resolve_cleared_anomalies(cursor")
    assert "send_email" not in src and "dispatch_many" not in src
