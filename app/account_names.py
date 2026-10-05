# app/account_names.py
"""
Human labels for audit-log text. The Compliance page showed rows like "account 10: 6 metric threshold(s) set from
defaults" and "account=7 metric_id=312 warn=70 crit=90": ids that mean nothing to a reader. Anything written to the
audit log or shown in a message should name the account (and metric), with the id only as a last-resort fallback.

Best effort and cheap: a failed lookup never breaks the request that is being audited.
"""
import logging
import time

from app.db import get_connection

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300
_cache: dict = {}          # account_id -> (expires_at, name)


def account_label(account_id) -> str:
    """'U4RAD' for a known account, 'account 10' only if the name cannot be read."""
    if account_id is None:
        return "all accounts"
    now = time.monotonic()
    hit = _cache.get(account_id)
    if hit and hit[0] > now:
        return hit[1]
    name = None
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT account_name FROM aws_accounts WHERE id = %s", (account_id,))
            row = cur.fetchone()
            if row:
                name = row[0] if not isinstance(row, dict) else row.get("account_name")
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[account_names] lookup failed for {account_id}: {exc}")
    label = name or f"account {account_id}"
    if name:
        _cache[account_id] = (now + _TTL_SECONDS, name)
    return label


def metric_name_label(metric_id) -> str:
    """'CPU Utilization' for a metric_catalog id, 'metric #312' if it cannot be read."""
    try:
        from app.metric_labels import metric_label
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT metric_name FROM metric_catalog WHERE id = %s", (metric_id,))
            row = cur.fetchone()
            raw = (row[0] if not isinstance(row, dict) else row.get("metric_name")) if row else None
        finally:
            conn.close()
        if raw:
            return metric_label(raw)
    except Exception as exc:
        logger.warning(f"[account_names] metric lookup failed for {metric_id}: {exc}")
    return f"metric #{metric_id}"


def threshold_label(threshold_id) -> str:
    """'U4RAD: CPU Utilization' for a thresholds row (account name and metric label), or 'threshold #12' if unreadable."""
    try:
        from app.metric_labels import metric_label
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT a.account_name, mc.metric_name FROM thresholds t
                           LEFT JOIN aws_accounts a ON a.id = t.aws_account_id
                           LEFT JOIN metric_catalog mc ON mc.id = t.metric_id WHERE t.id = %s""", (threshold_id,))
            row = cur.fetchone()
            if row:
                name, metric = (row if not isinstance(row, dict) else (row.get("account_name"), row.get("metric_name")))
                if name and metric:
                    return f"{name}: {metric_label(metric)}"
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[account_names] threshold lookup failed for {threshold_id}: {exc}")
    return f"threshold #{threshold_id}"


def plural(n: int, singular: str, plural_form: str = None) -> str:
    return singular if n == 1 else (plural_form or singular + "s")
