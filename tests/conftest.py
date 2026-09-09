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
    """
    parts = dotted_name.split(".")
    for i in range(1, len(parts) + 1):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)
    mod = sys.modules[dotted_name]
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def clear_stubs():
    """
    Remove every fake module this helper may have installed, so tests in
    different files don't see each other's stale stubs. Call from a
    fixture's teardown (see the `clean_sys_modules` fixture below) rather
    than manually in most tests.
    """
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
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
