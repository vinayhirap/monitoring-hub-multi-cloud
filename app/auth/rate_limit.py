# app/auth/rate_limit.py
"""
Fixed-window rate limiting for unauthenticated, abuse-prone endpoints --
login, forgot-password, reset-password. None of these had ANY limit
before this: an attacker could hammer /api/auth/login with unlimited
password guesses (credential stuffing / brute force), or spam
/api/auth/forgot-password (each call does a real DB lookup and, if SMTP
is configured, a real outbound email send).

Backed by Redis (INCR + EXPIRE, the standard fixed-window pattern) --
reuses the SAME optional-Redis philosophy already established by
app/ws/publisher.py and app/ws/pusher.py in this codebase: Redis being
down should degrade real-time websocket pushes, not take down login
entirely. Rate limiting here therefore FAILS OPEN (allows the request)
if Redis is unreachable, logged at warning level so an operator notices
Redis is down for a reason beyond just "the live tiles stopped
updating" -- the alternative, failing closed, would mean a Redis outage
locks every single user out of the app, including during an incident
where they need in to fix something. That trade-off is deliberate: an
attacker temporarily unthrottled during a Redis outage is a smaller
risk than every legitimate user being locked out during one.

REQUIRES uvicorn to be started with --proxy-headers --forwarded-allow-
ips=127.0.0.1 (see deploy/deploy.sh's ExecStart) -- without it,
request.client.host is always 127.0.0.1 (nginx's own IP) regardless of
who's actually connecting, which would put every real user in the same
rate-limit bucket.
"""
import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_client = None
_warned_no_proxy_headers = False


def _get_redis():
    global _client
    if _client is None:
        try:
            import redis
            _client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True,
                                   socket_connect_timeout=2)
            _client.ping()
        except Exception as e:
            logger.warning(f"Rate limiter: Redis unavailable ({e}) -- failing OPEN "
                            f"(no rate limiting enforced) rather than blocking login/"
                            f"password-reset entirely during a Redis outage.")
            _client = None
    return _client


def _client_ip(request: Request) -> str:
    global _warned_no_proxy_headers
    ip = request.client.host if request.client else "unknown"
    if ip == "127.0.0.1" and not _warned_no_proxy_headers:
        # Not necessarily wrong (e.g. local dev, or a health check hitting
        # the app directly) but worth a one-time heads-up in production,
        # since it silently defeats IP-based rate limiting if uvicorn's
        # --proxy-headers isn't actually taking effect.
        _warned_no_proxy_headers = True
        logger.warning(
            "Rate limiter: request.client.host is 127.0.0.1 -- if this app is "
            "running behind nginx in production, confirm uvicorn was started "
            "with --proxy-headers --forwarded-allow-ips=127.0.0.1 (see "
            "deploy/deploy.sh), or every real visitor will share one "
            "rate-limit bucket."
        )
    return ip


def check_rate_limit(key: str, max_attempts: int, window_seconds: int) -> None:
    """
    Raises HTTPException(429) if `key` has already hit `max_attempts`
    within the current `window_seconds` fixed window. No-op (fails
    open) if Redis is unavailable -- see module docstring.
    """
    r = _get_redis()
    if r is None:
        return
    try:
        redis_key = f"ratelimit:{key}"
        count = r.incr(redis_key)
        if count == 1:
            r.expire(redis_key, window_seconds)
            ttl = window_seconds
        else:
            ttl = r.ttl(redis_key)
            if ttl == -1:
                # INCR succeeded but the EXPIRE that should have followed it
                # never did (crash/timeout between the two calls). Without
                # this repair the key would live forever and permanently
                # lock out that IP/username once it passes max_attempts.
                r.expire(redis_key, window_seconds)
                ttl = window_seconds
        if count > max_attempts:
            retry_after = ttl if ttl and ttl > 0 else window_seconds
            raise HTTPException(
                status_code=429,
                detail=f"Too many attempts. Try again in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Rate limiter check failed ({e}) -- failing open for this request")


def _known_ip_key(uname: str, ip: str) -> str:
    return f"login:known:{uname}:{ip}"


def _is_known_login_ip(uname: str, ip: str) -> bool:
    """True if this username has previously logged in successfully from this
    IP (see record_login_success). Any Redis problem => False (treated as an
    unknown IP, i.e. the stricter shared bucket)."""
    r = _get_redis()
    if r is None:
        return False
    try:
        return bool(r.exists(_known_ip_key(uname, ip)))
    except Exception:
        return False


