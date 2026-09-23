# app/aws/federation.py
"""
Builds account-specific, resource-specific AWS Console deep links --
WITHOUT ever assuming this app's own IAM role/credentials on the
visiting person's behalf. See build_federated_console_url()'s own
docstring below for the full explanation of what this module does and
does not do; the short version: every link built here still requires
the person to sign in with their OWN IAM user and whatever permissions
THEY personally have -- this app never mints, embeds, or hands over any
credential that could authenticate anyone.

Why this links straight to the resource, not a wrapped sign-in URL
--------------------------------------------------------------------------
An earlier version of this module wrapped the destination in
https://{account}.signin.aws.amazon.com/console?redirect_uri=..., on
the theory that AWS's account-locked sign-in URL would carry the
person through to the specific resource page after they signed in.
Reported broken by direct testing (2026-09-19): AWS's own
documentation for that URL
(docs.aws.amazon.com/IAM/latest/UserGuide/id_users_sign-in.html) only
documents a `region` parameter -- `redirect_uri` isn't a real,
supported mechanism there. Genuine post-login deep-linking is only
supported by AWS via SAML `RelayState` (needs a SAML/SSO identity
provider this app doesn't set up) or STS federation's `Destination`
parameter (needs minting real temporary credentials -- the exact
impersonation this app was told never to do). With neither available,
linking straight to the destination is the tradeoff taken -- see
build_federated_console_url()'s docstring for the full explanation,
including the one known tradeoff (a browser already signed into a
DIFFERENT AWS account will open that wrong account instead of getting
a fresh sign-in prompt).

NOTE (2026-09-18 audit): this module previously (pre-2026-09-12) minted
real temporary credentials via STS + the AWS federation endpoint,
scoped by a session policy (build_scoped_session_policy() and its
helpers below) -- effectively auto-signing the visiting person in AS
this app's own monitoring role. That approach was removed first in
favor of an account-locked sign-in URL (itself replaced 2026-09-19,
see above, once THAT was found not to actually deep-link either).
_service_read_actions/_service_resource_arns/build_scoped_session_policy
are kept only because a future, genuinely different feature (e.g. a
"read-only session for support staff without their own IAM user"
tool, which would need its own explicit design and consent flow)
might reuse the scoping logic -- they are NOT called by anything in
this app today. Verified via repo-wide grep before writing this note.
"""
import datetime
import json
import logging
import re
import urllib.parse

logger = logging.getLogger(__name__)

# Real AWS region names only ever look like "us-east-1", "ap-south-2",
# "eu-central-1", etc. -- lowercase letters/digits and single hyphens,
# 2-3 letter-groups then a digit.
_VALID_REGION_RE = re.compile(r"^[a-z]{2,3}(-[a-z]+){1,2}-\d$")
_DEFAULT_REGION = "us-east-1"


def _safe_region(region: str | None) -> str:
    """
    SECURITY: every URL builder below places `region` directly into
    the URL's HOST/subdomain position (f"https://{region}.console.
    aws.amazon.com"), and region is caller-supplied on some paths
    (app/api/admin/accounts.py's GET .../console-url takes it as a
    plain, unrestricted query param, reachable by anyone with the
    viewer-level accounts.view permission). Without validation, a
    value like "evil.com/" splits the string so the resulting URL's
    actual host becomes evil.com instead of AWS -- a classic open-
    redirect / URL-spoofing primitive (CWE-601), since the app hands
    back what looks like an AWS link but isn't one. Every caller in
    this module must route region through this before it ever touches
    an f-string; falls back to a safe default rather than erroring,
    since a bad/spoofed region should degrade to "wrong region,
    console still opens" rather than break the whole console-link
    feature.
    """
    if region and _VALID_REGION_RE.match(region):
        return region
    return _DEFAULT_REGION


class NoConsoleCredentialsError(ValueError):
    """
    Raised when we have no way to obtain console credentials for the
    target account: no role_arn is configured AND the target account is
    not the server's own account. Callers should surface this as a 400
    (config problem), distinct from other exceptions in this module
    which mean the credential path was found but the AWS call itself
    failed (500/502).
    """
    pass


