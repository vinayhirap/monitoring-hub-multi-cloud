# tests/conftest.py
"""
Shared test infrastructure for this repo's first real test suite.

WHY THIS SHAPE, NOT A MORE STANDARD ONE
------------------------------------------
This codebase has no dependency injection anywhere -- every collector/
alerting module does `from app.db import get_connection` etc. at import
time and calls it directly. There was no test suite before this one
(confirmed: no tests/, no pytest in requirements.txt, nothing -- see
add_test_suite.py's docstring for the full audit). Rather than refactor
production modules just to make them more testable (real risk, no live
server to verify against), these tests use the same technique proven out
live, ad-hoc, throughout the VictoriaMetrics-removal session: load the
target .py file directly via importlib.util.spec_from_file_location
(bypassing package __init__ chains that would otherwise try to import
boto3/google-cloud/azure-identity clients and hit real network calls),
after pre-registering fake modules in sys.modules for exactly the
dependencies that module imports at its top level.

This means each test module gets an ISOLATED copy of the module under
test (a fresh importlib load, not sys.modules-cached), so tests can't
leak state into each other by both importing "the same" collector module
-- each call to load_module() below is a genuinely separate object.

HOW TO USE THIS IN A NEW TEST FILE
--------------------------------------
    from conftest import load_module, install_stub, FakeCursor, FakeConn

    def test_something():
        install_stub("app.db", get_connection=lambda: FakeConn({...}))
        install_stub("app.credentials", load_credential=lambda acct_id: "...")
        mod = load_module("app/providers/azure/metrics_collector.py")
        result = mod.collect_account_metrics({...})
        assert ...

Each test function should call install_stub() for everything the target
module imports at its top level, THEN call load_module() -- imports
happen at exec time, so stubs must be registered first.
"""
import importlib.util
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_module_counter = 0


