# tests/test_host_agent_platforms.py
"""Cross-platform host agent: the hub accepts what the Windows and macOS collectors send, and the packaging for
those OSes keeps its safety properties. The collectors themselves are unit-tested in Go (`cd agent && go test ./...`);
real Windows/macOS hosts are NOT available to CI, which is why docs/HOST_AGENT_PLATFORMS.md lists what is untested."""
import os
import re
import subprocess
import time

import pytest

from tests.conftest import REPO_ROOT, load_module

AGENT = os.path.join(REPO_ROOT, "agent")
SHIPPED = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64.exe", "windows-arm64.exe")


def _read(rel):
    with open(os.path.join(AGENT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def mm():
    return load_module("app/hostagents/metric_map.py")


def _payload(os_name, arch, metrics, services=()):
    now = int(time.time())
    return {
        "schema": 1, "agent_version": "0.2.0", "config_version": 1, "seq": 1, "sent_at": now,
        "host": {"hostname": "host-1", "instance_id": None, "os": os_name, "arch": arch, "boot_time": now - 100},
        "metrics": [dict(m, ts=now) for m in metrics], "services": list(services),
    }


# ── hub side: label conventions for the new OSes ─────────────────────────────────────────────────────────────

def test_windows_system_drive_is_the_root_series_and_other_drives_get_a_slug(mm):
    assert mm.stored_name("disk_used_percent", "C:") == "disk_used_percent"
    assert mm.stored_name("disk_used_percent", "D:") == "disk_used_percent__d"
    assert mm.is_real_mount("C:") and mm.is_real_mount("D:")


def test_macos_mounts_and_interfaces_map_cleanly(mm):
    assert mm.stored_name("disk_used_percent", "/") == "disk_used_percent"
    assert mm.stored_name("disk_used_percent", "/Volumes/Backup Drive") == "disk_used_percent__volumes_backup_drive"
    assert mm.is_real_mount("/Volumes/Backup Drive")
    assert mm.stored_name("net_rx_bytes_per_s", "en0") == "host_net_rx_bytes_per_s__en0"


def test_hub_accepts_a_windows_payload(mm):
    body = _payload("windows", "amd64", [
        {"name": "cpu_percent", "value": 12.5}, {"name": "mem_used_percent", "value": 61.0},
        {"name": "mem_available_bytes", "value": 6.4e9},
        {"name": "disk_used_percent", "value": 71.2, "labels": {"mount": "C:"}},
        {"name": "disk_used_percent", "value": 10.0, "labels": {"mount": "D:"}},
        {"name": "proc_count", "value": 143}, {"name": "uptime_seconds", "value": 7200},
    ], [{"name": "Spooler", "state": "running"}, {"name": "W32Time", "state": "stopped"}])
    clean, rejected = mm.validate_payload(body, max_batch_metrics=500)
    assert rejected == 0
    assert clean["host"]["os"] == "windows"
    assert {s[0] for s in clean["samples"]} == {
        "host_cpu_percent", "mem_used_percent", "host_mem_available_bytes", "disk_used_percent",
        "disk_used_percent__d", "host_proc_count", "host_uptime_seconds"}
    assert len(clean["services"]) == 2


def test_hub_accepts_a_macos_payload(mm):
    body = _payload("darwin", "arm64", [
        {"name": "cpu_percent", "value": 8.1}, {"name": "load1", "value": 1.52}, {"name": "swap_used_percent", "value": 25},
        {"name": "disk_used_percent", "value": 60, "labels": {"mount": "/"}},
        {"name": "disk_used_percent", "value": 40, "labels": {"mount": "/Volumes/Backup"}},
        {"name": "net_rx_bytes_per_s", "value": 10000, "labels": {"iface": "en0"}},
        {"name": "net_tx_bytes_per_s", "value": 5000, "labels": {"iface": "en0"}},
    ], [{"name": "com.example.web", "state": "running"}, {"name": "com.example.crashed", "state": "failed"}])
    clean, rejected = mm.validate_payload(body, max_batch_metrics=500)
    assert rejected == 0 and clean["host"]["os"] == "darwin"
    assert "disk_used_percent__volumes_backup" in {s[0] for s in clean["samples"]}


def test_enroll_body_accepts_every_shipped_os_and_arch():
    from pydantic import BaseModel  # noqa: F401  (only to fail loudly if the validator package is missing)
    src = open(os.path.join(REPO_ROOT, "app/api/host_agents.py"), encoding="utf-8").read()
    m = re.search(r'^_SAFE_TEXT = r"(.*)"$', src, re.M)
    assert m, "_SAFE_TEXT pattern not found"
    pat = re.compile(m.group(1))
    for goos in ("linux", "darwin", "windows"):
        assert pat.match(goos), goos
    for arch in ("amd64", "arm64"):
        assert pat.match(arch), arch


# ── packaging ────────────────────────────────────────────────────────────────────────────────────────────────

def test_makefile_ships_every_os_arch_static_and_checksummed():
    mk = _read("Makefile")
    for t in SHIPPED:
        assert f"cloudops-agent-{t}" in mk, t
    for goos in ("linux", "darwin", "windows"):
        assert f"GOOS={goos}" in mk
    assert mk.count("CGO_ENABLED=0") >= 6
    assert "sha256sum $(notdir $(BINS))" in mk, "SHA256SUMS must list every shipped binary"
    for script in ("install.sh", "uninstall.sh", "install-macos.sh", "uninstall-macos.sh", "install.ps1", "uninstall.ps1"):
        assert script in mk and os.path.exists(os.path.join(AGENT, script)), script


def test_macos_scripts_parse_and_verify_before_installing():
    for rel in ("install-macos.sh", "uninstall-macos.sh"):
        r = subprocess.run(["bash", "-n", os.path.join(AGENT, rel)], capture_output=True, text=True)
        assert r.returncode == 0, f"{rel}: {r.stderr}"
    sh = _read("install-macos.sh")
    assert "set -euo pipefail" in sh and "refusing http:// download" in sh
    assert sh.index("CHECKSUM MISMATCH") < sh.index("dscl . -create"), "checksum gate must precede any system change"
    assert sh.index("CHECKSUM MISMATCH") < sh.index('mv -f "$BIN.new"')
    assert "UserName" in sh and "_cloudops-agent" in sh, "must not run as root"
    assert "${" + "x,," not in sh and "declare -A" not in sh and "mapfile" not in sh, "stock macOS bash is 3.2"


def test_windows_installer_verifies_before_installing_and_locks_down_the_token_directory():
    ps = _read("install.ps1")
    assert "$ErrorActionPreference = 'Stop'" in ps
    gate = ps.index("CHECKSUM MISMATCH")
    assert gate < ps.index("Copy-Item -LiteralPath $TmpBin -Destination $Exe"), "checksum gate must precede install"
    assert gate < ps.index("Register-ScheduledTask")
    assert "refusing http:// download" in ps
    assert "*S-1-5-18" in ps and "*S-1-5-32-544" in ps, "ACLs must use SIDs (locale independent)"
    assert "/inheritance:r" in ps
    assert "'S-1-5-18'" in ps and "ServiceAccount" in ps, "task must run as SYSTEM"
    # the enrollment key must only ever live in the process environment, never on disk or in the task definition
    assert "Remove-Item Env:\\CLOUDOPS_AGENT_ENROLL_KEY" in ps
    assert not re.search(r"-Argument[^\n]*\$Key", ps)


def test_insecure_dev_reaches_the_running_service_on_macos_and_windows():
    assert "$INSECURE_XML" in _read("install-macos.sh") and "--insecure-dev</string>" in _read("install-macos.sh")
    assert "$runArgs += ' --insecure-dev'" in _read("install.ps1")


def test_every_installer_gates_http_downloads_behind_the_insecure_flag():
    assert "--insecure-dev" in _read("install-macos.sh") and "-InsecureDev" in _read("install.ps1")


def test_no_agent_code_touches_the_ai_agent_feature_in_the_new_files():
    for rel in ("install-macos.sh", "uninstall-macos.sh", "install.ps1", "uninstall.ps1"):
        text = _read(rel)
        assert "/api/agent/" not in text and "app.agent" not in text, rel


# ── the agent and the hub must agree on which mounts are ignored ──────────────────────────────────────────────

def _agent_ignore_prefixes():
    m = re.search(r"var hubIgnoredMountPrefixes = \[\]string\{(.*?)\}", _read("internal/collect/disk.go"), re.S)
    assert m, "hubIgnoredMountPrefixes not found in agent/internal/collect/disk.go"
    return tuple(re.findall(r'"([^"]*)"', m.group(1)))


def test_agent_ignore_list_is_identical_to_the_hubs(mm):
    """If these drift, the agent sends mounts the hub rejects (a WARN per payload per host, forever) or drops ones the hub keeps."""
    assert _agent_ignore_prefixes() == mm._IGNORED_PATH_PREFIXES


@pytest.mark.parametrize("path", ["/", "/boot", "/boot/efi", "/run", "/run/user/1", "/running", "/dev/shm", "/proc", "/sys",
                                  "/snap/x/1", "/var/snap/y", "/snapshots", "/mnt/data", "/home", "C:", "D:", "/Volumes/Backup"])
def test_agent_skips_exactly_what_the_hub_would_reject(mm, path):
    agent_skips = path != "/" and any(path.startswith(p) for p in _agent_ignore_prefixes())
    assert agent_skips == (not mm.is_real_mount(path)), path