def service_console_list_url(service: str, region: str) -> str:
    """
    List-view console URL for a whole service (e.g. all EC2 instances) —
    used when no specific resource is selected yet, and as the fallback
    destination for any resource-type this app tracks that doesn't (yet)
    have a resource-level deep link in resource_console_destination()
    below.

    Two honesty tiers, deliberately not blurred together (see this
    project's "do not fake support" principle,
    app/providers/base.py's module docstring):
      - The first several entries (ec2/ebs/rds/lambda/s3/elb/ecs/
        security_group/iam_user) are the original, long-established
        entries -- battle-tested in this codebase.
      - Everything from apigateway onward (added 2026-09-18, covering
        every resources.resource_type this app's extended discovery
        can produce -- see app/collector/discovery/extended.py) is each
        service's standard console entry point, to the best of
        available knowledge, but NOT individually search-verified the
        way EC2/DynamoDB/CloudWatch Logs were. A slightly-off hash
        fragment on one of these degrades gracefully -- the console
        still opens in the CORRECT account and region, on the correct
        top-level service, just possibly its default tab rather than
        the exact one -- which is why this is safe to ship even at
        lower confidence than a resource-level deep link would need
        (a wrong resource ID fails hard with "not found"; a slightly
        wrong list-page hash does not).
    """
    region = _safe_region(region)
    service = (service or "").lower()
    base = f"https://{region}.console.aws.amazon.com"
    return {
        "ec2":    f"{base}/ec2/home?region={region}#Instances:",
        "ebs":    f"{base}/ec2/home?region={region}#Volumes:",
        "rds":    f"{base}/rds/home?region={region}#databases:",
        "lambda": f"{base}/lambda/home?region={region}#/functions",
        "s3":     "https://s3.console.aws.amazon.com/s3/buckets",
        "elb":    f"{base}/ec2/home?region={region}#LoadBalancers:",
        "ecs":    f"{base}/ecs/home?region={region}",
        "security_group": f"{base}/ec2/home?region={region}#SecurityGroups:",
        # IAM is a global (non-regional) service -- console.aws.amazon.com
        # itself handles the redirect from any region subdomain, same as
        # every other global-service link this app already builds this way.
        "iam_user": f"{base}/iam/home#/users",

        # -- Extended-tier resource types (2026-09-18) --------------------
        "dynamodb":          f"{base}/dynamodbv2/home?region={region}#tables",
        "sqs":               f"{base}/sqs/v2/home?region={region}#/queues",
        "sns":               f"{base}/sns/v3/home?region={region}#/topics",
        "kinesis":           f"{base}/kinesis/home?region={region}#/streams/list",
        "firehose":          f"{base}/firehose/home?region={region}#/",
        "autoscaling":       f"{base}/ec2autoscaling/home?region={region}",
        "natgateway":        f"{base}/vpc/home?region={region}#NatGateways:",
        "efs":               f"{base}/efs/home?region={region}#/file-systems",
        "elasticache":       f"{base}/elasticache/home?region={region}",
        "redshift":          f"{base}/redshiftv2/home?region={region}#clusters:",
        "memorydb":          f"{base}/memorydb/home?region={region}#/clusters",
        "dax":               f"{base}/dax/home?region={region}",
        "states":            f"{base}/states/home?region={region}#/statemachines",
        "events":            f"{base}/events/home?region={region}#/rules",
        "kms":               f"{base}/kms/home?region={region}#/kms/keys",
        "certificatemanager": f"{base}/acm/home?region={region}#/certificates/list",
        "backup":            f"{base}/backup/home?region={region}",
        "cognito":           f"{base}/cognito/v2/home?region={region}#/user-pools",
        "logs":              f"{base}/cloudwatch/home?region={region}#logsV2:log-groups",
        "dms":               f"{base}/dms/v2/home?region={region}#/replicationInstances",
        "directconnect":     f"{base}/directconnect/v2/home?region={region}#/connections",
        "eks":               f"{base}/eks/home?region={region}#/clusters",
        "documentdb":        f"{base}/docdb/home?region={region}#clusters:",
        "neptune":           f"{base}/neptune/home?region={region}#databases:",
        "apigateway":        f"{base}/apigateway/main/apis?region={region}",
        # Route 53 is a global service, like IAM above.
        "route53":           "https://console.aws.amazon.com/route53/healthchecks/home#/",
        # CloudFront is global -- distributions aren't scoped to any region.
        "cloudfront":        "https://console.aws.amazon.com/cloudfront/v4/home#/distributions",
        "opensearch":        f"{base}/aos/home?region={region}#opensearch/domains",
        "wafv2":             f"{base}/wafv2/homev2/web-acls?region={region}",
        "msk":               f"{base}/msk/home?region={region}#/clusters",
        "transitgateway":    f"{base}/vpc/home?region={region}#TransitGateways:",
        "vpn":               f"{base}/vpc/home?region={region}#VpnConnections:",
        # Global Accelerator's control plane only lives in us-west-2,
        # regardless of which region the accelerator's endpoints are in
        # -- unlike every other entry here, this deliberately ignores
        # the passed-in `region`.
        "globalaccelerator": "https://us-west-2.console.aws.amazon.com/ga/home?region=us-west-2#/accelerators",
    }.get(service, f"{base}/console/home?region={region}")


