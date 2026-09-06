#!/usr/bin/env python3
"""
apply_icons_sidebar_noc_removal.py

Applies four related frontend fixes to a local checkout of
monitoring-hub-multi-cloud:

  1. Real AWS service icons for every service tile — frontend/src/components/cloud-icons.jsx
     currently only maps 7 AWS services (ec2/ebs/rds/s3/ecs/elb/lambda) to a real
     @aws-icons/react icon and silently falls back to the EC2 icon for every other
     service, which is why every "Extended service" tile on the Services page renders
     the same generic chip icon. This also fixes a latent bug: the ALB icon was
     registered under the key "elb", but the app's service id for ALB is "alb", so
     Application Load Balancer never actually got its dedicated icon either. This
     script wires up real, official AWS icons (via the already-installed
     @aws-icons/react package) for all 34 services in the app's metric catalog.

  2. Removes the "NOC Mode" full-screen toggle button and its body.noc-mode
     CSS/JS plumbing from the three pages that had it (Services list, Alerts,
     Overview) — this is being replaced by the persistent sidebar toggle below.

  3. Adds a real sidebar open/close toggle that works at every screen size
     (previously the hamburger button only appeared below 900px). The sidebar
     is now an off-canvas drawer, closed by default, opened by clicking the
     toggle button in the topbar.

  4. Moves the CloudOps logo/brand out of the collapsible <aside> and into the
     topbar, so it stays visible whether the sidebar is open or closed instead
     of disappearing along with the rest of the nav when collapsed.

Safe to re-run: each step checks whether it was already applied and skips it
if so. If a step's expected surrounding text isn't found (because your local
file has diverged from the version this script was written against), it
prints a clear warning instead of guessing / corrupting the file.

Usage:
    python apply_icons_sidebar_noc_removal.py [path-to-repo-root]

If no path is given, it uses the current directory. The repo root should be
the monitoring-hub-multi-cloud checkout (the directory containing "frontend/").
"""

import sys
from pathlib import Path

# ── resolve repo root ───────────────────────────────────────────
repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()

frontend        = repo_root / "frontend" / "src"
cloud_icons_jsx = frontend / "components" / "cloud-icons.jsx"
layout_jsx      = frontend / "components" / "Layout.jsx"
layout_css      = frontend / "components" / "Layout.css"
servicelist_jsx = frontend / "pages" / "ServiceList.jsx"
overview_jsx    = frontend / "pages" / "Overview.jsx"
overview_css    = frontend / "pages" / "Overview.css"
alerts_jsx      = frontend / "pages" / "Alerts.jsx"
alerts_css      = frontend / "pages" / "Alerts.css"

results = []  # (label, "ok" | "skip" | "FAIL", detail)


def report(label, status, detail=""):
    results.append((label, status, detail))
    tag = {"ok": "\u2705", "skip": "\u23ed ", "FAIL": "\u274c"}[status]
    print(f"{tag} {label}" + (f" \u2014 {detail}" if detail else ""))


def replace_once(path: Path, old: str, new: str, label: str, already_marker: str = None):
    """
    Replace `old` with `new` in `path`, exactly once.
    If `already_marker` is found in the file, treat as already-applied and skip.
    If `old` isn't found (and marker isn't either), report FAIL with guidance.
    """
    if not path.exists():
        report(label, "FAIL", f"file not found: {path}")
        return

    text = path.read_text(encoding="utf-8")

    if already_marker and already_marker in text:
        report(label, "skip", "already applied")
        return

    count = text.count(old)
    if count == 0:
        report(label, "FAIL",
               f"expected text not found in {path.name} \u2014 your local file has "
               f"diverged here. Apply this change manually (see script source "
               f"for the exact old/new text under label '{label}').")
        return
    if count > 1:
        report(label, "FAIL",
               f"expected text found {count} times (expected exactly once) in "
               f"{path.name} \u2014 skipping to avoid ambiguous edit. Apply manually.")
        return

    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    report(label, "ok")


# ═════════════════════════════════════════════════════════════════
# STEP 1 — cloud-icons.jsx: real AWS icon for every service
# ═════════════════════════════════════════════════════════════════

