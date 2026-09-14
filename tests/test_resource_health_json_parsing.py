# tests/test_resource_health_json_parsing.py
"""
Regression guard for a real bug found in production (2026-09-14): the
mysql-connector driver this app uses (use_pure=True, see app/db.py)
returns JSON columns as raw strings, not parsed dicts -- the SAME
convention every other JSON-column consumer in this codebase already
works around (resources.tags is always json.loads()'d before use, see
alert_evaluator.py, multivariate_anomaly.py). list_resource_health()
in app/api/incidents.py skipped that step for resource_health's
score_reason column, so the frontend received a JSON STRING it
couldn't dot-access (health.score_reason.critical_alerts on a string
is undefined in JS) -- ServiceDetail.jsx's health-score panel silently
fell back to a generic "lowered by alert(s) on this resource" sentence
instead of the real critical/warning/blast-radius breakdown, in
production, visible in a real screenshot.
"""
import sys

# app/api/incidents.py transitively imports app.auth.permissions ->
# app.auth.deps -> app.auth.security (bcrypt/JWT) and app.db (mysql
# connector) at module load time -- none of which this test wants to
# actually exercise. install_stub() alone isn't enough here: it also
# creates FAKE stub packages for any missing dotted-path PARENT (see
# its own docstring), and if "app.auth" doesn't already exist as a
# REAL package by the time incidents.py's `from app.auth.permissions
# import require_permission` runs, that stubbed "app.auth" has no
# __path__, so Python can't import a submodule from it ("app.auth is
# not a package"). Pre-importing the real (empty, side-effect-free)
# app/app.auth __init__.py packages first means install_stub only
# needs to create/replace the LEAF modules below, leaving the real
# parent packages alone.
import app          # noqa: F401 -- real, empty __init__.py, safe
import app.auth     # noqa: F401 -- real, empty __init__.py, safe

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_common_stubs():
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))


def test_list_resource_health_parses_score_reason_json_string():
    raw_row = {
        "resource_id": "i-052ad4c2b1578740a",
        "health_score": 44,
        # This is exactly what the real mysql-connector driver returns
        # for a JSON column -- a string, not a dict.
        "score_reason": '{"critical_alerts": 1, "warning_alerts": 0, "alert_penalty": 40, "blast_radius_fan_out": 16, "blast_penalty": 16}',
        "computed_at": "2026-09-14 12:00:00",
    }

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = [raw_row]

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    _install_common_stubs()
    mod = load_module("app/api/incidents.py")

    result = mod.list_resource_health(account_id=7, current_user={"username": "admin"})

    assert len(result) == 1
    reason = result[0]["score_reason"]
    # The whole point of the fix: this must be a dict a frontend can
    # dot-access into (health.score_reason.critical_alerts), not the
    # raw string that produced the generic fallback text in production.
    assert isinstance(reason, dict)
    assert reason["critical_alerts"] == 1
    assert reason["blast_radius_fan_out"] == 16


def test_list_resource_health_tolerates_malformed_json():
    """A malformed/unexpected score_reason value must not crash the
    endpoint -- falls back to an empty dict rather than propagating a
    JSONDecodeError to the customer-facing page."""
    raw_row = {
        "resource_id": "i-broken", "health_score": 50,
        "score_reason": "not valid json{{{", "computed_at": "2026-09-14 12:00:00",
    }

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = [raw_row]

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    _install_common_stubs()
    mod = load_module("app/api/incidents.py")

    result = mod.list_resource_health(account_id=7, current_user={"username": "admin"})
    assert result[0]["score_reason"] == {}


def test_list_resource_health_leaves_already_parsed_dict_alone():
    """If a future driver/config change ever DOES return an
    already-parsed dict, this endpoint must not double-encode or choke
    on it -- isinstance-guarded, not an unconditional json.loads()."""
    raw_row = {
        "resource_id": "i-already-dict", "health_score": 70,
        "score_reason": {"critical_alerts": 0, "warning_alerts": 1},
        "computed_at": "2026-09-14 12:00:00",
    }

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = [raw_row]

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    _install_common_stubs()
    mod = load_module("app/api/incidents.py")

    result = mod.list_resource_health(account_id=7, current_user={"username": "admin"})
    assert result[0]["score_reason"] == {"critical_alerts": 0, "warning_alerts": 1}
