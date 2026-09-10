#!/usr/bin/env python3
"""
apply_add_warning_threshold_line.py
========================================
Fixes: issue 2.4 from the Sep 6-10 handover -- every chart only ever
draws ONE dashed reference line, and there's no separate warning line
at all.

ROOT CAUSE (confirmed): MetricChart (frontend/src/pages/
ServiceDetail.jsx) only ever supported a single `threshold` prop / one
<Line dataKey="threshold">. When apply_fix_hardcoded_chart_thresholds.py
replaced the old hardcoded literal with a real lookup, that single line
was wired to `warning_value` -- there has never been a second line for
`critical_value` anywhere in this codebase.

FIX:
  1. thresholdMap (ServiceDetailPanel) now stores BOTH warning_value and
     critical_value per (resource_type, metric_name) key, instead of
     just warning_value.
  2. getThreshold(resourceType, metricName) now returns
     { warning, critical } (or undefined if nothing is configured for
     that pair) instead of a bare number.
  3. MetricChart now takes warningThreshold / criticalThreshold props
     (replacing the old single threshold / thresholdLabel props) and
     draws BOTH as distinct reference lines: warning in amber
     (#f59e0b, dashed) matching the Warn color already used in the
     Metric Thresholds editor, critical in red (#ef4444, dash-dot)
     matching the existing Crit color there -- so the chart's visual
     language matches Settings -> Metric Thresholds exactly.
  4. All 17 chart call sites updated from
     `threshold={getThreshold(a, b)}` to
     `warningThreshold={getThreshold(a, b)?.warning} criticalThreshold={getThreshold(a, b)?.critical}`
     -- same lookup, both values now threaded through. The one site
     with a custom `thresholdLabel="alert threshold"` had that prop
     dropped, since MetricChart's tooltip now always labels each line
     specifically as "Warn" / "Crit" rather than a single generic
     "alert threshold" -- more precise now that there are two lines to
     tell apart, and no call site relied on any label text besides that
     one.

SAFETY: purely additive to the data actually shown -- a chart with only
a warning_value configured still shows just that one line (critical
line simply doesn't render, same "undefined -> no line" behavior the
single-line version already had). A chart with neither configured
still shows no reference lines at all, same as before.

TESTED: after patching, this script re-parses the resulting
ServiceDetail.jsx with esbuild (a real JSX parser, not a guess) to
catch any malformed JSX before it's ever deployed, and greps the
result to confirm the expected 17-for-17 call-site conversion. A
manual `npm run build` is still required afterward as the final,
authoritative check (below) -- this script's checks catch the most
common failure modes (bad JSX, wrong replacement count) early, not the
full build.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_add_warning_threshold_line.py --dry-run
    python3 apply_add_warning_threshold_line.py --apply
    cd frontend && npm install && npm run build && cd ..
    sudo systemctl restart monitoring-hub
(the frontend rebuild is required -- this changes .jsx source, not the
built dist/ the server actually serves)
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

REL_PATH = os.path.join("frontend", "src", "pages", "ServiceDetail.jsx")

DONE_MARKER = "warningThreshold={getThreshold"

# ── 1 & 2: thresholdMap now stores both values; getThreshold returns both ──

STATE_OLD = '''  // Real, currently-configured thresholds for this account -- charts
  // used to draw a hardcoded, unrelated example number as the dashed
  // reference line (e.g. always "85" for CPUUtilization no matter what
  // Settings -> Metric Thresholds actually has configured for this
  // account). include_no_data=true because a chart-page visitor
  // benefits from seeing a configured-but-not-yet-collecting threshold
  // just as much as an actively-firing one -- the "hide no data" filter
  // is specifically for decluttering the Settings list, not relevant
  // here. Lives in THIS component, not the parent ServiceDetail(),
  // because that's a separate function scope -- the earlier version of
  // this fix defined getThreshold() in the parent while every chart
  // that needs it renders here, causing a live
  // "ReferenceError: getThreshold is not defined". See
  // apply_fix_getthreshold_scope.py.
  useEffect(() => {
    if (!accountId) return;
    fetch(`/api/settings/thresholds?account_id=${accountId}&include_no_data=true`)
      .then(r => r.ok ? r.json() : { thresholds: [] })
      .then(data => {
        const map = {};
        (data.thresholds || []).forEach(t => {
          if (t.metric_name) map[`${t.resource_type}:${t.metric_name}`] = t.warning_value;
        });
        setThresholdMap(map);
      })
      .catch(() => {});
  }, [accountId]);

  // Looks up the REAL warning threshold configured in Settings for this
  // exact (resourceType, metricName) pair -- returns undefined if none
  // is configured, which MetricChart already correctly renders as "no
  // dashed line" rather than a misleading default.
  function getThreshold(resourceType, metricName) {
    return thresholdMap[`${resourceType}:${metricName}`];
  }'''

STATE_NEW = '''  // Real, currently-configured thresholds for this account -- charts
  // used to draw a hardcoded, unrelated example number as the dashed
  // reference line (e.g. always "85" for CPUUtilization no matter what
  // Settings -> Metric Thresholds actually has configured for this
  // account). include_no_data=true because a chart-page visitor
  // benefits from seeing a configured-but-not-yet-collecting threshold
  // just as much as an actively-firing one -- the "hide no data" filter
  // is specifically for decluttering the Settings list, not relevant
  // here. Lives in THIS component, not the parent ServiceDetail(),
  // because that's a separate function scope -- the earlier version of
  // this fix defined getThreshold() in the parent while every chart
  // that needs it renders here, causing a live
  // "ReferenceError: getThreshold is not defined". See
  // apply_fix_getthreshold_scope.py.
  //
  // Stores BOTH warning_value and critical_value per key -- the
  // original version only kept warning_value, which is why charts
  // never had a separate critical line to draw. See
  // apply_add_warning_threshold_line.py.
  useEffect(() => {
    if (!accountId) return;
    fetch(`/api/settings/thresholds?account_id=${accountId}&include_no_data=true`)
      .then(r => r.ok ? r.json() : { thresholds: [] })
      .then(data => {
        const map = {};
        (data.thresholds || []).forEach(t => {
          if (t.metric_name) {
            map[`${t.resource_type}:${t.metric_name}`] = {
              warning: t.warning_value,
              critical: t.critical_value,
            };
          }
        });
        setThresholdMap(map);
      })
      .catch(() => {});
  }, [accountId]);

  // Looks up the REAL warning + critical thresholds configured in
  // Settings for this exact (resourceType, metricName) pair -- returns
  // undefined if nothing is configured for it, or { warning, critical }
  // (either of which may itself be undefined if only one was set).
  // MetricChart renders "no line" for whichever value is missing,
  // rather than a misleading default.
  function getThreshold(resourceType, metricName) {
    return thresholdMap[`${resourceType}:${metricName}`];
  }'''

# ── 3: MetricChart itself -- two lines instead of one ──

CHART_OLD = '''function MetricChart({ title, data, color, unit, threshold, thresholdLabel, timeRange }) {
  const { ianaName } = useTimezone();
  // data === null (not undefined, not []) means the backend knows this
  // metric structurally can never have data for this resource (e.g. EBS
  // BurstBalance -- dropped from collection with no fallback, see
  // apply_hide_no_data_metrics.py) -- hide the card entirely instead of
  // showing a permanent, pointless "no data" placeholder. data === []
  // still means "might have data later, just none in this window" and
  // keeps the existing placeholder below.
  if (data === null) return null;
  if (!data || data.length === 0) return (
    <div className="chart-box">
      <div className="chart-title">{title}</div>
      <div className="chart-empty">No data in last {timeRange || "6H"}</div>
    </div>
  );
  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName }),
    v: d.v,
    ...(threshold ? { threshold } : {}),
  }));
  return (
    <div className="chart-box">
      <div className="chart-header">
        <span className="chart-title">{title}</span>
        <span className="chart-latest" style={{ color }}>{latest.toFixed(1)}{unit}</span>
      </div>
      <ResponsiveContainer width="100%" height={90}>
        <LineChart data={formatted} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: "#7a90b8" }}
            formatter={(value, name) => {
              if (name === "threshold") return [`${value}${unit} (${thresholdLabel || "threshold"})`, <span style={{display:"inline-flex",alignItems:"center",gap:4}}><AlertTriangleIcon size={11} /> Alert at</span>];
              return [`${value.toFixed(2)}${unit}`, title];
            }}
            itemStyle={{ color }}
          />
          {threshold && (
            <Line type="monotone" dataKey="threshold" stroke="#ef4444" strokeDasharray="4 4" dot={false} strokeWidth={1} legendType="none" />
          )}
          <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: color }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}'''

CHART_NEW = '''function MetricChart({ title, data, color, unit, warningThreshold, criticalThreshold, timeRange }) {
  const { ianaName } = useTimezone();
  // data === null (not undefined, not []) means the backend knows this
  // metric structurally can never have data for this resource (e.g. EBS
  // BurstBalance -- dropped from collection with no fallback, see
  // apply_hide_no_data_metrics.py) -- hide the card entirely instead of
  // showing a permanent, pointless "no data" placeholder. data === []
  // still means "might have data later, just none in this window" and
  // keeps the existing placeholder below.
  if (data === null) return null;
  if (!data || data.length === 0) return (
    <div className="chart-box">
      <div className="chart-title">{title}</div>
      <div className="chart-empty">No data in last {timeRange || "6H"}</div>
    </div>
  );
  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName }),
    v: d.v,
    ...(warningThreshold != null ? { warningThreshold } : {}),
    ...(criticalThreshold != null ? { criticalThreshold } : {}),
  }));
  return (
    <div className="chart-box">
      <div className="chart-header">
        <span className="chart-title">{title}</span>
        <span className="chart-latest" style={{ color }}>{latest.toFixed(1)}{unit}</span>
      </div>
      <ResponsiveContainer width="100%" height={90}>
        <LineChart data={formatted} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: "#7a90b8" }}
            formatter={(value, name) => {
              if (name === "warningThreshold") return [`${value}${unit}`, <span style={{display:"inline-flex",alignItems:"center",gap:4}}><AlertTriangleIcon size={11} /> Warn at</span>];
              if (name === "criticalThreshold") return [`${value}${unit}`, <span style={{display:"inline-flex",alignItems:"center",gap:4}}><AlertTriangleIcon size={11} /> Crit at</span>];
              return [`${value.toFixed(2)}${unit}`, title];
            }}
            itemStyle={{ color }}
          />
          {warningThreshold != null && (
            <Line type="monotone" dataKey="warningThreshold" stroke="#f59e0b" strokeDasharray="4 4" dot={false} strokeWidth={1} legendType="none" />
          )}
          {criticalThreshold != null && (
            <Line type="monotone" dataKey="criticalThreshold" stroke="#ef4444" strokeDasharray="2 3" dot={false} strokeWidth={1} legendType="none" />
          )}
          <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: color }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}'''

# ── 4: all call sites -- regex-based, ~17 occurrences ──

# Matches: threshold={getThreshold("x", "y")}  (optionally followed by
# a thresholdLabel="..." prop, which is dropped -- MetricChart now
# always labels its own two lines "Warn at" / "Crit at").
CALLSITE_PATTERN = re.compile(
    r'threshold=\{getThreshold\((\s*"[^"]*"\s*,\s*"[^"]*"\s*)\)\}'
    r'(?:\s+thresholdLabel="[^"]*")?'
)


def callsite_replacement(m):
    args = m.group(1)
    return f'warningThreshold={{getThreshold({args})?.warning}} criticalThreshold={{getThreshold({args})?.critical}}'


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def esbuild_syntax_check(jsx_source):
    """
    Best-effort real JSX parse via esbuild, if available on PATH or via
    `npx esbuild`. Not fatal if esbuild itself can't be found (e.g. no
    network / not installed) -- `npm run build` in the manual follow-up
    remains the authoritative check either way. This just catches
    obviously broken JSX (mismatched braces/tags from the regex
    substitution) before that.
    """
    esbuild_bin = shutil.which("esbuild")
    with tempfile.NamedTemporaryFile(suffix=".jsx", mode="w", delete=False, encoding="utf-8") as f:
        f.write(jsx_source)
        tmp_path = f.name
    try:
        if esbuild_bin:
            cmd = [esbuild_bin, tmp_path, "--outfile=/dev/null"]
        else:
            cmd = ["npx", "--yes", "esbuild", tmp_path, "--outfile=/dev/null"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return False, result.stderr
        return True, None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return None, str(e)  # esbuild unavailable -- not a failure, just skipped
    finally:
        os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    path = os.path.join(repo_root, REL_PATH)
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    if not os.path.exists(path):
        die(f"{REL_PATH} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if DONE_MARKER in content:
        print(f"\n{REL_PATH} already patched -- skipping. Nothing to do.")
        return

    if STATE_OLD not in content:
        die(f"{REL_PATH}: thresholdMap/getThreshold block doesn't match what this script expects. "
            "File may have changed since this script was written.")
    if CHART_OLD not in content:
        die(f"{REL_PATH}: MetricChart() doesn't match what this script expects. "
            "File may have changed since this script was written.")

    callsite_matches = CALLSITE_PATTERN.findall(content)
    if len(callsite_matches) != 17:
        die(f"{REL_PATH}: expected exactly 17 chart call sites using threshold={{getThreshold(...)}}, "
            f"found {len(callsite_matches)}. File may have changed since this script was written -- "
            "refusing to guess.")

    new_content = content.replace(STATE_OLD, STATE_NEW, 1)
    new_content = new_content.replace(CHART_OLD, CHART_NEW, 1)
    new_content, n_subs = CALLSITE_PATTERN.subn(callsite_replacement, new_content)
    if n_subs != 17:
        die(f"Regex substitution converted {n_subs} call sites, expected 17. Aborting without writing.")

    print(f"\nFile patch plan:\n  {REL_PATH}: OK ({len(new_content) - len(content):+d} bytes, 17/17 call sites converted)")

    ok, detail = esbuild_syntax_check(new_content)
    if ok is False:
        die(f"Patched {REL_PATH} failed JSX syntax check via esbuild:\n{detail}")
    elif ok is None:
        print(f"[warn] Could not run esbuild syntax check ({detail}) -- skipping. "
              "`npm run build` in the manual follow-up below is still required.")
    else:
        print("[selftest] OK -- patched file parses cleanly as valid JSX (esbuild).")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print(f"Patched {REL_PATH}")

    print(f"""
[Manual follow-up -- REQUIRED]

  A) Rebuild the frontend:
       cd frontend
       npm install
       npm run build
       cd ..

  B) Restart:
       sudo systemctl restart monitoring-hub

  C) Open any resource detail page with both warning_value AND
     critical_value configured (e.g. EC2 CPUUtilization: warn 70 /
     crit 85) -- the chart should now show TWO dashed reference lines:
     amber for warning, red for critical, matching the colors already
     used in the Metric Thresholds editor.

  D) Review, commit, push:
       git diff {REL_PATH}
       git add {REL_PATH} apply_add_warning_threshold_line.py
       git commit -m "fix(ui): charts only ever drew one threshold line (wired to warning_value); add a second, distinct line for critical_value"
       git push origin main
""")


if __name__ == "__main__":
    main()
