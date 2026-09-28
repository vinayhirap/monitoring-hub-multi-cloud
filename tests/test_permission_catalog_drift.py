# tests/test_permission_catalog_drift.py
"""
Schema-drift guard for RBAC permission codes (recurring bug class #3:
"code references a ... permission code that no migration creates").

Every literal passed to `require_permission("<code>")` anywhere under app/
must exist as a row in the permission catalog seeded by db/migrations/*.sql.
A route guarded by a code that was never seeded is not merely
misconfigured -- require_permission() can never grant it to anyone except
the always-passes admin role, so the feature silently becomes admin-only
(or, depending on how a caller resolves unknown codes, unreachable) with
no error at import time and no failing test today.

Pure static analysis: no database, no imports of app code (so it runs in
the same stub-free way as the rest of this suite). It parses the Python
files with `ast` and the SQL files with a regex anchored on the catalog's
row shape -- ('code', 'Category', 'Label', 'Description', ...) -- rather
than accepting any quoted dotted string, so an unrelated string like
'app.main' in a comment can't mask a genuinely missing code.

Also guards the reverse direction loosely: a catalog row that nothing
references is reported by test_every_seeded_code_is_used_or_allowlisted
so dead permissions don't accumulate unnoticed. That second check is an
allowlist-based ratchet (see _KNOWN_UNUSED) -- it starts from today's
reality and only fails on NEW dead codes, or when a listed code becomes
used and should be removed from the list.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

_CATALOG_ROW = re.compile(
    r"^\s*\(\s*'([a-z_]+(?:\.[a-z_]+)+)'\s*,\s*'[^']*'\s*,\s*'", re.M
)


def _read(path):
    # utf-8-sig: a few files in this repo carry a BOM.
    return path.read_text(encoding="utf-8-sig")


def _codes_used_in_code():
    used = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else "")
            if (name == "require_permission" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                used.setdefault(node.args[0].value, []).append(
                    f"{path.relative_to(ROOT)}:{node.lineno}")
    return used


def _codes_seeded_in_migrations():
    seeded = set()
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        seeded |= set(_CATALOG_ROW.findall(_read(path)))
    return seeded


def test_scanner_actually_finds_codes():
    """Guards the guard: if the AST/regex scanners silently matched nothing
    (e.g. after a refactor of how routes declare permissions), the drift
    test below would pass vacuously."""
    assert len(_codes_used_in_code()) >= 30, "require_permission() scanner found suspiciously few codes"
    assert len(_codes_seeded_in_migrations()) >= 30, "migration catalog scanner found suspiciously few codes"


def test_every_permission_code_used_in_code_is_seeded_by_a_migration():
    used = _codes_used_in_code()
    seeded = _codes_seeded_in_migrations()
    missing = {code: where for code, where in used.items() if code not in seeded}
    assert not missing, (
        "require_permission() references permission code(s) that no migration "
        "seeds into the permission catalog -- add them to a db/migrations/*.sql "
        "INSERT into permissions (see 015/041/049 for the row shape):\n"
        + "\n".join(f"  {code}  used at {', '.join(where)}" for code, where in sorted(missing.items()))
    )


# Codes seeded in the permission catalog that no require_permission("<literal>")
# call references today. "Unused here" is not automatically "dead": some are
# consumed indirectly (has_permission(), RBAC v2 bindings, frontend nav
# gating). BUT every code below is also GRANTABLE in the RBAC admin UI, so
# any that are neither checked indirectly nor enforced by a route are
# "phantom" permissions -- granting or revoking them changes nothing.
# Known example at time of writing: alerts.acknowledge / alerts.resolve /
# alerts.suppress are seeded, but ack/resolve/mute routes are guarded by the
# coarser operations.execute, so revoking alerts.acknowledge from a role
# does not stop that role acknowledging alerts. Each should be either wired
# to its route or dropped from the catalog; this list exists so that set can
# only shrink, and so that any NEW seeded-but-unenforced code is a conscious
# decision rather than an accident.
_BASELINE_UNUSED = frozenset({
    "accounts.credentials.manage", "accounts.update", "alerts.acknowledge",
    "alerts.resolve", "alerts.suppress", "audit.export", "dashboard.view",
    "deploy_risk.manage", "incidents.create", "incidents.postmortem.generate",
    "incidents.postmortem.view", "incidents.resolve", "incidents.update",
    "metric_catalog.view", "monitoring.advanced", "op_events.create",
    "organization.settings.update", "organization.settings.view",
    "rbac.policy.manage", "rbac.policy.view", "security.resolve",
    "security.suppress", "sso.manage", "sso.view", "status_page.publish",
    "status_page.view", "synthetic.run", "system.admin", "system.config.manage",
    "system.smtp.manage", "topology.view", "troubleshooting.execute",
    "users.password.reset", "webhooks.manage", "webhooks.view",
})


def test_every_seeded_code_is_used_or_allowlisted():
    unused = set(_codes_seeded_in_migrations()) - set(_codes_used_in_code())
    new_dead = sorted(unused - _BASELINE_UNUSED)
    resurrected = sorted(_BASELINE_UNUSED - unused)
    assert not new_dead, (
        "New permission code(s) seeded but never referenced by a "
        f"require_permission() literal: {new_dead}. Wire them to a route, or if "
        "they are enforced indirectly (has_permission()/RBAC v2 binding), add "
        "them to _BASELINE_UNUSED with a note."
    )
    assert not resurrected, (
        f"These allowlisted codes are now enforced (or no longer seeded): {resurrected}. "
        "Remove them from _BASELINE_UNUSED so the ratchet keeps tightening."
    )
