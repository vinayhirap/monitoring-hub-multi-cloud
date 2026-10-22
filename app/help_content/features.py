# app/help_content/features.py
"""Feature sections: incidents, escalation, notifications, SLOs, synthetic, WAF, security, topology, reports,
audit trail, search, status page, maintenance, settings, access control, glossary, FAQ."""
from .blocks import h, p, ul, table, note, code, link, section

ALERTS_GROUP = "Alerts and incidents"
MON = "Monitoring"
ADMIN = "Administration"


def incidents():
    return section("incidents", "Incidents", ALERTS_GROUP, "viewer", "incidents.view", [
        p("Incidents are created by the system, not by users: related alerts on an account's resources are correlated "
          "into one incident. The Incidents page of an account also shows per-resource health scores and capacity "
          "forecasts."),
        ul(["Incident detail (including a postmortem) opens from the incident list; viewing needs incidents.view.",
            "Capacity forecast: a linear trend fitted over the last 14 days of a capacity metric, cached for about 2 minutes.",
            "Scope: you only see incidents for accounts you have access to."]),
        h("How alerts are grouped into an incident"),
        ul(["Correlation runs every 15 minutes and only looks at active alerts that are not already in an active incident.",
            "Two alerts are grouped when their resources are linked by a topology relationship (either direction, "
            "automatic or manual) and they started within 30 minutes of each other. Example: a load balancer breach and "
            "the breaches of its target instances become one incident.",
            "An alert joins an existing open incident when its resource is related to a resource already in it and it "
            "falls within 30 minutes of the incident start.",
            "Matching never crosses accounts: an incident only contains alerts from one account.",
            "A breach with no related active alert stays a normal alert; it does not become an incident.",
            "The incident takes the worst severity of its alerts. The title and start time come from the earliest "
            "breach, which is treated as the probable root; a probable cause is ranked for each new incident.",
            "An incident resolves on its own once every alert in it is resolved. An acknowledged alert still counts "
            "as open."]),
    ], "Correlated incidents, health scores, forecasts")


def escalation():
    return section("escalation", "Escalation policies", ALERTS_GROUP, "editor", "escalation.view", [
        p("An alert that is not acknowledged within the policy's SLA is reassigned to the target group and its members "
          "are emailed (when SMTP is configured)."),
        ul(["View: escalation.view. Create, edit, delete: escalation.manage.",
            "A policy belongs to one account, or has no account: that is the organisation-wide fallback applying to every "
            "account without its own policy. Only an admin may create, change or delete the fallback.",
            "You can only manage policies of accounts you have access to."]),
        note("Troubleshooting: no escalation email means SMTP is not configured on the server, or the target group has no members with an email."),
    ], "Who is paged when an alert is not acknowledged")


def notifications():
    return section("notifications", "Notifications", ALERTS_GROUP, "admin", "notifications.manage", [
        p("Notification channels deliver alert messages. Manage them in Settings, Notification channels "
          "(notifications.manage, admin by default)."),
        table(["Type", "Target", "Rules"], [
            ["email", "1 to 20 comma-separated addresses", "Needs SMTP configured on the server"],
            ["slack", "Incoming webhook URL", "Full https:// URL, no credentials in the URL"],
            ["teams", "Incoming webhook URL", "Same"],
            ["webhook", "Any https:// URL", "Same; loopback, private and metadata addresses are rejected"],
        ]),
        ul(["The URL is a secret: it is accepted on save, never shown again (only the host), and not written to the audit log.",
            "Edit a channel and leave the target empty to keep the stored secret.",
            "Test sends one message now and reports the real outcome. The delivery log keeps the last 100 attempts.",
            "A message reads like '[CloudOps] Alert opened - P2 WARNING: metric on resource (account)' with value, threshold and a link to Alerts. The P-level appears when priorities are on."]),
        p("Maintenance windows suppress paging while active; alerts are still recorded."),
        h("Which alerts a channel receives"),
        ul(["Minimum severity: WARNING (receives warning and critical) or CRITICAL (critical only). The default is CRITICAL.",
            "Events: opened (a new alert) and escalated (an unacknowledged alert was escalated). A channel can take either "
            "or both.",
            "Account: a channel can be tied to one account, or left without an account to receive every account.",
            "Silenced alerts send nothing.",
            "Priority (P1-P4) is shown in the message when priorities are on, but it is not a filter: a channel cannot "
            "be limited to P1 only."]),
    ], "Email, Slack, Teams and webhook channels")


