# tests/

This repo's first test suite (see `add_test_suite.py` in the repo root
for the full rationale). Run with:

```
pip install -r requirements.txt
pytest tests/ -v
```

## Why these tests look the way they do

This codebase has no dependency injection -- every collector/alerting
module does `from app.db import get_connection` etc. directly at import
time, and there's no existing seam to inject a fake one through. Rather
than refactor production code just to make it more testable (real risk,
with the live server as the only way to verify a refactor didn't break
anything), these tests use the same technique this session validated
live, ad-hoc, throughout a full VictoriaMetrics-removal effort:

1. `tests/conftest.py`'s `install_stub()` registers fake modules in
   `sys.modules` for exactly what the target file imports at its top
   level (`app.db`, `app.clients.vm_client`, `app.collector.metrics_writer`,
   the relevant `azure.*`/`google.cloud.*` SDK pieces, etc.).
2. `tests/conftest.py`'s `load_module()` then loads the target `.py` file
   directly by path via `importlib.util.spec_from_file_location` --
   NOT through its own package (e.g. `app.providers.azure`), which
   would trigger `app/providers/__init__.py`'s `from app.providers
   import aws/azure/gcp` and pull in real boto3/google-cloud/azure-
   identity client construction. Loading by path skips all of that.
3. An `autouse` fixture (`clean_sys_modules`) wipes every `app.*` stub
   before and after each test function, so one test's stubs can never
   leak into another.

## What's covered, and what isn't

Covered: the resource-matching and metric-name-mapping logic in the
direct-fetch collectors (`app/providers/azure/metrics_collector.py`,
`app/providers/gcp/metrics_collector.py`) and the local-metrics helpers
in `app/aws/collector_direct.py` and `app/aws/describe_polling.py`.
This is deliberate, not arbitrary: it's the exact category of logic a
real VM-removal session found broken or subtly wrong multiple times
(GCP's compute_instance numeric-ID bug, a resource_id-vs-name mismatch
across three different collectors, and a regression in Phase 5's own
patch that silently reintroduced a billed CloudWatch call). It's also
the easiest category to test well, since it's mostly pure functions
once the DB/SDK boundary is stubbed.

NOT covered: FastAPI routes (`app/api/*.py`), auth/permissions,
frontend, discovery beyond what the collector tests touch, and anything
requiring a real database transaction or a live cloud API response.
Extending coverage into those areas is real, separate work -- adding
route-level tests in particular would benefit from FastAPI's own
`TestClient` and probably a proper test database fixture, neither of
which exist yet.

## Adding a new test

Follow the pattern in any existing `tests/test_*.py` file: build a
minimal fake DB cursor/connection object exposing an SQL-shape-aware
`execute()`, `install_stub()` everything the target module imports,
`load_module()` the target file, call the function under test directly,
assert on the result.
