#!/usr/bin/env python3
"""
add_test_suite.py
========================================
Adds this repo's FIRST test suite. Confirmed before writing anything:
no tests/ directory, no pytest in requirements.txt, no conftest.py, no
test files of any name pattern anywhere in the repo -- genuinely zero
automated test coverage, matching HANDOVER.md's own admission that a
"testing-gaps audit" was never started.

SCOPE, HONESTLY STATED
------------------------
This does NOT attempt comprehensive coverage of the whole app -- that's
a much bigger, separate undertaking (the app has no dependency injection
anywhere, so testing e.g. the FastAPI routes properly would need a real
refactor, not just test files). Instead, this covers the HIGHEST-VALUE,
CONFIRMED-FRAGILE logic: the resource-matching and metric-name-mapping
code this session's own VM-removal work repeatedly found broken or
subtly wrong (GCP's compute_instance numeric-ID bug, the resource_id-
vs-name distinction across Azure/GCP/AWS collectors, the StatusCheckFailed
regression introduced by Phase 5's own patch and only caught by manual
re-investigation afterward). This is exactly the kind of logic most
likely to silently break again on a future change, and least likely to
be caught by "does the service start" smoke-testing alone.

Every test in tests/ is a real, already-passing pytest test -- not a
placeholder. Run `pytest tests/ -v` after this to see for yourself
(19 tests, all passing, confirmed here before shipping).

WHAT THIS ADDS
----------------
  tests/conftest.py                    -- shared test infrastructure
  tests/test_gcp_metrics_collector.py  -- 4 tests (resolver logic)
  tests/test_azure_metrics_collector.py -- 2 tests (write-path)
  tests/test_collector_direct.py       -- 10 tests (chart/list/threshold
                                           local-metrics helpers)
  tests/test_describe_polling.py       -- 3 tests (dual-write)
  pytest.ini                           -- test discovery config
  requirements.txt                     -- adds pytest (dev-only; see
                                           the "why in requirements.txt
                                           not a separate file" note
                                           below)
  tests/README.md                      -- explains the testing approach
                                           given this codebase has no
                                           dependency injection, and how
                                           to extend it

WHY PYTEST GOES IN requirements.txt, NOT A SEPARATE requirements-dev.txt
----------------------------------------------------------------------------
This repo has exactly one requirements file, referenced by name in
setup.sh/deploy.sh/update.sh's `pip install -r requirements.txt` step.
Splitting into requirements.txt + requirements-dev.txt would need those
3 scripts updated too, and pytest itself is small and harmless to have
installed on a production box (it doesn't run anything just by being
present). Simpler and lower-risk to add it to the one file that already
exists and is already wired everywhere it needs to be.

NOT DONE HERE, DELIBERATELY: CI wiring. No .github/workflows or any
other CI config exists in this repo at all -- setting one up is a real
decision (which runner, what secrets a job would need for DB-backed
tests, whether GitHub Actions is even the intended platform) this
script shouldn't make unilaterally. Running `pytest tests/` manually
before pushing, or wiring it into whatever CI you choose later, both
work fine with what's added here.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 add_test_suite.py --dry-run
    python3 add_test_suite.py --apply
    pip install -r requirements.txt   # picks up pytest
    pytest tests/ -v                  # should show 19 passed
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

REQUIREMENTS_ANCHOR_OLD = '''# Credential encryption-at-rest (Azure client secret / GCP SA key JSON)
cryptography>=42,<44'''

REQUIREMENTS_ANCHOR_NEW = '''# Credential encryption-at-rest (Azure client secret / GCP SA key JSON)
cryptography>=42,<44

# Testing (tests/ -- this repo's first test suite, see add_test_suite.py).
# Small and harmless to have installed in production; not worth splitting
# into a separate requirements-dev.txt when only one file is wired into
# setup.sh/deploy.sh/update.sh's pip install step.
pytest>=8,<9'''

PYTEST_INI = """[pytest]
testpaths = tests
python_files = test_*.py
"""

TESTS_README = """# tests/

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
"""


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    tests_src = os.path.join(script_dir, "tests")
    tests_dst = os.path.join(repo_root, "tests")
    if not os.path.isdir(tests_src):
        die(f"Expected a tests/ folder alongside this script at {tests_src} -- "
            f"make sure you copied the whole tests/ directory, not just this .py file.")

    req_path = os.path.join(repo_root, "requirements.txt")
    with open(req_path) as f:
        req_content = f.read()

    plan = []
    if "pytest" in req_content:
        req_needs_patch = False
        plan.append("requirements.txt: pytest already present -- skipping")
    else:
        if REQUIREMENTS_ANCHOR_OLD not in req_content:
            die("requirements.txt doesn't match expected anchor text -- won't guess where to insert pytest.")
        req_needs_patch = True
        plan.append("requirements.txt: add pytest>=8,<9")

    pytest_ini_path = os.path.join(repo_root, "pytest.ini")
    pytest_ini_exists = os.path.isfile(pytest_ini_path)
    plan.append(f"pytest.ini: {'already exists -- skipping' if pytest_ini_exists else 'will create'}")

    tests_dir_exists = os.path.isdir(tests_dst)
    plan.append(f"tests/: {'already exists at destination -- will overwrite files individually' if tests_dir_exists else 'will create'}")

    readme_path = os.path.join(tests_dst, "README.md")
    plan.append(f"tests/README.md: {'exists -- will overwrite' if os.path.isfile(readme_path) else 'will create'}")

    print("\nPlan:")
    for p in plan:
        print(f"  {p}")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    os.makedirs(tests_dst, exist_ok=True)
    for fname in os.listdir(tests_src):
        if not fname.endswith(".py"):
            continue
        src_path = os.path.join(tests_src, fname)
        dst_path = os.path.join(tests_dst, fname)
        if os.path.isfile(dst_path):
            backup(dst_path)
        shutil.copy2(src_path, dst_path)
        print(f"Copied tests/{fname}")

    with open(readme_path, "w") as f:
        f.write(TESTS_README)
    print("Wrote tests/README.md")

    if not pytest_ini_exists:
        with open(pytest_ini_path, "w") as f:
            f.write(PYTEST_INI)
        print("Wrote pytest.ini")

    if req_needs_patch:
        backup(req_path)
        with open(req_path, "w") as f:
            f.write(req_content.replace(REQUIREMENTS_ANCHOR_OLD, REQUIREMENTS_ANCHOR_NEW, 1))
        print("Patched requirements.txt")

    print("""
[Manual follow-up]

  A) Install pytest and run the suite:
       source /opt/monitoring-hub/venv/bin/activate  # or use the venv's pip/python directly
       pip install -r requirements.txt
       pytest tests/ -v
     Expect: 19 passed. If anything fails here but passed in development,
     it's almost certainly a Python version or installed-package-version
     difference -- worth checking before assuming the test is wrong.

  B) No restart needed -- this doesn't touch any code the running
     service imports, only adds new files and a requirements.txt line.

  C) Review, commit, push:
       git status
       git add tests/ pytest.ini requirements.txt add_test_suite.py
       git commit -m "test: add this repo's first test suite -- covers the resource-matching/metric-mapping logic the VM-removal session found repeatedly fragile (GCP numeric-ID bug, resource_id-vs-name distinction, the StatusCheckFailed regression). 19 tests, all passing."
       git push origin main

  D) Next steps, not done here: wiring `pytest tests/` into CI (no CI
     config exists in this repo at all -- a real platform/secrets
     decision, not made here), and extending coverage into
     app/api/*.py routes (would benefit from FastAPI's TestClient and
     a real test-database fixture, neither of which exist yet).
""")


if __name__ == "__main__":
    main()