def slos():
    return section("slos", "SLOs and error budget", MON, "viewer", "slo.view", [
        p("An SLO is a target such as 99.9% over 30 days. The error budget is the 'bad time' that target allows (99.9% over "
          "30 days is about 43 minutes). The page shows how much has been used."),
        table(["Mode", "Bad time is..."], [
            ["Synthetic check", "failed probes of the chosen synthetic check"],
            ["Resource (optionally one metric)", "total time CRITICAL alerts were open on that resource / metric in the window"],
        ]),
        ul(["Set exactly one mode per SLO.",
            "Target: above 0 and up to 100 (percent). Window: 1 to 365 days (default 30).",
            "View: slo.view. Create, edit, delete: slo.manage.",
            "Budgets are computed when the page is read, from alerts and probe results; nothing is stored in advance."]),
    ], "Targets and how much error budget is used")


def synthetic():
    return section("synthetic-checks", "Synthetic checks", MON, "viewer", "synthetic.view", [
        p("A synthetic check is an active probe run from CloudOps against a target on a schedule. A failing check becomes a "
          "normal alert, so correlation, escalation and notifications work as for any other alert."),
        h("Settings of a check"),
        table(["Field", "Rule", "Default"], [
            ["Type", "http, https, tcp or dns (a new https:// URL must use the https type)", "http"],
            ["Target", "URL, host:port or name; loopback, private and metadata addresses are rejected by default", "-"],
            ["Interval", "60 s to 24 h", "300 s"],
            ["Timeout", "1 to 60 s", "10 s"],
            ["Failures before it counts as down", "1 to 100 in a row", "2"],
            ["Expected status code", "100 to 599", "none"],
            ["Expected keyword", "up to 255 characters, searched in the first 1 MB of the body", "none"],
            ["Environment", "text up to 50 characters", "prod"],
            ["Certificate warning / critical days (https only)", "warning 2-365, critical 1-364, critical smaller than warning, set both or neither", "30 / 7"],
            ["Contacts", "up to 20 email addresses, each flagged for outage and / or certificate mail", "none"],
        ]),
        h("Reading the list"),
        ul(["Up / Down: the stored status.",
            "Failing n/N: n probes in a row have failed out of the N needed. The status only flips to Down at N, so in between "
            "the stored status can still say Up.",
            "Editing the target, status code, keyword or timeout restarts the failure streak."]),
        h("TLS certificates"),
        p("An https check records the certificate's days left, expiry date, subject and issuer. It raises its own alert "
          "(metric synthetic_cert_expiry) from the check's warning and critical days. The limits are the numbers you set, "
          "not a learned band."),
        ul(["View: synthetic.view. Create, edit, delete: synthetic.manage. Run on demand: synthetic.run.",
            "Probe results are kept 30 days; at most 100 checks are probed per cycle (the rest on the next cycle).",
            "A check can be the measure behind an SLO."]),
        note("Troubleshooting: Down but the site works from your laptop. The probe runs from the CloudOps server, so a firewall, "
             "proxy or private-network rule between it and the target is the usual cause."),
    ], "HTTP, HTTPS, TCP and DNS probes, TLS expiry")


def waf():
    return section("waf-shield-guardduty", "WAF, Shield and GuardDuty", MON, "viewer", "metrics.view", [
        p("WAF: AWS WAFv2 has its own detail page under an account's services. It shows allowed, blocked, counted, passed, "
          "CAPTCHA and challenge counts and a block ratio, and can show the rules that blocked or counted requests."),
        ul(["By default WAF metrics are polled daily because that was the original placement. Turn on WAF_FAST_POLLING_ENABLED "
            "for 5-minute polling at 1-minute resolution, and WAF_PER_RULE_ENABLED for per-rule series (admin setting).",
            "BlockedRequests starts as a placeholder (anomaly-only) threshold: scanner noise on an internet-facing ACL does not page; "
            "a sustained spike against its own baseline does.",
            "BlockRatio is computed locally from the counts and costs nothing."]),
        p("Shield: AWS Shield Advanced is listed in the directory catalog only; there is no Shield page and no curated metric list."),
        p("GuardDuty: CloudOps has no dedicated GuardDuty view. GuardDuty log groups and functions can appear as ordinary resources."),
        link("WAFv2 metrics, units and tiers", "#svc-aws-wafv2"),
    ], "WAFv2 page; what exists for Shield and GuardDuty")


def security():
    return section("security-findings", "Security findings", MON, "viewer", "security.view", [
        p("Findings are generated by the system, checked hourly for every onboarded account, and read-only in the page. "
          "Filter by account; each finding links to the right cloud console."),
        table(["Cloud", "Checks"], [
            ["AWS", "public S3 bucket, security group open to the world, unattached EBS volume, unencrypted EBS volume, IAM user without MFA, stale IAM access key"],
            ["Azure", "NSG open to the world, storage public access, storage insecure transport"],
            ["GCP", "firewall open to the world, public GCS bucket"],
        ]),
        note("Checks use reader-level access only; a check that the role may not read is skipped for that account, not failed."),
    ], "What the hourly security checks look for")


