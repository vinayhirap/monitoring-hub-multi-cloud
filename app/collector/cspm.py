# app/collector/cspm.py
"""
Lite CSPM (Cloud Security Posture Management), 2026-09-14, extended to
cover Azure and GCP 2026-09-17. See db/migrations/035_security_findings.sql's
module docstring for scope (a small, high-signal checklist, not a full
CSPM product).

Runs in scheduler.py's "extended" tier (60-min cadence) -- security
configuration changes far less often than metrics, so this doesn't
need critical/standard-tier freshness, and IAM/S3/ARM/GCP describe
calls are free but rate-limited like any cloud API, so a slower
cadence is kinder to accounts with many resources.

MULTI-CLOUD (2026-09-17): this module used to be AWS-only despite the
security_findings table/API/UI never mentioning AWS specifically --
Azure and GCP accounts silently got zero coverage and the frontend's
"checked hourly across every onboarded account" copy was simply false
for them. run_security_checks() now dispatches per account.provider,
reusing the exact credential-resolution helpers (app.credentials.
load_credential + ClientSecretCredential / service-account Credentials)
that app/providers/azure and app/providers/gcp's discovery.py already
use, so this doesn't introduce a second way of authenticating. The
AWS checklist is unchanged; Azure/GCP checklists are intentionally
smaller for now -- see PERMISSIONS WARNING below and each _run_*_checks
docstring for what's covered and what deliberately isn't yet (e.g. no
Azure AD / Cloud IAM user-MFA equivalent -- those need Graph/Admin API
scopes this app doesn't request today, flagged rather than faked).

PERMISSIONS WARNING (read this before enabling in a new environment):
  AWS:   iam:ListUsers, iam:ListMFADevices, iam:GetLoginProfile,
         iam:ListAccessKeys, s3:GetBucketPolicyStatus,
         s3:GetPublicAccessBlock, ec2:DescribeSecurityGroups,
         ec2:DescribeVolumes, ec2:DescribeRegions, s3:GetAccountPublicAccessBlock (optional;
         sts:GetCallerIdentity needs no grant) -- broader than this app's CloudWatch/
         Describe-only metrics permissions.
  Azure: Reader on the subscription is enough for all three checks
         (network_security_groups.list_all, storage_accounts.list) --
         no extra role assignment needed beyond what discovery already
         requires.
  GCP:   the same "roles/viewer" (or narrower compute.firewalls.list +
         storage.buckets.{list,getIamPolicy}) already granted for
         discovery/metrics covers both checks.
If credentials for an account don't have these, that CHECK (not the
whole account, not the whole run) is skipped with a logged warning --
see _CheckRun.run()'s try/except. Add these to get full coverage;
everything else in this app keeps working with zero changes if you don't.

Every check function returns a list of finding dicts:
    {"check_id": ..., "resource_id": ..., "severity": ..., "title": ..., "description": ...}
`resource_id` is provider-specific and doubles as the deep-link key
for the console-url endpoint (app/api/security.py): AWS check_ids use
the resources.resource_type-style service keys already understood by
app/aws/federation.py; Azure check_ids store the full ARM resource ID
(AzureProvider.get_console_url builds its link directly from that, no
service dispatch needed); GCP check_ids store the bare resource name,
matched against a `gcp_*` -> service-key table in app/api/security.py.

_upsert_findings() below reconciles that list against
security_findings for the account: new findings are inserted 'open',
findings no longer present are marked 'resolved', findings still
present get last_seen_at refreshed (and reopened if they'd been
manually/previously marked resolved but reappeared). This part is
already provider-agnostic (keyed only on aws_account_id/check_id/
resource_id), so it needed no changes for multi-cloud.
"""
import logging
import os
import re

from app.db import get_connection
from app.aws.sts import get_boto3_session
from app.aws.boto_config import STANDARD_RETRY

logger = logging.getLogger(__name__)

SENSITIVE_PORTS = {22, 3389, 3306, 5432, 1433, 6379, 9200, 27017}


