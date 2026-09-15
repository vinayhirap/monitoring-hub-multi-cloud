"""
app/auth/rbac.py

RBAC v2 resolver: the single place that answers

    "may THIS principal perform THIS permission on THIS target?"

where a target is a (cloud, account, region, service, resource) tuple
rather than the whole system.

RELATIONSHIP TO THE EXISTING MODULES
  app/auth/deps.py         -- authentication (who are you), unchanged.
  app/auth/permissions.py  -- v1 GLOBAL permission check. Still the
                              right tool for endpoints with no target
                              (GET /api/permissions/me, the user list).
  app/auth/authorization.py-- v1 scope resolver. Still authoritative
                              for access_scopes/group_policies and the
                              scope_within delegation gate; this module
                              reads through to it for users who have no
                              v2 bindings yet (see _legacy_grants).
  THIS MODULE              -- v2. Binds role to scope, adds the service
                              dimension, and produces query filters.

WHY A SECOND MODULE INSTEAD OF EDITING authorization.py
authorization.py is imported by 16 API modules and its semantics are
load-bearing for a production deployment. Changing get_effective_scope
in place would alter behaviour for every one of them simultaneously,
which is exactly the kind of big-bang authorization change that ships
a silent data leak. v2 lands alongside it, routers migrate one at a
time, and authorization.py is deleted only once nothing imports it.

THE TWO-STAGE ENFORCEMENT RULE
Every protected endpoint does BOTH of:

  Stage 1 -- ROUTE GATE. "Does this user hold <permission> anywhere at
             all?" Cheap, answered from the resolved binding set, 403s
             early. Depends(require_permission_v2("alerts.resolve")).

  Stage 2 -- OBJECT GATE / ROW FILTER. Either the endpoint loads one
             object and calls assert_can(user, perm, target), or it
             lists many and applies accessible_filter(user) to the
             query.

Stage 1 alone is what the v1 code does, and it is why a viewer scoped
to "ec2 in ap-south-1" can currently read RDS metrics from us-east-1:
every endpoint checked the permission and then queried unfiltered by
region or service. Stage 2 is not optional. accessible_filter returns
all three dimensions together specifically so a caller cannot filter by
account and silently forget the other two.

DECISION PRECEDENCE (evaluated in this order, first match wins)
  1. An explicit DENY override matching the permission and covering the
     target  ->  DENIED. Nothing overrides a deny, including a global
     admin binding. Same precedence as an AWS IAM explicit deny.
  2. Any binding whose role grants the permission AND whose scope
     covers the target  ->  ALLOWED.
  3. Otherwise  ->  DENIED (deny by default, unchanged from v1).
"""
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Iterable

from fastapi import Depends, HTTPException

from app.db import get_connection
from app.auth.deps import get_current_user

logger = logging.getLogger(__name__)

# How long a resolved principal's binding set is cached in-process.
# Authorization data changes rarely (a grant, a group move) but is read
# on essentially every request; without this the v1 pattern of opening
# a fresh pooled connection inside get_role_permissions() on every
# permission check is a measurable share of request latency and pool
# pressure (see app/db.py's Sep 5 2026 pool-exhaustion writeup).
# invalidate_principal() is called by every mutating RBAC endpoint, so
# the TTL is a backstop against a missed invalidation, not the primary
# correctness mechanism.
_CACHE_TTL_SECONDS = 60

_cache: dict = {}
_cache_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────
# Scope model
# ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Target:
    """
    What an action is being performed ON. Every field optional: an
    endpoint that only knows the account passes just that, and the
    scope check simply has fewer dimensions to discriminate on.

    A None field on the TARGET means "not specified by the caller", and
    is treated as NOT restricting the match -- it is the caller's job
    to pass everything it knows. An endpoint that knows the region but
    omits it gets a more permissive answer than it should, which is why
    assert_can() logs at DEBUG when a target is unusually sparse.
    """
    cloud: Optional[str] = None
    account_id: Optional[int] = None
    region: Optional[str] = None
    service: Optional[str] = None          # 'ec2', 'rds', ... resources.resource_type
    resource_id: Optional[str] = None
    resource_group: Optional[str] = None
    tags: Optional[dict] = None            # {"Environment": "prod", ...}


