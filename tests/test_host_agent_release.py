# tests/test_host_agent_release.py
"""scripts/host_agent_release.sh: stage + sign + verify, versions immutable, tampering and bad input detected."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/host_agent_release.sh"


def _openssl3():
    if not shutil.which("openssl"):
        return False
    return "OpenSSL 3" in subprocess.run(["openssl", "version"], capture_output=True, text=True).stdout


pytestmark = pytest.mark.skipif(not _openssl3(), reason="needs OpenSSL 3.x")


def run(*args, cwd=None):
    return subprocess.run(["bash", str(SCRIPT), *map(str, args)], capture_output=True, text=True, cwd=cwd, timeout=60)


@pytest.fixture
def kit(tmp_path):
    key, pub = tmp_path / "k.pem", tmp_path / "p.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", key], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
    src = tmp_path / "bin"
    src.mkdir()
    (src / "cloudops-agent-linux-amd64").write_bytes(b"\x7fELF-amd64")
    (src / "cloudops-agent-linux-arm64").write_bytes(b"\x7fELF-arm64")
    (src / "install.sh").write_text("#!/bin/sh\necho hi\n")
    return key, pub, src, tmp_path / "dist"


def test_stage_then_verify_round_trip(kit):
    key, pub, src, out = kit
    r = run("stage", "0.1.0", src, key, out)
    assert r.returncode == 0, r.stderr
    d = out / "0.1.0"
    assert {p.name for p in d.iterdir()} == {"cloudops-agent-linux-amd64", "cloudops-agent-linux-arm64", "install.sh",
                                              "SHA256SUMS", "SHA256SUMS.sig"}
    assert '"latest":"0.1.0"' in (out / "manifest.json").read_text() and (out / "manifest.json.sig").exists()
    assert run("verify", "0.1.0", pub, out).stdout.strip().splitlines()[-1] == "OK 0.1.0"


def test_tampered_binary_or_sums_or_wrong_key_fails(kit, tmp_path):
    key, pub, src, out = kit
    run("stage", "0.1.0", src, key, out)
    d = out / "0.1.0"
    (d / "cloudops-agent-linux-amd64").write_bytes(b"evil")
    r = run("verify", "0.1.0", pub, out)
    assert r.returncode != 0 and "checksum mismatch" in r.stderr
    (d / "cloudops-agent-linux-amd64").write_bytes(b"\x7fELF-amd64")
    (d / "SHA256SUMS").write_text((d / "SHA256SUMS").read_text().replace("a", "b", 1))
    assert run("verify", "0.1.0", pub, out).returncode != 0
    other = tmp_path / "other.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", tmp_path / "o.key"], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", tmp_path / "o.key", "-pubout", "-out", other], check=True, capture_output=True)
    run("stage", "0.2.0", src, key, out)
    r = run("verify", "0.2.0", other, out)
    assert r.returncode != 0 and "BAD signature" in r.stderr


def test_versions_are_immutable_and_input_is_validated(kit):
    key, pub, src, out = kit
    assert run("stage", "0.1.0", src, key, out).returncode == 0
    r = run("stage", "0.1.0", src, key, out)
    assert r.returncode != 0 and "immutable" in r.stderr
    assert run("stage", "latest", src, key, out).returncode != 0
    assert run("stage", "1.0.0", src, tmp := key.parent / "nokey.pem", out).returncode != 0 and not tmp.exists()
    empty = key.parent / "empty"
    empty.mkdir()
    r = run("stage", "1.0.0", empty, key, out)
    assert r.returncode != 0 and "no cloudops-agent" in r.stderr
