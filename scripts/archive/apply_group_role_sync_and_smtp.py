#!/usr/bin/env python3
"""
apply_group_role_sync_and_smtp.py — two features:

1. GROUP-LEVEL ROLE SYNC. Assigning a user to an organization group
   now sets their role automatically based on the group's level:
     L1 -> Viewer (read-only)
     L2 -> Editor (view + configure alerts)
     L3 -> Admin  (full access)
   This is applied server-side in POST /api/groups/{id}/members
   (app/api/admin/groups.py) — the single authoritative place group
   membership is granted — so it can't be bypassed by calling the
   endpoint directly, and it's applied to every user_id passed, not
   just newly-added ones, so re-running a membership call reconciles
   drift too. The Add User modal's Role dropdown now visibly locks to
   the group's implied role the moment a group is picked (and
   re-enables for manual choice when "No group" is selected), so the
   UI never shows a Role that's about to be silently overridden.

2. SMTP MAIL. New app/email/mailer.py (stdlib smtplib, no new pip
   dependency) sends:
     - A welcome email with a set-your-password link when a new user
       is created with an email on file (reuses the SAME
       password_reset_tokens flow as forgot-password — never emails a
       raw password).
     - The actual reset link on POST /api/auth/forgot-password,
       replacing the "return the token directly in the API response"
       fallback that was an explicit placeholder for this (see the
       comment that was already in app/api/auth.py).
   Configured entirely via .env (SMTP_HOST/PORT/USERNAME/PASSWORD/
   USE_TLS, MAIL_FROM, PUBLIC_APP_URL) — matching this app's existing
   env-var config convention rather than adding a DB-backed settings
   table. Leaving SMTP_HOST unset disables mail entirely and every
   caller falls back to its pre-mail behavior; nothing breaks if mail
   isn't configured.

   Adds one migration: db/migrations/014_user_email_column.sql (a
   nullable `email` column on `users` — required to know where to
   send anything). The Add User modal gains an optional Email field.

IMPORTANT: default MAIL_FROM is set to cloudops@aurionpro.com — if
that's not the exact address, edit MAIL_FROM in .env after applying
(one line, no need to re-run this script).

Usage:
    python apply_group_role_sync_and_smtp.py --dry-run
    python apply_group_role_sync_and_smtp.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-group-role-sync-and-smtp"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# New files
# ─────────────────────────────────────────────────────────────────────────
MIGRATION_SQL = r'''-- db/migrations/014_user_email_column.sql
--
-- Adds an optional email address per user, needed to actually send
-- mail (welcome emails on account creation, password-reset links --
-- see app/email/mailer.py). Nullable: existing users have none on
-- file and nothing breaks for them; they simply don't receive email
-- until an admin sets one. No UI for editing an EXISTING user's email
-- yet in this pass -- only settable at creation time.

ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL AFTER username;
'''

MAILER_PY = r'''# app/email/mailer.py
"""
app/email/mailer.py

Minimal SMTP mail sender, stdlib-only (smtplib + email.mime) -- no new
pip dependency required. Configured entirely via environment
variables, matching this app's existing convention (DB_HOST,
JWT_SECRET, VM_URL, ...) of env-var config rather than a DB-backed
settings table.

Required env vars to actually send mail:
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, MAIL_FROM