def topology():
    return section("topology", "Topology", MON, "viewer", "resources.view", [
        p("The dependency graph of one account's resources, grouped by category."),
        ul(["Automatic edges come from ALB target health that CloudOps already reads each cycle.",
            "Manual edges are added or removed by users with topology.manage, for dependencies no cloud API reveals.",
            "An edge whose endpoint is not a known resource is drawn as a missing resource: discovery never reported it."]),
        p("Viewing the graph needs resources.view; the Topology page route has no separate permission check."),
    ], "Resource dependency graph")


def reports():
    return section("reports", "Reports", "Reports", "viewer", "reports.view", [
        p("Generate PDF operations reports for an account, or for a client across all its accounts, then download or email them. "
          "The page appears only when the reports feature is on for the installation."),
        table(["Report type", "Title"], [["WEEKLY", "Weekly Operations Report"], ["MONTHLY", "Monthly Operations Review"],
                                          ["QUARTERLY", "Quarterly Operations Review"], ["CUSTOM", "Operations Report"]]),
        ul(["Permissions: reports.view (list), reports.generate, reports.download, reports.email.",
            "A client-scope report is titled Client Report / Client Review.",
            "Email needs SMTP configured on the server; without it the page says an admin must set it up."]),
    ], "Generate, download and email PDF reports")


def audit():
    return section("compliance-audit", "Compliance (audit trail)", "Reports", "viewer", "audit.view", [
        p("The Compliance page is the full activity history of admin and viewer actions. Reading it needs audit.view; "
          "downloading the trail needs audit.export."),
        note("Do not confuse it with tag compliance, which is a tab on the Environments page."),
    ], "Audit trail of user actions")


def search():
    return section("smart-search", "Smart Search and plain-English queries", "Intelligence", "viewer", "search.query", [
        p("Type a question in plain English and get matching alerts. Examples shown by the page: 'critical alerts last hour', "
          "'warning ec2 alerts today', 'disk alerts this week'."),
        ul(["It is a rule-based parser, not an AI model: it recognises severity, status, resource types from your own "
            "account, relative time phrases, and treats leftover words as a text match on resource name or id.",
            "Results respect your account access."]),
        note("If nothing matches, remove words: every unrecognised word narrows the text match."),
    ], "Natural-language alert search")


def status_page():
    return section("status-page", "Status page", ALERTS_GROUP, "editor", "status_page.manage", [
        p("A public page at /status that needs no sign-in. You choose a public name for each service and map it to the real "
          "resources behind it; only the name and a live status are ever shown, never resource ids, account names or alert details."),
        ul(["At most 100 resources per component.",
            "The public endpoint is cached for about 30 seconds, so a change can take that long to appear.",
            "Recent events from the last 24 hours (up to 10) are shown."]),
        link("Open the public page", "/status"),
    ], "Public service status")


def maintenance():
    return section("maintenance-windows", "Maintenance windows", MON, "viewer", "maintenance.view", [
        p("Silence a resource, and what depends on it, for planned work. Alerts still record but no page or email fires while "
          "a window is active."),
        ul(["View: maintenance.view. Create, edit, delete: maintenance.manage.",
            "A window can last at most 31 days. Times are stored in UTC; enter them with the right time zone.",
            "Silencing is applied by the background job on its 2-minute tick, so it starts within about 2 minutes of the start time."]),
        note("Troubleshooting: paged during a window means it had not started yet, or the resource is not covered by it."),
    ], "Planned silencing")


def settings():
    return section("settings", "Settings", ADMIN, "editor", "alerts.configure", [
        p("Open to admin and editor roles. Sections on the page:"),
        table(["Section", "What it does"], [
            ["Accounts and regions", "Which accounts and regions are monitored"],
            ["Metrics to Monitor", "Choose metrics per service; Discover lists what an account publishes; Apply Default Template selects the recommended set"],
            ["Metric Thresholds", "Warning / critical lines per metric (see Thresholds); Seed defaults"],
            ["Threshold coverage", "Which monitored metrics have usable thresholds"],
            ["Notification channels", "See Notifications (admin)"],
        ]),
        link("Thresholds", "#thresholds"),
        note("Settings that need a server restart live in the server .env, not on this page (see Polling settings).",),
    ], "Metrics to monitor, thresholds, channels")