def enforce_login_rate_limit(request: Request, username: str) -> None:
    """
    Two independent limits, both must pass:
      - Per-IP: stops a single source from brute-forcing ANY account.
      - Per-username: stops a distributed attack (many IPs/a botnet)
        from brute-forcing ONE specific account, e.g. 'admin'.
    A shared per-username bucket lets anyone lock a real user out just by
    failing 10 logins for that name, so an IP the user has ALREADY logged in
    from successfully (last LOGIN_KNOWN_IP_TTL_DAYS days) is counted in its
    own (username, ip) bucket instead: an attacker elsewhere can no longer
    exhaust it. Attackers on unknown IPs still share the strict per-username
    bucket, so brute-force protection is unchanged.
    Defaults are generous enough not to lock out a real user who
    mistypes their password a few times, tight enough to make online
    brute-forcing impractical. Configurable via env vars for
    environments that want tighter/looser limits without a code change.
    """
    ip_max    = int(os.getenv("LOGIN_RATE_LIMIT_PER_IP", 20))
    ip_window = int(os.getenv("LOGIN_RATE_LIMIT_PER_IP_WINDOW_SECONDS", 300))
    user_max    = int(os.getenv("LOGIN_RATE_LIMIT_PER_USERNAME", 10))
    user_window = int(os.getenv("LOGIN_RATE_LIMIT_PER_USERNAME_WINDOW_SECONDS", 300))

    ip = _client_ip(request)
    check_rate_limit(f"login:ip:{ip}", ip_max, ip_window)
    if username:
        uname = username.lower().strip()
        if _is_known_login_ip(uname, ip):
            check_rate_limit(f"login:userip:{uname}:{ip}", user_max, user_window)
        else:
            check_rate_limit(f"login:user:{uname}", user_max, user_window)


def record_login_success(request: Request, username: str) -> None:
    """Call after a VERIFIED login: remembers (username, ip) as a known pair
    and clears the shared per-username bucket. Best-effort; never raises."""
    r = _get_redis()
    if r is None or not username:
        return
    try:
        uname = username.lower().strip()
        days = int(os.getenv("LOGIN_KNOWN_IP_TTL_DAYS", 30))
        r.set(_known_ip_key(uname, _client_ip(request)), "1", ex=days * 86400)
        r.delete(f"ratelimit:login:user:{uname}")
    except Exception as e:
        logger.warning(f"Rate limiter: could not record login success ({e})")


def enforce_change_password_rate_limit(user_id: int) -> None:
    """
    Per session-user. change-password verifies the CURRENT password, so
    without a limit a stolen session cookie doubles as an unthrottled online
    guessing oracle for that password (login's limiter never sees it).
    """
    max_attempts = int(os.getenv("CHANGE_PASSWORD_RATE_LIMIT_PER_USER", 10))
    window       = int(os.getenv("CHANGE_PASSWORD_RATE_LIMIT_WINDOW_SECONDS", 900))
    check_rate_limit(f"change-password:user:{int(user_id)}", max_attempts, window)


def enforce_forgot_password_rate_limit(request: Request) -> None:
    """
    Per-IP only (no username limit -- the whole point of this endpoint
    is it doesn't reveal whether a username exists, so a per-username
    bucket would itself be a timing/enumeration side channel). Tighter
    than login's limit: every call here either does a real DB lookup
    or, when SMTP is configured, sends a real email -- spamming this
    is cheap for an attacker and costly for the app/mail provider.
    """
    max_attempts = int(os.getenv("FORGOT_PASSWORD_RATE_LIMIT_PER_IP", 5))
    window       = int(os.getenv("FORGOT_PASSWORD_RATE_LIMIT_WINDOW_SECONDS", 900))
    check_rate_limit(f"forgot-password:ip:{_client_ip(request)}", max_attempts, window)


def enforce_reset_password_rate_limit(request: Request) -> None:
    """
    Per-IP. Defense-in-depth against brute-forcing the reset token
    itself -- the token's own entropy is the primary defense (see
    app/api/auth.py's forgot_password), this just makes high-volume
    guessing impractical even if that ever regressed.
    """
    max_attempts = int(os.getenv("RESET_PASSWORD_RATE_LIMIT_PER_IP", 10))
    window       = int(os.getenv("RESET_PASSWORD_RATE_LIMIT_WINDOW_SECONDS", 900))
    check_rate_limit(f"reset-password:ip:{_client_ip(request)}", max_attempts, window)


def enforce_sso_rate_limit(request: Request) -> None:
    """
    Per-IP limit for the unauthenticated SAML endpoints. /sso/login writes
    a Redis key per call and /sso/acs runs XML-signature verification, so
    both are cheap for an attacker to spam and costly for the app.
    """
    max_attempts = int(os.getenv("SSO_RATE_LIMIT_PER_IP", 30))
    window       = int(os.getenv("SSO_RATE_LIMIT_WINDOW_SECONDS", 300))
    check_rate_limit(f"sso:ip:{_client_ip(request)}", max_attempts, window)