def resource_console_destination(service: str, resource_id: str, region: str,
                                  resource_name: str | None = None,
                                  ecs_service_name: str | None = None) -> str:
    """
    Resource-type-specific AWS Console deep link.

    `service` should be one of the resources.resource_type values
    (case-insensitive) -- see app/collector/discovery/runner.py and
    app/collector/discovery/extended.py for the full set this app can
    produce. This is the single source of truth for console-link
    construction — the same mapping frontend/src/pages/ServiceDetail.jsx
    used to keep as its own separate copy (see
    multi-cloud-architecture-assessment.md section 2.3); that copy is
    being retired in favor of calling through here.

    `resource_name` is used where the console needs a display name
    rather than an ARN/ID (e.g. ELB search-by-name). `ecs_service_name`
    enables the deeper cluster > service link for ECS when known;
    without it, ECS falls back to the cluster-level view.

    Every branch below is an individually confirmed, resource-SPECIFIC
    deep link (not just a service list page) -- see each branch's
    comment for how it was confirmed. Any resource_type not handled
    here falls through to service_console_list_url(), which still
    opens the correct account/region/service, just not narrowed to
    this one resource -- seeing this module's docstring for why that's
    an honest tradeoff rather than a gap being papered over.

    If `service` is missing/unrecognized (an older caller that hasn't
    been updated yet), falls back to the original ID-prefix-guessing
    behavior so nothing regresses for callers not yet passing `service`.
    """
    region = _safe_region(region)
    if not resource_id:
        return service_console_list_url(service, region)

    svc = (service or "").lower()
    base = f"https://{region}.console.aws.amazon.com"

    # Fix: 2026-09 B04 audit -- LOW/defense-in-depth. resource_id/
    # resource_name/ecs_service_name are caller-supplied (this endpoint's
    # `resource_id`/`resource_name`/`ecs_service_name` query params --
    # app/api/admin/accounts.py's get_account_console_url) and, unlike the
    # dynamodb/logs branches below (which already url-encode, see their
    # own comments), were being placed directly into the URL unescaped.
    # The fixed https://{validated-region}.console.aws.amazon.com prefix
    # (via _safe_region above) means this was never a host-takeover/open-
    # redirect vector on its own, but an unescaped value containing '#',
    # '&', '?' etc. could still corrupt the resulting deep link (wrong
    # fragment/query parsed by the console) -- encode consistently instead
    # of only where a bug was previously noticed.
    resource_id_enc = urllib.parse.quote(resource_id, safe="")
    resource_name_enc = urllib.parse.quote(resource_name, safe="") if resource_name else None
    ecs_service_name_enc = urllib.parse.quote(ecs_service_name, safe="") if ecs_service_name else None

    if svc == "ec2":
        return f"{base}/ec2/home?region={region}#Instances:instanceId={resource_id_enc}"
    if svc == "ebs":
        return f"{base}/ec2/home?region={region}#Volumes:volumeId={resource_id_enc}"
    if svc == "rds":
        return f"{base}/rds/home?region={region}#database:id={resource_id_enc}"
    if svc == "lambda":
        return f"{base}/lambda/home?region={region}#/functions/{resource_id_enc}"
    if svc == "s3":
        return f"https://s3.console.aws.amazon.com/s3/buckets/{resource_id_enc}"
    if svc == "elb":
        search_term = resource_name_enc or resource_id_enc
        return f"{base}/ec2/home?region={region}#LoadBalancers:search={search_term}"
    if svc == "ecs":
        cluster = resource_name_enc or resource_id_enc
        if ecs_service_name_enc:
            return (f"{base}/ecs/home?region={region}"
                    f"#/clusters/{cluster}/services/{ecs_service_name_enc}")
        return f"{base}/ecs/home?region={region}#/clusters/{cluster}"
    if svc == "security_group":
        return f"{base}/ec2/home?region={region}#SecurityGroups:groupId={resource_id_enc}"
    if svc == "iam_user":
        # `resource_name` carries the username -- for the stale-access-key
        # check `resource_id` is "username:key-id" (see cspm.py), so the
        # username alone (not the raw resource_id) is what belongs in the
        # path here. Falls back to resource_id itself when it's already a
        # bare username (the no-MFA check's case).
        username = resource_name_enc or resource_id_enc
        return f"{base}/iam/home#/users/details/{username}?section=security_credentials"

    # -- Extended-tier resource types (2026-09-18) ------------------------
    # `resource_id` here is exactly what
    # app/collector/discovery/extended.py's discovery functions store --
    # a plain table/queue/topic/cluster NAME for most services (the
    # console's own search-by-name works fine for those), or a full ARN
    # for the handful of services extended.py stores an ARN for
    # (states, certificatemanager, globalaccelerator) -- see that file's
    # `_discover_*` functions for exactly which.
    if svc == "dynamodb":
        # Confirmed via pynamodb_mate (a published, actively maintained
        # DynamoDB console-URL-generation library) -- table_name is
        # url-encoded since it can be used as-is in this query position.
        table_name = urllib.parse.quote(resource_id, safe="")
        return (f"{base}/dynamodbv2/home?region={region}"
                f"#table?initialTagKey=&name={table_name}&tab=overview")
    if svc == "logs":
        # Confirmed via 3 independent sources agreeing on this exact
        # pattern: AWS's own CodeBuild API response documentation (a
        # sample `deepLink` field), plus two actively maintained
        # open-source CloudWatch-Logs-URL-builder utilities. Log group
        # names always start with "/" and often contain more slashes
        # (e.g. "/aws/lambda/my-fn"), so this MUST be url-encoded or the
        # console misreads the path.
        log_group = urllib.parse.quote(resource_id, safe="")
        return f"{base}/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{log_group}"

    # Any resource_type this app tracks but doesn't have a resource-level
    # deep link for above (all 31 extended types except dynamodb/logs)
    # lands on that service's correct list page -- NOT the ID-shape-
    # guessing legacy path below, which doesn't know about any of these
    # service names and would silently fall through to a bare account
    # home page for every one of them (caught via direct testing before
    # shipping this fix, 2026-09-18). The legacy guesser is reserved for
    # when `service` itself is missing entirely -- an older caller that
    # hasn't been updated to pass it yet.
    if svc:
        return service_console_list_url(svc, region)
    return _legacy_prefix_guess_destination(resource_id, region)


