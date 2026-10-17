# app/env_digest.py
"""
Weekly digest of NEW resources that could not be placed in an environment (workstream C, item 5).

OFF unless ENV_DIGEST_ENABLED=true. Nothing is sent and nothing is written to environment_digest_log otherwise.

Who gets it. aws_accounts.owner_team (a free-text team name set at onboarding) is matched, ignoring case and
surrounding spaces, to the NAME of an existing org_group; the members of that group (user_group_memberships ->
users.email, active users only) receive it, and only those who may see the account, the same tenant rule the
escalation emails apply. No group with that name = no recipients: the digest is recorded as `no_recipients` and
visible in the preview, it is not sent anywhere else. The match is done in Python, not SQL: org_groups and
aws_accounts use different collations, and a string join across them raises MySQL error 1267.

Why not the 4b digest queue or the reports engine. The 4b queue batches ALERTS for notification channels and is not on the
repository tip this was built against; the reports engine renders PDFs to S3, far heavier than a list of names.
This digest goes to a team derived from the account, so it reuses the two pieces escalation already relies on:
org_groups membership and app/email/mailer.

What "new" means. Live resources whose environment is still `unknown` and that were first seen after the last
digest that was sent for the account (or in the last 7 days when none was). Older unknown resources are counted in
the "waiting in total" line, not listed again.

Once per account per ISO week: the log row is inserted BEFORE sending (unique key), so a restart or a leadership
change cannot send it twice. Runs from the discovery hook; sends on or after ENV_DIGEST_WEEKDAY (0 = Monday,
default 0) the first time the hook runs that week.
"""
import logging
import os
from datetime import datetime, timedelta

from app import environment_governance as eg
from app.db import get_connection

logger = logging.getLogger(__name__)

FIRST_WINDOW_DAYS = 7
LIST_FETCH = 200
_warned_smtp = {"done": False}


