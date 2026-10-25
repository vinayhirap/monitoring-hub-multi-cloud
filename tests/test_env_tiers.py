"""app/env_tiers.py: the two-environment vocabulary and its fail-safe rule."""
import pytest
from tests.conftest import load_module

et = load_module("app/env_tiers.py")


@pytest.mark.parametrize("value", ["prod", "PROD", "Production", " prd ", "prod2", "Prod-1"])
def test_prod_words(value):
    assert et.recognized_tier(value) == "prod"


@pytest.mark.parametrize("value", [
    "nonprod", "NON-PROD", "Non_Prod", "non-production", "dev", "Development", "dev2", "test", "testing", "sit",
    "UAT", "uat-1", "qa", "stage", "Staging", "stg", "pre-prod", "preprod", "sandbox"])
def test_nonprod_words(value):
    assert et.recognized_tier(value) == "nonprod"


@pytest.mark.parametrize("value", [None, "", "  ", "unknown", "ignore", "banana", "product", "prodapi", 5])
def test_unrecognised_is_rejected_by_recognized_tier_but_prod_by_to_tier(value):
    assert et.recognized_tier(value) is None
    assert et.to_tier(value) == "prod"        # fail-safe: never weaker than production
    assert et.is_prod(value) and not et.is_nonprod(value)


def test_labels():
    assert et.label("nonprod") == "NON-PROD"
    assert et.label("uat") == "NON-PROD"
    assert et.label("prod") == "PROD"
    assert et.label(None) == "PROD"
    assert et.TIERS == ("prod", "nonprod")
