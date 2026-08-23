"""
app/auth/security.py

Password hashing + JWT session tokens.

JWT_SECRET must be set in the environment — there is deliberately NO
insecure fallback default. A shared/predictable default secret would
let anyone forge a valid admin session token, which defeats every
authorization check built on top of this module.
"""
import os
from datetime import datetime, timedelta

import bcrypt
import jwt

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 12 * 60  # 12 hours


def _get_secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError(
            "JWT_SECRET is not set. Generate one and add it to your .env file:\n"
            "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "then add JWT_SECRET=<the printed value> to .env (and .env.production on the server)."
        )
    return secret


def hash_password(password: str) -> str:
    # Raw bcrypt, not passlib — passlib's bcrypt backend detection is
    # broken with modern bcrypt versions (the same class of issue the
    # deployment log already hit once; app/api/auth.py avoids passlib
    # for this exact reason).
    return bcrypt.hashpw(password[:72].encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password[:72].encode(), hashed_password.encode())
    except Exception:
        return False


def create_access_token(user_id: int, username: str, role: str,
                         expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError (ExpiredSignatureError / InvalidTokenError / etc.) on failure."""
    payload = jwt.decode(token, _get_secret(), algorithms=[ALGORITHM])
    return {
        "id": int(payload["sub"]),
        "username": payload["username"],
        "role": payload["role"],
    }
