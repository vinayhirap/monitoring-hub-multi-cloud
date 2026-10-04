# tests/test_audit_p11_ui_batch.py
"""Audit B6 (alert row actions), B8 (findings search/pagination), B9 (checklist), C4 (polling) - wiring guards."""
from pathlib import Path

FE = Path(__file__).resolve().parent.parent / "frontend/src"


def _t(rel):
    return (FE / rel).read_text()


def test_alert_rows_keep_two_primary_buttons_and_move_the_rest_into_menus():
    src = _t("pages/Alerts.jsx")
    row = src.split('<div className="console-links">')[1].split("</tr>")[0]
    assert row.count("<RowMenu") == 2                                   # one for links, one for state changes
    assert "Investigate" in row and "Ack" in row
    for gone in ('className="btn-resolve"', "Mute 1h", "Not genuine?"):
        assert gone not in row, gone                                    # no longer inline buttons


def test_resolve_is_confirmed_in_both_the_row_menu_and_the_drawer():
    assert "window.confirm(\"Resolve this alert?" in _t("pages/Alerts.jsx")
    assert "window.confirm(\"Resolve this alert?" in _t("components/AlertInvestigation.jsx")


def test_not_genuine_wording_is_identical_in_row_and_drawer():
    for rel in ("pages/Alerts.jsx", "components/AlertInvestigation.jsx"):
        text = _t(rel)
        assert "Mark as not genuine" in text and "Undo: not genuine" in text, rel
    assert "Not genuine?" not in _t("pages/Alerts.jsx").split('<div className="console-links">')[1].split("</tr>")[0]


def test_menu_is_accessible_and_does_not_trigger_row_expansion():
    menu = _t("components/RowMenu.jsx")
    for needle in ('aria-haspopup="menu"', "aria-expanded", 'role="menu"', 'role="menuitem"', 'e.key === "Escape"',
                   "e.stopPropagation()"):
        assert needle in menu, needle


def test_security_findings_filters_and_paginates():
    src = _t("pages/SecurityFindings.jsx")
    assert "view.rows.map" in src and "filterFindings(" in src and "paginate(filtered, page)" in src
    assert "findings.map(f =>" not in src                               # no longer renders every row
    assert 'aria-label="Search findings"' in src


def test_checklist_and_pollers_are_wired():
    assert "FirstRunChecklist" not in _t("pages/Overview.jsx")        # removed on request: the monitoring team shares admin access
    assert "visibleInterval(load, POLL_MS)" in _t("hooks/useDashboardData.js")
    assert "visibleInterval(load, POLL_MS)" in _t("hooks/useResourceAlerts.js")
    assert "backoffSkips(failures)" in _t("hooks/useAlertSync.js")
