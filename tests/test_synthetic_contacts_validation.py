# tests/test_synthetic_contacts_validation.py -- input rules for per-check contacts and certificate thresholds.
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

from tests.conftest import load_module, install_stub, FakeConn


@pytest.fixture(scope="module")
def api():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return load_module("app/api/synthetic.py")


def bad(fn, *a):
    with pytest.raises(HTTPException) as e:
        fn(*a)
    assert e.value.status_code == 400
    return e.value.detail


def test_contacts_are_normalised(api):
    out = api._clean_contacts([{"email": " Owner@Corp.com ", "label": "Site\nowner"},
                               {"email": "ops@corp.com", "notify_outage": False}])
    assert out[0] == {"email": "Owner@Corp.com", "label": "Site owner", "notify_outage": True, "notify_cert": True}
    assert out[1]["notify_outage"] is False and out[1]["notify_cert"] is True


def test_contacts_reject_bad_input(api):
    bad(api._clean_contacts, "x")
    bad(api._clean_contacts, [{"email": "not-an-email"}])
    bad(api._clean_contacts, [{"email": "a@b.com, c@d.com"}])                     # one address per row
    bad(api._clean_contacts, [{"email": "a@b.com"}, {"email": "A@B.com"}])        # duplicate, any case
    bad(api._clean_contacts, [{"email": "a@b.com", "notify_outage": False, "notify_cert": False}])
    bad(api._clean_contacts, [{"email": f"u{i}@b.com"} for i in range(21)])
    bad(api._clean_contacts, [{"email": "a@b.com", "label": "x" * 101}])


def test_cert_pair_rules(api):
    assert api._cert_pair({}, "https") == {}
    assert api._cert_pair({"cert_warn_days": 14, "cert_crit_days": 3}, "https") == {"cert_warn_days": 14, "cert_crit_days": 3}
    assert api._cert_pair({"cert_warn_days": None, "cert_crit_days": None}, "https") == {"cert_warn_days": None, "cert_crit_days": None}
    bad(api._cert_pair, {"cert_warn_days": 14}, "https")                          # both or neither
    bad(api._cert_pair, {"cert_warn_days": 7, "cert_crit_days": 7}, "https")      # crit must be below warn
    bad(api._cert_pair, {"cert_warn_days": 1, "cert_crit_days": 1}, "https")
    bad(api._cert_pair, {"cert_warn_days": 400, "cert_crit_days": 3}, "https")
    bad(api._cert_pair, {"cert_warn_days": 14, "cert_crit_days": 3}, "tcp")       # https only
    bad(api._cert_pair, {"cert_warn_days": "x", "cert_crit_days": 3}, "https")


def test_cert_pair_patch_merges_with_stored_values(api):
    cur = {"cert_warn_days": 30, "cert_crit_days": 7}
    assert api._cert_pair({"cert_warn_days": 10}, "https", cur) == {"cert_warn_days": 10, "cert_crit_days": 7}
    bad(api._cert_pair, {"cert_warn_days": 5}, "https", cur)                      # would leave crit (7) >= warn (5)


def test_masking(api):
    assert api._mask_email("owner@corp.com") == "o***@corp.com"
    assert api._mask_email("") == "(hidden)"
