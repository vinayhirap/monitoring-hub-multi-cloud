#!/usr/bin/env python3
"""
scripts/host_agent_loadtest.py -- simulate N host agents pushing to a DEV hub. Standard library only.

  python3 scripts/host_agent_loadtest.py --confirm-dev --base-url https://<DEV_HOST> \
      --enrollment-key cak_... --hosts 50 --duration 300 --save-tokens /tmp/ha_tokens.json
  python3 scripts/host_agent_loadtest.py --confirm-dev --base-url https://<DEV_HOST> \
      --tokens-file /tmp/ha_tokens.json --hosts 50 --duration 300        # re-run without enrolling again

What it does
  1. Enrolls N hosts named <prefix>-0001.. (POST /api/host-agents/enroll), or loads their tokens from --tokens-file.
  2. Each host sends one ingest payload (INGEST PAYLOAD v1, ~20 metrics + 2 services) every --interval seconds
     (default 60) for --duration seconds, over ONE persistent connection per host (like the real agent). Start times are
     spread evenly across the first interval so the hub is not hit by N simultaneous requests.
  3. Prints p50 / p95 / p99 latency, error rate, 429 count, accepted / rejected totals, and rows/min.

What it does NOT do
  - It never reads the database. "rows/min" is the sum of the `accepted` counts the hub returned, divided by the run
    time; it equals DB rows only if the hub stores one row per accepted sample. Confirm with the SQL it prints.
  - It does not clean up. Revoke the loadtest-* hosts afterwards (UI: Host agents -> revoke, or
    POST /api/host-agents/{id}/revoke) and delete the --save-tokens file (it holds live agent tokens).
  - It refuses to run without --confirm-dev, refuses http:// unless --insecure-dev, and never prints a token.

Enrollment is rate limited (HOST_AGENT_ENROLL_RATE_PER_HOUR per IP, default 20; nginx adds 30 r/m). To enroll more
than 20 hosts from one machine, raise that variable on the DEV hub for the test (and put it back), or enroll in
batches across hours and reuse --save-tokens / --tokens-file.

Exit code: 0 ok, 1 error rate above --max-error-rate, 2 usage / setup failure.
"""
import argparse
import http.client
import json
import os
import random
import ssl
import sys
import threading
import time
from urllib.parse import urlparse

API = "/api/host-agents"
AGENT_VERSION = "0.0.0-loadtest"


# ── payload ──────────────────────────────────────────────────────────────────

def build_payload(hostname: str, seq: int, config_version: int, now: float, rng: random.Random) -> dict:
    """INGEST PAYLOAD v1 with the exact v1 metric names. Values are random but valid (0-100 percentages, non-negative rates)."""
    ts = int(now)
    m = [
        ("cpu_percent", rng.uniform(1, 90), {}),
        ("load1", rng.uniform(0, 4), {}), ("load5", rng.uniform(0, 4), {}), ("load15", rng.uniform(0, 4), {}),
        ("mem_used_percent", rng.uniform(20, 90), {}),
        ("mem_available_bytes", rng.uniform(1e8, 8e9), {}),
        ("swap_used_percent", rng.uniform(0, 30), {}),
        ("disk_used_percent", rng.uniform(10, 85), {"mount": "/"}),
        ("disk_used_percent", rng.uniform(10, 85), {"mount": "/var"}),
        ("disk_inodes_used_percent", rng.uniform(1, 40), {"mount": "/"}),
        ("disk_inodes_used_percent", rng.uniform(1, 40), {"mount": "/var"}),
        ("disk_read_bytes_per_s", rng.uniform(0, 5e6), {}),
        ("disk_write_bytes_per_s", rng.uniform(0, 5e6), {}),
        ("net_rx_bytes_per_s", rng.uniform(0, 2e6), {"iface": "eth0"}),
        ("net_tx_bytes_per_s", rng.uniform(0, 2e6), {"iface": "eth0"}),
        ("net_rx_bytes_per_s", rng.uniform(0, 1e3), {"iface": "lo"}),
        ("net_tx_bytes_per_s", rng.uniform(0, 1e3), {"iface": "lo"}),
        ("proc_count", float(rng.randint(80, 400)), {}),
        ("uptime_seconds", float(86400 + seq * 60), {}),
    ]
    return {
        "schema": 1, "agent_version": AGENT_VERSION, "config_version": config_version, "seq": seq, "sent_at": ts,
        "host": {"hostname": hostname, "instance_id": None, "os": "linux", "arch": "amd64", "boot_time": ts - 86400},
        "metrics": [{"name": n, "value": round(v, 3), "ts": ts, **({"labels": lb} if lb else {})} for n, v, lb in m],
        "services": [{"name": "nginx", "state": "running"}, {"name": "sshd", "state": "running"}],
    }


