# app/help_content/concepts.py
"""Overview, accounts, environments/tagging, priority, metrics/polling, thresholds, alerts, API cost.
Facts come from the code named in each section; tables that mirror constants are built from those constants."""
from .blocks import h, p, ul, table, note, code, link, section, fmt_interval


def overview():
    return section("overview", "Overview", "Getting started", "viewer", None, [
        p("CloudOps (Monitoring Hub) watches AWS, Azure and GCP accounts: it discovers resources, collects metrics on a "
          "schedule, raises alerts when a metric crosses a threshold, and groups, routes and reports on them."),
        h("What is in the sidebar"),
        table(["Group", "Pages", "Who sees it"], [
            ["Overview", "Overview", "every signed-in user"],
            ["Infrastructure", "Account Onboarding (admin, editor), Environments (environments.view), and a live account / region tree", "per permission"],
            ["Monitoring", "SLOs (slo.view), Synthetic Checks (synthetic.view), Security Findings (security.view), Maintenance Windows (maintenance.view)", "per permission"],
            ["Alerts & Incidents", "Alerts, Escalation Policies (escalation.view), Status Page (admin, editor with status_page.manage)", "per permission"],
            ["Intelligence", "Smart Search (search.query)", "per permission"],
            ["Reports", "Reports (reports.view, when the reports feature is on), Compliance (the audit trail)", "per permission"],
            ["Administration", "Access Control (users.view), Settings (admin, editor)", "per permission"],
        ]),
        note("A page you cannot open is hidden from the sidebar and redirects to Overview. The server enforces the same "
             "permission on every API call, so hiding is a convenience, never the protection."),
        h("Pages reached from an account"),
        ul(["Account page: the account's resources and health.",
            "Services: every service type the account has, then one page per service (for example EC2, RDS, WAF).",
            "Topology: the dependency graph of the account's resources.",
            "Incidents: correlated incidents, health scores and capacity forecasts for the account."]),
        p("Operational Events (/op-events) and Deploy Risk exist in the code but are not linked from the sidebar."),
        h("Where to go next"),
        ul(["New here: read Accounts and onboarding, then Environments and tagging.",
            "Daily use: Alerts, then Thresholds and Metrics and polling.",
            "Why did I get paged: Priority and routing, Alerts, Notifications."]),
    ], "What CloudOps is and how the pages fit together")


def accounts():
    return section("accounts-onboarding", "Accounts and onboarding", "Getting started", "editor", "accounts.onboard", [
        p("Account Onboarding connects a cloud account. The page is open to admin and editor roles; the API call "
          "(POST /api/admin/accounts) requires accounts.onboard. Viewing accounts needs accounts.view."),
        h("Fields common to every cloud"),
        table(["Field", "Meaning"], [
            ["Account Name", "Display name (required)."],
            ["Alias / Alias Tag", "Short alias shown in lists."],
            ["Environment", "Account-level default environment (prod, uat, dev). Used when nothing more specific places a resource."],
            ["Owner Team", "Team accountable for the account."],
            ["Description", "Free text."],
        ]),
        h("AWS"),
        table(["Field", "Meaning"], [
            ["AWS Account ID", "12 digits, digits only."],
            ["IAM Role ARN + External ID", "Recommended. CloudOps assumes the role; the External ID must equal the sts:ExternalId in the role's trust policy."],
            ["Access Key ID + Secret Access Key", "Static keys alternative. Choose either the role or the keys, not both."],
        ]),
        p("The assumed role is expected to carry the AWS managed ReadOnlyAccess policy (app/aws/sts.py). A narrower custom "
          "policy needs tag:GetResources added if tag sync is used (docs/TAG_SYNC.md), and ce:GetCostAndUsage "
          "if the actual AWS cost panel is turned on (docs/AWS_ACTUAL_COST.md)."),
        h("Azure"),
        table(["Field", "Meaning"], [["Tenant (Directory) ID", "GUID of the Entra tenant."],
                                     ["Subscription ID", "GUID of the subscription."],
                                     ["Application (Client) ID and Client Secret", "Service principal that CloudOps signs in as."]]),
        h("GCP"),
        table(["Field", "Meaning"], [["GCP Project ID", "Project to monitor."],
                                     ["Service Account Key (JSON)", "Pasted key of a service account with monitoring read access."]]),
        h("Testing before saving"),
        p("The page can test credentials first (POST /api/admin/accounts/test-role, test-azure-credentials, "
          "test-gcp-credentials). Saving after a changed credential asks for a fresh test."),
        h("Discovery"),
        p("After onboarding, resources are discovered (POST /api/admin/accounts/{id}/discover re-runs it). Stopped and "
          "terminated EC2 instances are skipped by the metrics collector."),
        h("What each cloud scans"),
        table(["Cloud", "Scope of discovery", "What the region field does"], [
            ["AWS", "One region per account: the Default region chosen at onboarding. Resources in other regions are not "
                    "discovered. To monitor a second region, onboard the account again with that region.",
             "Selects the region that is scanned"],
            ["Azure", "The whole subscription: VMs, storage, databases, web apps, caches, messaging, load balancers and "
                      "more are listed across every location.",
             "Short name (for example centralindia) used for the metrics endpoint; lowercase letters and digits only"],
            ["GCP", "The whole project: Compute instances in every zone, Cloud Storage buckets and Cloud SQL instances. "
                    "Cloud Run has no cross-region list, so 12 fixed regions are probed (asia-south1, asia-south2, "
                    "asia-southeast1, asia-east1, asia-northeast1, australia-southeast1, us-central1, us-east1, us-west1, "
                    "europe-west1, europe-west2, europe-central2).",
             "Stored as the account's default region"],
        ]),
        note("A Cloud Run service in a region outside that list of 12 will not appear. A discovery step that fails for "
             "one service (for example a missing permission) is skipped and does not stop the others.", "warn"),
    ], "Connect AWS, Azure and GCP accounts")


