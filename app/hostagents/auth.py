# app/hostagents/auth.py
"""
Token / enrollment-key primitives and agent authentication for the host agent.

  agent token       "cah_" + 40 url-safe chars        enrollment key   "cak_" + 32 url-safe chars
  Only the SHA-256 hex digest is stored; the plaintext is returned once and never logged.

authenticate_agent(token) is the single entry point every agent-facing route uses (ingest imports it). It is
read-only: it updates nothing (last_seen_at / last_seq belong to the ingest path).
"""
import hashlib
import hmac
import logging
import re
import secrets

from app.db import get_connection
from app.hostagents import settings

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "cah_"
KEY_PREFIX = "cak_"
_TOKEN_BYTES = 30        # token_urlsafe(30) -> exactly 40 chars
_KEY_BYTES = 24          # token_urlsafe(24) -> exactly 32 chars
_TOKEN_RE = re.compile(r"^cah_[A-Za-z0-9_-]{40}$")
_KEY_RE = re.compile(r"^cak_[A-Za-z0-9_-]{32}$")


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def generate_enrollment_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)


def hash_secret(value: str) -> str:
    """SHA-256 hex (64 lowercase chars) of a token or enrollment key."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def looks_like_token(value) -> bool:
    return isinstance(value, str) and _TOKEN_RE.match(value) is not None


def looks_like_enrollment_key(value) -> bool:
    return isinstance(value, str) and _KEY_RE.match(value) is not None


def authenticate_agent(token: str):
    """
    -> {"host_agent_id", "aws_account_id", "hostname", "resource_id", "config_version"} or None.

    None when: the feature is disabled, the token is malformed / unknown / revoked, or its host is revoked.
    The stored hash is compared with hmac.compare_digest. Never writes. Never raises for a bad token.
    """
    if not settings.enabled() or not looks_like_token(token):
        return None
    digest = hash_secret(token)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT t.token_hash, t.revoked_at, h.id AS host_agent_id, h.aws_account_id, h.hostname, "
                "h.resource_id, h.config_version, h.status "
                "FROM host_agent_tokens t JOIN host_agents h ON h.id = t.host_agent_id "
                "WHERE t.token_hash = %s",
                (digest,),
            )
            row = cur.fetchone()
        finally:
            cur.close()
    finally:
        conn.close()
    if not row:
        return None
    if not hmac.compare_digest(str(row["token_hash"]).encode("utf-8"), digest.encode("utf-8")):
        return None
    if row["revoked_at"] is not None or row["status"] != "active":
        return None
    return {
        "host_agent_id": row["host_agent_id"],
        "aws_account_id": row["aws_account_id"],
        "hostname": row["hostname"],
        "resource_id": row["resource_id"],
        "config_version": row["config_version"],
    }
