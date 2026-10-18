# tests/test_tag_sync.py
"""Workstream B: tag_state with a synced result, flag-off behaviour, the sync run with a fake DB and a fake sweep,
and env_sync using the stored result."""
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401,E402
from app import environment_classifier as ec  # noqa: E402
from tests.conftest import install_stub, load_module  # noqa: E402

ACCT = "123456789012"
REG = "ap-south-1"


# ---------------------------------------------------------------- classifier
def test_unsynced_row_of_a_synced_type_is_not_checked():
    assert ec.tag_state("sqs", {"queue_url": "x"}) == "not_checked"
    assert ec.tag_state("sqs", {"queue_url": "x"}, None) == "not_checked"


@pytest.mark.parametrize("synced,expected", [
    ({"Environment": "prod"}, "ok"),
    ({"Env": "Production"}, "nonstandard"),
    ({"Environment": "banana"}, "invalid"),
    ({"Owner": "x"}, "missing"),
    ({}, "missing"),                       # swept and absent
])
def test_synced_result_gives_a_real_status(synced, expected):
    assert ec.tag_state("kms", {}, synced) == expected


def test_synced_result_is_ignored_for_types_the_sync_does_not_cover():
    assert ec.tag_state("dms", {}, {}) == "not_checked"
    assert ec.tag_state("ec2", {"Environment": "prod"}, {}) == "ok"          # ec2 keeps using its own tags
    # an attached volume is never "known" through the sync
    assert ec.tag_state("ebs", {"parent_ec2": "i-1"}, {"Environment": "prod"}) == "not_checked"


def test_effective_tags_prefers_the_synced_result_only_for_synced_types():
    assert ec.effective_tags("sqs", {"queue_url": "u"}, {"Owner": "a"}) == {"Owner": "a"}
    assert ec.effective_tags("sqs", {"queue_url": "u"}, None) == {"queue_url": "u"}
    assert ec.effective_tags("dms", {"x": "1"}, {"Owner": "a"}) == {"x": "1"}


def test_parse_synced_distinguishes_empty_from_unknown():
    assert ec.parse_synced(None) is None
    assert ec.parse_synced("not json") is None
    assert ec.parse_synced("[1]") is None
    assert ec.parse_synced("{}") == {}
    assert ec.parse_synced('{"a": "b"}') == {"a": "b"}
    assert ec.parse_synced({"a": "b"}) == {"a": "b"}


