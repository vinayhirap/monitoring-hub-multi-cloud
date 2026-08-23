# app/credentials.py
"""
Encryption-at-rest for non-AWS provider secrets (Azure client secret, GCP
service-account key JSON). AWS never needed this because IAM Role ARNs are
identities, not secrets — Azure/GCP's real auth methods are.

Secrets are stored in the `provider_credentials` table (see migration
010_provider_credentials_table.sql) as Fernet-encrypted blobs, keyed by
aws_accounts.id. The `credential_ref` column on aws_accounts holds a stable
opaque token (not the secret) so the DB row/API responses never carry it.

Encryption key resolution order:
  1. CREDENTIAL_ENCRYPTION_KEY env var (set this in production / .env)
  2. config/credential.key on disk — auto-generated on first use for local
     / dev convenience. This file is gitignored; it must never be committed.
"""
import os
import base64
import secrets as _secrets
from pathlib import Path

from cryptography.fernet import Fernet

from app.db import get_connection

_KEY_FILE = Path(__file__).resolve().parent.parent / "config" / "credential.key"


def _load_or_create_key() -> bytes:
    env_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode()

    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()

    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def new_credential_ref() -> str:
    """Opaque token stored on aws_accounts.credential_ref — not the secret itself."""
    return base64.urlsafe_b64encode(_secrets.token_bytes(18)).decode().rstrip("=")


def save_credential(account_id: int, provider: str, secret_plaintext: str, ref: str) -> None:
    """Encrypt and upsert the secret for this account. `secret_plaintext` is
    whatever the provider needs raw (Azure client secret string, or the full
    GCP service-account JSON key as text)."""
    token = _fernet().encrypt(secret_plaintext.encode())
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO provider_credentials (aws_account_id, provider, credential_ref, secret_encrypted)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            provider = VALUES(provider),
            credential_ref = VALUES(credential_ref),
            secret_encrypted = VALUES(secret_encrypted),
            updated_at = NOW()
    """, (account_id, provider, ref, token))
    conn.commit()
    cur.close()
    conn.close()


def load_credential(account_id: int) -> str | None:
    """Return the decrypted secret for this account, or None if not set
    (e.g. AWS accounts, which don't use this table at all)."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT secret_encrypted FROM provider_credentials WHERE aws_account_id = %s",
        (account_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    blob = row["secret_encrypted"]
    if isinstance(blob, str):
        blob = blob.encode()
    return _fernet().decrypt(blob).decode()


def delete_credential(account_id: int) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM provider_credentials WHERE aws_account_id = %s", (account_id,))
    conn.commit()
    cur.close()
    conn.close()