@dataclass
class Scope:
    """
    A scope row (rbac_scopes). None/[] on a dimension = unrestricted at
    that dimension. Dimensions nest cloud > account > region > service
    > resource, but each is matched independently -- a scope may pin a
    service without pinning a region ("RDS everywhere in prod").
    """
    id: Optional[int] = None
    label: Optional[str] = None
    cloud: Optional[str] = None
    account_ref_id: Optional[int] = None
    regions: Optional[list] = None
    services: Optional[list] = None
    resource_groups: Optional[list] = None
    resource_ids: Optional[list] = None
    tag_selector: Optional[dict] = None

    def is_global(self) -> bool:
        return not any([
            self.cloud, self.account_ref_id, self.regions, self.services,
            self.resource_groups, self.resource_ids, self.tag_selector,
        ])

    def covers(self, target: Target) -> bool:
        """
        True if this scope's boundary contains `target`.

        The asymmetry that matters: an unrestricted scope dimension
        covers any target value INCLUDING None, but a restricted scope
        dimension does NOT cover a target that leaves it None. That is
        deliberate. If the scope says "only ap-south-1" and the caller
        cannot say which region the object is in, the safe answer is
        no. Silently allowing it is how region scoping ends up
        unenforced -- which is the v1 bug this module exists to fix.
        """
        if self.cloud and target.cloud != self.cloud:
            return False
        if self.account_ref_id is not None and target.account_id != self.account_ref_id:
            return False
        if self.regions and target.region not in self.regions:
            return False
        if self.services and target.service not in self.services:
            return False
        if self.resource_groups and target.resource_group not in self.resource_groups:
            return False
        if self.resource_ids and target.resource_id not in self.resource_ids:
            return False
        if self.tag_selector:
            tags = target.tags or {}
            # ANDed across keys, ORed within a key -- AWS IAM
            # aws:ResourceTag semantics.
            for key, allowed in self.tag_selector.items():
                allowed_list = allowed if isinstance(allowed, list) else [allowed]
                if tags.get(key) not in allowed_list:
                    return False
        return True


@dataclass
class Binding:
    """One resolved (role -> permissions) at (scope), plus provenance."""
    role_key: str
    role_rank: int
    permissions: frozenset
    scope: Scope
    source: str = "user"                   # "user" | "group" | "legacy"
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    group_level: Optional[str] = None
    binding_id: Optional[int] = None
    expires_at: Optional[str] = None


@dataclass
class Denial:
    permission_code: str
    scope: Optional[Scope] = None          # None = everywhere


@dataclass
class ResolvedAccess:
    user_id: int
    bindings: list = field(default_factory=list)
    denials: list = field(default_factory=list)

    def all_permissions(self) -> set:
        """Every permission held at ANY scope -- the stage 1 route gate."""
        out = set()
        for b in self.bindings:
            out |= b.permissions
        return out

    def is_global_admin(self) -> bool:
        return any(
            b.role_key == "admin" and b.scope.is_global()
            for b in self.bindings
        )


# ─────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────

def _json_list(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value or None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed or None


def _scope_from_row(row, prefix="") -> Scope:
    g = lambda k: row.get(prefix + k)
    return Scope(
        id=g("id"),
        label=g("label"),
        cloud=g("cloud"),
        account_ref_id=g("account_ref_id"),
        regions=_json_list(g("regions")),
        services=_json_list(g("services")),
        resource_groups=_json_list(g("resource_groups")),
        resource_ids=_json_list(g("resource_ids")),
        tag_selector=_json_list(g("tag_selector")),
    )


def _principal_group_ids(conn, user_id: int) -> set:
    """
    Every group whose bindings apply to this user: groups they are a
    direct member of, plus every ANCESTOR of those groups.

    Ancestor inclusion mirrors app/auth/authorization.get_effective_scope
    exactly -- membership in an L3 pulls in the L2 and L1 above it,
    additively. Reimplemented here rather than imported because v2
    caches the whole resolution and wants one connection for all of it;
    the traversal is the same three-level walk and is covered by a
    parity test (tests/test_rbac_v2.py::test_group_inheritance_matches_v1).
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT group_id FROM user_group_memberships WHERE user_id = %s",
        (user_id,),
    )
    direct = [r["group_id"] for r in cursor.fetchall()]

    all_ids = set()
    for gid in direct:
        current, seen = gid, set()
        while current is not None and current not in seen:
            seen.add(current)
            all_ids.add(current)
            cursor.execute(
                "SELECT parent_group_id FROM org_groups WHERE id = %s", (current,)
            )
            row = cursor.fetchone()
            current = row["parent_group_id"] if row else None
    cursor.close()
    return all_ids


def _role_permission_map(conn) -> dict:
    """{role_id: frozenset(codes)} for every role, in one query."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT rpv.role_id, p.code FROM role_permissions_v2 rpv "
        "JOIN permissions p ON p.id = rpv.permission_id"
    )
    out = {}
    for r in cursor.fetchall():
        out.setdefault(r["role_id"], set()).add(r["code"])
    cursor.close()
    return {k: frozenset(v) for k, v in out.items()}