Optional:
  SMTP_USE_TLS   ("true"/"false", default "true" -- STARTTLS on the
                  normal submission port 587; set to "false" only if
                  using implicit TLS on port 465 instead)
  PUBLIC_APP_URL (used to build links in email bodies, e.g. the
                  password-reset link; default "http://localhost" --
                  set this to the server's real public URL)

If SMTP_HOST is unset, is_configured() returns False and send_email()
is a safe no-op that logs a warning and returns False. Every caller in
this app is written to fall back to its pre-mail behavior (e.g.
returning a reset token directly in the API response instead of
emailing it) rather than break when mail isn't configured -- the same
degrade-gracefully-never-crash pattern already used for VM/YACE
metrics elsewhere in this app.
"""
import logging
import os
import smtplib
import ssl
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def get_public_app_url() -> str:
    return os.getenv("PUBLIC_APP_URL", "http://localhost").rstrip("/")


def send_email(to_addr: str, subject: str, body_text: str) -> bool:
    """
    Sends a plain-text email. Returns True on success, False if SMTP
    isn't configured or the send failed (always logged, never raises
    -- callers should treat a False return as "email not sent, fall
    back to your existing non-email behavior", not as an error to
    surface to the end user).
    """
    if not is_configured():
        logger.warning(
            f"Mail not sent to {to_addr!r} (subject={subject!r}) -- SMTP_HOST is not set. "
            "Configure SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD/MAIL_FROM in .env to enable email."
        )
        return False

    host      = os.getenv("SMTP_HOST")
    port      = int(os.getenv("SMTP_PORT", "587"))
    username  = os.getenv("SMTP_USERNAME", "")
    password  = os.getenv("SMTP_PASSWORD", "")
    use_tls   = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
    mail_from = os.getenv("MAIL_FROM", username or "cloudops@aurionpro.com")

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = mail_from
    msg["To"]      = to_addr

    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls(context=ssl.create_default_context())
                if username:
                    server.login(username, password)
                server.sendmail(mail_from, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as server:
                if username:
                    server.login(username, password)
                server.sendmail(mail_from, [to_addr], msg.as_string())
        logger.info(f"Mail sent to {to_addr!r} (subject={subject!r})")
        return True
    except Exception as e:
        logger.error(f"Mail send failed to {to_addr!r} (subject={subject!r}): {e}")
        return False
'''

EMAIL_INIT_PY = "# app/email/__init__.py\n"

NEW_FILES = [
    ("db/migrations/014_user_email_column.sql", MIGRATION_SQL),
    ("app/email/__init__.py", EMAIL_INIT_PY),
    ("app/email/mailer.py", MAILER_PY),
]

PATCHES = [
    (
        "app/auth/authorization.py",
        [(r'''GROUP_LEVELS = ("L1", "L2", "L3")
# What level a group's parent MUST be, keyed by the child's own level.
# L1 -> None (top of the tree, no parent allowed).
GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}
''', r'''GROUP_LEVELS = ("L1", "L2", "L3")
# What level a group's parent MUST be, keyed by the child's own level.
# L1 -> None (top of the tree, no parent allowed).
GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}

# The role a user is given automatically when added to a group at each
# level -- L1 members get read-only Viewer access, L2 get Editor
# (configure alerts, onboard accounts), L3 get full Admin. Assigning a
# user to a group is now the single action that sets BOTH their scope
# (inherited group policy) and their role tier in one step, instead of
# the two being picked independently on the Add User form and
# potentially disagreeing (e.g. an L3 group member left as a Viewer
# by mistake).
GROUP_LEVEL_ROLE = {"L1": "viewer", "L2": "editor", "L3": "admin"}
''')],
    ),
    (
        "app/api/admin/groups.py",
        [(r'''    cursor = conn.cursor()
    added, already = [], []
    for uid in user_ids:
        try:
            cursor.execute(
                "INSERT INTO user_group_memberships (user_id, group_id, assigned_by) VALUES (%s, %s, %s)",
                (uid, group_id, current_user["id"]),
            )
            added.append(uid)
        except Exception as e:
            if "Duplicate" in str(e) or "1062" in str(e):
                already.append(uid)
            else:
                conn.rollback()
                cursor.close()
                conn.close()
                raise HTTPException(status_code=500, detail=str(e))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(
        current_user["username"], "Group membership added",
        f"{g['name']}: +{len(added)} user(s)" + (f", {len(already)} already member" if already else ""),
    )
    return {"status": "updated", "group_id": group_id, "added": added, "already_member": already}
''', r'''    cursor = conn.cursor()
    added, already = [], []
    for uid in user_ids:
        try:
            cursor.execute(
                "INSERT INTO user_group_memberships (user_id, group_id, assigned_by) VALUES (%s, %s, %s)",
                (uid, group_id, current_user["id"]),
            )
            added.append(uid)
        except Exception as e:
            if "Duplicate" in str(e) or "1062" in str(e):
                already.append(uid)
            else:
                conn.rollback()
                cursor.close()
                conn.close()
                raise HTTPException(status_code=500, detail=str(e))

    # Group level sets the member's role tier -- L1 members become
    # Viewer, L2 Editor, L3 Admin (see authz.GROUP_LEVEL_ROLE). This is
    # what makes "which group is this person in" the single source of
    # truth for both scope (via get_effective_scope) and capability
    # tier, instead of the two being picked independently and
    # potentially disagreeing. Applied to every id in user_ids, not
    # just newly-inserted ones, so re-adding an existing member still
    # reconciles their role if it had drifted.
    synced_role = authz.GROUP_LEVEL_ROLE.get(g["level"])
    if synced_role:
        cursor.execute(
            f"UPDATE users SET role = %s WHERE id IN ({placeholders})",
            (synced_role, *user_ids),
        )

    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(
        current_user["username"], "Group membership added",
        f"{g['name']}: +{len(added)} user(s)" + (f", {len(already)} already member" if already else "")
        + (f" -- role synced to {synced_role}" if synced_role else ""),
    )
    return {"status": "updated", "group_id": group_id, "added": added, "already_member": already, "role_synced": synced_role}
''')],
    ),
    (
        "app/api/admin/users.py",
        [
            (r'''from app.auth import authorization as authz
import bcrypt
import datetime
import json
''', r'''from app.auth import authorization as authz
from app.email import mailer
import bcrypt
import datetime
import json
import secrets
'''),
            (r'''    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    role     = (payload.get("role") or "viewer").strip().lower()
    scopes   = payload.get("scopes") or []
''', r'''    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    role     = (payload.get("role") or "viewer").strip().lower()
    scopes   = payload.get("scopes") or []
    email    = (payload.get("email") or "").strip() or None
'''),
            (r'''    pw_hash = _hash_password(password)
    conn    = get_connection()
    cursor  = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO users (username, password, role) VALUES (%s, %s, %s)",
            (username, pw_hash, role)
        )
''', r'''    pw_hash = _hash_password(password)
    conn    = get_connection()
    cursor  = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO users (username, password, role, email) VALUES (%s, %s, %s, %s)",
            (username, pw_hash, role, email)
        )
'''),
            (r'''    _write_audit(
        actor=current_user["username"], action="User created",
        detail=f"{username} added as {role.upper()} with {len(scopes)} scope grant(s)",
    )
    return {"status": "created", "id": new_id, "username": username, "role": role, "scopes_granted": len(scopes)}
''', r'''    _write_audit(
        actor=current_user["username"], action="User created",
        detail=f"{username} added as {role.upper()} with {len(scopes)} scope grant(s)",
    )

    # Welcome email with a set-your-password link, not the raw
    # password -- reuses the exact same password_reset_tokens flow as
    # /api/auth/forgot-password rather than a separate mechanism, and
    # never puts a plaintext credential in an email body/inbox. A
    # no-op (logged, not raised) if SMTP isn't configured or the user
    # has no email on file -- account creation itself already
    # succeeded above and must not be undone by a mail failure.
    email_sent = False
    if email and mailer.is_configured():
        token      = secrets.token_urlsafe(32)
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=60 * 24)
        mail_conn  = get_connection()
        mail_cur   = mail_conn.cursor()
        mail_cur.execute(
            "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
            (new_id, token, expires_at),
        )
        mail_conn.commit()
        mail_cur.close()
        mail_conn.close()

        reset_link = f"{mailer.get_public_app_url()}/reset-password?token={token}"
        email_sent = mailer.send_email(
            to_addr=email,
            subject="Your CloudOps account has been created",
            body_text=(
                f"Hi {username},\n\n"
                f"An account has been created for you on CloudOps with the role: {role.upper()}.\n\n"
                f"Set your password (link valid 24 hours):\n{reset_link}\n\n"
                f"If you weren't expecting this, contact your CloudOps administrator.\n"
            ),
        )

    return {
        "status": "created", "id": new_id, "username": username, "role": role,
        "scopes_granted": len(scopes), "email_sent": email_sent,
    }
'''),
        ],
    ),
    (
        "app/api/auth.py",
        [
            (r'''from app.auth.security import create_access_token
from app.auth.deps import get_current_user, COOKIE_NAME
import bcrypt
import logging
import secrets
import json
from datetime import datetime, timedelta
''', r'''from app.auth.security import create_access_token
from app.auth.deps import get_current_user, COOKIE_NAME
from app.email import mailer
import bcrypt
import logging
import secrets
import json
from datetime import datetime, timedelta
'''),
            (r'''    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id FROM users WHERE username = %s AND active = 1",
        (username,),
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        # Same response either way so usernames can't be enumerated.
        return {"status": "ok", "message": "If that account exists, a reset token has been generated."}

    token      = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)

    cursor.execute(
        "DELETE FROM password_reset_tokens WHERE user_id = %s",
        (user["id"],),
    )
    cursor.execute(
        """
        INSERT INTO password_reset_tokens (user_id, token, expires_at)
        VALUES (%s, %s, %s)
        """,
        (user["id"], token, expires_at),
    )
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(username, "Password reset requested", {"username": username})

    return {
        "status":     "ok",
        "message":    "If that account exists, a reset token has been generated.",
        "token":      token,
        "expires_in_minutes": RESET_TOKEN_TTL_MINUTES,
    }
''', r'''    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, email FROM users WHERE username = %s AND active = 1",
        (username,),
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        # Same response either way so usernames can't be enumerated.
        return {"status": "ok", "message": "If that account exists, a reset token has been generated."}

    token      = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)

    cursor.execute(
        "DELETE FROM password_reset_tokens WHERE user_id = %s",
        (user["id"],),
    )
    cursor.execute(
        """
        INSERT INTO password_reset_tokens (user_id, token, expires_at)
        VALUES (%s, %s, %s)
        """,
        (user["id"], token, expires_at),
    )
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(username, "Password reset requested", {"username": username})

    # Email the link when SMTP + an address on file are both available;
    # otherwise fall back to the original behavior (return the token
    # directly in the response) rather than leave the user stuck with
    # no way to reset at all -- this is exactly the swap-in point the
    # original docstring/comment on this endpoint called for.
    if user.get("email") and mailer.is_configured():
        reset_link = f"{mailer.get_public_app_url()}/reset-password?token={token}"
        sent = mailer.send_email(
            to_addr=user["email"],
            subject="CloudOps password reset",
            body_text=(
                f"A password reset was requested for the account '{username}'.\n\n"
                f"Reset your password (link valid {RESET_TOKEN_TTL_MINUTES} minutes):\n{reset_link}\n\n"
                f"If you didn't request this, you can ignore this email.\n"
            ),
        )
        if sent:
            return {
                "status":  "ok",
                "message": "If that account exists, a reset link has been emailed to it.",
                "expires_in_minutes": RESET_TOKEN_TTL_MINUTES,
            }

    return {
        "status":     "ok",
        "message":    "If that account exists, a reset token has been generated.",
        "token":      token,
        "expires_in_minutes": RESET_TOKEN_TTL_MINUTES,
    }
'''),
        ],
    ),
    (
        "frontend/src/pages/UserManagement.jsx",
        [
            (r'''const INITIAL_FORM = { username: "", password: "", role: "viewer", accountIds: [], groupId: "" };
''', r'''const INITIAL_FORM = { username: "", password: "", email: "", role: "viewer", accountIds: [], groupId: "" };

// Mirrors app/auth/authorization.py's GROUP_LEVEL_ROLE exactly -- the
// role a user is given automatically when assigned to a group at each
// level. Kept in sync here purely so the Role dropdown can show/lock
// to the right value the instant a group is picked, without waiting
// on a round trip; the backend applies the same mapping authoritatively
// when the membership is actually created, so this can never drift
// into being the source of truth.
const GROUP_LEVEL_ROLE = { L1: "viewer", L2: "editor", L3: "admin" };
'''),
            (r'''      const created = await apiFetch("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username: form.username.trim(),
          password: form.password,
          role:     form.role,
        }),
      });
''', r'''      const created = await apiFetch("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username: form.username.trim(),
          password: form.password,
          role:     form.role,
          email:    form.email.trim() || undefined,
        }),
      });
'''),
            (r'''              <div className={`mfield ${formErrs.password ? "merr" : ""}`}>
                <label>Password *</label>
                <input
                  type="password"
                  value={form.password}
                  onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                  placeholder="Min 6 characters"
                  autoComplete="new-password"
                />
                {formErrs.password && <span className="err-msg">{formErrs.password}</span>}
              </div>
              <div className="mfield">
                <label>Role</label>
                <select
                  value={form.role}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value, accountIds: [] }))}
                >
                  <option value="viewer">Viewer — read-only</option>
                  <option value="editor">Editor — view + configure alerts</option>
                  <option value="admin">Admin — full access</option>
                </select>
              </div>
              <div className="mfield">
                <label>Group (optional)</label>
                <select
                  value={form.groupId}
                  onChange={e => setForm(f => ({ ...f, groupId: e.target.value }))}
                >
                  <option value="">No group</option>
                  {groups.map(g => (
                    <option key={g.id} value={g.id}>
                      {"— ".repeat(g.level === "L3" ? 2 : g.level === "L2" ? 1 : 0)}{g.name} ({g.level})
                    </option>
                  ))}
                </select>
                <span className="field-hint">
                  {groups.length === 0
                    ? "No organization groups set up yet."
                    : "Inherits this group's access, plus every parent group's access."}
                </span>
              </div>
''', r'''              <div className={`mfield ${formErrs.password ? "merr" : ""}`}>
                <label>Password *</label>
                <input
                  type="password"
                  value={form.password}
                  onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                  placeholder="Min 6 characters"
                  autoComplete="new-password"
                />
                {formErrs.password && <span className="err-msg">{formErrs.password}</span>}
              </div>
              <div className="mfield">
                <label>Email (optional)</label>
                <input
                  type="email"
                  value={form.email}
                  onChange={e => setForm(f => ({ ...f, email: e.target.value }))}
                  placeholder="e.g. john.doe@aurionpro.com"
                  autoComplete="off"
                />
                <span className="field-hint">If SMTP is configured, a set-your-password link is emailed here.</span>
              </div>
              <div className="mfield">
                <label>Group (optional)</label>
                <select
                  value={form.groupId}
                  onChange={e => {
                    const groupId = e.target.value;
                    const selected = groups.find(g => String(g.id) === groupId);
                    const impliedRole = selected ? GROUP_LEVEL_ROLE[selected.level] : null;
                    setForm(f => ({
                      ...f,
                      groupId,
                      role: impliedRole || f.role,
                      accountIds: impliedRole ? [] : f.accountIds,
                    }));
                  }}
                >
                  <option value="">No group</option>
                  {groups.map(g => (
                    <option key={g.id} value={g.id}>
                      {"— ".repeat(g.level === "L3" ? 2 : g.level === "L2" ? 1 : 0)}{g.name} ({g.level})
                    </option>
                  ))}
                </select>
                <span className="field-hint">
                  {groups.length === 0
                    ? "No organization groups set up yet."
                    : "Sets the role below automatically (L1 = Viewer, L2 = Editor, L3 = Admin) and inherits this group's access plus every parent group's access."}
                </span>
              </div>
              <div className="mfield">
                <label>Role{form.groupId ? " (set by group)" : ""}</label>
                <select
                  value={form.role}
                  disabled={!!form.groupId}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value, accountIds: [] }))}
                >
                  <option value="viewer">Viewer — read-only</option>
                  <option value="editor">Editor — view + configure alerts</option>
                  <option value="admin">Admin — full access</option>
                </select>
                {form.groupId && (
                  <span className="field-hint">
                    Role is locked to this group's level. Choose "No group" above to set a role manually instead.
                  </span>
                )}
              </div>
'''),
        ],
    ),
    (
        ".env.production.example",
        [(r'''CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173''', r'''CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

# ── Outbound mail (optional) ──────────────────────────────────────
# Welcome emails (set-your-password link) on user creation, and the
# password-reset link on /api/auth/forgot-password, are both sent via
# this if SMTP_HOST is set. Leave SMTP_HOST empty/unset to disable
# email entirely — every mail-sending code path falls back to its
# pre-mail behavior (e.g. returning the reset token directly in the
# API response) rather than breaking, so this is safe to leave unset.
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
# true = STARTTLS on port 587 (typical). false = implicit TLS, port 465.
SMTP_USE_TLS=true
MAIL_FROM=cloudops@aurionpro.com
# The server's real public URL, used to build the link inside emails.
PUBLIC_APP_URL=http://13.200.102.131
''')],
    ),
]

# ─────────────────────────────────────────────────────────────────────────
# Preflight / apply / validate
# ─────────────────────────────────────────────────────────────────────────

def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            print(f"  (already exists, will skip creating) {rel_path}")
        else:
            print(f"  OK  {rel_path}: will be created")

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = (
            all(_already(rel, content) for rel, content in NEW_FILES)
            and all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        )
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            continue
        if dry_run:
            report.append(f"[DRY RUN] would create: {rel_path}")
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            report.append(f"CREATED: {rel_path}")
            changed_files.append(full_path)

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue  # already patched
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for line in report:
        print(line)

    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext steps:")
            print("  1. Run the new migration:")
            print("     mysql -umonitor -proot123 monitoring_hub < db/migrations/014_user_email_column.sql")
            print("  2. Add SMTP_* vars to your REAL .env (this script only updates the")
            print("     .env.production.example TEMPLATE, never your live .env, on purpose —")
            print("     copy the new SMTP_* block from .env.production.example into your")
            print("     actual .env and .env.production, then fill in real credentials).")
            print("  3. cd frontend && npm install (if needed) && npm run build")
            print("  4. Full backend restart:")
            print("     sudo systemctl restart monitoring-hub")
            print("\nTo verify:")
            print("  - Add User -> pick a Group -> Role dropdown should auto-select and")
            print("    grey out to match the group's level.")
            print("  - Without SMTP configured: creating a user with an email still")
            print("    succeeds, 'email_sent' in the response is false, and a WARNING is")
            print("    logged (not an error) — nothing breaks.")
            print("  - With SMTP configured: that becomes true and mail actually arrives.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
