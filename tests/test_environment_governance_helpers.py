# tests/test_environment_governance_helpers.py
"""Workstream C, pure helpers (app/environment_governance.py) and the sync module's tolerance of migration 098."""
import sys
from datetime import datetime

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401
from tests.conftest import load_module, install_stub

eg = load_module("app/environment_governance.py")


# ---- alert impact of an account default change --------------------------------------------------------

def _alerts(*pairs):
    return [{"resource_id": r, "severity": s} for r, s in pairs]


def test_with_the_flag_off_nothing_about_an_alert_changes():
    out = eg.alert_impact({"a": ("prod", "nonprod")}, _alerts(("a", "WARNING")), flag_on=False, priority_on=True)
    assert out["flag_on"] is False and out["open_alerts_affected"] == 0 and out["priority_shifts"] == []
    assert out["priority_on"] is False


def test_prod_like_to_nonprod_turns_warning_into_info_and_keeps_critical():
    ch = {"a": (None, "nonprod"), "b": ("unknown", "nonprod"), "c": ("prod", "nonprod")}
    out = eg.alert_impact(ch, _alerts(("a", "WARNING"), ("b", "INFO"), ("c", "CRITICAL")), flag_on=True, priority_on=False)
    # "b" is already INFO level (prod-like INFO counts as warning level) and moves to nonprod: still reported as warning_to_info
    assert out["warning_to_info"] == 2 and out["info_to_warning"] == 0
    assert out["open_alerts_affected"] == 2          # the critical one keeps its severity and, with priority off, is untouched


def test_nonprod_to_prod_turns_info_into_warning():
    out = eg.alert_impact({"a": ("nonprod", "prod")}, _alerts(("a", "INFO")), flag_on=True, priority_on=False)
    assert out["info_to_warning"] == 1


def test_priority_shifts_follow_the_matrix():
    ch = {"a": ("prod", "nonprod"), "b": ("prod", "nonprod"), "c": ("nonprod", "prod")}
    out = eg.alert_impact(ch, _alerts(("a", "CRITICAL"), ("b", "WARNING"), ("c", "WARNING")), flag_on=True, priority_on=True)
    shifts = {(s["from"], s["to"]): s["count"] for s in out["priority_shifts"]}
    assert shifts == {("P1", "P3"): 1, ("P2", "P4"): 1, ("P4", "P2"): 1}


def test_priority_flag_only_matters_when_the_environment_flag_is_on():
    out = eg.alert_impact({"a": ("prod", "nonprod")}, _alerts(("a", "WARNING")), flag_on=False, priority_on=True)
    assert out["priority_shifts"] == []


def test_alerts_on_unchanged_resources_are_ignored():
    out = eg.alert_impact({"a": ("prod", "nonprod")}, _alerts(("zzz", "WARNING")), flag_on=True, priority_on=True)
    assert out["open_alerts_affected"] == 0


def test_unknown_is_prod_like_for_priority():
    assert eg.priority_for("unknown", True) == 1 and eg.priority_for("ignore", False) == 2 and eg.priority_for(None, False) == 2


def test_default_change_summary_counts_moves_and_environment_changes():
    report = {"total": 5, "changed": 3, "by_env": {"nonprod": 4, "prod": 1}, "by_source": {"account": 4, "name": 1}}
    changes = [
        {"resource_id": "a", "old_env": "unknown", "old_source": "unknown", "new_env": "nonprod", "new_source": "account"},
        {"resource_id": "b", "old_env": "nonprod", "old_source": "account", "new_env": "nonprod", "new_source": "account"},   # reason text only
        {"resource_id": "c", "old_env": None, "old_source": None, "new_env": "nonprod", "new_source": "account"},
    ]
    out = eg.default_change_summary(report, changes, None, "nonprod", [], flag_on=False, priority_on=False)
    assert out["rows_rewritten"] == 3 and out["environment_changes"] == 2 and out["ignores_criticality_tag"] is True
    assert {(m["from"], m["to"]): m["count"] for m in out["moves"]} == {("unknown", "nonprod"): 1, ("nonprod", "nonprod"): 1, ("unclassified", "nonprod"): 1}


# ---- compliance ---------------------------------------------------------------------------------------

def _row(acct, rtype, state, src, warning=None, name="A", provider="aws"):
    return {"account_id": acct, "account_name": name, "provider": provider, "resource_type": rtype,
            "tag_state": state, "env_source": src, "warning": warning}