def _get_active_accounts():
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, provider, role_arn, external_id, auth_mode, default_region,
                   tenant_id, client_id, subscription_id,
                   project_id, service_account_email
            FROM aws_accounts WHERE status = 'active'
        """)
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def _account_public_access_fully_blocked(session) -> bool:
    """Account-level S3 Block Public Access overrides every bucket's
    own setting. Ignoring it produced a MEDIUM false positive for every
    bucket in accounts that (correctly) block public access account-wide.
    Missing permission / no config -> False (bucket-level check decides)."""
    from botocore.exceptions import ClientError
    try:
        account_number = session.client("sts", config=STANDARD_RETRY).get_caller_identity()["Account"]
        cfg = session.client("s3control", config=STANDARD_RETRY).get_public_access_block(
            AccountId=account_number
        )["PublicAccessBlockConfiguration"]
        return bool(cfg) and all(cfg.values())
    except ClientError:
        return False


def _check_public_s3_buckets(session) -> list:
    from botocore.exceptions import ClientError
    findings = []
    s3 = session.client("s3", config=STANDARD_RETRY)
    account_blocked = _account_public_access_fully_blocked(session)
    for bucket in s3.list_buckets().get("Buckets", []):
        name = bucket["Name"]
        try:
            status = s3.get_bucket_policy_status(Bucket=name)
            is_public = status["PolicyStatus"]["IsPublic"]
        except ClientError:
            is_public = False  # no bucket policy at all -- not public via policy
        if account_blocked:
            fully_blocked = True
        else:
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
    for sg in (sg for page in ec2.get_paginator("describe_security_groups").paginate()
               for sg in page.get("SecurityGroups", [])):
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
                "region": region,
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
    for vol in (v for page in ec2.get_paginator("describe_volumes").paginate()
                for v in page.get("Volumes", [])):
        if not vol.get("Encrypted", True):
            findings.append({
                "check_id": "ebs_unencrypted",
                "resource_id": vol["VolumeId"],
                "region": region,
                "severity": "MEDIUM",
                "title": f"EBS volume '{vol['VolumeId']}' is not encrypted",
                "description": "Data at rest on this volume is unencrypted. New volumes can default to "
                                "encrypted via the account's EBS encryption-by-default setting.",
            })
    return findings


# F17: security groups / EBS volumes are regional. Scanning only
# default_region missed every other region the account actually uses.
# Regions = the account's enabled regions (ec2:DescribeRegions), falling
# back to default_region + regions this app has discovered resources in.
# EC2 Describe* calls are not billed (unlike GetMetricData); hourly
# cadence. CSPM_AWS_REGIONS (comma-separated) restricts the set.
MAX_AWS_REGIONS = 30
_REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d+$")


def _discovered_regions(account_id: int) -> set:
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT DISTINCT region FROM resources WHERE aws_account_id = %s AND region IS NOT NULL",
                    (account_id,))
        return {r["region"] for r in cur.fetchall() if r.get("region")}
    finally:
        cur.close(); conn.close()


def _aws_regions(session, account: dict) -> list:
    default = account.get("default_region") or "us-east-1"
    regions = set()
    try:
        ec2 = session.client("ec2", region_name=default, config=STANDARD_RETRY)
        regions = {r["RegionName"] for r in ec2.describe_regions(AllRegions=False).get("Regions", [])}
    except Exception as e:
        logger.warning(f"[cspm] describe_regions failed for account id={account['id']} ({e}) -- "
                        f"falling back to default + discovered regions")
        try:
            regions = _discovered_regions(account["id"])
        except Exception:
            regions = set()
    regions.add(default)
    allow = {r.strip() for r in (os.getenv("CSPM_AWS_REGIONS") or "").split(",") if r.strip()}
    if allow:
        regions = {r for r in regions if r in allow} | ({default} if default in allow else set())
    regions = sorted(r for r in regions if _REGION_RE.match(r))
    if default in regions:
        regions.remove(default)
        regions.insert(0, default)
    return regions[:MAX_AWS_REGIONS]


def _iam_users(iam):
    for page in iam.get_paginator("list_users").paginate():
        for user in page.get("Users", []):
            yield user


def _check_iam_users_without_mfa(session) -> list:
    findings = []
    iam = session.client("iam", config=STANDARD_RETRY)
    # Paginated: ListUsers returns at most 100 per call, so users past
    # the first page were never checked AND their existing findings
    # were auto-resolved by _upsert_findings.
    for user in _iam_users(iam):
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
    for user in _iam_users(iam):
        username = user["UserName"]
        for key in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
            if key["Status"] != "Active":
                continue
            age_days = (now - key["CreateDate"]).days
            if age_days > max_age_days:
                findings.append({
                    "check_id": "iam_stale_access_key",
                    # "username:key-id", not just the key id -- the
                    # username is needed to build a console deep link
                    # to that user's Security Credentials tab (see
                    # app/api/security.py's console-url endpoint) and
                    # the key id alone doesn't carry it. Still unique
                    # per (user, key) for the upsert's dedupe key.
                    "resource_id": f"{username}:{key['AccessKeyId']}",
                    "severity": "LOW",
                    "title": f"IAM access key for '{username}' is {age_days} days old",
                    "description": f"Access keys older than {max_age_days} days should be rotated "
                                    f"as routine hygiene, regardless of whether compromise is suspected.",
                })
    return findings


# ── AZURE ────────────────────────────────────────────────────────────
_AZURE_SENSITIVE_PORTS = {str(p) for p in SENSITIVE_PORTS}


def _azure_credential(account: dict):
    from azure.identity import ClientSecretCredential
    from app.credentials import load_credential

    secret = account.get("client_secret") or load_credential(account["id"])
    if not secret:
        raise ValueError("no client_secret/credential_ref configured for this Azure account")
    return ClientSecretCredential(
        tenant_id=account["tenant_id"], client_id=account["client_id"], client_secret=secret,
    )


def _azure_port_range_hits_sensitive(port_range: str) -> bool:
    """destination_port_range(s) can be '*', a single port ('22'), or a
    range ('1000-2000') -- same idea as AWS's SENSITIVE_PORTS overlap
    check above, adapted to Azure's range-as-string representation.
    Unparseable input fails loud (treated as sensitive) rather than
    silently passing a malformed rule through as safe."""
    if not port_range or port_range == "*":
        return True
    if "-" in port_range:
        try:
            lo, hi = (int(x) for x in port_range.split("-", 1))
        except ValueError:
            return True
        return any(lo <= int(p) <= hi for p in _AZURE_SENSITIVE_PORTS)
    return port_range in _AZURE_SENSITIVE_PORTS


def _check_azure_nsg_open_to_world(cred, subscription_id: str) -> list:
    from azure.mgmt.network import NetworkManagementClient
    findings = []
    net = NetworkManagementClient(cred, subscription_id)
    for nsg in net.network_security_groups.list_all():
        for rule in (nsg.security_rules or []):
            if rule.direction != "Inbound" or rule.access != "Allow":
                continue
            sources = list(rule.source_address_prefixes or [])
            if rule.source_address_prefix:
                sources.append(rule.source_address_prefix)
            open_sources = [s for s in sources if s in ("*", "0.0.0.0/0", "::/0", "Internet", "Any")]
            if not open_sources:
                continue
            port_ranges = list(rule.destination_port_ranges or [])
            if rule.destination_port_range:
                port_ranges.append(rule.destination_port_range)
            port_ranges = port_ranges or ["*"]
            hits_sensitive = any(_azure_port_range_hits_sensitive(p) for p in port_ranges)
            findings.append({
                "check_id": "azure_nsg_open_to_world",
                "resource_id": nsg.id,  # full ARM id -- see module docstring on console links
                "severity": "HIGH" if hits_sensitive else "LOW",
                "title": f"NSG '{nsg.name}' allows inbound traffic from the internet",
                "description": (
                    f"Rule '{rule.name}' allows {rule.protocol} {', '.join(port_ranges)} "
                    f"from {', '.join(open_sources)}"
                    + (" -- includes a sensitive port (SSH/RDP/DB)." if hits_sensitive else ".")
                ),
            })
    return findings


def _check_azure_storage_public_access(cred, subscription_id: str) -> list:
    from azure.mgmt.storage import StorageManagementClient
    findings = []
    storage = StorageManagementClient(cred, subscription_id)
    for acct in storage.storage_accounts.list():
        if acct.allow_blob_public_access:
            findings.append({
                "check_id": "azure_storage_public_access",
                "resource_id": acct.id,
                "severity": "HIGH",
                "title": f"Storage account '{acct.name}' allows public blob access",
                "description": "allowBlobPublicAccess is enabled at the account level -- any "
                                "container/blob with a public access level set can be reached "
                                "anonymously. Disable unless intentional.",
            })
    return findings


def _check_azure_storage_insecure_transport(cred, subscription_id: str) -> list:
    from azure.mgmt.storage import StorageManagementClient
    findings = []
    storage = StorageManagementClient(cred, subscription_id)
    for acct in storage.storage_accounts.list():
        if not acct.enable_https_traffic_only:
            findings.append({
                "check_id": "azure_storage_insecure_transport",
                "resource_id": acct.id,
                "severity": "MEDIUM",
                "title": f"Storage account '{acct.name}' allows unencrypted (HTTP) access",
                "description": "Secure transfer (HTTPS-only) is disabled -- data in transit to "
                                "this storage account can be sent unencrypted over plain HTTP.",
            })
    return findings


def _run_azure_checks(account: dict):
    """Reader-level checks only (see module PERMISSIONS WARNING). No
    Azure AD/Entra user-MFA or access-key-age equivalent yet -- that
    needs Microsoft Graph scopes this app doesn't request; flagged
    here rather than silently skipped so it isn't mistaken for 'checked,
    found nothing'."""
    cred = _azure_credential(account)
    subscription_id = account["subscription_id"]
    run = _CheckRun()
    run.run("azure_nsg_open_to_world", _check_azure_nsg_open_to_world, cred, subscription_id)
    run.run("azure_storage_public_access", _check_azure_storage_public_access, cred, subscription_id)
    run.run("azure_storage_insecure_transport", _check_azure_storage_insecure_transport, cred, subscription_id)
    return run


