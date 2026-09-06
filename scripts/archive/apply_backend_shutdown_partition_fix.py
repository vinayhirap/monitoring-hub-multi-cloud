#!/usr/bin/env python3
"""
apply_backend_shutdown_partition_fix.py

Fixes two real backend bugs surfaced in the uvicorn startup log:

1. "Partition error: 1505 (HY000): Partition management on a not
   partitioned table is not possible" — logged every 15-min cycle.

   Root cause: db/migrations/004_metrics_last_value_only.sql deliberately
   converted `metrics` into a last-value-only cache (one row per
   resource+metric, upserted via a UNIQUE key) and REMOVED partitioning
   on purpose — there's no more history to prune by date. But
   app/collector/scheduler.py's _ensure_next_partition() is leftover code
   from before that migration; it still tries to add a monthly partition
   to a table that intentionally has none anymore. This isn't a broken
   table — it's dead code. Fix: remove the function and its scheduling.

2. "Task was destroyed but it is pending!" + RuntimeWarning: coroutine
   'PubSub.execute_command'/'Redis.aclose' was never awaited — logged on
   every --reload restart (and real shutdown).

   Root cause: app/main.py's lifespan() starts the redis listener with
   asyncio.create_task(...) but its shutdown phase never stops or awaits
   it — it only logs "Shutting down". app/ws/pusher.py already exposes a
   stop_listener() for exactly this, but nothing ever calls it, so the
   event loop closes while the task is still blocked inside
   pubsub.listen(), forcibly killing it mid-cleanup. Fix: capture the
   task, call stop_listener(), then cancel + await it with a timeout so
   cleanup finishes before the loop closes.

Run from the project root:
    python apply_backend_shutdown_partition_fix.py

Safe to re-run (detects it's already applied). Validates every anchor in
both files before writing anything — a failed match on one file never
leaves the other half-patched. Backs up originals to
*.bak.pre-shutdown-partition-fix.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

MAIN_PATH = REPO_ROOT / "app" / "main.py"
SCHEDULER_PATH = REPO_ROOT / "app" / "collector" / "scheduler.py"


def die(msg):
    print(f"\n[ABORTED] {msg}")
    print("No files were modified.")
    sys.exit(1)


def load(path: Path) -> str:
    if not path.exists():
        die(f"Expected file not found: {path}\n"
            f"Run this script from the project root (monitoring-hub-V4-cost-optimized).")
    return path.read_text(encoding="utf-8")


def require_one(text: str, needle: str, filename: str):
    count = text.count(needle)
    if count == 0:
        die(f"Anchor text not found in {filename} — the file has likely drifted "
            f"since this script was written. Aborting without changes.\n"
            f"--- missing anchor ---\n{needle}")
    if count > 1:
        die(f"Anchor text found {count} times in {filename} (expected exactly once) — "
            f"refusing to guess which one to patch.\n"
            f"--- ambiguous anchor ---\n{needle}")


# ── app/main.py edits ────────────────────────────────────────────────────

MAIN_OLD_IMPORT = "from app.ws.pusher  import redis_listener"
MAIN_NEW_IMPORT = "from app.ws.pusher  import redis_listener, stop_listener"

MAIN_OLD_LIFESPAN = '''@asynccontextmanager
async def lifespan(app):
    # ── Startup ───────────────────────────────────────────────
    threading.Thread(target=_run_collector, daemon=True, name="collector").start()
    threading.Thread(target=_run_describe_poll_loop, daemon=True, name="describe-poll").start()
    asyncio.create_task(_safe_redis_listener())
    logger.info("Startup complete — collector running, Redis listener started")
    yield
    # ── Shutdown — daemon thread dies automatically ───────────
    logger.info("Shutting down")'''

MAIN_NEW_LIFESPAN = '''@asynccontextmanager
async def lifespan(app):
    # ── Startup ───────────────────────────────────────────────
    threading.Thread(target=_run_collector, daemon=True, name="collector").start()
    threading.Thread(target=_run_describe_poll_loop, daemon=True, name="describe-poll").start()
    redis_task = asyncio.create_task(_safe_redis_listener())
    logger.info("Startup complete — collector running, Redis listener started")
    yield
    # ── Shutdown ────────────────────────────────────────────────
    logger.info("Shutting down")
    stop_listener()
    redis_task.cancel()
    try:
        await asyncio.wait_for(redis_task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    logger.info("Redis listener stopped cleanly")'''

MAIN_ALREADY_APPLIED_MARKER = "Redis listener stopped cleanly"


# ── app/collector/scheduler.py edits ─────────────────────────────────────

SCHED_OLD_CONST = '''DISCOVERY_INTERVAL = 900    # 15 min — aligned with low tier
PARTITION_INTERVAL = 86400  # daily'''
SCHED_NEW_CONST = '''DISCOVERY_INTERVAL = 900    # 15 min — aligned with low tier'''

SCHED_OLD_FUNC = '''def _ensure_next_partition():
    try:
        from datetime import date

        conn   = get_connection()
        cursor = conn.cursor()

        today = date.today()
        if today.month == 12:
            next_year, next_month = today.year + 1, 1
        else:
            next_year, next_month = today.year, today.month + 1

        if next_month == 12:
            bound_year, bound_month = next_year + 1, 1
        else:
            bound_year, bound_month = next_year, next_month + 1

        partition_name = f"p{next_year}_{next_month:02d}"
        bound_date     = f"{bound_year}-{bound_month:02d}-01"

        cursor.execute("""
            SELECT PARTITION_NAME FROM information_schema.PARTITIONS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'metrics'
              AND PARTITION_NAME = %s
        """, (partition_name,))

        if not cursor.fetchone():
            cursor.execute(f"""
                ALTER TABLE metrics REORGANIZE PARTITION p_future INTO (
                    PARTITION {partition_name} VALUES LESS THAN (TO_DAYS('{bound_date}')),
                    PARTITION p_future VALUES LESS THAN MAXVALUE
                )
            """)
            conn.commit()
            logger.info(f"Added partition: {partition_name}")

        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Partition error: {e}")


def run_once(tier="standard"):'''
SCHED_NEW_FUNC = '''def run_once(tier="standard"):'''

SCHED_OLD_VARS = '''    last_standard   = 0
    last_low        = 0
    last_discovery  = 0
    last_partition  = 0
    cycle           = 0'''
SCHED_NEW_VARS = '''    last_standard   = 0
    last_low        = 0
    last_discovery  = 0
    cycle           = 0'''

SCHED_OLD_LOOP_BLOCK = '''        if now - last_partition >= PARTITION_INTERVAL:
            try:
                _ensure_next_partition()
                last_partition = now
            except Exception as e:
                logger.error(f"Partition error: {e}")

        # Sleep until next critical cycle'''
SCHED_NEW_LOOP_BLOCK = '''        # Sleep until next critical cycle'''

SCHED_ALREADY_APPLIED_MARKER_ABSENT = "_ensure_next_partition"  # should be ABSENT once patched


def main():
    print(f"Project root: {REPO_ROOT}\n")

    main_text = load(MAIN_PATH)
    sched_text = load(SCHEDULER_PATH)

    already_main = MAIN_ALREADY_APPLIED_MARKER in main_text
    already_sched = SCHED_ALREADY_APPLIED_MARKER_ABSENT not in sched_text

    if already_main and already_sched:
        print("Already applied — both files already contain the fix. Nothing to do.")
        return

    # ---- validate every anchor BEFORE writing anything ----
    require_one(main_text, MAIN_OLD_IMPORT, "app/main.py")
    require_one(main_text, MAIN_OLD_LIFESPAN, "app/main.py")

    require_one(sched_text, SCHED_OLD_CONST, "app/collector/scheduler.py")
    require_one(sched_text, SCHED_OLD_FUNC, "app/collector/scheduler.py")
    require_one(sched_text, SCHED_OLD_VARS, "app/collector/scheduler.py")
    require_one(sched_text, SCHED_OLD_LOOP_BLOCK, "app/collector/scheduler.py")

    # ---- all anchors verified — safe to apply ----
    new_main_text = main_text.replace(MAIN_OLD_IMPORT, MAIN_NEW_IMPORT, 1)
    new_main_text = new_main_text.replace(MAIN_OLD_LIFESPAN, MAIN_NEW_LIFESPAN, 1)

    new_sched_text = sched_text.replace(SCHED_OLD_CONST, SCHED_NEW_CONST, 1)
    new_sched_text = new_sched_text.replace(SCHED_OLD_FUNC, SCHED_NEW_FUNC, 1)
    new_sched_text = new_sched_text.replace(SCHED_OLD_VARS, SCHED_NEW_VARS, 1)
    new_sched_text = new_sched_text.replace(SCHED_OLD_LOOP_BLOCK, SCHED_NEW_LOOP_BLOCK, 1)

    # ---- backup originals, then write ----
    main_backup = MAIN_PATH.with_suffix(MAIN_PATH.suffix + ".bak.pre-shutdown-partition-fix")
    sched_backup = SCHEDULER_PATH.with_suffix(SCHEDULER_PATH.suffix + ".bak.pre-shutdown-partition-fix")

    if not main_backup.exists():
        main_backup.write_text(main_text, encoding="utf-8")
    if not sched_backup.exists():
        sched_backup.write_text(sched_text, encoding="utf-8")

    MAIN_PATH.write_text(new_main_text, encoding="utf-8")
    SCHEDULER_PATH.write_text(new_sched_text, encoding="utf-8")

    print("Patched:")
    print(f"  {MAIN_PATH.relative_to(REPO_ROOT)}")
    print(f"  {SCHEDULER_PATH.relative_to(REPO_ROOT)}")
    print(f"\nBackups saved as *.bak.pre-shutdown-partition-fix next to each file.")
    print("\nNext steps:")
    print("  1. Restart uvicorn (or let --reload pick up the change)")
    print("  2. Confirm the 'Partition error' line no longer appears on the 15-min cycle")
    print("  3. Confirm no 'Task was destroyed but it is pending!' warning on the next reload/Ctrl+C")
    print("  4. git add app/main.py app/collector/scheduler.py")
    print("     git commit -m 'fix: remove dead partition code, clean up redis listener shutdown'")
    print("     git push origin main")


if __name__ == "__main__":
    main()
