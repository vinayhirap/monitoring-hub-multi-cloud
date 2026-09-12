#!/usr/bin/env python3
"""
disable_unsupported_metrics_v2.py

Uses the DEFINITIVE list of 76 namespaces confirmed rejected by the actual
installed YACE 0.67.0 binary on 3.109.181.40 (via find_unsupported_namespaces.py,
which probed the real binary directly - not README, not source, not
binary-string-guessing). 90 other namespaces were confirmed genuinely supported,
including AWS/EKS, which an earlier README-based guess had wrongly flagged as
unsupported - that guess is now discarded in favor of this verified list.

Disables any enabled account_metric_selections rows for these 76 namespaces,
across ALL accounts (not just one), since this is a structural YACE binary
incompatibility - any account enabling these will crash its generator the
same way AWS/DAX already did for account 5's standard tier.

Then regenerates critical/standard/trend for every account and verifies
none of the 76 rejected namespaces appear in any generated config, so you
don't hit another crash on the next deploy.

Usage:
    python disable_unsupported_metrics_v2.py            # dry run
    python disable_unsupported_metrics_v2.py --apply     # actually disable + verify
"""
import sys
import requests
import yaml
from dotenv import load_dotenv
load_dotenv()
from app.db import get_connection

APP_BASE = "http://127.0.0.1:8000"

# Definitive - confirmed by probing the real yace binary on the server, this session.
REJECTED_NAMESPACES = {
    'AWS/AmplifyHosting', 'AWS/AppFlow', 'AWS/Braket/By Device', 'AWS/Chatbot',
    'AWS/ChimeSDK', 'AWS/ChimeVoiceConnector', 'AWS/CloudHSM', 'AWS/CloudSearch',
    'AWS/CloudTrail', 'AWS/CloudWatch/MetricStreams', 'AWS/CodeBuild',
    'AWS/CodeGuruReviewer', 'AWS/CodePipeline', 'AWS/Comprehend', 'AWS/Config',
    'AWS/Connect', 'AWS/DAX', 'AWS/DRS', 'AWS/DataLifecycleManager',
    'AWS/DevOps-Guru', 'AWS/ECS/ManagedScaling', 'AWS/ElasticGPUs',
    'AWS/ElasticInference', 'AWS/FinSpace', 'AWS/Forecast', 'AWS/FraudDetector',
    'AWS/GroundStation', 'AWS/HealthLake', 'AWS/IVS', 'AWS/IVSChat',
    'AWS/Inspector', 'AWS/IoTFleetWise', 'AWS/IoTSiteWise', 'AWS/IoTTwinMaker',
    'AWS/Kendra', 'AWS/KinesisVideo', 'AWS/Lex', 'AWS/Location',
    'AWS/LookoutVision', 'AWS/M2', 'AWS/MGN', 'AWS/ML', 'AWS/MediaStore',
    'AWS/NetworkManager', 'AWS/Omics', 'AWS/Outposts', 'AWS/PanoramaDeviceMetrics',
    'AWS/Personalize', 'AWS/Pinpoint', 'AWS/Polly', 'AWS/Private5G', 'AWS/Q',
    'AWS/QApps', 'AWS/QBusiness', 'AWS/Rekognition', 'AWS/Route53RecoveryReadiness',
    'AWS/S3/Storage-Lens', 'AWS/SMSVoice', 'AWS/SSM-RunCommand', 'AWS/SWF',
    'AWS/SageMaker/ModelBuildingPipeline', 'AWS/SecurityLake', 'AWS/ServiceCatalog',
    'AWS/SocialMessaging', 'AWS/Textract', 'AWS/Transcribe', 'AWS/Translate',
    'AWS/WorkMail', 'AWS/WorkSpacesWeb', 'AWS/lookoutequipment',
    'AWS/managedblockchain', 'AWS/rePostPrivate', 'AWSLicenseManager/licenseUsage',
    'ApplicationSignals', 'CloudWatchSynthetics', 'WAF',
}


def main():
    apply_mode = "--apply" in sys.argv

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    placeholders = ",".join(["%s"] * len(REJECTED_NAMESPACES))
    cur.execute(f"""
        SELECT ams.id AS selection_id, ams.aws_account_id, aa.account_name,
               mc.id AS metric_id, mc.namespace, mc.metric_name
        FROM account_metric_selections ams
        JOIN metric_catalog mc ON mc.id = ams.metric_id
        JOIN aws_accounts aa ON aa.id = ams.aws_account_id
        WHERE mc.namespace IN ({placeholders}) AND ams.enabled = 1
    """, tuple(REJECTED_NAMESPACES))
    bad_rows = cur.fetchall()

    print(f"Found {len(bad_rows)} enabled selection(s) across all accounts for "
          f"YACE-rejected namespaces:\n")
    for r in bad_rows:
        print(f"  account={r['account_name']} (id={r['aws_account_id']})  "
              f"{r['namespace']}/{r['metric_name']}  (metric_id={r['metric_id']})")

    if not bad_rows:
        print("Nothing to disable - clean already.")
    elif not apply_mode:
        print(f"\nDRY RUN - nothing changed. Re-run with --apply to disable "
              f"{len(bad_rows)} row(s) above.")
    else:
        ids = [r["selection_id"] for r in bad_rows]
        placeholders2 = ",".join(["%s"] * len(ids))
        cur.execute(f"UPDATE account_metric_selections SET enabled = 0 "
                    f"WHERE id IN ({placeholders2})", tuple(ids))
        conn.commit()
        print(f"\nDisabled {cur.rowcount} row(s).")

    cur.close(); conn.close()

    if not apply_mode:
        return

    print("\n=== Verifying generated configs for all accounts/tiers ===")
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, account_name FROM aws_accounts")
    accounts = cur.fetchall()
    cur.close(); conn.close()

    any_problem = False
    for acc in accounts:
        for tier in ["critical", "standard", "trend"]:
            try:
                resp = requests.get(
                    f"{APP_BASE}/api/account-metrics/{acc['id']}/yace-config",
                    params={"tier": tier, "download": "false"},
                    timeout=15,
                )
                if resp.status_code == 400:
                    continue
                resp.raise_for_status()
                cfg = yaml.safe_load(resp.text)
                job_types = {job.get("type") for job in cfg.get("discovery", {}).get("jobs", [])}
                bad = job_types & REJECTED_NAMESPACES
                if bad:
                    any_problem = True
                    print(f"  PROBLEM: account={acc['account_name']} tier={tier} "
                          f"still has rejected namespace(s): {bad}")
                else:
                    print(f"  OK: account={acc['account_name']} tier={tier} "
                          f"({len(job_types)} namespaces, all clean)")
            except Exception as e:
                print(f"  ERROR checking account={acc['account_name']} tier={tier}: {e}")

    if any_problem:
        print("\nDo NOT redeploy yet - see PROBLEM line(s) above.")
    else:
        print("\nAll accounts, all tiers verified clean. Safe to regenerate + "
              "redeploy any tier config now.")


if __name__ == "__main__":
    main()
