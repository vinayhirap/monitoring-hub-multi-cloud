# tests/test_email_center_status_tab.py
"""The Status tab of Email & Reports (patch 0013): a healthy mail setup shows only the numbers. Nothing about the server's
.env file, no "Working" badge and no "No configuration problems found" line; a card appears only when something is wrong."""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "frontend/src/pages/emailcenter/StatusTab.jsx").read_text()
BODY = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("//"))      # comments may mention the .env


def test_the_status_tab_never_mentions_the_env_file_or_the_old_healthy_chatter():
    for text in (".env", "Working", "No configuration problems found", "Mail settings are kept"):
        assert text not in BODY, text


def test_the_attention_card_is_only_rendered_when_something_is_wrong():
    assert "{needsAttention && (" in BODY
    assert "const needsAttention = notConfigured || problems.length > 0;" in BODY
    assert "Badge" not in BODY                                   # no status badge on a healthy page


def test_the_numbers_are_three_cards_and_skipped_only_appears_when_there_is_something_to_show():
    assert BODY.count("<KpiCard") == 3
    assert "{anySkipped && <KpiCard" in BODY


def test_the_last_failure_panel_is_hidden_when_there_is_none():
    assert "{st.last_failure && (" in BODY and "No failed deliveries on record" not in BODY