def test_not_checked_is_its_own_bucket_and_never_counted_as_missing():
    rows = [_row(1, "ec2", "ok", "tag"), _row(1, "ec2", "missing", "name"), _row(1, "s3", "not_checked", "account"),
            _row(1, "s3", "not_checked", "unknown")]
    rep = eg.tally_compliance(rows)
    t = rep["totals"]
    assert t["by_tag_state"]["not_checked"]["n"] == 2 and t["by_tag_state"]["missing"]["n"] == 1
    assert t["checkable"] == 2 and t["ok_of_checkable_pct"] == 50.0           # not_checked is out of the denominator
    assert t["by_tag_state"]["not_checked"]["pct"] == 50.0                    # but visible, as a share of everything


def test_tag_buckets_sum_to_the_total_and_percentages_to_100():
    rows = [_row(1, "ec2", s, "tag") for s in ("ok", "ok", "missing", "nonstandard", "invalid", "not_checked", None)]
    t = eg.tally_compliance(rows)["totals"]
    assert sum(b["n"] for b in t["by_tag_state"].values()) == t["total"] == 7
    assert t["by_tag_state"]["pending"]["n"] == 1                              # NULL = not evaluated, not "missing"
    assert round(sum(b["pct"] for b in t["by_tag_state"].values())) == 100


def test_unevaluated_source_is_pending_not_unknown():
    t = eg.tally_compliance([_row(1, "ec2", "ok", None), _row(1, "ec2", "ok", "unknown")])["totals"]
    assert t["by_source"]["pending"]["n"] == 1 and t["by_source"]["unknown"]["n"] == 1


def test_per_account_and_per_type_breakdown_and_warning_counts():
    rows = [_row(1, "ec2", "ok", "tag", warning="tag_vs_name: x"), _row(1, "ebs", "ok", "related"),
            _row(2, "ec2", "missing", "account", name="B", provider="azure")]
    rep = eg.tally_compliance(rows)
    assert [a["account_id"] for a in rep["accounts"]] == [1, 2]
    a1 = rep["accounts"][0]
    assert a1["total"] == 2 and a1["warnings"] == 1 and {t["resource_type"] for t in a1["types"]} == {"ec2", "ebs"}
    assert rep["accounts"][1]["provider"] == "azure"
    assert rep["totals"]["total"] == 3


def test_empty_report_has_no_division_by_zero():
    t = eg.tally_compliance([])["totals"]
    assert t["total"] == 0 and t["ok_of_checkable_pct"] is None and t["by_tag_state"]["ok"]["pct"] is None


def test_compliance_csv_has_a_total_row_per_account_and_matching_columns():
    rep = eg.tally_compliance([_row(1, "ec2", "ok", "tag"), _row(1, "s3", "not_checked", "account")])
    head, body = eg.compliance_csv_rows(rep)
    assert all(len(r) == len(head) for r in body)
    assert [r[2] for r in body] == ["ec2", "s3", "ALL TYPES"] or [r[2] for r in body] == ["s3", "ec2", "ALL TYPES"]
    assert "tag_not_checked" in head and "decided_by_account" in head


def test_snapshot_rows_produce_the_same_shape():
    snap = {"aws_account_id": 1, "resource_type": "ec2", "total": 4, "tag_ok": 1, "tag_missing": 1, "tag_not_checked": 2,
            "src_tag": 1, "src_unknown": 3, "warnings": 1}
    rep = eg.compliance_from_snapshot_rows([snap], {1: ("A", "aws")})
    assert rep["totals"]["by_tag_state"]["not_checked"]["n"] == 2 and rep["accounts"][0]["account_name"] == "A"


# ---- csv ----------------------------------------------------------------------------------------------

def test_csv_cells_that_look_like_formulas_are_neutralised():
    for bad in ("=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd"):
        assert eg.csv_cell(bad).startswith("'"), bad
    assert eg.csv_cell("web-01") == "web-01" and eg.csv_cell(None) == "" and eg.csv_cell(7) == 7


def test_csv_text_quotes_commas_and_newlines_and_neutralises_names():
    text = eg.csv_text(["name", "tags"], [["=evil()", "a,b"], ["line\nbreak", None]])
    lines = text.split("\r\n")
    assert lines[0] == "name,tags" and lines[1] == "'=evil(),\"a,b\""
    assert "\"line\nbreak\"" in text


# ---- digest -------------------------------------------------------------------------------------------

def test_iso_week_key_is_stable_across_the_week_and_changes_on_monday():
    assert eg.iso_week_key(datetime(2026, 10, 12)) == eg.iso_week_key(datetime(2026, 10, 18, 23, 59)) == "2026W42"
    assert eg.iso_week_key(datetime(2026, 10, 19)) == "2026W43"
    assert eg.iso_week_key(datetime(2026, 12, 31)) == "2026W53"