def accounts_admin():
    return section("accounts-credentials", "Credential handling and offboarding", "Getting started", "admin",
                   "accounts.credentials.manage", [
        p("Credentials are secrets. The permission accounts.credentials.manage covers rotating role ARNs, keys and "
          "service principals; accounts.delete covers offboarding."),
        ul(["Prefer an IAM role with an External ID over static keys: nothing long-lived is stored.",
            "Secret fields are not echoed back in the form (autocomplete is switched off on them).",
            "Deleting an account is an admin action (accounts.delete). Exporting an account's data "
            "(GET /api/admin/accounts/{id}/export) also requires accounts.delete.",
            "Console links to the cloud are created on demand (POST .../console-url) and AWS opens are audit-logged."]),
        h("How secrets are stored"),
        ul(["Azure client secrets, GCP service-account keys and AWS static access keys are encrypted with Fernet "
            "(AES-128-CBC with an HMAC) before they are written to the provider_credentials table. The account row only "
            "keeps an opaque reference, never the secret.",
            "The key comes from the CREDENTIAL_ENCRYPTION_KEY setting in the server .env. If it is unset, a key file is "
            "auto-created under config/ (intended for local development only).",
            "IAM role ARNs and External IDs are identifiers, not long-lived secrets, so they are not stored this way."]),
        note("Keep CREDENTIAL_ENCRYPTION_KEY safe and unchanged. If it is lost or replaced, every stored secret becomes "
             "unreadable and each account's credentials must be entered again. The deploy script reuses the existing key "
             "on redeploy for this reason.", "warn"),
    ], "Rotation, offboarding and secret handling (admin)")


