# app/help_content/concepts.py
"""Overview, accounts, environments/tagging, priority, metrics/polling, thresholds, alerts, API cost.
Facts come from the code named in each section; tables that mirror constants are built from those constants."""
from .blocks import h, p, ul, table, note, code, link, section, fmt_interval


def overview():
    return section("overview", "Overview", "Getting started", "viewer", None, [
        p("CloudOps (Monitoring Hub) watches AWS, Azure and GCP accounts: it discovers resources, collects metrics on a "
          "schedule, raises alerts when a metric crosses a threshold, and groups, routes and reports on them. Servers can "
          "also report about themselves through a host agent."),
        h("What is in the sidebar"),
        table(["Group", "Pages", "Who sees it"], [
            ["Overview", "Overview", "every signed-in user"],
            ["Infrastructure", "Account Onboarding (admin, editor), Environments (environments.view), Host Agents (hostagents.view), and a live account / region tree", "per permission"],
            ["Monitoring", "SLOs (slo.view), Synthetic Checks (synthetic.view), Security Findings (security.view), Maintenance Windows (maintenance.view)", "per permission"],
            ["Alerts & Incidents", "Alerts, Escalation Policies (escalation.view), Status Page (admin, editor with status_page.manage)", "per permission"],
            ["Intelligence", "Smart Search (search.query)", "per permission"],
            ["Reports", "Reports (reports.view, when the reports feature is on), Compliance (the audit trail)", "per permission"],
            ["Administration", "Access Control (users.view), Settings (admin, editor), Cloud Cost (usage.view; admin, editor)", "per permission"],
            ["Help", "This Help Center (help.view)", "every signed-in user"],
        ]),
        note("A page you cannot open is hidden from the sidebar and redirects to Overview. The server enforces the same "
             "permission on every API call, so hiding is a convenience, never the protection."),
        h("What the Overview page shows"),
        ul(["Alert activity for the last 24 hours: alerts triggered per hour above the axis and resolved per hour below it, split by severity.",
            "Recent changes: triggered and resolved alerts, incidents and system events from the last 24 hours.",
            "Where it is happening: firing alerts by region and service. Click a cell to open that service.",
            "Top problematic resources: ranked by critical alerts, then warnings, then how long they have been firing.",
            "Active incidents: alerts the platform correlated into one problem (needs incident access).",
            "Anomalies and forecasts: model-derived signals (multivariate anomalies and linear-trend capacity forecasts), "
            "labelled so they are never mistaken for measurements.",
            "Coverage and data freshness: when each account and region last reported, and what is monitored.",
            "A health ring: services counted as healthy, warning or critical."]),
        h("Tools in the top bar"),
        ul(["Search and jump (Ctrl K): open the command palette to jump to a page or an account.",
            "Notification bell: the number of open alerts; new alerts also pop up while you are signed in.",
            "Time zone selector: show times in IST or UTC across the app.",
            "Live indicator: updates arrive by push, so lists refresh without reloading."]),
        h("Pages reached from an account"),
        ul(["Account page: the account's resources and health.",
            "Services: every service type the account has, then one page per service (for example EC2, RDS, WAF).",
            "Topology: the dependency graph of the account's resources.",
            "Incidents: correlated incidents, health scores and capacity forecasts for the account."]),
        p("Operational Events (/op-events) and Deploy Risk exist in the code but are not linked from the sidebar."),
        h("Where to go next"),
        ul(["New here: read Accounts and onboarding, then Environments and tagging.",
            "Daily use: Alerts, then Thresholds and Metrics and polling.",
            "Why did I get paged: Priority and routing, Alerts, Notifications.",
            "Monitoring a server from the inside: Host agents.",
            "What does monitoring cost: Cloud Cost page."]),
    ], "What CloudOps is and how the pages fit together")