def _legacy_grants(conn, user_id: int, role: str, perms_by_role_key: dict) -> list:
    """
    Compatibility shim. A user with no v2 role_bindings rows is
    resolved from their v1 users.role + access_scopes rows, producing
    the same effective access they have today.

    This is what makes 040 deployable without a coordinated data
    migration: bindings are created per user as administrators get to
    them, and until then nothing about that user's access changes. A
    user with even ONE v2 binding is resolved purely from v2 -- mixing
    the two for the same principal would make "why does Priya have
    this" unanswerable.
    """
    grants = []
    perms = perms_by_role_key.get(role, frozenset())

    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, cloud, account_ref_id, regions, resource_groups, "
        "resource_types, resource_ids FROM access_scopes WHERE user_id = %s",
        (user_id,),
    )
    rows = cursor.fetchall()

    group_ids = _principal_group_ids(conn, user_id)
    if group_ids:
        placeholders = ",".join(["%s"] * len(group_ids))
        cursor.execute(
            f"SELECT gp.id, gp.cloud, gp.account_ref_id, gp.regions, "
            f"gp.resource_groups, gp.resource_types, gp.resource_ids, "
            f"og.id AS gid, og.name AS gname, og.level AS glevel "
            f"FROM group_policies gp JOIN org_groups og ON og.id = gp.group_id "
            f"WHERE gp.group_id IN ({placeholders})",
            tuple(group_ids),
        )
        rows += cursor.fetchall()
    cursor.close()

    for r in rows:
        # v1's resource_types column IS the service dimension -- it was
        # simply never enforced. Mapping it onto Scope.services here
        # means a pre-existing v1 grant starts being honoured as a
        # service restriction the moment v2 enforcement is switched on.
        # That is a deliberate TIGHTENING: any v1 grant that named
        # resource_types was always intended to restrict, so honouring
        # it is correcting the bug, not changing the policy. Flagged in
        # the rollout notes because it is the one behaviour change in
        # this migration that can take access away from someone.
        scope = Scope(
            id=r["id"],
            cloud=r["cloud"],
            account_ref_id=r["account_ref_id"],
            regions=_json_list(r["regions"]),
            services=_json_list(r.get("resource_types")),
            resource_groups=_json_list(r["resource_groups"]),
            resource_ids=_json_list(r["resource_ids"]),
        )
        grants.append(Binding(
            role_key=role,
            role_rank={"viewer": 10, "editor": 20, "admin": 30}.get(role, 0),
            permissions=perms,
            scope=scope,
            source="group" if "gid" in r else "legacy",
            group_id=r.get("gid"),
            group_name=r.get("gname"),
            group_level=r.get("glevel"),
        ))

    # An admin with no access_scopes rows has always meant "everywhere"
    # (v1 short-circuits on role == 'admin' before ever reading the
    # table). Preserve that exactly, or every existing admin locks
    # themselves out the moment v2 enforcement turns on.
    if role == "admin" and not grants:
        grants.append(Binding(
            role_key="admin", role_rank=30,
            permissions=perms or frozenset(_all_permission_codes(conn)),
            scope=Scope(label="Organization (legacy admin)"),
            source="legacy",
        ))
    return grants


def _all_permission_codes(conn) -> set:
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT code FROM permissions")
    out = {r["code"] for r in cursor.fetchall()}
    cursor.close()
    return out