def percentile(sorted_values, pct: float) -> float:
    """Nearest-rank percentile of an already sorted list; 0.0 for an empty list."""
    if not sorted_values:
        return 0.0
    rank = max(1, int(-(-pct / 100.0 * len(sorted_values) // 1)))     # ceil without math import
    return sorted_values[min(rank, len(sorted_values)) - 1]


# ── transport ────────────────────────────────────────────────────────────────

class Hub:
    def __init__(self, base_url: str, insecure_dev: bool, timeout: float):
        u = urlparse(base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"bad --base-url: {base_url!r}")
        if u.scheme == "http" and not insecure_dev:
            raise ValueError("refusing http:// (the agent refuses it too); use https:// or --insecure-dev")
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.prefix = u.path.rstrip("/")
        self.timeout = timeout
        self.ctx = None
        if self.scheme == "https":
            self.ctx = ssl.create_default_context()
            if insecure_dev:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE

    def connect(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def request(self, conn, path: str, body: dict, token=None):
        """-> (status, parsed_json_or_None, conn). Reconnects once if the kept-alive connection was closed."""
        data = json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(data)), "Accept": "application/json",
                   "User-Agent": "cloudops-agent-loadtest"}
        if token:
            headers["Authorization"] = "Bearer " + token
        for attempt in (0, 1):
            try:
                conn.request("POST", self.prefix + API + path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw.decode() or "null"), conn
                except ValueError:
                    return resp.status, None, conn
            except (http.client.HTTPException, ConnectionError, ssl.SSLError, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
                conn = self.connect()
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")


# ── run ──────────────────────────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat_ms, self.status = [], {}
        self.accepted = self.rejected = self.exceptions = 0

    def add(self, ms, status, body):
        with self.lock:
            self.lat_ms.append(ms)
            self.status[status] = self.status.get(status, 0) + 1
            if status == 200 and isinstance(body, dict):
                self.accepted += int(body.get("accepted") or 0)
                self.rejected += int(body.get("rejected") or 0)

    def exc(self):
        with self.lock:
            self.exceptions += 1


def enroll_all(hub, key, prefix, n, pace):
    tokens = {}
    conn = hub.connect()
    for i in range(1, n + 1):
        name = f"{prefix}-{i:04d}"
        status, body, conn = hub.request(conn, "/enroll", {
            "enrollment_key": key, "hostname": name, "instance_id": None, "os": "linux", "arch": "amd64",
            "agent_version": AGENT_VERSION})
        if status == 429:
            raise RuntimeError(f"enroll rate limit hit after {i - 1} hosts (HOST_AGENT_ENROLL_RATE_PER_HOUR, nginx ha_enroll); "
                               "raise it on DEV for the test, or enroll in batches and reuse --save-tokens")
        if status != 200 or not isinstance(body, dict) or not body.get("agent_token"):
            raise RuntimeError(f"enroll failed for {name}: HTTP {status}")
        tokens[name] = {"token": body["agent_token"], "config_version": int((body.get("config") or {}).get("config_version", 1))}
        if pace:
            time.sleep(pace)
    conn.close()
    return tokens


def host_loop(hub, name, tok, interval, duration, start_delay, stats, t0, stop):
    rng = random.Random(name)
    conn, seq = hub.connect(), int(time.time()) % 1_000_000 * 100
    config_version = tok.get("config_version", 1)
    if stop.wait(start_delay):
        return
    n = 0
    while True:
        due = t0 + start_delay + n * interval
        if due - t0 >= duration or stop.is_set():
            break
        if stop.wait(max(0.0, due - time.time())):
            break
        seq += 1
        payload = build_payload(name, seq, config_version, time.time(), rng)
        started = time.perf_counter()
        try:
            status, body, conn = hub.request(conn, "/ingest", payload, token=tok["token"])
            stats.add((time.perf_counter() - started) * 1000.0, status, body)
            if status == 200 and isinstance(body, dict) and body.get("config_version"):
                config_version = int(body["config_version"])
        except Exception:
            stats.exc()
        n += 1
    try:
        conn.close()
    except Exception:
        pass


def summarize(stats: Stats, hosts: int, duration: float, interval: float, wall: float) -> dict:
    lat = sorted(stats.lat_ms)
    sent = len(lat) + stats.exceptions
    bad = sum(c for s, c in stats.status.items() if s != 200) + stats.exceptions
    minutes = max(wall, 1e-9) / 60.0
    return {
        "hosts": hosts, "interval_s": interval, "duration_s": duration, "wall_s": round(wall, 1),
        "requests": sent, "ok": stats.status.get(200, 0), "http_429": stats.status.get(429, 0),
        "exceptions": stats.exceptions, "status_counts": {str(k): v for k, v in sorted(stats.status.items())},
        "error_rate": round(bad / sent, 4) if sent else 1.0,
        "latency_ms": {"p50": round(percentile(lat, 50), 1), "p95": round(percentile(lat, 95), 1),
                       "p99": round(percentile(lat, 99), 1), "max": round(lat[-1], 1) if lat else 0.0},
        "requests_per_s": round(sent / max(wall, 1e-9), 2),
        "samples_accepted": stats.accepted, "samples_rejected": stats.rejected,
        "rows_per_min_from_accepted": round(stats.accepted / minutes, 1),
    }


def parse_args(argv):
    p = argparse.ArgumentParser(description="Simulate N host agents against a DEV hub.")
    p.add_argument("--base-url", required=True, help="https://<DEV_HOST> (no path unless the hub is mounted under one)")
    p.add_argument("--confirm-dev", action="store_true", help="required: asserts the target is the DEV hub")
    p.add_argument("--insecure-dev", action="store_true", help="allow http:// and skip TLS verification (self-signed DEV)")
    p.add_argument("--enrollment-key", default=os.environ.get("HOST_AGENT_LOADTEST_ENROLL_KEY"),
                   help="cak_... with max_uses >= --hosts (or env HOST_AGENT_LOADTEST_ENROLL_KEY); not needed with --tokens-file")
    p.add_argument("--tokens-file", help="JSON written by --save-tokens; skips enrollment")
    p.add_argument("--save-tokens", help="write {hostname: {token, config_version}} here (mode 0600). Delete it afterwards.")
    p.add_argument("--hosts", type=int, default=50)
    p.add_argument("--prefix", default="loadtest")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between payloads per host (agent default 60)")
    p.add_argument("--duration", type=float, default=300.0, help="seconds to run (use >= 3x --interval)")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--enroll-pace", type=float, default=0.0, help="seconds to sleep between enrollments")
    p.add_argument("--max-error-rate", type=float, default=0.01)
    p.add_argument("--json", action="store_true", help="print only the JSON summary")
    a = p.parse_args(argv)
    if a.hosts < 1 or a.interval <= 0 or a.duration <= 0:
        p.error("--hosts, --interval and --duration must be positive")
    return a


def main(argv=None) -> int:
    a = parse_args(argv)
    if not a.confirm_dev:
        print("refusing to run: pass --confirm-dev to confirm the target is the DEV hub (never PROD)", file=sys.stderr)
        return 2
    try:
        hub = Hub(a.base_url, a.insecure_dev, a.timeout)
        if a.tokens_file:
            with open(a.tokens_file) as fh:
                tokens = json.load(fh)
            names = sorted(tokens)[:a.hosts]
            if len(names) < a.hosts:
                raise RuntimeError(f"{a.tokens_file} has {len(names)} hosts, --hosts wants {a.hosts}")
            tokens = {n: tokens[n] for n in names}
        else:
            if not a.enrollment_key:
                raise RuntimeError("need --enrollment-key (or env HOST_AGENT_LOADTEST_ENROLL_KEY) or --tokens-file")
            tokens = enroll_all(hub, a.enrollment_key, a.prefix, a.hosts, a.enroll_pace)
            if a.save_tokens:
                fd = os.open(a.save_tokens, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    json.dump(tokens, fh)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 2

    stats, stop, t0 = Stats(), threading.Event(), time.time()
    threads = []
    for i, name in enumerate(sorted(tokens)):
        delay = (i / max(1, len(tokens))) * min(a.interval, a.duration)
        t = threading.Thread(target=host_loop, daemon=True,
                             args=(hub, name, tokens[name], a.interval, a.duration, delay, stats, t0, stop))
        t.start()
        threads.append(t)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(timeout=5)
    summary = summarize(stats, len(tokens), a.duration, a.interval, time.time() - t0)

    if a.json:
        print(json.dumps(summary, indent=2))
    else:
        lat = summary["latency_ms"]
        print(f"hosts={summary['hosts']} interval={a.interval:g}s duration={a.duration:g}s wall={summary['wall_s']}s")
        print(f"requests={summary['requests']} ok={summary['ok']} 429={summary['http_429']} exceptions={summary['exceptions']} "
              f"error_rate={summary['error_rate']:.2%}  status={summary['status_counts']}")
        print(f"latency ms: p50={lat['p50']} p95={lat['p95']} p99={lat['p99']} max={lat['max']}   req/s={summary['requests_per_s']}")
        print(f"samples accepted={summary['samples_accepted']} rejected={summary['samples_rejected']}  "
              f"rows/min (from accepted counts) = {summary['rows_per_min_from_accepted']}")
        print("confirm real DB rows: run before and after the test (inside /opt/monitoring-hub/app on DEV, standard SQL format),")
        print("  SELECT COUNT(*) FROM metric_history;     -- adjust the table if the ingest phase stores samples elsewhere")
        print("  and compare the difference with samples accepted above.")
        print("cleanup: revoke the loadtest hosts (UI or POST /api/host-agents/{id}/revoke) and delete the tokens file.")
    return 1 if summary["error_rate"] > a.max_error_rate else 0


if __name__ == "__main__":
    sys.exit(main())
