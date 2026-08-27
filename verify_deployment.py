#!/usr/bin/env python3
"""
verify_deployment.py

Post-deploy sanity check for CloudOps Monitoring Hub — added after the
2026-08-25 incident where the EC2 Instances dashboard silently showed
"No data in last 6H" for CPU/network/disk graphs, with the app itself
reporting no error to the UI. Root causes found that day, in order of
how they were actually diagnosed:

  1. VictoriaMetrics unreachable from THIS box (app server), using the
     real VM_URL from .env. Turned out to be a Security Group on the
     VM box (3.109.181.40 / test-server) not allowing inbound from the
     app server's public IP — NOT the app server's own SG, and NOT the
     monitored EC2 instance's SG (both of which looked "correct" and
     sent us looking in the wrong place for a while).
  2. Schema drift: app.collector.scheduler's standard-tier query
     references resources.region ('r.region'), which errored with
     "Unknown column" — the live DB's actual column differs from what
     the code expects, most likely because a migration adding/renaming
     it was never applied on this specific DB.
  3. Self-referential AssumeRole: an aws_accounts row had a role_arn
     pointing at the *same* IAM role already attached to this instance
     (Monitoring_Role assuming Monitoring_Role) — this always fails
     AccessDenied and will never work without a trust-policy change,
     so it's cheap to detect and flag rather than rediscover in logs.

This script is a VISIBILITY tool, not a gate: every check prints a
clear OK / WARN / FAIL line but the script always exits 0, so it never
blocks setup.sh or update.sh from finishing. Run it standalone any time:

    python3 verify_deployment.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import mysql.connector

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root123")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")
VM_URL = os.getenv("VM_URL", "http://3.109.181.40")

TIMEOUT = 5

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def line(status, msg):
    print(f"[{status}] {msg}")


def get_own_public_ip():
    """Best-effort IMDSv2 lookup of this instance's own public IP, so
    a WARN about an SG fix can tell you the exact CIDR to add instead
    of making you go find it yourself."""
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        token = urllib.request.urlopen(req, timeout=2).read().decode()
        req2 = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/public-ipv4",
            headers={"X-aws-ec2-metadata-token": token},
        )
        return urllib.request.urlopen(req2, timeout=2).read().decode().strip()
    except Exception:
        return None


def get_own_iam_role():
    """Best-effort IMDSv2 lookup of the IAM role name attached to THIS
    instance, so check_3 can flag a role assuming itself."""
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        token = urllib.request.urlopen(req, timeout=2).read().decode()
        req2 = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            headers={"X-aws-ec2-metadata-token": token},
        )
        return urllib.request.urlopen(req2, timeout=2).read().decode().strip()
    except Exception:
        return None


def check_1_vm_reachable():
    print("\n--- 1. VictoriaMetrics reachability (VM_URL from .env) ---")
    print(f"    VM_URL = {VM_URL}")
    url = f"{VM_URL.rstrip('/')}/api/v1/query?query=up"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
        jobs = [r["metric"].get("job") for r in body.get("data", {}).get("result", [])]
        if jobs:
            line(OK, f"VM reachable, active scrape jobs: {jobs}")
        else:
            line(WARN, "VM reachable but returned zero active jobs — check YACE/vmagent on the VM box.")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        my_ip = get_own_public_ip()
        line(FAIL, f"Cannot reach {url}: {e}")
        line(FAIL, "This is almost always a Security Group on the VictoriaMetrics box "
                    "(NOT this server's SG, NOT the monitored instance's SG) not allowing "
                    "inbound from this server's IP on the port VM_URL uses.")
        if my_ip:
            line(FAIL, f"Add an inbound rule on the VM box's SG for TCP port "
                        f"{urllib.parse.urlsplit(VM_URL).port or 80} from {my_ip}/32.")
        else:
            line(FAIL, "Could not auto-detect this server's public IP (IMDS unreachable) — "
                        "find it manually and add it to the VM box's SG inbound rules.")


def check_2_schema_drift():
    print("\n--- 2. Schema drift (columns the collector's queries depend on) ---")
    try:
        conn = mysql.connector.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME, use_pure=True,
        )
        cursor = conn.cursor()
    except mysql.connector.Error as e:
        line(FAIL, f"Cannot connect to DB to check schema: {e}")
        return

    # resources.region — scheduler.py's standard-tier query failed on
    # "Unknown column 'r.region'". Don't assume what it SHOULD be called;
    # just show what's actually there so drift is visible either way.
    for table, probe_col in (("resources", "region"), ("aws_accounts", "account_name"), ("aws_accounts", "role_arn")):
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (DB_NAME, table, probe_col),
        )
        exists = cursor.fetchone()[0] > 0
        if exists:
            line(OK, f"{table}.{probe_col} exists.")
        else:
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                (DB_NAME, table),
            )
            actual_cols = [r[0] for r in cursor.fetchall()]
            line(WARN, f"{table}.{probe_col} is MISSING. Actual columns on {table}: {actual_cols}")
            line(WARN, f"Code querying {table}.{probe_col} (e.g. app/collector/scheduler.py, "
                        f"'Unknown column' errors in journalctl) will fail every cycle until "
                        f"either the column is added via migration or the query is updated to "
                        f"match the real column name above.")

    cursor.close()
    conn.close()


def check_3_self_assume_role():
    print("\n--- 3. Self-referential AssumeRole (role_arn == this instance's own role) ---")
    own_role = get_own_iam_role()
    if not own_role:
        line(WARN, "Could not read this instance's own IAM role via IMDS — skipping self-assume check.")
        return

    try:
        conn = mysql.connector.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME, use_pure=True,
        )
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='aws_accounts' AND COLUMN_NAME='role_arn'",
            (DB_NAME,),
        )
        if cursor.fetchone()[0] == 0:
            line(WARN, "aws_accounts.role_arn column not found — skipping self-assume check "
                        "(see schema drift warning above).")
            cursor.close()
            conn.close()
            return

        cursor.execute("SELECT id, role_arn FROM aws_accounts WHERE role_arn IS NOT NULL")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except mysql.connector.Error as e:
        line(FAIL, f"Cannot query aws_accounts.role_arn: {e}")
        return

    bad = [(acc_id, arn) for acc_id, arn in rows if arn and arn.rstrip("/").split("/")[-1] == own_role]
    if bad:
        for acc_id, arn in bad:
            line(FAIL, f"aws_accounts.id={acc_id} has role_arn={arn}, which is the SAME role "
                        f"already attached to this instance ({own_role}). A role can't assume "
                        f"itself this way — this account's AssumeRole calls will always fail "
                        f"AccessDenied. Point role_arn at the actual cross-account role in the "
                        f"target AWS account instead.")
    else:
        line(OK, f"No aws_accounts rows reference this instance's own role ({own_role}).")


def check_4_discovery_producing_resources():
    print("\n--- 4. Discovery actually finding resources for active accounts ---")
    # Added 2026-08-26: separate from checks 1-3, describe_polling can log
    # "0 EC2 instances" every cycle even once VM connectivity and the
    # same-account AssumeRole short-circuit are both fixed — e.g. bad/
    # missing credentials, an inactive account row, or a wrong account_id.
    # This doesn't diagnose WHICH of those it is (that needs the app logs:
    # journalctl -u monitoring-hub | grep describe_polling), it just flags
    # that the count is suspiciously zero so it isn't mistaken for "fixed"
    # once checks 1-3 are all OK.
    try:
        conn = mysql.connector.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME, use_pure=True,
        )
        cursor = conn.cursor()
    except mysql.connector.Error as e:
        line(FAIL, f"Cannot connect to DB to check discovery output: {e}")
        return

    # aws_accounts uses a `status` ENUM('active','inactive') in the real
    # schema (db_schema_only.sql) — NOT a boolean `active` column. Checked
    # for both defensively so this doesn't silently miscount on an
    # environment that genuinely differs.
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='aws_accounts' "
        "AND COLUMN_NAME IN ('status','active')",
        (DB_NAME,),
    )
    present_cols = {r[0] for r in cursor.fetchall()}

    if "status" in present_cols:
        cursor.execute("SELECT COUNT(*) FROM aws_accounts WHERE status='active'")
    elif "active" in present_cols:
        cursor.execute("SELECT COUNT(*) FROM aws_accounts WHERE active=1")
    else:
        cursor.execute("SELECT COUNT(*) FROM aws_accounts")
    active_accounts = cursor.fetchone()[0]

    if active_accounts == 0:
        line(WARN, "No active aws_accounts rows — nothing for the collector to discover "
                    "against, so an empty resources table here is expected, not a bug.")
        cursor.close(); conn.close()
        return

    cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='resources'", (DB_NAME,))
    if cursor.fetchone()[0] == 0:
        line(WARN, "resources table doesn't exist — skipping (see schema drift check above).")
        cursor.close(); conn.close()
        return

    cursor.execute("SELECT COUNT(*) FROM resources")
    resource_count = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    if resource_count == 0:
        line(WARN, f"{active_accounts} active account(s) in aws_accounts, but 0 rows in "
                    f"resources. Discovery is running but finding nothing — check for bad/"
                    f"missing credentials, a wrong account_id, or an inactive/misconfigured "
                    f"account row. Look for the actual error with: "
                    f"journalctl -u monitoring-hub | grep -i describe_polling")
    else:
        line(OK, f"{active_accounts} active account(s), {resource_count} resource(s) discovered.")


def main():
    print(f"=== verify_deployment.py — {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} ===")
    check_1_vm_reachable()
    check_2_schema_drift()
    check_3_self_assume_role()
    check_4_discovery_producing_resources()
    print("\n=== Done. WARN/FAIL above need a manual fix (SG rule, migration, or role_arn "
          "correction) — this script only surfaces them, it doesn't change anything. ===")


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (only needed inside check_1's error path)
    main()
