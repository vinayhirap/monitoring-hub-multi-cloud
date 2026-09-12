#!/usr/bin/env python3
"""
find_unsupported_namespaces.py

Ground-truth check against the ACTUAL installed YACE binary (not README,
not GitHub source, not binary string-guessing) for every namespace
currently in metric_catalog. YACE only reports ONE bad namespace per
config-parse attempt (the first one it hits), so this loops: build a
probe config with one job per remaining candidate namespace, run yace
briefly, parse out the rejected namespace, remove it, repeat - until a
parse attempt produces no "Service is not in known list" error at all.

Uses a fake/generic metric name per job ("Info") since the "not in known
list" check happens against the job's namespace before any per-metric
validation - confirmed by the actual error format already seen this
session, which names only the namespace, not the metric.

Safe by design: each yace invocation is killed after a couple seconds
(enough for it to parse the config and either error out or start its
first scrape attempt) via `timeout`, so this never lets it run long
enough to actually hit CloudWatch/AWS or interfere with the real
yace-critical/standard/trend services, which are untouched by this script.

Run this ON THE SERVER (needs the real /usr/local/bin/yace binary):

    python3 find_unsupported_namespaces.py

No arguments needed - the full namespace list from metric_catalog (as
pulled earlier this session) is embedded below.
"""
import re
import subprocess
import tempfile
import os

# Exact distinct namespace list pulled from metric_catalog this session
RAW_NAMESPACES = """ApplicationSignals AWS/ACMPrivateCA AWS/AmazonMQ AWS/AmplifyHosting AWS/ApiGateway AWS/AppFlow AWS/ApplicationELB AWS/AppRunner AWS/AppStream AWS/AppSync AWS/Athena AWS/AutoScaling AWS/Backup AWS/Bedrock/Guardrails AWS/Billing AWS/Braket/By Device AWS/Cassandra AWS/CertificateManager AWS/Chatbot AWS/ChimeSDK AWS/ChimeVoiceConnector AWS/ClientVPN AWS/CloudFront AWS/CloudHSM AWS/CloudSearch AWS/CloudTrail AWS/CloudWatch/MetricStreams AWS/CodeBuild AWS/CodeGuruReviewer AWS/CodePipeline AWS/Cognito AWS/Comprehend AWS/Config AWS/Connect AWS/DataLifecycleManager AWS/DataSync AWS/DAX AWS/DDoSProtection AWS/DevOps-Guru AWS/DirectoryService AWS/DMS AWS/DocDB AWS/DRS AWS/DX AWS/DynamoDB AWS/EBS AWS/EC2 AWS/EC2Spot AWS/ECR AWS/ECS AWS/ECS/ManagedScaling AWS/EFS AWS/EKS AWS/ElastiCache AWS/ElasticBeanstalk AWS/ElasticGPUs AWS/ElasticInference AWS/ElasticMapReduce AWS/ELB AWS/EMRServerless AWS/ES AWS/Events AWS/FinSpace AWS/Firehose AWS/Forecast AWS/FraudDetector AWS/FSx AWS/GameLift AWS/GatewayELB AWS/GlobalAccelerator AWS/GroundStation AWS/HealthLake AWS/Inspector AWS/IoT AWS/IoTFleetWise AWS/IoTSiteWise AWS/IoTTwinMaker AWS/IPAM AWS/IVS AWS/IVSChat AWS/Kafka AWS/KafkaConnect AWS/Kendra AWS/Kinesis AWS/KinesisAnalytics AWS/KinesisVideo AWS/KMS AWS/Lambda AWS/Lex AWS/Location AWS/Logs AWS/lookoutequipment AWS/LookoutVision AWS/M2 AWS/managedblockchain AWS/MediaConnect AWS/MediaConvert AWS/MediaLive AWS/MediaPackage AWS/MediaStore AWS/MediaTailor AWS/MemoryDB AWS/MGN AWS/ML AWS/MWAA AWS/NATGateway AWS/Neptune AWS/NetworkELB AWS/NetworkFirewall AWS/NetworkManager AWS/Omics AWS/Outposts AWS/PanoramaDeviceMetrics AWS/Personalize AWS/Pinpoint AWS/Polly AWS/Private5G AWS/PrivateLinkEndpoints AWS/PrivateLinkServices AWS/Prometheus AWS/Q AWS/QApps AWS/QBusiness AWS/QuickSight AWS/RDS AWS/Redshift AWS/Rekognition AWS/rePostPrivate AWS/Route53 AWS/Route53RecoveryReadiness AWS/RUM AWS/S3 AWS/S3/Storage-Lens AWS/SageMaker AWS/SageMaker/ModelBuildingPipeline AWS/SecretsManager AWS/SecurityLake AWS/ServiceCatalog AWS/SES AWS/SMSVoice AWS/SNS AWS/SocialMessaging AWS/SQS AWS/SSM-RunCommand AWS/States AWS/StorageGateway AWS/SWF AWS/Textract AWS/Timestream AWS/Transcribe AWS/Transfer AWS/TransitGateway AWS/Translate AWS/TrustedAdvisor AWS/VPN AWS/WAFV2 AWS/WorkMail AWS/WorkSpaces AWS/WorkSpacesWeb AWSLicenseManager/licenseUsage CloudWatchSynthetics ContainerInsights CWAgent ECS/ContainerInsights Glue WAF"""

