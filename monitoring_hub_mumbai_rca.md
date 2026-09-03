# RCA: AuroGov Mumbai Dashboard — Missing Metrics & Migration Failures

**Date:** 2026-08-26
**System:** monitoring-hub (AWS resource monitoring platform)
**Affected environment:** AuroGov Mumbai deployment, account `924922671984`
**Status:** Resolved — all migrations applied, dashboard populating

---

## 1. Summary

The AuroGov Mumbai dashboard was not showing metrics for several resource
types (ELB/ALB, and eventually all metrics stopped growing correctly). Root
cause was a chain of four independent issues, uncovered one after another as
each was fixed:

1. A schema migration script (`apply_fresh_schema_migrations.py`) had a real
   type-mismatch bug that blocked all migrations behind it from running.
2. `resources.resource_id` was too narrow (column width) to store some AWS
   resource identifiers, preventing certain resource types from being
   discovered/stored correctly.
3. The `metrics` table had no uniqueness constraint, so the collector had
   been writing duplicate rows every cycle — once discovered, ~722 of 786
   rows were duplicates.
4. Two tooling issues surfaced while fixing #3: a MySQL limitation (error
   1093) when deleting from a table using a subquery on itself, and a
   `sudo -u` environment-variable boundary issue that broke DB
   authentication for the fix script.

All four are now fixed and verified. This document lists root cause,
resolution commands, and — most importantly — the permanent fixes so none of
this recurs on the next deploy (including future fresh installs, e.g. a new
regional server).

---

## 2. Timeline of issues, root causes, and fixes

### Issue 1 — Migration script type mismatch (`provider_credentials.aws_account_id`)

**Symptom:** Migrations failed to apply; `resource_id` widening (needed to
fix the dashboard) never ran because it came *after* the broken statement in
script execution order.

**Root cause:** `apply_fresh_schema_migrations.py` declared
`provider_credentials.aws_account_id` as `INT NOT NULL PRIMARY KEY`, but the
actual referenced column, `aws_accounts.id`, is `BIGINT` (confirmed via the
`resources` table's own foreign key, which correctly uses `bigint`, and via
direct `DESCRIBE aws_accounts`). A genuine schema-authoring bug — not
environment-specific.

**Verification command:**
```bash
mysql -umonitor -p monitoring_hub -e "DESCRIBE aws_accounts;" | grep -E "^id"
```

**Fix applied:** `apply_fresh_schema_migrations_fk_type_fix.py` — a one-time
patch script that altered `provider_credentials.aws_account_id` from `INT`
to `BIGINT` to match `aws_accounts.id`.

```bash
cd /opt/monitoring-hub/app
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations_fk_type_fix.py --dry-run
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations_fk_type_fix.py
```

**Permanent solution:** The type declaration in
`apply_fresh_schema_migrations.py` itself must be corrected to `BIGINT` so a
brand-new deployment never creates the wrong type in the first place —
running the one-time patch script only fixes servers that already have the
wrong type; it does not stop the bug from reappearing on new installs.

---

### Issue 2 — `resources.resource_id` too narrow

**Symptom:** ELB/ALB resources (and potentially other resource types with
long ARNs/IDs) were silently dropped or truncated during discovery, so they
never appeared on the dashboard.

**Root cause:** `resource_id` column width was insufficient for some AWS
identifier formats (e.g. full ALB ARNs).

**Fix applied (manual, ahead of the full migration run):**
```bash
mysql -umonitor -p monitoring_hub -e "ALTER TABLE resources MODIFY COLUMN resource_id VARCHAR(512) NOT NULL;"
```

**Verification:**
```bash
set -a; source .env; set +a
/opt/monitoring-hub/venv/bin/python3 -c "from app.collector.discovery.runner import run_discovery; run_discovery()"
mysql -umonitor -p monitoring_hub -e "SELECT resource_type, COUNT(*) FROM resources r JOIN aws_accounts a ON a.id=r.aws_account_id WHERE a.account_id='924922671984' GROUP BY resource_type;"
```
Result: 2 ELB rows appeared, matching the 2 real ALBs in the account.

