#!/usr/bin/env python3
"""
apply_ec2_memory_disk_metrics.py — adds Memory Utilization % and Disk
Space Utilized % to the EC2 detail dashboard, gated on whether the
CloudWatch Agent is actually installed and reporting for that
instance, and removes the Disk Read / Disk Write boxes (instance-store
metrics that never have data on modern EBS-backed instances).

WHY CLOUDWATCH AGENT DETECTION IS NEEDED
EC2's hypervisor-level CloudWatch metrics (CPU, Network, Status Check)
are always available for every instance. Memory and disk-space
utilization are NOT — AWS has no visibility into what's happening
inside the guest OS unless the CloudWatch Agent is installed and
configured to publish `mem_used_percent` / `disk_used_percent` into
the CWAgent namespace. There's no "agent installed" flag exposed
anywhere else (SSM association status only proves an install was
*attempted* via SSM, not that the agent is actually running), so the
only reliable signal is: does the CWAgent namespace have ANY metric
for this instance ID right now.

WHAT THIS CHANGES

  - app/aws/collector_direct.py — get_ec2_metric_series() now:
      1. Checks CWAgent presence via a free ListMetrics call
         (cloudwatch:ListMetrics, no cost) against the CWAgent
         namespace, filtered to this instance's InstanceId dimension.
      2. Only if present, looks up the exact dimension set CWAgent
         published mem_used_percent / disk_used_percent under (disk
         metrics also carry `path`/`device`/`fstype` per mount point —
         GetMetricData needs the complete set a datapoint was actually
         published under, not just InstanceId) and pulls both series
         via the same batched GetMetricData helper (_gmd_series)
         already used for the Lambda/ECS/ALB boto3-fallback paths
         elsewhere in this file — same call pattern, same cost model,
         nothing new architecturally.
      3. Returns a new "cwagent_installed" boolean alongside the
         series, so the frontend can decide whether to render the
         chart boxes AT ALL (not just whether they have data).
      4. Skips the CWAgent GetMetricData calls entirely (zero extra
         cost) when the agent isn't installed, rather than making them
         and getting empty series back.
      Existing series (cpu/network_in/network_out/disk_read/disk_write)
      are untouched — still VM-backed, same as before. disk_read/
      disk_write are still COMPUTED here (harmless, a free VM query,
      and something else may read this dict later) — they're only
      removed from the DASHBOARD, i.e. the frontend, per below.

  - frontend/src/pages/ServiceDetail.jsx — the EC2 metrics grid:
      * Removes the "Disk Read (bytes)" / "Disk Write (bytes)" chart
        boxes outright (not just hidden-when-empty — gone).
      * Adds "Memory Utilization %" and "Disk Space Utilized %" boxes,
        wrapped in `{metrics.cwagent_installed && (...)}` — if the
        agent isn't installed/reporting, neither box is rendered at
        all, not even as an empty/placeholder state.

PREREQUISITE: the cross-account role this app assumes for CloudWatch
calls needs cloudwatch:ListMetrics and cloudwatch:GetMetricData (both
already required for every other boto3-fallback path in this file —
if Lambda/ECS/ALB metrics already work, this will too).

Usage:
    python apply_ec2_memory_disk_metrics.py --dry-run
    python apply_ec2_memory_disk_metrics.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-ec2-memory-disk-metrics"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# Patches
# ─────────────────────────────────────────────────────────────────────────
PATCHES = [
    (
        "app/aws/collector_direct.py",
        [
            (
r'''# ── Metric series — EC2 (now VM-backed) ──────────────────────────────────

def get_ec2_metric_series(instance_id, region=None, hours=6) -> dict:
    try:
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_InstanceId="{instance_id}"'

        def s(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )
        return {
            "instance_id":  instance_id,
            "cpu":          s("aws_ec2_cpuutilization_average"),
            "network_in":   s("aws_ec2_network_in_average"),
            "network_out":  s("aws_ec2_network_out_average"),
            "disk_read":    s("aws_ec2_disk_read_bytes_sum"),
            "disk_write":   s("aws_ec2_disk_write_bytes_sum"),
            "period_hours": hours,
            "period_secs":  period,
        }
    except Exception as e:
        logger.warning(f"EC2 series [{instance_id}]: {e}")
        return {"instance_id": instance_id, "cpu": [], "network_in": [],
                "network_out": [], "disk_read": [], "disk_write": []}
''',
                r'''# ── Metric series — EC2 (now VM-backed) ──────────────────────────────────

def _ec2_cwagent_installed(instance_id, region=None) -> bool:
    """
    True iff the CWAgent CloudWatch namespace has ANY metric published
    for this instance — the only reliable signal that the CloudWatch
    Agent is actually installed AND reporting (an SSM association only
    proves an install was *attempted*, not that the agent process is
    running and publishing). Cached briefly since this runs on every
    EC2 detail-page open.
    """
    return _cached(
        f"cwagent_present_{instance_id}_{region}",
        lambda: _ec2_cwagent_installed_raw(instance_id, region),
        ttl=300,
    )


def _ec2_cwagent_installed_raw(instance_id, region=None) -> bool:
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        resp = cw.list_metrics(
            Namespace="CWAgent",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )
        return bool(resp.get("Metrics"))
    except Exception as e:
        logger.warning(f"CWAgent presence check [{instance_id}]: {e}")
        return False


def _ec2_cwagent_dimensions(cw, metric_name, instance_id):
    """
    Find the exact dimension set CWAgent published `metric_name` under
    for this instance. mem_used_percent is dimensioned by InstanceId
    alone, but disk_used_percent also carries `path` / `device` /
    `fstype` (CWAgent's own defaults, one metric per mount point) —
    GetMetricData needs the COMPLETE dimension set a datapoint was
    actually published under; a partial match (InstanceId only)
    returns nothing. If multiple mount points are reporting, prefer
    the root filesystem ("/" on Linux, "C:" on Windows) since that's
    what "disk space utilized" means to someone glancing at the
    dashboard; otherwise fall back to whichever mount point
    CloudWatch happens to return first.
    """
    try:
        resp = cw.list_metrics(
            Namespace="CWAgent", MetricName=metric_name,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )
        metrics = resp.get("Metrics", [])
        if not metrics:
            return None
        for m in metrics:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            if dims.get("path") in ("/", "C:"):
                return m["Dimensions"]
        return metrics[0]["Dimensions"]
    except Exception as e:
        logger.warning(f"CWAgent dimension lookup [{instance_id}/{metric_name}]: {e}")
        return None


def get_ec2_metric_series(instance_id, region=None, hours=6) -> dict:
    try:
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_InstanceId="{instance_id}"'

        def s(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )

        cwagent_installed = _ec2_cwagent_installed(instance_id, region)

        # Memory/disk-space utilization ONLY exist if the CloudWatch
        # Agent is installed and reporting — EC2 never publishes these
        # from the hypervisor side the way CPU/Network/StatusCheck are.
        # Skip the GetMetricData calls entirely when the agent isn't
        # present (zero extra cost) rather than making them and
        # getting empty series back — the frontend uses
        # cwagent_installed to decide whether to render the chart
        # boxes at all, not just whether they have data.
        mem_utilization   = []
        disk_used_percent = []
        if cwagent_installed:
            try:
                cw        = boto3.client("cloudwatch", region_name=region)
                cw_period = max(period, 60)  # CWAgent's own default reporting interval

                mem_dims  = _ec2_cwagent_dimensions(cw, "mem_used_percent",  instance_id)
                disk_dims = _ec2_cwagent_dimensions(cw, "disk_used_percent", instance_id)

                queries = []
                if mem_dims:
                    queries.append(_make_query("mem",  "CWAgent", "mem_used_percent",  mem_dims,  "Average", cw_period))
                if disk_dims:
                    queries.append(_make_query("disk", "CWAgent", "disk_used_percent", disk_dims, "Average", cw_period))

                if queries:
                    fb = _gmd_series(cw, queries, hours)
                    mem_utilization   = fb.get("mem", [])
                    disk_used_percent = fb.get("disk", [])
            except Exception as e:
                logger.warning(f"CWAgent series [{instance_id}]: {e}")

        return {
            "instance_id":       instance_id,
            "cpu":               s("aws_ec2_cpuutilization_average"),
            "network_in":        s("aws_ec2_network_in_average"),
            "network_out":       s("aws_ec2_network_out_average"),
            "disk_read":         s("aws_ec2_disk_read_bytes_sum"),
            "disk_write":        s("aws_ec2_disk_write_bytes_sum"),
            "cwagent_installed": cwagent_installed,
            "mem_utilization":   mem_utilization,
            "disk_used_percent": disk_used_percent,
            "period_hours":      hours,
            "period_secs":       period,
        }
    except Exception as e:
        logger.warning(f"EC2 series [{instance_id}]: {e}")
        return {"instance_id": instance_id, "cpu": [], "network_in": [],
                "network_out": [], "disk_read": [], "disk_write": [],
                "cwagent_installed": False, "mem_utilization": [], "disk_used_percent": []}
''',
            ),
        ],
    ),
    (
        "frontend/src/pages/ServiceDetail.jsx",
        [
            (
                r'''            {service === "EC2" && <>
              <MetricChart title="Network In (KB)"  data={metrics.network_in?.map(d => ({ ...d, v: d.v / 1024 }))}  color="#22c55e" unit="KB" timeRange={rangLabel} />
              <MetricChart title="Network Out (KB)" data={metrics.network_out?.map(d => ({ ...d, v: d.v / 1024 }))} color="#7c6ee0" unit="KB" timeRange={rangLabel} />
              <MetricChart title="Disk Read (bytes)"  data={metrics.disk_read}  color="#fbbf24" unit="B" timeRange={rangLabel} />
              <MetricChart title="Disk Write (bytes)" data={metrics.disk_write} color="#fb7185" unit="B" timeRange={rangLabel} />
            </>}
''',
                r'''            {service === "EC2" && <>
              <MetricChart title="Network In (KB)"  data={metrics.network_in?.map(d => ({ ...d, v: d.v / 1024 }))}  color="#22c55e" unit="KB" timeRange={rangLabel} />
              <MetricChart title="Network Out (KB)" data={metrics.network_out?.map(d => ({ ...d, v: d.v / 1024 }))} color="#7c6ee0" unit="KB" timeRange={rangLabel} />
              {/* Disk Read/Write are instance-store metrics that modern
                  EBS-backed instances never publish — removed outright
                  rather than shown as permanently-empty boxes. Memory
                  and disk-space utilization require the CloudWatch
                  Agent and are hidden completely (not shown as "no
                  data") when it isn't installed/reporting for this
                  instance — see cwagent_installed in get_ec2_metric_series. */}
              {metrics.cwagent_installed && <>
                <MetricChart title="Memory Utilization %"  data={metrics.mem_utilization}   color="#7c6ee0" unit="%" threshold={90} timeRange={rangLabel} />
                <MetricChart title="Disk Space Utilized %" data={metrics.disk_used_percent} color="#fbbf24" unit="%" threshold={90} timeRange={rangLabel} />
              </>}
            </>}
''',
            ),
        ],
    ),
]


# ─────────────────────────────────────────────────────────────────────────
# Preflight / apply / validate
# ─────────────────────────────────────────────────────────────────────────

def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue  # already patched
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for line in report:
        print(line)

    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext steps:")
            print("  1. Backend: full uvicorn restart (not --reload) to pick up")
            print("     app/aws/collector_direct.py.")
            print("  2. Frontend: cd frontend && npm install (if needed) && npm run build")
            print("  3. Verify IAM: the role this app assumes per account needs")
            print("     cloudwatch:ListMetrics + cloudwatch:GetMetricData (both already")
            print("     required for the existing Lambda/ECS/ALB boto3-fallback paths —")
            print("     if those already work for an account, this will too).")
            print("  4. Open an EC2 instance's detail page:")
            print("       - Instance WITHOUT the CloudWatch Agent: Memory/Disk-space")
            print("         boxes are simply absent. Disk Read/Write are gone entirely.")
            print("       - Instance WITH the agent (mem_used_percent/disk_used_percent")
            print("         actively publishing): both new charts appear and populate.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