# ── GCP ──────────────────────────────────────────────────────────────
_GCP_SENSITIVE_PORTS = set(SENSITIVE_PORTS)


def _gcp_credentials(account: dict):
    import json
    from google.oauth2 import service_account as gcp_service_account
    from app.credentials import load_credential

    sa_key_json = account.get("service_account_key") or load_credential(account["id"])
    if not sa_key_json:
        raise ValueError("no service_account_key/credential_ref configured for this GCP account")
    info = json.loads(sa_key_json)
    return gcp_service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"]
    )


def _gcp_ports_hit_sensitive(allowed: list) -> bool:
    """`allowed` is compute_v1.Firewall.allowed -- a list of
    {ipProtocol, ports: [...]}. An empty/missing `ports` list means
    ALL ports for that protocol (GCP's own semantics), so that's
    treated as sensitive rather than skipped."""
    for entry in allowed:
        ports = entry.get("ports") or []
        if not ports:
            return True
        for p in ports:
            if "-" in p:
                lo, hi = (int(x) for x in p.split("-", 1))
                if any(lo <= sp <= hi for sp in _GCP_SENSITIVE_PORTS):
                    return True
            elif int(p) in _GCP_SENSITIVE_PORTS:
                return True
    return False


def _check_gcp_firewall_open_to_world(creds, project_id: str) -> list:
    from google.cloud import compute_v1
    findings = []
    client = compute_v1.FirewallsClient(credentials=creds)
    for rule in client.list(project=project_id):
        if rule.direction != "INGRESS" or rule.disabled:
            continue
        world_ranges = [r for r in (rule.source_ranges or []) if r in ("0.0.0.0/0", "::/0")]
        if not world_ranges:
            continue
        allowed = [{"ipProtocol": a.I_p_protocol, "ports": list(a.ports)} for a in (rule.allowed or [])]
        if not allowed:
            continue  # a deny rule, or an allow rule with nothing in `allowed` -- nothing open
        hits_sensitive = _gcp_ports_hit_sensitive(allowed)
        findings.append({
            "check_id": "gcp_firewall_open_to_world",
            "resource_id": rule.name,
            "severity": "HIGH" if hits_sensitive else "LOW",
            "title": f"Firewall rule '{rule.name}' allows ingress from the internet",
            "description": (
                f"Allows {', '.join(a['ipProtocol'] for a in allowed)} from {', '.join(world_ranges)}"
                + (" -- includes a sensitive port (SSH/RDP/DB)." if hits_sensitive else ".")
            ),
        })
    return findings