def load_module(relative_path):
    """
    Load REPO_ROOT/relative_path as a fresh, isolated module object.
    Does NOT go through the file's own package (e.g. app.providers.azure)
    -- that would trigger app/providers/__init__.py's
    `from app.providers import aws/azure/gcp`, which imports boto3/
    google-cloud/azure-identity provider classes with real API-client
    side effects. Loading by path skips all of that; the module's own
    `from app.X import Y` statements still resolve normally against
    sys.modules, which is why install_stub() must run first.
    """
    global _module_counter
    _module_counter += 1
    full_path = os.path.join(REPO_ROOT, relative_path)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"No such file: {full_path} (REPO_ROOT={REPO_ROOT})")
    spec = importlib.util.spec_from_file_location(
        f"_under_test_{_module_counter}_{os.path.basename(relative_path)}", full_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def install_stub(dotted_name, **attrs):
    """
    Create (or update) a fake module at dotted_name in sys.modules, with
    the given attributes set on it, registering any missing parent
    packages along the way (e.g. install_stub("app.clients.vm_client",
    vm_query=...) also creates bare "app" and "app.clients" if absent).
    Returns the module object so callers can further customize it.

    Every module this call actually CREATES (didn't already exist --
    not one it merely mutated attributes on) is marked with a sentinel
    attribute so clear_stubs() can find and remove exactly those later,
    regardless of dotted-name prefix. The sentinel lives ON the module
    object in sys.modules -- a genuine process-wide singleton -- rather
    than in a separate tracking variable in this file, because THIS
    file itself gets imported as two distinct module objects in a
    normal pytest run (once as "conftest" via pytest's own auto-
    discovery, once as "tests.conftest" via every test file's explicit
    `from tests.conftest import ...`) with two separate copies of any
    plain module-level variable. A set-based tracker tried here first
    silently failed for exactly that reason: install_stub calls (via
    the "tests.conftest" copy) populated one set, while the autouse
    clean_sys_modules fixture's clear_stubs() call (bound to whichever
    copy pytest's fixture machinery resolved) read a different, empty
    one -- so nothing was ever actually cleared, and a stub installed
    by one test (e.g. "google.cloud.monitoring_v3" in
    test_gcp_metrics_collector.py) permanently shadowed the real
    "google.cloud" package for every later test needing one of its
    OTHER real submodules (compute_v1 etc, in
    test_gcp_topology_sync.py) -- only reproducible by running both
    test files in the same pytest session, never in isolation. A
    sentinel attribute on the module object itself sidesteps the
    duplicate-module-instance problem entirely, since sys.modules is
    the one piece of state here that's truly shared no matter which
    copy of this file's code is executing.

    This generalizes the old "app.*" prefix-only cleanup this replaced:
    that version was immune to the duplicate-module-instance problem
    (it scanned the live sys.modules directly, no separate tracking
    variable) but only ever cleaned up the "app." tree, leaving any
    stub outside it (google.*, azure.*, ...) to leak for the rest of
    the pytest process.
    """
    parts = dotted_name.split(".")
    for i in range(1, len(parts) + 1):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            stub = types.ModuleType(parent)
            stub._pytest_conftest_stub = True
            sys.modules[parent] = stub
    mod = sys.modules[dotted_name]
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def clear_stubs():
    """
    Remove every fake module install_stub() actually created, found by
    the _pytest_conftest_stub sentinel it sets on each one (see
    install_stub's docstring for why that's a sentinel attribute on the
    shared sys.modules object rather than a separate tracking
    variable). Call from a fixture's teardown (see the
    `clean_sys_modules` fixture below) rather than manually in most
    tests.
    """
    for name in list(sys.modules):
        mod = sys.modules[name]
        if getattr(mod, "_pytest_conftest_stub", False):
            del sys.modules[name]


class FakeCursor:
    """
    A minimal stand-in for a mysql-connector cursor(dictionary=True).
    `script` is a list of (sql_predicate, result) pairs, checked in
    order on each execute() call -- sql_predicate is a callable taking
    the whitespace-normalized SQL string and the params tuple, returning
    True if this entry should answer that call. First match wins. Raises
    AssertionError if no entry matches, so an unexpected query fails
    loudly instead of returning silently wrong data.
    """
    def __init__(self, script):
        self.script = script
        self._pending = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        for predicate, result in self.script:
            if predicate(normalized, params or ()):
                self._pending = result
                return
        raise AssertionError(f"FakeCursor: no script entry matched query: {normalized!r} params={params!r}")

    def fetchone(self):
        rows = self._pending or []
        return rows[0] if rows else None

    def fetchall(self):
        return self._pending or []

    def close(self):
        pass


class FakeConn:
    def __init__(self, script):
        self.script = script

    def cursor(self, dictionary=True):
        return FakeCursor(self.script)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def contains(*substrings):
    """SQL predicate helper: `contains("FROM resources", "resource_type")`
    matches queries containing ALL given substrings (whitespace-normalized)."""
    def predicate(sql, params):
        return all(s in sql for s in substrings)
    return predicate


import pytest


@pytest.fixture(autouse=True)
def clean_sys_modules():
    """
    Runs around EVERY test automatically: clears any app.* stub modules
    left over from a previous test before it starts, and again after it
    finishes. Prevents one test's install_stub() calls from leaking into
    the next test in the same file or a different file.
    """
    clear_stubs()
    yield
    clear_stubs()


def install_polling_modules():
    """Register the REAL, dependency-free app.collector.polling_model and
    app.collector.api_usage modules (pure data / in-memory counters) so a
    module under test can `from app.collector import polling_model` through
    the isolated loader's stub package tree. Returns (polling_model,
    api_usage)."""
    pm = load_module("app/collector/polling_model.py")
    au = load_module("app/collector/api_usage.py")
    sys.modules["app.collector.polling_model"] = pm
    sys.modules["app.collector.api_usage"] = au
    install_stub("app.collector", polling_model=pm, api_usage=au)
    return pm, au
