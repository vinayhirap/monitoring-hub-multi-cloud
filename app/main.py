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
from app.api.permissions    import router as permissions_router
from app.api.settings       import router as settings_router
from app.api.live_data      import router as live_data_router
from app.api.audit_logs     import router as audit_logs_router
from app.api.metric_catalog import router as metric_catalog_router
from app.auth.deps          import get_current_user, COOKIE_NAME
from app.auth.security      import decode_token

from app.ws.manager import ws_manager
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
    (default 30s) independent of the tiered scheduler's cadence, for the
    lowest latency the AWS Describe APIs can give us.

    leader_event: checked once per cycle so this loop stops itself if this
    worker ever loses collector leadership mid-run (see app/collector/
    leader.py's docstring for the 2026-09-12 incident this closes the gap
    on — losing the DB lock used to leave this loop running forever).
    """
    import time
    from app.aws.describe_polling import poll_all
    interval = 30
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


def _start_all_collector_threads(leader_event):
    threading.Thread(target=_run_collector, args=(leader_event,), daemon=True, name="collector").start()
    threading.Thread(target=_run_describe_poll_loop, args=(leader_event,), daemon=True, name="describe-poll").start()
    threading.Thread(target=_run_multicloud_collector, args=(leader_event,), daemon=True, name="multicloud-collector").start()


@asynccontextmanager
async def lifespan(app):
    # ── Startup ───────────────────────────────────────────────
    # Leader election across uvicorn workers -- see app/collector/leader.py
    # for why this exists (Sep 5 2026 incident: duplicate collector loops
    # across --workers 2 caused DB deadlocks + doubled AWS API calls).
    # Only the worker that wins the MySQL named lock actually starts the
    # background threads; others stand by and take over automatically if
    # the leader worker dies.
    from app.collector.leader import run_when_leader
    run_when_leader(_start_all_collector_threads)
    redis_task = asyncio.create_task(_safe_redis_listener())
    logger.info("Startup complete — collector leader-election started, Redis listener started")
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
    # NOTE: this closes the "must be logged in at all" gap. It does
    # NOT yet filter broadcast payloads per-connection by the caller's
    # account/region scope (get_effective_scope) -- that would require
    # per-message filtering keyed to each connection's user, a larger
    # change to ws/manager.py + ws/publisher.py. Recorded as a
    # follow-up finding below; every currently-connected client is at
    # minimum an authenticated user of the system, which is the
    # binary access-control gap this fixes.
    token = websocket.cookies.get(COOKIE_NAME)
    if not token:
        await websocket.close(code=4401)
        return
    try:
        decode_token(token)
    except Exception:
        await websocket.close(code=4401)
        return

    await ws_manager.connect(websocket, channel)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
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
app.include_router(permissions_router)
app.include_router(live_data_router,      dependencies=_auth_dep)
app.include_router(audit_logs_router,     dependencies=_auth_dep)
app.include_router(settings_router,       dependencies=_auth_dep)
app.include_router(metric_catalog_router, dependencies=_auth_dep)
