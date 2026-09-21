"""
app/auth/security.py

Password hashing + JWT session tokens.

JWT_SECRET must be set in the environment — there is deliberately NO
insecure fallback default. A shared/predictable default secret would
let anyone forge a valid admin session token, which defeats every
authorization check built on top of this module.

Session tokens carry a random `jti` (used for per-session logout
revocation) and a `tv` (the user's users.token_version at issue time;
bumped on password change/reset so every older session dies). Both are
enforced server-side in app/auth/deps.py — see audit B01.
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 12 * 60  # 12 hours

_BCRYPT_MAX_BYTES = 72


def _rounds_from_env() -> int:
    try:
        n = int(os.getenv("BCRYPT_ROUNDS", "12"))
    except ValueError:
        return 12
    return max(10, min(15, n))   # <10 is too weak, >15 makes login take seconds


BCRYPT_ROUNDS = _rounds_from_env()
_MIN_SECRET_LEN = 32
_warned_weak_secret = False


def _get_secret() -> str:
    global _warned_weak_secret
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError(
            "JWT_SECRET is not set. Generate one and add it to your .env file:\n"
            "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "then add JWT_SECRET=<the printed value> to .env."
        )
    if len(secret) < _MIN_SECRET_LEN and not _warned_weak_secret:
        # Warn, don't raise: raising would lock every user out of an
        # existing deployment on upgrade. A short HS256 secret can be
        # brute-forced offline from any captured cookie.
        _warned_weak_secret = True
        logger.warning(
            "JWT_SECRET is only %d characters — use at least %d "
            "(python -c \"import secrets; print(secrets.token_hex(32))\") "
            "and restart; changing it logs everyone out.",
            len(secret), _MIN_SECRET_LEN,
        )
    return secret


def _pw_bytes(password: str) -> bytes:
    # bcrypt only ever uses the first 72 BYTES. Truncate explicitly on
    # bytes (not characters) so behaviour is identical on bcrypt 4.x
    # (silent truncation) and bcrypt >= 5 (raises ValueError > 72 bytes).
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    # Raw bcrypt, not passlib — passlib's bcrypt backend detection is
    # broken with modern bcrypt versions (the same class of issue the
    # deployment log already hit once; app/api/auth.py avoids passlib
    # for this exact reason).
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(plain_password), hashed_password.encode())
    except Exception as e:
        logger.warning("Password verify error: %s", type(e).__name__)
        return False


def needs_rehash(hashed_password) -> bool:
    """True if a stored bcrypt hash was made with a different cost than the
    current BCRYPT_ROUNDS (auto-upgraded on the user's next successful login)."""
    try:
        return int(hashed_password.split("$")[2]) != BCRYPT_ROUNDS
    except Exception:
        return False


def create_access_token(user_id: int, username: str, role: str,
                         expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES,
                         token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": secrets.token_hex(16),
        "tv": int(token_version or 0),
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError (ExpiredSignatureError / InvalidTokenError / etc.) on failure.

    Algorithm is pinned (alg=none / RS-HS confusion rejected) and exp/iat/sub
    are mandatory. `jti`/`tv` are absent on tokens issued before audit B01;
    those decode as jti=None, tv=0 and keep working until they expire.
    """
    payload = jwt.decode(
        token, _get_secret(), algorithms=[ALGORITHM],
        options={"require": ["exp", "iat", "sub"]},
    )
    return {
        "id": int(payload["sub"]),
        "username": payload["username"],
        "role": payload["role"],
        "jti": payload.get("jti"),
        "tv": int(payload.get("tv", 0)),
        "exp": int(payload["exp"]),
    }
