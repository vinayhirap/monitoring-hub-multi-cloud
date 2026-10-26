"""Phase D: in-app Help Center content. Pure tests: no database, no cloud, no network."""
import glob
import itertools
import os
import re

import pytest

import app.help_content as hc
from app.help_content.blocks import fmt_interval

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCK_TYPES = {"heading", "paragraph", "list", "table", "note", "code", "link"}


def _permission_catalog():
    codes = set()
    for path in glob.glob(os.path.join(ROOT, "db", "migrations", "*.sql")):
        sql = re.sub(r"--[^\n]*", "", open(path, encoding="utf-8").read())
        for stmt in re.findall(r"INSERT\s+(?:IGNORE\s+)?INTO\s+permissions\b.*?;", sql, flags=re.S | re.I):
            codes |= set(re.findall(r"\(\s*'([a-z0-9_.]+)'\s*,", stmt))
    return codes


CATALOG = _permission_catalog()
SECTIONS = hc.all_sections()


def test_catalog_parsed():
    assert {"alerts.view", "metrics.view", "alerts.configure", "accounts.onboard", "users.view", "system.admin"} <= CATALOG


def test_every_section_has_required_fields():
    ids = set()
    for s in SECTIONS:
        for k in ("id", "title", "group", "audience", "requires", "blocks"):
            assert k in s, (s.get("id"), k)
        assert s["id"] and s["title"] and s["group"] and s["blocks"], s["id"]
        assert s["audience"] in hc.AUDIENCE_RANK, s["id"]
        assert s["id"] not in ids, f"duplicate id {s['id']}"
        ids.add(s["id"])


def test_every_requires_code_exists_in_catalog():
    for s in SECTIONS:
        if s["requires"] is not None:
            assert s["requires"] in CATALOG, (s["id"], s["requires"])


def test_blocks_are_well_formed():
    for s in SECTIONS:
        for b in s["blocks"]:
            assert b["type"] in BLOCK_TYPES, (s["id"], b)
            if b["type"] in ("paragraph", "note", "code", "heading", "link"):
                assert b["text"].strip(), (s["id"], b)
            if b["type"] == "list":
                assert b["items"], s["id"]
            if b["type"] == "table":
                n = len(b["columns"])
                assert all(len(r) == n for r in b["rows"]), (s["id"], b["columns"])


def test_admin_sections_are_permission_gated():
    for s in SECTIONS:
        if s["audience"] == "admin":
            assert s["requires"] is not None, s["id"]
    by_id = {s["id"]: s for s in SECTIONS}
    for sid in ("cloud-actual-cost", "cost-jump-alert", "polling-config", "server-settings", "deploy-events",
                "accounts-credentials", "rbac-administration", "notifications"):
        assert by_id[sid]["audience"] == "admin"
    assert by_id["cloud-api-cost"]["requires"] == "usage.view"      # open to editors; admin-only panels are separate sections


def test_get_sections_never_returns_section_user_lacks():
    reqs = sorted({s["requires"] for s in SECTIONS if s["requires"]})
    cases = [set(), set(reqs), {"alerts.view"}, {"metrics.view", "alerts.view"}, {"system.admin"}]
    cases += [set(c) for n in (1, 2) for c in itertools.combinations(reqs[:12], n)]
    for perms in cases:
        for role in ("viewer", "editor", "admin", "custom-role", None):
            for s in hc.get_sections(perms, role):
                assert s["requires"] is None or s["requires"] in perms, (s["id"], perms)


def test_get_sections_includes_everything_when_all_permissions_held():
    reqs = {s["requires"] for s in SECTIONS if s["requires"]}
    got = hc.get_sections(reqs, "admin")
    assert {s["id"] for s in got} == {s["id"] for s in SECTIONS}


def test_open_sections_visible_to_a_user_with_no_permissions():
    got = {s["id"] for s in hc.get_sections(set(), "viewer")}
    assert {"overview", "glossary", "faq"} <= got
    assert "cloud-api-cost" not in got and "alerts" not in got


def test_get_sections_accepts_none_and_lists():
    assert hc.get_sections(None, None)
    assert hc.get_sections(["alerts.view"], "viewer")


def test_every_curated_service_has_a_section_with_every_metric():
    from app.aws.metric_catalog_data import CURATED as aws
    from app.providers.azure.metric_catalog_data import CURATED as az
    from app.providers.gcp.metric_catalog_data import CURATED as gcp
    by_id = {s["id"]: s for s in SECTIONS}
    for provider, cat in (("aws", aws), ("azure", az), ("gcp", gcp)):
        for key, (_d, _ns, _c, metrics) in cat.items():
            sec = by_id[f"svc-{provider}-{key}"]
            tbl = next(b for b in sec["blocks"] if b["type"] == "table")
            assert [r[0] for r in tbl["rows"]] == [m[0] for m in metrics], sec["id"]


