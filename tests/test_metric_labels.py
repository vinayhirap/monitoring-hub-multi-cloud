# tests/test_metric_labels.py
"""Metric names shown to people come from the seed metric catalogues (2026-09-30).

scripts/generate_metric_labels.py derives a label for every catalogue metric from its OFFICIAL name
and writes frontend/src/utils/metricLabels.generated.js. These tests keep that honest: a catalogue
metric without a proper label, a label that breaks the style rules, a stale generated file, or a
frontend helper that disagrees with the generated map all fail here -- so raw keys such as
"disk_used_percent" or "bytesouttodestination" cannot reach the UI again."""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import generate_metric_labels as gen  # noqa: E402

LABELS, CONFLICTS = gen.build()
ROWS = list(gen.catalogue_metrics())


def test_every_catalogue_metric_has_a_label():
    missing = sorted({stored for _, _, _, stored in ROWS if stored not in LABELS})
    assert not missing, f"no label for: {missing}"


def test_no_two_catalogue_entries_with_the_same_stored_name_get_different_labels():
    assert CONFLICTS == [], f"conflicting labels (add an OVERRIDES entry): {CONFLICTS}"


def test_official_names_the_tokenizer_cannot_split_are_overridden():
    # e.g. "cachehits", "evictedkeys": lower-case with no separators -> would become "Cachehits"
    unsplittable = sorted({(o, s) for _, _, o, s in ROWS
                           if re.fullmatch(r"[a-z0-9]{9,}", o) and s not in gen.OVERRIDES and o not in gen.OVERRIDES})
    # lower-case official names that are genuinely one word are fine; anything else needs an override
    single_words = {"availability", "throughput", "latency", "duration", "invocations", "evictions", "requests",
                    "throttles", "transactions", "deadlock", "egress", "ingress", "uptime", "nodes", "errors",
                    "coldstart"}
    bad = [(o, s) for o, s in unsplittable if o not in single_words and LABELS[s].count(" ") == 0]
    assert not bad, f"one-word labels for run-together names, add OVERRIDES: {bad}"


ACRONYM_WORDS = set(gen.ACRONYMS.values())
JOINERS = {w for w in gen.LOWER_WORDS}


def _bad_word(w, first):
    if w in ACRONYM_WORDS or re.fullmatch(r"[0-9]+[a-z]*", w) or re.fullmatch(r"[A-Z0-9/]{2,6}", w):
        return False
    if not first and w in JOINERS:
        return False
    return not re.fullmatch(r"[A-Z][a-z0-9]*(?:/[A-Z][a-z0-9]*)?|[A-Za-z]+-[A-Za-z-]+|\([^)]*\)|\(/.*\)", w)


def test_labels_follow_the_style_rules():
    problems = []
    for stored, label in LABELS.items():
        plain = label.replace("I/O", "IO").replace("Read/Write", "ReadWrite")      # legitimate slashes
        if "_" in plain or ("/" in plain and "(" not in plain) or "  " in label or label != label.strip():
            problems.append((stored, label, "characters"))
        if len(label) > 44:
            problems.append((stored, label, "too long"))
        words = label.split(" ")
        for i, w in enumerate(words):
            if _bad_word(w, i == 0):
                problems.append((stored, label, f"word {w!r}"))
    assert not problems, "\n".join(map(str, problems[:25]))


def test_the_names_people_complained_about_read_properly():
    assert LABELS["disk_used_percent"] == "Disk Utilization"
    assert LABELS["mem_used_percent"] == "Memory Utilization"
    assert LABELS["cpuutilization"] == "CPU Utilization"
    assert LABELS["errors5xx"] == "Target 5xx Errors"
    assert LABELS["bytesouttodestination"] == "Bytes Out to Destination"
    assert LABELS["volumewriteops"] == "Volume Write Operations"


def test_generated_frontend_file_is_up_to_date():
    current = open(gen.OUT, encoding="utf-8").read()
    assert current == gen.render(LABELS), "run: python3 scripts/generate_metric_labels.py"


def test_percent_metrics_come_from_the_catalogue_units():
    pct = set(gen.percent_metrics())
    assert {"disk_used_percent", "mem_used_percent", "cpuutilization", "burstbalance"} <= pct
    assert "networkin" not in pct and "volumewriteops" not in pct


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_helper_returns_exactly_the_generated_labels():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    names = sorted(LABELS) + ["DISK_USED_PERCENT", "disk_used_percent__var_lib_mysql", "disk_used_percent__boot",
                              "SomeNewCloudMetric", "my_custom_metric", "", None]
    script = ("import { metricLabel, metricUnit, formatMetricValue } from 'file://" + root +
              "/frontend/src/utils/metricLabels.js';\n"
              "const names = JSON.parse(process.argv[1]);\n"
              "console.log(JSON.stringify({labels: names.map(metricLabel), units: names.map(metricUnit),"
              " v: [formatMetricValue('disk_used_percent', 83.1), formatMetricValue('volumewriteops', 12100),"
              " formatMetricValue('burstbalance', 40)]}));\n")
    out = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(names)],
                         capture_output=True, text=True, check=True).stdout
    got = json.loads(out)
    for name, label in zip(names, got["labels"]):
        if name in LABELS:
            assert label == LABELS[name], (name, label)
    by = dict(zip(names, got["labels"]))
    assert by["DISK_USED_PERCENT"] == "Disk Utilization"
    assert by["disk_used_percent__var_lib_mysql"] == "Disk Utilization (/var/lib/mysql)"
    assert by["disk_used_percent__boot"] == "Disk Utilization (/boot)"
    assert by["SomeNewCloudMetric"] == "Some New Cloud Metric"
    assert by["my_custom_metric"] == "My Custom Metric"
    assert by[""] == "" and by[None] == ""
    units = dict(zip(names, got["units"]))
    assert units["disk_used_percent__boot"] == "%" and units["volumewriteops"] == ""
    assert got["v"] == ["83.1%", "12.1K", "40%"]


def test_no_hand_typed_label_table_remains_in_the_frontend_helper():
    js = open("frontend/src/utils/metricLabels.js", encoding="utf-8").read()
    assert "GENERATED_METRIC_LABELS" in js and "volumewriteops" not in js and "Volume Write" not in js
