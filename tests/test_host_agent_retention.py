# tests/test_host_agent_retention.py
"""Host agent phase F: the new credential rows are pruned safely, and ingested samples are covered by metric_history retention."""
import sys
from pathlib import Path

import app          # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _Cur:
    def __init__(self, plan, log):
        self.plan, self.log, self.rowcount = plan, log, 0
    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        self.rowcount = self.plan.pop(0) if self.plan else 0


class _Conn:
    def __init__(self, plan, log):
        self.plan, self.log = plan, log
    def cursor(self):
        return _Cur(self.plan, self.log)
    def commit(self):
        pass
    def close(self):
        pass


def _mod(plan=None):
    log, plan = [], plan if plan is not None else []
    install_stub("app.db", get_connection=lambda: _Conn(plan, log))
    return load_module("app/collector/retention.py"), log


def test_only_dead_credentials_are_deleted(monkeypatch):
    monkeypatch.delenv("HOST_AGENT_CREDENTIAL_RETENTION_DAYS", raising=False)
    m, log = _mod([3, 2])
    assert m.prune_host_agent_credentials(sleep=lambda s: None) == 5
    (tok_sql, tok_params), (key_sql, key_params) = log
    assert "FROM host_agent_tokens" in tok_sql and "revoked_at IS NOT NULL" in tok_sql and "revoked_at <" in tok_sql
    assert "FROM host_agent_enrollment_keys" in key_sql
    # a key is only removable when it is revoked, used up or expired -- never merely old
    assert "revoked = 1 OR used_count >= max_uses OR (expires_at IS NOT NULL AND expires_at < UTC_TIMESTAMP())" in key_sql
    assert "created_at <" in key_sql
    assert tok_params == (90,) and key_params == (90,)
    assert "host_agents " not in tok_sql + key_sql.replace("host_agent_enrollment_keys", "")


def test_window_floor_and_disable(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_CREDENTIAL_RETENTION_DAYS", "3")
    m, log = _mod()
    m.prune_host_agent_credentials(sleep=lambda s: None)
    assert [p for _, p in log] == [(30,), (30,)]          # a typo cannot wipe recent credentials
    monkeypatch.setenv("HOST_AGENT_CREDENTIAL_RETENTION_DAYS", "0")
    m, log = _mod()
    assert m.prune_host_agent_credentials() == 0 and log == []


def test_wired_into_run_retention():
    src = (ROOT / "app/collector/retention.py").read_text()
    assert '("host_agent_credentials", prune_host_agent_credentials)' in src


def test_ingested_samples_ride_the_existing_metric_history_prune():
    sched = (ROOT / "app/collector/scheduler.py").read_text()
    assert "prune_metric_history(retain_days=METRIC_HISTORY_RETENTION_DAYS)" in sched
    assert "metric_timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)" in (ROOT / "app/collector/metrics_writer.py").read_text()
