# Alert priority P1-P4 (phase 4b)

Patch 1 (this document's scope): **show priority**. Patch 2 (escalation by priority and channel routing) is
described at the bottom and is not built yet.

## The rule

| Environment | Critical alert | Warning / info alert |
|---|---|---|
| prod | P1 | P2 |
| uat | P2 | P3 |
| dev | P3 | P4 |

* An environment that is not prod, uat or dev (unknown, "ignore", or a free-text label) is ranked like prod, so an
  unclassified resource never gets a weaker priority.
* A resource tagged `Criticality=critical` moves up one level, never above P1.
* The environment is the one CloudOps resolved for the resource (override > tag > name > account default) when
  `ENV_AWARE_CLASSIFICATION` is on, otherwise the label already stored on the alert.
* Priority is recomputed for every open alert each evaluation cycle, so marking a resource Prod/UAT/Dev in
  Environments moves its open alerts on the next cycle. Resolved alerts keep their last value.

## Flag

| Flag | Default | Effect |
|---|---|---|
| `PRIORITY_ENABLED` | off | Computes and stores `alerts.priority`. Shows P-chips, the "Firing by priority" strip (Alerts and Overview), click-to-filter on the Alerts page, and P1-first ordering. **Changes no severity, escalation, notification, silence or auto-resolve.** |

With the flag off nothing reads or writes the column, the API returns no `priority` field, and the UI shows nothing.

## Deploy and turn on (dev first, then prod)

1. Deploy the patch and apply migration `101_alert_priority.sql` (adds `alerts.priority` and an index; idempotent;
   changes no behaviour). Rebuild the frontend and restart.
2. Dry run: `scripts/backfill_alert_priority.py` (writes nothing). Review the counts.
3. `scripts/backfill_alert_priority.py --apply`, then set `PRIORITY_ENABLED=true` in `.env` and restart.
4. Check: `SELECT priority, COUNT(*) FROM alerts WHERE status IN ('active','acknowledged') GROUP BY priority;`
   (no NULLs after the next cycle) and the Alerts page.

Rollback: remove the flag and restart. The column stays and is ignored.

## Not in this patch (patch 2, flag `PRIORITY_ROUTING_ENABLED`)

Priority escalation policies (a priority policy replaces the severity policy for the same alert), per-channel
"send P-level and above", and an hourly digest for lower priorities. Delivery of P3/P4 will be a per-channel setting
that stays off until an admin chooses.