def _legacy_prefix_guess_destination(resource: str, region: str) -> str:
    """
    Original ID-shape-guessing dispatch, kept as a fallback for any
    caller that doesn't pass an explicit `service`. Covers only
    EC2/EBS/Lambda/RDS — identical to this file's behavior before this
    patch, no S3/ELB/ECS support on this path.
    """
    region = _safe_region(region)
    if not resource:
        return f"https://{region}.console.aws.amazon.com/console/home?region={region}"

    if resource.startswith("i-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Instances:instanceId={resource}")
    if resource.startswith("vol-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Volumes:volumeId={resource}")
    if "lambda" in resource or resource.startswith("arn:aws:lambda"):
        fn = resource.split(":")[-1]
        return (f"https://{region}.console.aws.amazon.com/lambda/home"
                f"?region={region}#/functions/{fn}")
    if resource.startswith("db-") or "rds" in resource:
        return f"https://{region}.console.aws.amazon.com/rds/home?region={region}#database:"

    return f"https://{region}.console.aws.amazon.com/console/home?region={region}"


def _service_read_actions(service: str) -> list[str]:
    """
    Minimal read-only IAM actions needed to view/monitor ONE AWS
    service in the console, used to build a session policy that
    narrows a federated session down to just this service — instead
    of the previous blanket ReadOnlyAccess, which grants read access
    to every AWS service regardless of which alert/resource the
    person actually clicked into.
    """
    common = [
        "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms",
        "tag:GetResources", "tag:GetTagKeys", "tag:GetTagValues",
        "sts:GetCallerIdentity",
    ]
    per_service = {
        "ec2":    ["ec2:Describe*", "ec2:GetConsoleOutput", "ec2:GetConsoleScreenshot"],
        "ebs":    ["ec2:Describe*"],
        "rds":    ["rds:Describe*", "rds:ListTagsForResource"],
        "lambda": ["lambda:Get*", "lambda:List*"],
        "s3":     ["s3:GetBucket*", "s3:ListBucket", "s3:GetObject", "s3:ListAllMyBuckets"],
        "elb":    ["elasticloadbalancing:Describe*"],
        "ecs":    ["ecs:Describe*", "ecs:List*"],
        "security_group": ["ec2:DescribeSecurityGroups", "ec2:DescribeSecurityGroupRules"],
        "iam_user": ["iam:GetUser", "iam:ListMFADevices", "iam:ListAccessKeys",
                     "iam:GetLoginProfile", "iam:GetAccessKeyLastUsed"],
    }
    extra = per_service.get((service or "").lower())
    if not extra:
        return []
    return common + extra


