# app/api/webhooks.py
"""
Deploy-risk correlation, ingestion half (2026-09-14).

This app has always been able to correlate AWS CloudTrail activity and
its OWN config changes (audit_logs) against an alert -- see
app/collector/rca.py's _gather_signals(). It has never known about
APPLICATION deploys, because nothing upstream ever told it one
happened -- a code deploy is invisible to CloudWatch/Azure Monitor/GCP
metrics and to CloudTrail alike (unless the deploy itself calls a cloud
API, e.g. a Lambda update).

This is a single, deliberately generic ingestion endpoint any CI/CD
system (GitHub Actions, Jenkins, GitLab CI, a plain shell script) can
POST to at the end of a deploy step -- one curl call, no SDK, no new
service to run. It writes into the EXISTING op_events table (see
app/collector/op_log.py -- no new table needed) with
event_type='deployment', which app/collector/rca.py's
_gather_deployment_signal() then reads to flag "this alert started
shortly after a deployment" -- see rca.py's own docstring update for
how that's surfaced.

AUTH: machine-to-machine, not a logged-in user -- there is no browser
session here, just a CI job's curl call. Uses a single shared-secret
bearer token (DEPLOY_WEBHOOK_TOKEN in .env), same "off unless
explicitly configured" pattern as LLM_SUMMARY_ENABLED/ANTHROPIC_API_KEY
-- if the token isn't set in .env, this endpoint refuses every request
with 503, never silently accepts unauthenticated writes.
"""
import hmac
import logging
import os

from fastapi import APIRouter, Body, HTTPException, Header
from app.db import get_connection
from app.collector.op_log import log_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


def _check_webhook_token(provided: str):
    expected = os.getenv("DEPLOY_WEBHOOK_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Deploy webhook is not configured -- set DEPLOY_WEBHOOK_TOKEN in .env to enable it",
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook token")


def _account_exists(account_id: int) -> bool:
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM aws_accounts WHERE id = %s", (account_id,))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


@router.post("/deploy")
def record_deployment(
    payload: dict = Body(...),
    x_webhook_token: str = Header(None, alias="X-Webhook-Token"),
):
    """
    Body: {
      "aws_account_id": 3,               # required
      "resource_id": "i-0abc123",        # optional -- the specific resource this deploy touched
      "service": "payment-api",          # optional -- free-text label
      "version": "v2.14.0" or a commit sha,  # optional
      "actor": "vinayhirap",             # optional -- who/what triggered it
      "description": "..."               # optional -- free text (e.g. PR title)
    }

    Example CI step:
      curl -X POST https://<your-app>/api/webhooks/deploy \\
        -H "X-Webhook-Token: $DEPLOY_WEBHOOK_TOKEN" \\
        -H "Content-Type: application/json" \\
        -d '{"aws_account_id": 3, "service": "payment-api", "version": "'"$GIT_SHA"'", "actor": "'"$GITHUB_ACTOR"'"}'
    """
    _check_webhook_token(x_webhook_token)

    account_id = payload.get("aws_account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    if not _account_exists(int(account_id)):
        raise HTTPException(status_code=404, detail="aws_account_id does not exist")

    service = payload.get("service", "unspecified service")
    version = payload.get("version", "")
    actor = payload.get("actor", "unknown")
    resource_id = payload.get("resource_id")

    message = f"Deployment: {service}" + (f" {version}" if version else "") + f" by {actor}"
    log_event(
        "deployment", message, severity="INFO",
        account_id=int(account_id), resource_id=resource_id,
        detail={
            "service": service, "version": version, "actor": actor,
            "description": payload.get("description"),
        },
    )
    return {"status": "recorded"}
