#!/usr/bin/env python3
"""
apply_alb_key_and_service_tile_fix.py

Idempotent patch for monitoring-hub-multi-cloud. Run from the repo root:

    python apply_alb_key_and_service_tile_fix.py .

What it does
------------
1. ServiceList.jsx / ServiceDetail.jsx / Alerts.jsx
   metric_catalog's real key for Application Load Balancer is "alb"
   (see app/aws/metric_catalog_data.py), not "elb". These three files
   only recognized "elb", so the ALB tile was misrouted to
   console-link mode instead of its internal detail page, showed the
   wrong description, never got alert badges, and ALB alert rows
   couldn't deep-link. Adds "alb" as an alias everywhere "elb" was
   used as a lookup key (additive, backward-compatible — "elb" stays
   working too).

2. collector_direct.py / live_data.py
   Adds real collect_cognito_user_pools and
   collect_global_accelerator_accelerators collectors and registers
   them in _RESOURCE_COLLECTORS. These were the only 2 of 41 curated
   services with no collector, so their tiles could never be hidden
   even at zero resources (the "fail open on missing collector"
   safety rule was permanently open for just these two).

3. ServiceList.jsx ServiceCard
   Splits the icon-circle's border shorthand into
   borderWidth/borderStyle/borderColor longhands so hover no longer
   mixes shorthand+longhand on the same style object — this was the
   cause of the repeated "Removing a style property during rerender
   (borderColor)" console warning firing on every tile hover.

Safe to re-run: every edit is guarded, so running this twice on an
already-patched tree is a no-op.
"""
import sys
import re
from pathlib import Path