def _service_resource_arns(service: str, resource_id: str | None, region: str | None,
                            account_id: str | None, resource_name: str | None = None,
                            ecs_service_name: str | None = None) -> list[str] | None:
    """
    Best-effort ARN(s) for the SPECIFIC resource being viewed, so the
    session policy's Resource element can be scoped to just that
    resource wherever AWS IAM actually supports resource-level
    permissions for the relevant read actions. Returns None (caller
    falls back to "*") for services where the Describe/List calls
    involved are account/region-wide by design in AWS IAM — e.g.
    ec2:DescribeInstances has no resource-level permission support —
    which is a hard AWS limitation, not a gap in this function.
    """
    if not resource_id or not account_id:
        return None
    svc = (service or "").lower()
    region = region or "us-east-1"
    if svc == "s3":
        bucket = resource_id
        return [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]
    if svc == "lambda":
        return [f"arn:aws:lambda:{region}:{account_id}:function:{resource_id}"]
    if svc == "rds":
        return [f"arn:aws:rds:{region}:{account_id}:db:{resource_id}"]
    if svc == "ecs":
        cluster = resource_name or resource_id
        arns = [f"arn:aws:ecs:{region}:{account_id}:cluster/{cluster}"]
        if ecs_service_name:
            arns.append(f"arn:aws:ecs:{region}:{account_id}:service/{cluster}/{ecs_service_name}")
        return arns
    if svc == "iam_user":
        # IAM is global -- no region in this ARN. Resource-level
        # restriction IS supported here (unlike ec2:Describe*/
        # security_group above, a hard AWS limitation, not an
        # oversight), so this narrows further than "*".
        username = resource_name or resource_id
        return [f"arn:aws:iam::{account_id}:user/{username}"]
    return None


def build_scoped_session_policy(service: str | None, resource_id: str | None = None,
                                 region: str | None = None,
                                 target_account_id: str | None = None,
                                 resource_name: str | None = None,
                                 ecs_service_name: str | None = None) -> str | None:
    """
    Builds an IAM session-policy JSON string that narrows a federated
    console session to read-only access for ONE service — and, where
    AWS IAM supports it, ONE specific resource — instead of the
    previous blanket ReadOnlyAccess across every AWS service. Returns
    None if `service` isn't recognized, so callers fall back to
    whichever base policy they already had.

    This is a real IAM session policy: AWS enforces the
    INTERSECTION of this policy and the underlying role/user's own
    permissions, so it can only ever narrow access further — never
    grant anything the base identity didn't already have.
    """
    if not service:
        return None
    actions = _service_read_actions(service)
    if not actions:
        return None
    arns = _service_resource_arns(service, resource_id, region, target_account_id,
                                   resource_name, ecs_service_name)
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect":   "Allow",
            "Action":   sorted(set(actions)),
            "Resource": arns if arns else "*",
        }],
    }
    body = json.dumps(policy)
    # STS session-policy documents are capped at 2048 chars — fall
    # back to no extra scoping (base policy still applies) rather
    # than send something AWS would reject outright.
    if len(body) > 2000:
        logger.warning("Scoped session policy for %s too large (%d chars) — skipping extra scoping", service, len(body))
        return None
    return body


