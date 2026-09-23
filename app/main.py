# app/main.py
from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import os
import threading

from app.api.alerts         import router as alerts_router
from app.api.admin.accounts import router as admin_accounts_router
from app.api.auth           import router as auth_router
from app.api.admin.users    import router as admin_users_router
from app.api.admin.groups   import router as admin_groups_router
from app.api.admin.roles       import router as admin_roles_router
from app.api.admin.rbac_scopes import router as admin_rbac_scopes_router
from app.api.admin.bindings    import router as admin_bindings_router
from app.api.permissions    import router as permissions_router
from app.api.settings       import router as settings_router
from app.api.live_data      import router as live_data_router
from app.api.audit_logs     import router as audit_logs_router
from app.api.metric_catalog import router as metric_catalog_router
from app.api.topology       import router as topology_router
from app.api.op_events      import router as op_events_router
from app.api.escalation     import router as escalation_router
from app.api.incidents      import router as incidents_router
from app.api.reports        import router as reports_router
from app.api.nlquery        import router as nlquery_router
from app.api.synthetic      import router as synthetic_router
from app.api.webhooks       import router as webhooks_router
from app.api.deploy_risk    import router as deploy_risk_router
from app.api.sso            import router as sso_router
from app.api.slo            import router as slo_router
from app.api.security       import router as security_router
from app.api.maintenance    import router as maintenance_router
from app.api.status_page    import admin_router as status_page_admin_router, public_router as status_page_public_router
from app.auth.deps          import get_current_user, COOKIE_NAME, validate_session_claims
from app.auth.security      import decode_token

from app.ws.manager import ws_manager, KNOWN_CHANNELS
from app.ws.pusher  import redis_listener, stop_listener

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


async def _safe_redis_listener():
    try:
        await redis_listener()
    except Exception as e:
        logger.warning(f"Redis listener crashed (server continues): {e}")


def _run_collector(leader_event):
    try:
        from app.collector.scheduler import run_loop
        run_loop(leader_event)
    except Exception as e:
        logger.error(f"Collector crashed: {e}")


def _run_describe_poll_loop(leader_event):
    """
    Free EC2 status + ALB target health via Describe APIs — not CloudWatch,
    zero GetMetricData cost either way, so this runs on its own tight loop
    (every 60s) independent of the tiered scheduler's cadence, for the
    lowest latency the AWS Describe APIs can give us.

    leader_event: checked once per cycle so this loop stops itself if this
    worker ever loses collector leadership mid-run (see app/collector/
    leader.py's docstring for the 2026-09-12 incident this closes the gap
    on — losing the DB lock used to leave this loop running forever).
    """
    import time
    from app.aws.describe_polling import poll_all
    interval = 60   # polling audit 2026-09-23: 30 s bought no alerting speed (alerts evaluate every 2-5 min) and doubled Describe* throttling exposure
    while True:
        if not leader_event.is_set():
            logger.warning("[describe-poll] leadership lost -- stopping this loop")
            return
        try:
            poll_all()
        except Exception as e:
            logger.warning(f"Describe-poll loop error: {e}")
        time.sleep(interval)


def _run_multicloud_collector(leader_event):
    """Azure/GCP metric collection — see app/collector/multicloud_scheduler.py for why
    this is a separate loop from the AWS tiered scheduler rather than folded into it."""
    try:
        from app.collector.multicloud_scheduler import run_loop
        run_loop(leader_event)
    except Exception as e:
        logger.error(f"Multi-cloud collector crashed: {e}")