def _load(user: dict) -> ResolvedAccess:
    """Uncached full resolution for one user. One connection, ~6 queries."""
    user_id = user["id"]
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id, role_key, role_rank FROM roles")
        role_rows = cursor.fetchall()
        role_by_id = {r["id"]: r for r in role_rows}
        perms_by_role_id = _role_permission_map(conn)
        perms_by_role_key = {
            r["role_key"]: perms_by_role_id.get(r["id"], frozenset()) for r in role_rows
        }

        group_ids = _principal_group_ids(conn, user_id)

        # Bindings for this user directly, plus for any group they are
        # in or beneath. Expired bindings are filtered in SQL so an
        # expiry can never be forgotten by a caller.
        params = [user_id]
        clause = "(rb.principal_type = 'user' AND rb.principal_id = %s)"
        if group_ids:
            placeholders = ",".join(["%s"] * len(group_ids))
            clause += f" OR (rb.principal_type = 'group' AND rb.principal_id IN ({placeholders}))"
            params += list(group_ids)

        cursor.execute(
            f"""
            SELECT rb.id AS binding_id, rb.role_id, rb.principal_type,
                   rb.principal_id, rb.expires_at,
                   s.id, s.label, s.cloud, s.account_ref_id, s.regions,
                   s.services, s.resource_groups, s.resource_ids, s.tag_selector,
                   og.name AS group_name, og.level AS group_level
            FROM role_bindings rb
            JOIN rbac_scopes s ON s.id = rb.scope_id
            LEFT JOIN org_groups og
                   ON og.id = rb.principal_id AND rb.principal_type = 'group'
            WHERE ({clause})
              AND (rb.expires_at IS NULL OR rb.expires_at > NOW())
            """,
            tuple(params),
        )
        binding_rows = cursor.fetchall()

        bindings = []
        for r in binding_rows:
            role = role_by_id.get(r["role_id"])
            if not role:
                continue
            bindings.append(Binding(
                role_key=role["role_key"],
                role_rank=role["role_rank"],
                permissions=perms_by_role_id.get(r["role_id"], frozenset()),
                scope=_scope_from_row(r),
                source="group" if r["principal_type"] == "group" else "user",
                group_id=r["principal_id"] if r["principal_type"] == "group" else None,
                group_name=r["group_name"],
                group_level=r["group_level"],
                binding_id=r["binding_id"],
                expires_at=str(r["expires_at"]) if r["expires_at"] else None,
            ))

        if not bindings:
            bindings = _legacy_grants(conn, user_id, user.get("role"), perms_by_role_key)

        # Deny overrides.
        cursor.execute(
            f"""
            SELECT p.code, po.effect,
                   s.id, s.label, s.cloud, s.account_ref_id, s.regions,
                   s.services, s.resource_groups, s.resource_ids, s.tag_selector
            FROM permission_overrides po
            JOIN permissions p ON p.id = po.permission_id
            LEFT JOIN rbac_scopes s ON s.id = po.scope_id
            WHERE po.effect = 'deny'
              AND ({clause.replace('rb.', 'po.')})
              AND (po.expires_at IS NULL OR po.expires_at > NOW())
            """,
            tuple(params),
        )
        denials = [
            Denial(
                permission_code=r["code"],
                scope=_scope_from_row(r) if r["id"] is not None else None,
            )
            for r in cursor.fetchall()
        ]

        cursor.close()
        return ResolvedAccess(user_id=user_id, bindings=bindings, denials=denials)
    finally:
        conn.close()


def resolve(user: dict) -> ResolvedAccess:
    """Cached entry point. Everything else in this module goes through here."""
    key = user["id"]
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    resolved = _load(user)
    with _cache_lock:
        _cache[key] = (now + _CACHE_TTL_SECONDS, resolved)
    return resolved


def invalidate_principal(user_id: Optional[int] = None) -> None:
    """
    Drop cached resolution. Called by every RBAC mutation endpoint.

    A GROUP change (policy edit, membership change, reparenting) must
    call this with user_id=None -- the blast radius of a group edit is
    every member of every descendant group, and computing that set just
    to invalidate precisely is more expensive and more fragile than
    dropping the whole cache. Authorization caches are small and
    refill in one request.
    """
    with _cache_lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


# ─────────────────────────────────────────────────────────────────────
# Decisions
# ─────────────────────────────────────────────────────────────────────

