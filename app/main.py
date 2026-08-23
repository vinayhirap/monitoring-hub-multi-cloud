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
from app.api.settings       import router as settings_router
from app.api.live_data      import router as live_data_router
from app.api.audit_logs     import router as audit_logs_router
from app.api.metric_catalog import router as metric_catalog_router
from app.auth.deps          import get_current_user

from app.ws.manager import ws_manager
from app.ws.pusher  import redis_listener, stop_listener

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


async def _safe_redis_listener():
    try:
        await redis_listener()
    except Exception as e:
        logger.warning(f"Redis listener crashed (server continues): {e}")


def _run_collector():
    try:
        from app.collector.scheduler import run_loop
        run_loop()
    except Exception as e:
        logger.error(f"Collector crashed: {e}")


def _run_describe_poll_loop():
    """
    Free EC2 status + ALB target health via Describe APIs — not CloudWatch,
    zero GetMetricData cost either way, so this runs on its own tight loop
    (default 30s) independent of the tiered scheduler's cadence, for the
    lowest latency the AWS Describe APIs can give us.
    """
    import time
    from app.aws.describe_polling import poll_all
    interval = 30
    while True:
        try:
            poll_all()
        except Exception as e:
            logger.warning(f"Describe-poll loop error: {e}")
        time.sleep(interval)


@asynccontextmanager
async def lifespan(app):
    # ── Startup ───────────────────────────────────────────────
    threading.Thread(target=_run_collector, daemon=True, name="collector").start()
    threading.Thread(target=_run_describe_poll_loop, daemon=True, name="describe-poll").start()
    redis_task = asyncio.create_task(_safe_redis_listener())
    logger.info("Startup complete — collector running, Redis listener started")
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


@app.get("/")
def root():
    return {"status": "ok", "version": "0.3.0"}


@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str):
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
app.include_router(live_data_router,      dependencies=_auth_dep)
app.include_router(audit_logs_router,     dependencies=_auth_dep)
app.include_router(settings_router,       dependencies=_auth_dep)
app.include_router(metric_catalog_router, dependencies=_auth_dep)
