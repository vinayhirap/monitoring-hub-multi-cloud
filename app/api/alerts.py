# app/api/alerts.py
from typing import Optional
import datetime
import time
import logging
from fastapi import APIRouter, HTTPException, Depends, Body, Response
from app.db import get_connection
from app.auth.deps import get_current_user, require_role
from app.auth.permissions import require_permission
from app.aws.federation import NoConsoleCredentialsError
from app.ws.publisher import publish_alert_resolved
from app.api.live_data import invalidate_accounts_cache
from app.auth.authorization import get_accessible_account_ids
from app.audit import write_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["Alerts"])

# Simple in-process cache — alerts list doesn't change sub-second
_alerts_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 15  # seconds — short enough for near-realtime, avoids hammering DB

# Authoritative, uncapped tab counts (see /counts below). Kept in its own
# cache/entry, invalidated in lockstep with _alerts_cache by
# _invalidate_cache(), so a badge can never read a count from before the
# write that changed it while the row list already reflects it.
_counts_cache: dict = {"data": None, "ts": 0}

# An active alert whose last_seen_at hasn't been touched in this long has
# stopped getting fresh metric data -- surfaced to the UI as "stale / no
# data" so it's not mistaken for a live, just-reconfirmed breach. It is
# NOT auto-resolved (see 008_revert_falsely_resolved_alerts.sql) -- this
# is display-only, the operator decides whether to resolve it.
_STALE_AFTER_MINUTES = 20

# multivariate_anomaly hiding: moved to app/alert_visibility.py (2026-09-15)
# so app/api/live_data.py's _get_active_alert_counts_by_account() can apply
# the identical filter too. This file already imports FROM live_data.py
# (invalidate_accounts_cache, below) -- live_data.py importing the filter
# back from here would be a circular import, so both files import it from
# a third, dependency-free module instead. See alert_visibility.py's
# docstring for the drift this fixes. _hidden_metrics_sql kept as a thin
# alias so nothing below in this file needs to change.
from app.alert_visibility import hidden_metrics_sql as _hidden_metrics_sql


def _filter_rows_by_scope(rows: list, current_user: dict) -> list:
    """
    SECURITY: every row list this module returns includes account_id,
    but until this fix nothing filtered by it -- a viewer with only
    alerts.view (no admin/editor role) could see every alert for
    every account in the entire system, not just the accounts their
    access_scopes/group_policies actually grant them. The row lists
    themselves stay globally cached (they're cheap to compute once and
    identical for everyone before this filter), so this filters a
    per-request COPY rather than changing what's cached -- correctness
    of the RBAC boundary doesn't depend on cache TTL or who warmed it.
    """
    accessible = get_accessible_account_ids(current_user)
    if accessible is None:
        return rows  # FULL_ACCESS (admin)
    return [r for r in rows if r.get("account_id") in accessible]


