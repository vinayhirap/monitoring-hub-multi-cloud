# tests/test_audit_p6_nav_icons.py
"""Reports and Compliance must not share the same sidebar glyph."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "frontend/src/components/Layout.jsx").read_text()


def _icon(name):
    m = re.search(rf"function {name}\(\).*", SRC)
    assert m, name
    return m.group(0)


def test_reports_and_compliance_icons_differ():
    assert _icon("ReportsIcon") != _icon("ComplianceIcon")


def test_compliance_is_a_shield_and_reports_has_bars():
    assert "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" in _icon("ComplianceIcon")
    rep = _icon("ReportsIcon")
    assert rep.count("<line") == 3 and "M14 2H6" in rep
