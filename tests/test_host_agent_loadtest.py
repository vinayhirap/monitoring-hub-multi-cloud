# tests/test_host_agent_loadtest.py
"""scripts/host_agent_loadtest.py against a throwaway local HTTP server that speaks the enroll / ingest contract."""
import importlib.util
import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("host_agent_loadtest", ROOT / "scripts/host_agent_loadtest.py")
lt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lt)

V1_NAMES = {"cpu_percent", "load1", "load5", "load15", "mem_used_percent", "mem_available_bytes", "swap_used_percent",
            "disk_used_percent", "disk_inodes_used_percent", "disk_read_bytes_per_s", "disk_write_bytes_per_s",
            "net_rx_bytes_per_s", "net_tx_bytes_per_s", "proc_count", "uptime_seconds"}


class FakeHub:
    def __init__(self, fail_every=0, enroll_limit=None):
        self.enrolled, self.tokens, self.payloads, self.lock = [], {}, [], threading.Lock()
        self.fail_every, self.enroll_limit, self.calls = fail_every, enroll_limit, 0
        hub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a):
                pass
            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with hub.lock:
                    hub.calls += 1
                    n = hub.calls
                    if self.path == "/api/host-agents/enroll":
                        if hub.enroll_limit is not None and len(hub.enrolled) >= hub.enroll_limit:
                            return self._send(429, {"detail": "Too many attempts"})
                        tok = "cah_" + "x" * 36 + f"{len(hub.enrolled):04d}"
                        hub.enrolled.append(body["hostname"]); hub.tokens[tok] = body["hostname"]
                        return self._send(200, {"host_agent_id": len(hub.enrolled), "agent_token": tok,
                                                "config": {"interval_seconds": 60}})
                    if self.path == "/api/host-agents/ingest":
                        auth = self.headers.get("Authorization", "")
                        if not auth.startswith("Bearer ") or auth[7:] not in hub.tokens:
                            return self._send(401, {"detail": "unauthorized"})
                        if hub.fail_every and n % hub.fail_every == 0:
                            return self._send(500, {"detail": "boom"})
                        hub.payloads.append(body)
                        return self._send(200, {"accepted": len(body["metrics"]), "rejected": 0, "config_version": 1})
                self._send(404, {})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown(); self.srv.server_close()


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.stop()


def _run(argv, capsys):
    rc = lt.main(argv)
    return rc, capsys.readouterr()


def test_payload_is_contract_v1():
    import random
    p = lt.build_payload("web-1", 7, 3, 1760000000.9, random.Random(1))
    assert p["schema"] == 1 and p["seq"] == 7 and p["config_version"] == 3 and p["sent_at"] == 1760000000
    assert {m["name"] for m in p["metrics"]} <= V1_NAMES and len(p["metrics"]) >= 15
    assert all(0 <= m["value"] <= 100 for m in p["metrics"] if m["name"].endswith("_percent"))
    assert all(m["ts"] == 1760000000 for m in p["metrics"])
    assert all(m["name"] not in ("disk_used_percent", "disk_inodes_used_percent") or "mount" in m["labels"] for m in p["metrics"])
    assert all(m["name"] not in ("net_rx_bytes_per_s", "net_tx_bytes_per_s") or "iface" in m["labels"] for m in p["metrics"])
    assert p["host"]["hostname"] == "web-1" and {s["state"] for s in p["services"]} == {"running"}


def test_percentile_nearest_rank():
    v = list(range(1, 101))
    assert lt.percentile(v, 50) == 50 and lt.percentile(v, 95) == 95 and lt.percentile(v, 99) == 99
    assert lt.percentile([], 95) == 0.0 and lt.percentile([4.0], 99) == 4.0


def test_refuses_without_confirm_dev_and_plain_http(hub, capsys):
    rc, out = _run(["--base-url", hub.url, "--enrollment-key", "cak_x", "--hosts", "1"], capsys)
    assert rc == 2 and "--confirm-dev" in out.err and hub.calls == 0
    rc, out = _run(["--confirm-dev", "--base-url", hub.url, "--enrollment-key", "cak_x", "--hosts", "1",
                    "--duration", "1"], capsys)
    assert rc == 2 and "http://" in out.err and hub.calls == 0


def test_full_run_enrolls_sends_and_reports(hub, capsys, tmp_path):
    tok = tmp_path / "tokens.json"
    rc, out = _run(["--confirm-dev", "--insecure-dev", "--base-url", hub.url, "--enrollment-key", "cak_x", "--hosts", "3",
                    "--interval", "1", "--duration", "2.5", "--save-tokens", str(tok), "--json"], capsys)
    s = json.loads(out.out)
    assert rc == 0 and s["error_rate"] == 0 and s["hosts"] == 3
    assert hub.enrolled == ["loadtest-0001", "loadtest-0002", "loadtest-0003"]
    assert s["requests"] == len(hub.payloads) >= 6 and s["ok"] == s["requests"]
    assert s["samples_accepted"] == sum(len(p["metrics"]) for p in hub.payloads) and s["rows_per_min_from_accepted"] > 0
    assert s["latency_ms"]["p50"] <= s["latency_ms"]["p95"] <= s["latency_ms"]["p99"] <= s["latency_ms"]["max"]
    for name in hub.enrolled:                                   # seq is strictly increasing per host
        seqs = [p["seq"] for p in hub.payloads if p["host"]["hostname"] == name]
        assert seqs == sorted(set(seqs))
    assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600 and len(json.loads(tok.read_text())) == 3
    assert "cah_" not in out.out + out.err                      # tokens are never printed


def test_tokens_file_skips_enrollment_and_counts_errors(capsys, tmp_path):
    h = FakeHub(fail_every=2)
    try:
        tok = tmp_path / "t.json"
        rc, _ = _run(["--confirm-dev", "--insecure-dev", "--base-url", h.url, "--enrollment-key", "cak_x", "--hosts", "1",
                      "--interval", "0.2", "--duration", "0.3", "--save-tokens", str(tok), "--json"], capsys)
        enrolled_before = len(h.enrolled)
        rc, out = _run(["--confirm-dev", "--insecure-dev", "--base-url", h.url, "--tokens-file", str(tok), "--hosts", "1",
                        "--interval", "0.2", "--duration", "1.1", "--json"], capsys)
        s = json.loads(out.out)
        assert len(h.enrolled) == enrolled_before                 # no new enrollment
        assert s["status_counts"].get("500", 0) >= 1 and s["error_rate"] > 0.01 and rc == 1
    finally:
        h.stop()


def test_enroll_rate_limit_is_reported_clearly(capsys):
    h = FakeHub(enroll_limit=2)
    try:
        rc, out = _run(["--confirm-dev", "--insecure-dev", "--base-url", h.url, "--enrollment-key", "cak_x", "--hosts", "5",
                        "--duration", "1"], capsys)
        assert rc == 2 and "enroll rate limit" in out.err and "after 2 hosts" in out.err
    finally:
        h.stop()
