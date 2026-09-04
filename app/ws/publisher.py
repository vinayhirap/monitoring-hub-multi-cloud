# app/ws/publisher.py
from __future__ import annotations
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)
_client = None


def get_redis():
    global _client
    if _client is None:
        try:
            import redis  # lazy import — avoids startup warning
            _client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            _client.ping()
            logger.info("Redis publisher connected")
        except Exception as e:
            logger.warning(f"Redis publisher unavailable: {e}")
            _client = None
    return _client


def publish(channel: str, data: dict):
    r = get_redis()
    if r is None:
        return
    try:
        r.publish(f"channel:{channel}", json.dumps(data))
    except Exception as e:
        logger.error(f"Redis publish error: {e}")
        global _client
        _client = None  # reset so next call retries


def publish_metric_update(account_id: int, service: str, cpu: float, memory: float):
    publish("overview", {
        "type": "metric_update",
        "account_id": account_id,
        "service": service,
        "cpu": round(cpu, 2),
        "memory": round(memory, 2),
    })


def publish_alert(alert_id: int, severity: str, metric: str,
                  value: float, threshold: float, account_id: int,
                  account_name: str = None, region: str = None):
    publish("alerts", {
        "type": "new_alert",
        "alert_id": alert_id,
        "severity": severity,
        "metric": metric,
        "value": round(value, 2),
        "threshold": round(threshold, 2),
        "account_id": account_id,
        "account_name": account_name,
        "region": region,
    })


def publish_alert_resolved(alert_id: int | None, account_id: int | None, bulk: bool = False):
    """
    Pushed whenever an alert leaves 'active' status outside of a direct
    user click (auto-resolved by evaluate_alerts' sustained-recovery
    check or by _auto_resolve_stale_alerts). The Alerts page already
    polls every 10s, so this just makes it feel instant / lets other
    open tabs update without waiting for the poll.

    bulk=True (id/account omitted) is used for the stale-alert sweep,
    which can touch several alerts in one pass — the frontend just
    reloads its list rather than patching individual rows.
    """
    publish("alerts", {
        "type": "alert_resolved" if not bulk else "bulk_alerts_changed",
        "id": alert_id,
        "account_id": account_id,
    })