def _check_gcp_gcs_bucket_public(creds, project_id: str) -> list:
    from google.cloud import storage as gcs
    findings = []
    client = gcs.Client(project=project_id, credentials=creds)
    for bucket in client.list_buckets():
        policy = bucket.get_iam_policy(requested_policy_version=3)
        public_principals = set()
        for binding in policy.bindings:
            for member in binding.get("members", []):
                if member in ("allUsers", "allAuthenticatedUsers"):
                    public_principals.add(member)
        if public_principals:
            findings.append({
                "check_id": "gcp_gcs_bucket_public",
                "resource_id": bucket.name,
                "severity": "HIGH",
                "title": f"GCS bucket '{bucket.name}' is publicly accessible",
                "description": f"Bucket IAM policy grants access to {', '.join(sorted(public_principals))} "
                                f"-- review and remove unless intentional.",
            })
    return findings


def _run_gcp_checks(account: dict):
    """Viewer-level checks only (see module PERMISSIONS WARNING). No
    Cloud IAM user-MFA or service-account-key-age equivalent yet --
    the former needs Cloud Identity Admin API scope, the latter needs
    iam.serviceAccountKeys.list across every SA, neither requested by
    this app today; flagged here rather than silently skipped."""
    creds = _gcp_credentials(account)
    project_id = account["project_id"]
    run = _CheckRun()
    run.run("gcp_firewall_open_to_world", _check_gcp_firewall_open_to_world, creds, project_id)
    run.run("gcp_gcs_bucket_public", _check_gcp_gcs_bucket_public, creds, project_id)
    return run