def can(user: dict, permission: str, target: Optional[Target] = None) -> bool:
    """
    THE authorization question. See DECISION PRECEDENCE in the module
    docstring: deny first, then any covering allow, else deny.
    """
    access = resolve(user)
    target = target or Target()

    for d in access.denials:
        if d.permission_code != permission:
            continue
        if d.scope is None or d.scope.covers(target):
            return False

    for b in access.bindings:
        if permission in b.permissions and b.scope.covers(target):
            return True
    return False


def assert_can(user: dict, permission: str, target: Optional[Target] = None) -> None:
    """403 if not permitted. The stage 2 object gate."""
    if not can(user, permission, target):
        raise HTTPException(
            status_code=403,
            detail=f"Not permitted: {permission} on the requested resource",
        )


def require_permission_v2(code: str):
    """
    Stage 1 route gate -- 403s unless the user holds `code` at SOME
    scope. Drop-in replacement for permissions.require_permission,
    with the same 401-then-403 ordering.

    This alone does NOT authorize access to any particular object. An
    endpoint that returns data MUST also apply accessible_filter() or
    assert_can(). See the module docstring.
    """
    def _check(user: dict = Depends(get_current_user)) -> dict:
        access = resolve(user)
        if code not in access.all_permissions():
            raise HTTPException(status_code=403, detail=f"Missing permission: {code}")
        # A deny that applies everywhere short-circuits here too, so an
        # unconditional deny does not require every endpoint to
        # remember the stage 2 call to take effect.
        for d in access.denials:
            if d.permission_code == code and d.scope is None:
                raise HTTPException(status_code=403, detail=f"Denied permission: {code}")
        return user
    return _check


# ─────────────────────────────────────────────────────────────────────
# Query filters (stage 2, list endpoints)
# ─────────────────────────────────────────────────────────────────────

@dataclass
class AccessFilter:
    """
    What a list query must be narrowed to. `unrestricted` short-circuits
    everything; otherwise apply ALL THREE of account/region/service.

    A None on one of the three sets means "that dimension is
    unrestricted" and a set means "exactly these". Empty set means NO
    access on that dimension -- callers must distinguish None from
    set(), the same trap v1's get_accessible_account_ids documents.
    """
    unrestricted: bool = False
    account_ids: Optional[set] = None
    regions_by_account: Optional[dict] = None     # {account_id: {region,...} or None}
    services: Optional[set] = None
    resource_ids: Optional[set] = None

    def is_empty(self) -> bool:
        return not self.unrestricted and self.account_ids is not None and not self.account_ids

    def sql(self, alias: str = "r", account_col: str = "aws_account_id"):
        """
        Returns (where_fragment, params) to AND into a query over a
        table with account/region/resource_type columns (`resources`,
        and every view built on it).

        Returning SQL rather than making callers hand-roll the IN
        clauses is the point: the v1 helpers returned bare sets, every
        caller wrote its own filter, and the region/service dimensions
        were simply left out of most of them. One fragment, three
        dimensions, no opportunity to forget one.
        """
        if self.unrestricted:
            return "1=1", []
        if self.is_empty():
            return "1=0", []

        clauses, params = [], []

        if self.account_ids is not None:
            placeholders = ",".join(["%s"] * len(self.account_ids))
            clauses.append(f"{alias}.{account_col} IN ({placeholders})")
            params += sorted(self.account_ids)

        if self.services is not None:
            if not self.services:
                return "1=0", []
            placeholders = ",".join(["%s"] * len(self.services))
            clauses.append(f"{alias}.resource_type IN ({placeholders})")
            params += sorted(self.services)

        if self.regions_by_account:
            # Per-account region lists: "all regions in dev, only
            # ap-south-1 in prod" is a normal ask and a single flat
            # region IN (...) would wrongly allow ap-south-1's peers in
            # dev, or wrongly block dev entirely.
            per_account = []
            for acct_id, regions in self.regions_by_account.items():
                if regions is None:
                    per_account.append(f"({alias}.{account_col} = %s)")
                    params.append(acct_id)
                elif regions:
                    placeholders = ",".join(["%s"] * len(regions))
                    per_account.append(
                        f"({alias}.{account_col} = %s AND {alias}.region IN ({placeholders}))"
                    )
                    params.append(acct_id)
                    params += sorted(regions)
            if per_account:
                clauses.append("(" + " OR ".join(per_account) + ")")

        return (" AND ".join(clauses) if clauses else "1=1"), params