def environments():
    return section("environments-tagging", "Environments and tagging", "Configuration", "viewer", "environments.view", [
        p("Every resource has an environment: prod, uat or dev. It drives alert severity, priority (P1-P4) and, when "
          "environment polling is on, how often non-critical metrics are collected."),
        h("How the environment is decided"),
        p("In this order; the winning step is shown as 'Decided by' on the Environments page:"),
        ul(["Manual choice saved on the Environments page.",
            "The Environment tag (aliases Env, Stage, DeploymentEnvironment, AppEnv, EnvType; key case ignored).",
            "A whole-word environment token in the resource name (prod, uat, dev). 'product' or 'prodapi' do not match on purpose.",
            "The resource it is attached to (an EBS volume follows its EC2 instance).",
            "The account's default environment.",
            "Unknown: alerted at prod strength and listed under Needs classification."], ordered=True),
        table(["Value", "Standard tag", "Also understood"], [
            ["Production", "prod", "production, prd"],
            ["UAT", "uat", "preprod, stage, staging, stg, qa"],
            ["Development", "dev", "development, test, testing, sandbox"],
        ]),
        h("Tags CloudOps reads"),
        table(["Tag", "Used for"], [
            ["Environment", "alert strength, priority, polling, Needs classification"],
            ["Criticality", "Criticality=critical raises the alert priority one level (never above P1) when priorities are on (app/priority.py)"],
            ["Owner", "who answers for the resource"],
            ["Application", "what it belongs to; also used for placement suggestions"],
        ]),
        p("CostCenter, DataClassification and ManagedBy are listed in docs/TAGGING_STANDARD.md for finance and security; "
          "CloudOps does not read them."),
        h("Naming pattern"),
        code("<org>-<application>-<env>-<role>-<nn>     e.g. u4rad-orthanc-prod-db-01"),
        p("Exactly one environment word per name. Names like test-prod cannot be classified."),
        h("Working the Environments page"),
        ul(["Needs classification: resources nothing could place. Suggestions come from already-placed siblings in the same "
            "account; nothing is applied until a person accepts, and accepted suggestions are audited.",
            "Mismatches: tag, name, manual choice or attached resource disagree. A warning never changes the environment used.",
            "Tag compliance: percentages per account and resource type, a daily trend and a CSV export. Types whose tags "
            "CloudOps cannot read are 'cannot be checked', never 'untagged'.",
            "Tagging gaps: CSV of checkable resources missing standard tags.",
            "Changing an account default shows a preview of the impact first (environments.manage to apply)."]),
        h("Tag sync (off by default)"),
        p("With TAG_SYNC_ENABLED on, CloudOps reads tags for more AWS types through the Resource Groups Tagging API "
          "(free) so 'untagged' can be stated for them. Azure tags and GCP labels are read from discovery. "
          "A sweep that fails or is empty claims nothing. Details: docs/TAG_SYNC.md."),
        table(["Setting", "Default", "Effect"], [
            ["TAG_SYNC_ENABLED", "off", "Turns the sync on"],
            ["TAG_SYNC_INTERVAL_MINUTES", "60 (min 5)", "How often a sweep runs"],
            ["TAG_SYNC_MAX_AGE_HOURS", "6", "How long a stored result is trusted"],
            ["TAG_SYNC_DIRECT_LOOKUP", "off", "Read S3 / WAFv2 tags directly when the sweep cannot decide"],
            ["TAG_SYNC_FEED_ENVIRONMENT", "off", "Lets a synced Environment tag decide the environment (changes alert strength)"],
        ]),
        p("Weekly digest of new unplaced resources: ENV_DIGEST_ENABLED (off by default), ENV_DIGEST_WEEKDAY."),
        note("Troubleshooting: a resource shows 'Needs classification' until a tag, a clean name, an attached parent, an "
             "account default or a manual choice places it. Fix the tag at the source where you can."),
    ], "Environment resolution, tags, naming and tag sync")


def priority():
    return section("priority-routing", "Priority P1-P4 and routing", "Configuration", "viewer", "alerts.view", [
        p("Priority says who is told first. It comes from the environment and the severity together and does not change "
          "how often data is collected."),
        table(["Environment", "Critical alert", "Warning or info alert"],
              [["Prod", "P1", "P2"], ["UAT", "P2", "P3"], ["Dev", "P3", "P4"]]),
        ul(["An environment that is not prod, uat or dev (unknown, ignore, free text) is ranked like prod.",
            "A resource tagged Criticality=critical goes up one level, never above P1.",
            "Priority is recomputed for every open alert each cycle; resolved alerts keep their last value."]),
        p("Switch: PRIORITY_ENABLED (default off). Off: no priority is stored or shown. On: P-chips, the Firing by priority strip "
          "on the Alerts page, P1-first ordering, and the level at the front of new-alert popups and notification messages."),
        p("PRIORITY_ROUTING_ENABLED is reserved for a later release (escalation by priority and per-channel minimum "
          "level). The flag exists but nothing reads it yet, so turning it on changes no behaviour today. Escalation and "
          "notification channels still route by severity, not by P-level."),
        link("The same table in the app: Alerts, 'How to read alerts'", "/alerts"),
    ], "P1-P4 rule and flags")


