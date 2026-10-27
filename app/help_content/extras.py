# app/help_content/extras.py
"""Help sections added after the first Help Center release: the cost-jump alert, signing in (password reset, SSO),
Explain / RCA report / AI summaries / anomaly detection, deployment events (CI/CD webhook) and the .env settings reference.

Every number is read from the code that enforces it (imports below) or tied to a source line by
tests/test_help_content.py::test_platform_sections_are_tied_to_the_code. No section states a price.
"""
from app import cost_jump_settings as cjs
from app.hostagents import settings as ha

from .blocks import h, p, ul, table, note, code, link, section

ALERTS_GROUP = "Alerts and incidents"
ADMIN = "Administration"


def cost_jump_alert():
    lim = cjs.LIMITS
    pct = cjs.BUILTIN["threshold_pct"]
    rise = cjs.BUILTIN["min_increase_usd"]
    return section("cost-jump-alert", "Cost-jump alert", ALERTS_GROUP, "admin", "usage.manage", [
        p("The cost-jump alert warns you when the billed cost of CloudOps's own metric polling for an account is well above its recent "
          "average, for example after more resources are discovered or a polling tier is made faster. It is switched off until you turn it on."),
        link("Open Cloud Cost", "/api-cost"),
        h("What is compared"),
        ul([f"The latest settled day of billed CloudOps cost for one cloud account (the same figures the Billed cost panel shows, for AWS, Azure and GCP) "
            f"against the average of the 7 days before it.",
            f"It raises an alert when that day is more than {pct:g} percent above the average (you can change the percent) AND the rise is at least "
            f"{rise:g} US dollars a day (the noise floor, also changeable).",
            "Settled means at least 2 days old, because the cloud's billing data lags by about 48 hours. The alert is therefore 2 to 3 days "
            "behind the spend: it catches a step change in polling cost, it is not a real-time meter.",
            "At least 5 of the 7 baseline days must exist. With less data nothing is raised or cleared."]),
        h("The alert"),
        ul(["One alert per account, severity Warning, metric cloud_cost_jump. It appears on the Alerts page like any other alert, so "
            "priority, escalation, maintenance windows, the status page and notifications all apply.",
            "It resolves on its own once the latest settled day is back inside the limit, and resolves at once if you switch the account off.",
            "If cost data is missing the alert is left alone and simply goes stale.",
            "The check reads only CloudOps's own database, makes no cloud call and costs nothing. It runs about every 10 minutes, and a "
            "settings change takes effect within about ten minutes with no restart."]),
        h("Settings (Cloud Cost page, usage.manage)"),
        p("A table with one row for All accounts (first) and one per account, with the columns Alert (on / off / inherit), % above average, "
          "Min rise per day and In effect, plus Save per row. Leave a field blank to inherit."),
        ul([f"Percent above average: {lim['threshold_pct'][0]:g} to {lim['threshold_pct'][1]:g}. Minimum rise per day: {lim['min_increase_usd'][0]:g} or more.",
            "Each field uses the account's own value, else the All accounts value, else the server setting (COST_JUMP_ALERT_ENABLED, "
            "COST_JUMP_THRESHOLD_PCT, COST_JUMP_MIN_INCREASE_USD), else the built-in default (off, 30, 0.10). The In effect column says which one won.",
            "Reset all accounts to the default clears every per-account setting so all accounts follow the All accounts row. Recipients are not changed."]),
        h("Who is told when it opens"),
        ul(["The Alerts page, the bell and the pop-up, for everyone who can see the account.",
            "Every matching notification channel. A channel must be set to receive Warning: the default minimum is Critical only.",
            "The alert's own recipients, set at the bottom of the page for All accounts and per account. Pick people (CloudOps users with an email "
            f"address, up to {cjs.MAX_RECIPIENT_USERS}) and type other addresses (up to {cjs.MAX_RECIPIENT_EMAILS}, press Enter after each). "
            "An account is emailed at its own recipients plus the All accounts recipients.",
            "Send test email sends a test to everyone the chosen scope would reach (save your changes first).",
            "Nothing is sent when an alert resolves, is silenced, or is merely refreshed: a notification goes out only when an alert opens."]),
        note("Email needs SMTP configured on the server. Without SMTP_HOST the page warns that recipients cannot receive email yet; "
             "the alert still shows on the Alerts page.", "warn"),
    ], "Warn when CloudOps's billed cost jumps above its 7-day average")


