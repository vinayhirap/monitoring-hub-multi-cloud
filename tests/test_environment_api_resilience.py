# tests/test_environment_api_resilience.py
"""A re-resolve that fails AFTER a change was committed must not turn the saved change into an error."""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401
import app.auth  # noqa: F401

from tests.conftest import load_module, install_stub


def _api():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.collector.env_sync", NON_ALERTABLE_TYPES=("eni",), STALE_RESOURCE_HOURS=48,
                 sync_resource_environments=lambda **kw: {"applied": 3, "by_env": {"prod": 3}})
    install_stub("app.collector.tag_plan", DEFAULT_SOURCES=("manual", "name", "related"), build_tag_plan=lambda *a, **k: {})
    return load_module("app/api/environments.py")


def test_resync_returns_the_report_when_it_works():
    rep, warn = _api()._resync(resource_pks=[1])
    assert rep["applied"] == 3 and warn is None


def test_resync_failure_is_a_warning_not_an_exception():
    mod = _api()

    def boom(**kw):
        raise RuntimeError("db went away")
    mod.sync_resource_environments = boom
    rep, warn = mod._resync(account_id=1)
    assert rep["applied"] == 0 and "could not be re-evaluated" in warn


def test_source_filter_message_lists_related():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/api/environments.py", encoding="utf-8").read()
    assert "manual, tag, name, related, account or unknown" in src
