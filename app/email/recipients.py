# app/email/recipients.py
"""
Named recipient lists for the Email & Reports centre.

A list holds three kinds of member: CloudOps users (their CURRENT email is read when mail is sent, so a changed address
follows them), org groups (their active members), and typed addresses (people with no CloudOps user, shared mailboxes).
Inactive users and users with no email are skipped. When the mail is about one account, user and group members who may
not see that account are skipped too (group membership alone is not an account grant, the same tenant rule escalation
and the environment digest apply); typed addresses are the sender's own choice and always stay.

normalize_list() and parse_emails() are pure. resolve() reads the database.
"""
import json
import re

MAX_EMAILS = 50
MAX_IDS = 200
_EMAIL_RE = re.compile(r"^[^@\s,;<>\"'\\]+@[^@\s,;<>\"'\\]+\.[^@\s,;<>\"'\\]+$")


def parse_emails(value, limit=MAX_EMAILS):
    """List or comma / semicolon / newline separated text -> de-duplicated list (case-insensitive, first spelling kept).
    Raises ValueError naming the first bad address."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;\n]", str(value))
    out, seen = [], set()
    for raw in items:
        a = str(raw).strip()
        if not a:
            continue
        if len(a) > 254 or not _EMAIL_RE.match(a):
            raise ValueError(f"not a valid email address: {a[:60]}")
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    if len(out) > limit:
        raise ValueError(f"at most {limit} typed addresses")
    return out


def parse_ids(value, label):
    if value is None or value == "":
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of ids")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).strip().lstrip("-").isdigit():
            raise ValueError(f"{label} must contain only numeric ids")
        n = int(v)
        if n <= 0:
            raise ValueError(f"{label} must contain only positive ids")
        if n not in out:
            out.append(n)
    if len(out) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} entries in {label}")
    return out


def load_ids(text):
    """Stored JSON text -> list of ints. Bad or empty text -> []."""
    try:
        data = json.loads(text) if text else []
    except (TypeError, ValueError):
        return []
    return [int(x) for x in data if isinstance(x, int) and not isinstance(x, bool)] if isinstance(data, list) else []


def normalize_list(payload, current=None):
    """API payload (+ the stored row on update) -> the columns to write. Raises ValueError with a user-safe message."""
    cur = current or {}
    name = payload.get("name", cur.get("name"))
    name = (name or "").strip() if isinstance(name, str) or name is None else None
    if not name or len(name) > 100:
        raise ValueError("name is required (max 100 characters)")
    desc = payload.get("description", cur.get("description"))
    desc = (desc or "").strip() if isinstance(desc, str) or desc is None else None
    if desc is None or len(desc) > 255:
        raise ValueError("description must be text of at most 255 characters")
    user_ids = parse_ids(payload["user_ids"], "user_ids") if "user_ids" in payload else load_ids(cur.get("user_ids"))
    group_ids = parse_ids(payload["group_ids"], "group_ids") if "group_ids" in payload else load_ids(cur.get("group_ids"))
    if "emails" in payload:
        emails = parse_emails(payload["emails"])
    else:
        emails = parse_emails(cur.get("emails"))
    if not (user_ids or group_ids or emails):
        raise ValueError("a list needs at least one user, group or email address")
    return {"name": name, "description": desc or None,
            "user_ids": json.dumps(user_ids) if user_ids else None,
            "group_ids": json.dumps(group_ids) if group_ids else None,
            "emails": ", ".join(emails) if emails else None}


def _visible(user, account_id, accessible_fn):
    if account_id is None:
        return True
    try:
        accessible = accessible_fn({"id": user["id"], "role": user["role"]})
    except Exception:
        return False                       # cannot tell: do not send account data to them
    return accessible is None or int(account_id) in accessible


def resolve(row, extra_emails=None, account_id=None, cursor=None, accessible_fn=None):
    """-> {"addresses": [...], "skipped": [{"who": ..., "reason": ...}]}. `row` is a stored list row (or None, then only
    extra_emails count). `cursor` must be a dictionary cursor; one is opened when not given."""
    if accessible_fn is None:
        from app.auth.authorization import get_accessible_account_ids as accessible_fn
    user_ids = load_ids((row or {}).get("user_ids"))
    group_ids = load_ids((row or {}).get("group_ids"))
    typed = parse_emails((row or {}).get("emails")) + parse_emails(extra_emails)

    def work(cur):
        users = []
        if user_ids:
            cur.execute("SELECT id, username, role, email, COALESCE(active, 1) AS active FROM users WHERE id IN (%s)"
                        % ",".join(["%s"] * len(user_ids)), tuple(user_ids))
            users += cur.fetchall()
        if group_ids:
            cur.execute("SELECT DISTINCT u.id, u.username, u.role, u.email, COALESCE(u.active, 1) AS active "
                        "FROM user_group_memberships ugm JOIN users u ON u.id = ugm.user_id "
                        "WHERE ugm.group_id IN (%s)" % ",".join(["%s"] * len(group_ids)), tuple(group_ids))
            users += cur.fetchall()
        return users

    if cursor is not None:
        users = work(cursor) if (user_ids or group_ids) else []
    elif user_ids or group_ids:
        from app.db import get_db_cursor
        with get_db_cursor(dictionary=True, commit=False) as (_, cur):
            users = work(cur)
    else:
        users = []

    addresses, skipped, seen = [], [], set()

    def add(addr):
        if addr.lower() not in seen:
            seen.add(addr.lower())
            addresses.append(addr)

    done = set()
    for u in users:
        if u["id"] in done:
            continue
        done.add(u["id"])
        who = u.get("username") or f"user {u['id']}"
        if not u.get("active"):
            skipped.append({"who": who, "reason": "user is inactive"})
        elif not (u.get("email") or "").strip():
            skipped.append({"who": who, "reason": "no email on file"})
        elif not _visible(u, account_id, accessible_fn):
            skipped.append({"who": who, "reason": "may not see this account"})
        else:
            add(u["email"].strip())
    for a in typed:
        add(a)
    return {"addresses": addresses, "skipped": skipped}
