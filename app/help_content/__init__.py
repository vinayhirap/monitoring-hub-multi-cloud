# app/help_content/__init__.py
"""Structured content for the in-app Help Center.

get_sections(user_permissions, role) is a PURE function: it returns the sections that user may see, in display
order, as plain dicts {id, title, group, audience, requires, summary, blocks}. Phase E exposes it as
GET /api/help/sections -> {"sections": [...]}.

Visibility rule
  * a section with `requires` is visible only to a user holding that permission code;
  * a section with requires=None is visible to everyone whose role rank reaches its `audience`
    (viewer < editor < admin; an unknown custom role ranks as viewer).
`audience` is who the section is written for; the permission is what actually protects it.

Content is built from code where a fact is a constant (metric catalogs, polling tiers, defaults) so it cannot drift.
Anything not confirmed in code is a block with "unverified": true (see unverified_blocks()).
"""
from functools import lru_cache

from . import concepts, extras, features, host_agents
from .services import service_sections, directory_section

AUDIENCE_RANK = {"viewer": 1, "editor": 2, "admin": 3}

# feature -> section id (used by tests and by the coverage table in the delivery notes)
COVERAGE = {
    "Overview, navigation": "overview",
    "Account onboarding (AWS, Azure, GCP)": "accounts-onboarding",
    "Credentials, rotation, offboarding": "accounts-credentials",
    "Environments, tagging, tag sync, Needs classification": "environments-tagging",
    "Priority P1-P4": "priority-routing",
    "Metrics tiers, free vs paid, stale / no data, ENV_POLLING": "metrics-polling",
    "Polling .env settings": "polling-config",
    "Thresholds (static, dynamic, placeholder)": "thresholds",
    "Alerts, severity, lifecycle, noise control": "alerts",
    "Incidents": "incidents",
    "Escalation policies": "escalation",
    "Notification channels": "notifications",
    "SLOs": "slos",
    "Synthetic checks": "synthetic-checks",
    "WAF / Shield / GuardDuty": "waf-shield-guardduty",
    "Security findings": "security-findings",
    "Host agents (install, add services, readings in Settings)": "host-agents",
    "Topology": "topology",
    "Reports": "reports",
    "Compliance (audit trail)": "compliance-audit",
    "Smart Search / NL query": "smart-search",
    "Status page": "status-page",
    "Maintenance windows": "maintenance-windows",
    "Settings": "settings",
    "Access control and RBAC": "access-control",
    "RBAC administration": "rbac-administration",
    "Cloud Cost page (metering, cost by polling tier)": "cloud-api-cost",
    "Billed cost: AWS, Azure, GCP": "cloud-actual-cost",
    "Cost-jump alert": "cost-jump-alert",
    "Signing in, password reset, SSO": "signin-security",
    "Explain, RCA report, AI assistance, anomaly detection": "explain-rca-ai",
    "Deployment events (CI/CD webhook), Operational Events": "deploy-events",
    "Server settings (.env) reference": "server-settings",
    "Glossary": "glossary",
    "FAQ": "faq",
    "Services (every cataloged service, 3 clouds)": "svc-* and services-directory",
}


@lru_cache(maxsize=1)
def all_sections():
    static = [
        concepts.overview(), extras.signin(), concepts.accounts(), concepts.accounts_admin(), concepts.environments(),
        concepts.priority(), concepts.metrics_polling(), concepts.thresholds(), concepts.alerts(),
        features.incidents(), features.escalation(), features.notifications(), extras.cost_jump_alert(), features.status_page(),
        features.slos(), features.synthetic(), features.maintenance(), features.waf(), features.security(),
        features.topology(), host_agents.host_agents(), features.search(), extras.explain_ai(), features.reports(), features.audit(), features.settings(),
        features.access(), features.rbac_admin(), concepts.polling_admin(), extras.server_settings(), extras.deploy_events(), concepts.api_cost(), concepts.api_actual_cost(),
    ]
    tail = [features.glossary(), features.faq()]
    return static + [directory_section()] + list(service_sections()) + tail


def _rank(role):
    return AUDIENCE_RANK.get(str(role or "").strip().lower(), 1)


def _visible(section, perms, role_rank):
    req = section["requires"]
    if req is not None:
        return req in perms
    return role_rank >= AUDIENCE_RANK[section["audience"]]


def get_sections(user_permissions, role):
    perms = set(user_permissions or ())
    rank = _rank(role)
    return [s for s in all_sections() if _visible(s, perms, rank)]


def unverified_blocks():
    """[(section_id, block)] for every block flagged unverified -- listed in the delivery report."""
    return [(s["id"], b) for s in all_sections() for b in s["blocks"] if b.get("unverified")]
