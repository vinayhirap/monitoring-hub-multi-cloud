# app/collector/env_sync.py
"""
Persists app/environment_classifier.resolve_environment() onto resources
(env_resolved / env_source / env_reason, migration 083).

Used by:
  * the discovery loop (scheduler.py) -- right after each discovery run, so a
    resource whose tags or name changed is re-classified, and a brand-new
    resource lands in the Needs-classification queue without waiting for an
    alert;
  * the classification API -- immediately after a manual override or an
    account-default change;
  * apply_environment_backfill.py -- the dry-run-first backfill.

Idempotent: a row is written only when its (env, source, reason) differ from
what is already stored, so re-running is a no-op and cheap.

INHERITANCE. A resource with no environment evidence of its own follows the resource it
is attached to (resource_relationships 'attached_to': EC2 -> its EBS volumes). Two
passes, one hop: every resource is first resolved on its own evidence, then children are
resolved against their parents' pass-1 result, so a value is never passed down a chain.
Only 'attached_to' is followed. 'routes_to' / 'invokes' / 'origin' are "calls" edges, not
"belongs to", and a load balancer fronting targets says nothing about their environment.
Classifying a parent re-evaluates its children in the same call.

dry_run=True computes everything and writes NOTHING.

The resolution itself is pure and lives in app/environment_classifier.py;
this module only loads inputs, compares and writes.
"""
import logging

from app.db import get_connection
from app import environment_classifier as ec

logger = logging.getLogger(__name__)

# Same freshness rule the metric collector uses for "still exists".
STALE_RESOURCE_HOURS = 48
# ENIs publish no metrics and are never alerted on; keep them out of the queue.
NON_ALERTABLE_TYPES = ("eni",)
_CHUNK = 500
INHERIT_EDGE_TYPES = ("attached_to",)

_BASE_SELECT = """
    SELECT r.id, r.aws_account_id, r.resource_id, r.resource_type, r.name, r.tags,
           r.env_override, r.env_resolved, r.env_source, r.env_reason, r.tag_state,
           {extra}{synced},
           aa.default_env, aa.provider
    FROM resources r
    JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
    WHERE (r.instance_state IS NULL OR r.instance_state != 'terminated')
      AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR))
      AND r.resource_type NOT IN ({types})
"""


def _row_synced(row):
    """The fresh tag-sync result for a row (dict, {} = swept and absent) or None. Only types the sync covers."""
    synced = ec.parse_synced(row.get("synced_tags"))
    if synced is None or str(row.get("resource_type") or "").lower() not in ec.TAG_SYNC_TYPES:
        return None
    return synced


def _row_provider(row):
    """'azure' / 'gcp' for rows of those clouds while TAG_SYNC_ENABLED is on, else None (AWS rows, and every row with
    the flag off, are judged exactly as before). Azure and GCP discovery store the resource's own tags / labels for the
    types listed in environment_classifier.PROVIDER_TAG_TYPES."""
    if not ec.tag_sync_enabled():
        return None
    p = str(row.get("provider") or "").lower()
    return p if p in ec.PROVIDER_TAG_TYPES else None


def resolve_row(row, related=None):
    """One resources row (dict) -> classifier Resolution."""
    tags = row.get("tags")
    synced = _row_synced(row)
    if synced and ec.tag_sync_feeds_environment():
        # TAG_SYNC_FEED_ENVIRONMENT: an Environment tag learned from AWS decides the environment. Off by default.
        tags = {**ec._tags_dict(tags), **synced}
    return ec.resolve_environment(
        override=row.get("env_override"),
        tags=tags,
        name=row.get("name"),
        resource_id=row.get("resource_id"),
        account_default=row.get("default_env"),
        related=related,
    )


def _synced_fragment(cursor):
    """Select-list piece for the tag-sync result: NULL unless TAG_SYNC_ENABLED and migration 095 are both in place,
    so a deploy that restarts before the migration cannot break classification."""
    try:
        from app.collector.tag_sync import synced_select
        return synced_select(cursor)
    except Exception:
        return "NULL AS synced_tags"


def _load(cursor, account_ids=None, with_warning=True):
    sql = _BASE_SELECT.format(types=", ".join(["%s"] * len(NON_ALERTABLE_TYPES)),
                              extra="r.env_warning, " if with_warning else "",
                              synced=_synced_fragment(cursor))
    params = [STALE_RESOURCE_HOURS, *NON_ALERTABLE_TYPES]
    if account_ids is not None:
        if not account_ids:
            return []
        sql += " AND r.aws_account_id IN (" + ", ".join(["%s"] * len(account_ids)) + ")"
        params.extend(account_ids)
    cursor.execute(sql, params)
    return cursor.fetchall()