def digest_enabled():
    return os.getenv("ENV_DIGEST_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def digest_weekday():
    try:
        return max(0, min(6, int(os.getenv("ENV_DIGEST_WEEKDAY", "0"))))
    except ValueError:
        return 0


def match_group(owner_team, groups):
    """Pure. owner_team text + [{id, name}] -> group id or None (case and surrounding spaces ignored, exact otherwise)."""
    want = (owner_team or "").strip().lower()
    if not want:
        return None
    hits = [g["id"] for g in groups if (g.get("name") or "").strip().lower() == want]
    return hits[0] if len(hits) == 1 else None          # two groups with the same name is ambiguous: do not guess


def _recipients(cur, group_id, account_pk):
    cur.execute("""SELECT DISTINCT u.id, u.role, u.email
                   FROM user_group_memberships ugm JOIN users u ON u.id = ugm.user_id
                   WHERE ugm.group_id = %s AND u.email IS NOT NULL AND u.email != '' AND COALESCE(u.active, 1) = 1""",
                (group_id,))
    out = []
    for m in cur.fetchall():
        try:
            from app.auth.authorization import get_accessible_account_ids
            acc = get_accessible_account_ids({"id": m["id"], "role": m["role"]})
        except Exception as e:
            logger.warning(f"[env-digest] scope lookup failed for user {m['id']}: {e} -- not emailing them")
            continue
        if acc is None or account_pk in acc:
            out.append(m["email"])
    return list(dict.fromkeys(out))


def build_plan(now=None):
    """-> list of per-account dicts describing what a run would do right now. Read-only; used by the preview route
    and by the sender. Each: account_id, account_name, owner_team, group_id, recipients (list of addresses, internal
    use only), new_items, new_count, total_unknown, week_key, already_handled, since."""
    from app.collector.env_sync import NON_ALERTABLE_TYPES, STALE_RESOURCE_HOURS
    now = now or datetime.utcnow()
    week = eg.iso_week_key(now)
    types = ", ".join(["%s"] * len(NON_ALERTABLE_TYPES))
    conn = get_connection()
    plan = []
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, account_name, owner_team FROM aws_accounts WHERE status = 'active' ORDER BY id")
            accounts = cur.fetchall()
            cur.execute("SELECT id, name FROM org_groups")
            groups = cur.fetchall()
            for a in accounts:
                cur.execute("SELECT status FROM environment_digest_log WHERE aws_account_id = %s AND week_key = %s",
                            (a["id"], week))
                handled = cur.fetchone()
                cur.execute("SELECT MAX(created_at) AS t FROM environment_digest_log "
                            "WHERE aws_account_id = %s AND status = 'sent'", (a["id"],))
                last = (cur.fetchone() or {}).get("t")
                since = last or (now - timedelta(days=FIRST_WINDOW_DAYS))
                live = ("(r.instance_state IS NULL OR r.instance_state != 'terminated') "
                        "AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)) "
                        f"AND r.resource_type NOT IN ({types})")
                lp = [STALE_RESOURCE_HOURS, *NON_ALERTABLE_TYPES]
                cur.execute(f"SELECT COUNT(*) AS n FROM resources r WHERE r.aws_account_id = %s AND {live} "
                            "AND r.env_source = 'unknown'", [a["id"], *lp])
                total_unknown = int(cur.fetchone()["n"])
                cur.execute(f"SELECT r.name, r.resource_id, r.resource_type, r.region FROM resources r "
                            f"WHERE r.aws_account_id = %s AND {live} AND r.env_source = 'unknown' AND r.created_at > %s "
                            f"ORDER BY r.created_at DESC LIMIT {LIST_FETCH}", [a["id"], *lp, since])
                new_items = [{"name": r["name"] or r["resource_id"], "resource_type": r["resource_type"],
                              "region": r["region"]} for r in cur.fetchall()]
                gid = match_group(a["owner_team"], groups)
                plan.append({"account_id": a["id"], "account_name": a["account_name"],
                             "owner_team": (a["owner_team"] or "").strip(), "group_id": gid,
                             "recipients": _recipients(cur, gid, a["id"]) if gid and new_items else [],
                             "new_items": new_items, "new_count": len(new_items), "total_unknown": total_unknown,
                             "week_key": week, "already_handled": handled["status"] if handled else None,
                             "since": since})
        finally:
            cur.close()
    finally:
        conn.close()
    return plan


def preview(now=None):
    """The plan without addresses, for the API: counts and the group name only."""
    out = []
    for p in build_plan(now):
        if p["already_handled"]:
            verdict = f"already handled this week ({p['already_handled']})"
        elif not p["new_count"]:
            verdict = "nothing new to report"
        elif not p["group_id"]:
            verdict = "no group is named like the owner team; nobody would receive it"
        elif not p["recipients"]:
            verdict = "the group has no active member who may see this account; nobody would receive it"
        else:
            verdict = "would be sent"
        out.append({"account_id": p["account_id"], "account_name": p["account_name"], "owner_team": p["owner_team"],
                    "group_found": p["group_id"] is not None, "recipient_count": len(p["recipients"]),
                    "new_count": p["new_count"], "total_unknown": p["total_unknown"], "week": p["week_key"],
                    "verdict": verdict})
    return {"enabled": digest_enabled(), "weekday": digest_weekday(), "accounts": out}


def _log(cur, account_id, week, status, new, total, recipients, detail=None):
    cur.execute("UPDATE environment_digest_log SET status = %s, new_unknown = %s, total_unknown = %s, recipients = %s, "
                "detail = %s WHERE aws_account_id = %s AND week_key = %s",
                (status, new, total, recipients, (detail or "")[:300] or None, account_id, week))


def send_weekly_digest(now=None):
    """Send what is due. Returns {account_id: status}. Raises only on a database problem."""
    from app.email import mailer
    now = now or datetime.utcnow()
    results = {}
    for p in build_plan(now):
        if p["already_handled"] or not p["new_count"]:
            continue
        aid, week = p["account_id"], p["week_key"]
        if p["group_id"] and p["recipients"] and not mailer.is_configured():
            if not _warned_smtp["done"]:
                logger.warning("[env-digest] SMTP_HOST is not set; the environment digest is waiting, nothing sent")
                _warned_smtp["done"] = True
            continue                                        # not claimed: it goes out once SMTP is configured
        conn = get_connection()
        try:
            cur = conn.cursor()
            try:
                cur.execute("INSERT IGNORE INTO environment_digest_log (aws_account_id, week_key, status) "
                            "VALUES (%s, %s, 'claimed')", (aid, week))
                claimed = cur.rowcount == 1
                conn.commit()
                if not claimed:
                    continue                                # another process took this account's week
                if not p["group_id"] or not p["recipients"]:
                    why = ("no group is named like the owner team" if not p["group_id"]
                           else "no active member of the group may see this account")
                    _log(cur, aid, week, "no_recipients", p["new_count"], p["total_unknown"], 0,
                         f"owner_team '{p['owner_team']}': {why}")
                    conn.commit()
                    results[aid] = "no_recipients"
                    continue
                url = f"{mailer.get_public_app_url()}/environments"
                body = eg.digest_text(p["account_name"], p["new_items"], p["total_unknown"], url)
                subject = f"CloudOps: {p['new_count']} new resource(s) in {p['account_name']} need an environment"
                sent = sum(1 for addr in p["recipients"] if mailer.send_email(addr, subject, body))
                status = "sent" if sent else "failed"
                _log(cur, aid, week, status, p["new_count"], p["total_unknown"], sent,
                     None if sent else "the mail server refused every recipient")
                conn.commit()
                results[aid] = status
            finally:
                cur.close()
        finally:
            conn.close()
    return results


def maybe_send_weekly_digest(now=None):
    """Discovery-loop hook. Does nothing unless ENV_DIGEST_ENABLED=true and the configured weekday has come this
    week. Never raises."""
    if not digest_enabled():
        return {}
    now = now or datetime.utcnow()
    if now.weekday() < digest_weekday():
        return {}
    try:
        res = send_weekly_digest(now)
        if res:
            logger.info(f"[env-digest] {res}")
        return res
    except Exception as e:
        if getattr(e, "errno", None) in (1146, 1054):
            logger.debug("[env-digest] migration 100 not applied yet -- skipping")
        else:
            logger.warning(f"[env-digest] failed (non-fatal): {e}")
        return {}