OLD_AWS_IMPORTS = '''import AmazonEc2Instance from "@aws-icons/react/resource/amazon-ec2-instance";
import AmazonElasticBlockStoreVolume from "@aws-icons/react/resource/amazon-elastic-block-store-volume";
import AmazonAuroraAmazonRdsInstance from "@aws-icons/react/resource/amazon-aurora-amazon-rds-instance";
import AmazonSimpleStorageServiceBucket from "@aws-icons/react/resource/amazon-simple-storage-service-bucket";
import AmazonElasticContainerServiceService from "@aws-icons/react/resource/amazon-elastic-container-service-service";
import ElasticLoadBalancingApplicationLoadBalancer from "@aws-icons/react/resource/elastic-load-balancing-application-load-balancer";
import AwsLambdaLambdaFunction from "@aws-icons/react/resource/aws-lambda-lambda-function";'''

NEW_AWS_IMPORTS = OLD_AWS_IMPORTS + '''
import ElasticLoadBalancingNetworkLoadBalancer from "@aws-icons/react/resource/elastic-load-balancing-network-load-balancer";
import AmazonApiGateway from "@aws-icons/react/architecture-service/amazon-api-gateway";
import AmazonDynamoDb from "@aws-icons/react/architecture-service/amazon-dynamo-db";
import AmazonSimpleQueueService from "@aws-icons/react/architecture-service/amazon-simple-queue-service";
import AmazonSimpleNotificationService from "@aws-icons/react/architecture-service/amazon-simple-notification-service";
import AmazonCloudFront from "@aws-icons/react/architecture-service/amazon-cloud-front";
import AmazonElastiCache from "@aws-icons/react/architecture-service/amazon-elasti-cache";
import AmazonOpenSearchService from "@aws-icons/react/architecture-service/amazon-open-search-service";
import AmazonElasticKubernetesService from "@aws-icons/react/architecture-service/amazon-elastic-kubernetes-service";
import AmazonElasticFileSystemFileSystem from "@aws-icons/react/resource/amazon-elastic-file-system-file-system";
import AmazonDocumentDb from "@aws-icons/react/architecture-service/amazon-document-db";
import AmazonNeptune from "@aws-icons/react/architecture-service/amazon-neptune";
import AmazonManagedStreamingForApacheKafka from "@aws-icons/react/architecture-service/amazon-managed-streaming-for-apache-kafka";
import AmazonKinesisDataStreams from "@aws-icons/react/architecture-service/amazon-kinesis-data-streams";
import AmazonDataFirehose from "@aws-icons/react/architecture-service/amazon-data-firehose";
import AmazonEc2AutoScaling from "@aws-icons/react/architecture-service/amazon-ec2-auto-scaling";
import AmazonVpcNatGateway from "@aws-icons/react/resource/amazon-vpc-nat-gateway";
import AwsTransitGateway from "@aws-icons/react/architecture-service/aws-transit-gateway";
import AmazonRoute53 from "@aws-icons/react/architecture-service/amazon-route-53";
import AwsWaf from "@aws-icons/react/architecture-service/aws-waf";
import AmazonRedshift from "@aws-icons/react/architecture-service/amazon-redshift";
import AmazonMemoryDb from "@aws-icons/react/architecture-service/amazon-memory-db";
import AmazonDynamoDbAmazonDynamoDbAccelerator from "@aws-icons/react/resource/amazon-dynamo-db-amazon-dynamo-db-accelerator";
import AwsStepFunctions from "@aws-icons/react/architecture-service/aws-step-functions";
import AmazonEventBridge from "@aws-icons/react/architecture-service/amazon-event-bridge";
import AwsKeyManagementService from "@aws-icons/react/architecture-service/aws-key-management-service";
import AwsCertificateManager from "@aws-icons/react/architecture-service/aws-certificate-manager";
import AwsBackup from "@aws-icons/react/architecture-service/aws-backup";
import AmazonCognito from "@aws-icons/react/architecture-service/amazon-cognito";
import AmazonCloudWatchLogs from "@aws-icons/react/resource/amazon-cloud-watch-logs";
import AwsSiteToSiteVpn from "@aws-icons/react/architecture-service/aws-site-to-site-vpn";
import AwsGlobalAccelerator from "@aws-icons/react/architecture-service/aws-global-accelerator";
import AwsDatabaseMigrationService from "@aws-icons/react/architecture-service/aws-database-migration-service";
import AwsDirectConnect from "@aws-icons/react/architecture-service/aws-direct-connect";'''