class _CheckRun:
    """Collects findings for one account's checklist and remembers which
    checks FAILED to run. A failed check must not auto-resolve its
    previously-open findings: before this, a transient throttle or a
    missing permission returned [] and _upsert_findings marked every
    open finding of that check 'resolved' (then reopened it next hour)."""

    def __init__(self):
        self.findings = []
        self.failed_checks = set()

    def run(self, name, fn, *args, region=None) -> None:
        """region=None: the whole check failed. With a region, only that
        region's findings of this check are protected from auto-resolve
        (e.g. an SCP denying one region must not freeze every region)."""
        try:
            self.findings += fn(*args)
        except Exception as e:
            self.failed_checks.add((name, region) if region else name)
            _log_check_failure(f"{name}@{region}" if region else name, e)


def _log_check_failure(name, e) -> None:
    # Most common cause: the assumed role/static keys lack the IAM
    # permission this check needs -- see module docstring's
    # PERMISSIONS WARNING. Skip just this check, not the account.
    logger.warning(f"[cspm] check '{name}' failed (likely a missing IAM permission on the "
                    f"monitoring role -- see cspm.py's module docstring): {e}")


_SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _merge_findings(findings: list) -> list:
    """Collapses findings sharing (check_id, resource_id) -- one security
    group / NSG / firewall can yield several rules. Previously each was
    upserted in turn, so the LAST rule's severity won and a HIGH (SSH
    open to the world) could be stored as LOW. Keeps the highest
    severity and joins the distinct descriptions."""
    merged = {}
    for f in findings:
        key = (f["check_id"], f["resource_id"])
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(f)
            continue
        if _SEVERITY_RANK.get(f["severity"], 0) > _SEVERITY_RANK.get(cur["severity"], 0):
            cur["severity"] = f["severity"]
            cur["title"] = f["title"]
        if f.get("description") and f["description"] not in (cur.get("description") or ""):
            cur["description"] = f"{cur.get('description') or ''}\n{f['description']}".strip()
    return list(merged.values())


