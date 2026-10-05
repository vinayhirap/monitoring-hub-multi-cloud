# app/report_names.py
"""
Default file names for downloaded PDFs. They used to come from internal keys:

    weekly_20260926-20261003_83ed45d4.pdf     (periodic report: the storage key, with a content hash)
    rca-report-alert-9467.pdf                  (RCA report: no account, no metric, no date)

Now a person who saves ten of them can tell them apart in a folder:

    CloudOps-Weekly-Report-U4RAD-2026-09-26_to_2026-10-03.pdf
    CloudOps-RCA-Alert-9467-Volume-Read-Operations-AuroGov-Mumbai-2026-10-04.pdf

ASCII only, no spaces, no characters that are unsafe in a file system or a Content-Disposition header.
"""
import re
import unicodedata


def slug(text, maxlen=40) -> str:
    """'AuroGov Mumbai' -> 'AuroGov-Mumbai'. Accents folded, everything else non-alphanumeric becomes one hyphen."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:maxlen].rstrip("-")


def _day(value) -> str:
    try:
        return value.strftime("%Y-%m-%d")
    except AttributeError:
        return slug(str(value or ""), 10)


_TYPE_WORDS = {"WEEKLY": "Weekly", "MONTHLY": "Monthly", "QUARTERLY": "Quarterly", "CUSTOM": "Custom"}


def periodic_report_filename(report: dict) -> str:
    """report: a `reports` row (report_type, scope_type, scope_label/scope_id, period_start, period_end)."""
    scope = (report.get("scope_type") or "").upper()
    if scope == "INCIDENT":
        kind = "Incident"
    elif scope == "RESOURCE":
        kind = "Resource"
    else:
        kind = _TYPE_WORDS.get((report.get("report_type") or "").upper(), "Operations")
    who = slug(report.get("scope_label") or report.get("scope_id") or "report", 36)
    start, end = report.get("period_start"), report.get("period_end")
    period = f"{_day(start)}_to_{_day(end)}" if start and end else ""
    return "-".join(p for p in ("CloudOps", kind, "Report", who, period) if p) + ".pdf"


def rca_report_filename(alert_id, metric_label_text, account_name, triggered_at, ext="pdf") -> str:
    parts = ["CloudOps", "RCA", f"Alert-{alert_id}", slug(metric_label_text, 32), slug(account_name, 24), _day(triggered_at) if triggered_at else ""]
    return "-".join(p for p in parts if p) + f".{ext}"
