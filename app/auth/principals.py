# app/auth/principals.py
"""
Shared lifecycle helpers for the two kinds of RBAC principal (users and
org groups), so users.py / groups.py stop each carrying their own
half-copy of "what has to happen when one goes away".

WHY THIS EXISTS (final access-control audit)

1. Deleting a user used to 500. Eight tables record WHO did something
   with `... REFERENCES users(id) ON DELETE RESTRICT`:
       access_scopes.granted_by        group_policies.granted_by
       org_groups.created_by           user_group_memberships.assigned_by
       rbac_scopes.created_by          role_bindings.granted_by
       permission_overrides.granted_by access_reviews.reviewed_by
   so any admin who had ever created a group, granted a scope or added
   someone to a group could never be deleted -- MySQL refused with an FK
   error that surfaced as a raw 500. `reassign_authorship()` hands those
   rows to the administrator performing the delete, which keeps the
   history intact ("granted by <someone>") without blocking the delete.

2. Bindings / overrides / reviews point at a principal through a
   polymorphic (principal_type, principal_id) with no FK, so deleting a
   user or group left live grants behind. `purge_principal_grants()`
   removes them in the same transaction as the delete.

Everything here takes an open connection and does NOT commit -- the
caller owns the transaction, so the delete and its cleanup are atomic.
"""
from typing import Optional

# (table, column) pairs that record the acting user and FK to users(id).
_AUTHORSHIP_COLUMNS = (
    ("access_scopes", "granted_by"),
    ("org_groups", "created_by"),
    ("group_policies", "granted_by"),
    ("user_group_memberships", "assigned_by"),
    ("rbac_scopes", "created_by"),
    ("role_bindings", "granted_by"),
    ("permission_overrides", "granted_by"),
    ("access_reviews", "reviewed_by"),
)


def reassign_authorship(conn, from_user_id: int, to_user_id: int) -> None:
    """Point every 'done by' column that references `from_user_id` at
    `to_user_id`. Caller commits."""
    cursor = conn.cursor()
    try:
        for table, column in _AUTHORSHIP_COLUMNS:
            cursor.execute(
                f"UPDATE {table} SET {column} = %s WHERE {column} = %s",
                (to_user_id, from_user_id),
            )
    finally:
        cursor.close()


def purge_principal_grants(conn, principal_type: str, principal_id: int) -> dict:
    """Delete role bindings, permission overrides and access-review rows
    that target this principal. Returns the row counts. Caller commits."""
    if principal_type not in ("user", "group"):
        raise ValueError("principal_type must be 'user' or 'group'")
    counts = {}
    cursor = conn.cursor()
    try:
        for label, table in (
            ("bindings", "role_bindings"),
            ("overrides", "permission_overrides"),
            ("reviews", "access_reviews"),
        ):
            cursor.execute(
                f"DELETE FROM {table} WHERE principal_type = %s AND principal_id = %s",
                (principal_type, principal_id),
            )
            counts[label] = getattr(cursor, "rowcount", 0) or 0
    finally:
        cursor.close()
    return counts


def active_admin_count(conn, exclude_user_id: Optional[int] = None) -> int:
    """Number of ACTIVE users whose role is admin (optionally excluding
    one). Deactivated admins do not count: they cannot log in, so they
    cannot be the recovery path the last-admin guard exists to protect."""
    cursor = conn.cursor(dictionary=True)
    try:
        if exclude_user_id is None:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND (active = 1 OR active IS NULL)"
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' "
                "AND (active = 1 OR active IS NULL) AND id <> %s",
                (exclude_user_id,),
            )
        return int(cursor.fetchone()["n"])
    finally:
        cursor.close()