def _load_edges(cursor, account_ids=None):
    """-> [(aws_account_id, parent_resource_id, child_resource_id)]"""
    sql = ("SELECT aws_account_id, source_resource_id, target_resource_id FROM resource_relationships "
           "WHERE relationship_type IN (" + ", ".join(["%s"] * len(INHERIT_EDGE_TYPES)) + ")")
    params = list(INHERIT_EDGE_TYPES)
    if account_ids is not None:
        if not account_ids:
            return []
        sql += " AND aws_account_id IN (" + ", ".join(["%s"] * len(account_ids)) + ")"
        params.extend(account_ids)
    cursor.execute(sql, params)
    return [(r["aws_account_id"], r["source_resource_id"], r["target_resource_id"])
            for r in cursor.fetchall()]


def classify_rows(rows, edges):
    """Pure two-pass classification. -> {row id: Resolution}"""
    by_key = {(r["aws_account_id"], r["resource_id"]): r for r in rows}
    base = {r["id"]: resolve_row(r) for r in rows}
    parents_of = {}
    for acct, parent, child in edges:
        p, c = by_key.get((acct, parent)), by_key.get((acct, child))
        if p is not None and c is not None and p["id"] != c["id"]:
            parents_of.setdefault(c["id"], []).append(p)
    out = {}
    for r in rows:
        parents = parents_of.get(r["id"])
        if not parents:
            out[r["id"]] = base[r["id"]]
            continue
        related = [(base[p["id"]], f"{p['resource_type']} {p.get('name') or p['resource_id']}")
                   for p in parents]
        out[r["id"]] = resolve_row(r, related=related)
    return out, parents_of


def warn_rows(rows, resolved, parents_of):
    """Pure. -> {row id: warning message or None}. Warnings never alter `resolved`; they only describe
    evidence that disagrees (environment_classifier.detect_warning)."""
    out = {}
    for r in rows:
        res = resolved[r["id"]]
        attached = [(resolved[p["id"]], f"{p['resource_type']} {p.get('name') or p['resource_id']}")
                    for p in parents_of.get(r["id"], ())]
        hit = ec.detect_warning(
            env=res.env, source=res.source, override=r.get("env_override"), tags=r.get("tags"),
            name=r.get("name"), resource_id=r.get("resource_id"), resource_type=r.get("resource_type"),
            attached=attached)
        out[r["id"]] = hit[1] if hit else None
    return out


def _tally(counter, key):
    counter[key] = counter.get(key, 0) + 1


