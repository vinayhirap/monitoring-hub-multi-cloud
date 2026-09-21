# app/api/alerts.py
from typing import Optional
import datetime
import time
import logging
from fastapi import APIRouter, HTTPException, Depends, Body, Response, Query
from app.db import get_connection
from app.auth.deps import get_current_user
from app.auth.permissions import require_permission
from app.aws.federation import NoConsoleCredentialsError
from app.ws.publisher import publish_alert_resolved
from app.api.live_data import invalidate_accounts_cache
from app.auth.authorization import get_accessible_account_ids
from app.audit import write_audit
from app import alert_rules

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
# Canonical per-account/service/resource rollup (see app/alert_rules.py)
_rollup_cache: dict = {"data": None, "ts": 0}
_ROLLUP_TTL = 10

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
    touching the row -- see _require_alert_access.

    Reads alerts.aws_account_id directly (migration 047) rather than
    re-deriving it via `JOIN resources ON resource_id = resource_id`.
    That join was a real authorization bug, not just a display one:
    resource_id is only unique WITHIN one AWS account (confirmed
    colliding in production between two real accounts -- see
    alert_evaluator.py's _dynamic_bounds() comment), so the join could
    match resources rows in a DIFFERENT account and fetchone() would
    silently pick one, potentially authorizing this action against the
    wrong account's scope.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT aws_account_id AS account_id
        FROM alerts
        WHERE id = %s
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
    _rollup_cache["data"] = None
    _rollup_cache["ts"]   = 0


# ── scope helper ───────────────────────────────────────────────
def _scope_sql(current_user: dict, alias: str = "a"):
    """(sql_fragment, params) restricting to the caller's accounts, applied IN
    SQL (not after the fact) so LIMIT/OFFSET pages and totals are correct for
    scoped users. Returns None when the caller may see NO accounts."""
    accessible = get_accessible_account_ids(current_user)
    if accessible is None:
        return "", []
    if not accessible:
        return None
    ids = sorted(accessible)
    return f" AND {alias}.aws_account_id IN ({', '.join(['%s'] * len(ids))})", ids


def _fmt_ts_fields(rows):
    for r in rows:
        for field in ("triggered_at", "resolved_at", "last_seen_at", "muted_until", "acked_at"):
            v = r.get(field)
            if v and isinstance(v, datetime.datetime):
                r[field] = v.strftime("%Y-%m-%dT%H:%M:%SZ")
        r["stale"] = (r.get("state") == "stale")
        r["silenced"] = bool(r.get("silenced"))
        try:
            from app.threshold_defaults import normalize_service_key
            r["service_key"] = normalize_service_key(r.get("service"), r.get("resource"))
        except Exception:
            r["service_key"] = r.get("service")
    return rows


# tab -> extra WHERE fragment. These are the ONLY definitions of the tabs
# and match /counts, the Overview banner and every badge (app/alert_rules.py).
def _tab_where(tab: str) -> str:
    st = alert_rules.state_sql()
    return {
        "all":          "",
        "active":       f" AND ({st}) = 'firing'",
        "stale":        f" AND ({st}) = 'stale'",
        "critical":     f" AND ({st}) = 'firing' AND UPPER(a.severity) = 'CRITICAL'",
        "acknowledged": " AND a.status = 'acknowledged'",
        "resolved":     " AND a.status = 'resolved'",
        "suppressed":   f" AND ({st}) = 'suppressed'",
    }[tab]


_TABS = ("all", "active", "stale", "critical", "acknowledged", "resolved", "suppressed")
_MAX_LIMIT = 1000


def _fetch_alerts_from_db(current_user: dict, tab: str = "all", limit: int = 500,
                          offset: int = 0, account_id: Optional[int] = None,
                          q: Optional[str] = None, open_only: bool = False):
    """Returns (rows, total). Filtering, scoping, paging and the total are all
    done in SQL so the list can never disagree with the tab badge."""
    scope = _scope_sql(current_user)
    if scope is None:
        return [], 0
    scope_sql, scope_params = scope

    where = f" WHERE {alert_rules.base_where()}{scope_sql}{_tab_where(tab)}"
    params = list(scope_params)
    if open_only:
        where += " AND a.status IN ('active', 'acknowledged')"
    if account_id is not None:
        where += " AND a.aws_account_id = %s"
        params.append(account_id)
    if q:
        like = f"%{q}%"
        where += (" AND (a.metric_name LIKE %s OR a.resource_id LIKE %s"
                  " OR r.name LIKE %s OR a.severity LIKE %s)")
        params += [like, like, like, like]

    if tab == "resolved":
        order = "a.resolved_at DESC, a.id DESC"
    else:
        order = ("(a.status = 'resolved') ASC, FIELD(UPPER(a.severity), 'CRITICAL', 'WARNING', 'INFO'), "
                 "a.triggered_at DESC, a.id DESC")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT COUNT(*) AS n {alert_rules.alert_base_from()}{where}", params)
        total = int(cursor.fetchone()["n"])
        cursor.execute(f"""
            SELECT
                a.id,
                a.resource_id                          AS resource,
                COALESCE(a.region, acc.default_region) AS region,
                a.metric_name,
                UPPER(a.severity)                      AS severity,
                a.status,
                {alert_rules.state_sql()}              AS state,
                a.current_value,
                a.threshold,
                a.value,
                CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,
                CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,
                CONVERT_TZ(a.last_seen_at, @@session.time_zone, '+00:00') AS last_seen_at,
                a.acked,
                a.acked_by,
                CONVERT_TZ(a.acked_at,     @@session.time_zone, '+00:00') AS acked_at,
                CONVERT_TZ(a.muted_until,  @@session.time_zone, '+00:00') AS muted_until,
                a.silenced,
                a.silenced_reason,
                a.resolution_reason,
                a.environment,
                a.marked_false_positive,
                r.resource_type                        AS service,
                COALESCE(r.name, a.resource_id)        AS resource_name,
                acc.account_name,
                acc.id                                 AS account_id
            {alert_rules.alert_base_from()}
            {where}
            ORDER BY {order}
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()
    return _fmt_ts_fields(rows), total


# ── GET alerts (server-side tab/paging) ────────────────────────
@router.get("")
def get_alerts(
    response: Response,
    tab: str = Query("all"),
    limit: int = Query(500, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    account_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None, max_length=100),
    current_user: dict = Depends(require_permission("alerts.view")),
):
    """
    `tab` is one of all|active|stale|critical|acknowledged|resolved|suppressed
    and means EXACTLY what the same-named badge from /alerts/counts counts.
    The full match count is returned in the X-Total-Count header so the UI
    can page instead of silently truncating (the old fixed 500-row window
    made 'Resolved 3615' show ~465 rows).
    """
    if tab not in _TABS:
        raise HTTPException(status_code=400, detail=f"tab must be one of {', '.join(_TABS)}")
    rows, total = _fetch_alerts_from_db(current_user, tab, limit, offset, account_id, q)
    response.headers["X-Total-Count"] = str(total)
    return rows


# ── GET open (unresolved) ──────────────────────────────────────
@router.get("/open")
def open_alerts(current_user: dict = Depends(require_permission("alerts.view"))):
    """Every unresolved alert (active + acknowledged) with its derived `state`.
    Callers that want a count or a badge must NOT derive it from this list --
    use /alerts/summary or /alerts/by-resource (same rules, one source)."""
    rows, _ = _fetch_alerts_from_db(current_user, "all", limit=2000, open_only=True)
    return rows


def _fetch_counts_from_db() -> list:
    """Per-account tab counts, defined in terms of alert_rules.state_sql() so
    every number matches the list a tab shows and the Overview banner.
    Kept per-account (not one grand total) so a scoped viewer is only ever
    summed over their own accounts."""
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"""
            SELECT account_id, account_name,
                   COUNT(*)                                         AS all_count,
                   SUM(state = 'firing')                            AS active_count,
                   SUM(state = 'stale')                             AS stale_count,
                   SUM(state = 'firing' AND sev = 'CRITICAL')       AS critical_count,
                   SUM(state = 'acknowledged')                      AS acknowledged_count,
                   SUM(state = 'resolved')                          AS resolved_count,
                   SUM(state = 'suppressed')                        AS suppressed_count
            FROM (
                SELECT acc.id AS account_id, acc.account_name AS account_name, UPPER(a.severity) AS sev,
                       {alert_rules.state_sql()} AS state
                {alert_rules.alert_base_from()}
                WHERE {alert_rules.base_where()}
            ) t
            GROUP BY account_id, account_name
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def _aggregate_counts_for_user(per_account_rows: list, current_user: dict,
                               account_id: Optional[int] = None) -> dict:
    accessible = get_accessible_account_ids(current_user)
    keys = ("all_count", "active_count", "stale_count", "critical_count",
            "acknowledged_count", "resolved_count", "suppressed_count")
    totals = {k: 0 for k in keys}
    accounts = []
    for row in per_account_rows:
        if accessible is not None and row["account_id"] not in accessible:
            continue
        accounts.append({"id": row["account_id"], "name": row.get("account_name") or f"Account {row['account_id']}"})
        if account_id is not None and row["account_id"] != account_id:
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
        "suppressed":   totals["suppressed_count"],
        # the account dropdown's options (RBAC-scoped) -- independent of the
        # account filter so choosing one account doesn't empty the dropdown
        "accounts":     sorted(accounts, key=lambda a: a["name"].lower()),
    }


@router.get("/counts")
def alert_counts(account_id: Optional[int] = Query(None),
                 current_user: dict = Depends(require_permission("alerts.view"))):
    """Tab-badge counts. Exactly the tab definitions in _tab_where(); pass
    account_id so the badges match a list filtered to that account."""
    now = time.time()
    if _counts_cache["data"] is None or now - _counts_cache["ts"] >= _CACHE_TTL:
        _counts_cache["data"] = _fetch_counts_from_db()
        _counts_cache["ts"]   = now
    return _aggregate_counts_for_user(_counts_cache["data"], current_user, account_id)


# ── canonical rollup (Overview / Services / resource badges) ────
def get_alert_rollup() -> dict:
    """Unscoped rollup, cached ~10s and invalidated on every alert write.
    Callers filter per request by the caller's accessible accounts."""
    now = time.time()
    if _rollup_cache["data"] is not None and now - _rollup_cache["ts"] < _ROLLUP_TTL:
        return _rollup_cache["data"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        rows = alert_rules.fetch_open_alert_rows(cursor, None)
    finally:
        cursor.close()
        conn.close()
    data = alert_rules.rollup(rows)
    _rollup_cache["data"] = data
    _rollup_cache["ts"] = now
    return data


_EMPTY_BUCKET = {"critical": 0, "warning": 0, "info": 0, "stale": 0, "acknowledged": 0,
                 "suppressed": 0, "resources_affected": 0, "critical_resources": 0,
                 "warning_resources": 0, "firing": 0, "services": {}}


@router.get("/summary")
def alert_summary(
    account_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_permission("alerts.view")),
):
    """
    THE source for every 'N critical / N warning' shown outside the Alerts
    list itself: Overview banner + account cards, and (with account_id) the
    per-service tile badges on the Services page, core AND extended/directory.
    Unit = alert rows in state 'firing'; stale/acknowledged/suppressed are
    reported separately and never counted as critical/warning.
    """
    roll = get_alert_rollup()
    accessible = get_accessible_account_ids(current_user)
    accounts = {a: v for a, v in roll["accounts"].items()
                if accessible is None or a in accessible}
    if account_id is not None:
        if accessible is not None and account_id not in accessible:
            raise HTTPException(status_code=403, detail="You do not have access to this account")
        accounts = {account_id: accounts.get(account_id, dict(_EMPTY_BUCKET))}
    totals = {k: 0 for k in ("critical", "warning", "info", "stale", "acknowledged", "suppressed", "firing")}
    for v in accounts.values():
        for k in totals:
            totals[k] += v.get(k, 0)
    return {"totals": totals, "accounts": {str(a): v for a, v in accounts.items()}}


@router.get("/by-resource")
def alerts_by_resource(
    account_id: int = Query(...),
    service: Optional[str] = Query(None),
    current_user: dict = Depends(require_permission("alerts.view")),
):
    """
    {resource_id: {worst, critical, warning, info, stale, acknowledged, total}}
    for ONE account (optionally one service key), powering the CRITICAL /
    WARNING badge on every resource row of every resource page. `worst` is the
    highest firing severity, not 'whichever alert was found first'.
    """
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")
    roll = get_alert_rollup()
    svc = (service or "").lower()
    out = {}
    for (acct, rid), v in roll["resources"].items():
        if acct != account_id:
            continue
        if svc and v["service"] != svc and not (svc == "elb" and v["service"] in ("alb", "nlb", "elb")):
            continue
        out[rid] = v
    return out


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
                               AND r.aws_account_id = a.aws_account_id
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


# ── lifecycle actions ─────────────────────────────────────────
# Every action: scope-checked, state-checked (a resolved alert can no longer
# be "acknowledged" back into existence), idempotent, audited, and it records
# who/why on the row (migration 051).
def _audit(current_user, action, alert_id, extra=""):
    write_audit(
        current_user["username"], action,
        f"alert_id={alert_id} {extra}".strip(),
        role=current_user["role"].upper(),
    )


def _current_status(cursor, alert_id):
    cursor.execute("SELECT status FROM alerts WHERE id = %s", (alert_id,))
    row = cursor.fetchone()
    return row["status"] if row else None


@router.post("/{alert_id}/ack")
@router.patch("/{alert_id}/ack")
def ack_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):
    _require_alert_access(alert_id, current_user)

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            UPDATE alerts
            SET acked = 1, status = 'acknowledged',
                acked_by = %s, acked_at = UTC_TIMESTAMP()
            WHERE id = %s AND status = 'active'
        """, (current_user["username"], alert_id))
        changed = cursor.rowcount
        status = None if changed else _current_status(cursor, alert_id)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    if not changed:
        if status is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        if status == "resolved":
            raise HTTPException(status_code=409, detail="Alert is already resolved and cannot be acknowledged")
        # already acknowledged: idempotent success
        return {"status": "acknowledged", "changed": False}

    _audit(current_user, "Alert acknowledged", alert_id)
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "acknowledged", "changed": True}


@router.post("/{alert_id}/resolve")
@router.patch("/{alert_id}/resolve")
def resolve_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):
    account_id = _require_alert_access(alert_id, current_user)

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            UPDATE alerts
            SET resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
                status = 'resolved', resolution_reason = 'manual', resolved_by = %s
            WHERE id = %s AND status <> 'resolved'
        """, (current_user["username"], alert_id))
        changed = cursor.rowcount
        exists = True if changed else (_current_status(cursor, alert_id) is not None)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    if not exists:
        raise HTTPException(status_code=404, detail="Alert not found")
    if changed:
        _audit(current_user, "Alert resolved", alert_id)
        _invalidate_cache()
        invalidate_accounts_cache()
        try:
            publish_alert_resolved(alert_id=alert_id, account_id=account_id)
        except Exception as e:
            logger.warning(f"Resolve publish failed: {e}")
    return {"status": "resolved", "alert_id": alert_id, "changed": bool(changed)}