def test_flags_default_off(monkeypatch):
    monkeypatch.delenv("TAG_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("TAG_SYNC_FEED_ENVIRONMENT", raising=False)
    assert not ec.tag_sync_enabled() and not ec.tag_sync_feeds_environment()
    monkeypatch.setenv("TAG_SYNC_FEED_ENVIRONMENT", "true")
    assert not ec.tag_sync_feeds_environment()                    # needs the main flag too
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    assert ec.tag_sync_feeds_environment()


# ---------------------------------------------------------------- env_sync
def _env_sync():
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/collector/env_sync.py")


def _row(rtype="sqs", synced=None, tags=None, name="orders", rid="orders"):
    return {"id": 1, "aws_account_id": 1, "resource_id": rid, "resource_type": rtype, "name": name,
            "tags": tags or {}, "env_override": None, "env_resolved": None, "env_source": None,
            "env_reason": None, "tag_state": None, "default_env": None, "synced_tags": synced}


def test_env_sync_resolution_ignores_synced_tags_unless_the_feed_flag_is_on(monkeypatch):
    mod = _env_sync()
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    monkeypatch.delenv("TAG_SYNC_FEED_ENVIRONMENT", raising=False)
    r = _row(synced='{"Environment": "dev"}')
    assert mod.resolve_row(r).source == "unknown"                  # nothing about alerting changed
    monkeypatch.setenv("TAG_SYNC_FEED_ENVIRONMENT", "true")
    res = mod.resolve_row(r)
    assert (res.env, res.source) == ("dev", "tag")


def test_env_sync_synced_value_is_used_only_for_synced_types():
    mod = _env_sync()
    assert mod._row_synced(_row("sqs", synced='{"a": "b"}')) == {"a": "b"}
    assert mod._row_synced(_row("sqs", synced="{}")) == {}
    assert mod._row_synced(_row("sqs", synced=None)) is None
    assert mod._row_synced(_row("dms", synced='{"a": "b"}')) is None


def test_synced_select_is_null_when_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("TAG_SYNC_ENABLED", raising=False)
    ts = load_module("app/collector/tag_sync.py")

    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("no query may run with the flag off")
    assert ts.synced_select(Boom()) == "NULL AS synced_tags"


def test_synced_select_uses_the_columns_only_when_migration_095_is_in_place(monkeypatch):
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    ts = load_module("app/collector/tag_sync.py")

    class Cur:
        def __init__(self, n):
            self.n = n

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return {"n": self.n}
    assert ts.synced_select(Cur(0)) == "NULL AS synced_tags"       # deploy restarted before the migration
    sql = ts.synced_select(Cur(2))
    assert "tags_synced_at >= DATE_SUB(NOW(), INTERVAL 6 HOUR)" in sql and sql.endswith("AS synced_tags")


# ---------------------------------------------------------------- tag_sync run
class FakeDB:
    """One account, a few resources rows, records the UPDATEs."""
    def __init__(self, rows, columns=2):
        self.rows, self.columns, self.updates, self.connections = rows, columns, [], 0

    def get_connection(self):
        self.connections += 1
        db = self

        class Cur:
            def __init__(self, dictionary=False):
                self.last = ""
                self.rowcount = 0

            def execute(self, sql, params=None):
                self.last = " ".join(sql.split())

            def fetchone(self):
                return {"n": db.columns}

            def fetchall(self):
                return db.rows if "FROM resources r" in self.last else []

            def executemany(self, sql, chunk):
                db.updates.extend(chunk)
                self.rowcount = len(chunk)

            def close(self):
                pass

        class Conn:
            def cursor(self, dictionary=False):
                return Cur(dictionary)

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass
        return Conn()


def _db_row(pk, rtype, rid, region=REG, stored=None, age=None, tags=None):
    return {"id": pk, "resource_type": rtype, "resource_id": rid, "region": region, "tags": tags or {},
            "stored_synced": stored, "synced_age_min": age}


def _load_sync(monkeypatch, db, accounts=None):
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    install_stub("app.db", get_connection=db.get_connection)
    ts = load_module("app/collector/tag_sync.py")
    acc = accounts if accounts is not None else [{"id": 1, "account_name": "AuroGov", "account_id": ACCT,
                                                  "default_region": REG}]
    ts._get_accounts = lambda: acc
    ts._get_session = lambda a: object()
    return ts


def _sweeper(items=None, ok=True, error=None, pages=1, calls=None):
    def f(account, spec):
        region = spec["region"]
        if calls is not None:
            calls.append((region, spec["scope"]) if spec["scope"] != "full" else region)
        return {"region": region, "scope": spec["scope"], "ok": ok,
                "items": [{"arn": a, "tags": t} for a, t in (items or [])], "pages": pages, "error": error}
    return f


def test_sync_stores_tagged_and_absent_results(monkeypatch):
    db = FakeDB([_db_row(1, "sqs", "orders"), _db_row(2, "sqs", "legacy"), _db_row(3, "kms", "k-1")])
    ts = _load_sync(monkeypatch, db)
    rep = ts.run_tag_sync(sweeper=_sweeper([(f"arn:aws:sqs:{REG}:{ACCT}:orders", {"Environment": "prod"}),
                                              (f"arn:aws:kms:{REG}:{ACCT}:key/k-1", {})]))
    assert dict((pk, v) for v, pk in db.updates) == {1: '{"Environment":"prod"}', 2: "{}", 3: "{}"}
    assert rep["written"] == 3 and rep["calls"] == 1


def test_sync_failure_leaves_stored_values_untouched(monkeypatch):
    db = FakeDB([_db_row(1, "sqs", "orders", stored='{"Environment": "prod"}', age=10)])
    ts = _load_sync(monkeypatch, db)
    rep = ts.run_tag_sync(sweeper=_sweeper(ok=False, error=RuntimeError("Throttling")))
    assert db.updates == [] and rep["written"] == 0


def test_unchanged_recent_result_is_not_rewritten_but_an_old_one_is_refreshed(monkeypatch):
    arn = f"arn:aws:sqs:{REG}:{ACCT}:orders"
    db = FakeDB([_db_row(1, "sqs", "orders", stored='{"Owner": "a"}', age=5)])
    ts = _load_sync(monkeypatch, db)
    ts.run_tag_sync(sweeper=_sweeper([(arn, {"Owner": "a"})]))
    assert db.updates == []
    db2 = FakeDB([_db_row(1, "sqs", "orders", stored='{"Owner": "a"}', age=120)])
    ts2 = _load_sync(monkeypatch, db2)
    ts2.run_tag_sync(sweeper=_sweeper([(arn, {"Owner": "a"})]))
    assert [pk for _, pk in db2.updates] == [1]


def test_dry_run_writes_nothing(monkeypatch):
    db = FakeDB([_db_row(1, "sqs", "orders")])
    ts = _load_sync(monkeypatch, db)
    rep = ts.run_tag_sync(dry_run=True, sweeper=_sweeper([(f"arn:aws:sqs:{REG}:{ACCT}:orders", {"a": "b"})]))
    assert db.updates == [] and rep["accounts"]["AuroGov"]["types"]["sqs"]["matched"] == 1


def test_sweep_plan_is_one_full_sweep_plus_cheap_filtered_ones_only_when_needed(monkeypatch):
    calls = []
    db = FakeDB([_db_row(1, "sqs", "orders")])
    ts = _load_sync(monkeypatch, db)
    ts.run_tag_sync(sweeper=_sweeper(calls=calls))
    assert calls == [REG]                                              # nothing global: one call series
    calls.clear()
    db = FakeDB([_db_row(1, "sqs", "orders"), _db_row(2, "s3", "bucket", region="ap-south-1"),
                 _db_row(3, "s3", "other", region="ap-south-2"), _db_row(4, "s3", "far", region="ap-south-2"),
                 _db_row(5, "cloudfront", "E1", region="global")])
    ts = _load_sync(monkeypatch, db)
    ts.run_tag_sync(sweeper=_sweeper(calls=calls))
    assert calls == [REG, ("us-east-1", "global"), ("ap-south-2", "s3")]  # one filtered sweep per extra bucket region


def test_sweep_plan_filters_and_default_region_in_us_east_1(monkeypatch):
    ts = load_module("app/collector/tag_sync.py")
    rows = [{"resource_type": "s3", "region": "us-east-1"}, {"resource_type": "cloudfront", "region": "global"}]
    assert ts.sweep_plan({"default_region": "us-east-1"}, rows) == [
        {"region": "us-east-1", "scope": "full", "filters": None}]
    plan = ts.sweep_plan({"default_region": REG}, rows)
    assert plan[1] == {"region": "us-east-1", "scope": "global", "filters": ["cloudfront", "s3"]}
    many = [{"resource_type": "s3", "region": f"r-{i}"} for i in range(30)]
    assert len(ts.sweep_plan({"default_region": REG}, many)) <= 1 + 1 + ts.MAX_EXTRA_S3_REGIONS


def test_hook_sweeps_at_most_once_per_interval(monkeypatch):
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    monkeypatch.setenv("TAG_SYNC_INTERVAL_MINUTES", "60")
    install_stub("app.db", get_connection=lambda: None)
    ts = load_module("app/collector/tag_sync.py")
    runs = []
    ts.run_tag_sync = lambda *a, **k: runs.append(1) or {"written": 0, "calls": 0}
    ts.sync_after_discovery()
    ts.sync_after_discovery()
    assert len(runs) == 1
    ts._last_run -= 3601
    ts.sync_after_discovery()
    assert len(runs) == 2


def test_interval_setting_is_sane(monkeypatch):
    ts = load_module("app/collector/tag_sync.py")
    monkeypatch.delenv("TAG_SYNC_INTERVAL_MINUTES", raising=False)
    assert ts.interval_minutes() == 60
    for bad in ("abc", "0", "-3", "4"):
        monkeypatch.setenv("TAG_SYNC_INTERVAL_MINUTES", bad)
        assert ts.interval_minutes() == 60
    monkeypatch.setenv("TAG_SYNC_INTERVAL_MINUTES", "15")
    assert ts.interval_minutes() == 15


def test_absence_types_setting_can_only_narrow(monkeypatch):
    ts = load_module("app/collector/tag_sync.py")
    monkeypatch.delenv("TAG_SYNC_ABSENCE_TYPES", raising=False)
    assert ts.absence_types() == ts.arn_match.ABSENCE_VERIFIED_TYPES
    monkeypatch.setenv("TAG_SYNC_ABSENCE_TYPES", "none")
    assert ts.absence_types() == frozenset()
    monkeypatch.setenv("TAG_SYNC_ABSENCE_TYPES", "logs, s3, bogus")
    assert ts.absence_types() == frozenset({"logs"})


def test_account_without_a_number_is_skipped(monkeypatch):
    db = FakeDB([_db_row(1, "sqs", "orders")])
    ts = _load_sync(monkeypatch, db, accounts=[{"id": 1, "account_name": "X", "account_id": None, "default_region": REG}])
    rep = ts.run_tag_sync(sweeper=_sweeper())
    assert "skipped" in rep["accounts"]["X"] and db.updates == []


def test_missing_migration_skips_without_error(monkeypatch):
    db = FakeDB([_db_row(1, "sqs", "orders")], columns=0)
    ts = _load_sync(monkeypatch, db)
    rep = ts.run_tag_sync(sweeper=_sweeper([]))
    assert rep["skipped"] == "migration 095 not applied" and db.updates == []


def test_flag_off_hook_makes_no_connection_and_no_aws_call(monkeypatch):
    monkeypatch.delenv("TAG_SYNC_ENABLED", raising=False)
    db = FakeDB([])
    install_stub("app.db", get_connection=db.get_connection)
    ts = load_module("app/collector/tag_sync.py")
    ts._get_accounts = lambda: (_ for _ in ()).throw(AssertionError("must not list accounts"))
    ts.sweep_region = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call AWS"))
    ts.sync_after_discovery()
    assert db.connections == 0


def test_hook_never_raises(monkeypatch):
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    ts = load_module("app/collector/tag_sync.py")
    ts._get_accounts = lambda: [{"id": 1, "account_name": "A", "account_id": ACCT, "default_region": REG}]
    ts.sync_after_discovery()      # must not raise


# ---------------------------------------------------------------- the AWS sweep itself
class FakePaginator:
    def __init__(self, pages, fail_after=None, seen=None, reject_filter=False):
        self.pages, self.fail_after = pages, fail_after
        self.seen = seen if seen is not None else []
        self.reject_filter = reject_filter

    def paginate(self, **kw):
        assert kw.get("ResourcesPerPage") == 100
        self.seen.append(dict(kw))
        if self.reject_filter and "ResourceTypeFilters" in kw:
            class Bad(Exception):
                response = {"Error": {"Code": "InvalidParameterException"}}
            raise Bad("bad filter")
        for i, p in enumerate(self.pages):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("Throttling: Rate exceeded")
            yield p


class FakeSession:
    def __init__(self, pages, fail_after=None, reject_filter=False):
        self.pages, self.fail_after, self.clients = pages, fail_after, []
        self.seen, self.reject_filter = [], reject_filter

    def client(self, name, region_name=None, config=None):
        self.clients.append((name, region_name))
        sess = self

        class C:
            def get_paginator(self, op):
                assert op == "get_resources"
                return FakePaginator(sess.pages, sess.fail_after, sess.seen, sess.reject_filter)
        return C()


def _page(*items):
    return {"ResourceTagMappingList": [{"ResourceARN": a, "Tags": [{"Key": k, "Value": v} for k, v in t.items()]}
                                       for a, t in items]}


def test_sweep_reads_every_page_in_one_paginated_call_series():
    ts = load_module("app/collector/tag_sync.py")
    sess = FakeSession([_page(("arn:aws:sqs:r:a:q1", {"k": "v"})), _page(("arn:aws:sqs:r:a:q2", {}))])
    out = ts.sweep_region(sess, REG)
    assert out["ok"] and out["pages"] == 2 and [i["arn"] for i in out["items"]] == ["arn:aws:sqs:r:a:q1", "arn:aws:sqs:r:a:q2"]
    assert sess.clients == [("resourcegroupstaggingapi", REG)]


def test_a_failure_on_a_later_page_discards_the_whole_sweep():
    ts = load_module("app/collector/tag_sync.py")
    out = ts.sweep_region(FakeSession([_page(("arn:aws:sqs:r:a:q1", {})), _page(("arn:aws:sqs:r:a:q2", {}))], fail_after=1), REG)
    assert out["ok"] is False and out["items"] == []


def test_access_denied_is_logged_once_per_day(monkeypatch, caplog):
    ts = load_module("app/collector/tag_sync.py")

    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}
    acct = {"id": 1, "account_name": "AuroGov"}
    with caplog.at_level("WARNING"):
        for _ in range(3):
            ts._log_sweep_failure(acct, REG, Denied("no"))
    assert sum("AccessDeniedException" in r.message for r in caplog.records) == 1