def _write_console_open_audit(requested_by, target_account_id, service, resource_id):
    """
    Records who opened a console link and for what, in the app's OWN
    audit log (visible under Audit Logs in the UI). This is the only
    attribution the app can meaningfully provide now that it no
    longer impersonates anyone for console access — AWS-side
    attribution is whatever identity the person is personally signed
    in as, which the app has no visibility into or control over.

    `resource_id` is legitimately None for two different reasons that
    used to be indistinguishable in the audit payload -- a reviewer
    reading the raw JSON couldn't tell "console opened for a specific
    resource, id somehow missing" from "console opened at the
    service/account level by design, there was never a resource id to
    record" (e.g. ServiceList's "Open Console" action for a
    service that has no drill-down detail page, or AccountDetail's
    top-of-page "Open Console" button before any single instance is
    selected). The explicit `scope` field below removes that
    ambiguity: "resource" whenever an id is present, "service"
    otherwise, so a null resource_id reads as intentional rather than
    looking like missing data.

    NOTE: role is deliberately not recorded here yet -- doing so
    requires threading the caller's role down through every provider's
    get_console_url() signature (app/providers/base.py and each of
    aws/azure/gcp's implementations), which is out of scope for this
    fix. The Compliance UI no longer fabricates a role for entries
    that don't have one (see Compliance.jsx), so this omission now
    shows as "no role recorded" rather than a misleading "ADMIN".
    """
    from app.audit import write_audit
    write_audit(
        requested_by or "unknown",
        "Opened AWS console link",
        payload={
            "account_id": target_account_id,
            "service": service,
            "resource_id": resource_id,
            "scope": "resource" if resource_id else "service",
            "at": datetime.datetime.utcnow().isoformat(),
        },
    )


def build_federated_console_url(role_arn: str | None, external_id: str | None,
                                 destination: str,
                                 target_account_id: str | None = None,
                                 requested_by: str | None = None,
                                 service: str | None = None,
                                 resource_id: str | None = None,
                                 region: str | None = None,
                                 resource_name: str | None = None,
                                 ecs_service_name: str | None = None) -> str:
    """
    Returns `destination` (the exact resource-specific console URL)
    directly -- NO session is minted, NO identity is assumed on the
    person's behalf, and NO password is ever seen or handled by this
    app. Still records the click in this app's own audit log via
    `requested_by`/`service`/`resource_id`, and `role_arn`/
    `external_id` are accepted for backward compatibility with callers
    but unused (no credentials are minted here or ever were, post-
    2026-09-12 -- see this module's docstring).

    HISTORY / WHY THIS ISN'T WRAPPED IN A SIGN-IN URL (2026-09-19):
    an earlier version of this function wrapped `destination` in
    https://{account}.signin.aws.amazon.com/console?redirect_uri=...,
    intending an "account-locked sign-in that lands you on the right
    page after login" -- reported broken by direct user testing
    (landed on the console after sign-in, but not the specific
    resource). Researched AWS's own documentation
    (docs.aws.amazon.com/IAM/latest/UserGuide/id_users_sign-in.html)
    plus multiple independent real-world sources afterward: that
    sign-in endpoint documents exactly ONE query parameter (`region`)
    -- `redirect_uri` is not a real, supported mechanism for it. Post-
    login deep-linking for a specific console page is only genuinely
    supported by AWS via SAML `RelayState` (requires a SAML/SSO
    identity-provider relationship this app doesn't set up) or the
    STS-federation `Destination` parameter (requires minting real
    temporary credentials via GetSigninToken -- exactly the
    impersonation this app was told never to do). Neither is
    available without either infrastructure this app doesn't own or
    reintroducing the exact anti-pattern already ruled out.

    Given that hard constraint, linking straight to `destination` is
    the tradeoff actually taken, matching how every real-world AWS
    deep-linking tool (e.g. the aws-link-accountifier browser
    extension, or simply bookmarking a console page, which AWS's own
    docs explicitly describe as supported) already works: if the
    browser has no AWS session yet, visiting `destination` triggers
    AWS's own native username/password sign-in prompt and correctly
    returns the person to that exact page afterward -- genuine
    deep-linking, with their own IAM credentials, no app involvement.
    The known tradeoff: if that browser already has an authenticated
    AWS session for a DIFFERENT account, `destination` opens under
    that wrong account instead of prompting a fresh sign-in (AWS has
    no way to know a different account was intended from a plain
    resource URL alone) -- there is no way to force an account
    mismatch to re-prompt without either of the two options ruled out
    above. If this becomes a recurring problem, AWS IAM Identity
    Center's "Create shortcut" feature is the AWS-native way to get
    real cross-account deep-linking safely, but it requires migrating
    off native per-account IAM users onto IAM Identity Center first --
    a real infrastructure decision, not something this function can
    silently assume or set up on its own.
    """
    _write_console_open_audit(requested_by, target_account_id, service, resource_id)
    return destination
