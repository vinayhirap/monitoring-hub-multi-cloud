# tests/test_refresh_cost_caching.py
"""
Refresh-cost audit 2026-10-01: the chart auto-refresh / manual refresh must not
turn into AWS API calls. ECS Container Insights GetMetricData (billed) and the
ELB DescribeLoadBalancers name lookup used to run on EVERY metrics request.
They are now cached; these tests pin that so a refactor cannot silently make
every refresh tick bill CloudWatch again.
"""
import sys
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.test_collector_direct import _stub_and_load, _RoutingConn


class _FakeAws:
    def __init__(self):
        self.describe_lb = 0
        self.clients = []

    def get_session(self, region=None, role_arn=None, external_id=None, account=None):
        outer = self

        class _Sess:
            def client(self, name, config=None):
                outer.clients.append(name)

                class _C:
                    def describe_load_balancers(self, Names=None):
                        outer.describe_lb += 1
                        return {"LoadBalancers": [{"LoadBalancerArn": "arn:aws:elasticloadbalancing:r:1:loadbalancer/app/x/abc"}]}
                return _C()
        return _Sess()


def _mod():
    mod = _stub_and_load(_RoutingConn())
    aws = _FakeAws()
    mod.get_session = aws.get_session
    mod._metric_history_query_range = lambda *a, **k: []
    return mod, aws


def test_ecs_billed_getmetricdata_is_cached_across_refreshes():
    mod, aws = _mod()
    calls = []
    mod._gmd_series = lambda cw, queries, hours=6: calls.append([q["Id"] for q in queries]) or {}
    for _ in range(5):                       # 5 refresh ticks
        mod._get_ecs_metric_series("c1", "svc1", "ap-south-1", 6, account={"id": 7})
    # one CPU/MEM fallback + one Container Insights call in total, NOT 5 + 5
    assert len(calls) == 2, calls


def test_ecs_cache_is_per_range_and_per_service():
    mod, aws = _mod()
    calls = []
    mod._gmd_series = lambda cw, queries, hours=6: calls.append(hours) or {}
    mod._get_ecs_metric_series("c1", "svc1", "r", 6, account={"id": 7})
    mod._get_ecs_metric_series("c1", "svc1", "r", 24, account={"id": 7})   # other range -> own data
    mod._get_ecs_metric_series("c1", "svc2", "r", 6, account={"id": 7})    # other service -> own data
    assert len(calls) == 6, calls


def test_elb_dimension_lookup_is_cached():
    mod, aws = _mod()
    mod._gmd_series = lambda *a, **k: {}
    for _ in range(4):
        mod._get_elb_metric_series("my-alb", "ap-south-1", 6, account={"id": 7})
    assert aws.describe_lb == 1