def metrics_polling():
    from app.collector import polling_model as pm
    core_counts = {}
    for m in pm.AWS_CORE_METRICS:
        core_counts[m.tier] = core_counts.get(m.tier, 0) + 1
    tiers = [
        ("critical", "Availability, error and saturation signals published every minute; also evaluated on every 2-minute tick."),
        ("standard", "Alertable signals, or sources the cloud itself publishes every 5 minutes (EC2 basic monitoring)."),
        ("low", "Trends, capacity and slower failure signals."),
        ("extended", "Informational metrics of less critical services. AWS and GCP 60 min; Azure 15 min."),
        ("slow_extended", "Values published once a day (S3 storage, certificate and key expiry). Azure 60 min, GCP has no daily tier."),
    ]
    rows = [[t, fmt_interval(pm.TIER_SECONDS[t]), d, core_counts.get(t, 0)] for t, d in tiers]
    ev = [[fmt_interval(i), f"{pm.EVAL_WINDOW_MIN_BY_INTERVAL[i]} min", f"{pm.STALE_MIN_BY_INTERVAL[i]} min"]
          for i in sorted(pm.EVAL_WINDOW_MIN_BY_INTERVAL)]
    return section("metrics-polling", "Metrics and polling", "Configuration", "viewer", "metrics.view", [
        p("Each metric is collected on a tier. The tier follows the metric, never the alert priority, and no metric that "
          "the cloud publishes every 5 minutes or slower is placed in the 2-minute tier."),
        table(["Tier", "Polled every", "What is in it", "AWS core metrics in tier"], rows),
        p("Each metric chart shows its exact polling interval; the per-service pages in this guide list it for every metric."),
        h("Free and paid"),
        ul(["Free: Describe*/List* resource calls, Resource Groups Tagging, STS, CloudTrail LookupEvents. EC2 status checks and "
            "ALB healthy/unhealthy host counts come from Describe calls polled every "
            f"{pm.DESCRIBE_POLL_SECONDS} s.",
            "Paid: CloudWatch GetMetricData (per metric requested), ListMetrics, GetMetricStatistics; Azure Monitor; GCP ListTimeSeries."]),
        h("Look-back, stale and 'no data'"),
        p("A poll looks back further than its interval because clouds publish late (CloudWatch 5-minute points appear 5-10 "
          "minutes after their period; Azure and GCP about 5 minutes). 'No data' means no point arrived in the window. "
          "'Stale' is judged from the metric's own interval:"),
        table(["Poll interval", "Alert evaluation window", "Treated as stale after"], ev),
        h("Environment polling (ENV_POLLING_ENABLED)"),
        p("Needs ENV_AWARE_CLASSIFICATION on as well. Off: every resource is polled at its base interval. On:"),
        table(["Environment", "Critical-class metric", "Everything else"], [
            ["prod, unknown, ignore", "unchanged", "unchanged"],
            ["uat", "unchanged", "base interval x 3 (POLL_UAT_MULTIPLIER)"],
            ["dev", "at least 10 min", "at least 60 min, or not collected (POLL_DEV_NONCRITICAL_MODE=off)"],
        ]),
        ul(["Slowing never goes below the base interval; the poll that does run looks back further, so charts keep the same number of points and are only later.",
            "Never slowed: metrics whose base interval is 24 h or more, WAFv2, and the free Describe-API metrics.",
            "Critical-class means the set evaluated every 2 minutes. On dev only critical-class metrics are evaluated for alerts by default."]),
        h("WAF fast polling (off by default)"),
        p("WAF_FAST_POLLING_ENABLED moves key WAFv2 metrics from daily to 5-minute polling at 1-minute resolution; "
          "WAF_PER_RULE_ENABLED adds per-rule blocked/counted series. Both read once per process: restart after a change."),
        link("Per-service polling intervals", "#svc-aws-ec2"),
    ], "Tiers, free vs paid, stale and no data, environment polling")