def accessible_filter(user: dict, permission: Optional[str] = None) -> AccessFilter:
    """
    Collapse a principal's bindings into a query filter.

    `permission` narrows to bindings that actually grant that
    permission -- so a user who is Editor on prod and Viewer on dev
    gets both accounts for 'alerts.view' but only prod for
    'alerts.resolve'. Omitting it means "any binding", which is almost
    never what a data endpoint wants; pass it.
    """
    access = resolve(user)
    relevant = [
        b for b in access.bindings
        if permission is None or permission in b.permissions
    ]

    if not relevant:
        return AccessFilter(account_ids=set())

    if any(b.scope.is_global() for b in relevant):
        return AccessFilter(unrestricted=True)

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, provider FROM aws_accounts")
        accounts = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    account_ids = set()
    regions_by_account: dict = {}
    services: Optional[set] = set()
    resource_ids: Optional[set] = set()

    for b in relevant:
        s = b.scope
        if s.account_ref_id is not None:
            matched = [s.account_ref_id]
        else:
            # Cloud-wide (or org-wide) grant: expand to the concrete
            # account ids so the caller always gets ids, never a
            # provider string it would have to re-resolve.
            matched = [
                a["id"] for a in accounts
                if s.cloud is None or a["provider"] == s.cloud
            ]

        for acct_id in matched:
            account_ids.add(acct_id)
            if not s.regions:
                regions_by_account[acct_id] = None      # all regions
            elif regions_by_account.get(acct_id, "unset") is not None:
                existing = regions_by_account.get(acct_id) or set()
                regions_by_account[acct_id] = set(existing) | set(s.regions)

        if services is not None:
            if not s.services:
                services = None                          # any one unrestricted grant wins
            else:
                services |= set(s.services)

        if resource_ids is not None:
            if not s.resource_ids:
                resource_ids = None
            else:
                resource_ids |= set(s.resource_ids)

    return AccessFilter(
        account_ids=account_ids,
        regions_by_account=regions_by_account,
        services=services,
        resource_ids=resource_ids,
    )


def accessible_services(user: dict, account_id: Optional[int] = None,
                        permission: Optional[str] = None) -> Optional[set]:
    """
    Service keys this user may see, optionally within one account.
    None = unrestricted. Backs the service pickers in the UI and the
    'which tabs do I render' question on the resource detail page.

    There was no v1 equivalent of this function -- the service
    dimension was stored and never read.
    """
    access = resolve(user)
    out: Optional[set] = set()
    for b in access.bindings:
        if permission is not None and permission not in b.permissions:
            continue
        s = b.scope
        if account_id is not None and s.account_ref_id is not None \
                and s.account_ref_id != account_id:
            continue
        if not s.services:
            return None
        if out is not None:
            out |= set(s.services)
    return out


def accessible_regions(user: dict, account_id: int,
                       permission: Optional[str] = None) -> Optional[set]:
    """
    Regions this user may see within one account. None = unrestricted.

    The v1 counterpart (authorization.get_accessible_regions_for_account)
    exists but has zero callers anywhere in the codebase -- region scope
    has never actually been enforced. Endpoints migrating to v2 should
    prefer accessible_filter().sql(), which cannot be partially applied.
    """
    f = accessible_filter(user, permission)
    if f.unrestricted:
        return None
    if account_id not in (f.account_ids or set()):
        return set()
    return (f.regions_by_account or {}).get(account_id)


# ─────────────────────────────────────────────────────────────────────
# Delegation
# ─────────────────────────────────────────────────────────────────────

def can_grant(actor: dict, role_key: str, scope: Scope) -> bool:
    """
    The privilege-escalation gate for creating a binding. An actor may
    grant (role, scope) only if BOTH:

      1. they hold a role of equal or higher rank somewhere whose scope
         COVERS the scope they are handing out, and
      2. they hold rbac.binding.manage at that same scope.

    Condition 1 is the containment check v1 did via scope_within; the
    difference is that rank is now checked against the specific binding
    that supplies the coverage, not against a global role column. An
    Editor on prod cannot mint an Admin on prod, and an Admin on dev
    cannot mint anything at all on prod.
    """
    access = resolve(actor)
    target_rank = _role_rank(role_key)

    for b in access.bindings:
        if b.role_rank < target_rank:
            continue
        if "rbac.binding.manage" not in b.permissions:
            continue
        if _scope_contains(b.scope, scope):
            return True
    return False