def access():
    return section("access-control", "Access control and RBAC", ADMIN, "viewer", "users.view", [
        p("Access = a role (what you may do) plus a scope (which accounts, regions and resources you may see)."),
        table(["Role", "Typical use"], [
            ["admin", "everything, including security-boundary settings"],
            ["editor", "operational changes within scope: acknowledge, thresholds, escalation, synthetic checks, webhooks"],
            ["viewer", "read-only"],
        ]),
        p("Custom roles can also be created (roles.create). Each role is a set of permission codes such as alerts.view, "
          "metrics.view, alerts.configure, accounts.onboard, users.view, system.admin."),
        h("Tabs of Access Control"),
        table(["Tab", "Needs", "Purpose"], [
            ["Users", "users.view", "People and their roles"],
            ["Groups", "groups.view", "An L1/L2/L3 hierarchy with account / region policies and members"],
            ["Roles & permissions", "roles.view", "Permission sets of roles"],
            ["Scopes & overrides", "rbac.scope.view, rbac.binding.view or rbac.override.manage", "Reusable scopes, role bindings, explicit denies"],
            ["Access reviews", "rbac.review.conduct", "Attest or revoke access"],
        ]),
        ul(["Groups: a user inherits the policies of the groups they belong to; group structure is admin-managed.",
            "Deny overrides (rbac.override.manage) are explicit denies of a permission.",
            "Hiding a page is not authorization: every API call checks the permission and the account scope."]),
        link("Route-by-route permission list: docs/RBAC_MATRIX.md in the repository", "https://github.com/vinayhirap/monitoring-hub-multi-cloud/blob/main/docs/RBAC_MATRIX.md"),
        h("Deny always wins"),
        ul(["Order of decision: 1) an explicit deny that matches the permission and covers the target blocks it, "
            "2) otherwise any role binding that grants the permission and covers the target allows it, "
            "3) otherwise it is refused.",
            "A deny beats every grant, even a binding that covers all accounts. It works like an explicit deny in AWS IAM.",
            "An unscoped deny (applies everywhere) removes the permission everywhere and the page is hidden too. "
            "A scoped deny removes only the accounts, regions or services it covers.",
            "The built-in admin role passes the page and route permission check regardless of a deny; row-level checks inside data endpoints still apply a deny."]),
    ], "Roles, permissions, groups, scopes")


def rbac_admin():
    return section("rbac-administration", "RBAC administration (admin)", ADMIN, "admin", "rbac.override.manage", [
        p("Permissions that change the security boundary stay admin-only: rbac.* codes, sso.manage, accounts.delete, "
          "accounts.credentials.manage."),
        ul(["rbac.scope.manage: create and edit reusable access scopes.",
            "rbac.binding.manage: grant or revoke a role at a scope.",
            "rbac.override.manage: create explicit denies.",
            "rbac.review.conduct: attest or revoke access during a review.",
            "roles.create / roles.update / roles.delete: custom roles.",
            "audit.view and audit.export: read and download the audit trail."]),
    ], "Permission codes that change who can do what")


def glossary():
    return section("glossary", "Glossary", "Reference", "viewer", None, [
        table(["Term", "Meaning"], [
            ["Tier", "How often a metric is collected: critical 2 min, standard 5, low 15, extended (60 min; Azure 15), daily."],
            ["Critical-class", "A metric evaluated for alerts every 2 minutes."],
            ["Stale", "No new point within the metric's allowed age (see Metrics and polling)."],
            ["No data", "The cloud returned no point in the window."],
            ["Static / dynamic threshold", "Typed lines versus a learned band around the metric's own baseline."],
            ["Placeholder threshold", "1,000,000 / 5,000,000 '>': alert only on a sustained anomaly."],
            ["Environment", "prod, uat or dev, decided as described under Environments and tagging."],
            ["Priority", "P1 to P4, from environment and severity."],
            ["Error budget", "The bad time an SLO target allows."],
            ["Maintenance window", "A planned period in which paging is silenced."],
        ]),
    ], "Terms used in CloudOps")


def faq():
    return section("faq", "FAQ", "Reference", "viewer", None, [
        h("Why is my alert Info and not Warning?"),
        p("The resource is UAT or Dev. Set its Environment on the Environments page if it is really Production."),
        h("Why is a chart a few minutes behind?"),
        p("The cloud publishes metrics late and CloudOps polls on a tier. See Metrics and polling."),
        h("Why does a resource say Needs classification?"),
        p("No tag, name token, attached parent, account default or manual choice placed it. It is alerted like production until then."),
        h("I cannot see a page that a colleague can."),
        p("Your role or permissions differ. Ask an administrator."),
        h("An alert did not fire on a bad-looking metric."),
        p("See the troubleshooting note under Alerts."),
    ], "Common questions")