def _get_alert_account_id(alert_id: int):
    """Returns the aws_accounts.id an alert belongs to, or None if the
    alert doesn't exist. Used to authorize single-alert actions
    (ack/resolve/mute/console-url) against the caller's scope before
    touching the row -- see _require_alert_access."""
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT acc.id AS account_id
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
        WHERE a.id = %s
    """, (alert_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row["account_id"] if row else None


def _require_alert_access(alert_id: int, current_user: dict) -> int:
    """
    SECURITY: ack/resolve/mute/console-url previously took no scope
    check at all -- any authenticated user holding the ROLE-level
    operations.execute/alerts.view permission (which is not itself
    account-scoped) could act on an alert belonging to any account in
    the system just by iterating alert_id, regardless of their
    assigned account/region scope. This is the same
    account_id-not-in-accessible pattern already used consistently in
    app/api/live_data.py and app/api/admin/accounts.py, applied here
    for parity. Raises 404 if the alert doesn't exist at all (so a
    caller with real access can't distinguish "doesn't exist" from
    "not yours" by a different status code), 403 if it exists but is
    outside the caller's scope. Returns the alert's account_id on
    success, for callers (e.g. resolve's publish_alert_resolved) that
    need it afterward without a second query.
    """
    account_id = _get_alert_account_id(alert_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this alert")
    return account_id


def _invalidate_cache():
    _alerts_cache["data"] = None
    _alerts_cache["ts"]   = 0
    _counts_cache["data"] = None
    _counts_cache["ts"]   = 0

def _fetch_alerts_from_db():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.id,
            a.resource_id                          AS resource,
            COALESCE(a.region, acc.default_region) AS region,
            a.metric_name,
            a.severity,
            a.status,
            a.current_value,
            a.threshold,
            a.value,
            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,
            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,
            CONVERT_TZ(a.last_seen_at, @@session.time_zone, '+00:00') AS last_seen_at,
            (a.status = 'active'
             AND a.last_seen_at IS NOT NULL
             AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE)
            ) AS stale,
            a.acked,
            a.muted_until,
            a.environment,
            a.marked_false_positive,
            r.resource_type                        AS service,
            COALESCE(r.name, a.resource_id)        AS resource_name,
            acc.account_name,
            acc.id                                 AS account_id
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
                               AND acc.status = 'active'
        WHERE a.metric_name NOT IN ({hidden})
        ORDER BY
            -- Unresolved rows always sort ahead of resolved ones. Without
            -- this, a burst of alerts that trigger-then-quickly-resolve
            -- (e.g. a flapping metric re-creating a row every cycle) can
            -- fill the entire LIMIT window with fresh *resolved* noise by
            -- triggered_at alone, silently pushing a genuinely still-open
            -- alert (older triggered_at, never resolved) out of the page
            -- entirely -- which is exactly how "Active" showed 0 while
            -- Overview's separate, uncapped query correctly showed 26.
            (a.resolved_at IS NULL) DESC,
            a.triggered_at DESC
        LIMIT 500
    """.format(stale=_STALE_AFTER_MINUTES, hidden=_hidden_metrics_sql()))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for r in rows:
        for field in ("triggered_at", "resolved_at", "last_seen_at"):
            if r.get(field) and isinstance(r[field], datetime.datetime):
                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")
            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith("Z"):
                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"
        r["stale"] = bool(r.get("stale"))

    return rows


# ── GET all alerts (cached) ───────────────────────────────────
@router.get("")
def get_alerts(current_user: dict = Depends(require_permission("alerts.view"))):
    now = time.time()
    if _alerts_cache["data"] is not None and now - _alerts_cache["ts"] < _CACHE_TTL:
        return _filter_rows_by_scope(_alerts_cache["data"], current_user)
    rows = _fetch_alerts_from_db()
    _alerts_cache["data"] = rows
    _alerts_cache["ts"]   = now
    return _filter_rows_by_scope(rows, current_user)