def accounts():
    return section("accounts-onboarding", "Accounts and onboarding", "Getting started", "editor", "accounts.onboard", [
        p("Account Onboarding connects a cloud account. The page is open to admin and editor roles; the API call "
          "(POST /api/admin/accounts) requires accounts.onboard. Viewing accounts needs accounts.view."),
        h("Fields common to every cloud"),
        table(["Field", "Meaning"], [
            ["Account Name", "Display name (required)."],
            ["Alias / Alias Tag", "Short alias shown in lists."],
            ["Environment", "Account-level default environment (PROD or NON-PROD). Used when nothing more specific places a resource."],
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
        p("Every resource is either PROD or NON-PROD (dev, test, SIT, UAT, QA, staging and the like are all NON-PROD). It drives alert severity, priority (P1-P4) and, when "
          "environment polling is on, how often non-critical metrics are collected."),
        h("How the environment is decided"),
        p("In this order; the winning step is shown as 'Decided by' on the Environments page:"),
        ul(["Manual choice saved on the Environments page.",
            "The Environment tag (aliases Env, Stage, DeploymentEnvironment, AppEnv, EnvType; key case ignored).",
            "A whole-word environment token in the resource name (prod, nonprod, dev, uat, sit ...). 'product' or 'prodapi' do not match on purpose.",
            "The resource it is attached to (an EBS volume follows its EC2 instance).",
            "The account's default environment.",
            "Nothing matches: treated as PROD (fail-safe) and listed under Needs classification until someone confirms it."], ordered=True),
        table(["Value", "Standard tag", "Also understood"], [
            ["PROD", "prod", "production, prd"],
            ["NON-PROD", "nonprod", "non-prod, dev, development, test, testing, sit, uat, qa, preprod, stage, staging, stg, sandbox"],
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
              [["PROD", "P1", "P2"], ["NON-PROD", "P3", "P4"]]),
        ul(["An environment that is not recognised (unknown, free text, a value saved before the two-environment model) is ranked like PROD.",
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
            ["PROD (and anything unplaced)", "unchanged", "unchanged"],
            ["NON-PROD", "at least 10 min", "at least 60 min, or not collected (POLL_NONPROD_NONCRITICAL_MODE=off)"],
        ]),
        ul(["Slowing never goes below the base interval; the poll that does run looks back further, so charts keep the same number of points and are only later.",
            "Never slowed: metrics whose base interval is 24 h or more, WAFv2, and the free Describe-API metrics.",
            "Critical-class means the set evaluated every 2 minutes. On NON-PROD only critical-class metrics are evaluated for alerts by default."]),
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
            ["POLL_NONPROD_INTERVAL_SECONDS", str(ep.DEFAULT_NONPROD_INTERVAL_SECONDS), f"{ep.MIN_INTERVAL_SECONDS}-{ep.MAX_INTERVAL_SECONDS}", "NON-PROD non-critical interval"],
            ["POLL_NONPROD_CRITICAL_INTERVAL_SECONDS", str(ep.DEFAULT_NONPROD_CRITICAL_INTERVAL_SECONDS), f"{ep.MIN_INTERVAL_SECONDS}-{ep.MAX_INTERVAL_SECONDS}", "NON-PROD critical-class interval"],
            ["POLL_NONPROD_NONCRITICAL_MODE", ep.DEFAULT_NONPROD_NONCRITICAL_MODE, "slow | off", "slow polls NON-PROD non-critical hourly; off stops collecting"],
            ["POLL_NONPROD_EVAL_CRITICAL_ONLY", "true", "true/false", "NON-PROD: evaluate only critical-class metrics"],
            ["WAF_FAST_POLLING_ENABLED / WAF_PER_RULE_ENABLED", "off", "true/false", "See Metrics and polling"],
            ["PRIORITY_ENABLED / PRIORITY_ROUTING_ENABLED", "off", "true/false", "See Priority"],
        ]),
        link("Settings for host agents, cost, sign-in and AI", "#server-settings"),
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
        ul(["Critical (red), Warning (amber), Info (blue), Resolved (green). Info is the warning-level breach on a NON-PROD resource.",
            "If a resource shows Info but should be Production, set its Environment on the Environments page."]),
        h("Lifecycle"),
        ul(["Active: firing. Acknowledged: someone has taken it (still open). Resolved: back inside its limits, or resolved by hand.",
            "Acknowledge, Resolve, Mute and Unmute, and Mark false positive need operations.execute; a group of alerts can be acknowledged at once.",
            "Mute lasts 1 minute to 7 days (default 30 minutes).",
            "Clearing alerts (alerts.clear) deletes records and is an admin-level action."]),
        h("Tabs on the Alerts page"),
        table(["Tab", "Lists"], [
            ["Active", "Alerts that are firing"],
            ["Critical", "Firing alerts of Critical severity"],
            ["Needs Attention", "Firing alerts on a resource whose health score is below 70"],
            ["Auto-tuning", "Firing alerts the system sees flapping (opening and closing repeatedly): candidates for a better threshold"],
            ["Stale", "Alerts whose metric has stopped reporting, so their state cannot be confirmed"],
            ["Acknowledged", "Alerts someone has taken"],
            ["Muted / Maint.", "Alerts silenced by a mute or a maintenance window"],
            ["Resolved", "Alerts that closed"],
            ["All", "Everything"],
        ]),
        p("Each tab shows a count that always equals the number of rows in it, and you can search, filter by account and (when priorities are on) by P-level. "
          "The page updates by push when alerts change."),
        h("When they are checked"),
        p("The evaluator runs every 5 minutes. Critical-class metrics are also evaluated on every 2-minute tick. "
          "Alert windows and staleness follow each metric's own polling interval (see Metrics and polling)."),
        h("Noise control"),
        ul(["Several consecutive breaches before opening.",
            "Anomaly-only placeholder thresholds and minimum absolute floors (see Thresholds).",
            "Maintenance windows silence alerts during planned work.",
            "Info instead of Warning on NON-PROD."]),
        h("Other tools on an alert"),
        ul(["Explain and RCA report (alerts.view), console link into the cloud console."]),
        note("Troubleshooting: no alert although a metric looks bad. Check the metric is enabled for the account, the "
             "resource is not in an active maintenance window, the threshold is not the placeholder, and the data is not stale."),
    ], "Severity, lifecycle, acknowledge, mute, noise control")


def api_cost():
    return section("cloud-api-cost", "Cloud Cost page", "Administration", "editor", "usage.view", [
        p("Cloud Cost (sidebar, Administration) shows what CloudOps itself spends on the clouds it monitors. Only metric "
          "reads cost money; everything else CloudOps calls is free. What you see depends on your permission:"),
        table(["You hold", "You see"], [
            ["usage.view (editor, admin)", "Metering: how many paid metric reads CloudOps made, counted by CloudOps itself, for 24 hours, 7 days or 30 days."],
            ["usage.manage (admin)", "The billed cost the cloud itself reports, the cost by polling tier, and the cost-jump alert settings. See Billed cost and Cost-jump alert."],
        ]),
        h("Which calls cost money"),
        table(["Call", "Class"], [
            ["Describe* / List* resource APIs, Resource Groups Tagging, STS, CloudTrail LookupEvents", "Free; polled freely"],
            ["CloudWatch GetMetricData", "Paid per metric requested"],
            ["CloudWatch ListMetrics, GetMetricStatistics", "Paid (per request / per metric); counted separately, with no estimated price"],
            ["Azure Monitor", "Per 1,000 calls after 1M free per subscription per month"],
            ["GCP ListTimeSeries", "Per series after 1M free per billing account per month"],
            ["Host agent reports", "None: the agent calls CloudOps, not the cloud"],
        ]),
        h("Metering view"),
        ul(["Three figures at the top: CloudOps reads at list-price estimate, metric read calls (all clouds, including free tag sync) and the last usage flush, "
            "with how many hours of the range had one. A warning shows when no flush has arrived for a while, because then the newest numbers are too low.",
            "One panel per cloud with its units, calls and list-price estimate, a bar per polling tier, and how much of one free allowance has been used "
            "(Azure and GCP). Free allowances are ignored in the price, so for those clouds it is a ceiling, not the invoice.",
            "Not metered yet: paid reads that no counter covers, so the totals exclude them.",
            "Free and paid: which calls cost nothing and which are paid.",
            "Environment polling is ON / OFF: whether NON-PROD resources are polled less often (see Metrics and polling).",
            "How to read these numbers: notes on what the figures include, and the time of the newest flush."]),
        p("Measured usage: once an hour each process writes one operational event per provider and tier "
          "(event type metric_api_usage) with calls, units and a list-price ceiling, and counters are also written when the "
          "service stops cleanly. A server that makes no paid cloud calls, such as DEV, shows nothing."),
        h("Cost by polling tier (admins)"),
        ul(["A table of every tier that made reads: Critical (2 min), Standard (5 min), Low (15 min), Extended (hourly), Daily, "
            "Tag sync, Pages opened by users and Metric discovery, with units, calls and the share of the total.",
            "Pages opened by users and Metric discovery are reads that people trigger, not scheduled polling.",
            "Where to cut: plain suggestions built from the numbers. The largest polling tier, with the arithmetic saving of polling it "
            "half as often (calls scale with 1 / interval, so this is not a forecast of the bill); the share caused by people; and a "
            "reminder when environment polling is off.",
            "It is a list-price estimate of CloudOps's own metered reads, not the invoice. Rows with no stored rate are counted as unpriced, not as zero."]),
        note("This guide states no prices: confirm them on the cloud bill."),
    ], "Metering, free vs paid calls, cost by polling tier")


def api_actual_cost():
    return section("cloud-actual-cost", "Billed cost: AWS, Azure and GCP", "Administration", "admin", "usage.manage", [
        p("The top panel of Cloud Cost, 'CloudOps polling cost, billed by' AWS, Azure or GCP, shows what the cloud really "
          "billed for CloudOps's own metric reads, so the estimate on the same page can be checked. Only admins "
          "(usage.manage) see it or can refresh it. It works like the cost explorer of the cloud console."),
        h("What counts as CloudOps cost"),
        table(["Cloud", "Source", "Counted as CloudOps"], [
            ["AWS", "Cost Explorer (ce:GetCostAndUsage)", "CloudWatch GetMetricData, in the regions an account is onboarded in"],
            ["Azure", "Cost Management Query API, actual cost, daily, Azure Monitor", "The metric API call meter"],
            ["GCP", "BigQuery billing export, Cloud Monitoring, credits netted off", "The Monitoring API request SKU"],
        ]),
        p("Other charges in the same service (custom metrics, alarms, logs, ingestion, calls made by agents) are not shown in the "
          "chart; the footnote totals them as 'Not shown' so a wrong match shows up instead of hiding."),
        h("What it needs"),
        ul(["ACTUAL_COST_ENABLED=1 on the PROD server, and at least one active account of that cloud. Leave it off on DEV: DEV does not poll metrics.",
            "AWS: the read-only IAM action ce:GetCostAndUsage on each monitored role (policy and steps in docs/AWS_ACTUAL_COST.md, "
            "deploy/iam/monitoring-hub-cost-read.json) and Cost Explorer enabled in the account (first data can take up to a day).",
            "Azure: the role Cost Management Reader on the subscription, for the service principal CloudOps already uses. "
            "A subscription billed in a local currency without USD columns is refused with a message instead of showing wrong numbers.",
            "GCP: there is no cost API, so turn on Billing, Billing export, Standard usage cost to BigQuery (it only covers days after it is switched on) "
            "and set GCP_BILLING_EXPORT_TABLE=project.dataset.table. The service account needs roles/bigquery.jobUser on a project "
            "(GCP_BILLING_JOB_PROJECT if it should not be the account's own project) and roles/bigquery.dataViewer on the export dataset.",
            "If a vendor renames its meter and the page shows zero CloudOps cost next to a non-zero Not shown total, set "
            "AZURE_CLOUDOPS_METER_REGEX or GCP_CLOUDOPS_SKU_REGEX to the real name. Details: docs/CLOUD_COST_AZURE_GCP.md."]),
        p("The Azure and GCP readers follow the vendors' documentation and were not confirmed against a live account when written. "
          "The first refresh reports its outcome per account on the page: send that message if it is not 'up to date'.", unverified=True),
        h("What asking costs"),
        p("AWS bills every Cost Explorer request. Azure asks are free. GCP asks run a BigQuery job billed by bytes scanned (the first TiB "
          "each month is free at Google). CloudOps reads at most once a day per account, never on page load: the first read pulls 90 days, "
          "later reads 14 days. Refresh now is limited to once an hour per account. The footer shows how many requests were made and what checking cost."),
        h("Using the report"),
        table(["Control", "What it does"], [
            ["Cloud", "AWS / Azure / GCP switch; appears when more than one cloud has an onboarded account"],
            ["Date range", "7 days, 30 days, 90 days, This month, Last month or a Custom from / to"],
            ["Granularity", "Daily or Monthly columns"],
            ["Group by", "Account, Region or Usage type (the usage-type label names the region, so three GetMetricData lines are told apart)"],
            ["Account, Region", "Filter popovers with search (when there are more than 6), Select all and Clear; chosen filters show as chips you can remove"],
            ["Legend", "Click a series to hide or show it in the chart"],
            ["Download CSV", "The table as shown, with provisional days marked"],
            ["Refresh now", "Reads the latest figures from the cloud now (once an hour per account)"],
        ]),
        p("The table lists each group with its total, share, and (AWS) the number of metrics CloudOps requested, then one column per day or month."),
        h("Reading it"),
        table(["Status", "Meaning"], [
            ["up to date", "The last read worked."],
            ["permission missing", "AWS: ce:GetCostAndUsage on the role. Azure: Cost Management Reader. GCP: BigQuery access to the billing export. Fix it, then Refresh now."],
            ["Cost Explorer not enabled", "Enable Cost Explorer in that AWS account."],
            ["not set up yet", "The cloud's reader is not configured (for GCP, the export table)."],
            ["could not read", "The cloud returned another error; the message is shown."],
        ]),
        ul(["The cloud lags real usage by up to about 48 hours, so today and yesterday are provisional (marked with an asterisk) and lower than the estimate. "
            "Compare the two only for complete days.",
            "Tax (for example GST) is billed separately and is not included.",
            "'Not shown' in the footnote means charges in regions where no account of that cloud is onboarded, or other monitoring charges CloudOps does not create.",
            "A member AWS account shows only its own charges; a management (payer) account's role shows every linked account."]),
        note("Troubleshooting: AccessDenied with the policy attached usually means the AWS account setting \"IAM user and role "
             "access to Billing information\" is off. Only the account root user can change it."),
    ], "What the cloud really billed for CloudOps's metric reads, and how to turn it on")
