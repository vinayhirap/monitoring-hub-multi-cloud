# app/topology_sync.py
"""
Shared upsert/prune helper for auto-detected resource_relationships
edges (db/migrations/021_resource_relationships.sql).

NOT used by the original two auto-sync functions in
app/aws/describe_polling.py (_sync_topology_edges for ALB->EC2,
_sync_ebs_attachment_edges for EC2->EBS) -- those were already live and
verified working in production by the time a third and fourth caller
(Lambda event-source mappings, CloudFront->S3 origins, both added
2026-09-13) made "just copy the same ~25 lines a third time" the wrong
call. Deliberately left those two exactly as they were rather than
retrofitting them onto this helper -- refactoring already-proven,
already-deployed code for a DRY win alone isn't worth the regression
risk on code with no way to test against a real AWS account here.
Every NEW auto-sync caller should use this instead of writing its own
copy.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)


def sync_auto_edges(account_id: int, relationship_type: str, pairs) -> int:
    """
    Upserts 'auto' edges into resource_relationships for one
    (account_id, relationship_type) scope, and deletes any 'auto' edge
    of that same scope no longer present in `pairs` -- so a resync
    reflects removals (event source mapping deleted, CloudFront origin
    changed) as well as additions, not just an ever-growing edge set.
    'manual' edges are never touched, matching every other auto-sync in
    this app, so a resync can never clobber something an operator
    declared by hand.

    account_id: aws_accounts.id (or the equivalent internal id for
        Azure/GCP accounts -- this table isn't AWS-specific despite the
        column name).
    relationship_type: e.g. 'invokes' (Lambda event source), 'origin'
        (CloudFront->S3). Scopes both the upsert and the prune query, so
        two different relationship_types for the same account never
        interfere with each other's pruning.
    pairs: iterable of (source_resource_id, target_resource_id) tuples.

    Returns the number of distinct pairs written. Callers should skip
    calling this at all when they have zero pairs for a cycle (matches
    every existing auto-sync's "if edges: sync(...)" guard) -- this
    function does NOT special-case an empty `pairs` into "delete every
    edge of this type", since a transient empty result (e.g. a single
    failed API call) silently wiping real edges would be worse than
    just leaving that account's edges stale until the next successful
    cycle.
    """
    pairs = list(dict.fromkeys(pairs))  # de-dupe, preserve order
    if not pairs:
        return 0
    conn = get_connection(); cur = conn.cursor()
    try:
        for src, tgt in pairs:
            cur.execute("""
                INSERT INTO resource_relationships
                    (aws_account_id, source_resource_id, target_resource_id,
                     relationship_type, source)
                VALUES (%s, %s, %s, %s, 'auto')
                ON DUPLICATE KEY UPDATE source_resource_id = VALUES(source_resource_id)
            """, (account_id, src, tgt, relationship_type))

        cur.execute("""
            SELECT id, source_resource_id, target_resource_id
            FROM resource_relationships
            WHERE aws_account_id = %s AND relationship_type = %s AND source = 'auto'
        """, (account_id, relationship_type))
        existing = cur.fetchall()
        stale_ids = [row[0] for row in existing if (row[1], row[2]) not in pairs]
        if stale_ids:
            fmt = ",".join(["%s"] * len(stale_ids))
            cur.execute(f"DELETE FROM resource_relationships WHERE id IN ({fmt})", stale_ids)
        conn.commit()
        return len(pairs)
    except Exception as e:
        logger.warning(f"topology_sync: edge sync failed [account {account_id}, {relationship_type}]: {e}")
        conn.rollback()
        return 0
    finally:
        cur.close(); conn.close()