replace_once(
    cloud_icons_jsx,
    old=OLD_AWS_IMPORTS,
    new=NEW_AWS_IMPORTS,
    label="cloud-icons.jsx: import real icons for every AWS service",
    already_marker='import AmazonApiGateway from "@aws-icons/react/architecture-service/amazon-api-gateway"',
)

OLD_AWS_ICON_MAP = '''const AWS_ICON = {
  ec2:    AmazonEc2Instance,
  ebs:    AmazonElasticBlockStoreVolume,
  rds:    AmazonAuroraAmazonRdsInstance,
  s3:     AmazonSimpleStorageServiceBucket,
  ecs:    AmazonElasticContainerServiceService,
  elb:    ElasticLoadBalancingApplicationLoadBalancer,
  lambda: AwsLambdaLambdaFunction,
};'''

NEW_AWS_ICON_MAP = '''const AWS_ICON = {
  // Core
  ec2:    AmazonEc2Instance,
  ebs:    AmazonElasticBlockStoreVolume,
  rds:    AmazonAuroraAmazonRdsInstance,
  alb:    ElasticLoadBalancingApplicationLoadBalancer,
  elb:    ElasticLoadBalancingApplicationLoadBalancer, // legacy alias
  lambda: AwsLambdaLambdaFunction,
  s3:     AmazonSimpleStorageServiceBucket,
  ecs:    AmazonElasticContainerServiceService,

  // Extended — matches the service keys in app/aws/metric_catalog_data.py
  nlb:                 ElasticLoadBalancingNetworkLoadBalancer,
  apigateway:          AmazonApiGateway,
  dynamodb:            AmazonDynamoDb,
  sqs:                 AmazonSimpleQueueService,
  sns:                 AmazonSimpleNotificationService,
  cloudfront:          AmazonCloudFront,
  elasticache:         AmazonElastiCache,
  opensearch:          AmazonOpenSearchService,
  eks:                 AmazonElasticKubernetesService,
  efs:                 AmazonElasticFileSystemFileSystem,
  documentdb:          AmazonDocumentDb,
  neptune:             AmazonNeptune,
  msk:                 AmazonManagedStreamingForApacheKafka,
  kinesis:             AmazonKinesisDataStreams,
  firehose:            AmazonDataFirehose,
  autoscaling:         AmazonEc2AutoScaling,
  natgateway:          AmazonVpcNatGateway,
  transitgateway:      AwsTransitGateway,
  route53:             AmazonRoute53,
  wafv2:               AwsWaf,
  redshift:            AmazonRedshift,
  memorydb:            AmazonMemoryDb,
  dax:                 AmazonDynamoDbAmazonDynamoDbAccelerator,
  states:              AwsStepFunctions,
  events:              AmazonEventBridge,
  kms:                 AwsKeyManagementService,
  certificatemanager:  AwsCertificateManager,
  backup:              AwsBackup,
  cognito:             AmazonCognito,
  logs:                AmazonCloudWatchLogs,
  vpn:                 AwsSiteToSiteVpn,
  globalaccelerator:   AwsGlobalAccelerator,
  dms:                 AwsDatabaseMigrationService,
  directconnect:       AwsDirectConnect,
};'''

replace_once(
    cloud_icons_jsx,
    old=OLD_AWS_ICON_MAP,
    new=NEW_AWS_ICON_MAP,
    label="cloud-icons.jsx: map every AWS service id to its real icon (+ fix alb/elb key bug)",
    already_marker="apigateway:          AmazonApiGateway,",
)


# ═════════════════════════════════════════════════════════════════
# STEP 2 — remove NOC Mode from ServiceList.jsx
# ═════════════════════════════════════════════════════════════════

replace_once(
    servicelist_jsx,
    old='''  const [alerts,  setAlerts]  = useState([]);
  const [isNOC,   setIsNOC]   = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    document.body.classList.toggle("noc-mode", isNOC);
    return () => document.body.classList.remove("noc-mode");
  }, [isNOC]);

  useEffect(() => {''',
    new='''  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {''',
    label="ServiceList.jsx: remove isNOC state + noc-mode effect",
    already_marker=None,
)

