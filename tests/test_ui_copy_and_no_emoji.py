# tests/test_ui_copy_and_no_emoji.py
"""UI conventions (2026-09-30): this application uses SVG line icons, never emoji; the
Alerts page tab is called "Alerts Needs Attention"; a language-model summary is labelled
"Generated summary" (not "AI-polished")."""
import glob
import re

# pictographs (emoji), dingbats / misc symbols (checkmarks, sparkles, warning signs, clouds...),
# hourglass, play/pause, variation selector -- arrows, bullets and typographic dashes are fine
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u23E9-\u23FF\u2315\u25B6\u25C0\ufe0f]")


def _ui_files():
    files = []
    for pat in ("**/*.jsx", "**/*.js", "**/*.css"):
        files += glob.glob("frontend/src/" + pat, recursive=True)
    return sorted(files)


def test_no_emoji_or_emoji_like_glyphs_in_the_frontend():
    hits = []
    for f in _ui_files():
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            for m in EMOJI.finditer(line):
                hits.append(f"{f}:{i} U+{ord(m.group()):04X} {line.strip()[:70]}")
    assert not hits, "emoji/pictograph glyphs found (use components/icons.jsx instead):\n" + "\n".join(hits[:20])


def test_no_ai_polished_wording_and_the_new_labels_are_present():
    alerts = open("frontend/src/pages/Alerts.jsx", encoding="utf-8").read()
    css = open("frontend/src/pages/Alerts.css", encoding="utf-8").read()
    assert "AI-polished" not in alerts and "explain-ai-badge" not in alerts + css
    assert "Generated summary" in alerts and ".explain-source" in css
    assert '"Alerts Needs Attention"' in alerts and "Needs attention" not in alerts


def test_alert_tabs_are_ordered_attention_first_then_lifecycle_then_history():
    alerts = open("frontend/src/pages/Alerts.jsx", encoding="utf-8").read()
    block = alerts[alerts.index("const TAB_GROUPS"):alerts.index("];", alerts.index("const TAB_GROUPS"))]
    keys = re.findall(r'\["(\w+)", "', block)
    assert keys == ["active", "critical", "attention", "tuning", "stale", "acknowledged",
                    "suppressed", "resolved", "all"]
    # every tab still exists server-side (counts + rows)
    api = open("app/api/alerts.py", encoding="utf-8").read()
    for k in keys:
        assert f'"{k}"' in api


def test_the_decorative_live_pill_and_meta_subtitle_are_gone_from_the_alerts_page():
    alerts = open("frontend/src/pages/Alerts.jsx", encoding="utf-8").read()
    assert "live-pill" not in alerts and "counts here match the Overview banner" not in alerts


def test_overview_banner_red_dot_only_with_critical_alerts():
    """The Overview alert banner showed a red dot (and a stray separator) even with zero critical
    alerts. The dot must be conditional on critical > 0 and a warnings-only banner is amber."""
    src = open("frontend/src/pages/Overview.jsx", encoding="utf-8").read()
    i = src.index("function AlertStrip(")
    body = src[i:src.index("\n}\n", i)]
    assert "{hasCritical && <span className=\"as-dot critical\" />}" in body
    assert body.count("as-dot") == 1                      # no unconditional dot anywhere in the banner
    assert "alert-strip-warn" in body
    assert "{hasCritical && <span style={{ color: \"var(--text-muted)\", marginRight: 8 }}>·</span>}" in body
    css = open("frontend/src/pages/Overview.css", encoding="utf-8").read()
    assert ".alert-strip.alert-strip-warn" in css


def test_overview_banner_has_no_explanatory_text_and_tile_is_renamed():
    src = open("frontend/src/pages/Overview.jsx", encoding="utf-8").read()
    i = src.index("function AlertStrip(")
    body = src[i:src.index("\n}\n", i)]
    assert "not counted" not in body and "Not counted above" not in src     # no explanatory text in the banner
    assert "otherAlerts" not in src
    assert 'label="Resources Need Attention"' in src and "<h2>Resources Need Attention</h2>" in src
    assert 'label="Need Attention"' not in src