def polling_admin():
    from app.collector import env_polling as ep
    return section("polling-config", "Polling settings (admin)", "Administration", "admin", "system.admin", [
        p("These are .env values on the server (read at call time; restart the service to be sure they apply)."),
        table(["Setting", "Default", "Range", "Effect"], [
            ["ENV_AWARE_CLASSIFICATION", "off", "true/false", "Required for environment polling and priority from resolved environment"],
            ["ENV_POLLING_ENABLED", "off", "true/false", "Environment-based polling (needs the flag above)"],
            ["POLL_UAT_MULTIPLIER", str(ep.DEFAULT_UAT_MULTIPLIER), f"1-{ep.MAX_MULTIPLIER}", "UAT non-critical interval multiplier"],
            ["POLL_DEV_INTERVAL_SECONDS", str(ep.DEFAULT_DEV_INTERVAL_SECONDS), f"{ep.MIN_INTERVAL_SECONDS}-{ep.MAX_INTERVAL_SECONDS}", "Dev non-critical interval"],
            ["POLL_DEV_CRITICAL_INTERVAL_SECONDS", str(ep.DEFAULT_DEV_CRITICAL_INTERVAL_SECONDS), f"{ep.MIN_INTERVAL_SECONDS}-{ep.MAX_INTERVAL_SECONDS}", "Dev critical-class interval"],
            ["POLL_DEV_NONCRITICAL_MODE", ep.DEFAULT_DEV_NONCRITICAL_MODE, "slow | off", "slow polls dev non-critical hourly; off stops collecting"],
            ["POLL_DEV_EVAL_CRITICAL_ONLY", "true", "true/false", "Dev: evaluate only critical-class metrics"],
            ["WAF_FAST_POLLING_ENABLED / WAF_PER_RULE_ENABLED", "off", "true/false", "See Metrics and polling"],
            ["PRIORITY_ENABLED / PRIORITY_ROUTING_ENABLED", "off", "true/false", "See Priority"],
        ]),
        note("Measure paid-call volume on the PROD server only; DEV intentionally makes no paid cloud calls."),
    ], "Environment variables that change polling")


def thresholds():
    from app import threshold_defaults as td
    return section("thresholds", "Thresholds", "Configuration", "editor", "alerts.configure", [
        p("A threshold is a warning value, a critical value and a comparison: '>' / '>=' alert at or above the number, "
          "'<' alerts below it (credits, free space, healthy hosts). Edit them in Settings, Metric Thresholds "
          "(alerts.configure). 'Seed defaults' fills in the built-in starting values."),
        h("Static, dynamic and placeholder"),
        ul(["Static: the typed numbers are the lines.",
            "Dynamic: a learned band around the metric's own baseline. The auto-tuner can switch a chronically noisy static "
            "threshold to dynamic.",
            f"Placeholder ({td.PLACEHOLDER_THRESHOLD[0]:g} / {td.PLACEHOLDER_THRESHOLD[1]:g} '>'): anomaly-only. It alerts only on a "
            "sustained spike: above 1.5x the usual value for that hour and for about 3 cycles (15 min on the 5-minute tier)."]),
        h("Always static"),
        p("Some metrics must never use a learned band because the absolute value is the signal: disk and memory used %, "
          "certificate and key expiry (days or seconds to expiry), status checks, healthy-host counts, health-check and "
          "cluster state, tunnel and connection state, backup and notification failures, free storage, credit and burst "
          "balances, and failure counters where any non-zero reading is a defect. The per-service pages in this guide mark "
          "each metric 'static only' or 'static or dynamic'."),
        h("Floors and whole numbers"),
        p("A few metrics have a minimum absolute value below which no alert is raised (ALERT_MIN_ABSOLUTE, for example "
          "target 4xx counts). Count metrics get whole-number learned limits so the number shown is one you can act on."),
        link("Default thresholds for each metric", "#svc-aws-ec2"),
        note("Troubleshooting: an alert that keeps opening and closing usually means a line sits just above the normal "
             "level; raise it or use the static line for capacity metrics."),
    ], "Warning/critical lines, dynamic bands, placeholders")