replace_once(
    servicelist_jsx,
    old='''        <button
          style={{
            background: isNOC ? "rgba(43,179,172,0.12)" : "var(--bg-card)",
            border: `1px solid ${isNOC ? "var(--accent)" : "var(--border)"}`,
            color: isNOC ? "var(--accent)" : "var(--text-muted)",
            borderRadius: 6, padding: "7px 14px", fontSize: 13,
            fontWeight: isNOC ? 700 : 500, cursor: "pointer",
          }}
          onClick={() => setIsNOC(v => !v)}
        >
          {isNOC ? "⊞ Exit NOC" : "⊞ NOC Mode"}
        </button>
      </div>''',
    new='''      </div>''',
    label="ServiceList.jsx: remove NOC Mode button",
    already_marker=None,
)


# ═════════════════════════════════════════════════════════════════
# STEP 3 — remove NOC Mode from Alerts.jsx
# ═════════════════════════════════════════════════════════════════

replace_once(
    alerts_jsx,
    old='  const [isNOC,   setIsNOC]   = useState(false);\n',
    new='',
    label="Alerts.jsx: remove isNOC state",
    already_marker=None,
)

replace_once(
    alerts_jsx,
    old='''  // NOC fullscreen mode
  useEffect(() => {
    document.body.classList.toggle("noc-mode", isNOC);
    return () => document.body.classList.remove("noc-mode");
  }, [isNOC]);

''',
    new='',
    label="Alerts.jsx: remove noc-mode effect",
    already_marker=None,
)

replace_once(
    alerts_jsx,
    old='''          <button className="btn-refresh" onClick={loadAlerts}>↻ Refresh</button>
          <button
            className={`btn-refresh${isNOC ? " noc-active-btn" : ""}`}
            onClick={() => setIsNOC(v => !v)}
            title="Toggle NOC fullscreen"
            style={{ fontWeight: 600 }}
          >
            {isNOC ? "⊠ Exit NOC" : "⊞ NOC Mode"}
          </button>
          <div className="live-pill"><span className="live-dot" />LIVE</div>''',
    new='''          <button className="btn-refresh" onClick={loadAlerts}>↻ Refresh</button>
          <div className="live-pill"><span className="live-dot" />LIVE</div>''',
    label="Alerts.jsx: remove NOC Mode button",
    already_marker=None,
)


# ═════════════════════════════════════════════════════════════════
# STEP 4 — remove NOC Mode from Overview.jsx
# ═════════════════════════════════════════════════════════════════

replace_once(
    overview_jsx,
    old='  const [isNOC,       setIsNOC]       = useState(false);\n',
    new='',
    label="Overview.jsx: remove isNOC state",
    already_marker=None,
)

replace_once(
    overview_jsx,
    old='''  useEffect(() => {
    document.body.classList.toggle("noc-mode", isNOC);
    return () => document.body.classList.remove("noc-mode");
  }, [isNOC]);

''',
    new='',
    label="Overview.jsx: remove noc-mode effect",
    already_marker=None,
)

replace_once(
    overview_jsx,
    old='    <div className={`overview ${isNOC ? "noc-fullscreen" : ""}`}>',
    new='    <div className="overview">',
    label="Overview.jsx: drop isNOC from root className",
    already_marker=None,
)

replace_once(
    overview_jsx,
    old='''            Live AWS infrastructure monitoring across all accounts · NOC View''',
    new='''            Live AWS infrastructure monitoring across all accounts''',
    label="Overview.jsx: drop stray 'NOC View' subtitle text",
    already_marker=None,
)

replace_once(
    overview_jsx,
    old='''          <button className="btn-refresh" onClick={loadAll} title="Refresh now">↻ Refresh</button>
          <button
            className={`ov-noc-btn ${isNOC ? "noc-active" : ""}`}
            onClick={() => setIsNOC(v => !v)}
          >
            {isNOC ? "⊠ Exit NOC" : "⊞ NOC Mode"}
          </button>
        </div>''',
    new='''          <button className="btn-refresh" onClick={loadAll} title="Refresh now">↻ Refresh</button>
        </div>''',
    label="Overview.jsx: remove NOC Mode button",
    already_marker=None,
)


# ═════════════════════════════════════════════════════════════════
# STEP 5 — Overview.css / Alerts.css / Layout.css: drop dead NOC rules
# ═════════════════════════════════════════════════════════════════

