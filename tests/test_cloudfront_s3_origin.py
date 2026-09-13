# tests/test_cloudfront_s3_origin.py
"""
Covers app/collector/discovery/extended.py's
_s3_bucket_from_origin_domain(), added alongside the CloudFront->S3
topology auto-sync (roadmap phase 4/7 follow-up, 2026-09-13).

Loaded through the real module (not copy-pasted logic) so a future edit
to the regex can't silently drift from what's actually deployed.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module


def _load():
    return load_module("app/collector/discovery/extended.py")


def test_rest_endpoint_legacy():
    mod = _load()
    assert mod._s3_bucket_from_origin_domain("my-bucket.s3.amazonaws.com") == "my-bucket"


def test_rest_endpoint_regional():
    mod = _load()
    assert mod._s3_bucket_from_origin_domain("my-bucket.s3.ap-south-1.amazonaws.com") == "my-bucket"


def test_website_endpoint_dash_form():
    mod = _load()
    assert mod._s3_bucket_from_origin_domain("my-bucket.s3-website-ap-south-1.amazonaws.com") == "my-bucket"


def test_website_endpoint_dot_form():
    mod = _load()
    assert mod._s3_bucket_from_origin_domain("my-bucket.s3-website.ap-south-1.amazonaws.com") == "my-bucket"


def test_bucket_name_containing_dots():
    mod = _load()
    assert mod._s3_bucket_from_origin_domain("my.dotted.bucket.name.s3.amazonaws.com") == "my.dotted.bucket.name"


def test_custom_origin_returns_none():
    mod = _load()
    # ALB, EC2, API Gateway, a third-party domain -- none of these are S3
    assert mod._s3_bucket_from_origin_domain("my-alb-123456.ap-south-1.elb.amazonaws.com") is None
    assert mod._s3_bucket_from_origin_domain("www.example.com") is None


def test_none_and_empty_return_none():
    mod = _load()
    assert mod._s3_bucket_from_origin_domain(None) is None
    assert mod._s3_bucket_from_origin_domain("") is None
