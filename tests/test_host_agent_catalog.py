# tests/test_host_agent_catalog.py
"""app/hostagents/catalog.py: every host-agent metric gets a catalog row and a (disabled) threshold row for the resource type
its samples were stored under, per-mount / per-interface series clone the base, repeated calls do no work, and a database
failure never escapes."""
import sys

import pytest

from tests.conftest import install_stub, load_module


@pytest.fixture(autouse=True)
def _isolate():
    def _is_app(n):
        return n == "app" or n.startswith("app.")
    saved = {n: m for n, m in sys.modules.items() if _is_app(n) and m is not None}
    dicts = {n: dict(m.__dict__) for n, m in saved.items()}
    yield
    for n in [n for n in list(sys.modules) if _is_app(n) and n not in saved]:
        del sys.modules[n]
    for n, m in saved.items():
        sys.modules[n] = m
        m.__dict__.clear()
        m.__dict__.update(dicts[n])


class Db:
    def __init__(self):
        self.connections = 0
        self.sql = []                 # (normalized sql, params)
        self.catalog = {}             # metric_name -> id
        self.commits = 0
        self.rollbacks = 0
        self.fail = None

    def get_connection(self):
        if self.fail:
            raise self.fail
        self.connections += 1
        return Conn(self)


class Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self, dictionary=True):
        return Cur(self.db)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        self.db.rollbacks += 1

    def close(self):
        pass


class Cur:
    def __init__(self, db):
        self.db, self._row = db, None

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        self.db.sql.append((n, params))
        self._row = None
        if n.startswith("INSERT IGNORE INTO metric_catalog"):
            name = params[3] if n.count("VALUES") else params[0]
            self.db.catalog.setdefault(name, len(self.db.catalog) + 1)
        elif n.startswith("SELECT id FROM metric_catalog"):
            i = self.db.catalog.get(params[1])
            self._row = {"id": i} if i else None

    def fetchone(self):
        return self._row

    def close(self):
        pass


def _load(db):
    install_stub("app.db", get_connection=db.get_connection)
    mm = load_module("app/hostagents/metric_map.py")
    install_stub("app.hostagents", metric_map=mm)
    sys.modules["app.hostagents.metric_map"] = mm
    td = load_module("app/threshold_defaults.py")
    install_stub("app.threshold_defaults", DEFAULT_THRESHOLDS=td.DEFAULT_THRESHOLDS, PLACEHOLDER_THRESHOLD=td.PLACEHOLDER_THRESHOLD)
    cat = load_module("app/hostagents/catalog.py")
    cat._DONE.clear()
    cat._SERIES_DONE.clear()
    return cat, mm, td


def _thresholds(db):
    return [p for n, p in db.sql if n.startswith("INSERT IGNORE INTO thresholds")]


def test_catalog_covers_every_non_cwagent_metric_the_agent_can_send():
    cat, mm, _td = _load(Db())
    bases = {spec[0] for spec in mm.METRICS.values()}
    cw = {mm.BASE_DISK_METRIC, "mem_used_percent"}            # already catalogued by the CWAgent path
    assert set(cat.CATALOG) == bases - cw


def test_every_cataloged_metric_has_a_real_default_or_the_blank_placeholder():
    cat, _mm, td = _load(Db())
    for name in cat.CATALOG:
        w, c, comp = cat.default_for(name)
        assert comp in (">", "<", ">=")
        assert (w <= c) if comp in (">", ">=") else (w >= c), name
    real = [n for n in cat.CATALOG if n in td.DEFAULT_THRESHOLDS]
    assert len(real) == len(cat.CATALOG)                      # nothing silently falls back


@pytest.mark.parametrize("rtype", ["ec2", "host"])
def test_base_rows_are_registered_for_the_resource_type_the_samples_were_stored_under(rtype):
    db = Db()
    cat, _mm, _td = _load(db)
    assert cat.ensure_host_metrics_registered(7, rtype) is True
    rows = _thresholds(db)
    assert len(rows) == len(cat.CATALOG)
    assert {r[0] for r in rows} == {7} and {r[1] for r in rows} == {rtype}
    assert all(sql.endswith(", 5, 0)") for sql, _ in db.sql if sql.startswith("INSERT IGNORE INTO thresholds"))  # disabled
    assert db.commits == 1 and db.connections == 1


def test_second_call_for_the_same_account_and_type_does_nothing_but_a_new_type_registers():
    db = Db()
    cat, _mm, _td = _load(db)
    cat.ensure_host_metrics_registered(1, "ec2")
    n = len(db.sql)
    assert cat.ensure_host_metrics_registered(1, "ec2") is True
    assert len(db.sql) == n and db.connections == 1
    cat.ensure_host_metrics_registered(1, "host")
    assert db.connections == 2
    cat.ensure_host_metrics_registered(2, "ec2")
    assert db.connections == 3


def test_labelled_series_clone_the_base_and_reuse_its_default():
    db = Db()
    cat, _mm, _td = _load(db)
    names = ["host_disk_inodes_used_percent__boot", "host_net_rx_bytes_per_s__ens5", "host_cpu_percent", "disk_used_percent__boot"]
    cat.ensure_host_metrics_registered(3, "ec2", names)
    clones = [p for n, p in db.sql if n.startswith("INSERT IGNORE INTO metric_catalog SELECT") or
              (n.startswith("INSERT IGNORE INTO metric_catalog") and "SELECT service" in n)]
    assert [c[0] for c in clones] == ["host_disk_inodes_used_percent__boot", "host_net_rx_bytes_per_s__ens5"]
    got = {p[2]: p for p in _thresholds(db)}
    inode_id = db.catalog["host_disk_inodes_used_percent__boot"]
    assert got[inode_id][3:6] == (80, 90, ">")                # the base default, not the placeholder
    # series are remembered: a second pass with the same names adds nothing
    n = len(db.sql)
    cat.ensure_host_metrics_registered(3, "ec2", names)
    assert len(db.sql) == n


def test_series_base_only_recognises_labelled_host_series():
    cat, _mm, _td = _load(Db())
    assert cat.series_base("host_net_tx_bytes_per_s__eth0") == "host_net_tx_bytes_per_s"
    assert cat.series_base("host_disk_inodes_used_percent__data") == "host_disk_inodes_used_percent"
    assert cat.series_base("host_cpu_percent") is None                 # a base, already registered
    assert cat.series_base("host_cpu_percent__x") is None              # cpu has no label, so no such series exists
    assert cat.series_base("disk_used_percent__boot") is None          # CWAgent convention, other module's job
    assert cat.series_base(None) is None


def test_a_database_outage_is_swallowed_and_retried_on_the_next_report():
    db = Db()
    cat, _mm, _td = _load(db)
    db.fail = RuntimeError("db down")
    assert cat.ensure_host_metrics_registered(1, "ec2") is False        # get_connection() raising must not escape
    db.fail = None
    assert cat.ensure_host_metrics_registered(1, "ec2") is True         # nothing was cached by the failure
    assert db.connections == 1