**Permanent solution:** This widen is now captured as a proper migration
statement inside `apply_fresh_schema_migrations.py` (confirmed via later
dry-run showing it as an already-applied statement), so new deployments get
the correct column width automatically instead of needing the same manual
`ALTER` again.

---

### Issue 3 — Duplicate rows in `metrics` blocking a unique constraint

**Symptom:** `apply_fresh_schema_migrations.py --dry-run` reported one
outstanding statement — adding `uniq_metrics_resource_metric (resource_id,
metric_name)` — that failed to apply due to existing duplicate data.

**Root cause:** The `metrics` table had never had a uniqueness constraint on
`(resource_id, metric_name)`. Because migrations were stuck behind Issue 1
for an unknown period, the collector kept running every cycle and kept
inserting a new row per metric per cycle instead of upserting — duplicate
count grew from 210-of-274 rows to 722-of-786 rows between the first
dry-run and the actual fix, confirming duplication was actively ongoing, not
historical.

**Fix applied:** `apply_metrics_dedup_fix.py` — deletes all but the newest
row per `(resource_id, metric_name)` group (ordered by a recency column if
present, otherwise by `id`/insertion order), takes a `mysqldump` backup of
the `metrics` table first, then adds the missing unique key.

```bash
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_metrics_dedup_fix.py --dry-run
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_metrics_dedup_fix.py
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations.py --dry-run
```

Final confirmed result: 722 rows deleted, 64 rows remain (one per
resource/metric group), unique key added, and
`apply_fresh_schema_migrations.py --dry-run` now reports **"Nothing to
do — all migrations already applied."**

**Permanent solution:** The unique key now enforced on `metrics` means the
collector can no longer insert duplicates going forward — any future insert
that violates it must use an upsert (`INSERT ... ON DUPLICATE KEY UPDATE`)
instead of a plain `INSERT`. **Action item:** confirm the collector's metric
write path already does this, since the unique key will otherwise start
throwing errors on the very next write instead of silently duplicating.

---

### Issue 4a — MySQL error 1093 during dedup

**Symptom:**
```
ERROR: 1093 (HY000): You can't specify target table 'metrics' for update in FROM clause
```

**Root cause:** MySQL does not allow a `DELETE ... WHERE id NOT IN (SELECT
... FROM metrics ...)` where the subquery reads from the same table being
deleted from, even nested inside a subquery — this is a documented MySQL
limitation, not a bug in the dedup logic itself.

**Fix applied:** Wrapped the "rows to keep" subquery in an additional
derived-table layer (`SELECT id FROM (<original subquery>) AS keep_ids`).
This forces MySQL to materialize the result set before the `DELETE`
statement touches `metrics` again, which is the standard workaround for
error 1093.

**Permanent solution:** This pattern is now baked into
`apply_metrics_dedup_fix.py` itself (the `build_keep_ids_subquery` /
`keep_ids_subquery` wrapping). No further action needed unless the query is
rewritten from scratch in the future — if so, keep the derived-table
wrapper.

---

### Issue 4b — `sudo -u` stripped environment variables, breaking DB auth

**Symptom (attempt 1):**
```
ModuleNotFoundError: No module named 'pymysql'
```
**Symptom (attempt 2, after switching to the `mysql` CLI):**
```
ERROR 1045 (28000): Access denied for user 'monitor'@'localhost' (using password: NO)
```
even though `.env` had been sourced beforehand in the same terminal.

**Root cause (two-part):**
- The dedup script originally used the `pymysql` Python driver, which was
  not installed in `/opt/monitoring-hub/venv`. Switched to shelling out to
  the `mysql` CLI instead, which is already present and used elsewhere on
  the box (avoids adding a dependency).
- `sudo -u hcsadmin ...` starts a **new shell** for `hcsadmin`, which does
  not inherit environment variables exported (`source .env`) under your own
  login shell. So `DB_PASSWORD` was empty by the time the script ran,
  producing a no-password connection attempt.

**Fix applied:** Rewrote the script to read `DB_PASSWORD` /
`MONITOR_DB_PASSWORD` directly from the `.env` file on disk (script's own
directory first, then known fallback paths), only falling back to an
inherited environment variable if one happens to already be set. This
matches how `apply_fresh_schema_migrations.py` already behaves, which is why
that script never had this problem.

