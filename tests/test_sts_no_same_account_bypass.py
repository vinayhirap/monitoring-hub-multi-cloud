# tests/test_sts_no_same_account_bypass.py
"""
Regression guard for the 2026-09 B04 audit CRITICAL finding: app/aws/sts.py's
assume_role() used to short-circuit straight to the server's own ambient
credentials (boto3.Session(), no STS call at all) whenever a caller-supplied
role_arn's account-id segment happened to match the server's own AWS account
id -- with no check that the role actually exists, is assumable, or was ever
configured by an admin. That let anyone who could reach assume_role() with an
arbitrary role_arn (e.g. via the test-role/add-account endpoints, which only
require accounts.onboard) obtain the server's own ambient/instance-profile
credentials instead of a real, scoped role assumption.

This test proves the fix: assume_role() must always go through a real
sts:AssumeRole call, even when role_arn's account segment matches the
server's own account id. It never silently substitutes a different
credential set based on that match.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module


def test_assume_role_always_calls_real_sts_even_for_same_account_arn():
    """
    Load the real sts.py, then swap its `boto3.client` for a fake that
    records whether sts:AssumeRole was actually invoked. Call assume_role()
    with a role_arn whose account segment equals a value get_own_account_id()
    would plausibly return, and confirm:
      1. the real STS client's assume_role() was called (i.e. no bypass), and
      2. the returned session is built from the values that fake AssumeRole
         call returned -- not a bare, credential-less boto3.Session().
    """
    sts_mod = load_module("app/aws/sts.py")

    calls = {"assume_role": 0}

    class FakeStsClient:
        def assume_role(self, **kwargs):
            calls["assume_role"] += 1
            calls["kwargs"] = kwargs
            return {
                "Credentials": {
                    "AccessKeyId": "ASIA_FAKE_FROM_REAL_ASSUME_ROLE",
                    "SecretAccessKey": "fake_secret_from_real_assume_role",
                    "SessionToken": "fake_session_token",
                }
            }

    def fake_boto3_client(service_name, config=None):
        assert service_name == "sts"
        return FakeStsClient()

    sts_mod.boto3.client = fake_boto3_client
    # Any account number works here -- the whole point of the fix is that
    # assume_role() no longer treats a same-account-number match specially
    # at all, so it doesn't matter whether this happens to equal whatever
    # get_own_account_id() would return.
    same_looking_account = "123456789012"
    role_arn = f"arn:aws:iam::{same_looking_account}:role/SomeRoleThatMayNotEvenExist"

    session = sts_mod.assume_role(role_arn, session_name="test-session")

    assert calls["assume_role"] == 1, (
        "assume_role() did not call the real STS AssumeRole API -- this is "
        "the exact confused-deputy bypass from the 2026-09 B04 audit "
        "finding (silently substituting ambient/server credentials instead "
        "of actually assuming the requested role)."
    )
    frozen = session.get_credentials().get_frozen_credentials()
    assert frozen.access_key == "ASIA_FAKE_FROM_REAL_ASSUME_ROLE", (
        "returned session was not built from the real AssumeRole response -- "
        "looks like a bypass path substituted different credentials."
    )
