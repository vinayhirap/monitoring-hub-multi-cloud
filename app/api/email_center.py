# app/api/email_center.py
"""
Email & Reports centre API (foundation).

  GET    /api/email-center/status                 emailcenter.view     SMTP state (no secrets), config problems, 24 h / 7 d delivery counts
  POST   /api/email-center/test                   emailcenter.manage   {to}  send one test email now and report the real outcome
  GET    /api/email-center/lists                  emailcenter.manage   recipient lists (addresses visible)
  POST   /api/email-center/lists                  emailcenter.manage   {name, description?, user_ids?, group_ids?, emails?}
  PUT    /api/email-center/lists/{id}             emailcenter.manage   same fields, omitted ones keep their stored value
  DELETE /api/email-center/lists/{id}             emailcenter.manage   409 while a schedule still uses it
  GET    /api/email-center/lists/{id}/preview     emailcenter.manage   who would actually receive mail (account_id? narrows it)
  GET    /api/email-center/schedules              emailcenter.view     report schedules (recipients hidden unless manage)
  POST   /api/email-center/schedules              emailcenter.manage   create (see app/email/schedules.py for the fields)
  PUT    /api/email-center/schedules/{id}         emailcenter.manage
  DELETE /api/email-center/schedules/{id}         emailcenter.manage
  POST   /api/email-center/schedules/{id}/run     emailcenter.manage   {dry_run?} run now for the last full period (dry_run only shows what would happen)
  GET    /api/email-center/log                    emailcenter.manage   delivery log, newest first (status, category, before_id, limit)

Off unless EMAIL_CENTER_ENABLED=true (503 with a clear message, like REPORTS_ENABLED). SMTP itself stays in .env: this API
shows it and tests it, it never accepts or returns the username or password. A schedule is only saved by someone who could
generate that very report by hand (same permission and account-scope checks as POST /api/reports/generate). Saved schedules
are run by app/reports/scheduler.py (leader-guarded thread, once per calendar period, as the 'scheduler' system user).
Every change is written to the audit log.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request

from app.audit import write_audit
from app.auth.authorization import get_accessible_account_ids
from app.auth.permissions import has_permission, require_permission
from app.db import get_db_cursor
from app.email import delivery_log, mailer, recipients, schedules

logger = logging.getLogger(__name__)

MIGRATION_NOTE = "Run the pending migrations (migrate.py apply --all-pending): 135 adds the Email & Reports tables."


def _require_enabled():
    if not delivery_log.enabled():
        raise HTTPException(status_code=503,
                            detail="Email & Reports is not enabled on this environment -- set EMAIL_CENTER_ENABLED=true in .env to activate it")


router = APIRouter(prefix="/api/email-center", tags=["Email & Reports"], dependencies=[Depends(_require_enabled)])


# ── helpers ──────────────────────────────────────────────────────────

def _iso(v):
    if v is None or isinstance(v, str):
        return v
    s = v.isoformat()
    return s if v.tzinfo else s + "Z"


def _missing_table(exc) -> bool:
    return getattr(exc, "errno", None) in (1146, 1054)


def _db(fn):
    """Runs fn(cursor); a table that is not there yet becomes a 503 that names the migration."""
    try:
        with get_db_cursor(dictionary=True) as (_, cur):
            return fn(cur)
    except HTTPException:
        raise
    except Exception as exc:
        if _missing_table(exc):
            raise HTTPException(status_code=503, detail=MIGRATION_NOTE)
        if getattr(exc, "errno", None) == 1062:
            raise HTTPException(status_code=409, detail="That name is already in use")
        raise


def _audit(user, action, detail, request):
    write_audit(user["username"], action, detail, role=user.get("role"), request=request)


def _allowed_domains():
    return [d.strip().lower().lstrip("@") for d in (os.getenv("REPORT_EMAIL_ALLOWED_DOMAINS") or "").split(",") if d.strip()]


def _check_domains(addresses):
    allowed = _allowed_domains()
    if not allowed:
        return
    bad = [a for a in addresses if a.rsplit("@", 1)[-1].lower() not in allowed]
    if bad:
        raise HTTPException(status_code=400, detail="Email can only be sent to: " + ", ".join("@" + d for d in allowed)
                            + " (REPORT_EMAIL_ALLOWED_DOMAINS). Not allowed: " + ", ".join(bad[:5]))


def _require_account_access(account_id, user):
    if account_id is None:
        return
    accessible = get_accessible_account_ids(user)
    if accessible is not None and int(account_id) not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


def _list_out(row, used_by=0):
    return {"id": row["id"], "name": row["name"], "description": row.get("description") or "",
            "user_ids": recipients.load_ids(row.get("user_ids")), "group_ids": recipients.load_ids(row.get("group_ids")),
            "emails": recipients.parse_emails(row.get("emails")) if row.get("emails") else [],
            "used_by_schedules": used_by, "created_by": row.get("created_by"),
            "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at"))}


def _next_run(row):
    if not row.get("enabled"):
        return None
    try:
        return _iso(schedules.next_occurrence(row, datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None))
    except Exception:
        return None


def _schedule_out(row, list_names, show_recipients):
    out = {"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]), "report_type": row["report_type"],
           "scope_type": row["scope_type"], "scope_id": row["scope_id"], "account_id": row["account_id"],
           "run_weekday": row["run_weekday"], "run_month": row["run_month"], "run_day": row["run_day"],
           "run_hour": row["run_hour"], "run_minute": row["run_minute"], "timezone": row["timezone"],
           "when": schedules.describe(row), "recipient_list_id": row["recipient_list_id"],
           "recipient_list_name": list_names.get(row["recipient_list_id"]),
           "created_by": row.get("created_by"), "created_at": _iso(row.get("created_at")),
           "next_run_at": _next_run(row),
           "last_run_at": _iso(row.get("last_run_at")), "last_status": row.get("last_status"),
           "last_error": row.get("last_error")}
    if show_recipients:
        out["extra_emails"] = recipients.parse_emails(row.get("extra_emails")) if row.get("extra_emails") else []
    return out


# ── status + test ────────────────────────────────────────────────────

@router.get("/status")
def status(current_user: dict = Depends(require_permission("emailcenter.view"))):
    c = mailer._settings()
    smtp = {"configured": mailer.is_configured(), "host": c["host"] or None, "port": c["port"],
            "security": c["security"], "authenticated": bool(c["username"]),
            "from_address": parseaddr(c["mail_from"])[1] or None,
            "from_name": (os.getenv("MAIL_FROM_NAME") or "").strip() or "CloudOps",
            "timezone": (os.getenv("MAIL_TIMEZONE") or "").strip() or "Asia/Kolkata",
            "public_app_url": mailer.get_public_app_url(), "problems": mailer.config_problems(),
            "allowed_recipient_domains": _allowed_domains()}
    counts = {"24h": {"sent": 0, "failed": 0, "skipped": 0}, "7d": {"sent": 0, "failed": 0, "skipped": 0}}
    last_failure = None
    migration_pending = False

    def work(cur):
        nonlocal last_failure
        for key, hours in (("24h", 24), ("7d", 168)):
            cur.execute("SELECT status, COUNT(*) AS n FROM email_delivery_log "
                        "WHERE created_at > DATE_SUB(NOW(), INTERVAL %s HOUR) GROUP BY status", (hours,))
            for r in cur.fetchall():
                counts[key][r["status"]] = r["n"]
        cur.execute("SELECT created_at, category, to_addr, error FROM email_delivery_log "
                    "WHERE status = 'failed' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            last_failure = {"at": _iso(row["created_at"]), "category": row["category"], "to": row["to_addr"], "error": row["error"]}
    try:
        _db(work)
    except HTTPException as e:
        if e.status_code != 503:
            raise
        migration_pending = True
    return {"smtp": smtp, "delivery": counts, "last_failure": last_failure, "migration_pending": migration_pending,
            "migration_note": MIGRATION_NOTE if migration_pending else None,
            "reports_enabled": _reports_enabled()}


def _reports_enabled() -> bool:
    return os.getenv("REPORTS_ENABLED", "false").strip().lower() in ("true", "1", "yes")


@router.post("/test")
def send_test(request: Request, payload: dict = Body(...),
              current_user: dict = Depends(require_permission("emailcenter.manage"))):
    try:
        to = recipients.parse_emails(payload.get("to"), limit=1)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not to:
        raise HTTPException(status_code=400, detail="to is required")
    _check_domains(to)
    if not mailer.is_configured():
        raise HTTPException(status_code=501, detail="SMTP is not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, "
                                                    "SMTP_PASSWORD and MAIL_FROM in .env, then restart the service.")
    from app.email import templates
    mail = templates.test_notification_email(mailer.get_public_app_url(), "Email & Reports")
    ok, err = mailer.send_email_ex(to[0], mail["subject"], mail["text"], mail["html"], "Alerts", "test")
    _audit(current_user, "Test email sent", f"to={to[0]}: {'ok' if ok else 'failed'}", request)
    return {"ok": ok, "detail": "Test email accepted by the mail server" if ok else (err or "Send failed")}


# ── recipient lists ──────────────────────────────────────────────────

@router.get("/lists")
def list_lists(current_user: dict = Depends(require_permission("emailcenter.manage"))):
    def work(cur):
        cur.execute("SELECT * FROM email_recipient_lists ORDER BY name")
        rows = cur.fetchall()
        cur.execute("SELECT recipient_list_id, COUNT(*) AS n FROM report_schedules "
                    "WHERE recipient_list_id IS NOT NULL GROUP BY recipient_list_id")
        used = {r["recipient_list_id"]: r["n"] for r in cur.fetchall()}
        return [_list_out(r, used.get(r["id"], 0)) for r in rows]
    return _db(work)


@router.post("/lists")
def create_list(request: Request, payload: dict = Body(...),
                current_user: dict = Depends(require_permission("emailcenter.manage"))):
    try:
        f = recipients.normalize_list(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _check_domains(recipients.parse_emails(f["emails"]))

    def work(cur):
        cur.execute("SELECT id FROM email_recipient_lists WHERE name = %s", (f["name"],))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="A list with that name already exists")
        cur.execute("INSERT INTO email_recipient_lists (name, description, user_ids, group_ids, emails, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (f["name"], f["description"], f["user_ids"], f["group_ids"], f["emails"], current_user["username"]))
        new_id = cur.lastrowid
        cur.execute("SELECT * FROM email_recipient_lists WHERE id = %s", (new_id,))
        return _list_out(cur.fetchone())
    out = _db(work)
    _audit(current_user, "Recipient list created", f"list '{out['name']}'", request)
    return out


@router.put("/lists/{list_id}")
def update_list(list_id: int, request: Request, payload: dict = Body(...),
                current_user: dict = Depends(require_permission("emailcenter.manage"))):
    def work(cur):
        cur.execute("SELECT * FROM email_recipient_lists WHERE id = %s", (list_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="List not found")
        try:
            f = recipients.normalize_list(payload, current=row)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _check_domains(recipients.parse_emails(f["emails"]))
        cur.execute("SELECT id FROM email_recipient_lists WHERE name = %s AND id <> %s", (f["name"], list_id))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="A list with that name already exists")
        cur.execute("UPDATE email_recipient_lists SET name=%s, description=%s, user_ids=%s, group_ids=%s, emails=%s WHERE id=%s",
                    (f["name"], f["description"], f["user_ids"], f["group_ids"], f["emails"], list_id))
        cur.execute("SELECT * FROM email_recipient_lists WHERE id = %s", (list_id,))
        return _list_out(cur.fetchone())
    out = _db(work)
    _audit(current_user, "Recipient list updated", f"list '{out['name']}'", request)
    return out


@router.delete("/lists/{list_id}")
def delete_list(list_id: int, request: Request,
                current_user: dict = Depends(require_permission("emailcenter.manage"))):
    def work(cur):
        cur.execute("SELECT name FROM email_recipient_lists WHERE id = %s", (list_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="List not found")
        cur.execute("SELECT COUNT(*) AS n FROM report_schedules WHERE recipient_list_id = %s", (list_id,))
        if cur.fetchone()["n"]:
            raise HTTPException(status_code=409, detail="A report schedule still uses this list: change or delete that schedule first")
        cur.execute("DELETE FROM email_recipient_lists WHERE id = %s", (list_id,))
        return row["name"]
    name = _db(work)
    _audit(current_user, "Recipient list deleted", f"list '{name}'", request)
    return {"status": "deleted"}


@router.get("/lists/{list_id}/preview")
def preview_list(list_id: int, account_id: int | None = Query(None),
                 current_user: dict = Depends(require_permission("emailcenter.manage"))):
    _require_account_access(account_id, current_user)

    def work(cur):
        cur.execute("SELECT * FROM email_recipient_lists WHERE id = %s", (list_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="List not found")
        return recipients.resolve(row, account_id=account_id, cursor=cur)
    return _db(work)


# ── report schedules ─────────────────────────────────────────────────

def _validate_scope(f, user):
    """Same gate as POST /api/reports/generate: the person saving a schedule must be allowed to generate that report."""
    stype, acct, scope_id = f["scope_type"], f["account_id"], f["scope_id"]
    if stype == "CLIENT" and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="CLIENT-scoped reports span multiple accounts and are admin-only")
    _require_account_access(acct, user)
    from app.reports import registry
    if registry.is_section(stype):
        registry.validate(stype, scope_id, acct, user)
        return
    if stype == "SYNTHETIC":                                   # not a registry section: its own scope rules
        if not has_permission(user, "synthetic.view"):
            raise HTTPException(status_code=403, detail="Missing permission: synthetic.view")
        from app.reports.synthetic_report import parse_scope
        try:
            a, _check = parse_scope(scope_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if a != int(acct):
            raise HTTPException(status_code=400, detail="scope_id must name the selected account")
        return
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        if stype == "RESOURCE":
            cur.execute("SELECT 1 AS ok FROM resources WHERE resource_id = %s AND aws_account_id = %s LIMIT 1", (scope_id, acct))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="That resource does not exist in this account")
        elif stype == "ACCOUNT":
            cur.execute("SELECT 1 AS ok FROM aws_accounts WHERE id = %s", (acct,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="That account does not exist")


def _check_list_exists(cur, f):
    if f["recipient_list_id"] is not None:
        cur.execute("SELECT id FROM email_recipient_lists WHERE id = %s", (f["recipient_list_id"],))
        if not cur.fetchone():
            raise HTTPException(status_code=400, detail="That recipient list does not exist")
    _check_domains(recipients.parse_emails(f["extra_emails"]))


@router.get("/schedules")
def list_schedules(current_user: dict = Depends(require_permission("emailcenter.view"))):
    show = has_permission(current_user, "emailcenter.manage")
    accessible = get_accessible_account_ids(current_user)

    def work(cur):
        cur.execute("SELECT * FROM report_schedules ORDER BY name")
        rows = cur.fetchall()
        cur.execute("SELECT id, name FROM email_recipient_lists")
        names = {r["id"]: r["name"] for r in cur.fetchall()}
        return rows, names
    rows, names = _db(work)
    if accessible is not None:        # a scoped user sees schedules for accounts they can see, never the org-wide ones
        rows = [r for r in rows if r["account_id"] is not None and r["account_id"] in accessible]
    return [_schedule_out(r, names, show) for r in rows]


@router.post("/schedules")
def create_schedule(request: Request, payload: dict = Body(...),
                    current_user: dict = Depends(require_permission("emailcenter.manage"))):
    try:
        f = schedules.normalize(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_scope(f, current_user)

    def work(cur):
        _check_list_exists(cur, f)
        cur.execute("SELECT id FROM report_schedules WHERE name = %s", (f["name"],))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="A schedule with that name already exists")
        cur.execute(
            "INSERT INTO report_schedules (name, enabled, report_type, scope_type, scope_id, account_id, run_weekday, "
            "run_month, run_day, run_hour, run_minute, timezone, recipient_list_id, extra_emails, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (f["name"], f["enabled"], f["report_type"], f["scope_type"], f["scope_id"], f["account_id"], f["run_weekday"],
             f["run_month"], f["run_day"], f["run_hour"], f["run_minute"], f["timezone"], f["recipient_list_id"],
             f["extra_emails"], current_user["username"]))
        new_id = cur.lastrowid
        cur.execute("SELECT * FROM report_schedules WHERE id = %s", (new_id,))
        row = cur.fetchone()
        cur.execute("SELECT id, name FROM email_recipient_lists")
        return _schedule_out(row, {r["id"]: r["name"] for r in cur.fetchall()}, True)
    out = _db(work)
    _audit(current_user, "Report schedule created", f"'{out['name']}': {out['report_type']} {out['scope_type']}={out['scope_id']}, {out['when']}", request)
    return out


@router.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, request: Request, payload: dict = Body(...),
                    current_user: dict = Depends(require_permission("emailcenter.manage"))):
    def load(cur):
        cur.execute("SELECT * FROM report_schedules WHERE id = %s", (schedule_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return row
    row = _db(load)
    try:
        f = schedules.normalize(payload, current=row)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_scope(f, current_user)

    def work(cur):
        _check_list_exists(cur, f)
        cur.execute("SELECT id FROM report_schedules WHERE name = %s AND id <> %s", (f["name"], schedule_id))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="A schedule with that name already exists")
        cur.execute(
            "UPDATE report_schedules SET name=%s, enabled=%s, report_type=%s, scope_type=%s, scope_id=%s, account_id=%s, "
            "run_weekday=%s, run_month=%s, run_day=%s, run_hour=%s, run_minute=%s, timezone=%s, recipient_list_id=%s, "
            "extra_emails=%s WHERE id=%s",
            (f["name"], f["enabled"], f["report_type"], f["scope_type"], f["scope_id"], f["account_id"], f["run_weekday"],
             f["run_month"], f["run_day"], f["run_hour"], f["run_minute"], f["timezone"], f["recipient_list_id"],
             f["extra_emails"], schedule_id))
        cur.execute("SELECT * FROM report_schedules WHERE id = %s", (schedule_id,))
        new = cur.fetchone()
        cur.execute("SELECT id, name FROM email_recipient_lists")
        return _schedule_out(new, {r["id"]: r["name"] for r in cur.fetchall()}, True)
    out = _db(work)
    _audit(current_user, "Report schedule updated", f"'{out['name']}': {out['when']}, {'enabled' if out['enabled'] else 'disabled'}", request)
    return out


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, request: Request,
                    current_user: dict = Depends(require_permission("emailcenter.manage"))):
    def work(cur):
        cur.execute("SELECT name FROM report_schedules WHERE id = %s", (schedule_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found")
        cur.execute("DELETE FROM report_schedules WHERE id = %s", (schedule_id,))
        return row["name"]
    name = _db(work)
    _audit(current_user, "Report schedule deleted", f"'{name}'", request)
    return {"status": "deleted"}


@router.post("/schedules/{schedule_id}/run")
def run_schedule_now(schedule_id: int, background_tasks: BackgroundTasks, request: Request,
                     payload: dict | None = Body(None),
                     current_user: dict = Depends(require_permission("emailcenter.manage"))):
    """Run one schedule now for its most recent full calendar period. It does not use up the period (the next scheduled run
    still happens). {"dry_run": true} only reports the period, the recipients and what would happen."""
    from app.reports import scheduler
    dry = bool((payload or {}).get("dry_run"))

    def load(cur):
        cur.execute("SELECT * FROM report_schedules WHERE id = %s", (schedule_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return row
    row = _db(load)
    _validate_scope(row, current_user)
    now = datetime.now(timezone.utc)
    period = {"period_start": None, "period_end": None}
    period["period_start"], period["period_end"] = schedules.period_for(row, schedules.latest_occurrence(row, now))
    reports_on = os.getenv("REPORTS_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    smtp_ok = mailer.is_configured()
    if dry:
        addresses, skipped = scheduler.resolve_recipients(row)
        would = ("send the report to everyone listed" if addresses and reports_on and smtp_ok else
                 "not send: no one to send to" if not addresses else
                 "not send: reports are disabled on this environment (REPORTS_ENABLED=false)" if not reports_on else
                 "not send: SMTP is not configured")
        return {"dry_run": True, "when": schedules.describe(row), "period_start": _iso(period["period_start"]),
                "period_end": _iso(period["period_end"]), "recipients": addresses, "skipped": skipped,
                "reports_enabled": reports_on, "smtp_configured": smtp_ok, "would": would}
    last = row.get("last_run_at")
    if row.get("last_status") == "RUNNING" and last and datetime.utcnow() - last < timedelta(minutes=30):
        raise HTTPException(status_code=409, detail="This schedule is already running")

    def mark(cur):
        cur.execute("UPDATE report_schedules SET last_status = 'RUNNING', last_error = NULL, last_run_at = UTC_TIMESTAMP() WHERE id = %s",
                    (schedule_id,))
    _db(mark)
    background_tasks.add_task(scheduler.execute, row, period)
    _audit(current_user, "Report schedule run now", f"'{row['name']}' for {period['period_start']} .. {period['period_end']}", request)
    return {"status": "started", "period_start": _iso(period["period_start"]), "period_end": _iso(period["period_end"])}


# ── delivery log ─────────────────────────────────────────────────────

@router.get("/log")
def delivery_log_rows(status: str | None = Query(None), category: str | None = Query(None),
                      before_id: int | None = Query(None), limit: int = Query(100, ge=1, le=200),
                      current_user: dict = Depends(require_permission("emailcenter.manage"))):
    where, params = [], []
    if status:
        if status not in delivery_log.STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {list(delivery_log.STATUSES)}")
        where.append("status = %s"); params.append(status)
    if category:
        if not re.fullmatch(r"[a-z_]{1,30}", category):
            raise HTTPException(status_code=400, detail="category must be lower-case letters")
        where.append("category = %s"); params.append(category)
    if before_id:
        where.append("id < %s"); params.append(before_id)
    sql = ("SELECT id, created_at, category, to_addr, subject, status, error, report_id, schedule_id FROM email_delivery_log"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT %s")
    params.append(limit)

    def work(cur):
        cur.execute(sql, tuple(params))
        return cur.fetchall()
    rows = _db(work)
    for r in rows:
        r["created_at"] = _iso(r["created_at"])
    return rows
