#!/usr/bin/env python3
"""
scripts/smtp_check.py -- step-by-step SMTP diagnostic.

Run on the server, from the app directory, with the venv python:

    /opt/monitoring-hub/venv/bin/python3 scripts/smtp_check.py              # connect + login only
    /opt/monitoring-hub/venv/bin/python3 scripts/smtp_check.py --to me@x.com  # ...and send a test mail

It reads .env exactly like the app does, never prints the password, and
stops at the first failing step with a plain-English hint.
"""
import argparse
import os
import smtplib
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from app.email import mailer  # noqa: E402


def step(ok, label, detail=""):
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", help="send a real test email to this address")
    args = ap.parse_args()

    c = mailer._settings()
    print("Effective SMTP settings (.env):")
    print(f"  host={c['host'] or '(unset)'}  port={c['port']}  security={c['security']}  timeout={c['timeout']}s")
    print(f"  username={c['username'] or '(none)'}  password={'(set, %d chars)' % len(c['password']) if c['password'] else '(empty)'}")
    print(f"  from={c['mail_from']}  ehlo={c['ehlo'] or '(default: ' + socket.getfqdn() + ')'}")
    print(f"  public url={mailer.get_public_app_url()}")
    problems = mailer.config_problems()
    for p in problems:
        print(f"  [WARN] {p}")
    if not c["host"] or c["port"] is None:
        return 1

    print("\nChecks:")
    try:
        ip = socket.gethostbyname(c["host"])
        step(True, "DNS lookup", ip)
    except OSError as e:
        step(False, "DNS lookup", f"{e} -- check SMTP_HOST spelling / VPC DNS")
        return 2

    try:
        socket.create_connection((c["host"], c["port"]), timeout=c["timeout"]).close()
        step(True, f"TCP connect to port {c['port']}")
    except OSError as e:
        step(False, f"TCP connect to port {c['port']}", f"{e} -- security-group/NACL egress, or provider blocks this port")
        return 3

    kw = {"local_hostname": c["ehlo"]} if c["ehlo"] else {}
    ctx = ssl.create_default_context()
    try:
        srv = (smtplib.SMTP_SSL(c["host"], c["port"], timeout=c["timeout"], context=ctx, **kw)
               if c["security"] == "ssl" else smtplib.SMTP(c["host"], c["port"], timeout=c["timeout"], **kw))
        with srv:
            code, banner = srv.ehlo()
            step(code == 250, "EHLO", banner.decode(errors="replace").splitlines()[0][:80])
            if c["security"] == "starttls":
                srv.starttls(context=ctx)
                srv.ehlo()
                step(True, "STARTTLS (certificate verified)")
            if c["username"]:
                srv.login(c["username"], c["password"])
                step(True, "AUTH login")
            else:
                step(True, "AUTH skipped (no SMTP_USERNAME)")
    except Exception as e:
        step(False, "SMTP session", mailer._short_err(e))
        return 4

    if args.to:
        ok, err = mailer.send_email_ex(
            args.to, "[CloudOps] SMTP test",
            "If you can read this, outbound mail from CloudOps works.\n")
        step(ok, f"send test mail to {args.to}", err or "accepted by the server -- now check the inbox AND spam")
        return 0 if ok else 5
    print("\nConnection and login work. Re-run with --to you@example.com to send a real message.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