def _resolve_blocked(check_id: str, region, failed_checks) -> bool:
    """True if this open finding must NOT be auto-resolved because the
    check (or its region) failed to run this cycle. failed_checks holds
    check names (whole check failed) and/or (check, region) tuples."""
    if check_id in failed_checks:
        return True
    region_failures = {r for f in failed_checks if isinstance(f, tuple) and f[0] == check_id for r in [f[1]]}
    if not region_failures:
        return False
    # A legacy row with no region recorded can't be attributed -- keep it.
    return region is None or region in region_failures


def _upsert_findings(cursor, account_id: int, findings: list, failed_checks=frozenset()) -> None:
    findings = _merge_findings(findings)
    seen_keys = set()
    for f in findings:
        seen_keys.add((f["check_id"], f["resource_id"]))
        cursor.execute("""
            INSERT INTO security_findings
                (aws_account_id, check_id, resource_id, region, severity, title, description, status, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', NOW())
            ON DUPLICATE KEY UPDATE
                region = VALUES(region),
                severity = VALUES(severity), title = VALUES(title), description = VALUES(description),
                status = 'open', resolved_at = NULL, last_seen_at = NOW()
        """, (account_id, f["check_id"], f["resource_id"], f.get("region"),
              f["severity"], f["title"], f["description"]))

    cursor.execute("SELECT id, check_id, resource_id, region FROM security_findings "
                    "WHERE aws_account_id = %s AND status = 'open'", (account_id,))
    for row in cursor.fetchall():
        if _resolve_blocked(row["check_id"], row.get("region"), failed_checks):
            continue  # check/region didn't run this cycle -- keep its findings as-is
        if (row["check_id"], row["resource_id"]) not in seen_keys:
            cursor.execute("""
                UPDATE security_findings SET status = 'resolved', resolved_at = NOW()
                WHERE id = %s
            """, (row["id"],))


def _run_aws_checks(account: dict):
    session = get_boto3_session(account)
    run = _CheckRun()
    run.run("s3_bucket_public", _check_public_s3_buckets, session)
    for region in _aws_regions(session, account):
        run.run("sg_open_to_world", _check_open_security_groups, session, region, region=region)
        run.run("ebs_unencrypted", _check_unencrypted_ebs, session, region, region=region)
    run.run("iam_user_no_mfa", _check_iam_users_without_mfa, session)
    run.run("iam_stale_access_key", _check_stale_access_keys, session)
    return run


_CHECKS_BY_PROVIDER = {
    "aws":   _run_aws_checks,
    "azure": _run_azure_checks,
    "gcp":   _run_gcp_checks,
}


def run_security_checks() -> int:
    """Runs the full checklist against every active account across
    every provider (see module docstring's 2026-09-17 multi-cloud
    note), reconciles findings (open/resolved), returns total open
    findings across the fleet after this run. Non-fatal per account
    AND per check -- one account's credential failure, or one check's
    missing permission, never blocks the rest."""
    # One short-lived DB connection per account, opened only AFTER the
    # (slow) cloud API calls: previously a single pooled connection was
    # held for the whole multi-account run (minutes), starving the
    # 10-connection pool.
    for account in _get_active_accounts():
        provider = account.get("provider") or "aws"
        run_checks = _CHECKS_BY_PROVIDER.get(provider)
        if run_checks is None:
            logger.warning(f"[cspm] no security checklist implemented for provider "
                            f"'{provider}' (account id={account['id']}) -- skipping")
            continue
        try:
            run = run_checks(account)
        except Exception:
            logger.exception(f"[cspm] security checks failed entirely for account id={account['id']} "
                              f"(provider={provider}, likely a credential/session failure) -- "
                              f"skipping this account this cycle")
            continue
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            _upsert_findings(cursor, account["id"], run.findings, run.failed_checks)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception(f"[cspm] failed to store findings for account id={account['id']} -- "
                              f"skipping this account this cycle")
        finally:
            cursor.close()
            conn.close()

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT COUNT(*) AS n FROM security_findings f
            JOIN aws_accounts acc ON acc.id = f.aws_account_id AND acc.status = 'active'
            WHERE f.status = 'open'
        """)
        total_open = cursor.fetchone()["n"]
        logger.info(f"[cspm] security checks complete -- {total_open} open finding(s) fleet-wide")
        return total_open
    finally:
        cursor.close()
        conn.close()