def sync_resource_environments(account_id=None, resource_pks=None, dry_run=False,
                               account_default_override=None, collect_changes=False):
    """Classify and persist. Returns a report dict:

        total            resources considered
        changed          rows whose stored classification differs from the new one
        applied          rows actually written (0 when dry_run)
        by_source        {source: count}   NEW classification, every considered row
        by_env           {env: count}
        changed_by_source / changed_by_env   the same, restricted to changed rows
        newly_unknown    rows that are now unknown and were not before

    account_default_override=(account_id, env_or_None) classifies as if that account's default were that value.
    It exists for the preview in the Environments page and REQUIRES dry_run=True: a hypothetical default is never
    written. collect_changes=True adds report["changes"], one dict per row that would be written
    (resource_id, old_env, old_source, new_env, new_source).

    resource_pks limits what is WRITTEN to those resources plus the resources attached to
    them (their children); the whole of each touched account is still loaded so a child can
    see its parent.
    """
    if account_default_override is not None and not dry_run:
        raise ValueError("account_default_override is for previews only and requires dry_run=True")
    report = {
        "dry_run": bool(dry_run), "total": 0, "changed": 0, "applied": 0,
        "by_source": {}, "by_env": {}, "changed_by_source": {}, "changed_by_env": {},
        "newly_unknown": 0,
        # mismatch warnings (migration 098): rows now carrying one / rows whose stored warning changed / written
        "warnings": 0, "warning_changed": 0, "warning_applied": 0,
    }
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            account_ids = None
            if account_id is not None:
                account_ids = [account_id]
            elif resource_pks:
                cursor.execute("SELECT DISTINCT aws_account_id FROM resources WHERE id IN ("
                               + ", ".join(["%s"] * len(resource_pks)) + ")", list(resource_pks))
                account_ids = [r["aws_account_id"] for r in cursor.fetchall()]
            with_warning = True
            try:
                rows = _load(cursor, account_ids)
            except Exception as e:
                if getattr(e, "errno", None) != 1054:
                    raise
                # migration 098 not applied yet: classify exactly as before, just without mismatch warnings
                with_warning = False
                rows = _load(cursor, account_ids, with_warning=False)
            edges = _load_edges(cursor, account_ids)
        finally:
            cursor.close()
        conn.commit()      # release the read snapshot before any write

        if account_default_override is not None:
            ov_account, ov_env = account_default_override
            rows = [dict(r, default_env=ov_env) if r["aws_account_id"] == ov_account else r for r in rows]
        if collect_changes:
            report["changes"] = []

        resolved, parents_of = classify_rows(rows, edges)

        scope = None
        if resource_pks:
            scope = set(resource_pks)
            for child_id, parents in parents_of.items():
                if any(p["id"] in scope for p in parents):
                    scope.add(child_id)

        warnings = warn_rows(rows, resolved, parents_of) if with_warning else {}
        warn_updates = []
        updates = []
        for row in rows:
            if scope is not None and row["id"] not in scope:
                continue
            res = resolved[row["id"]]
            report["total"] += 1
            if with_warning:
                new_w = warnings.get(row["id"])
                if new_w:
                    report["warnings"] += 1
                if (row.get("env_warning") or None) != new_w:
                    report["warning_changed"] += 1
                    warn_updates.append((new_w, row["id"], row.get("env_override")))
            _tally(report["by_source"], res.source)
            _tally(report["by_env"], res.env)
            tstate = ec.tag_state(row.get("resource_type"), row.get("tags"), _row_synced(row), _row_provider(row))
            same = (row.get("env_resolved") == res.env
                    and row.get("env_source") == res.source
                    and (row.get("env_reason") or "") == res.reason
                    and row.get("tag_state") == tstate)
            if same:
                continue
            report["changed"] += 1
            _tally(report["changed_by_source"], res.source)
            _tally(report["changed_by_env"], res.env)
            if res.env == ec.UNKNOWN and row.get("env_resolved") != ec.UNKNOWN:
                report["newly_unknown"] += 1
            updates.append((res.env, res.source, res.reason, tstate, row["id"], row.get("env_override")))
            if collect_changes:
                report["changes"].append({
                    "resource_id": row["resource_id"], "old_env": row.get("env_resolved"),
                    "old_source": row.get("env_source"), "new_env": res.env, "new_source": res.source})

        if dry_run or not (updates or warn_updates):
            return report

        # Ascending id: every writer that touches many resources rows takes their locks in the same order,
        # so two of them cannot wait on each other (metric_history inserts also lock the parent resources row).
        updates.sort(key=lambda u: u[4])
        wcur = conn.cursor()
        try:
            for i in range(0, len(updates), _CHUNK):
                chunk = updates[i:i + _CHUNK]
                # `env_override <=> %s`: only write if the manual choice is still what this pass read. A user may
                # have saved (or cleared) an override since the rows were loaded; the classify API re-resolves that
                # row itself, and a stale write here must not put the old answer back.
                wcur.executemany(
                    "UPDATE resources SET env_resolved = %s, env_source = %s, env_reason = %s, "
                    "tag_state = %s WHERE id = %s AND env_override <=> %s", chunk)
                conn.commit()
                n = getattr(wcur, "rowcount", None)
                report["applied"] += n if isinstance(n, int) and n >= 0 else len(chunk)
            # Warnings are a separate statement so the classification UPDATE above stays exactly as it was. Same
            # id order and the same guard: a manual choice saved since the read must not get a stale warning.
            warn_updates.sort(key=lambda u: u[1])
            for i in range(0, len(warn_updates), _CHUNK):
                chunk = warn_updates[i:i + _CHUNK]
                wcur.executemany("UPDATE resources SET env_warning = %s WHERE id = %s AND env_override <=> %s", chunk)
                conn.commit()
                n = getattr(wcur, "rowcount", None)
                report["warning_applied"] += n if isinstance(n, int) and n >= 0 else len(chunk)
        except Exception:
            conn.rollback()
            raise
        finally:
            wcur.close()
        return report
    finally:
        conn.close()


def sync_after_discovery():
    """Discovery-loop hook. Never raises: a classification problem must not
    disturb discovery. Skipped silently if migration 083 has not been applied."""
    try:
        rep = sync_resource_environments()
        if rep["applied"]:
            logger.info(f"[env-sync] classified {rep['applied']} of {rep['total']} resources "
                        f"(by source: {rep['by_source']})")
    except Exception as e:
        if getattr(e, "errno", None) == 1054:       # unknown column: 083 not applied yet
            logger.debug("[env-sync] migration 083 not applied yet -- skipping")
        else:
            logger.warning(f"[env-sync] failed (non-fatal): {e}")
        return
    # Workstream C, only after a clean sync: the daily compliance snapshot (ENV_COMPLIANCE_SNAPSHOT, default on, writes
    # a report table only) and the weekly digest (ENV_DIGEST_ENABLED, default OFF). Each is a no-op when its migration
    # is missing and neither can raise into discovery.
    try:
        from app.env_governance_db import snapshot_after_discovery
        snapshot_after_discovery()
    except Exception as e:
        logger.debug(f"[env-sync] compliance snapshot skipped: {e}")
    try:
        from app.env_digest import maybe_send_weekly_digest
        maybe_send_weekly_digest()
    except Exception as e:
        logger.debug(f"[env-sync] digest skipped: {e}")
