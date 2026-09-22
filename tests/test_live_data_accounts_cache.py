# tests/test_live_data_accounts_cache.py
"""
Audit b16, app/api/live_data.py:

1. F-CACHE: the shared module-level _accounts_cache used to be
   populated from whichever caller's OWN scope-filtered account list
   happened to hit it first (a cache miss), then re-filtered again by
   each subsequent caller's scope on a cache hit. That meant a
   narrowly-scoped user populating the cache first would silently
   shrink what every OTHER user -- including an admin -- saw for up to
   CACHE_TTL seconds, since the cached payload itself never held more
   than the first caller's own accounts. Fixed: the cache now always
   holds every active account's processed data, unfiltered, and the
   scope filter is applied fresh on every request instead.

2. F-LEAK-A/B: 6 helper functions opened a pooled DB connection with
   no try/finally (or, for 2 of them, a finally that lived only inside
   an outer try/except, closing on the happy path but leaking straight
   past .close() whenever the query itself raised). Fixed with
   try/finally around every connection. These tests use a connection
   whose cursor() raises to prove close() is still called.
   (A 7th flagged function, _get_active_alert_counts_by_account, had
   the same leak at audit time but was independently rewritten and
   fixed by a parallel audit chat before this patch was built --
   nothing left to do there, so it isn't covered here.)

See tests/conftest.py's module docstring for why this repo's tests
load target modules in isolation (fake app.* stubs) rather than
mocking through a DI framework that doesn't exist here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub


def _install_common_stubs():
    """Stub every top-level import live_data.py needs so it can be
    loaded standalone, without pulling in boto3/real DB/real auth."""
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)

    collector_names = [
        "collect_ec2_instances", "collect_ebs_volumes", "collect_rds_instances",
        "collect_lambda_functions", "collect_s3_buckets", "collect_elb",
        "collect_ecs_clusters", "collect_nlb", "collect_acm_certificates",
        "collect_backup_resources", "collect_dms_instances", "collect_direct_connections",
        "collect_state_machines", "collect_apigateway", "collect_dynamodb_tables",
        "collect_sqs_queues", "collect_sns_topics", "collect_cloudfront_distributions",
        "collect_elasticache_clusters", "collect_opensearch_domains", "collect_eks_clusters",
        "collect_efs_filesystems", "collect_documentdb_clusters", "collect_neptune_clusters",
        "collect_msk_clusters", "collect_kinesis_streams", "collect_firehose_streams",
        "collect_autoscaling_groups", "collect_nat_gateways", "collect_transit_gateways",
        "collect_route53_zones", "collect_waf_web_acls", "collect_redshift_clusters",
        "collect_memorydb_clusters", "collect_dax_clusters", "collect_eventbridge_rules",
        "collect_kms_keys", "collect_cloudwatch_log_groups", "collect_vpn_connections",
        "collect_cognito_user_pools", "collect_global_accelerator_accelerators",
        "get_ec2_metric_series", "get_s3_metric_series", "_get_ebs_metric_series",
        "_metric_history_query_range", "_get_lambda_metric_series", "_get_rds_metric_series",
        "_get_elb_metric_series", "_get_ecs_metric_series",
    ]
    attrs = {name: (lambda *a, **k: None) for name in collector_names}
    attrs["get_account_summary"] = lambda region, role_arn=None, external_id=None, account=None: {}
    install_stub("app.aws.collector_direct", **attrs)

    install_stub("app.alert_visibility", hidden_metrics_sql=lambda: "'multivariate_anomaly'")
    install_stub("app.threshold_defaults", normalize_service_key=lambda rt, rid: rt)
    install_stub(
        "app.alert_rules",
        firing_where=lambda: "a.status = 'active'",
        base_where=lambda: "a.resolved_at IS NULL",
        fetch_open_alert_rows=lambda cursor, x: [],
        rollup=lambda rows: {"accounts": {}},
    )


class _RaisingCursor:
    def execute(self, *a, **k):
        raise RuntimeError("simulated query failure")
    def fetchall(self):
        return []
    def fetchone(self):
        return None
    def close(self):
        pass


class _RaisingConn:
    """A connection whose cursor() itself works, but whose queries
    always raise -- used to prove close() still runs on the error path."""
    def __init__(self):
        self.closed = False
    def cursor(self, dictionary=False):
        return _RaisingCursor()
    def close(self):
        self.closed = True


def _load_live_data():
    _install_common_stubs()
    install_stub("app.db", get_connection=lambda: None)  # overridden per-test below
    return load_module("app/api/live_data.py")


# ─────────────────────────────────────────────────────────────────
# F-CACHE: shared cache must not be scoped to whichever caller
# populated it first.
# ─────────────────────────────────────────────────────────────────

def test_narrowly_scoped_caller_populating_cache_does_not_shrink_it_for_admin():
    mod = _load_live_data()

    all_accounts = [
        {"id": 1, "account_name": "a1", "account_id": "111", "default_region": "us-east-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None},
        {"id": 2, "account_name": "a2", "account_id": "222", "default_region": "us-east-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None},
        {"id": 3, "account_name": "a3", "account_id": "333", "default_region": "us-east-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None},
    ]
    mod._get_db_accounts = lambda: all_accounts
    mod._get_active_alert_counts_by_account = lambda: {}
    mod._get_ec2_instance_health_by_account = lambda: {}
    mod.get_account_summary = lambda *a, **k: {}

    # First caller: a narrowly-scoped viewer who can only see account 1.
    viewer = {"id": 10, "username": "viewer", "role": "viewer"}
    mod.get_accessible_account_ids = lambda user: {1} if user is viewer else None
    viewer_result = mod.live_accounts(current_user=viewer)
    assert {a["id"] for a in viewer_result} == {1}

    # Second caller, same cache window (no time has passed): an admin
    # with unrestricted access must still see ALL THREE accounts, not
    # just the one the first (narrower) caller happened to see.
    admin = {"id": 20, "username": "admin", "role": "admin"}
    admin_result = mod.live_accounts(current_user=admin)
    assert {a["id"] for a in admin_result} == {1, 2, 3}


def test_two_differently_scoped_callers_each_see_their_own_full_scope():
    mod = _load_live_data()

    all_accounts = [
        {"id": 1, "account_name": "a1", "account_id": "111", "default_region": "us-east-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None},
        {"id": 2, "account_name": "a2", "account_id": "222", "default_region": "us-east-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None},
        {"id": 3, "account_name": "a3", "account_id": "333", "default_region": "us-east-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None},
    ]
    mod._get_db_accounts = lambda: all_accounts
    mod._get_active_alert_counts_by_account = lambda: {}
    mod._get_ec2_instance_health_by_account = lambda: {}
    mod.get_account_summary = lambda *a, **k: {}

    user_a = {"id": 1, "username": "a", "role": "viewer"}
    user_b = {"id": 2, "username": "b", "role": "viewer"}
    scopes = {1: {1, 2}, 2: {3}}
    mod.get_accessible_account_ids = lambda user: scopes[user["id"]]

    # user_a (scope {1,2}) populates the cache first.
    result_a = mod.live_accounts(current_user=user_a)
    assert {a["id"] for a in result_a} == {1, 2}

    # user_b (scope {3}, no overlap with user_a) must still see account
    # 3 on the very next request within the same cache window -- not
    # an empty list, which the old scope-baked-into-the-cache bug
    # would have produced (cached data was already narrowed to {1,2}).
    result_b = mod.live_accounts(current_user=user_b)
    assert {a["id"] for a in result_b} == {3}


def test_cache_is_reused_within_ttl_db_hit_only_once():
    """Confirms this is still actually a cache (the fix doesn't
    accidentally requery the DB on every request)."""
    mod = _load_live_data()
    calls = {"n": 0}

    def _accounts():
        calls["n"] += 1
        return [{"id": 1, "account_name": "a1", "account_id": "111", "default_region": "us-east-1",
                  "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None}]

    mod._get_db_accounts = _accounts
    mod._get_active_alert_counts_by_account = lambda: {}
    mod._get_ec2_instance_health_by_account = lambda: {}
    mod.get_account_summary = lambda *a, **k: {}
    mod.get_accessible_account_ids = lambda user: None

    admin = {"id": 1, "username": "admin", "role": "admin"}
    mod.live_accounts(current_user=admin)
    mod.live_accounts(current_user=admin)
    mod.live_accounts(current_user=admin)
    assert calls["n"] == 1


# ─────────────────────────────────────────────────────────────────
# F-LEAK-A/B: connection must be closed even when the query raises.
# ─────────────────────────────────────────────────────────────────

def test_get_db_accounts_closes_connection_on_query_error():
    mod = _load_live_data()
    conn = _RaisingConn()
    mod.get_connection = lambda: conn
    try:
        mod._get_db_accounts()
    except RuntimeError:
        pass
    assert conn.closed is True


def test_get_db_account_closes_connection_on_query_error():
    mod = _load_live_data()
    conn = _RaisingConn()
    mod.get_connection = lambda: conn
    try:
        mod._get_db_account(1)
    except RuntimeError:
        pass
    assert conn.closed is True


def test_check_resource_scope_closes_connection_on_query_error():
    mod = _load_live_data()
    mod.get_accessible_account_ids = lambda user: {1, 2}
    conn = _RaisingConn()
    mod.get_connection = lambda: conn
    try:
        mod._check_resource_scope({"id": 1}, "i-abc123")
    except RuntimeError:
        pass
    assert conn.closed is True


def test_resolve_resource_account_closes_connection_on_query_error():
    mod = _load_live_data()
    conn = _RaisingConn()
    mod.get_connection = lambda: conn
    try:
        mod._resolve_resource_account("i-abc123")
    except RuntimeError:
        pass
    assert conn.closed is True


def test_get_running_ec2_ids_by_account_closes_connection_on_query_error():
    mod = _load_live_data()
    conn = _RaisingConn()
    mod.get_connection = lambda: conn
    result = mod._get_running_ec2_ids_by_account()
    assert result == {}
    assert conn.closed is True


def test_get_ec2_instance_health_by_account_closes_connection_on_query_error():
    mod = _load_live_data()
    conn = _RaisingConn()
    mod.get_connection = lambda: conn
    result = mod._get_ec2_instance_health_by_account()
    assert result == {}
    assert conn.closed is True
