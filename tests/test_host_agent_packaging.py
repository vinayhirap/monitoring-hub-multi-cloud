# tests/test_host_agent_packaging.py
"""Guards the host-agent packaging contract (agent/install.sh, Makefile, Go module layout).

The Go behaviour itself is covered by `cd agent && go test -race ./...`. These checks only pin
the things a later edit could silently weaken: the systemd hardening/limits, the sha256 gate,
no cgo, and that the agent never touches the separate AI-agent feature (app/agent, /api/agent).
No app.* imports, so no install_stub() is needed (see tests/conftest.py).
"""
import os
import re
import subprocess

from conftest import REPO_ROOT

AGENT = os.path.join(REPO_ROOT, "agent")


def _read(rel):
    with open(os.path.join(AGENT, rel), encoding="utf-8") as fh:
        return fh.read()


def _unit():
    m = re.search(r"<<'UNITEOF'\n(.*?)\nUNITEOF", _read("install.sh"), re.S)
    assert m, "systemd unit heredoc not found in install.sh"
    return m.group(1)


def test_unit_has_required_limits_and_hardening():
    unit = _unit()
    for line in ("Restart=always", "MemoryMax=64M", "CPUQuota=10%", "NoNewPrivileges=yes",
                 "ProtectSystem=strict", "User=cloudops-agent", "RestartPreventExitStatus=3",
                 "ReadWritePaths=/var/lib/cloudops-agent"):
        assert line in unit.splitlines(), f"missing {line!r} in unit"
    assert "CapabilityBoundingSet=\n" in unit + "\n"       # no capabilities at all
    assert "ExecStart=/usr/local/bin/cloudops-agent run" in unit


def test_installer_verifies_checksum_before_installing():
    sh = _read("install.sh")
    assert "set -euo pipefail" in sh
    assert sh.index("CHECKSUM MISMATCH") < sh.index("useradd"), "checksum gate must precede any system change"
    assert sh.index("CHECKSUM MISMATCH") < sh.index('mv -f "$BIN.new"')
    assert "refusing http:// download" in sh


def test_shell_scripts_parse():
    for rel in ("install.sh", "uninstall.sh", "scripts/e2e-dev.sh"):
        r = subprocess.run(["bash", "-n", os.path.join(AGENT, rel)], capture_output=True, text=True)
        assert r.returncode == 0, f"{rel}: {r.stderr}"


def test_makefile_builds_static_both_arches_with_checksums():
    mk = _read("Makefile")
    assert mk.count("CGO_ENABLED=0") >= 2
    assert "GOARCH=amd64" in mk and "GOARCH=arm64" in mk and "SHA256SUMS" in mk


def test_go_module_has_no_external_dependencies_and_no_cgo():
    assert "require" not in _read("go.mod"), "agent is stdlib-only; a dependency needs a deliberate decision"
    for root, _dirs, files in os.walk(AGENT):
        for f in files:
            if f.endswith(".go"):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    assert 'import "C"' not in fh.read(), f


def test_agent_code_never_references_the_ai_agent_feature():
    for root, _dirs, files in os.walk(AGENT):
        for f in files:
            if f.endswith((".go", ".sh")):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    text = fh.read()
                assert "/api/agent/" not in text, f
                assert "app/app/agent" not in text and "app.agent" not in text, f