def signin():
    return section("signin-security", "Signing in, password reset and SSO", "Getting started", "viewer", None, [
        p("You sign in with a username and password, or with single sign-on (SSO) when your administrator has turned it on. "
          "A session lasts 12 hours; signing out ends it on the server too, not only in your browser."),
        h("Password"),
        ul(["Change password is on the sign-in page: enter your current password and choose a new one.",
            "Forgot password on the sign-in page sends a reset link by email. The link is never shown on screen, it works for 30 minutes, "
            "and only one reset email is sent per account per minute. This needs SMTP configured on the server.",
            "The sign-in page always gives the same reply whether or not the username exists, so it cannot be used to find usernames."]),
        h("Limits against guessing"),
        table(["Action", "Limit", "Setting"], [
            ["Sign in, per IP address", "20 tries in 5 minutes", "LOGIN_RATE_LIMIT_PER_IP"],
            ["Sign in, per username", "10 tries in 5 minutes", "LOGIN_RATE_LIMIT_PER_USERNAME"],
            ["Forgot password, per IP address", "5 requests in 15 minutes", "FORGOT_PASSWORD_RATE_LIMIT_PER_IP"],
        ]),
        h("Single sign-on (SAML 2.0)"),
        ul(["Off until an administrator sets SSO_SAML_ENABLED=true and the identity provider settings (SSO_IDP_ENTITY_ID, SSO_IDP_SSO_URL, "
            "SSO_IDP_X509_CERT and the service provider URLs). Until then the Log in with SSO button explains that single sign-on is not set up here and "
            "you sign in with your username and password.",
            "Only sign-in that starts from CloudOps is accepted, and each response can be used once, so a captured response cannot be replayed.",
            "The identity provider's email address is matched to an existing active CloudOps account. If there is none the person is refused, unless "
            "SSO_AUTO_PROVISION=true; the account is then created with the role in SSO_DEFAULT_ROLE (viewer by default; only viewer or editor is "
            "allowed, never admin). A deactivated account, or an email that matches more than one active account, is refused.",
            "Service provider metadata is published for the identity provider's administrator."]),
        note("Local sign-in keeps working if SSO or its session store is unavailable."),
    ], "Passwords, reset links, rate limits and SAML single sign-on")


