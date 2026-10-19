"""Backfill alerts.priority for alerts that are already open (phase 4b). Dry run unless apply=True.
Uses the same function the evaluator uses, so the backfill and the live cycle can never disagree."""
from app import priority as _priority


def run(cursor, apply=False, use_resolved_env=False):
    """-> the refresh_open_alert_priorities() result. Caller commits when apply=True."""
    return _priority.refresh_open_alert_priorities(cursor, use_resolved_env=use_resolved_env, apply=apply)
