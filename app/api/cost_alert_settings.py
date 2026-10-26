# app/api/cost_alert_settings.py
"""
Cost-jump alert settings, for the Cloud cost page. Everything needs usage.manage (admin by default, assignable in RBAC).

  GET  /api/usage/cost-alert-settings                    all-accounts settings, every account with its own override and effective values
  PUT  /api/usage/cost-alert-settings/all-accounts       {enabled?, threshold_pct?, min_increase_usd?}   null = back to the environment/default
  PUT  /api/usage/cost-alert-settings/accounts           {account_ids: [id, ...] | "all", enabled?, threshold_pct?, min_increase_usd?}
                                                         null = inherit the all-accounts value ("all" with every field null clears every override)
  POST /api/usage/cost-alert-settings/test-email         {account_id?}  sends a test to the recipients that account would be told (null = all-accounts ones)

Recipients (notify_user_ids, notify_emails) are accepted by both PUTs. They are ADDITIVE: an account is told at its own plus the
all-accounts recipients. A chosen user must be active and have an email on file; anyone else is reached by typing the address.

Only accounts the caller may see (get_accessible_account_ids) can be read or changed. Every change is written to the audit
log. The settings are read by the collector every check (app/collector/cost_jump_alert.py), so a change takes effect within
about ten minutes with no restart.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app import cost_jump_settings as cjs
from app.audit import write_audit
from app.auth.authorization import get_accessible_account_ids, require_org_wide
from app.auth.permissions import require_permission
from app.db import get_connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/usage", tags=["Usage"])

MAX_TARGETS = 500
METRIC = "cloud_cost_jump"
RECIPIENTS_MIGRATION_NOTE = "Run the pending migrations (migrate.py apply --all-pending): 133 adds the alert recipients columns."
MIGRATION_NOTE = "Run the pending migrations (migrate.py apply --all-pending): 132 adds the cost alert settings table."


def _iso(v):
    if v is None or isinstance(v, str):
        return v
    s = v.isoformat()
    return s if v.tzinfo else s + "Z"


def build_view(settings, accounts, open_alert_accounts, users=(), smtp_configured=False):
    """Everything the settings panel shows. Pure. accounts: [{id, account_name, provider}] the caller may see;
    users: active CloudOps users [{id, username, email}] to pick recipients from."""
    glob = settings.get("global") or {}
    res = cjs.resolve(settings, None)
    env = cjs.env_defaults()
    rows = []
    for a in accounts:
        own = (settings.get("accounts") or {}).get(a["id"]) or {}
        r = cjs.resolve(settings, a["id"])
        rows.append({"id": a["id"], "name": a["account_name"], "provider": a["provider"],
                     "set": {f: own.get(f) for f in cjs.FIELDS + cjs.RECIPIENT_FIELDS},
                     "recipient_count": len(set(cjs.recipients(settings, a["id"])[0])) + len(cjs.recipients(settings, a["id"])[1]),
                     "effective": {f: r[f][0] for f in cjs.FIELDS}, "source": {f: r[f][1] for f in cjs.FIELDS},
                     "has_override": any(own.get(f) is not None for f in cjs.FIELDS) or bool(own.get("notify_user_ids") or own.get("notify_emails")),
                     "open_alert": a["id"] in open_alert_accounts,
                     "updated_by": own.get("updated_by"), "updated_at": _iso(own.get("updated_at"))})
    return {"all_accounts": {"set": {f: glob.get(f) for f in cjs.FIELDS + cjs.RECIPIENT_FIELDS},
                             "effective": {f: res[f][0] for f in cjs.FIELDS}, "source": {f: res[f][1] for f in cjs.FIELDS},
                             "updated_by": glob.get("updated_by"), "updated_at": _iso(glob.get("updated_at"))},
            "defaults": {"environment": {f: {"value": v, "source": s} for f, (v, s) in env.items()}, "builtin": cjs.BUILTIN},
            "limits": {f: list(v) for f, v in cjs.LIMITS.items()},
            "accounts": rows, "table_missing": bool(settings.get("table_missing")),
            "recipients_missing": bool(settings.get("recipients_missing")), "smtp_configured": bool(smtp_configured),
            "users": [{"id": u["id"], "username": u["username"], "email": (u.get("email") or "").strip() or None,
                       "has_email": bool((u.get("email") or "").strip())} for u in users],
            "limits_recipients": {"users": cjs.MAX_RECIPIENT_USERS, "emails": cjs.MAX_RECIPIENT_EMAILS},
            "notes": ["A change takes effect within about ten minutes, with no restart.",
                      "The alert compares the latest settled day of billed cost (two days behind) with the average of the 7 days before it, "
                      "so it is 2 to 3 days behind the spend.",
                      "Each field falls back: the account's own value, then the all-accounts value, then the server environment, then the built-in default.",
                      "Switching an account off resolves its open alert.",
                      "Recipients are added together: an account's alert goes to its own recipients plus the all-accounts recipients, "
                      "on top of any notification channel set to receive warnings.",
                      "An email is sent when an alert opens, not when it resolves."]}


def plan_targets(requested, visible_ids):
    """requested: 'all' or a list of account ids. visible_ids: ids of active accounts the caller may see.
    Returns the sorted list to change, or raises ValueError with a plain message."""
    if requested == "all":
        return sorted(visible_ids)
    if not isinstance(requested, list) or not requested:
        raise ValueError('account_ids must be a list of account ids, or "all".')
    if len(requested) > MAX_TARGETS:
        raise ValueError(f"At most {MAX_TARGETS} accounts per request.")
    ids = []
    for x in requested:
        if isinstance(x, bool) or not isinstance(x, int):
            raise ValueError("account_ids must be whole numbers.")
        ids.append(x)
    unknown = sorted(set(ids) - set(visible_ids))
    if unknown:
        raise ValueError(f"Unknown or not accessible account(s): {unknown}.")
    return sorted(set(ids))


def _read(cursor, current_user):
    settings = cjs.load(cursor)
    cursor.execute("SELECT id, account_name, provider FROM aws_accounts WHERE status = 'active' ORDER BY provider, account_name")
    accounts = cursor.fetchall()
    allowed = get_accessible_account_ids(current_user)
    if allowed is not None:
        accounts = [a for a in accounts if a["id"] in allowed]
    try:
        cursor.execute("SELECT DISTINCT aws_account_id FROM alerts WHERE metric_name = %s AND status IN ('active', 'acknowledged')", (METRIC,))
        open_ids = {r["aws_account_id"] for r in cursor.fetchall()}
    except Exception:
        logger.exception("cost-alert-settings: could not read open alerts")
        open_ids = set()
    users = []
    try:
        cursor.execute("SELECT id, username, email FROM users WHERE COALESCE(active, 1) = 1 ORDER BY username")
        users = cursor.fetchall()
    except Exception:
        logger.exception("cost-alert-settings: could not list users")
    smtp = False
    try:
        from app.email import mailer
        smtp = mailer.is_configured()
    except Exception:
        logger.exception("cost-alert-settings: could not check SMTP")
    return build_view(settings, accounts, open_ids, users, smtp), accounts


def _check_users(cursor, ids):
    """Every chosen user must exist, be active and have an email on file. Plain messages that say what to do instead."""
    cursor.execute(f"SELECT id, username, email, COALESCE(active, 1) AS active FROM users WHERE id IN ({', '.join(['%s'] * len(ids))})",
                   tuple(ids))
    found = {r["id"]: r for r in cursor.fetchall()}
    for uid in ids:
        u = found.get(uid)
        if not u:
            raise HTTPException(status_code=400, detail=f"User {uid} does not exist.")
        if not u["active"]:
            raise HTTPException(status_code=400, detail=f"User '{u['username']}' is inactive.")
        if not (u["email"] or "").strip():
            raise HTTPException(status_code=400, detail=f"User '{u['username']}' has no email on file. Add an email to the user, "
                                                        f"or type the address in directly.")


def _describe(values):
    return ", ".join(f"{k} {'inherit' if v is None else v}" for k, v in values.items())


@router.get("/cost-alert-settings")
def get_settings(current_user: dict = Depends(require_permission("usage.manage"))):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        view, _ = _read(cur, current_user)
        return view
    except Exception:
        logger.exception("cost-alert-settings read failed")
        raise HTTPException(status_code=500, detail="Could not read the cost alert settings")
    finally:
        cur.close()
        conn.close()


def _write(request, current_user, scope_keys, values, label, audit_extra):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        before = cjs.load(cur)
        if before.get("table_missing"):
            raise HTTPException(status_code=409, detail=MIGRATION_NOTE)
        supported = not before.get("recipients_missing")
        if not supported and any(f in values for f in cjs.RECIPIENT_FIELDS):
            raise HTTPException(status_code=409, detail=RECIPIENTS_MIGRATION_NOTE)
        if values.get("notify_user_ids"):
            _check_users(cur, values["notify_user_ids"])
        for key in scope_keys:
            cjs.save(cur, key, values, current_user["username"], recipients_supported=supported)
        conn.commit()
        view, _ = _read(cur, current_user)
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        logger.exception("cost-alert-settings write failed")
        raise HTTPException(status_code=500, detail="Could not save the cost alert settings")
    finally:
        cur.close()
        conn.close()
    write_audit(current_user["username"], "Cost alert settings changed", role=str(current_user.get("role", "")).upper(),
                payload={"detail": f"{label}: {_describe(values)}", "values": values, **audit_extra}, request=request)
    return view


@router.put("/cost-alert-settings/all-accounts")
def put_all_accounts(request: Request, payload: dict = Body(...),
                     current_user: dict = Depends(require_permission("usage.manage"))):
    require_org_wide(current_user, "The all-accounts setting")
    try:
        values = cjs.validate(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _write(request, current_user, [cjs.GLOBAL_KEY], values, "All accounts", {"scope": "all accounts"})


@router.put("/cost-alert-settings/accounts")
def put_accounts(request: Request, payload: dict = Body(...),
                 current_user: dict = Depends(require_permission("usage.manage"))):
    body = dict(payload or {})
    requested = body.pop("account_ids", None)
    try:
        values = cjs.validate(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _, visible = _read(cur, current_user)
    except Exception:
        logger.exception("cost-alert-settings: could not list accounts")
        raise HTTPException(status_code=500, detail="Could not read the accounts")
    finally:
        cur.close()
        conn.close()
    try:
        targets = plan_targets(requested, [a["id"] for a in visible])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not targets:
        raise HTTPException(status_code=400, detail="There are no accounts to change.")
    names = {a["id"]: a["account_name"] for a in visible}
    label = (f"{len(targets)} accounts" if len(targets) > 1 else f"Account {names.get(targets[0], targets[0])}")
    return _write(request, current_user, targets, values, label, {"scope": "accounts", "account_ids": targets})


@router.post("/cost-alert-settings/test-email")
def send_test_email(request: Request, payload: dict = Body(default={}),
                    current_user: dict = Depends(require_permission("usage.manage"))):
    """Sends one test email to the recipients an alert for this account (or, with no account, the all-accounts list) would reach."""
    account_id = (payload or {}).get("account_id")
    if account_id is None:
        require_org_wide(current_user, "The all-accounts recipient list")
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        settings = cjs.load(cur)
        _, visible = _read(cur, current_user)
    except Exception:
        logger.exception("cost-alert-settings: test email could not read settings")
        raise HTTPException(status_code=500, detail="Could not read the cost alert settings")
    finally:
        cur.close()
        conn.close()
    if account_id is not None:
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id not in {a["id"] for a in visible}:
            raise HTTPException(status_code=400, detail="Unknown or not accessible account.")
    users, emails = cjs.recipients(settings, account_id)
    if not users and not emails:
        raise HTTPException(status_code=400, detail="No recipients are set. Choose users or type addresses, save, then send the test.")
    from app.collector.cost_jump_alert import send_to_recipients
    message = {"headline": "[CloudOps] Test: cost-jump alert recipients",
               "text": "This is a test from CloudOps. If you can read it, you will receive an email when the cost-jump alert opens."}
    result = send_to_recipients(message, users, emails, event="test")
    write_audit(current_user["username"], "Cost alert test email sent", role=str(current_user.get("role", "")).upper(),
                payload={"detail": f"sent {len(result['sent'])}, failed {len(result['failed'])}, skipped {len(result['skipped'])}",
                         "account_id": account_id}, request=request)
    return result