def explain_ai():
    return section("explain-rca-ai", "Explain, RCA report and AI assistance", "Intelligence", "viewer", "alerts.view", [
        p("CloudOps can explain why an alert probably happened and write an incident report for it. The facts always come from "
          "stored data. An AI model, when your administrator turns one on, only rephrases them."),
        h("Explain (on an alert)"),
        ul(["The explanation is built from real signals around the alert: CloudTrail events, changes made inside CloudOps, application "
            "deployments reported by your pipeline (see Deployment events), the metric's trend, flapping, related alerts and the topology.",
            "A deployment counts when it was recorded within 45 minutes before the alert started.",
            "For an incident a probable cause is ranked when the incident is created (see Incidents).",
            "Needs alerts.view."]),
        h("RCA report"),
        ul(["Use the RCA report (PDF) button on an alert, in the Alerts list or in the alert's investigation panel. The same report is available as Markdown from the API (the rca-report endpoint with format=md).",
            "The timeline, resource details, severity and duration come only from stored data. The Executive Summary and Recommendations "
            "can be written by the AI model; if it is off or fails, the same facts are shown as plain bullet points. A report is always produced.",
            "The AI text is generated in the background. A download waits a few seconds (LLM_RCA_WAIT_SECONDS, default 8) and otherwise returns "
            "the plain version at once; the next download of the same report has the AI version."]),
        h("AI assistance (admin setting)"),
        ul(["Off by default: set LLM_SUMMARY_ENABLED=true to turn it on.",
            "Provider: LLM_PROVIDER. The default, ollama, runs a model on the CloudOps server itself with no account and no per-call charge. "
            "The anthropic provider is hosted, charges per call, and is used only if it is chosen and ANTHROPIC_API_KEY is set.",
            "The model never sees raw database rows, is told not to add facts, and its text is discarded in favour of the plain text if it times out or looks unusable.",
            "AI-written text is the only part that can differ between two downloads of the same report."]),
        h("Anomaly detection"),
        ul(["Single-metric thresholds cannot see several metrics moving together. Every 15 minutes CloudOps scores each resource's current "
            "mix of readings against that resource's own history (an Isolation Forest model that runs on the CloudOps server, no cloud calls).",
            "An unusual combination becomes an alert named multivariate_anomaly, so correlation, health scores and escalation treat it like any other alert.",
            "On by default; set ANOMALY_ENABLED=false to switch it off. Overview shows these as Metric anomalies next to the Capacity forecast, "
            "marked as model output rather than a measurement."]),
    ], "How an alert is explained, the RCA report, optional AI wording, anomaly detection")


def deploy_events():
    return section("deploy-events", "Deployment events (CI/CD webhook)", ADMIN, "admin", "system.admin", [
        p("A code deploy is invisible to the cloud's own metrics. If your pipeline tells CloudOps when it deploys, an alert that starts soon "
          "afterwards is flagged in its explanation and RCA report as 'started shortly after a deployment' (within 45 minutes)."),
        h("Turn it on"),
        ul(["Set DEPLOY_WEBHOOK_TOKEN in the server .env for a shared token that may record a deployment for any account, and / or "
            "DEPLOY_WEBHOOK_TOKENS=3:tokenA,5:tokenB for per-account tokens (account id, a colon, the token) that can write only to their own account.",
            "With no token set the endpoint refuses every call (503); it never accepts unauthenticated writes.",
            "Send the token in the X-Webhook-Token header. A wrong or missing token gets 401; a per-account token used for another account gets 403."]),
        h("Call it at the end of a deploy step"),
        code("curl -X POST https://<hub>/api/webhooks/deploy \\\n"
             "  -H \"Content-Type: application/json\" \\\n"
             "  -H \"X-Webhook-Token: <token>\" \\\n"
             "  -d '{\"aws_account_id\": 3, \"service\": \"payment-api\", \"version\": \"<git-sha>\", \"actor\": \"<who>\"}'"),
        table(["Field", "Rule"], [
            ["aws_account_id", "Required. The CloudOps account id (a whole number that exists)."],
            ["resource_id", "Optional. The specific resource the deploy touched, up to 512 characters."],
            ["service", "Optional label, up to 200 characters."],
            ["version", "Optional, a version or commit id, up to 200 characters."],
            ["actor", "Optional, who or what triggered it, up to 200 characters."],
            ["description", "Optional free text such as a pull request title, up to 2000 characters."],
        ]),
        ul(["A successful call answers status recorded. At most 60 calls per minute are accepted from one IP address.",
            "Deployments are stored as operational events. Operational Events (/op-events, needs operations.view) lists them together with "
            "collector and discovery failures; it is not in the sidebar. An event tied to an account is visible only to people who can access that account."]),
    ], "Tell CloudOps when you deploy so alerts can be linked to it")