def test_filtered_sweep_sends_the_filter_and_marks_its_scope():
    ts = load_module("app/collector/tag_sync.py")
    sess = FakeSession([_page(("arn:aws:cloudfront::123456789012:distribution/E1", {"k": "v"}))])
    out = ts.sweep_region(sess, "us-east-1", ["cloudfront", "s3"], "global")
    assert out["ok"] and out["scope"] == "global"
    assert sess.seen == [{"ResourcesPerPage": 100, "ResourceTypeFilters": ["cloudfront", "s3"]}]


def test_a_rejected_filter_falls_back_to_an_unfiltered_sweep():
    ts = load_module("app/collector/tag_sync.py")
    sess = FakeSession([_page(("arn:aws:s3:::b", {"k": "v"}))], reject_filter=True)
    out = ts.sweep_region(sess, "us-east-1", ["s3"], "global")
    assert out["ok"] and len(out["items"]) == 1 and out["pages"] == 1
    assert "ResourceTypeFilters" not in sess.seen[-1]


def test_a_non_filter_error_is_not_retried():
    ts = load_module("app/collector/tag_sync.py")
    out = ts.sweep_region(FakeSession([_page()], fail_after=0), REG, ["s3"], "global")
    assert out["ok"] is False and out["items"] == []


# ---------------------------------------------------------------- direct lookups
class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class DirectSession:
    def __init__(self, s3=None, waf=None):
        self.s3, self.waf, self.calls, self.regions = s3 or {}, waf or {}, [], []

    def client(self, name, region_name=None, config=None):
        sess = self
        sess.regions.append((name, region_name))

        class C:
            def get_bucket_tagging(self, Bucket):
                sess.calls.append(("s3", Bucket))
                v = sess.s3[Bucket]
                if isinstance(v, Exception):
                    raise v
                return {"TagSet": [{"Key": k, "Value": x} for k, x in v.items()]}

            def list_tags_for_resource(self, ResourceARN):
                sess.calls.append(("waf", ResourceARN))
                v = sess.waf[ResourceARN]
                if isinstance(v, Exception):
                    raise v
                return {"TagInfoForResource": {"ResourceARN": ResourceARN,
                                               "TagList": [{"Key": k, "Value": x} for k, x in v.items()]}}
        return C()