def test_service_rows_match_polling_model():
    from app.collector import polling_model as pm
    sec = next(s for s in SECTIONS if s["id"] == "svc-aws-ec2")
    rows = {r[0]: r for r in next(b for b in sec["blocks"] if b["type"] == "table")["rows"]}
    assert rows["CPUUtilization"][5].startswith(fmt_interval(pm.TIER_SECONDS["standard"]))
    assert rows["StatusCheckFailed"][6].startswith("Free"), rows["StatusCheckFailed"]
    assert "static only" in rows["StatusCheckFailed"][7]


def test_derived_and_unsupported_metrics_are_not_labelled_paid():
    waf = next(s for s in SECTIONS if s["id"] == "svc-aws-wafv2")
    rows = {r[0]: r for r in next(b for b in waf["blocks"] if b["type"] == "table")["rows"]}
    assert rows["BlockRatio"][6].startswith("Free")
    ddb = next(s for s in SECTIONS if s["id"] == "svc-aws-dynamodb")
    rows = {r[0]: r for r in next(b for b in ddb["blocks"] if b["type"] == "table")["rows"]}
    if "UserErrors" in rows:
        assert rows["UserErrors"][6].startswith("Never queried")


def test_polling_section_numbers_come_from_the_model():
    from app.collector import polling_model as pm
    sec = next(s for s in SECTIONS if s["id"] == "metrics-polling")
    tiers = {r[0]: r[1] for r in next(b for b in sec["blocks"] if b["type"] == "table")["rows"]}
    for tier, seconds in pm.TIER_SECONDS.items():
        assert tiers[tier] == fmt_interval(seconds)


def test_no_five_minute_metric_documented_in_the_critical_tier():
    from app.collector import polling_model as pm
    for m in pm.AWS_CORE_METRICS:
        if m.tier == "critical":
            assert m.period_sec <= 60, m


def test_certificate_expiry_is_static_only():
    from app import threshold_defaults as td
    for name in ("DaysToExpiry", "SecondsUntilKeyMaterialExpiration"):
        assert td.is_static_only_metric(name)
    sec = next(s for s in SECTIONS if s["id"] == "svc-aws-certificatemanager")
    rows = {r[0]: r for r in next(b for b in sec["blocks"] if b["type"] == "table")["rows"]}
    assert "static only" in rows["DaysToExpiry"][7]


def _all_text(section):
    parts = [section["title"]]
    for b in section["blocks"]:
        if "text" in b:
            parts.append(b["text"])
        parts += b.get("items", [])
        parts += b.get("columns", [])
        for r in b.get("rows", []):
            parts += r
    return " ".join(str(x) for x in parts)


def test_no_prices_stated():
    for s in SECTIONS:
        assert "$" not in _all_text(s), s["id"]


def test_no_http_to_https_redirect_control_documented():
    syn = next(s for s in SECTIONS if s["id"] == "synthetic-checks")
    assert "redirect" not in _all_text(syn).lower()


def test_internal_links_resolve():
    ids = {s["id"] for s in SECTIONS}
    app_jsx = open(os.path.join(ROOT, "frontend", "src", "App.jsx"), encoding="utf-8").read()
    routes = set(re.findall(r'path="([^"]+)"', app_jsx))
    for s in SECTIONS:
        for b in s["blocks"]:
            if b["type"] != "link":
                continue
            href = b["href"]
            if href.startswith("#"):
                assert href[1:] in ids, (s["id"], href)
            elif href.startswith("/"):
                assert href.lstrip("/") in {r.lstrip("/") for r in routes}, (s["id"], href)
            else:
                assert href.startswith("https://"), (s["id"], href)


def test_coverage_table_points_at_real_sections():
    ids = {s["id"] for s in SECTIONS}
    for feature, sid in hc.COVERAGE.items():
        if "*" in sid:
            continue
        assert sid in ids, (feature, sid)


def test_unverified_blocks_are_flagged_and_listed():
    # The mechanism must keep working for future content, whether or not any block is flagged today.
    from app.help_content.blocks import p
    assert p("x", unverified=True)["unverified"] is True
    assert "unverified" not in p("x")
    listed = hc.unverified_blocks()
    assert all(b.get("unverified") is True for _sid, b in listed)


