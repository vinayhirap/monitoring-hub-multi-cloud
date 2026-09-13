# tests/test_gcp_topology_sync.py
"""
Covers app/providers/gcp/discovery.py's _gcp_relative_resource_path(),
added alongside the GCP persistent-disk attachment topology auto-sync
(roadmap phase 4/7 follow-up, 2026-09-13).

Same reasoning as tests/test_event_source_resource_id.py's AWS Lambda
counterpart: Disk.users returns full resource URIs
(https://www.googleapis.com/compute/v1/projects/.../instances/...), but
resources.resource_id for compute_instance is the relative path only
(see _discover_compute_instances in this same file) -- getting this
normalization wrong would silently produce edges that can never resolve
to a real node.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module


def _load():
    return load_module("app/providers/gcp/discovery.py")


def test_full_uri_stripped_to_relative_path():
    mod = _load()
    uri = "https://www.googleapis.com/compute/v1/projects/my-proj/zones/us-central1-a/instances/my-vm"
    assert mod._gcp_relative_resource_path(uri) == "projects/my-proj/zones/us-central1-a/instances/my-vm"


def test_already_relative_path_passes_through():
    mod = _load()
    path = "projects/my-proj/zones/us-central1-a/instances/my-vm"
    assert mod._gcp_relative_resource_path(path) == path


def test_none_and_empty_return_none():
    mod = _load()
    assert mod._gcp_relative_resource_path(None) is None
    assert mod._gcp_relative_resource_path("") is None


def test_garbage_input_returns_none():
    mod = _load()
    assert mod._gcp_relative_resource_path("not-a-gcp-uri-at-all") is None