def server_settings():
    return section("server-settings", "Server settings (.env) reference", ADMIN, "admin", "system.admin", [
        p("These values live in the server .env, not in a page. Restart the service after changing one. Polling and priority settings are "
          "listed under Polling settings; the ones below belong to features described elsewhere in this guide."),
        link("Polling settings", "#polling-config"),
        h("Host agents"),
        table(["Setting", "Default", "Effect"], [
            ["HOST_AGENT_ENABLED", "off", "Turns the host agent feature on; every host agent route answers 404 while it is off"],
            ["HOST_AGENT_STALE_SECONDS", str(ha.stale_seconds()), "Silence before a server is Stale; Offline is 5 times this"],
            ["HOST_AGENT_PREFER_OVER_CWAGENT", "off", "Skip the paid CloudWatch agent query for memory and disk a live agent supplies"],
            ["HOST_AGENT_CREDENTIAL_RETENTION_DAYS", "90 (minimum 30; 0 off)", "Delete old revoked tokens and spent keys"],
            ["HOST_AGENT_INGEST_RATE_PER_MIN", str(ha.ingest_rate_per_min()), "Report requests accepted per minute"],
            ["HOST_AGENT_ENROLL_RATE_PER_HOUR", str(ha.enroll_rate_per_hour()), "Enrollment requests accepted per hour"],
            ["HOST_AGENT_MAX_BODY_BYTES", str(ha.max_body_bytes()), "Largest report accepted"],
        ]),
        h("Cost"),
        table(["Setting", "Default", "Effect"], [
            ["ACTUAL_COST_ENABLED", "off", "Reads billed cost from AWS, Azure and GCP (admin Billed cost panel). Leave off on DEV"],
            ["GCP_BILLING_EXPORT_TABLE", "unset", "project.dataset.table of the GCP billing export"],
            ["GCP_BILLING_JOB_PROJECT", "the account's own project", "Project that runs the BigQuery job"],
            ["AZURE_CLOUDOPS_METER_REGEX / GCP_CLOUDOPS_SKU_REGEX", "built-in pattern", "Which meter or SKU counts as CloudOps's metric reads"],
            ["COST_JUMP_ALERT_ENABLED", "off", "Cost-jump alert on or off (the Cloud Cost page overrides it)"],
            ["COST_JUMP_THRESHOLD_PCT / COST_JUMP_MIN_INCREASE_USD", "30 / 0.10", "Cost-jump percent and minimum daily rise"],
        ]),
        h("Sign-in and email"),
        table(["Setting", "Default", "Effect"], [
            ["SSO_SAML_ENABLED", "off", "SAML single sign-on"],
            ["SSO_AUTO_PROVISION / SSO_DEFAULT_ROLE", "off / viewer", "Create unknown people on first SSO sign-in, and the role they get"],
            ["LOGIN_RATE_LIMIT_PER_IP / _PER_USERNAME", "20 / 10 per 5 minutes", "Sign-in guessing limits"],
            ["SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_USE_TLS, MAIL_FROM", "unset", "Outgoing email for escalation, reports, password reset and cost-jump recipients"],
            ["CREDENTIAL_ENCRYPTION_KEY", "auto-created key file (development only)", "Encrypts stored cloud secrets; never change or lose it"],
        ]),
        h("Intelligence and integrations"),
        table(["Setting", "Default", "Effect"], [
            ["LLM_SUMMARY_ENABLED", "off", "AI wording of explanations and RCA reports"],
            ["LLM_PROVIDER", "ollama", "ollama (self-hosted) or anthropic (hosted, charged per call)"],
            ["LLM_RCA_WAIT_SECONDS", "8", "How long an RCA download waits for the AI text"],
            ["ANOMALY_ENABLED", "on", "Multivariate anomaly detection"],
            ["DEPLOY_WEBHOOK_TOKEN / DEPLOY_WEBHOOK_TOKENS", "unset", "Enable the deployment webhook"],
            ["REPORTS_ENABLED", "off", "Shows the Reports page and generates reports"],
            ["REPORT_RETENTION_DAYS", "365", "How long generated reports are kept"],
        ]),
        note("Settings that decide how often data is collected are in Polling settings; thresholds and notification channels are changed in Settings, not in .env."),
    ], "Environment variables for host agents, cost, sign-in, AI and integrations")