WAF_ARN = f"arn:aws:wafv2:{REG}:{ACCT}:regional/webacl/acl1/id-1"


def _drow(pk, rtype, rid, region=REG, tags=None):
    return {"id": pk, "resource_type": rtype, "resource_id": rid, "region": region, "tags": tags or {}}


def test_direct_lookup_reads_s3_and_waf_and_classifies_every_outcome():
    ts = load_module("app/collector/tag_sync.py")
    sess = DirectSession(
        s3={"tagged": {"Owner": "a"}, "plain": ClientError("NoSuchTagSet"), "denied": ClientError("AccessDenied"),
            "moved": ClientError("PermanentRedirect")},
        waf={WAF_ARN: {}})
    rows = [_drow(1, "s3", "tagged", "ap-south-2"), _drow(2, "s3", "plain", "ap-south-2"),
            _drow(3, "s3", "denied", "ap-south-2"), _drow(4, "s3", "moved", "ap-south-1"),
            _drow(5, "wafv2", "acl1", REG, {"acl_id": "id-1"}), _drow(6, "wafv2", "acl2", REG),
            _drow(7, "s3", "nowhere", "global")]
    results, calls = ts.direct_lookup(sess, {"id": 1, "account_name": "A"}, ACCT, rows)
    assert results == {1: {"Owner": "a"}, 2: {}, 3: None, 4: None, 5: {}, 6: None, 7: None}
    assert calls == 5                                         # rows 6 and 7 could not even be asked
    assert ("s3", "ap-south-2") in sess.regions               # the bucket's own region, not the account's


