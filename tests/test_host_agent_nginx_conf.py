# tests/test_host_agent_nginx_conf.py
"""Host agent phase F: deploy/nginx.conf has the agent locations with the right limits and does not weaken the headers."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONF = (ROOT / "deploy/nginx.conf").read_text()
NO_COMMENTS = "\n".join(line.split("#", 1)[0] for line in CONF.splitlines())

INGEST = "location = /api/host-agents/ingest"
ENROLL = "location = /api/host-agents/enroll"


def _block(header: str) -> str:
    start = NO_COMMENTS.index(header)
    i = NO_COMMENTS.index("{", start)
    depth, j = 0, i
    while True:
        depth += NO_COMMENTS[j] == "{"
        depth -= NO_COMMENTS[j] == "}"
        if depth == 0:
            return NO_COMMENTS[i:j + 1]
        j += 1


def test_body_limit_matches_the_default_hub_limit():
    assert re.search(r"client_max_body_size\s+256k;", _block(INGEST))     # 256k == 262144 == HOST_AGENT_MAX_BODY_BYTES default
    assert 256 * 1024 == 262144
    assert re.search(r"client_max_body_size\s+16k;", _block(ENROLL))


def test_agent_locations_have_limits_timeouts_keepalive_and_buffered_requests():
    for loc in (INGEST, ENROLL):
        b = _block(loc)
        assert "limit_req " in b and "limit_conn " in b and "limit_req_status 429" in b
        assert "client_body_timeout" in b and "proxy_read_timeout" in b and "proxy_connect_timeout" in b
        assert 'proxy_set_header Connection ""' in b and "proxy_http_version 1.1" in b
        assert "proxy_request_buffering on" in b                  # slow-loris bodies stop at nginx
        assert "proxy_set_header X-Forwarded-Proto $scheme" in b and "proxy_set_header X-Real-IP $remote_addr" in b
        assert "Upgrade" not in b                                 # no websocket upgrade headers on the agent channel
        assert "proxy_pass http://monitoring_hub_agents;" in b
    assert re.search(r"upstream monitoring_hub_agents \{[^}]*keepalive \d+;", NO_COMMENTS)
    for zone in ("ha_enroll", "ha_ingest", "ha_conn"):
        assert re.search(rf"^(limit_req_zone|limit_conn_zone)\s+\$binary_remote_addr\s+zone={zone}:", NO_COMMENTS, re.M)


def test_agent_locations_do_not_break_header_inheritance():
    # nginx drops inherited add_header directives in any location that declares its own, so the agent
    # locations must declare none (deploy/apply_security_headers.sh adds them once at server level).
    for loc in (INGEST, ENROLL):
        b = _block(loc)
        assert "add_header" not in b and "proxy_hide_header" not in b


def test_exact_locations_precede_the_api_prefix_and_existing_blocks_are_intact():
    assert CONF.index(INGEST) < CONF.index("location /api/ {")
    for frag in ("location / {", "location /api/ {", "location /ws/ {", "proxy_pass http://127.0.0.1:8000;",
                 "proxy_read_timeout 3600s;", "server_name _;", "listen 80;"):
        assert frag in CONF
    # deploy.sh / setup.sh substitute these literals with sed
    assert "root /opt/monitoring-hub/app/frontend/dist;" in CONF


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx binary not installed")
def test_nginx_accepts_the_config():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "dist").mkdir()
        site = CONF.replace("/opt/monitoring-hub/app/frontend/dist", str(d / "dist")).replace("listen 80;", "listen 127.0.0.1:18099;")
        (d / "site.conf").write_text(site)
        (d / "nginx.conf").write_text(
            f"pid {d}/n.pid; error_log {d}/e.log;\nevents {{}}\nhttp {{ access_log off; client_body_temp_path {d}/b; "
            f"proxy_temp_path {d}/p; fastcgi_temp_path {d}/f; uwsgi_temp_path {d}/u; scgi_temp_path {d}/s;\n"
            f"include {d}/site.conf; }}\n")
        r = subprocess.run(["nginx", "-t", "-c", str(d / "nginx.conf"), "-p", str(d)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr


def test_download_location_is_static_read_only_and_keeps_headers():
    b = _block("location ^~ /dl/host-agent/")
    assert "alias /opt/monitoring-hub/host-agent-dist/;" in b and "autoindex off" in b
    assert "limit_req zone=ha_dl" in b and "limit_except GET HEAD" in b
    assert "add_header" not in b and "proxy_pass" not in b
    assert re.search(r"^limit_req_zone\s+\$binary_remote_addr\s+zone=ha_dl:", NO_COMMENTS, re.M)
