#!/usr/bin/env python3
"""
apply_alert_evaluation_hardening_code_fix.py

Companion to apply_alert_evaluation_hardening_migration.py (run that
FIRST — this code expects alerts.last_seen_at / alerts.healthy_streak /
alert_pending to already exist).

What this changes and why:

1. app/collector/alert_evaluator.py — full rewrite. Was: any single
   breaching reading immediately creates a visible, beeping alert, and an
   alert that's still breaching every cycle never gets its current_value /
   last_seen_at touched again until it escalates or resolves — which is
   why alerts from months ago were still sitting there looking frozen.
   Now: a breach has to sustain for thresholds.evaluation_period minutes
   (a column that already existed in the schema but was never read) via a
   holding table (alert_pending) before it becomes a visible alert, and
   resolving requires the same number of consecutive healthy readings.
   Visible alerts get last_seen_at/current_value refreshed every cycle
   they're re-confirmed, whether breaching or recovering.

2. app/collector/alert_evaluator.py's stale-alert cleanup also now
   auto-resolves alerts whose resource_id has NO row in `resources` at
   all (orphaned/mistyped legacy resource_ids from before the VM/YACE
   migration — confirmed present in this repo's data, e.g. a volume ID
   off by one digit from anything currently discovered). This is
   DELIBERATELY narrower than "no data recently" -- see
   db/migrations/008_revert_falsely_resolved_alerts.sql for why a
   missing-data-based auto-resolve was tried before and reverted. A
   resource that simply hasn't reported in a while is surfaced as
   "stale", not resolved.

3. app/ws/publisher.py — adds publish_alert_resolved() so resolutions
   (manual, sustained-recovery, or the stale-account/orphan sweep) push
   over the existing alerts WebSocket channel instead of only being
   picked up on the next 10s poll.

4. app/api/alerts.py — both alert list endpoints now return
   last_seen_at and a computed `stale` boolean (active + no fresh data
   in 20+ min). Manual resolve now also publishes over the socket.

5. frontend/src/pages/Alerts.jsx — shows a "stale — no data Xh" flag
   under TRIGGERED for alerts the API marks stale, and reloads the list
   on the new bulk_alerts_changed WebSocket event.

Usage:
    python apply_alert_evaluation_hardening_code_fix.py [repo_root]

Idempotent, backs up to "<file>.bak.pre-alert-hardening-fix", reverts
automatically if any patched Python file fails py_compile.
"""
import ast
import py_compile
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-alert-hardening-fix"
MARKER = "Requires a breach to be RE-CONFIRMED"