def test_waf_error_leaves_the_row_undecided():
    ts = load_module("app/collector/tag_sync.py")
    sess = DirectSession(waf={WAF_ARN: ClientError("WAFNonexistentItemException")})
    results, _ = ts.direct_lookup(sess, {"id": 1}, ACCT, [_drow(1, "wafv2", "acl1", REG, {"acl_id": "id-1"})])
    assert results == {1: None}


def test_direct_flag_defaults_off_and_needs_the_main_flag(monkeypatch):
    ts = load_module("app/collector/tag_sync.py")
    monkeypatch.delenv("TAG_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("TAG_SYNC_DIRECT_LOOKUP", raising=False)
    assert not ts.direct_enabled()
    monkeypatch.setenv("TAG_SYNC_DIRECT_LOOKUP", "true")
    assert not ts.direct_enabled()
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    assert ts.direct_enabled()
    monkeypatch.delenv("TAG_SYNC_DIRECT_MAX", raising=False)
    assert ts.direct_max() == 100
    monkeypatch.setenv("TAG_SYNC_DIRECT_MAX", "7")
    assert ts.direct_max() == 7


def _looker(results, seen):
    def f(account, rows):
        seen.append([r["id"] for r in rows])
        return results, len(rows)
    return f


def test_run_sync_with_direct_off_never_looks(monkeypatch):
    db = FakeDB([_db_row(1, "s3", "ghost", region="ap-south-2")])
    ts = _load_sync(monkeypatch, db)
    monkeypatch.delenv("TAG_SYNC_DIRECT_LOOKUP", raising=False)
    seen = []
    ts.run_tag_sync(sweeper=_sweeper([("arn:aws:states:r:a:stateMachine:x", {})]), looker=_looker({1: {}}, seen))
    assert seen == [] and db.updates == []


def test_run_sync_with_direct_on_stores_proven_answers_for_undecided_rows_only(monkeypatch):
    db = FakeDB([_db_row(1, "s3", "ghost", region="ap-south-2"), _db_row(2, "s3", "tagged", region="ap-south-1"),
                 _db_row(3, "s3", "unreadable", region="ap-south-2"), _db_row(4, "sqs", "orders")])
    ts = _load_sync(monkeypatch, db)
    monkeypatch.setenv("TAG_SYNC_DIRECT_LOOKUP", "true")
    seen = []
    rep = ts.run_tag_sync(sweeper=_sweeper([("arn:aws:s3:::tagged", {"Owner": "a"}),
                                              (f"arn:aws:sqs:{REG}:{ACCT}:orders", {"k": "v"})]),
                          looker=_looker({1: {}, 3: None}, seen))
    assert seen == [[1, 3]]                                    # the tagged bucket and the queue were decided by the sweep
    stored = {pk: v for v, pk in db.updates}
    assert stored[1] == "{}" and stored[2] == '{"Owner":"a"}' and 3 not in stored
    assert rep["accounts"]["AuroGov"]["direct_calls"] == 2 and rep["calls"] >= 2


def test_a_failing_looker_does_not_break_the_sweep_results(monkeypatch):
    db = FakeDB([_db_row(1, "s3", "ghost", region="ap-south-2"), _db_row(4, "sqs", "orders")])
    ts = _load_sync(monkeypatch, db)
    monkeypatch.setenv("TAG_SYNC_DIRECT_LOOKUP", "true")

    def boom(account, rows):
        raise RuntimeError("s3 down")
    ts.run_tag_sync(sweeper=_sweeper([(f"arn:aws:sqs:{REG}:{ACCT}:orders", {"k": "v"})]), looker=boom)
    assert {pk for _, pk in db.updates} == {4}


def test_direct_max_caps_the_lookups_per_account(monkeypatch):
    db = FakeDB([_db_row(i, "s3", f"b{i}", region="ap-south-2") for i in range(1, 6)])
    ts = _load_sync(monkeypatch, db)
    monkeypatch.setenv("TAG_SYNC_DIRECT_LOOKUP", "true")
    monkeypatch.setenv("TAG_SYNC_DIRECT_MAX", "2")
    seen = []
    ts.run_tag_sync(sweeper=_sweeper([("arn:aws:states:r:a:stateMachine:x", {})]), looker=_looker({}, seen))
    assert seen == [[1, 2]]
