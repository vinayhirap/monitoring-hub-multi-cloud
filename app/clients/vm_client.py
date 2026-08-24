# app/clients/vm_client.py
import os
import json
import datetime
import logging
import requests

logger = logging.getLogger(__name__)

VM_URL = os.environ.get("VM_URL", "http://localhost")


def vm_query_all(promql: str, dim_label: str) -> dict:
    """
    Instant query returning ALL matching series at once, keyed by the given
    dimension label. Used for list/snapshot views where we want e.g. every
    EC2 instance's current CPU in a single VM call instead of one call per
    instance (VM handles this natively — YACE already scrapes every
    resource into one metric name).
    Returns: {dimension_value: float_value}
    """
    out = {}
    try:
        r = requests.get(f"{VM_URL}/api/v1/query", params={"query": promql}, timeout=5)
        r.raise_for_status()
        for series in r.json().get("data", {}).get("result", []):
            label_val = series.get("metric", {}).get(dim_label)
            if label_val is not None:
                out[label_val] = float(series["value"][1])
    except Exception as e:
        logger.warning(f"VM query_all failed [{promql}]: {e}")
    return out


def vm_query(promql: str) -> float | None:
    """
    Instant query. Returns the single latest value for a PromQL expression,
    or None if no series matched.
    """
    try:
        r = requests.get(f"{VM_URL}/api/v1/query", params={"query": promql}, timeout=5)
        r.raise_for_status()
        result = r.json().get("data", {}).get("result", [])
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception as e:
        logger.warning(f"VM query failed [{promql}]: {e}")
        return None


def vm_query_range(promql: str, start: int, end: int, step: str = "60s") -> list[dict]:
    """
    Range query. Returns [{"t": iso_timestamp, "v": value}, ...] sorted oldest->newest.
    Matches the shape collector_direct.py's series functions already return.
    """
    try:
        r = requests.get(f"{VM_URL}/api/v1/query_range", params={
            "query": promql, "start": start, "end": end, "step": step
        }, timeout=10)
        r.raise_for_status()
        result = r.json().get("data", {}).get("result", [])
        if not result:
            return []
        points = result[0].get("values", [])
        return sorted(
            [
                {
                    "t": datetime.datetime.utcfromtimestamp(p[0]).isoformat(),
                    "v": round(float(p[1]), 2),
                }
                for p in points
            ],
            key=lambda x: x["t"],
        )
    except Exception as e:
        logger.warning(f"VM query_range failed [{promql}]: {e}")
        return []


def vm_write_batch(series: list[dict]) -> bool:
    """
    Push datapoints into VictoriaMetrics. Used by the Azure/GCP metric
    collectors (app/providers/{azure,gcp}/metrics_collector.py) -- there is
    no YACE-equivalent Prometheus exporter for those clouds, so unlike AWS
    (where YACE scrapes CloudWatch and pushes to VM on its own), something
    has to actively write this data in.

    Uses VM's /api/v1/import JSON-lines endpoint (see
    https://docs.victoriametrics.com/#how-to-import-data-in-json-line-format)
    rather than remote_write/protobuf, since it's a plain HTTP+JSON POST with
    no extra client dependency.

    series: [
      {
        "metric": "azure_vm_percentage_cpu",   # becomes __name__
        "labels": {"account_id": "5", "resource_id": "...", "region": "..."},
        "value": 42.1,
        "timestamp": 1735000000,   # unix seconds; omit for "now"
      },
      ...
    ]
    Returns True if the whole batch was accepted.
    """
    if not series:
        return True

    lines = []
    now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
    for point in series:
        metric = {"__name__": point["metric"], **point.get("labels", {})}
        ts_ms = int(point["timestamp"] * 1000) if point.get("timestamp") else now_ms
        lines.append(json.dumps({
            "metric": metric,
            "values": [point["value"]],
            "timestamps": [ts_ms],
        }))

    payload = "\n".join(lines)
    try:
        r = requests.post(
            f"{VM_URL}/api/v1/import",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"VM write_batch failed ({len(series)} datapoints): {e}")
        return False