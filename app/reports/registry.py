# app/reports/registry.py
"""
The section reports (cost, host agents, environments, security findings, SLOs): one place that says, for each scope_type,
which module builds the PDF and what a caller must be allowed to ask for. app/api/reports.py calls validate() before a job
is queued; app/reports/worker.py calls build() to render it. ACCOUNT / RESOURCE / INCIDENT / CLIENT stay in engine.py and
SYNTHETIC in synthetic_report.py, untouched.

Every section report asks for the permission of the section it reads (a report is another way to read that data), on top of
reports.generate and the account-scope check the API already applies.
"""
import importlib

from fastapi import HTTPException

from app.db import get_db_cursor

# scope_type -> (module, permission, needs an account). COST is org-wide: admin only, no account.
SECTION_REPORTS = {
    "COST": ("app.reports.cost_report", "usage.manage", False),
    "HOST": ("app.reports.host_report", "hostagents.view", True),
    "ENVIRONMENT": ("app.reports.environment_report", "environments.view", True),
    "SECURITY": ("app.reports.security_report", "security.view", True),
    "SLO": ("app.reports.slo_report", "slo.view", True),
}


def is_section(scope_type):
    return scope_type in SECTION_REPORTS


def needs_account(scope_type):
    return SECTION_REPORTS[scope_type][2]


def _module(scope_type):
    return importlib.import_module(SECTION_REPORTS[scope_type][0])


def _exists(sql, params, message):
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute(sql, params)
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=message)


def validate(scope_type, scope_id, account_id, user):
    """Raises HTTPException unless this caller may generate this section report. Returns the account_id to store
    (None for COST, which spans every account)."""
    from app.auth.permissions import has_permission
    _, perm, _ = SECTION_REPORTS[scope_type]
    if scope_type == "COST":
        # Billing totals are not filtered per account and a report without an account can only be downloaded by an admin
        # (reports.py::_load_report_or_404), so generating it is admin-only too, exactly like CLIENT.
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Cost reports cover every account and are admin-only")
    elif not has_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    mod = _module(scope_type)
    try:
        if scope_type == "COST":
            mod.parse_scope(scope_id)
            return None
        parsed = mod.parse_scope(scope_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    acct, item = parsed if isinstance(parsed, tuple) else (parsed, None)
    if acct != int(account_id):
        raise HTTPException(status_code=400, detail="scope_id must name the selected account")
    if scope_type == "HOST":
        from app.hostagents import settings
        if not settings.enabled():
            raise HTTPException(status_code=409, detail="Host agents are not enabled on this environment (HOST_AGENT_ENABLED)")
        if item is not None:
            _exists("SELECT id FROM host_agents WHERE id = %s AND aws_account_id = %s", (item, acct), "That host does not exist in this account")
    if scope_type == "SLO" and item is not None:
        _exists("SELECT id FROM slo_definitions WHERE id = %s AND aws_account_id = %s", (item, acct), "That SLO does not exist in this account")
    return account_id


def build(job):
    """-> (pdf_bytes, scope_label) for a queued job row."""
    return _module(job["scope_type"]).build_pdf(job)
