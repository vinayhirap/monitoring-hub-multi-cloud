# tests/test_credential_choke_point.py
"""
Regression guard for the 2026-09-16 U4RAD/AuroGov Mumbai incident: four
independent code paths (app/aws/collector_direct.py's get_session,
app/aws/describe_polling.py's _session_for, app/api/metric_catalog.py's
_discover_aws_metrics, plus the account-summary/live-data callers of all
of these) each reimplemented AWS credential resolution using only
role_arn, silently falling back to ambient/self credentials for any
static-key account. All four now delegate to app.aws.sts.get_boto3_session()
-- the single real choke point, which is the only place that knows how
to resolve auth_mode == "static_keys".

Two layers, matching how the actual bug was found and fixed:

1. A real behavioral test of the choke point itself: given a
   static-key account, does it actually return a session built from
   the STORED keys, not ambient credentials? This is the invariant
   that matters; if this ever breaks, everything downstream is wrong
   regardless of how many callers correctly delegate to it.

2. A cheap source-level check on the four previously-broken call
   sites: each must still reference get_boto3_session somewhere in its
   own source. This won't catch every possible future regression, but
   it WILL catch the exact failure mode that happened four times in
   one day -- someone (or something) reverting one of these functions
   back to a role_arn-only branch -- without needing to mock boto3/AWS
   for each one.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub, REPO_ROOT


def test_get_boto3_session_uses_stored_static_keys_not_ambient():
    """
    The actual choke point (app/aws/sts.py get_boto3_session): a
    static_keys account must resolve to a boto3.Session built from the
    credential stored via app.credentials.save_credential/load_credential
    -- NOT ambient/instance credentials. This is the exact distinction
    that was broken in all four incident call sites.
    """
    import json

    fake_creds = json.dumps({
        "access_key_id": "AKIA_FAKE_U4RAD_KEY",
        "secret_access_key": "fake_secret_should_never_be_ambient",
    })
    install_stub("app.credentials", load_credential=lambda account_id: fake_creds)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)

    sts_mod = load_module("app/aws/sts.py")

    account = {"id": 10, "auth_mode": "static_keys", "role_arn": "", "external_id": None}
    session = sts_mod.get_boto3_session(account)

    frozen = session.get_credentials().get_frozen_credentials()
    assert frozen.access_key == "AKIA_FAKE_U4RAD_KEY", (
        "static_keys account resolved to something other than its own stored "
        "access key -- this is the exact defect class from the 2026-09-16 "
        "U4RAD incident (silent fallback to ambient/self credentials)."
    )
    assert frozen.secret_key == "fake_secret_should_never_be_ambient"


def test_get_boto3_session_missing_static_credential_raises_not_silent_fallback():
    """
    If auth_mode is static_keys but no credential row exists (onboarding
    failed partway through), get_boto3_session must raise loudly --
    NOT silently fall back to ambient credentials, which is what would
    turn a broken/incomplete onboarding into exactly this incident again.
    """
    install_stub("app.credentials", load_credential=lambda account_id: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    sts_mod = load_module("app/aws/sts.py")

    account = {"id": 999, "auth_mode": "static_keys", "role_arn": "", "external_id": None}
    try:
        sts_mod.get_boto3_session(account)
        assert False, "expected RuntimeError for a static_keys account with no stored credential"
    except RuntimeError:
        pass


_PREVIOUSLY_BROKEN_FILES = [
    "app/aws/collector_direct.py",   # get_session() -- Overview cards / Service Detail pages
    "app/aws/describe_polling.py",   # _session_for() -- EC2 status/ALB health/EBS attachment pollers
    "app/api/metric_catalog.py",     # _discover_aws_metrics() -- live namespace metric discovery
]


def test_previously_broken_call_sites_still_delegate_to_choke_point():
    """
    Cheap source-level tripwire: each of the four call sites fixed on
    2026-09-16 must still reference get_boto3_session. Catches a
    regression back to role_arn-only resolution without needing to
    mock AWS for each one individually.
    """
    for relative_path in _PREVIOUSLY_BROKEN_FILES:
        full_path = os.path.join(REPO_ROOT, relative_path)
        with open(full_path) as f:
            source = f.read()
        assert "get_boto3_session" in source, (
            f"{relative_path} no longer references get_boto3_session() -- this "
            f"file was one of four fixed for the 2026-09-16 U4RAD incident "
            f"(role_arn-only credential resolution silently using ambient/"
            f"wrong-account credentials for static-key accounts). If this "
            f"function was intentionally rewritten, make sure it still "
            f"resolves auth_mode == 'static_keys' correctly before removing "
            f"this check."
        )
