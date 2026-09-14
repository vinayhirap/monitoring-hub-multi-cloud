# app/collector/cspm.py
"""
Lite CSPM (Cloud Security Posture Management), 2026-09-14. See
db/migrations/035_security_findings.sql's module docstring for scope
(a small, high-signal checklist, not a full CSPM product).

Runs in scheduler.py's "extended" tier (60-min cadence) -- security
configuration changes far less often than metrics, so this doesn't
need critical/standard-tier freshness, and IAM/S3 describe calls are
free but rate-limited like any AWS API, so a slower cadence is kinder
to accounts with many resources.

PERMISSIONS WARNING (read this before enabling in a new environment):
these checks call iam:ListUsers, iam:ListMFADevices, iam:GetLoginProfile,
iam:ListAccessKeys, s3:GetBucketPolicyStatus, s3:GetPublicAccessBlock,
ec2:DescribeSecurityGroups, and ec2:DescribeVolumes -- broader than the
CloudWatch/Describe-only permissions this app's metric collectors need.
If the assumed role/static keys for an account don't have these, that
CHECK (not the whole account, not the whole run) is skipped with a
logged warning -- see _run_check()'s try/except. Add these to your
monitoring IAM role/policy to get full coverage; everything else in
this app keeps working with zero changes if you don't.

Every check function returns a list of finding dicts:
    {"check_id": ..., "resource_id": ..., "severity": ..., "title": ..., "description": ...}
_upsert_findings() below reconciles that list against
security_findings for the account: new findings are inserted 'open',
findings no longer present are marked 'resolved', findings still
present get last_seen_at refreshed (and reopened if they'd been
manually/previously marked resolved but reappeared).
"""
import logging

from app.db import get_connection
from app.aws.sts import get_boto3_session
from app.aws.boto_config import STANDARD_RETRY

logger = logging.getLogger(__name__)

SENSITIVE_PORTS = {22, 3389, 3306, 5432, 1433, 6379, 9200, 27017}


