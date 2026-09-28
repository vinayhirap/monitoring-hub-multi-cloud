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

Originally this suite covered only the direct-fetch collectors
(`app/providers/{azure,gcp}/metrics_collector.py`, the local-metrics
helpers in `app/aws/collector_direct.py`, `app/aws/describe_polling.py`).
It has since grown well beyond that -- alert lifecycle, threshold tuning,
RBAC v2, rate limiting, session security, reports scoping, the WebSocket
allowlist, and more. Coverage is still uneven: at the last audit 57 of the
109 modules under `app/` were loaded directly by a test via `load_module()`,
and these had no test referencing them at all: `app/aws/resource_discovery.py`,
`app/collector/{baseline_stl,discovery_ec2,ec2_cpu_collector,integrity_check}.py`,
`app/llm/{aws_docs,rca_report,rca_report_pdf}.py`, `app/nlquery/parser.py`,
`app/providers/registry.py`. Route bodies are mostly tested through their
helpers rather than end-to-end (there is no FastAPI `TestClient` fixture).
Before assuming a change is covered, check with:

```
grep -rl 'module_name' tests/
```

### Real-database tests are skipped by default

`tests/test_alert_lifecycle_integration.py` runs against a real MySQL and is
skipped unless `MH_TEST_DB=1` is set (see its docstring for the schema
fixture it loads). A plain `pytest` run therefore reports those as
*skipped*, not passed -- they exercise real SQL and are the only tests that
can catch a query that is valid Python but invalid for the actual schema.
Run them before shipping any change to alert SQL.

### Static guard tests (ratchets)

Three tests parse the source with `ast`/regex instead of running it, to
guard recurring bug classes that stub-based tests cannot see:

| Test file | Guards |
|---|---|
| `test_permission_catalog_drift.py` | every `require_permission("x")` code exists in a migration's permission catalog |
| `test_connection_hygiene_ratchet.py` | no function opens `get_connection()` without a `finally`/`with` release |
| `test_tenant_isolation.py` | every route taking an account/alert/resource id shows a scope check |

They are **ratchets**: each holds a small, commented allowlist of known
exceptions. A NEW violation fails the test; *fixing* an allowlisted one
also fails it until you delete the entry, so the lists can only shrink.
When one fails, fix the code -- only extend an allowlist for a case that is
genuinely intentional, and write the reason next to the entry.

## Keeping stubs honest

These tests match SQL by prefix (`normalized.startswith("SELECT ...")`) and
raise on any query they don't recognise. That strictness is a feature -- it
is what notices when production code changes -- but it means that when a
query is changed, the corresponding test stub must be updated in the same
commit. At the last audit 29 of 154 tests had silently gone red because
account-scoping columns had been added to queries without touching the
stubs; a red suite that nobody trusts is worth nothing. Run `pytest` before
pushing.

## Adding a new test

Follow the pattern in any existing `tests/test_*.py` file: build a
minimal fake DB cursor/connection object exposing an SQL-shape-aware
`execute()`, `install_stub()` everything the target module imports,
`load_module()` the target file, call the function under test directly,
assert on the result.
