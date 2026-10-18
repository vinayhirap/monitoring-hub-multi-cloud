# app/collector/tag_sync.py
"""
Tag knowledge for resource types whose discovery stores no real AWS tags (workstream B).

One paginated Resource Groups Tagging API sweep (GetResources) per account and region per discovery cycle,
matched to `resources` rows by app/aws/arn_match.py (pure, tested). The result is stored in
resources.synced_tags / tags_synced_at (migration 095), NEVER in resources.tags: discovery rewrites `tags`
every run and the legacy alert evaluator reads tags["Environment"], so merging would change alerting.

Flag TAG_SYNC_ENABLED (default off). Off means: no AWS call, no query on the new columns, nothing changes.

What the sweep proves, and what it does not (see arn_match.py's docstring for the full rule):
  * a matched ARN proves the resource's tags;
  * absence is turned into "untagged" only for ABSENCE_VERIFIED_TYPES, only from a completed sweep that
    returned something, and only when the type's matching passes the canary;
  * any failure (AccessDenied, throttling, a page that did not arrive) discards that sweep entirely and
    leaves every stored value as it was. Stored values older than TAG_SYNC_MAX_AGE_HOURS (default 6) are
    ignored by readers, so a permanent failure degrades to not_checked instead of freezing a stale claim.

Never raises into the discovery loop.
"""
import json
import logging
import os
import time

from app import environment_classifier as ec
from app.aws import arn_match

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_HOURS = 6
REFRESH_MINUTES = 30              # re-stamp an unchanged result at most this often (keeps writes small)
DEFAULT_INTERVAL_MINUTES = 60     # how often the sweep runs (discovery itself runs every 15 minutes)
GLOBAL_FILTERS = ("cloudfront", "s3")      # ResourceTypeFilters for the us-east-1 sweep (global services only)
S3_FILTERS = ("s3",)
MAX_EXTRA_S3_REGIONS = 10
STALE_RESOURCE_HOURS = 48         # same freshness rule as env_sync
_CHUNK = 500
_ERROR_LOG_INTERVAL = 24 * 3600
_last_error_log = {}              # (account id, region, code) -> time of the last log line
_columns_ok = False
_last_run = 0.0


def enabled():
    return ec.tag_sync_enabled()