def _get_active_accounts():
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, role_arn, external_id, auth_mode, default_region
            FROM aws_accounts WHERE status = 'active'
        """)
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def _check_public_s3_buckets(session) -> list:
    from botocore.exceptions import ClientError
    findings = []
    s3 = session.client("s3", config=STANDARD_RETRY)
    for bucket in s3.list_buckets().get("Buckets", []):
        name = bucket["Name"]
        try:
            status = s3.get_bucket_policy_status(Bucket=name)
            is_public = status["PolicyStatus"]["IsPublic"]
        except ClientError:
            is_public = False  # no bucket policy at all -- not public via policy
        try:
            pab = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
            fully_blocked = all(pab.values())
        except ClientError:
            fully_blocked = False  # no public access block configured at all

        if is_public or not fully_blocked:
            findings.append({
                "check_id": "s3_bucket_public",
                "resource_id": name,
                "severity": "HIGH" if is_public else "MEDIUM",
                "title": f"S3 bucket '{name}' may be publicly accessible",
                "description": (
                    f"Bucket policy reports IsPublic={is_public}; "
                    f"Public Access Block is {'fully enabled' if fully_blocked else 'NOT fully enabled'}. "
                    f"Review this bucket's policy/ACL and enable Block Public Access unless intentional."
                ),
            })
    return findings


def _check_open_security_groups(session, region: str) -> list:
    findings = []
    ec2 = session.client("ec2", region_name=region, config=STANDARD_RETRY)
    for sg in ec2.describe_security_groups().get("SecurityGroups", []):
        for perm in sg.get("IpPermissions", []):
            from_port = perm.get("FromPort")
            to_port = perm.get("ToPort")
            open_ranges = [r["CidrIp"] for r in perm.get("IpRanges", []) if r.get("CidrIp") == "0.0.0.0/0"]
            open_ranges += [r["CidrIpv6"] for r in perm.get("Ipv6Ranges", []) if r.get("CidrIpv6") == "::/0"]
            if not open_ranges:
                continue
            # No FromPort/ToPort at all means "all ports" (e.g. -1 protocol).
            port_span = set(range(from_port, to_port + 1)) if from_port is not None and to_port is not None else None
            hits_sensitive = port_span is None or bool(port_span & SENSITIVE_PORTS)
            findings.append({
                "check_id": "sg_open_to_world",
                "resource_id": sg["GroupId"],
                "severity": "HIGH" if hits_sensitive else "LOW",
                "title": f"Security group '{sg.get('GroupName', sg['GroupId'])}' allows traffic from the internet",
                "description": (
                    f"Ports {from_port}-{to_port} open to {', '.join(open_ranges)}"
                    + (" -- includes a sensitive port (SSH/RDP/DB)." if hits_sensitive else ".")
                ),
            })
    return findings


def _check_unencrypted_ebs(session, region: str) -> list:
    findings = []
    ec2 = session.client("ec2", region_name=region, config=STANDARD_RETRY)
    for vol in ec2.describe_volumes().get("Volumes", []):
        if not vol.get("Encrypted", True):
            findings.append({
                "check_id": "ebs_unencrypted",
                "resource_id": vol["VolumeId"],
                "severity": "MEDIUM",
                "title": f"EBS volume '{vol['VolumeId']}' is not encrypted",
                "description": "Data at rest on this volume is unencrypted. New volumes can default to "
                                "encrypted via the account's EBS encryption-by-default setting.",
            })
    return findings


def _check_iam_users_without_mfa(session) -> list:
    findings = []
    iam = session.client("iam", config=STANDARD_RETRY)
    for user in iam.list_users().get("Users", []):
        username = user["UserName"]
        try:
            iam.get_login_profile(UserName=username)  # raises if no console password set
            has_console_access = True
        except iam.exceptions.NoSuchEntityException:
            has_console_access = False
        if not has_console_access:
            continue  # API-only user -- MFA on console login doesn't apply

        mfa_devices = iam.list_mfa_devices(UserName=username).get("MFADevices", [])
        if not mfa_devices:
            findings.append({
                "check_id": "iam_user_no_mfa",
                "resource_id": username,
                "severity": "HIGH",
                "title": f"IAM user '{username}' has console access with no MFA device",
                "description": "This user can sign in with just a password -- enable MFA to protect "
                                "against credential-stuffing/phishing.",
            })
    return findings


def _check_stale_access_keys(session, max_age_days: int = 90) -> list:
    import datetime
    findings = []
    iam = session.client("iam", config=STANDARD_RETRY)
    now = datetime.datetime.now(datetime.timezone.utc)
    for user in iam.list_users().get("Users", []):
        username = user["UserName"]
        for key in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
            if key["Status"] != "Active":
                continue
            age_days = (now - key["CreateDate"]).days
            if age_days > max_age_days:
                findings.append({
                    "check_id": "iam_stale_access_key",
                    "resource_id": key["AccessKeyId"],
                    "severity": "LOW",
                    "title": f"IAM access key for '{username}' is {age_days} days old",
                    "description": f"Access keys older than {max_age_days} days should be rotated "
                                    f"as routine hygiene, regardless of whether compromise is suspected.",
                })
    return findings


def _run_check(name, fn, *args) -> list:
    try:
        return fn(*args)
    except Exception as e:
        # Most common cause: the assumed role/static keys lack the IAM
        # permission this check needs -- see module docstring's
        # PERMISSIONS WARNING. Skip just this check, not the account.
        logger.warning(f"[cspm] check '{name}' failed (likely a missing IAM permission on the "
                        f"monitoring role -- see cspm.py's module docstring): {e}")
        return []


def _upsert_findings(cursor, account_id: int, findings: list) -> None:
    seen_keys = set()
    for f in findings:
        seen_keys.add((f["check_id"], f["resource_id"]))
        cursor.execute("""
            INSERT INTO security_findings
                (aws_account_id, check_id, resource_id, severity, title, description, status, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'open', NOW())
            ON DUPLICATE KEY UPDATE
                severity = VALUES(severity), title = VALUES(title), description = VALUES(description),
                status = 'open', resolved_at = NULL, last_seen_at = NOW()
        """, (account_id, f["check_id"], f["resource_id"], f["severity"], f["title"], f["description"]))

    cursor.execute("SELECT id, check_id, resource_id FROM security_findings "
                    "WHERE aws_account_id = %s AND status = 'open'", (account_id,))
    for row in cursor.fetchall():
        if (row["check_id"], row["resource_id"]) not in seen_keys:
            cursor.execute("""
                UPDATE security_findings SET status = 'resolved', resolved_at = NOW()
                WHERE id = %s
            """, (row["id"],))


def run_security_checks() -> int:
    """Runs the full checklist against every active account, reconciles
    findings (open/resolved), returns total open findings across the
    fleet after this run. Non-fatal per account AND per check -- one
    account's assume-role failure, or one check's missing IAM
    permission, never blocks the rest."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    total_open = 0
    try:
        for account in _get_active_accounts():
            try:
                session = get_boto3_session(account)
                region = account["default_region"] or "us-east-1"

                findings = []
                findings += _run_check("s3_bucket_public", _check_public_s3_buckets, session)
                findings += _run_check("sg_open_to_world", _check_open_security_groups, session, region)
                findings += _run_check("ebs_unencrypted", _check_unencrypted_ebs, session, region)
                findings += _run_check("iam_user_no_mfa", _check_iam_users_without_mfa, session)
                findings += _run_check("iam_stale_access_key", _check_stale_access_keys, session)

                _upsert_findings(cursor, account["id"], findings)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(f"[cspm] security checks failed entirely for account id={account['id']} "
                                  f"(likely a session/assume-role failure) -- skipping this account this cycle")
                continue

        cursor.execute("SELECT COUNT(*) AS n FROM security_findings WHERE status = 'open'")
        total_open = cursor.fetchone()["n"]
        logger.info(f"[cspm] security checks complete -- {total_open} open finding(s) fleet-wide")
        return total_open
    finally:
        cursor.close()
        conn.close()