def _start_all_collector_threads(leader_event, collector_enabled=True, describe_poll_enabled=True):
    if collector_enabled:
        threading.Thread(target=_run_collector, args=(leader_event,), daemon=True, name="collector").start()
    if describe_poll_enabled:
        threading.Thread(target=_run_describe_poll_loop, args=(leader_event,), daemon=True, name="describe-poll").start()
    if collector_enabled:
        # Azure/GCP metric collection calls each provider's own billed
        # monitoring API (Azure Monitor / GCP Cloud Monitoring) -- the
        # same cost category as AWS's CloudWatch-based tiered scheduler,
        # not the free describe-poll loop -- so this stays tied to
        # collector_enabled, not describe_poll_enabled.
        threading.Thread(target=_run_multicloud_collector, args=(leader_event,), daemon=True, name="multicloud-collector").start()
    # Report-job sweeper: independent of collector_enabled/describe_poll_enabled
    # (it requeues stuck report_jobs, unrelated to CloudWatch/Describe polling
    # cost) but still leader-guarded so only one app instance runs it.
    # Gated on REPORTS_ENABLED (2026-09-19) -- this is the actual source of
    # the "table doesn't exist" log noise on any box where the feature is
    # off (e.g. dev, if only prod has REPORTS_ENABLED=true): with the flag
    # off, don't even start the thread rather than let it loop and log.
    if os.getenv("REPORTS_ENABLED", "false").strip().lower() in ("true", "1", "yes"):
        from app.reports.worker import run_sweeper_loop
        threading.Thread(target=run_sweeper_loop, args=(leader_event,), daemon=True, name="report-sweeper").start()