def max_age_hours():
    try:
        v = int(os.getenv("TAG_SYNC_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS)))
        return v if v > 0 else DEFAULT_MAX_AGE_HOURS
    except ValueError:
        return DEFAULT_MAX_AGE_HOURS


def direct_enabled():
    """TAG_SYNC_DIRECT_LOOKUP (needs TAG_SYNC_ENABLED, default off): for S3 buckets and WAFv2 ACLs the sweep could
    not decide, read their tags directly (GetBucketTagging / ListTagsForResource), one call per such resource per
    sweep. Gives an authoritative tagged / untagged answer where the Tagging-API index cannot."""
    return enabled() and os.getenv("TAG_SYNC_DIRECT_LOOKUP", "false").strip().lower() in ("true", "1", "yes")


def direct_max():
    """TAG_SYNC_DIRECT_MAX (default 100): most direct lookups per account per sweep."""
    try:
        v = int(os.getenv("TAG_SYNC_DIRECT_MAX", "100"))
        return v if v > 0 else 100
    except ValueError:
        return 100


def interval_minutes():
    """TAG_SYNC_INTERVAL_MINUTES (default 60, minimum 5): how often the discovery hook actually sweeps."""
    try:
        v = int(os.getenv("TAG_SYNC_INTERVAL_MINUTES", str(DEFAULT_INTERVAL_MINUTES)))
        return v if v >= 5 else DEFAULT_INTERVAL_MINUTES
    except ValueError:
        return DEFAULT_INTERVAL_MINUTES


def absence_types():
    """TAG_SYNC_ABSENCE_TYPES may only NARROW arn_match.ABSENCE_VERIFIED_TYPES: unset = all of them,
    'none' = no untagged claims at all, 'logs,kms' = just those. It can never add a type."""
    raw = os.getenv("TAG_SYNC_ABSENCE_TYPES", "").strip().lower()
    if not raw:
        return arn_match.ABSENCE_VERIFIED_TYPES
    if raw == "none":
        return frozenset()
    return arn_match.ABSENCE_VERIFIED_TYPES & {t.strip() for t in raw.split(",") if t.strip()}


def columns_ready(cursor):
    """True when migration 095 is applied. A positive answer is cached; a negative one is re-checked."""
    global _columns_ok
    if _columns_ok:
        return True
    cursor.execute("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema = DATABASE() "
                   "AND table_name = 'resources' AND column_name IN ('synced_tags', 'tags_synced_at')")
    row = cursor.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    _columns_ok = int(n) == 2
    return _columns_ok


def synced_select(cursor, alias="r"):
    """SQL select-list fragment giving the FRESH tag-sync result as `synced_tags` (NULL when the flag is off,
    the migration is missing, or the stored value is older than the max age). Safe to call whenever; adds
    nothing to the query unless the flag is on and migration 095 is applied."""
    if enabled():
        try:
            if columns_ready(cursor):
                return (f"IF({alias}.tags_synced_at >= DATE_SUB(NOW(), INTERVAL {int(max_age_hours())} HOUR), "
                        f"{alias}.synced_tags, NULL) AS synced_tags")
        except Exception as e:
            logger.debug(f"[tag-sync] column check failed, treating as off: {e}")
    return "NULL AS synced_tags"


# ---------------------------------------------------------------------------------------------
# AWS sweep
# ---------------------------------------------------------------------------------------------
def _read_pages(client, kwargs, out):
    items = []
    for page in client.get_paginator("get_resources").paginate(**kwargs):
        out["pages"] += 1
        for m in page.get("ResourceTagMappingList", []):
            arn = m.get("ResourceARN")
            if arn:
                items.append({"arn": arn, "tags": {t.get("Key"): t.get("Value") for t in m.get("Tags", [])
                                                     if t.get("Key")}})
    return items


def sweep_region(session, region, filters=None, scope="full"):
    """One paginated GetResources for `region`. -> {"region", "scope", "ok", "items", "pages", "error"}.
    `filters` (ResourceTypeFilters) is used only for the cheap global / S3 sweeps; if AWS rejects the filter the
    sweep is repeated unfiltered (arn_match then keeps only the types that scope may speak for).
    ANY other exception (AccessDenied, throttling after the client's own retries, network) makes the whole
    sweep ok=False and returns no items: a partial result must never be read as "the rest is untagged"."""
    out = {"region": region, "scope": scope, "ok": False, "items": [], "pages": 0, "error": None}
    try:
        from app.aws.boto_config import STANDARD_RETRY
        client = session.client("resourcegroupstaggingapi", region_name=region, config=STANDARD_RETRY)
        kwargs = {"ResourcesPerPage": 100}
        if filters:
            kwargs["ResourceTypeFilters"] = list(filters)
        try:
            items = _read_pages(client, kwargs, out)
        except Exception as e:
            if not (filters and _error_code(e) == "InvalidParameterException"):
                raise
            logger.warning(f"[tag-sync] {region}: ResourceTypeFilters rejected ({e}); repeating unfiltered")
            kwargs.pop("ResourceTypeFilters")
            out["pages"] = 0
            items = _read_pages(client, kwargs, out)
        out["items"] = items
        out["ok"] = True
    except Exception as e:
        out["error"] = e
        out["items"] = []
    return out


def _client_error_code(e):
    resp = getattr(e, "response", None)
    return ((resp or {}).get("Error") or {}).get("Code") if isinstance(resp, dict) else None


def _log_lookup_failure(account, what, e):
    code = _client_error_code(e) or type(e).__name__
    key = (account.get("id"), what, code)
    now = time.time()
    if now - _last_error_log.get(key, 0) < _ERROR_LOG_INTERVAL:
        return
    _last_error_log[key] = now
    logger.warning(f"[tag-sync] {account.get('account_name')}: direct {what} lookup failed ({code}: {e}); "
                   f"those resources stay undecided (logged once per day)")


def direct_lookup(session, account, account_number, rows):
    """Authoritative tags for S3 buckets and WAFv2 ACLs, one call each. -> ({row id: tags dict | None}, calls).
    A dict is a proven answer (S3 'NoSuchTagSet' and an empty WAF tag list both mean {}); None means unknown --
    any other error (AccessDenied, wrong region redirect, throttling after retries, a deleted resource). Never raises."""
    from app.aws.boto_config import STANDARD_RETRY
    results, calls, clients = {}, 0, {}

    def client(service, region):
        k = (service, region)
        if k not in clients:
            clients[k] = session.client(service, region_name=region, config=STANDARD_RETRY)
        return clients[k]

    for r in rows:
        rid, rtype = r["id"], str(r.get("resource_type") or "").lower()
        try:
            if rtype == "s3":
                region = str(r.get("region") or "")
                if not region or region == "global" or not r.get("resource_id"):
                    results[rid] = None
                    continue
                calls += 1
                try:
                    resp = client("s3", region).get_bucket_tagging(Bucket=str(r["resource_id"]))
                    results[rid] = {t["Key"]: t.get("Value", "") for t in resp.get("TagSet", []) if t.get("Key")}
                except Exception as e:
                    if _client_error_code(e) == "NoSuchTagSet":
                        results[rid] = {}
                    else:
                        results[rid] = None
                        _log_lookup_failure(account, "S3 GetBucketTagging", e)
            elif rtype == "wafv2":
                arn = arn_match.waf_acl_arn(r, account_number)
                if arn is None:
                    results[rid] = None
                    continue
                calls += 1
                try:
                    resp = client("wafv2", str(r.get("region"))).list_tags_for_resource(ResourceARN=arn)
                    tag_list = (resp.get("TagInfoForResource") or {}).get("TagList") or []
                    results[rid] = {t["Key"]: t.get("Value", "") for t in tag_list if t.get("Key")}
                except Exception as e:
                    results[rid] = None
                    _log_lookup_failure(account, "WAFv2 ListTagsForResource", e)
        except Exception as e:                     # belt and braces: one resource can never break the sync
            results[rid] = None
            _log_lookup_failure(account, "direct", e)
    return results, calls


def _error_code(e):
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        return (resp.get("Error") or {}).get("Code") or type(e).__name__
    return type(e).__name__


def _log_sweep_failure(account, region, e):
    code = _error_code(e)
    key = (account.get("id"), region, code)
    now = time.time()
    if now - _last_error_log.get(key, 0) < _ERROR_LOG_INTERVAL:
        return
    _last_error_log[key] = now
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        logger.warning(f"[tag-sync] {account.get('account_name')}/{region}: {code} on tag:GetResources -- tag status "
                       f"stays as it was (not_checked) for the extra types. Grant tag:GetResources to the monitoring role.")
    else:
        logger.warning(f"[tag-sync] {account.get('account_name')}/{region}: sweep failed ({code}: {e}); "
                       f"stored tag status left untouched (logged once per day)")


def sweep_plan(account, rows):
    """What to sweep for one account -> [{"region", "scope", "filters"}].
      * the discovery region, unfiltered ("full"): proves tags for every type and can support "untagged";
      * us-east-1, filtered to CloudFront and S3 ("global"), when the account has such rows;
      * each other region that holds an S3 bucket row, filtered to S3 ("s3"), so buckets outside the discovery
        region are found too (at most MAX_EXTRA_S3_REGIONS).
    Filtered sweeps never support an "untagged" claim (see arn_match.SCOPE_TYPES)."""
    plan, covered = [], set()
    default = account.get("default_region")
    if default:
        plan.append({"region": default, "scope": "full", "filters": None})
        covered.add(default)
    types = {str(r.get("resource_type")).lower() for r in rows}
    if types & arn_match.GLOBAL_TYPES and "us-east-1" not in covered:
        plan.append({"region": "us-east-1", "scope": "global", "filters": list(GLOBAL_FILTERS)})
        covered.add("us-east-1")
    s3_regions = sorted({str(r.get("region")) for r in rows
                         if str(r.get("resource_type")).lower() == "s3" and r.get("region")
                         and str(r.get("region")) not in ("global",)} - covered)
    for region in s3_regions[:MAX_EXTRA_S3_REGIONS]:
        plan.append({"region": region, "scope": "s3", "filters": list(S3_FILTERS)})
    return plan


# ---------------------------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------------------------
_ROWS_SQL = """
    SELECT r.id, r.resource_type, r.resource_id, r.region, r.tags,
           r.synced_tags AS stored_synced,
           TIMESTAMPDIFF(MINUTE, r.tags_synced_at, NOW()) AS synced_age_min
    FROM resources r
    WHERE r.aws_account_id = %s
      AND (r.instance_state IS NULL OR r.instance_state != 'terminated')
      AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR))
      AND r.resource_type IN ({types})
"""


def load_rows(cursor, account_pk):
    types = sorted(arn_match.SYNC_TYPES)
    cursor.execute(_ROWS_SQL.format(types=", ".join(["%s"] * len(types))),
                   [account_pk, STALE_RESOURCE_HOURS, *types])
    return cursor.fetchall()


def _canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def writes_for(rows, decisions):
    """-> [(synced_json, row id)] for decisions whose stored value changed or is due a refresh."""
    out = []
    for r in rows:
        d = decisions.get(r["id"])
        if d is None:
            continue
        new = _canon(d.tags)
        old = ec.parse_synced(r.get("stored_synced"))
        age = r.get("synced_age_min")
        if old is not None and _canon(old) == new and age is not None and age < REFRESH_MINUTES:
            continue
        out.append((new, r["id"]))
    return out


def _get_accounts():
    from app.collector.discovery.runner import _get_active_accounts
    return _get_active_accounts()


def _get_session(account):
    from app.collector.discovery.runner import _get_session
    return _get_session(account)


def run_tag_sync(account_ids=None, dry_run=False, sweeper=None, looker=None):
    """Sweep, match, store. Returns a report dict; never raises.
        {"accounts": {account name: {"regions": {...}, "types": {...}, "written": n}}, "calls": pages,
         "written": n, "skipped": reason|None}
    `sweeper(account, spec) -> sweep dict` replaces the AWS call (tests); spec is a sweep_plan() entry.
    `looker(account, rows) -> ({row id: tags | None}, calls)` replaces direct_lookup (tests)."""
    from app.db import get_connection
    report = {"accounts": {}, "calls": 0, "written": 0, "skipped": None, "dry_run": bool(dry_run)}
    try:
        accounts = [a for a in _get_accounts() if account_ids is None or a["id"] in account_ids]
    except Exception as e:
        report["skipped"] = f"could not list accounts: {e}"
        return report

    for account in accounts:
        number = str(account.get("account_id") or "").strip()
        name = account.get("account_name") or account["id"]
        if not number:
            report["accounts"][name] = {"skipped": "no AWS account number on the account row"}
            continue
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            try:
                if not columns_ready(cur):
                    report["skipped"] = "migration 095 not applied"
                    logger.debug("[tag-sync] migration 095 not applied yet -- skipping")
                    return report
                rows = load_rows(cur, account["id"])
            finally:
                cur.close()
            conn.commit()
            if not rows:
                report["accounts"][name] = {"regions": {}, "types": {}, "written": 0}
                continue

            need_session = sweeper is None or (direct_enabled() and looker is None)
            session = _get_session(account) if need_session else None
            if need_session and session is None:
                report["accounts"][name] = {"skipped": "no session"}
                continue
            sweeps = []
            for spec in sweep_plan(account, rows):
                sw = (sweeper(account, spec) if sweeper
                      else sweep_region(session, spec["region"], spec["filters"], spec["scope"]))
                sw.setdefault("scope", spec["scope"])
                report["calls"] += sw.get("pages", 0) or 0
                if not sw.get("ok"):
                    _log_sweep_failure(account, spec["region"], sw.get("error") or RuntimeError("sweep failed"))
                sweeps.append(sw)

            plan = arn_match.plan_account(rows, sweeps, number, absence_types())
            direct_calls = 0
            if direct_enabled():
                cands = arn_match.direct_candidates(rows, plan, direct_max())
                if cands:
                    try:
                        results, direct_calls = (looker(account, cands) if looker
                                                 else direct_lookup(session, account, number, cands))
                        arn_match.merge_direct(plan, rows, results)
                    except Exception as e:
                        logger.warning(f"[tag-sync] {name}: direct lookups failed (non-fatal): {e}")
                    report["calls"] += direct_calls
            writes = writes_for(rows, plan["decisions"])
            written = 0
            if writes and not dry_run:
                writes.sort(key=lambda w: w[1])          # ascending id, same lock order as env_sync
                wcur = conn.cursor()
                try:
                    for i in range(0, len(writes), _CHUNK):
                        wcur.executemany("UPDATE resources SET synced_tags = %s, tags_synced_at = NOW() WHERE id = %s",
                                         writes[i:i + _CHUNK])
                        conn.commit()
                        written += len(writes[i:i + _CHUNK])
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    wcur.close()
            report["written"] += written
            report["accounts"][name] = {
                "regions": {f"{s['region']}/{s.get('scope', 'full')}": {
                    "ok": bool(s.get("ok")), "resources": len(s.get("items") or []), "pages": s.get("pages", 0)}
                    for s in sweeps},
                "types": plan["types"], "written": written, "direct_calls": direct_calls,
            }
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            report["accounts"][name] = {"error": str(e)}
            logger.warning(f"[tag-sync] {name} failed (non-fatal): {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if report["calls"]:
        try:
            from app.collector import api_usage
            api_usage.record("aws", "tag_sync", calls=report["calls"], units=0)
        except Exception:
            pass
    return report


def sync_after_discovery():
    """Discovery-loop hook: runs BEFORE env_sync.sync_after_discovery so the classification pass sees the fresh
    result. Does nothing, and makes no AWS call, unless TAG_SYNC_ENABLED is on. Sweeps at most once per
    TAG_SYNC_INTERVAL_MINUTES (default 60; tags change rarely, discovery runs every 15 minutes). Never raises."""
    global _last_run
    if not enabled():
        return
    now = time.time()
    if _last_run and now - _last_run < interval_minutes() * 60:
        return
    _last_run = now          # a failed attempt also waits for the next interval; failures are logged once a day
    try:
        rep = run_tag_sync()
        if rep.get("written"):
            logger.info(f"[tag-sync] stored tag results for {rep['written']} resources in {rep['calls']} API call(s)")
    except Exception as e:
        logger.warning(f"[tag-sync] failed (non-fatal): {e}")
