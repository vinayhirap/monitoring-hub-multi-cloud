# tests/test_audit_p10_ui_polish.py
"""Audit B2/B8/B10/B13/B15 - UI polish batch. Source-level guards plus the one backend formatter."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend/src"


def _port_formatter():
    src = (ROOT / "app/collector/cspm.py").read_text()
    body = src[src.index("def format_sg_ports("):src.index("def _check_open_security_groups(")]
    ns = {}
    exec(body, ns)
    return ns["format_sg_ports"]


def test_security_group_port_wording():
    f = _port_formatter()
    assert f("-1", None, None) == "All traffic, all ports"            # was "Ports None-None"
    assert f("icmp", -1, -1) == "ICMP, all types"                     # was "Ports -1--1"
    assert f("icmp", 8, 0) == "ICMP type 8"
    assert f("tcp", 0, 65535) == "All TCP ports"                      # was "Ports 0-65535"
    assert f("tcp", 22, 22) == "TCP port 22"
    assert f("udp", 1000, 2000) == "UDP ports 1000-2000"
    assert f("6", 443, 443) == "TCP port 443"
    assert "None" not in f(None, None, None)


def test_finding_text_uses_the_formatter_and_severity_logic_is_unchanged():
    src = (ROOT / "app/collector/cspm.py").read_text()
    assert "format_sg_ports(perm.get('IpProtocol'), from_port, to_port)" in src
    assert 'f"Ports {from_port}-{to_port}' not in src
    assert 'hits_sensitive = port_span is None or bool(port_span & SENSITIVE_PORTS)' in src


def test_pages_use_the_shared_time_formatter_not_us_style_dates():
    for page in ("Compliance", "SecurityFindings", "Incidents", "MaintenanceWindows", "Search", "Reports", "GenericServiceDetail"):
        text = (FE / f"pages/{page}.jsx").read_text()
        assert "utils/timeFormat" in text, page
    # the ambiguous US-style full timestamps are gone from these pages
    for page in ("SecurityFindings", "Incidents", "MaintenanceWindows", "Search", "GenericServiceDetail"):
        text = (FE / f"pages/{page}.jsx").read_text()
        assert not re.search(r'toLocale\w*String\("en-US"', text), page
    comp = (FE / "pages/Compliance.jsx").read_text()
    assert "ar-date" not in comp and "function formatTs" not in comp          # one timestamp per row, not two formats


def test_settings_copy_does_not_expose_server_paths():
    text = (FE / "pages/Settings.jsx").read_text()
    visible = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    visible = re.sub(r"^\s*//.*$", "", visible, flags=re.M)
    assert "app/collector/scheduler.py" not in visible


def test_status_page_has_its_own_title_and_toasts_are_a_live_region():
    assert "Service status" in (FE / "pages/StatusPagePublic.jsx").read_text()
    toast = (FE / "components/AlertToast.jsx").read_text()
    assert 'role="status" aria-live="polite"' in toast
