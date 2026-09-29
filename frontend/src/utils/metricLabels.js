// src/utils/metricLabels.js
// ONE place that turns a stored metric name into what a person should read.
// Stored names are the collector's internal keys (lower-case CloudWatch names,
// CWAgent snake_case names, per-mount suffixes...). They were shown raw in some
// places and abbreviated ("CPU %", "Net In", "Mem %") in others.
//
// The CloudWatch metric is literally named CPUUtilization, so its label is
// "CPU Utilization" -- "CPU %" put the unit in the name.

const LABELS = {
  // EC2
  cpuutilization:        "CPU Utilization",
  cpucreditbalance:      "CPU Credit Balance",
  networkin:             "Network In",
  networkout:            "Network Out",
  diskreadbytes:         "Disk Read Bytes",
  diskwritebytes:        "Disk Write Bytes",
  statuscheckfailed:     "Status Check Failed",
  statuscheckfailed_instance: "Instance Status Check Failed",
  statuscheckfailed_system:   "System Status Check Failed",
  mem_used_percent:      "Memory Used %",
  disk_used_percent:     "Disk Used %",
  memutilization:        "Memory Utilization",
  // EBS
  volumereadops:         "Volume Read Ops",
  volumewriteops:        "Volume Write Ops",
  volumereadbytes:       "Volume Read Bytes",
  volumewritebytes:      "Volume Write Bytes",
  volumequeuelength:     "Volume Queue Length",
  burstbalance:          "Burst Balance",
  // RDS
  dbconnections:         "DB Connections",
  freestorage:           "Free Storage Space",
  freeablememory:        "Freeable Memory",
  readiops:              "Read IOPS",
  writeiops:             "Write IOPS",
  readlatency:           "Read Latency",
  writelatency:          "Write Latency",
  diskqueuedepth:        "Disk Queue Depth",
  replicalag:            "Replica Lag",
  swapusage:             "Swap Usage",
  // ELB
  requestcount:          "Request Count",
  errors5xx:             "Target 5xx Errors",
  errors4xx:             "4xx Errors",
  httpcode_target_5xx_count: "Target 5xx Count",
  httpcode_target_4xx_count: "Target 4xx Count",
  httpcode_elb_5xx_count:    "ELB 5xx Count",
  responselatency:       "Target Response Time",
  healthyhosts:          "Healthy Hosts",
  unhealthyhosts:        "Unhealthy Hosts",
  healthyhosts_describe:   "Healthy Hosts",
  unhealthyhosts_describe: "Unhealthy Hosts",
  activeconnectioncount:   "Active Connections",
  newconnectioncount:      "New Connections",
  rejectedconnectioncount: "Rejected Connections",
  targetconnectionerrorcount: "Target Connection Errors",
  // Lambda
  invocations:           "Invocations",
  errors:                "Errors",
  duration:              "Duration",
  throttles:             "Throttles",
  concurrentexecutions:  "Concurrent Executions",
  iteratorage:           "Iterator Age",
  // WAF
  allowedrequests:       "Allowed Requests",
  blockedrequests:       "Blocked Requests",
  // internal
  multivariate_anomaly:  "Multivariate Anomaly",
  synthetic_uptime:      "Synthetic Uptime",
};

const SMALL = new Set(["of", "in", "on", "per", "and"]);
const ACRONYMS = { cpu: "CPU", ebs: "EBS", rds: "RDS", elb: "ELB", alb: "ALB", nlb: "NLB",
                   waf: "WAF", iops: "IOPS", io: "I/O", db: "DB", http: "HTTP", ssl: "SSL",
                   tls: "TLS", api: "API", sqs: "SQS", sns: "SNS", dns: "DNS", vpn: "VPN" };

function titleCase(str) {
  return str.split(/[\s_]+/).filter(Boolean).map((w, i) => {
    const lw = w.toLowerCase();
    if (ACRONYMS[lw]) return ACRONYMS[lw];
    if (i > 0 && SMALL.has(lw)) return lw;
    return lw.charAt(0).toUpperCase() + lw.slice(1);
  }).join(" ");
}

/** "disk_used_percent__var_lib_mysql" -> "Disk Used % (/var/lib/mysql)" */
export function metricLabel(name) {
  if (!name) return "";
  const raw = String(name);
  const lower = raw.toLowerCase();
  if (LABELS[lower]) return LABELS[lower];

  const m = lower.match(/^(disk_used_percent)__(.+)$/);
  if (m) return `${LABELS[m[1]]} (/${m[2].replace(/_/g, "/")})`;

  // CamelCase CloudWatch names (extended tier stores them as-is, e.g. VolumeWriteOps)
  if (/[a-z][A-Z]/.test(raw) && !raw.includes("_")) {
    const spaced = raw.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
    return titleCase(spaced);
  }
  return titleCase(raw);
}

const PERCENT = /^(cpuutilization|memutilization|mem_used_percent|disk_used_percent(__.+)?|burstbalance)$/;

/** "%" for metrics whose value is a percentage, otherwise "". */
export function metricUnit(name) {
  return PERCENT.test(String(name || "").toLowerCase()) ? "%" : "";
}

/** Compact, unit-aware number: 95.13 -> "95.13%" for CPU, 12100 -> "12.1K" for ops. */
export function formatMetricValue(name, v) {
  if (v == null || v === "") return "—";
  const n = parseFloat(v);
  if (Number.isNaN(n)) return String(v);
  const abs = Math.abs(n);
  let out;
  if (abs >= 1e12)      out = (n / 1e12).toFixed(2) + "T";
  else if (abs >= 1e9)  out = (n / 1e9).toFixed(2) + "G";
  else if (abs >= 1e6)  out = (n / 1e6).toFixed(2) + "M";
  else if (abs >= 1e4)  out = (n / 1e3).toFixed(1) + "K";
  else if (abs !== 0 && abs < 0.1) out = n.toPrecision(2);
  else out = n % 1 === 0 ? String(n) : n.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  return out + metricUnit(name);
}