# ── GET open/active only ──────────────────────────────────────
@router.get("/open")
def open_alerts(current_user: dict = Depends(require_permission("alerts.view"))):
    """
    Returns only unresolved alerts — used by Overview alert strip + api.js getAlerts().
    Also cached. Invalidated on ack/resolve.
    """
    now = time.time()
    # Reuse full cache if available, filter client-side to avoid second DB call
    if _alerts_cache["data"] is not None and now - _alerts_cache["ts"] < _CACHE_TTL:
        rows = [a for a in _alerts_cache["data"] if a.get("status") != "resolved"]
        return _filter_rows_by_scope(rows, current_user)

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.id,
            a.resource_id                          AS resource,
            a.metric_name,
            a.severity,
            a.status,
            a.current_value,
            a.threshold,
            a.value,
            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,
            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,
            CONVERT_TZ(a.last_seen_at, @@session.time_zone, '+00:00') AS last_seen_at,
            (a.status = 'active'
             AND a.last_seen_at IS NOT NULL
             AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE)
            ) AS stale,
            a.acked,
            a.environment,
            a.marked_false_positive,
            r.resource_type                        AS service,
            COALESCE(r.name, a.resource_id)        AS resource_name,
            acc.account_name,
            acc.id                                 AS account_id,
            COALESCE(a.region, acc.default_region) AS region
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
                               AND acc.status = 'active'
        WHERE a.resolved_at IS NULL
          AND a.metric_name NOT IN ({hidden})
            ORDER BY
            FIELD(a.severity, 'CRITICAL', 'WARNING', 'INFO'),
            a.triggered_at DESC
        LIMIT 2000
    """.format(stale=_STALE_AFTER_MINUTES, hidden=_hidden_metrics_sql()))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for r in rows:
        for field in ("triggered_at", "resolved_at", "last_seen_at"):
            if r.get(field) and isinstance(r[field], datetime.datetime):
                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")
            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith("Z"):
                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"
        r["stale"] = bool(r.get("stale"))

    return _filter_rows_by_scope(rows, current_user)


def _fetch_counts_from_db() -> list:
    """
    Authoritative tab-badge counts, aggregated directly in SQL with no
    LIMIT/windowing of any kind — so they can never disagree with
    reality the way client-side counts derived from a capped, recency-
    ordered row list can (see the ORDER BY comment in
    _fetch_alerts_from_db above for how that happened in practice).

    "critical" is defined identically to live_data.py's
    _get_active_alert_counts_by_account() -- status = 'active' AND
    severity = 'CRITICAL' -- so this number always matches the Overview
    banner/tiles for the same moment in time. It deliberately does NOT
    fold in acknowledged or resolved rows just because they were once
    critical; a resolved alert isn't something that "requires attention"
    any more, no matter what severity it broke at.

    SECURITY: GROUPed BY account (rather than one grand-total row like
    the pre-fix version) so the cache holds a per-account breakdown --
    _aggregate_counts_for_user then sums only the accounts the calling
    user is actually scoped to see. A single flat total across every
    account would have leaked "how many alerts exist system-wide" (and,
    combined with acking/resolving elsewhere, actual activity volume)
    to a viewer scoped to a single account, regardless of role.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            acc.id AS account_id,
            COUNT(*) AS all_count,
            SUM(CASE WHEN a.status = 'active'
                      AND NOT (a.last_seen_at IS NOT NULL
                               AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE))
                     THEN 1 ELSE 0 END) AS active_count,
            SUM(CASE WHEN a.status = 'active'
                      AND a.last_seen_at IS NOT NULL
                      AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE)
                     THEN 1 ELSE 0 END) AS stale_count,
            SUM(CASE WHEN a.status = 'active' AND a.severity = 'CRITICAL'
                     THEN 1 ELSE 0 END) AS critical_count,
            SUM(CASE WHEN a.status = 'acknowledged' THEN 1 ELSE 0 END) AS acknowledged_count,
            SUM(CASE WHEN a.status = 'resolved' THEN 1 ELSE 0 END) AS resolved_count
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
                               AND acc.status = 'active'
        WHERE a.metric_name NOT IN ({hidden})
        GROUP BY acc.id
    """.format(stale=_STALE_AFTER_MINUTES, hidden=_hidden_metrics_sql()))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def _aggregate_counts_for_user(per_account_rows: list, current_user: dict) -> dict:
    accessible = get_accessible_account_ids(current_user)
    keys = ("all_count", "active_count", "stale_count",
            "critical_count", "acknowledged_count", "resolved_count")
    totals = {k: 0 for k in keys}
    for row in per_account_rows:
        if accessible is not None and row["account_id"] not in accessible:
            continue
        for k in keys:
            totals[k] += int(row.get(k) or 0)
    return {
        "all":          totals["all_count"],
        "active":       totals["active_count"],
        "stale":        totals["stale_count"],
        "critical":     totals["critical_count"],
        "acknowledged": totals["acknowledged_count"],
        "resolved":     totals["resolved_count"],
    }


# ── GET tab-badge counts (uncapped, authoritative) ──────────────
@router.get("/counts")
def alert_counts(current_user: dict = Depends(require_permission("alerts.view"))):
    """
    Source of truth for every alert-count badge in the app (Alerts page
    tabs, sidebar nav badge). Unlike /alerts and /alerts/open, this is
    never paginated/limited, so a badge fed from here can't under- or
    over-report just because the underlying row list got crowded out --
    see _fetch_alerts_from_db's ORDER BY comment for the failure mode
    this replaces. Same 15s TTL and invalidation path (_invalidate_cache)
    as the row-list cache, so both stay in sync on every alert write.
    """
    now = time.time()
    if _counts_cache["data"] is not None and now - _counts_cache["ts"] < _CACHE_TTL:
        return _aggregate_counts_for_user(_counts_cache["data"], current_user)
    per_account_rows = _fetch_counts_from_db()
    _counts_cache["data"] = per_account_rows
    _counts_cache["ts"]   = now
    return _aggregate_counts_for_user(per_account_rows, current_user)


# ── AWS CONSOLE DEEP-LINK (account-correct) ────────────────────
@router.post("/{alert_id}/console-url")
def get_console_url(alert_id: int, user: dict = Depends(require_permission("alerts.view"))):
    """
    Returns a console deep link that opens THIS alert's resource in THIS
    alert's account -- regardless of which account/cloud the operator's
    browser currently happens to be signed into.

    Dispatches through the provider layer (get_provider().get_console_url)
    the same way app/api/admin/accounts.py's sibling endpoint already
    does -- this one was the one place that migration was never finished,
    which meant no Azure/GCP alert could ever produce a working console
    link (AWS's federation helpers were being called directly regardless
    of the alert's actual account provider).

    POST, not GET, despite only fetching a URL: see
    app/api/admin/accounts.py's matching get_account_console_url
    docstring for why (writes an audit-log entry as a side effect,
    which a GET version would let a CSRF attacker trigger via a plain
    top-level navigation under SameSite=Lax). Alerts.jsx already calls
    this via apiFetch(), so this required no other frontend change
    beyond the method itself.
    """
    # SECURITY: previously generated a live cloud-console federation
    # link for this alert's account with no scope check at all -- any
    # user holding the role-level alerts.view permission could obtain
    # console access into an account entirely outside their assigned
    # scope just by supplying its alert_id.
    _require_alert_access(alert_id, user)

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.resource_id                          AS resource,
            r.resource_type                        AS resource_type,
            r.name                                  AS resource_name,
            COALESCE(a.region, acc.default_region) AS region,
            acc.*
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
        WHERE a.id = %s
    """, (alert_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    try:
        from app.providers.registry import get_provider
        provider = get_provider(row.get("provider") or "aws")
        url = provider.get_console_url(
            row, row["resource"], row["region"],
            service=row.get("resource_type"), resource_name=row.get("resource_name"),
            requested_by=user["username"],
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Failed to build console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate console link")

    return {"url": url, "account_id": row["account_id"]}


# ── ROOT-CAUSE EXPLANATION (customer-facing) ────────────────────
@router.get("/{alert_id}/explain")
def explain_alert(alert_id: int, current_user: dict = Depends(require_permission("alerts.view"))):
    """
    Plain-English probable-root-cause explanation for a single alert --
    deep RCA (real AWS CloudTrail activity, topology context, trend
    behavior, related alerts), written for the actual customer looking
    at this alert, not an internal ops console. See
    app/collector/rca.py's explain_alert() for how each part is derived
    and why this is deliberately jargon-free.

    GET, not POST: unlike /console-url this has no side effect (no
    audit-log write, no external API call) -- it's a pure read over
    data this app already collected, safe to cache/refetch freely.
    """
    _require_alert_access(alert_id, current_user)

    from app.collector.rca import explain_alert as _explain_alert
    result = _explain_alert(alert_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return result


@router.get("/{alert_id}/rca-report")
def get_rca_report(
    alert_id: int,
    format: str = "md",
    current_user: dict = Depends(require_permission("alerts.view")),
):
    """
    Downloadable incident RCA (Root Cause Analysis) report -- works for
    any alert, standalone or part of a multi-alert incident. format=md
    (default) or format=pdf. Renamed from "postmortem" 2026-09-17 for a
    more professional, client-facing name -- no behavior change.

    See app/llm/rca_report.py's module docstring: the timeline and
    resource/severity/duration facts are always deterministic (real
    rows), only the Executive Summary/Recommendations prose is
    optionally LLM-written, with a deterministic bullet-point fallback
    when the LLM is disabled or its call fails -- a report is ALWAYS
    produced either way.

    GET, not POST: read-only, generates on demand (reports are
    requested rarely, unlike /explain which loads on every alert page
    view -- so this is NOT cached the way /explain's LLM summary is,
    see app/collector/llm_summarizer.py for why that one needed a
    background cache and this one doesn't).
    """
    if format not in ("md", "pdf"):
        raise HTTPException(status_code=400, detail="format must be 'md' or 'pdf'")

    _require_alert_access(alert_id, current_user)

    from app.llm.rca_report import generate_rca_report, render_markdown
    report = generate_rca_report(alert_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Alert not found")

    markdown_text = render_markdown(report)
    title = f"rca-report-alert-{alert_id}"

    if format == "md":
        return Response(
            content=markdown_text,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{title}.md"'},
        )

    from app.llm.rca_report_pdf import render_pdf
    pdf_title = f"RCA Report: {report['facts']['metric_name']} on {report['facts']['resource_name'] or report['facts']['resource_id']}"
    pdf_bytes = render_pdf(markdown_text, pdf_title, severity=report["facts"]["severity"])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{title}.pdf"'},
    )


# ── MARK / UNMARK FALSE POSITIVE (2026-09-14) ────────────────────
@router.patch("/{alert_id}/false-positive")
def mark_false_positive(alert_id: int, payload: dict = Body(default={}),
                         current_user: dict = Depends(require_permission("operations.execute"))):
    """
    Closes the loop on this session's false-alert-reduction work: the
    system already self-corrects chronic/flapping thresholds
    automatically (app/collector/threshold_tuning.py), but until now
    there was no way for a HUMAN to directly say "this alert wasn't
    genuine" and have that recorded. A person confirming an alert is
    noise is stronger evidence than the automatic detector's own
    inference alone -- app/collector/threshold_tuning.py's new
    manually-confirmed path (see its own docstring) uses a history of
    these markings to switch a threshold to dynamic faster than the
    chronic-mean/chronic-noise paths would on statistical inference
    alone.

    payload: {"marked": true|false} -- true to mark (default if the
    body is omitted/empty), false to un-mark (undoing an accidental
    click; also settable so a reviewer correcting someone else's
    marking doesn't need a separate endpoint).

    Same security/permission bar as ack/resolve (operations.execute +
    account-scope check) -- this is an operational action on a specific
    alert, not a read.
    """
    _require_alert_access(alert_id, current_user)

    marked = bool(payload.get("marked", True))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT resource_id, metric_name, severity FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        if marked:
            cursor.execute("""
                UPDATE alerts
                SET marked_false_positive = 1, false_positive_marked_by = %s, false_positive_marked_at = NOW()
                WHERE id = %s
            """, (current_user["username"], alert_id))
        else:
            cursor.execute("""
                UPDATE alerts
                SET marked_false_positive = 0, false_positive_marked_by = NULL, false_positive_marked_at = NULL
                WHERE id = %s
            """, (alert_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    write_audit(
        current_user["username"],
        "Alert marked false positive" if marked else "Alert false-positive marking removed",
        f"alert_id={alert_id} resource={alert['resource_id']} metric={alert['metric_name']} "
        f"severity={alert['severity']}",
        role=current_user["role"].upper(),
    )
    _invalidate_cache()
    return {"status": "updated", "marked_false_positive": marked}


# ── ACK ───────────────────────────────────────────────────────
@router.post("/{alert_id}/ack")
@router.patch("/{alert_id}/ack")
def ack_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):
    # SECURITY: operations.execute is a role-level permission, not an
    # account-scoped one -- without this check any editor could
    # acknowledge an alert belonging to any account in the system by
    # guessing/iterating alert_id, regardless of their assigned scope.
    _require_alert_access(alert_id, current_user)

    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE alerts SET acked = 1, status = 'acknowledged' WHERE id = %s",
        (alert_id,)
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
    conn.commit()
    cursor.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "acknowledged"}


# ── RESOLVE ───────────────────────────────────────────────────
@router.post("/{alert_id}/resolve")
@router.patch("/{alert_id}/resolve")
def resolve_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):
    # SECURITY: same class of gap as ack_alert above -- resolving is
    # also a destructive, account-scoped action that had no scope
    # check at all.
    account_id = _require_alert_access(alert_id, current_user)

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "UPDATE alerts SET resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(), "
        "status = 'resolved' WHERE id = %s",
        (alert_id,)
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
    conn.commit()
    cursor.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()

    try:
        publish_alert_resolved(alert_id=alert_id, account_id=account_id)
    except Exception as e:
        logger.warning(f"Resolve publish failed: {e}")

    return {"status": "resolved", "alert_id": alert_id}


# ── MUTE ──────────────────────────────────────────────────────
@router.post("/{alert_id}/mute")
def mute_alert(alert_id: int, minutes: int = 30, current_user: dict = Depends(require_permission("operations.execute"))):
    # SECURITY: same class of gap as ack_alert/resolve_alert above.
    _require_alert_access(alert_id, current_user)

    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE alerts SET muted_until = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE) WHERE id = %s",
        (minutes, alert_id)
    )
    conn.commit()
    cursor.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "muted", "minutes": minutes}


