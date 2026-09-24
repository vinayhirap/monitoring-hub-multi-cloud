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
explicitly configured" pattern as LLM_SUMMARY_ENABLED
-- if the token isn't set in .env, this endpoint refuses every request
with 503, never silently accepts unauthenticated writes.
"""
import hmac
import json
import logging
import os

from fastapi import APIRouter, Body, HTTPException, Header, Request
from app.db import get_connection
from app.auth.rate_limit import check_rate_limit, _client_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])

# Per-field caps (audit b08): every free-text field lands in op_events
# and is later rendered in RCA text; unbounded input let one call write
# megabytes per row.
_FIELD_LIMITS = {"service": 200, "version": 200, "actor": 200, "description": 2000}
_RESOURCE_ID_LIMIT = 512
# Per-client-IP request budget. Bounds token brute force and op_events
# flooding by a leaked token (fails open if Redis is down, same as login).
WEBHOOK_RATE_LIMIT = 60
WEBHOOK_RATE_WINDOW_SECONDS = 60


def _per_account_tokens() -> dict:
    """DEPLOY_WEBHOOK_TOKENS = "3:tokenA,5:tokenB" -- optional per-account
    tokens. A per-account token can only record deployments for ITS
    account; the shared DEPLOY_WEBHOOK_TOKEN (if set) can still write to
    any account, exactly as before. Malformed entries are ignored."""
    out = {}
    for entry in (os.getenv("DEPLOY_WEBHOOK_TOKENS") or "").split(","):
        acct, sep, token = entry.strip().partition(":")
        if not sep or not token.strip():
            continue
        try:
            out[int(acct)] = token.strip()
        except ValueError:
            logger.warning("[webhooks] ignoring malformed DEPLOY_WEBHOOK_TOKENS entry")
    return out


def _check_webhook_token(provided: str):
    """Returns None if the caller presented the shared (all-accounts)
    token, or the set of account ids a per-account token may write to.
    Raises 503 if no token of either kind is configured, 401 on mismatch.

    Audit b08 follow-up: with only one shared secret, any CI pipeline
    (i.e. any client) holding it could inject fake 'deployments' into
    every other client's RCA and deploy-risk view."""
    expected = (os.getenv("DEPLOY_WEBHOOK_TOKEN") or "").strip()
    per_account = _per_account_tokens()
    if not expected and not per_account:
        raise HTTPException(
            status_code=503,
            detail="Deploy webhook is not configured -- set DEPLOY_WEBHOOK_TOKEN in .env to enable it",
        )
    if not provided:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook token")
    # Compare as bytes: hmac.compare_digest raises TypeError (-> 500) on
    # a non-ASCII str, so a junk header used to crash instead of 401.
    given = provided.strip().encode("utf-8")
    if expected and hmac.compare_digest(given, expected.encode("utf-8")):
        return None
    allowed = {acct for acct, tok in per_account.items() if hmac.compare_digest(given, tok.encode("utf-8"))}
    if not allowed:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook token")
    return allowed


def _text_field(payload: dict, key: str, default: str) -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{key} must be a string")
    value = str(value).strip()
    limit = _FIELD_LIMITS[key]
    if len(value) > limit:
        raise HTTPException(status_code=400, detail=f"{key} must be at most {limit} characters")
    return value


def _account_exists(account_id: int) -> bool:
    conn = get_connection(); cur = conn.cursor()
    try:
        # Active accounts only: a deactivated account is no longer
        # monitored, so a deployment recorded against it is never shown.
        cur.execute("SELECT 1 FROM aws_accounts WHERE id = %s AND status = 'active'", (account_id,))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


@router.post("/deploy")
def record_deployment(
    request: Request,
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
    check_rate_limit(f"webhook_deploy:{_client_ip(request)}", WEBHOOK_RATE_LIMIT, WEBHOOK_RATE_WINDOW_SECONDS)
    allowed_accounts = _check_webhook_token(x_webhook_token)

    raw_account = payload.get("aws_account_id")
    if raw_account is None or raw_account == "" or isinstance(raw_account, bool):
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    try:
        account_id = int(raw_account)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="aws_account_id must be an integer")
    if allowed_accounts is not None and account_id not in allowed_accounts:
        raise HTTPException(status_code=403, detail="This webhook token may not record deployments for that account")
    if account_id <= 0 or not _account_exists(account_id):
        raise HTTPException(status_code=404, detail="aws_account_id does not exist")

    service = _text_field(payload, "service", "unspecified service") or "unspecified service"
    version = _text_field(payload, "version", "")
    actor = _text_field(payload, "actor", "unknown") or "unknown"
    description = _text_field(payload, "description", "") or None
    resource_id = payload.get("resource_id")
    if resource_id is not None:
        if not isinstance(resource_id, str) or not resource_id.strip() or len(resource_id) > _RESOURCE_ID_LIMIT:
            raise HTTPException(status_code=400, detail=f"resource_id must be a string of at most {_RESOURCE_ID_LIMIT} characters")
        resource_id = resource_id.strip()

    message = f"Deployment: {service}" + (f" {version}" if version else "") + f" by {actor}"
    detail = {"service": service, "version": version, "actor": actor, "description": description}

    # Written directly, not via op_log.log_event(): log_event swallows
    # every DB error, so a failed write used to return {"status":
    # "recorded"} to the CI job while nothing was stored.
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO op_events (event_type, severity, aws_account_id, resource_id, message, detail)
            VALUES ('deployment', 'INFO', %s, %s, %s, %s)
        """, (account_id, resource_id, message, json.dumps(detail)))
        conn.commit()
        event_id = cur.lastrowid
    except Exception:
        conn.rollback()
        logger.exception("[webhooks] failed to record deployment for account %s", account_id)
        raise HTTPException(status_code=503, detail="Could not record deployment -- retry later")
    finally:
        cur.close(); conn.close()
    logger.info(f"[deployment] {message} (account={account_id})")
    return {"status": "recorded", "id": event_id}
