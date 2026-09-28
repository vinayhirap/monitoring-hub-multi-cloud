# tests/test_connection_hygiene_ratchet.py
"""
Guards recurring bug class #2: "DB connections opened without try/finally
exhausting the 10-connection pool (-> 500s on login)".

A function that calls get_connection() must guarantee the connection goes
back to the pool on EVERY exit path -- including the exception path -- by
either:
  * closing it in a `finally:` block, or
  * using a `with` block (get_db_cursor() / a context-managed connection), or
  * being a factory that returns the connection to its own caller.
A straight-line `conn = get_connection(); ...; conn.close()` leaks the
connection the first time anything between those two lines raises, and a
leak of just 10 of them takes login down.

This is a static (ast) check -- no DB, no imports of app code -- so it runs
in the same environment as the rest of this suite and cannot be defeated by
stubs. It is deliberately a RATCHET:

  * A NEW offender fails the test immediately.
  * Fixing an allowlisted offender ALSO fails the test until it is removed
    from _ALLOWED, so the list can only shrink.

The scanner is itself unit-tested below on small source snippets so a
refactor of the heuristic can't silently make the ratchet pass vacuously.
"""
import ast
import pathlib
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Modules that DEFINE the connection pool / factory rather than consume it.
_EXCLUDED_FILES = {"app/db.py"}

# "path::function" -> why this one is intentionally exempt.
_ALLOWED = {
    "app/collector/leader.py::_loop": (
        "Holds ONE long-lived connection for the process lifetime on purpose: "
        "MySQL GET_LOCK() is session-scoped, so closing this connection would "
        "release the leader lock. Cleanup on loss is handled by its own except "
        "path (leader_event.clear())."
    ),
}


def _own_nodes(fn):
    """All nodes in fn's body, excluding nested function/lambda bodies."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _is_call(node, name):
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == name) or (
        isinstance(f, ast.Attribute) and f.attr == name)


def find_leaky_functions(source: str):
    """Return [(function_name, lineno_of_get_connection)] for functions that
    open a connection but do not guarantee it is released on all paths."""
    tree = ast.parse(source)
    leaks = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_own_nodes(fn))
        opens = [n for n in nodes if _is_call(n, "get_connection")]
        if not opens:
            continue
        if any(isinstance(n, ast.With) for n in nodes):
            continue  # with-managed
        released_in_finally = any(
            isinstance(n, ast.Try) and n.finalbody and any(
                _is_call(x, "close") for b in n.finalbody for x in ast.walk(b))
            for n in nodes
        )
        if released_in_finally:
            continue
        returns_conn = any(
            isinstance(n, ast.Return) and n.value is not None and any(
                _is_call(x, "get_connection") for x in ast.walk(n.value))
            for n in nodes
        )
        if returns_conn:
            continue  # factory: caller owns the connection
        leaks.append((fn.name, opens[0].lineno))
    return leaks


def _scan_repo():
    found = set()
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in _EXCLUDED_FILES:
            continue
        try:
            src = path.read_text(encoding="utf-8-sig")
            leaks = find_leaky_functions(src)
        except SyntaxError:
            continue
        found |= {f"{rel}::{name}" for name, _ in leaks}
    return found


# ── the ratchet itself ──────────────────────────────────────────────

def test_no_new_functions_leak_a_connection_on_the_exception_path():
    new = sorted(_scan_repo() - set(_ALLOWED))
    assert not new, (
        "These functions call get_connection() without guaranteeing release on "
        "the exception path. Wrap the work in try/finally: conn.close() (or use "
        "get_db_cursor()):\n  " + "\n  ".join(new)
    )


def test_allowlist_has_no_stale_entries():
    stale = sorted(set(_ALLOWED) - _scan_repo())
    assert not stale, (
        f"These allowlisted functions no longer leak (or no longer exist): {stale}. "
        "Delete them from _ALLOWED so the ratchet keeps tightening."
    )


# ── unit tests for the scanner (so the ratchet can't pass vacuously) ─

def _leaks(code):
    return [n for n, _ in find_leaky_functions(textwrap.dedent(code))]


def test_scanner_flags_straight_line_open_and_close():
    assert _leaks("""
        def f():
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            conn.close()
    """) == ["f"]


def test_scanner_accepts_try_finally_close():
    assert _leaks("""
        def f():
            conn = get_connection()
            try:
                conn.cursor().execute("SELECT 1")
            finally:
                conn.close()
    """) == []


def test_scanner_accepts_with_block():
    assert _leaks("""
        def f():
            with get_db_cursor() as cur:
                cur.execute("SELECT 1")
        def g():
            with get_connection() as conn:
                pass
    """) == []


def test_scanner_accepts_a_factory_that_returns_the_connection():
    assert _leaks("""
        def make():
            return get_connection()
    """) == []


def test_scanner_does_not_credit_a_close_that_is_not_in_finally():
    # A close() in an except branch or after the try still leaks on the
    # happy-path-then-later-raise and on un-caught exception types.
    assert _leaks("""
        def f():
            conn = get_connection()
            try:
                work(conn)
            except ValueError:
                conn.close()
    """) == ["f"]