def alerts():
    return section("alerts", "Alerts", "Alerts and incidents", "viewer", "alerts.view", [
        p("An alert opens when a metric breaches its threshold for several evaluation cycles in a row, so one stray reading "
          "does not raise a false alarm; it closes after the same number of healthy readings."),
        link("Colours, priority table and polling groups: Alerts page, 'How to read alerts'", "/alerts"),
        h("Severity"),
        ul(["Critical (red), Warning (amber), Info (blue), Resolved (green). Info is the warning-level breach on a UAT or Dev resource.",
            "If a resource shows Info but should be Production, set its Environment on the Environments page."]),
        h("Lifecycle"),
        ul(["Active: firing. Acknowledged: someone has taken it (still open). Resolved: back inside its limits, or resolved by hand.",
            "Acknowledge, Resolve, Mute and Unmute, and Mark false positive need operations.execute; a group of alerts can be acknowledged at once.",
            "Mute lasts 1 minute to 7 days (default 30 minutes).",
            "Clearing alerts (alerts.clear) deletes records and is an admin-level action."]),
        h("When they are checked"),
        p("The evaluator runs every 5 minutes. Critical-class metrics are also evaluated on every 2-minute tick. "
          "Alert windows and staleness follow each metric's own polling interval (see Metrics and polling)."),
        h("Noise control"),
        ul(["Several consecutive breaches before opening.",
            "Anomaly-only placeholder thresholds and minimum absolute floors (see Thresholds).",
            "Maintenance windows silence alerts during planned work.",
            "Info instead of Warning on UAT and Dev."]),
        h("Other tools on an alert"),
        ul(["Explain and RCA report (alerts.view), console link into the cloud console."]),
        note("Troubleshooting: no alert although a metric looks bad. Check the metric is enabled for the account, the "
             "resource is not in an active maintenance window, the threshold is not the placeholder, and the data is not stale."),
    ], "Severity, lifecycle, acknowledge, mute, noise control")


def api_cost():
    return section("cloud-api-cost", "Cloud API cost: free and paid", "Administration", "admin", "system.admin", [
        p("Only metric reads cost money. The cost class of every metric is on its per-service page."),
        table(["Call", "Class"], [
            ["Describe* / List* resource APIs, Resource Groups Tagging, STS, CloudTrail LookupEvents", "Free; polled freely"],
            ["CloudWatch GetMetricData", "Paid per metric requested"],
            ["CloudWatch ListMetrics, GetMetricStatistics", "Paid (per request / per metric); counted separately, with no estimated price"],
            ["Azure Monitor", "Per 1,000 calls after 1M free per subscription per month"],
            ["GCP ListTimeSeries", "Per series after 1M free per billing account per month"],
        ]),
        p("Measured usage: once an hour each process writes one operational event per provider and tier "
          "(event type metric_api_usage) with calls, units and a list-price ceiling that ignores free tiers. "
          "Read the totals from those events on PROD."),
        note("This guide states no prices: confirm them on the cloud bill."),
    ], "Which calls cost money and how usage is measured")


def api_actual_cost():
    return section("cloud-actual-cost", "Actual AWS charge (Cost Explorer)", "Administration", "admin", "usage.manage", [
        p("The Cloud API cost page estimates cost from CloudOps's own counters. The Actual AWS charge panel on the same "
          "page shows what AWS really billed for the CloudWatch service, so the estimate can be checked. Only admins "
          "(permission usage.manage) can see this panel or refresh it."),
        h("What it needs"),
        ul(["One read-only IAM action on each monitored AWS role: ce:GetCostAndUsage. It cannot change anything. "
            "The policy and the steps are in docs/AWS_ACTUAL_COST.md (deploy/iam/monitoring-hub-cost-read.json).",
            "Cost Explorer enabled in the AWS account (first data can take up to a day).",
            "ACTUAL_COST_ENABLED=1 on the PROD server. Leave it off on DEV: DEV does not poll metrics."]),
        h("What it costs"),
        p("AWS bills every Cost Explorer request. CloudOps asks at most once a day per account and never on page load. "
          "The Refresh now button is limited to once an hour per account. The panel shows how many requests were made."),
        h("Reading it"),
        table(["Status", "Meaning"], [
            ["up to date", "The last read worked."],
            ["permission missing", "The role lacks ce:GetCostAndUsage. Attach the policy, then Refresh now."],
            ["Cost Explorer not enabled", "Enable Cost Explorer in that AWS account."],
            ["could not read", "AWS returned another error; the message is shown."],
        ]),
        ul(["AWS lags real usage by up to about 48 hours, so the newest day is partial and lower than the estimate. "
            "Compare the two only for complete days.",
            "The actual figure covers everything CloudWatch bills (alarms, custom metrics, logs, API requests), not only "
            "CloudOps's reads. The usage-type list shows how much is GetMetricData.",
            "A member account shows only its own charges; a management (payer) account's role shows every linked account."]),
        note("Troubleshooting: AccessDenied with the policy attached usually means the account setting \"IAM user and role "
             "access to Billing information\" is off. Only the account root user can change it."),
    ], "What AWS really billed for CloudWatch, and how to turn it on")
