# tests/test_host_agent_services_real_db.py
"""
REAL-DATABASE test of the host_agent_services SQL (migration 123 + the ingest upsert and prune + the CASCADE) on MySQL 8 /
MariaDB. Same setup as tests/test_host_agents_real_db.py (apply migrations up to and including 123), then:

    MH_TEST_DB=1 pytest tests/test_host_agent_services_real_db.py

Skipped unless MH_TEST_DB is set. Touches only host_agents / host_agent_services rows with aws_account_id 990003, which it removes.
"""
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

if os.getenv("MH_TEST_DB"):
    import mysql.connector

ROOT = Path(__file__).resolve().parent.parent
ACCT = 990003
T0 = datetime(2026, 10, 24, 10, 0, 0)


def _upsert_sql():
    src = (ROOT / "app/hostagents/ingest.py").read_text()
    m = re.search(r"SQL_UPSERT_SERVICES = \((.*?)\n\)\n", src, re.S)
    return eval("(" + m.group(1) + ")")


@pytest.fixture
def db():
    conn = mysql.connector.connect(
        host=os.getenv("MH_TEST_DB_HOST", "127.0.0.1"), port=int(os.getenv("MH_TEST_DB_PORT", 3306)),
        user=os.getenv("MH_TEST_DB_USER", "mh"), password=os.getenv("MH_TEST_DB_PASSWORD", "mh"),
        database=os.getenv("MH_TEST_DB_NAME", "mh_test"), use_pure=True, autocommit=False)
    cur = conn.cursor(dictionary=True)
    cur.execute("DELETE FROM host_agents WHERE aws_account_id = %s", (ACCT,))
    cur.execute("INSERT INTO host_agents (aws_account_id, hostname) VALUES (%s, 'svc-test')", (ACCT,))
    hid = cur.lastrowid
    conn.commit()
    yield conn, cur, hid
    cur.execute("DELETE FROM host_agents WHERE aws_account_id = %s", (ACCT,))
    conn.commit()
    conn.close()


def _row(cur, hid, name):
    cur.execute("SELECT state, state_since, observed_at FROM host_agent_services WHERE host_agent_id = %s AND service_name = %s",
                (hid, name))
    return cur.fetchone()


def _up(conn, cur, hid, name, state, t):
    cur.executemany(_upsert_sql(), [(hid, name, state, t, t)])
    conn.commit()


def test_insert_then_change_moves_state_since_only_on_a_change(db):
    conn, cur, hid = db
    _up(conn, cur, hid, "nginx", "running", T0)
    assert _row(cur, hid, "nginx") == {"state": "running", "state_since": T0, "observed_at": T0}
    _up(conn, cur, hid, "nginx", "running", T0 + timedelta(minutes=1))
    r = _row(cur, hid, "nginx")
    assert r["state_since"] == T0 and r["observed_at"] == T0 + timedelta(minutes=1)
    _up(conn, cur, hid, "nginx", "failed", T0 + timedelta(minutes=2))
    r = _row(cur, hid, "nginx")
    assert r["state"] == "failed" and r["state_since"] == T0 + timedelta(minutes=2)


def test_a_late_or_replayed_payload_never_overwrites_a_newer_reading(db):
    conn, cur, hid = db
    _up(conn, cur, hid, "nginx", "failed", T0 + timedelta(minutes=5))
    _up(conn, cur, hid, "nginx", "running", T0 + timedelta(minutes=1))
    r = _row(cur, hid, "nginx")
    assert r["state"] == "failed" and r["observed_at"] == T0 + timedelta(minutes=5) and r["state_since"] == T0 + timedelta(minutes=5)


def test_the_four_contract_states_fit_the_enum_and_junk_is_refused(db):
    conn, cur, hid = db
    for i, st in enumerate(("running", "stopped", "failed", "unknown")):
        _up(conn, cur, hid, f"s{i}", st, T0)
    with pytest.raises(mysql.connector.Error):
        conn.rollback()
        cur.execute("SET SESSION sql_mode = 'STRICT_ALL_TABLES'")
        _up(conn, cur, hid, "bad", "paused", T0)


def test_prune_keeps_only_the_named_services_and_the_cascade_follows_the_host(db):
    conn, cur, hid = db
    for n in ("a", "b", "c"):
        _up(conn, cur, hid, n, "running", T0)
    cur.execute("DELETE FROM host_agent_services WHERE host_agent_id = %s AND service_name NOT IN (%s, %s)", (hid, "a", "c"))
    conn.commit()
    cur.execute("SELECT service_name FROM host_agent_services WHERE host_agent_id = %s ORDER BY 1", (hid,))
    assert [r["service_name"] for r in cur.fetchall()] == ["a", "c"]
    cur.execute("DELETE FROM host_agents WHERE id = %s", (hid,))
    conn.commit()
    cur.execute("SELECT COUNT(*) AS n FROM host_agent_services WHERE host_agent_id = %s", (hid,))
    assert cur.fetchone()["n"] == 0
