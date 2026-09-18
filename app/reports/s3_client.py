# app/reports/s3_client.py
"""
Thin, deliberately narrow boto3 S3 wrapper for the report engine.

Design choices:
  - A SEPARATE, dedicated bucket (REPORTS_S3_BUCKET), not reused from
    any other feature -- so its bucket policy / lifecycle / KMS key can
    be scoped tightly to "generated reports", independent of anything
    else this app might ever store in S3.
  - Server-side encryption is mandatory on every PutObject
    (SSE-KMS if REPORTS_S3_KMS_KEY_ID is set, else SSE-S3/AES256 as a
    safe default -- never unencrypted).
  - Every write is content-addressed by sha256 in the key path and the
    hash is independently recomputed on download and compared -- this
    catches both S3-side corruption and a compromised/replaced object,
    not just transport errors.
  - No public access, no bucket ACLs. Reads only ever go out as
    short-lived presigned URLs (default 5 min) generated per-download-
    request, gated behind this app's own RBAC -- objects themselves
    are never publicly reachable, and a presigned URL is never longer
    lived than the single download it was minted for.
  - Retries: boto3's own adaptive retry mode (network/5xx/throttling)
    PLUS one manual retry loop here for the whole put/head/get
    operation, so a transient failure doesn't fail an entire report
    generation job outright (that's what report_jobs.attempts is for
    at the job level; this is the finer-grained per-call layer).
"""
import hashlib
import io
import logging
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_RETRYABLE_MAX = 3
_RETRYABLE_BASE_DELAY = 0.5  # seconds, exponential backoff


def _bucket() -> str:
    bucket = os.getenv("REPORTS_S3_BUCKET")
    if not bucket:
        raise RuntimeError(
            "REPORTS_S3_BUCKET is not set -- the report engine has no bucket "
            "to write to. Set it in .env (see AWS setup guide)."
        )
    return bucket


def _client():
    # boto3 resolves credentials via the standard chain -- on dev/prod
    # this means the EC2 instance role (IMDSv2), never a static key in
    # .env. See the AWS setup guide re: scoping that role's S3 policy
    # down to this one bucket/prefix rather than reusing a broad
    # "monitoring" role's existing permissions as-is.
    return boto3.client(
        "s3",
        region_name=os.getenv("AWS_DEFAULT_REGION", "ap-south-1"),
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _with_retry(fn, what: str):
    last_exc = None
    for attempt in range(1, _RETRYABLE_MAX + 1):
        try:
            return fn()
        except ClientError as e:
            last_exc = e
            code = e.response.get("Error", {}).get("Code", "")
            # Don't burn retries on things that will never succeed by
            # retrying (bad key, access denied, bucket missing).
            if code in ("AccessDenied", "NoSuchBucket", "NoSuchKey", "InvalidAccessKeyId"):
                raise
            delay = _RETRYABLE_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(f"S3 {what} attempt {attempt}/{_RETRYABLE_MAX} failed ({code}); retrying in {delay}s")
            time.sleep(delay)
    raise last_exc


def build_key(scope_type: str, scope_id: str, report_type: str, period_start, period_end, sha256_hex: str) -> str:
    """
    Layout: reports/{scope_type}/{scope_id}/{year}/{report_type}_{period_start}_{period_end}_{sha8}.pdf
    Organized by scope first so a per-client/per-account prefix can
    also be used as an IAM condition key (see AWS setup guide) if
    access ever needs to be split further than the app's own RBAC.
    """
    year = period_start.strftime("%Y")
    span = f"{period_start.strftime('%Y%m%d')}-{period_end.strftime('%Y%m%d')}"
    safe_scope_id = str(scope_id).replace("/", "_")
    return (
        f"reports/{scope_type.lower()}/{safe_scope_id}/{year}/"
        f"{report_type.lower()}_{span}_{sha256_hex[:8]}.pdf"
    )


def put_report(key: str, data: bytes, *, content_type: str = "application/pdf") -> dict:
    """Uploads with mandatory encryption + integrity metadata. Returns
    {'sha256', 'size_bytes', 'version_id'}."""
    sha256_hex = hashlib.sha256(data).hexdigest()
    bucket = _bucket()
    kms_key_id = os.getenv("REPORTS_S3_KMS_KEY_ID")

    extra = {
        "ContentType": content_type,
        "Metadata": {"sha256": sha256_hex, "generated-by": "monitoring-hub-reports"},
        # ObjectLock/legal-hold intentionally not set here -- retention
        # is enforced via the bucket lifecycle rule (see setup guide),
        # not per-object Object Lock, so reports remain deletable by an
        # admin action if ever legally required (right-to-erasure etc.)
        # without needing a Object-Lock-compliance-mode bucket.
    }
    if kms_key_id:
        extra["ServerSideEncryption"] = "aws:kms"
        extra["SSEKMSKeyId"] = kms_key_id
    else:
        extra["ServerSideEncryption"] = "AES256"

    def _do_put():
        return _client().put_object(Bucket=bucket, Key=key, Body=data, **extra)

    resp = _with_retry(_do_put, f"put_object({key})")
    return {
        "sha256": sha256_hex,
        "size_bytes": len(data),
        "version_id": resp.get("VersionId"),
    }


def get_report_bytes(key: str, expected_sha256: str) -> bytes:
    """Downloads and verifies integrity before returning. Raises
    ValueError on hash mismatch -- callers must treat that as
    "do not serve this file", not silently serve corrupted/tampered
    content."""
    bucket = _bucket()

    def _do_get():
        return _client().get_object(Bucket=bucket, Key=key)

    resp = _with_retry(_do_get, f"get_object({key})")
    data = resp["Body"].read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        logger.error(f"S3 integrity mismatch for {key}: expected {expected_sha256}, got {actual}")
        raise ValueError("Report integrity check failed -- refusing to serve this file")
    return data


def generate_presigned_url(key: str, expires_seconds: int = 300) -> str:
    """Short-lived, single-object presigned GET. Never cached, never
    logged in full (only the key + expiry, see app/api/reports.py's
    audit call)."""
    bucket = _bucket()
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_seconds,
    )


def delete_report(key: str) -> None:
    """Used for admin-triggered early deletion (e.g. GDPR/right-to-
    erasure requests) -- normal expiry is handled by the bucket
    lifecycle rule, not this function."""
    bucket = _bucket()
    _with_retry(lambda: _client().delete_object(Bucket=bucket, Key=key), f"delete_object({key})")