class PatchError(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backup(path: Path):
    bak = path.with_name(path.name + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


def apply_replacements(path: Path, replacements, already_applied_marker=None):
    text = read(path)
    if already_applied_marker and already_applied_marker in text:
        print(f"  SKIP  {path} (already patched)")
        return False

    backup(path)
    for old, new, label in replacements:
        count = text.count(old)
        if count == 0:
            raise PatchError(f"{path}: pattern not found for '{label}'")
        if count > 1:
            raise PatchError(f"{path}: pattern for '{label}' matches {count} times, expected 1")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"  OK    {path} ({len(replacements)} edit{'s' if len(replacements) != 1 else ''})")
    return True


def replace_whole_file(path: Path, new_content: str, already_applied_marker: str):
    if already_applied_marker in read(path):
        print(f"  SKIP  {path} (already patched)")
        return False
    backup(path)
    path.write_text(new_content, encoding="utf-8")
    print(f"  OK    {path} (full rewrite)")
    return True


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    print(f"Repo root: {repo_root}")

    staging = repo_root / "app" / "collector" / "alert_evaluator_new.py"
    if not staging.exists():
        print(f"ABORTED: staging file not found: {staging}\n"
              f"This script expects alert_evaluator_new.py to already be present "
              f"next to alert_evaluator.py (it ships alongside this script).",
              file=sys.stderr)
        sys.exit(1)

    touched_py_files = []
    any_change = False

    # ------------------------------------------------------------------
    # app/collector/alert_evaluator.py — full rewrite
    # ------------------------------------------------------------------
    p = repo_root / "app" / "collector" / "alert_evaluator.py"
    if replace_whole_file(p, read(staging), already_applied_marker=MARKER):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # app/ws/publisher.py — add publish_alert_resolved
    # ------------------------------------------------------------------
    p = repo_root / "app" / "ws" / "publisher.py"
    marker = "def publish_alert_resolved"
    replacements = [
        (
            'def publish_alert(alert_id: int, severity: str, metric: str,\n'
            '                  value: float, threshold: float, account_id: int):\n'
            '    publish("alerts", {\n'
            '        "type": "new_alert",\n'
            '        "alert_id": alert_id,\n'
            '        "severity": severity,\n'
            '        "metric": metric,\n'
            '        "value": round(value, 2),\n'
            '        "threshold": round(threshold, 2),\n'
            '        "account_id": account_id,\n'
            '    })',

            'def publish_alert(alert_id: int, severity: str, metric: str,\n'
            '                  value: float, threshold: float, account_id: int):\n'
            '    publish("alerts", {\n'
            '        "type": "new_alert",\n'
            '        "alert_id": alert_id,\n'
            '        "severity": severity,\n'
            '        "metric": metric,\n'
            '        "value": round(value, 2),\n'
            '        "threshold": round(threshold, 2),\n'
            '        "account_id": account_id,\n'
            '    })\n'
            '\n'
            '\n'
            'def publish_alert_resolved(alert_id: int | None, account_id: int | None, bulk: bool = False):\n'
            '    """\n'
            "    Pushed whenever an alert leaves 'active' status outside of a direct\n"
            "    user click (auto-resolved by evaluate_alerts' sustained-recovery\n"
            '    check or by _auto_resolve_stale_alerts). The Alerts page already\n'
            '    polls every 10s, so this just makes it feel instant / lets other\n'
            '    open tabs update without waiting for the poll.\n'
            '\n'
            '    bulk=True (id/account omitted) is used for the stale-alert sweep,\n'
            '    which can touch several alerts in one pass — the frontend just\n'
            "    reloads its list rather than patching individual rows.\n"
            '    """\n'
            '    publish("alerts", {\n'
            '        "type": "alert_resolved" if not bulk else "bulk_alerts_changed",\n'
            '        "id": alert_id,\n'
            '        "account_id": account_id,\n'
            '    })',
            "publisher.py: add publish_alert_resolved",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # app/api/alerts.py — last_seen_at + stale flag, publish on resolve
    # ------------------------------------------------------------------
    p = repo_root / "app" / "api" / "alerts.py"
    marker = "_STALE_AFTER_MINUTES"
    replacements = [
        (
            'from app.aws.federation import (\n'
            '    build_federated_console_url,\n'
            '    resource_console_destination,\n'
            '    NoConsoleCredentialsError,\n'
            ')\n'
            '\n'
            'logger = logging.getLogger(__name__)\n'
            '\n'
            'router = APIRouter(prefix="/alerts", tags=["Alerts"])\n'
            '\n'
            '# Simple in-process cache — alerts list doesn\'t change sub-second\n'
            '_alerts_cache: dict = {"data": None, "ts": 0}\n'
            '_CACHE_TTL = 15  # seconds — short enough for near-realtime, avoids hammering DB',

            'from app.aws.federation import (\n'
            '    build_federated_console_url,\n'
            '    resource_console_destination,\n'
            '    NoConsoleCredentialsError,\n'
            ')\n'
            'from app.ws.publisher import publish_alert_resolved\n'
            '\n'
            'logger = logging.getLogger(__name__)\n'
            '\n'
            'router = APIRouter(prefix="/alerts", tags=["Alerts"])\n'
            '\n'
            '# Simple in-process cache — alerts list doesn\'t change sub-second\n'
            '_alerts_cache: dict = {"data": None, "ts": 0}\n'
            '_CACHE_TTL = 15  # seconds — short enough for near-realtime, avoids hammering DB\n'
            '\n'
            '# An active alert whose last_seen_at hasn\'t been touched in this long has\n'
            '# stopped getting fresh metric data -- surfaced to the UI as "stale / no\n'
            '# data" so it\'s not mistaken for a live, just-reconfirmed breach. It is\n'
            '# NOT auto-resolved (see 008_revert_falsely_resolved_alerts.sql) -- this\n'
            '# is display-only, the operator decides whether to resolve it.\n'
            '_STALE_AFTER_MINUTES = 20',
            "alerts.py: import publisher + stale threshold constant",
        ),
        (
            "            a.value,\n"
            "            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,\n"
            "            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,\n"
            "            a.acked,\n"
            "            a.muted_until,\n"
            "            a.environment,\n"
            "            r.resource_type                        AS service,\n"
            "            COALESCE(r.name, a.resource_id)        AS resource_name,\n"
            "            acc.account_name,\n"
            "            acc.id                                 AS account_id\n"
            "        FROM alerts a\n"
            "        JOIN resources r      ON r.resource_id = a.resource_id\n"
            "        JOIN aws_accounts acc ON acc.id = r.aws_account_id\n"
            "                               AND acc.status = 'active'\n"
            "        ORDER BY a.triggered_at DESC\n"
            "        LIMIT 200\n"
            '    """)\n'
            "    rows = cursor.fetchall()\n"
            "    cursor.close()\n"
            "    conn.close()\n"
            "\n"
            "    for r in rows:\n"
            '        for field in ("triggered_at", "resolved_at"):\n'
            "            if r.get(field) and isinstance(r[field], datetime.datetime):\n"
            '                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")\n'
            "            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith(\"Z\"):\n"
            '                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"\n'
            "\n"
            "    return rows\n"
            "\n"
            "\n"
            "# ── GET all alerts (cached) ───────────────────────────────────",

            "            a.value,\n"
            "            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,\n"
            "            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,\n"
            "            CONVERT_TZ(a.last_seen_at, @@session.time_zone, '+00:00') AS last_seen_at,\n"
            "            (a.status = 'active'\n"
            "             AND a.last_seen_at IS NOT NULL\n"
            "             AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE)\n"
            "            ) AS stale,\n"
            "            a.acked,\n"
            "            a.muted_until,\n"
            "            a.environment,\n"
            "            r.resource_type                        AS service,\n"
            "            COALESCE(r.name, a.resource_id)        AS resource_name,\n"
            "            acc.account_name,\n"
            "            acc.id                                 AS account_id\n"
            "        FROM alerts a\n"
            "        JOIN resources r      ON r.resource_id = a.resource_id\n"
            "        JOIN aws_accounts acc ON acc.id = r.aws_account_id\n"
            "                               AND acc.status = 'active'\n"
            "        ORDER BY a.triggered_at DESC\n"
            "        LIMIT 200\n"
            '    """.format(stale=_STALE_AFTER_MINUTES))\n'
            "    rows = cursor.fetchall()\n"
            "    cursor.close()\n"
            "    conn.close()\n"
            "\n"
            "    for r in rows:\n"
            '        for field in ("triggered_at", "resolved_at", "last_seen_at"):\n'
            "            if r.get(field) and isinstance(r[field], datetime.datetime):\n"
            '                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")\n'
            "            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith(\"Z\"):\n"
            '                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"\n'
            '        r["stale"] = bool(r.get("stale"))\n'
            "\n"
            "    return rows\n"
            "\n"
            "\n"
            "# ── GET all alerts (cached) ───────────────────────────────────",
            "alerts.py: get_alerts() adds last_seen_at + stale",
        ),
        (
            "            a.value,\n"
            "            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,\n"
            "            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,\n"
            "            a.acked,\n"
            "            a.environment,\n"
            "            r.resource_type                        AS service,\n"
            "            COALESCE(r.name, a.resource_id)        AS resource_name,\n"
            "            acc.account_name,\n"
            "            acc.id                                 AS account_id,\n"
            "            COALESCE(a.region, acc.default_region) AS region\n"
            "        FROM alerts a\n"
            "        JOIN resources r      ON r.resource_id = a.resource_id\n"
            "        JOIN aws_accounts acc ON acc.id = r.aws_account_id\n"
            "                               AND acc.status = 'active'\n"
            "        WHERE a.resolved_at IS NULL\n"
            "            ORDER BY\n"
            "            FIELD(a.severity, 'CRITICAL', 'WARNING', 'INFO'),\n"
            "            a.triggered_at DESC\n"
            "        LIMIT 100\n"
            '    """)\n'
            "    rows = cursor.fetchall()\n"
            "    cursor.close()\n"
            "    conn.close()\n"
            "\n"
            "    for r in rows:\n"
            '        for field in ("triggered_at", "resolved_at"):\n'
            "            if r.get(field) and isinstance(r[field], datetime.datetime):\n"
            '                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")\n'
            "            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith(\"Z\"):\n"
            '                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"\n'
            "\n"
            "    return rows",

            "            a.value,\n"
            "            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,\n"
            "            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,\n"
            "            CONVERT_TZ(a.last_seen_at, @@session.time_zone, '+00:00') AS last_seen_at,\n"
            "            (a.status = 'active'\n"
            "             AND a.last_seen_at IS NOT NULL\n"
            "             AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE)\n"
            "            ) AS stale,\n"
            "            a.acked,\n"
            "            a.environment,\n"
            "            r.resource_type                        AS service,\n"
            "            COALESCE(r.name, a.resource_id)        AS resource_name,\n"
            "            acc.account_name,\n"
            "            acc.id                                 AS account_id,\n"
            "            COALESCE(a.region, acc.default_region) AS region\n"
            "        FROM alerts a\n"
            "        JOIN resources r      ON r.resource_id = a.resource_id\n"
            "        JOIN aws_accounts acc ON acc.id = r.aws_account_id\n"
            "                               AND acc.status = 'active'\n"
            "        WHERE a.resolved_at IS NULL\n"
            "            ORDER BY\n"
            "            FIELD(a.severity, 'CRITICAL', 'WARNING', 'INFO'),\n"
            "            a.triggered_at DESC\n"
            "        LIMIT 100\n"
            '    """.format(stale=_STALE_AFTER_MINUTES))\n'
            "    rows = cursor.fetchall()\n"
            "    cursor.close()\n"
            "    conn.close()\n"
            "\n"
            "    for r in rows:\n"
            '        for field in ("triggered_at", "resolved_at", "last_seen_at"):\n'
            "            if r.get(field) and isinstance(r[field], datetime.datetime):\n"
            '                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")\n'
            "            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith(\"Z\"):\n"
            '                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"\n'
            '        r["stale"] = bool(r.get("stale"))\n'
            "\n"
            "    return rows",
            "alerts.py: open_alerts() adds last_seen_at + stale",
        ),
        (
            '# ── RESOLVE ───────────────────────────────────────────────────\n'
            '@router.post("/{alert_id}/resolve")\n'
            '@router.patch("/{alert_id}/resolve")\n'
            'def resolve_alert(alert_id: int):\n'
            '    conn   = get_connection()\n'
            '    cursor = conn.cursor()\n'
            '    cursor.execute(\n'
            '        "UPDATE alerts SET resolved_at = UTC_TIMESTAMP(), status = \'resolved\' WHERE id = %s",\n'
            '        (alert_id,)\n'
            '    )\n'
            '    if cursor.rowcount == 0:\n'
            '        raise HTTPException(status_code=404, detail="Alert not found")\n'
            '    conn.commit()\n'
            '    cursor.close()\n'
            '    conn.close()\n'
            '    _invalidate_cache()\n'
            '    return {"status": "resolved", "alert_id": alert_id}',

            '# ── RESOLVE ───────────────────────────────────────────────────\n'
            '@router.post("/{alert_id}/resolve")\n'
            '@router.patch("/{alert_id}/resolve")\n'
            'def resolve_alert(alert_id: int):\n'
            '    conn   = get_connection()\n'
            '    cursor = conn.cursor(dictionary=True)\n'
            '    cursor.execute(\n'
            '        "UPDATE alerts SET resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(), "\n'
            '        "status = \'resolved\' WHERE id = %s",\n'
            '        (alert_id,)\n'
            '    )\n'
            '    if cursor.rowcount == 0:\n'
            '        raise HTTPException(status_code=404, detail="Alert not found")\n'
            '    conn.commit()\n'
            '\n'
            '    cursor.execute("""\n'
            '        SELECT acc.id AS account_id\n'
            '        FROM alerts a\n'
            '        JOIN resources r      ON r.resource_id = a.resource_id\n'
            '        JOIN aws_accounts acc ON acc.id = r.aws_account_id\n'
            '        WHERE a.id = %s\n'
            '    """, (alert_id,))\n'
            '    row = cursor.fetchone()\n'
            '    cursor.close()\n'
            '    conn.close()\n'
            '    _invalidate_cache()\n'
            '\n'
            '    try:\n'
            '        publish_alert_resolved(alert_id=alert_id, account_id=row["account_id"] if row else None)\n'
            '    except Exception as e:\n'
            '        logger.warning(f"Resolve publish failed: {e}")\n'
            '\n'
            '    return {"status": "resolved", "alert_id": alert_id}',
            "alerts.py: resolve_alert() publishes + touches last_seen_at",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # frontend/src/pages/Alerts.jsx — stale flag + bulk-refresh handling
    # ------------------------------------------------------------------
    p = repo_root / "frontend" / "src" / "pages" / "Alerts.jsx"
    marker = "bulk_alerts_changed"
    replacements = [
        (
            '    if (lastMessage.type === "alert_acknowledged" && lastMessage.id) {\n'
            '      setAlerts(prev =>\n'
            '        prev.map(a => a.id === lastMessage.id ? { ...a, status: "acknowledged" } : a)\n'
            '      );\n'
            '    }\n'
            '  }, [lastMessage, soundOn]);',

            '    if (lastMessage.type === "alert_acknowledged" && lastMessage.id) {\n'
            '      setAlerts(prev =>\n'
            '        prev.map(a => a.id === lastMessage.id ? { ...a, status: "acknowledged" } : a)\n'
            '      );\n'
            '    }\n'
            '\n'
            '    // Backend auto-resolved a batch (account removed / orphaned resource\n'
            '    // cleanup) — just reload rather than trying to patch rows we may not\n'
            '    // even have IDs for.\n'
            '    if (lastMessage.type === "bulk_alerts_changed") {\n'
            '      loadAlerts();\n'
            '    }\n'
            '  }, [lastMessage, soundOn, loadAlerts]);',
            "Alerts.jsx: handle bulk_alerts_changed",
        ),
        (
            '                      <td className="mono small">\n'
            '                        {a.triggered_at ? shortDateTime(a.triggered_at) : "—"}\n'
            '                      </td>',

            '                      <td className="mono small">\n'
            '                        {a.triggered_at ? shortDateTime(a.triggered_at) : "—"}\n'
            '                        {a.stale && (\n'
            '                          <div\n'
            '                            className="alert-stale-flag"\n'
            '                            title="No fresh metric data for this resource in a while — the resource may have been decommissioned, or the collector/VictoriaMetrics pipeline may be down for it. This alert has NOT been auto-resolved; verify before dismissing."\n'
            '                            style={{ color: "#c98a2b", fontSize: 11, marginTop: 2 }}\n'
            '                          >\n'
            '                            ⚠ stale — no data {timeSince(a.last_seen_at)}\n'
            '                          </div>\n'
            '                        )}\n'
            '                      </td>',
            "Alerts.jsx: render stale flag",
        ),
        (
            'function shortDateTime(iso) {',

            'function timeSince(iso) {\n'
            '  if (!iso) return "";\n'
            '  try {\n'
            '    const ms = Date.now() - new Date(iso).getTime();\n'
            '    const mins = Math.floor(ms / 60000);\n'
            '    if (mins < 60) return `${mins}m`;\n'
            '    const hrs = Math.floor(mins / 60);\n'
            '    if (hrs < 24) return `${hrs}h`;\n'
            '    return `${Math.floor(hrs / 24)}d`;\n'
            '  } catch {\n'
            '    return "";\n'
            '  }\n'
            '}\n'
            '\n'
            'function shortDateTime(iso) {',
            "Alerts.jsx: add timeSince helper",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True

    # ------------------------------------------------------------------
    # Verify Python compiles; revert everything on failure
    # ------------------------------------------------------------------
    compile_errors = []
    for py_path in touched_py_files:
        try:
            ast.parse(read(py_path), filename=str(py_path))
            py_compile.compile(str(py_path), doraise=True)
        except Exception as e:
            compile_errors.append((py_path, e))

    if compile_errors:
        print("\nCOMPILE ERRORS — reverting all changes:")
        for py_path, e in compile_errors:
            print(f"  {py_path}: {e}")
        for py_path in touched_py_files:
            bak = py_path.with_name(py_path.name + BAK_SUFFIX)
            if bak.exists():
                shutil.copy2(bak, py_path)
                print(f"  reverted {py_path}")
        sys.exit(1)

    print("\nAll patched Python files compiled cleanly.")
    if any_change:
        staging.unlink(missing_ok=True)
        print("\nDone. Before restarting the backend:")
        print("  1. Run apply_alert_evaluation_hardening_migration.py FIRST if you")
        print("     haven't already (this code depends on its schema changes).")
        print("  2. Restart the backend so the scheduler picks up the new evaluator.")
        print("  3. Rebuild/restart the frontend (npm run build, or your dev server")
        print("     will hot-reload Alerts.jsx automatically).")
        print("\nExpect a quiet period right after deploy: every currently-open alert")
        print("keeps running as-is (nothing is touched retroactively), but any NEW")
        print("breach now needs to sustain for its threshold's evaluation_period")
        print("before it becomes visible — so don't be alarmed if the alert count")
        print("looks lower for the first cycle or two.")
    else:
        print("Nothing to do — already patched.")


if __name__ == "__main__":
    main()
