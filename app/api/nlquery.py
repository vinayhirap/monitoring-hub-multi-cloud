# app/api/nlquery.py
"""
AIOps roadmap #11 -- natural-language dashboard search (2026-09-14).
Single read-only endpoint: GET /api/search?q=... . See
app/nlquery/parser.py for why this is deterministic keyword matching
rather than an embedding model, and app/nlquery/search.py for the
scoped query execution.
"""
import logging
from fastapi import APIRouter, Depends, Query
from app.auth.permissions import require_permission
from app.nlquery.search import run_nl_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("")
def nl_search(
    q: str = Query(..., min_length=1, max_length=300,
                    description="Plain-English query, e.g. 'critical rds alerts last hour'"),
    current_user: dict = Depends(require_permission("search.query")),
):
    """
    Plain-English search over this caller's own alerts, scoped exactly
    like GET /alerts (same get_accessible_account_ids() check) -- this
    is a new way to ASK for existing data, never a new data-access
    path. Returns the parsed interpretation alongside the results so
    the UI can show the user what was actually matched ("Showing:
    severity = CRITICAL; resource type = rds; within the last 1
    hour(s)") rather than a silent black-box filter.
    """
    return run_nl_search(q, current_user)
