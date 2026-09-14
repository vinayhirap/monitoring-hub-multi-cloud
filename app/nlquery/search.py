# app/nlquery/search.py
"""
AIOps roadmap #11 -- natural-language dashboard search, EXECUTION half
(2026-09-14). Takes app/nlquery/parser.py's structured filter dict and
runs it as a parameterized SQL query over `alerts` (joined the same way
app/api/alerts.py already joins resources/aws_accounts), scoped through
the SAME app/auth/authorization.get_accessible_account_ids() every
other read endpoint in this app uses -- this is a NEW way to query
existing data, not a new data-access path, so it must not be able to
see anything a plain GET /alerts couldn't already show this caller.
"""
from app.db import get_connection
from app.auth.authorization import get_accessible_account_ids
from app.nlquery.parser import parse_query

MAX_RESULTS = 100


def _known_resource_types(cursor, accessible_account_ids) -> list:
    if accessible_account_ids is None:
        cursor.execute("SELECT DISTINCT resource_type FROM resources")
    else:
        if not accessible_account_ids:
            return []
        placeholders = ", ".join(["%s"] * len(accessible_account_ids))
        cursor.execute(
            f"SELECT DISTINCT resource_type FROM resources "
            f"WHERE aws_account_id IN ({placeholders})",
            tuple(accessible_account_ids),
        )
    return [r["resource_type"] for r in cursor.fetchall() if r["resource_type"]]


def run_nl_search(query_text: str, current_user: dict) -> dict:
    """
    Returns {"interpreted_as": str, "filters": dict, "results": [...]}.
    `results` rows use the same shape as GET /alerts rows (id, resource,
    metric_name, severity, status, value, threshold, created_at) so the
    frontend can reuse its existing alert-row rendering with no new
    component.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        accessible = get_accessible_account_ids(current_user)
        known_types = _known_resource_types(cursor, accessible)
        filters = parse_query(query_text, known_types)

        where = ["1=1"]
        params = []

        if accessible is not None:
            if not accessible:
                return {
                    "interpreted_as": filters["interpreted_as"],
                    "filters": filters,
                    "results": [],
                }
            placeholders = ", ".join(["%s"] * len(accessible))
            where.append(f"acc.id IN ({placeholders})")
            params.extend(accessible)

        if filters["severity"]:
            where.append("a.severity = %s")
            params.append(filters["severity"])

        if filters["status"]:
            where.append("a.status = %s")
            params.append(filters["status"])

        if filters["resource_type"]:
            where.append("r.resource_type = %s")
            params.append(filters["resource_type"])

        if filters["since_minutes"]:
            where.append("a.created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)")
            params.append(filters["since_minutes"])

        if filters["free_text"]:
            where.append("(r.name LIKE %s OR r.resource_id LIKE %s)")
            like = f"%{filters['free_text']}%"
            params.extend([like, like])

        # multivariate_anomaly rows are hidden from the end-user Alerts
        # page (see app/api/alerts.py's _HIDDEN_FROM_ALERTS_UI_METRICS) --
        # search results should match what the caller would actually see
        # if they clicked through, not surface an internal detector row
        # via a search shortcut.
        where.append("a.metric_name != 'multivariate_anomaly'")

        sql = f"""
            SELECT a.id, a.resource_id AS aws_resource_id, r.name AS resource_name,
                   r.resource_type, a.metric_name, a.severity, a.status,
                   a.value, a.threshold, a.created_at, acc.id AS account_id,
                   acc.name AS account_name
            FROM alerts a
            JOIN resources r      ON r.resource_id = a.resource_id
            JOIN aws_accounts acc ON acc.id = r.aws_account_id
            WHERE {' AND '.join(where)}
            ORDER BY a.created_at DESC
            LIMIT {MAX_RESULTS}
        """
        cursor.execute(sql, tuple(params))
        results = cursor.fetchall()

        return {
            "interpreted_as": filters["interpreted_as"],
            "filters": filters,
            "results": results,
        }
    finally:
        cursor.close()
        conn.close()