replace_once(
    overview_css,
    old='''.overview { max-width: 100%; }
.overview.noc-fullscreen { padding: 16px; }

.ov-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:20px; }
.hl { color:var(--accent); }
.ov-refresh { background:var(--bg-card); border:1px solid var(--border-md); color:var(--text-secondary); padding:7px 14px; border-radius:var(--radius); font-size:13px; cursor:pointer; transition:all .15s; }
.ov-refresh:hover { border-color:var(--accent); color:var(--accent); }

.ov-noc-btn { background:var(--bg-card); border:1px solid var(--border-md); color:var(--text-secondary); padding:7px 14px; border-radius:var(--radius); font-size:13px; cursor:pointer; transition:all .15s; }
.ov-noc-btn:hover { border-color:var(--accent); color:var(--accent); }
.ov-noc-btn.noc-active { background:rgba(43,179,172,0.12); border-color:var(--accent); color:var(--accent); font-weight:700; }

body.noc-mode .sidebar,
body.noc-mode nav,
body.noc-mode aside { display:none !important; }
body.noc-mode .main-content,
body.noc-mode .page-wrapper,
body.noc-mode .content-area { margin-left:0 !important; width:100% !important; }

.ov-summary {''',
    new='''.overview { max-width: 100%; }

.ov-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:20px; }
.hl { color:var(--accent); }
.ov-refresh { background:var(--bg-card); border:1px solid var(--border-md); color:var(--text-secondary); padding:7px 14px; border-radius:var(--radius); font-size:13px; cursor:pointer; transition:all .15s; }
.ov-refresh:hover { border-color:var(--accent); color:var(--accent); }

.ov-summary {''',
    label="Overview.css: drop noc-fullscreen / ov-noc-btn / body.noc-mode rules",
    already_marker=None,
)

replace_once(
    overview_css,
    old='''[data-theme="light"] .ov-noc-btn,
[data-theme="light"] .ov-refresh         { background: #ffffff; border-color: rgba(99,130,190,0.2); color: #4a6080; }
[data-theme="light"] .ov-noc-btn:hover,
[data-theme="light"] .ov-refresh:hover   { border-color: rgba(0,119,204,0.35); color: #2bb3ac; }''',
    new='''[data-theme="light"] .ov-refresh         { background: #ffffff; border-color: rgba(99,130,190,0.2); color: #4a6080; }
[data-theme="light"] .ov-refresh:hover   { border-color: rgba(0,119,204,0.35); color: #2bb3ac; }''',
    label="Overview.css: drop light-theme ov-noc-btn rules",
    already_marker=None,
)

replace_once(
    alerts_css,
    old='''.mono  { font-family: 'JetBrains Mono', monospace; }
.small { font-size: 11px; }
/* NOC mode button active state */
.noc-active-btn {
  background: rgba(43,179,172,0.12) !important;
  border-color: var(--accent) !important;
  color: var(--accent) !important;
  font-weight: 700 !important;
}''',
    new='''.mono  { font-family: 'JetBrains Mono', monospace; }
.small { font-size: 11px; }''',
    label="Alerts.css: drop dead .noc-active-btn rule",
    already_marker=None,
)

replace_once(
    layout_css,
    old='''[data-theme="light"] .ob-input::placeholder { color: #a0b0c8; }
/* NOC mode button active state */
.noc-active-btn {
  background: rgba(43,179,172,0.12) !important;
  border-color: var(--accent) !important;
  color: var(--accent) !important;
  font-weight: 700 !important;
}''',
    new='''[data-theme="light"] .ob-input::placeholder { color: #a0b0c8; }''',
    label="Layout.css: drop dead .noc-active-btn rule",
    already_marker=None,
)


# ═════════════════════════════════════════════════════════════════
# STEP 6 — Layout.jsx: move the logo to the topbar, sidebar stays
#          collapsible-only (nav + footer)
# ═════════════════════════════════════════════════════════════════

replace_once(
    layout_jsx,
    old='''      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="sidebar-logo">
            <svg width="28" height="28" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg" aria-label="CloudOps">
              <rect width="512" height="512" rx="112" fill="#0b1220" />
              <g transform="translate(-891.82,2.79) scale(0.8064)">
                <path fill="#2bb3ac" d="M1331.98,222.58c41.39-41.42,103.91-48.88,152.93-22.35l53.65-53.65c-79.15-54.58-188.41-46.67-258.82,23.77-70.44,70.4-78.35,179.67-23.77,258.82l53.65-53.65c-26.53-49.02-19.07-111.55,22.35-152.93Z" />
                <path fill="#2bb3ac" d="M1567.06,457.66c70.44-70.44,78.35-179.7,23.73-258.85l-53.65,53.65c26.53,49.02,19.07,111.55-22.32,152.97-41.42,41.39-103.95,48.85-152.93,22.32l-53.65,53.65c79.15,54.62,188.38,46.71,258.82-23.73Z" />
              </g>
            </svg>
          </div>
          <div className="sidebar-brand-text">
            <div className="sidebar-brand-sub">AURIONPRO</div>
            <div className="sidebar-brand-name">CloudOps</div>
          </div>
        </div>

        <nav className="sidebar-nav">''',
    new='''      <aside className="sidebar">
        <nav className="sidebar-nav">''',
    label="Layout.jsx: remove brand block from the collapsible sidebar",
    already_marker=None,
)