def _section_text(sid):
    s = next(x for x in SECTIONS if x["id"] == sid)
    parts = [s["title"], s.get("summary", "")]
    for b in s["blocks"]:
        if "text" in b:
            parts.append(b["text"])
        parts += b.get("items", [])
        for r in b.get("rows", []):
            parts += r
    return " ".join(parts)


def _source(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_previously_unverified_topics_are_now_documented_from_code():
    """The six notes that used to say 'not verified' now state what the code does. Each assertion below ties a
    statement in the guide to the source line that makes it true, so the guide cannot silently drift."""
    # incident correlation window and root-cause rule
    window = re.search(r"CORRELATION_WINDOW_MINUTES\s*=\s*(\d+)", _source("app/collector/correlate.py")).group(1)
    assert f"{window} minutes" in _section_text("incidents")
    assert "topology" in _section_text("incidents").lower()
    # notification channel filters
    sender = _source("app/notifications/sender.py")
    assert 'VALID_EVENTS = ("opened", "escalated")' in sender
    assert "minimum severity" in _section_text("notifications").lower()
    assert "not a filter" in _section_text("notifications")
    # encryption at rest
    assert "Fernet" in _source("app/credentials.py") and "Fernet" in _section_text("accounts-credentials")
    assert "CREDENTIAL_ENCRYPTION_KEY" in _section_text("accounts-credentials")
    # deny beats grant
    assert "DENIED. Nothing overrides a deny" in _source("app/auth/rbac.py")
    assert "deny" in _section_text("access-control").lower() and "wins" in _section_text("access-control").lower()
    # priority routing is a flag nobody reads yet
    users = [p for p in glob.glob(os.path.join(ROOT, "app", "**", "*.py"), recursive=True)
             if "routing_enabled(" in open(p, encoding="utf-8").read() and not p.endswith("priority.py")]
    assert not users, f"routing is now used by {users}: update the Priority help text"
    assert "reserved for a later release" in _section_text("priority-routing")
    # one AWS region per account
    assert 'region = account.get("default_region")' in _source("app/collector/discovery/runner.py")
    assert "One region per account" in _section_text("accounts-onboarding")


def test_content_is_json_serialisable():
    import json
    json.dumps({"sections": SECTIONS})


def test_host_agents_guide_is_tied_to_the_code():
    """The end-user host agent guide states numbers and names that the code enforces; keep them in lockstep."""
    from app.hostagents import settings as ha
    text = _section_text("host-agents")
    sec = next(x for x in SECTIONS if x["id"] == "host-agents")
    assert sec["requires"] == "hostagents.view"
    # stale window and the offline multiple
    watchdog = _source("app/collector/host_agent_watchdog.py")
    factor = int(re.search(r"OFFLINE_FACTOR\s*=\s*(\d+)", watchdog).group(1))
    from app.help_content import host_agents as page
    assert page.OFFLINE_FACTOR == factor
    assert fmt_interval(ha.stale_seconds()) in text and fmt_interval(ha.stale_seconds() * factor) in text
    # interval limits the config form enforces
    assert f"{ha.INTERVAL_MIN_SECONDS} to {ha.INTERVAL_MAX_SECONDS} seconds" in text
    # names the installer really uses (the uninstall steps and troubleshooting depend on them)
    installer = _source("agent/install.sh")
    for needle in ("SVC_USER=cloudops-agent", "BIN=$BIN_DIR/cloudops-agent", "ETC_DIR=/etc/cloudops-agent",
                   "DATA_DIR=/var/lib/cloudops-agent", "UNIT=/etc/systemd/system/cloudops-agent.service"):
        assert needle in installer, needle
    for shown in ("systemctl disable --now cloudops-agent", "/usr/local/bin/cloudops-agent",
                  "/etc/cloudops-agent", "/var/lib/cloudops-agent"):
        assert shown in " ".join(b.get("text", "") for b in sec["blocks"] if b["type"] == "code")
    # the shipped installers accept the flags the guide shows
    assert "--base-url" in _source("agent/install-macos.sh") and "$BaseUrl" in _source("agent/install.ps1")
    # the dialog command and the hub's download path agree with nginx
    assert "/dl/host-agent/" in _source("deploy/nginx.conf")
    assert 'DOWNLOAD_PATH = "/dl/host-agent"' in _source("frontend/src/utils/hostAgentsView.js")
    # every reading named in the guide exists in the ingest metric map
    from app.hostagents.metric_map import METRICS
    assert len(METRICS) == 15


def test_cloud_cost_sections_match_the_page_permissions():
    """The Cloud Cost page is open to usage.view (editor, admin); the billed-cost panel and the cost-jump alert need usage.manage."""
    by_id = {s["id"]: s for s in SECTIONS}
    assert by_id["cloud-api-cost"]["requires"] == "usage.view"
    for sid in ("cloud-actual-cost", "cost-jump-alert"):
        assert by_id[sid]["requires"] == "usage.manage", sid
    layout = _source("frontend/src/components/Layout.jsx")
    assert 'label: "Cloud Cost"' in layout and 'perm: "usage.view"' in layout
    api = _source("app/api/usage.py")
    assert 'require_permission("usage.manage")' in api


def test_platform_sections_are_tied_to_the_code():
    from app import cost_jump_settings as cjs
    from app.hostagents import settings as ha
    jump = _section_text("cost-jump-alert")
    assert f"{cjs.MAX_RECIPIENT_USERS}" in jump and f"{cjs.MAX_RECIPIENT_EMAILS}" in jump
    assert "30" in jump and "0.1" in jump                      # built-in percent and noise floor
    alert = _source("app/collector/cost_jump_alert.py")
    assert 'METRIC = "cloud_cost_jump"' in alert and "MIN_BASELINE_DAYS = 5" in alert and "WINDOW_DAYS = 7" in alert
    assert "PROVISIONAL_DAYS = 2" in alert
    # sign-in numbers
    limiter = _source("app/auth/rate_limit.py")
    assert '"LOGIN_RATE_LIMIT_PER_IP", 20' in limiter and '"LOGIN_RATE_LIMIT_PER_USERNAME", 10' in limiter
    assert '"FORGOT_PASSWORD_RATE_LIMIT_PER_IP", 5' in limiter
    assert "RESET_TOKEN_TTL_MINUTES = 30" in _source("app/api/auth.py")
    assert "COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60" in _source("app/auth/deps.py")
    # explain / RCA / deploy webhook
    assert "DEPLOY_LOOKBACK_MINUTES = 45" in _source("app/collector/rca.py")
    assert "45 minutes" in _section_text("explain-rca-ai") and "45 minutes" in _section_text("deploy-events")
    assert "_DEFAULT_WAIT_SECONDS = 8" in _source("app/llm/rca_report.py")
    assert 'WEBHOOK_RATE_LIMIT = 60' in _source("app/api/webhooks.py")
    assert 'alias="X-Webhook-Token"' in _source("app/api/webhooks.py")
    assert 'os.getenv("ANOMALY_ENABLED", "true")' in _source("app/collector/multivariate_anomaly.py")
    assert 'os.getenv("LLM_SUMMARY_ENABLED", "false")' in _source("app/llm/summarizer.py")
    # settings reference
    ref = _section_text("server-settings")
    assert str(ha.stale_seconds()) in ref and str(ha.ingest_rate_per_min()) in ref
    assert "DEFAULT_HOST_AGENT_CREDENTIAL_RETENTION_DAYS = 90" in _source("app/collector/retention.py")
    assert "DEFAULT_HOST_AGENT_CREDENTIAL_RETENTION_DAYS" in _source("app/collector/retention.py")


def test_host_agent_readings_in_settings_match_the_defaults():
    from app.threshold_defaults import DEFAULT_THRESHOLDS
    import ast
    text = _section_text("host-agents")
    tree = ast.parse(_source("app/hostagents/catalog.py"))      # parsed, not imported: the module opens a database pool
    catalog = next(n.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "CATALOG")
    assert len(catalog.keys) == 13 and "all 13 host readings" in text
    for name, shown in (("host_cpu_percent", "85 / 95"), ("host_swap_used_percent", "50 / 80"),
                        ("host_disk_inodes_used_percent", "80 / 90"), ("host_proc_count", "600 / 1000"),
                        ("host_load1", "4 / 8")):
        w, c, _cmp = DEFAULT_THRESHOLDS[name]
        assert shown == f"{w:g} / {c:g}", name
        assert shown in text, name
    assert DEFAULT_THRESHOLDS["host_mem_available_bytes"][:2] == (524288000, 209715200)
    assert DEFAULT_THRESHOLDS["host_uptime_seconds"][:2] == (300, 60)
    assert "Add service" in text and "OS-internal" in text


def test_alert_tabs_documented_match_the_api():
    import re as _re
    tabs = _re.search(r"_TABS = \(([^)]*)\)", _source("app/api/alerts.py")).group(1)
    names = {t.strip().strip('"') for t in tabs.split(",")}
    assert names == {"all", "active", "stale", "critical", "attention", "tuning", "acknowledged", "resolved", "suppressed"}
    text = _section_text("alerts")
    for label in ("Active", "Critical", "Needs Attention", "Auto-tuning", "Stale", "Acknowledged", "Muted / Maint.", "Resolved", "All"):
        assert label in text, label
