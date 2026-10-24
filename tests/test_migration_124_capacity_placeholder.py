"""Migration 124 repairs placeholder (1000000 / 5000000 / '>') thresholds on disk/memory used-% metrics only. Static checks
that keep it consistent with the code it mirrors; the behaviour was verified on a real MariaDB (see the migration header)."""
import re
from pathlib import Path

from app import threshold_coverage as tc, threshold_defaults as td

SQL = (Path(__file__).resolve().parent.parent / "db/migrations/124_capacity_threshold_placeholder_repair.sql").read_text()
BODY = "\n".join(l for l in SQL.splitlines() if not l.lstrip().startswith("--"))


def test_only_the_exact_placeholder_is_matched():
    w, c, comp = td.PLACEHOLDER_THRESHOLD
    assert f"warning_value = {int(w)}" in BODY and f"critical_value = {int(c)}" in BODY and f"comparison = '{comp}'" in BODY


def test_writes_the_shipped_default_and_nothing_else():
    w, c, _ = td.DEFAULT_THRESHOLDS["disk_used_percent"]
    assert td.DEFAULT_THRESHOLDS["mem_used_percent"][:2] == (w, c)
    assert f"SET t.warning_value = {w}, t.critical_value = {c}" in BODY
    assert re.findall(r"SET (.*?)\nWHERE", BODY, re.S)[0].count("=") == 2            # no use_dynamic / enabled change


def test_pseudo_mount_list_matches_threshold_coverage():
    skipped = set(re.findall(r"NOT LIKE 'disk\\_used\\_percent\\_\\_(\w+)%'", BODY))
    assert skipped == set(tc._PSEUDO_MOUNT_SLUGS)


def test_families_match_the_capacity_metrics():
    for base in td.CAPACITY_PERCENT_METRICS:
        assert f"'{base}'" in BODY