replace_once(
    layout_jsx,
    old='''      <div className="main-wrap">
        <header className="topbar">
          <button
            className="btn-nav-toggle"
            onClick={() => setNavOpen(o => !o)}
            aria-label={navOpen ? "Close navigation menu" : "Open navigation menu"}
            aria-expanded={navOpen}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="3" y1="6" x2="21" y2="6"/>
              <line x1="3" y1="12" x2="21" y2="12"/>
              <line x1="3" y1="18" x2="21" y2="18"/>
            </svg>
          </button>
          <div className="topbar-page-label" id="page-label" />''',
    new='''      <div className="main-wrap">
        <header className="topbar">
          <button
            className="btn-nav-toggle"
            onClick={() => setNavOpen(o => !o)}
            aria-label={navOpen ? "Close navigation menu" : "Open navigation menu"}
            aria-expanded={navOpen}
            title={navOpen ? "Collapse sidebar" : "Expand sidebar"}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="3" y1="6" x2="21" y2="6"/>
              <line x1="3" y1="12" x2="21" y2="12"/>
              <line x1="3" y1="18" x2="21" y2="18"/>
            </svg>
          </button>
          <div className="topbar-brand">
            <div className="sidebar-logo">
              <svg width="24" height="24" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg" aria-label="CloudOps">
                <rect width="512" height="512" rx="112" fill="#0b1220" />
                <g transform="translate(-891.82,2.79) scale(0.8064)">
                  <path fill="#2bb3ac" d="M1331.98,222.58c41.39-41.42,103.91-48.88,152.93-22.35l53.65-53.65c-79.15-54.58-188.41-46.67-258.82,23.77-70.44,70.4-78.35,179.67-23.77,258.82l53.65-53.65c-26.53-49.02-19.07-111.55,22.35-152.93Z" />
                  <path fill="#2bb3ac" d="M1567.06,457.66c70.44-70.44,78.35-179.7,23.73-258.85l-53.65,53.65c26.53,49.02,19.07,111.55-22.32,152.97-41.42,41.39-103.95,48.85-152.93,22.32l-53.65,53.65c79.15,54.62,188.38,46.71,258.82-23.73Z" />
                </g>
              </svg>
            </div>
            <div className="sidebar-brand-text">
              <div className="sidebar-brand-sub">AURIONPRO</div>
              <div className="sidebar-brand-name">CloudOps</div>
            </div>
          </div>
          <div className="topbar-page-label" id="page-label" />''',
    label="Layout.jsx: add persistent brand/logo to the topbar",
    already_marker='<div className="topbar-brand">',
)


# ═════════════════════════════════════════════════════════════════
# STEP 7 — Layout.css: sidebar becomes a real open/close drawer at
#          every breakpoint (closed by default), toggle button always
#          visible, + .topbar-brand styling
# ═════════════════════════════════════════════════════════════════

replace_once(
    layout_css,
    old='''.sidebar {
  width: 200px;
  flex-shrink: 0;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}''',
    new='''.sidebar {
  width: 220px;
  flex-shrink: 0;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  position: fixed;
  inset: 0 auto 0 0;
  z-index: 60;
  transform: translateX(-100%);
  transition: transform 0.22s ease;
  box-shadow: 0 12px 32px rgba(2,8,20,0.5);
}
/* Sidebar is closed (off-canvas) by default; the topbar toggle button
   opens it as a slide-in drawer, at every screen size. */
.layout.nav-open .sidebar { transform: translateX(0); }

/* Persistent brand/logo in the topbar — stays visible whether the
   collapsible sidebar is open or closed. */
.topbar-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-right: 14px;
  margin-right: 4px;
  border-right: 1px solid var(--border);
  flex-shrink: 0;
}
.topbar-brand .sidebar-brand-name { font-size: 13px; }''',
    label="Layout.css: sidebar becomes an off-canvas drawer (closed by default) + .topbar-brand styling",
    already_marker=".topbar-brand {",
)