# ── GROUPED VIEW (dedup/collapse, roadmap phase 10) ────────────
# Does NOT merge alert rows (see db/migrations/019_alert_grouping.sql
# docstring for why) -- this is a read-time GROUP BY over the same
# `alerts` table the ungrouped /alerts endpoint reads, so ack/resolve/
# mute below still act on individual alert IDs. Frontend shows one card
# per group ("CPU high — 6 resources") that expands to the individual
# alerts for per-resource actions, or uses /grouped/{group_key}/ack
# below to ack everything in the group in one call.
@router.get("/grouped")
def get_grouped_alerts(current_user: dict = Depends(require_permission("alerts.view"))):
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.group_key,
            COUNT(*)                                   AS resource_count,
            MAX(a.severity = 'CRITICAL')               AS has_critical,
            MIN(a.triggered_at)                        AS first_triggered_at,
            MAX(a.last_seen_at)                        AS last_seen_at,
            SUM(a.status = 'active')                   AS active_count,
            SUM(a.status = 'acknowledged')              AS acknowledged_count,
            r.resource_type                            AS service,
            a.metric_name,
            acc.id                                      AS account_id,
            acc.account_name
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
                               AND acc.status = 'active'
        WHERE a.group_key IS NOT NULL
          AND a.status IN ('active', 'acknowledged')
        GROUP BY a.group_key, r.resource_type, a.metric_name, acc.id, acc.account_name
        ORDER BY has_critical DESC, resource_count DESC
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    rows = _filter_rows_by_scope(rows, current_user)
    for r in rows:
        r["has_critical"] = bool(r["has_critical"])
        for field in ("first_triggered_at", "last_seen_at"):
            if r.get(field) and isinstance(r[field], datetime.datetime):
                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")
    return rows