@asynccontextmanager
async def lifespan(app):
    # ── Startup ───────────────────────────────────────────────
    # Leader election across uvicorn workers -- see app/collector/leader.py
    # for why this exists (Sep 5 2026 incident: duplicate collector loops
    # across --workers 2 caused DB deadlocks + doubled AWS API calls).
    # Only the worker that wins the MySQL named lock actually starts the
    # background threads; others stand by and take over automatically if
    # the leader worker dies.
    #
    # Two independent flags, not one: the describe-poll loop (EC2 status,
    # ALB target health, EBS/managed-disk attachment topology sync) makes
    # zero CloudWatch/GetMetricData calls -- it has always been free to
    # run continuously regardless of whether the paid tiered collector is
    # on. Previously it was bundled into COLLECTOR_ENABLED purely because
    # _start_all_collector_threads() started every thread together, with
    # no way to turn off just the billed ones. Splitting this out lets a
    # cost-conscious environment (dev) leave topology/EC2-status/ALB-
    # health continuously fresh without paying for CloudWatch polling --
    # see the 2026-09-13 Topology-page conversation this was requested
    # in. DESCRIBE_POLL_ENABLED defaults to whatever COLLECTOR_ENABLED
    # resolves to when unset, so an existing .env with only
    # COLLECTOR_ENABLED set keeps its exact current combined behavior.
    collector_enabled = os.getenv("COLLECTOR_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    describe_poll_enabled = os.getenv("DESCRIBE_POLL_ENABLED", str(collector_enabled)).strip().lower() not in ("false", "0", "no")
    if collector_enabled or describe_poll_enabled:
        from app.collector.leader import run_when_leader
        run_when_leader(lambda leader_event: _start_all_collector_threads(leader_event, collector_enabled, describe_poll_enabled))
    else:
        logger.warning(
            "COLLECTOR_ENABLED=false and DESCRIBE_POLL_ENABLED=false -- skipping leader "
            "election and all collector threads (AWS/Azure/GCP scheduler, describe-poll). "
            "This process will serve UI/API traffic only and make zero cloud provider API calls."
        )
    redis_task = asyncio.create_task(_safe_redis_listener())
    logger.info(
        "Startup complete — collector %s, describe-poll %s, Redis listener started",
        "enabled" if collector_enabled else "disabled (COLLECTOR_ENABLED=false)",
        "enabled" if describe_poll_enabled else "disabled (DESCRIBE_POLL_ENABLED=false)",
    )
    yield
    # ── Shutdown ────────────────────────────────────────────────
    logger.info("Shutting down")
    stop_listener()
    redis_task.cancel()
    try:
        await asyncio.wait_for(redis_task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    logger.info("Redis listener stopped cleanly")


app = FastAPI(title="CloudOps API", version="0.3.0", lifespan=lifespan)

_cors_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    # Explicit origins are required here (not "*") because credentialed
    # (cookie-based) requests need the browser to see its own exact
    # origin echoed back in the response — wildcard + credentials is
    # rejected by browsers outright and would silently break session
    # cookies. Configure via CORS_ALLOWED_ORIGINS in .env.
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request, call_next):
    """
    Baseline security headers -- previously set nowhere (neither here
    nor in deploy/nginx.conf), so every response left the defaults of
    "no policy at all" for clickjacking/MIME-sniffing/referrer leakage
    protections. Applied here rather than in nginx so they travel with
    the app regardless of how it's fronted, and can't silently
    disappear if the reverse-proxy config drifts.

    No CSP `upgrade-insecure-requests`/HSTS forced on by default: this
    deployment currently serves plain HTTP (COOKIE_SECURE defaults to
    false for the same reason -- see app/api/auth.py), and sending
    Strict-Transport-Security or forcing HTTPS upgrades over a
    non-HTTPS origin would break the app outright rather than harden
    it. HSTS is added automatically once request.url.scheme is https,
    i.e. as soon as this is placed behind TLS per the deployment
    guide's Security Checklist.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    )
    # frame-ancestors 'none' backs up X-Frame-Options for browsers that
    # honor CSP over the legacy header; base-uri/object-src pinned down
    # as cheap, safe-by-default hardening that this SPA doesn't need to
    # relax (it doesn't use <base> or plugins). style-src needs
    # 'unsafe-inline' because the React app sets inline style={{...}}
    # attributes throughout (frontend/src/**) -- tightening that to a
    # nonce/hash scheme would need frontend build changes out of scope
    # here. font/style-src also allow Google Fonts, the only external
    # resource frontend/index.html actually loads; connect-src allows
    # ws/wss for the same-origin WebSocket feed.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/")
def root():
    return {"status": "ok", "version": "0.3.0"}


@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str):
    # SECURITY: this endpoint previously accepted every connection with
    # no authentication at all. The channels it serves ("overview",
    # "alerts", "metrics") broadcast live account IDs/names, regions,
    # CPU/memory values, and alert severity/thresholds for every cloud
    # account in the system -- an anonymous client (no login, no
    # cookie) could open ws://<host>/ws/overview directly and receive
    # that live feed for accounts far outside anything an authenticated
    # viewer's RBAC scope would ever show them. Same-origin browser
    # WebSocket handshakes already carry the httpOnly mh_session
    # cookie automatically (see frontend/src/hooks/useWebSocket.js --
    # it connects to a same-origin ws://.../ws/{channel} URL, no
    # frontend change needed), so we validate it the same way
    # get_current_user does for REST routes before accepting the
    # upgrade.
    #
    # NOTE: authentication closes the "must be logged in at all" gap. As of
    # 2026-09-20 the connection's account scope (get_accessible_account_ids)
    # is also captured below and ws/manager.py only delivers payloads that
    # carry an `account_id` to sockets whose scope includes it. Region-level
    # scope (get_effective_scope's region grants) is still NOT applied to
    # pushes; a scope change made mid-connection applies on reconnect.
    # Audit B01 follow-up: decode_token() only checks the JWT signature/expiry
    # -- it says nothing about whether the session behind it is still valid.
    # A cookie from a user who has since logged out (POST /api/auth/logout,
    # which revokes the token's jti), been deactivated, or had their role
    # changed would decode fine forever, up to 12h. REST routes already
    # catch this via get_current_user() -> validate_session_claims(); the
    # WebSocket endpoint didn't, so a revoked/deactivated session could
    # still open (or keep) a live feed. validate_session_claims() does the
    # same active/token_version/revoked-jti check used everywhere else, and
    # fails closed with an HTTPException (503) if the check itself can't run.
    # Audit b05: {channel} is also validated against the fixed set of
    # channels this app actually serves, rather than accepted as an
    # arbitrary client-supplied string -- see ws/manager.py's
    # KNOWN_CHANNELS for why an unvalidated value was a (small)
    # unbounded-memory-growth footgun. Checked first, before any
    # cookie/DB work, same "reject cheaply before doing anything
    # expensive" shape as the auth checks below.
    if channel not in KNOWN_CHANNELS:
        await websocket.close(code=4404)
        return

    token = websocket.cookies.get(COOKIE_NAME)
    if not token:
        await websocket.close(code=4401)
        return
    try:
        claims = decode_token(token)
        validate_session_claims(claims)
    except Exception:
        await websocket.close(code=4401)
        return

    # Per-connection account scope (see ws/manager.py). Fail CLOSED: if the
    # scope cannot be resolved, deliver nothing account-specific.
    try:
        from starlette.concurrency import run_in_threadpool
        from app.auth.authorization import get_accessible_account_ids
        accessible = await run_in_threadpool(get_accessible_account_ids, claims)
    except Exception:
        accessible = set()

    await ws_manager.connect(websocket, channel, accessible)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                # Re-check the session on every client ping (frontend pings
                # every 10s -- see useWebSocket.js) so a logout/deactivation/
                # role change that happens mid-connection closes the socket
                # within ~10s instead of only at the JWT's 12h expiry.
                try:
                    validate_session_claims(claims)
                except Exception:
                    await websocket.close(code=4401)
                    ws_manager.disconnect(websocket, channel)
                    return
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, channel)


@app.get("/ws/status")
async def ws_status():
    return {"connections": ws_manager.connection_count()}


# auth_router stays public (it contains /login itself; /me and
# /change-password enforce auth per-route internally). admin_users_router
# enforces admin-only per-route internally (app/api/admin/users.py).
# Every other router below requires a valid session at minimum — more
# specific role/scope checks are a later authorization phase.
_auth_dep = [Depends(get_current_user)]

app.include_router(alerts_router,         prefix="/api", dependencies=_auth_dep)
app.include_router(admin_accounts_router, dependencies=_auth_dep)
app.include_router(auth_router)
app.include_router(admin_users_router)
app.include_router(admin_groups_router)
app.include_router(admin_roles_router)
app.include_router(admin_rbac_scopes_router)
app.include_router(admin_bindings_router)
app.include_router(permissions_router)
app.include_router(live_data_router,      dependencies=_auth_dep)
app.include_router(audit_logs_router,     dependencies=_auth_dep)
app.include_router(settings_router,       dependencies=_auth_dep)
app.include_router(metric_catalog_router, dependencies=_auth_dep)
app.include_router(topology_router,       dependencies=_auth_dep)
app.include_router(op_events_router,      dependencies=_auth_dep)
app.include_router(escalation_router,     dependencies=_auth_dep)
app.include_router(incidents_router,      dependencies=_auth_dep)
app.include_router(reports_router,        dependencies=_auth_dep)
app.include_router(nlquery_router,        prefix="/api", dependencies=_auth_dep)
app.include_router(synthetic_router,      dependencies=_auth_dep)
# NOTE: webhooks_router deliberately has NO _auth_dep -- it's called by
# an external CI/CD system with no browser session, authenticated via
# its own X-Webhook-Token header check (see app/api/webhooks.py's
# _check_webhook_token()), not get_current_user()'s cookie/JWT flow.
app.include_router(webhooks_router)
app.include_router(deploy_risk_router,    dependencies=_auth_dep)
# NOTE: sso_router deliberately has NO _auth_dep -- like auth_router
# above, these ARE the pre-authentication login flow (SP-initiated
# login redirect, IdP callback, public SP metadata). See
# app/api/sso.py's module docstring. Every endpoint inside is itself
# gated on SSO_SAML_ENABLED (503 until explicitly turned on).
app.include_router(sso_router)
app.include_router(slo_router,            dependencies=_auth_dep)
app.include_router(security_router,       dependencies=_auth_dep)
app.include_router(maintenance_router,    dependencies=_auth_dep)
app.include_router(status_page_admin_router, dependencies=_auth_dep)
# NOTE: status_page_public_router deliberately has NO _auth_dep -- this
# IS the public status page (status.yourcompany.com style), meant to
# be reachable with no login. See app/api/status_page.py's module
# docstring for the sanitization boundary that makes this safe.
app.include_router(status_page_public_router)
