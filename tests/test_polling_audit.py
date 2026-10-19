# tests/test_polling_audit.py
"""The 2026-10-19 polling audit: every promoted metric stays at its intended interval, and every listed
demotion is still a genuine slow-down. scripts/polling_audit.py --check is the single source of truth."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_polling_audit_check_passes():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "polling_audit.py"), "--check"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
