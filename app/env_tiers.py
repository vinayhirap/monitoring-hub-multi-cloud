# app/env_tiers.py
"""
The two environments CloudOps knows -- the single source of truth.

    PROD       production
    NON-PROD   everything else: dev, test, sit, uat, qa, staging, pre-prod, sandbox ...

Everything that depends on the environment (alert severity, alert priority P1-P4, polling cadence, the
Environments page, the account label, the Environment tag standard) is a function of exactly these two values.

Stored / API value : "prod" | "nonprod"
Display label      : "PROD" | "NON-PROD"

FAIL-SAFE RULE. Anything that cannot be placed -- no tag, no recognisable name, a free-text label nobody
has heard of, a NULL, a value stored before the two-environment model -- is PROD. An unclassified resource is
therefore never alerted, ranked or polled more weakly than production. It also waits in the Needs-classification
queue (resources.env_source = 'unknown') until a person confirms it.

This module is stdlib-only and import-free so the evaluator, the collectors, the API, the migration helpers and
the unit tests can all share it without pulling in a database or a framework.
"""
import re

PROD = "prod"
NONPROD = "nonprod"
TIERS = (PROD, NONPROD)

LABEL = {PROD: "PROD", NONPROD: "NON-PROD"}

# Whole-word vocabulary. Compared in lower case with every non-alphanumeric character removed and trailing digits
# dropped, so "Non-Prod", "non_prod", "NonProd", "dev2" and "UAT-1" all land on a word below.
PROD_WORDS = frozenset({"prod", "production", "prd"})
NONPROD_WORDS = frozenset({
    "dev", "development", "test", "testing", "sit", "uat", "qa", "qas", "stage", "staging", "stg",
    "preprod", "preproduction", "nonprod", "nonproduction", "nonprd", "sandbox",
})

_STRIP = re.compile(r"[^a-z0-9]+")
_TRAILING_DIGITS = re.compile(r"\d+$")


def _fold(value):
    return _TRAILING_DIGITS.sub("", _STRIP.sub("", str(value).strip().lower()))


def recognized_tier(value):
    """'Production' -> 'prod'; 'SIT' / 'Staging' / 'non-prod' / 'uat' -> 'nonprod'; anything else (including None,
    '', 'unknown', 'ignore', free text) -> None. Use this when an unrecognised value must be REJECTED rather than
    guessed at (API input); use to_tier() when it must be handled safely."""
    if value is None:
        return None
    word = _fold(value)
    if word in PROD_WORDS:
        return PROD
    if word in NONPROD_WORDS:
        return NONPROD
    return None


def to_tier(value):
    """Any label we may meet -> 'prod' | 'nonprod'. Unrecognised input is PROD (see the fail-safe rule above)."""
    return recognized_tier(value) or PROD


def is_nonprod(value):
    return to_tier(value) == NONPROD


def is_prod(value):
    return to_tier(value) == PROD


def label(value):
    """'PROD' | 'NON-PROD' for any value (unrecognised -> 'PROD')."""
    return LABEL[to_tier(value)]