def test_digest_text_lists_names_not_tags_and_says_how_many_are_not_shown():
    items = [{"name": f"srv-{i}", "resource_type": "ec2", "region": "ap-south-1"} for i in range(40)]
    body = eg.digest_text("AuroGov", items, 57, "http://x/environments")
    assert "40 new resource(s) in AuroGov" in body and "srv-0 (ec2, ap-south-1)" in body
    assert "and 15 more" in body and "57 resource(s)" in body and "http://x/environments" in body


# ---- digest recipient mapping -------------------------------------------------------------------------

def test_owner_team_matches_a_group_name_ignoring_case_and_spaces():
    install_stub("app.db", get_connection=lambda: None)
    d = load_module("app/env_digest.py")
    groups = [{"id": 1, "name": "Platform Team"}, {"id": 2, "name": "Data"}]
    assert d.match_group("  platform team ", groups) == 1
    assert d.match_group("Platform", groups) is None and d.match_group("", groups) is None and d.match_group(None, groups) is None


def test_two_groups_with_the_same_name_are_ambiguous_and_match_nothing():
    install_stub("app.db", get_connection=lambda: None)
    d = load_module("app/env_digest.py")
    assert d.match_group("ops", [{"id": 1, "name": "Ops"}, {"id": 2, "name": "ops"}]) is None


def test_digest_is_off_by_default_and_sends_nothing(monkeypatch):
    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(AssertionError("must not touch the database")))
    d = load_module("app/env_digest.py")
    monkeypatch.delenv("ENV_DIGEST_ENABLED", raising=False)
    assert d.digest_enabled() is False and d.maybe_send_weekly_digest(datetime(2026, 10, 19)) == {}


def test_digest_waits_for_the_configured_weekday(monkeypatch):
    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(AssertionError("must not touch the database")))
    d = load_module("app/env_digest.py")
    monkeypatch.setenv("ENV_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ENV_DIGEST_WEEKDAY", "2")
    assert d.maybe_send_weekly_digest(datetime(2026, 10, 19)) == {}            # Monday, configured Wednesday


# ---- env_sync keeps working before migration 098 -----------------------------------------------------

def _rows():
    base = {"aws_account_id": 1, "resource_type": "ec2", "tags": None, "env_resolved": None, "env_source": None,
            "env_reason": None, "tag_state": None, "default_env": None, "env_override": None}
    return [dict(base, id=1, resource_id="i-1", name="web-prod-1"),
            dict(base, id=2, resource_id="i-2", name="x-dev-1", env_override="prod")]


class _MissingColumn(Exception):
    errno = 1054


def _run_sync(raise_on_warning_select):
    written, selects = [], []

    class RCur:
        def __init__(self):
            self._last = ""

        def execute(self, sql, params=None):
            self._last = " ".join(sql.split())
            if "FROM resources r" in self._last:
                selects.append(self._last)
                if raise_on_warning_select and "r.env_warning" in self._last:
                    raise _MissingColumn("Unknown column 'r.env_warning'")

        def fetchall(self):
            return _rows() if "FROM resources r" in self._last else []

        def close(self):
            pass

    class WCur:
        rowcount = 0

        def executemany(self, sql, chunk):
            written.append((" ".join(sql.split()), list(chunk)))
            self.rowcount = len(chunk)

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return RCur() if dictionary else WCur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    install_stub("app.db", get_connection=lambda: Conn())
    mod = load_module("app/collector/env_sync.py")
    return mod.sync_resource_environments(), written, selects


def test_sync_before_migration_098_classifies_as_before_and_writes_no_warning():
    rep, written, selects = _run_sync(raise_on_warning_select=True)
    assert rep["applied"] == 2 and rep["warnings"] == 0 and rep["warning_applied"] == 0
    assert len(selects) == 2 and "r.env_warning" not in selects[1]
    assert all("env_warning" not in sql for sql, _ in written)


def test_sync_after_migration_098_writes_warnings_in_a_separate_guarded_statement():
    rep, written, _ = _run_sync(raise_on_warning_select=False)
    assert rep["warnings"] == 1 and rep["warning_applied"] == 1
    assert written[0][0].endswith("WHERE id = %s AND env_override <=> %s") and "env_warning" not in written[0][0]
    sql, chunk = written[1]
    assert sql == "UPDATE resources SET env_warning = %s WHERE id = %s AND env_override <=> %s"
    assert chunk[0][1] == 2 and chunk[0][0].startswith("manual_vs_name:") and chunk[0][2] == "prod"


def test_hypothetical_default_can_never_be_written():
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/collector/env_sync.py")
    try:
        mod.sync_resource_environments(account_id=1, account_default_override=(1, "nonprod"))
    except ValueError as e:
        assert "dry_run" in str(e)
    else:
        raise AssertionError("a preview override without dry_run must be refused")