@router.post("/grouped/{group_key}/ack")
def ack_group(group_key: str, current_user: dict = Depends(require_permission("operations.execute"))):
    """
    Acks every currently-active alert sharing this group_key, scoped to
    the caller's accessible accounts -- NOT a bare `WHERE group_key = %s`,
    since group_key alone doesn't carry an account boundary a
    non-admin's scope check can apply to without first knowing which
    accounts they're allowed to touch.
    """
    accessible = get_accessible_account_ids(current_user)
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    if accessible is None:
        cursor.execute(
            "UPDATE alerts SET acked = 1, status = 'acknowledged' "
            "WHERE group_key = %s AND status = 'active'",
            (group_key,)
        )
    else:
        if not accessible:
            cursor.close(); conn.close()
            return {"status": "acknowledged", "count": 0}
        fmt = ",".join(["%s"] * len(accessible))
        cursor.execute(f"""
            UPDATE alerts a
            JOIN resources r      ON r.resource_id = a.resource_id
            JOIN aws_accounts acc ON acc.id = r.aws_account_id AND acc.id IN ({fmt})
            SET a.acked = 1, a.status = 'acknowledged'
            WHERE a.group_key = %s AND a.status = 'active'
        """, (*accessible, group_key))
    affected = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "acknowledged", "count": affected}


# ── CLEAR ─────────────────────────────────────────────────────
@router.delete("/clear")
def clear_alerts(current_user: dict = Depends(require_role("admin"))):
    # Admin-only: bulk-deletes every unresolved/unacked alert with no
    # undo. No existing permission code covers a bulk-destructive action
    # like this (operations.execute covers acting on ONE alert), so this
    # is intentionally locked tighter than the single-alert actions above.
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM alerts WHERE resolved_at IS NULL AND acked = 0")
    conn.commit()
    affected = cur.rowcount
    cur.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "cleared", "count": affected}