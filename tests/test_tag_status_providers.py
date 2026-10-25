# tests/test_tag_status_providers.py
"""Tag status for Azure and GCP rows (workstream B): which types are judged, GCP's lowercase label keys, and the
gate that keeps every row exactly as before while the feature is off."""
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401,E402
from app import environment_classifier as ec  # noqa: E402
from tests.conftest import install_stub, load_module  # noqa: E402


def test_allow_lists_keep_out_the_types_whose_discovery_stores_no_real_labels():
    for t in ("gke_node", "firestore_database", "nat_gateway", "bigquery_project", "other"):
        assert t not in ec.GCP_LABELED_TYPES
    assert "vm" in ec.AZURE_TAGGED_TYPES and "compute_instance" in ec.GCP_LABELED_TYPES
    assert len(ec.AZURE_TAGGED_TYPES) == 19 and len(ec.GCP_LABELED_TYPES) == 12


@pytest.mark.parametrize("provider,rtype", [("azure", "vm"), ("azure", "key_vault"), ("gcp", "gcs_bucket"), ("gcp", "pubsub_topic")])
def test_listed_types_are_judged_from_their_stored_tags(provider, rtype):
    assert ec.tags_known(rtype, {}, None, provider) is True
    assert ec.tag_state(rtype, {}, None, provider) == "missing"            # discovery stores the real tags: none


@pytest.mark.parametrize("provider,rtype", [("azure", "some_generic_type"), ("azure", "ec2"), ("gcp", "gke_node"),
                                            ("gcp", "firestore_database"), ("gcp", "nat_gateway"), ("gcp", "other")])
def test_unlisted_types_stay_not_checked_even_with_empty_tags(provider, rtype):
    assert ec.tag_state(rtype, {}, None, provider) == "not_checked"


def test_azure_keeps_the_exact_standard_key():
    assert ec.tag_state("vm", {"Environment": "prod"}, None, "azure") == "ok"
    assert ec.tag_state("vm", {"environment": "prod"}, None, "azure") == "nonstandard"
    assert ec.tag_state("vm", {"Env": "Production"}, None, "azure") == "nonstandard"
    assert ec.tag_state("vm", {"Environment": "banana"}, None, "azure") == "invalid"


def test_gcp_labels_are_lowercase_so_the_lowercase_key_is_the_standard():
    assert ec.tag_state("compute_instance", {"environment": "prod"}, None, "gcp") == "ok"
    assert ec.tag_state("compute_instance", {"Environment": "prod"}, None, "gcp") == "nonstandard"
    assert ec.tag_state("compute_instance", {"env": "prod"}, None, "gcp") == "nonstandard"
    assert ec.tag_state("compute_instance", {"environment": "banana"}, None, "gcp") == "invalid"
    # discovery adds an internal key to instances; it must not count as a tag
    assert ec.tag_state("compute_instance", {"_gcp_numeric_id": "123"}, None, "gcp") == "missing"


def test_missing_tags_for_gcp_labels_ignores_case():
    labels = {"environment": "prod", "owner": "team", "costcenter": "c1"}
    assert ec.missing_tags(labels, ec.MANDATORY_TAGS) == ["Application", "Criticality", "DataClassification", "ManagedBy"]


def test_no_provider_means_exactly_the_old_behaviour():
    assert ec.tag_state("vm", {}, None, None) == "not_checked"
    assert ec.tag_state("vm", {}) == "not_checked"
    assert ec.tag_state("ec2", {}, None, None) == "missing"
    assert ec.tag_state("ec2", {"Environment": "nonprod"}, None, "aws") == "ok"
    # an AWS type name on a non-AWS account is not judged as AWS
    assert ec.tag_state("ec2", {"Environment": "nonprod"}, None, "azure") == "not_checked"


def test_sync_types_and_provider_types_do_not_overlap_with_aws_collected_types():
    assert not (ec.AZURE_TAGGED_TYPES | ec.GCP_LABELED_TYPES) & ec.TAG_SYNC_TYPES
    assert not (ec.AZURE_TAGGED_TYPES | ec.GCP_LABELED_TYPES) & ec.TAGS_COLLECTED_TYPES


# ---- the gate in env_sync
def _env_sync():
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/collector/env_sync.py")


def test_provider_is_passed_only_while_the_feature_is_on(monkeypatch):
    mod = _env_sync()
    monkeypatch.delenv("TAG_SYNC_ENABLED", raising=False)
    assert mod._row_provider({"provider": "azure"}) is None and mod._row_provider({"provider": "gcp"}) is None
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    assert mod._row_provider({"provider": "azure"}) == "azure"
    assert mod._row_provider({"provider": "GCP"}) == "gcp"
    assert mod._row_provider({"provider": "aws"}) is None and mod._row_provider({}) is None
