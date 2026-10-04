# tests/test_audit_p15_ui_consistency.py
"""
Regressions found by looking at the running UI (screenshots of the EC2 / EBS pages, Compliance log, Overview, Security
Findings):

  * pages rendered unstyled after the route-level code splitting, because each page's CSS only downloaded with that page
    while pages share class names across files -> ONE stylesheet again (vite cssCodeSplit: false)
  * audit entries naming accounts by id ("account 10: ...")
  * the Overview status filter claiming "No accounts found" when it simply matched none
  * "--" used as a dash and "alert(s)" plurals in text people read
"""
import re
import sys
from pathlib import Path

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend/src"


def _t(rel):
    return (FE / rel).read_text()


# ── styling ──────────────────────────────────────────────────────────────────

def test_the_app_ships_one_stylesheet():
    cfg = (ROOT / "frontend/vite.config.js").read_text()
    assert re.search(r"cssCodeSplit:\s*false", cfg), "per-page CSS made EC2/EBS/Overview pages render unstyled"
    assert "unstyled" in cfg or "looked like raw HTML" in cfg          # the reason stays next to the setting


def test_every_class_a_page_uses_from_another_pages_css_is_covered_by_that_single_stylesheet():
    """Belt and braces for the setting above: the classes behind the broken screenshots live in files that are NOT the
    page's own CSS, so they only work because everything is bundled together."""
    assert "id-header" in _t("pages/AccountDetail.css") and "id-header" in _t("pages/ServiceDetail.jsx")
    assert ".btn-refresh" in _t("pages/Alerts.css") and 'className="btn-refresh"' in _t("pages/Overview.jsx")


# ── account names instead of ids ─────────────────────────────────────────────

class _Cur:
    def __init__(self, rows):
        self.rows, self.sql = rows, []
    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._row = self.rows.get(params[0]) if params else None
    def fetchone(self):
        return self._row
    def close(self):
        pass


class _Conn:
    def __init__(self, rows, boom=False):
        self.cur, self.boom = _Cur(rows), boom
    def cursor(self, dictionary=False):
        return self.cur
    def close(self):
        pass


def _names(rows, boom=False):
    conn = _Conn(rows, boom)
    def get_connection():
        if boom:
            raise RuntimeError("db down")
        return conn
    install_stub("app.db", get_connection=get_connection)
    return load_module("app/account_names.py"), conn


def test_account_label_returns_the_name_caches_it_and_falls_back_to_the_id():
    m, conn = _names({10: ("U4RAD",)})
    assert m.account_label(10) == "U4RAD" and m.account_label(10) == "U4RAD"
    assert len(conn.cur.sql) == 1                                        # second call served from the cache
    assert m.account_label(99) == "account 99"                           # unknown id: still readable, never an error
    assert m.account_label(None) == "all accounts"
    m2, _ = _names({}, boom=True)
    assert m2.account_label(5) == "account 5"                            # a failed lookup must not break the audited request


def test_metric_name_label_uses_the_ui_wording():
    m, _ = _names({312: ("volumereadops",)})
    assert m.metric_name_label(312) == "Volume Read Operations"
    assert m.metric_name_label(1) == "metric #1"
    assert m.plural(1, "threshold") == "threshold" and m.plural(2, "threshold") == "thresholds"


def test_no_audit_message_prints_a_bare_account_id_any_more():
    for rel in ("app/api/settings.py", "app/api/metric_catalog.py"):
        src = (ROOT / rel).read_text()
        for stmt in re.findall(r"_write_audit\((.*?)\)\s*\n", src, re.S):
            assert "account={account_id}" not in stmt and "account {account_id}" not in stmt, (rel, stmt[:80])
        assert "account_label(account_id)" in src
    assert "metric_id={metric_id}" not in (ROOT / "app/api/settings.py").read_text()


def test_compliance_translates_stored_account_ids_for_display_and_export():
    jsx = _t("pages/Compliance.jsx")
    assert "getLiveAccounts" in jsx and "humanizePayload(" in jsx and "nameById" in jsx
    assert "export function humanizeAuditText" in _t("utils/auditText.js")


# ── Overview filter ──────────────────────────────────────────────────────────

def test_a_status_filter_that_matches_nothing_does_not_say_no_accounts_found():
    jsx = _t("pages/Overview.jsx")
    empty_filter = jsx.index("filteredGroups.length === 0 && grouped.length > 0")
    no_accounts = jsx.index("No accounts found.")
    assert empty_filter < no_accounts                                     # the filter case is checked first
    assert "No accounts are in a critical state right now." in jsx and "Show all accounts" in jsx
    assert '`${filteredGroups.length} of ${grouped.length}`' in jsx       # "(0 of 2)", not "(0)"


# ── wording ──────────────────────────────────────────────────────────────────

def test_findings_and_explanations_no_longer_use_double_hyphens_as_dashes():
    cspm = (ROOT / "app/collector/cspm.py").read_text()
    for stale in ('" -- includes a sensitive port', "password -- enable MFA", "account level -- any", "disabled -- data in transit"):
        assert stale not in cspm, stale
    assert "Includes a sensitive port (SSH/RDP/DB)." in cspm and "password. Enable MFA" in cspm
    rca = (ROOT / "app/collector/rca.py").read_text()
    assert "message']}) -- this is the most likely trigger" not in rca


def test_visible_ui_text_uses_real_plurals_and_dashes():
    for rel in ("components/AlertBadge.jsx", "pages/Incidents.jsx", "components/AlertInvestigation.jsx",
                "pages/access/RolesTab.jsx", "pages/access/AdvancedTab.jsx"):
        text = _t(rel)
        visible = re.sub(r"\{/\*.*?\*/\}|^\s*//.*$", "", text, flags=re.S | re.M)
        assert not re.search(r"\b(alert|binding)\(s\)", visible), rel
    assert " -- " not in _t("pages/MaintenanceWindows.jsx").split('className="sub"')[1].split("</p>")[0]
    assert "alert(s)" not in _t("pages/Settings.jsx").split("closed.")[0][-80:]