# "AWS/Braket/By Device" has a literal space in it from the source dump -
# treat the whole run of non-whitespace-that-looks-like-a-namespace as one
# token isn't reliable with a plain .split(). Namespaces are actually
# space-free except this one edge case, so handle it explicitly.
NAMESPACES = RAW_NAMESPACES.replace("AWS/Braket/By Device", "AWS/Braket/By_Device_PLACEHOLDER").split()
NAMESPACES = [n.replace("AWS/Braket/By_Device_PLACEHOLDER", "AWS/Braket/By Device") for n in NAMESPACES]
NAMESPACES = sorted(set(NAMESPACES))

YACE_BIN = "/usr/local/bin/yace"
PATTERN = re.compile(r'Service is not in known list!: ([^"}]+)')


def build_config(namespaces):
    lines = ["apiVersion: v1alpha1", "discovery:", "  jobs:"]
    for ns in namespaces:
        lines += [
            f"  - type: {ns}",
            "    regions:",
            "    - ap-south-1",
            "    period: 300",
            "    length: 300",
            "    metrics:",
            "    - name: Info",
            "      statistics:",
            "      - Average",
        ]
    return "\n".join(lines) + "\n"


def probe(namespaces):
    """Returns the first rejected namespace, or None if the config parses clean."""
    cfg = build_config(namespaces)
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
        f.write(cfg)
        path = f.name
    try:
        result = subprocess.run(
            ["timeout", "3", YACE_BIN, f"--config.file={path}"],
            capture_output=True, text=True,
        )
        combined = result.stdout + result.stderr
        m = PATTERN.search(combined)
        return m.group(1).strip() if m else None
    finally:
        os.unlink(path)


def main():
    already_known_bad = {"AWS/AmplifyHosting", "AWS/AppFlow"}
    remaining = [n for n in NAMESPACES if n not in already_known_bad]
    rejected = list(already_known_bad)

    print(f"Starting with {len(remaining)} distinct namespaces from metric_catalog.\n")

    for i in range(len(NAMESPACES) + 5):  # safety cap
        bad = probe(remaining)
        if bad is None:
            print(f"Round {i}: config parses clean with {len(remaining)} namespaces remaining. Done.")
            break
        if bad not in remaining:
            print(f"Round {i}: got '{bad}' but it's not in our remaining list - "
                  f"unexpected, stopping to avoid an infinite loop.")
            break
        print(f"Round {i}: REJECTED -> {bad}")
        rejected.append(bad)
        remaining.remove(bad)
    else:
        print("Hit safety cap without a clean parse - something else may be wrong "
              "with the probe config structure itself, not just namespace names.")

    print(f"\n=== RESULTS ===")
    print(f"Total namespaces checked: {len(NAMESPACES)}")
    print(f"Rejected by YACE ({len(rejected)}): {rejected}")
    print(f"Confirmed supported ({len(remaining)})")

    with open("/tmp/yace_rejected_namespaces.txt", "w") as f:
        f.write("\n".join(rejected))
    print(f"\nRejected list saved to /tmp/yace_rejected_namespaces.txt "
          f"- scp this back to your Windows machine.")


if __name__ == "__main__":
    main()