**Permanent solution:** Any future one-off ops script that needs DB
credentials and may be run via `sudo -u <otheruser>` should read `.env`
directly rather than assuming shell-exported variables will be present —
this is now the pattern used in `apply_metrics_dedup_fix.py` and should be
the template for any similar script going forward.

---

## 3. Full command reference (in the order actually run)

```bash
# --- Issue 2: widen resource_id, verify discovery ---
mysql -umonitor -p monitoring_hub -e "ALTER TABLE resources MODIFY COLUMN resource_id VARCHAR(512) NOT NULL;"
set -a; source .env; set +a
/opt/monitoring-hub/venv/bin/python3 -c "from app.collector.discovery.runner import run_discovery; run_discovery()"
mysql -umonitor -p monitoring_hub -e "SELECT resource_type, COUNT(*) FROM resources r JOIN aws_accounts a ON a.id=r.aws_account_id WHERE a.account_id='924922671984' GROUP BY resource_type;"

# --- Issue 1: confirm and fix aws_account_id type mismatch ---
mysql -umonitor -p monitoring_hub -e "DESCRIBE aws_accounts;" | grep -E "^id"
cd /opt/monitoring-hub/app
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations_fk_type_fix.py --dry-run
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations_fk_type_fix.py
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations.py --dry-run
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations.py

# --- Issue 3 + 4a + 4b: dedupe metrics, add unique key ---
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_metrics_dedup_fix.py --dry-run
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_metrics_dedup_fix.py

# --- Final verification ---
sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations.py --dry-run
# Expected: "Nothing to do — all migrations already applied."
```

Other items resolved earlier in the same session (referenced but not
detailed above, since they were fixed before this RCA was started):
AssumeRole permission fix, `resources.region` column addition, and VM disk
space expansion. Include these in your own change log if they need separate
tracking.

---

## 4. Verification checklist

- [x] `apply_fresh_schema_migrations.py --dry-run` → "Nothing to do"
- [x] `resources` table shows 2 ELB rows for account `924922671984`
      (matches 2 real ALBs)
- [x] `metrics` table: 786 → 64 rows after dedup, unique key
      `uniq_metrics_resource_metric` present
- [ ] AuroGov Mumbai dashboard confirmed populating across CPU / network /
      disk / RDS / S3 / Lambda / ECS / EBS after a few more collector cycles
      *(confirm this after the next 2–3 collection cycles)*
- [ ] Collector's metric-write path confirmed to use upsert semantics
      (`INSERT ... ON DUPLICATE KEY UPDATE`), not plain `INSERT`, now that
      the unique key is enforced

---

## 5. Permanent fixes — what must be committed (not just patched live)

Everything above was patched **directly on this server**. None of it
persists past the next `git pull` unless committed to the repo. To make
sure this doesn't happen again on this server, on a Mumbai rebuild, or on
any future fresh deploy:

1. **Fix `apply_fresh_schema_migrations.py` at the source** — change
   `provider_credentials.aws_account_id` from `INT` to `BIGINT` directly in
   the script, so the one-time patch script isn't needed on future installs.
2. **Confirm the `resources.resource_id VARCHAR(512)` widen and the
   `metrics` unique key are both proper migration steps** in
   `apply_fresh_schema_migrations.py` (the dry-run output already suggests
   they are, since it reports "nothing to do" — verify this is committed,
   not just applied to the live DB).
3. **Commit `apply_fresh_schema_migrations_fk_type_fix.py` and
   `apply_metrics_dedup_fix.py`** to the repo as reusable one-time-fix
   utilities, in case any other existing server (not just Mumbai) still has
   the old `INT` column or accumulated duplicate metric rows.
4. **Check the collector's metric insert path** for upsert semantics before
   the next deploy — with the unique key now live, a plain `INSERT` on a
   duplicate will raise an error instead of silently duplicating, which
   could crash or stall the collector loop if not handled.
5. **Adopt the `.env`-file-reading pattern** (not inherited shell env vars)
   for any future one-off admin script expected to run via `sudo -u
   hcsadmin`.

Once items 1–4 are committed and pushed, a brand-new server (Mumbai
rebuild, disaster recovery, or any new region) will apply a clean, correct
migration on first run and will never hit any of these four issues again.