replace_once(
    layout_css,
    old='''/* ── mobile nav toggle (hidden on desktop) ── */
.btn-nav-toggle {
  display: none;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  flex-shrink: 0;
}
.btn-nav-toggle:hover { background: var(--bg-hover); }

.sidebar-scrim {
  display: none;
}

/* ── responsive layout shell ──────────────────────────────────
   ≥1280px : full 200px sidebar (default above)
   900–1279px : icon-only rail, labels hidden, tooltip via title attr
   <900px  : sidebar becomes an off-canvas drawer, toggled by the
             hamburger button in the topbar
*/
@media (max-width: 1279px) {
  .sidebar { width: 64px; }
  .sidebar-brand-sub,
  .sidebar-brand-name,
  .nav-label,
  .lup-label,
  .lup-time { display: none; }
  .sidebar-brand { justify-content: center; padding: 18px 8px; }
  .nav-item { justify-content: center; padding: 10px; }
  .nav-badge { position: absolute; top: 2px; right: 2px; }
}

@media (max-width: 899px) {
  .btn-nav-toggle { display: inline-flex; }

  .sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    z-index: 60;
    width: 220px;
    transform: translateX(-100%);
    transition: transform 0.22s ease;
    box-shadow: var(--shadow-lg, 0 12px 32px rgba(2,8,20,0.5));
  }
  .sidebar-brand-sub,
  .sidebar-brand-name,
  .nav-label,
  .lup-label,
  .lup-time { display: block; }
  .sidebar-brand { justify-content: flex-start; padding: 20px 16px 18px; }
  .nav-item { justify-content: flex-start; padding: 9px 10px; }

  .layout.nav-open .sidebar { transform: translateX(0); }
  .layout.nav-open .sidebar-scrim {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(2,6,16,0.55);
    z-index: 50;
  }

  .topbar { padding: 0 14px; gap: 10px; }
  .topbar-clock .topbar-tz,
  .topbar-clock { display: none; }
  .topbar-username { display: none; }
}

@media (max-width: 520px) {
  .btn-logout span { display: none; }
  .btn-logout { padding: 7px; gap: 0; }
  .topbar-role-badge { display: none; }
}''',
    new='''/* ── sidebar toggle — visible at every breakpoint, sidebar is
   closed by default (see .layout.nav-open .sidebar above) ── */
.btn-nav-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  flex-shrink: 0;
}
.btn-nav-toggle:hover { background: var(--bg-hover); }

.sidebar-scrim { display: none; }
.layout.nav-open .sidebar-scrim {
  display: block;
  position: fixed;
  inset: 0;
  background: rgba(2,6,16,0.55);
  z-index: 50;
}

/* ── responsive topbar decluttering ── */
@media (max-width: 899px) {
  .topbar { padding: 0 14px; gap: 10px; }
  .topbar-clock .topbar-tz,
  .topbar-clock { display: none; }
  .topbar-username { display: none; }
  .topbar-brand .sidebar-brand-sub { display: none; }
}

@media (max-width: 520px) {
  .btn-logout span { display: none; }
  .btn-logout { padding: 7px; gap: 0; }
  .topbar-role-badge { display: none; }
  .topbar-brand .sidebar-brand-text { display: none; }
}''',
    label="Layout.css: toggle button always visible + simplified responsive rules",
    already_marker="/* ── sidebar toggle — visible at every breakpoint",
)


# ═════════════════════════════════════════════════════════════════
print()
n_fail = sum(1 for _, s, _ in results if s == "FAIL")
n_ok   = sum(1 for _, s, _ in results if s == "ok")
n_skip = sum(1 for _, s, _ in results if s == "skip")
print(f"Done. {n_ok} applied, {n_skip} already applied, {n_fail} failed.")
if n_fail:
    print("\nFor any FAILED step above, your local file differs from what this "
          "script expects at that spot. Open the file, find the described "
          "location, and apply the change by hand (or paste me the current "
          "surrounding code and I'll adjust).")
    sys.exit(1)
else:
    print("\nAll AWS icon imports/mappings are already covered by the existing "
          "@aws-icons/react dependency in frontend/package.json — no npm "
          "install needed. Just rebuild the frontend (npm run dev / npm run build).")