_MAX_MUTE_MINUTES = 7 * 24 * 60


@router.post("/{alert_id}/mute")
@router.patch("/{alert_id}/mute")
def mute_alert(alert_id: int, minutes: int = Query(30, ge=1, le=_MAX_MUTE_MINUTES),
               current_user: dict = Depends(require_permission("operations.execute"))):
    """Suppresses an OPEN alert for `minutes` (1 min .. 7 days). While muted it
    is state 'suppressed': not counted as critical/warning anywhere, not
    escalated, not on the public status page. It is NOT resolved -- if it is
    still breaching when the mute lapses it counts again automatically.
    (Previously this only wrote muted_until, which nothing ever read.)"""
    _require_alert_access(alert_id, current_user)

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            UPDATE alerts SET muted_until = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE)
            WHERE id = %s AND status IN ('active', 'acknowledged')
        """, (minutes, alert_id))
        changed = cursor.rowcount
        status = None if changed else _current_status(cursor, alert_id)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    if not changed:
        if status is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        raise HTTPException(status_code=409, detail="Only open alerts can be muted")
    _audit(current_user, "Alert muted", alert_id, f"minutes={minutes}")
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "muted", "minutes": minutes}


@router.post("/{alert_id}/unmute")
@router.patch("/{alert_id}/unmute")
def unmute_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):
    _require_alert_access(alert_id, current_user)
    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE alerts SET muted_until = NULL WHERE id = %s", (alert_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    _audit(current_user, "Alert unmuted", alert_id)
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "unmuted"}


# ── GROUPED VIEW (dedup/collapse, roadmap phase 10) ────────────
# Read-time GROUP BY over the same `alerts` table; ack/resolve/mute above
# still act on individual alert ids.
@router.get("/grouped")
def get_grouped_alerts(current_user: dict = Depends(require_permission("alerts.view"))):
    scope = _scope_sql(current_user)
    if scope is None:
        return []
    scope_sql, scope_params = scope
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"""
            SELECT
                a.group_key,
                COUNT(*)                                   AS resource_count,
                MAX(UPPER(a.severity) = 'CRITICAL')        AS has_critical,
                MIN(a.triggered_at)                        AS first_triggered_at,
                MAX(a.last_seen_at)                        AS last_seen_at,
                SUM(a.status = 'active')                   AS active_count,
                SUM(a.status = 'acknowledged')             AS acknowledged_count,
                r.resource_type                            AS service,
                a.metric_name,
                acc.id                                     AS account_id,
                acc.account_name
            {alert_rules.alert_base_from()}
            WHERE a.group_key IS NOT NULL
              AND a.status IN ('active', 'acknowledged')
              AND {alert_rules.base_where()}{scope_sql}
            GROUP BY a.group_key, r.resource_type, a.metric_name, acc.id, acc.account_name
            ORDER BY has_critical DESC, resource_count DESC
        """, scope_params)
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    for r in rows:
        r["has_critical"] = bool(r["has_critical"])
        for field in ("first_triggered_at", "last_seen_at"):
            if r.get(field) and isinstance(r[field], datetime.datetime):
                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")
    return rows


@router.post("/grouped/{group_key}/ack")
def ack_group(group_key: str, current_user: dict = Depends(require_permission("operations.execute"))):
    """Acks every currently-active alert sharing this group_key, scoped to the
    caller's accessible accounts."""
    scope = _scope_sql(current_user)
    if scope is None:
        return {"status": "acknowledged", "count": 0}
    scope_sql, scope_params = scope
    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            UPDATE alerts a
            SET a.acked = 1, a.status = 'acknowledged',
                a.acked_by = %s, a.acked_at = UTC_TIMESTAMP()
            WHERE a.group_key = %s AND a.status = 'active'{scope_sql}
        """, (current_user["username"], group_key, *scope_params))
        affected = cursor.rowcount
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    write_audit(current_user["username"], "Alert group acknowledged",
                f"group_key={group_key} count={affected}", role=current_user["role"].upper())
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "acknowledged", "count": affected}


# ── CLEAR ─────────────────────────────────────────────────────
@router.delete("/clear")
def clear_alerts(current_user: dict = Depends(require_permission("alerts.clear"))):
    """Bulk close of every open, un-acknowledged alert (permission
    alerts.clear -- intentionally tighter than the single-alert actions).

    2026-09-20: this used to DELETE the rows, destroying incident history,
    SLO inputs and audit evidence with no trace, and the very next
    evaluation cycle simply re-created them. It now RESOLVES them with
    resolution_reason='bulk_clear' + resolved_by, and writes an audit entry
    with the count."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE alerts
            SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
                resolution_reason = 'bulk_clear', resolved_by = %s
            WHERE status = 'active' AND acked = 0
        """, (current_user["username"],))
        affected = cur.rowcount
        conn.commit()
    finally:
        cur.close()
        conn.close()
    write_audit(current_user["username"], "Alerts bulk cleared", f"count={affected}",
                role=current_user["role"].upper())
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "cleared", "count": affected}