def patch(path: Path, replacements, label):
    if not path.exists():
        print(f"  SKIP  {path} (not found)")
        return False
    text = path.read_text(encoding="utf-8")
    original = text
    for old, new, guard in replacements:
        if guard in text:
            continue  # already applied
        if old not in text:
            print(f"  WARN  {path}: expected snippet not found, skipping one edit")
            continue
        text = text.replace(old, new, 1)
    if text != original:
        path.write_text(text, encoding="utf-8")
        print(f"  OK    {path} ({label})")
        return True
    else:
        print(f"  SKIP  {path} (already patched or nothing to do)")
        return False


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    print(f"Applying patch to {root}\n")

    # 1. frontend/src/pages/ServiceList.jsx
    p = root / "frontend/src/pages/ServiceList.jsx"
    patch(p, [
        (
            's3: "Object storage buckets", ecs: "Container services", elb: "Load balancers", lambda: "Serverless functions",',
            's3: "Object storage buckets", ecs: "Container services", elb: "Load balancers", alb: "Load balancers", lambda: "Serverless functions",',
            'alb: "Load balancers",',
        ),
        (
            'const CORE_AWS_SERVICES = new Set(["ec2", "ebs", "rds", "lambda", "s3", "elb", "ecs"]);',
            'const CORE_AWS_SERVICES = new Set(["ec2", "ebs", "rds", "lambda", "s3", "elb", "alb", "ecs"]);',
            '"elb", "alb", "ecs"',
        ),
        (
            '      elb: r => r?.includes("alb") || r?.includes("elb") || r?.includes("loadbalancer"),\n'
            '      s3: r => r?.includes("s3"), ecs: r => r?.includes("ecs"),',
            '      elb: r => r?.includes("alb") || r?.includes("elb") || r?.includes("loadbalancer"),\n'
            '      alb: r => r?.includes("alb") || r?.includes("elb") || r?.includes("loadbalancer"),\n'
            '      s3: r => r?.includes("s3"), ecs: r => r?.includes("ecs"),',
            'alb: r => r?.includes("alb")',
        ),
        (
            '      <div style={{\n'
            '        width:64, height:64, borderRadius:"50%",\n'
            '        background: svc.color+"15", border:`1px solid ${svc.color}25`,\n'
            '        display:"flex", alignItems:"center", justifyContent:"center",\n'
            '        margin:"0 auto 14px", transition:"background .18s",\n'
            '        ...(hovered ? { background:svc.color+"25", borderColor:svc.color+"50" } : {}),\n'
            '      }}>',
            '      <div style={{\n'
            '        width:64, height:64, borderRadius:"50%",\n'
            '        background: hovered ? svc.color+"25" : svc.color+"15",\n'
            '        borderWidth:1, borderStyle:"solid",\n'
            '        borderColor: hovered ? svc.color+"50" : svc.color+"25",\n'
            '        display:"flex", alignItems:"center", justifyContent:"center",\n'
            '        margin:"0 auto 14px", transition:"background .18s, border-color .18s",\n'
            '      }}>',
            'borderWidth:1, borderStyle:"solid",',
        ),
    ], "alb alias + border-shorthand fix")

    # 2. frontend/src/pages/ServiceDetail.jsx
    p = root / "frontend/src/pages/ServiceDetail.jsx"
    patch(p, [
        (
            'const KNOWN_SERVICE_KEYS = { ec2: "EC2", ebs: "EBS", rds: "RDS", s3: "S3", ecs: "ECS", elb: "ELB", lambda: "Lambda" };',
            'const KNOWN_SERVICE_KEYS = { ec2: "EC2", ebs: "EBS", rds: "RDS", s3: "S3", ecs: "ECS", elb: "ELB", alb: "ELB", lambda: "Lambda" };',
            'alb: "ELB"',
        ),
    ], "alb alias")

    # 3. frontend/src/pages/Alerts.jsx
    p = root / "frontend/src/pages/Alerts.jsx"
    patch(p, [
        (
            'const ROUTE_SEGMENT_BY_SERVICE = {\n'
            '  ec2: "ec2", ebs: "ebs", rds: "rds", lambda: "lambda",\n'
            '  s3: "s3", elb: "elb", ecs: "ecs",\n'
            '};',
            'const ROUTE_SEGMENT_BY_SERVICE = {\n'
            '  ec2: "ec2", ebs: "ebs", rds: "rds", lambda: "lambda",\n'
            '  s3: "s3", elb: "elb", alb: "alb", ecs: "ecs",\n'
            '};',
            'alb: "alb"',
        ),
    ], "alb alias")

    # 4. app/aws/collector_direct.py — new collectors
    p = root / "app/aws/collector_direct.py"
    if p.exists():
        text = p.read_text(encoding="utf-8")
        if "def collect_cognito_user_pools" in text:
            print(f"  SKIP  {p} (already patched or nothing to do)")
        else:
            anchor = (
                'def collect_vpn_connections(region=None) -> list:\n'
                '    return _cached(f"vpn_{region}", lambda: _vpn_raw(region))\n\n'
                'def _vpn_raw(region) -> list:\n'
                '    try:\n'
                '        ec2 = get_session(region).client("ec2")\n'
                '        conns = ec2.describe_vpn_connections().get("VpnConnections", [])\n'
                '        return [c for c in conns if c.get("State") not in ("deleted", "deleting")]\n'
                '    except Exception as e:\n'
                '        logger.error(f"Site-to-Site VPN [{region}]: {e}"); return []'
            )
            addition = (
                '\n\n\n'
                'def collect_cognito_user_pools(region=None) -> list:\n'
                '    return _cached(f"cognito_{region}", lambda: _cognito_raw(region))\n\n'
                'def _cognito_raw(region) -> list:\n'
                '    try:\n'
                '        idp = get_session(region).client("cognito-idp")\n'
                '        out = []\n'
                '        for page in idp.get_paginator("list_user_pools").paginate(PaginationConfig={"PageSize": 60}):\n'
                '            out.extend(page.get("UserPools", []))\n'
                '        return out\n'
                '    except Exception as e:\n'
                '        logger.error(f"Cognito [{region}]: {e}"); return []\n\n\n'
                'def collect_global_accelerator_accelerators(region=None) -> list:\n'
                '    # Global service — Global Accelerator\'s control-plane API is only\n'
                '    # available in us-west-2 regardless of the account\'s default region.\n'
                '    return _cached("globalaccelerator", _global_accelerator_raw)\n\n'
                'def _global_accelerator_raw() -> list:\n'
                '    try:\n'
                '        ga = boto3.client("globalaccelerator", region_name="us-west-2")\n'
                '        out = []\n'
                '        for page in ga.get_paginator("list_accelerators").paginate():\n'
                '            out.extend(page.get("Accelerators", []))\n'
                '        return out\n'
                '    except Exception as e:\n'
                '        logger.error(f"Global Accelerator: {e}"); return []'
            )
            if anchor in text:
                text = text.replace(anchor, anchor + addition, 1)
                p.write_text(text, encoding="utf-8")
                print(f"  OK    {p} (added collect_cognito_user_pools, collect_global_accelerator_accelerators)")
            else:
                print(f"  WARN  {p}: anchor not found, skipping")
    else:
        print(f"  SKIP  {p} (not found)")

    # 5. app/api/live_data.py — import + registration
    p = root / "app/api/live_data.py"
    patch(p, [
        (
            '    collect_vpn_connections,\n    get_account_summary,',
            '    collect_vpn_connections,\n    collect_cognito_user_pools,\n    collect_global_accelerator_accelerators,\n    get_account_summary,',
            'collect_cognito_user_pools,\n    collect_global_accelerator_accelerators,',
        ),
        (
            '    "vpn":            collect_vpn_connections,\n}',
            '    "vpn":            collect_vpn_connections,\n    "cognito":        collect_cognito_user_pools,\n    "globalaccelerator": collect_global_accelerator_accelerators,\n}',
            '"cognito":        collect_cognito_user_pools,',
        ),
    ], "register new collectors")

    print("\nDone.")


if __name__ == "__main__":
    main()
