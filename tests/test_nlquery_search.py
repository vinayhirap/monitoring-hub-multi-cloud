# tests/test_nlquery_search.py
"""
Coverage for app/nlquery/search.py, focused on this audit's fix: a
literal "%" or "_" in the free-text search term (common in real AWS
resource names, e.g. "my_bucket") used to be interpreted as a SQL LIKE
wildcard instead of a literal character, silently over-matching (e.g.
"my_bucket" would also match "myXbucket"). Not a SQL injection risk
either way -- app/nlquery/parser.py's _extract_free_text() already
restricts free_text to [a-zA-Z0-9._-] tokens, and it's always passed as
a bound parameter, never interpolated into the SQL text -- this is a
correctness fix, not a security one.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_db_stub(known_resource_types=None):
    known_resource_types = known_resource_types or ["ec2", "s3"]
    executed = []

    # app/nlquery/search.py does `from app.nlquery.parser import
    # parse_query` at module level -- register the REAL parser.py under
    # its actual dotted path so that import resolves to real parsing
    # logic, not a stub (the whole point of these tests is to exercise
    # the real free_text -> LIKE-pattern escaping end to end).
    real_parser = load_module("app/nlquery/parser.py")
    install_stub("app.nlquery.parser", parse_query=real_parser.parse_query)

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            executed.append((normalized, params))
            if normalized.startswith("SELECT DISTINCT resource_type FROM resources"):
                self._pending = [{"resource_type": t} for t in known_resource_types]
            elif normalized.startswith("SELECT a.id"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return executed


def test_underscore_in_free_text_is_escaped_for_like():
    executed = _install_db_stub()
    mod = load_module("app/nlquery/search.py")

    mod.run_nl_search("show me alerts on my_bucket", {"id": 1, "role": "admin"})

    main_query = next(p for sql, p in executed if sql.startswith("SELECT a.id"))
    # my_bucket -> free_text token "my_bucket" -> LIKE pattern must have
    # the underscore escaped (\\_), not left as a live wildcard.
    like_params = [p for p in main_query if isinstance(p, str) and "bucket" in p]
    assert like_params, f"expected a LIKE param containing 'bucket', got {main_query!r}"
    assert like_params[0] == "%my\\_bucket%", like_params[0]


def test_percent_in_free_text_is_escaped_for_like():
    """parser.py's own tokenizer already can't produce a literal '%' in
    free_text (its regex only captures [a-zA-Z0-9._-] runs, so '%' acts
    as a token separator, not a captured character) -- but run_nl_search
    escapes independently of where free_text comes from, so this
    exercises that escaping directly by substituting a synthetic parsed
    result, proving the defense holds even if a future caller/parser
    change ever lets a literal '%' through."""
    executed = _install_db_stub()
    mod = load_module("app/nlquery/search.py")
    mod.parse_query = lambda text, known_types: {
        "severity": None, "status": None, "resource_type": None, "since_minutes": None,
        "free_text": "cpu100%usage", "interpreted_as": "matching \"cpu100%usage\"",
    }

    mod.run_nl_search("irrelevant, parse_query is stubbed above", {"id": 1, "role": "admin"})

    main_query = next(p for sql, p in executed if sql.startswith("SELECT a.id"))
    like_params = [p for p in main_query if isinstance(p, str) and "cpu100" in p]
    assert like_params, f"expected a LIKE param containing 'cpu100', got {main_query!r}"
    assert like_params[0] == "%cpu100\\%usage%", like_params[0]


def test_plain_free_text_is_unaffected():
    """No escaping needed -> pattern is unchanged from before this fix."""
    executed = _install_db_stub()
    mod = load_module("app/nlquery/search.py")

    mod.run_nl_search("alerts on webserver", {"id": 1, "role": "admin"})

    main_query = next(p for sql, p in executed if sql.startswith("SELECT a.id"))
    like_params = [p for p in main_query if isinstance(p, str) and "webserver" in p]
    assert like_params == ["%webserver%", "%webserver%"]
