# tests/test_environment_group_classify.py
"""POST /api/environments/classify with a group: one account + one resource type, only what is still in the
Needs-classification queue, scope-checked, capped."""
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401
import app.auth  # noqa: F401

from tests.conftest import load_module, install_stub

_USER = {"id": 1, "username": "u", "role": "editor"}


def _api(accessible=None):
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    install_stub("app.collector.env_sync", NON_ALERTABLE_TYPES=("eni",), STALE_RESOURCE_HOURS=48,
                 sync_resource_environments=lambda **kw: {"applied": 0, "by_env": {}})
    install_stub("app.collector.tag_plan", DEFAULT_SOURCES=("manual", "name", "related"), build_tag_plan=lambda *a, **k: {})
    return load_module("app/api/environments.py")


def _db(mod, monkeypatch, ids):
    seen = []

    class Cur:
        def execute(self, sql, params=None):
            seen.append((" ".join(sql.split()), list(params or [])))

        def fetchall(self):
            return [{"id": i} for i in ids]

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def close(self):
            pass
    monkeypatch.setattr(mod, "get_connection", lambda: Conn())
    return seen


def test_group_selects_only_the_queue_for_that_account_and_type(monkeypatch):
    mod = _api()
    seen = _db(mod, monkeypatch, [5, 7, 9])
    pks, info = mod._group_pks(_USER, {"account_id": "3", "resource_type": " certificatemanager "})
    sql, params = seen[0]
    assert pks == [5, 7, 9] and info == {"account_id": 3, "resource_type": "certificatemanager"}
    assert "(r.env_source = 'unknown' OR r.env_source IS NULL)" in sql      # never a placed / tagged / named resource
    assert "r.aws_account_id = %s AND r.resource_type = %s" in sql
    assert params[-3:] == [3, "certificatemanager", mod.MAX_GROUP + 1]


@pytest.mark.parametrize("group", [
    "x", [], {"resource_type": "ec2"}, {"account_id": True, "resource_type": "ec2"},
    {"account_id": "abc", "resource_type": "ec2"}, {"account_id": 1}, {"account_id": 1, "resource_type": ""},
    {"account_id": 1, "resource_type": "x" * 61}, {"account_id": 1, "resource_type": 5}])
def test_bad_group_is_a_400(monkeypatch, group):
    mod = _api()
    _db(mod, monkeypatch, [1])
    with pytest.raises(HTTPException) as e:
        mod._group_pks(_USER, group)
    assert e.value.status_code == 400


def test_group_outside_the_callers_accounts_is_a_403(monkeypatch):
    mod = _api(accessible={1, 2})
    seen = _db(mod, monkeypatch, [1])
    with pytest.raises(HTTPException) as e:
        mod._group_pks(_USER, {"account_id": 3, "resource_type": "ec2"})
    assert e.value.status_code == 403 and not seen          # refused before any query ran


def test_empty_group_is_a_404_and_an_oversized_one_a_400(monkeypatch):
    mod = _api()
    _db(mod, monkeypatch, [])
    with pytest.raises(HTTPException) as e:
        mod._group_pks(_USER, {"account_id": 1, "resource_type": "ec2"})
    assert e.value.status_code == 404
    _db(mod, monkeypatch, list(range(mod.MAX_GROUP + 1)))
    with pytest.raises(HTTPException) as e:
        mod._group_pks(_USER, {"account_id": 1, "resource_type": "ec2"})
    assert e.value.status_code == 400


def test_a_group_cannot_be_cleared():
    mod = _api()
    with pytest.raises(HTTPException) as e:
        mod.classify(None, {"env": "clear", "group": {"account_id": 1, "resource_type": "ec2"}}, current_user=_USER)
    assert e.value.status_code == 400
