# tests/test_audit_p5_notfound_and_index.py
"""Audit B1 (404 page instead of silent redirect) and D3 (audit_logs created_at index)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_unknown_urls_render_not_found_inside_the_layout():
    app = (ROOT / "frontend/src/App.jsx").read_text()
    assert 'import NotFound' in app and '<Route path="*"' in app and "<NotFound />" in app
    # no top-level catch-all that silently redirects to /overview any more
    assert '<Route path="*" element={<Navigate to="/overview" replace />} />' not in app
    layout_end = app.index("</Route>", app.index('path="accounts/:id"'))
    assert app.index("<NotFound />") < layout_end          # a child of the authenticated Layout route


def test_not_found_page_shows_the_attempted_path_and_a_way_back():
    jsx = (ROOT / "frontend/src/pages/NotFound.jsx").read_text()
    assert "useLocation" in jsx and "{pathname}" in jsx and 'to="/overview"' in jsx


def test_migration_079_is_idempotent():
    sql = (ROOT / "db/migrations/079_audit_logs_created_at_index.sql").read_text()
    assert "index_name = 'idx_audit_logs_created_at'" in sql
    assert "ADD INDEX idx_audit_logs_created_at (created_at)" in sql