def _role_rank(role_key: str) -> int:
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT role_rank FROM roles WHERE role_key = %s", (role_key,))
        row = cursor.fetchone()
        cursor.close()
        return row["role_rank"] if row else 0
    finally:
        conn.close()


def _list_contains(outer: Optional[list], inner: Optional[list]) -> bool:
    """
    outer unrestricted (None/[]) covers anything. Otherwise inner must
    be a non-empty subset -- requesting "unrestricted" on a dimension
    the actor themselves has pinned IS the escalation. Same rule as
    authorization._covers; kept as a separate function only so v2's
    containment can be unit-tested without importing v1.
    """
    if not outer:
        return True
    if not inner:
        return False
    return set(inner).issubset(set(outer))


def _scope_contains(outer: Scope, inner: Scope) -> bool:
    if outer.is_global():
        return True
    if outer.cloud and inner.cloud != outer.cloud:
        return False
    if outer.account_ref_id is not None and inner.account_ref_id != outer.account_ref_id:
        return False
    if not _list_contains(outer.regions, inner.regions):
        return False
    if not _list_contains(outer.services, inner.services):
        return False
    if not _list_contains(outer.resource_groups, inner.resource_groups):
        return False
    if not _list_contains(outer.resource_ids, inner.resource_ids):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────
# Introspection
# ─────────────────────────────────────────────────────────────────────

def explain(user: dict, permission: str, target: Optional[Target] = None) -> dict:
    """
    Why was this allowed or denied. Powers an "explain access" admin
    view and, more importantly, makes support tickets answerable
    without reading the database by hand. Every mature authorization
    system needs this; its absence is why "why can't Priya see prod"
    turns into an afternoon.
    """
    access = resolve(user)
    target = target or Target()

    for d in access.denials:
        if d.permission_code == permission and (d.scope is None or d.scope.covers(target)):
            return {
                "allowed": False,
                "reason": "explicit_deny",
                "detail": f"An explicit deny for {permission} covers this target.",
            }

    matched = [
        b for b in access.bindings
        if permission in b.permissions and b.scope.covers(target)
    ]
    if matched:
        b = matched[0]
        via = (f"group '{b.group_name}' ({b.group_level})"
               if b.source == "group" else "a direct binding")
        return {
            "allowed": True,
            "reason": "binding",
            "detail": f"Role '{b.role_key}' at scope "
                      f"'{b.scope.label or b.scope.id}' via {via}.",
            "binding_id": b.binding_id,
            "role": b.role_key,
            "scope_id": b.scope.id,
        }

    holds_anywhere = permission in access.all_permissions()
    return {
        "allowed": False,
        "reason": "out_of_scope" if holds_anywhere else "no_permission",
        "detail": (
            f"Holds {permission}, but no binding's scope covers this target."
            if holds_anywhere else
            f"No role bound to this user grants {permission}."
        ),
    }


def serialize_access(user: dict) -> dict:
    """Shape returned by GET /api/auth/me and the access-management UI."""
    access = resolve(user)
    return {
        "user_id": access.user_id,
        "global_admin": access.is_global_admin(),
        "permissions": sorted(access.all_permissions()),
        "bindings": [
            {
                "binding_id": b.binding_id,
                "role": b.role_key,
                "source": b.source,
                "group_id": b.group_id,
                "group_name": b.group_name,
                "group_level": b.group_level,
                "expires_at": b.expires_at,
                "scope": {
                    "id": b.scope.id,
                    "label": b.scope.label,
                    "cloud": b.scope.cloud,
                    "account_ref_id": b.scope.account_ref_id,
                    "regions": b.scope.regions,
                    "services": b.scope.services,
                    "resource_ids": b.scope.resource_ids,
                    "tag_selector": b.scope.tag_selector,
                },
            }
            for b in access.bindings
        ],
        "denials": [
            {"permission": d.permission_code,
             "scope_id": d.scope.id if d.scope else None}
            for d in access.denials
        ],
    }
